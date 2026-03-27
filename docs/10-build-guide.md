# 構築ガイド（SharePoint RAG Lite）

## 変更履歴

| 版数 | 日付 | 変更内容 |
|------|------|----------|
| 0.1 | 2026-03-26 | 初版作成 |

---

## 1. 前提条件

| 項目 | 状態 |
|------|------|
| Azure サブスクリプション | 既存（sharepoint-rag-azure と同一） |
| Entra ID アプリ登録 | 既存 `app-sprag-poc` を流用 |
| Azure OpenAI | 既存 `oai-sprag-poc-eastus2` を流用 |
| SharePoint ��ォルダ権限 | 設定済み（01_経営 / 02_人事労務 / 03_営業） |
| Python 3.12+ | ローカル開発環境 |
| Docker | Container Apps デプロイ用 |

---

## 2. 構築順序

```
Step 1: Supabase セットアップ（DB）
Step 2: Azure リソース作成（RG + KV + Container Apps）
Step 3: アプリケーションコード（ingest.py + API + UI）
Step 4: インジェスション実行
Step 5: 動作確認
```

---

## 3. Step 1: Supabase セットアップ

### 3.1 プロジェクト作成

1. https://supabase.com でアカウント作成（GitHub 連携）
2. New Project → 名前: `spraglite-poc` / リージョン: Northeast Asia (Tokyo) / パスワード設定
3. Project Settings → Database → Connection string (URI) を控える

### 3.2 pgvector 有効化 + テーブル作成

SQL Editor で実行:

```sql
-- pgvector 拡張を有効化
CREATE EXTENSION IF NOT EXISTS vector;

-- チャンクテーブル（ベクトル検索 + ACL）
CREATE TABLE chunks (
    chunk_id    TEXT PRIMARY KEY,
    file_id     TEXT NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   vector(1536) NOT NULL,
    title       TEXT,
    source_url  TEXT,
    category    TEXT,
    allowed_groups TEXT[] NOT NULL DEFAULT '{"*"}',
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_chunks_embedding ON chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);
CREATE INDEX idx_chunks_allowed_groups ON chunks USING gin (allowed_groups);

-- 会話履歴テーブル
CREATE TABLE conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  TEXT NOT NULL,
    user_email  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    citations   JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_conversations_session ON conversations (session_id, created_at);

-- クエリログテーブル
CREATE TABLE query_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_email  TEXT NOT NULL,
    query       TEXT NOT NULL,
    chunks_used INT,
    response_time_ms INT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_query_logs_user ON query_logs (user_email, created_at);
```

### 3.3 確認

```sql
SELECT * FROM pg_extension WHERE extname = 'vector';
-- 1行返ればOK

SELECT count(*) FROM chunks;
-- 0 が返ればOK
```

---

## 4. Step 2: Azure リソース作成

### 4.1 Resource Group

```bash
az group create --name rg-spraglite-poc-jpe --location japaneast \
  --tags Environment=poc Project=sp-rag-lite
```

### 4.2 Key Vault + シークレット

```bash
az keyvault create --name kv-spraglite-poc-jpe \
  --resource-group rg-spraglite-poc-jpe \
  --location japaneast \
  --enable-rbac-authorization true

# シークレット登録（3件）
az keyvault secret set --vault-name kv-spraglite-poc-jpe \
  --name AZURE-OPENAI-KEY --value "<oai-sprag-poc-eastus2 の API キー>"

az keyvault secret set --vault-name kv-spraglite-poc-jpe \
  --name GRAPH-CLIENT-SECRET --value "<app-sprag-poc のシークレット>"

az keyvault secret set --vault-name kv-spraglite-poc-jpe \
  --name DATABASE-URL --value "<Supabase 接続文字列>"
```

### 4.3 Azure OpenAI に embedding-small を追加

既存の `oai-sprag-poc-eastus2` に text-embedding-3-small モデルをデプロイ:

```bash
az cognitiveservices account deployment create \
  --resource-group <既存RG> \
  --name oai-sprag-poc-eastus2 \
  --deployment-name text-embedding-3-small \
  --model-name text-embedding-3-small \
  --model-version "1" \
  --model-format OpenAI \
  --sku-capacity 120 \
  --sku-name Standard
```

### 4.4 Container Apps Environment + App

```bash
# Environment
az containerapp env create \
  --name cae-spraglite-poc-jpe \
  --resource-group rg-spraglite-poc-jpe \
  --location japaneast

# Container App（初期デプロイ。コードは Step 3 で作成後に更新）
az containerapp create \
  --name ca-spraglite-poc-jpe \
  --resource-group rg-spraglite-poc-jpe \
  --environment cae-spraglite-poc-jpe \
  --image mcr.microsoft.com/azuredocs/containerapps-helloworld:latest \
  --target-port 8000 \
  --ingress external \
  --min-replicas 0 \
  --max-replicas 1
```

### 4.5 RBAC 設定

```bash
# Container App のマネージ��� ID を有効化
az containerapp identity assign \
  --name ca-spraglite-poc-jpe \
  --resource-group rg-spraglite-poc-jpe \
  --system-assigned

# Key Vault Secrets User
CA_PRINCIPAL=$(az containerapp identity show \
  --name ca-spraglite-poc-jpe \
  --resource-group rg-spraglite-poc-jpe \
  --query principalId -o tsv)

KV_ID=$(az keyvault show --name kv-spraglite-poc-jpe --query id -o tsv)

az role assignment create --assignee $CA_PRINCIPAL \
  --role "Key Vault Secrets User" --scope $KV_ID

# Azure OpenAI User
OAI_ID=$(az cognitiveservices account show \
  --resource-group <既存RG> --name oai-sprag-poc-eastus2 --query id -o tsv)

az role assignment create --assignee $CA_PRINCIPAL \
  --role "Cognitive Services OpenAI User" --scope $OAI_ID
```

### 4.6 Entra ID SSO（EasyAuth）

```bash
az containerapp auth microsoft update \
  --name ca-spraglite-poc-jpe \
  --resource-group rg-spraglite-poc-jpe \
  --client-id <app-sprag-poc のクライアント ID> \
  --client-secret-name GRAPH-CLIENT-SECRET \
  --issuer https://login.microsoftonline.com/<テナントID>/v2.0 \
  --yes
```

---

## 5. Step 3: アプリケーションコード

### 5.1 プロジェクト構造

```
sharepoint-rag-lite/
├── docs/                    # 設計書（本ファイル群）
├── src/
│   ├── ingest.py            # SP → テキスト抽出 → pgvector
│   ├── search.py            # ベクトル検索 + ACL フィルタ
│   ├── llm.py               # 回答生成（既存 llm.py 流用）
│   ├── api.py               # FastAPI エントリポイント
│   └── config.py            # 環境変数・設定
├── webapp/                  # チャット UI（既存 webapp 流用）
├── Dockerfile
├── requirements.txt
└── README.md
```

### 5.2 主要ファイルの実装方針

| ファイル | ベース | 変更点 |
|---------|--------|--------|
| `ingest.py` | 既存 `sp_to_blob.py` + `update_index_metadata.py` | Blob 書き込み → pgvector INSERT。テキスト抽出を Python ライブラリに置換 |
| `search.py` | 既存 `search.py` | AI Search SDK → psycopg2 + pgvector SQL |
| `llm.py` | 既存 `llm.py` | ほぼそのまま。citation 抑制ロジック流用 |
| `api.py` | 既存 `server.js` + `orchestrator.py` | Node.js → Python FastAPI に統合。2層 → 1層 |
| `webapp/` | 既存 `webapp/` | API エンドポイント URL の変更のみ |

---

## 6. Step 4: インジェスション

```bash
# 環境変数を設定
export DATABASE_URL="<Supabase 接続文字列>"
export AZURE_OPENAI_ENDPOINT="<oai-sprag-poc-eastus2 エンドポイント>"
export AZURE_OPENAI_KEY="<API キー>"
export GRAPH_CLIENT_ID="<app-sprag-poc クライアント ID>"
export GRAPH_CLIENT_SECRET="<シークレット>"
export GRAPH_TENANT_ID="<テナント ID>"
export SP_SITE_ID="<SharePoint サイト ID>"
export SP_DRIVE_ID="<ドキュメントライブラリ ID>"

# 全件インジェスト
python src/ingest.py

# 確認
python -c "
import psycopg2
conn = psycopg2.connect('$DATABASE_URL')
cur = conn.cursor()
cur.execute('SELECT count(*) FROM chunks')
print(f'Chunks: {cur.fetchone()[0]}')
cur.execute('SELECT DISTINCT title FROM chunks')
print(f'Files: {cur.rowcount}')
"
```

---

## 7. Step 5: 動作確認

### 7.1 ローカル動作確認

```bash
# API 起動
uvicorn src.api:app --host 0.0.0.0 --port 8000

# テストクエリ（ACL なし）
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "情報セキュリティの基本方針は？", "user_email": "test@example.com"}'
```

### 7.2 ACL 動作確認（簡易）

```bash
# 全員閲覧可のクエリ → チャンクが返る
curl -X POST http://localhost:8000/chat \
  -d '{"message": "就業規則は？", "user_email": "anyone@example.com"}'

# 経営文書のクエリ（権限なしユーザー） → 「該当なし」が返る
curl -X POST http://localhost:8000/chat \
  -d '{"message": "事業計画の概要は？", "user_email": "anyone@example.com"}'
```

### 7.3 Container Apps デプロイ

```bash
# Docker ビルド + ACR push + Container App 更新
az containerapp update \
  --name ca-spraglite-poc-jpe \
  --resource-group rg-spraglite-poc-jpe \
  --image <ACR or Docker Hub イメージ>
```

---

## 8. PoC 終了後の削除

```bash
# Azure リソース
az group delete --name rg-spraglite-poc-jpe --yes

# Supabase
# ダッシュボードから Project Settings → Delete Project
# または7日間アクセスなしで自動停止
```

---

以上
