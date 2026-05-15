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

class ContentFilter:
    def __init__(self, api_key: str):
        self.client = OpenAI(api_key=api_key)

    def _openai_classify(self, caption: str) -> Dict[str, bool]:
        """Классификация через GPT-4o-mini"""
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
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"OpenAI classification error: {e}")
            return {"is_ad": False, "is_greeting": False, "is_personal": False}

    def classify_post(self, caption: str) -> Dict[str, any]:
        gpt_result = self._openai_classify(caption)
        # Оставляем логику скоринга по ключевым словам для надежности
        return {
            'is_ad': gpt_result.get('is_ad', False),
            'is_greeting': gpt_result.get('is_greeting', False),
            'is_personal': gpt_result.get('is_personal', False)
        }
