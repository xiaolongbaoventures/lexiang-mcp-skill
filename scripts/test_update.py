"""update.py 测试（pytest，仅标准库 + 本地 file:// / http.server）。"""

import hashlib
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "update.py"
NAME = "lexiang-knowledge-base"

_spec = importlib.util.spec_from_file_location("lexiang_update", SCRIPT)
upd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(upd)


# ---------------------------------------------------------------- 夹具

def make_skill(root, build=1, version="2.2.3-hj.1", manifest_url="__MANIFEST_URL__"):
    d = Path(root) / "skills" / NAME
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: %s\nversion: %s\n---\nold\n" % (NAME, version), encoding="utf-8")
    (d / "VERSION.json").write_text(json.dumps({"name": NAME, "version": version, "build": build}), encoding="utf-8")
    (d / "update.json").write_text(json.dumps({"manifest_url": manifest_url, "check_interval_hours": 8}), encoding="utf-8")
    (d / "mcp.json").write_text("old-mcp", encoding="utf-8")
    shutil.copy2(SCRIPT, d / "scripts" / "update.py")
    return d


def make_zip(path, build=2, version="2.2.3-hj.2", prefix=NAME + "/", extra=None, skip=()):
    files = {
        "SKILL.md": "---\nname: %s\nversion: %s\n---\nnew\n" % (NAME, version),
        "VERSION.json": json.dumps({"name": NAME, "version": version, "build": build}),
        "update.json": json.dumps({"manifest_url": "__MANIFEST_URL__", "check_interval_hours": 8}),
        "scripts/update.py": SCRIPT.read_text(encoding="utf-8"),
        "references/update.md": "new doc",
    }
    with zipfile.ZipFile(path, "w") as zf:
        for rel, content in files.items():
            if rel not in skip:
                zf.writestr(prefix + rel, content)
        for name, content in (extra or {}).items():
            zf.writestr(name, content)
    return path


def publish(root, zip_path, build=2, version="2.2.3-hj.2", sha=None, size=None, name=NAME, base_url=None):
    data = Path(zip_path).read_bytes()
    zip_url = (base_url + "/" + Path(zip_path).name) if base_url else Path(zip_path).as_uri()
    m = {"name": name, "version": version, "build": build, "zip_url": zip_url,
         "sha256": sha or hashlib.sha256(data).hexdigest(), "size": size if size is not None else len(data),
         "notes": "修复若干问题。", "released_at": "2026-11-02T12:00:00+08:00"}
    mp = Path(root) / "latest.json"
    mp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return (base_url + "/latest.json") if base_url else mp.as_uri()


def set_url(skill, url):
    (skill / "update.json").write_text(json.dumps({"manifest_url": url, "check_interval_hours": 8}), encoding="utf-8")


def run(skill, *args):
    """以子进程运行安装目录里的 update.py（验证 __file__ 定位）。"""
    r = subprocess.run([sys.executable, str(skill / "scripts" / "update.py")] + list(args),
                       capture_output=True, text=True, encoding="utf-8", cwd=str(skill))
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, r.stdout + r.stderr
    return r.returncode, json.loads(lines[0])


def snapshot(d):
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(Path(d).rglob("*")) if p.is_file()}


@pytest.fixture
def env(tmp_path):
    skill = make_skill(tmp_path)
    return tmp_path, skill


# ---------------------------------------------------------------- check

def test_placeholder_url_skips(env):
    _, skill = env
    code, out = run(skill, "check", "--force")
    assert code == 0 and out["status"] == "skipped" and out["reason"] == "not_configured"
    assert not (skill / ".last_update_check").exists()


def test_check_up_to_date_and_update_available(env):
    root, skill = env
    url = publish(root, make_zip(root / "p.zip"), build=1, version="2.2.3-hj.1")
    set_url(skill, url)
    code, out = run(skill, "check", "--force")
    assert code == 0 and out["status"] == "up_to_date"
    url = publish(root, make_zip(root / "p.zip"))
    code, out = run(skill, "check", "--force")
    assert out["status"] == "update_available" and out["latest"] == "2.2.3-hj.2"
    assert out["latest_build"] == 2 and out["notes"] == "修复若干问题。"


def test_interval_throttling(env):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    assert run(skill, "check")[1]["status"] == "update_available"
    assert (skill / ".last_update_check").exists()
    assert run(skill, "check")[1]["reason"] == "checked_recently"
    assert run(skill, "check", "--force")[1]["status"] == "update_available"
    (skill / ".last_update_check").write_text(str(int(time.time()) - 25 * 3600), encoding="utf-8")
    assert run(skill, "check")[1]["status"] == "update_available"


def test_network_error_is_quiet_and_recorded(env):
    root, skill = env
    set_url(skill, (root / "missing.json").as_uri())
    code, out = run(skill, "check")
    assert code == 0 and out["status"] == "error"
    assert (skill / ".last_update_check").exists()


def test_name_mismatch(env):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip"), name="other-skill"))
    assert run(skill, "check", "--force")[1]["reason"] == "name_mismatch"
    code, out = run(skill, "apply")
    assert code == 1 and out["status"] == "failed"


def test_check_over_http(env):
    root, skill = env
    make_zip(root / "p.zip")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        set_url(skill, publish(root, root / "p.zip", base_url=base))
        assert run(skill, "check", "--force")[1]["status"] == "update_available"
        code, out = run(skill, "apply")
        assert code == 0 and out["status"] == "updated", out
    finally:
        srv.shutdown()


def test_non_local_http_rejected():
    assert not upd.is_configured("http://example.com/latest.json")
    assert upd.is_configured("https://example.com/latest.json")
    assert not upd.is_configured("__MANIFEST_URL__")


# ---------------------------------------------------------------- apply

def _assert_untouched(skill, before, root):
    assert snapshot(skill) == before
    assert not skill.with_name(NAME + ".bak").exists()


def test_sha256_mismatch_aborts(env):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip"), sha="0" * 64))
    before = snapshot(skill)
    code, out = run(skill, "apply")
    assert code == 1 and "sha256" in out["message"]
    _assert_untouched(skill, before, root)


def test_size_mismatch_aborts(env):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip"), size=10))
    before = snapshot(skill)
    code, out = run(skill, "apply")
    assert code == 1 and out["status"] == "failed"
    _assert_untouched(skill, before, root)


@pytest.mark.parametrize("kwargs", [
    {"prefix": "other-dir/"},
    {"extra": {"stray.txt": "x"}},
    {"skip": ("VERSION.json",)},
    {"build": 3},  # zip 内 build 与 manifest 不一致
])
def test_bad_package_rejected(env, kwargs):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip", **kwargs)))
    before = snapshot(skill)
    code, out = run(skill, "apply")
    assert code == 1 and out["status"] == "failed"
    _assert_untouched(skill, before, root)


@pytest.mark.parametrize("evil", [NAME + "/../evil.txt", NAME + "/../../evil.txt", "/" + NAME + "/abs.txt"])
def test_zip_slip_rejected(env, evil):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip", extra={evil: "pwned"})))
    before = snapshot(skill)
    code, _ = run(skill, "apply")
    assert code == 1
    _assert_untouched(skill, before, root)
    assert not list(root.rglob("evil.txt"))


def test_apply_and_rollback(env):
    root, skill = env
    url = publish(root, make_zip(root / "p.zip"))
    set_url(skill, url)
    code, out = run(skill, "apply")
    assert code == 0 and out["status"] == "updated" and out["to"] == "2.2.3-hj.2"
    bak = skill.with_name(NAME + ".bak")
    assert json.loads((skill / "VERSION.json").read_text())["build"] == 2
    assert "new" in (skill / "SKILL.md").read_text(encoding="utf-8")
    assert not (skill / "mcp.json").exists()  # 新包没有该文件
    assert (skill / ".last_update_check").exists()
    assert (bak / "mcp.json").read_text() == "old-mcp"
    assert not (bak / "SKILL.md").exists() and (bak / upd.DISABLED_SKILL_MD).exists()

    # 再次 apply：已是最新（新包里 update.json 为占位符，测试中重新指向本地 manifest）
    set_url(skill, url)
    code, out = run(skill, "apply")
    assert code == 0 and out["status"] == "up_to_date"

    code, out = run(skill, "rollback")
    assert code == 0 and out["status"] == "rolled_back" and out["to"] == "2.2.3-hj.1"
    assert json.loads((skill / "VERSION.json").read_text())["build"] == 1
    assert (skill / "SKILL.md").exists() and (skill / "mcp.json").read_text() == "old-mcp"
    assert json.loads((bak / "VERSION.json").read_text())["build"] == 2
    assert (bak / upd.DISABLED_SKILL_MD).exists()
    assert sorted(p.name for p in skill.parent.iterdir()) == [NAME, NAME + ".bak"]


def test_rollback_without_backup(env):
    _, skill = env
    code, out = run(skill, "rollback")
    assert code == 1 and "备份" in out["message"]


def test_replaces_older_backup(env):
    root, skill = env
    old_bak = skill.with_name(NAME + ".bak")
    old_bak.mkdir()
    (old_bak / "ancient.txt").write_text("x")
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    assert run(skill, "apply")[0] == 0
    assert not (old_bak / "ancient.txt").exists()


def test_failure_mid_apply_restores(env, monkeypatch, capsys):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    before = snapshot(skill)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(upd, "_post_install", boom)
    code = upd.main(["apply"], skill_dir=skill)
    out = json.loads(capsys.readouterr().out.strip())
    assert code == 1 and "已恢复原版本" in out["message"]
    assert snapshot(skill) == before


def test_last_check_carried_over(env, monkeypatch, capsys):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    (skill / ".last_update_check").write_text("12345", encoding="utf-8")
    monkeypatch.setattr(upd, "_write_last_check", lambda d: None)
    assert upd.main(["apply"], skill_dir=skill) == 0
    capsys.readouterr()
    assert (skill / ".last_update_check").read_text(encoding="utf-8") == "12345"


def test_failure_during_move_restores(env, monkeypatch, capsys):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    before = snapshot(skill)

    def bad_move(src, dst):
        Path(dst).mkdir()
        (Path(dst) / "partial").write_text("x")
        raise OSError("copy interrupted")

    monkeypatch.setattr(upd.shutil, "move", bad_move)
    code = upd.main(["apply"], skill_dir=skill)
    capsys.readouterr()
    assert code == 1 and snapshot(skill) == before


def test_windows_rename_blocked_fallback(env, monkeypatch, capsys):
    """模拟 Windows 目录被占用无法改名：走内容复制路径，结果一致。"""
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    real_rename = os.rename

    def guarded(src, dst):
        if Path(src) == skill:
            raise PermissionError("in use")
        return real_rename(src, dst)

    monkeypatch.setattr(upd.os, "rename", guarded)
    assert upd.main(["apply"], skill_dir=skill) == 0
    assert json.loads((skill / "VERSION.json").read_text())["build"] == 2
    assert (skill.with_name(NAME + ".bak") / "mcp.json").read_text() == "old-mcp"
    assert upd.main(["rollback"], skill_dir=skill) == 0
    capsys.readouterr()
    assert json.loads((skill / "VERSION.json").read_text())["build"] == 1


def test_refuses_git_checkout(env):
    root, skill = env
    (skill / ".git").mkdir()
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    code, out = run(skill, "apply")
    assert code == 1 and ".git" in out["message"]


def test_version(env):
    _, skill = env
    code, out = run(skill, "version")
    assert code == 0 and out == {"name": NAME, "version": "2.2.3-hj.1", "build": 1}
