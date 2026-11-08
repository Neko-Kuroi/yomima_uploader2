"""段階3 (ビューワー側): N-9, N-20, 丙-2 (ビューワー側の防御),
N-8 の残り (サムネイル取得のサイズ確認)。

- N-9:  展開キャッシュはアーカイブの差し替えを検知する。同名ファイルが
        別フォルダにある場合は両方のページが残る
- N-20: /images の offset は 0 以上、limit は上限で丸める
- 丙-2: セッションの tile_size は範囲外なら既定値に落とす
- N-8:  プラットフォームから取ったサムネイルが巨大なら配らない
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


def _png(color, size=(60, 90)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _pixel(path):
    with Image.open(path) as im:
        return im.convert("RGB").getpixel((0, 0))


# ---------------------------------------------------------------------------
# N-9: 展開キャッシュの陳腐化と同名衝突
# ---------------------------------------------------------------------------

def test_extract_refreshes_when_archive_is_replaced(manga, tmp_path):
    zp = str(tmp_path / "arc.zip")
    ep = str(tmp_path / "extracted")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("a.png", _png((255, 0, 0)))
    files, _ = manga.extract_archive(zp, ep)
    assert len(files) == 1
    assert _pixel(files[0]) == (255, 0, 0)

    # 同じパスで中身を差し替える (ページ追加 + 色変更)。
    # サイズと更新時刻の両方が変わるようにする
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("a.png", _png((0, 0, 255)))
        z.writestr("b.png", _png((0, 255, 0)))
    st = os.stat(zp)
    os.utime(zp, (st.st_atime, st.st_mtime + 5))

    files, _ = manga.extract_archive(zp, ep)
    assert len(files) == 2, files
    # 通常ファイルは連番プレフィックス付きで保存される (00000_a.png 等)
    by_name = {os.path.basename(f).split("_", 1)[-1]: f for f in files}
    assert _pixel(by_name["a.png"]) == (0, 0, 255), "古い内容が残っている"
    assert _pixel(by_name["b.png"]) == (0, 255, 0)


def test_extract_same_scrambled_name_in_two_folders_keeps_both(manga,
                                                              tmp_path):
    stem = "0001a1b2c3d4e5f6a7b8"  # スクランブル形式 (20文字, 後半hex)
    zp = str(tmp_path / "arc2.zip")
    ep = str(tmp_path / "extracted2")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(f"dir1/{stem}.png", _png((255, 0, 0)))
        z.writestr(f"dir2/{stem}.png", _png((0, 0, 255)))
    files, _ = manga.extract_archive(zp, ep)
    assert len(files) == 2, files
    assert len(set(files)) == 2, f"保存先が衝突している: {files}"
    colors = {_pixel(f) for f in files}
    assert colors == {(255, 0, 0), (0, 0, 255)}, colors
    # 配信時の復号判定 (ファイル名形式) が壊れていないこと
    for f in files:
        assert manga.is_scrambled_filename(os.path.basename(f)), f


# ---------------------------------------------------------------------------
# 丙-2 (ビューワー側): tile_size の正規化
# ---------------------------------------------------------------------------

def test_session_tile_size_is_normalized(manga):
    url = "http://example.com/ts.zip"
    for bad in (0, -1, 1, 99999, "abc", None):
        token = manga.create_session(url, "1.2.3.4", tile_size=bad)
        sess = manga.validate_session(token)
        assert sess["tile_size"] == 16, (bad, sess["tile_size"])
    token = manga.create_session(url, "1.2.3.4", tile_size=32)
    assert manga.validate_session(token)["tile_size"] == 32


# ---------------------------------------------------------------------------
# N-20: /images の offset / limit
# ---------------------------------------------------------------------------

@pytest.fixture()
def many_pages_work(manga, cache_dir):
    url = "http://example.com/many.zip"
    png = _png((30, 60, 90), size=(30, 45))
    zp = manga.get_cache_path(url) + ".zip"
    with zipfile.ZipFile(zp, "w") as z:
        for i in range(15):
            z.writestr(f"p{i:02d}.png", png)
    token = manga.create_session(url, "1.2.3.4")
    return {"url": url, "token": token}


def test_images_offset_and_limit_are_clamped(viewer, many_pages_work):
    r = viewer.get("/images", params={
        "url": many_pages_work["url"], "offset": -1, "limit": 9999,
        "session_token": many_pages_work["token"]})
    assert r.status_code == 200, r.status_code
    # 負の offset は末尾から数えず、先頭から始まる
    assert 'alt="ページ 1 / 15"' in r.text, r.text[:500]
    # limit は上限で丸められ、全ページ分は返ってこない
    assert r.text.count('class="img-block"') <= 10, \
        r.text.count('class="img-block"')
    assert 'data-has-more="true"' in r.text


def test_images_normal_batch_still_works(viewer, many_pages_work):
    r = viewer.get("/images", params={
        "url": many_pages_work["url"], "offset": 0, "limit": 3,
        "session_token": many_pages_work["token"]})
    assert r.status_code == 200, r.status_code
    assert r.text.count('class="img-block"') == 3
    r = viewer.get("/images", params={
        "url": many_pages_work["url"], "offset": 3, "limit": 3,
        "session_token": many_pages_work["token"]})
    assert r.status_code == 200, r.status_code
    assert 'alt="ページ 4 / 15"' in r.text


# ---------------------------------------------------------------------------
# N-8 の残り: プラットフォームから取ったサムネイルのサイズ確認
# ---------------------------------------------------------------------------

def test_thumbnail_does_not_serve_huge_platform_response(
        viewer, viewer_main, monkeypatch):
    class FakeResp:
        status_code = 200
        content = b"\xff\xd8\xff" + b"x" * (11 * 1024 * 1024)
        headers = {"content-type": "image/jpeg",
                   "content-length": str(len(content))}

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
    # 巨大な応答をそのまま中継しない (502 で落とすか、フォールバックで 404)
    assert r.status_code in (404, 413, 502), r.status_code
    assert len(r.content) < 11 * 1024 * 1024
