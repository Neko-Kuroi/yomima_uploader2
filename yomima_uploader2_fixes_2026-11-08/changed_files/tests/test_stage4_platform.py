"""段階4 (プラットフォーム側): 丙-5, 丙-6, N-19, N-21 と、
N-4 の土台になる内部プレビュー設定の受け口。

- 丙-5: カタログは全 settings.json の再読みを毎回しない (メモ化)。
        ただし設定保存・公開切替・閲覧数の変化は即座に反映される
- 丙-6: CBZ 配信の Referer 検査はオリジン完全一致。
        デバッグログに CBZ トークンそのものを残さない
- N-19: カタログ応答に title_id / episode_id を含める
- N-21: アップロード進捗は所有者だけが引ける
- N-4:  ビューワーがプレビュー作品の設定 (scrambled / tile_size) を
        プレビュー専用トークン経由で取得できる内部受け口
"""
import logging

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def platform(platform_main):
    return TestClient(platform_main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_limiters(platform_main):
    try:
        platform_main.limiter.reset()
    except Exception:
        pass
    yield


def _signup(platform, username, email, password="<redacted>"):
    return platform.post("/api/auth/signup", json={
        "username": username, "email": email, "password": password})


def _make_user(platform, username, email):
    r = _signup(platform, username, email, "stage4-password")
    assert r.status_code == 200, r.text
    r = platform.post("/api/auth/token", data={
        "username": username, "password": "stage4-password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _user_id(platform_main, username):
    db = platform_main.SessionLocal()
    try:
        return db.query(platform_main.User).filter_by(
            username=username).first().id
    finally:
        db.close()


def _settings_payload(title_id, episode_id, **over):
    payload = {
        "status": "published", "access_level": "public", "price": 0,
        "title_name": f"T{title_id}", "episode_name": f"E{episode_id}",
        "caption": "", "comment": "", "note": "", "feedback": "",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "rating": "General", "warnings": [], "genre_tags": ["日常"],
        "motif_tags": [],
    }
    payload.update(over)
    return payload


def _publish(platform, platform_main, headers, title_id, episode_id, **over):
    r = platform.patch(
        f"/api/author/titles/{title_id}/episodes/{episode_id}/settings",
        json=_settings_payload(title_id, episode_id, **over), headers=headers)
    assert r.status_code == 200, r.text
    db = platform_main.SessionLocal()
    try:
        ep = db.query(platform_main.Episode).filter_by(id=episode_id).first()
        return ep.public_id
    finally:
        db.close()


def _catalog_entry(platform, public_id):
    r = platform.get("/api/public/catalog")
    assert r.status_code == 200, r.text
    return next((i for i in r.json() if i["public_id"] == public_id), None)


# ---------------------------------------------------------------------------
# N-19: カタログに title_id / episode_id が含まれる
# ---------------------------------------------------------------------------

def test_catalog_includes_title_and_episode_ids(platform, platform_main):
    headers = _make_user(platform, "s4catalog", "s4catalog@example.com")
    pid = _publish(platform, platform_main, headers, 411, 411,
                   title_name="", episode_name="")
    entry = _catalog_entry(platform, pid)
    assert entry is not None
    # 名前が空でも画面の代用表示が undefined にならないよう ID が返る
    assert entry["title_id"] == 411
    assert entry["episode_id"] == 411


# ---------------------------------------------------------------------------
# 丙-5: カタログのメモ化と鮮度
# ---------------------------------------------------------------------------

def test_catalog_is_cached_but_not_stale(platform, platform_main,
                                         monkeypatch):
    headers = _make_user(platform, "s4cache", "s4cache@example.com")
    pid = _publish(platform, platform_main, headers, 421, 421)

    calls = {"n": 0}
    orig = platform_main.read_settings_for_listing

    def counting(path):
        calls["n"] += 1
        return orig(path)

    monkeypatch.setattr(platform_main, "read_settings_for_listing", counting)

    entry = _catalog_entry(platform, pid)
    assert entry is not None and entry["title_name"] == "T421"
    first_calls = calls["n"]
    assert first_calls > 0

    # 2回目は全ファイルを読み直さない (メモから返す)
    entry = _catalog_entry(platform, pid)
    assert entry is not None
    assert calls["n"] == first_calls, "カタログが毎回全ファイルを読み直している"

    # 設定保存 (タイトル変更) の直後は新しい内容になる
    _publish(platform, platform_main, headers, 421, 421,
             title_name="T421-renamed")
    entry = _catalog_entry(platform, pid)
    assert entry is not None and entry["title_name"] == "T421-renamed"

    # 公開を下書きに戻すと、カタログから即座に消える
    _publish(platform, platform_main, headers, 421, 421, status="draft")
    assert _catalog_entry(platform, pid) is None


# ---------------------------------------------------------------------------
# 丙-6: Referer はオリジン完全一致。トークンをログに残さない
# ---------------------------------------------------------------------------

def test_referer_origin_matching(platform_main):
    check = platform_main._referer_is_viewer
    base = "http://localhost:8001"
    assert check("http://localhost:8001/reader?url=x", base)
    assert check("http://localhost:8001", base)
    # 前方一致だと通ってしまう書き出しが同じ別ホスト
    assert not check("http://localhost:8001.evil.com/reader", base)
    # ユーザー情報部に紛れ込ませる形
    assert not check("http://localhost:8001@evil.com/reader", base)
    # ポート違い・スキーム違い・Referer 無し
    assert not check("http://localhost:9999/reader", base)
    assert not check("https://localhost:8001/reader", base)
    assert not check(None, base)
    assert not check("", base)


def test_cbz_rejects_same_prefix_referer(platform, platform_main):
    headers = _make_user(platform, "s4referer", "s4referer@example.com")
    pid = _publish(platform, platform_main, headers, 431, 431)
    viewer_base = platform_main.get_viewer_base_url()
    evil = viewer_base + ".evil.com/reader"
    r = platform.get(f"/api/public/cbz/{pid}.cbz",
                     params={"cbz_token": "dummy"},
                     headers={"Referer": evil})
    assert r.status_code == 403, r.status_code


def test_cbz_token_is_not_written_to_logs(platform, platform_main, caplog):
    headers = _make_user(platform, "s4logmask", "s4logmask@example.com")
    pid = _publish(platform, platform_main, headers, 441, 441)
    viewer_base = platform_main.get_viewer_base_url()
    secret_token = "<redacted>"
    with caplog.at_level(logging.INFO):
        r = platform.get(f"/api/public/cbz/{pid}.cbz",
                         params={"cbz_token": secret_token},
                         headers={"Referer": f"{viewer_base}/reader"})
    assert r.status_code == 403  # トークン自体は無効なので当然 403
    assert secret_token not in caplog.text, "CBZトークンがログに残っている"


def test_mask_token_helper(platform_main):
    masked = platform_main._mask_token("abcdefghijklmnop")
    assert "abcdefghijklmnop" not in masked
    assert masked.startswith("abcd")
    assert platform_main._mask_token("") == "(none)"
    assert platform_main._mask_token(None) == "(none)"


# ---------------------------------------------------------------------------
# N-21: アップロード進捗の所有者確認
# ---------------------------------------------------------------------------

def test_upload_progress_requires_owner(platform, platform_main):
    owner = _make_user(platform, "s4progowner", "s4progowner@example.com")
    other = _make_user(platform, "s4progother", "s4progother@example.com")
    owner_id = _user_id(platform_main, "s4progowner")
    platform_main.upload_progress["s4-job-1"] = {
        "phase": "resize", "current": 1, "total": 3, "owner_id": owner_id}
    try:
        r = platform.get("/api/author/upload-progress/s4-job-1",
                         headers=other)
        assert r.status_code in (403, 404), r.status_code

        r = platform.get("/api/author/upload-progress/s4-job-1",
                         headers=owner)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["phase"] == "resize"
        # 内部の所有者情報は応答に含めない
        assert "owner_id" not in body
    finally:
        platform_main.upload_progress.pop("s4-job-1", None)


# ---------------------------------------------------------------------------
# N-4 (プラットフォーム側): プレビュー設定の内部受け口
# ---------------------------------------------------------------------------

def test_internal_preview_settings(platform, platform_main):
    headers = _make_user(platform, "s4prevset", "s4prevset@example.com")
    pid = _publish(platform, platform_main, headers, 451, 451,
                   status="draft", scrambled=False, tile_size=32)
    r = platform.post(f"/api/author/episodes/{pid}/preview-token",
                      headers=headers)
    assert r.status_code == 200, r.text
    ptoken = r.json()["preview_token"]

    url = "/api/internal/preview-settings"
    # 内部キーが無い・違うと断る
    r = platform.get(url, params={"public_id": pid, "token": ptoken})
    assert r.status_code == 403, r.status_code
    r = platform.get(url, params={"public_id": pid, "token": ptoken},
                     headers={"x-internal-key": "wrong"})
    assert r.status_code == 403, r.status_code
    # プレビュートークンが違うと断る
    r = platform.get(url, params={"public_id": pid, "token": "bogus"},
                     headers={"x-internal-key": "test-internal-key"})
    assert r.status_code == 401, r.status_code
    # 正常系: 設定がそのまま返る
    r = platform.get(url, params={"public_id": pid, "token": ptoken},
                     headers={"x-internal-key": "test-internal-key"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["scrambled"] is False
    assert data["tile_size"] == 32
