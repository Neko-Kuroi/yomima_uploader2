"""
manga.py - Core business logic

変更点:
- load_image_bytes() にスクランブル画像フラグを追加（リサイズスキップ）
- セッショントークン管理を追加
- /thumbnail はepisode_thumbs/から返すよう変更
"""

import os
import re
import zipfile
import rarfile
import hashlib
import ipaddress
import tempfile
import requests
import shutil
import time
import json
import base64
import secrets
import socket
import urllib.parse
import numpy as np
from io import BytesIO
from typing import Optional

from PIL import Image
from natsort import natsorted

from watermark import embed_watermarks
# scramble(seed_B) 適用後に呼ぶ
#seed_b = ip_to_seed_b(client_ip)
#img = embed_watermarks(img, seed_b, client_ip)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_MANGA_COUNT     = 12
CACHE_SIZE_LIMIT_MB = 270
IMAGES_PER_LOAD     = 1          # 1枚ずつfetch（reader.htmlのbuffer方式に合わせる）
MAX_IMAGES_PER_REQUEST = 10      # /images が1回に返す最大ページ数 (N-20)
MAX_DOWNLOAD_SIZE_MB = 240
IMAGE_EXTENSIONS    = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
ARCHIVE_EXTENSIONS  = ('.zip', '.cbz', '.rar', '.cbr')
CACHE_TTL_SECONDS   = 86400 / 48  # ~30分
SESSION_TTL_SECONDS = 3600        # セッション有効期限1時間

# 丙-2 (ビューワー側の防御): tile_size の範囲はプラットフォーム側と揃える
TILE_SIZE_DEFAULT   = 16
TILE_SIZE_MIN       = 4
TILE_SIZE_MAX       = 256

# N-14 (ビューワー側): 外部アーカイブ内の画像にも画素数の歯止めを明示する
Image.MAX_IMAGE_PIXELS = 25_000_000

def normalize_tile_size(value) -> int:
    """範囲外・解釈できない tile_size は既定値に落とす。

    プラットフォーム側は入口で 400 にして止めるが、ビューワーは
    設定ファイルやプラットフォーム応答から受け取る側なので、
    配信そのものが落ちないよう既定値への寄せで防御する。
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return TILE_SIZE_DEFAULT
    return v if TILE_SIZE_MIN <= v <= TILE_SIZE_MAX else TILE_SIZE_DEFAULT

# ---------------------------------------------------------------------------
# セッショントークン管理
# ---------------------------------------------------------------------------

# { session_token: { cbz_url, created_at, expires_at, ip, page_count } }
_session_store: dict[str, dict] = {}

def create_session(cbz_url: str, client_ip: str, tile_size: int = 16,
                   scrambled: bool = True, a_scrambled: bool = False) -> str:
    """セッショントークンを発行する。

    N-4: 「A が掛かっているか」と「B を掛けるか」は別の旗で持つ。
    - scrambled   … 配信時にスクランブルBを掛けるか (従来の意味のまま)
    - a_scrambled … 作品の中身にスクランブルAが既に掛かっているか。
                    ブラウザが B を戻したあと A も戻す必要があるかの合図になる
    """
    token = secrets.token_urlsafe(32)   # 推測不可能な43文字
    now   = time.time()
    _session_store[token] = {
        "cbz_url":    cbz_url,
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
        "ip":         client_ip,
        "page_count": None,   # 初回アクセス時に確定
        # 丙-2: 受け取った tile_size は範囲内に正規化してから持つ
        "tile_size":  normalize_tile_size(tile_size),
        "scrambled":  scrambled,
        "a_scrambled": a_scrambled,
        "read_at":    now,    # 読み開始時刻（ヒエログリフ埋め込み用・全ページ共通）
        "seed_b":     ip_to_seed_b(client_ip),  # 一度だけ計算
    }
    _cleanup_expired_sessions()
    return token

def validate_session(token: str) -> Optional[dict]:
    """
    セッションを検証して情報を返す。
    無効・期限切れの場合は None を返す。
    """
    session = _session_store.get(token)
    if not session:
        return None
    if time.time() > session["expires_at"]:
        _session_store.pop(token, None)
        return None
    return session

def set_session_page_count(token: str, page_count: int):
    if token in _session_store:
        _session_store[token]["page_count"] = page_count

def _cleanup_expired_sessions():
    """期限切れセッションを掃除する"""
    now     = time.time()
    expired = [t for t, s in _session_store.items() if now > s["expires_at"]]
    for t in expired:
        _session_store.pop(t, None)

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def get_cache_dir() -> str:
    cache_dir = os.path.join(tempfile.gettempdir(), "manga_cache")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir

def get_cache_path(url: str) -> str:
    file_hash = hashlib.md5(url.encode()).hexdigest()
    return os.path.join(get_cache_dir(), file_hash)

def get_dir_size(path: str) -> int:
    total = 0
    if os.path.exists(path):
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
    return total

def manage_cache_size(current_reading_url: Optional[str] = None) -> None:
    cache_dir    = get_cache_dir()
    current_hash = (
        hashlib.md5(current_reading_url.encode()).hexdigest()
        if current_reading_url else None
    )
    items: dict[str, dict] = {}

    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        ext  = os.path.splitext(name)[-1].lower()

        if os.path.isfile(path) and ext in ARCHIVE_EXTENSIONS:
            url_hash = os.path.splitext(name)[0]
            items.setdefault(url_hash, {
                'archive_path': None, 'extracted_path': None,
                'total_size': 0, 'mtime': 0,
                'is_currently_reading': (url_hash == current_hash)
            })
            items[url_hash]['archive_path'] = path
            items[url_hash]['mtime'] = max(items[url_hash]['mtime'], os.path.getmtime(path))

        elif os.path.isdir(path) and name.endswith("_extracted"):
            url_hash = name.replace("_extracted", "")
            items.setdefault(url_hash, {
                'archive_path': None, 'extracted_path': None,
                'total_size': 0, 'mtime': 0,
                'is_currently_reading': (url_hash == current_hash)
            })
            items[url_hash]['extracted_path'] = path
            items[url_hash]['mtime'] = max(items[url_hash]['mtime'], os.path.getmtime(path))

    total_bytes  = 0
    for info in items.values():
        size = 0
        if info.get('archive_path') and os.path.exists(info['archive_path']):
            size += os.path.getsize(info['archive_path'])
        if info.get('extracted_path') and os.path.exists(info['extracted_path']):
            size += get_dir_size(info['extracted_path'])
        info['total_size'] = size
        total_bytes += size

    limit_bytes  = CACHE_SIZE_LIMIT_MB * 1024 * 1024
    if total_bytes <= limit_bytes:
        return

    sorted_items = sorted(items.values(), key=lambda x: (x['is_currently_reading'], x['mtime']))
    for info in sorted_items:
        if total_bytes <= limit_bytes:
            break
        if info['is_currently_reading']:
            continue
        try:
            if info.get('archive_path') and os.path.exists(info['archive_path']):
                os.remove(info['archive_path'])
            if info.get('extracted_path') and os.path.exists(info['extracted_path']):
                shutil.rmtree(info['extracted_path'])
            total_bytes -= info['total_size']
        except Exception:
            pass

def cleanup_old_cache() -> None:
    cache_dir = get_cache_dir()
    now       = time.time()
    try:
        for name in os.listdir(cache_dir):
            path = os.path.join(cache_dir, name)
            if os.path.isfile(path) and now - os.path.getmtime(path) > CACHE_TTL_SECONDS:
                os.remove(path)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

# --- 甲-5: ダウンロード宛先の制限 -------------------------------------------
# 原則: 内部宛先 (プライベート / ループバック / リンクローカル等) は常に
# 拒否し、公開宛先は許可する。ホスト名は DNS 解決後の IP で判定する
# (名前だけ見ると解決先を内部に向ける手口を通してしまうため)。
# 例外は PLATFORM_BASE_URL と同一オリジンの宛先だけ: これは利用者が
# 入力する URL ではなく運用者が環境変数で設定したプラットフォーム自身
# で、ローカル運用では localhost になるのが正当なため、信頼済みとする。

def _origin_of(url: str):
    try:
        p = urllib.parse.urlparse(url)
        if not p.hostname:
            return None
        port = p.port or (443 if p.scheme == "https" else 80)
        return (p.scheme.lower(), p.hostname.lower(), port)
    except Exception:
        return None


def _platform_origin():
    raw = os.environ.get("PLATFORM_BASE_URL", "http://localhost:8000")
    return _origin_of(raw)


def _is_platform_url(url: str) -> bool:
    origin = _origin_of(url)
    return origin is not None and origin == _platform_origin()


def _resolve_host_ips(hostname: str) -> list[str]:
    """ホスト名を解決して IP 文字列の一覧を返す (失敗時は空)。"""
    try:
        ip = ipaddress.ip_address(hostname)
        return [str(ip)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return []
    ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in ips:
            ips.append(addr)
    return ips


def _ip_is_internal(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # 解釈できないものは内部扱いで拒否 (fail-closed)
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def _operator_allowed_hosts() -> set[str]:
    """運用者が環境変数で明示した許可ホスト (カンマ区切り)。

    fake-IP 型プロキシ配下など、正当な外部宛先が特殊な IP 範囲に
    解決されてしまう運用環境のための逃げ道。利用者は指定できない。
    """
    raw = os.environ.get("MANGA_DOWNLOAD_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def validate_download_url(url: str) -> tuple[bool, str]:
    """ダウンロード先として安全かを確認する。(可否, 理由) を返す。"""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False, "URLの形式が正しくありません"
    if p.scheme.lower() not in ("http", "https"):
        return False, "http / https のURLだけを許可しています"
    if not p.hostname:
        return False, "URLにホストがありません"
    if _is_platform_url(url):
        return True, ""
    if p.hostname.lower() in _operator_allowed_hosts():
        return True, ""
    ips = _resolve_host_ips(p.hostname)
    if not ips:
        return False, "ホスト名を解決できませんでした"
    for ip_str in ips:
        if _ip_is_internal(ip_str):
            return False, "内部ネットワークへのダウンロードは禁止されています"
    return True, ""


def url_targets_internal(url: str) -> bool:
    """宛先が「確実に内部」と分かる場合だけ True を返す。

    /session や /download/start の入口での早期拒否に使う。
    DNS が引けない場合はここでは断定せず、実際に取りに行く
    download_file 側の validate_download_url で fail-closed に判定する。
    """
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return True
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return True
    if _is_platform_url(url):
        return False
    if p.hostname.lower() in _operator_allowed_hosts():
        return False
    return any(_ip_is_internal(ip) for ip in _resolve_host_ips(p.hostname))


# --- Discord Webhook の宛先制限 (甲-3) --------------------------------------

_DISCORD_WEBHOOK_HOSTS = ("discord.com", "discordapp.com")
_DISCORD_WEBHOOK_PATH = re.compile(r"^/api/webhooks/\d+/[\w.\-~]+$")


def validate_discord_webhook(webhook_url: str) -> bool:
    """Discord 公式の https Webhook URL 形式だけを許可する。"""
    try:
        p = urllib.parse.urlparse(webhook_url)
    except Exception:
        return False
    return bool(
        p.scheme == "https"
        and p.hostname in _DISCORD_WEBHOOK_HOSTS
        and _DISCORD_WEBHOOK_PATH.match(p.path or "")
    )


def download_file(
    url: str,
    save_path: str,
    max_size_mb: int = MAX_DOWNLOAD_SIZE_MB,
    extra_headers: dict = {},
    progress_callback=None,
) -> tuple[bool, str]:
    if os.path.exists(save_path):
        return True, ""

    max_bytes = max_size_mb * 1024 * 1024
    # 丙-8 の残り: 一時ファイル名は取得ごとに一意にする。固定名だと
    # 同じ URL の同時ダウンロードが同じファイルへ書き合い、片方の
    # rename が相手の途中ファイルを完成品にしてしまう (または失敗する)。
    tmp_path  = (save_path + f".tmp{os.getpid()}."
                 + secrets.token_hex(4))

    def _cleanup_tmp():
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass

    # 甲-5: 最初の宛先を検査する
    ok, err = validate_download_url(url)
    if not ok:
        return False, err

    try:
        headers = {}
        headers.update(extra_headers)

        # 甲-5: リダイレクトは自動で辿らず、各ホップで同じ検査をする
        current_url = url
        response = None
        for _hop in range(6):
            response = requests.get(
                current_url, stream=True, timeout=(10, 60),
                headers=headers, allow_redirects=False,
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                response = None
                if not location:
                    return False, "リダイレクト先がありません"
                current_url = urllib.parse.urljoin(current_url, location)
                ok, err = validate_download_url(current_url)
                if not ok:
                    return False, err
                continue
            break
        if response is None:
            return False, "リダイレクトが多すぎます"

        with response:
            response.raise_for_status()
            if progress_callback:
                progress_callback(0.0)  # 接続確立を通知
            try:
                total_size = int(response.headers.get('content-length', 0))
            except (TypeError, ValueError):
                total_size = 0

            if total_size > 0 and total_size > max_bytes:
                return False, f"ファイルが {max_size_mb}MB を超えています ({total_size/1024/1024:.1f}MB)"

            bytes_downloaded = 0
            with open(tmp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    bytes_downloaded += len(chunk)
                    # N-8: 申告サイズ (Content-Length) に関わらず、
                    # 実際に受け取った (展開後の) 累積バイトで常時判定する。
                    # gzip で送られると申告だけ小さいため、条件付きでは迂回できる。
                    if bytes_downloaded > max_bytes:
                        f.close()
                        _cleanup_tmp()
                        return False, f"ダウンロードが {max_size_mb}MB を超えたため中止しました"
                    f.write(chunk)
                    if progress_callback:
                        progress = (bytes_downloaded / total_size
                                    if total_size > 0
                                    else min(bytes_downloaded / max_bytes, 1.0))
                        progress_callback(progress)

        # os.replace は原子的な置き換え。同時取得の相手が先に完成させて
        # いても、同じ内容で上書きされるだけで壊れない
        os.replace(tmp_path, save_path)
        return True, ""

    except requests.exceptions.RequestException as e:
        _cleanup_tmp()
        return False, f"ダウンロードエラー: {e}"

# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------

def is_valid_image(filename: str) -> bool:
    return os.path.basename(filename).lower().endswith(IMAGE_EXTENSIONS)

def guess_ext_from_bytes(data: bytes) -> str:
    if data[:3] == b'\xff\xd8\xff':      return '.jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n': return '.png'
    if data[:6] in (b'GIF87a', b'GIF89a'): return '.gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return '.webp'
    return ''

def is_rar_archive(path: str) -> bool:
    return path.lower().endswith(('.rar', '.cbr'))

def get_safe_filename(original: str, index: int, ext_override: str = "") -> str:
    basename = original.replace('\\', '/').split('/')[-1]
    ext      = ext_override if ext_override else os.path.splitext(basename)[1].lower()
    stem     = os.path.splitext(basename)[0]

    # スクランブル命名規則（stem=20文字: 4文字Base62 + 16文字hex）はそのまま保持
    # プレフィックスを付けるとJS側のseed抽出位置がズレて復号できなくなる
    if len(stem) == 20:
        try:
            int(stem[4:], 16)
            # スクランブルファイル名と判定 → そのまま返す
            return stem + ext
        except ValueError:
            pass

    # 通常ファイルは従来通りプレフィックス付きで返す
    for ch in ('#', '?', '&', ' ', '+', '%', '=', ':', ';', '@', '!', '*', "'", '(', ')'):
        stem = stem.replace(ch, '_')
    stem = stem[:40] if len(stem) > 40 else stem
    return f"{index:05d}_{stem}{ext}"

# N-9: 展開結果のメモは「パス + 中身の版 (更新時刻とサイズ)」で持つ。
# パスだけだと、アーカイブを差し替えても古い一覧と古い内容が残り続けた。
_extract_cache: dict[tuple, tuple[list[str], list[str]]] = {}

def _archive_stamp(archive_path: str):
    try:
        st = os.stat(archive_path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None

def extract_archive(archive_path: str, extract_to: str) -> tuple[list[str], list[str]]:
    cache_key = (archive_path, _archive_stamp(archive_path))
    if cache_key in _extract_cache:
        return _extract_cache[cache_key]
    # 同じパスの古い版のメモは捨てる
    for k in [k for k in _extract_cache if k[0] == archive_path]:
        del _extract_cache[k]

    # 差し替え後に削除されたページや古い内容が残らないよう、
    # 再展開のときは展開先をまっさらにしてから始める
    if os.path.isdir(extract_to):
        shutil.rmtree(extract_to)
    os.makedirs(extract_to, exist_ok=True)
    image_files: list[str] = []
    warnings:    list[str] = []
    is_rar = is_rar_archive(archive_path)
    used_names: dict[str, str] = {}   # 保存名 -> 内容の目印 (md5)

    try:
        archive = rarfile.RarFile(archive_path) if is_rar else zipfile.ZipFile(archive_path)
        with archive:
            for index, file_info in enumerate(archive.infolist()):
                filename = file_info.filename
                if filename.endswith('/') or filename.endswith('\\'):
                    continue
                try:
                    data = archive.read(file_info)
                except Exception as e:
                    warnings.append(f"読み込みスキップ: {os.path.basename(filename)} ({e})")
                    continue

                if is_valid_image(filename):
                    ext = os.path.splitext(
                        os.path.basename(filename).replace('\\', '/').split('/')[-1]
                    )[1].lower()
                else:
                    ext = guess_ext_from_bytes(data)
                    if not ext:
                        continue

                try:
                    safe_name = get_safe_filename(filename, index, ext_override=ext)
                    digest    = hashlib.md5(data).hexdigest()
                    dest_name = safe_name
                    if dest_name in used_names and used_names[dest_name] != digest:
                        # N-9: 別フォルダにある同名ファイル (中身は別)。
                        # スクランブル形式の名前は復号の種に使うため
                        # 本体は変えず、末尾に __d<連番> を付けて一意化する
                        # (is_scrambled_filename 側でもこの形を認識する)。
                        stem2, ext2 = os.path.splitext(safe_name)
                        dest_name = f"{stem2}__d{index}{ext2}"
                    dest = os.path.join(extract_to, dest_name)
                    if dest_name not in used_names:
                        with open(dest, 'wb') as f:
                            f.write(data)
                        used_names[dest_name] = digest
                    image_files.append(dest)
                except Exception as e:
                    warnings.append(f"スキップ: {os.path.basename(filename)} ({e})")

    except (zipfile.BadZipFile, rarfile.BadRarFile) as e:
        warnings.append(f"無効なアーカイブ: {e}")
    except Exception as e:
        warnings.append(f"解凍エラー: {e}")

    result = (natsorted(image_files), warnings)
    _extract_cache[cache_key] = result
    return result

# ---------------------------------------------------------------------------
# IP由来seed_B生成・画像スクランブル（配信時二重スクランブル用）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ページ番号の Base62 解釈 (N-15)
# ページ名の先頭4文字はプラットフォームの to_base62 (CHARSET_62 =
# "0-9A-Za-z") で作られたページ番号。数字だけとは限らないため、
# int() で読むと11ページ目 ("000A") 以降で落ちたり 0 に潰れたりした。
# ---------------------------------------------------------------------------
BASE62_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_BASE62_VALUES = {c: i for i, c in enumerate(BASE62_CHARSET)}

def base62_to_int(text: str) -> int:
    """Base62 文字列を整数として読む。解釈できない文字があれば ValueError。"""
    if not text:
        raise ValueError("空文字列は Base62 として読めません")
    value = 0
    for ch in text:
        if ch not in _BASE62_VALUES:
            raise ValueError(f"Base62 でない文字: {ch!r}")
        value = value * 62 + _BASE62_VALUES[ch]
    return value

def page_index_from_filename(filename: str) -> int:
    """ファイル名の先頭4文字 (Base62) からページ番号を取り出す。

    取り出せない名前は 0 に落とす (透かしの配置がページで変わらない
    だけで、配信自体は止めない)。
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    try:
        return base62_to_int(stem[:4])
    except ValueError:
        return 0

def ip_to_seed_b(client_ip: str) -> int:
    """
    IPアドレスをseed_Bに変換。
    IPv4: ビット反転（可逆）。seed_B → ip_int → ビット反転 → IPv4 で復元可能。
    IPv6: MD5の先頭4バイトを使用（不可逆。スクランブル用途のみ）。
    """
    try:
        ip_int = int.from_bytes(socket.inet_aton(client_ip), 'big')
        return int(f"{ip_int:032b}"[::-1], 2)
    except Exception:
        import hashlib
        h = hashlib.md5(client_ip.encode()).digest()[:4]
        return int.from_bytes(h, 'big')

def seed_b_to_ip(seed_b: int) -> str:
    """seed_B → IPv4アドレスに復元（ビット反転で逆算）。

    アプリ本体からは呼ばれていない。透かしから IP を読み戻す
    鑑識用の部品として残してある (N-18)。
    """
    ip_int = int(f"{seed_b:032b}"[::-1], 2)
    return socket.inet_ntoa(ip_int.to_bytes(4, 'big'))

def _rand_int(rng, n: int) -> int:
    """rejection samplingで偏りなし均等整数を返す"""
    limit = (1 << 32) - ((1 << 32) % n)
    while True:
        r = rng.next()
        if r < limit:
            return r % n

def _make_perm_viewer(n: int, seed: int) -> np.ndarray:
    """rejection samplingでnumpyの順列配列を生成（ビューワー用）"""
    state = seed & 0xFFFFFFFF or 1
    perm  = np.arange(n, dtype=np.int32)
    for i in range(n - 1, 0, -1):
        ni = i + 1
        limit = (1 << 32) - ((1 << 32) % ni)
        while True:
            state ^= (state << 13) & 0xFFFFFFFF
            state ^= (state >> 17) & 0xFFFFFFFF
            state ^= (state << 5)  & 0xFFFFFFFF
            state &= 0xFFFFFFFF
            if state < limit:
                j = state % ni
                break
        perm[i], perm[j] = perm[j], perm[i]
    return perm

def scramble_image_pil(img: Image.Image, seed: int, tile_size: int) -> Image.Image:
    """
    中央80%エリアのタイルを完全ベクトル化で高速スクランブルする。
    上下左右10%は不動エリアとして保持。
    配信時の二重スクランブル（seed_B）に使用。
    """
    arr = np.array(img)
    h, w = arr.shape[:2]
    cols = w // tile_size
    rows = h // tile_size

    x0 = int(cols * 0.1)
    x1 = int(cols * 0.9)
    y0 = int(rows * 0.1)
    y1 = int(rows * 0.9)

    cx = x1 - x0
    cy = y1 - y0
    n  = cy * cx

    center = arr[y0*tile_size:y1*tile_size, x0*tile_size:x1*tile_size]
    tiled  = (center
              .reshape(cy, tile_size, cx, tile_size, -1)
              .transpose(0, 2, 1, 3, 4)
              .reshape(n, tile_size, tile_size, -1))

    perm     = _make_perm_viewer(n, seed)
    shuffled = tiled[perm]

    result = arr.copy()
    result[y0*tile_size:y1*tile_size, x0*tile_size:x1*tile_size] = (
        shuffled
        .reshape(cy, cx, tile_size, tile_size, -1)
        .transpose(0, 2, 1, 3, 4)
        .reshape(cy * tile_size, cx * tile_size, -1)
    )
    return Image.fromarray(result)

def make_delivery_filename(original_filename: str, seed_b: int) -> str:
    """
    配信ファイル名を生成する。
    CBZ内ファイル名: {4文字index}{8文字seed_A}{8文字乱数}.png
    配信ファイル名:  {4文字index}{8文字seed_B}{8文字seed_A}.png
    """
    stem      = os.path.splitext(os.path.basename(original_filename))[0]
    index     = stem[:4]    # ページindex（Base62 4文字）
    seed_a    = stem[4:12]  # seed_A（8文字hex）
    seed_b_hex = f"{seed_b:08x}"
    return f"{index}{seed_b_hex}{seed_a}.png"

def delivery_cache_key(url_hash: str, delivery_filename: str,
                       watermark_ip: str) -> str:
    """配信キャッシュのキー (乙-4)。

    スクランブル無しの作品では delivery_filename に読者ごとの情報が
    入らないため、キーに透かしへ入る IP の指紋を含めないと、最初に
    読んだ人の透かし入りの画像が全員に配られてしまう。IP そのものは
    ファイル名に残さず、短いハッシュで区別する。
    """
    ip_tag = hashlib.md5(watermark_ip.encode()).hexdigest()[:10]
    return f"{url_hash}_{delivery_filename}_{ip_tag}"

# ---------------------------------------------------------------------------
# Image loading
# ---------------------------------------------------------------------------

_SCRAMBLED_DUP_SUFFIX = re.compile(r"__d\d+$")

def is_scrambled_filename(filename: str) -> bool:
    """
    ファイル名がスクランブル命名規則かどうか判定。
    規則: {4桁Base62}{16文字hex}.png
    例:   0001a1b2c3d4e5f6g7h8.png
    展開時の同名衝突で付けた __d<連番> の末尾 (N-9) は、判定の前に外す。
    種の抽出 (先頭4文字と 4〜12 文字目) は末尾を付けても変わらない。
    """
    name = os.path.basename(filename)
    stem = os.path.splitext(name)[0]
    stem = _SCRAMBLED_DUP_SUFFIX.sub("", stem)
    # 全長20文字かつ先頭4文字がBase62、残り16文字がhex
    if len(stem) != 20:
        return False
    hex_part = stem[4:]
    try:
        int(hex_part, 16)
        return True
    except ValueError:
        return False

def load_image_bytes(
    image_path: str,
    max_size: tuple[int, int] = (1200, 1800),
    force_no_resize: bool = False,
) -> Optional[bytes]:
    """
    画像バイナリを返す。

    スクランブル済み画像（または force_no_resize=True）の場合は
    リサイズしない。アップロード時にすでに適切なサイズになっているため。

    非スクランブル画像（外部ZIP等）は従来通りmax_sizeでリサイズする。
    """
    if not os.path.exists(image_path):
        return None

    # スクランブル画像かどうかをファイル名で判定
    scrambled = force_no_resize or is_scrambled_filename(image_path)

    try:
        thumb_cache_dir = os.path.join(get_cache_dir(), "thumbs")
        os.makedirs(thumb_cache_dir, exist_ok=True)

        stat      = os.stat(image_path)
        cache_key = hashlib.md5(
            f"{image_path}:{stat.st_mtime}:{max_size}:{scrambled}".encode()
        ).hexdigest()
        jpg_cache = os.path.join(thumb_cache_dir, cache_key + ".jpg")
        png_cache = os.path.join(thumb_cache_dir, cache_key + ".png")

        if os.path.exists(jpg_cache):
            with open(jpg_cache, "rb") as f: return f.read()
        if os.path.exists(png_cache):
            with open(png_cache, "rb") as f: return f.read()

        with Image.open(image_path) as img:
            # スクランブル済み画像はリサイズしない
            if not scrambled:
                img.thumbnail(max_size, Image.LANCZOS)

            buf = BytesIO()
            if img.mode in ("RGBA", "LA"):
                img.save(buf, format="PNG")
                data = buf.getvalue()
                with open(png_cache, "wb") as f: f.write(data)
            else:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(buf, format="JPEG", quality=90)
                data = buf.getvalue()
                with open(jpg_cache, "wb") as f: f.write(data)
            return data

    except Exception:
        return None

def get_image_path(url_hash: str, filename: str) -> Optional[str]:
    # 現在はアプリ本体から呼ばれていない (N-18 で明示)。
    # 展開済みファイルの場所を引く小さな部品として残してある。
    path = os.path.join(get_cache_dir(), f"{url_hash}_extracted", filename)
    return path if os.path.exists(path) else None


def apply_watermark(img: Image.Image, seed_b: int, client_ip: str, page_index: int = 0, dt=None) -> Image.Image:
    watermarked = embed_watermarks(img, seed_b, client_ip, page_index, dt=dt)
    return watermarked.convert("RGB")
# ---------------------------------------------------------------------------
# Manga list serialization
# ---------------------------------------------------------------------------

def encode_manga_list(manga_list: list[dict]) -> str:
    payload    = {'manga_urls': manga_list, 'export_time': time.time()}
    json_bytes = json.dumps(payload, ensure_ascii=True).encode('ascii')
    return base64.urlsafe_b64encode(json_bytes).decode('ascii')

def decode_manga_list(encoded: str) -> tuple[list[dict], str]:
    try:
        encoded = encoded.strip().replace(' ', '+')
        padding = 4 - len(encoded) % 4
        if padding != 4:
            encoded += '=' * padding
        try:
            raw = base64.urlsafe_b64decode(encoded).decode('utf-8')
        except Exception:
            raw = base64.b64decode(encoded).decode('utf-8')
        data = json.loads(raw)
        return data.get('manga_urls', []), ""
    except Exception as e:
        return [], f"デコードエラー: {e}"

def validate_manga_url(url: str) -> bool:
    ext = os.path.splitext(url.split('?')[0])[-1].lower()
    return ext in ARCHIVE_EXTENSIONS

def get_filename_from_url(url: str) -> str:
    filename = os.path.basename(url).split('?')[0]
    try:
        return urllib.parse.unquote(filename, encoding='utf-8')
    except Exception:
        return filename

def build_manga_entry(url: str, title: str = "") -> dict:
    return {
        'url':        url,
        'title':      title or get_filename_from_url(url),
        'added_time': time.time(),
    }

def merge_manga_lists(
    existing: list[dict],
    incoming: list[dict],
    max_count: int = MAX_MANGA_COUNT
) -> tuple[list[dict], int]:
    """マイリスト同士の合体。現在はアプリ本体から呼ばれていない
    (N-18 で明示)。共有リストの取り込みを再び実装するときの部品。"""
    existing_urls = {m['url'] for m in existing}
    added  = 0
    result = list(existing)
    for manga in incoming:
        if len(result) >= max_count:
            break
        if manga['url'] not in existing_urls:
            result.append(manga)
            existing_urls.add(manga['url'])
            added += 1
    return result, added

# ---------------------------------------------------------------------------
# Discord Webhook
# ---------------------------------------------------------------------------

def send_image_to_discord(
    image_path:   str,
    webhook_urls: list[str],
    filename:     Optional[str] = None,
) -> list[str]:
    if not os.path.exists(image_path):
        return [f"ファイルが見つかりません: {image_path}"]

    if filename is None:
        filename = os.path.basename(image_path)

    ext      = os.path.splitext(image_path)[-1].lower()
    mime_map = {'.png': 'image/png', '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
    mime     = mime_map.get(ext, 'image/png')

    # Discord に送るファイル名：元のstemの先頭5文字 + 拡張子
    stem     = os.path.splitext(filename)[0]
    filename = stem[:5] + ext

    errors: list[str] = []
    for webhook_url in webhook_urls:
        if not webhook_url:
            continue
        # 甲-3: 送り先は Discord 公式の https Webhook だけに限る
        if not validate_discord_webhook(webhook_url):
            errors.append(f"Discord送信エラー: 許可されていないWebhook URLです ({webhook_url[:40]}...)")
            continue
        try:
            with open(image_path, 'rb') as f:
                resp = requests.post(
                    webhook_url,
                    files={'file': (filename, f, mime)},
                    timeout=30
                )
                resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            errors.append(f"Discord送信エラー ({webhook_url[:40]}...): {e}")

    return errors

# ---------------------------------------------------------------------------
# TinyURL
# ---------------------------------------------------------------------------

def shorten_url(long_url: str) -> str:
    try:
        resp = requests.get(
            "https://tinyurl.com/api-create.php",
            params={"url": long_url},
            timeout=10
        )
        resp.raise_for_status()
        return resp.text.strip()
    except Exception:
        return long_url