# 構築ランブック — SharePoint RAG 構築代行

> 構築側が順番に実行する手順書。前提条件チェックリスト（02）が全完了していること。

## 事前準備

```bash
# リポジトリ clone
git clone https://github.com/delight-ihara/sharepoint-rag-lite.git
cd sharepoint-rag-lite
pip install -r requirements.txt

# 顧客情報を .env.local に設定
cp .env.example .env.local
```

`.env.local` に以下を記入（顧客から受領した情報）:

```env
# Supabase / Azure PostgreSQL
DATABASE_URL=postgresql://...

# Azure OpenAI
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_KEY=<key>

# Graph API（顧客 Entra ID アプリ）
GRAPH_TENANT_ID=<顧客テナントID>
GRAPH_CLIENT_ID=<アプリ Client ID>
GRAPH_CLIENT_SECRET=<Client Secret>

# SharePoint
SP_SITE_ID=<Graph Explorer で取得>
SP_DRIVE_ID=<Graph Explorer で取得>

# 対象フォルダ（顧客と合意済み）
SP_TARGET_FOLDERS=01_経営,02_人事労務,03_営業

# ACL 設定
ACL_ENABLED=true
REJECT_ANONYMOUS_LINKS=true

# SP サイトメンバーフォールバック（Sites.Read.All が付与できない場合のみ）
# SP_SITE_MEMBERS=user1@customer.co.jp,user2@customer.co.jp

# テストユーザー（顧客から受領）
TEST_BOSS_EMAIL=boss@customer.co.jp
TEST_MEMBER_EMAIL=member@customer.co.jp
TEST_SALES_EMAIL=sales@customer.co.jp
TEST_GENERAL_EMAIL=nobody@customer.co.jp
```

---

## Step 1: Graph API 接続テスト（10分）

```bash
python -c "
from src.acl import get_app_token, check_graph_permissions
token = get_app_token()
print('Token OK')
check_graph_permissions(token)
print('Permissions OK')
"
```

**期待結果**: `Token OK` → `Permissions OK`
**失敗した場合**: 顧客に admin consent を再依頼（02-prerequisites の A-1 参照）

---

## Step 2: SharePoint 権限の実態確認（15分）

```bash
python -c "
from src.acl import get_app_token, resolve_folder_acl, clear_caches
import os, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

token = get_app_token()
folders = os.environ.get('SP_TARGET_FOLDERS', '').split(',')

for f in folders:
    f = f.strip()
    if not f:
        continue
    clear_caches()
    # SP_TARGET_FOLDERS のプレフィックスだけでは正確なフォルダ名が分からないので
    # list_sp_files で実際のフォルダ名を取得する方が正確
    # ここでは手動でフォルダ名を指定する
    acl = resolve_folder_acl(token, f)
    if acl == ['*']:
        print(f'{f}: 全員アクセス可（匿名リンク or 継承）')
    else:
        print(f'{f}: {len(acl)} ユーザー')
        for u in sorted(acl)[:10]:
            print(f'  - {u}')
"
```

**確認事項**:
- [ ] 各フォルダの ACL が真理値表（03）と一致するか
- [ ] 匿名リンクがある場合、`REJECT_ANONYMOUS_LINKS=true` で意図通り動くか
- [ ] グループが個人メールに展開されているか

---

## Step 3: DB セットアップ（10分）

Supabase SQL Editor or psql で以下を実行:

```sql
-- pgvector 有効化
CREATE EXTENSION IF NOT EXISTS vector;

-- テーブル作成（docs/10-build-guide.md のSQLを実行）
-- chunks, conversations, query_logs, feedback, entities, relations
```

---

## Step 4: インジェスト（20-60分、ファイル数による）

```bash
python -m src.ingest 2>&1 | tee ingest.log
```

**確認事項**:
- [ ] `権限チェック OK` が出力されるか
- [ ] スキップレポートの内容を確認（未対応形式・抽出失敗）
- [ ] 処理件数 / スキップ件数が想定通りか

**インジェスト後の DB チェック**:
```sql
-- カテゴリ別チャンク数
SELECT category, COUNT(*) as chunks, COUNT(DISTINCT title) as files
FROM chunks GROUP BY category ORDER BY category;

-- 未解決UUID チェック（0件であること）
SELECT count(*) FROM chunks
WHERE EXISTS (
    SELECT 1 FROM unnest(allowed_groups) AS g
    WHERE g ~ '^c:0' OR g ~ '^[0-9a-f]{8}-[0-9a-f]{4}-'
);

-- ワイルドカードチェック（制限フォルダに * がないこと）
SELECT DISTINCT category FROM chunks
WHERE '*' = ANY(allowed_groups);
```

---

## Step 5: ユニットテスト（5分）

```bash
python -m pytest tests/ -v
```

**期待結果**: 全件 pass

---

## Step 6: 結合テスト（10分）

```bash
# .env.local にテストユーザーが設定されていること
python run_tests.py 2>&1 | tee test_results.log
```

**期待結果**: 全件 OK、0件 NG
**NG が出た場合**: test_results.txt を確認し、ACL 真理値表と照合

---

## Step 7: デプロイ（15分）

```bash
# Docker ビルド + ACR push
az acr build --registry <ACR_NAME> \
  --image sharepoint-rag-lite:latest --file Dockerfile .

# Container App 更新
az containerapp update \
  --name <CONTAINER_APP_NAME> \
  --resource-group <RESOURCE_GROUP> \
  --image <ACR_NAME>.azurecr.io/sharepoint-rag-lite:latest

# 環境変数設定（Key Vault 参照）
az containerapp update \
  --name <CONTAINER_APP_NAME> \
  --resource-group <RESOURCE_GROUP> \
  --set-env-vars \
    KEY_VAULT_NAME=<KV_NAME> \
    AZURE_OPENAI_ENDPOINT=<ENDPOINT> \
    SP_SITE_ID=<SITE_ID> \
    SP_DRIVE_ID=<DRIVE_ID> \
    SP_TARGET_FOLDERS=<FOLDERS> \
    ACL_ENABLED=true \
    REJECT_ANONYMOUS_LINKS=true
```

---

## Step 8: 本番動作確認（10分）

```bash
# ヘルスチェック
curl https://<CONTAINER_APP_URL>/health

# テストユーザーでブラウザからアクセスし、検索結果が ACL 通りか確認
```

---

## Step 9: 顧客受入テスト（顧客作業）

- `05-acceptance-test.md` を顧客に渡す
- 顧客がテストケースを実行し、合否を記入
- 全件合格 → 本番稼働開始
