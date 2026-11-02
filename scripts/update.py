#!/usr/bin/env python3
"""乐享知识库 Skill 自动更新（仅用 Python 3 标准库，支持 macOS / Windows / Linux）。

用法：
    python3 scripts/update.py check [--force]   # 检查更新，输出一行 JSON
    python3 scripts/update.py apply             # 下载、校验并安装新版本（需用户同意后执行）
    python3 scripts/update.py rollback          # 回滚到上一版本（.bak）
    python3 scripts/update.py version           # 输出本地版本

同等实现：scripts/update.mjs（Node ≥ 18）、scripts/update.ps1（Windows PowerShell 5.1 / pwsh 7），
命令、输出 JSON 和行为保持一致，修改时须三者同步（测试见 scripts/test_update.py）。

只会改动本 Skill 目录及其同级备份目录 `<目录名>.bak`，不触碰 MCP 客户端配置或任何密钥。
"""

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

SKILL_NAME = "lexiang-knowledge-base"
DEFAULT_SKILL_DIR = Path(__file__).resolve().parent.parent
CHECK_FILE = ".last_update_check"
PLACEHOLDER = "__MANIFEST_URL__"
MANIFEST_TIMEOUT = 5
DOWNLOAD_TIMEOUT = 30
MAX_ZIP_SIZE = 50 * 1024 * 1024
DISABLED_SKILL_MD = "SKILL.md.bak-disabled"  # 备份目录里的 SKILL.md 改名，避免被客户端当成第二个 Skill 加载
# 仅供测试注入故障：rename_blocked（模拟 Windows 目录被占用）/ move / post_install
FAULT = os.environ.get("LEXIANG_UPDATE_FAULT", "")


class UpdateError(Exception):
    """带中文说明的更新失败。"""


# ---------------------------------------------------------------- 基础工具

def _out(obj):
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), flush=True)


def _read_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def local_version(skill_dir):
    data = _read_json(Path(skill_dir) / "VERSION.json")
    return {"name": data["name"], "version": str(data["version"]), "build": int(data["build"])}


def load_config(skill_dir):
    try:
        cfg = _read_json(Path(skill_dir) / "update.json")
    except (OSError, ValueError):
        cfg = {}
    url = str(cfg.get("manifest_url") or "").strip()
    try:
        hours = float(cfg.get("check_interval_hours", 8))
    except (TypeError, ValueError):
        hours = 8.0
    return url, hours


def _url_allowed(url):
    """https 任意主机；http 仅限本机（测试）；file:// 用于本地测试。"""
    p = urllib.parse.urlparse(url)
    if p.scheme == "https":
        return bool(p.netloc)
    if p.scheme == "http":
        return p.hostname in ("127.0.0.1", "localhost", "::1")
    return p.scheme == "file"


def is_configured(url):
    return bool(url) and PLACEHOLDER not in url and not url.startswith("__") and _url_allowed(url)


def _open(url, timeout):
    if not _url_allowed(url):
        raise UpdateError("不支持的下载地址：%s" % url)
    req = urllib.request.Request(url, headers={
        "User-Agent": "%s-updater" % SKILL_NAME,
        "Cache-Control": "no-cache",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_manifest(url):
    with _open(url, MANIFEST_TIMEOUT) as resp:
        raw = resp.read(256 * 1024)
    m = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(m, dict):
        raise ValueError("manifest is not an object")
    for key in ("name", "version", "build", "zip_url", "sha256"):
        if key not in m:
            raise ValueError("manifest missing %s" % key)
    m["build"] = int(m["build"])
    return m


def _read_last_check(skill_dir):
    try:
        return float((Path(skill_dir) / CHECK_FILE).read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return None


def _write_last_check(skill_dir):
    try:
        (Path(skill_dir) / CHECK_FILE).write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- check

def cmd_check(skill_dir, force=False):
    skill_dir = Path(skill_dir)
    try:
        cur = local_version(skill_dir)
    except (OSError, ValueError, KeyError):
        _out({"status": "error", "reason": "local_version_unreadable"})
        return 0
    base = {"current": cur["version"], "current_build": cur["build"]}
    url, hours = load_config(skill_dir)
    if not is_configured(url):
        _out(dict(status="skipped", reason="not_configured", **base))
        return 0
    if not force:
        last = _read_last_check(skill_dir)
        now = time.time()
        if last is not None and 0 <= now - last < hours * 3600:
            _out(dict(status="skipped", reason="checked_recently", **base))
            return 0
    try:
        m = fetch_manifest(url)
    except Exception:  # 网络/解析失败：安静跳过，不打扰用户
        _write_last_check(skill_dir)
        _out(dict(status="error", reason="manifest_unavailable", **base))
        return 0
    _write_last_check(skill_dir)
    if m.get("name") != cur["name"]:
        _out(dict(status="error", reason="name_mismatch", **base))
        return 0
    res = dict(base, latest=str(m["version"]), latest_build=m["build"], notes=m.get("notes", ""))
    res["status"] = "update_available" if m["build"] > cur["build"] else "up_to_date"
    _out(res)
    return 0


# ---------------------------------------------------------------- apply 辅助

def _download(url, dest, expected_size):
    h = hashlib.sha256()
    total = 0
    limit = expected_size if expected_size else MAX_ZIP_SIZE
    with _open(url, DOWNLOAD_TIMEOUT) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UpdateError("下载的安装包大小超出预期，已中止")
            h.update(chunk)
            f.write(chunk)
    if expected_size and total != expected_size:
        raise UpdateError("安装包大小不符（应为 %d 字节，实际 %d 字节），已中止" % (expected_size, total))
    return h.hexdigest()


def _safe_extract(zip_path, dest_root, expected_build):
    """校验 zip 结构并解压，返回解压出的 lexiang-knowledge-base 目录。"""
    prefix = SKILL_NAME + "/"
    dest_root = Path(dest_root).resolve()
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise UpdateError("安装包不是有效的 zip 文件")
    with zf:
        names = zf.namelist()
        if not names:
            raise UpdateError("安装包为空")
        for info in zf.infolist():
            n = info.filename
            parts = PurePosixPath(n).parts
            if (not n.startswith(prefix) or "\\" in n or n.startswith("/")
                    or ".." in parts or any(":" in x for x in parts)):
                raise UpdateError("安装包结构不正确或包含非法路径：%s" % n)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise UpdateError("安装包包含符号链接，已拒绝：%s" % n)
        for required in ("SKILL.md", "VERSION.json"):
            if prefix + required not in names:
                raise UpdateError("安装包缺少 %s" % required)
        try:
            inner = json.loads(zf.read(prefix + "VERSION.json").decode("utf-8-sig"))
            if inner.get("name") != SKILL_NAME or int(inner.get("build")) != expected_build:
                raise ValueError
        except (ValueError, TypeError):
            raise UpdateError("安装包内的 VERSION.json 与发布信息不一致")
        for info in zf.infolist():
            target = (dest_root / info.filename).resolve()
            if dest_root not in target.parents:
                raise UpdateError("安装包包含非法路径：%s" % info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return dest_root / SKILL_NAME


def _clear_dir(d):
    for child in Path(d).iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _replace_contents(dst, src):
    """保留 dst 目录本身，只替换其内容（Windows 上目录被占用、无法改名时使用）。"""
    _clear_dir(dst)
    for child in Path(src).iterdir():
        target = Path(dst) / child.name
        if child.is_dir():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target)


def _disable_backup(bak):
    p = Path(bak) / "SKILL.md"
    if p.exists():
        os.replace(p, Path(bak) / DISABLED_SKILL_MD)


def _enable_backup(bak):
    p = Path(bak) / DISABLED_SKILL_MD
    if p.exists():
        os.replace(p, Path(bak) / "SKILL.md")


def _rename(src, dst, skill_dir=None):
    if FAULT == "rename_blocked" and skill_dir is not None and Path(src) == Path(skill_dir):
        raise PermissionError("simulated: directory in use")
    os.rename(src, dst)


def _install(skill_dir, new_dir, bak):
    """skill_dir → bak，new_dir → skill_dir。优先整体改名，失败（如 Windows 占用）则改为内容复制。"""
    if bak.exists():
        shutil.rmtree(bak)
    try:
        _rename(skill_dir, bak, skill_dir)
    except OSError:
        shutil.copytree(skill_dir, bak, symlinks=True)
        try:
            _replace_contents(skill_dir, new_dir)
        except Exception:
            _replace_contents(skill_dir, bak)
            raise
        return
    try:
        if FAULT == "move":
            skill_dir.mkdir()
            (skill_dir / "partial").write_text("x")
            raise OSError("simulated: move interrupted")
        shutil.move(str(new_dir), str(skill_dir))
    except Exception:
        shutil.rmtree(skill_dir, ignore_errors=True)
        os.rename(bak, skill_dir)
        raise


def _restore(skill_dir, bak):
    """把 bak 恢复为 skill_dir（尽力而为）。"""
    _enable_backup(bak)
    try:
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        os.rename(bak, skill_dir)
    except OSError:
        skill_dir.mkdir(exist_ok=True)
        _replace_contents(skill_dir, bak)


def _post_install(skill_dir, bak, expected_build):
    if FAULT == "post_install":
        raise OSError("simulated: disk full")
    old_check = bak / CHECK_FILE
    if old_check.exists():
        shutil.copy2(old_check, skill_dir / CHECK_FILE)
    if local_version(skill_dir)["build"] != expected_build:
        raise UpdateError("安装后版本校验失败")
    _disable_backup(bak)


def _guard_skill_dir(skill_dir):
    if (skill_dir / ".git").exists():
        raise UpdateError("当前目录是源码仓库（含 .git），不是 Skill 安装目录，已拒绝更新")
    if not (skill_dir / "SKILL.md").exists():
        raise UpdateError("未找到 SKILL.md，无法确认 Skill 安装目录")


# ---------------------------------------------------------------- apply / rollback

def cmd_apply(skill_dir):
    skill_dir = Path(skill_dir).resolve()
    bak = skill_dir.with_name(skill_dir.name + ".bak")
    os.chdir(tempfile.gettempdir())  # 避免自身工作目录占用 Skill 目录（Windows）
    installed = False
    try:
        _guard_skill_dir(skill_dir)
        try:
            cur = local_version(skill_dir)
        except (OSError, ValueError, KeyError):
            raise UpdateError("无法读取本地 VERSION.json")
        url, _ = load_config(skill_dir)
        if not is_configured(url):
            raise UpdateError("未配置更新地址（update.json），无法在线更新")
        try:
            m = fetch_manifest(url)
        except Exception:
            raise UpdateError("无法获取最新版本信息，请检查网络后重试")
        if m.get("name") != cur["name"] or m.get("name") != SKILL_NAME:
            raise UpdateError("发布信息中的 Skill 名称不匹配，已中止")
        if m["build"] <= cur["build"]:
            _out({"status": "up_to_date", "current": cur["version"],
                  "message": "当前已是最新版本 %s，无需更新" % cur["version"]})
            return 0
        size = int(m.get("size") or 0)
        with tempfile.TemporaryDirectory(prefix="lexiang-update-") as tmp:
            tmp = Path(tmp)
            zip_path = tmp / "package.zip"
            try:
                digest = _download(m["zip_url"], zip_path, size)
            except UpdateError:
                raise
            except Exception:
                raise UpdateError("下载安装包失败，请检查网络后重试")
            if digest.lower() != str(m["sha256"]).strip().lower():
                raise UpdateError("安装包校验失败（sha256 不一致），已中止")
            new_dir = _safe_extract(zip_path, tmp / "extract", m["build"])
            _install(skill_dir, new_dir, bak)
            installed = True
            _post_install(skill_dir, bak, m["build"])
        _write_last_check(skill_dir)
        _out({"status": "updated", "from": cur["version"], "to": str(m["version"]),
              "message": "已更新到 %s，请新开一个对话后生效" % m["version"]})
        return 0
    except Exception as e:
        restored = ""
        if installed:
            try:
                _restore(skill_dir, bak)
                restored = "，已恢复原版本"
            except Exception:
                restored = "，自动恢复失败，请联系管理员"
        msg = str(e) if isinstance(e, UpdateError) else "更新出错（%s）" % type(e).__name__
        _out({"status": "failed", "message": "更新失败：%s%s" % (msg, restored)})
        return 1


def cmd_rollback(skill_dir):
    skill_dir = Path(skill_dir).resolve()
    bak = skill_dir.with_name(skill_dir.name + ".bak")
    os.chdir(tempfile.gettempdir())
    try:
        _guard_skill_dir(skill_dir)
        if not bak.is_dir():
            raise UpdateError("没有可回滚的备份版本")
        _enable_backup(bak)
        try:
            prev = local_version(bak)
        except (OSError, ValueError, KeyError):
            _disable_backup(bak)
            raise UpdateError("备份目录不完整，无法回滚")
        with tempfile.TemporaryDirectory(prefix="lexiang-rollback-") as tmp:
            cur_copy = Path(tmp) / "current"
            shutil.copytree(skill_dir, cur_copy, symlinks=True)
            try:
                _restore(skill_dir, bak)  # bak → skill_dir
            except Exception:
                _replace_contents(skill_dir, cur_copy)
                raise
            # 原当前版本变成新的 .bak，便于再次回滚
            if bak.exists():
                shutil.rmtree(bak)
            shutil.copytree(cur_copy, bak, symlinks=True)
            _disable_backup(bak)
        _out({"status": "rolled_back", "to": prev["version"],
              "message": "已回滚到 %s，请新开一个对话后生效" % prev["version"]})
        return 0
    except Exception as e:
        msg = str(e) if isinstance(e, UpdateError) else "回滚出错（%s）" % type(e).__name__
        _out({"status": "failed", "message": "回滚失败：%s" % msg})
        return 1


def cmd_version(skill_dir):
    try:
        _out(local_version(skill_dir))
        return 0
    except (OSError, ValueError, KeyError):
        _out({"status": "error", "message": "无法读取 VERSION.json"})
        return 1


def main(argv=None, skill_dir=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    skill_dir = Path(skill_dir) if skill_dir else DEFAULT_SKILL_DIR
    p = argparse.ArgumentParser(description="乐享知识库 Skill 更新工具")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="检查是否有新版本")
    c.add_argument("--force", action="store_true", help="忽略检查间隔")
    sub.add_parser("apply", help="下载并安装新版本")
    sub.add_parser("rollback", help="回滚到上一版本")
    sub.add_parser("version", help="输出本地版本")
    args = p.parse_args(argv)
    if args.cmd == "check":
        return cmd_check(skill_dir, args.force)
    if args.cmd == "apply":
        return cmd_apply(skill_dir)
    if args.cmd == "rollback":
        return cmd_rollback(skill_dir)
    return cmd_version(skill_dir)


if __name__ == "__main__":
    sys.exit(main())
