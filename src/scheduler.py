"""Delta sync スケジューラ — SP ファイル変更を定期ポーリングしてインジェスト

Usage:
    python -m src.scheduler                  # 5分間隔（デフォルト）
    python -m src.scheduler --interval 300   # 秒指定
    python -m src.scheduler --once           # 1回だけ実行

delta query で前回からの変更ファイルのみを検出し、incremental インジェストを実行する。
初回はフルスキャンと同等（deltaLink がないため全件返る）。
"""

import logging
import os
import sys
import time

import requests

from .acl import get_app_token, resolve_folder_acl, clear_caches
from .config import GRAPH_BASE, SP_DRIVE_ID, TARGET_FOLDERS
from .ingest import run

log = logging.getLogger(__name__)

DELTA_LINK_FILE = os.path.join(os.path.dirname(__file__), ".delta_link")


def _load_delta_link() -> str | None:
    if os.path.exists(DELTA_LINK_FILE):
        with open(DELTA_LINK_FILE) as f:
            return f.read().strip() or None
    return None


def _save_delta_link(link: str):
    with open(DELTA_LINK_FILE, "w") as f:
        f.write(link)


def check_for_changes() -> bool:
    """Delta query で変更があるか確認。変更があれば True。"""
    token = get_app_token()
    headers = {"Authorization": f"Bearer {token}"}

    delta_link = _load_delta_link()

    if delta_link:
        url = delta_link
    else:
        url = f"{GRAPH_BASE}/drives/{SP_DRIVE_ID}/root/delta?$select=id,name,lastModifiedDateTime,parentReference,file"

    changed_files = []
    new_delta_link = None

    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        if not resp.ok:
            log.warning("Delta query 失敗: %d — フルスキャンにフォールバック", resp.status_code)
            # deltaLink が無効になった場合はリセット
            if os.path.exists(DELTA_LINK_FILE):
                os.unlink(DELTA_LINK_FILE)
            return True  # 変更ありとして扱い、incremental を実行

        data = resp.json()
        for item in data.get("value", []):
            # フォルダは無視、ファイルのみ
            if "file" not in item and "deleted" not in item:
                continue

            # TARGET_FOLDERS フィルタ
            parent = item.get("parentReference", {}).get("path", "")
            if TARGET_FOLDERS:
                name = item.get("name", "")
                path_parts = parent.split("/root:/")[-1] if "/root:/" in parent else ""
                full_path = f"{path_parts}/{name}" if path_parts else name
                if not any(full_path.startswith(prefix) for prefix in TARGET_FOLDERS):
                    continue

            changed_files.append(item.get("name", "unknown"))

        url = data.get("@odata.nextLink")
        if "@odata.deltaLink" in data:
            new_delta_link = data["@odata.deltaLink"]

    if new_delta_link:
        _save_delta_link(new_delta_link)

    if changed_files:
        log.info("Delta 検出: %d ファイル変更 — %s", len(changed_files), changed_files[:5])
        return True
    else:
        log.debug("Delta 検出: 変更なし")
        return False


def _refresh_acl_only():
    """ファイル変更なしでも ACL だけ更新する（権限変更検知用）"""
    import psycopg2
    from .config import DATABASE_URL
    from .db import get_conn, put_conn

    token = get_app_token()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT category FROM chunks")
        categories = [row[0] for row in cur.fetchall()]
        cur.close()

        updated = 0
        for cat in categories:
            acl = resolve_folder_acl(token, cat)
            cur = conn.cursor()
            cur.execute(
                "UPDATE chunks SET allowed_groups = %s WHERE category = %s AND allowed_groups != %s",
                (acl, cat, acl),
            )
            if cur.rowcount > 0:
                log.info("ACL 更新: %s → %d チャンク", cat, cur.rowcount)
                updated += cur.rowcount
            cur.close()
        conn.commit()

        if updated:
            log.info("ACL リフレッシュ完了: %d チャンク更新", updated)
        else:
            log.debug("ACL 変更なし")
    finally:
        put_conn(conn)


def run_delta_sync():
    """Delta で変更を検出し、あれば incremental インジェストを実行。
    ファイル変更がなくても ACL は毎回リフレッシュする。"""
    clear_caches()  # ACL キャッシュをクリアして最新権限を取得

    if check_for_changes():
        log.info("変更検出 — incremental インジェスト開始")
        run(incremental=True)
    else:
        log.info("ファイル変更なし — ACL のみリフレッシュ")
        _refresh_acl_only()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="SharePoint RAG delta sync scheduler")
    parser.add_argument("--interval", type=int, default=300, help="ポーリング間隔（秒、デフォルト300=5分）")
    parser.add_argument("--once", action="store_true", help="1回だけ実行して終了")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.once:
        run_delta_sync()
        return

    log.info("Delta sync スケジューラ開始 (interval=%ds)", args.interval)
    while True:
        try:
            run_delta_sync()
        except Exception:
            log.exception("Delta sync エラー — 次回リトライ")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
