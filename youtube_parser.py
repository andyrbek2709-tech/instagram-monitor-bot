import os
import re
import json
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeClient:
    """Клиент для YouTube Data API v3"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def search_videos(self, query: str, max_results: int = 10) -> List[Dict]:
        """Поиск видео по ключевым словам"""
        r = self.session.get(
            f"{YOUTUBE_API_BASE}/search",
            params={
                "part": "snippet",
                "q": query,
                "maxResults": max_results,
                "type": "video",
                "order": "relevance",
                "key": self.api_key,
            },
            timeout=15
        )
        r.raise_for_status()
        return r.json().get("items", [])

    def get_trending_videos(self, region_code: str = "RU", max_results: int = 10) -> List[Dict]:
        """Получить трендовые видео"""
        r = self.session.get(
            f"{YOUTUBE_API_BASE}/videos",
            params={
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "regionCode": region_code,
                "maxResults": max_results,
                "key": self.api_key,
            },
            timeout=15
        )
        r.raise_for_status()
        return r.json().get("items", [])

    def get_video_details(self, video_id: str) -> Optional[Dict]:
        """Получить детальную информацию о видео"""
        r = self.session.get(
            f"{YOUTUBE_API_BASE}/videos",
            params={
                "part": "snippet,statistics",
                "id": video_id,
                "key": self.api_key,
            },
            timeout=15
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        return items[0] if items else None

    def search_videos_by_hashtag(self, hashtag: str, max_results: int = 10) -> List[Dict]:
        """Поиск видео по хэштегу"""
        return self.search_videos(f"#{hashtag}", max_results)

    @staticmethod
    def parse_video_data(item: Dict) -> Dict:
        """Преобразовать ответ API в единый формат"""
        snippet = item.get("snippet", {})
        statistics = item.get("statistics", {})

        video_id = item.get("id", "")
        if isinstance(video_id, dict):
            video_id = video_id.get("videoId", "")

        # Форматируем дату
        published = snippet.get("publishedAt", "")
        if published:
            try:
                published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
                hours_ago = (datetime.utcnow().replace(tzinfo=None) - published_dt.replace(tzinfo=None)).total_seconds() / 3600
                if hours_ago < 1:
                    time_str = "только что"
                elif hours_ago < 24:
                    time_str = f"{int(hours_ago)}ч назад"
                else:
                    time_str = f"{int(hours_ago / 24)}д назад"
            except Exception:
                time_str = published[:10]
        else:
            time_str = ""

        likes = int(statistics.get("likeCount", 0) or 0)
        views = int(statistics.get("viewCount", 0) or 0)
        comments = int(statistics.get("commentCount", 0) or 0)

        return {
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "channel": snippet.get("channelTitle", ""),
            "published_at": published,
            "published_ago": time_str,
            "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
            "url": f"https://youtube.com/watch?v={video_id}",
            "views": views,
            "likes": likes,
            "comments": comments,
            "engagement_score": likes * 2 + comments * 3 + views // 100,
        }


class YouTubeParser:
    """Парсер YouTube — поиск, тренды, детали"""

    def __init__(self, api_key: str):
        self.client = YouTubeClient(api_key)

    def search(self, query: str, max_results: int = 10) -> List[Dict]:
        """Поиск видео"""
        items = self.client.search_videos(query, max_results)
        videos = []
        for item in items:
            videos.append(YouTubeClient.parse_video_data(item))
        # Сортируем по engagement
        videos.sort(key=lambda v: v["engagement_score"], reverse=True)
        return videos

    def trending(self, region: str = "RU", max_results: int = 10) -> List[Dict]:
        """Трендовые видео"""
        items = self.client.get_trending_videos(region, max_results)
        videos = []
        for item in items:
            videos.append(YouTubeClient.parse_video_data(item))
        return videos

    def search_by_hashtag(self, hashtag: str, max_results: int = 10) -> List[Dict]:
        """Поиск по хэштегу"""
        return self.search(f"#{hashtag}", max_results)
