import os
import sqlite3
import hashlib
import pickle
import random
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ChallengeRequired
import requests

logger = logging.getLogger(__name__)

class LoginManager:
    """Управление аутентификацией Instagram с ротацией User-Agent и сохранением сессии"""

    USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    ]

    def __init__(self, session_dir: str = 'data/sessions'):
        self.session_dir = session_dir
        os.makedirs(session_dir, exist_ok=True)

    def _get_session_path(self, username: str) -> str:
        """Получить путь к файлу сессии"""
        return os.path.join(self.session_dir, f'{username}_session.pkl')

    def _load_session(self, username: str) -> Optional[Client]:
        """Загрузить сохраненную сессию"""
        session_path = self._get_session_path(username)
        if os.path.exists(session_path):
            try:
                with open(session_path, 'rb') as f:
                    return pickle.load(f)
            except Exception as e:
                logger.warning(f"Failed to load session for {username}: {e}")
        return None

    def _save_session(self, username: str, client: Client) -> None:
        """Сохранить сессию для повторного использования"""
        session_path = self._get_session_path(username)
        try:
            with open(session_path, 'wb') as f:
                pickle.dump(client, f)
            logger.info(f"Session saved for {username}")
        except Exception as e:
            logger.error(f"Failed to save session for {username}: {e}")

    def login(self, username: str, password: str) -> Client:
        """Авторизоваться в Instagram с сохранением сессии"""
        # Попытка загрузить существующую сессию
        client = self._load_session(username)
        if client:
            try:
                client.get_user_info(client.user_id)
                logger.info(f"Using saved session for {username}")
                return client
            except (LoginRequired, Exception):
                logger.info(f"Saved session invalid for {username}, creating new")

        # Создать новый клиент с ротацией User-Agent
        user_agent = random.choice(self.USER_AGENTS)
        client = Client(user_agent=user_agent)

        # Случайная задержка перед логином
        delay = random.uniform(8, 20)
        time.sleep(delay)

        try:
            client.login(username, password)
            logger.info(f"Successfully logged in as {username}")
            self._save_session(username, client)
            return client
        except ChallengeRequired:
            logger.error(f"Challenge required for {username}")
            raise
        except LoginRequired:
            logger.error(f"Invalid credentials for {username}")
            raise


class PostFetcher:
    """Извлечение постов с Instagram с антидетекцией"""

    def __init__(self, client: Client, min_delay: float = 8, max_delay: float = 20):
        self.client = client
        self.min_delay = min_delay
        self.max_delay = max_delay

    def fetch_posts(self, username: str, num_posts: int = 10) -> List[Dict]:
        """Получить посты пользователя"""
        posts = []
        try:
            user = self.client.user_info_by_username(username)
            user_id = user.pk

            medias = self.client.user_medias(user_id, amount=num_posts)

            for media in medias:
                post_data = {
                    'account': username,
                    'post_id': str(media.pk),
                    'url': f'https://www.instagram.com/p/{media.code}/',
                    'caption': media.caption_text or '',
                    'media_type': media.media_type,
                    'likes': media.like_count or 0,
                    'comments': media.comment_count or 0,
                    'fetched_at': datetime.utcnow().isoformat()
                }
                posts.append(post_data)

                # Случайная задержка между постами
                delay = random.uniform(self.min_delay, self.max_delay)
                time.sleep(delay)

            logger.info(f"Fetched {len(posts)} posts from {username}")
            return posts

        except Exception as e:
            logger.error(f"Error fetching posts from {username}: {e}")
            return []


class MediaDownloader:
    """Загрузка медиа с хешированием и дедубликацией"""

    def __init__(self, download_dir: str = 'data/media'):
        self.download_dir = download_dir
        os.makedirs(download_dir, exist_ok=True)

    def _compute_hash(self, content: bytes) -> str:
        """Вычислить SHA256 хеш контента"""
        return hashlib.sha256(content).hexdigest()

    def download_media(self, client: Client, media_id: str, username: str) -> Optional[str]:
        """Загрузить медиа и вернуть путь"""
        user_media_dir = os.path.join(self.download_dir, username)
        os.makedirs(user_media_dir, exist_ok=True)

        try:
            # Случайная задержка перед загрузкой
            delay = random.uniform(5, 15)
            time.sleep(delay)

            media = client.media_info(int(media_id))

            if media.media_type == 1:  # Image
                path = client.download_photo(media_id, user_media_dir)
                return path
            elif media.media_type == 2:  # Video
                path = client.download_video(media_id, user_media_dir)
                return path

        except Exception as e:
            logger.error(f"Error downloading media {media_id}: {e}")

        return None


class Parser:
    """Основной класс парсера для orchestration"""

    def __init__(self, db_path: str, session_dir: str = 'data/sessions',
                 media_dir: str = 'data/media'):
        self.db_path = db_path
        self.login_manager = LoginManager(session_dir)
        self.media_downloader = MediaDownloader(media_dir)
        self.min_account_delay = 30
        self.max_account_delay = 90

    def _insert_post(self, cursor: sqlite3.Cursor, account_id: int, post_data: Dict) -> bool:
        """Вставить пост в базу данных"""
        try:
            content_hash = hashlib.sha256(post_data['caption'].encode()).hexdigest()

            cursor.execute('''
                INSERT OR IGNORE INTO posts
                (account_id, post_id, url, caption, media_type, content_hash, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                account_id,
                post_data['post_id'],
                post_data['url'],
                post_data['caption'],
                post_data['media_type'],
                content_hash,
                post_data['fetched_at']
            ))
            return True
        except sqlite3.IntegrityError:
            logger.info(f"Post {post_data['post_id']} already exists")
            return False

    def monitor_account(self, username: str, password: str, num_posts: int = 10) -> List[Dict]:
        """Полный цикл мониторинга аккаунта"""
        try:
            # Логин с сохранением сессии
            client = self.login_manager.login(username, password)

            # Задержка между аккаунтами
            delay = random.uniform(self.min_account_delay, self.max_account_delay)
            time.sleep(delay)

            # Получение постов
            fetcher = PostFetcher(client)
            posts = fetcher.fetch_posts(username, num_posts)

            if not posts:
                return []

            # Сохранение в БД
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('PRAGMA foreign_keys = ON')
                cursor = conn.cursor()

                # Получить или создать запись аккаунта
                cursor.execute('SELECT id FROM monitored_accounts WHERE username = ?', (username,))
                result = cursor.fetchone()

                if result:
                    account_id = result[0]
                else:
                    cursor.execute('''
                        INSERT INTO monitored_accounts (username, session_key, last_fetch, created_at)
                        VALUES (?, ?, ?, ?)
                    ''', (username, '', datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
                    account_id = cursor.lastrowid

                # Вставить посты
                inserted = 0
                for post in posts:
                    if self._insert_post(cursor, account_id, post):
                        inserted += 1

                conn.commit()
                logger.info(f"Inserted {inserted} new posts for {username}")

            return posts

        except Exception as e:
            logger.error(f"Error monitoring account {username}: {e}")
            return []
