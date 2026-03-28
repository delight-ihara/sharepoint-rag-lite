"""SharePoint → テキスト抽出 → pgvector インジェストパイプライン

既存 sp_to_blob.py の ACL ロジックを流用。
Blob Storage を経由せず、直接 pgvector に書き込む。
"""

import hashlib
import io
import logging
import sys
import time
from urllib.parse import quote

import fitz  # PyMuPDF
import requests
from docx import Document as DocxDocument
from openai import APIConnectionError, APIStatusError, AzureOpenAI, RateLimitError
from openpyxl import load_workbook
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import (
    AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_KEY,
    EMBEDDING_DEPLOYMENT,
    GRAPH_BASE,
    GRAPH_CLIENT_ID,
    GRAPH_CLIENT_SECRET,
    GRAPH_TENANT_ID,
    SP_DRIVE_ID,
    SP_SITE_ID,
    TARGET_FOLDERS,
)
from .db import get_conn, put_conn

log = logging.getLogger(__name__)


def _is_transient_openai_error(exc: BaseException) -> bool:
    """リトライ対象の OpenAI 一時エラーか判定"""
    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code in (502, 503, 504):
        return True
    return False


def _is_transient_http_error(exc: BaseException) -> bool:
    """リトライ対象の HTTP 一時エラーか判定"""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        if exc.response.status_code in (429, 502, 503, 504):
            return True
    return False


# ── Graph API 認証 ──────────────────────────────────────

@retry(
    retry=retry_if_exception(_is_transient_http_error),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def get_graph_token() -> str:
    """クライアントシークレットで Graph API トークンを取得"""
    url = f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}/oauth2/v2.0/token"
    resp = requests.post(url, data={
        "client_id": GRAPH_CLIENT_ID,
        "client_secret": GRAPH_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


# ── SP 権限取得（既存 sp_to_blob.py から流用） ──────────────

_perm_cache: dict[str, list[str]] = {}


def get_folder_permissions(token: str, folder_path: str) -> list[str]:
    """
    フォルダの権限を取得し、閲覧可能なユーザーの UPN リストを返す。
    継承権限（明示的権限なし）の場合は ["*"] を返す。
    """
    top_folder = folder_path.split("/")[0] if folder_path else ""
    if not top_folder:
        return ["*"]

    if top_folder in _perm_cache:
        return _perm_cache[top_folder]

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE}/drives/{SP_DRIVE_ID}/root:/{quote(top_folder)}:/permissions"

    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 404:
        log.warning("権限取得失敗 (404): %s — 継承権限と判断", top_folder)
        _perm_cache[top_folder] = ["*"]
        return ["*"]
    resp.raise_for_status()

    allowed_users: list[str] = []
    for perm in resp.json().get("value", []):
        granted = perm.get("grantedToV2") or perm.get("grantedTo") or {}
        user = granted.get("user") or granted.get("siteUser") or {}
        if user.get("email"):
            allowed_users.append(user["email"].lower())
        elif user.get("loginName"):
            allowed_users.append(user["loginName"].lower())

        group = granted.get("group") or granted.get("siteGroup") or {}
        if group.get("email"):
            allowed_users.append(group["email"].lower())

        for identity in perm.get("grantedToIdentitiesV2", perm.get("grantedToIdentities", [])):
            u = identity.get("user", {})
            if u.get("email"):
                allowed_users.append(u["email"].lower())

    if not allowed_users:
        log.info("  フォルダ '%s' の明示的権限なし → 継承（全員アクセス可）", top_folder)
        _perm_cache[top_folder] = ["*"]
        return ["*"]

    result = list(set(allowed_users))
    _perm_cache[top_folder] = result
    return result


# ── SP ファイル取得 ─────────────────────────────────────

def list_sp_files(token: str) -> list[dict]:
    """SharePoint ドキュメントライブラリの全ファイルを再帰取得"""
    headers = {"Authorization": f"Bearer {token}"}
    files = []

    def walk(path: str = ""):
        if path:
            url = f"{GRAPH_BASE}/drives/{SP_DRIVE_ID}/root:/{quote(path)}:/children"
        else:
            url = f"{GRAPH_BASE}/drives/{SP_DRIVE_ID}/root/children"

        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()

        for item in resp.json().get("value", []):
            if item.get("folder"):
                child_path = f"{path}/{item['name']}" if path else item["name"]
                # TARGET_FOLDERS が指定されている場合、ルートレベルでフィルタ
                if not path and TARGET_FOLDERS:
                    if not any(child_path.startswith(prefix) for prefix in TARGET_FOLDERS):
                        continue
                walk(child_path)
            elif item.get("file"):
                files.append({
                    "id": item["id"],
                    "name": item["name"],
                    "path": path,
                    "size": item.get("size", 0),
                    "mimeType": item["file"].get("mimeType", ""),
                    "lastModified": item.get("lastModifiedDateTime", ""),
                    "webUrl": item.get("webUrl", ""),
                    "@microsoft.graph.downloadUrl": item.get("@microsoft.graph.downloadUrl", ""),
                })

    walk()
    return files


@retry(
    retry=retry_if_exception(_is_transient_http_error),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def download_file(token: str, file_id: str) -> bytes:
    """ファイルのバイナリをダウンロード（リトライ付き）"""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE}/drives/{SP_DRIVE_ID}/items/{file_id}/content"
    resp = requests.get(url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.content


# ── テキスト抽出 ───────────────────────────────────────

def extract_text(content: bytes, filename: str) -> str:
    """ファイルからテキストを抽出"""
    # ~$ で始まるファイルはOfficeの一時ファイル → スキップ
    if filename.startswith("~$"):
        log.info("  一時ファイルをスキップ: %s", filename)
        return ""

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    try:
        if ext == "pdf":
            return _extract_pdf(content)
        elif ext in ("docx",):
            return _extract_docx(content)
        elif ext in ("xlsx",):
            return _extract_xlsx(content)
        elif ext in ("pptx",):
            return _extract_pptx(content)
        elif ext in ("txt", "csv", "md"):
            return content.decode("utf-8", errors="replace")
        elif ext == "doc":
            log.warning("  .doc 形式は未対応: %s", filename)
            return ""
        else:
            log.warning("  未対応形式: %s", filename)
            return ""
    except Exception as e:
        log.warning("  テキスト抽出エラー: %s — %s", filename, e)
        return ""


def _extract_pdf(content: bytes) -> str:
    doc = fitz.open(stream=content, filetype="pdf")
    texts = [page.get_text() for page in doc]
    doc.close()
    return "\n".join(texts)


def _extract_docx(content: bytes) -> str:
    doc = DocxDocument(io.BytesIO(content))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_xlsx(content: bytes) -> str:
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    texts = []
    for ws in wb.worksheets:
        texts.append(f"=== {ws.title} ===")
        for row in ws.iter_rows(values_only=True):
            vals = [str(c) if c is not None else "" for c in row]
            if any(vals):
                texts.append("\t".join(vals))
    wb.close()
    return "\n".join(texts)


def _extract_pptx(content: bytes) -> str:
    from pptx import Presentation
    prs = Presentation(io.BytesIO(content))
    texts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
    return "\n".join(texts)


# ── チャンキング ───────────────────────────────────────

def chunk_text(text: str, chunk_size: int = 1024, overlap: int = 200) -> list[str]:
    """固定長チャンク分割（フォールバック用）"""
    char_size = chunk_size * 2
    char_overlap = overlap * 2

    if len(text) <= char_size:
        return [text] if text.strip() else []

    chunks = []
    start = 0
    while start < len(text):
        end = start + char_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start += char_size - char_overlap

    return chunks


import math
import re


def _split_sentences(text: str) -> list[str]:
    """テキストを文単位で分割（日本語・英語対応）"""
    # 段落（空行）→ 文末句読点で分割
    parts = re.split(r'(?<=[。！？\.\!\?\n])\s*', text)
    sentences = [s.strip() for s in parts if s.strip()]
    return sentences if sentences else [text.strip()] if text.strip() else []


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """コサイン類似度（pure Python）"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def semantic_chunk_text(
    text: str,
    max_chars: int = 2048,
    min_chars: int = 100,
    breakpoint_percentile: int = 25,
) -> list[str]:
    """セマンティックチャンキング: 文の埋め込み類似度で意味境界を検出して分割

    1. 文に分割
    2. 各文を埋め込み
    3. 隣接文の類似度が低い箇所（話題の変わり目）で分割
    4. max_chars / min_chars で調整
    """
    if not text.strip():
        return []
    if len(text) <= max_chars:
        return [text.strip()]

    sentences = _split_sentences(text)
    if len(sentences) <= 1:
        return [text.strip()]

    # 文のエンベディングを取得（バッチ）
    try:
        embeddings = get_embeddings(sentences)
    except Exception:
        log.warning("セマンティックチャンキング失敗 → 固定長フォールバック")
        return chunk_text(text)

    # 隣接文間のコサイン類似度
    similarities = []
    for i in range(len(embeddings) - 1):
        similarities.append(_cosine_sim(embeddings[i], embeddings[i + 1]))

    # ブレークポイント閾値（パーセンタイル）
    sorted_sims = sorted(similarities)
    idx = max(0, int(len(sorted_sims) * breakpoint_percentile / 100) - 1)
    threshold = sorted_sims[idx] if sorted_sims else 0.5

    # 意味境界で分割
    chunks = []
    current = sentences[0]

    for i in range(1, len(sentences)):
        candidate = current + "\n" + sentences[i]

        # 類似度が閾値未満 = 話題が変わった → 分割
        if similarities[i - 1] < threshold and len(current) >= min_chars:
            chunks.append(current.strip())
            current = sentences[i]
        # max_chars 超過 → 強制分割
        elif len(candidate) > max_chars and len(current) >= min_chars:
            chunks.append(current.strip())
            current = sentences[i]
        else:
            current = candidate

    if current.strip():
        chunks.append(current.strip())

    return [c for c in chunks if c]


# ── エンベディング ─────────────────────────────────────

@retry(
    retry=retry_if_exception(_is_transient_openai_error),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def _embed_batch(client: AzureOpenAI, batch: list[str]) -> list[list[float]]:
    """エンベディングバッチ呼び出し（リトライ付き）"""
    resp = client.embeddings.create(input=batch, model=EMBEDDING_DEPLOYMENT)
    return [d.embedding for d in resp.data]


def get_embeddings(texts: list[str]) -> list[list[float]]:
    """Azure OpenAI でエンベディングを生成"""
    client = AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_key=AZURE_OPENAI_KEY,
        api_version="2024-06-01",
    )
    # バッチサイズ制限（Azure OpenAI は 16 件/リクエスト推奨）
    all_embeddings = []
    batch_size = 16
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        all_embeddings.extend(_embed_batch(client, batch))
        if i + batch_size < len(texts):
            time.sleep(0.5)  # レート制限対策
    return all_embeddings


# ── DB 書き込み ────────────────────────────────────────

def upsert_chunks(
    file_id: str,
    title: str,
    source_url: str,
    category: str,
    allowed_groups: list[str],
    chunks: list[str],
    embeddings: list[list[float]],
):
    """チャンクを pgvector に upsert — トランザクション保証 (F-25)

    クラッシュ時は rollback → 旧チャンクが残る（データ消失なし）。
    """
    conn = get_conn()
    try:
        conn.autocommit = False
        cur = conn.cursor()
        try:
            # 既存チャンクを削除
            cur.execute("DELETE FROM chunks WHERE file_id = %s", (file_id,))

            for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                chunk_id = hashlib.sha256(f"{file_id}:{i}".encode()).hexdigest()[:16]
                cur.execute(
                    """INSERT INTO chunks
                       (chunk_id, file_id, chunk_text, embedding, title, source_url, category, allowed_groups)
                       VALUES (%s, %s, %s, %s::vector, %s, %s, %s, %s)""",
                    (chunk_id, file_id, chunk, emb, title, source_url, category, allowed_groups),
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        conn.autocommit = True
        put_conn(conn)


# ── DB からの既存ファイル情報取得 ─────────────────────────

def _get_indexed_files() -> dict[str, str]:
    """DB にインジェスト済みのファイル ID と updated_at を取得"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT file_id, updated_at FROM chunks")
        result = {row[0]: row[1].isoformat() if row[1] else "" for row in cur.fetchall()}
        cur.close()
        return result
    finally:
        put_conn(conn)


def _delete_file_chunks(file_id: str):
    """ファイルのチャンクを全削除（SP から消えたファイル用）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM chunks WHERE file_id = %s", (file_id,))
        deleted = cur.rowcount
        conn.commit()
        cur.close()
        return deleted
    finally:
        put_conn(conn)


def _update_acl_only(file_id: str, allowed_groups: list[str]):
    """ACL だけ更新（ファイル内容は変わっていないが権限が変わった場合）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE chunks SET allowed_groups = %s, updated_at = now() WHERE file_id = %s",
            (allowed_groups, file_id),
        )
        conn.commit()
        cur.close()
    finally:
        put_conn(conn)


# ── メインパイプライン ─────────────────────────────────

_INGEST_LOCK_ID = 839201764  # アドバイザリーロック用の固有 ID


def run(incremental: bool = False):
    """インジェストパイプライン実行

    incremental=False: 全件再処理
    incremental=True:  差分更新（変更・追加・削除を検知）

    排他制御 (F-26): PostgreSQL アドバイザリーロックで二重実行を防止。
    """
    # ── 排他制御: アドバイザリーロック取得 ──
    lock_conn = get_conn()
    try:
        lock_cur = lock_conn.cursor()
        lock_cur.execute("SELECT pg_try_advisory_lock(%s)", (_INGEST_LOCK_ID,))
        acquired = lock_cur.fetchone()[0]
        lock_cur.close()
        if not acquired:
            log.warning("Another ingest process is running. Skipping.")
            put_conn(lock_conn)
            return
    except Exception:
        put_conn(lock_conn)
        raise

    try:
        _run_ingest(lock_conn, incremental)
    finally:
        # ロック解放
        try:
            unlock_cur = lock_conn.cursor()
            unlock_cur.execute("SELECT pg_advisory_unlock(%s)", (_INGEST_LOCK_ID,))
            unlock_cur.close()
        except Exception:
            log.exception("Failed to release advisory lock")
        put_conn(lock_conn)


def _run_ingest(lock_conn, incremental: bool):
    """インジェスト本体（ロック取得後に実行）"""
    log.info("=== インジェスト開始 (incremental=%s) ===", incremental)
    token = get_graph_token()
    files = list_sp_files(token)
    log.info("SP ファイル数: %d", len(files))

    sp_file_ids = {f["id"] for f in files}

    # 差分検知用: DB の既存ファイル
    indexed = _get_indexed_files() if incremental else {}

    processed = 0
    skipped = 0
    acl_updated = 0
    deleted = 0

    for f in files:
        title = f["name"]
        path = f["path"]
        category = path.split("/")[0] if path else "root"
        source_url = f["webUrl"]
        file_id = f["id"]
        last_modified = f["lastModified"]

        # 差分チェック: incremental モードでは変更がないファイルをスキップ
        if incremental and file_id in indexed:
            # ACL だけ更新チェック
            current_acl = get_folder_permissions(token, path)
            _update_acl_only(file_id, current_acl)
            acl_updated += 1

            # ファイル内容が変わっていなければスキップ
            # lastModifiedDateTime と DB の updated_at を比較
            if last_modified <= indexed[file_id]:
                skipped += 1
                continue

        log.info("処理中: %s/%s", path, title)

        # テキスト抽出
        content = download_file(token, file_id)
        text = extract_text(content, title)
        if not text.strip():
            log.warning("  テキスト抽出結果が空: %s", title)
            skipped += 1
            continue

        # ACL 取得
        allowed_groups = get_folder_permissions(token, path)
        log.info("  ACL: %s", allowed_groups)

        # セマンティックチャンキング
        chunks = semantic_chunk_text(text)
        log.info("  チャンク数: %d (semantic)", len(chunks))

        if not chunks:
            skipped += 1
            continue

        # エンベディング
        embeddings = get_embeddings(chunks)

        # DB 書き込み
        upsert_chunks(file_id, title, source_url, category, allowed_groups, chunks, embeddings)
        processed += 1

    # SP から削除されたファイルを DB からも削除
    if incremental and indexed:
        for old_id in indexed:
            if old_id not in sp_file_ids:
                count = _delete_file_chunks(old_id)
                log.info("削除: file_id=%s (%d chunks)", old_id, count)
                deleted += count

    log.info(
        "=== インジェスト完了: %d 処理, %d スキップ, %d ACL更新, %d チャンク削除 ===",
        processed, skipped, acl_updated, deleted,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    incremental = "--incremental" in sys.argv
    run(incremental=incremental)
