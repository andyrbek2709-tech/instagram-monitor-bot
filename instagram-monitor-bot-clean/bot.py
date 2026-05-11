import os
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler, ConversationHandler,
                          CallbackQueryHandler, filters, ContextTypes)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import random

logger = logging.getLogger(__name__)

ADD_ACCOUNT, CONFIRM_ACCOUNT = range(2)


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

    def __init__(self, token: str, db_path: str):
        self.token = token
        self.db_path = db_path
        self.application = None

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обработчик /start"""
        keyboard = [
            [InlineKeyboardButton("➕ Добавить аккаунт", callback_data='add_account')],
            [InlineKeyboardButton("📋 Мои аккаунты", callback_data='list_accounts')],
            [InlineKeyboardButton("📊 Получить дайджест", callback_data='get_digest')],
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
        """Подтвердить добавление аккаунта"""
        username = update.message.text.strip()

        # Сохранить в БД
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT OR IGNORE INTO monitored_accounts (username, session_key, created_at) VALUES (?, ?, ?)',
                    (username, '', datetime.utcnow().isoformat())
                )

                # Добавить пользователя Telegram
                cursor.execute(
                    'INSERT OR IGNORE INTO telegram_users (user_id, username, created_at) VALUES (?, ?, ?)',
                    (update.message.from_user.id, update.message.from_user.username or 'unknown',
                     datetime.utcnow().isoformat())
                )
                conn.commit()

            await update.message.reply_text(
                f"✅ Аккаунт @{username} добавлен для мониторинга!"
            )
        except Exception as e:
            logger.error(f"Error adding account: {e}")
            await update.message.reply_text("❌ Ошибка при добавлении аккаунта")

        return ConversationHandler.END

    async def list_accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Список отслеживаемых аккаунтов"""
        query = update.callback_query
        await query.answer()

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT username, last_fetch FROM monitored_accounts')
                accounts = cursor.fetchall()

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
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Получить анализы за последние 24 часа
                since = datetime.utcnow() - timedelta(hours=24)
                cursor.execute('''
                    SELECT a.sentiment, a.key_topics, a.relevance_score,
                           a.viral_potential, a.recommendations
                    FROM analyses a
                    WHERE a.analyzed_at > ?
                    ORDER BY a.relevance_score DESC
                ''', (since.isoformat(),))

                rows = cursor.fetchall()
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

        keyboard = [
            [InlineKeyboardButton("🔔 Время дайджеста", callback_data='digest_time')],
            [InlineKeyboardButton("📏 Минимальная релевантность", callback_data='min_relevance')],
            [InlineKeyboardButton("🔙 Назад", callback_data='back')]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("⚙️ Настройки бота", reply_markup=reply_markup)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Справка"""
        query = update.callback_query
        await query.answer()

        help_text = """
📚 *Справка Instagram Monitor Bot*

*Основные команды:*
/start - главное меню
/help - эта справка

*Функции:*
✅ Мониторинг постов Instagram
✅ Анализ контента через AI
✅ Определение вирусного потенциала
✅ Ежедневные дайджесты

*Как начать:*
1. Нажмите "Добавить аккаунт"
2. Введите имя пользователя
3. Получайте ежедневные анализы

Вопросы? Свяжитесь с поддержкой.
"""

        await query.edit_message_text(help_text, parse_mode='Markdown')

    def setup(self) -> Application:
        """Настроить приложение Telegram"""
        self.application = Application.builder().token(self.token).build()

        # Обработчики команд
        self.application.add_handler(CommandHandler('start', self.start))
        self.application.add_handler(CommandHandler('help', self.help_command))

        # Обработчик добавления аккаунта
        conv_handler = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_account, pattern='^add_account$')],
            states={
                ADD_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.confirm_account)]
            },
            fallbacks=[]
        )
        self.application.add_handler(conv_handler)

        # Обработчики кнопок
        self.application.add_handler(
            CallbackQueryHandler(self.list_accounts, pattern='^list_accounts$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.get_digest, pattern='^get_digest$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.settings_command, pattern='^settings$')
        )
        self.application.add_handler(
            CallbackQueryHandler(self.help_command, pattern='^help$')
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
    """Управление расписанием для ежедневных дайджестов"""

    def __init__(self, bot: TelegramBot, db_path: str):
        self.bot = bot
        self.db_path = db_path
        self.scheduler = BackgroundScheduler()

    def _get_scheduled_time(self) -> Tuple[int, int]:
        """Получить время отправки с jitter"""
        hour, minute = 9, 0
        jitter_minutes = random.randint(-15, 15)
        total_minutes = hour * 60 + minute + jitter_minutes
        final_hour = (total_minutes // 60) % 24
        final_minute = total_minutes % 60
        return final_hour, final_minute

    async def daily_digest_job(self) -> None:
        """Ежедневная работа отправки дайджестов"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Получить всех пользователей
                cursor.execute('SELECT id, user_id FROM telegram_users')
                users = cursor.fetchall()

                since = datetime.utcnow() - timedelta(hours=24)

                for user_db_id, user_id in users:
                    # Получить анализы
                    cursor.execute('''
                        SELECT a.sentiment, a.key_topics, a.relevance_score,
                               a.viral_potential, a.recommendations
                        FROM analyses a
                        WHERE a.analyzed_at > ?
                        ORDER BY a.relevance_score DESC
                    ''', (since.isoformat(),))

                    rows = cursor.fetchall()
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

                    logger.info(f"Sent digest to user {user_id}")

        except Exception as e:
            logger.error(f"Error in daily_digest_job: {e}")

    def start_scheduler(self) -> None:
        """Запустить планировщик"""
        hour, minute = self._get_scheduled_time()
        trigger = CronTrigger(hour=hour, minute=minute)

        self.scheduler.add_job(
            self.daily_digest_job,
            trigger=trigger,
            id='daily_digest'
        )

        self.scheduler.start()
        logger.info(f"Scheduler started. Daily digest at {hour:02d}:{minute:02d}")

    def stop_scheduler(self) -> None:
        """Остановить планировщик"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler stopped")
