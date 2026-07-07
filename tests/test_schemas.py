from beak.cli import build_parser, normalize_bind_host
from beak.jobs import JobManager
from beak.schemas import BrowserEngine, OutputType, RenderRequest


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
