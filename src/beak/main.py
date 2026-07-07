from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import FileResponse

from . import __version__
from .browser import BrowserRouter
from .jobs import JobManager, JobNotFound
from .playwright_edge import EdgePlaywrightClient
from .schemas import HealthResponse, JobAccepted, JobInfo, RenderRequest, RenderResult
from .worker import WebView2WorkerClient, WorkerError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("BEAK_DATA_DIR", PROJECT_ROOT / "data"))
WEBVIEW_WORKER = WebView2WorkerClient(PROJECT_ROOT)
EDGE_WORKER = EdgePlaywrightClient()
BROWSERS = BrowserRouter(webview=WEBVIEW_WORKER, edge=EDGE_WORKER)
JOBS = JobManager(data_dir=DATA_DIR, worker=BROWSERS, max_workers=int(os.environ.get("BEAK_MAX_WORKERS", "4")))


app = FastAPI(
    title="Beak",
    version=__version__,
    summary="Windows browser-rendering crawler service.",
    description=(
        "Beak exposes HTTP APIs for rendering pages with Microsoft Edge WebView2 or headless "
        "Playwright-driven Microsoft Edge on Windows. Each task uses an isolated browser profile "
        "so cookies, proxy settings, and browser state do not bleed across requests. Small "
        "`rendered_html` and `screenshot` requests can complete synchronously; heavy "
        "`complete_page` and `single_file` exports default to asynchronous jobs."
    ),
    contact={"name": "Beak API"},
    license_info={"name": "Apache-2.0"},
)


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="Check service and worker availability.",
)
def health() -> HealthResponse:
    return HealthResponse(
        ok=True,
        worker_configured=WEBVIEW_WORKER.is_configured,
        worker_path=str(WEBVIEW_WORKER.worker_path),
        webview_worker_configured=WEBVIEW_WORKER.is_configured,
        webview_worker_path=str(WEBVIEW_WORKER.worker_path),
        edge_playwright_configured=EDGE_WORKER.is_configured,
        edge_executable_path=EDGE_WORKER.edge_executable_path(),
    )


@app.post(
    "/render",
    response_model=RenderResult | JobAccepted,
    responses={
        202: {"model": JobAccepted, "description": "Task accepted and queued for asynchronous execution."},
        503: {"description": "The WebView2 worker is not published or failed to run."},
    },
    tags=["rendering"],
    summary="Render a page through WebView2 or headless Edge.",
    description=(
        "Loads the target URL in the selected browser engine, applies optional proxy, Cookie, "
        "User-Agent, viewport and wait strategy settings, then exports the requested result. "
        "Use `engine=webview` for WebView2 or `engine=edge` for headless Playwright Edge. "
        "When `async_mode` is true, the endpoint returns a job id immediately."
    ),
)
def render(request: RenderRequest, response: Response) -> RenderResult | JobAccepted:
    return _submit(request, response)


@app.post(
    "/capture",
    response_model=RenderResult | JobAccepted,
    responses={
        202: {"model": JobAccepted, "description": "Task accepted and queued for asynchronous execution."},
        503: {"description": "The WebView2 worker is not published or failed to run."},
    },
    tags=["rendering"],
    summary="Alias of /render for capture-oriented clients.",
    description="Accepts the same request body as `/render` and exists for clients that model this operation as capture.",
)
def capture(request: RenderRequest, response: Response) -> RenderResult | JobAccepted:
    return _submit(request, response)


@app.get(
    "/jobs/{job_id}",
    response_model=JobInfo,
    responses={404: {"description": "Job not found."}},
    tags=["jobs"],
    summary="Get an asynchronous job status and result metadata.",
)
def get_job(job_id: str) -> JobInfo:
    try:
        return JOBS.get(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found") from exc


@app.get(
    "/jobs/{job_id}/artifact/{name}",
    responses={404: {"description": "Artifact not found."}},
    tags=["jobs"],
    summary="Download a job artifact.",
    description="Downloads artifacts such as screenshot images, MHTML files, or complete-page HTML packages.",
)
def download_artifact(job_id: str, name: str) -> FileResponse:
    try:
        path = JOBS.artifact_path(job_id, name)
    except JobNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found") from exc
    return FileResponse(path)


def _submit(request: RenderRequest, response: Response) -> RenderResult | JobAccepted:
    if JOBS.should_run_async(request):
        job_id = JOBS.create_job(request)
        response.status_code = status.HTTP_202_ACCEPTED
        return JobAccepted(job_id=job_id, status_url=f"/jobs/{job_id}")

    try:
        return JOBS.run_sync(request)
    except WorkerError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


def run() -> None:
    uvicorn.run("beak.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()
