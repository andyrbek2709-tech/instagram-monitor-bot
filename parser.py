import os
import json
import requests
import psycopg2
import hashlib
import logging
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import unquote

logger = logging.getLogger(__name__)

HIKER_BASE_URL = "https://api.hikerapi.com/v1"


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


class HikerAPIClient:
    """HTTP клиент для HikerAPI — никакого логина в Instagram"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "accept": "application/json",
            "x-access-key": api_key
        })

    def get_user_by_username(self, username: str) -> Dict:
        """Получить информацию о пользователе по username"""
        r = self.session.get(
            f"{HIKER_BASE_URL}/user/by/username",
            params={"username": username},
            timeout=30
        )
        r.raise_for_status()
        return r.json()

    def get_user_medias(self, user_id: str, amount: int = 10) -> List[Dict]:
        """Получить посты пользователя по user_id"""
        r = self.session.get(
            f"{HIKER_BASE_URL}/user/medias",
            params={"user_id": user_id, "amount": amount},
            timeout=30
        )
        r.raise_for_status()
        data = r.json()

        # HikerAPI может возвращать данные в разных форматах
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # response.items или items или response
            items = data.get("response", {})
            if isinstance(items, list):
                return items
            if isinstance(items, dict):
                return items.get("items", [])
            return data.get("items", [])
        return []


class Parser:
    """Основной парсер — использует HikerAPI вместо прямого логина в Instagram"""

    def __init__(self, db_url: str):
        self.db_url = db_url
        api_key = os.getenv("HIKER_API_KEY")
        if not api_key:
            raise ValueError("HIKER_API_KEY environment variable is required. Add it to Railway.")
        self.hiker = HikerAPIClient(api_key)

    def _parse_caption(self, caption_data) -> str:
        """Извлечь текст caption из разных форматов ответа"""
        if caption_data is None:
            return ''
        if isinstance(caption_data, str):
            return caption_data
        if isinstance(caption_data, dict):
            return caption_data.get('text', '')
        return ''

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
        except Exception as e:
            logger.error(f"Error inserting post {post_data.get('post_id')}: {e}")
            return False

    def monitor_account(self, target_username: str, login_username: str = None,
                        login_password: str = None, num_posts: int = 10) -> List[Dict]:
        """Парсинг аккаунта через HikerAPI. login_username/password больше не нужны."""
        log_to_db(self.db_url, target_username, 'START', 'INFO',
                  f'HikerAPI pipeline start: target={target_username}, num_posts={num_posts}')

        try:
            # Шаг 1: получить user_id по username
            logger.info(f"[HIKER] Getting user info for @{target_username}...")
            log_to_db(self.db_url, target_username, 'FETCH', 'INFO',
                      f'Getting user info for @{target_username}')

            user_data = self.hiker.get_user_by_username(target_username)
            user_id = str(user_data.get('pk') or user_data.get('id') or '')

            if not user_id:
                msg = f"User @{target_username} not found in HikerAPI response: {json.dumps(user_data)[:200]}"
                logger.error(f"[HIKER] {msg}")
                log_to_db(self.db_url, target_username, 'FETCH', 'ERROR', msg)
                return []

            is_private = user_data.get('is_private', False)
            logger.info(f"[HIKER] Found @{target_username} pk={user_id} private={is_private}")
            log_to_db(self.db_url, target_username, 'FETCH', 'INFO',
                      f'Found user pk={user_id} private={is_private}')

            if is_private:
                msg = f"@{target_username} is private — cannot fetch posts"
                log_to_db(self.db_url, target_username, 'FETCH', 'WARN', msg)
                return []

            # Шаг 2: получить медиа
            logger.info(f"[HIKER] Fetching {num_posts} medias for @{target_username}...")
            medias = self.hiker.get_user_medias(user_id, amount=num_posts)
            logger.info(f"[HIKER] Got {len(medias)} medias")
            log_to_db(self.db_url, target_username, 'FETCH', 'INFO',
                      f'Got {len(medias)} medias from HikerAPI')

            if not medias:
                log_to_db(self.db_url, target_username, 'FETCH', 'WARN',
                          'HikerAPI returned 0 medias')
                return []

            # Шаг 3: сформировать список постов
            posts = []
            for media in medias:
                caption_raw = media.get('caption') or ''
                caption = self._parse_caption(caption_raw)
                code = media.get('code') or media.get('shortcode') or ''
                post_id = str(media.get('pk') or media.get('id') or '')

                post_data = {
                    'account': target_username,
                    'post_id': post_id,
                    'url': f'https://www.instagram.com/p/{code}/' if code else '',
                    'caption': caption,
                    'media_type': media.get('media_type', 1),
                    'likes': media.get('like_count', 0) or 0,
                    'comments': media.get('comment_count', 0) or 0,
                    'fetched_at': datetime.utcnow().isoformat()
                }
                posts.append(post_data)

            # Шаг 4: сохранить в БД
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    'SELECT id FROM monitored_accounts WHERE username = %s',
                    (target_username,)
                )
                result = cursor.fetchone()

                if result:
                    account_id = result[0]
                else:
                    cursor.execute('''
                        INSERT INTO monitored_accounts (username, session_key, last_fetch, created_at)
                        VALUES (%s, %s, %s, %s) RETURNING id
                    ''', (target_username, '', datetime.utcnow().isoformat(),
                          datetime.utcnow().isoformat()))
                    account_id = cursor.fetchone()[0]

                inserted = sum(1 for p in posts if self._insert_post(cursor, account_id, p))
                conn.commit()
                logger.info(f"[HIKER] Inserted {inserted} new posts for @{target_username}")
                log_to_db(self.db_url, target_username, 'SAVE', 'SUCCESS',
                          f'Inserted {inserted} new posts (fetched {len(posts)})')
            finally:
                cursor.close()
                conn.close()

            return posts

        except requests.HTTPError as e:
            status = e.response.status_code if e.response else '?'
            body = e.response.text[:300] if e.response else ''
            msg = f"HikerAPI HTTP {status}: {body}"
            logger.error(f"[HIKER] {msg}")
            log_to_db(self.db_url, target_username, 'FETCH', 'ERROR', msg)
            raise Exception(f"HikerAPI ошибка {status}: {body}") from e

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            logger.error(f"[HIKER] Error: {msg}")
            log_to_db(self.db_url, target_username, 'FETCH', 'ERROR', msg)
            raise Exception(f"Ошибка парсинга @{target_username}: {msg}") from e
