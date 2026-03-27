"""テスト共通フィクスチャ"""

import os

# テスト用ダミー環境変数（config.py が import 時に読むため、先にセット）
os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://dummy.openai.azure.com")
os.environ.setdefault("AZURE_OPENAI_KEY", "dummy-key")
os.environ.setdefault("GRAPH_TENANT_ID", "dummy")
os.environ.setdefault("GRAPH_CLIENT_ID", "dummy")
os.environ.setdefault("GRAPH_CLIENT_SECRET", "dummy")
os.environ.setdefault("SP_SITE_ID", "dummy")
os.environ.setdefault("SP_DRIVE_ID", "dummy")

import pytest
from fastapi.testclient import TestClient

from src.api import app


@pytest.fixture
def client():
    return TestClient(app)
