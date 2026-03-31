# 構築ガイド（SharePoint RAG Lite）

## 変更履歴

| 版数 | 日付 | 変更内容 |
|------|------|----------|
| 0.1 | 2026-03-26 | 初版作成 |
| 0.2 | 2026-03-27 | Phase 0.1: プロジェクト構造更新（tests/、.dockerignore）、Dockerfile 強化 |
| 0.3 | 2026-03-27 | Phase 0 ギャップ対応: feedback テーブル・依存パッケージ追加 |
| 0.4 | 2026-03-27 | セマンティックチャンキング(F-22)説明追加、インジェスト手順更新 |

---

## 1. 前提条件

| 項目 | 状態 |
|------|------|
| Azure サブスクリプション | 既存（sharepoint-rag-azure と同一） |
| Entra ID アプリ登録 | 既存 `<YOUR_ENTRA_APP>` を流用 |
| Azure OpenAI | 既存 `<YOUR_OPENAI_RESOURCE>` を流用 |
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
2. New Project → 名前: `<YOUR_SUPABASE_PROJECT>` / リージョン: Northeast Asia (Tokyo) / パスワード設定
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

-- クエリログテーブル（+ F-30 メータリング）
CREATE TABLE query_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_email  TEXT NOT NULL,
    query       TEXT NOT NULL,
    chunks_used INT,
    tokens_used INT,
    response_time_ms INT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_query_logs_user ON query_logs (user_email, created_at);

-- ユーザーフィードバック（F-19）
CREATE TABLE feedback (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  TEXT NOT NULL,
    message_id  UUID NOT NULL,
    user_email  TEXT NOT NULL,
    rating      SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
    comment     TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_feedback_session ON feedback (session_id);
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
az group create --name <YOUR_RESOURCE_GROUP> --location japaneast \
  --tags Environment=poc Project=sp-rag-lite
```

### 4.2 Key Vault + シークレット

```bash
az keyvault create --name <YOUR_KEY_VAULT> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --location japaneast \
  --enable-rbac-authorization true

# シークレット登録（3件）
az keyvault secret set --vault-name <YOUR_KEY_VAULT> \
  --name AZURE-OPENAI-KEY --value "<<YOUR_OPENAI_RESOURCE> の API キー>"

az keyvault secret set --vault-name <YOUR_KEY_VAULT> \
  --name GRAPH-CLIENT-SECRET --value "<<YOUR_ENTRA_APP> のシークレット>"

az keyvault secret set --vault-name <YOUR_KEY_VAULT> \
  --name DATABASE-URL --value "<Supabase 接続文字列>"
```

### 4.3 Azure OpenAI に embedding-small を追加

既存の `<YOUR_OPENAI_RESOURCE>` に text-embedding-3-small モデルをデプロイ:

```bash
az cognitiveservices account deployment create \
  --resource-group <既存RG> \
  --name <YOUR_OPENAI_RESOURCE> \
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
  --name <YOUR_CONTAINER_ENV> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --location japaneast

# Container App（初期デプロイ。コードは Step 3 で作成後に更新）
az containerapp create \
  --name <YOUR_CONTAINER_APP> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --environment <YOUR_CONTAINER_ENV> \
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
  --name <YOUR_CONTAINER_APP> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --system-assigned

# Key Vault Secrets User
CA_PRINCIPAL=$(az containerapp identity show \
  --name <YOUR_CONTAINER_APP> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --query principalId -o tsv)

KV_ID=$(az keyvault show --name <YOUR_KEY_VAULT> --query id -o tsv)

az role assignment create --assignee $CA_PRINCIPAL \
  --role "Key Vault Secrets User" --scope $KV_ID

# Azure OpenAI User
OAI_ID=$(az cognitiveservices account show \
  --resource-group <既存RG> --name <YOUR_OPENAI_RESOURCE> --query id -o tsv)

az role assignment create --assignee $CA_PRINCIPAL \
  --role "Cognitive Services OpenAI User" --scope $OAI_ID
```

### 4.6 Entra ID SSO（EasyAuth）

```bash
az containerapp auth microsoft update \
  --name <YOUR_CONTAINER_APP> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --client-id <<YOUR_ENTRA_APP> のクライアント ID> \
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
│   ├── api.py               # FastAPI エントリポイント（バリデーション・エラーハンドリング）
│   ├── config.py            # 環境変数・設定（ALLOWED_ORIGINS 含む）
│   ├── db.py                # PostgreSQL 接続プール管理
│   ├── ingest.py            # SP → テキスト抽出 → pgvector
│   ├── llm.py               # 回答生成（既存 llm.py 流用）
│   ├── search.py            # ベクトル検索 + ACL フィルタ
│   └── static/index.html    # チャット UI
├── scripts/
│   ├── evaluate.py          # RAG 評価パイプライン（F-16）
│   └── cleanup.py           # データ保持クリーンアップ（F-32）
├── tests/
│   ├── conftest.py          # テストフィクスチャ（ダミー環境変数 + TestClient）
│   └── test_api.py          # API ユニットテスト（15 ケース）
├── webapp/                  # チャット UI（既存 webapp 流用）
├── .dockerignore            # ビルドコンテキスト除外
├── Dockerfile               # non-root + HEALTHCHECK
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

### 6.1 セマンティックチャンキング

インジェスト時にセマンティックチャンキング（F-22）が自動適用される。

| パラメータ | デフォルト値 | 説明 |
|-----------|------------|------|
| `max_chars` | 2048 | チャンクの最大文字数 |
| `min_chars` | 100 | 分割判定の最小文字数 |
| `breakpoint_percentile` | 25 | 類似度の分割閾値（低い = 多く分割） |

**動作:**
1. テキストを文単位に分割（日本語句読点 `。！？` + 英語 `.!?` + 改行）
2. 各文を text-embedding-3-small で埋め込み
3. 隣接文のコサイン類似度を計算
4. 類似度がパーセンタイル閾値未満の箇所（話題の変わり目）で分割
5. `max_chars` 超過時は強制分割

**フォールバック:** 埋め込み API 失敗時は固定長チャンク（1024トークン / 200トークンオーバーラップ）に自動切替。

### 6.2 実行

```bash
# 環境変数を設定
export DATABASE_URL="<Supabase 接続文字列>"
export AZURE_OPENAI_ENDPOINT="<<YOUR_OPENAI_RESOURCE> エンドポイント>"
export AZURE_OPENAI_KEY="<API キー>"
export GRAPH_CLIENT_ID="<<YOUR_ENTRA_APP> クライアント ID>"
export GRAPH_CLIENT_SECRET="<シークレット>"
export GRAPH_TENANT_ID="<テナント ID>"
export SP_SITE_ID="<SharePoint サイト ID>"
export SP_DRIVE_ID="<ドキュメントライブラリ ID>"
# export SP_SITE_MEMBERS="user1@example.com,user2@example.com"  # Sites.Read.All が付与できない場合のフォールバック

# 全件インジェスト（セマンティックチャンキング適用）
python -m src.ingest

# 差分更新（変更ファイルのみ再処理）
python -m src.ingest --incremental

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
  --name <YOUR_CONTAINER_APP> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --image <ACR or Docker Hub イメージ>
```

---

## 8. Step 6: Application Insights セットアップ（NF-M01）

```bash
# Application Insights 作成
az monitor app-insights component create \
  --app <YOUR_APP_INSIGHTS> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --location japaneast \
  --kind web

# 接続文字列を取得
APPI_CONN=$(az monitor app-insights component show \
  --app <YOUR_APP_INSIGHTS> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --query connectionString -o tsv)

# Container App に環境変数として設定
az containerapp update \
  --name <YOUR_CONTAINER_APP> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --set-env-vars "APPLICATIONINSIGHTS_CONNECTION_STRING=$APPI_CONN"
```

Python 側: `azure-monitor-opentelemetry` パッケージで自動計装。

---

## 9. Step 7: Key Vault 統合（F-12）

### 9.1 シークレット移行

Key Vault に全シークレットを格納し、環境変数からの直接参照を廃止。

```bash
# シークレット 3 件が登録済みであることを確認
az keyvault secret list --vault-name <YOUR_KEY_VAULT> --query "[].name" -o tsv
# 期待: AZURE-OPENAI-KEY, DATABASE-URL, GRAPH-CLIENT-SECRET
```

### 9.2 アプリケーション設定

Container App の環境変数を Key Vault 参照に変更:

```bash
az containerapp update \
  --name <YOUR_CONTAINER_APP> \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --set-env-vars \
    "KEY_VAULT_NAME=<YOUR_KEY_VAULT>"
```

`src/config.py` が `DefaultAzureCredential` → Key Vault から自動取得。ローカル開発は `.env.local` フォールバック。

---

## 10. Step 8: CI/CD セットアップ（F-13）

### 10.1 GitHub Actions ワークフロー

```
.github/workflows/
├── ci.yml        # PR: ruff lint + pytest
└── deploy.yml    # main push: Docker build → ACR push → Container Apps update
```

### 10.2 GitHub Secrets 登録

| Secret 名 | 内容 |
|-----------|------|
| `AZURE_CREDENTIALS` | Service Principal JSON（az ad sp create-for-rbac） |
| `ACR_LOGIN_SERVER` | `<YOUR_ACR_NAME>.azurecr.io` |
| `ACR_USERNAME` | ACR ユーザー名 |
| `ACR_PASSWORD` | ACR パスワード |

### 10.3 Service Principal 作成

```bash
az ad sp create-for-rbac --name <YOUR_SP_NAME> \
  --role contributor \
  --scopes /subscriptions/<SUB_ID>/resourceGroups/<YOUR_RESOURCE_GROUP> \
  --sdk-auth
# 出力 JSON を GitHub Secrets の AZURE_CREDENTIALS に登録
```

---

## 11. Step 9: GraphRAG テーブル作成（F-14）

Supabase SQL Editor で実行:

```sql
-- pg_trgm 拡張を有効化（エンティティ名のあいまい検索用）
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- エンティティテーブル
CREATE TABLE entities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    properties  JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (name, type)
);

CREATE INDEX idx_entities_name ON entities USING gin (name gin_trgm_ops);
CREATE INDEX idx_entities_type ON entities (type);

-- 関係テーブル
CREATE TABLE relations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_entity_id  UUID REFERENCES entities(id),
    to_entity_id    UUID REFERENCES entities(id),
    relation_type   TEXT NOT NULL,
    source_chunk_id TEXT REFERENCES chunks(chunk_id),
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_relations_from ON relations (from_entity_id);
CREATE INDEX idx_relations_to ON relations (to_entity_id);
CREATE INDEX idx_relations_chunk ON relations (source_chunk_id);
```

---

## 12. Step 10: コスト監視セットアップ（NF-C03）

```bash
# 月額バジェットアラートを設定（¥4,000 で警告、¥5,000 で通知）
az consumption budget create \
  --budget-name <YOUR_BUDGET_NAME> \
  --amount 5000 \
  --time-grain Monthly \
  --start-date $(date +%Y-%m-01) \
  --end-date 2027-03-31 \
  --category Cost \
  --resource-group <YOUR_RESOURCE_GROUP> \
  --notifications \
    '{"warning80":{"enabled":true,"operator":"GreaterThanOrEqualTo","threshold":80,"contactEmails":["admin@example.com"]},"limit100":{"enabled":true,"operator":"GreaterThanOrEqualTo","threshold":100,"contactEmails":["admin@example.com"]}}'
```

---

## 13. PoC 終了後の削除

```bash
# Azure リソース
az group delete --name <YOUR_RESOURCE_GROUP> --yes

# Supabase
# ダッシュボードから Project Settings → Delete Project
# または7日間アクセスなしで自動停止
```

---

以上
