import os
import logging
import psycopg2
from psycopg2 import sql
from datetime import datetime

logger = logging.getLogger(__name__)


class DatabaseInitializer:
    """Инициализация PostgreSQL базы данных"""

    def __init__(self, db_url: str = None):
        if db_url is None:
            db_url = os.getenv('DATABASE_URL')
            if not db_url:
                raise ValueError("DATABASE_URL environment variable is required")
        self.db_url = db_url

    def get_connection(self):
        """Получить подключение к БД"""
        return psycopg2.connect(self.db_url)

    def create_tables(self) -> None:
        """Создать все необходимые таблицы"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Таблица monitored_accounts
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitored_accounts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT UNIQUE NOT NULL,
                    session_key TEXT,
                    num_posts INTEGER DEFAULT 10,
                    min_likes INTEGER DEFAULT 0,
                    check_interval_hours INTEGER DEFAULT 24,
                    last_fetch TIMESTAMP,
                    next_check TIMESTAMP,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP NOT NULL
                )
            ''')

            # Таблица posts
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS posts (
                    id SERIAL PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES monitored_accounts(id),
                    post_id TEXT NOT NULL,
                    url TEXT,
                    caption TEXT,
                    media_type INTEGER,
                    media_path TEXT,
                    content_hash TEXT UNIQUE,
                    fetched_at TIMESTAMP,
                    UNIQUE (account_id, post_id)
                )
            ''')

            # Индексы для posts
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_account_id ON posts(account_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_content_hash ON posts(content_hash)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_posts_fetched_at ON posts(fetched_at)')

            # Таблица filter_results
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS filter_results (
                    id SERIAL PRIMARY KEY,
                    post_id INTEGER NOT NULL UNIQUE REFERENCES posts(id),
                    is_ad INTEGER DEFAULT 0,
                    is_greeting INTEGER DEFAULT 0,
                    is_personal INTEGER DEFAULT 0,
                    engagement_rate REAL,
                    text_length INTEGER,
                    has_media INTEGER DEFAULT 0,
                    analyzed_at TIMESTAMP
                )
            ''')

            # Индексы для filter_results
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_filter_is_ad ON filter_results(is_ad)')

            # Таблица analyses
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS analyses (
                    id SERIAL PRIMARY KEY,
                    post_id INTEGER NOT NULL UNIQUE REFERENCES posts(id),
                    sentiment TEXT,
                    key_topics TEXT,
                    brand_mentions TEXT,
                    audience_segment TEXT,
                    content_quality TEXT,
                    relevance_score REAL,
                    viral_potential TEXT,
                    recommendations TEXT,
                    analyzed_at TIMESTAMP
                )
            ''')

            # Таблица для дневной статистики
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_stats (
                    id SERIAL PRIMARY KEY,
                    date DATE UNIQUE,
                    total_posts INTEGER DEFAULT 0,
                    avg_relevance REAL DEFAULT 0,
                    high_relevance_count INTEGER DEFAULT 0,
                    viral_count INTEGER DEFAULT 0,
                    sentiment_positive INTEGER DEFAULT 0,
                    sentiment_negative INTEGER DEFAULT 0,
                    sentiment_neutral INTEGER DEFAULT 0,
                    created_at TIMESTAMP
                )
            ''')

            # Индексы для analyses
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_analyses_sentiment ON analyses(sentiment)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_analyses_relevance ON analyses(relevance_score)')

            # Таблица telegram_users
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS telegram_users (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    monitored_accounts TEXT,
                    created_at TIMESTAMP,
                    settings TEXT
                )
            ''')

            # Индекс для telegram_users
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_telegram_user_id ON telegram_users(user_id)')

            # Таблица instagram_sessions — критично для персистентности логина
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS instagram_sessions (
                    username TEXT PRIMARY KEY,
                    settings_json TEXT,
                    sessionid TEXT,
                    is_valid BOOLEAN DEFAULT true,
                    last_used TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL
                )
            ''')

            # Таблица parse_logs — для дебага парсинга через Telegram
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS parse_logs (
                    id SERIAL PRIMARY KEY,
                    target_username TEXT,
                    stage TEXT,
                    level TEXT,
                    message TEXT,
                    created_at TIMESTAMP NOT NULL
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_parse_logs_created ON parse_logs(created_at DESC)')

            conn.commit()
            logger.info("Database tables created successfully")

    def verify_database(self) -> bool:
        """Проверить создание всех таблиц"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                required_tables = [
                    'monitored_accounts',
                    'posts',
                    'filter_results',
                    'analyses',
                    'telegram_users'
                ]

                cursor.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public'"
                )
                existing_tables = [row[0] for row in cursor.fetchall()]

                missing = set(required_tables) - set(existing_tables)
                if missing:
                    logger.error(f"Missing tables: {missing}")
                    return False

                logger.info(f"All required tables verified: {required_tables}")
                return True
        except Exception as e:
            logger.error(f"Database verification failed: {e}")
            return False

    def initialize(self) -> bool:
        """Полная инициализация БД"""
        try:
            self.create_tables()
            success = self.verify_database()

            if success:
                logger.info("Database initialization completed successfully")

            return success
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            return False


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    initializer = DatabaseInitializer()
    if initializer.initialize():
        print("✅ База данных инициализирована")
    else:
        print("❌ Ошибка инициализации БД")
