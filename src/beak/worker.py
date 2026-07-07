from __future__ import annotations

import json
import os
import shutil
import subprocess
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
            self.worker_path = (
                project_root
                / "workers"
                / "Beak.WebView2Worker"
                / "bin"
                / "Release"
                / "net8.0-windows"
                / "win-x64"
                / "publish"
                / "Beak.WebView2Worker.exe"
            )

    @property
    def is_configured(self) -> bool:
        return self.worker_path.exists() or shutil.which("dotnet") is not None

    def invoke(
        self,
        *,
        job_id: str,
        request: RenderRequest,
        job_dir: Path,
        user_data_dir: Path,
    ) -> WorkerResult:
        job_dir.mkdir(parents=True, exist_ok=True)
        user_data_dir.mkdir(parents=True, exist_ok=True)

        worker_request = self._to_worker_payload(job_id, request, job_dir, user_data_dir)
        request_path = job_dir / "worker-request.json"
        request_path.write_text(json.dumps(worker_request, ensure_ascii=False, indent=2), encoding="utf-8")

        command = self._build_command(request_path)
        timeout_seconds = max(1, int(request.timeout_ms / 1000) + 20)
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise WorkerError(self._missing_worker_message()) from exc
        except subprocess.TimeoutExpired as exc:
            raise WorkerError(f"WebView2 worker exceeded timeout after {timeout_seconds}s.") from exc

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
        if dotnet:
            project = self.project_root / "workers" / "Beak.WebView2Worker" / "Beak.WebView2Worker.csproj"
            return [dotnet, "run", "--project", str(project), "--", "--request", str(request_path)]

        raise WorkerError(self._missing_worker_message())

    def _missing_worker_message(self) -> str:
        return (
            "WebView2 worker executable was not found and dotnet is not available. "
            "Install the .NET SDK, run `dotnet publish workers/Beak.WebView2Worker "
            "-c Release -r win-x64 --self-contained false`, or set BEAK_WEBVIEW2_WORKER "
            "to a published Beak.WebView2Worker.exe."
        )

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

