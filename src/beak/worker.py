from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .schemas import RenderRequest, WorkerResult


class WorkerError(RuntimeError):
    """Raised when the WebView2 worker cannot complete a task."""


class WebView2WorkerClient:
    def __init__(self, project_root: Path, worker_path: str | None = None) -> None:
        self.project_root = project_root
        configured = worker_path or os.environ.get("BEAK_WEBVIEW2_WORKER")
        if configured:
            self.worker_path = Path(configured)
        else:
            self.worker_path = self._default_worker_path()

    @property
    def is_configured(self) -> bool:
        return self.worker_path.exists() or (self._source_project_path().exists() and shutil.which("dotnet") is not None)

    def invoke(
        self,
        *,
        job_id: str,
        request: RenderRequest,
        job_dir: Path,
        user_data_dir: Path,
        cancel_event: threading.Event | None = None,
    ) -> WorkerResult:
        job_dir.mkdir(parents=True, exist_ok=True)
        user_data_dir.mkdir(parents=True, exist_ok=True)

        worker_request = self._to_worker_payload(job_id, request, job_dir, user_data_dir)
        request_path = job_dir / "worker-request.json"
        request_path.write_text(json.dumps(worker_request, ensure_ascii=False, indent=2), encoding="utf-8")

        command = self._build_command(request_path)
        timeout_seconds = max(1, int(request.timeout_ms / 1000) + 20)
        try:
            completed = self._run_command(
                command,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
        except FileNotFoundError as exc:
            raise WorkerError(self._missing_worker_message()) from exc

        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            detail = stderr or stdout or f"exit code {completed.returncode}"
            raise WorkerError(f"WebView2 worker failed: {detail}")

        result = self._parse_stdout(completed.stdout)
        if not result.success:
            raise WorkerError(result.error or "WebView2 worker reported an unknown failure.")
        return result

    def _build_command(self, request_path: Path) -> list[str]:
        if self.worker_path.exists():
            return [str(self.worker_path), "--request", str(request_path)]

        dotnet = shutil.which("dotnet")
        project = self._source_project_path()
        if dotnet and project.exists():
            return [dotnet, "run", "--project", str(project), "--", "--request", str(request_path)]

        raise WorkerError(self._missing_worker_message())

    def _run_command(
        self,
        command: list[str],
        *,
        timeout_seconds: int,
        cancel_event: threading.Event | None,
    ) -> subprocess.CompletedProcess[str]:
        started_at = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=str(self.project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if cancel_event is not None and cancel_event.is_set():
                    process.kill()
                    stdout, stderr = process.communicate()
                    raise WorkerError("WebView2 worker was cancelled.") from None
                if time.monotonic() - started_at > timeout_seconds:
                    process.kill()
                    stdout, stderr = process.communicate()
                    raise WorkerError(f"WebView2 worker exceeded timeout after {timeout_seconds}s.") from None

    def _missing_worker_message(self) -> str:
        return (
            "WebView2 worker executable was not found and dotnet is not available. "
            "Install a Beak wheel that bundles the worker, run `dotnet publish "
            "workers/Beak.WebView2Worker -c Release -r win-x64 --self-contained true`, "
            "or set BEAK_WEBVIEW2_WORKER to a published Beak.WebView2Worker.exe."
        )

    def _default_worker_path(self) -> Path:
        packaged_worker = Path(__file__).resolve().parent / "webview2-worker" / "Beak.WebView2Worker.exe"
        if packaged_worker.exists():
            return packaged_worker

        return (
            self.project_root
            / "workers"
            / "Beak.WebView2Worker"
            / "bin"
            / "Release"
            / "net8.0-windows"
            / "win-x64"
            / "publish"
            / "Beak.WebView2Worker.exe"
        )

    def _source_project_path(self) -> Path:
        return self.project_root / "workers" / "Beak.WebView2Worker" / "Beak.WebView2Worker.csproj"

    @staticmethod
    def _parse_stdout(stdout: str) -> WorkerResult:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            return WorkerResult.model_validate(data)
        raise WorkerError("WebView2 worker did not emit a JSON result.")

    @staticmethod
    def _to_worker_payload(
        job_id: str,
        request: RenderRequest,
        job_dir: Path,
        user_data_dir: Path,
    ) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "url": str(request.url),
            "timeout_ms": request.timeout_ms,
            "ignore_https_errors": request.ignore_https_errors,
            "wait_until": request.wait.until,
            "after_load_ms": request.wait.after_load_ms,
            "network_idle_ms": request.wait.network_idle_ms,
            "fixed_delay_ms": request.wait.fixed_delay_ms,
            "proxy": request.proxy.model_dump(mode="json") if request.proxy else None,
            "cookies": [cookie.model_dump(mode="json") for cookie in request.cookies],
            "user_agent": request.user_agent,
            "viewport": request.viewport.model_dump(mode="json"),
            "output": request.output,
            "screenshot_format": request.screenshot_format,
            "jpeg_quality": request.jpeg_quality,
            "user_data_dir": str(user_data_dir),
            "output_dir": str(job_dir),
        }
