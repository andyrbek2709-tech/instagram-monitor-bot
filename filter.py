import re
import psycopg2
import logging
import json
import time
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from openai import OpenAI
import os

logger = logging.getLogger(__name__)

# Ключевые слова для детекции рекламы
RUSSIAN_AD_KEYWORDS = {
    'реклама': 1.0, 'спонсор': 1.0, 'промокод': 0.9, 'купить': 0.8, 'заказать': 0.8,
    'скидка': 0.7, 'акция': 0.7, 'выиграй': 0.8, 'розыгрыш': 0.8, 'ссылка в био': 0.6
}
ENGLISH_AD_KEYWORDS = {
    'sponsored': 1.0, 'advertisement': 1.0, 'ad': 0.9, 'promo': 0.9, 'discount': 0.7,
    'buy': 0.7, 'order': 0.7, 'sale': 0.8, 'coupon': 0.9, 'link in bio': 0.6
}

class ContentFilter:
    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)

    def classify_post(self, caption: str) -> Dict[str, any]:
        prompt = (
            f"Классифицируй подпись Instagram:\n{caption}\n\n"
            'Верни ТОЛЬКО JSON: {"is_ad": bool, "is_greeting": bool, "is_personal": bool}'
        )
        try:
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={ "type": "json_object" }
            )
            result = json.loads(response.choices[0].message.content)
            keyword_score = self._keyword_scoring(caption)
            return {
                'is_ad': result.get('is_ad', False) or keyword_score > 0.7,
                'is_greeting': result.get('is_greeting', False),
                'is_personal': result.get('is_personal', False),
                'ad_score': keyword_score
            }
        except Exception as e:
            logger.error(f"OpenAI classification error: {e}")
            return {"is_ad": False, "is_greeting": False, "is_personal": False, "ad_score": 0.0}

    def _keyword_scoring(self, text: str) -> float:
        text_lower = text.lower()
        score = 0.0
        for keyword, weight in {**RUSSIAN_AD_KEYWORDS, **ENGLISH_AD_KEYWORDS}.items():
            if keyword in text_lower:
                score = max(score, weight)
        return min(score, 1.0)

class AdDetector:
    def detect_ad(self, caption: str, engagement_rate: float = 0.0) -> Tuple[bool, float]:
        score = 0.0
        text_lower = caption.lower()
        for keyword, weight in {**RUSSIAN_AD_KEYWORDS, **ENGLISH_AD_KEYWORDS}.items():
            if keyword in text_lower:
                score = max(score, weight)
        if engagement_rate < 0.02: score = min(score + 0.1, 1.0)
        return score > 0.6, min(score, 1.0)

class MetadataAnalyzer:
    def analyze_engagement(self, post_data: Dict) -> Dict[str, any]:
        likes = post_data.get('likes', 0)
        comments = post_data.get('comments', 0)
        engagement_rate = (likes + comments * 2) / max(likes + 1, 100)
        return {
            'engagement_rate': min(engagement_rate, 1.0),
            'text_length': len(post_data.get('caption', '')),
            'has_media': post_data.get('media_type') in [1, 2]
        }

class Filter:
    def __init__(self, db_url: str, openai_api_key: str):
        self.db_url = db_url
        self.content_filter = ContentFilter(openai_api_key)
        self.ad_detector = AdDetector()
        self.metadata_analyzer = MetadataAnalyzer()

    def process_posts(self, posts: List[Dict]) -> List[Dict]:
        results = []
        for post in posts:
            cls = self.content_filter.classify_post(post.get('caption', ''))
            is_ad, ad_conf = self.ad_detector.detect_ad(post.get('caption', ''), post.get('engagement_rate', 0.0))
            meta = self.metadata_analyzer.analyze_engagement(post)
            results.append({
                'post_id': post['post_id'],
                'is_ad': is_ad or cls['is_ad'],
                'is_greeting': cls['is_greeting'],
                'is_personal': cls['is_personal'],
                'engagement_rate': meta['engagement_rate'],
                'text_length': meta['text_length'],
                'has_media': meta['has_media'],
                'ad_score': ad_conf
            })
        return results
