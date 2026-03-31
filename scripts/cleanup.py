"""データ保持自動クリーンアップ (F-32)

90日超のレコードを query_logs / conversations / feedback から削除。
cron で日次実行: python -m scripts.cleanup
"""

import logging
import os
import sys

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

TABLES = ["query_logs", "conversations", "feedback"]


def cleanup():
    if not DATABASE_URL:
        log.error("DATABASE_URL is not set")
        sys.exit(1)

    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        for table in TABLES:
            cur.execute(
                f"DELETE FROM {table} WHERE created_at < now() - interval '%s days'",
                (RETENTION_DAYS,),
            )
            deleted = cur.rowcount
            if deleted > 0:
                log.info("Deleted %d rows from %s (older than %d days)", deleted, table, RETENTION_DAYS)
        conn.commit()
        cur.close()
        log.info("Cleanup completed")
    finally:
        conn.close()


if __name__ == "__main__":
    cleanup()
