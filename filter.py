import re
import psycopg2
import logging
import json
import time
from datetime import datetime
from typing import Dict, List, Tuple, Optional
import openai

logger = logging.getLogger(__name__)

MAX_FILTER_RETRIES = 3
FILTER_RETRY_DELAY = 2

# Ключевые слова для детекции рекламы
RUSSIAN_AD_KEYWORDS = {
    'реклама': 1.0,
    'спонсор': 1.0,
    'промокод': 0.9,
    'купить': 0.8,
    'заказать': 0.8,
    'скидка': 0.7,
    'акция': 0.7,
    'выиграй': 0.8,
    'розыгрыш': 0.8,
    'ссылка в био': 0.6
}

ENGLISH_AD_KEYWORDS = {
    'sponsored': 1.0,
    'advertisement': 1.0,
    'ad': 0.9,
    'promo': 0.9,
    'discount': 0.7,
    'buy': 0.7,
    'order': 0.7,
    'sale': 0.8,
    'coupon': 0.9,
    'link in bio': 0.6
}


class ContentFilter:
    """Фильтрация контента с двухуровневой классификацией"""

    def __init__(self, gemini_api_key: str):
        openai.api_key = gemini_api_key
        self.openai_client = openai.OpenAI(api_key=gemini_api_key)

    def _gemini_quick_classify(self, caption: str) -> Dict[str, bool]:
        """Быстрая классификация через Gemini с retry"""
        for attempt in range(MAX_FILTER_RETRIES):
            try:
                response = self.openai_client.chat.completions.create(
                    model="gemini-1.5",
                    messages=[{
                        "role": "user",
                        "content": f"""Ты — Gemini. Классифицируй эту подпись Instagram:
{caption}

Верни JSON: {{"is_ad": bool, "is_greeting": bool, "is_personal": bool}}
Только JSON, без текста."""
                    }],
                    temperature=0.0,
                    max_tokens=80
                )

                result_text = response.choices[0].message.content.strip()
                return json.loads(result_text)
            except json.JSONDecodeError as e:
                logger.warning(f"Gemini JSON error on attempt {attempt + 1}: {e}")
                if attempt < MAX_FILTER_RETRIES - 1:
                    time.sleep(FILTER_RETRY_DELAY)
            except Exception as e:
                logger.warning(f"Gemini error on attempt {attempt + 1}: {e}")
                if attempt < MAX_FILTER_RETRIES - 1:
                    time.sleep(FILTER_RETRY_DELAY)
                else:
                    logger.error(f"Gemini classification failed after {MAX_FILTER_RETRIES} attempts")

        return {"is_ad": False, "is_greeting": False, "is_personal": False}

    def _keyword_scoring(self, text: str) -> float:
        """Оценка текста по ключевым словам"""
        text_lower = text.lower()
        score = 0.0
        max_score = 0.0

        # Русские ключевые слова
        for keyword, weight in RUSSIAN_AD_KEYWORDS.items():
            if keyword in text_lower:
                score = max(score, weight)
                max_score = weight

        # Английские ключевые слова
        for keyword, weight in ENGLISH_AD_KEYWORDS.items():
            if keyword in text_lower:
                score = max(score, weight)

        return min(score, 1.0)

    def classify_post(self, caption: str) -> Dict[str, any]:
        """Полная классификация поста"""

        # Этап 1: быстрая классификация Gemini
        gpt_result = self._gemini_quick_classify(caption)

        # Этап 2: ключевое слово scoring
        keyword_score = self._keyword_scoring(caption)

        return {
            'is_ad': gpt_result.get('is_ad', False) or keyword_score > 0.7,
            'is_greeting': gpt_result.get('is_greeting', False),
            'is_personal': gpt_result.get('is_personal', False),
            'ad_score': keyword_score
        }


class AdDetector:
    """Детекция рекламы с взвешиванием"""

    def detect_ad(self, caption: str, engagement_rate: float = 0.0) -> Tuple[bool, float]:
        """Детектировать рекламу и вернуть (is_ad, confidence)"""

        # Базовое ключевое слово scoring
        score = 0.0

        text_lower = caption.lower()

        # Проверка русских ключевых слов
        for keyword, weight in RUSSIAN_AD_KEYWORDS.items():
            if keyword in text_lower:
                score = max(score, weight)

        # Проверка английских ключевых слов
        for keyword, weight in ENGLISH_AD_KEYWORDS.items():
            if keyword in text_lower:
                score = max(score, weight)

        # Корректировка по engagement rate
        if engagement_rate < 0.02:
            score = min(score + 0.1, 1.0)

        is_ad = score > 0.6
        return is_ad, min(score, 1.0)


class MetadataAnalyzer:
    """Анализ метаданных и engagement"""

    def analyze_engagement(self, post_data: Dict) -> Dict[str, any]:
        """Анализировать engagement поста"""

        likes = post_data.get('likes', 0)
        comments = post_data.get('comments', 0)
        caption = post_data.get('caption', '')

        # Расчет engagement rate (упрощенный)
        engagement_rate = (likes + comments * 2) / max(likes + 1, 100)
        engagement_rate = min(engagement_rate, 1.0)

        # Качество engagement
        comment_to_like_ratio = comments / max(likes, 1)
        quality = 'high' if comment_to_like_ratio > 0.1 else 'medium' if comment_to_like_ratio > 0.02 else 'low'

        # Текстовые метрики
        text_length = len(caption)
        has_hashtags = '#' in caption
        has_mentions = '@' in caption
        has_emoji = bool(re.search(r'[\U0001F300-\U0001F9FF]', caption))
        has_url = 'http' in caption or 'www.' in caption

        return {
            'engagement_rate': engagement_rate,
            'engagement_quality': quality,
            'text_length': text_length,
            'has_hashtags': has_hashtags,
            'has_mentions': has_mentions,
            'has_emoji': has_emoji,
            'has_url': has_url,
            'comment_count': comments,
            'like_count': likes
        }

    def analyze_content_patterns(self, caption: str) -> Dict[str, any]:
        """Анализировать паттерны контента"""

        return {
            'has_hashtags': '#' in caption,
            'hashtag_count': len(re.findall(r'#\w+', caption)),
            'has_mentions': '@' in caption,
            'mention_count': len(re.findall(r'@\w+', caption)),
            'emoji_count': len(re.findall(r'[\U0001F300-\U0001F9FF]', caption)),
            'url_count': len(re.findall(r'http\S+|www\.\S+', caption)),
            'line_count': len(caption.split('\n'))
        }


class Filter:
    """Основной фильтр для orchestration"""

    def __init__(self, db_url: str, gemini_api_key: str):
        self.db_url = db_url
        self.content_filter = ContentFilter(gemini_api_key)
        self.ad_detector = AdDetector()
        self.metadata_analyzer = MetadataAnalyzer()

    def _insert_filter_result(self, cursor, post_id: int, result: Dict) -> None:
        """Вставить результат фильтра в БД"""
        try:
            cursor.execute('''
                INSERT INTO filter_results
                (post_id, is_ad, is_greeting, is_personal, engagement_rate, text_length, has_media, analyzed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (post_id) DO UPDATE SET
                is_ad = EXCLUDED.is_ad,
                is_greeting = EXCLUDED.is_greeting,
                is_personal = EXCLUDED.is_personal,
                engagement_rate = EXCLUDED.engagement_rate,
                text_length = EXCLUDED.text_length,
                has_media = EXCLUDED.has_media,
                analyzed_at = EXCLUDED.analyzed_at
            ''', (
                post_id,
                1 if result.get('is_ad') else 0,
                1 if result.get('is_greeting') else 0,
                1 if result.get('is_personal') else 0,
                result.get('engagement_rate', 0.0),
                result.get('text_length', 0),
                1 if result.get('has_media') else 0,
                datetime.utcnow().isoformat()
            ))
        except Exception as e:
            logger.error(f"Error inserting filter result: {e}")

    def process_posts(self, posts: List[Dict]) -> List[Dict]:
        """Обработать список постов"""
        results = []

        for post in posts:
            # Классификация контента
            classification = self.content_filter.classify_post(post.get('caption', ''))

            # Детекция рекламы
            is_ad, ad_confidence = self.ad_detector.detect_ad(post.get('caption', ''))

            # Анализ engagement
            metadata = self.metadata_analyzer.analyze_engagement(post)

            result = {
                'post_id': post['post_id'],
                'is_ad': is_ad or classification['is_ad'],
                'is_greeting': classification['is_greeting'],
                'is_personal': classification['is_personal'],
                'engagement_rate': metadata['engagement_rate'],
                'text_length': metadata['text_length'],
                'has_media': post.get('media_type') in [1, 2],
                'ad_score': ad_confidence
            }

            results.append(result)

        # Сохранить в БД
        conn = psycopg2.connect(self.db_url)
        cursor = conn.cursor()

        try:
            for result in results:
                # Получить post_id из таблицы posts
                cursor.execute('SELECT id FROM posts WHERE post_id = %s', (result['post_id'],))
                post_row = cursor.fetchone()
                if post_row:
                    self._insert_filter_result(cursor, post_row[0], result)

            conn.commit()
        finally:
            cursor.close()
            conn.close()

        return results
