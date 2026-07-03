"""
gigafile.py
~~~~~~~~~~~
Gigafile.nu 非同期チャンクアップローダー + ダウンローダー + 削除（パイプラインB方式）

パイプラインB:
    読み込み(asyncio) と 送信(aiohttp) をオーバーラップさせる。
    常に「送信中チャンク + 先読みチャンク」の最大2チャンク分がメモリに乗る。
    chunk_size_mb=35 → 最大 70MB 程度。

依存:
    pip install aiohttp aiofiles tqdm nest_asyncio beautifulsoup4
"""

import asyncio
import math
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import aiohttp
from bs4 import BeautifulSoup
from tqdm import tqdm
import nest_asyncio
nest_asyncio.apply()

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
GIGAFILE_TOP    = "https://gigafile.nu/"
SERVER_RE       = re.compile(r'var\s+server\s*=\s*"([^"]+)"')
FILENAME_RE     = re.compile(r'filename\*?=(?:UTF-8\'\')?([^;\r\n]+)', re.IGNORECASE)
DEFAULT_CHUNK_MB  = 35
DEFAULT_LIFETIME  = 100
DEFAULT_RETRIES   = 3


# ===========================================================================
# アップロード
# ===========================================================================
async def gigafile_upload_async(
    file_path: str,
    chunk_size_mb: int = DEFAULT_CHUNK_MB,
    lifetime_days: int = DEFAULT_LIFETIME,
    dl_password: str | None = None,
    progress: bool = True,
    retries: int = DEFAULT_RETRIES,
) -> dict:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"ファイルが見つからないにゃ: {path}")

    total_size = path.stat().st_size
    if total_size == 0:
        raise ValueError("空ファイルはアップロードできないにゃ")

    chunk_size   = chunk_size_mb * 1024 * 1024
    total_chunks = math.ceil(total_size / chunk_size)
    filename     = path.name

    async with aiohttp.ClientSession() as session:

        async with session.get(GIGAFILE_TOP, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            top_html = await resp.text()

        match = SERVER_RE.search(top_html)
        if not match:
            raise RuntimeError(f"サーバー取得失敗 (body[:300]={top_html[:300]!r})")
        server     = match.group(1)
        token      = uuid.uuid1().hex
        upload_url = f"https://{server}/upload_chunk.php"

        print(
            f"[gigafile] {filename}  "
            f"{total_size / (1024**2):.1f} MB  "
            f"→ {total_chunks} chunks × {chunk_size_mb}MB  "
            f"(server={server})"
        )

        pbar = tqdm(
            total=total_size,
            unit="B", unit_scale=True, unit_divisor=1024,
            desc=filename, dynamic_ncols=True,
        ) if progress else None

        async def read_chunk(chunk_no: int) -> bytes:
            start = chunk_no * chunk_size
            end   = min(start + chunk_size, total_size)
            async with aiofiles.open(path, "rb") as f:
                await f.seek(start)
                return await f.read(end - start)

        async def send_chunk(chunk_no: int, chunk_data: bytes) -> dict:
            checkpoint = pbar.n if pbar else 0
            this_size  = len(chunk_data)

            for attempt in range(retries):
                if attempt > 0:
                    if pbar:
                        pbar.n = checkpoint
                        pbar.refresh()
                    wait = 2 ** (attempt - 1)
                    print(f"\n⚠️  chunk {chunk_no} リトライ {attempt}/{retries-1} (wait {wait}s)")
                    await asyncio.sleep(wait)

                try:
                    form = aiohttp.FormData()
                    form.add_field("id",       token)
                    form.add_field("name",     filename)
                    form.add_field("chunk",    str(chunk_no))
                    form.add_field("chunks",   str(total_chunks))
                    form.add_field("lifetime", str(lifetime_days))
                    if dl_password:
                        form.add_field("dlkey", dl_password)
                    form.add_field(
                        "file", chunk_data,
                        filename="blob",
                        content_type="application/octet-stream",
                    )

                    async with session.post(
                        upload_url, data=form,
                        timeout=aiohttp.ClientTimeout(total=180),
                    ) as resp:
                        resp.raise_for_status()
                        result = await resp.json(content_type=None)

                    if pbar:
                        already = pbar.n - checkpoint
                        pbar.update(this_size - already)

                    return result

                except aiohttp.ClientResponseError as e:
                    if attempt == retries - 1:
                        raise RuntimeError(f"chunk {chunk_no} が {retries}回失敗") from e
                except Exception as e:
                    if attempt == retries - 1:
                        raise RuntimeError(f"chunk {chunk_no} が {retries}回失敗") from e

        last_result: dict = {}
        try:
            prefetch: asyncio.Task = asyncio.create_task(read_chunk(0))
            for i in range(total_chunks):
                chunk_data = await prefetch
                if i + 1 < total_chunks:
                    prefetch = asyncio.create_task(read_chunk(i + 1))
                last_result = await send_chunk(i, chunk_data)
        finally:
            if pbar:
                pbar.close()

        url = last_result.get("url")
        if not url:
            if last_result.get("status") == 0:
                url = f"https://{server}/{token}"
            else:
                raise RuntimeError(f"アップロード失敗 (response={last_result!r})")

        del_key: str | None = last_result.get("del_key") or last_result.get("delkey")
        if not del_key:
            del_key = await _fetch_del_key_from_page(session, url)

        if del_key:
            print(f"\n✅ アップロード完了: {url}")
            print(f"🔑 削除キー: {del_key}")
        else:
            print(f"\n✅ アップロード完了: {url}")
            print("⚠️  削除キーを取得できませんでした")

        return {"url": url, "del_key": del_key}


async def _fetch_del_key_from_page(session: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            html = await resp.text()
    except Exception as e:
        print(f"⚠️  ダウンロードページ取得エラー: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    tag = soup.find("input", {"name": re.compile(r"del_?key", re.I)})
    if tag and tag.get("value"):
        return tag["value"].strip()

    tag = soup.find(attrs={"data-del-key": True})
    if tag:
        return tag["data-del-key"].strip()

    m = re.search(r"del_?key['\"]?\s*[=:]\s*['\"]([A-Za-z0-9_\-]+)['\"]", html, re.I)
    if m:
        return m.group(1)

    return None


def gigafile_upload(
    file_path: str,
    chunk_size_mb: int = DEFAULT_CHUNK_MB,
    lifetime_days: int = DEFAULT_LIFETIME,
    dl_password: str | None = None,
    progress: bool = True,
    retries: int = DEFAULT_RETRIES,
) -> dict:
    return asyncio.run(gigafile_upload_async(
        file_path, chunk_size_mb, lifetime_days, dl_password, progress, retries,
    ))


# ===========================================================================
# ダウンロード
# ===========================================================================
STREAM_CHUNK_BYTES = 512 * 1024


async def gigafile_download_async(
    url: str,
    output: str | None = None,
    dl_password: str | None = None,
    progress: bool = True,
    retries: int = DEFAULT_RETRIES,
) -> str:
    save_path = None

    async with aiohttp.ClientSession() as session:

        print(f"[gigafile] ページ取得: {url}")
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                page_html = await resp.text()
        except aiohttp.ClientError as e:
            raise RuntimeError(f"ダウンロードページ取得失敗: {e}") from e

        server_match = SERVER_RE.search(page_html)
        server = server_match.group(1) if server_match else urlparse(url).netloc

        file_id_match = re.search(r"/([a-z0-9-]+)$", url)
        if not file_id_match:
            raise RuntimeError("ファイルIDを抽出できませんでした")

        file_id      = file_id_match.group(1)
        download_url = f"https://{server}/download.php?file={file_id}"
        if dl_password:
            download_url += f"&dlkey={dl_password}"

        for attempt in range(retries):
            if attempt > 0:
                wait = 2 ** (attempt - 1)
                await asyncio.sleep(wait)

            try:
                async with session.get(
                    download_url, timeout=aiohttp.ClientTimeout(total=300),
                ) as resp:
                    resp.raise_for_status()

                    cd = resp.headers.get("Content-Disposition", "")
                    fn_match = FILENAME_RE.search(cd)
                    remote_name = (
                        fn_match.group(1).strip().strip("\"'")
                        if fn_match
                        else Path(url).name or "gigafile_download"
                    )

                    save_path  = Path(output or remote_name).resolve()
                    total_size = int(resp.headers.get("Content-Length", 0))

                    pbar = tqdm(
                        total=total_size or None,
                        unit="B", unit_scale=True, unit_divisor=1024,
                        desc=save_path.name, dynamic_ncols=True,
                    ) if progress else None

                    try:
                        async with aiofiles.open(save_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(STREAM_CHUNK_BYTES):
                                await f.write(chunk)
                                if pbar:
                                    pbar.update(len(chunk))
                    finally:
                        if pbar:
                            pbar.close()

                print(f"\n✅ ダウンロード完了: {save_path}")
                return str(save_path)

            except Exception as e:
                if attempt == retries - 1:
                    if save_path and save_path.exists():
                        save_path.unlink(missing_ok=True)
                    raise RuntimeError(f"ダウンロードが {retries}回失敗しました: {e}") from e

    raise RuntimeError("ダウンロードに失敗しました")


def gigafile_download(
    url: str,
    output: str | None = None,
    dl_password: str | None = None,
    progress: bool = True,
    retries: int = DEFAULT_RETRIES,
) -> str:
    return asyncio.run(gigafile_download_async(url, output, dl_password, progress, retries))


# ===========================================================================
# プロキシストリーミング（ディスクに保存せずそのまま転送する）
# ===========================================================================
async def gigafile_proxy_stream_async(
    page_url: str,
    dl_password: str | None = None,
):
    """
    Gigafileの案内ページURLから実ファイルを取得し、ディスクに保存せず
    チャンク単位でそのままyieldする。プラットフォーム側でのプロキシ配信用。

    gigafile_download_async() と同じ「ページ取得 → サーバー抽出 →
    download.php URL組み立て」のロジックを流用しているが、ローカルファイルへの
    書き込みは行わず、呼び出し元（StreamingResponse等）にバイト列を渡すだけ。
    """
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                page_url, timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                page_html = await resp.text()
        except aiohttp.ClientError as e:
            raise RuntimeError(f"ダウンロードページ取得失敗: {e}") from e

        server_match = SERVER_RE.search(page_html)
        server = server_match.group(1) if server_match else urlparse(page_url).netloc

        file_id_match = re.search(r"/([a-z0-9-]+)$", page_url)
        if not file_id_match:
            raise RuntimeError("ファイルIDを抽出できませんでした")
        file_id = file_id_match.group(1)

        download_url = f"https://{server}/download.php?file={file_id}"
        if dl_password:
            download_url += f"&dlkey={dl_password}"

        async with session.get(
            download_url, timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(STREAM_CHUNK_BYTES):
                yield chunk


# ===========================================================================
# 削除
# ===========================================================================
async def gigafile_delete_async(
    url: str,
    del_key: str,
    retries: int = DEFAULT_RETRIES,
) -> bool:
    if not url or not del_key:
        raise ValueError("url と del_key は必須です")

    async with aiohttp.ClientSession() as session:

        print(f"[gigafile] 削除対象ページ取得: {url}")
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                page_html = await resp.text()
        except aiohttp.ClientError as e:
            raise RuntimeError(f"ページ取得失敗: {e}") from e

        delete_url = _find_delete_endpoint(page_html, url)

        file_id_match = re.search(r"/([a-z0-9-]+)$", url)
        if not file_id_match:
            raise RuntimeError("ファイルIDを抽出できませんでした")
        file_id = file_id_match.group(1)

        for attempt in range(retries):
            if attempt > 0:
                wait = 2 ** (attempt - 1)
                await asyncio.sleep(wait)

            try:
                form = aiohttp.FormData()
                form.add_field("file",    file_id)
                form.add_field("del_key", del_key)

                async with session.post(
                    delete_url, data=form,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    body = await resp.text()

                success = _parse_delete_response(body, resp.status)
                if success:
                    print(f"✅ 削除完了: {url}")
                    return True
                else:
                    if attempt == retries - 1:
                        print(f"❌ 削除失敗 (status={resp.status})")
                        return False

            except Exception as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"削除が {retries}回失敗しました: {e}") from e

    return False


def _find_delete_endpoint(page_html: str, page_url: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")

    for form in soup.find_all("form"):
        has_del = form.find("input", {"name": re.compile(r"del_?key", re.I)})
        action = form.get("action", "")
        if has_del and action:
            if action.startswith("http"):
                return action
            parsed = urlparse(page_url)
            return f"{parsed.scheme}://{parsed.netloc}/{action.lstrip('/')}"

    m = re.search(r"['\"]([^'\"]*delete[^'\"]*\.php[^'\"]*)['\"]", page_html, re.I)
    if m:
        endpoint = m.group(1)
        if endpoint.startswith("http"):
            return endpoint
        parsed = urlparse(page_url)
        return f"{parsed.scheme}://{parsed.netloc}/{endpoint.lstrip('/')}"

    parsed = urlparse(page_url)
    return f"{parsed.scheme}://{parsed.netloc}/delete.php"


def _parse_delete_response(body: str, status_code: int) -> bool:
    try:
        import json
        data = json.loads(body)
        if isinstance(data, dict):
            s = data.get("status")
            if s == 0 or s == "ok" or data.get("result") == "ok":
                return True
            if s and s != 0:
                return False
    except Exception:
        pass

    if status_code == 200:
        lower = body.lower()
        if any(k in lower for k in ["削除しました", "deleted", "success", "完了"]):
            return True
        if any(k in lower for k in ["失敗", "error", "invalid", "不正"]):
            return False
        return True

    return False


def gigafile_delete(url: str, del_key: str, retries: int = DEFAULT_RETRIES) -> bool:
    return asyncio.run(gigafile_delete_async(url, del_key, retries))