import sqlite3
import logging
from datetime import datetime, timedelta
from typing import List

logger = logging.getLogger(__name__)


class DataMaintenance:
    """Обслуживание базы данных"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def cleanup_old_posts(self, days_to_keep: int = 90) -> int:
        """Удалить посты старше указанного периода"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cutoff_date = (datetime.utcnow() - timedelta(days=days_to_keep)).isoformat()

                # Получить ID постов для удаления
                cursor.execute(
                    'SELECT id FROM posts WHERE fetched_at < ?',
                    (cutoff_date,)
                )
                post_ids = [row[0] for row in cursor.fetchall()]

                if not post_ids:
                    logger.info("No old posts to cleanup")
                    return 0

                # Удалить связанные записи
                placeholders = ','.join('?' * len(post_ids))
                cursor.execute(f'DELETE FROM filter_results WHERE post_id IN ({placeholders})', post_ids)
                cursor.execute(f'DELETE FROM analyses WHERE post_id IN ({placeholders})', post_ids)
                cursor.execute(f'DELETE FROM posts WHERE id IN ({placeholders})', post_ids)

                conn.commit()
                logger.info(f"Cleaned up {len(post_ids)} old posts")
                return len(post_ids)

        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
            return 0

    def cleanup_old_logs(self, days_to_keep: int = 30) -> None:
        """Очистить логи старше указанного периода"""
        try:
            import os
            log_file = 'logs/bot.log'

            if not os.path.exists(log_file):
                return

            cutoff_date = datetime.utcnow() - timedelta(days=days_to_keep)
            file_mtime = datetime.fromtimestamp(os.path.getmtime(log_file))

            if file_mtime < cutoff_date:
                with open(log_file, 'w') as f:
                    f.write('')
                logger.info("Log file cleared")

        except Exception as e:
            logger.error(f"Error clearing logs: {e}")

    def archive_old_stats(self, days_to_keep: int = 365) -> int:
        """Архивировать старые статистики"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cutoff_date = (datetime.utcnow() - timedelta(days=days_to_keep)).isoformat()

                cursor.execute(
                    'DELETE FROM daily_stats WHERE date < ?',
                    (cutoff_date,)
                )

                conn.commit()
                deleted = cursor.rowcount
                logger.info(f"Archived {deleted} old stats")
                return deleted

        except Exception as e:
            logger.error(f"Error archiving stats: {e}")
            return 0

    def generate_daily_stats(self) -> bool:
        """Генерировать ежедневную статистику"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                today = datetime.utcnow().date().isoformat()

                # Получить статистику за день
                cursor.execute('''
                    SELECT
                        COUNT(*) as total_posts,
                        AVG(a.relevance_score) as avg_relevance,
                        SUM(CASE WHEN a.relevance_score > 0.7 THEN 1 ELSE 0 END) as high_relevance,
                        SUM(CASE WHEN a.viral_potential = 'high' THEN 1 ELSE 0 END) as viral_count,
                        SUM(CASE WHEN a.sentiment = 'positive' THEN 1 ELSE 0 END) as positive,
                        SUM(CASE WHEN a.sentiment = 'negative' THEN 1 ELSE 0 END) as negative,
                        SUM(CASE WHEN a.sentiment = 'neutral' THEN 1 ELSE 0 END) as neutral
                    FROM analyses a
                    WHERE DATE(a.analyzed_at) = ?
                ''', (today,))

                row = cursor.fetchone()
                if not row or row[0] == 0:
                    logger.info(f"No posts analyzed today")
                    return False

                total, avg_rel, high_rel, viral, pos, neg, neut = row

                cursor.execute('''
                    INSERT OR REPLACE INTO daily_stats
                    (date, total_posts, avg_relevance, high_relevance_count, viral_count,
                     sentiment_positive, sentiment_negative, sentiment_neutral, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    today, int(total), avg_rel or 0, int(high_rel or 0), int(viral or 0),
                    int(pos or 0), int(neg or 0), int(neut or 0), datetime.utcnow().isoformat()
                ))

                conn.commit()
                logger.info(f"Generated daily stats for {today}: {int(total)} posts")
                return True

        except Exception as e:
            logger.error(f"Error generating daily stats: {e}")
            return False

    def run_maintenance(self) -> None:
        """Запустить все задачи обслуживания"""
        logger.info("Starting maintenance tasks")
        self.cleanup_old_posts(days_to_keep=90)
        self.cleanup_old_logs(days_to_keep=30)
        self.archive_old_stats(days_to_keep=365)
        self.generate_daily_stats()
        logger.info("Maintenance tasks completed")
