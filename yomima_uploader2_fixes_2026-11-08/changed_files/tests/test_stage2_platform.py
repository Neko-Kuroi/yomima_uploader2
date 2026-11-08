"""段階2 (プラットフォーム側): 甲-6, 乙-1, 乙-2, 丙-3, 丙-7, 丙-10/Y-3,
N-13, N-5, 追加 甲-1/甲-2 のうち API 挙動で確かめられるもの。
"""
import hashlib
import hmac as hmac_lib
import json
import base64
from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jose import jwt as jose_jwt


@pytest.fixture(scope="module")
def platform(platform_main):
    return TestClient(platform_main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_limiters(platform_main):
    # slowapi のカウントはプロセス内メモリ。テスト同士が制限に
    # 引っかからないよう、各テストの前にリセットする。
    try:
        platform_main.limiter.reset()
    except Exception:
        pass
    yield


def _signup(platform, username, email, password="stage2-password"):
    return platform.post("/api/auth/signup", json={
        "username": username, "email": email, "password": password})


def _login(platform, username, password="stage2-password"):
    return platform.post("/api/auth/token", data={
        "username": username, "password": password})


def _make_user(platform, username, email):
    r = _signup(platform, username, email)
    assert r.status_code == 200, r.text
    r = _login(platform, username)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _admin_headers(platform):
    r = platform.post("/api/admin/token", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _publish(platform, platform_main, headers, title_id, episode_id,
             status="published"):
    payload = {
        "status": status, "access_level": "public", "price": 0,
        "title_name": f"T{title_id}", "episode_name": f"E{episode_id}",
        "caption": "", "comment": "", "note": "", "feedback": "",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "rating": "General", "warnings": [], "genre_tags": ["日常"],
        "motif_tags": [],
    }
    r = platform.patch(
        f"/api/author/titles/{title_id}/episodes/{episode_id}/settings",
        json=payload, headers=headers)
    assert r.status_code == 200, r.text
    db = platform_main.SessionLocal()
    try:
        ep = db.query(platform_main.Episode).filter_by(id=episode_id).first()
        return ep.public_id
    finally:
        db.close()


def _fake_request(cf_ip=None, host="198.51.100.7"):
    headers = {}
    if cf_ip is not None:
        headers["CF-Connecting-IP"] = cf_ip
    return SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, d=None: headers.get(k, d)),
        client=SimpleNamespace(host=host))


# ---------------------------------------------------------------------------
# 甲-6: CF-Connecting-IP を既定では信じない
# ---------------------------------------------------------------------------

def test_platform_get_client_ip_ignores_cf_header_by_default(platform_main):
    req = _fake_request(cf_ip="203.0.113.99")
    assert platform_main.get_client_ip(req) == "198.51.100.7"


def test_platform_get_client_ip_trusts_cf_only_with_flag(platform_main,
                                                         monkeypatch):
    req = _fake_request(cf_ip="203.0.113.99")
    monkeypatch.setenv("TRUST_CF_CONNECTING_IP", "1")
    assert platform_main.get_client_ip(req) == "203.0.113.99"
    monkeypatch.delenv("TRUST_CF_CONNECTING_IP")
    assert platform_main.get_client_ip(req) == "198.51.100.7"


def test_viewer_get_client_ip_same_policy(viewer_main, monkeypatch):
    req = _fake_request(cf_ip="203.0.113.99")
    assert viewer_main.get_client_ip(req) == "198.51.100.7"
    monkeypatch.setenv("TRUST_CF_CONNECTING_IP", "1")
    assert viewer_main.get_client_ip(req) == "203.0.113.99"


def test_viewer_session_ip_is_not_forged_cf_header(viewer_main, manga,
                                                   monkeypatch):
    monkeypatch.setattr(manga, "_resolve_host_ips",
                        lambda host: ["93.184.216.34"], raising=False)
    monkeypatch.delenv("TRUST_CF_CONNECTING_IP", raising=False)
    client = TestClient(viewer_main.app, raise_server_exceptions=False)
    r = client.get("/session", params={"url": "http://example.com/s2.zip"},
                   headers={"CF-Connecting-IP": "203.0.113.99"})
    assert r.status_code == 200, r.text
    sess = manga._session_store[r.json()["session_token"]]
    assert sess["ip"] != "203.0.113.99"
    assert sess["ip"] == "testclient"


# ---------------------------------------------------------------------------
# 丙-3: 登録検査
# ---------------------------------------------------------------------------

def test_signup_rejects_short_password(platform):
    r = _signup(platform, "s2shortpw", "s2shortpw@example.com", password="pw")
    assert r.status_code in (400, 422), r.text


def test_signup_rejects_reserved_username(platform, platform_main):
    for name in ("admin", "Admin", "root", "system"):
        platform_main.limiter.reset()  # 登録は 3/hour 制限があるため都度リセット
        r = _signup(platform, name, f"s2{name.lower()}x@example.com")
        assert r.status_code in (400, 422), (name, r.text)


def test_signup_rejects_bad_username_chars(platform, platform_main):
    for name in ("bad name", "bad'name", "bad<script>", "あいうえお"):
        platform_main.limiter.reset()
        r = _signup(platform, name, "s2badchars@example.com")
        assert r.status_code in (400, 422), (name, r.text)


def test_signup_duplicate_checks_are_case_insensitive(platform):
    _make_user(platform, "S2CaseUser", "S2Case@Example.com")
    r = _signup(platform, "s2caseuser", "other1@example.com")
    assert r.status_code == 400, r.text
    r = _signup(platform, "s2caseuser2", "s2case@example.com")
    assert r.status_code == 400, r.text
    # 保存されたメールは小文字化されている
    r = _login(platform, "S2CaseUser")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 乙-1: アカウント停止が効く
# ---------------------------------------------------------------------------

def test_deactivated_user_cannot_login_or_use_token(platform, platform_main):
    headers = _make_user(platform, "s2victim", "s2victim@example.com")
    db = platform_main.SessionLocal()
    try:
        uid = db.query(platform_main.User).filter_by(
            username="s2victim").first().id
    finally:
        db.close()
    admin = _admin_headers(platform)
    r = platform.patch(f"/api/admin/users/{uid}/active",
                       json={"is_active": False}, headers=admin)
    assert r.status_code == 200 and r.json()["is_active"] is False

    # 停止後はログインできない
    r = _login(platform, "s2victim")
    assert r.status_code in (401, 403), r.text
    # 停止前に発行されたトークンも使えない
    r = platform.get("/api/author/works", headers=headers)
    assert r.status_code in (401, 403), r.text

    # 再開すれば元に戻る
    r = platform.patch(f"/api/admin/users/{uid}/active",
                       json={"is_active": True}, headers=admin)
    assert r.status_code == 200 and r.json()["is_active"] is True
    r = _login(platform, "s2victim")
    assert r.status_code == 200, r.text
    r = platform.get("/api/author/works", headers=headers)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 丙-10: sub が数字でない / 欠落なら 500 でなく 401
# ---------------------------------------------------------------------------

def _jwt(platform_main, claims):
    base = {"exp": datetime.now(UTC) + timedelta(minutes=5)}
    base.update(claims)
    return jose_jwt.encode(base, platform_main.SECRET_KEY, algorithm="HS256")


def test_sub_non_numeric_returns_401_not_500(platform, platform_main):
    bad = _jwt(platform_main, {"sub": "abc"})
    r = platform.get("/api/author/works",
                     headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401, r.status_code


def test_sub_missing_returns_401_not_500(platform, platform_main):
    bad = _jwt(platform_main, {})
    r = platform.get("/api/author/works",
                     headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401, r.status_code


# ---------------------------------------------------------------------------
# Y-3: CBZ トークン検証で exp 欠落は例外でなく False
# ---------------------------------------------------------------------------

def test_cbz_token_without_exp_returns_false(platform_main):
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps({"pid": "x", "nonce": "n-stage2"}).encode()
    ).decode().rstrip("=")
    sig = hmac_lib.new(platform_main.CBZ_TOKEN_SECRET.encode(),
                       payload_b64.encode(), hashlib.sha256).hexdigest()[:24]
    assert platform_main._verify_cbz_token(f"{payload_b64}.{sig}", "x") is False


# ---------------------------------------------------------------------------
# 丙-7: 内部キーの照合 (正しいキーは通り、間違いは 403 のまま)
# ---------------------------------------------------------------------------

def test_internal_key_check_accepts_correct_and_rejects_wrong(
        platform, platform_main):
    headers = _make_user(platform, "s2internal", "s2internal@example.com")
    pid = _publish(platform, platform_main, headers, 211, 211)
    r = platform.get("/api/internal/issue-cbz-token",
                     params={"public_id": pid},
                     headers={"x-internal-key": "test-internal-key"})
    assert r.status_code == 200, r.text
    r = platform.get("/api/internal/issue-cbz-token",
                     params={"public_id": pid},
                     headers={"x-internal-key": "wrong-key"})
    assert r.status_code == 403, r.text
    r = platform.get("/api/internal/issue-cbz-token",
                     params={"public_id": pid})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 乙-2: リアクションはサーバー側 IP で数え、偽装・二重・下書きを防ぐ
# ---------------------------------------------------------------------------

def test_reaction_ignores_forged_client_ip_and_dedupes(platform,
                                                       platform_main):
    headers = _make_user(platform, "s2react", "s2react@example.com")
    pid = _publish(platform, platform_main, headers, 221, 221)
    for forged in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        r = platform.post("/api/public/reaction", json={
            "public_id": pid, "reaction_type": "いいね",
            "client_ip": forged})
        assert r.status_code == 200, r.text
    # 本文の IP が違っても同一接続元なので 1 票だけ
    assert r.json()["added"] is False
    assert r.json()["counts"]["いいね"] == 1


def test_reaction_on_draft_is_rejected(platform, platform_main):
    headers = _make_user(platform, "s2draft", "s2draft@example.com")
    pid = _publish(platform, platform_main, headers, 222, 222, status="draft")
    r = platform.post("/api/public/reaction", json={
        "public_id": pid, "reaction_type": "いいね", "client_ip": ""})
    assert r.status_code in (400, 403, 404), r.status_code
    r = platform.get(f"/api/public/reactions/{pid}")
    assert r.json()["counts"].get("いいね", 0) == 0


# ---------------------------------------------------------------------------
# N-13: プレビュー専用トークン
# ---------------------------------------------------------------------------

def _make_cbz_and_thumb(platform_main, headers_user_id, title_id, episode_id):
    user_dir = (platform_main.FINAL_ZIP_DIR / f"user_{headers_user_id}"
                / f"title_{title_id}")
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / f"episode_{episode_id}.cbz").write_bytes(b"PK dummy cbz")
    thumbs = user_dir / f"episode_{episode_id}_thumbs"
    thumbs.mkdir(exist_ok=True)
    (thumbs / "thumb_0000.jpg").write_bytes(b"\xff\xd8\xff thumb")
    return user_dir


def _user_id(platform_main, username):
    db = platform_main.SessionLocal()
    try:
        return db.query(platform_main.User).filter_by(
            username=username).first().id
    finally:
        db.close()


def test_preview_requires_dedicated_token(platform, platform_main):
    headers = _make_user(platform, "s2preview", "s2preview@example.com")
    uid = _user_id(platform_main, "s2preview")
    pid = _publish(platform, platform_main, headers, 231, 231, status="draft")
    _make_cbz_and_thumb(platform_main, uid, 231, 231)

    # ログイン JWT そのものではプレビューできない
    login_jwt = headers["Authorization"].split()[1]
    r = platform.get(f"/api/author/preview/{pid}.cbz",
                     params={"token": login_jwt})
    assert r.status_code == 401, r.status_code
    r = platform.get(f"/api/author/thumb/{pid}/0",
                     params={"token": login_jwt})
    assert r.status_code == 401, r.status_code

    # 専用トークンを発行して使うと通る
    r = platform.post(f"/api/author/episodes/{pid}/preview-token",
                      headers=headers)
    assert r.status_code == 200, r.text
    ptoken = r.json()["preview_token"]
    assert ptoken != login_jwt
    r = platform.get(f"/api/author/preview/{pid}.cbz",
                     params={"token": ptoken})
    assert r.status_code == 200, r.status_code
    r = platform.get(f"/api/author/thumb/{pid}/0",
                     params={"token": ptoken})
    assert r.status_code == 200, r.status_code

    # 別作品の public_id には流用できない
    pid2 = _publish(platform, platform_main, headers, 232, 232, status="draft")
    _make_cbz_and_thumb(platform_main, uid, 232, 232)
    r = platform.get(f"/api/author/preview/{pid2}.cbz",
                     params={"token": ptoken})
    assert r.status_code == 401, r.status_code

    # 他人の作品には専用トークンを発行できない
    other = _make_user(platform, "s2preview2", "s2preview2@example.com")
    r = platform.post(f"/api/author/episodes/{pid}/preview-token",
                      headers=other)
    assert r.status_code in (403, 404), r.status_code


def test_preview_token_bad_sub_is_401(platform, platform_main):
    bad = _jwt(platform_main, {"sub": "abc", "purpose": "preview",
                               "pid": "whatever"})
    r = platform.get("/api/author/preview/whatever.cbz",
                     params={"token": bad})
    assert r.status_code == 401, r.status_code


# ---------------------------------------------------------------------------
# N-5: view_count の正本は DB。カタログと管理画面で一致する
# ---------------------------------------------------------------------------

def test_view_count_consistent_between_catalog_and_admin(platform,
                                                         platform_main):
    headers = _make_user(platform, "s2views", "s2views@example.com")
    uid = _user_id(platform_main, "s2views")
    pid = _publish(platform, platform_main, headers, 241, 241)
    _make_cbz_and_thumb(platform_main, uid, 241, 241)

    # CBZ を 1 回取得する (内部キーで CBZ トークンを取り、Referer を付ける)
    r = platform.get("/api/internal/issue-cbz-token",
                     params={"public_id": pid},
                     headers={"x-internal-key": "test-internal-key"})
    assert r.status_code == 200, r.text
    cbz_token = r.json()["cbz_token"]
    viewer_base = platform_main.get_viewer_base_url()
    r = platform.get(f"/api/public/cbz/{pid}.cbz",
                     params={"cbz_token": cbz_token},
                     headers={"Referer": f"{viewer_base}/reader"})
    assert r.status_code == 200, r.status_code

    catalog = platform.get("/api/public/catalog").json()
    entry = next(i for i in catalog if i["public_id"] == pid)
    admin = _admin_headers(platform)
    episodes = platform.get("/api/admin/episodes", headers=admin).json()
    aentry = next(e for e in episodes if e["public_id"] == pid)
    assert entry["view_count"] == 1, entry
    assert aentry["view_count"] == 1, aentry
    assert entry["view_count"] == aentry["view_count"]


# ---------------------------------------------------------------------------
# 甲-2: 管理者ログイン (正しいパスワードは通り、間違いは断られる)
# ---------------------------------------------------------------------------

def test_admin_login_still_works(platform):
    r = platform.post("/api/admin/token", json={"password": "test-admin-pass"})
    assert r.status_code == 200, r.text
    r = platform.post("/api/admin/token", json={"password": "wrong-pass"})
    assert r.status_code == 401, r.text


def test_old_hardcoded_keys_do_not_work(platform, platform_main):
    """甲-1/甲-2: 旧来の直書き鍵・既定鍵で署名したトークンは通らない。"""
    forged = jose_jwt.encode(
        {"sub": "1", "exp": datetime.now(UTC) + timedelta(minutes=5)},
        "MANGA_PLATFORM_SUPER_SECRET_KEY", algorithm="HS256")
    r = platform.get("/api/author/works",
                     headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401, r.status_code
    forged_admin = jose_jwt.encode(
        {"role": "admin", "exp": datetime.now(UTC) + timedelta(hours=1)},
        "ADMIN_SUPER_SECRET_CHANGE_THIS", algorithm="HS256")
    r = platform.get("/api/admin/users",
                     headers={"Authorization": f"Bearer {forged_admin}"})
    assert r.status_code in (401, 403), r.status_code
