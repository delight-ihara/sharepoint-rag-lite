"""ACL 解決モジュール — インジェストと検索で共有

インジェスト時: resolve_folder_acl() でフォルダ権限を個人メールに展開
検索時:         resolve_user_groups() でユーザーの全グループ所属を取得

前提: Entra ID アプリに以下の Application 権限が必要
  - GroupMember.Read.All
  - Directory.Read.All
  - User.Read.All
  - Sites.Read.All  (SP サイトメンバー展開。未付与時は SP_SITE_MEMBERS 環境変数でフォールバック)
"""

import logging
import re
from urllib.parse import quote

import requests
from cachetools import TTLCache
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import (
    ACL_GROUP_CACHE_TTL,
    GRAPH_BASE,
    GRAPH_CLIENT_ID,
    GRAPH_CLIENT_SECRET,
    GRAPH_TENANT_ID,
    REJECT_ANONYMOUS_LINKS,
    SP_DRIVE_ID,
    SP_SITE_ID,
    SP_SITE_MEMBERS_FALLBACK,
)

log = logging.getLogger(__name__)

# ── キャッシュ ──────────────────────────────────────────

_folder_acl_cache: dict[str, list[str]] = {}
_group_member_cache: dict[str, list[str]] = {}
_user_groups_cache: TTLCache = TTLCache(maxsize=256, ttl=ACL_GROUP_CACHE_TTL)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# ── Graph API 認証 ──────────────────────────────────────


def _is_transient_http_error(exc: BaseException) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        if exc.response.status_code in (429, 502, 503, 504):
            return True
    return False


@retry(
    retry=retry_if_exception(_is_transient_http_error),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(log, logging.WARNING),
    reraise=True,
)
def get_app_token() -> str:
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


# ── 権限チェック ────────────────────────────────────────


def check_graph_permissions(token: str) -> None:
    """Graph API の必要権限があるか確認。不足なら PermissionError を送出。

    インジェスト開始前に呼び出し、壊れた ACL でインジェストするのを防止する。
    """
    headers = {"Authorization": f"Bearer {token}"}
    checks = {
        "GroupMember.Read.All": f"{GRAPH_BASE}/groups?$top=1&$select=id",
        "User.Read.All": f"{GRAPH_BASE}/users?$top=1&$select=id",
    }
    missing = []
    for perm, url in checks.items():
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 403:
                missing.append(perm)
        except Exception:
            log.warning("権限チェック中にエラー (%s)", perm)

    if missing:
        msg = (
            f"Graph API 権限不足: {', '.join(missing)}\n"
            f"手順: docs/05-acl-remediation.md を参照してください。"
        )
        log.error(msg)
        raise PermissionError(msg)

    log.info("Graph API 権限チェック OK")


# ── ユーザーグループ解決（検索時）─────────────────────────


def resolve_user_groups(user_email: str) -> list[str]:
    """ユーザーのメール + 全グループ所属を Graph API で取得

    検索時に呼び出し、ACL フィルタの user_groups パラメータに渡す。
    TTL キャッシュ付きで頻繁な API 呼び出しを回避。
    """
    email_lower = user_email.lower()

    if email_lower in _user_groups_cache:
        return _user_groups_cache[email_lower]

    groups = [email_lower]

    try:
        token = get_app_token()
        headers = {"Authorization": f"Bearer {token}"}

        # ユーザーの全グループ所属を取得
        url: str | None = (
            f"{GRAPH_BASE}/users/{quote(email_lower)}"
            f"/transitiveMemberOf?$select=id,mail,displayName&$top=999"
        )
        while url:
            resp = requests.get(url, headers=headers, timeout=30)
            if not resp.ok:
                log.warning("ユーザーグループ取得失敗 (%s): %d", email_lower, resp.status_code)
                break
            data = resp.json()
            for g in data.get("value", []):
                mail = g.get("mail")
                if mail:
                    groups.append(mail.lower())
            url = data.get("@odata.nextLink")

    except Exception as e:
        log.warning("ユーザーグループ解決失敗 (%s): %s — メールのみで ACL チェック", email_lower, e)

    result = list(set(groups))
    _user_groups_cache[email_lower] = result
    log.info("resolve_user_groups(%s): %d グループ", email_lower, len(result))
    return result


# ── フォルダ ACL 解決（インジェスト時）──────────────────────


def _extract_uuid(login_name: str) -> str | None:
    """c:0t.c|tenant|UUID 形式から UUID を抽出"""
    if "|" in login_name:
        candidate = login_name.split("|")[-1]
        if _UUID_RE.match(candidate):
            return candidate
    return None


def _resolve_entra_group_or_role(token: str, uuid: str) -> list[str]:
    """Entra ID グループまたはディレクトリロールのメンバーを展開

    1. /groups/{uuid}/transitiveMembers（M365/セキュリティグループ）
    2. 404 → /directoryRoles のうち roleTemplateId 一致を検索 → members
    """
    if uuid in _group_member_cache:
        return _group_member_cache[uuid]

    headers = {"Authorization": f"Bearer {token}"}
    members: list[str] = []

    # 試行1: M365/セキュリティグループ
    url: str | None = f"{GRAPH_BASE}/groups/{uuid}/transitiveMembers?$select=mail,userPrincipalName&$top=999"
    found = False
    try:
        while url:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            found = True
            for m in resp.json().get("value", []):
                if m.get("@odata.type", "") and "user" not in m["@odata.type"].lower():
                    continue
                email = m.get("mail") or m.get("userPrincipalName") or ""
                if email:
                    members.append(email.lower())
            url = resp.json().get("@odata.nextLink")
    except Exception as e:
        log.warning("Entra グループ展開失敗 (%s): %s", uuid, e)

    # 試行2: ディレクトリロール（直接アクセス）
    if not found:
        try:
            mr = requests.get(
                f"{GRAPH_BASE}/directoryRoles/{uuid}/members?$select=mail,userPrincipalName",
                headers=headers, timeout=30,
            )
            if mr.ok:
                for m in mr.json().get("value", []):
                    email = m.get("mail") or m.get("userPrincipalName") or ""
                    if email:
                        members.append(email.lower())
                found = True
            elif mr.status_code == 404:
                # roleTemplateId かもしれない → ロール一覧から検索
                roles_resp = requests.get(
                    f"{GRAPH_BASE}/directoryRoles?$select=id,roleTemplateId",
                    headers=headers, timeout=30,
                )
                if roles_resp.ok:
                    for role in roles_resp.json().get("value", []):
                        if role.get("roleTemplateId") == uuid:
                            mr2 = requests.get(
                                f"{GRAPH_BASE}/directoryRoles/{role['id']}/members?$select=mail,userPrincipalName",
                                headers=headers, timeout=30,
                            )
                            if mr2.ok:
                                for m in mr2.json().get("value", []):
                                    email = m.get("mail") or m.get("userPrincipalName") or ""
                                    if email:
                                        members.append(email.lower())
                                found = True
                            break
        except Exception as e:
            log.warning("Entra ロール展開失敗 (%s): %s", uuid, e)

    if not found:
        log.warning("Entra ID %s: グループでもロールでもありません", uuid)

    result = list(set(members))
    _group_member_cache[uuid] = result
    if result:
        log.info("Entra %s → %d メンバー展開", uuid, len(result))
    return result


def _resolve_sp_site_group(token: str) -> list[str]:
    """SharePoint サイトメンバー全体を取得

    SP サイトグループ（Owners/Members/Visitors）の個別展開は Graph API 非対応。
    代わりにサイトメンバー全体を返す。
    """
    cache_key = f"sp_site:{SP_SITE_ID}"
    if cache_key in _group_member_cache:
        return _group_member_cache[cache_key]

    headers = {"Authorization": f"Bearer {token}"}
    members: list[str] = []

    try:
        url: str | None = f"{GRAPH_BASE}/sites/{SP_SITE_ID}/members?$select=mail,userPrincipalName&$top=999"
        while url:
            resp = requests.get(url, headers=headers, timeout=30)
            if not resp.ok:
                log.warning("SP サイトメンバー取得失敗: %d", resp.status_code)
                break
            data = resp.json()
            for m in data.get("value", []):
                email = m.get("mail") or m.get("userPrincipalName") or ""
                if email:
                    members.append(email.lower())
            url = data.get("@odata.nextLink")
    except Exception as e:
        log.warning("SP サイトメンバー取得失敗: %s", e)

    if not members:
        log.warning("SP サイトメンバー展開不可 — Sites.Read.All 未付与。このグループのメンバーは ACL に含まれません")

    result = list(set(members))
    _group_member_cache[cache_key] = result
    if result:
        log.info("SP サイトメンバー: %d 名", len(result))
    return result


def resolve_folder_acl(token: str, folder_path: str) -> list[str]:
    """フォルダの権限を取得し、閲覧可能なユーザーの UPN リストを返す

    処理順:
    1. 匿名/組織全体リンク → ["*"] or 拒否 (REJECT_ANONYMOUS_LINKS)
    2. SharePoint サイトグループ → サイトメンバーを展開
    3. Entra ID グループ/ロール → transitiveMembers で個人メールに展開
    4. 個人ユーザー → メールアドレスをそのまま使用
    """
    top_folder = folder_path.split("/")[0] if folder_path else ""
    if not top_folder:
        return ["*"]

    if top_folder in _folder_acl_cache:
        return _folder_acl_cache[top_folder]

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{GRAPH_BASE}/drives/{SP_DRIVE_ID}/root:/{quote(top_folder)}:/permissions"

    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 404:
        log.warning("権限取得失敗 (404): %s — 継承権限と判断", top_folder)
        _folder_acl_cache[top_folder] = ["*"]
        return ["*"]
    resp.raise_for_status()

    allowed_users: list[str] = []
    entra_ids_to_resolve: list[str] = []
    has_sp_site_groups = False

    for perm in resp.json().get("value", []):
        # --- 共有リンク ---
        link = perm.get("link", {})
        link_scope = link.get("scope", "")
        if link_scope in ("anonymous", "organization"):
            if REJECT_ANONYMOUS_LINKS:
                log.warning(
                    "SECURITY: フォルダ '%s' に %s リンクあり → REJECT_ANONYMOUS_LINKS=true のためスキップ",
                    top_folder, link_scope,
                )
                continue
            log.warning(
                "SECURITY: フォルダ '%s' に %s リンクあり → 全員アクセス可として処理",
                top_folder, link_scope,
            )
            _folder_acl_cache[top_folder] = ["*"]
            return ["*"]

        granted = perm.get("grantedToV2") or perm.get("grantedTo") or {}

        # --- SP サイトグループ ---
        sp_group = granted.get("siteGroup") or {}
        sp_pg = (perm.get("grantedToV2") or {}).get("sharePointGroup") or {}
        if sp_group.get("principalId") or sp_group.get("id") or sp_pg.get("id"):
            has_sp_site_groups = True
            continue

        # --- Entra ID グループ/ロール ---
        entra_group = granted.get("group") or {}
        if entra_group.get("id"):
            entra_ids_to_resolve.append(entra_group["id"])
            continue
        site_user = granted.get("siteUser") or {}
        if site_user.get("loginName"):
            uuid = _extract_uuid(site_user["loginName"])
            if uuid:
                entra_ids_to_resolve.append(uuid)
                continue

        # --- 個人ユーザー ---
        user = granted.get("user") or granted.get("siteUser") or {}
        if user.get("email"):
            allowed_users.append(user["email"].lower())
        elif user.get("loginName") and not user["loginName"].startswith("c:0"):
            allowed_users.append(user["loginName"].lower())

        # --- grantedToIdentities ---
        for identity in perm.get("grantedToIdentitiesV2", perm.get("grantedToIdentities", [])):
            u = identity.get("user", {})
            if u.get("email"):
                allowed_users.append(u["email"].lower())
            elif u.get("id"):
                entra_ids_to_resolve.append(u["id"])
            g = identity.get("group", {})
            if g.get("id"):
                entra_ids_to_resolve.append(g["id"])

    # --- Entra グループ/ロール展開 ---
    for eid in set(entra_ids_to_resolve):
        members = _resolve_entra_group_or_role(token, eid)
        allowed_users.extend(members)

    # --- SP サイトグループ展開 ---
    if has_sp_site_groups:
        members = _resolve_sp_site_group(token)
        allowed_users.extend(members)

    if not allowed_users:
        log.info("フォルダ '%s' の明示的権限なし → 継承（全員アクセス可）", top_folder)
        _folder_acl_cache[top_folder] = ["*"]
        return ["*"]

    result = list(set(allowed_users))
    _folder_acl_cache[top_folder] = result
    log.info("フォルダ '%s' ACL: %d ユーザー（グループ展開後）", top_folder, len(result))
    return result


def clear_caches():
    """キャッシュをクリア（テスト用）"""
    _folder_acl_cache.clear()
    _group_member_cache.clear()
    _user_groups_cache.clear()
