import os
import re
import json
import requests
import psycopg2
import hashlib
import logging
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse, parse_qs

logger = logging.getLogger(__name__)

HIKER_BASE_URL = "https://api.hikerapi.com/v1"


def _unwrap_hiker_media_payload(raw) -> Dict:
    """Достать объект медиа из ответа HikerAPI (часто обёрнут в response/items)."""
    if raw is None:
        return {}
    if isinstance(raw, list):
        return _unwrap_hiker_media_payload(raw[0]) if raw else {}
    if not isinstance(raw, dict):
        return {}
    data = raw
    for _ in range(5):
        inner = data.get("response")
        if isinstance(inner, dict):
            data = inner
            continue
        items = data.get("items")
        if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict):
            data = items[0]
            continue
        break
    return data if isinstance(data, dict) else {}


def _carousel_items_from_media(media: Dict) -> List[Dict]:
    """Список слайдов карусели в формате Instagram (mobile API)."""
    cm = media.get("carousel_media")
    if isinstance(cm, list) and cm:
        return cm
    esc = media.get("edge_sidecar_to_children") or {}
    edges = esc.get("edges") or []
    out = []
    for e in edges:
        if isinstance(e, dict):
            n = e.get("node")
            if isinstance(n, dict):
                out.append(n)
    return out


def _slide_image_and_video_urls(slide: Dict) -> tuple:
    """URL превью картинки и видео для одного слайда карусели."""
    image_url = None
    iv2 = slide.get("image_versions2") or {}
    cands = iv2.get("candidates") or []
    if cands and isinstance(cands[0], dict):
        image_url = cands[0].get("url")
    if not image_url:
        image_url = (
            slide.get("display_url")
            or slide.get("thumbnail_url")
            or slide.get("cover_frame_url")
        )
    video_url = None
    vv = slide.get("video_versions") or []
    if vv and isinstance(vv[0], dict):
        video_url = vv[0].get("url")
    if not video_url:
        video_url = slide.get("video_url")
    return image_url, video_url


def _parse_img_index_from_url(url: str) -> Optional[int]:
    """img_index из query (Instagram web): обычно 0 — первый слайд, 1 — второй и т.д."""
    try:
        q = parse_qs(urlparse(url).query)
        if "img_index" not in q or not q["img_index"]:
            return None
        v = int(q["img_index"][0])
        return max(0, v)
    except (ValueError, TypeError):
        return None


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

    def get_media_by_code(self, code: str) -> Dict:
        """Получить пост по shortcode (из URL instagram.com/p/<code>/)"""
        r = self.session.get(
            f"{HIKER_BASE_URL}/media/by/code",
            params={"code": code},
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

    def get_tag_medias(self, tag_name: str, amount: int = 20) -> List[Dict]:
        """Получить посты по хэштегу (глобальный поиск)"""
        r = self.session.get(
            f"{HIKER_BASE_URL}/tag/medias",
            params={"tag_name": tag_name, "amount": amount},
            timeout=30
        )
        r.raise_for_status()
        data = r.json()

        # HikerAPI может возвращать в разных форматах
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = data.get("response", {})
            if isinstance(items, list):
                return items
            if isinstance(items, dict):
                return items.get("items", [])
            return data.get("items", [])
        return []

    def get_tag_info(self, tag_name: str) -> Dict:
        """Получить информацию о хэштеге (кол-во постов и т.д.)"""
        r = self.session.get(
            f"{HIKER_BASE_URL}/tag/by/name",
            params={"tag_name": tag_name},
            timeout=30
        )
        r.raise_for_status()
        return r.json()


class Parser:
    """Основной парсер — использует HikerAPI вместо прямого логина в Instagram"""

    def __init__(self, db_url: str):
        self.db_url = db_url
        api_key = os.getenv("HIKER_API_KEY")
        if not api_key:
            raise ValueError("HIKER_API_KEY environment variable is required. Add it to Railway.")
        self.hiker = HikerAPIClient(api_key)

    def get_post_by_url(self, url: str) -> Optional[Dict]:
        """Получить один пост по прямой ссылке Instagram (включая карусель и img_index)."""
        match = re.search(r'instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)', url)
        if not match:
            return None
        code = match.group(1)
        focus_slide = _parse_img_index_from_url(url)

        raw = self.hiker.get_media_by_code(code)
        media = _unwrap_hiker_media_payload(raw)
        if not media:
            return None

        carousel_items = _carousel_items_from_media(media)
        carousel_slides = []
        carousel_images = []
        for i, slide in enumerate(carousel_items):
            img_u, vid_u = _slide_image_and_video_urls(slide)
            acc = slide.get('accessibility_caption') or ''
            if isinstance(acc, dict):
                acc = self._parse_caption(acc) or ''
            elif not isinstance(acc, str):
                acc = str(acc) if acc else ''
            mt = slide.get('media_type', 1)
            try:
                mt = int(mt)
            except (TypeError, ValueError):
                mt = 1
            carousel_slides.append({
                'index': i,
                'media_type': mt,
                'image_url': img_u,
                'video_url': vid_u,
                'accessibility_caption': acc.strip(),
            })
            if img_u:
                carousel_images.append(img_u)

        logger.info(
            f"HikerAPI [{code}]: media_type={media.get('media_type')}, "
            f"carousel_slides={len(carousel_slides)}, "
            f"has_image_versions2={bool(media.get('image_versions2'))}, "
            f"has_video_versions={bool(media.get('video_versions'))}, "
            f"has_thumbnail_url={bool(media.get('thumbnail_url'))}, "
            f"keys={list(media.keys())[:24]}"
        )

        caption_raw = media.get('caption') or ''
        caption = self._parse_caption(caption_raw)
        user = media.get('user') or {}

        # Превью/обложка верхнего уровня
        thumbnail_url = None
        image_versions = media.get('image_versions2') or {}
        candidates = image_versions.get('candidates') or []
        if candidates and isinstance(candidates[0], dict):
            thumbnail_url = candidates[0].get('url')
        if not thumbnail_url:
            thumbnail_url = (
                media.get('thumbnail_url')
                or media.get('display_url')
                or media.get('cover_frame_url')
            )

        # Видео верхнего уровня (не карусель или первое видео в ленте)
        video_url = None
        video_versions = media.get('video_versions') or []
        if video_versions and isinstance(video_versions[0], dict):
            video_url = video_versions[0].get('url')
        if not video_url:
            video_url = media.get('video_url')

        # Карусель: превью и видео — слайд из img_index в ссылке, иначе первый слайд
        if carousel_slides:
            idx = focus_slide if focus_slide is not None else 0
            if idx >= len(carousel_slides):
                idx = 0
            focused = carousel_slides[idx]
            if focused.get('image_url'):
                thumbnail_url = focused['image_url']
            if focused.get('video_url'):
                video_url = focused['video_url']
            if not video_url:
                for s in carousel_slides:
                    if s.get('video_url'):
                        video_url = s['video_url']
                        break
            if not thumbnail_url and carousel_images:
                thumbnail_url = carousel_images[0]

        canonical_url = f"https://www.instagram.com/p/{code}/"
        if focus_slide is not None:
            canonical_url = f"{canonical_url}?img_index={focus_slide}"

        return {
            'post_id': str(media.get('pk') or media.get('id') or ''),
            'url': canonical_url,
            'source_url': url.strip(),
            'caption': caption,
            'media_type': media.get('media_type', 1),
            'likes': media.get('like_count', 0) or 0,
            'comments': media.get('comment_count', 0) or 0,
            'code': code,
            'account': user.get('username', ''),
            'thumbnail_url': thumbnail_url,
            'video_url': video_url,
            'carousel_images': carousel_images,
            'carousel_slides': carousel_slides,
            'focus_slide_index': focus_slide if focus_slide is not None else 0,
            'is_carousel': len(carousel_slides) > 1 or media.get('media_type') == 8,
        }

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

                # Сбор статистики по хэштегам
                hashtag_stats_count = 0
                for p in posts:
                    caption = p.get('caption', '') or ''
                    hashtags = re.findall(r'#(\w[\wа-яёА-ЯЁ]*)', caption)
                    if not hashtags:
                        continue
                    # Получить post_id из БД
                    cursor.execute('SELECT id FROM posts WHERE account_id = %s AND post_id = %s',
                                   (account_id, p['post_id']))
                    post_row = cursor.fetchone()
                    if not post_row:
                        continue
                    db_post_id = post_row[0]
                    now = datetime.utcnow().isoformat()
                    for tag in hashtags:
                        tag_lower = tag.lower()
                        try:
                            cursor.execute('''
                                INSERT INTO hashtag_stats
                                (hashtag, post_id, account_id, likes, comments, fetched_at)
                                VALUES (%s, %s, %s, %s, %s, %s)
                            ''', (tag_lower, db_post_id, account_id,
                                  p.get('likes', 0), p.get('comments', 0), now))
                            hashtag_stats_count += 1
                        except Exception:
                            pass  # дубликаты игнорируем (нет UNIQUE, но OK)

                conn.commit()
                logger.info(f"[HIKER] Inserted {inserted} new posts for @{target_username}, "
                           f"saved {hashtag_stats_count} hashtag stats")
                log_to_db(self.db_url, target_username, 'SAVE', 'SUCCESS',
                          f'Inserted {inserted} new posts, {hashtag_stats_count} hashtag stats (fetched {len(posts)})')
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

    def search_by_hashtag(self, hashtag: str, amount: int = 20) -> List[Dict]:
        """Глобальный поиск постов по хэштегу через HikerAPI"""
        log_to_db(self.db_url, f"#{hashtag}", 'HASHTAG_SEARCH', 'INFO',
                  f'Global hashtag search: #{hashtag}, limit={amount}')

        try:
            tag_name = hashtag.lstrip('#').strip()
            medias = self.hiker.get_tag_medias(tag_name, amount)

            if not medias:
                log_to_db(self.db_url, f"#{hashtag}", 'HASHTAG_SEARCH', 'WARN',
                          'HikerAPI returned 0 medias for this hashtag')
                return []

            posts = []
            for media in medias:
                caption_raw = media.get('caption') or ''
                caption = self._parse_caption(caption_raw)

                # Вычисляем "виральность" (лайки + комменты * 2)
                likes = media.get('like_count', 0) or 0
                comments = media.get('comment_count', 0) or 0
                engagement_score = likes + comments * 3

                code = media.get('code') or media.get('shortcode') or ''
                user = media.get('user') or {}

                post_data = {
                    'account': user.get('username', ''),
                    'post_id': str(media.get('pk') or media.get('id') or ''),
                    'url': f'https://www.instagram.com/p/{code}/' if code else '',
                    'caption': caption,
                    'media_type': media.get('media_type', 1),
                    'likes': likes,
                    'comments': comments,
                    'engagement_score': engagement_score,
                    'fetched_at': datetime.utcnow().isoformat(),
                }
                posts.append(post_data)

            # Сортируем по engagement для показа трендовых
            posts.sort(key=lambda p: p['engagement_score'], reverse=True)

            log_to_db(self.db_url, f"#{hashtag}", 'HASHTAG_SEARCH', 'SUCCESS',
                      f'Found {len(posts)} posts for #{hashtag}')
            return posts

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            logger.error(f"[HIKER] Hashtag search error: {msg}")
            log_to_db(self.db_url, f"#{hashtag}", 'HASHTAG_SEARCH', 'ERROR', msg)
            return []
