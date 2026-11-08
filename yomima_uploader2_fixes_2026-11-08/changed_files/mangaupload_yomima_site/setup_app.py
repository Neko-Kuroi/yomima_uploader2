"""
setup_app.py (プラットフォーム用 / ポート8000)

Google Colab 等でアプリと公開トンネル (Bore / Cloudflared) を
立ち上げるためのセットアップスクリプト。

段階4 (N-11 / N-12 / Y-1) で直したこと:
- (N-12a) プロセス出力の読み手は「ポンプ」スレッド1本に統一した。
  以前は Bore の stdout をスレッドと本体ループが奪い合い、URL 行が
  スレッド側に取られて本体が永久に待つことがあった。URL 検出と
  ログ保存は同じポンプの中で行う。
- (N-12b) Cloudflared は URL が取れた後もポンプが読み続けるため、
  出力が溜まってパイプが詰まることがない。
- (N-12c) ログの保存先は /content 直書きではなく、このファイルの
  あるフォルダ (相対) にした。IPython が無い環境でも import できる
  よう、表示部品は失敗時のフォールバックを持つ。
- (N-12d) 末尾の選択・起動・生存監視は main() の中へ移した。
  import しただけでは何も動き出さない。
- (N-12e) 起動失敗時 (プロセスが None) でも生存確認が落ちない。
- (Y-1) Bore 経路でも自分の URL を platform_url.txt に書く
  (Cloudflare 経路と同じファイルに揃えた)。
"""
import os
import re
import subprocess
import sys
import threading
import time
import logging

try:
    from IPython.display import HTML, display
except Exception:  # Colab 以外 (IPython が無い環境) でも読み込めるように
    class HTML(str):
        """IPython が無い場合の素朴な代用品 (ただの文字列)。"""

    def display(obj):  # noqa: A001 - IPython 互換のための名前
        print(obj)

try:
    import requests
except Exception:  # requests が無い環境では urllib で代替する
    requests = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

APP_PORT = 8000                      # このアプリ (プラットフォーム) のポート
LOG_NAME = "platform_debug.log"      # ログファイル名 (相対パスで置く)
current_directory = os.getcwd()
exposed_url = ""

# ---------------------------------------------------------------------------
# ログファイル (相対パス。/content 直書きはやめた: N-12c)
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()
_log_file = None


def _get_log_file():
    global _log_file
    with _log_lock:
        if _log_file is None:
            _log_file = open(os.path.join(current_directory, LOG_NAME),
                             "w", buffering=1)
        return _log_file


def _log_write(text: str):
    try:
        f = _get_log_file()
        f.write(text)
        f.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 出力ポンプ (N-12a): 1ストリームにつき読み手は1つだけ。
# 読んだ行は「ログへ保存」と「URL の検出」を同じループで行う。
# ---------------------------------------------------------------------------
class _UrlWatcher:
    """出力行から URL を1回だけ拾う担当。"""

    def __init__(self, pattern: str):
        self._re = re.compile(pattern)
        self.event = threading.Event()
        self.url = ""

    def feed(self, line: str):
        if self.event.is_set():
            return
        m = self._re.search(line)
        if m:
            self.url = m.group(1) if m.groups() else m.group(0)
            self.event.set()


def _pump_stream(process, stream, prefix="", watcher: _UrlWatcher = None):
    """プロセスの出力を最後まで読み続ける (ポンプ)。

    読み手が複数いると行を取り合うため、stdout/stderr ごとに
    このポンプを1本だけ立てる。URL が必要な場合は watcher を渡し、
    検出もこの中で済ませる。
    """
    if stream is None:
        return
    try:
        while True:
            line = stream.readline()
            if line:
                text = line if isinstance(line, str) else line.decode(
                    "utf-8", "replace")
                _log_write(f"{prefix}{text.strip()}\n")
                if watcher is not None:
                    watcher.feed(text)
            elif process is None or process.poll() is not None:
                rest = ""
                try:
                    rest = stream.read() or ""
                except Exception:
                    rest = ""
                if rest:
                    _log_write(f"{prefix}{rest.strip()}\n")
                    if watcher is not None:
                        for extra in rest.splitlines():
                            watcher.feed(extra)
                break
            else:
                threading.Event().wait(0.01)
    except Exception as e:
        logger.debug(f"出力ポンプで例外: {e}")
    finally:
        try:
            stream.close()
        except Exception:
            pass


def read_process_output(process, stream, prefix=""):
    """旧来の名前の互換用。中身は _pump_stream と同じ。"""
    _pump_stream(process, stream, prefix)


def _start_pump(process, stream, prefix="", watcher: _UrlWatcher = None):
    t = threading.Thread(target=_pump_stream,
                         args=(process, stream, prefix, watcher),
                         daemon=True)
    t.start()
    return t


def _wait_for_url(watcher: _UrlWatcher, process, timeout: float):
    """watcher が URL を拾うか、プロセスが終わるか、時間切れまで待つ。"""
    start = time.time()
    while time.time() - start < timeout:
        if watcher.event.wait(0.2):
            return watcher.url
        if process is not None and process.poll() is not None:
            # 終了直前の行がまだポンプに残っている可能性があるため少し待つ
            watcher.event.wait(0.5)
            return watcher.url or ""
    return watcher.url or ""


def _processes_alive(flask_process, tunnel_process) -> bool:
    """生存確認 (N-12e)。None のプロセスがあっても例外にしない。

    どちらか一方でも「起動したのに終わっている」ものがあれば False。
    両方とも None (起動失敗) の場合も False。
    """
    started = False
    for proc in (flask_process, tunnel_process):
        if proc is None:
            continue
        started = True
        if proc.poll() is not None:
            return False
    return started


def _write_url_files(public_url: str):
    """自分の公開 URL をファイルへ書く (Y-1)。

    platform_url.txt (自分のフォルダ): 記録用。プラットフォーム側の
    コードでこのファイルを読む箇所はない (ビューワー側の同名ファイル
    とは役割が違う。対応表は STAGE4_REPORT 参照)。
    """
    with open(os.path.join(current_directory, "platform_url.txt"), "w") as f:
        f.write(public_url)
    print("📝 platform_url.txt に保存しました")


def _start_app():
    """FastAPI アプリを uvicorn で起動し、出力ポンプを付ける。"""
    print("🚀 FastAPI アプリケーションを起動しています...")
    flask_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--reload",
         "--host", "0.0.0.0", "--port", str(APP_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    _start_pump(flask_process, flask_process.stdout, "[FLASK_OUT] ")
    _start_pump(flask_process, flask_process.stderr, "[FLASK_ERR] ")
    return flask_process


# ---------------------------------------------------------------------------
# Bore
# ---------------------------------------------------------------------------
def setup_bore_tunnel():
    """Rust製のboreトンネルの設定"""
    print("🦀 Bore トンネルをセットアップしています...")

    os.system('sudo wget -nc https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz')
    os.system('sudo tar -zxvf bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz')
    os.system('sudo chmod 764 bore')

    flask_process = _start_app()
    if not wait_for_flask_server(port=APP_PORT, timeout=60):
        print("❌ FastAPI起動失敗")
        return flask_process, None, ""

    print("🌐 bore トンネルを開始しています...")
    bore_process = subprocess.Popen(
        ['sudo', './bore', 'local', str(APP_PORT), '--to', 'bore.pub'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    # stdout の読み手はポンプ1本 (N-12a)。URL 検出もポンプの中で行う。
    watcher = _UrlWatcher(r'(bore\.pub:\d+)')
    _start_pump(bore_process, bore_process.stdout, "[BORE_OUT] ", watcher)
    _start_pump(bore_process, bore_process.stderr, "[BORE_ERR] ")

    print("🔍 トンネルURLを待機しています...")
    url = _wait_for_url(watcher, bore_process, timeout=130)

    if url:
        print(f"✅ トンネルが開始されました: {url}")
        display(HTML(f'<a href="http://{url}" target="_blank" style="font-size:18px; color:pink;">{url}</a>'))
        # Y-1: Bore 経路でも自分の URL ファイルを書く
        _write_url_files(f"http://{url}")
        print("\n--- Boreトンネルへの内部curlテスト ---")
        try:
            curl_result = subprocess.run(['curl', '-s', '-I', f"http://{url}"],
                                         capture_output=True, text=True, timeout=10)
            print("curl出力（ヘッダのみ）：")
            print(curl_result.stdout.strip())
            if "200 OK" in curl_result.stdout:
                print("✅ curlテスト成功: HTTP 200 OK を受信しました。ブラウザでアクセスできるはずです。")
            else:
                print("❌ curlテスト失敗: 予期しない応答コードを受信しました。")
        except subprocess.TimeoutExpired:
            print("❌ curlテスト失敗: タイムアウトしました。Boreサービスが応答していません。")
        except Exception as e:
            print(f"❌ curlテスト中にエラーが発生しました: {e}")
        print("----------------------------")
    else:
        print("⚠️ トンネルURLの取得に失敗しました。")
        print(f"ログ ({LOG_NAME}) を確認してください。")

    return flask_process, bore_process, url


# ---------------------------------------------------------------------------
# Cloudflared
# ---------------------------------------------------------------------------
def setup_cloudflare_tunnel():
    """Cloudflare Tunnelの設定"""
    print("☁️ Cloudflare Tunnel をセットアップしています...")

    os.system('sudo wget -nc https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb')
    os.system('sudo dpkg -i cloudflared-linux-amd64.deb 2>/dev/null')

    flask_process = _start_app()
    if not wait_for_flask_server(port=APP_PORT, timeout=60):
        print("❌ FastAPI起動失敗")
        return None, None, ""

    print("🌐 Cloudflare トンネルを開始しています...")
    tunnel_process = subprocess.Popen(
        ['/usr/local/bin/cloudflared', 'tunnel', '--url',
         f'http://localhost:{APP_PORT}'],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # stdoutとstderrを統合
        text=True,
        bufsize=1,
    )

    # URL 検出はポンプの中で行い、URL が取れた後もポンプは読み続ける
    # (N-12a / N-12b: 読み手の統一とパイプ詰まり防止)。
    watcher = _UrlWatcher(r'(https?://[^\s]+\.trycloudflare\.com)')
    _start_pump(tunnel_process, tunnel_process.stdout, "[CF] ", watcher)

    print("🔍 トンネルURLを待機中...")
    url = _wait_for_url(watcher, tunnel_process, timeout=180)

    if url:
        print(f"✅ Cloudflare トンネルが開始されました: {url}")
        display(HTML(f'<a href="{url}" target="_blank" style="font-size:18px; color:pink;">{url}</a>'))
        _write_url_files(url)
    else:
        print("⚠️ URL取得失敗。")
        print(f"ログ ({LOG_NAME}) を確認してください。")

    return flask_process, tunnel_process, url


# ---------------------------------------------------------------------------
# 補助
# ---------------------------------------------------------------------------
def wait_for_flask_server(port=8000, timeout=60):
    """アプリのサーバーが応答するのを待つ。"""
    url = f"http://localhost:{port}"
    print(f"Waiting for server to start at {url}...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            if requests is not None:
                response = requests.get(url, timeout=1)
                ok = response.status_code == 200
            else:
                import urllib.request
                with urllib.request.urlopen(url, timeout=1) as resp:
                    ok = resp.status == 200
            if ok:
                print("✅ server is up and running!")
                return True
        except Exception:
            pass
        time.sleep(1)
    print(f"❌ server did not respond within {timeout} seconds.")
    return False


def get_colab_external_ip():
    """Colabの外部IPアドレスを取得します。"""
    try:
        result = subprocess.run(['curl', 'ipinfo.io/ip'],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except Exception as e:
        print(f"⚠️ 外部IPアドレスの取得に失敗しました: {e}")
        return "UNKNOWN_IP"


# ---------------------------------------------------------------------------
# 対話的な起動は main() の中だけ (N-12d: import しても動き出さない)
# ---------------------------------------------------------------------------
def main():
    global exposed_url
    selected_tunnel_service = ""
    while True:
        print("\n--- トンネル方法を選択してください ---")
        print("1. Bore")
        print("2. Cloudflared")
        choice = input("選択 (1/2): ").strip()
        if choice == '1':
            selected_tunnel_service = "bore"
            break
        elif choice == '2':
            selected_tunnel_service = "cloudflared"
            break
        else:
            print("無効な選択です。1 または 2 を入力してください。")

    flask_process = None
    tunnel_process = None
    if selected_tunnel_service == "bore":
        flask_process, tunnel_process, exposed_url = setup_bore_tunnel()
    elif selected_tunnel_service == "cloudflared":
        flask_process, tunnel_process, exposed_url = setup_cloudflare_tunnel()

    print(f"\nColab 外部IPアドレス: {get_colab_external_ip()}")
    print(f"expose : {exposed_url}")
    print("\n" + "=" * 60)
    print("✅ トンネル起動完了！このセルを終了させないでください。")
    print("ブラウザで以下のURLにアクセスできます：")
    print(f"→ {exposed_url}")
    print("=" * 60)

    try:
        while _processes_alive(flask_process, tunnel_process):
            time.sleep(5)
        if flask_process is not None or tunnel_process is not None:
            print("⚠️ いずれかのプロセスが終了しました")
    except KeyboardInterrupt:
        print("\n🛑 ユーザーにより停止されました")
    finally:
        print("終了処理中...")
        for proc in (flask_process, tunnel_process):
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
