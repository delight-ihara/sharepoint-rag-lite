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
from openpyxl import load_workbook
from openai import AzureOpenAI

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

# ── Graph API 認証 ──────────────────────────────────────

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


def download_file(token: str, file_id: str) -> bytes:
    """ファイルのバイナリをダウンロード"""
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
    """トークン近似のチャンク分割（文字数ベース、1トークン≒2文字で概算）"""
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


# ── エンベディング ─────────────────────────────────────

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
        resp = client.embeddings.create(input=batch, model=EMBEDDING_DEPLOYMENT)
        all_embeddings.extend([d.embedding for d in resp.data])
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
    """チャンクを pgvector に upsert（DELETE + INSERT）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
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
        cur.close()
    finally:
        put_conn(conn)


# ── メインパイプライン ─────────────────────────────────

def run(incremental: bool = False):
    """インジェストパイプライン実行"""
    log.info("=== インジェスト開始 ===")
    token = get_graph_token()
    files = list_sp_files(token)
    log.info("SP ファイル数: %d", len(files))

    processed = 0
    skipped = 0

    for f in files:
        title = f["name"]
        path = f["path"]
        category = path.split("/")[0] if path else "root"
        source_url = f["webUrl"]
        file_id = f["id"]

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

        # チャンキング
        chunks = chunk_text(text)
        log.info("  チャンク数: %d", len(chunks))

        if not chunks:
            skipped += 1
            continue

        # エンベディング
        embeddings = get_embeddings(chunks)

        # DB 書き込み
        upsert_chunks(file_id, title, source_url, category, allowed_groups, chunks, embeddings)
        processed += 1

    log.info("=== インジェスト完了: %d 処理, %d スキップ ===", processed, skipped)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    incremental = "--incremental" in sys.argv
    run(incremental=incremental)
