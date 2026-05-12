import os
import re
import json
import logging
import subprocess
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class MediaParser:
    """Универсальный парсер для YouTube, TikTok и других платформ через yt-dlp

    yt-dlp поддерживает:
    - YouTube (поиск, тренды, детали)
    - TikTok (поиск, видео по ссылке)
    - Instagram (Reels, посты — через HikerAPI, но yt-dlp тоже умеет)
    - Twitter/X, Vimeo, Twitch и сотни других
    """

    def __init__(self):
        self._check_installed()

    @staticmethod
    def _check_installed():
        """Проверить, установлен ли yt-dlp"""
        try:
            subprocess.run(
                ["yt-dlp", "--version"],
                capture_output=True, text=True, timeout=10
            )
        except FileNotFoundError:
            raise RuntimeError("yt-dlp не установлен. Установи: pip install yt-dlp")

    def search(self, query: str, max_results: int = 10, platform: str = "ytsearch") -> List[Dict]:
        """Поиск видео по ключевым словам

        platform: ytsearch (YouTube), ttsearch (TikTok) или другие
        """
        search_query = f"{platform}{max_results}:{query}"
        try:
            result = subprocess.run(
                ["yt-dlp", f"--dump-json", "--no-download",
                 "--flat-playlist", search_query],
                capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                logger.warning(f"yt-dlp search error: {result.stderr[:200]}")
                return []

            videos = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    videos.append(self._parse_data(data))
                except json.JSONDecodeError:
                    continue

            # Сортируем по engagement
            videos.sort(key=lambda v: v.get("engagement_score", 0), reverse=True)
            return videos[:max_results]

        except subprocess.TimeoutExpired:
            logger.error("yt-dlp search timed out")
            return []
        except Exception as e:
            logger.error(f"yt-dlp search error: {e}")
            return []

    def get_video_info(self, url: str) -> Optional[Dict]:
        """Получить детальную информацию о видео по URL"""
        try:
            result = subprocess.run(
                ["yt-dlp", "--dump-json", "--no-download",
                 "--no-playlist", url],
                capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0 or not result.stdout.strip():
                logger.warning(f"yt-dlp info error for {url}: {result.stderr[:200]}")
                return None

            data = json.loads(result.stdout.strip().split("\n")[0])
            return self._parse_data(data)

        except Exception as e:
            logger.error(f"yt-dlp info error: {e}")
            return None

    def trending(self, max_results: int = 10) -> List[Dict]:
        """Трендовые видео YouTube"""
        return self.search("", max_results=min(max_results, 50))

    def search_tiktok(self, query: str, max_results: int = 10) -> List[Dict]:
        """Поиск в TikTok"""
        return self.search(query, max_results, platform="ttsearch")

    def get_tiktok_trending(self, max_results: int = 15) -> List[Dict]:
        """Трендовые TikTok"""
        return self.search_tiktok("trending", max_results)

    @staticmethod
    def _parse_data(data: Dict) -> Dict:
        """Преобразовать yt-dlp данные в единый формат

        yt-dlp выдаёт много полей, часть может отсутствовать
        в зависимости от платформы.
        """
        extractor = data.get("extractor_key", "unknown").lower()

        # Определяем платформу
        if "tiktok" in extractor:
            platform = "tiktok"
        elif "youtube" in extractor or extractor == "youtube":
            platform = "youtube"
        elif "instagram" in extractor:
            platform = "instagram"
        elif "twitter" in extractor or "x" in extractor:
            platform = "twitter"
        else:
            platform = extractor

        # Форматируем дату
        timestamp = data.get("timestamp")
        published_ago = ""
        if timestamp:
            try:
                dt = datetime.fromtimestamp(timestamp)
                hours_ago = (datetime.utcnow() - dt).total_seconds() / 3600
                if hours_ago < 1:
                    published_ago = "только что"
                elif hours_ago < 24:
                    published_ago = f"{int(hours_ago)}ч назад"
                else:
                    published_ago = f"{int(hours_ago / 24)}д назад"
            except Exception:
                published_ago = data.get("upload_date", "")[:8] if data.get("upload_date") else ""

        # Статистика (может отсутствовать на некоторых платформах)
        views = data.get("view_count") or 0
        likes = data.get("like_count") or 0
        comments = data.get("comment_count") or 0

        # Engagement score (универсальный)
        engagement_score = likes * 2 + comments * 3 + views // 100

        # Описание — обрезаем до 2000 символов
        description = (data.get("description") or "")
        if isinstance(description, bytes):
            description = description.decode("utf-8", errors="replace")
        if len(description) > 2000:
            description = description[:2000] + "…"

        # Длительность
        duration = data.get("duration") or 0
        duration_str = ""
        if duration > 0:
            minutes = duration // 60
            seconds = duration % 60
            if minutes >= 60:
                hours = minutes // 60
                minutes = minutes % 60
                duration_str = f"{hours}:{minutes:02d}:{seconds:02d}"
            else:
                duration_str = f"{minutes}:{seconds:02d}"

        # Теги
        tags = data.get("tags") or []

        return {
            "id": data.get("id", ""),
            "platform": platform,
            "title": data.get("title", ""),
            "description": description,
            "channel": data.get("channel") or data.get("uploader") or "",
            "channel_url": data.get("channel_url") or data.get("uploader_url") or "",
            "published_ago": published_ago,
            "thumbnail": data.get("thumbnail", ""),
            "url": data.get("webpage_url", ""),
            "duration": duration,
            "duration_str": duration_str,
            "views": views,
            "likes": likes,
            "comments": comments,
            "engagement_score": engagement_score,
            "tags": tags,
            "extractor": extractor,
        }
