"""環境変数・設定

Phase 0.9: Key Vault 統合（F-12）— KEY_VAULT_NAME が設定されていれば Key Vault から取得
"""

import logging
import os

log = logging.getLogger(__name__)

# ── Key Vault 統合 (F-12) ──

_kv_cache: dict[str, str] = {}


def _get_from_keyvault(secret_name: str) -> str | None:
    """Key Vault からシークレットを取得（フォールバック: 環境変数）"""
    kv_name = os.environ.get("KEY_VAULT_NAME", "")
    if not kv_name:
        return None

    if secret_name in _kv_cache:
        return _kv_cache[secret_name]

    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        client = SecretClient(
            vault_url=f"https://{kv_name}.vault.azure.net",
            credential=DefaultAzureCredential(),
        )
        value = client.get_secret(secret_name).value
        _kv_cache[secret_name] = value
        return value
    except Exception:
        log.warning("Key Vault から %s を取得できません。環境変数にフォールバック", secret_name)
        return None


def _get_secret(env_var: str, kv_name: str = "") -> str:
    """シークレットを取得: Key Vault → 環境変数の優先順"""
    if kv_name:
        kv_value = _get_from_keyvault(kv_name)
        if kv_value:
            return kv_value
    return os.environ.get(env_var, "")


# PostgreSQL (pgvector)
DATABASE_URL = _get_secret("DATABASE_URL", "DATABASE-URL") or os.environ["DATABASE_URL"]

# Azure OpenAI
AZURE_OPENAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
AZURE_OPENAI_KEY = _get_secret("AZURE_OPENAI_KEY", "AZURE-OPENAI-KEY") or os.environ["AZURE_OPENAI_KEY"]
EMBEDDING_DEPLOYMENT = os.environ.get("EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
CHAT_DEPLOYMENT = os.environ.get("CHAT_DEPLOYMENT", "gpt-4o-mini")

# Graph API (SharePoint)
GRAPH_TENANT_ID = os.environ["GRAPH_TENANT_ID"]
GRAPH_CLIENT_ID = os.environ["GRAPH_CLIENT_ID"]
GRAPH_CLIENT_SECRET = _get_secret("GRAPH_CLIENT_SECRET", "GRAPH-CLIENT-SECRET") or os.environ["GRAPH_CLIENT_SECRET"]
SP_SITE_ID = os.environ["SP_SITE_ID"]
SP_DRIVE_ID = os.environ["SP_DRIVE_ID"]

# Search settings
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "7"))
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.3"))
ACL_ENABLED = os.environ.get("ACL_ENABLED", "true").lower() == "true"

# Target folders (empty = all, CSV = filter by prefix)
TARGET_FOLDERS_CSV = os.environ.get("SP_TARGET_FOLDERS", "")
TARGET_FOLDERS = [f.strip() for f in TARGET_FOLDERS_CSV.split(",") if f.strip()] if TARGET_FOLDERS_CSV else []

# CORS
_ALLOWED_ORIGINS_CSV = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = (
    [o.strip() for o in _ALLOWED_ORIGINS_CSV.split(",") if o.strip()]
    if _ALLOWED_ORIGINS_CSV
    else ["*"]  # 未設定時は全許可（EasyAuth が前段で認証するため）
)

# Graph API base
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# ACL configuration
ACL_GROUP_CACHE_TTL = int(os.environ.get("ACL_GROUP_CACHE_TTL", "300"))
REJECT_ANONYMOUS_LINKS = os.environ.get("REJECT_ANONYMOUS_LINKS", "true").lower() == "true"
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "100"))

# SP サイトメンバーのフォールバック（Sites.Read.All がない場合に使用）
_SP_SITE_MEMBERS_CSV = os.environ.get("SP_SITE_MEMBERS", "")
SP_SITE_MEMBERS_FALLBACK = [m.strip().lower() for m in _SP_SITE_MEMBERS_CSV.split(",") if m.strip()] if _SP_SITE_MEMBERS_CSV else []

# Application Insights (NF-M01)
APPLICATIONINSIGHTS_CONNECTION_STRING = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
