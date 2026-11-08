"""甲-3 (/discord/send) と 甲-4 (/thumbnail, /images) のテスト。

- /discord/send: filename のパストラバーサル拒否、webhook の送り先制限、
  丙-9 (エラー表示のエスケープ)
- /thumbnail: セッション必須化、thumb_url は PLATFORM_BASE_URL からのみ
  組み立てる (SSRF 防止)、返す Content-Type は画像系のみ
- /images: セッション必須化。reader の通常利用 (セッション付き) は壊さない
"""
import hashlib
import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture(scope="module")
def viewer(viewer_main):
    return TestClient(viewer_main.app, raise_server_exceptions=False)


@pytest.fixture()
def external_work(manga, cache_dir):
    """外部 ZIP をビューワーのキャッシュに置いた状態を作る (監査 test6 と同じ形)。"""
    url = "http://example.com/ext.zip"
    buf = io.BytesIO()
    Image.new("RGB", (120, 180), (200, 30, 30)).save(buf, "PNG")
    png = buf.getvalue()
    zp = manga.get_cache_path(url) + ".zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("page1.png", png)
    ep = manga.get_cache_path(url) + "_extracted"
    files, _ = manga.extract_archive(zp, ep)
    token = manga.create_session(url, "1.2.3.4")
    return {
        "url": url,
        "token": token,
        "url_hash": hashlib.md5(url.encode()).hexdigest(),
        "extracted": ep,
        "filename": os.path.basename(files[0]),
    }


# ---------------------------------------------------------------------------
# /images
# ---------------------------------------------------------------------------

def test_images_without_session_is_rejected(viewer, external_work):
    r = viewer.get("/images", params={
        "url": external_work["url"], "offset": 0, "limit": 1})
    assert r.status_code in (401, 403), r.status_code


def test_images_with_session_still_works(viewer, external_work):
    r = viewer.get("/images", params={
        "url": external_work["url"], "offset": 0, "limit": 1,
        "session_token": external_work["token"]})
    assert r.status_code == 200, r.status_code


def test_images_with_wrong_url_session_is_rejected(viewer, external_work):
    r = viewer.get("/images", params={
        "url": "http://example.com/other.zip", "offset": 0, "limit": 1,
        "session_token": external_work["token"]})
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# /thumbnail
# ---------------------------------------------------------------------------

def test_thumbnail_without_session_is_rejected(viewer, external_work):
    r = viewer.get("/thumbnail", params={
        "url": external_work["url"], "index": 0})
    assert r.status_code in (401, 403), r.status_code


def test_thumbnail_with_session_fallback_still_works(viewer, external_work):
    r = viewer.get("/thumbnail", params={
        "url": external_work["url"], "index": 0,
        "session_token": external_work["token"]})
    assert r.status_code == 200, r.status_code
    assert r.headers["content-type"].startswith("image/")


def test_thumbnail_does_not_fetch_arbitrary_host(viewer, external_work):
    """甲-4 の再現形: URL の切り貼りで内部ホストを取りに行かせない。

    攻撃URLの中のホストは無視され、組み立て先は PLATFORM_BASE_URL
    (テストでは何も動いていない localhost:8000) だけになるため、
    秘密の本文が返ってくることはない。
    """
    evil = "http://127.0.0.1:9/secret?/api/public/cbz/x.cbz"
    r = viewer.get("/thumbnail", params={
        "url": evil, "index": 0,
        "session_token": external_work["token"]})
    # セッションの URL と一致しないので 403、でなければ少なくとも
    # 任意ホスト由来の 200 にはならない
    assert r.status_code in (400, 403, 404, 502), r.status_code


def test_thumbnail_rejects_non_image_content_type(
        viewer, viewer_main, external_work, monkeypatch):
    """プラットフォーム側が HTML を返してきても、そのまま配らない。"""

    class FakeResp:
        status_code = 200
        content = b"<html><script>alert(1)</script></html>"
        headers = {"content-type": "text/html; charset=utf-8"}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            return FakeResp()

    monkeypatch.setattr(viewer_main.httpx, "AsyncClient", FakeClient)
    url = "http://localhost:8000/api/public/cbz/somepid.cbz"
    token = viewer_main.mg.create_session(url, "1.2.3.4")
    r = viewer.get("/thumbnail", params={
        "url": url, "index": 0, "session_token": token})
    assert r.status_code != 200 or not r.headers["content-type"].startswith(
        "text/html"), (r.status_code, r.headers.get("content-type"))
    assert b"<script>alert(1)</script>" not in r.content


# ---------------------------------------------------------------------------
# /discord/send
# ---------------------------------------------------------------------------

@pytest.fixture()
def discord_capture(manga, monkeypatch):
    calls = []

    def fake_send(image_path, webhook_urls, filename=None):
        calls.append({"image_path": image_path,
                      "webhooks": list(webhook_urls),
                      "filename": filename})
        return []

    monkeypatch.setattr(manga, "send_image_to_discord", fake_send)
    return calls


VALID_WEBHOOK = "https://discord.com/api/webhooks/1234567890/abcdef-token_1"


def _post_discord(viewer, work, filename, webhook=VALID_WEBHOOK):
    return viewer.post("/discord/send", data={
        "url": work["url"], "filename": filename,
        "webhook1": webhook, "session_token": work["token"]})


def test_discord_traversal_is_refused(viewer, external_work, discord_capture,
                                      cache_dir):
    # 展開フォルダの1つ上に「秘密」ファイルを置く
    secret = cache_dir / "secret.txt"
    secret.write_text("TOP-SECRET")
    r = _post_discord(viewer, external_work, "../secret.txt")
    assert discord_capture == [], "フォルダ外のファイルが送信されてしまった"
    assert "TOP-SECRET" not in r.text


def test_discord_absolute_path_is_refused(viewer, external_work,
                                          discord_capture):
    r = _post_discord(viewer, external_work, "/etc/passwd")
    assert discord_capture == []


def test_discord_normal_file_still_sends(viewer, external_work,
                                         discord_capture):
    r = _post_discord(viewer, external_work, external_work["filename"])
    assert len(discord_capture) == 1
    sent = os.path.realpath(discord_capture[0]["image_path"])
    assert sent.startswith(os.path.realpath(external_work["extracted"]))


def test_discord_webhook_host_restricted(viewer, external_work,
                                         discord_capture):
    for bad in [
        "https://evil.example.com/api/webhooks/123/abc",
        "http://discord.com/api/webhooks/123/abc",       # https のみ
        "https://discord.com.evil.com/api/webhooks/123/abc",
        "https://discord.com/not-a-webhook",
        "https://example.com/hook",
    ]:
        discord_capture.clear()
        r = _post_discord(viewer, external_work,
                          external_work["filename"], webhook=bad)
        assert discord_capture == [], f"{bad} へ送信されてしまった"


def test_discord_error_messages_are_escaped(viewer, external_work, manga,
                                            monkeypatch):
    """丙-9: 送信エラーの文面に HTML を入れても、そのまま表示しない。"""
    def failing_send(image_path, webhook_urls, filename=None):
        return ["<script>alert(1)</script>"]

    monkeypatch.setattr(manga, "send_image_to_discord", failing_send)
    r = _post_discord(viewer, external_work, external_work["filename"])
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_discord_send_to_discord_unit(manga, cache_dir):
    """manga.send_image_to_discord 自体も、Discord 以外の宛先を断る。"""
    img = cache_dir / "p.png"
    Image.new("RGB", (10, 10)).save(img, "PNG")
    errors = manga.send_image_to_discord(
        str(img), ["https://evil.example.com/api/webhooks/1/x"], "p.png")
    assert errors, "不正な webhook がエラーにならなかった"
