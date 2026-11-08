"""段階1修正のテスト共通基盤。

- プラットフォーム (mangaupload_yomima_site/main.py) はカレントディレクトリ
  相対で DB / storage を作るため、import 前に一時ディレクトリへ chdir する。
- ビューワー (yomima/main.py) は `import manga` というフラット import を
  使うため、yomima ディレクトリを sys.path に入れてから読み込む。
- どちらの app もモジュール名が main.py で衝突するので、importlib で
  別名 (platform_main / viewer_main) として読み込む。
"""
import importlib.util
import os
import sys
import tempfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_DIR = os.path.join(REPO_ROOT, "mangaupload_yomima_site")
VIEWER_DIR = os.path.join(SITE_DIR, "yomima")

os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pass")
# 甲-1/甲-2 (段階2追加): 鍵は環境変数必須になったため、テスト用の値を渡す
os.environ.setdefault("SECRET_KEY", "test-secret-stage2-key")
os.environ.setdefault("ADMIN_SECRET_KEY", "test-admin-secret-stage2")
# ビューワーがプラットフォームを呼ぶ先 (テストでは実サーバーは立てない)
os.environ.setdefault("PLATFORM_BASE_URL", "http://localhost:8000")

_WORKDIR = tempfile.mkdtemp(prefix="yomima_stage1_")
os.chdir(_WORKDIR)

if VIEWER_DIR not in sys.path:
    sys.path.insert(0, VIEWER_DIR)
if SITE_DIR not in sys.path:
    sys.path.insert(0, SITE_DIR)


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def manga():
    import manga as mg  # yomima/manga.py (フラット import)
    return mg


@pytest.fixture(scope="session")
def viewer_main(manga):
    return _load("viewer_main", os.path.join(VIEWER_DIR, "main.py"))


@pytest.fixture(scope="session")
def platform_main():
    return _load("platform_main", os.path.join(SITE_DIR, "main.py"))


@pytest.fixture()
def cache_dir(tmp_path, manga, monkeypatch):
    """manga.get_cache_dir をテスト専用ディレクトリへ差し替える。"""
    d = tmp_path / "manga_cache"
    d.mkdir()
    monkeypatch.setattr(manga, "get_cache_dir", lambda: str(d))
    return d
