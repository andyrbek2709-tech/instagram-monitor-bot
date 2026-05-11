import os
import psycopg2
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

MAX_LOGIN_RETRIES = 3
MAX_FETCH_RETRIES = 3
LOGIN_RETRY_DELAY = 5
FETCH_RETRY_DELAY = 3

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
        """Авторизоваться в Instagram с сохранением сессии и retry"""
        logger.info(f"[LOGIN MANAGER] Attempting to login as {username}")
        # Попытка загрузить существующую сессию
        client = self._load_session(username)
        if client:
            logger.info(f"[SESSION CACHE] Found saved session for {username}, validating...")
            try:
                client.get_user_info(client.user_id)
                logger.info(f"[SESSION VALID] Using saved session for {username}")
                return client
            except (LoginRequired, Exception) as e:
                logger.warning(f"[SESSION INVALID] Saved session invalid for {username}: {e}, creating new")

        # Retry логин
        logger.info(f"[LOGIN RETRY] Starting login retry loop for {username} (max {MAX_LOGIN_RETRIES} attempts)")
        for attempt in range(MAX_LOGIN_RETRIES):
            try:
                logger.info(f"[LOGIN ATTEMPT {attempt + 1}/{MAX_LOGIN_RETRIES}] Creating new Instagram client...")
                # Создать новый клиент (instagrapi ротирует User-Agent внутри)
                client = Client()
                logger.info(f"[LOGIN ATTEMPT {attempt + 1}] Client created, random delay before login...")

                # Случайная задержка перед логином
                delay = random.uniform(8, 20)
                logger.info(f"[LOGIN ATTEMPT {attempt + 1}] Sleeping {delay:.1f}s...")
                time.sleep(delay)

                logger.info(f"[LOGIN ATTEMPT {attempt + 1}] Calling client.login({username}, ***)")
                client.login(username, password)
                logger.info(f"[LOGIN SUCCESS] Successfully logged in as {username}")
                self._save_session(username, client)
                return client

            except ChallengeRequired:
                logger.error(f"[LOGIN CHALLENGE] Challenge required for {username} on attempt {attempt + 1}/attempt {MAX_LOGIN_RETRIES}")
                if attempt < MAX_LOGIN_RETRIES - 1:
                    logger.info(f"[LOGIN CHALLENGE] Retrying in {LOGIN_RETRY_DELAY}s...")
                    time.sleep(LOGIN_RETRY_DELAY)
                else:
                    logger.error(f"[LOGIN FAILED] Max attempts reached for challenge")
                    raise
            except LoginRequired:
                logger.error(f"[LOGIN ERROR] Invalid credentials for {username}")
                raise
            except Exception as e:
                logger.error(f"[LOGIN ATTEMPT {attempt + 1}] Failed: {type(e).__name__}: {e}")
                if attempt < MAX_LOGIN_RETRIES - 1:
                    logger.info(f"[LOGIN RETRY] Waiting {LOGIN_RETRY_DELAY}s before next attempt...")
                    time.sleep(LOGIN_RETRY_DELAY)
                else:
                    logger.error(f"[LOGIN FAILED] All {MAX_LOGIN_RETRIES} attempts exhausted")
                    raise


class PostFetcher:
    """Извлечение постов с Instagram с антидетекцией"""

    def __init__(self, client: Client, min_delay: float = 8, max_delay: float = 20):
        self.client = client
        self.min_delay = min_delay
        self.max_delay = max_delay

    def fetch_posts(self, username: str, num_posts: int = 10) -> List[Dict]:
        """Получить посты пользователя с retry"""
        logger.info(f"[FETCH POSTS] Starting fetch for @{username} (target: {num_posts} posts)")
        for attempt in range(MAX_FETCH_RETRIES):
            try:
                logger.info(f"[FETCH ATTEMPT {attempt + 1}/{MAX_FETCH_RETRIES}] Getting user info for @{username}...")
                user = self.client.user_info_by_username(username)
                user_id = user.pk
                logger.info(f"[FETCH ATTEMPT {attempt + 1}] Found user @{username} with ID {user_id}")

                logger.info(f"[FETCH ATTEMPT {attempt + 1}] Fetching {num_posts} medias...")
                medias = self.client.user_medias(user_id, amount=num_posts)
                logger.info(f"[FETCH ATTEMPT {attempt + 1}] Got {len(list(medias))} medias from Instagram API")

                posts = []
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

                logger.info(f"[FETCH SUCCESS] Fetched {len(posts)} posts from @{username}")
                return posts

            except Exception as e:
                logger.error(f"[FETCH ATTEMPT {attempt + 1}] Error fetching posts from @{username}: {type(e).__name__}: {e}")
                if attempt < MAX_FETCH_RETRIES - 1:
                    logger.info(f"[FETCH RETRY] Waiting {FETCH_RETRY_DELAY}s before retry...")
                    time.sleep(FETCH_RETRY_DELAY)
                else:
                    logger.error(f"[FETCH FAILED] All {MAX_FETCH_RETRIES} attempts exhausted for @{username}")
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

    def __init__(self, db_url: str, session_dir: str = 'data/sessions',
                 media_dir: str = 'data/media'):
        self.db_url = db_url
        self.login_manager = LoginManager(session_dir)
        self.media_downloader = MediaDownloader(media_dir)
        self.min_account_delay = 30
        self.max_account_delay = 90

    def _insert_post(self, cursor, account_id: int, post_data: Dict) -> bool:
        """Вставить пост в базу данных"""
        try:
            content_hash = hashlib.sha256(post_data['caption'].encode()).hexdigest()

            cursor.execute('''
                INSERT INTO posts
                (account_id, post_id, url, caption, media_type, content_hash, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
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
        except psycopg2.IntegrityError:
            logger.info(f"Post {post_data['post_id']} already exists")
            return False

    def monitor_account(self, target_username: str, login_username: str, login_password: str, num_posts: int = 10) -> List[Dict]:
        """Полный цикл мониторинга аккаунта"""
        try:
            logger.info(f"[PARSE START] target={target_username}, login_as={login_username}, num_posts={num_posts}")

            # Логин с основным аккаунтом с сохранением сессии
            logger.info(f"[LOGIN] Attempting login as {login_username}...")
            client = self.login_manager.login(login_username, login_password)
            logger.info(f"[LOGIN OK] Successfully logged in as {login_username}")

            # Задержка между аккаунтами
            delay = random.uniform(self.min_account_delay, self.max_account_delay)
            logger.info(f"[DELAY] Waiting {delay:.1f} seconds before fetching posts...")
            time.sleep(delay)

            # Получение постов из целевого аккаунта
            logger.info(f"[FETCH] Getting {num_posts} posts from @{target_username}...")
            fetcher = PostFetcher(client)
            posts = fetcher.fetch_posts(target_username, num_posts)
            logger.info(f"[FETCH OK] Got {len(posts)} posts from @{target_username}")

            if not posts:
                logger.warning(f"[NO POSTS] No posts fetched from @{target_username}")
                return []

            # Сохранение в БД
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()

            try:
                # Получить или создать запись аккаунта
                cursor.execute('SELECT id FROM monitored_accounts WHERE username = %s', (target_username,))
                result = cursor.fetchone()

                if result:
                    account_id = result[0]
                else:
                    cursor.execute('''
                        INSERT INTO monitored_accounts (username, session_key, last_fetch, created_at)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id
                    ''', (target_username, '', datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
                    account_id = cursor.fetchone()[0]

                # Вставить посты
                inserted = 0
                for post in posts:
                    if self._insert_post(cursor, account_id, post):
                        inserted += 1

                conn.commit()
                logger.info(f"Inserted {inserted} new posts for {target_username}")
            finally:
                cursor.close()
                conn.close()

            return posts

        except Exception as e:
            logger.error(f"Error monitoring account {target_username}: {e}")
            return []
