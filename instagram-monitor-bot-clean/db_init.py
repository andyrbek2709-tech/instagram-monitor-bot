import os
import sqlite3
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class DatabaseInitializer:
    """Инициализация SQLite базы данных"""

    def __init__(self, db_path: str = 'database/instagram_monitor.db'):
        self.db_path = db_path
        self.db_dir = os.path.dirname(db_path) or 'database'

    def ensure_database_dir(self) -> None:
        """Создать директорию для БД если её нет"""
        os.makedirs(self.db_dir, exist_ok=True)
        logger.info(f"Database directory ensured: {self.db_dir}")

    def get_connection(self) -> sqlite3.Connection:
        """Получить подключение к БД с включенными foreign keys"""
        conn = sqlite3.connect(self.db_path)
        conn.execute('PRAGMA foreign_keys = ON')
        return conn

    def create_tables(self) -> None:
        """Создать все необходимые таблицы"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Таблица monitored_accounts
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitored_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    session_key TEXT,
                    last_fetch TIMESTAMP,
                    created_at TIMESTAMP NOT NULL
                )
            ''')

            # Таблица posts
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    post_id TEXT NOT NULL,
                    url TEXT,
                    caption TEXT,
                    media_type INTEGER,
                    media_path TEXT,
                    content_hash TEXT UNIQUE,
                    fetched_at TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES monitored_accounts(id),
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
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id INTEGER NOT NULL UNIQUE,
                    is_ad INTEGER DEFAULT 0,
                    is_greeting INTEGER DEFAULT 0,
                    is_personal INTEGER DEFAULT 0,
                    engagement_rate REAL,
                    text_length INTEGER,
                    has_media INTEGER DEFAULT 0,
                    analyzed_at TIMESTAMP,
                    FOREIGN KEY (post_id) REFERENCES posts(id)
                )
            ''')

            # Индексы для filter_results
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_filter_is_ad ON filter_results(is_ad)')

            # Таблица analyses
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id INTEGER NOT NULL UNIQUE,
                    sentiment TEXT,
                    key_topics TEXT,
                    brand_mentions TEXT,
                    audience_segment TEXT,
                    content_quality TEXT,
                    relevance_score REAL,
                    viral_potential TEXT,
                    recommendations TEXT,
                    analyzed_at TIMESTAMP,
                    FOREIGN KEY (post_id) REFERENCES posts(id)
                )
            ''')

            # Индексы для analyses
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_analyses_sentiment ON analyses(sentiment)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_analyses_relevance ON analyses(relevance_score)')

            # Таблица telegram_users
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS telegram_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    monitored_accounts TEXT,
                    created_at TIMESTAMP,
                    settings TEXT
                )
            ''')

            # Индекс для telegram_users
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_telegram_user_id ON telegram_users(user_id)')

            conn.commit()
            logger.info("Database tables created successfully")

    def verify_database(self) -> bool:
        """Проверить создание всех таблиц"""
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
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            existing_tables = [row[0] for row in cursor.fetchall()]

            missing = set(required_tables) - set(existing_tables)
            if missing:
                logger.error(f"Missing tables: {missing}")
                return False

            logger.info(f"All required tables verified: {required_tables}")
            return True

    def initialize(self) -> bool:
        """Полная инициализация БД"""
        try:
            self.ensure_database_dir()
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
