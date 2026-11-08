"""正常系: ビューワーの /session → /images → /image の通常閲覧が
壊れていないことの確認 (監査 test6 相当)。

注意: この実行環境の DNS は fake-IP プロキシ配下で、外部ホスト名が
198.18.x (ベンチマーク用範囲) に解決されてしまう。そのままだと
SSRF 検査が「内部宛先」と判定するため、通常運用 (公開IPに解決される
環境) を再現するために DNS 解決だけを公開IPへ差し替える。
宛先検査そのもののテストは test_ssrf_download.py にある。
"""
import hashlib
import io
import os
import zipfile
from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture(scope="module")
def viewer(viewer_main):
    return TestClient(viewer_main.app, raise_server_exceptions=False)


def test_session_images_image_flow(viewer, viewer_main, manga, cache_dir,
                                   monkeypatch):
    monkeypatch.setattr(
        manga, "_resolve_host_ips",
        lambda host: ["93.184.216.34"], raising=False)

    url = "http://example.com/ext.zip"
    buf = io.BytesIO()
    Image.new("RGB", (120, 180), (200, 30, 30)).save(buf, "PNG")
    png = buf.getvalue()
    zp = manga.get_cache_path(url) + ".zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("page1.png", png)
    ep = manga.get_cache_path(url) + "_extracted"
    files, _ = manga.extract_archive(zp, ep)
    fname = os.path.basename(files[0])

    # /session (通常の入口)
    r = viewer.get("/session", params={"url": url})
    assert r.status_code == 200, r.text
    tok = r.json()["session_token"]
    assert tok

    # /images はセッション付きで 200
    r = viewer.get("/images", params={
        "url": url, "offset": 0, "limit": 1, "session_token": tok})
    assert r.status_code == 200, r.status_code

    # /thumbnail もセッション付きで 200 (フォールバック生成)
    r = viewer.get("/thumbnail", params={
        "url": url, "index": 0, "session_token": tok})
    assert r.status_code == 200, r.status_code
    assert r.headers["content-type"].startswith("image/")

    # /image はスクランブルB+透かし適用後の画像が返る (監査 test6 と同じ照合)
    sess = manga._session_store[tok]
    h = hashlib.md5(url.encode()).hexdigest()
    r = viewer.get(f"/image/{tok}/{h}/{fname}")
    assert r.status_code == 200, r.status_code
    served = Image.open(io.BytesIO(r.content)).convert("RGB")
    orig = Image.open(io.BytesIO(png)).convert("RGB")
    expected = manga.apply_watermark(
        manga.scramble_image_pil(orig, sess["seed_b"], sess["tile_size"]),
        sess["seed_b"], sess["ip"], 0,
        dt=datetime.fromtimestamp(sess["read_at"], tz=timezone.utc))
    assert np.array_equal(np.array(served), np.array(expected))
