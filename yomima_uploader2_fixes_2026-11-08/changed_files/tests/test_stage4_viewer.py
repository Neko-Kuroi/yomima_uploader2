"""段階4 (ビューワー側): 乙-4, N-22, N-3, N-4, N-15, 丙-8 の残り。

- 乙-4/N-22: 配信キャッシュと透かしの IP はセッション発行時の IP で統一する。
  スクランブル無し作品でも、読者ごとに自分の透かしが配られる
- N-3:  リアクションの配線 (中継ルート、reader への public_id 受け渡し、
  起動時の件数読み、最終ページでのバー表示)
- N-4:  A (作品のスクランブル) と B (配信時スクランブル) の旗を分ける。
  外部作品は B だけを戻せば読める合図が /images に付き、プラットフォームの
  スクランブル作品は従来どおり B→A で戻せる。作者プレビューは専用
  トークン経由で tile_size / scrambled を取る
- N-15: ページ番号の先頭4文字は Base62 として解釈する
- 丙-8: download_file の一時ファイル名が同時ダウンロードで衝突しない
"""
import hashlib
import io
import os
import re
import threading
import time
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture(scope="module")
def viewer(viewer_main):
    return TestClient(viewer_main.app, raise_server_exceptions=False)


@pytest.fixture(scope="module")
def platform(platform_main):
    return TestClient(platform_main.app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_limiters(platform_main, viewer_main):
    for m in (platform_main, viewer_main):
        try:
            m.limiter.reset()
        except Exception:
            pass
    yield


def _png_bytes(size=(240, 360), seed=3):
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()


def _unscramble(img, seed, tile_size, manga):
    """scramble_image_pil の逆操作 (JS の復号と同じ逆順列)。"""
    arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    cols, rows = w // tile_size, h // tile_size
    x0, x1 = int(cols * 0.1), int(cols * 0.9)
    y0, y1 = int(rows * 0.1), int(rows * 0.9)
    cx, cy = x1 - x0, y1 - y0
    n = cx * cy
    center = arr[y0 * tile_size:y1 * tile_size, x0 * tile_size:x1 * tile_size]
    tiled = (center.reshape(cy, tile_size, cx, tile_size, -1)
             .transpose(0, 2, 1, 3, 4).reshape(n, tile_size, tile_size, -1))
    perm = manga._make_perm_viewer(n, seed)
    inv = np.empty_like(tiled)
    inv[perm] = tiled
    result = arr.copy()
    result[y0 * tile_size:y1 * tile_size, x0 * tile_size:x1 * tile_size] = (
        inv.reshape(cy, cx, tile_size, tile_size, -1)
        .transpose(0, 2, 1, 3, 4)
        .reshape(cy * tile_size, cx * tile_size, -1))
    return Image.fromarray(result)


# ---------------------------------------------------------------------------
# プラットフォーム連携の偽 httpx (viewer → platform を TestClient へ中継)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, r):
        self.status_code = r.status_code
        self.content = r.content
        self.headers = r.headers
        self._r = r

    def json(self):
        return self._r.json()


def _install_fake_platform(viewer_main, platform, monkeypatch):
    base = viewer_main.PLATFORM_BASE_URL

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def _path(self, url):
            assert url.startswith(base), url
            return url[len(base):]

        async def get(self, url, **kw):
            r = platform.get(self._path(url), params=kw.get("params"),
                             headers=kw.get("headers"))
            return _FakeResp(r)

        async def post(self, url, **kw):
            r = platform.post(self._path(url), json=kw.get("json"),
                              headers=kw.get("headers"))
            return _FakeResp(r)

    monkeypatch.setattr(viewer_main.httpx, "AsyncClient", FakeClient)


def _make_user(platform, username, email):
    r = platform.post("/api/auth/signup", json={
        "username": username, "email": email, "password": "stage4-password"})
    assert r.status_code == 200, r.text
    r = platform.post("/api/auth/token", data={
        "username": username, "password": "stage4-password"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _publish(platform, platform_main, headers, title_id, episode_id, **over):
    payload = {
        "status": "published", "access_level": "public", "price": 0,
        "title_name": f"T{title_id}", "episode_name": f"E{episode_id}",
        "caption": "", "comment": "", "note": "", "feedback": "",
        "scrambled": True, "tile_size": 16, "webhook_enabled": False,
        "rating": "General", "warnings": [], "genre_tags": ["日常"],
        "motif_tags": [],
    }
    payload.update(over)
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


# ---------------------------------------------------------------------------
# 乙-4 / N-22: 透かしとキャッシュはセッションの IP で統一する
# ---------------------------------------------------------------------------

def test_delivery_watermark_uses_session_ip(viewer, manga, cache_dir):
    url = "http://example.com/plain.zip"
    # 透かしは上下10%の帯に入る。帯が狭いと透かし自体が配置されないため、
    # 帯の高さが透かし (RFVP 104px) より大きくなる大きさの画像を使う
    png = _png_bytes(size=(800, 1200))
    zp = manga.get_cache_path(url) + ".zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("page1.png", png)
    ep = manga.get_cache_path(url) + "_extracted"
    files, _ = manga.extract_archive(zp, ep)
    fname = os.path.basename(files[0])
    h = hashlib.md5(url.encode()).hexdigest()
    orig = Image.open(io.BytesIO(png)).convert("RGB")

    results = {}
    for ip in ("198.51.100.10", "198.51.100.99"):
        tok = manga.create_session(url, ip, scrambled=False)
        sess = manga.validate_session(tok)
        r = viewer.get(f"/image/{tok}/{h}/{fname}")
        assert r.status_code == 200, r.status_code
        expected = manga.apply_watermark(
            orig, sess["seed_b"], ip, 0,
            dt=datetime.fromtimestamp(sess["read_at"], tz=timezone.utc))
        served = Image.open(io.BytesIO(r.content)).convert("RGB")
        assert np.array_equal(np.array(served), np.array(expected)), \
            f"{ip} の透かしになっていない (他人の透かしが配られている)"
        results[ip] = r.content

    # 2人分のキャッシュが別々に残り、内容も違う
    assert results["198.51.100.10"] != results["198.51.100.99"]
    delivery = cache_dir / "delivery"
    assert len(list(delivery.iterdir())) >= 2


# ---------------------------------------------------------------------------
# N-15: ページ番号は Base62
# ---------------------------------------------------------------------------

def test_page_index_from_filename_base62(manga):
    assert manga.page_index_from_filename("000Aabcd1234abcd1234.png") == 10
    assert manga.page_index_from_filename("0009abcd1234abcd1234.png") == 9
    assert manga.page_index_from_filename("0010abcd1234abcd1234.png") == 62
    assert manga.page_index_from_filename("!!!!.png") == 0


def _scrambled_page_file(manga, cache_dir, url, index_head="000A"):
    stem = f"{index_head}a1b2c3d4e5f60708"  # 20文字のスクランブル形式
    extract_dir = cache_dir / f"{hashlib.md5(url.encode()).hexdigest()}_extracted"
    extract_dir.mkdir(exist_ok=True)
    path = extract_dir / f"{stem}.png"
    Image.new("RGB", (120, 180), (10, 120, 200)).save(path, "PNG")
    return f"{stem}.png"


def test_serve_image_page_index_is_base62(viewer, manga, cache_dir,
                                          monkeypatch):
    url = "http://example.com/b62.zip"
    fname = _scrambled_page_file(manga, cache_dir, url)
    tok = manga.create_session(url, "198.51.100.10", scrambled=True)
    seen = {}
    orig_wm = manga.apply_watermark

    def recording(img, seed_b, client_ip, page_index=0, dt=None):
        seen["page_index"] = page_index
        return orig_wm(img, seed_b, client_ip, page_index, dt=dt)

    monkeypatch.setattr(manga, "apply_watermark", recording)
    h = hashlib.md5(url.encode()).hexdigest()
    r = viewer.get(f"/image/{tok}/{h}/{fname}")
    assert r.status_code == 200, r.status_code
    assert seen["page_index"] == 10, seen


def test_discord_send_page_index_is_base62(viewer, viewer_main, manga,
                                           cache_dir, monkeypatch):
    url = "http://example.com/b62d.zip"
    fname = _scrambled_page_file(manga, cache_dir, url)
    tok = manga.create_session(url, "198.51.100.10", scrambled=True)
    sess = manga.validate_session(tok)
    delivery = manga.make_delivery_filename(fname, sess["seed_b"])
    seen = {}
    orig_wm = manga.apply_watermark

    def recording(img, seed_b, client_ip, page_index=0, dt=None):
        seen["page_index"] = page_index
        return orig_wm(img, seed_b, client_ip, page_index, dt=dt)

    monkeypatch.setattr(manga, "apply_watermark", recording)
    monkeypatch.setattr(manga, "send_image_to_discord", lambda *a, **k: [])
    r = viewer.post("/discord/send", data={
        "url": url, "filename": fname,
        "webhook1": "https://discord.com/api/webhooks/123456789/abcdef",
        "webhook2": "", "session_token": tok})
    assert r.status_code == 200, r.status_code
    assert "送信しました" in r.text
    assert seen["page_index"] == 10, seen


# ---------------------------------------------------------------------------
# N-3: リアクション配線
# ---------------------------------------------------------------------------

def test_reaction_relay_and_reader_wiring(viewer, viewer_main, platform,
                                          platform_main, monkeypatch):
    _install_fake_platform(viewer_main, platform, monkeypatch)
    headers = _make_user(platform, "s4react", "s4react@example.com")
    pid = _publish(platform, platform_main, headers, 461, 461)

    # 中継ルート: 1回目は added=True、2回目は added=False (段階2の仕様どおり)
    r = viewer.post("/reaction", json={
        "public_id": pid, "reaction_type": "いいね"})
    assert r.status_code == 200, r.text
    assert r.json()["added"] is True
    assert r.json()["counts"]["いいね"] == 1
    r = viewer.post("/reaction", json={
        "public_id": pid, "reaction_type": "いいね"})
    assert r.status_code == 200, r.text
    assert r.json()["added"] is False
    assert r.json()["counts"]["いいね"] == 1

    r = viewer.get(f"/reaction_counts/{pid}")
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["いいね"] == 1

    # reader 画面に public_id が渡る
    cbz_url = f"http://testserver/api/public/cbz/{pid}.cbz"
    r = viewer.get("/reader", params={"url": cbz_url})
    assert r.status_code == 200, r.status_code
    assert f'id="episode-public-id"  value="{pid}"' in r.text


def test_reader_html_reaction_boot_and_bar():
    path = os.path.join(os.path.dirname(__file__), "..",
                        "mangaupload_yomima_site", "yomima",
                        "templates", "reader.html")
    src = open(path, encoding="utf-8").read()
    boot = src[src.index("async function boot()"):]
    assert "loadReactionCounts()" in boot, "起動時に件数を読みに行っていない"
    # 最終ページでバーを表示する処理がある
    assert "reaction-bar" in src and "hidden = false" in src


def test_reader_matches_catalog_by_public_id(viewer, viewer_main, platform,
                                             platform_main, monkeypatch):
    """Y-2: cbz_url のホストがカタログと違っても public_id で特定できる。"""
    _install_fake_platform(viewer_main, platform, monkeypatch)
    headers = _make_user(platform, "s4y2", "s4y2@example.com")
    pid = _publish(platform, platform_main, headers, 471, 471)
    # カタログの cbz_url は testserver ホスト。別ホスト表記で開いても
    # スクランブル設定と public_id が取れること
    url = f"http://localhost:8000/api/public/cbz/{pid}.cbz"
    r = viewer.get("/reader", params={"url": url})
    assert r.status_code == 200, r.status_code
    assert f'value="{pid}"' in r.text
    assert re.search(r"SCRAMBLED\s*=\s*true", r.text), r.text[-2000:]


# ---------------------------------------------------------------------------
# N-4: 外部作品は B の合図だけで戻せる / プラットフォーム作品は B→A
# ---------------------------------------------------------------------------

def test_external_work_has_b_only_signal(viewer, manga, monkeypatch):
    monkeypatch.setattr(manga, "_resolve_host_ips",
                        lambda host: ["93.184.216.34"], raising=False)
    url = "http://example.com/ext4.zip"
    png = _png_bytes(seed=5)
    zp = manga.get_cache_path(url) + ".zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("page1.png", png)
    ep = manga.get_cache_path(url) + "_extracted"
    files, _ = manga.extract_archive(zp, ep)
    fname = os.path.basename(files[0])

    r = viewer.get("/session", params={"url": url})
    assert r.status_code == 200, r.text
    tok = r.json()["session_token"]
    sess = manga.validate_session(tok)
    assert sess.get("a_scrambled") is False

    r = viewer.get("/images", params={
        "url": url, "offset": 0, "limit": 1, "session_token": tok})
    assert r.status_code == 200, r.status_code
    assert 'data-decode-b="true"' in r.text
    assert 'data-decode-a="false"' in r.text
    assert f'data-seed-b="{sess["seed_b"]:08x}"' in r.text

    # 配信画像を B だけ戻すと、透かし入りの元画像に一致する
    h = hashlib.md5(url.encode()).hexdigest()
    r = viewer.get(f"/image/{tok}/{h}/{fname}")
    assert r.status_code == 200, r.status_code
    served = Image.open(io.BytesIO(r.content)).convert("RGB")
    orig = Image.open(io.BytesIO(png)).convert("RGB")
    expected = manga.apply_watermark(
        orig, sess["seed_b"], sess["ip"], 0,
        dt=datetime.fromtimestamp(sess["read_at"], tz=timezone.utc))
    decoded = _unscramble(served, sess["seed_b"], sess["tile_size"], manga)
    assert np.array_equal(np.array(decoded), np.array(expected))


def test_platform_scrambled_work_full_roundtrip(viewer, viewer_main, manga,
                                                platform, platform_main,
                                                monkeypatch):
    _install_fake_platform(viewer_main, platform, monkeypatch)
    headers = _make_user(platform, "s4round", "s4round@example.com")
    pid = _publish(platform, platform_main, headers, 481, 481)

    url = f"http://localhost:8000/api/public/cbz/{pid}.cbz"
    png = _png_bytes(seed=8)
    orig = Image.open(io.BytesIO(png)).convert("RGB")
    # プラットフォームの A スクランブル済みページを用意する
    page_name = platform_main.generate_page_filename(0)
    seed_a = int(page_name[4:12], 16)
    a_img = platform_main.scramble_image(orig, seed_a, 16)
    buf = io.BytesIO()
    a_img.save(buf, "PNG")
    zp = manga.get_cache_path(url) + ".cbz"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(page_name, buf.getvalue())
    ep = manga.get_cache_path(url) + "_extracted"
    files, _ = manga.extract_archive(zp, ep)
    fname = os.path.basename(files[0])

    r = viewer.get("/session", params={"url": url})
    assert r.status_code == 200, r.text
    tok = r.json()["session_token"]
    sess = manga.validate_session(tok)
    assert sess.get("a_scrambled") is True

    r = viewer.get("/images", params={
        "url": url, "offset": 0, "limit": 1, "session_token": tok})
    assert r.status_code == 200, r.status_code
    assert 'data-decode-b="true"' in r.text
    assert 'data-decode-a="true"' in r.text

    h = hashlib.md5(url.encode()).hexdigest()
    r = viewer.get(f"/image/{tok}/{h}/{fname}")
    assert r.status_code == 200, r.status_code
    served = Image.open(io.BytesIO(r.content)).convert("RGB")
    # ブラウザと同じ順序 (B を戻してから A を戻す) で元画像 + 透かしに戻る
    step1 = _unscramble(served, sess["seed_b"], sess["tile_size"], manga)
    step2 = _unscramble(step1, seed_a, sess["tile_size"], manga)
    expected = manga.apply_watermark(
        orig, sess["seed_b"], sess["ip"], 0,
        dt=datetime.fromtimestamp(sess["read_at"], tz=timezone.utc))
    assert np.array_equal(np.array(step2), np.array(expected))


def test_preview_session_and_reader_use_preview_settings(
        viewer, viewer_main, platform, platform_main, manga, monkeypatch):
    _install_fake_platform(viewer_main, platform, monkeypatch)
    headers = _make_user(platform, "s4prevview", "s4prevview@example.com")
    pid = _publish(platform, platform_main, headers, 491, 491,
                   status="draft", scrambled=False, tile_size=32)
    r = platform.post(f"/api/author/episodes/{pid}/preview-token",
                      headers=headers)
    ptoken = r.json()["preview_token"]
    url = f"http://localhost:8000/api/author/preview/{pid}.cbz?token={ptoken}"

    r = viewer.get("/session", params={"url": url})
    assert r.status_code == 200, r.text
    sess = manga.validate_session(r.json()["session_token"])
    assert sess["tile_size"] == 32, sess
    assert sess.get("a_scrambled") is False

    r = viewer.get("/reader", params={"url": url})
    assert r.status_code == 200, r.status_code
    assert re.search(r"tileSize\s*=\s*32", r.text), "プレビューの tile_size が画面に渡っていない"


# ---------------------------------------------------------------------------
# 丙-8 の残り: 同時ダウンロードで一時ファイルが衝突しない
# ---------------------------------------------------------------------------

def test_concurrent_downloads_of_same_url(manga, cache_dir):
    body = b"PK\x03\x04" + b"x" * 5000

    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            # 2回に分けてゆっくり送り、2スレッドの書き込み期間を重ねる
            self.wfile.write(body[:2500])
            self.wfile.flush()
            time.sleep(0.5)
            self.wfile.write(body[2500:])

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    os.environ["PLATFORM_BASE_URL"] = f"http://127.0.0.1:{port}"
    try:
        url = f"http://127.0.0.1:{port}/same.cbz"
        save = str(cache_dir / "same.cbz")
        results = {}

        def run(i):
            results[i] = manga.download_file(url, save)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert results.get(0, (False, "no result"))[0], results
        assert results.get(1, (False, "no result"))[0], results
        with open(save, "rb") as f:
            assert f.read() == body
        leftovers = [p for p in cache_dir.iterdir() if ".tmp" in p.name]
        assert leftovers == [], f"一時ファイルが残っている: {leftovers}"
    finally:
        os.environ["PLATFORM_BASE_URL"] = "http://localhost:8000"
        srv.shutdown()
