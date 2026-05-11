import os
import psycopg2
import json
import logging
import asyncio
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
WAIT_SESSIONID = 100


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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обработчик /start"""
        keyboard = [
            [InlineKeyboardButton("🚀 Начать парсинг", callback_data='start_parsing')],
            [InlineKeyboardButton("➕ Добавить аккаунт", callback_data='add_account')],
            [InlineKeyboardButton("📋 Мои аккаунты", callback_data='list_accounts')],
            [InlineKeyboardButton("📊 Получить дайджест", callback_data='get_digest')],
            [InlineKeyboardButton("📈 Статистика", callback_data='stats')],
            [InlineKeyboardButton("⏸️ Пауза / ▶️ Возобновить", callback_data='pause')],
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
                ORDER BY a.relevance_score DESC
            ''', (since.isoformat(),))

            rows = cursor.fetchall()
            cursor.close()
            conn.close()

            analyses = []
            for row in rows:
                analyses.append({
                    'sentiment': row[0],
                    'key_topics': json.loads(row[1]),
                    'relevance_score': row[2],
                    'viral_potential': row[3],
                    'recommendations': json.loads(row[4])
                })

            digest_text = DigestFormatter.format_daily_digest(analyses)
            await query.edit_message_text(digest_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error getting digest: {e}")
            await query.edit_message_text("❌ Ошибка при получении дайджеста")

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
            [InlineKeyboardButton("➕ Добавить аккаунт", callback_data='add_account')],
            [InlineKeyboardButton("📋 Мои аккаунты", callback_data='list_accounts')],
            [InlineKeyboardButton("📊 Получить дайджест", callback_data='get_digest')],
            [InlineKeyboardButton("📈 Статистика", callback_data='stats')],
            [InlineKeyboardButton("⏸️ Пауза / ▶️ Возобновить", callback_data='pause')],
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

            results = []
            total_posts = 0

            for acc_id, username, num_posts, min_likes, check_interval_hours in accounts:
                try:
                    logger.info(f"Parsing {username} ({num_posts} posts, min_likes={min_likes})...")

                    await query.edit_message_text(
                        f"⏳ *Парсинг в процессе...*\n\n"
                        f"📱 Обработка: @{username}\n"
                        f"📊 Постов к парсингу: {num_posts}\n\n"
                        f"⏱️ Это займет ~{num_posts * 15}сек...",
                        parse_mode='Markdown'
                    )

                    # Вызвать парсер (логиниться под основным аккаунтом, парсить целевой аккаунт)
                    posts = self.parser.monitor_account(
                        target_username=username,
                        login_username=self.instagram_username,
                        login_password=self.instagram_password,
                        num_posts=num_posts
                    )

                    if posts:
                        # Профильтровать посты
                        filtered = self.filter.process_posts(posts)

                        # Обогатить посты данными
                        for i, post in enumerate(posts):
                            if i < len(filtered):
                                post.update({
                                    'engagement_rate': filtered[i].get('engagement_rate', 0),
                                    'text_length': filtered[i].get('text_length', 0)
                                })

                        # Анализировать посты
                        analyses = self.analyzer.process_posts(posts)

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

                        total_posts += len(posts)
                        results.append({
                            'username': username,
                            'posts_parsed': len(posts),
                            'status': '✅'
                        })
                    else:
                        results.append({
                            'username': username,
                            'posts_parsed': 0,
                            'status': '⚠️ Нет новых постов'
                        })

                except Exception as e:
                    error_msg = f"{type(e).__name__}: {str(e)}"
                    logger.error(f"Error parsing {username}: {error_msg}", exc_info=True)
                    results.append({
                        'username': username,
                        'posts_parsed': 0,
                        'status': '❌',
                        'error': error_msg
                    })

            # Показать результаты
            has_errors = any(r.get('error') for r in results)
            if total_posts > 0 and not has_errors:
                result_text = f"✅ *Парсинг завершен!*\n\n"
            elif total_posts > 0:
                result_text = f"⚠️ *Парсинг завершен с ошибками*\n\n"
            else:
                result_text = f"❌ *Парсинг не получил постов*\n\n"

            result_text += f"📊 Всего постов спарсено: {total_posts}\n\n"
            result_text += "*По аккаунтам:*\n"
            for result in results:
                result_text += f"{result['status']} @{result['username']} — {result['posts_parsed']} постов\n"
                if result.get('error'):
                    err = result['error'][:300]
                    result_text += f"   `{err}`\n"

            if has_errors or total_posts == 0:
                result_text += "\n💡 Нажми */debug* для подробных логов\n"
                result_text += "💡 Если Instagram блокирует логин — нажми */sessionid* и установи sessionid из браузера"

            keyboard = [
                [InlineKeyboardButton("🔍 Подробные логи", callback_data='debug_logs')],
                [InlineKeyboardButton("🔑 Установить sessionid", callback_data='set_sessionid')],
                [InlineKeyboardButton("📊 Получить дайджест", callback_data='get_digest')],
                [InlineKeyboardButton("🔙 Назад в меню", callback_data='back')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Markdown может сломаться на спецсимволах в ошибке — подстраховываемся
            try:
                await query.edit_message_text(result_text, reply_markup=reply_markup, parse_mode='Markdown')
            except Exception:
                await query.edit_message_text(result_text, reply_markup=reply_markup)

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

        try:
            if self.parser:
                ok = self.parser.login_manager.save_sessionid(self.instagram_username, sessionid)
            else:
                ok = False

            if ok:
                await update.effective_chat.send_message(
                    f"✅ sessionid сохранён для @{self.instagram_username}\n\n"
                    "Теперь нажми /start → 'Начать парсинг' для проверки."
                )
            else:
                await update.effective_chat.send_message(
                    "❌ Не удалось сохранить sessionid. Посмотри /debug"
                )
        except Exception as e:
            logger.error(f"Error saving sessionid: {e}", exc_info=True)
            await update.effective_chat.send_message(f"❌ Ошибка: {type(e).__name__}: {e}")

        return ConversationHandler.END

    async def cancel_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Отменить текущий диалог"""
        await update.message.reply_text("❌ Отменено.")
        return ConversationHandler.END

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Справка - работает как прямая команда и как кнопка"""
        help_text = """
📚 *СПРАВКА Instagram Monitor Bot*

*━━━━━━━━━━━━━━━━━*
*🔧 ОСНОВНЫЕ КОМАНДЫ:*
━━━━━━━━━━━━━━━━━

/start - главное меню
/help - эта справка

*━━━━━━━━━━━━━━━━━*
*📱 ФУНКЦИИ И КНОПКИ:*
━━━━━━━━━━━━━━━━━

*➕ Добавить аккаунт*
Формат: введите имя пользователя Instagram
Примеры: @instagram или instagram (без @)
Сохраняется для ежедневного мониторинга

*📋 Мои аккаунты*
Показывает список всех отслеживаемых аккаунтов
Дата добавления и статус активности

*📊 Получить дайджест*
Ежедневный анализ постов за последние 24 часа
- Количество постов
- Средняя релевантность
- Определение вирусного контента
- Анализ настроения (sentiment)

*📈 Статистика*
Общая статистика по всем постам:
- Всего проанализировано постов
- Средний engagement rate
- Распределение по типам контента
- Тренды и рекомендации

*⚙️ Настройки*
Конфигурация бота:
- Интервал проверки (мин)
- Часовой пояс
- Время отправки дайджеста
- Порог релевантности

*⏸️ Пауза / ▶️ Возобновить*
Управление мониторингом
- Пауза: остановит все проверки
- Возобновить: продолжит мониторинг

*━━━━━━━━━━━━━━━━━*
*❓ КАК НАЧАТЬ:*
━━━━━━━━━━━━━━━━━

1️⃣ Нажмите "Добавить аккаунт"
2️⃣ Введите имя пользователя (например: instagram)
3️⃣ Подождите подтверждения
4️⃣ Получайте ежедневный дайджест в 09:00

*━━━━━━━━━━━━━━━━━*
*ℹ️ ВАЖНАЯ ИНФОРМАЦИЯ:*
━━━━━━━━━━━━━━━━━

• Дайджесты отправляются ежедневно в 09:00 (МСК)
• Анализ выполняется через Claude AI
• Данные хранятся в защищенной БД
• Проверка новых постов каждый час
"""

        if update.callback_query:
            query = update.callback_query
            await query.answer()
            await query.edit_message_text(help_text, parse_mode='Markdown')
        else:
            await update.message.reply_text(help_text, parse_mode='Markdown')

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
                            'sentiment': row[0],
                            'key_topics': json.loads(row[1]),
                            'relevance_score': row[2],
                            'viral_potential': row[3],
                            'recommendations': json.loads(row[4])
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
