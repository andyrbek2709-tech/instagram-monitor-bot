import sqlite3
import logging
from typing import Dict, List, Tuple
from datetime import datetime, timedelta
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, UserNotFound

logger = logging.getLogger(__name__)


class AccountValidator:
    """Валидация и проверка статуса Instagram аккаунтов"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def validate_account_exists(self, client: Client, username: str) -> Tuple[bool, str]:
        """Проверить существование аккаунта"""
        try:
            user = client.user_info_by_username(username)
            logger.info(f"Account {username} validated successfully (ID: {user.pk})")
            return True, f"Account found: {user.full_name or username}"
        except UserNotFound:
            logger.warning(f"Account {username} not found")
            return False, f"Account @{username} not found on Instagram"
        except Exception as e:
            logger.error(f"Error validating account {username}: {e}")
            return False, f"Error validating account: {str(e)}"

    def validate_account_is_active(self, client: Client, username: str) -> Tuple[bool, str]:
        """Проверить активность аккаунта (может быть заблокирован)"""
        try:
            user = client.user_info_by_username(username)

            # Проверить есть ли недавние посты
            medias = client.user_medias(user.pk, amount=1)
            if not medias:
                return False, "Account has no posts"

            return True, "Account is active"
        except Exception as e:
            logger.error(f"Error checking account activity {username}: {e}")
            return False, f"Account may be restricted: {str(e)}"

    def mark_account_invalid(self, username: str) -> bool:
        """Отметить аккаунт как неактивный"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE monitored_accounts SET is_active = 0 WHERE username = ?',
                    (username,)
                )
                conn.commit()
                logger.info(f"Marked account {username} as inactive")
                return True
        except Exception as e:
            logger.error(f"Error marking account invalid: {e}")
            return False

    def check_stale_accounts(self, stale_days: int = 7) -> List[Dict]:
        """Найти аккаунты, которые давно не обновлялись"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cutoff_date = (datetime.utcnow() - timedelta(days=stale_days)).isoformat()

                cursor.execute('''
                    SELECT id, username, last_fetch
                    FROM monitored_accounts
                    WHERE is_active = 1
                    AND (last_fetch IS NULL OR last_fetch < ?)
                    ORDER BY last_fetch ASC
                ''', (cutoff_date,))

                stale_accounts = []
                for row in cursor.fetchall():
                    stale_accounts.append({
                        'id': row[0],
                        'username': row[1],
                        'last_fetch': row[2]
                    })

                if stale_accounts:
                    logger.warning(f"Found {len(stale_accounts)} stale accounts")

                return stale_accounts

        except Exception as e:
            logger.error(f"Error checking stale accounts: {e}")
            return []

    def verify_all_accounts(self, client: Client) -> Dict[str, List[str]]:
        """Проверить все активные аккаунты"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                cursor.execute(
                    'SELECT username FROM monitored_accounts WHERE is_active = 1'
                )
                usernames = [row[0] for row in cursor.fetchall()]

            valid_accounts = []
            invalid_accounts = []

            for username in usernames:
                is_valid, msg = self.validate_account_exists(client, username)
                if is_valid:
                    is_active, activity_msg = self.validate_account_is_active(client, username)
                    if is_active:
                        valid_accounts.append(username)
                    else:
                        invalid_accounts.append(username)
                        self.mark_account_invalid(username)
                else:
                    invalid_accounts.append(username)
                    self.mark_account_invalid(username)

            logger.info(f"Account verification: {len(valid_accounts)} valid, {len(invalid_accounts)} invalid")
            return {
                'valid': valid_accounts,
                'invalid': invalid_accounts
            }

        except Exception as e:
            logger.error(f"Error verifying accounts: {e}")
            return {'valid': [], 'invalid': []}
