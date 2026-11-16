"""Chromium's native download manager streams binary files to disk."""

import mimetypes
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .schemas import OutputType, WorkerArtifact, WorkerResult
from .worker import WorkerError


def download_resource(
    context, page, request, job_dir: Path, cancel_event: threading.Event | None
):
    deadline = time.monotonic() + request.timeout_ms / 1000
    state = {
        "guid": None,
        "status": "pending",
        "received": 0,
        "total": 0,
        "mime": "application/octet-stream",
        "error": None,
        "error_code": None,
        "response": False,
    }
    session = context.browser.new_browser_cdp_session()

    def started(event):
        state.update(guid=event["guid"])

    def progress(event):
        if state["guid"] == event["guid"]:
            state.update(
                status=event["state"],
                received=event["receivedBytes"],
                total=event["totalBytes"],
            )

    session.on("Browser.downloadWillBegin", started)
    session.on("Browser.downloadProgress", progress)
    session.send(
        "Browser.setDownloadBehavior",
        {
            "behavior": "allowAndName",
            "downloadPath": str(job_dir.resolve()),
            "eventsEnabled": True,
        },
    )
    referer = str(request.download_referer or request.url)
    origin = urlsplit(referer)
    bootstrap = f"{origin.scheme}://{origin.netloc}/__beak_download__/{job_dir.name}"
    page.route(
        bootstrap,
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><title>Download</title>",
        ),
    )
    completed = False
    try:
        page.goto(bootstrap, wait_until="domcontentloaded", timeout=request.timeout_ms)
        page.unroute(bootstrap)
        network = context.new_cdp_session(page)

        def intercepted(event):
            request_id = event["requestId"]
            status = event.get("responseStatusCode")
            if status is None:
                headers = {**event["request"]["headers"], "Referer": referer}
                network.send(
                    "Fetch.continueRequest",
                    {
                        "requestId": request_id,
                        "headers": [
                            {"name": key, "value": value}
                            for key, value in headers.items()
                        ],
                    },
                )
                return
            headers = event.get("responseHeaders", [])
            state["response"] = True
            values = {item["name"].lower(): item["value"] for item in headers}
            length = values.get("content-length", "")
            if status >= 400 or (
                length.isdigit() and int(length) > request.max_download_bytes
            ):
                state["error"] = (
                    f"浏览器媒体下载返回 HTTP {status}。"
                    if status >= 400
                    else "媒体文件超过大小限制。"
                )
                state["error_code"] = (
                    "DOWNLOAD_HTTP_ERROR" if status >= 400 else "DOWNLOAD_TOO_LARGE"
                )
                network.send(
                    "Fetch.fulfillRequest",
                    {
                        "requestId": request_id,
                        "responseCode": status if status >= 400 else 413,
                        "responseHeaders": [{"name": "Content-Length", "value": "0"}],
                        "body": "",
                    },
                )
            elif 200 <= status < 300:
                state["mime"] = values.get(
                    "content-type", "application/octet-stream"
                ).split(";")[0]
                # Change only the disposition; Chromium streams the original response body to disk.
                headers = [
                    item
                    for item in headers
                    if item["name"].lower() != "content-disposition"
                ]
                suffix = mimetypes.guess_extension(state["mime"]) or ".bin"
                headers.append(
                    {
                        "name": "Content-Disposition",
                        "value": f'attachment; filename="download{suffix}"',
                    }
                )
                network.send(
                    "Fetch.continueResponse",
                    {
                        "requestId": request_id,
                        "responseCode": status,
                        "responseHeaders": headers,
                    },
                )
            else:
                network.send("Fetch.continueResponse", {"requestId": request_id})

        network.on("Fetch.requestPaused", intercepted)

        def failed(event):
            reason = event.get("errorText", "")
            if not state["error"] and reason != "net::ERR_ABORTED":
                state.update(
                    error=f"浏览器媒体连接失败：{reason}",
                    error_code="DOWNLOAD_NETWORK_ERROR",
                )

        network.on("Network.loadingFailed", failed)
        network.send("Network.enable")
        network.send(
            "Fetch.enable",
            {
                "patterns": [
                    {"urlPattern": "*", "requestStage": stage}
                    for stage in ("Request", "Response")
                ]
            },
        )
        # Do not block on response headers: the deadline/cancel loop must also cover connection stalls.
        page.evaluate(
            "url => { setTimeout(() => window.location.assign(url), 0); }",
            str(request.url),
        )
        while True:
            if state["error"]:
                raise WorkerError(
                    state["error"], state["error_code"] or "WORKER_FAILED"
                )
            if cancel_event is not None and cancel_event.is_set():
                raise WorkerError("浏览器媒体下载已取消。", "DOWNLOAD_CANCELLED")
            if time.monotonic() >= deadline:
                phase = (
                    "接收文件"
                    if state["guid"]
                    else "等待浏览器开始下载"
                    if state["response"]
                    else "等待响应头"
                )
                raise WorkerError(
                    f"浏览器媒体下载超时（{request.timeout_ms}ms，{phase}，{int(state['received'])}/{int(state['total'])} 字节）。",
                    "DOWNLOAD_TIMEOUT",
                )
            if max(state["received"], state["total"]) > request.max_download_bytes:
                raise WorkerError("媒体文件超过大小限制。", "DOWNLOAD_TOO_LARGE")
            if state["status"] == "canceled":
                raise WorkerError(
                    "浏览器媒体下载失败或被服务器中断。", "DOWNLOAD_INTERRUPTED"
                )
            if state["status"] == "completed":
                path = job_dir / state["guid"]
                if not path.is_file() or path.stat().st_size != state["received"]:
                    raise WorkerError("浏览器媒体下载不完整。", "DOWNLOAD_INCOMPLETE")
                path = path.replace(job_dir / "download.bin")
                completed = True
                return WorkerResult(
                    success=True,
                    output=OutputType.DOWNLOAD,
                    artifacts=[
                        WorkerArtifact(
                            name="download", content_type=state["mime"], path=str(path)
                        )
                    ],
                    metadata={
                        "url": str(request.url),
                        "engine": "edge",
                        "size_bytes": path.stat().st_size,
                    },
                )
            page.wait_for_timeout(100)
    finally:
        if not completed and state["guid"]:
            if state["status"] not in {"canceled", "completed"}:
                session.send("Browser.cancelDownload", {"guid": state["guid"]})
            for suffix in ("", ".crdownload"):
                (job_dir / (state["guid"] + suffix)).unlink(missing_ok=True)
        session.detach()
