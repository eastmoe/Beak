import threading

from beak import BeakClient
from beak.cli import build_parser, normalize_bind_host
from beak.history import HistoryStore
from beak.jobs import JobManager
from beak.main import app
from beak.schemas import BrowserEngine, JobStatus, OutputType, RenderRequest, WorkerArtifact, WorkerResult
from beak.worker import WebView2WorkerClient
import beak.worker as worker_module
import beak.client as client_module
import beak.main as main_module
from fastapi.testclient import TestClient


def test_complete_page_defaults_to_async(tmp_path):
    class DummyWorker:
        pass

    manager = JobManager(data_dir=tmp_path, worker=DummyWorker())  # type: ignore[arg-type]
    request = RenderRequest(url="https://example.com", output=OutputType.COMPLETE_PAGE)

    assert manager.should_run_async(request) is True


def test_rendered_html_defaults_to_sync(tmp_path):
    class DummyWorker:
        pass

    manager = JobManager(data_dir=tmp_path, worker=DummyWorker())  # type: ignore[arg-type]
    request = RenderRequest(url="https://example.com", output=OutputType.RENDERED_HTML)

    assert manager.should_run_async(request) is False


def test_browser_engine_defaults_to_webview():
    request = RenderRequest(url="https://example.com")

    assert request.engine == BrowserEngine.WEBVIEW


def test_browser_engine_accepts_edge():
    request = RenderRequest(url="https://example.com", engine=BrowserEngine.EDGE)

    assert request.engine == BrowserEngine.EDGE


def test_render_request_accepts_request_level_options(tmp_path):
    request = RenderRequest(
        url="https://example.com",
        timeout_ms=12345,
        ignore_https_errors=True,
        output_dir=str(tmp_path / "captures"),
    )

    assert request.timeout_ms == 12345
    assert request.ignore_https_errors is True
    assert request.output_dir == tmp_path / "captures"


def test_job_manager_uses_custom_output_dir(tmp_path):
    class DummyWorker:
        def invoke(self, *, job_id, request, job_dir, user_data_dir, cancel_event=None):  # noqa: ANN001
            job_dir.mkdir(parents=True, exist_ok=True)
            path = job_dir / "rendered.html"
            path.write_text("<html></html>", encoding="utf-8")
            return WorkerResult(
                success=True,
                output=request.output,
                html_path=str(path),
                artifacts=[WorkerArtifact(name="rendered_html", content_type="text/html; charset=utf-8", path=str(path))],
            )

    output_dir = tmp_path / "custom-output"
    manager = JobManager(data_dir=tmp_path / "data", worker=DummyWorker())  # type: ignore[arg-type]
    request = RenderRequest(url="https://example.com", output_dir=output_dir)

    result = manager.run_sync(request)

    assert result.artifacts[0].path == str(output_dir / "rendered.html")
    assert (output_dir / "rendered.html").exists()


def test_job_manager_lists_and_cancels_queued_jobs(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class BlockingWorker:
        def invoke(self, *, job_id, request, job_dir, user_data_dir, cancel_event=None):  # noqa: ANN001
            started.set()
            release.wait(timeout=5)
            job_dir.mkdir(parents=True, exist_ok=True)
            path = job_dir / "rendered.html"
            path.write_text("<html></html>", encoding="utf-8")
            return WorkerResult(
                success=True,
                output=request.output,
                html_path=str(path),
                artifacts=[WorkerArtifact(name="rendered_html", content_type="text/html; charset=utf-8", path=str(path))],
            )

    manager = JobManager(data_dir=tmp_path, worker=BlockingWorker(), max_workers=1)  # type: ignore[arg-type]
    first = manager.create_job(RenderRequest(url="https://example.com", async_mode=True))
    assert started.wait(timeout=5)
    second = manager.create_job(RenderRequest(url="https://example.com", async_mode=True))

    cancelled = manager.cancel(second)
    active = manager.list_jobs(active_only=True)
    release.set()
    manager._futures[first].result(timeout=5)  # noqa: SLF001
    manager.executor.shutdown(wait=True)

    assert cancelled.status == JobStatus.CANCELLED
    assert all(job.job_id != second for job in active)


def test_job_manager_persists_history(tmp_path):
    class DummyWorker:
        def invoke(self, *, job_id, request, job_dir, user_data_dir, cancel_event=None):  # noqa: ANN001
            job_dir.mkdir(parents=True, exist_ok=True)
            path = job_dir / "rendered.html"
            path.write_text("<html></html>", encoding="utf-8")
            return WorkerResult(
                success=True,
                output=request.output,
                html_path=str(path),
                artifacts=[WorkerArtifact(name="rendered_html", content_type="text/html; charset=utf-8", path=str(path))],
            )

    history = HistoryStore(tmp_path / "history.json")
    manager = JobManager(data_dir=tmp_path / "data", worker=DummyWorker(), history=history)  # type: ignore[arg-type]

    result = manager.run_sync(RenderRequest(url="https://example.com"))
    records = history.list()

    assert records[0].job_id == result.job_id
    assert records[0].status == JobStatus.SUCCEEDED
    assert records[0].artifacts[0].name == "rendered_html"


def test_server_cli_accepts_host_and_port():
    args = build_parser().parse_args(["server", "--host", "::", "--port", "8080"])

    assert args.command == "server"
    assert args.host == "::"
    assert args.port == 8080


def test_server_cli_requires_subcommand():
    try:
        build_parser().parse_args([])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("beak CLI should require an explicit subcommand")


def test_server_cli_normalizes_all_host_aliases():
    assert normalize_bind_host("all") == "0.0.0.0"
    assert normalize_bind_host("*") == "0.0.0.0"
    assert normalize_bind_host("all-v6") == "::"
    assert normalize_bind_host("::1") == "::1"


def test_webview_worker_prefers_packaged_executable(tmp_path, monkeypatch):
    package_dir = tmp_path / "site-packages" / "beak"
    packaged_worker = package_dir / "webview2-worker" / "Beak.WebView2Worker.exe"
    packaged_worker.parent.mkdir(parents=True)
    packaged_worker.write_text("", encoding="utf-8")
    monkeypatch.setattr(worker_module, "__file__", str(package_dir / "worker.py"))

    client = WebView2WorkerClient(project_root=tmp_path / "project")

    assert client.worker_path == packaged_worker


def test_webview_worker_payload_includes_ignore_https_errors(tmp_path):
    request = RenderRequest(url="https://example.com", ignore_https_errors=True)

    payload = WebView2WorkerClient._to_worker_payload(  # noqa: SLF001
        "job-id",
        request,
        tmp_path / "job",
        tmp_path / "profile",
    )

    assert payload["ignore_https_errors"] is True


def test_mcp_tools_list_exposes_render_tool():
    client = TestClient(app)

    response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response.status_code == 200
    data = response.json()
    tools = data["result"]["tools"]
    render_tool = next(tool for tool in tools if tool["name"] == "beak_render")
    properties = render_tool["inputSchema"]["properties"]
    assert "ignore_https_errors" in properties
    assert "output_dir" in properties
    assert "timeout_ms" in properties


def test_mcp_allows_localhost_origin_with_port():
    client = TestClient(app)

    response = client.post(
        "/mcp",
        headers={"Origin": "http://localhost:3000"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    )

    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2025-06-18"


def test_webui_root_serves_html():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Beak WebUI" in response.text


def test_jobs_and_history_api_with_patched_manager(tmp_path, monkeypatch):
    class DummyWorker:
        def invoke(self, *, job_id, request, job_dir, user_data_dir, cancel_event=None):  # noqa: ANN001
            job_dir.mkdir(parents=True, exist_ok=True)
            path = job_dir / "rendered.html"
            path.write_text("<html></html>", encoding="utf-8")
            return WorkerResult(
                success=True,
                output=request.output,
                html_path=str(path),
                artifacts=[WorkerArtifact(name="rendered_html", content_type="text/html; charset=utf-8", path=str(path))],
            )

    history = HistoryStore(tmp_path / "history.json")
    manager = JobManager(data_dir=tmp_path / "data", worker=DummyWorker(), history=history)  # type: ignore[arg-type]
    monkeypatch.setattr(main_module, "JOBS", manager)
    monkeypatch.setattr(main_module, "HISTORY", history)
    client = TestClient(app)

    accepted = client.post("/jobs", json={"url": "https://example.com", "output": "rendered_html"}).json()
    manager._futures[accepted["job_id"]].result(timeout=5)  # noqa: SLF001
    jobs = client.get("/jobs").json()
    history_response = client.get("/history").json()
    deleted = client.delete(f"/history/{accepted['job_id']}").json()

    assert jobs["jobs"][0]["job_id"] == accepted["job_id"]
    assert history_response["items"][0]["job_id"] == accepted["job_id"]
    assert deleted["deleted"] is True


def test_python_client_posts_json(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        def read(self):
            return b'{"job_id":"abc","status":"queued","status_url":"/jobs/abc"}'

    def fake_urlopen(request, timeout):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = request.data
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(client_module, "urlopen", fake_urlopen)
    client = BeakClient("http://beak.test/", timeout=12)

    result = client.add_job(url="https://example.com", output="screenshot")

    assert result["job_id"] == "abc"
    assert captured["url"] == "http://beak.test/jobs"
    assert captured["method"] == "POST"
    assert b'"output": "screenshot"' in captured["body"]
    assert captured["timeout"] == 12
