import psycopg2
import logging
from typing import Dict, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class AccountValidator:
    """Валидация аккаунтов через БД (HikerAPI не требует прямого логина)"""

    def __init__(self, db_url: str):
        self.db_url = db_url

    def mark_account_invalid(self, username: str) -> bool:
        """Отметить аккаунт как неактивный"""
        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE monitored_accounts SET is_active = 0 WHERE username = %s',
                (username,)
            )
            conn.commit()
            cursor.close()
            conn.close()
            logger.info(f"Marked account {username} as inactive")
            return True
        except Exception as e:
            logger.error(f"Error marking account invalid: {e}")
            return False

    def check_stale_accounts(self, stale_days: int = 7) -> List[Dict]:
        """Найти аккаунты, которые давно не обновлялись"""
        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            cutoff_date = (datetime.utcnow() - timedelta(days=stale_days)).isoformat()

            cursor.execute('''
                SELECT id, username, last_fetch
                FROM monitored_accounts
                WHERE is_active = 1
                AND (last_fetch IS NULL OR last_fetch < %s)
                ORDER BY last_fetch ASC
            ''', (cutoff_date,))

            stale_accounts = []
            for row in cursor.fetchall():
                stale_accounts.append({
                    'id': row[0],
                    'username': row[1],
                    'last_fetch': row[2]
                })

            cursor.close()
            conn.close()

            if stale_accounts:
                logger.warning(f"Found {len(stale_accounts)} stale accounts")

            return stale_accounts

        except Exception as e:
            logger.error(f"Error checking stale accounts: {e}")
            return []
