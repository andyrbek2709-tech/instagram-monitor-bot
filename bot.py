import os
import re
import psycopg2
import json
import logging
import asyncio
import anthropic
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler, ConversationHandler,
                          CallbackQueryHandler, filters, ContextTypes)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import random

logger = logging.getLogger(__name__)

ADD_ACCOUNT, CONFIRM_ACCOUNT, SELECT_NUM_POSTS, SELECT_INTERVAL = range(4)
ADD_HASHTAG = 5
SEARCH_HASHTAG = 6
SEARCH_YOUTUBE = 7
SEARCH_TIKTOK = 8
WAIT_SESSIONID = 100
WAIT_URL = 101


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
        # Универсальный парсер YouTube/TikTok через yt-dlp
        try:
            from media_parser import MediaParser
            self.media_parser = MediaParser()
            logger.info("MediaParser (yt-dlp) initialized")
        except Exception as e:
            self.media_parser = None
            logger.warning(f"MediaParser not available: {e}")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обработчик /start"""
        keyboard = [
            [InlineKeyboardButton("🔗 Разобрать пост по ссылке", callback_data='analyze_url')],
            [InlineKeyboardButton("🚀 Начать парсинг аккаунта", callback_data='start_parsing')],
            [InlineKeyboardButton("🔍 Поиск по хэштегу 🌐", callback_data='search_hashtag')],
            [InlineKeyboardButton("📺 YouTube поиск", callback_data='search_youtube'),
             InlineKeyboardButton("🎵 TikTok поиск", callback_data='search_tiktok')],
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
            [InlineKeyboardButton("🏷 Мои хэштеги", callback_data='my_hashtags')],
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

    async def my_hashtags_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Показать управление хэштегами"""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        conn = psycopg2.connect(self.db_url)
        cursor = conn.cursor()
        cursor.execute('SELECT hashtag FROM user_hashtags WHERE user_id = %s ORDER BY created_at DESC', (user_id,))
        tags = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()

        text = "🏷 *Мои хэштеги*\n\n"
        if tags:
            text += "Ты отслеживаешь эти хэштеги:\n"
            for t in tags:
                text += f"• `#{t}`\n"
        else:
            text += "У тебя пока нет отслеживаемых хэштегов.\n\n"
            text += "Нажми «Добавить хэштег» и введи #тему для отслеживания."

        keyboard = [
            [InlineKeyboardButton("➕ Добавить хэштег", callback_data='hashtag_add')],
        ]
        if tags:
            keyboard.append([InlineKeyboardButton("🗑 Очистить все", callback_data='hashtag_clear')])
            keyboard.append([InlineKeyboardButton("📊 Аналитика", callback_data='hashtag_stats')])
        keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='settings')])

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    async def hashtag_add_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Начать добавление хэштега"""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "➕ *Добавить хэштег*\n\n"
            "Отправь хэштег для отслеживания. Например:\n"
            "`#грузоперевозки` или просто `грузоперевозки`\n\n"
            "Отправь /cancel чтобы отменить.",
            parse_mode='Markdown'
        )
        return ADD_HASHTAG

    async def hashtag_add_save(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Сохранить хэштег"""
        text = update.message.text.strip().lower().lstrip('#')
        if not text or len(text) > 100:
            await update.message.reply_text("❌ Некорректный хэштег. Попробуй ещё раз или /cancel")
            return ADD_HASHTAG

        user_id = update.effective_user.id
        conn = psycopg2.connect(self.db_url)
        cursor = conn.cursor()
        try:
            cursor.execute(
                'INSERT INTO user_hashtags (user_id, hashtag, created_at) VALUES (%s, %s, %s) ON CONFLICT (user_id, hashtag) DO NOTHING',
                (user_id, text, datetime.utcnow().isoformat())
            )
            conn.commit()
            await update.message.reply_text(f"✅ Хэштег `#{text}` добавлен для отслеживания!", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error saving hashtag: {e}")
            await update.message.reply_text("❌ Ошибка при сохранении хэштега")
        finally:
            cursor.close()
            conn.close()

        return ConversationHandler.END

    async def hashtag_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Очистить все хэштеги пользователя"""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        conn = psycopg2.connect(self.db_url)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM user_hashtags WHERE user_id = %s', (user_id,))
        conn.commit()
        cursor.close()
        conn.close()

        await query.edit_message_text("🗑 Все хэштеги удалены.\n\nНажми «➕ Добавить хэштег» чтобы добавить новые.")

    async def hashtag_stats_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Показать аналитику по хэштегам"""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        conn = psycopg2.connect(self.db_url)
        cursor = conn.cursor()

        # Получить хэштеги пользователя
        cursor.execute('SELECT hashtag FROM user_hashtags WHERE user_id = %s ORDER BY created_at', (user_id,))
        user_tags = [row[0] for row in cursor.fetchall()]

        if not user_tags:
            await query.edit_message_text(
                "📊 *Аналитика хэштегов*\n\n"
                "Сначала добавь хэштеги в настройках: ⚙️ → 🏷 Мои хэштеги",
                parse_mode='Markdown'
            )
            cursor.close()
            conn.close()
            return

        # Получить статистику из БД
        placeholders = ','.join(['%s'] * len(user_tags))
        cursor.execute(f'''
            SELECT hs.hashtag,
                   COUNT(DISTINCT hs.post_id) as post_count,
                   COALESCE(AVG(hs.likes), 0) as avg_likes,
                   COALESCE(AVG(hs.comments), 0) as avg_comments,
                   COUNT(DISTINCT ma.username) as accounts_count,
                   MAX(hs.fetched_at) as last_seen
            FROM hashtag_stats hs
            LEFT JOIN monitored_accounts ma ON hs.account_id = ma.id
            WHERE hs.hashtag IN ({placeholders})
            GROUP BY hs.hashtag
            ORDER BY post_count DESC
        ''', user_tags)
        rows = cursor.fetchall()

        # Также показать общую статистику по всем постам
        cursor.execute(f'''
            SELECT AVG(likes), AVG(comments)
            FROM hashtag_stats
            WHERE hashtag IN ({placeholders})
        ''', user_tags)
        overall = cursor.fetchone()
        cursor.close()
        conn.close()

        if not rows:
            text = "📊 *Аналитика хэштегов*\n\n"
            text += "По этим хэштегам ещё нет данных. Запусти парсинг аккаунтов."
        else:
            text = "📊 *Аналитика хэштегов*\n\n"
            if overall and overall[0]:
                text += f"📈 Всего: {sum(r[1] for r in rows)} постов, "
                text += f"средние лайки {overall[0]:.0f}, комменты {overall[1]:.0f}\n\n"
            text += "По хэштегам:\n\n"
            for hashtag, post_count, avg_likes, avg_comments, acc_count, last_seen in rows:
                er = (avg_likes + avg_comments * 2) / max(avg_likes + 1, 10) if avg_likes > 0 else 0
                text += f"`#{hashtag}`\n"
                text += f"  • {post_count} постов, {acc_count} аккаунтов\n"
                text += f"  • ♥ {avg_likes:.0f} 💬 {avg_comments:.0f} 📊 ER: {er:.1%}\n\n"

        keyboard = [
            [InlineKeyboardButton("🔙 К моим хэштегам", callback_data='my_hashtags')],
            [InlineKeyboardButton("🔙 Настройки", callback_data='settings')]
        ]

        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except Exception:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    async def search_hashtag_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Начать глобальный поиск по хэштегу"""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🌐 *Глобальный поиск по хэштегу*\n\n"
            "Отправь хэштег для поиска по всему Instagram.\n"
            "Например: `#грузоперевозки` или просто `грузоперевозки`\n\n"
            "Я найду последние посты с этим хэштегом и покажу их.\n\n"
            "Отправь /cancel чтобы отменить.",
            parse_mode='Markdown'
        )
        return SEARCH_HASHTAG

    async def search_hashtag_receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Получить хэштег и выполнить глобальный поиск"""
        text = update.message.text.strip()
        # Разделяем на отдельные хэштеги
        raw_tags = re.split(r'[,#\s]+', text)
        hashtags = [t.strip().lower() for t in raw_tags if t.strip()]

        if not hashtags:
            await update.message.reply_text("❌ Некорректный хэштег. Попробуй ещё раз или /cancel")
            return SEARCH_HASHTAG

        if len(hashtags) > 5:
            await update.message.reply_text("❌ Слишком много хэштегов. Максимум 5 за раз.")
            return SEARCH_HASHTAG

        if not self.parser:
            await update.message.reply_text("❌ Парсер не инициализирован.")
            return ConversationHandler.END

        status_msg = await update.message.reply_text(f"🔍 Ищу посты по {len(hashtags)} хэштегам...")

        all_posts = []
        for h in hashtags:
            try:
                await status_msg.edit_text(f"🔍 Ищу `#{h}`...", parse_mode='Markdown')
                posts = await asyncio.to_thread(self.parser.search_by_hashtag, h, 10)
                if posts:
                    for p in posts:
                        p['search_hashtag'] = h
                    all_posts.extend(posts)
            except Exception as e:
                logger.warning(f"Search error for #{h}: {e}")

        # Если HikerAPI ничего не дал — пробуем yt-dlp для Instagram
        if not all_posts and self.media_parser:
            await status_msg.edit_text("🔄 HikerAPI не дал результатов, пробую yt-dlp...")
            for h in hashtags:
                try:
                    posts = await asyncio.to_thread(
                        self.media_parser.search, f"#{h}", 5, platform="ytsearch"
                    )
                    if posts:
                        for p in posts:
                            p['search_hashtag'] = h
                            p['source'] = 'yt-dlp'
                        all_posts.extend(posts)
                except Exception as e:
                    logger.warning(f"yt-dlp fallback error for #{h}: {e}")

        if not all_posts:
            await status_msg.edit_text(
                f"❌ Постов по хэштегам не найдено.\n\n"
                f"Попробуй другие хэштеги или проверь соединение с HikerAPI.",
                parse_mode='Markdown'
            )
            return ConversationHandler.END

        # Сортируем по engagement
        all_posts.sort(key=lambda p: p.get('engagement_score', p.get('likes', 0)), reverse=True)

        total = len(all_posts)
        total_likes = sum(p.get('likes', 0) for p in all_posts)
        total_comments = sum(p.get('comments', 0) for p in all_posts)
        top_post = all_posts[0]
        hashtags_str = ', '.join(f'#{h}' for h in hashtags)

        await status_msg.edit_text(
            f"🌐 *Результаты поиска*\n\n"
            f"• Хэштеги: {hashtags_str}\n"
            f"• Найдено: {total} постов\n"
            f"• Всего лайков: {total_likes:,}\n"
            f"• Всего комментариев: {total_comments:,}\n"
            f"• Топ: ♥ {top_post.get('likes', 0)} 💬 {top_post.get('comments', 0)}\n"
            f"  от @{top_post.get('account', '?')}\n\n"
            f"Отправляю каждый пост ниже 👇",
            parse_mode='Markdown'
        )

        # Отправить каждый пост (макс 10)
        for i, post in enumerate(all_posts[:10], 1):
            caption = post.get('caption', '') or ''
            cap_display = (caption[:500] + '…') if len(caption) > 500 else (caption or '_(нет текста)_')
            likes = post.get('likes', 0)
            comments = post.get('comments', 0)
            account = post.get('account', '')
            url = post.get('url', '')
            tag = post.get('search_hashtag', '')

            # Перевод
            translated = ''
            claude_key = os.getenv('CLAUDE_API_KEY')
            if claude_key and caption and not self._is_russian(caption):
                try:
                    import anthropic
                    client = anthropic.Anthropic(api_key=claude_key)
                    msg = client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=300,
                        messages=[{"role": "user", "content": (
                            f"Переведи этот текст на русский язык. Только перевод, без комментариев:\n\n{caption[:1500]}"
                        )}]
                    )
                    translated = msg.content[0].text.strip()
                except Exception:
                    pass

            tag_label = f"[#{tag}] " if tag else ""
            post_text = f"{i}. {tag_label}📌 *@{account}*  ♥{likes} 💬{comments}\n\n{cap_display}"
            if translated:
                post_text += f"\n\n🔄 *Перевод:*\n{translated}"
            if url:
                post_text += f"\n\n🔗 {url}"

            try:
                await update.effective_chat.send_message(post_text, parse_mode='Markdown')
            except Exception:
                plain = f"{i}. {tag_label}@{account} ♥{likes} 💬{comments}\n\n{cap_display}"
                if translated:
                    plain += f"\n\nПеревод:\n{translated}"
                if url:
                    plain += f"\n{url}"
                await update.effective_chat.send_message(plain)

        if len(all_posts) > 10:
            await update.effective_chat.send_message(
                f"📌 Показано 10 из {total} постов. Нажми «🔍 Поиск по хэштегу 🌐» чтобы посмотреть ещё."
            )

        return ConversationHandler.END

    async def search_youtube_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Начать поиск на YouTube"""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "📺 *Поиск на YouTube*\n\n"
            "Отправь поисковый запрос. Например:\n"
            "`грузоперевозки казахстан` или `логистика 2025`\n\n"
            "Я найду актуальные видео и переведу описание на русский.\n\n"
            "Отправь /cancel чтобы отменить.",
            parse_mode='Markdown'
        )
        return SEARCH_YOUTUBE

    async def search_youtube_receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Получить запрос и найти на YouTube"""
        query_text = update.message.text.strip()
        if not query_text:
            await update.message.reply_text("❌ Пустой запрос. Попробуй ещё раз или /cancel")
            return SEARCH_YOUTUBE

        if not self.media_parser:
            await update.message.reply_text("❌ MediaParser не доступен (yt-dlp не установлен)")
            return ConversationHandler.END

        status_msg = await update.message.reply_text(f"📺 Ищу на YouTube: «{query_text}»...")

        try:
            videos = await asyncio.to_thread(self.media_parser.search, query_text, 8)

            if not videos:
                await status_msg.edit_text(f"❌ Ничего не найдено по запросу: «{query_text}»")
                return ConversationHandler.END

            await status_msg.edit_text(
                f"📺 *Результаты поиска:* «{query_text}»\n"
                f"Найдено: {len(videos)} видео\n"
                f"Отправляю каждое ниже 👇",
                parse_mode='Markdown'
            )

            for i, vid in enumerate(videos, 1):
                title = vid.get('title', '')
                channel = vid.get('channel', '')
                views = vid.get('views', 0)
                likes = vid.get('likes', 0)
                duration = vid.get('duration_str', '')
                url = vid.get('url', '')
                description = vid.get('description', '')

                # Перевод описания
                translated = ''
                claude_key = os.getenv('CLAUDE_API_KEY')
                if claude_key and description and not self._is_russian(description):
                    try:
                        import anthropic
                        client = anthropic.Anthropic(api_key=claude_key)
                        msg = client.messages.create(
                            model="claude-haiku-4-5-20251001",
                            max_tokens=300,
                            messages=[{"role": "user", "content": (
                                f"Переведи это описание на русский язык. Только перевод:\n\n{description[:1500]}"
                            )}]
                        )
                        translated = msg.content[0].text.strip()
                    except Exception:
                        pass

                text = f"{i}. 📺 *{title}*\n👤 {channel}  ⏱ {duration}\n👁 {views:,}  ♥ {likes:,}"
                if translated:
                    text += f"\n\n🔄 *Описание:*\n{translated}"
                text += f"\n\n🔗 {url}"

                try:
                    await update.effective_chat.send_message(text, parse_mode='Markdown')
                except Exception:
                    plain = f"{i}. {title}\n{channel} | {duration}\n👁 {views} ♥ {likes}"
                    if translated:
                        plain += f"\n\nОписание:\n{translated}"
                    plain += f"\n{url}"
                    await update.effective_chat.send_message(plain)

        except Exception as e:
            logger.error(f"YouTube search error: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ Ошибка поиска на YouTube: {str(e)[:300]}")

        return ConversationHandler.END

    async def search_tiktok_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Начать поиск в TikTok"""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🎵 *Поиск в TikTok*\n\n"
            "К сожалению, yt-dlp пока не поддерживает поиск в TikTok :(\n\n"
            "Но ты можешь попробовать:\n"
            "• 📺 **YouTube** — нажми кнопку «YouTube поиск»\n"
            "• 🌐 **Instagram** — нажми «Поиск по хэштегу»\n\n"
            "TikTok доработаю позже, когда появится нормальный API.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    async def search_tiktok_receive(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Получить запрос и найти в TikTok"""
        query_text = update.message.text.strip()
        if not query_text:
            await update.message.reply_text("❌ Пустой запрос. Попробуй ещё раз или /cancel")
            return SEARCH_TIKTOK

        if not self.media_parser:
            await update.message.reply_text("❌ MediaParser не доступен (yt-dlp не установлен)")
            return ConversationHandler.END

        status_msg = await update.message.reply_text(f"🎵 Ищу в TikTok: «{query_text}»...")

        try:
            videos = await asyncio.to_thread(self.media_parser.search_tiktok, query_text, 8)

            if not videos:
                await status_msg.edit_text(f"❌ Ничего не найдено в TikTok по запросу: «{query_text}»")
                return ConversationHandler.END

            await status_msg.edit_text(
                f"🎵 *Результаты TikTok:* «{query_text}»\n"
                f"Найдено: {len(videos)} видео\n"
                f"Отправляю каждое ниже 👇",
                parse_mode='Markdown'
            )

            for i, vid in enumerate(videos, 1):
                title = vid.get('title', '')
                channel = vid.get('channel', '')
                views = vid.get('views', 0)
                likes = vid.get('likes', 0)
                duration = vid.get('duration_str', '')
                url = vid.get('url', '')

                text = f"{i}. 🎵 *{title}*\n👤 {channel}  ⏱ {duration}\n👁 {views:,}  ♥ {likes:,}"
                text += f"\n\n🔗 {url}"

                try:
                    await update.effective_chat.send_message(text, parse_mode='Markdown')
                except Exception:
                    plain = f"{i}. {title}\n{channel} | {duration}\n👁 {views} ♥ {likes}"
                    plain += f"\n{url}"
                    await update.effective_chat.send_message(plain)

        except Exception as e:
            logger.error(f"TikTok search error: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ Ошибка поиска в TikTok: {str(e)[:300]}")

        return ConversationHandler.END

    @staticmethod
    def _is_russian(text: str) -> bool:
        """Проверить, что текст в основном на русском"""
        if not text:
            return True
        russian_chars = sum(1 for c in text if 'а' <= c.lower() <= 'я' or c.lower() == 'ё')
        total_chars = sum(1 for c in text if c.isalpha())
        return total_chars == 0 or (russian_chars / total_chars) > 0.3

    async def back_to_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Вернуться в главное меню"""
        query = update.callback_query
        await query.answer()

        keyboard = [
            [InlineKeyboardButton("🔗 Разобрать пост по ссылке", callback_data='analyze_url')],
            [InlineKeyboardButton("🚀 Начать парсинг аккаунта", callback_data='start_parsing')],
            [InlineKeyboardButton("🔍 Поиск по хэштегу 🌐", callback_data='search_hashtag')],
            [InlineKeyboardButton("📺 YouTube поиск", callback_data='search_youtube'),
             InlineKeyboardButton("🎵 TikTok поиск", callback_data='search_tiktok')],
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
Бот покажет текст поста и сделает разбор через Claude:
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
        """Получить ссылку, спарсить пост и автоматически разобрать через Claude"""
        text = update.message.text.strip()
        match = re.search(r'https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)', text)
        if not match:
            await update.message.reply_text(
                "❌ Это не похоже на ссылку Instagram.\n\n"
                "Нужна ссылка вида `https://www.instagram.com/p/...` или `/reel/...`\n\n"
                "Попробуй ещё раз или /cancel",
                parse_mode='Markdown'
            )
            return WAIT_URL

        url = match.group(0)
        status_msg = await update.message.reply_text("⏳ Получаю пост...")

        try:
            if not self.parser:
                await status_msg.edit_text("❌ Парсер не инициализирован.")
                return ConversationHandler.END

            post = await asyncio.to_thread(self.parser.get_post_by_url, url)
            if not post:
                await status_msg.edit_text(
                    "❌ Не удалось получить пост.\n\n"
                    "Возможные причины: закрытый аккаунт, удалённый пост или неверная ссылка."
                )
                return ConversationHandler.END

            caption = post.get('caption', '') or ''
            account = post.get('account', '')
            likes = post.get('likes', 0)
            comments = post.get('comments', 0)

            # Показать сырой текст поста
            cap_display = (caption[:800] + '…') if len(caption) > 800 else caption
            raw_text = f"📌 *Пост{' @' + account if account else ''}*\n\n"
            raw_text += cap_display if cap_display else '_(текст отсутствует — только медиа)_'
            raw_text += f"\n\n❤️ {likes}  💬 {comments}  🔗 {url}"

            try:
                await status_msg.edit_text(raw_text, parse_mode='Markdown')
            except Exception:
                await status_msg.edit_text(f"Пост {'@' + account if account else ''}\n\n{cap_display or '(нет текста)'}\n{url}")

            # Если нет текста — попробовать GPT-4o Vision по thumbnail
            if not caption:
                thumbnail_url = post.get('thumbnail_url')
                if not thumbnail_url:
                    keyboard = [[InlineKeyboardButton("🔗 Разобрать другой пост", callback_data='analyze_url'),
                                 InlineKeyboardButton("🔙 Меню", callback_data='back')]]
                    await update.effective_chat.send_message(
                        "⚠️ В этом посте нет текста и не удалось получить превью видео.",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    return ConversationHandler.END

                openai_key = os.getenv('OPENAI_API_KEY')
                if not openai_key:
                    keyboard = [[InlineKeyboardButton("🔗 Разобрать другой пост", callback_data='analyze_url'),
                                 InlineKeyboardButton("🔙 Меню", callback_data='back')]]
                    await update.effective_chat.send_message(
                        "⚠️ Пост без текста — нужен OPENAI_API_KEY для анализа видео через GPT-4o Vision.\n"
                        "Добавь его в Railway Variables.",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    return ConversationHandler.END

                await update.effective_chat.send_message("🎥 Пост без текста — анализирую видео через GPT-4o Vision...")

                try:
                    import base64
                    import requests as _req
                    from openai import OpenAI as _OpenAI

                    # Скачать превью сами — Instagram CDN не отдаёт напрямую OpenAI
                    img_resp = _req.get(
                        thumbnail_url,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
                            'Referer': 'https://www.instagram.com/',
                        },
                        timeout=20
                    )
                    img_resp.raise_for_status()
                    content_type = img_resp.headers.get('content-type', 'image/jpeg').split(';')[0]
                    img_b64 = base64.b64encode(img_resp.content).decode('utf-8')
                    img_data_uri = f"data:{content_type};base64,{img_b64}"

                    oa_client = _OpenAI(api_key=openai_key)
                    vision_resp = oa_client.chat.completions.create(
                        model="gpt-4o",
                        messages=[{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": img_data_uri}},
                                {"type": "text", "text": (
                                    "Это превью видео/Reel из Instagram. "
                                    "Подробно опиши: что показано, кто в кадре, тема, настроение, "
                                    "о чём вероятно этот видео-пост. Ответь на русском языке."
                                )}
                            ]
                        }],
                        max_tokens=600
                    )
                    visual_desc = vision_resp.choices[0].message.content.strip()
                    logger.info(f"GPT-4o Vision description: {visual_desc[:100]}...")
                    try:
                        await update.effective_chat.send_message(
                            f"👁 *GPT-4o видит:*\n\n{visual_desc}",
                            parse_mode='Markdown'
                        )
                    except Exception:
                        await update.effective_chat.send_message(f"GPT-4o видит:\n\n{visual_desc}")

                    # Whisper: транскрипция речи из видео
                    transcript_text = ''
                    video_url = post.get('video_url')
                    if video_url:
                        await update.effective_chat.send_message("🎤 Слушаю что говорят в видео (Whisper)...")
                        try:
                            import io
                            ig_headers = {
                                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
                                'Referer': 'https://www.instagram.com/',
                            }
                            # Проверить размер файла перед скачиванием
                            head = _req.head(video_url, headers=ig_headers, timeout=10)
                            size = int(head.headers.get('content-length', 0))
                            if 0 < size <= 24 * 1024 * 1024:  # не больше 24 МБ (лимит Whisper 25 МБ)
                                vid_data = _req.get(video_url, headers=ig_headers, timeout=60)
                                vid_data.raise_for_status()
                                audio_buf = io.BytesIO(vid_data.content)
                                audio_buf.name = "video.mp4"
                                transcript = oa_client.audio.transcriptions.create(
                                    model="whisper-1",
                                    file=audio_buf
                                )
                                transcript_text = transcript.text.strip()
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
                            else:
                                await update.effective_chat.send_message(
                                    f"⚠️ Видео слишком большое ({size // 1024 // 1024} МБ) — транскрипция пропущена"
                                )
                        except Exception as we:
                            logger.warning(f"Whisper failed: {we}")
                            await update.effective_chat.send_message(f"⚠️ Whisper не смог транскрибировать: {str(we)[:150]}")

                    # Объединить визуальное описание + транскрипцию для Claude
                    if transcript_text:
                        caption = f"[Что видно в кадре]: {visual_desc}\n\n[Что говорит человек]: {transcript_text}"
                    else:
                        caption = visual_desc

                except Exception as ve:
                    logger.error(f"GPT-4o Vision error: {ve}", exc_info=True)
                    keyboard = [[InlineKeyboardButton("🔗 Разобрать другой пост", callback_data='analyze_url'),
                                 InlineKeyboardButton("🔙 Меню", callback_data='back')]]
                    await update.effective_chat.send_message(
                        f"❌ GPT-4o Vision не смог проанализировать видео: {str(ve)[:200]}",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    return ConversationHandler.END

            # Автоматический анализ через Claude
            await update.effective_chat.send_message("🤖 Разбираю через Claude...")

            claude_key = os.getenv('CLAUDE_API_KEY')
            if not claude_key:
                await update.effective_chat.send_message("❌ CLAUDE_API_KEY не задан в Railway — анализ недоступен.")
                return ConversationHandler.END

            # Выбрать промпт в зависимости от типа контента
            has_speech = "[Что говорит человек]" in caption
            has_visual = "[Что видно в кадре]" in caption

            if has_speech and has_visual:
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

            client = anthropic.Anthropic(api_key=claude_key)
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )
            result = message.content[0].text.strip()

            # Сохранить контекст для "Создать промпт"
            context.user_data['last_analysis'] = result
            context.user_data['last_raw_content'] = caption

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
            await update.effective_chat.send_message(f"❌ Ошибка: {type(e).__name__}: {str(e)[:300]}")

        return ConversationHandler.END

    async def create_prompt_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Сгенерировать готовый промпт для реализации идеи через Claude"""
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

        claude_key = os.getenv('CLAUDE_API_KEY')
        if not claude_key:
            await query.edit_message_text("❌ CLAUDE_API_KEY не задан в Railway")
            return

        try:
            prompt = (
                "На основе анализа видео-поста сгенерируй готовый промпт, который пользователь скопирует "
                "и вставит в чат Claude или ChatGPT для реализации идеи из этого поста.\n\n"
                "Промпт должен:\n"
                "1. Содержать весь необходимый контекст (что за идея, откуда взята)\n"
                "2. Чётко ставить задачу на реализацию (что нужно сделать)\n"
                "3. Запрашивать: технологический стек, пошаговый план, с чего начать\n"
                "4. Быть на русском языке\n"
                "5. Начинаться с фразы: 'Я хочу реализовать следующую идею...'\n\n"
                "ВАЖНО: выдай ТОЛЬКО сам промпт — без пояснений, без обёрток, "
                "без 'вот твой промпт:'. Просто текст готового промпта.\n\n"
                f"Анализ поста:\n{analysis}\n\n"
                f"Исходный контент:\n{raw_content[:1500]}"
            )

            client = anthropic.Anthropic(api_key=claude_key)
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
            ready_prompt = message.content[0].text.strip()

            keyboard = [
                [InlineKeyboardButton("🔗 Разобрать другой пост", callback_data='analyze_url')],
                [InlineKeyboardButton("🔙 Главное меню", callback_data='back')],
            ]

            header = "📋 *Готовый промпт — скопируй и вставь в Claude:*\n\n"
            try:
                await query.edit_message_text(
                    header + ready_prompt,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
            except Exception:
                await query.edit_message_text(
                    f"Готовый промпт:\n\n{ready_prompt}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        except Exception as e:
            logger.error(f"create_prompt_action error: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Ошибка генерации промпта: {str(e)[:200]}")

    async def handle_instagram_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Получить пост по ссылке Instagram и показать содержимое"""
        text = update.message.text.strip()
        match = re.search(r'https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)', text)
        if not match:
            return

        url = match.group(0)
        msg = await update.message.reply_text("⏳ Получаю пост по ссылке...")

        try:
            if not self.parser:
                await msg.edit_text("❌ Парсер не инициализирован.")
                return

            post = await asyncio.to_thread(self.parser.get_post_by_url, url)
            if not post:
                await msg.edit_text("❌ Не удалось получить пост. Проверь ссылку.")
                return

            context.user_data['last_post'] = post

            caption = post.get('caption', '') or ''
            account = post.get('account', '')
            likes = post.get('likes', 0)
            comments = post.get('comments', 0)

            cap_display = (caption[:800] + '…') if len(caption) > 800 else caption

            header = f"📌 *Пост{' от @' + account if account else ''}*\n\n"
            body = cap_display if cap_display else '_(текст отсутствует — только медиа)_'
            footer = f"\n\n❤️ {likes}  💬 {comments}\n🔗 {url}"

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
        """Анализировать пост через Claude по выбранному действию"""
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
        await query.edit_message_text("🤖 Claude думает...")

        try:
            claude_key = os.getenv('CLAUDE_API_KEY')
            if not claude_key:
                await query.edit_message_text("❌ CLAUDE_API_KEY не задан в Railway")
                return

            client = anthropic.Anthropic(api_key=claude_key)
            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
            result = message.content[0].text.strip()

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
            await query.edit_message_text(f"❌ Ошибка Claude: {str(e)[:300]}")

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

        # Claude-анализ поста (кнопки на inline-сообщении)
        self.application.add_handler(
            CallbackQueryHandler(self.analyze_post_action,
                                 pattern='^analyze_(summary|ideas|adapt)$')
        )

        # Создать промпт для реализации
        self.application.add_handler(
            CallbackQueryHandler(self.create_prompt_action, pattern='^create_prompt$')
        )

        # ── Хэндлеры для хэштегов ──
        self.application.add_handler(
            CallbackQueryHandler(self.my_hashtags_handler, pattern='^my_hashtags$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.hashtag_clear, pattern='^hashtag_clear$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.hashtag_stats_handler, pattern='^hashtag_stats$')
        )

        # ConversationHandler для добавления хэштега
        hashtag_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.hashtag_add_start, pattern='^hashtag_add$')],
            states={
                ADD_HASHTAG: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.hashtag_add_save)]
            },
            fallbacks=[CommandHandler('cancel', self.cancel_conversation)]
        )
        self.application.add_handler(hashtag_conv)

        # ── Глобальный поиск по хэштегу ──
        search_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.search_hashtag_start, pattern='^search_hashtag$')],
            states={
                SEARCH_HASHTAG: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.search_hashtag_receive)]
            },
            fallbacks=[CommandHandler('cancel', self.cancel_conversation)]
        )
        self.application.add_handler(search_conv)

        # ── YouTube поиск ──
        youtube_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.search_youtube_start, pattern='^search_youtube$')],
            states={
                SEARCH_YOUTUBE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.search_youtube_receive)]
            },
            fallbacks=[CommandHandler('cancel', self.cancel_conversation)]
        )
        self.application.add_handler(youtube_conv)

        # ── TikTok поиск ──
        tiktok_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.search_tiktok_start, pattern='^search_tiktok$')],
            states={
                SEARCH_TIKTOK: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.search_tiktok_receive)]
            },
            fallbacks=[CommandHandler('cancel', self.cancel_conversation)]
        )
        self.application.add_handler(tiktok_conv)

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
