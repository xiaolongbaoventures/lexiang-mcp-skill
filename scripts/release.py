#!/usr/bin/env python3
"""乐享知识库 Skill 发布工具（维护者使用，仅用 Python 3 标准库）。

在仓库内运行：
    python3 scripts/release.py build --version 2.2.3-hj.N
        更新 SKILL.md（frontmatter + 页脚）和 VERSION.json（build+1），提交、打 tag vX，
        生成 dist/lexiang-knowledge-base-<version>.zip，并输出 sha256 和大小。
        若 --version 与当前 VERSION.json 相同且 tag 尚不存在，则不改文件、不提交，直接打 tag 并打包。
    python3 scripts/release.py manifest --zip-url URL [--notes "一两句中文说明"]
        根据最近一次 build 生成 dist/latest.json（notes 缺省取 CHANGELOG.md 中该版本的「摘要：」行）。
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_NAME = "lexiang-knowledge-base"
REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"
LAST_BUILD = DIST / "last_build.json"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?$")
FRONTMATTER_RE = re.compile(r"\A(---\n(?:.*\n)*?)version: *[^\n]*\n((?:.*\n)*?---\n)")
FOOTER_RE = re.compile(r"^> Skill version: \*\*[^*]+\*\*$", re.M)


def die(msg):
    print("错误：" + msg, file=sys.stderr)
    sys.exit(1)


def git(*args, capture=True):
    r = subprocess.run(["git", "-C", str(REPO)] + list(args), capture_output=capture, text=True)
    if r.returncode != 0:
        die("git %s 失败：%s" % (" ".join(args), (r.stderr or "").strip()))
    return (r.stdout or "").strip()


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def bump_skill_md(text, version):
    new, n1 = FRONTMATTER_RE.subn(lambda m: m.group(1) + "version: " + version + "\n" + m.group(2), text, count=1)
    new, n2 = FOOTER_RE.subn("> Skill version: **%s**" % version, new)
    if n1 != 1 or n2 != 1:
        die("SKILL.md 中未找到唯一的 frontmatter version 或页脚版本行")
    return new


def verify_zip(path, build):
    prefix = SKILL_NAME + "/"
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        bad = [n for n in names if not n.startswith(prefix)]
        if bad:
            die("zip 顶层目录不正确：%s" % bad[:3])
        for req in ("SKILL.md", "VERSION.json", "update.json", "scripts/update.py", "scripts/update.mjs", "scripts/update.ps1"):
            if prefix + req not in names:
                die("zip 缺少 %s" % req)
        for junk in ("dist/", "__pycache__", ".last_update_check"):
            if any(junk in n for n in names):
                die("zip 包含不应发布的内容：%s" % junk)
        v = json.loads(zf.read(prefix + "VERSION.json").decode("utf-8"))
        if v.get("build") != build:
            die("zip 内 VERSION.json build 不一致")


def cmd_build(version):
    if not VERSION_RE.match(version):
        die("版本号格式不正确：%s" % version)
    if git("status", "--porcelain"):
        die("工作区不干净，请先提交或清理改动")
    tag = "v" + version
    if git("tag", "--list", tag):
        die("tag %s 已存在" % tag)
    vfile = REPO / "VERSION.json"
    cur = json.loads(vfile.read_text(encoding="utf-8"))
    if cur.get("version") == version:
        build = int(cur["build"])  # 首次为当前版本打包，不再 +1
    else:
        build = int(cur["build"]) + 1
        skill_md = REPO / "SKILL.md"
        skill_md.write_text(bump_skill_md(skill_md.read_text(encoding="utf-8"), version), encoding="utf-8")
        vfile.write_text(json.dumps({"name": SKILL_NAME, "version": version, "build": build},
                                    ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        git("add", "SKILL.md", "VERSION.json")
        git("commit", "-m", "release: %s" % tag, capture=False)
    git("tag", "-a", tag, "-m", "release %s" % tag)
    DIST.mkdir(exist_ok=True)
    zip_path = DIST / ("%s-%s.zip" % (SKILL_NAME, version))
    git("archive", "--format=zip", "--prefix=%s/" % SKILL_NAME, "-o", str(zip_path), tag)
    verify_zip(zip_path, build)
    info = {"version": version, "build": build, "tag": tag, "commit": git("rev-parse", tag + "^{commit}"),
            "zip": zip_path.name, "sha256": sha256_of(zip_path), "size": zip_path.stat().st_size}
    LAST_BUILD.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已打包：%s" % zip_path)
    print("version: %s  build: %d  tag: %s" % (version, build, tag))
    print("sha256: %s" % info["sha256"])
    print("size:   %d" % info["size"])
    return 0


def notes_from_changelog(version):
    path = REPO / "CHANGELOG.md"
    if not path.exists():
        return ""
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_section = version in line
            continue
        if in_section:
            m = re.match(r"^>?\s*摘要[:：]\s*(.+)$", line.strip())
            if m:
                return m.group(1).strip()
    return ""


def cmd_manifest(zip_url, notes):
    if not LAST_BUILD.exists():
        die("未找到 dist/last_build.json，请先运行 build")
    info = json.loads(LAST_BUILD.read_text(encoding="utf-8"))
    zip_path = DIST / info["zip"]
    if not zip_path.exists() or sha256_of(zip_path) != info["sha256"]:
        die("dist 中的 zip 与最近一次 build 记录不一致")
    notes = notes or notes_from_changelog(info["version"])
    if not notes:
        die("缺少更新说明：请用 --notes 指定，或在 CHANGELOG.md 该版本下写「摘要：」行")
    manifest = {
        "name": SKILL_NAME,
        "version": info["version"],
        "build": info["build"],
        "zip_url": zip_url,
        "sha256": info["sha256"],
        "size": info["size"],
        "notes": notes,
        "released_at": datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat(),
    }
    out = DIST / "latest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已生成：%s" % out)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="乐享知识库 Skill 发布工具")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="升版本、提交、打 tag 并打包")
    b.add_argument("--version", required=True)
    m = sub.add_parser("manifest", help="生成 dist/latest.json")
    m.add_argument("--zip-url", required=True)
    m.add_argument("--notes", default="")
    args = p.parse_args(argv)
    if args.cmd == "build":
        return cmd_build(args.version)
    return cmd_manifest(args.zip_url, args.notes)


if __name__ == "__main__":
    sys.exit(main())
