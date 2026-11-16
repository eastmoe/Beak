import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from beak.playwright_edge import EdgePlaywrightClient
from beak.schemas import CookieSpec, RenderRequest
from beak.worker import WorkerError


@pytest.fixture
def media_server():
    requests = []
    stop = threading.Event()
    body = b"\x00\x00\x00\x18ftypisom" + bytes(range(256)) * 200

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(dict(self.headers))
            if self.path == "/headers-slow":
                stop.wait(10)
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/media")
                self.end_headers()
                return
            self.send_response(403 if self.path == "/forbidden" else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.path == "/slow":
                self.wfile.write(body[:16])
                self.wfile.flush()
                stop.wait(10)
            else:
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", body, requests
    finally:
        stop.set()
        server.shutdown()
        server.server_close()


def invoke(tmp_path, url, **kwargs):
    return EdgePlaywrightClient().invoke(
        job_id="native-download",
        request=RenderRequest(
            url=url,
            output="download",
            engine="edge",
            timeout_ms=kwargs.pop("timeout_ms", 10000),
            **kwargs,
        ),
        job_dir=tmp_path / "job",
        user_data_dir=tmp_path / "profile",
    )


def test_native_download_handles_redirects_referer_cookies_and_exact_bytes(
    tmp_path, media_server
):
    from pathlib import Path

    base, body, requests = media_server
    result = invoke(
        tmp_path,
        base + "/redirect",
        download_referer="https://www.dlsite.com/maniax/work/=/product_id/RJ01388771.html",
        cookies=[CookieSpec(name="test-age", value="1", domain="127.0.0.1")],
    )
    assert Path(result.artifacts[0].path).read_bytes() == body
    assert result.artifacts[0].content_type == "video/mp4"
    assert len(requests) == 2
    assert all("HeadlessChrome" in headers["User-Agent"] for headers in requests)
    assert all("RJ01388771" in headers["Referer"] for headers in requests)
    assert all("test-age=1" in headers["Cookie"] for headers in requests)


@pytest.mark.parametrize(
    ("path", "options", "message", "code"),
    [
        ("/media", {"max_download_bytes": 32}, "大小限制", "DOWNLOAD_TOO_LARGE"),
        ("/forbidden", {}, "403", "DOWNLOAD_HTTP_ERROR"),
        ("/slow", {"timeout_ms": 1000}, "超时", "DOWNLOAD_TIMEOUT"),
        ("/headers-slow", {"timeout_ms": 1000}, "超时", "DOWNLOAD_TIMEOUT"),
    ],
)
def test_native_download_failure_never_publishes_a_file(
    tmp_path, media_server, path, options, message, code, monkeypatch
):
    from beak import playwright_edge

    base, *_ = media_server
    started = None
    download = playwright_edge.download_resource

    def timed_download(*args, **kwargs):
        nonlocal started
        started = time.monotonic()
        return download(*args, **kwargs)

    monkeypatch.setattr(playwright_edge, "download_resource", timed_download)
    with pytest.raises(WorkerError, match=message) as error:
        invoke(tmp_path, base + path, **options)
    assert error.value.code == code
    if path == "/headers-slow":
        # Download timeout starts after Edge launches; cold profile startup is separate.
        assert started is not None
        assert time.monotonic() - started < 8, (
            "A stalled header must not bypass the download deadline"
        )
    assert not list((tmp_path / "job").iterdir())


def test_native_download_honors_cancellation(tmp_path, media_server):
    base, *_ = media_server
    event = threading.Event()
    timer = threading.Timer(1.5, event.set)
    timer.start()
    try:
        with pytest.raises(WorkerError, match="取消|cancel"):
            EdgePlaywrightClient().invoke(
                job_id="cancel",
                request=RenderRequest(
                    url=base + "/slow",
                    output="download",
                    engine="edge",
                    timeout_ms=10000,
                ),
                job_dir=tmp_path / "job",
                user_data_dir=tmp_path / "profile",
                cancel_event=event,
            )
        assert not list((tmp_path / "job").iterdir())
    finally:
        timer.cancel()


def test_download_requires_edge():
    with pytest.raises(ValueError, match="engine=edge"):
        RenderRequest(
            url="https://example.org/file", output="download", engine="webview"
        )


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Page.goto: net::ERR_CONNECTION_CLOSED", "BROWSER_NETWORK_ERROR"),
        ("Page.goto: net::ERR_CERT_AUTHORITY_INVALID", "WORKER_FAILED"),
    ],
)
def test_page_connection_failure_has_stable_code(tmp_path, monkeypatch, message, code):
    def fail(*args, **kwargs):
        raise RuntimeError(message)

    client = EdgePlaywrightClient()
    monkeypatch.setattr(client, "_invoke", fail)
    with pytest.raises(WorkerError) as error:
        client.invoke(
            job_id="test",
            request=RenderRequest(url="https://example.org"),
            job_dir=tmp_path,
            user_data_dir=tmp_path,
        )
    assert error.value.code == code


def test_render_http_error_is_not_reported_as_a_successful_page(tmp_path, media_server):
    base, *_ = media_server
    with pytest.raises(WorkerError, match="HTTP 403") as error:
        EdgePlaywrightClient().invoke(
            job_id="http-error",
            request=RenderRequest(
                url=base + "/forbidden", output="rendered_html", engine="edge"
            ),
            job_dir=tmp_path / "job",
            user_data_dir=tmp_path / "profile",
        )
    assert error.value.code == "BROWSER_HTTP_ERROR"


def test_finished_download_releases_files_and_profile(tmp_path, media_server):
    from pathlib import Path

    from beak.jobs import JobManager

    base, *_ = media_server
    manager = JobManager(data_dir=tmp_path, worker=EdgePlaywrightClient())
    try:
        result = manager.run_sync(
            RenderRequest(url=base + "/media", output="download", engine="edge")
        )
        assert Path(result.artifacts[0].path).is_file()
        manager.release_download(result.job_id)
        assert not list((tmp_path / "jobs").iterdir())
        assert not list((tmp_path / "profiles").iterdir())
    finally:
        manager.executor.shutdown(wait=True)
