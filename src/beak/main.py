from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from . import __version__
from .browser import BrowserRouter
from .history import HistoryStore
from .jobs import JobManager, JobNotFound
from .playwright_edge import EdgePlaywrightClient
from .schemas import HealthResponse, HistoryListResponse, JobAccepted, JobInfo, JobListResponse, RenderRequest, RenderResult
from .worker import WebView2WorkerClient, WorkerError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("BEAK_DATA_DIR", PROJECT_ROOT / "data"))
WEBVIEW_WORKER = WebView2WorkerClient(PROJECT_ROOT)
EDGE_WORKER = EdgePlaywrightClient()
BROWSERS = BrowserRouter(webview=WEBVIEW_WORKER, edge=EDGE_WORKER)
HISTORY = HistoryStore(DATA_DIR / "history.json")
JOBS = JobManager(
    data_dir=DATA_DIR,
    worker=BROWSERS,
    max_workers=int(os.environ.get("BEAK_MAX_WORKERS", "4")),
    history=HISTORY,
)
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_ALLOWED_ORIGINS_CONFIGURED = "BEAK_MCP_ALLOWED_ORIGINS" in os.environ
MCP_ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.environ.get(
        "BEAK_MCP_ALLOWED_ORIGINS",
        "http://127.0.0.1,http://localhost,http://[::1]",
    ).split(",")
    if origin.strip()
}


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


@app.get("/", include_in_schema=False)
def webui() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parent / "webui" / "index.html")


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
    "/mcp",
    tags=["mcp"],
    summary="Describe the MCP endpoint.",
    description=(
        "Returns a human-readable declaration for Beak's MCP Streamable HTTP endpoint. "
        "MCP clients should POST JSON-RPC messages to this same path."
    ),
)
def mcp_declaration(request: Request) -> JSONResponse:
    _validate_mcp_origin(request)
    if "text/event-stream" in request.headers.get("accept", ""):
        return JSONResponse(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            content={"detail": "This MCP endpoint does not provide a server-initiated SSE stream."},
        )
    return JSONResponse(
        {
            "name": "beak",
            "version": __version__,
            "protocol_version": MCP_PROTOCOL_VERSION,
            "transport": "streamable_http",
            "endpoint": "/mcp",
            "capabilities": {"tools": {"listChanged": False}},
            "tools": _mcp_tools(),
        }
    )


@app.post(
    "/mcp",
    tags=["mcp"],
    summary="Handle MCP JSON-RPC requests.",
    description="Supports initialize, notifications/initialized, tools/list, and tools/call over Streamable HTTP.",
)
async def mcp_endpoint(request: Request) -> Response:
    _validate_mcp_origin(request)
    try:
        message = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(_json_rpc_error(None, -32700, "Parse error"))

    if not isinstance(message, dict):
        return JSONResponse(_json_rpc_error(None, -32600, "Invalid Request"))
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return JSONResponse(_json_rpc_error(message.get("id"), -32600, "Invalid Request"))

    request_id = message.get("id")
    if request_id is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    method = message["method"]
    params = message.get("params") or {}
    if method == "initialize":
        return JSONResponse(_json_rpc_result(request_id, _mcp_initialize_result()))
    if method == "tools/list":
        return JSONResponse(_json_rpc_result(request_id, {"tools": _mcp_tools()}))
    if method == "tools/call":
        return JSONResponse(_mcp_call_tool(request_id, params))
    return JSONResponse(_json_rpc_error(request_id, -32601, f"Method not found: {method}"))


@app.get(
    "/jobs",
    response_model=JobListResponse,
    tags=["jobs"],
    summary="List in-memory jobs.",
    description="Returns current job records. Use active_only=true to show only queued and running jobs.",
)
def list_jobs(active_only: bool = False) -> JobListResponse:
    return JobListResponse(jobs=JOBS.list_jobs(active_only=active_only))


@app.post(
    "/jobs",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["jobs"],
    summary="Add an asynchronous render job.",
    description="Queues a render request and returns immediately with a job id.",
)
def add_job(request: RenderRequest) -> JobAccepted:
    queued_request = request.model_copy(update={"async_mode": True})
    job_id = JOBS.create_job(queued_request)
    return JobAccepted(job_id=job_id, status_url=f"/jobs/{job_id}")


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


@app.post(
    "/jobs/{job_id}/cancel",
    response_model=JobInfo,
    responses={404: {"description": "Job not found."}},
    tags=["jobs"],
    summary="Cancel a queued or running job.",
)
def cancel_job(job_id: str) -> JobInfo:
    try:
        return JOBS.cancel(job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found") from exc


@app.delete(
    "/jobs/{job_id}",
    response_model=JobInfo,
    responses={404: {"description": "Job not found."}},
    tags=["jobs"],
    summary="Cancel a queued or running job.",
    description="Alias of POST /jobs/{job_id}/cancel for clients that model cancellation as deletion.",
)
def delete_job(job_id: str) -> JobInfo:
    return cancel_job(job_id)


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


@app.get(
    "/history",
    response_model=HistoryListResponse,
    tags=["history"],
    summary="List persisted job history.",
)
def list_history(limit: int | None = None) -> HistoryListResponse:
    normalized_limit = None if limit is None else max(1, min(limit, 1000))
    return HistoryListResponse(items=HISTORY.list(limit=normalized_limit))


@app.delete(
    "/history/{job_id}",
    tags=["history"],
    summary="Delete one history record.",
)
def delete_history(job_id: str) -> dict[str, bool]:
    return {"deleted": HISTORY.delete(job_id)}


@app.delete(
    "/history",
    tags=["history"],
    summary="Clear all history records.",
)
def clear_history() -> dict[str, int]:
    return {"deleted": HISTORY.clear()}


def _submit(request: RenderRequest, response: Response) -> RenderResult | JobAccepted:
    if JOBS.should_run_async(request):
        job_id = JOBS.create_job(request)
        response.status_code = status.HTTP_202_ACCEPTED
        return JobAccepted(job_id=job_id, status_url=f"/jobs/{job_id}")

    try:
        return JOBS.run_sync(request)
    except WorkerError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


def _validate_mcp_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    parsed = urlparse(origin)
    normalized = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else origin
    if not MCP_ALLOWED_ORIGINS_CONFIGURED and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return
    if normalized not in MCP_ALLOWED_ORIGINS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="MCP origin is not allowed.")


def _mcp_initialize_result() -> dict[str, Any]:
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "beak", "version": __version__},
    }


def _mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "beak_render",
            "title": "Render or capture a webpage",
            "description": (
                "Render a URL with WebView2 or headless Edge and return rendered HTML, a screenshot, "
                "a single-file MHTML snapshot, or a complete-page ZIP. Supports per-request timeout, "
                "proxy, cookies, SSL-error skipping, and custom artifact output directories."
            ),
            "inputSchema": RenderRequest.model_json_schema(mode="validation"),
        },
        {
            "name": "beak_get_job",
            "title": "Get Beak job status",
            "description": "Fetch status and result metadata for an asynchronous Beak render job.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "minLength": 1, "description": "Job id returned by beak_render."}
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
    ]


def _mcp_call_tool(request_id: Any, params: object) -> dict[str, Any]:
    if not isinstance(params, dict):
        return _json_rpc_error(request_id, -32602, "Invalid params")
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _json_rpc_error(request_id, -32602, "Tool arguments must be an object")

    if name == "beak_render":
        try:
            render_request = RenderRequest.model_validate(arguments)
        except ValidationError as exc:
            return _json_rpc_error(request_id, -32602, "Invalid beak_render arguments", exc.errors())
        try:
            if JOBS.should_run_async(render_request):
                job_id = JOBS.create_job(render_request)
                payload: dict[str, Any] = JobAccepted(job_id=job_id, status_url=f"/jobs/{job_id}").model_dump(mode="json")
            else:
                payload = JOBS.run_sync(render_request).model_dump(mode="json")
        except WorkerError as exc:
            return _json_rpc_result(request_id, _mcp_tool_result({"error": str(exc)}, is_error=True))
        return _json_rpc_result(request_id, _mcp_tool_result(payload))

    if name == "beak_get_job":
        job_id = str(arguments.get("job_id", "")).strip()
        if not job_id:
            return _json_rpc_error(request_id, -32602, "job_id is required")
        try:
            payload = JOBS.get(job_id).model_dump(mode="json")
        except JobNotFound:
            return _json_rpc_result(request_id, _mcp_tool_result({"error": "job not found"}, is_error=True))
        return _json_rpc_result(request_id, _mcp_tool_result(payload))

    return _json_rpc_error(request_id, -32602, f"Unknown tool: {name}")


def _mcp_tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def _json_rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _json_rpc_error(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def run() -> None:
    from .cli import run_server

    run_server(host="127.0.0.1", port=8000)


if __name__ == "__main__":
    run()
