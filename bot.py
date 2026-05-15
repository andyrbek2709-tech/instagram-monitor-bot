import os
import re
import psycopg2
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (Application, CommandHandler, MessageHandler, ConversationHandler,
                          CallbackQueryHandler, filters, ContextTypes)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import random
import asyncio
from media_parser import MediaParser

logger = logging.getLogger(__name__)

ADD_ACCOUNT, CONFIRM_ACCOUNT, SELECT_NUM_POSTS, SELECT_INTERVAL = range(4)
WAIT_SESSIONID = 100
WAIT_URL = 101

IG_CDN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
    'Referer': 'https://www.instagram.com/',
}


def _resize_image_for_vision(img, max_side: int = 960):
    """Уменьшить кадр перед Vision — меньше полезной нагрузки и обычно быстрее ответ Gemini."""
    try:
        import PIL.Image

        w, h = img.size
        if w <= max_side and h <= max_side:
            return img
        scale = min(max_side / float(w), max_side / float(h))
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        try:
            resample = PIL.Image.Resampling.LANCZOS
        except AttributeError:
            resample = PIL.Image.LANCZOS  # type: ignore[attr-defined]
        return img.resize((nw, nh), resample)
    except Exception:
        return img


def _carousel_skip_video_when_has_image() -> bool:
    v = os.getenv('CAROUSEL_SKIP_VIDEO_WHEN_HAS_IMAGE', '1').strip().lower()
    return v in ('1', 'true', 'yes', 'on')


def _openai_response_text(response) -> str:
    """Текст ответа OpenAI chat.completions."""
    if hasattr(response, 'choices') and len(response.choices) > 0:
        msg = response.choices[0].message
        if hasattr(msg, 'content'):
            return (msg.content or '').strip()
    return str(response or '').strip()


def _openai_generate_with_retry(client, *, model: str, messages: List[Dict], max_retries: int = 7):
    """Вызов OpenAI chat.completions.create с паузами при 429 / quota."""
    last = None
    for attempt in range(max_retries):
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages
            )
        except Exception as e:
            last = e
            el = str(e).lower()
            if any(x in el for x in ('429', 'resource_exhausted', 'quota', 'rate limit', 'too many')):
                if attempt < max_retries - 1:
                    wait = min(120.0, (2 ** attempt) + random.uniform(0.5, 2.5))
                    logger.warning(
                        'OpenAI rate limit, sleep %.1fs (attempt %s/%s)',
                        wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    continue
            raise
    raise last


def _carousel_slide_block(sl: Dict, idx: int, openai_key: str) -> str:
    """Один слайд карусели: OCR картинки через OpenAI Vision API."""
    import io
    import os as _os
    import requests as _req
    import PIL.Image

    if not isinstance(sl, dict):
        return f'=== Слайд {idx} ===\n(некорректные данные слайда)'

    lines = [f'=== Слайд {idx} (из карусели) ===']
    acc = sl.get('accessibility_caption')
    if isinstance(acc, list):
        acc = '\n'.join(str(x) for x in acc)
    elif not isinstance(acc, str):
        acc = str(acc) if acc else ''
    acc = (acc or '').strip()
    if acc:
        lines.append(f'Подпись доступности Instagram:\n{acc}')

    try:
        delay_sec = float(os.getenv('CAROUSEL_OPENAI_DELAY_SEC', '1.2'))
    except ValueError:
        delay_sec = 1.2

    vision_prompt = (
        'Это кадр из карусели Instagram. '
        '1) Выпиши ВЕСЬ читаемый текст на кадре (OCR), дословно, построчно если нужно. '
        '2) Два коротких предложения — что изображено и зачем этот кадр в посте. '
        'Ответ на русском. Если текста на кадре нет — первая строка: «Текста на кадре нет».'
    )

    openai_client = OpenAI(api_key=openai_key)
    img_url = sl.get('image_url')
    if img_url:
        try:
            img_resp = _req.get(img_url, headers=IG_CDN_HEADERS, timeout=25)
            img_resp.raise_for_status()
            img = PIL.Image.open(io.BytesIO(img_resp.content))
            try:
                max_side = int(os.getenv('CAROUSEL_VISION_MAX_SIDE', '960'))
            except ValueError:
                max_side = 960
            img = _resize_image_for_vision(img, max_side=max(512, min(max_side, 2048)))
            
            # Конвертируем изображение в base64 для OpenAI Vision
            import base64
            buffered = io.BytesIO()
            img.save(buffered, format="JPEG")
            img_base64 = base64.b64encode(buffered.getvalue()).decode()
            
            vision_resp = _openai_generate_with_retry(
                openai_client,
                model='gpt-4o-mini',
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": vision_prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}
                            }
                        ]
                    }
                ],
                max_retries=4,
            )
            lines.append('Анализ кадра (OCR + описание):\n' + vision_resp.choices[0].message.content)
            time.sleep(delay_sec)
        except Exception as e:
            logger.warning(f'Carousel slide {idx} vision failed: {e}')
            lines.append(f'(не удалось разобрать картинку слайда: {str(e)[:160]})')

    vid_url = sl.get('video_url')
    if vid_url:
        # Для видео OpenAI не имеет прямого транскрипционного API в том же формате
        lines.append(f'(видео на слайде {idx}: временно пропущено при миграции на OpenAI)')

    return '\n'.join(lines)


def _build_carousel_slides_context(slides: List[Dict], openai_key: str, max_slides: int = 12) -> str:
    """OCR всех слайдов через OpenAI Vision (для фоновых вызовов)."""
    if not slides or not openai_key:
        return ''

    slides = [s for s in slides if isinstance(s, dict)]
    if not slides:
        return ''

    try:
        env_cap = int(os.getenv('CAROUSEL_OPENAI_MAX_SLIDES', '6'))
    except ValueError:
        env_cap = 6
    limit = max(1, min(len(slides), env_cap, max_slides))

    parts = [_carousel_slide_block(slides[i], i + 1, openai_key) for i in range(limit)]
    return '\n\n'.join(parts)


class DigestFormatter:
    """Форматирование дайджестов для Telegram"""

    @staticmethod
    def format_post_digest(analysis: Dict) -> str:
        """Форматировать один пост для дайджеста"""
        text = f"""
📊 *Анализ поста*

*Тематика:* {', '.join(analysis.get('key_topics', ['Общее']))}
*Sentiment:* {analysis.get('sentiment', 'нейтральный')}
*Релевантность:* {analysis.get('relevance_score', 0):.1%}
*Вирусный потенциал:* {analysis.get('viral_potential', 'низкий')}

*Рекомендации:*
"""
        for rec in analysis.get('recommendations', []):
            text += f"• {rec}\n"

        return text

    @staticmethod
    def format_daily_digest(analyses: List[Dict]) -> str:
        """Форматировать ежедневный дайджест"""
        if not analyses:
            return "Нет новых постов за последние 24 часа"

        avg_relevance = sum(a.get('relevance_score', 0) for a in analyses) / len(analyses)
        high_relevance = len([a for a in analyses if a.get('relevance_score', 0) > 0.7])
        viral_count = len([a for a in analyses if a.get('viral_potential') == 'high'])

        sentiments = {}
        for a in analyses:
            s = a.get('sentiment', 'neutral')
            sentiments[s] = sentiments.get(s, 0) + 1

        topics = {}
        for a in analyses:
            for t in a.get('key_topics', []):
                topics[t] = topics.get(t, 0) + 1

        top_topics = sorted(topics.items(), key=lambda x: x[1], reverse=True)[:3]

        text = f"""
📈 *Ежедневный дайджест Instagram*

*Статистика:*
• Всего постов: {len(analyses)}
• Средняя релевантность: {avg_relevance:.1%}
• Высокорелевантных (>70%): {high_relevance}
• Вирусный потенциал (High): {viral_count}

*Sentiment распределение:*
"""
        for sentiment, count in sentiments.items():
            text += f"• {sentiment.capitalize()}: {count}\n"

        if top_topics:
            text += "\n*Топ тематики:*\n"
            for topic, count in top_topics:
                text += f"• {topic}: {count}\n"

        return text

    @staticmethod
    def format_account_stats(username: str, post_count: int) -> str:
        """Форматировать статистику аккаунта"""
        return f"""
👤 *Аккаунт: {username}*
• Постов проанализировано: {post_count}
• Дата добавления: {datetime.now().strftime('%d.%m.%Y')}
"""


class TelegramBot:
    """Telegram бот с обработчиками команд"""

    def __init__(self, token: str, db_url: str, parser=None, filter_obj=None, analyzer=None):
        self.token = token
        self.db_url = db_url
        self.application = None
        self.parser = parser
        self.filter = filter_obj
        self.analyzer = analyzer
        self.instagram_username = os.getenv('INSTAGRAM_USERNAME')
        self.instagram_password = os.getenv('INSTAGRAM_PASSWORD')
        self.media_parser = MediaParser()  # Для YouTube, TikTok

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обработчик /start"""
        keyboard = [
            [InlineKeyboardButton("🔗 Разобрать пост по ссылке", callback_data='analyze_url')],
            [InlineKeyboardButton("🚀 Начать парсинг аккаунта", callback_data='start_parsing')],
            [InlineKeyboardButton("➕ Добавить аккаунт", callback_data='add_account')],
            [InlineKeyboardButton("📋 Мои аккаунты", callback_data='list_accounts')],
            [InlineKeyboardButton("📊 Получить дайджест", callback_data='get_digest')],
            [InlineKeyboardButton("📈 Статистика", callback_data='stats')],
            [InlineKeyboardButton("⚙️ Настройки", callback_data='settings')],
            [InlineKeyboardButton("❓ Справка", callback_data='help')]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "👋 Добро пожаловать в Instagram Monitor Bot!\n\n"
            "Я помогу вам отслеживать и анализировать посты Instagram.",
            reply_markup=reply_markup
        )

    async def add_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Добавить аккаунт (начало разговора)"""
        query = update.callback_query
        await query.answer()

        await query.edit_message_text(
            "Введите имя пользователя Instagram для мониторинга:"
        )

        return ADD_ACCOUNT

    async def confirm_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Подтвердить имя аккаунта и выбрать количество постов"""
        username = update.message.text.strip()
        context.user_data['account_username'] = username
        context.user_data['user_id'] = update.message.from_user.id

        keyboard = [
            [InlineKeyboardButton("5 постов (быстро)", callback_data='posts_5')],
            [InlineKeyboardButton("10 постов (стандарт)", callback_data='posts_10')],
            [InlineKeyboardButton("20 постов (много)", callback_data='posts_20')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"📱 Аккаунт *@{username}*\n\n"
            "Сколько последних постов парсить?",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        return SELECT_NUM_POSTS

    async def select_num_posts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Выбрать количество постов"""
        query = update.callback_query
        await query.answer()

        num_posts_map = {
            'posts_5': 5,
            'posts_10': 10,
            'posts_20': 20
        }

        num_posts = num_posts_map.get(query.data, 10)
        context.user_data['num_posts'] = num_posts

        keyboard = [
            [InlineKeyboardButton("⏰ Каждый день", callback_data='interval_24')],
            [InlineKeyboardButton("🔔 Каждые 6 часов", callback_data='interval_6')],
            [InlineKeyboardButton("⚡ Каждый час", callback_data='interval_1')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"⏱️ Как часто проверять?\n\n"
            f"_(Выбрано: {num_posts} постов)_",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        return SELECT_INTERVAL

    async def select_interval(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Выбрать интервал проверки"""
        query = update.callback_query
        await query.answer()

        interval_map = {
            'interval_1': 1,
            'interval_6': 6,
            'interval_24': 24
        }

        interval_hours = interval_map.get(query.data, 24)
        username = context.user_data.get('account_username')
        num_posts = context.user_data.get('num_posts', 10)
        user_id = context.user_data.get('user_id')

        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            next_check = datetime.utcnow() + timedelta(hours=interval_hours)

            cursor.execute(
                '''INSERT INTO monitored_accounts
                   (user_id, username, num_posts, check_interval_hours, next_check, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (username) DO NOTHING''',
                (user_id, username, num_posts, interval_hours, next_check.isoformat(),
                 datetime.utcnow().isoformat())
            )

            cursor.execute(
                'INSERT INTO telegram_users (user_id, username, created_at) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING',
                (user_id, update.effective_user.username or 'unknown', datetime.utcnow().isoformat())
            )
            conn.commit()
            cursor.close()
            conn.close()

            interval_text = {1: "каждый час", 6: "каждые 6 часов", 24: "каждый день"}.get(interval_hours)
            await query.edit_message_text(
                f"✅ *Аккаунт добавлен!*\n\n"
                f"@{username}\n"
                f"📊 Постов: {num_posts}\n"
                f"⏱️ Интервал: {interval_text}",
                parse_mode='Markdown'
            )
            logger.info(f"Account added: {username} ({num_posts} posts, {interval_hours}h interval)")

        except Exception as e:
            logger.error(f"Error adding account: {e}")
            await query.edit_message_text("❌ Ошибка при добавлении аккаунта")

        return ConversationHandler.END

    async def list_accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Список отслеживаемых аккаунтов"""
        query = update.callback_query
        await query.answer()

        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            cursor.execute('SELECT username, last_fetch FROM monitored_accounts')
            accounts = cursor.fetchall()
            cursor.close()
            conn.close()

            if accounts:
                text = "📱 *Отслеживаемые аккаунты:*\n\n"
                for username, last_fetch in accounts:
                    text += f"• @{username}\n"
            else:
                text = "Нет отслеживаемых аккаунтов"

            await query.edit_message_text(text)
        except Exception as e:
            logger.error(f"Error listing accounts: {e}")
            await query.edit_message_text("❌ Ошибка при получении аккаунтов")

    async def get_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Получить дневной дайджест"""
        query = update.callback_query
        await query.answer()

        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()

            since = datetime.utcnow() - timedelta(hours=24)
            cursor.execute('''
                SELECT a.sentiment, a.key_topics, a.relevance_score,
                       a.viral_potential, a.recommendations
                FROM analyses a
                WHERE a.analyzed_at > %s
                ORDER BY a.relevance_score DESC NULLS LAST
            ''', (since.isoformat(),))

            rows = cursor.fetchall()

            analyses = []
            for row in rows:
                analyses.append({
                    'sentiment': row[0] or 'neutral',
                    'key_topics': json.loads(row[1]) if row[1] else [],
                    'relevance_score': row[2] or 0,
                    'viral_potential': row[3] or 'low',
                    'recommendations': json.loads(row[4]) if row[4] else []
                })

            if analyses:
                digest_text = DigestFormatter.format_daily_digest(analyses)
            else:
                # Нет аналитики — показать сырые посты
                cursor.execute('''
                    SELECT p.caption, p.url, p.fetched_at, ma.username
                    FROM posts p
                    JOIN monitored_accounts ma ON p.account_id = ma.id
                    WHERE p.fetched_at > %s
                    ORDER BY p.fetched_at DESC
                    LIMIT 10
                ''', (since.isoformat(),))
                post_rows = cursor.fetchall()

                if post_rows:
                    digest_text = "📋 *Посты за последние 24 часа*\n_(AI-анализ ещё не выполнен)_\n\n"
                    for i, (caption, url, fetched_at, username) in enumerate(post_rows, 1):
                        cap = (caption[:100] + '…') if caption and len(caption) > 100 else (caption or '_(без подписи)_')
                        digest_text += f"*{i}. @{username}*\n{cap}\n"
                        if url:
                            digest_text += f"{url}\n"
                        digest_text += "\n"
                else:
                    digest_text = "Нет новых постов за последние 24 часа"

            cursor.close()
            conn.close()

            try:
                await query.edit_message_text(digest_text, parse_mode='Markdown')
            except Exception:
                await query.edit_message_text(digest_text)
        except Exception as e:
            logger.error(f"Error getting digest: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Ошибка дайджеста: {type(e).__name__}: {str(e)[:200]}")

    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Панель настроек"""
        query = update.callback_query
        await query.answer()

        settings_text = """⚙️ *НАСТРОЙКИ БОТА*

Текущие параметры:
• Время дайджеста: 09:00 (МСК)
• Минимальная релевантность: 0.5
• Интервал проверки: каждый час
• Часовой пояс: Europe/Moscow

Что хотите изменить?"""

        keyboard = [
            [InlineKeyboardButton("🔔 Время дайджеста", callback_data='digest_time')],
            [InlineKeyboardButton("📏 Минимальная релевантность", callback_data='min_relevance')],
            [InlineKeyboardButton("🔙 Назад", callback_data='back')]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(settings_text, reply_markup=reply_markup, parse_mode='Markdown')

    async def settings_digest_time(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Настройка времени дайджеста"""
        query = update.callback_query
        await query.answer()

        keyboard = [
            [InlineKeyboardButton("08:00", callback_data='set_digest_08'),
             InlineKeyboardButton("09:00", callback_data='set_digest_09'),
             InlineKeyboardButton("10:00", callback_data='set_digest_10')],
            [InlineKeyboardButton("🔙 Назад", callback_data='settings')]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🔔 *Время отправки ежедневного дайджеста:*",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def settings_min_relevance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Настройка минимальной релевантности"""
        query = update.callback_query
        await query.answer()

        keyboard = [
            [InlineKeyboardButton("0.3 (низкий)", callback_data='set_rel_03'),
             InlineKeyboardButton("0.5 (средний)", callback_data='set_rel_05')],
            [InlineKeyboardButton("0.7 (высокий)", callback_data='set_rel_07'),
             InlineKeyboardButton("0.9 (очень высокий)", callback_data='set_rel_09')],
            [InlineKeyboardButton("🔙 Назад", callback_data='settings')]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "📏 *Минимальный порог релевантности для дайджеста:*\n\n"
            "Выше порог = только наиболее релевантные посты",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def back_to_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Вернуться в главное меню"""
        query = update.callback_query
        await query.answer()

        keyboard = [
            [InlineKeyboardButton("🔗 Разобрать пост по ссылке", callback_data='analyze_url')],
            [InlineKeyboardButton("🚀 Начать парсинг аккаунта", callback_data='start_parsing')],
            [InlineKeyboardButton("➕ Добавить аккаунт", callback_data='add_account')],
            [InlineKeyboardButton("📋 Мои аккаунты", callback_data='list_accounts')],
            [InlineKeyboardButton("📊 Получить дайджест", callback_data='get_digest')],
            [InlineKeyboardButton("📈 Статистика", callback_data='stats')],
            [InlineKeyboardButton("⚙️ Настройки", callback_data='settings')],
            [InlineKeyboardButton("❓ Справка", callback_data='help')]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "👋 *Добро пожаловать в Instagram Monitor Bot!*\n\n"
            "Я помогу вам отслеживать и анализировать посты Instagram.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def get_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Получить статистику"""
        query = update.callback_query
        await query.answer()

        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()

            cursor.execute('SELECT COUNT(*) FROM posts')
            total_posts = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM monitored_accounts WHERE is_active = 1')
            active_accounts = cursor.fetchone()[0]

            cursor.execute('SELECT AVG(relevance_score) FROM analyses')
            avg_relevance = cursor.fetchone()[0] or 0

            cursor.execute('''
                SELECT COUNT(*) FROM analyses
                WHERE relevance_score > 0.7
                AND analyzed_at > NOW() - INTERVAL '7 days'
            ''')
            high_relevance_7d = cursor.fetchone()[0]

            cursor.execute('''
                SELECT COUNT(*) FROM analyses
                WHERE viral_potential = 'high'
                AND analyzed_at > NOW() - INTERVAL '7 days'
            ''')
            viral_7d = cursor.fetchone()[0]

            cursor.close()
            conn.close()

            stats_text = f"""
📊 *Статистика бота*

*Общие показатели:*
• Всего постов обработано: {total_posts}
• Активных аккаунтов: {active_accounts}
• Средняя релевантность: {avg_relevance:.1%}

*За последние 7 дней:*
• Высокорелевантных (>70%): {high_relevance_7d}
• Вирусных постов: {viral_7d}

Обновляется каждый день в 09:00
"""
            await query.edit_message_text(stats_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            await query.edit_message_text("❌ Ошибка при получении статистики")

    async def pause_monitoring(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Пауза мониторинга"""
        query = update.callback_query
        await query.answer()

        try:
            user_id = update.effective_user.id
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            settings = json.dumps({"paused": True, "paused_at": datetime.utcnow().isoformat()})
            cursor.execute(
                'UPDATE telegram_users SET settings = %s WHERE user_id = %s',
                (settings, user_id)
            )
            conn.commit()
            cursor.close()
            conn.close()

            keyboard = [
                [InlineKeyboardButton("▶️ Возобновить", callback_data='resume')],
                [InlineKeyboardButton("🔙 Назад в меню", callback_data='back')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                "⏸️ *Мониторинг приостановлен*\n\n"
                "Все проверки остановлены.\n"
                "Возобновите в любой момент.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error pausing monitoring: {e}")
            await query.edit_message_text("❌ Ошибка при приостановке")

    async def resume_monitoring(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Возобновить мониторинг с показом активных аккаунтов"""
        query = update.callback_query
        await query.answer()

        try:
            user_id = update.effective_user.id

            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, username, last_fetch, is_active
                FROM monitored_accounts
                WHERE user_id = %s OR (user_id IS NULL)
                ORDER BY is_active DESC, last_fetch DESC
            ''', (user_id,))
            accounts = cursor.fetchall()

            settings = json.dumps({"paused": False, "resumed_at": datetime.utcnow().isoformat()})
            cursor.execute(
                'UPDATE telegram_users SET settings = %s WHERE user_id = %s',
                (settings, user_id)
            )
            conn.commit()
            cursor.close()
            conn.close()

            # Построить список активных аккаунтов
            if accounts:
                text = "▶️ *Мониторинг возобновлен*\n\n"
                text += "*Отслеживаемые аккаунты:*\n\n"

                active_count = 0
                for acc_id, username, last_fetch, is_active in accounts:
                    if is_active:
                        icon = "🟢"
                        active_count += 1

                        # Форматировать время
                        if last_fetch:
                            last_fetch_dt = datetime.fromisoformat(last_fetch)
                            hours_ago = (datetime.utcnow() - last_fetch_dt).total_seconds() / 3600
                            if hours_ago < 1:
                                time_str = "только что"
                            elif hours_ago < 24:
                                time_str = f"{int(hours_ago)}ч назад"
                            else:
                                days_ago = int(hours_ago / 24)
                                time_str = f"{days_ago}д назад"
                        else:
                            time_str = "в очереди"

                        text += f"{icon} *@{username}*\n   └─ Проверка: {time_str}\n"

                if active_count == 0:
                    text += "_(нет активных аккаунтов)_\n"

                text += f"\n✅ Всего аккаунтов в мониторинге: {len(accounts)}"
            else:
                text = "▶️ *Мониторинг возобновлен*\n\n_(Аккаунты не добавлены)_"

            keyboard = [
                [InlineKeyboardButton("📋 Мои аккаунты", callback_data='list_accounts')],
                [InlineKeyboardButton("🔙 Назад в меню", callback_data='back')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error resuming monitoring: {e}")
            await query.edit_message_text("❌ Ошибка при возобновлении")

    async def start_parsing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Начать парсинг вручную"""
        query = update.callback_query
        await query.answer()

        try:
            await query.edit_message_text(
                "⏳ *Парсинг запущен...*\n\n"
                "Это может занять несколько минут.\n"
                "Вы получите уведомление когда процесс завершится.",
                parse_mode='Markdown'
            )

            user_id = update.effective_user.id
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, username, num_posts, min_likes, check_interval_hours
                FROM monitored_accounts
                WHERE (user_id = %s OR user_id IS NULL) AND is_active = 1
            ''', (user_id,))
            accounts = cursor.fetchall()
            cursor.close()
            conn.close()

            if not accounts:
                await query.edit_message_text(
                    "❌ Нет активных аккаунтов для парсинга.\n\n"
                    "Добавьте аккаунт чтобы начать мониторинг."
                )
                return

            if not self.parser or not self.filter or not self.analyzer:
                await query.edit_message_text(
                    "❌ Ошибка: парсер не инициализирован.\n\n"
                    "Пожалуйста, перезагрузите бота."
                )
                return

            all_posts = []
            errors = []

            for acc_id, username, num_posts, min_likes, check_interval_hours in accounts:
                try:
                    logger.info(f"Parsing {username} ({num_posts} posts)...")

                    await query.edit_message_text(
                        f"⏳ Получаю посты @{username}...",
                        parse_mode='Markdown'
                    )

                    posts = await asyncio.to_thread(self.parser.monitor_account, target_username=username, num_posts=num_posts)

                    if posts:
                        # Сохранить время последней проверки
                        conn = psycopg2.connect(self.db_url)
                        cursor = conn.cursor()
                        next_check = datetime.utcnow() + timedelta(hours=check_interval_hours)
                        cursor.execute(
                            'UPDATE monitored_accounts SET last_fetch = %s, next_check = %s WHERE id = %s',
                            (datetime.utcnow().isoformat(), next_check.isoformat(), acc_id)
                        )
                        conn.commit()
                        cursor.close()
                        conn.close()

                        # Запустить анализ в фоне (для статистики в БД)
                        try:
                            if self.filter:
                                await asyncio.to_thread(self.filter.process_posts, posts)
                            if self.analyzer:
                                await asyncio.to_thread(self.analyzer.process_posts, posts)
                        except Exception as ae:
                            logger.warning(f"Background analysis error (non-critical): {ae}")

                        all_posts.extend(posts)

                except Exception as e:
                    error_msg = f"{type(e).__name__}: {str(e)}"
                    logger.error(f"Error parsing {username}: {error_msg}", exc_info=True)
                    errors.append(f"@{username}: {error_msg[:200]}")

            # Показать итог
            if not all_posts and not errors:
                await query.edit_message_text("⚠️ Постов не найдено.")
                return

            if errors and not all_posts:
                err_text = "❌ Ошибка парсинга:\n" + "\n".join(errors)
                keyboard = [[InlineKeyboardButton("🔍 Логи", callback_data='debug_logs'),
                             InlineKeyboardButton("🔙 Меню", callback_data='back')]]
                await query.edit_message_text(err_text, reply_markup=InlineKeyboardMarkup(keyboard))
                return

            summary = f"✅ Готово — спарсено {len(all_posts)} постов. Отправляю каждый ниже 👇"
            if errors:
                summary += f"\n⚠️ Ошибки по некоторым аккаунтам: {'; '.join(errors)}"
            await query.edit_message_text(summary)

            # Отправить каждый пост отдельным сообщением
            for i, post in enumerate(all_posts, 1):
                caption = post.get('caption') or ''
                caption_display = (caption[:600] + '…') if len(caption) > 600 else caption
                url = post.get('url', '')
                account = post.get('account', '')

                post_text = f"📌 *Пост {i} — @{account}*\n\n"
                if caption_display:
                    post_text += caption_display
                else:
                    post_text += '_(текст отсутствует — возможно только видео)_'
                if url:
                    post_text += f"\n\n🔗 {url}"

                try:
                    await update.effective_chat.send_message(post_text, parse_mode='Markdown')
                except Exception:
                    # Если Markdown сломался на спецсимволах — отправить без форматирования
                    plain = f"Пост {i} — @{account}\n\n{caption_display or '(нет текста)'}"
                    if url:
                        plain += f"\n{url}"
                    await update.effective_chat.send_message(plain)

        except Exception as e:
            logger.error(f"Error in start_parsing: {e}", exc_info=True)
            await query.edit_message_text(
                f"❌ Критическая ошибка парсинга:\n\n{type(e).__name__}: {str(e)[:500]}\n\n"
                f"Нажми /debug для логов."
            )

    async def debug_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Показать последние логи парсинга из БД"""
        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT target_username, stage, level, message, created_at
                FROM parse_logs
                ORDER BY created_at DESC
                LIMIT 25
            ''')
            rows = cursor.fetchall()
            cursor.close()
            conn.close()

            if not rows:
                text = "📭 Логов парсинга пока нет.\n\nЗапустите парсинг чтобы увидеть подробности."
            else:
                lines = ["🔍 Последние логи парсинга:\n"]
                for target, stage, level, message, created_at in reversed(rows):
                    icon = {'INFO': 'ℹ️', 'SUCCESS': '✅', 'WARN': '⚠️', 'ERROR': '❌'}.get(level, '•')
                    time_str = created_at.strftime('%H:%M:%S') if hasattr(created_at, 'strftime') else str(created_at)[11:19]
                    msg_short = (message[:200] + '...') if len(message) > 200 else message
                    lines.append(f"{icon} [{time_str}] {stage} @{target}\n   {msg_short}")
                text = "\n\n".join(lines)
                if len(text) > 4000:
                    text = text[-4000:]

            if update.callback_query:
                query = update.callback_query
                await query.answer()
                try:
                    await query.edit_message_text(text)
                except Exception:
                    await update.effective_chat.send_message(text)
            else:
                await update.message.reply_text(text)

        except Exception as e:
            logger.error(f"Error in debug_command: {e}", exc_info=True)
            err_text = f"❌ Ошибка получения логов: {type(e).__name__}: {e}"
            if update.callback_query:
                await update.callback_query.edit_message_text(err_text)
            else:
                await update.message.reply_text(err_text)

    async def set_sessionid_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Начать процесс установки sessionid"""
        instructions = (
            "🔑 *Установка sessionid Instagram*\n\n"
            "Это самый надёжный способ обойти блокировку Instagram на хостинге.\n\n"
            "*Как получить sessionid:*\n"
            "1. Открой Instagram в браузере и залогинься\n"
            "2. F12 → вкладка *Application* → *Cookies* → `https://www.instagram.com`\n"
            "3. Найди cookie с именем *sessionid*\n"
            "4. Скопируй значение (длинная строка)\n"
            "5. Пришли его сюда следующим сообщением\n\n"
            f"⚠️ sessionid привяжется к аккаунту: *{self.instagram_username}*\n"
            f"⚠️ Важно: ты должен быть залогинен в браузере именно под этим аккаунтом!\n\n"
            "Отправь /cancel чтобы отменить."
        )

        if update.callback_query:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(instructions, parse_mode='Markdown')
        else:
            await update.message.reply_text(instructions, parse_mode='Markdown')

        return WAIT_SESSIONID

    async def set_sessionid_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Сохранить sessionid в БД"""
        sessionid = update.message.text.strip()

        # Удалить сообщение с sessionid из чата (приватные данные)
        try:
            await update.message.delete()
        except Exception:
            pass

        if len(sessionid) < 20 or ' ' in sessionid or '\n' in sessionid:
            await update.effective_chat.send_message(
                "❌ sessionid выглядит некорректно. Должна быть длинная строка без пробелов.\n"
                "Попробуй ещё раз через /sessionid"
            )
            return ConversationHandler.END

        await update.effective_chat.send_message(
            "ℹ️ sessionid больше не нужен — бот использует HikerAPI и не логинится в Instagram напрямую.\n\n"
            "Нажми /start → 'Начать парсинг'."
        )

        return ConversationHandler.END

    async def cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Отменить текущий диалог"""
        await update.message.reply_text("❌ Отменено.")
        return ConversationHandler.END

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Справка - работает как прямая команда и как кнопка"""
        help_text = """
📚 *Instagram Monitor Bot — Справка*

─────────────────
*🔗 РАЗОБРАТЬ ПОСТ ПО ССЫЛКЕ*
─────────────────
Нажми кнопку → отправь ссылку на любой пост или Reel.
Бот покажет текст поста и сделает разбор через Gemini:
• О чём пост (суть в 1–2 предложениях)
• Ключевые идеи
• Как использовать для своего контента

Работает с постами на любом языке — ответ всегда на русском.

─────────────────
*🚀 ПАРСИНГ АККАУНТА*
─────────────────
Добавь аккаунт → нажми "Начать парсинг".
Бот пришлёт каждый пост отдельным сообщением: текст + ссылка.
Ты сам решаешь что с ним делать.

─────────────────
*➕ ДОБАВИТЬ АККАУНТ*
─────────────────
Введи имя пользователя (например: `artemiimiller`).
Выбери сколько постов парсить и как часто проверять.
Аккаунт сохраняется — можно добавить несколько.

─────────────────
*📊 ДАЙДЖЕСТ*
─────────────────
Статистика по всем постам за последние 24 часа.
Автоматически приходит каждый день в 09:00.

─────────────────
*📈 СТАТИСТИКА*
─────────────────
Общее количество постов, средняя релевантность,
количество вирусных постов за 7 дней.

─────────────────
*❓ КАК НАЧАТЬ:*
─────────────────
1️⃣ Нажми *"Разобрать пост по ссылке"* и отправь ссылку
2️⃣ Или нажми *"Добавить аккаунт"* для регулярного мониторинга
3️⃣ Нажми *"Начать парсинг"* чтобы получить посты сейчас
"""

        if update.callback_query:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(help_text, parse_mode='Markdown')
        else:
            await update.message.reply_text(help_text, parse_mode='Markdown')

    async def analyze_url_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Начать флоу анализа поста по ссылке"""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🔗 *Разбор поста по ссылке*\n\n"
            "Отправь ссылку на пост или Reel Instagram — я получу текст и сразу разберу его.\n\n"
            "Пример: `https://www.instagram.com/p/ABC123/`\n\n"
            "Отправь /cancel чтобы отменить.",
            parse_mode='Markdown'
        )
        return WAIT_URL

    async def analyze_url_receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Получить ссылку, спарсить пост и автоматически разобрать через Gemini (Instagram, YouTube, TikTok)"""
        text = update.message.text.strip()

        # Проверяем Instagram ссылки
        ig_full = re.search(
            r'(https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_-]+[^\s]*)',
            text,
            re.IGNORECASE,
        )

        # Проверяем YouTube ссылки
        yt_full = re.search(
            r'(https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/|m\.youtube\.com/(?:watch\?v=|shorts/))[A-Za-z0-9_-]+[^\s]*)',
            text,
            re.IGNORECASE,
        )

        # Проверяем TikTok ссылки (включая короткие vt.tiktok.com)
        tt_full = re.search(
            r'(https?://(?:www\.|vt\.)?tiktok\.com/[^\s]+)',
            text,
            re.IGNORECASE,
        )

        full = ig_full or yt_full or tt_full

        if not full:
            await update.message.reply_text(
                "❌ Это не похоже на ссылку Instagram/YouTube/TikTok.\n\n"
                "Нужна ссылка вида:\n"
                "- `https://www.instagram.com/p/...` или `/reel/...`\n"
                "- `https://youtube.com/watch?v=...` или `/shorts/...`\n"
                "- `https://tiktok.com/@username/video/...` или `vt.tiktok.com/...`\n\n"
                "Попробуй ещё раз или /cancel",
                parse_mode='Markdown'
            )
            return WAIT_URL

        url = full.group(1).strip().rstrip('.,;)]>»»\'"')

        # Определяем платформу
        platform = 'instagram'
        if yt_full:
            platform = 'youtube'
        elif tt_full:
            platform = 'tiktok'

        status_msg = await update.message.reply_text(f"⏳ Получаю {platform} контент...")

        try:
            post = None
            if platform == 'instagram':
                if not self.parser:
                    await status_msg.edit_text("❌ Парсер Instagram не инициализирован.")
                    return ConversationHandler.END
                post = await asyncio.to_thread(self.parser.get_post_by_url, url)
            else:
                # YouTube или TikTok через MediaParser
                logger.info(f"Пытаемся распарсить {platform}: {url}")
                try:
                    post = await asyncio.to_thread(self.media_parser.get_video_info, url)
                    logger.info(f"Результат парсинга {platform}: {bool(post)}")
                except Exception as e:
                    logger.error(f"Ошибка при парсинге {platform}: {e}", exc_info=True)
                    await status_msg.edit_text(
                        f"❌ Ошибка парсинга {platform}: {str(e)[:200]}"
                    )
                    return ConversationHandler.END

            if not post:
                await status_msg.edit_text(
                    f"❌ Не удалось получить {platform} контент.\n\n"
                    "Возможные причины: удалённый контент, закрытый аккаунт или неверная ссылка."
                )
                return ConversationHandler.END

            # Форматируем данные для совместимости с Instagram форматом
            caption = post.get('description') or post.get('caption') or ''
            account = post.get('channel') or post.get('uploader') or post.get('account', '')
            likes = post.get('likes', 0) or post.get('like_count', 0)
            comments = post.get('comments', 0) or post.get('comment_count', 0)
            views = post.get('views', 0) or post.get('view_count', 0)
            display_link = post.get('url') or post.get('webpage_url') or url

            # Получаем слайды карусели (только для Instagram)
            slides = [s for s in (post.get('carousel_slides') or []) if isinstance(s, dict)] if platform == 'instagram' else []

            # Показываем сырой текст поста
            cap_display = (caption[:800] + '…') if len(caption) > 800 else caption

            platform_emoji = {'instagram': '📸', 'youtube': '📺', 'tiktok': '🎵'}
            raw_text = f"{platform_emoji.get(platform, '🔗')} *{platform.title()} {'от @' + account if account else ''}*\n\n"
            raw_text += cap_display if cap_display else '_(текст отсутствует — только медиа)_'

            # Для YouTube/TikTok показываем количество просмотров
            if platform in ('youtube', 'tiktok'):
                raw_text += f"\n\n👁️ {views}  ❤️ {likes}  💬 {comments}"
            else:
                raw_text += f"\n\n❤️ {likes}  💬 {comments}"

            raw_text += f"  🔗 {display_link}"

            try:
                await status_msg.edit_text(raw_text, parse_mode='Markdown')
            except Exception:
                fb_lines = [
                    f"Пост {'@' + account if account else ''}",
                    "",
                    cap_display or "(нет текста)",
                    "",
                    f"❤️ {likes}  💬 {comments}",
                    display_link,
                ]
                if platform == 'instagram' and (post.get('is_carousel') or len(slides) > 1):
                    fb_lines.extend(["", f"Карусель: {len(slides)} слайд(ов)"])
                await update.effective_chat.send_message("\n".join(fb_lines))

            gemini_key = os.getenv('OPENAI_API_KEY')

            # Картинки карусели в Telegram (альбом 2–10 фото или одно фото)
            import io
            import requests as _req
            photo_slides = [s for s in slides if s.get('image_url')]
            if len(photo_slides) >= 2:
                media_items = []
                for sl in photo_slides[:10]:
                    iu = sl.get('image_url')
                    try:
                        blob = await asyncio.to_thread(
                            lambda u=iu: _req.get(u, headers=IG_CDN_HEADERS, timeout=35).content
                        )
                        media_items.append(InputMediaPhoto(media=io.BytesIO(blob)))
                    except Exception as me:
                        logger.warning(f'Carousel photo download skip: {me}')
                if len(media_items) >= 2:
                    try:
                        await update.effective_chat.send_media_group(media_items)
                    except Exception as mg_e:
                        logger.warning(f'media_group failed: {mg_e}')
            elif len(photo_slides) == 1:
                try:
                    iu = photo_slides[0]['image_url']
                    blob = await asyncio.to_thread(
                        lambda: _req.get(iu, headers=IG_CDN_HEADERS, timeout=35).content
                    )
                    await update.effective_chat.send_photo(
                        photo=io.BytesIO(blob),
                        caption=f"Слайд 1/{len(slides)}" if slides else None,
                    )
                except Exception as pe:
                    logger.warning(f'send_photo carousel: {pe}')

            if slides and not gemini_key:
                await update.effective_chat.send_message(
                    "⚠️ Чтобы прочитать текст с картинок и речь в видео-слайдах карусели, "
                    "нужен `GEMINI_API_KEY` в переменных окружения."
                )

            if slides and gemini_key:
                try:
                    env_cap = int(os.getenv('CAROUSEL_GEMINI_MAX_SLIDES', '6'))
                except ValueError:
                    env_cap = 6
                limit = max(1, min(len(slides), env_cap))
                try:
                    slide_timeout = float(os.getenv('CAROUSEL_SLIDE_TIMEOUT_SEC', '90'))
                except ValueError:
                    slide_timeout = 90.0

                skip_vid = _carousel_skip_video_when_has_image()
                progress_msg = await update.effective_chat.send_message(
                    f"🖼 Карусель: до {limit} из {len(slides)} слайдов. "
                    f"{'Без транскрипта видео, если есть кадр — быстрее. ' if skip_vid else ''}"
                    f"Статус обновляется по ходу…"
                )
                parts: List[str] = []
                for i in range(limit):
                    try:
                        await progress_msg.edit_text(
                            f"🖼 Слайд {i + 1}/{limit} — OCR"
                            f"{' (+видео если нет кадра)' if not skip_vid else ''}"
                            f" (до ~{int(slide_timeout)} с)…"
                        )
                    except Exception:
                        pass
                    try:
                        block = await asyncio.wait_for(
                            asyncio.to_thread(_carousel_slide_block, slides[i], i + 1, gemini_key),
                            timeout=slide_timeout,
                        )
                    except asyncio.TimeoutError:
                        block = (
                            f"=== Слайд {i + 1} (из карусели) ===\n"
                            f"(таймаут {int(slide_timeout)} с — пропуск; уменьши карусель или увеличь CAROUSEL_SLIDE_TIMEOUT_SEC)"
                        )
                        logger.warning('Carousel slide %s timed out after %ss', i + 1, slide_timeout)
                    parts.append(block)

                try:
                    await progress_msg.edit_text(f"✅ Слайды: {limit}/{len(slides)}. Формирую итоговый разбор…")
                except Exception:
                    pass

                carousel_ctx = '\n\n'.join(parts)
                if carousel_ctx:
                    if caption.strip():
                        caption = (
                            caption.strip()
                            + "\n\n=== Содержимое слайдов карусели ===\n"
                            + carousel_ctx
                        )
                    else:
                        caption = "=== Содержимое слайдов карусели ===\n" + carousel_ctx

            # Если нет текста — попробовать Gemini Vision по thumbnail
            if not caption.strip():
                thumbnail_url = post.get('thumbnail_url')
                if not thumbnail_url and slides:
                    for sl in slides:
                        if sl.get('image_url'):
                            thumbnail_url = sl['image_url']
                            break
                if not thumbnail_url:
                    keyboard = [[InlineKeyboardButton("🔗 Разобрать другой пост", callback_data='analyze_url'),
                                 InlineKeyboardButton("🔙 Меню", callback_data='back')]]
                    await update.effective_chat.send_message(
                        "⚠️ Нет текста и не удалось получить картинку превью (карусель или API). "
                        "Проверь деплой последней версии бота и HikerAPI.",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    return ConversationHandler.END

                gemini_key = os.getenv('OPENAI_API_KEY')
                if not gemini_key:
                    keyboard = [[InlineKeyboardButton("🔗 Разобрать другой пост", callback_data='analyze_url'),
                                 InlineKeyboardButton("🔙 Меню", callback_data='back')]]
                    await update.effective_chat.send_message(
                        "⚠️ Пост без текста — нужен GEMINI_API_KEY для анализа видео через GPT-4o Vision.\n"
                        "Добавь его в Railway Variables.",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    return ConversationHandler.END

                await update.effective_chat.send_message("🎥 Пост без текста — анализирую видео через GPT-4o Vision...")

                try:
                    import requests as _req
                    import PIL.Image
                    import io as _io
                    import base64

                    img_resp = _req.get(
                        thumbnail_url,
                        headers=IG_CDN_HEADERS,
                        timeout=20
                    )
                    img_resp.raise_for_status()

                    openai_client = OpenAI(api_key=gemini_key)
                    img = PIL.Image.open(_io.BytesIO(img_resp.content))
                    
                    # Конвертируем изображение в base64 для OpenAI Vision
                    buffered = _io.BytesIO()
                    img.save(buffered, format="JPEG")
                    img_base64 = base64.b64encode(buffered.getvalue()).decode()
                    
                    vision_resp = _openai_generate_with_retry(
                        openai_client,
                        model='gpt-4o-mini',
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "Это превью видео/Reel из Instagram. Подробно опиши: что показано, кто в кадре, тема, настроение, о чём вероятно этот видео-пост. Ответь на русском языке."},
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": f"data:image/jpeg;base64,{img_base64}"}
                                    }
                                ]
                            }
                        ]
                    )
                    visual_desc = _openai_response_text(vision_resp)
                    logger.info(f"OpenAI Vision description: {visual_desc[:100]}...")
                    try:
                        await update.effective_chat.send_message(
                            f"👁 *GPT-4o видит:*\n\n{visual_desc}",
                            parse_mode='Markdown'
                        )
                    except Exception:
                        await update.effective_chat.send_message(f"GPT-4o видит:\n\n{visual_desc}")

                    # Транскрипция видео через OpenAI Whisper API (если доступно)
                    transcript_text = ''
                    video_url = post.get('video_url')
                    if video_url:
                        await update.effective_chat.send_message("🎤 Слушаю что говорят в видео (Whisper)...")
                        try:
                            head = _req.head(video_url, headers=IG_CDN_HEADERS, timeout=10)
                            size = int(head.headers.get('content-length', 0))
                            if 0 < size <= 25 * 1024 * 1024:  # до 25 МБ для Whisper
                                vid_data = _req.get(video_url, headers=IG_CDN_HEADERS, timeout=60)
                                vid_data.raise_for_status()
                                # Используем временный файл для Whisper API
                                import tempfile
                                with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_f:
                                    tmp_path = tmp_f.name
                                    tmp_f.write(vid_data.content)
                                try:
                                    # OpenAI Whisper API для транскрипции
                                    with open(tmp_path, 'rb') as audio_file:
                                        transcript = openai_client.audio.transcriptions.create(
                                            model="whisper-1",
                                            file=audio_file,
                                            language="ru"  # Принудительно русский
                                        )
                                    transcript_text = transcript.text if transcript else ''
                                    if transcript_text:
                                        try:
                                            await update.effective_chat.send_message(
                                                f"🎤 *Речь в видео:*\n\n{transcript_text}",
                                                parse_mode='Markdown'
                                            )
                                        except Exception:
                                            await update.effective_chat.send_message(f"Речь в видео:\n\n{transcript_text}")
                                    else:
                                        await update.effective_chat.send_message("🔇 Речи в видео не обнаружено")
                                except Exception as whisper_error:
                                    logger.warning(f"Whisper transcription failed: {whisper_error}")
                                    await update.effective_chat.send_message(f"⚠️ Не удалось транскрибировать видео: {str(whisper_error)[:100]}")
                                finally:
                                    import os as _os
                                    try:
                                        _os.unlink(tmp_path)
                                    except OSError:
                                        pass
                            else:
                                await update.effective_chat.send_message(f"⚠️ Видео слишком большое для транскрипции ({size // 1024 // 1024} МБ, лимит 25 МБ)")
                        except Exception as e:
                            logger.warning(f"Video processing failed: {e}")
                            await update.effective_chat.send_message(f"⚠️ Не удалось обработать видео: {str(e)[:100]}")

                # Формируем caption для анализа
                    # Объединить визуальное описание + транскрипцию для Gemini
                    if transcript_text:
                        caption = f"[Что видно в кадре]: {visual_desc}\n\n[Что говорит человек]: {transcript_text}"
                    else:
                        caption = visual_desc

                except Exception as ve:
                    logger.error(f"Gemini Vision error: {ve}", exc_info=True)
                    keyboard = [[InlineKeyboardButton("🔗 Разобрать другой пост", callback_data='analyze_url'),
                                 InlineKeyboardButton("🔙 Меню", callback_data='back')]]
                    await update.effective_chat.send_message(
                        f"❌ Gemini Vision не смог проанализировать видео: {str(ve)[:200]}",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    return ConversationHandler.END

            # Автоматический анализ через Gemini
            await update.effective_chat.send_message("🤖 Разбираю через Gemini...")

            try:
                gemini_key = os.getenv('OPENAI_API_KEY')
                if not gemini_key:
                    await update.effective_chat.send_message("❌ GEMINI_API_KEY не задан в Railway — анализ недоступен.")
                    return ConversationHandler.END

                # Выбрать промпт в зависимости от типа контента
                has_speech = "[Что говорит человек]" in caption
                has_visual = "[Что видно в кадре]" in caption
                is_carousel_prompt = (
                    "=== Содержимое слайдов карусели ===" in caption
                    or "=== Слайд" in caption
                )

                if is_carousel_prompt:
                    prompt = (
                        "Это пост Instagram с каруселью (несколько кадров). В тексте ниже — подпись автора "
                        "и разбор слайдов: OCR с картинок и транскрипты речи из видео-слайдов.\n"
                        "Свяжи слайды в единую мысль и посыл. Ответь на русском языке:\n\n"
                        "СУТЬ ПУБЛИКАЦИИ:\n[2–4 предложения]\n\n"
                        "ЛОГИКА ПО СЛАЙДАМ:\n[кратко: что на каждом этапе и зачем]\n\n"
                        "КЛЮЧЕВЫЕ ИДЕИ:\n[3–5 тезисов через •]\n\n"
                        "ИДЕИ ДЛЯ СВОЕГО КОНТЕНТА:\n[2–3 идеи]\n\n"
                        f"---\n{caption[:12000]}"
                    )
                elif has_speech and has_visual:
                    # Видео со звуком: речь = суть, визуал = контекст
                    prompt = (
                        "Ты анализируешь видео-пост из Instagram. У тебя два источника:\n"
                        "• ВИЗУАЛЬНЫЙ РЯД — что видно в кадре (обстановка, антураж, подача)\n"
                        "• РЕЧЬ АВТОРА — что он говорит (ГЛАВНЫЙ источник смысла)\n\n"
                        "Правило: если визуал и речь о разном — суть берём из РЕЧИ. "
                        "Визуал упоминаем только как формат/стиль подачи.\n\n"
                        "Ответь строго в этом формате на русском языке:\n\n"
                        "ФОРМАТ И СТИЛЬ ПОДАЧИ:\n"
                        "[1 строка: как снято, кто, где, какой тип контента]\n\n"
                        "СУТЬ (из речи):\n"
                        "[2–3 предложения — главная мысль и посыл автора]\n\n"
                        "КЛЮЧЕВЫЕ ТЕЗИСЫ:\n"
                        "[3–5 конкретных тезисов из речи автора, через •]\n\n"
                        "ИДЕИ ДЛЯ СВОЕГО КОНТЕНТА:\n"
                        "[2–3 конкретные идеи как адаптировать эту тему или формат под себя]\n\n"
                        f"---\n{caption[:2500]}"
                    )
                elif has_visual:
                    # Видео без речи или речь не распозналась
                    prompt = (
                        "Это превью видео-поста из Instagram без распознанной речи.\n"
                        "Ответь на русском языке:\n\n"
                        "ФОРМАТ И СТИЛЬ:\n[тип контента, подача]\n\n"
                        "О ЧЁМ ВЕРОЯТНО:\n[гипотеза по визуалу]\n\n"
                        "ИДЕИ ДЛЯ СВОЕГО КОНТЕНТА:\n[2–3 идеи]\n\n"
                        f"---\n{caption[:2000]}"
                    )
                else:
                    # Обычный текстовый пост
                    prompt = (
                        "Это пост из Instagram. Ответь на русском языке:\n\n"
                        "СУТЬ:\n[2–3 предложения — главная мысль]\n\n"
                        "КЛЮЧЕВЫЕ ИДЕИ:\n[3–5 тезисов через •]\n\n"
                        "ИДЕИ ДЛЯ СВОЕГО КОНТЕНТА:\n[2–3 конкретные идеи]\n\n"
                        f"---\n{caption[:2000]}"
                    )

                max_tokens = 2048 if is_carousel_prompt else 1000
                openai_client = OpenAI(api_key=gemini_key)
                response = _openai_generate_with_retry(
                    openai_client,
                    model='gpt-4o-mini',
                    messages=[{"role": "user", "content": prompt}]
                )
                result = _openai_response_text(response)

                # Сохранить контекст для "Создать промпт" - полный словарь поста
                context.user_data['last_analysis'] = result
                context.user_data['last_raw_content'] = post

                keyboard = [
                    [InlineKeyboardButton("📝 Создать промпт", callback_data='create_prompt')],
                    [InlineKeyboardButton("🔗 Разобрать другой пост", callback_data='analyze_url')],
                    [InlineKeyboardButton("🔙 Главное меню", callback_data='back')],
                ]
                try:
                    await update.effective_chat.send_message(
                        f"📊 *Анализ:*\n\n{result}",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='Markdown'
                    )
                except Exception:
                    await update.effective_chat.send_message(
                        f"Анализ:\n\n{result}",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )

            except Exception as e:
                logger.error(f"analyze_url_receive error: {e}", exc_info=True)
                el = str(e).lower()
                if any(x in el for x in ('429', 'resource_exhausted', 'quota')):
                    await update.effective_chat.send_message(
                        "❌ Превышен лимит запросов Gemini (429). Подожди 1–2 минуты или проверь квоту в "
                        "Google AI Studio (aistudio.google.com). Для длинных каруселей в Railway задай переменные "
                        "CAROUSEL_GEMINI_MAX_SLIDES=4 и CAROUSEL_GEMINI_DELAY_SEC=3."
                    )
                else:
                    await update.effective_chat.send_message(f"❌ Ошибка: {type(e).__name__}: {str(e)[:300]}")

        except Exception as e:
            logger.error(f"analyze_url_receive outer error: {e}", exc_info=True)
            await update.effective_chat.send_message(f"❌ Внутренняя ошибка: {str(e)[:200]}")

        return ConversationHandler.END

    async def create_prompt_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Сгенерировать готовый промпт для реализации идеи через Gemini"""
        query = update.callback_query
        await query.answer()

        analysis = context.user_data.get('last_analysis', '')
        raw_content = context.user_data.get('last_raw_content', '')

        if not analysis:
            await query.edit_message_text(
                "❌ Анализ не найден. Сначала разбери пост по ссылке.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔗 Разобрать пост", callback_data='analyze_url')
                ]])
            )
            return

        await query.edit_message_text("⚙️ Генерирую промпт для реализации...")

        gemini_key = os.getenv('OPENAI_API_KEY')
        if not gemini_key:
            await query.edit_message_text("❌ GEMINI_API_KEY не задан в Railway")
            return

        try:
            prompt = (
                "На основе анализа Instagram поста создай промпт для AI-агента.\n\n"
                "ГЛАВНОЕ ПРАВИЛО: Вытащи ВСЮ конкретику из поста — названия инструментов, "
                "фичи, бизнес-модель, целевую аудиторию, цифры. Не обобщай!\n\n"
                "СТРУКТУРА ПРОМПТА:\n\n"
                "Ты [роль на основе поста: разработчик/дизайнер/маркетолог/предприниматель и т.д.].\n\n"
                "Я увидел пост про [конкретная тема]. Там показывали [что именно: инструмент, фича, процесс].\n\n"
                "Хочу сделать аналогичное. Конкретно:\n"
                "- Что это: [точное описание продукта/сервиса]\n"
                "- Для кого: [целевая аудитория]\n"
                "- Какие функции: [конкретные фичи из поста]\n"
                "- Как работает: [описание процесса из поста]\n\n"
                "Проведи разведку:\n"
                "1. Найди на GitHub готовые решения по теме [ключевые слова из поста] — топ-5 по звёздам\n"
                "2. Найди конкурентов и аналоги — что уже существует на рынке\n"
                "3. Определи оптимальный стек: какие библиотеки/API/сервисы нужны\n"
                "4. Дай пошаговый план реализации с оценкой сроков\n"
                "5. Укажи бюджет: бесплатные vs платные варианты\n"
                "6. Для каждой опции — плюсы, минусы, сложность\n\n"
                "Важно: я не хочу отвечать на вопросы. Выдай полный результат сразу.\n\n"
                "ОГРАНИЧЕНИЯ: до 1200 символов, максимум конкретики.\n\n"
                f"--- ИСХОДНЫЙ ПОСТ ---\n"
                f"Аккаунт: {raw_content.get('account', 'Неизвестно')}\n"
                f"Лайки: {raw_content.get('likes', 0)} | Комментарии: {raw_content.get('comments', 0)}\n"
                f"Текст поста:\n{raw_content.get('caption', 'Нет текста')}\n\n"
                f"--- АНАЛИЗ ---\n{analysis}\n\n"
                "Создай промпт. Вытащи ВСЕ конкретные названия, инструменты и детали из поста. "
                "Начни с подходящей роли агента."
            )

            openai_client = OpenAI(api_key=gemini_key)
            response = _openai_generate_with_retry(
                openai_client,
                model='gpt-4o-mini',
                messages=[{"role": "user", "content": prompt}]
            )
            ready_prompt = _openai_response_text(response)

            keyboard = [
                [InlineKeyboardButton("🔗 Разобрать другой пост", callback_data='analyze_url')],
                [InlineKeyboardButton("🔙 Главное меню", callback_data='back')],
            ]

            try:
                await query.edit_message_text(
                    ready_prompt,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
            except Exception:
                await query.edit_message_text(
                    ready_prompt,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        except Exception as e:
            logger.error(f"create_prompt_action error: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Ошибка генерации промпта: {str(e)[:200]}")

    async def handle_instagram_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Получить пост по ссылке Instagram, YouTube, TikTok и показать содержимое"""
        text = update.message.text.strip()

        # Проверяем Instagram ссылки
        ig_match = re.search(r'https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)', text)

        # Проверяем YouTube ссылки
        yt_match = re.search(r'(https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/|m\.youtube\.com/(?:watch\?v=|shorts/))([A-Za-z0-9_-]+))', text)

        # Проверяем TikTok ссылки (включая короткие vt.tiktok.com)
        tt_match = re.search(r'https?://(?:www\.|vt\.)?tiktok\.com/[^\s]+', text)

        if not ig_match and not yt_match and not tt_match:
            return

        # Определяем тип ссылки
        platform = 'instagram'
        url = ig_match.group(0) if ig_match else (yt_match.group(0) if yt_match else tt_match.group(0))

        if yt_match:
            platform = 'youtube'
        elif tt_match:
            platform = 'tiktok'

        msg = await update.message.reply_text(f"⏳ Получаю {platform} контент...")

        try:
            post = None
            if platform == 'instagram':
                if not self.parser:
                    await msg.edit_text("❌ Парсер Instagram не инициализирован.")
                    return
                post = await asyncio.to_thread(self.parser.get_post_by_url, url)
            else:
                # YouTube или TikTok через MediaParser
                post = await asyncio.to_thread(self.media_parser.get_video_info, url)

            if not post:
                await msg.edit_text("❌ Не удалось получить контент. Проверь ссылку.")
                return

            # Форматируем данные для совместимости с Instagram форматом
            context.user_data['last_post'] = post

            caption = post.get('description') or post.get('caption') or ''
            account = post.get('channel') or post.get('uploader') or post.get('account', '')
            likes = post.get('likes', 0) or post.get('like_count', 0)
            comments = post.get('comments', 0) or post.get('comment_count', 0)
            views = post.get('views', 0) or post.get('view_count', 0)

            cap_display = (caption[:800] + '…') if len(caption) > 800 else caption

            platform_emoji = {'instagram': '📸', 'youtube': '📺', 'tiktok': '🎵'}
            header = f"{platform_emoji.get(platform, '🔗')} *{platform.title()} {'от @' + account if account else ''}*\n\n"

            # Для YouTube/TikTok показываем количество просмотров
            if platform in ('youtube', 'tiktok'):
                footer = f"\n\n👁️ {views}  ❤️ {likes}  💬 {comments}\n🔗 {url}"
            else:
                footer = f"\n\n❤️ {likes}  💬 {comments}\n🔗 {url}"

            body = cap_display if cap_display else '_(текст отсутствует — только медиа)_'

            keyboard = [
                [InlineKeyboardButton("📝 Краткое резюме", callback_data='analyze_summary')],
                [InlineKeyboardButton("💡 Идеи для контента", callback_data='analyze_ideas')],
                [InlineKeyboardButton("🌐 Перевести и адаптировать", callback_data='analyze_adapt')],
            ]
            try:
                await msg.edit_text(
                    header + body + footer,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
            except Exception:
                await msg.edit_text(
                    f"Пост{' от @' + account if account else ''}\n\n{body}\n\n{url}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        except Exception as e:
            logger.error(f"handle_instagram_url error: {e}", exc_info=True)
            await msg.edit_text(f"❌ Ошибка: {type(e).__name__}: {str(e)[:300]}")

    async def analyze_post_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Анализировать пост через Gemini по выбранному действию"""
        query = update.callback_query
        await query.answer()

        post = context.user_data.get('last_post')
        if not post:
            await query.edit_message_text("❌ Пост не найден. Отправь ссылку заново.")
            return

        caption = post.get('caption', '') or ''
        action = query.data

        if not caption:
            await query.edit_message_text(
                "⚠️ В этом посте нет текста (только видео/фото).\n\n"
                "Ничего не могу проанализировать — нет текстового контента."
            )
            return

        prompts = {
            'analyze_summary': (
                "Сделай краткое резюме этого поста в 2–3 предложениях на русском языке. "
                "Только суть — без вводных слов типа 'В этом посте...':\n\n" + caption
            ),
            'analyze_ideas': (
                "На основе этого поста предложи 3–5 конкретных идей для создания своего контента "
                "на похожую тему. Отвечай на русском языке, каждую идею с новой строки:\n\n" + caption
            ),
            'analyze_adapt': (
                "Переведи этот текст на русский язык (если он не на русском) и адаптируй для "
                "русскоязычной аудитории. Сохрани смысл, но сделай живо и естественно:\n\n" + caption
            ),
        }

        prompt = prompts.get(action, prompts['analyze_summary'])
        await query.edit_message_text("🤖 Gemini думает...")

        try:
            gemini_key = os.getenv('OPENAI_API_KEY')
            if not gemini_key:
                await query.edit_message_text("❌ GEMINI_API_KEY не задан в Railway")
                return

            openai_client = OpenAI(api_key=gemini_key)
            response = _openai_generate_with_retry(
                openai_client,
                model='gpt-4o-mini',
                messages=[{"role": "user", "content": prompt}]
            )
            result = _openai_response_text(response)

            keyboard = [
                [InlineKeyboardButton("📝 Резюме", callback_data='analyze_summary'),
                 InlineKeyboardButton("💡 Идеи", callback_data='analyze_ideas')],
                [InlineKeyboardButton("🌐 Перевести", callback_data='analyze_adapt')],
                [InlineKeyboardButton("🔙 Меню", callback_data='back')],
            ]
            try:
                await query.edit_message_text(
                    f"🤖 *Результат:*\n\n{result}",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
            except Exception:
                await query.edit_message_text(
                    f"Результат:\n\n{result}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        except Exception as e:
            logger.error(f"analyze_post_action error: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Ошибка Gemini: {str(e)[:300]}")

    def setup(self) -> Application:
        """Настроить приложение Telegram"""
        self.application = Application.builder().token(self.token).build()

        # Обработчики команд
        self.application.add_handler(CommandHandler('start', self.start))
        self.application.add_handler(CommandHandler('help', self.help_command))
        self.application.add_handler(CommandHandler('debug', self.debug_command))

        # Обработчик добавления аккаунта
        conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_account, pattern='^add_account$')],
            states={
                ADD_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.confirm_account)],
                SELECT_NUM_POSTS: [CallbackQueryHandler(self.select_num_posts, pattern='^posts_(5|10|20)$')],
                SELECT_INTERVAL: [CallbackQueryHandler(self.select_interval, pattern='^interval_(1|6|24)$')]
            },
            fallbacks=[CommandHandler('cancel', self.cancel_conversation)]
        )
        self.application.add_handler(conv_handler)

        # Обработчик "Разобрать пост по ссылке"
        url_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.analyze_url_start, pattern='^analyze_url$')],
            states={
                WAIT_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.analyze_url_receive)]
            },
            fallbacks=[CommandHandler('cancel', self.cancel_conversation)]
        )
        self.application.add_handler(url_conv)

        # Обработчик установки sessionid
        sessionid_conv = ConversationHandler(
            entry_points=[
                CommandHandler('sessionid', self.set_sessionid_start),
                CallbackQueryHandler(self.set_sessionid_start, pattern='^set_sessionid$')
            ],
            states={
                WAIT_SESSIONID: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_sessionid_save)]
            },
            fallbacks=[CommandHandler('cancel', self.cancel_conversation)]
        )
        self.application.add_handler(sessionid_conv)

        # Обработчики кнопок
        self.application.add_handler(
            CallbackQueryHandler(self.start_parsing, pattern='^start_parsing$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.list_accounts, pattern='^list_accounts$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.get_digest, pattern='^get_digest$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.get_stats, pattern='^stats$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.settings_command, pattern='^settings$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.pause_monitoring, pattern='^pause$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.resume_monitoring, pattern='^resume$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.help_command, pattern='^help$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.settings_digest_time, pattern='^digest_time$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.settings_min_relevance, pattern='^min_relevance$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.back_to_menu, pattern='^back$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.debug_command, pattern='^debug_logs$')
        )

        # Обработчик прямых ссылок на посты Instagram
        self.application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND &
                filters.Regex(r'instagram\.com/(?:p|reel|tv)/'),
                self.handle_instagram_url
            )
        )

        # Gemini-анализ поста (кнопки на inline-сообщении)
        self.application.add_handler(
            CallbackQueryHandler(self.analyze_post_action,
                                 pattern='^analyze_(summary|ideas|adapt)$')
        )

        # Создать промпт для реализации
        self.application.add_handler(
            CallbackQueryHandler(self.create_prompt_action, pattern='^create_prompt$')
        )

        return self.application

    async def send_digest_to_user(self, user_id: int, digest_text: str) -> None:
        """Отправить дайджест пользователю"""
        try:
            await self.application.bot.send_message(
                chat_id=user_id,
                text=digest_text,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Error sending digest to {user_id}: {e}")


class SchedulerManager:
    """Управление расписанием для ежедневных дайджестов с AsyncIO"""

    def __init__(self, bot: TelegramBot, db_url: str):
        self.bot = bot
        self.db_url = db_url
        self.scheduler = AsyncIOScheduler()
        self.scheduled_time = self._get_scheduled_time()

    def _get_scheduled_time(self) -> Tuple[int, int]:
        """Получить время отправки с jitter"""
        hour, minute = 9, 0
        jitter_minutes = random.randint(-15, 15)
        total_minutes = hour * 60 + minute + jitter_minutes
        final_hour = (total_minutes // 60) % 24
        final_minute = total_minutes % 60
        return int(final_hour), int(final_minute)

    async def daily_digest_job(self) -> None:
        """Ежедневная работа отправки дайджестов"""
        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()

            cursor.execute('''SELECT id, user_id FROM telegram_users
                             WHERE settings IS NULL OR (settings::jsonb->>'paused')::boolean = false''')
            users = cursor.fetchall()

            if not users:
                logger.info("No active users for digest")
                cursor.close()
                conn.close()
                return

            since = datetime.utcnow() - timedelta(hours=24)

            for user_db_id, user_id in users:
                try:
                    cursor.execute('''
                        SELECT a.sentiment, a.key_topics, a.relevance_score,
                               a.viral_potential, a.recommendations
                        FROM analyses a
                        WHERE a.analyzed_at > %s
                        ORDER BY a.relevance_score DESC
                        LIMIT 20
                    ''', (since.isoformat(),))

                    rows = cursor.fetchall()
                    if not rows:
                        logger.info(f"No new posts for user {user_id}")
                        continue

                    analyses = []
                    for row in rows:
                        analyses.append({
                            'sentiment': row[0] or 'neutral',
                            'key_topics': json.loads(row[1]) if row[1] else [],
                            'relevance_score': row[2] or 0,
                            'viral_potential': row[3] or 'low',
                            'recommendations': json.loads(row[4]) if row[4] else []
                        })

                    digest_text = DigestFormatter.format_daily_digest(analyses)
                    await self.bot.send_digest_to_user(user_id, digest_text)
                    logger.info(f"Sent digest to user {user_id} ({len(analyses)} posts)")

                except Exception as e:
                    logger.error(f"Error sending digest to user {user_id}: {e}")

            cursor.close()
            conn.close()

        except Exception as e:
            logger.error(f"Error in daily_digest_job: {e}")

    def start_scheduler(self) -> None:
        """Запустить планировщик"""
        hour, minute = self.scheduled_time
        trigger = CronTrigger(hour=hour, minute=minute)

        self.scheduler.add_job(
            self.daily_digest_job,
            trigger=trigger,
            id='daily_digest',
            name='Daily Digest'
        )

        self.scheduler.start()
        logger.info(f"AsyncIO Scheduler started. Daily digest at {hour:02d}:{minute:02d}")

    def stop_scheduler(self) -> None:
        """Остановить планировщик"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler stopped")
