"""
viewer_main.py - FastAPI router

変更点:
- セッショントークン発行・検証
- /image/{session_token}/{filename} に変更
- /thumbnail を episode_thumbs/ から返すよう変更
- slowapi レート制限
- CF-Connecting-IP 対応
"""

import asyncio
import concurrent.futures
import hashlib
import html
import logging
import os
import re
import httpx
from pathlib import Path
from io import BytesIO
from contextlib import asynccontextmanager
from typing import Annotated, Optional

import urllib.parse
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.responses import Response as StarletteResponse

import manga as mg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR    = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

PLATFORM_BASE_URL = os.environ.get("PLATFORM_BASE_URL", "http://localhost:8000")
INTERNAL_API_KEY  = os.environ.get("INTERNAL_API_KEY", "")
_VIEWER_BASE_URL_ENV = os.environ.get("VIEWER_BASE_URL", "")

def get_viewer_base_url() -> str:
    """自分自身のURLをplatform_url.txt（ビューワー側）から動的に読む"""
    url_file = Path(__file__).parent / "platform_url.txt"
    if url_file.exists():
        val = url_file.read_text().strip()
        if val.startswith("http"):
            return val
    if _VIEWER_BASE_URL_ENV:
        return _VIEWER_BASE_URL_ENV
    return "http://localhost:8001"

_executor       = concurrent.futures.ThreadPoolExecutor(max_workers=4)
_thumb_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

# ---------------------------------------------------------------------------
# 段階1の共通ヘルパー (甲-3 / 甲-4)
# ---------------------------------------------------------------------------

def _require_session(session_token: str, url: str) -> dict:
    """セッション必須化: 空トークンは素通りさせず、URL の一致まで確かめる。"""
    if not session_token:
        raise HTTPException(status_code=403, detail="セッションが必要です")
    session = mg.validate_session(session_token)
    if not session or session["cbz_url"] != url:
        raise HTTPException(status_code=403, detail="セッションが無効です")
    return session


def _safe_child_path(base_dir: str, filename: str) -> str | None:
    """base_dir 配下に収まる場合だけ解決済みパスを返す (甲-3)。

    `../` や絶対パスでフォルダの外を指すものは None。
    """
    try:
        base = Path(base_dir).resolve()
        candidate = (base / filename).resolve()
    except Exception:
        return None
    if candidate == base or base not in candidate.parents:
        return None
    return str(candidate)


_ALLOWED_THUMB_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif", "image/avif",
}
# N-8 の残り: プラットフォームから中継するサムネイルの大きさの上限。
# サムネイルは (150x220) の JPEG で、通常は数十KBしかない。
MAX_THUMB_RESPONSE_BYTES = 10 * 1024 * 1024
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _platform_thumb_url(url: str) -> str | None:
    """サムネイル URL を PLATFORM_BASE_URL からだけ組み立てる (甲-4)。

    利用者が渡した URL のホスト部分は一切使わない。public_id は
    パスの1要素として安全な文字だけを許可する。
    """
    base = PLATFORM_BASE_URL.rstrip("/")
    for marker, kind in (("/api/public/cbz/", "public"),
                         ("/api/author/preview/", "author")):
        if marker in url:
            rest = url.split(marker, 1)[1]
            public_id = rest.split("/")[0].split("?")[0].removesuffix(".cbz")
            if not _PUBLIC_ID_RE.match(public_id):
                return None
            if kind == "public":
                return f"{base}/api/public/thumb/{public_id}"
            thumb = f"{base}/api/author/thumb/{public_id}"
            # プレビューは token クエリだけを引き継ぐ (他のパラメータは捨てる)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            token_vals = qs.get("token")
            if token_vals:
                thumb += "?token=" + urllib.parse.quote(token_vals[0], safe="")
            return thumb
    return None

def _public_id_from_url(url: str) -> str:
    """プラットフォーム作品の URL から public_id を抜き出す。

    公開 CBZ (/api/public/cbz/{id}.cbz) と作者プレビュー
    (/api/author/preview/{id}.cbz?token=...) の両形式に対応する。
    抜き出せない場合は空文字 (Y-2: カタログ照合を完全一致だけに
    頼らないための部品)。
    """
    for marker in ("/api/public/cbz/", "/api/author/preview/"):
        if marker in url:
            rest = url.split(marker, 1)[1]
            public_id = rest.split("/")[0].split("?")[0].removesuffix(".cbz")
            return public_id if _PUBLIC_ID_RE.match(public_id) else ""
    return ""


async def _fetch_preview_settings(url: str) -> Optional[dict]:
    """作者プレビューの作品設定 (scrambled / tile_size) を、
    プレビュー専用トークン経由でプラットフォームから取得する (N-4)。

    取得できなければ None。プレビューは下書きを含むため公開カタログ
    には載らず、この内部受け口でしか設定が分からない。
    """
    public_id = _public_id_from_url(url)
    if not public_id:
        return None
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    token_vals = qs.get("token")
    if not token_vals:
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                f"{PLATFORM_BASE_URL}/api/internal/preview-settings",
                params={"public_id": public_id, "token": token_vals[0]},
                headers={"x-internal-key": INTERNAL_API_KEY},
            )
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.warning(f"プレビュー設定の取得に失敗: {e}")
    return None

# ---------------------------------------------------------------------------
# IP取得（Cloudflared対応）
# ---------------------------------------------------------------------------
def trust_cf_connecting_ip() -> bool:
    """甲-6: CF-Connecting-IP を信じてよいのは、Cloudflare 経由の運用で
    環境変数 TRUST_CF_CONNECTING_IP=1 (true/yes/on) を明示した場合だけ。
    プラットフォーム側 (../main.py) と同じ決め方に揃えている。
    この IP は透かし・seed_B・レート制限の元になるため、直接アクセスで
    ヘッダを付けただけでは偽装できないようにする。"""
    return os.environ.get("TRUST_CF_CONNECTING_IP", "").strip().lower() in (
        "1", "true", "yes", "on")

def get_client_ip(request: Request) -> str:
    if trust_cf_connecting_ip():
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip
    return request.client.host if request.client else "unknown"

# ---------------------------------------------------------------------------
# レート制限
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_client_ip)

# ---------------------------------------------------------------------------
# アプリ
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    mg.manage_cache_size()
    mg.cleanup_old_cache()
    yield
    _executor.shutdown(wait=False)
    _thumb_executor.shutdown(wait=False)

app = FastAPI(title="Manga Reader", lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

templates = Jinja2Templates(directory=TEMPLATES_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return StarletteResponse("リクエストが多すぎます。しばらく待ってから再試行してください。", status_code=429)

# ---------------------------------------------------------------------------
# List screen  GET /
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    share: Optional[str] = Query(default=None),
):
    manga_list:     list[dict] = []
    import_message: Optional[str] = None

    if share:
        manga_list, err = mg.decode_manga_list(share)
        if err:
            import_message = err

    return templates.TemplateResponse(request, "list.html", {
        "request":        request,
        "manga_list":     manga_list,
        "share_param":    share or "",
        "import_message": import_message,
        "max_count":      mg.MAX_MANGA_COUNT,
    })

# ---------------------------------------------------------------------------
# Public catalog screen  GET /public
# ---------------------------------------------------------------------------

@app.get("/public", response_class=HTMLResponse)
async def public_catalog(request: Request):
    catalog   = []
    error_msg = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{PLATFORM_BASE_URL}/api/public/catalog")
            resp.raise_for_status()
            catalog = resp.json()
    except Exception as e:
        error_msg = f"カタログの取得に失敗しました: {e}"

    return templates.TemplateResponse(request, "public.html", {
        "request":      request,
        "catalog":      catalog,
        "error_msg":    error_msg,
        "platform_url": PLATFORM_BASE_URL,
    })

# ---------------------------------------------------------------------------
# セッショントークン発行  GET /session
# ---------------------------------------------------------------------------

@app.get("/session")
@limiter.limit("30/minute")
async def create_session(
    request: Request,
    url: str = Query(...),
):
    """
    CBZ URLに対するセッショントークンを発行する。
    プラットフォームURLの場合はCBZ専用トークンも同時に発行する。
    """
    if not mg.validate_manga_url(url):
        raise HTTPException(status_code=400, detail="無効なURL")
    # 甲-5: 内部宛先と分かる URL にはセッションを発行しない
    if mg.url_targets_internal(url):
        raise HTTPException(status_code=400, detail="無効なURL")

    client_ip = get_client_ip(request)
    cbz_token = None
    tile_size = 16
    scrambled = True  # デフォルトはスクランブルあり
    # N-4: 「A が掛かっているか」は B の旗とは別に持つ。外部作品は
    # 中身に A が掛かっていないので False。プラットフォーム作品は
    # 設定の scrambled がそのまま「A が掛かっているか」になる。
    a_scrambled = False

    # プラットフォームのCBZ URLの場合のみトークンをアップローダーに発行依頼
    if "/api/public/cbz/" in url:
        public_id = url.split("/api/public/cbz/")[1].replace(".cbz", "")
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(
                    f"{PLATFORM_BASE_URL}/api/internal/issue-cbz-token",
                    params={"public_id": public_id},
                    headers={"x-internal-key": INTERNAL_API_KEY},
                )
                if r.status_code == 200:
                    data      = r.json()
                    cbz_token = data.get("cbz_token")
                    # 丙-2: プラットフォームから受けた値も範囲内に寄せる
                    tile_size = mg.normalize_tile_size(data.get("tile_size", 16))
                    scrambled = data.get("scrambled", True)
                    a_scrambled = bool(scrambled)
        except Exception as e:
            logger.warning(f"cbz_token取得失敗: {e}")
    elif "/api/author/preview/" in url:
        # 作者プレビュー: 下書きを含むためカタログには載らない。
        # プレビュー専用トークン経由で作品設定を取得する (N-4)。
        # B は通常閲覧と同じく配信時に掛ける (scrambled=True のまま)。
        settings = await _fetch_preview_settings(url)
        if settings is not None:
            tile_size = mg.normalize_tile_size(settings.get("tile_size", 16))
            a_scrambled = bool(settings.get("scrambled", True))

    token = mg.create_session(url, client_ip, tile_size=tile_size,
                              scrambled=scrambled, a_scrambled=a_scrambled)

    return {
        "session_token": token,
        "expires_in":    mg.SESSION_TTL_SECONDS,
        "cbz_token":     cbz_token,
    }

# ---------------------------------------------------------------------------
# Add manga  POST /manga/add
# ---------------------------------------------------------------------------

@app.post("/manga/add", response_class=HTMLResponse)
async def manga_add(
    request: Request,
    url:   Annotated[str, Form()],
    title: Annotated[str, Form()] = "",
    share: Annotated[str, Form()] = "",
):
    manga_list, _ = mg.decode_manga_list(share) if share else ([], "")
    error: Optional[str] = None

    if not mg.validate_manga_url(url):
        error = "URLは .zip / .cbz / .rar / .cbr である必要があります"
    elif len(manga_list) >= mg.MAX_MANGA_COUNT:
        error = f"最大 {mg.MAX_MANGA_COUNT} 件まで登録できます"
    elif url in {m['url'] for m in manga_list}:
        error = "このURLはすでに追加されています"
    else:
        manga_list.append(mg.build_manga_entry(url, title))

    new_share = mg.encode_manga_list(manga_list) if manga_list else ""

    return templates.TemplateResponse(request, "partials/manga_list.html", {
        "request":    request,
        "manga_list": manga_list,
        "share_param": new_share,
        "error":      error,
        "max_count":  mg.MAX_MANGA_COUNT,
    }, headers={"HX-Push-Url": f"/?share={new_share}" if new_share else "/"})

# ---------------------------------------------------------------------------
# Remove manga  POST /manga/remove
# ---------------------------------------------------------------------------

@app.post("/manga/remove", response_class=HTMLResponse)
async def manga_remove(
    request: Request,
    url:   Annotated[str, Form()],
    share: Annotated[str, Form()] = "",
):
    manga_list, _ = mg.decode_manga_list(share) if share else ([], "")
    manga_list    = [m for m in manga_list if m['url'] != url]
    new_share     = mg.encode_manga_list(manga_list) if manga_list else ""

    return templates.TemplateResponse(request, "partials/manga_list.html", {
        "request":    request,
        "manga_list": manga_list,
        "share_param": new_share,
        "error":      None,
        "max_count":  mg.MAX_MANGA_COUNT,
    }, headers={"HX-Push-Url": f"/?share={new_share}" if new_share else "/"})

# ---------------------------------------------------------------------------
# Reader screen  GET /reader
# ---------------------------------------------------------------------------

@app.get("/reader", response_class=HTMLResponse)
async def reader(
    request:  Request,
    url:      str = Query(...),
    share:    str = Query(default=""),
    webhook1: str = Query(default=""),
    webhook2: str = Query(default=""),
):
    if not mg.validate_manga_url(url):
        raise HTTPException(status_code=400, detail="無効なURL")

    manga_list, _ = mg.decode_manga_list(share) if share else ([], "")
    manga_title   = next(
        (m['title'] for m in manga_list if m['url'] == url),
        mg.get_filename_from_url(url)
    )

    # settings.json から scrambled / tile_size / タイトル / webhook_enabled を取得する
    scrambled           = False
    tile_size           = 16
    manga_title_display = manga_title

    # URLがプラットフォームのCBZ形式かどうかで許可判定を分ける。
    # 外部ZIP・マイリスト追加のURLは無条件で Discord 送信を許可する。
    # プラットフォーム公開作品（/api/public/cbz/）のみ作者の webhook_enabled 設定に従う。
    is_platform_url = "/api/public/cbz/" in url
    is_preview_url  = "/api/author/preview/" in url
    webhook_enabled = not (is_platform_url or is_preview_url)
    # N-3: リアクションの宛先。公開作品のときだけ public_id を渡す
    # (プレビューや外部作品では空のままにして無効化する)。
    episode_public_id = _public_id_from_url(url) if is_platform_url else ""

    if is_preview_url:
        # 作者プレビューはカタログに載らないため、作品設定は
        # プレビュー専用トークン経由で取得する (N-4)。
        settings = await _fetch_preview_settings(url)
        if settings is not None:
            scrambled = bool(settings.get("scrambled", False))
            tile_size = mg.normalize_tile_size(settings.get("tile_size", 16))
    else:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                catalog_url = f"{PLATFORM_BASE_URL}/api/public/catalog"
                resp = await client.get(catalog_url)
                if resp.status_code == 200:
                    catalog = resp.json()
                    for item in catalog:
                        # Y-2: 完全一致を第一に、だめなら public_id の
                        # 一致でも同一作品とみなす。URL のホスト部分や
                        # 末尾の違いで作品を取り違えないための緩和で、
                        # 判定に使うのは抜き出した ID 同士の比較。
                        item_pid = _public_id_from_url(
                            item.get("cbz_url") or "")
                        if item.get("cbz_url") == url or (
                                episode_public_id and item_pid
                                and item_pid == episode_public_id):
                            scrambled = item.get("scrambled", False)
                            # 丙-2: カタログ経由の tile_size も範囲内に寄せる
                            tile_size = mg.normalize_tile_size(
                                item.get("tile_size", 16))
                            # プラットフォーム作品のみ作者設定を反映。外部URLは上書きしない。
                            if is_platform_url:
                                webhook_enabled = item.get("webhook_enabled", False)
                            t = item.get("title_name", "").strip()
                            e = item.get("episode_name", "").strip()
                            if t and e:
                                manga_title_display = f"{t} — {e}"
                            elif t:
                                manga_title_display = t
                            elif e:
                                manga_title_display = e
                            break
        except Exception:
            pass

    return templates.TemplateResponse(request, "reader.html", {
        "request":         request,
        "manga_url":       url,
        "manga_title":     manga_title_display,
        "share_param":     share,
        "per_page":        mg.IMAGES_PER_LOAD,
        "webhook1":        webhook1,
        "webhook2":        webhook2,
        "scrambled":       scrambled,
        "tile_size":       tile_size,
        "webhook_enabled": webhook_enabled,
        "episode_public_id": episode_public_id,
    })

# ---------------------------------------------------------------------------
# Reaction relay  POST /reaction, GET /reaction_counts/{public_id}  (N-3)
# リアクションの実体はプラットフォーム側にある。ビューワーは中継だけを
# 行い、転送する本文は public_id と reaction_type のみ。本文に利用者の
# IP は載せない (段階2の乙-2と同じ決め方)。重複判定とレート制限は
# プラットフォーム側が「中継してきたビューワーの IP」で行う。
# ---------------------------------------------------------------------------

@app.post("/reaction")
@limiter.limit("30/minute")
async def reaction_relay(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    public_id     = str(payload.get("public_id") or "")
    reaction_type = str(payload.get("reaction_type") or "")
    if not public_id or not reaction_type:
        raise HTTPException(status_code=400, detail="パラメータが不足しています")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{PLATFORM_BASE_URL}/api/public/reaction",
                json={"public_id": public_id, "reaction_type": reaction_type},
            )
            if r.status_code == 200:
                return r.json()
            logger.warning(f"リアクション中継に失敗: {r.status_code}")
    except Exception as e:
        logger.warning(f"リアクション中継に失敗: {e}")
    return JSONResponse({"added": False, "counts": {}}, status_code=502)


@app.get("/reaction_counts/{public_id}")
async def reaction_counts_relay(public_id: str):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{PLATFORM_BASE_URL}/api/public/reactions/{public_id}")
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        logger.warning(f"リアクション数の取得に失敗: {e}")
    return JSONResponse({"counts": {}}, status_code=502)

# ---------------------------------------------------------------------------
# Image list partial  GET /images
# セッショントークン必須
# ---------------------------------------------------------------------------

@app.get("/images", response_class=HTMLResponse)
@limiter.limit("60/minute")
async def images_partial(
    request:       Request,
    url:           str = Query(...),
    offset:        int = Query(default=0),
    limit:         int = Query(default=0),
    webhook1:      str = Query(default=""),
    webhook2:      str = Query(default=""),
    session_token: str = Query(default=""),
):
    # セッション検証 (甲-4: 空トークンの素通りをやめ、必須にする)
    session = _require_session(session_token, url)

    url_hash     = hashlib.md5(url.encode()).hexdigest()
    file_ext     = os.path.splitext(url.split('?')[0])[-1].lower()
    archive_path = mg.get_cache_path(url) + file_ext
    extract_path = mg.get_cache_path(url) + "_extracted"

    if not os.path.exists(archive_path):
        return templates.TemplateResponse(request, "partials/download_pending.html", {
            "request":   request,
            "manga_url": url,
            "offset":    offset,
            "webhook1":  webhook1,
            "webhook2":  webhook2,
        })

    image_files, warnings = mg.extract_archive(archive_path, extract_path)
    total     = len(image_files)
    # N-20: offset は 0 以上に、limit は1回の応答で返す上限以下に丸める。
    # 負の offset は Python のスライスの意味で末尾側から数えてしまい、
    # 巨大な limit は全ページ分の HTML を1応答に詰め込めてしまう。
    # reader の通常利用は最大3枚ずつの要求なので、上限10でも壊れない。
    offset    = max(0, offset)
    page_size = limit if limit > 0 else mg.IMAGES_PER_LOAD
    page_size = max(1, min(page_size, mg.MAX_IMAGES_PER_REQUEST))
    batch     = image_files[offset: offset + page_size]
    next_offset = offset + len(batch)
    has_more  = next_offset < total

    # セッションにpage_countを記録
    if session_token:
        mg.set_session_page_count(session_token, total)

    image_items = []
    # N-22: seed_B はリクエストの IP から計算し直さず、セッション発行時に
    # 確定したものを使う。配信側 (B の種・透かし) とブラウザ側 (B の復号)
    # が同じ種を指すための統一。
    seed_b = session["seed_b"]
    # N-4: ブラウザへ渡す復号の合図。B を戻すかはセッションの旗
    # (配信時に B を掛けるか) とファイル名形式の両方、A を戻すかは
    # 作品設定で決まる「A が掛かっているか」の旗で別々に判断する。
    gate_b = bool(session.get("scrambled", True))
    flag_a = bool(session.get("a_scrambled", False))

    for i, path in enumerate(batch, start=offset):
        filename          = os.path.basename(path)
        name_is_delivery  = mg.is_scrambled_filename(filename)
        delivery_filename = mg.make_delivery_filename(filename, seed_b) \
                            if name_is_delivery else filename
        image_items.append({
            "src":      f"/image/{session_token}/{url_hash}/{filename}",
            "filename": delivery_filename,
            "index":    i,
            "total":    total,
            # B の合図は「配信時に B を掛けたか」だけで決まる (外部作品の
            # ファイル名はスクランブル形式でないため、名前に紐づけない)。
            # A の合図は、種を配信ファイル名から読む都合で形式も必要。
            "decode_b": gate_b,
            "decode_a": flag_a and name_is_delivery,
        })

    return templates.TemplateResponse(request, "partials/image_batch.html", {
        "request":     request,
        "image_items": image_items,
        "next_offset": next_offset,
        "has_more":    has_more,
        "total":       total,
        "manga_url":   url,
        "webhook1":    webhook1,
        "webhook2":    webhook2,
        "warnings":    warnings,
        # N-4: B 復号の種は配信ファイル名からではなくセッションの値を
        # そのまま渡す (外部作品ではファイル名に種が入らないため)
        "seed_b_hex":  f"{seed_b:08x}",
    })

# ---------------------------------------------------------------------------
# Image binary  GET /image/{session_token}/{url_hash}/{filename}
# ---------------------------------------------------------------------------

@app.get("/image/{session_token}/{url_hash}/{filename}")
@limiter.limit("60/minute")
async def serve_image(
    request:       Request,
    session_token: str,
    url_hash:      str,
    filename:      str,
):
    # セッション検証
    session = mg.validate_session(session_token)
    if not session:
        raise HTTPException(status_code=403, detail="セッションが無効または期限切れです")

    # url_hashがセッションのcbz_urlと一致するか確認
    expected_hash = hashlib.md5(session["cbz_url"].encode()).hexdigest()
    if url_hash != expected_hash:
        raise HTTPException(status_code=403, detail="不正なアクセスです")

    path = os.path.join(mg.get_cache_dir(), f"{url_hash}_extracted", filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="画像が見つかりません")

    # N-22: 透かしに入れる IP はリクエストごとの IP ではなく、
    # セッション発行時の IP に統一する。スクランブルBの種 (seed_B) と
    # 透かしの記録が、同じ「このセッションの持ち主」を指すようにする。
    watermark_ip = session["ip"]
    seed_b     = session["seed_b"]
    is_scrambled = session.get("scrambled", True)
    tile_size  = session.get("tile_size", 16)
    # N-15: ページ番号は先頭4文字を Base62 として読む (共通関数)
    page_index = mg.page_index_from_filename(filename)
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(session["read_at"], tz=timezone.utc)

    # delivery_filenameはScrambleBありなしで変わる
    delivery_filename = mg.make_delivery_filename(filename, seed_b) if is_scrambled else filename
    # 乙-4: キャッシュキーに透かしの IP の指紋を含める。スクランブル
    # 無しの作品では delivery_filename が全員共通のため、これが無いと
    # 最初の読者の透かし入り画像が全員に配られてしまう。
    cache_key  = mg.delivery_cache_key(url_hash, delivery_filename, watermark_ip)
    cache_dir  = os.path.join(mg.get_cache_dir(), "delivery")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, cache_key)

    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            data = f.read()
    else:
        loop = asyncio.get_running_loop()

        def process():
            from PIL import Image as PILImage
            with PILImage.open(path) as img:
                img = img.convert("RGB")
                if is_scrambled:
                    img = mg.scramble_image_pil(img, seed_b, tile_size)
                watermarked = mg.apply_watermark(img, seed_b, watermark_ip, page_index, dt)
                buf = BytesIO()
                watermarked.save(buf, format="PNG")
                return buf.getvalue()

        data = await loop.run_in_executor(_executor, process)
        with open(cache_path, "wb") as f:
            f.write(data)

    media_type = "image/png"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{delivery_filename}"',
            "Cache-Control":       "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )

# ---------------------------------------------------------------------------
# Download + progress  GET /download/start  (SSE)
# ---------------------------------------------------------------------------

@app.get("/download/start")
@limiter.limit("3/minute")
async def download_start(
    request:   Request,
    url:       str = Query(...),
    cbz_token: str = Query(default=""),
):
    # 甲-5: 内部宛先と分かるものはダウンロード開始前に断る
    # (実際の取得は manga.download_file 側でも毎ホップ検査する)
    if mg.url_targets_internal(url):
        raise HTTPException(status_code=400, detail="無効なURL")

    file_ext     = os.path.splitext(url.split('?')[0])[-1].lower()
    archive_path = mg.get_cache_path(url) + file_ext

    async def event_stream():
        if os.path.exists(archive_path):
            yield "data: done\n\n"
            return

        loop:  asyncio.AbstractEventLoop = asyncio.get_running_loop()
        queue: asyncio.Queue             = asyncio.Queue()

        def progress_cb(p: float):
            loop.call_soon_threadsafe(queue.put_nowait, p)

        def run_download():
            download_url  = url
            extra_headers = {}
            if cbz_token and "/api/public/cbz/" in url:
                download_url  = f"{url}?cbz_token={cbz_token}"
                extra_headers = {"Referer": get_viewer_base_url()}
            success, err = mg.download_file(
                download_url, archive_path,
                extra_headers=extra_headers,
                progress_callback=progress_cb,
            )
            if success:
                loop.call_soon_threadsafe(queue.put_nowait, "done")
            else:
                loop.call_soon_threadsafe(queue.put_nowait, f"error:{err}")

        loop.run_in_executor(_executor, run_download)

        while True:
            item = await queue.get()
            if item == "done":
                yield "data: done\n\n"
                break
            elif isinstance(item, str) and item.startswith("error:"):
                yield f"data: {item}\n\n"
                break
            else:
                yield f"data: {item:.3f}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

# ---------------------------------------------------------------------------
# Thumbnail  GET /thumbnail
# episode_thumbs/ から返す（非スクランブル・事前生成済み）
# ---------------------------------------------------------------------------

@app.get("/thumbnail")
@limiter.limit("30/minute")
async def serve_thumbnail(
    request:       Request,
    url:           str = Query(...),
    index:         int = Query(...),
    session_token: str = Query(default=""),
):
    # セッション検証 (甲-4: 空トークンの素通りをやめ、必須にする)
    _require_session(session_token, url)

    # ★ プラットフォームのCBZ URLから thumb URLを組み立てる（全て public_id ベース）
    # 公開URL:    {platform}/api/public/cbz/{public_id}.cbz
    #          →  {PLATFORM_BASE_URL}/api/public/thumb/{public_id}/{index}
    # プレビューURL: {platform}/api/author/preview/{public_id}.cbz?token=...
    #            →  {PLATFORM_BASE_URL}/api/author/thumb/{public_id}/{index}?token=...
    # 甲-4: 組み立てに使うホストは、利用者の URL からではなく
    # 設定済みの PLATFORM_BASE_URL だけにする (任意ホストへの SSRF 防止)。
    thumb_base = _platform_thumb_url(url)
    thumb_url = f"{thumb_base}/{index}" if thumb_base else None

    if thumb_url:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(thumb_url)
                if resp.status_code == 200:
                    # 甲-4: 返す Content-Type は画像系だけに限る。
                    # 相手が返した HTML 等をそのまま配らない。
                    ctype = (resp.headers.get("content-type") or "") \
                        .split(";")[0].strip().lower()
                    if ctype not in _ALLOWED_THUMB_CONTENT_TYPES:
                        raise HTTPException(
                            status_code=502,
                            detail="サムネイルの形式が正しくありません")
                    # N-8 の残り: 申告サイズ・実体のどちらかでも
                    # サムネイルとして大きすぎる応答は中継しない
                    declared = resp.headers.get("content-length")
                    if declared is not None:
                        try:
                            if int(declared) > MAX_THUMB_RESPONSE_BYTES:
                                raise HTTPException(
                                    status_code=502,
                                    detail="サムネイルが大きすぎます")
                        except ValueError:
                            pass
                    if len(resp.content) > MAX_THUMB_RESPONSE_BYTES:
                        raise HTTPException(
                            status_code=502,
                            detail="サムネイルが大きすぎます")
                    return Response(
                        content=resp.content,
                        media_type=ctype,
                        headers={"Cache-Control": "private, max-age=86400",
                                 "X-Content-Type-Options": "nosniff"},
                    )
        except HTTPException:
            raise
        except Exception:
            pass

    # フォールバック: キャッシュから生成（外部ZIP等）
    file_ext     = os.path.splitext(url.split('?')[0])[-1].lower()
    archive_path = mg.get_cache_path(url) + file_ext
    extract_path = mg.get_cache_path(url) + "_extracted"

    if not os.path.exists(archive_path):
        raise HTTPException(status_code=404, detail="アーカイブが見つかりません")

    image_files, _ = mg.extract_archive(archive_path, extract_path)
    if index < 0 or index >= len(image_files):
        raise HTTPException(status_code=404, detail="ページが見つかりません")

    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        _thumb_executor,
        lambda: mg.load_image_bytes(image_files[index], max_size=(80, 120)),
    )
    if data is None:
        raise HTTPException(status_code=500, detail="サムネイル生成失敗")

    media_type = "image/png" if data[:8] == b'\x89PNG\r\n\x1a\n' else "image/jpeg"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )

# ---------------------------------------------------------------------------
# Discord send  POST /discord/send
# ---------------------------------------------------------------------------

@app.post("/discord/send", response_class=HTMLResponse)
async def discord_send(
    request:  Request,
    url:      Annotated[str, Form()],
    filename: Annotated[str, Form()],
    webhook1: Annotated[str, Form()] = "",
    webhook2: Annotated[str, Form()] = "",
    session_token: Annotated[str, Form()] = "",
):
    # セッション検証
    if session_token:
        session = mg.validate_session(session_token)
        if not session or session["cbz_url"] != url:
            return HTMLResponse("<span class='status-error'>セッションが無効です</span>")
    else:
        return HTMLResponse("<span class='status-error'>セッションが無効です</span>")

    webhooks = [w for w in [webhook1, webhook2] if w]
    if not webhooks:
        return HTMLResponse("<span class='status-error'>Webhookが設定されていません</span>")

    # 甲-3: 送り先は Discord 公式の https Webhook URL だけに限る
    if any(not mg.validate_discord_webhook(w) for w in webhooks):
        return HTMLResponse("<span class='status-error'>WebhookのURLが許可されていません</span>")

    url_hash  = hashlib.md5(url.encode()).hexdigest()
    # N-22: こちらも透かしの IP はセッション発行時のものに統一し、
    # キャッシュキーも配信 (/image) と同じ式で作る (乙-4)
    client_ip = session["ip"]
    seed_b    = session["seed_b"]

    # 甲-3: filename は展開フォルダ配下に収まるものだけ許可する。
    # スクランブル判定は basename で行い、パス自体は _safe_child_path で検証する。
    extracted_dir = os.path.join(mg.get_cache_dir(), f"{url_hash}_extracted")
    safe_src_path = _safe_child_path(extracted_dir, filename)
    if safe_src_path is None:
        return HTMLResponse("<span class='status-error'>ファイル名が正しくありません</span>")

    if mg.is_scrambled_filename(filename):
        # スクランブルB＋ウォーターマーク済みのdeliveryキャッシュを使う
        delivery_filename = mg.make_delivery_filename(filename, seed_b)
        cache_key   = mg.delivery_cache_key(url_hash, delivery_filename, client_ip)
        cache_dir   = os.path.join(mg.get_cache_dir(), "delivery")
        cache_path  = os.path.join(cache_dir, cache_key)

        if not os.path.exists(cache_path):
            # キャッシュがない場合は生成する
            src_path = safe_src_path
            if not os.path.exists(src_path):
                return HTMLResponse("<span class='status-error'>画像が見つかりません</span>")
            loop      = asyncio.get_running_loop()
            tile_size = session.get("tile_size", 16)
            # N-15: ページ番号は先頭4文字を Base62 として読む (共通関数。
            # 段階1の暫定ガード「数字でなければ0」はこれに置き換えた)
            page_index = mg.page_index_from_filename(filename)
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(session["read_at"], tz=timezone.utc)

            def process():
                from PIL import Image as PILImage
                with PILImage.open(src_path) as img:
                    img       = img.convert("RGB")
                    scrambled = mg.scramble_image_pil(img, seed_b, tile_size)
                    watermarked = mg.apply_watermark(scrambled, seed_b, client_ip, page_index, dt)
                    buf = BytesIO()
                    watermarked.save(buf, format="PNG")
                    return buf.getvalue()

            os.makedirs(cache_dir, exist_ok=True)
            data = await loop.run_in_executor(_executor, process)
            with open(cache_path, "wb") as f:
                f.write(data)

        image_path = cache_path
    else:
        image_path = safe_src_path

    errors = mg.send_image_to_discord(image_path, webhooks, filename)
    if errors:
        # 丙-9: エラー文面は HTML として解釈されないようエスケープする
        escaped = "<br>".join(html.escape(str(e)) for e in errors)
        return HTMLResponse(f"<span class='status-error'>{escaped}</span>")
    return HTMLResponse("<span class='status-ok'>✓ 送信しました</span>")

# ---------------------------------------------------------------------------
# Share URL  GET /share/url
# ---------------------------------------------------------------------------

@app.get("/share/url")
async def share_url(
    request: Request,
    share:   str  = Query(default=""),
    shorten: bool = Query(default=True),
):
    base         = str(request.base_url).rstrip("/")
    encoded      = urllib.parse.quote(share, safe="")
    full_url     = f"{base}/?share={encoded}"
    result       = mg.shorten_url(full_url) if shorten else full_url
    return Response(content=result, media_type="text/plain")