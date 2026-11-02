#!/usr/bin/env python3
"""把乐享图片/文件/视频下载到本地，供对话里用本地路径展示（仅用 Python 3 标准库）。

用法（在 Skill 目录运行）：
    python3 scripts/fetch_media.py --name "<file_describe_file 返回的 name>" --file-id <file_id> \
        [--size <字节数>] [--url "<file_download_file 返回的 data.url>"] [--dir 目录] [--max-mb 50] [--yes]

- 不传 --url：只检查本地是否已有同一文件（status=exists / need_url），不联网。
- 传 --url：已存在则不重复下载；否则下载到临时文件，校验后改名。
- 超过 --max-mb（默认 50）且未加 --yes：不下载，返回 status=too_large，由 Agent 先问用户。
- 输出一行 JSON，**不包含下载地址**（地址是带签名的临时 URL，不要展示给用户）。

保存目录：--dir > 环境变量 LEXIANG_MEDIA_DIR > ~/Downloads/lexiang > <系统临时目录>/lexiang
文件名：<原文件名主干>_<file_id 前 8 位><扩展名>，同名同大小视为同一文件。
"""

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv", ".wmv"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
MAGIC = [  # (前缀, 扩展名集合)
    (b"\x89PNG\r\n\x1a\n", {".png"}),
    (b"\xff\xd8\xff", {".jpg", ".jpeg"}),
    (b"GIF87a", {".gif"}),
    (b"GIF89a", {".gif"}),
    (b"BM", {".bmp"}),
]
CHUNK = 1 << 16
TIMEOUT = 60


def out(**kw):
    print(json.dumps(kw, ensure_ascii=False))
    return 0 if kw.get("status") in ("ok", "exists", "need_url", "too_large") else 1


def default_dir():
    env = os.environ.get("LEXIANG_MEDIA_DIR")
    if env:
        return Path(env).expanduser()
    dl = Path.home() / "Downloads"
    if dl.is_dir() and os.access(str(dl), os.W_OK):
        return dl / "lexiang"
    return Path(tempfile.gettempdir()) / "lexiang"


def safe_name(name, file_id):
    name = os.path.basename((name or "").replace("\\", "/")) or "file"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "file"
    stem, ext = os.path.splitext(name)
    tag = re.sub(r"[^0-9A-Za-z]", "", file_id or "")[:8]
    stem = stem[:80]
    return (stem + "_" + tag if tag else stem) + ext.lower()


def kind_of(path):
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "file"


def md_path(path):
    """Markdown 里用的本地绝对路径：统一正斜杠；含空格/括号时用 <> 包裹（CommonMark）。"""
    p = str(path).replace("\\", "/")
    return "<%s>" % p if re.search(r"[\s()<>]", p) else p


def markdown(path, label):
    label = label.replace("[", "(").replace("]", ")")
    if kind_of(path) == "image":
        return "![%s](%s)" % (label, md_path(path))
    return "[%s](%s)" % (label, md_path(path))


def looks_valid(path, head):
    ext = path.suffix.lower()
    for magic, exts in MAGIC:
        if ext in exts:
            return head.startswith(magic)
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return True


def result(status, path, label, size, **extra):
    d = {"status": status, "kind": kind_of(path), "path": str(path), "size": size,
         "markdown": markdown(path, label)}
    d.update(extra)
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--name", required=True)
    ap.add_argument("--file-id", default="")
    ap.add_argument("--size", type=int, default=None)
    ap.add_argument("--url", default=None)
    ap.add_argument("--dir", default=None)
    ap.add_argument("--max-mb", type=float, default=50)
    ap.add_argument("--yes", action="store_true", help="已获用户同意，忽略大小限制")
    a = ap.parse_args(argv)

    folder = Path(a.dir).expanduser() if a.dir else default_dir()
    folder = folder.resolve()
    target = folder / safe_name(a.name, a.file_id)
    label = os.path.basename(a.name) or target.name
    limit = int(a.max_mb * 1024 * 1024)

    if target.is_file() and (a.size is None or target.stat().st_size == a.size):
        return out(**result("exists", target, label, target.stat().st_size))
    if a.size is not None and a.size > limit and not a.yes:
        return out(status="too_large", kind=kind_of(target), size=a.size, max_mb=a.max_mb, path=str(target))
    if not a.url:
        return out(status="need_url", kind=kind_of(target), path=str(target))

    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return out(status="error", error="cannot_create_dir", detail=str(e)[:200])
    part = target.with_name(target.name + ".part")
    req = urllib.request.Request(a.url, headers={"User-Agent": "lexiang-skill-fetch-media"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            clen = r.headers.get("Content-Length")
            if clen and clen.isdigit() and int(clen) > limit and not a.yes:
                return out(status="too_large", kind=kind_of(target), size=int(clen), max_mb=a.max_mb, path=str(target))
            if ctype in ("application/xml", "text/xml", "text/html"):
                return out(status="error", error="unexpected_content_type", content_type=ctype,
                           hint="下载地址可能已过期，请重新调用 file_download_file 获取新地址后再试一次")
            got, head = 0, b""
            with open(part, "wb") as f:
                while True:
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    if len(head) < 16:
                        head += buf[:16 - len(head)]
                    got += len(buf)
                    if got > limit and not a.yes:
                        f.close()
                        part.unlink()
                        return out(status="too_large", kind=kind_of(target), size=got, max_mb=a.max_mb, path=str(target))
                    f.write(buf)
    except urllib.error.HTTPError as e:
        if part.exists():
            part.unlink()
        hint = "下载地址已过期或无效，请重新调用 file_download_file 获取新地址后再试一次" if e.code in (401, 403, 404) else ""
        return out(status="error", error="http_%d" % e.code, hint=hint)
    except (urllib.error.URLError, OSError) as e:
        if part.exists():
            part.unlink()
        return out(status="error", error="network", detail=str(getattr(e, "reason", e))[:200])

    if got == 0 or (a.size is not None and got != a.size) or not looks_valid(target, head):
        part.unlink()
        return out(status="error", error="invalid_file", size=got, expected_size=a.size)
    os.replace(part, target)
    return out(**result("ok", target, label, got, content_type=ctype))


if __name__ == "__main__":
    sys.exit(main())
