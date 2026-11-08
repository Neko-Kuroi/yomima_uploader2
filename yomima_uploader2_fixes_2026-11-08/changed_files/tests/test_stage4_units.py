"""段階4 (部品単体): N-10, N-15 (単位), N-17, N-18, N-11, N-12。

- N-10: RFVP-64 は中央軸セルの1シンボル破損でも復元できる
- N-15: Base62 の往復 (プラットフォーム to_base62 とビューワー側の解釈)
- N-17: 透かしキャッシュのキーに効く引数を全部含める。
        seed_B 変換は manga.py を正本として IPv6 でも例外を出さない
- N-18: watermark.py / rfvp64.py の動作確認 (__main__) が現在の関数で動き、
        保存先が相対パスである
- N-11/N-12: setup_app 2本は import しても動き出さず、Bore のポートが
        uvicorn と一致し、出力の読み手が1つに統一されている
"""
import importlib.util
import io
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

SITE_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "mangaupload_yomima_site")
VIEWER_DIR = os.path.join(SITE_DIR, "yomima")


# ---------------------------------------------------------------------------
# N-10: RFVP-64 の軸セル破損
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def codec():
    sys.path.insert(0, VIEWER_DIR)
    from rfvp64 import RFVP64Codec
    return RFVP64Codec()


def _flip(arr, codec, r, c, palette):
    cx = (c + codec.QUIET) * codec.CELL_PX + codec.CELL_PX // 2
    cy = (r + codec.QUIET) * codec.CELL_PX + codec.CELL_PX // 2
    orig = codec._nearest_color(arr[cy, cx], palette)
    new = (orig + 1) % 4
    arr[(r + codec.QUIET) * codec.CELL_PX:(r + codec.QUIET + 1) * codec.CELL_PX,
        (c + codec.QUIET) * codec.CELL_PX:(c + codec.QUIET + 1) * codec.CELL_PX] = palette[new]


def test_rfvp_axis_cell_corruption_is_repaired(codec):
    import numpy as np
    ip, pal = "192.168.1.100", 1
    palette = codec.COLOR_THEMES[pal]
    base = np.array(codec.encode(ip, pal))
    axis = [(r, c) for (r, c) in codec.DATA_COORDS if c == 4]
    assert len(axis) == 9
    for (r, c) in axis:
        arr = base.copy()
        _flip(arr, codec, r, c, palette)
        restored, msg = codec.decode(Image.fromarray(arr), pal)
        assert restored == ip, f"軸セル ({r},{c}) の破損が直らない: {msg}"


def test_rfvp_existing_correction_still_works(codec):
    import numpy as np
    ip, pal = "192.168.1.100", 1
    palette = codec.COLOR_THEMES[pal]
    # 無傷
    restored, _ = codec.decode(codec.encode(ip, pal), pal)
    assert restored == ip
    base = np.array(codec.encode(ip, pal))
    # 片側ミラーの破損 (消失訂正) は従来どおり
    for n in (1, 2, 4):
        arr = base.copy()
        for idx in [i for i, (r, c) in enumerate(codec.DATA_COORDS)
                    if c != 4][:n]:
            r, c = codec.DATA_COORDS[idx]
            _flip(arr, codec, r, c, palette)
        restored, msg = codec.decode(Image.fromarray(arr), pal)
        assert restored == ip, (n, msg)
    # 両側ミラーが同じように壊れた場合 (消失として検出できない誤り)
    arr = base.copy()
    idx = [i for i, (r, c) in enumerate(codec.DATA_COORDS) if c != 4][0]
    r, c = codec.DATA_COORDS[idx]
    _flip(arr, codec, r, c, palette)
    _flip(arr, codec, r, 8 - c, palette)
    restored, msg = codec.decode(Image.fromarray(arr), pal)
    assert restored == ip, msg


# ---------------------------------------------------------------------------
# N-15: Base62 の往復
# ---------------------------------------------------------------------------

def test_base62_roundtrip_with_platform(manga, platform_main):
    for n in (0, 1, 9, 10, 61, 62, 123, 3843, 10000):
        s = platform_main.to_base62(n)
        assert manga.base62_to_int(s) == n
        assert manga.page_index_from_filename(f"{s}xxxxxxxxxxxxxxxx.png") == n


# ---------------------------------------------------------------------------
# N-17: 透かしキャッシュキーと seed_B 変換の統一
# ---------------------------------------------------------------------------

def test_qr_cache_key_includes_scale_and_opacity():
    sys.path.insert(0, VIEWER_DIR)
    import watermark as wm
    a = wm._get_cached_qr(12345, "203.0.113.1", scale=3, opacity=0.5)
    b = wm._get_cached_qr(12345, "203.0.113.1", scale=4, opacity=0.5)
    assert a is not b and a.size != b.size, "scale を変えても同じ QR が返る"
    c = wm._get_cached_qr(12345, "203.0.113.1", scale=3, opacity=0.25)
    assert c is not a, "opacity を変えても同じ QR が返る"
    d = wm._get_cached_qr(12345, "203.0.113.1", scale=3, opacity=0.5)
    assert d is a, "同じ引数ならキャッシュが効くべき"


def test_hiero_cache_key_includes_font_size():
    sys.path.insert(0, VIEWER_DIR)
    import watermark as wm
    from datetime import datetime, timezone
    from hiero_mark import HIERO_FONT_PATH
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    a = wm._get_cached_hiero(dt, font_size=18, opacity=1.0,
                             color=(0, 0, 0), font_path=HIERO_FONT_PATH)
    b = wm._get_cached_hiero(dt, font_size=24, opacity=1.0,
                             color=(0, 0, 0), font_path=HIERO_FONT_PATH)
    assert a is not b and a.size != b.size, "font_size を変えても同じ画像が返る"
    c = wm._get_cached_hiero(dt, font_size=18, opacity=1.0,
                             color=(0, 0, 0), font_path=HIERO_FONT_PATH)
    assert c is a


def test_seed_b_conversion_is_unified(manga):
    sys.path.insert(0, VIEWER_DIR)
    import watermark as wm
    # IPv4 は従来どおり往復できる
    for ip in ("192.168.1.100", "8.8.8.8"):
        assert wm.ip_to_seed_b(ip) == manga.ip_to_seed_b(ip)
        assert manga.seed_b_to_ip(manga.ip_to_seed_b(ip)) == ip
    # IPv6 でも例外にならず、manga 版 (正本) と同じ値になる
    v6 = "2001:db8::1"
    assert wm.ip_to_seed_b(v6) == manga.ip_to_seed_b(v6)


# ---------------------------------------------------------------------------
# N-18: __main__ の動作確認スクリプト
# ---------------------------------------------------------------------------

def test_watermark_main_runs(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = VIEWER_DIR
    r = subprocess.run(
        [sys.executable, os.path.join(VIEWER_DIR, "watermark.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert (tmp_path / "watermark_test.png").exists()


def test_rfvp_main_runs(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = VIEWER_DIR
    r = subprocess.run(
        [sys.executable, os.path.join(VIEWER_DIR, "rfvp64.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert (tmp_path / "rfvp64_test.png").exists()


# ---------------------------------------------------------------------------
# N-11 / N-12: setup_app
# ---------------------------------------------------------------------------

def _load_setup(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # import しただけで input() 等が走らないこと
    return mod


def test_setup_apps_import_without_running():
    for name, path in (
            ("setup_platform_s4", os.path.join(SITE_DIR, "setup_app.py")),
            ("setup_viewer_s4", os.path.join(VIEWER_DIR, "setup_app.py"))):
        mod = _load_setup(name, path)
        assert callable(mod.main)
        assert callable(mod.setup_bore_tunnel)
        assert callable(mod.setup_cloudflare_tunnel)
        # 起動失敗 (None) でも生存確認が例外にならない
        assert mod._processes_alive(None, None) is False


class _FakeProc:
    def __init__(self, stdout=None):
        self.stdout = stdout if stdout is not None else io.StringIO("")
        self.stderr = io.StringIO("")
        self._poll = None

    def poll(self):
        return self._poll

    def terminate(self):
        self._poll = 0


def _fake_subprocess(record, bore_out="", cf_out=""):
    import subprocess as real

    def popen(args, **kw):
        record.append(list(args))
        joined = " ".join(str(a) for a in args)
        if "bore" in joined:
            return _FakeProc(stdout=io.StringIO(bore_out))
        if "cloudflared" in joined:
            return _FakeProc(stdout=io.StringIO(cf_out))
        return _FakeProc()

    def run(*a, **kw):
        return SimpleNamespace(stdout="200 OK", stderr="")

    return SimpleNamespace(Popen=popen, PIPE=real.PIPE, STDOUT=real.STDOUT,
                           run=run, TimeoutExpired=real.TimeoutExpired)


def _prep_setup(mod, tmp_path, monkeypatch, record, **fake_kw):
    monkeypatch.setattr(mod, "subprocess", _fake_subprocess(record, **fake_kw))
    monkeypatch.setattr(mod, "current_directory", str(tmp_path))
    monkeypatch.setattr(mod.os, "system", lambda cmd: 0)
    monkeypatch.setattr(mod, "wait_for_flask_server", lambda **kw: True)


def test_viewer_bore_port_and_url_files(tmp_path, monkeypatch):
    mod = _load_setup("setup_viewer_bore", os.path.join(VIEWER_DIR, "setup_app.py"))
    record = []
    _prep_setup(mod, tmp_path, monkeypatch, record,
                bore_out="proxy started\nlistening at bore.pub:54321\n")
    flask_process, bore_process, url = mod.setup_bore_tunnel()
    assert url == "bore.pub:54321"
    uvicorn = next(a for a in record if "uvicorn" in a)
    bore = next(a for a in record if any("bore" in str(x) for x in a)
                and "uvicorn" not in a)
    # N-11: uvicorn のポートと bore の転送ポートが一致する (8001)
    assert "--port" in uvicorn
    assert uvicorn[uvicorn.index("--port") + 1] == "8001"
    assert "8001" in bore
    # Y-1: Bore でも自分の URL ファイル (platform_url.txt) が自フォルダに書かれ、
    # プラットフォーム側の viewer_url.txt も書かれる
    own = tmp_path / "platform_url.txt"
    assert own.exists() and own.read_text().strip() == "http://bore.pub:54321"
    for_platform = tmp_path.parent / "viewer_url.txt"
    assert for_platform.exists()
    assert for_platform.read_text().strip() == "http://bore.pub:54321"


def test_platform_bore_writes_platform_url(tmp_path, monkeypatch):
    mod = _load_setup("setup_platform_bore", os.path.join(SITE_DIR, "setup_app.py"))
    record = []
    _prep_setup(mod, tmp_path, monkeypatch, record,
                bore_out="listening at bore.pub:60000\n")
    flask_process, bore_process, url = mod.setup_bore_tunnel()
    assert url == "bore.pub:60000"
    bore = next(a for a in record if any("bore" in str(x) for x in a)
                and "uvicorn" not in a)
    assert "8000" in bore
    own = tmp_path / "platform_url.txt"
    assert own.exists() and "bore.pub:60000" in own.read_text()


def test_viewer_cloudflare_url_detection_and_files(tmp_path, monkeypatch):
    mod = _load_setup("setup_viewer_cf", os.path.join(VIEWER_DIR, "setup_app.py"))
    record = []
    cf = "2026-01-01 INF https://test-tunnel.trycloudflare.com\nnext line\n"
    _prep_setup(mod, tmp_path, monkeypatch, record, cf_out=cf)
    flask_process, tunnel_process, url = mod.setup_cloudflare_tunnel()
    assert url == "https://test-tunnel.trycloudflare.com"
    own = tmp_path / "platform_url.txt"
    assert own.exists() and own.read_text().strip() == url
    for_platform = tmp_path.parent / "viewer_url.txt"
    assert for_platform.exists() and for_platform.read_text().strip() == url
