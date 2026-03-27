"""環境変数・設定"""

import os

# PostgreSQL (pgvector)
DATABASE_URL = os.environ["DATABASE_URL"]

# Azure OpenAI
AZURE_OPENAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
AZURE_OPENAI_KEY = os.environ["AZURE_OPENAI_KEY"]
EMBEDDING_DEPLOYMENT = os.environ.get("EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
CHAT_DEPLOYMENT = os.environ.get("CHAT_DEPLOYMENT", "gpt-4o-mini")

# Graph API (SharePoint)
GRAPH_TENANT_ID = os.environ["GRAPH_TENANT_ID"]
GRAPH_CLIENT_ID = os.environ["GRAPH_CLIENT_ID"]
GRAPH_CLIENT_SECRET = os.environ["GRAPH_CLIENT_SECRET"]
SP_SITE_ID = os.environ["SP_SITE_ID"]
SP_DRIVE_ID = os.environ["SP_DRIVE_ID"]

# Search settings
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "7"))
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.3"))
ACL_ENABLED = os.environ.get("ACL_ENABLED", "true").lower() == "true"

# Target folders (empty = all, CSV = filter by prefix)
TARGET_FOLDERS_CSV = os.environ.get("SP_TARGET_FOLDERS", "")
TARGET_FOLDERS = [f.strip() for f in TARGET_FOLDERS_CSV.split(",") if f.strip()] if TARGET_FOLDERS_CSV else []

# Graph API base
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
