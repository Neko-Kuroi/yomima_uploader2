"""段階3 (プラットフォーム側): 乙-3, 乙-5, 乙-6, 乙-7, 丙-1, 丙-2,
丙-4, N-6, N-7, N-14, N-16。

- 乙-3: 他人の作品/話番号との衝突は 500 でなく 409。所有者確認も行う
- 乙-5: access_level != public は公開配信しない (所有者プレビューは可)
- 乙-6: 下書きカバーは公開受け口で返さない。作者用受け口を別に用意する
- 乙-7: CORS は全開にしない (環境変数で許可したオリジンだけ)
- 丙-1: サイズ制限は Content-Length が無くても効き、数字でない値は 400
- 丙-2: tile_size の範囲外は入口で 400
- 丙-4: settings.json は原子的に書き、壊れたら退避して表に出す
- N-6:  public_thumb は設定ファイルが無いと配信しない (fail-closed)
- N-7:  設定保存は部分更新 (送られなかった項目は既存値を保つ)
- N-14: 画素数の上限をサーバー側でも確認する
- N-16: ファイル名に / が含まれてもジョブが落ちない
"""
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image


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
    r = _signup(platform, username, email)
    assert r.status_code == 200, r.text
    r = platform.post("/api/auth/token", data={
        "username": username, "password": "<redacted>"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _user_id(platform_main, username):
    db = platform_main.SessionLocal()
    try:
        return db.query(platform_main.User).filter_by(
            username=username).first().id
    finally:
        db.close()


def _payload(**over):
    base = {
        "status": "published", "access_level": "public", "price": 0,
        "title_name": "T", "episode_name": "E", "caption": "",
        "comment": "", "note": "", "feedback": "",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "rating": "General", "warnings": [], "genre_tags": ["日常"],
        "motif_tags": [],
    }
    base.update(over)
    return base


def _patch_settings(platform, headers, title_id, episode_id, **over):
    return platform.patch(
        f"/api/author/titles/{title_id}/episodes/{episode_id}/settings",
        json=_payload(**over), headers=headers)


def _public_id(platform_main, episode_id):
    db = platform_main.SessionLocal()
    try:
        ep = db.query(platform_main.Episode).filter_by(id=episode_id).first()
        return ep.public_id if ep else None
    finally:
        db.close()


def _make_files(platform_main, uid, title_id, episode_id, cover=False):
    user_dir = (platform_main.FINAL_ZIP_DIR / f"user_{uid}"
                / f"title_{title_id}")
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / f"episode_{episode_id}.cbz").write_bytes(b"PK dummy cbz")
    thumbs = user_dir / f"episode_{episode_id}_thumbs"
    thumbs.mkdir(exist_ok=True)
    (thumbs / "thumb_0000.jpg").write_bytes(b"\xff\xd8\xff thumb")
    if cover:
        (user_dir / f"episode_{episode_id}_cover.jpg").write_bytes(
            b"\xff\xd8\xff cover-image")
    return user_dir


def _settings_path(platform_main, uid, title_id, episode_id):
    return (platform_main.FINAL_ZIP_DIR / f"user_{uid}" / f"title_{title_id}"
            / f"episode_{episode_id}_settings.json")


def _png_bytes(w=100, h=150, color=(10, 20, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _upload(platform, headers, title_id, episode_id, files,
            tile_size=16):
    return platform.post(
        f"/api/author/titles/{title_id}/episodes/{episode_id}/upload-and-zip",
        params={"do_scramble": True, "tile_size": tile_size},
        files=files, headers=headers)


def _wait_job(platform, headers, job_id, timeout=30.0):
    deadline = time.time() + timeout
    phase, last = None, {}
    while time.time() < deadline:
        r = platform.get(f"/api/author/upload-progress/{job_id}",
                         headers=headers)
        last = r.json()
        phase = last.get("phase")
        if phase in ("done", "error"):
            break
        time.sleep(0.2)
    return phase, last


# ---------------------------------------------------------------------------
# 乙-3: 番号の衝突は 500 でなく 409。他人の番号は使えない
# ---------------------------------------------------------------------------

def test_settings_conflict_with_other_users_ids_returns_409(
        platform, platform_main):
    owner = _make_user(platform, "s3owner1", "s3owner1@example.com")
    r = _patch_settings(platform, owner, 311, 311)
    assert r.status_code == 200, r.text

    other = _make_user(platform, "s3owner2", "s3owner2@example.com")
    # 他人の title_id + episode_id をそのまま使う
    r = _patch_settings(platform, other, 311, 311)
    assert r.status_code == 409, (r.status_code, r.text)
    # 他人の title_id に自分の新しい episode_id をぶつける
    r = _patch_settings(platform, other, 311, 319)
    assert r.status_code == 409, (r.status_code, r.text)
    # 自分の title_id に他人の episode_id をぶつける
    r = _patch_settings(platform, other, 319, 311)
    assert r.status_code == 409, (r.status_code, r.text)
    # 本人は同じ番号へ再保存できる (正常系)
    r = _patch_settings(platform, owner, 311, 311)
    assert r.status_code == 200, r.text


def test_upload_conflict_with_other_users_ids_returns_409(
        platform, platform_main):
    owner = {"Authorization": None}
    # 311/311 は上のテストで s3owner1 が所有済み。前提が無い環境では作る
    db = platform_main.SessionLocal()
    try:
        exists = db.query(platform_main.Work).filter_by(id=311).first()
    finally:
        db.close()
    if not exists:
        owner = _make_user(platform, "s3owner1b", "s3owner1b@example.com")
        assert _patch_settings(platform, owner, 311, 311).status_code == 200

    other = _make_user(platform, "s3owner3", "s3owner3@example.com")
    png = _png_bytes()
    r = _upload(platform, other, 311, 311,
                [("files", ("p1.png", png, "image/png"))])
    assert r.status_code == 409, (r.status_code, r.text)


# ---------------------------------------------------------------------------
# 乙-5: access_level が public でない作品は公開配信しない
# ---------------------------------------------------------------------------

def test_premium_episode_is_not_publicly_delivered(platform, platform_main):
    headers = _make_user(platform, "s3prem", "s3prem@example.com")
    uid = _user_id(platform_main, "s3prem")
    r = _patch_settings(platform, headers, 321, 321,
                        access_level="premium")
    assert r.status_code == 200, r.text
    pid = _public_id(platform_main, 321)
    _make_files(platform_main, uid, 321, 321, cover=True)

    # 公開カバー・サムネイルは未認証で取れない
    r = platform.get(f"/api/public/cover/{pid}")
    assert r.status_code in (403, 404), r.status_code
    r = platform.get(f"/api/public/thumb/{pid}/0")
    assert r.status_code in (403, 404), r.status_code
    # ビューワーへの CBZ トークン発行も断られる
    r = platform.get("/api/internal/issue-cbz-token",
                     params={"public_id": pid},
                     headers={"x-internal-key": "test-internal-key"})
    assert r.status_code in (403, 404), r.status_code

    # カタログの一覧には (PREMIUM 表示として) 残る
    catalog = platform.get("/api/public/catalog").json()
    entry = next((i for i in catalog if i["public_id"] == pid), None)
    assert entry is not None, "premium 作品がカタログから消えた"
    assert entry["access_level"] == "premium"

    # 作者本人のプレビューは従来どおり使える
    r = platform.post(f"/api/author/episodes/{pid}/preview-token",
                      headers=headers)
    assert r.status_code == 200, r.text
    token = r.json()["preview_token"]
    r = platform.get(f"/api/author/preview/{pid}.cbz",
                     params={"token": token})
    assert r.status_code == 200, r.status_code


# ---------------------------------------------------------------------------
# 乙-6: 下書きカバーは公開受け口で返さない / 作者用受け口を別に用意
# ---------------------------------------------------------------------------

def test_draft_cover_blocked_publicly_but_author_can_see_it(
        platform, platform_main):
    headers = _make_user(platform, "s3cover", "s3cover@example.com")
    uid = _user_id(platform_main, "s3cover")
    r = _patch_settings(platform, headers, 322, 322, status="draft")
    assert r.status_code == 200, r.text
    pid = _public_id(platform_main, 322)
    _make_files(platform_main, uid, 322, 322, cover=True)

    # 公開受け口: 下書きは未認証で取れない
    r = platform.get(f"/api/public/cover/{pid}")
    assert r.status_code in (403, 404), r.status_code

    # 作者用受け口: トークン無しでは取れない
    r = platform.get(f"/api/author/cover/{pid}")
    assert r.status_code in (401, 403), r.status_code

    # 作者本人のプレビュー用トークンでは自分の下書きカバーが見える
    r = platform.post(f"/api/author/episodes/{pid}/preview-token",
                      headers=headers)
    assert r.status_code == 200, r.text
    token = r.json()["preview_token"]
    r = platform.get(f"/api/author/cover/{pid}", params={"token": token})
    assert r.status_code == 200, r.status_code
    assert r.content == b"\xff\xd8\xff cover-image"

    # 別作品のトークンでは流用できない
    r = _patch_settings(platform, headers, 325, 325, status="draft")
    assert r.status_code == 200, r.text
    pid2 = _public_id(platform_main, 325)
    r = platform.post(f"/api/author/episodes/{pid2}/preview-token",
                      headers=headers)
    token2 = r.json()["preview_token"]
    r = platform.get(f"/api/author/cover/{pid}", params={"token": token2})
    assert r.status_code == 401, r.status_code


# ---------------------------------------------------------------------------
# N-6: public_thumb は設定ファイルが無いと配信しない (fail-closed)
# ---------------------------------------------------------------------------

def test_public_thumb_fail_closed_when_settings_missing(
        platform, platform_main):
    headers = _make_user(platform, "s3thumb", "s3thumb@example.com")
    uid = _user_id(platform_main, "s3thumb")
    r = _patch_settings(platform, headers, 323, 323)
    assert r.status_code == 200, r.text
    pid = _public_id(platform_main, 323)
    _make_files(platform_main, uid, 323, 323)

    # 正常系: 設定がある間は取れる
    r = platform.get(f"/api/public/thumb/{pid}/0")
    assert r.status_code == 200, r.status_code

    # 設定ファイルが消えたら、公開扱いに倒れず配信しない
    _settings_path(platform_main, uid, 323, 323).unlink()
    r = platform.get(f"/api/public/thumb/{pid}/0")
    assert r.status_code in (403, 404), r.status_code


# ---------------------------------------------------------------------------
# 乙-7: CORS は全開にしない
# ---------------------------------------------------------------------------

def test_cors_wildcard_is_not_returned_by_default(platform):
    r = platform.get("/api/public/catalog",
                     headers={"Origin": "https://evil.example"})
    assert r.headers.get("access-control-allow-origin") != "*"
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------------
# 丙-1: アップロードサイズ制限は Content-Length 頼みにしない
# ---------------------------------------------------------------------------

def _middleware_client(platform_main):
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route(
        "/api/author/titles/1/episodes/1/upload-and-zip", ok,
        methods=["POST"])])
    app.add_middleware(platform_main.LimitUploadSizeMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def test_size_middleware_counts_actual_bytes_without_content_length(
        platform_main):
    client = _middleware_client(platform_main)
    limit = platform_main.MAX_TOTAL_SIZE_BYTES + 5 * 1024 * 1024

    def gen():
        sent = 0
        chunk = b"x" * (1024 * 1024)
        while sent <= limit:
            yield chunk
            sent += len(chunk)

    # Content-Length を付けない (チャンク転送) でも、実バイト数で止まる
    r = client.post("/api/author/titles/1/episodes/1/upload-and-zip",
                    content=gen())
    assert r.status_code == 413, r.status_code


def test_size_middleware_rejects_non_numeric_content_length(platform_main):
    client = _middleware_client(platform_main)
    r = client.post("/api/author/titles/1/episodes/1/upload-and-zip",
                    content=b"hello",
                    headers={"Content-Length": "abc"})
    assert r.status_code in (400, 413), r.status_code
    assert r.status_code != 500, r.status_code


def test_size_middleware_passes_small_requests(platform_main):
    client = _middleware_client(platform_main)
    r = client.post("/api/author/titles/1/episodes/1/upload-and-zip",
                    content=b"small body")
    assert r.status_code == 200, r.status_code
    # 上限超えの申告は従来どおり 413
    limit = platform_main.MAX_TOTAL_SIZE_BYTES + 5 * 1024 * 1024
    r = client.post("/api/author/titles/1/episodes/1/upload-and-zip",
                    content=b"small",
                    headers={"Content-Length": str(limit + 1)})
    assert r.status_code == 413, r.status_code


# ---------------------------------------------------------------------------
# 丙-2: tile_size の範囲外は入口で 400
# ---------------------------------------------------------------------------

def test_tile_size_out_of_range_rejected_at_entry(platform, platform_main):
    headers = _make_user(platform, "s3tile", "s3tile@example.com")
    png = _png_bytes()
    for bad in (0, 1, 99999):
        r = _upload(platform, headers, 341, 341,
                    [("files", ("p1.png", png, "image/png"))],
                    tile_size=bad)
        assert r.status_code == 400, (bad, r.status_code, r.text)
    # 設定保存でも同じ
    for bad in (0, 99999):
        r = _patch_settings(platform, headers, 351, 351, tile_size=bad)
        assert r.status_code == 400, (bad, r.status_code, r.text)
    # 範囲内は通る
    r = _patch_settings(platform, headers, 351, 351, tile_size=4)
    assert r.status_code == 200, r.text
    r = _patch_settings(platform, headers, 351, 351, tile_size=256)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 丙-4: settings.json の安全な読み書き
# ---------------------------------------------------------------------------

def test_settings_write_is_atomic_and_readable(platform_main, tmp_path):
    target = tmp_path / "episode_1_settings.json"
    platform_main.write_settings_json(target, {"a": 1, "status": "draft"})
    assert json.loads(target.read_text()) == {"a": 1, "status": "draft"}
    platform_main.write_settings_json(target, {"a": 2, "status": "published"})
    assert json.loads(target.read_text())["a"] == 2
    # 一時ファイルが残っていない
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name != target.name]
    assert leftovers == [], leftovers
    # 読み側ヘルパー: 無い場合は None、壊れている場合は専用例外
    assert platform_main.read_settings_json(tmp_path / "none.json") is None
    target.write_text("{broken json")
    with pytest.raises(platform_main.SettingsCorruptError):
        platform_main.read_settings_json(target)


def test_corrupt_settings_is_backed_up_not_silently_dropped(
        platform, platform_main):
    headers = _make_user(platform, "s3corrupt", "s3corrupt@example.com")
    uid = _user_id(platform_main, "s3corrupt")
    r = _patch_settings(platform, headers, 324, 324,
                        title_name="S3CorruptWork")
    assert r.status_code == 200, r.text
    path = _settings_path(platform_main, uid, 324, 324)
    path.write_text("{this is not json")

    # カタログ取得が落ちないこと。壊れたファイルは退避されること
    r = platform.get("/api/public/catalog")
    assert r.status_code == 200, r.status_code
    backups = list(path.parent.glob(path.name + ".corrupt*"))
    assert backups, "壊れた settings.json が退避されていない"
    assert "{this is not json" in backups[0].read_text()

    # 作者一覧も落ちない
    r = platform.get("/api/author/works", headers=headers)
    assert r.status_code == 200, r.status_code


# ---------------------------------------------------------------------------
# N-7: 設定保存は部分更新 (送られなかった項目は既存値を保つ)
# ---------------------------------------------------------------------------

def test_partial_settings_update_preserves_unset_fields(
        platform, platform_main):
    headers = _make_user(platform, "s3edit", "s3edit@example.com")
    uid = _user_id(platform_main, "s3edit")
    r = _patch_settings(
        platform, headers, 331, 331,
        rating="Adult", warnings=["暴力"], genre_tags=["日常", "冒険"],
        motif_tags=["猫"], feedback="直してほしい点",
        scrambled=False, tile_size=32, caption="最初の説明")
    assert r.status_code == 200, r.text

    # 編集モーダル相当の最小ペイロード (rating やタグ類を含まない)
    minimal = {
        "status": "published", "access_level": "public",
        "title_name": "T", "episode_name": "E",
        "caption": "書き換えた説明", "comment": "", "note": "",
        "webhook_enabled": False, "price": 0,
    }
    r = platform.patch(
        "/api/author/titles/331/episodes/331/settings",
        json=minimal, headers=headers)
    assert r.status_code == 200, r.text

    meta = json.loads(
        _settings_path(platform_main, uid, 331, 331).read_text())
    assert meta["caption"] == "書き換えた説明"      # 送った項目は更新される
    assert meta["rating"] == "Adult", meta          # 送らない項目は保たれる
    assert meta["warnings"] == ["暴力"], meta
    assert meta["genre_tags"] == ["日常", "冒険"], meta
    assert meta["motif_tags"] == ["猫"], meta
    assert meta["feedback"] == "直してほしい点", meta
    assert meta["scrambled"] is False, meta
    assert meta["tile_size"] == 32, meta

    # DB 側も消えていない
    db = platform_main.SessionLocal()
    try:
        ep = db.query(platform_main.Episode).filter_by(id=331).first()
        assert ep.rating == "Adult"
        assert ep.feedback == "直してほしい点"
        assert ep.scrambled is False
    finally:
        db.close()


def test_frontend_save_edit_sends_current_values():
    """画面側 saveEdit が固定値 (scrambled:true / tile_size:16) を送らず、
    現在の rating・タグ・feedback 等を送り返す形になっていること。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(
        root, "mangaupload_yomima_site", "static", "index.html"),
        encoding="utf-8").read()
    start = html.index("async function saveEdit()")
    body = html[start:html.index("\n}", start)]
    # かつての固定値ペア (scrambled: true, tile_size: 16) が残っていない
    assert "scrambled:true,tile_size:16" not in body.replace(" ", "")
    assert "rating" in body
    assert "feedback" in body
    assert "_editWork" in body
    # マイ作品のカバーは公開受け口ではなく作者用受け口を使う
    assert "/api/author/cover/" in html


# ---------------------------------------------------------------------------
# N-14: 画素数の上限
# ---------------------------------------------------------------------------

def test_upload_rejects_image_with_too_many_pixels(
        platform, platform_main):
    headers = _make_user(platform, "s3pixel", "s3pixel@example.com")
    # 6000x5000 = 3000万画素。単色なのでバイト数は小さい (展開爆弾型)
    big = _png_bytes(6000, 5000, (7, 7, 7))
    assert len(big) < platform_main.MAX_PAGE_SIZE_BYTES
    r = _upload(platform, headers, 342, 342,
                [("files", ("big.png", big, "image/png"))])
    assert r.status_code == 200, r.text
    phase, info = _wait_job(platform, headers, r.json()["job_id"])
    assert phase == "error", info
    assert "画素" in str(info.get("message", "")) or \
        "pixel" in str(info.get("message", "")).lower(), info


# ---------------------------------------------------------------------------
# N-16: ファイル名に / が含まれてもジョブが落ちない
# ---------------------------------------------------------------------------

def test_upload_filename_with_slash_completes(platform, platform_main):
    headers = _make_user(platform, "s3slash", "s3slash@example.com")
    png = _png_bytes()
    r = _upload(platform, headers, 343, 343,
                [("files", ("sub/p1.png", png, "image/png"))])
    assert r.status_code == 200, r.text
    phase, info = _wait_job(platform, headers, r.json()["job_id"])
    assert phase == "done", info
    assert info.get("page_count") == 1, info
