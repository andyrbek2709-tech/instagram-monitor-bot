import os
import logging
from datetime import datetime
from typing import List, Dict
from dotenv import load_dotenv

# Загрузить переменные окружения
load_dotenv()

# Создать необходимые директории перед настройкой логирования
def _ensure_directories() -> None:
    """Создать необходимые директории"""
    directories = ['logs', 'data', 'data/media', 'data/sessions', 'database']
    for directory in directories:
        os.makedirs(directory, exist_ok=True)

_ensure_directories()

# Импорты модулей проекта
from parser import Parser
from filter import Filter
from analyzer import Analyzer
from bot import TelegramBot, SchedulerManager
from db_init import DatabaseInitializer
from maintenance import DataMaintenance
from validator import AccountValidator

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/bot.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)


class InstagramMonitorBotPipeline:
    """Основной класс для orchestration всего пайплайна"""

    def __init__(self):
        self.parser = None
        self.filter = None
        self.analyzer = None
        self.telegram_bot = None
        self.scheduler_manager = None
        self.maintenance = None
        self.validator = None

        # Пути
        self.db_url = os.getenv('DATABASE_URL')
        self.instagram_username = os.getenv('INSTAGRAM_USERNAME')
        self.instagram_password = os.getenv('INSTAGRAM_PASSWORD')

    def validate_environment(self) -> bool:
        """Проверить наличие всех необходимых переменных окружения"""
        required_env_vars = [
            'DATABASE_URL',
            'TELEGRAM_BOT_TOKEN',
            'HIKER_API_KEY',
        ]

        missing = []
        for var in required_env_vars:
            if not os.getenv(var):
                missing.append(var)

        if missing:
            logger.error(f"Missing environment variables: {', '.join(missing)}")
            return False

        # Опциональные ключи — предупреждение, но не блокировка
        optional_vars = ['GEMINI_API_KEY', 'OPENAI_API_KEY']
        for var in optional_vars:
            if not os.getenv(var):
                logger.warning(f"Optional env var not set: {var} (some analysis features will be degraded)")

        logger.info("Environment variables validated")
        return True

    def ensure_directories(self) -> None:
        """Создать необходимые директории"""
        directories = [
            'logs',
            'data',
            'data/media',
            'data/sessions',
            'database'
        ]

        for directory in directories:
            os.makedirs(directory, exist_ok=True)

        logger.info(f"Directories ensured: {directories}")

    def initialize_components(self) -> bool:
        """Инициализировать все компоненты"""
        try:
            # Инициализировать БД
            db_init = DatabaseInitializer(self.db_url)
            if not db_init.initialize():
                return False

            # Создать компоненты
            self.parser = Parser(self.db_url)
            self.filter = Filter(self.db_url, os.getenv('OPENAI_API_KEY'))
            self.analyzer = Analyzer(self.db_url, os.getenv('GEMINI_API_KEY'))
            self.telegram_bot = TelegramBot(
                os.getenv('TELEGRAM_BOT_TOKEN'),
                self.db_url,
                parser=self.parser,
                filter_obj=self.filter,
                analyzer=self.analyzer
            )
            self.maintenance = DataMaintenance(self.db_url)
            self.validator = AccountValidator(self.db_url)

            logger.info("All components initialized successfully")
            return True

        except Exception as e:
            logger.error(f"Component initialization failed: {e}")
            return False

    def run_pipeline_cycle(self) -> None:
        """Выполнить один цикл пайплайна"""
        try:
            logger.info("Starting pipeline cycle")

            # Этап 0: Обслуживание
            logger.info("Stage 0: Maintenance")
            self.maintenance.run_maintenance()

            # Этап 1: Парсинг (пример с основным аккаунтом)
            logger.info("Stage 1: Parsing")
            posts = self.parser.monitor_account(
                target_username=self.instagram_username,
                login_username=self.instagram_username,
                login_password=self.instagram_password,
                num_posts=10
            )

            if not posts:
                logger.warning("No posts parsed")
                return

            # Этап 2: Фильтрация
            logger.info("Stage 2: Filtering")
            filtered = self.filter.process_posts(posts)

            # Обогатить посты данными из filter_results
            for i, post in enumerate(posts):
                if i < len(filtered):
                    post.update({
                        'engagement_rate': filtered[i].get('engagement_rate', 0),
                        'text_length': filtered[i].get('text_length', 0)
                    })

            # Этап 3: Анализ
            logger.info("Stage 3: Analyzing")
            analyses = self.analyzer.process_posts(posts)

            logger.info(f"Pipeline cycle completed: {len(posts)} posts processed")

        except Exception as e:
            logger.error(f"Pipeline cycle error: {e}")

    async def start(self) -> None:
        """Запустить бота с планировщиком"""
        try:
            # Настроить Telegram бота
            self.telegram_bot.setup()

            # Запустить планировщик
            self.scheduler_manager = SchedulerManager(self.telegram_bot, self.db_url)
            self.scheduler_manager.start_scheduler()

            # Запустить бота
            logger.info("Starting Telegram bot")
            await self.telegram_bot.application.run_polling()

        except Exception as e:
            logger.error(f"Bot startup error: {e}", exc_info=True)

    def run_manual_cycle(self) -> None:
        """Запустить один цикл вручную (для тестирования)"""
        logger.info("Running manual pipeline cycle")
        self.run_pipeline_cycle()


def main():
    """Главная функция"""

    # Создать пайплайн
    pipeline = InstagramMonitorBotPipeline()

    # Проверить окружение
    if not pipeline.validate_environment():
        logger.error("Environment validation failed")
        return

    # Создать директории
    pipeline.ensure_directories()

    # Инициализировать компоненты
    if not pipeline.initialize_components():
        logger.error("Component initialization failed")
        return

    # Настроить Telegram бота
    pipeline.telegram_bot.setup()

    # Запустить планировщик
    pipeline.scheduler_manager = SchedulerManager(pipeline.telegram_bot, pipeline.db_url)
    pipeline.scheduler_manager.start_scheduler()

    # Запустить бота с polling (это блокирующий вызов, управляет собственным event loop)
    logger.info("Starting Telegram bot polling")
    pipeline.telegram_bot.application.run_polling()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
