from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BeakClientError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class BeakClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def render(self, **request: Any) -> dict[str, Any]:
        return self._request("POST", "/render", request)

    def capture(self, **request: Any) -> dict[str, Any]:
        return self._request("POST", "/capture", request)

    def add_job(self, **request: Any) -> dict[str, Any]:
        return self._request("POST", "/jobs", request)

    def list_jobs(self, *, active_only: bool = False) -> dict[str, Any]:
        query = urlencode({"active_only": str(active_only).lower()})
        return self._request("GET", f"/jobs?{query}")

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jobs/{job_id}")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/cancel", {})

    def list_history(self, *, limit: int | None = None) -> dict[str, Any]:
        query = "" if limit is None else f"?{urlencode({'limit': limit})}"
        return self._request("GET", f"/history{query}")

    def delete_history(self, job_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/history/{job_id}")

    def clear_history(self) -> dict[str, Any]:
        return self._request("DELETE", "/history")

    def download_artifact(self, job_id: str, name: str, destination: str | Path) -> Path:
        destination_path = Path(destination)
        if destination_path.is_dir():
            destination_path = destination_path / name
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        data = self._request_bytes("GET", f"/jobs/{job_id}/artifact/{name}")
        destination_path.write_bytes(data)
        return destination_path

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self._url(path), data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BeakClientError(exc.code, self._error_message(detail)) from exc
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _request_bytes(self, method: str, path: str) -> bytes:
        request = Request(self._url(path), method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BeakClientError(exc.code, self._error_message(detail)) from exc

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else f'/{path}'}"

    @staticmethod
    def _error_message(detail: str) -> str:
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            return detail or "Beak request failed"
        if isinstance(parsed, dict):
            value = parsed.get("detail")
            if isinstance(value, str):
                return value
        return detail or "Beak request failed"
