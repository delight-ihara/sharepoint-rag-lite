# 前提条件チェックリスト — SharePoint RAG 構築代行

> 構築開始前に全項目を完了すること。1つでも未完了なら構築を開始しない。

## Phase A: 先方（顧客）側作業

### A-1. Entra ID アプリ登録 + 権限付与

**実施者**: Global Admin（または Application Administrator + Privileged Role Administrator）

- [ ] アプリ登録を作成（または既存アプリを指定）
- [ ] Application (Client) ID を共有
- [ ] Client Secret を作成し、安全な方法で共有
- [ ] 以下の **Application 権限** を追加:

| 権限 | 用途 |
|------|------|
| `Files.Read.All` | SharePoint ファイル読み取り |
| `Sites.Read.All` | SharePoint サイト・権限エントリ取得 |
| `GroupMember.Read.All` | M365/セキュリティグループのメンバー展開 |
| `Directory.Read.All` | ディレクトリロール（Global Admin 等）メンバー展開 |
| `User.Read.All` | ユーザー情報・グループ所属取得 |

> **Note:** `Sites.Read.All` は権限エントリ取得に加え、SP サイトメンバー展開にも使用する。付与できない場合は `SP_SITE_MEMBERS` 環境変数（カンマ区切りメールアドレス）でフォールバック可能。

- [ ] **管理者の同意** (Grant admin consent) を実行

**検証コマンド（構築側が実行）:**
```bash
# トークン取得
TOKEN=$(curl -s -X POST "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token" \
  -d "client_id=$CLIENT_ID&client_secret=$SECRET&scope=https://graph.microsoft.com/.default&grant_type=client_credentials" \
  | jq -r .access_token)

# 権限テスト（全て200が返ればOK、403なら権限不足）
curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" "https://graph.microsoft.com/v1.0/groups?\$top=1"
curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" "https://graph.microsoft.com/v1.0/users?\$top=1"
curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" "https://graph.microsoft.com/v1.0/sites?\$top=1"
```

### A-2. SharePoint 権限の棚卸し

**実施者**: SharePoint 管理者 or 文書管理担当

- [ ] 対象サイトの URL を共有
- [ ] 各フォルダの権限設定を確認（管理画面 → アクセス許可の管理）
- [ ] **匿名共有リンク（「リンクを知っているすべてのユーザー」）がないことを確認**
  - ある場合は削除するか、意図的な全社公開かを判断
- [ ] 各フォルダのアクセス対象者一覧を ACL 真理値表に記入

### A-3. テストユーザーの指定

**実施者**: 顧客担当者

- [ ] 4種類のテストユーザー（実在のメールアドレス）を指定:
  - BOSS: 全フォルダにアクセス可能
  - MEMBER: 一部フォルダにアクセス可能
  - SALES: 限定フォルダのみ
  - GENERAL: アクセス権なし
- [ ] 各テストユーザーで SharePoint の対象フォルダが実際に閲覧できることを先方自身で確認

### A-4. ACL 真理値表の合意

**実施者**: 顧客担当者 + 構築側

- [ ] `03-acl-truth-table.md` を共同で記入
- [ ] 「誰が何を見れるか」を書面で合意（署名 or メール確認）
- [ ] テストユーザーと真理値表が整合していることを確認

### A-5. Azure 環境の準備

**実施者**: Azure サブスクリプション管理者

- [ ] Azure サブスクリプションが利用可能
- [ ] 構築側にリソースグループの Contributor 権限を付与（または専用 RG を作成して移譲）
- [ ] Azure OpenAI の利用が可能（リージョン・クォータ確認）

---

## Phase B: 構築側作業（先方作業完了後）

### B-1. 環境構築

- [ ] Azure リソースグループ作成
- [ ] Azure OpenAI リソース作成 + モデルデプロイ（embedding + chat）
- [ ] Key Vault 作成 + シークレット3件登録
- [ ] Supabase プロジェクト作成（PoC）/ Azure PostgreSQL Flexible（本番）
- [ ] pgvector 有効化 + テーブル作成（6テーブル）
- [ ] Container Apps Environment + Container App 作成
- [ ] EasyAuth 設定（Entra ID SSO）
- [ ] Managed Identity → RBAC 設定（Key Vault, OpenAI）

### B-2. SharePoint 接続テスト

- [ ] Graph API でファイル一覧が取得できることを確認
- [ ] Graph API で各フォルダの権限エントリが取得できることを確認
- [ ] グループメンバー展開ができることを確認（`GroupMember.Read.All` テスト）
- [ ] SP サイトメンバー展開ができることを確認（`Sites.Read.All` テスト。失敗時は `SP_SITE_MEMBERS` 環境変数を設定）
- [ ] `check_graph_permissions()` が成功することを確認

### B-3. インジェスト

- [ ] フルインジェスト実行（`python -m src.ingest`）
- [ ] スキップレポート確認（未対応形式・抽出失敗の一覧）
- [ ] DB の ACL データ品質チェック:
  - 未解決 UUID (`c:0t.c|tenant|...`) が0件
  - 制限フォルダに `["*"]` がない
  - 各フォルダの ACL が真理値表と一致

### B-4. テスト

- [ ] `pytest tests/` — 全ユニットテスト pass
- [ ] `python run_tests.py` — 全結合テスト pass（実メール使用）
- [ ] ACL テスト結果を真理値表と突き合わせて合格判定
- [ ] テスト結果レポートを顧客に共有

### B-5. デプロイ + 受入テスト

- [ ] Container Apps にデプロイ
- [ ] 顧客に受入テストを実施してもらう（`05-acceptance-test.md` に基づく）
- [ ] 受入テスト合格 → 本番稼働開始

---

## ブロッカー判定基準

以下のいずれかが未完了の場合、**構築を開始しない**:

| ブロッカー | 理由 |
|-----------|------|
| admin consent 未取得 | ACL が機能しない |
| 匿名リンク未確認 | ACL が無効化される可能性 |
| テストユーザー未指定 | テスト結果が偽陽性になる |
| ACL 真理値表未合意 | テストの合否判定基準がない |
| Azure サブスクリプション未準備 | リソース作成不可 |
