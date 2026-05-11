import os
import json
import psycopg2
import hashlib
import random
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ChallengeRequired, BadPassword, PleaseWaitFewMinutes

logger = logging.getLogger(__name__)

MAX_LOGIN_RETRIES = 2
MAX_FETCH_RETRIES = 2
LOGIN_RETRY_DELAY = 5
FETCH_RETRY_DELAY = 3


def log_to_db(db_url: str, target: str, stage: str, level: str, message: str) -> None:
    """Записать лог в БД для просмотра через Telegram"""
    try:
        conn = psycopg2.connect(db_url)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO parse_logs (target_username, stage, level, message, created_at) VALUES (%s, %s, %s, %s, %s)',
            (target, stage, level, message[:2000], datetime.utcnow().isoformat())
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to log to DB: {e}")


class LoginManager:
    """Хранение сессии Instagram в PostgreSQL (выдерживает перезапуски Railway)"""

    def __init__(self, db_url: str):
        self.db_url = db_url

    def _load_session_from_db(self, username: str) -> Tuple[Optional[str], Optional[str]]:
        """Загрузить (settings_json, sessionid) из БД"""
        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            cursor.execute(
                'SELECT settings_json, sessionid FROM instagram_sessions WHERE username = %s AND is_valid = true',
                (username,)
            )
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if row:
                return row[0], row[1]
            return None, None
        except Exception as e:
            logger.error(f"[DB SESSION LOAD] Error: {e}")
            return None, None

    def _save_session_to_db(self, username: str, client: Client) -> None:
        """Сохранить settings из instagrapi в БД через dump_settings()"""
        try:
            settings = client.get_settings()
            settings_json = json.dumps(settings)
            now = datetime.utcnow().isoformat()

            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO instagram_sessions (username, settings_json, is_valid, last_used, updated_at)
                VALUES (%s, %s, true, %s, %s)
                ON CONFLICT (username) DO UPDATE SET
                    settings_json = EXCLUDED.settings_json,
                    is_valid = true,
                    last_used = EXCLUDED.last_used,
                    updated_at = EXCLUDED.updated_at
            ''', (username, settings_json, now, now))
            conn.commit()
            cursor.close()
            conn.close()
            logger.info(f"[DB SESSION SAVE] Session settings saved for {username}")
        except Exception as e:
            logger.error(f"[DB SESSION SAVE] Failed: {e}")

    def _mark_session_invalid(self, username: str) -> None:
        """Пометить сессию как невалидную"""
        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE instagram_sessions SET is_valid = false WHERE username = %s',
                (username,)
            )
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            logger.error(f"[DB SESSION INVALID] Failed: {e}")

    def save_sessionid(self, username: str, sessionid: str) -> bool:
        """Сохранить sessionid из браузера (ручной вход)"""
        try:
            now = datetime.utcnow().isoformat()
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO instagram_sessions (username, sessionid, is_valid, updated_at)
                VALUES (%s, %s, true, %s)
                ON CONFLICT (username) DO UPDATE SET
                    sessionid = EXCLUDED.sessionid,
                    is_valid = true,
                    updated_at = EXCLUDED.updated_at
            ''', (username, sessionid, now))
            conn.commit()
            cursor.close()
            conn.close()
            logger.info(f"[SESSIONID SAVE] Saved sessionid for {username}")
            return True
        except Exception as e:
            logger.error(f"[SESSIONID SAVE] Failed: {e}")
            return False

    def login(self, username: str, password: str) -> Client:
        """Залогиниться: 1) settings из БД 2) sessionid 3) login/password"""
        log_to_db(self.db_url, username, 'LOGIN', 'INFO', f'Starting login flow for {username}')

        settings_json, sessionid = self._load_session_from_db(username)

        # Попытка 1: восстановить полную сессию через settings_json
        if settings_json:
            logger.info(f"[LOGIN] Found saved settings_json for {username}, trying to restore...")
            log_to_db(self.db_url, username, 'LOGIN', 'INFO', 'Restoring session from DB settings_json')
            try:
                client = Client()
                settings = json.loads(settings_json)
                client.set_settings(settings)
                # Лёгкая проверка сессии — account_info запрашивает /accounts/current_user/
                client.account_info()
                logger.info(f"[LOGIN] Session restored from settings_json for {username}")
                log_to_db(self.db_url, username, 'LOGIN', 'SUCCESS', 'Session restored from DB')
                return client
            except Exception as e:
                logger.warning(f"[LOGIN] settings_json invalid for {username}: {type(e).__name__}: {e}")
                log_to_db(self.db_url, username, 'LOGIN', 'WARN', f'settings_json invalid: {type(e).__name__}: {e}')

        # Попытка 2: sessionid (от пользователя из браузера)
        if sessionid:
            logger.info(f"[LOGIN] Found sessionid for {username}, trying login_by_sessionid...")
            log_to_db(self.db_url, username, 'LOGIN', 'INFO', 'Trying login_by_sessionid')
            try:
                client = Client()
                client.login_by_sessionid(sessionid)
                # Сохранить полные settings после успешного login_by_sessionid
                self._save_session_to_db(username, client)
                logger.info(f"[LOGIN] login_by_sessionid succeeded for {username}")
                log_to_db(self.db_url, username, 'LOGIN', 'SUCCESS', 'login_by_sessionid succeeded')
                return client
            except Exception as e:
                logger.warning(f"[LOGIN] sessionid invalid for {username}: {type(e).__name__}: {e}")
                log_to_db(self.db_url, username, 'LOGIN', 'WARN', f'sessionid invalid: {type(e).__name__}: {e}')
                self._mark_session_invalid(username)

        # Попытка 3: логин по username/password (нестабильно на хостинге!)
        logger.info(f"[LOGIN] Falling back to username/password login for {username}")
        log_to_db(self.db_url, username, 'LOGIN', 'INFO', 'Trying username/password login (may fail on hosting)')

        for attempt in range(MAX_LOGIN_RETRIES):
            try:
                logger.info(f"[LOGIN ATTEMPT {attempt + 1}/{MAX_LOGIN_RETRIES}] Creating client...")
                client = Client()
                delay = random.uniform(3, 8)
                logger.info(f"[LOGIN ATTEMPT {attempt + 1}] Sleeping {delay:.1f}s before login...")
                time.sleep(delay)

                logger.info(f"[LOGIN ATTEMPT {attempt + 1}] Calling client.login({username}, ***)")
                client.login(username, password)
                logger.info(f"[LOGIN SUCCESS] Logged in as {username}")
                self._save_session_to_db(username, client)
                log_to_db(self.db_url, username, 'LOGIN', 'SUCCESS', 'Logged in via username/password')
                return client

            except BadPassword as e:
                msg = f"Bad password for {username}: {e}"
                logger.error(f"[LOGIN] {msg}")
                log_to_db(self.db_url, username, 'LOGIN', 'ERROR', msg)
                raise Exception(f"Неверный пароль Instagram для {username}. Проверьте INSTAGRAM_PASSWORD в Railway.") from e

            except ChallengeRequired as e:
                msg = f"Instagram challenge required for {username} (нужен код с email/SMS)"
                logger.error(f"[LOGIN] {msg}: {e}")
                log_to_db(self.db_url, username, 'LOGIN', 'ERROR', msg)
                raise Exception(
                    f"Instagram требует подтверждение для {username}. "
                    f"Решение: войдите в Instagram через браузер, скопируйте sessionid из cookies "
                    f"и установите через бот."
                ) from e

            except PleaseWaitFewMinutes as e:
                msg = f"Instagram rate limit for {username}: {e}"
                logger.error(f"[LOGIN] {msg}")
                log_to_db(self.db_url, username, 'LOGIN', 'ERROR', msg)
                if attempt < MAX_LOGIN_RETRIES - 1:
                    time.sleep(LOGIN_RETRY_DELAY * 3)
                else:
                    raise Exception(f"Instagram rate limit. Попробуйте через 10-30 минут.") from e

            except Exception as e:
                msg = f"Login attempt {attempt + 1} failed: {type(e).__name__}: {e}"
                logger.error(f"[LOGIN] {msg}")
                log_to_db(self.db_url, username, 'LOGIN', 'ERROR', msg)
                if attempt < MAX_LOGIN_RETRIES - 1:
                    time.sleep(LOGIN_RETRY_DELAY)
                else:
                    raise Exception(f"Не удалось залогиниться в Instagram: {type(e).__name__}: {e}") from e

        raise Exception("Login failed after all retries")


class PostFetcher:
    """Извлечение постов с Instagram"""

    def __init__(self, client: Client, db_url: str, min_delay: float = 2, max_delay: float = 5):
        self.client = client
        self.db_url = db_url
        self.min_delay = min_delay
        self.max_delay = max_delay

    def fetch_posts(self, username: str, num_posts: int = 10) -> List[Dict]:
        """Получить посты пользователя с retry"""
        logger.info(f"[FETCH POSTS] Starting fetch for @{username} (target: {num_posts} posts)")
        log_to_db(self.db_url, username, 'FETCH', 'INFO', f'Starting fetch (target: {num_posts} posts)')

        for attempt in range(MAX_FETCH_RETRIES):
            try:
                logger.info(f"[FETCH ATTEMPT {attempt + 1}] Getting user info for @{username}...")
                user = self.client.user_info_by_username(username)
                user_id = user.pk
                logger.info(f"[FETCH ATTEMPT {attempt + 1}] Found @{username} pk={user_id} private={user.is_private}")
                log_to_db(self.db_url, username, 'FETCH', 'INFO', f'Found user pk={user_id} private={user.is_private}')

                if user.is_private:
                    msg = f"@{username} is private — cannot fetch posts without following"
                    logger.warning(f"[FETCH] {msg}")
                    log_to_db(self.db_url, username, 'FETCH', 'WARN', msg)
                    return []

                logger.info(f"[FETCH ATTEMPT {attempt + 1}] Calling user_medias_v1(amount={num_posts})...")
                # v1 более стабилен чем дефолтный (который пробует gql)
                try:
                    medias = self.client.user_medias_v1(user_id, amount=num_posts)
                except Exception as v1_err:
                    logger.warning(f"[FETCH] user_medias_v1 failed: {v1_err}, falling back to user_medias")
                    medias = self.client.user_medias(user_id, amount=num_posts)

                medias_count = len(medias) if medias else 0
                logger.info(f"[FETCH ATTEMPT {attempt + 1}] Got {medias_count} medias from API")
                log_to_db(self.db_url, username, 'FETCH', 'INFO', f'Got {medias_count} medias from API')

                if medias_count == 0:
                    log_to_db(self.db_url, username, 'FETCH', 'WARN', 'Instagram returned 0 medias')
                    return []

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

                logger.info(f"[FETCH SUCCESS] Fetched {len(posts)} posts from @{username}")
                log_to_db(self.db_url, username, 'FETCH', 'SUCCESS', f'Fetched {len(posts)} posts')
                return posts

            except Exception as e:
                msg = f"Fetch attempt {attempt + 1} failed: {type(e).__name__}: {e}"
                logger.error(f"[FETCH] {msg}")
                log_to_db(self.db_url, username, 'FETCH', 'ERROR', msg)
                if attempt < MAX_FETCH_RETRIES - 1:
                    time.sleep(FETCH_RETRY_DELAY)
                else:
                    raise Exception(f"Не удалось получить посты @{username}: {type(e).__name__}: {e}") from e

        return []


class Parser:
    """Основной класс парсера для orchestration"""

    def __init__(self, db_url: str):
        self.db_url = db_url
        self.login_manager = LoginManager(db_url)

    def _insert_post(self, cursor, account_id: int, post_data: Dict) -> bool:
        """Вставить пост в базу данных"""
        try:
            content_hash = hashlib.sha256(
                f"{account_id}_{post_data['post_id']}_{post_data['caption']}".encode()
            ).hexdigest()

            cursor.execute('''
                INSERT INTO posts
                (account_id, post_id, url, caption, media_type, content_hash, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (account_id, post_id) DO NOTHING
            ''', (
                account_id,
                post_data['post_id'],
                post_data['url'],
                post_data['caption'],
                post_data['media_type'],
                content_hash,
                post_data['fetched_at']
            ))
            return cursor.rowcount > 0
        except psycopg2.IntegrityError as e:
            logger.info(f"Post {post_data['post_id']} already exists: {e}")
            return False

    def monitor_account(self, target_username: str, login_username: str, login_password: str, num_posts: int = 10) -> List[Dict]:
        """Полный цикл мониторинга аккаунта. Бросает исключения наверх."""
        logger.info(f"[PARSE START] target={target_username}, login_as={login_username}, num_posts={num_posts}")
        log_to_db(self.db_url, target_username, 'START', 'INFO',
                  f'Pipeline start: login_as={login_username}, num_posts={num_posts}')

        # Логин — бросает исключение если не удалось
        client = self.login_manager.login(login_username, login_password)
        logger.info(f"[LOGIN OK] Successfully logged in as {login_username}")

        # Получение постов
        fetcher = PostFetcher(client, self.db_url)
        posts = fetcher.fetch_posts(target_username, num_posts)
        logger.info(f"[FETCH OK] Got {len(posts)} posts from @{target_username}")

        if not posts:
            logger.warning(f"[NO POSTS] No posts fetched from @{target_username}")
            log_to_db(self.db_url, target_username, 'PARSE', 'WARN', 'No posts returned from Instagram')
            return []

        # Сохранение в БД
        conn = psycopg2.connect(self.db_url)
        cursor = conn.cursor()
        try:
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

            inserted = 0
            for post in posts:
                if self._insert_post(cursor, account_id, post):
                    inserted += 1

            conn.commit()
            logger.info(f"[DB SAVE] Inserted {inserted} new posts for {target_username} (out of {len(posts)} fetched)")
            log_to_db(self.db_url, target_username, 'SAVE', 'SUCCESS',
                      f'Inserted {inserted} new posts (fetched {len(posts)})')
        finally:
            cursor.close()
            conn.close()

        return posts
