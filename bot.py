import os
import sqlite3
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
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Общая статистика
                cursor.execute('SELECT COUNT(*) FROM posts')
                total_posts = cursor.fetchone()[0]

                cursor.execute('SELECT COUNT(*) FROM monitored_accounts WHERE is_active = 1')
                active_accounts = cursor.fetchone()[0]

                cursor.execute('SELECT AVG(relevance_score) FROM analyses')
                avg_relevance = cursor.fetchone()[0] or 0

                cursor.execute('''
                    SELECT COUNT(*) FROM analyses
                    WHERE relevance_score > 0.7
                    AND analyzed_at > datetime('now', '-7 days')
                ''')
                high_relevance_7d = cursor.fetchone()[0]

                cursor.execute('''
                    SELECT COUNT(*) FROM analyses
                    WHERE viral_potential = 'high'
                    AND analyzed_at > datetime('now', '-7 days')
                ''')
                viral_7d = cursor.fetchone()[0]

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
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                settings = json.dumps({"paused": True})
                cursor.execute(
                    'UPDATE telegram_users SET settings = ? WHERE user_id = ?',
                    (settings, user_id)
                )
                conn.commit()

            await query.edit_message_text("⏸️ Мониторинг приостановлен")
        except Exception as e:
            logger.error(f"Error pausing monitoring: {e}")
            await query.edit_message_text("❌ Ошибка при приостановке")

    async def resume_monitoring(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Возобновить мониторинг"""
        query = update.callback_query
        await query.answer()

        try:
            user_id = update.effective_user.id
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                settings = json.dumps({"paused": False})
                cursor.execute(
                    'UPDATE telegram_users SET settings = ? WHERE user_id = ?',
                    (settings, user_id)
                )
                conn.commit()

            await query.edit_message_text("▶️ Мониторинг возобновлен")
        except Exception as e:
            logger.error(f"Error resuming monitoring: {e}")
            await query.edit_message_text("❌ Ошибка при возобновлении")

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
            CallbackQueryHandler(self.back_to_menu, pattern='^settings$')
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

    def __init__(self, bot: TelegramBot, db_path: str):
        self.bot = bot
        self.db_path = db_path
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
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Получить всех активных пользователей
                cursor.execute('SELECT id, user_id FROM telegram_users WHERE settings IS NULL OR json_extract(settings, "$.paused") = 0')
                users = cursor.fetchall()

                if not users:
                    logger.info("No active users for digest")
                    return

                since = datetime.utcnow() - timedelta(hours=24)

                for user_db_id, user_id in users:
                    try:
                        # Получить анализы
                        cursor.execute('''
                            SELECT a.sentiment, a.key_topics, a.relevance_score,
                                   a.viral_potential, a.recommendations
                            FROM analyses a
                            WHERE a.analyzed_at > ?
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
