from __future__ import annotations

from pathlib import Path

from .playwright_edge import EdgePlaywrightClient
from .schemas import BrowserEngine, RenderRequest, WorkerResult
from .worker import WebView2WorkerClient


class BrowserRouter:
    def __init__(self, *, webview: WebView2WorkerClient, edge: EdgePlaywrightClient) -> None:
        self.webview = webview
        self.edge = edge

    def invoke(
        self,
        *,
        job_id: str,
        request: RenderRequest,
        job_dir: Path,
        user_data_dir: Path,
    ) -> WorkerResult:
        if request.engine == BrowserEngine.EDGE:
            return self.edge.invoke(job_id=job_id, request=request, job_dir=job_dir, user_data_dir=user_data_dir)
        return self.webview.invoke(job_id=job_id, request=request, job_dir=job_dir, user_data_dir=user_data_dir)

