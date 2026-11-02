"""更新脚本行为测试：同一组用例分别跑 update.py / update.mjs / update.ps1（pytest + 本地 file:// / http.server）。

node 或 pwsh 不存在时对应参数自动跳过。
"""

import hashlib
import http.server
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = {"python": "update.py", "node": "update.mjs", "pwsh": "update.ps1"}
NAME = "lexiang-knowledge-base"
# 可用 LEXIANG_TEST_NODE / LEXIANG_TEST_PWSH 指定解释器路径（如测试 Node 18）
NODE = os.environ.get("LEXIANG_TEST_NODE") or shutil.which("node")
PWSH = os.environ.get("LEXIANG_TEST_PWSH") or shutil.which("pwsh") or shutil.which("powershell")


def _cmd(impl, script):
    if impl == "python":
        return [sys.executable, str(script)]
    if impl == "node":
        return [NODE, str(script)]
    return [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]


IMPLS = [
    pytest.param("python", id="python"),
    pytest.param("node", id="node", marks=pytest.mark.skipif(not NODE, reason="node 未安装")),
    pytest.param("pwsh", id="pwsh", marks=pytest.mark.skipif(not PWSH, reason="PowerShell 未安装")),
]


# ---------------------------------------------------------------- 夹具

def _copy_scripts(scripts_dir):
    for name in SCRIPTS.values():
        shutil.copy2(HERE / name, scripts_dir / name)


def make_skill(root, build=1, version="2.2.3-hj.1", manifest_url="__MANIFEST_URL__"):
    d = Path(root) / "skills" / NAME
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: %s\nversion: %s\n---\nold\n" % (NAME, version), encoding="utf-8")
    (d / "VERSION.json").write_text(json.dumps({"name": NAME, "version": version, "build": build}), encoding="utf-8")
    (d / "update.json").write_text(json.dumps({"manifest_url": manifest_url, "check_interval_hours": 8}), encoding="utf-8")
    (d / "mcp.json").write_text("old-mcp", encoding="utf-8")
    _copy_scripts(d / "scripts")
    return d


def make_zip(path, build=2, version="2.2.3-hj.2", prefix=NAME + "/", extra=None, skip=(),
             compression=zipfile.ZIP_DEFLATED, symlink=None):
    files = {
        "SKILL.md": "---\nname: %s\nversion: %s\n---\nnew\n" % (NAME, version),
        "VERSION.json": json.dumps({"name": NAME, "version": version, "build": build}),
        "update.json": json.dumps({"manifest_url": "__MANIFEST_URL__", "check_interval_hours": 8}),
        "references/update.md": "新文档",
    }
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        zf.writestr(prefix, "")  # 目录项
        for rel, content in files.items():
            if rel not in skip:
                zf.writestr(prefix + rel, content)
        for name in SCRIPTS.values():
            zf.write(HERE / name, prefix + "scripts/" + name)
        for name, content in (extra or {}).items():
            zf.writestr(name, content)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
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


def run(skill, impl, *args, fault=None):
    """以子进程运行安装目录里的脚本（验证脚本自身定位 Skill 目录）。"""
    env = dict(os.environ)
    env.pop("LEXIANG_UPDATE_FAULT", None)
    if fault:
        env["LEXIANG_UPDATE_FAULT"] = fault
    r = subprocess.run(_cmd(impl, skill / "scripts" / SCRIPTS[impl]) + list(args), capture_output=True,
                       cwd=str(skill), env=env, timeout=120)
    stdout = r.stdout.decode("utf-8")
    assert not r.stdout.startswith(b"\xef\xbb\xbf"), "stdout 不应带 BOM"
    lines = [l for l in stdout.splitlines() if l.strip()]
    assert len(lines) == 1, stdout + r.stderr.decode("utf-8", "replace")
    return r.returncode, json.loads(lines[0])


def snapshot(d):
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(Path(d).rglob("*")) if p.is_file()}


def build_of(d):
    return json.loads((Path(d) / "VERSION.json").read_text(encoding="utf-8"))["build"]


@pytest.fixture(params=IMPLS)
def impl(request):
    return request.param


@pytest.fixture
def env(tmp_path):
    return tmp_path, make_skill(tmp_path)


def _assert_untouched(skill, before):
    assert snapshot(skill) == before
    assert not skill.with_name(NAME + ".bak").exists()


# ---------------------------------------------------------------- check

def test_placeholder_url_skips(env, impl):
    _, skill = env
    code, out = run(skill, impl, "check", "--force")
    assert code == 0 and out == {"status": "skipped", "reason": "not_configured", "current": "2.2.3-hj.1", "current_build": 1}
    assert not (skill / ".last_update_check").exists()


@pytest.mark.parametrize("url", ["http://example.com/latest.json", "ftp://127.0.0.1/latest.json", ""])
def test_disallowed_url_not_configured(env, impl, url):
    _, skill = env
    set_url(skill, url)
    assert run(skill, impl, "check", "--force")[1]["reason"] == "not_configured"


def test_check_up_to_date_and_update_available(env, impl):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip"), build=1, version="2.2.3-hj.1"))
    code, out = run(skill, impl, "check", "--force")
    assert code == 0 and out["status"] == "up_to_date"
    publish(root, make_zip(root / "p.zip"))
    code, out = run(skill, impl, "check", "--force")
    assert out == {"current": "2.2.3-hj.1", "current_build": 1, "latest": "2.2.3-hj.2", "latest_build": 2,
                   "notes": "修复若干问题。", "status": "update_available"}


def test_interval_throttling(env, impl):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    assert run(skill, impl, "check")[1]["status"] == "update_available"
    stamp = (skill / ".last_update_check").read_text(encoding="utf-8")
    assert stamp.isdigit() and abs(int(stamp) - time.time()) < 60
    assert run(skill, impl, "check")[1]["reason"] == "checked_recently"
    assert run(skill, impl, "check", "--force")[1]["status"] == "update_available"
    (skill / ".last_update_check").write_text(str(int(time.time()) - 7 * 3600), encoding="utf-8")
    assert run(skill, impl, "check")[1]["reason"] == "checked_recently"
    (skill / ".last_update_check").write_text(str(int(time.time()) - 9 * 3600), encoding="utf-8")
    assert run(skill, impl, "check")[1]["status"] == "update_available"


def test_network_error_is_quiet_and_recorded(env, impl):
    root, skill = env
    set_url(skill, (root / "missing.json").as_uri())
    code, out = run(skill, impl, "check")
    assert code == 0 and out["status"] == "error" and out["reason"] == "manifest_unavailable"
    assert (skill / ".last_update_check").exists()


def test_incomplete_manifest_is_error(env, impl):
    root, skill = env
    (root / "latest.json").write_text(json.dumps({"name": NAME, "version": "2.2.3", "build": 0}), encoding="utf-8")
    set_url(skill, (root / "latest.json").as_uri())
    assert run(skill, impl, "check", "--force")[1]["reason"] == "manifest_unavailable"


def test_name_mismatch(env, impl):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip"), name="other-skill"))
    assert run(skill, impl, "check", "--force")[1]["reason"] == "name_mismatch"
    code, out = run(skill, impl, "apply")
    assert code == 1 and out["status"] == "failed" and "名称不匹配" in out["message"]


def test_check_and_apply_over_http(env, impl):
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
        assert run(skill, impl, "check", "--force")[1]["status"] == "update_available"
        code, out = run(skill, impl, "apply")
        assert code == 0 and out["status"] == "updated", out
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- apply

def test_sha256_mismatch_aborts(env, impl):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip"), sha="0" * 64))
    before = snapshot(skill)
    code, out = run(skill, impl, "apply")
    assert code == 1 and out == {"status": "failed", "message": "更新失败：安装包校验失败（sha256 不一致），已中止"}
    _assert_untouched(skill, before)


def test_size_mismatch_aborts(env, impl):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip"), size=10))
    before = snapshot(skill)
    code, out = run(skill, impl, "apply")
    assert code == 1 and out["message"] == "更新失败：下载的安装包大小超出预期，已中止"
    _assert_untouched(skill, before)


@pytest.mark.parametrize("kwargs, message", [
    ({"prefix": "other-dir/"}, "安装包结构不正确或包含非法路径"),
    ({"extra": {"stray.txt": "x"}}, "安装包结构不正确或包含非法路径"),
    ({"skip": ("VERSION.json",)}, "安装包缺少 VERSION.json"),
    ({"build": 3}, "安装包内的 VERSION.json 与发布信息不一致"),
    ({"symlink": NAME + "/link"}, "安装包包含符号链接"),
], ids=["top-dir", "stray", "no-version", "build-mismatch", "symlink"])
def test_bad_package_rejected(env, impl, kwargs, message):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip", **kwargs)))
    before = snapshot(skill)
    code, out = run(skill, impl, "apply")
    assert code == 1 and out["status"] == "failed" and message in out["message"], out
    _assert_untouched(skill, before)


def test_not_a_zip_rejected(env, impl):
    root, skill = env
    (root / "p.zip").write_bytes(b"not a zip at all" * 10)
    set_url(skill, publish(root, root / "p.zip"))
    before = snapshot(skill)
    code, out = run(skill, impl, "apply")
    assert code == 1 and "不是有效的 zip" in out["message"]
    _assert_untouched(skill, before)


@pytest.mark.parametrize("evil", [NAME + "/../evil.txt", NAME + "/../../evil.txt", "/" + NAME + "/abs.txt",
                                  NAME + "/a\\..\\..\\evil.txt", NAME + "/C:/evil.txt"])
def test_zip_slip_rejected(env, impl, evil):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip", extra={evil: "pwned"})))
    before = snapshot(skill)
    code, _ = run(skill, impl, "apply")
    assert code == 1
    _assert_untouched(skill, before)
    assert not list(root.rglob("evil.txt"))


@pytest.mark.parametrize("compression", [zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED], ids=["deflate", "stored"])
def test_apply_and_rollback(env, impl, compression):
    root, skill = env
    url = publish(root, make_zip(root / "p.zip", compression=compression))
    set_url(skill, url)
    code, out = run(skill, impl, "apply")
    assert code == 0 and out == {"status": "updated", "from": "2.2.3-hj.1", "to": "2.2.3-hj.2",
                                 "message": "已更新到 2.2.3-hj.2，请新开一个对话后生效"}
    bak = skill.with_name(NAME + ".bak")
    assert build_of(skill) == 2
    assert "new" in (skill / "SKILL.md").read_text(encoding="utf-8")
    assert (skill / "references" / "update.md").read_text(encoding="utf-8") == "新文档"
    assert not (skill / "mcp.json").exists()  # 新包没有该文件
    assert (skill / ".last_update_check").exists()
    assert (bak / "mcp.json").read_text() == "old-mcp"
    assert not (bak / "SKILL.md").exists() and (bak / "SKILL.md.bak-disabled").exists()

    # 再次 apply：已是最新（新包里 update.json 为占位符，测试中重新指向本地 manifest）
    set_url(skill, url)
    code, out = run(skill, impl, "apply")
    assert code == 0 and out == {"status": "up_to_date", "current": "2.2.3-hj.2",
                                 "message": "当前已是最新版本 2.2.3-hj.2，无需更新"}

    code, out = run(skill, impl, "rollback")
    assert code == 0 and out == {"status": "rolled_back", "to": "2.2.3-hj.1",
                                 "message": "已回滚到 2.2.3-hj.1，请新开一个对话后生效"}
    assert build_of(skill) == 1
    assert (skill / "SKILL.md").exists() and (skill / "mcp.json").read_text() == "old-mcp"
    assert build_of(bak) == 2 and (bak / "SKILL.md.bak-disabled").exists()
    assert sorted(p.name for p in skill.parent.iterdir()) == [NAME, NAME + ".bak"]


def test_rollback_without_backup(env, impl):
    _, skill = env
    code, out = run(skill, impl, "rollback")
    assert code == 1 and out == {"status": "failed", "message": "回滚失败：没有可回滚的备份版本"}


def test_replaces_older_backup(env, impl):
    root, skill = env
    old_bak = skill.with_name(NAME + ".bak")
    old_bak.mkdir()
    (old_bak / "ancient.txt").write_text("x")
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    assert run(skill, impl, "apply")[0] == 0
    assert not (old_bak / "ancient.txt").exists()


def test_last_check_carried_over(env, impl):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    (skill / ".last_update_check").write_text("12345", encoding="utf-8")
    assert run(skill, impl, "apply")[0] == 0
    stamp = int((skill / ".last_update_check").read_text(encoding="utf-8"))
    assert abs(stamp - time.time()) < 60  # 已延续并在更新成功后刷新


@pytest.mark.parametrize("fault", ["post_install", "move"])
def test_failure_mid_apply_restores(env, impl, fault):
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    before = snapshot(skill)
    code, out = run(skill, impl, "apply", fault=fault)
    assert code == 1 and out["status"] == "failed" and out["message"].startswith("更新失败：更新出错（")
    if fault == "post_install":
        assert out["message"].endswith("已恢复原版本")
    assert snapshot(skill) == before
    assert not (skill.with_name(NAME + ".bak") / "SKILL.md").exists()


def test_windows_rename_blocked_fallback(env, impl):
    """模拟 Windows 目录被占用无法改名：走内容复制路径，结果一致。"""
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    assert run(skill, impl, "apply", fault="rename_blocked")[0] == 0
    assert build_of(skill) == 2
    bak = skill.with_name(NAME + ".bak")
    assert (bak / "mcp.json").read_text() == "old-mcp" and (bak / "SKILL.md.bak-disabled").exists()
    assert run(skill, impl, "rollback", fault="rename_blocked")[0] == 0
    assert build_of(skill) == 1 and build_of(bak) == 2


def test_refuses_git_checkout(env, impl):
    root, skill = env
    (skill / ".git").mkdir()
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    before = snapshot(skill)
    code, out = run(skill, impl, "apply")
    assert code == 1 and ".git" in out["message"]
    assert snapshot(skill) == before


def test_version(env, impl):
    _, skill = env
    code, out = run(skill, impl, "version")
    assert code == 0 and out == {"name": NAME, "version": "2.2.3-hj.1", "build": 1}


def test_bom_in_json_files_tolerated(env, impl):
    _, skill = env
    p = skill / "VERSION.json"
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
    assert run(skill, impl, "version")[1]["build"] == 1


@pytest.mark.parametrize("other", ["python", "node", "pwsh"])
def test_cross_runtime_rollback(env, impl, other):
    """一个运行时更新，另一个运行时回滚（.bak 格式一致）。"""
    if (other == "node" and not NODE) or (other == "pwsh" and not PWSH):
        pytest.skip("runtime 未安装")
    root, skill = env
    set_url(skill, publish(root, make_zip(root / "p.zip")))
    assert run(skill, impl, "apply")[0] == 0
    assert run(skill, other, "rollback")[0] == 0
    assert build_of(skill) == 1 and (skill / "mcp.json").read_text() == "old-mcp"
