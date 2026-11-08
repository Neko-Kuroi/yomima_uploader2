"""甲-5: ダウンロード宛先制限 / N-8: gzip 迂回 (manga.download_file)。

原則: 内部宛先 (プライベート/ループバック/リンクローカル等) は常に拒否、
公開宛先は許可。ただし運用者が環境変数 PLATFORM_BASE_URL で指定した
プラットフォーム自身の宛先は、信頼済みの宛先として許可する
(ローカル運用では localhost になるため)。
"""
import gzip
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


def _serve(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class _ZipHandler(BaseHTTPRequestHandler):
    body = b"PK\x03\x04dummy-archive-bytes"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def test_validate_rejects_private_and_loopback(manga):
    for url in [
        "http://127.0.0.1:9999/x.cbz",
        "http://10.0.0.5/x.cbz",
        "http://192.168.1.10/x.cbz",
        "http://172.16.0.9/x.cbz",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x.cbz",
        "file:///etc/passwd",
        "ftp://example.com/x.cbz",
    ]:
        ok, err = manga.validate_download_url(url)
        assert not ok, f"{url} は拒否されるべき: {err}"


def test_validate_allows_public_with_fake_dns(manga, monkeypatch):
    # DNS 解決を差し替え、公開IPなら許可されることを確認する
    monkeypatch.setattr(
        manga, "_resolve_host_ips",
        lambda host: ["93.184.216.34"], raising=False)
    ok, err = manga.validate_download_url("https://example.com/book.cbz")
    assert ok, err


def test_validate_rejects_hostname_resolving_to_private(manga, monkeypatch):
    # ホスト名自体は普通でも、解決先が内部なら拒否 (DNS リバインディング対策)
    monkeypatch.setattr(
        manga, "_resolve_host_ips",
        lambda host: ["10.1.2.3"], raising=False)
    ok, err = manga.validate_download_url("https://sneaky.example.com/book.cbz")
    assert not ok


def test_download_from_loopback_is_refused(manga, cache_dir):
    """監査 R-17 の再現: 以前は 127.0.0.1 のサーバーから取得できてしまった。"""
    srv, port = _serve(_ZipHandler)
    try:
        save = str(cache_dir / "dl.cbz")
        ok, err = manga.download_file(f"http://127.0.0.1:{port}/x.cbz", save)
        assert not ok
        assert not os.path.exists(save)
    finally:
        srv.shutdown()


def test_download_redirect_to_private_is_refused(manga, cache_dir):
    """リダイレクトの各ホップでも同じ検査をすること。"""
    class Redirector(BaseHTTPRequestHandler):
        target = ""

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", self.target)
            self.end_headers()

        def log_message(self, *a):
            pass

    secret_srv, secret_port = _serve(_ZipHandler)
    Redirector.target = f"http://127.0.0.1:{secret_port}/secret.cbz"
    # リダイレクト元自体は「プラットフォーム (信頼済み)」として扱い、
    # 飛び先の検査だけを効かせる
    redir_srv, redir_port = _serve(Redirector)
    os.environ["PLATFORM_BASE_URL"] = f"http://127.0.0.1:{redir_port}"
    try:
        save = str(cache_dir / "redir.cbz")
        ok, err = manga.download_file(
            f"http://127.0.0.1:{redir_port}/start.cbz", save)
        assert not ok, "内部宛先へのリダイレクトは拒否されるべき"
        assert not os.path.exists(save)
    finally:
        os.environ["PLATFORM_BASE_URL"] = "http://localhost:8000"
        redir_srv.shutdown()
        secret_srv.shutdown()


def test_download_from_platform_origin_still_works(manga, cache_dir):
    """正常系: PLATFORM_BASE_URL と同一オリジンの宛先は取得できる。"""
    srv, port = _serve(_ZipHandler)
    os.environ["PLATFORM_BASE_URL"] = f"http://127.0.0.1:{port}"
    try:
        save = str(cache_dir / "platform.cbz")
        ok, err = manga.download_file(
            f"http://127.0.0.1:{port}/api/public/cbz/abc.cbz", save)
        assert ok, err
        with open(save, "rb") as f:
            assert f.read() == _ZipHandler.body
    finally:
        os.environ["PLATFORM_BASE_URL"] = "http://localhost:8000"
        srv.shutdown()


def test_download_gzip_bomb_is_stopped_by_cumulative_bytes(manga, cache_dir):
    """N-8 / 監査 R-18: 申告は小さい gzip で実体が上限を超えたら止める。"""
    raw = b"A" * (3 * 1024 * 1024)
    gz = gzip.compress(raw)

    class GzipHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(gz)))
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(gz)

        def log_message(self, *a):
            pass

    srv, port = _serve(GzipHandler)
    os.environ["PLATFORM_BASE_URL"] = f"http://127.0.0.1:{port}"
    try:
        save = str(cache_dir / "gz.cbz")
        ok, err = manga.download_file(
            f"http://127.0.0.1:{port}/big.cbz", save, max_size_mb=1)
        assert not ok, "展開後 3MB は上限 1MB で止まるべき"
        assert not os.path.exists(save)
        assert not os.path.exists(save + ".tmp"), ".tmp 残骸を残さない"
    finally:
        os.environ["PLATFORM_BASE_URL"] = "http://localhost:8000"
        srv.shutdown()
