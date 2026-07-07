from beak.jobs import JobManager
from beak.schemas import OutputType, RenderRequest


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

