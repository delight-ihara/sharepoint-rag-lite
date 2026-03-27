# アーキテクチャ設計書（SharePoint RAG Lite）

## 変更履歴

| 版数 | 日付 | 変更内容 |
|------|------|----------|
| 0.1 | 2026-03-26 | 初版作成 |
| 0.2 | 2026-03-27 | Phase 0.1: エラーハンドリング・入力バリデーション・CORS 制限・Dockerfile 強化・pytest 追加 |

---

## 1. 設計方針

| # | 方針 | 説明 |
|---|------|------|
| 1 | AI Search を排除しコスト最小化 | ベクトル検索は pgvector、リランカー・RRF は段階的に追加 |
| 2 | 既存コード最大流用 | sp_to_blob.py / llm.py / server.js の ACL・回答生成ロジックをそのまま使う |
| 3 | PoC 構成 + スケール設計 | 10名・100件以上で構築。100-300名へのパスは本書に記載 |
| 4 | セキュリティ バイ デザイン | ACL を初期構築から組み込む（既存構成と同一水準） |
| 5 | 段階的精度改善 | ベクトル検索のみ → Cohere Rerank → RRF → embedding-large の順に追加。70% 達成で止める |

---

## 2. 全体構成図

### 2.1 テキスト構成図

```
[SharePoint Online]
    │ Graph API（sp_to_blob.py 流用 → ingest.py に統合）
    │ ファイル取得 + フォルダ権限取得（allowed_groups）
    ▼
[Python インジェストパイプライン]
    ├─ テキスト抽出（PyMuPDF / python-docx / openpyxl）
    ├─ チャンキング（1024トークン / 200オーバーラップ）
    ├─ エンベディング（Azure OpenAI text-embedding-3-small）
    └─ DB 書き込み（チャンク + ベクトル + ACL メタデータ）
    │
    ▼
[PostgreSQL + pgvector]
    ├─ chunks テーブル（ベクトル検索 + ACL フィルタ）
    ├─ conversations テーブル（会話履歴）
    └─ query_logs テーブル（クエリログ）
    │
    ▼
[Azure Container Apps / App Service]
    ├─ Web API（Python / FastAPI）
    │    ├─ Entra ID SSO（ユーザー email 抽出）
    │    ├─ pgvector ベクトル検索 + ACL WHERE 句
    │    ├─ （段階的）Cohere Rerank / RRF
    │    └─ Azure OpenAI GPT-4o-mini → 根拠リンク付き回答
    └─ チャット UI（既存 webapp 流用 or 簡易 SPA）
    │
    ▼
ユーザー
```

### 2.2 既存構成との対応

```
既存:  SP → Blob → AI Search インデクサー → AI Search → Functions → App Service
本構成: SP →（Blob 不要）→ Python パイプライン → pgvector → FastAPI → Container Apps
```

Blob Storage を中間ストレージとして使わない。Graph API で取得したファイルを Python 内でテキスト抽出し、直接 pgvector に書き込む。

---

## 3. コンポーネント一覧

| # | コンポーネント | サービス / SKU | 役割 | 既存との差分 |
|---|---|---|---|---|
| 1 | 文書取得 + ACL | Graph API（Python） | SP 文書取得 + フォルダ権限取得 | 既存 sp_to_blob.py を流用。Blob 書き込みを DB 書き込みに変更 |
| 2 | テキスト抽出 | PyMuPDF / python-docx / openpyxl | PDF・Word・Excel からテキスト抽出 | 既存: DI Layout。本構成: OSS ライブラリ（D-03） |
| 3 | チャンキング | カスタム Python or LlamaIndex | 1024トークン / 200オーバーラップ | 既存: DI Layout の構造認識チャンキング |
| 4 | エンベディング | Azure OpenAI text-embedding-3-small | 1536次元ベクトル生成 | 既存: text-embedding-3-large 3072次元（D-04） |
| 5 | ベクトル DB | PostgreSQL + pgvector | ベクトル検索 + ACL フィルタ + 会話履歴 + クエリログ | 既存: AI Search + Cosmos DB |
| 6 | 回答生成 | Azure OpenAI GPT-4o-mini | 根拠付き自然言語回答 | 同一 |
| 7 | Web API | Python / FastAPI | クエリ処理・プロンプト構築・認証・入力バリデーション・エラーハンドリング | 既存: Azure Functions + App Service（2層） |
| 8 | ホスティング | Azure Container Apps（Consumption） | API + UI のホスト | 既存: App Service B1 + Functions Y1 |
| 9 | 認証 | Entra ID SSO | SSO + Graph API 認証 | 同一 |
| 10 | シークレット | Azure Key Vault | API キー・接続文字列 | 同一 |

**削除されたコンポーネント**（コスト削減）:
- Azure AI Search（S1）→ pgvector で代替
- Cosmos DB → PostgreSQL で統合
- Blob Storage（中間格納用）→ 不要
- Document Intelligence → Python ライブラリで代替
- Cognitive Services マルチサービス → 不要
- Application Insights → PoC では省略（外販時に追加）

---

## 4. Alternatives Considered

| 選定事項 | 採用 | 代替案 | 不採用理由 |
|---------|------|--------|-----------|
| ベクトル DB | pgvector (PostgreSQL) | Qdrant / Weaviate / FAISS | pgvector は PostgreSQL 拡張のため ACL・会話履歴・クエリログを同一 DB で管理可能。専用 DB は追加インフラが必要 |
| ホスティング | Azure Container Apps (Consumption) | Azure App Service B1 / Azure VM B1s | Container Apps はスケールゼロ対応でスタートアップ規模向け。App Service B1 は常時 ~¥1,800/月。VM は管理工数増 |
| PostgreSQL ホスト（PoC） | Supabase Free | Azure PostgreSQL Flexible B1ms | PoC で ¥5,000/月の DB は過剰。Supabase Free で 500MB・pgvector 対応 |
| PostgreSQL ホスト（本番） | Azure PostgreSQL Flexible B1ms | Supabase Pro | Azure 統一。バックアップ・監視・VNet 統合。外販時の信頼性 |
| テキスト抽出 | PyMuPDF + python-docx + openpyxl | DI Layout API（単体呼び出し） | 100文書なら OSS で十分。DI Layout は $10/1,000ページ（精度不足時に追加検討） |
| Web フレームワーク | FastAPI | Azure Functions + Semantic Kernel | Functions の2層構成（App Service + Functions）を1層に統合。FastAPI は軽量で Container Apps と相性良 |
| リランカー | Cohere Rerank（段階的追加） | MS Semantic Ranker / Jina Reranker | Cohere は無料枠 1,000クエリ/月。MS のは AI Search 専用。Jina は無料枠なし |
| エンベディング | text-embedding-3-small | text-embedding-3-large | コスト優先。精度不足時に large に切替（コスト差は月額数円） |
| フレームワーク | カスタム Python（初期） | LlamaIndex / LangChain | 100文書・10ユーザーなら素の Python + pgvector で十分。フレームワークの抽象化層が不要 |

---

## 5. データフロー

### 5.1 インジェスション（文書取り込み）

```
1. ingest.py（手動実行 or cron）
   SP → Graph API → ファイル一覧取得
   → 各ファイルをダウンロード
   → 同時に Graph API /permissions でフォルダ権限取得
     ※ 継承権限（明示的権限なし）は ["*"]（全員アクセス可）

2. テキスト抽出 + チャンキング
   PDF  → PyMuPDF で抽出
   Word → python-docx で抽出
   Excel → openpyxl でシート単位抽出
   → 1024トークン / 200オーバーラップでチャンク分割

3. エンベディング + DB 書き込み
   各チャンク → Azure OpenAI text-embedding-3-small → 1536次元ベクトル
   → PostgreSQL chunks テーブルに INSERT
     (chunk_id, chunk_text, embedding, title, source_url, category, allowed_groups[])
```

### 5.2 差分更新

```
ingest.py --incremental
   SP の lastModifiedDateTime と DB の updated_at を比較
   → 変更ファイルのみ再取得・再チャンク・再エンベディング
   → DB の該当チャンクを DELETE + INSERT（upsert）
   → 権限変更のみの場合は allowed_groups[] を UPDATE
```

### 5.3 クエリ（検索・回答）

```
ユーザー → Container Apps（FastAPI）
  → Entra ID SSO でユーザー email 取得
  → クエリをエンベディング（text-embedding-3-small）
  → pgvector 検索:
      SELECT chunk_text, source_url, title, category
      FROM chunks
      WHERE allowed_groups && ARRAY[$user_groups]  -- ACL フィルタ
         OR '*' = ANY(allowed_groups)              -- 全員アクセス
      ORDER BY embedding <=> $query_embedding      -- コサイン類似度
      LIMIT 10
  → （段階的）Cohere Rerank で上位を再順位付け
  → 上位チャンク + 会話履歴 + システムプロンプト → GPT-4o-mini
  → 根拠リンク付き回答（「該当なし」時は citation 抑制）
  → conversations テーブルに会話保存
  → query_logs テーブルにログ保存（ユーザー・クエリ・タイムスタンプ）
```

### 5.4 エラーハンドリング方針

| 障害箇所 | HTTP ステータス | ユーザーへの影響 | 方針 |
|----------|---------------|----------------|------|
| ベクトル検索（DB） | 503 | 回答不可 | エラーメッセージを返却。スタックトレースは非公開 |
| LLM 回答生成（Azure OpenAI） | 503 | 回答不可 | 同上 |
| 会話履歴取得（DB） | — | なし（空履歴で続行） | 非致命的。回答は返す |
| 会話履歴保存（DB） | — | なし | 非致命的。回答は返す |
| クエリログ保存（DB） | — | なし | 非致命的。回答は返す |
| 未処理例外 | 500 | 回答不可 | グローバル例外ハンドラでキャッチ。日本語エラーメッセージを返却 |

### 5.5 入力バリデーション

| フィールド | 制約 | 超過時 |
|-----------|------|--------|
| message | 1〜2000文字。空白のみ不可。前後空白は自動除去 | 422 Unprocessable Entity |
| session_id | 最大100文字 | 422 |
| user_email | 最大254文字（RFC 5321） | 422 |

---

## 6. DB 設計

### 6.1 テーブル一覧

```sql
-- ベクトル検索 + ACL
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

-- ベクトル検索用インデックス（IVFFlat: 10,000ベクトル以下に適切）
CREATE INDEX idx_chunks_embedding ON chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

-- ACL フィルタ用インデックス
CREATE INDEX idx_chunks_allowed_groups ON chunks USING gin (allowed_groups);

-- 会話履歴
CREATE TABLE conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  TEXT NOT NULL,
    user_email  TEXT NOT NULL,
    role        TEXT NOT NULL,  -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    citations   JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_conversations_session ON conversations (session_id, created_at);

-- クエリログ（UC-4）
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

### 6.2 容量見積

| テーブル | レコード数 | 1レコードあたり | 合計 |
|----------|-----------|--------------|------|
| chunks | ~5,000 | ~7KB（テキスト1KB + ベクトル6KB） | ~35MB |
| conversations | ~50,000/年 | ~1KB | ~50MB |
| query_logs | ~18,000/年 | ~0.5KB | ~9MB |
| **合計** | | | **~100MB** |

Supabase Free（500MB）に十分収まる。

---

## 7. 認証経路

| 経路 | 方式（PoC） | 100-300名時 |
|------|------------|------------|
| ユーザー → Container Apps | Entra ID SSO（EasyAuth or MSAL） | + 条件付きアクセス |
| API → Graph API | クライアントシークレット | 証明書認証 |
| API → Azure OpenAI | API キー（Key Vault） | マネージド ID |
| API → PostgreSQL | 接続文字列（環境変数 or Key Vault） | マネージド ID（Azure PostgreSQL 時） |

---

## 8. 段階的精度改善プラン

要件定義書 §1.3 の劣後点（D-01〜D-04）に対する段階的改善ステップ。各ステップで評価質問 10 問を実施し、70% 達成で停止。

| Step | 追加する機能 | 対応する劣後点 | 追加コスト | 実装工数 |
|------|------------|-------------|----------|---------|
| 0（初期） | pgvector ベクトル検索のみ | — | ¥0 | 基本構成 |
| 1 | Cohere Rerank | D-01 リランカー | ¥0（1,000クエリ/月無料） | API 呼び出し追加（~20行） |
| 2 | RRF ハイブリッド検索 | D-02 RRF | ¥0 | PostgreSQL 全文検索 + RRF スコア計算（~30行） |
| 3 | text-embedding-3-large | D-04 エンベディング | ~¥10/月 | モデル名変更 + DB カラム幅変更 |
| 4 | DI Layout API（単体） | D-03 構造解析 | ~¥200（100文書） | API 呼び出し追加 |

---

## 9. スケール時の構成変更

| 項目 | PoC（10名） | 本番（100-300名） |
|------|-----------|----------------|
| PostgreSQL | Supabase Free | Azure PostgreSQL Flexible B1ms〜B2s |
| ホスティング | Container Apps Consumption | Container Apps Dedicated or App Service |
| ベクトルインデックス | IVFFlat (lists=10) | HNSW (m=16, ef_construction=64) |
| リランカー | Cohere Free（1,000クエリ/月） | Cohere Production or 自前 Cross-Encoder |
| 認証 | EasyAuth | + 条件付きアクセス + MFA |
| 監視 | ログ出力のみ | Application Insights |
| バックアップ | なし | Azure PostgreSQL 自動バックアップ |
| ネットワーク | パブリック | VNet + Private Endpoint |

---

## 10. 要件トレーサビリティ

| 要件 | 実現 |
|------|------|
| F-01 文書検索 | pgvector ベクトル検索（+ 段階的 Cohere/RRF） |
| F-02 回答生成 | GPT-4o-mini + 根拠チャンクベースのプロンプト（llm.py 流用） |
| F-03 会話履歴 | PostgreSQL conversations テーブル |
| F-04 文書取り込み | ingest.py（Graph API → テキスト抽出 → pgvector） |
| F-05 認証 | Entra ID SSO |
| F-06 チャット UI | Container Apps 上の SPA |
| F-07 ACL 制御 | allowed_groups[] + SQL WHERE 句 |
| F-08 クエリログ | PostgreSQL query_logs テーブル |
| F-09 権限同期 | ingest.py --incremental（cron 実行） |

---

## 11. 関連文書

| 文書 | 内容 |
|------|------|
| 要件定義書 | 本書が実現する要件。劣後点一覧（§1.3） |
| セキュリティ設計書 | 認証・認可・ACL 詳細（作成予定） |
| テスト仕様書 | 既存 18ケース + 追加分（作成予定） |
| 既存アーキテクチャ設計書 | [`02-architecture.md`](https://github.com/YuhtaIhara/sharepoint-rag-azure/blob/main/docs/02-architecture.md) |

---

以上
