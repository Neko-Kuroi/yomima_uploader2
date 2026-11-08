"""甲-1 / 甲-2 (案ア): 鍵は環境変数から読む。

サブプロセスで import の成否を確認する。SECRET_KEY が無いと起動時に
停止すること、設定すれば通常どおり動くこと、ADMIN_PASSWORD があるのに
ADMIN_SECRET_KEY だけ無い場合は停止すること。
"""
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_DIR = os.path.join(REPO, "mangaupload_yomima_site")
VENV_PY = os.path.join(REPO, ".venv_audit", "bin", "python")
PYTHON = VENV_PY if os.path.exists(VENV_PY) else sys.executable

def _run_import(extra_env, remove=()):
    env = dict(os.environ)
    for k in remove:
        env.pop(k, None)
    env.update(extra_env)
    env["INTERNAL_API_KEY"] = "subprocess-internal"
    workdir = tempfile.mkdtemp(prefix="yomima_stage2_env_")
    # 文字列の置き換えで組み立てると、リポジトリが一時フォルダの下にある
    # 場合に SITE_DIR まで置き換わってしまうため、部品ごとに repr で埋める
    snippet = (
        "import os, sys;"
        f"os.chdir({workdir!r});"
        f"sys.path.insert(0, {SITE_DIR!r});"
        "import main; print('IMPORTED', bool(main.SECRET_KEY))"
    )
    return subprocess.run([PYTHON, "-c", snippet], env=env,
                          capture_output=True, text=True, timeout=120)


def test_import_fails_without_secret_key():
    r = _run_import({}, remove=("SECRET_KEY",))
    assert r.returncode != 0, r.stdout
    assert "SECRET_KEY" in (r.stderr + r.stdout)


def test_import_fails_with_empty_secret_key():
    r = _run_import({"SECRET_KEY": ""})
    assert r.returncode != 0, r.stdout
    assert "SECRET_KEY" in (r.stderr + r.stdout)


def test_import_works_with_secret_key():
    r = _run_import({"SECRET_KEY": "subprocess-secret-key"})
    assert r.returncode == 0, r.stderr
    assert "IMPORTED True" in r.stdout


def test_import_fails_when_admin_password_set_but_no_admin_secret():
    r = _run_import(
        {"SECRET_KEY": "subprocess-secret-key",
         "ADMIN_PASSWORD": "some-admin-pass"},
        remove=("ADMIN_SECRET_KEY",))
    assert r.returncode != 0, r.stdout
    assert "ADMIN_SECRET_KEY" in (r.stderr + r.stdout)


def test_import_works_without_admin_when_both_unset():
    # 管理者を使わない通常運用まで起動不能にはしない
    r = _run_import({"SECRET_KEY": "subprocess-secret-key"},
                    remove=("ADMIN_PASSWORD", "ADMIN_SECRET_KEY"))
    assert r.returncode == 0, r.stderr
