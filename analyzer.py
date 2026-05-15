import json
import psycopg2
import logging
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from openai import OpenAI
import time

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2

BRAND_LIST = [
    'Apple', 'Samsung', 'Nike', 'Adidas', 'Louis Vuitton', 'Gucci', 'Prada',
    'Mercedes', 'BMW', 'Tesla', 'Toyota', 'Coca-Cola', 'Pepsi', 'Starbucks',
    'McDonald\'s', 'Amazon', 'Google', 'Microsoft', 'Facebook', 'Instagram'
]

CONTENT_CATEGORIES = {
    'lifestyle': ['lifestyle', 'life', 'daily', 'everyday', 'mood', 'moment', 'vibe'],
    'technology': ['tech', 'digital', 'gadget', 'innovation', 'software'],
    'beauty': ['beauty', 'makeup', 'skincare', 'cosmetics', 'face', 'hair'],
    'fitness': ['fitness', 'gym', 'workout', 'exercise', 'sport', 'health'],
    'food': ['food', 'eat', 'recipe', 'cook', 'restaurant', 'delicious'],
    'travel': ['travel', 'trip', 'vacation', 'adventure', 'explore', 'journey'],
    'business': ['business', 'work', 'career', 'entrepreneur', 'startup', 'success'],
    'education': ['education', 'learn', 'study', 'knowledge', 'course', 'class'],
    'entertainment': ['entertainment', 'fun', 'party', 'event', 'show', 'movie'],
    'personal': ['personal', 'diary', 'thoughts', 'story', 'reflection', 'feelings']
}

AUDIENCE_SEGMENTS = [
    'young_professionals', 'health_enthusiasts', 'tech_savvy', 'entrepreneurs',
    'students', 'foodies', 'travelers', 'fashion_conscious', 'general_audience'
]

class TextAnalyzer:
    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)

    def analyze_sentiment(self, text: str) -> Tuple[str, float]:
        prompt = (
            f"Проанализируй тон текста и верни JSON:\n"
            '{"sentiment": "positive"|"negative"|"neutral", "confidence": 0.0-1.0}\n\n'
            f"Текст:\n{text}"
        )
        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"}
                )
                result = json.loads(response.choices[0].message.content)
                return result.get('sentiment', 'neutral'), result.get('confidence', 0.5)
            except Exception as e:
                logger.warning(f"Sentiment analysis error on attempt {attempt + 1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                else:
                    return 'neutral', 0.5
        return 'neutral', 0.5

    def extract_key_topics(self, text: str) -> List[str]:
        topics = []
        text_lower = text.lower()
        for category, keywords in CONTENT_CATEGORIES.items():
            for keyword in keywords:
                if keyword in text_lower:
                    if category not in topics:
                        topics.append(category)
                    break
        return topics if topics else ['general_audience']

    def extract_brand_mentions(self, text: str) -> List[str]:
        brands = []
        for brand in BRAND_LIST:
            if brand.lower() in text.lower():
                brands.append(brand)
        return brands

    def assess_content_quality(self, text: str, engagement_rate: float) -> str:
        length_score = len(text) / 500
        engagement_score = engagement_rate
        combined_score = (length_score + engagement_score) / 2
        if combined_score > 0.6: return 'high'
        elif combined_score > 0.3: return 'medium'
        else: return 'low'

class InsightGenerator:
    def determine_audience_segment(self, topics: List[str], text: str) -> str:
        segment_map = {
            'business': 'entrepreneurs', 'fitness': 'health_enthusiasts',
            'technology': 'tech_savvy', 'education': 'students', 'food': 'foodies',
            'travel': 'travelers', 'beauty': 'fashion_conscious', 'lifestyle': 'young_professionals'
        }
        for topic in topics:
            if topic in segment_map: return segment_map[topic]
        return 'general_audience'

    def calculate_relevance_score(self, engagement_rate: float, quality: str, sentiment: str) -> float:
        engagement_score = min(engagement_rate / 0.05, 1.0) * 0.6
        quality_score = {'high': 0.3, 'medium': 0.15, 'low': 0.0}.get(quality, 0.0)
        sentiment_score = {'positive': 0.15, 'neutral': 0.05, 'negative': 0.0}.get(sentiment, 0.0)
        return min(engagement_score + quality_score + sentiment_score, 1.0)

    def assess_viral_potential(self, engagement_rate: float, sentiment: str, quality: str) -> str:
        score = 0.0
        if engagement_rate > 0.1: score += 0.4
        elif engagement_rate > 0.05: score += 0.2
        if sentiment == 'positive': score += 0.3
        if quality == 'high': score += 0.3
        return 'high' if score > 0.6 else 'medium' if score > 0.3 else 'low'

    def generate_recommendations(self, post_data: Dict) -> List[str]:
        recommendations = []
        if post_data.get('sentiment') == 'positive':
            recommendations.append('Positive sentiment detected - good for engagement')
        if post_data.get('relevance_score', 0) > 0.7:
            recommendations.append('High relevance - suitable for audience')
        if post_data.get('viral_potential') == 'high':
            recommendations.append('High viral potential - consider boosting')
        return recommendations[:4] if recommendations else ['Monitor performance trends']

class Analyzer:
    def __init__(self, db_url: str, openai_api_key: str):
        self.db_url = db_url
        self.text_analyzer = TextAnalyzer(openai_api_key)
        self.insight_generator = InsightGenerator()

    def analyze_post(self, post_data: Dict) -> Dict:
        caption = post_data.get('caption', '')
        sentiment, sentiment_conf = self.text_analyzer.analyze_sentiment(caption)
        topics = self.text_analyzer.extract_key_topics(caption)
        brands = self.text_analyzer.extract_brand_mentions(caption)
        quality = self.text_analyzer.assess_content_quality(caption, post_data.get('engagement_rate', 0.0))
        audience = self.insight_generator.determine_audience_segment(topics, caption)
        relevance = self.insight_generator.calculate_relevance_score(
            post_data.get('engagement_rate', 0.0), quality, sentiment
        )
        viral = self.insight_generator.assess_viral_potential(
            post_data.get('engagement_rate', 0.0), sentiment, quality
        )
        recommendations = self.insight_generator.generate_recommendations({
            'sentiment': sentiment, 'relevance_score': relevance, 'viral_potential': viral
        })
        return {
            'post_id': post_data['post_id'], 'sentiment': sentiment, 'key_topics': topics,
            'brand_mentions': brands, 'audience_segment': audience, 'content_quality': quality,
            'relevance_score': relevance, 'viral_potential': viral, 'recommendations': recommendations,
            'analyzed_at': datetime.utcnow().isoformat()
        }

    def process_posts(self, posts: List[Dict]) -> List[Dict]:
        results = []
        for post in posts:
            analysis = self.analyze_post(post)
            results.append(analysis)
        conn = psycopg2.connect(self.db_url)
        cursor = conn.cursor()
        try:
            for analysis in results:
                cursor.execute('SELECT id FROM posts WHERE post_id = %s', (analysis['post_id'],))
                post_row = cursor.fetchone()
                if post_row:
                    cursor.execute('''
                        INSERT INTO analyses
                        (post_id, sentiment, key_topics, brand_mentions, audience_segment,
                         content_quality, relevance_score, viral_potential, recommendations, analyzed_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (post_id) DO UPDATE SET
                        sentiment = EXCLUDED.sentiment, key_topics = EXCLUDED.key_topics,
                        brand_mentions = EXCLUDED.brand_mentions, audience_segment = EXCLUDED.audience_segment,
                        content_quality = EXCLUDED.content_quality, relevance_score = EXCLUDED.relevance_score,
                        viral_potential = EXCLUDED.viral_potential, recommendations = EXCLUDED.recommendations,
                        analyzed_at = EXCLUDED.analyzed_at
                    ''', (
                        post_row[0], analysis['sentiment'], json.dumps(analysis['key_topics']),
                        json.dumps(analysis['brand_mentions']), analysis['audience_segment'],
                        analysis['content_quality'], analysis['relevance_score'], analysis['viral_potential'],
                        json.dumps(analysis['recommendations']), analysis['analyzed_at']
                    ))
            conn.commit()
        finally:
            cursor.close()
            conn.close()
        return results
