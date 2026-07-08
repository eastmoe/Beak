from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from .schemas import OutputType, RenderRequest, WorkerArtifact, WorkerResult
from .semantic import SEMANTIC_DOM_SCRIPT, semantic_items_to_jsonl, semantic_items_to_markdown
from .worker import WorkerError


class EdgePlaywrightClient:
    """Headless Microsoft Edge backend driven through Playwright."""

    @property
    def is_configured(self) -> bool:
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
        return self.edge_executable_path() is not None

    @staticmethod
    def edge_executable_path() -> str | None:
        command = shutil.which("msedge")
        if command:
            return command
        candidates = [
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None

    def invoke(
        self,
        *,
        job_id: str,
        request: RenderRequest,
        job_dir: Path,
        user_data_dir: Path,
        cancel_event: threading.Event | None = None,
    ) -> WorkerResult:
        try:
            return self._invoke(
                job_id=job_id,
                request=request,
                job_dir=job_dir,
                user_data_dir=user_data_dir,
                cancel_event=cancel_event,
            )
        except WorkerError:
            raise
        except Exception as exc:  # noqa: BLE001 - callers need a normalized browser failure.
            raise WorkerError(f"Playwright Edge failed: {exc}") from exc

    def _invoke(
        self,
        *,
        job_id: str,
        request: RenderRequest,
        job_dir: Path,
        user_data_dir: Path,
        cancel_event: threading.Event | None,
    ) -> WorkerResult:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise WorkerError("Playwright is not installed. Run `pip install -e .` or `pip install playwright`.") from exc

        job_dir.mkdir(parents=True, exist_ok=True)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        responses: list[Any] = []
        timeout_ms = request.timeout_ms
        self._raise_if_cancelled(cancel_event)

        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "channel": "msedge",
                "headless": True,
                "viewport": {"width": request.viewport.width, "height": request.viewport.height},
                "device_scale_factor": request.viewport.device_scale_factor,
                "ignore_https_errors": request.ignore_https_errors,
            }
            if request.user_agent:
                launch_options["user_agent"] = request.user_agent
            if request.proxy:
                launch_options["proxy"] = {
                    "server": request.proxy.server,
                    **({"bypass": request.proxy.bypass_list} if request.proxy.bypass_list else {}),
                }

            context = playwright.chromium.launch_persistent_context(str(user_data_dir), **launch_options)
            try:
                context.set_default_timeout(timeout_ms)
                context.set_default_navigation_timeout(timeout_ms)
                self._apply_cookies(context, request)
                page = context.new_page()
                page.on("response", lambda response: responses.append(response))

                goto_wait = self._goto_wait_until(request)
                self._raise_if_cancelled(cancel_event)
                page.goto(str(request.url), wait_until=goto_wait, timeout=timeout_ms)
                self._raise_if_cancelled(cancel_event)
                if request.wait.until == "network_idle":
                    page.wait_for_load_state("networkidle", timeout=timeout_ms)
                elif request.wait.until == "fixed_delay" and request.wait.fixed_delay_ms > 0:
                    page.wait_for_timeout(request.wait.fixed_delay_ms)
                if request.wait.after_load_ms > 0:
                    page.wait_for_timeout(request.wait.after_load_ms)
                self._raise_if_cancelled(cancel_event)

                html = page.content()
                if request.output == OutputType.RENDERED_HTML:
                    return self._export_rendered_html(request, job_dir, html)
                if request.output == OutputType.SCREENSHOT:
                    return self._export_screenshot(page, request, job_dir)
                if request.output == OutputType.SINGLE_FILE:
                    return self._export_single_file(context, page, request, job_dir, user_data_dir)
                if request.output == OutputType.COMPLETE_PAGE:
                    return self._export_complete_page(request, job_dir, user_data_dir, html, responses)
                if request.output in {OutputType.SEMANTIC_MARKDOWN, OutputType.SEMANTIC_JSONL}:
                    return self._export_semantic_dom(page, request, job_dir)
                if request.output == OutputType.ACCESSIBILITY_YAML:
                    return self._export_accessibility_yaml(page, request, job_dir)
                raise WorkerError(f"Unsupported output type: {request.output}")
            except PlaywrightTimeoutError as exc:
                raise WorkerError(f"Playwright Edge timed out after {timeout_ms}ms: {exc}") from exc
            finally:
                context.close()

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise WorkerError("Playwright Edge worker was cancelled.")

    @staticmethod
    def _goto_wait_until(request: RenderRequest) -> str:
        if request.wait.until == "dom_content_loaded":
            return "domcontentloaded"
        if request.wait.until == "network_idle":
            return "domcontentloaded"
        return "load"

    @staticmethod
    def _apply_cookies(context: Any, request: RenderRequest) -> None:
        cookies = []
        for cookie in request.cookies:
            item: dict[str, Any] = {
                "name": cookie.name,
                "value": cookie.value,
                "path": cookie.path,
                "secure": cookie.secure,
                "httpOnly": cookie.http_only,
            }
            if cookie.domain:
                item["domain"] = cookie.domain
            else:
                item["url"] = str(request.url)
            if cookie.expires_unix is not None:
                item["expires"] = cookie.expires_unix
            cookies.append(item)
        if cookies:
            context.add_cookies(cookies)

    def _export_rendered_html(self, request: RenderRequest, job_dir: Path, html: str) -> WorkerResult:
        path = job_dir / "rendered.html"
        path.write_text(html, encoding="utf-8")
        return WorkerResult(
            success=True,
            output=request.output,
            html_path=str(path),
            artifacts=[WorkerArtifact(name="rendered_html", content_type="text/html; charset=utf-8", path=str(path))],
            metadata=self._metadata(request, None),
        )

    def _export_screenshot(self, page: Any, request: RenderRequest, job_dir: Path) -> WorkerResult:
        extension = "jpg" if request.screenshot_format == "jpeg" else "png"
        content_type = "image/jpeg" if request.screenshot_format == "jpeg" else "image/png"
        path = job_dir / f"screenshot.{extension}"
        options: dict[str, Any] = {"path": str(path), "type": request.screenshot_format.value}
        if request.screenshot_format == "jpeg":
            options["quality"] = request.jpeg_quality
        page.screenshot(**options)
        metadata = self._metadata(request, None)
        metadata["jpeg_quality_requested"] = request.jpeg_quality
        return WorkerResult(
            success=True,
            output=request.output,
            artifacts=[WorkerArtifact(name="screenshot", content_type=content_type, path=str(path))],
            metadata=metadata,
        )

    def _export_single_file(
        self,
        context: Any,
        page: Any,
        request: RenderRequest,
        job_dir: Path,
        user_data_dir: Path,
    ) -> WorkerResult:
        session = context.new_cdp_session(page)
        session.send("Page.enable")
        snapshot = session.send("Page.captureSnapshot", {"format": "mhtml"})
        path = job_dir / "page.mhtml"
        path.write_text(snapshot.get("data", ""), encoding="utf-8")
        return WorkerResult(
            success=True,
            output=request.output,
            artifacts=[WorkerArtifact(name="single_file", content_type="multipart/related", path=str(path))],
            metadata=self._metadata(request, user_data_dir),
        )

    def _export_complete_page(
        self,
        request: RenderRequest,
        job_dir: Path,
        user_data_dir: Path,
        html: str,
        responses: list[Any],
    ) -> WorkerResult:
        package_dir = job_dir / "complete_page"
        resources_dir = package_dir / "resources"
        resources_dir.mkdir(parents=True, exist_ok=True)

        resource_map: dict[str, str] = {}
        for response in responses:
            try:
                url = response.url
                body = response.body()
            except Exception:  # noqa: BLE001 - some responses are no longer readable.
                continue
            if not url or not body:
                continue
            content_type = self._content_type(response)
            name = f"{self._hash(url)}{self._extension_for(url, content_type)}"
            path = resources_dir / name
            path.write_bytes(body)
            resource_map[url] = f"resources/{name}"
            parsed = urlparse(url)
            resource_map[f"{parsed.scheme}://{parsed.netloc}{parsed.path}"] = f"resources/{name}"

        index_path = package_dir / "index.html"
        index_path.write_text(self._rewrite_resource_references(html, str(request.url), resource_map), encoding="utf-8")

        zip_path = job_dir / "complete_page.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in package_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(package_dir.parent))

        metadata = self._metadata(request, user_data_dir)
        metadata["resource_count"] = len({value for value in resource_map.values()})
        return WorkerResult(
            success=True,
            output=request.output,
            artifacts=[
                WorkerArtifact(name="complete_page", content_type="application/zip", path=str(zip_path)),
                WorkerArtifact(name="complete_page_index", content_type="text/html; charset=utf-8", path=str(index_path)),
            ],
            metadata=metadata,
        )

    def _export_semantic_dom(self, page: Any, request: RenderRequest, job_dir: Path) -> WorkerResult:
        items = page.evaluate(SEMANTIC_DOM_SCRIPT)
        if not isinstance(items, list):
            items = []

        if request.output == OutputType.SEMANTIC_MARKDOWN:
            path = job_dir / "semantic.md"
            content = semantic_items_to_markdown(items)
            content_type = "text/markdown; charset=utf-8"
            artifact_name = "semantic_markdown"
        else:
            path = job_dir / "semantic.jsonl"
            content = semantic_items_to_jsonl(items)
            content_type = "application/x-ndjson; charset=utf-8"
            artifact_name = "semantic_jsonl"

        path.write_text(content, encoding="utf-8")
        metadata = self._metadata(request, None)
        metadata["semantic_node_count"] = len(items)
        return WorkerResult(
            success=True,
            output=request.output,
            artifacts=[WorkerArtifact(name=artifact_name, content_type=content_type, path=str(path))],
            metadata=metadata,
        )

    def _export_accessibility_yaml(self, page: Any, request: RenderRequest, job_dir: Path) -> WorkerResult:
        snapshot = page.locator("body").aria_snapshot(timeout=request.timeout_ms)
        path = job_dir / "accessibility.yaml"
        path.write_text(snapshot, encoding="utf-8")
        return WorkerResult(
            success=True,
            output=request.output,
            artifacts=[WorkerArtifact(name="accessibility_yaml", content_type="application/yaml; charset=utf-8", path=str(path))],
            metadata=self._metadata(request, None),
        )

    @staticmethod
    def _content_type(response: Any) -> str:
        content_type = response.headers.get("content-type", "")
        return content_type.split(";", 1)[0].strip().lower()

    @staticmethod
    def _extension_for(url: str, content_type: str) -> str:
        suffix = Path(urlparse(url).path).suffix
        if suffix and len(suffix) <= 8:
            return suffix
        guessed = mimetypes.guess_extension(content_type)
        if guessed:
            return guessed
        return {
            "application/javascript": ".js",
            "text/javascript": ".js",
            "image/svg+xml": ".svg",
        }.get(content_type, ".bin")

    @staticmethod
    def _rewrite_resource_references(html: str, base_url: str, resource_map: dict[str, str]) -> str:
        pattern = re.compile(r"(?P<prefix>\b(?:src|href)\s*=\s*[\"'])(?P<url>[^\"']+)(?P<suffix>[\"'])", re.I)

        def replace(match: re.Match[str]) -> str:
            raw_url = match.group("url")
            if raw_url.startswith(("data:", "javascript:", "mailto:", "#")):
                return match.group(0)
            absolute = urljoin(base_url, raw_url)
            local = resource_map.get(absolute)
            if not local:
                parsed = urlparse(absolute)
                local = resource_map.get(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
            if not local:
                return match.group(0)
            return f"{match.group('prefix')}{local}{match.group('suffix')}"

        return pattern.sub(replace, html)

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _metadata(request: RenderRequest, user_data_dir: Path | None) -> dict[str, Any]:
        return {
            "url": str(request.url),
            "engine": "edge",
            "headless": True,
            "wait_until": request.wait.until,
            "viewport": request.viewport.model_dump(mode="json"),
            "proxy_isolated": request.proxy is not None,
            "ignore_https_errors": request.ignore_https_errors,
            "user_data_dir": str(user_data_dir) if user_data_dir else None,
            "captured_at_unix": int(time.time()),
        }
