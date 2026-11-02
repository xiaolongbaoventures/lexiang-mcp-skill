"""fetch_media.py 测试：本地 http.server 模拟签名下载地址。"""

import http.server
import importlib.util
import io
import json
import os
import threading
from contextlib import redirect_stdout
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("fetch_media", HERE / "fetch_media.py")
fm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fm)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
ROUTES = {
    "/img.png": (200, "image/png", PNG),
    "/fake.png": (200, "image/png", b"<html>not png</html>"),
    "/doc.pdf": (200, "application/pdf", b"%PDF-1.4 hello"),
    "/big.mp4": (200, "video/mp4", b"\x00" * (2 * 1024 * 1024)),
    "/expired": (403, "application/xml", b"<Error><Code>AccessDenied</Code></Error>"),
    "/xml200": (200, "application/xml", b"<Error/>"),
}
HITS = []


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        HITS.append(path)
        code, ctype, body = ROUTES.get(path, (404, "text/plain", b"nf"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def base():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def run(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = fm.main(list(args))
    return code, json.loads(buf.getvalue())


def test_image_download_and_markdown(base, tmp_path):
    code, r = run("--name", "案例图 1.png", "--file-id", "efe7caf4ddd3", "--size", str(len(PNG)),
                  "--url", base + "/img.png?X-Amz-Signature=secret", "--dir", str(tmp_path))
    assert code == 0 and r["status"] == "ok" and r["kind"] == "image"
    p = Path(r["path"])
    assert p.is_absolute() and p.read_bytes() == PNG and p.name == "案例图 1_efe7caf4.png"
    assert r["markdown"] == "![案例图 1.png](<%s>)" % str(p).replace("\\", "/")
    assert "secret" not in json.dumps(r) and "127.0.0.1" not in json.dumps(r)
    assert not list(tmp_path.glob("*.part"))


def test_no_redownload_when_present(base, tmp_path):
    args = ["--name", "a.png", "--file-id", "abcdef1234", "--size", str(len(PNG)), "--dir", str(tmp_path)]
    assert run(*args, "--url", base + "/img.png")[1]["status"] == "ok"
    n = len(HITS)
    code, r = run(*args, "--url", base + "/img.png")
    assert r["status"] == "exists" and len(HITS) == n
    code, r = run(*args)  # 无 url 也能命中本地
    assert r["status"] == "exists" and r["markdown"].startswith("![a.png](")


def test_need_url_without_network(tmp_path):
    code, r = run("--name", "x.pdf", "--file-id", "f1", "--dir", str(tmp_path))
    assert code == 0 and r["status"] == "need_url" and r["kind"] == "file"


def test_size_guard_before_download(base, tmp_path):
    n = len(HITS)
    code, r = run("--name", "完整版.MP4", "--file-id", "8ddb1729", "--size", str(551122874),
                  "--url", base + "/big.mp4", "--dir", str(tmp_path))
    assert r["status"] == "too_large" and r["kind"] == "video" and len(HITS) == n
    assert not any(tmp_path.iterdir())


def test_size_guard_from_content_length_and_yes(base, tmp_path):
    code, r = run("--name", "v.mp4", "--file-id", "v1", "--url", base + "/big.mp4", "--dir", str(tmp_path), "--max-mb", "1")
    assert r["status"] == "too_large" and not any(tmp_path.iterdir())
    code, r = run("--name", "v.mp4", "--file-id", "v1", "--url", base + "/big.mp4", "--dir", str(tmp_path), "--max-mb", "1", "--yes")
    assert r["status"] == "ok" and r["kind"] == "video" and r["markdown"].startswith("[v.mp4](")


def test_expired_url_and_xml_error(base, tmp_path):
    code, r = run("--name", "a.png", "--file-id", "e1", "--url", base + "/expired", "--dir", str(tmp_path))
    assert code == 1 and r["error"] == "http_403" and "file_download_file" in r["hint"]
    code, r = run("--name", "a.png", "--file-id", "e1", "--url", base + "/xml200", "--dir", str(tmp_path))
    assert r["error"] == "unexpected_content_type"
    assert not any(tmp_path.glob("*.png")) and not any(tmp_path.glob("*.part"))


def test_invalid_image_rejected(base, tmp_path):
    code, r = run("--name", "fake.png", "--file-id", "z1", "--url", base + "/fake.png", "--dir", str(tmp_path))
    assert r["status"] == "error" and r["error"] == "invalid_file" and not any(tmp_path.iterdir())


def test_size_mismatch_rejected(base, tmp_path):
    code, r = run("--name", "d.pdf", "--file-id", "d1", "--size", "999", "--url", base + "/doc.pdf", "--dir", str(tmp_path))
    assert r["error"] == "invalid_file"


def test_pdf_link(base, tmp_path):
    code, r = run("--name", "说明.pdf", "--file-id", "d1", "--url", base + "/doc.pdf", "--dir", str(tmp_path))
    assert r["status"] == "ok" and r["kind"] == "file" and r["markdown"].startswith("[说明.pdf](")


def test_default_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LEXIANG_MEDIA_DIR", str(tmp_path / "m"))
    assert fm.default_dir() == tmp_path / "m"
    monkeypatch.delenv("LEXIANG_MEDIA_DIR")
    monkeypatch.setattr(fm.Path, "home", lambda: tmp_path)
    assert fm.default_dir().name == "lexiang" and fm.default_dir().parent != tmp_path / "Downloads"
    (tmp_path / "Downloads").mkdir()
    assert fm.default_dir() == tmp_path / "Downloads" / "lexiang"


def test_safe_name_and_md_path():
    assert fm.safe_name("../../etc/pa:ss?.PNG", "abc-123-def") == "pa_ss__abc123de.png"
    assert fm.md_path(r"C:\Users\me\Downloads\lexiang\a.png") == "C:/Users/me/Downloads/lexiang/a.png"
    assert fm.md_path("/tmp/a b.png") == "</tmp/a b.png>"
    assert fm.markdown(Path("/tmp/x[1].png"), "x[1].png") == "![x(1).png](/tmp/x[1].png)"
