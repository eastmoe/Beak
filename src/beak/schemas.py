from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator


class OutputType(StrEnum):
    RENDERED_HTML = "rendered_html"
    SCREENSHOT = "screenshot"
    COMPLETE_PAGE = "complete_page"
    SINGLE_FILE = "single_file"


class BrowserEngine(StrEnum):
    WEBVIEW = "webview"
    EDGE = "edge"


class WaitUntil(StrEnum):
    LOAD = "load"
    DOM_CONTENT_LOADED = "dom_content_loaded"
    NETWORK_IDLE = "network_idle"
    FIXED_DELAY = "fixed_delay"


class ScreenshotFormat(StrEnum):
    PNG = "png"
    JPEG = "jpeg"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Viewport(BaseModel):
    width: Annotated[int, Field(ge=320, le=7680, description="Viewport width in CSS pixels.")] = 1365
    height: Annotated[int, Field(ge=240, le=4320, description="Viewport height in CSS pixels.")] = 768
    device_scale_factor: Annotated[
        float,
        Field(ge=0.25, le=4.0, description="Device scale factor used by Playwright Edge and reserved for WebView2 DPI handling."),
    ] = 1.0


class ProxyConfig(BaseModel):
    server: Annotated[
        str,
        Field(
            examples=["http://127.0.0.1:7890", "socks5://127.0.0.1:1080"],
            description="Proxy server passed to WebView2 with --proxy-server or to Playwright Edge launch options.",
        ),
    ]
    bypass_list: Annotated[
        str | None,
        Field(
            default=None,
            examples=["<-loopback>;*.local"],
            description="Optional Chromium proxy bypass list.",
        ),
    ] = None


class CookieSpec(BaseModel):
    name: Annotated[str, Field(min_length=1, examples=["sessionid"])]
    value: Annotated[str, Field(examples=["abc123"])]
    domain: Annotated[
        str | None,
        Field(default=None, description="Cookie domain. Defaults to the target URL host."),
    ] = None
    path: Annotated[str, Field(default="/", description="Cookie path.")]
    secure: bool = False
    http_only: bool = False
    expires_unix: Annotated[
        int | None,
        Field(default=None, description="Unix timestamp in seconds. Omit for a session cookie."),
    ] = None


class WaitStrategy(BaseModel):
    until: WaitUntil = Field(
        default=WaitUntil.LOAD,
        description="Page readiness condition used before exporting the result.",
    )
    after_load_ms: Annotated[
        int,
        Field(ge=0, le=120_000, description="Extra quiet time after the selected readiness condition."),
    ] = 500
    network_idle_ms: Annotated[
        int,
        Field(ge=100, le=30_000, description="Idle window used when until=network_idle."),
    ] = 800
    fixed_delay_ms: Annotated[
        int,
        Field(ge=0, le=300_000, description="Delay used when until=fixed_delay."),
    ] = 0


class RenderRequest(BaseModel):
    url: Annotated[HttpUrl, Field(description="Target page URL.")]
    engine: BrowserEngine = Field(
        default=BrowserEngine.WEBVIEW,
        description="Browser backend. `webview` uses the Windows WebView2 worker; `edge` uses headless Playwright with Microsoft Edge.",
    )
    output: OutputType = Field(
        default=OutputType.RENDERED_HTML,
        description="Export format produced after the selected browser engine finishes rendering.",
    )
    timeout_ms: Annotated[
        int,
        Field(ge=1_000, le=600_000, description="End-to-end browser task timeout."),
    ] = 30_000
    ignore_https_errors: bool = Field(
        default=False,
        description="When true, skip TLS/SSL certificate validation errors for this request.",
    )
    output_dir: Annotated[
        Path | None,
        Field(
            default=None,
            description=(
                "Optional server-side directory where artifacts for this request are saved. "
                "Relative paths are resolved under BEAK_DATA_DIR."
            ),
        ),
    ] = None
    wait: WaitStrategy = Field(default_factory=WaitStrategy)
    proxy: ProxyConfig | None = None
    cookies: list[CookieSpec] = Field(default_factory=list)
    user_agent: Annotated[
        str | None,
        Field(default=None, description="Optional User-Agent override for this isolated task."),
    ] = None
    viewport: Viewport = Field(default_factory=Viewport)
    screenshot_format: ScreenshotFormat = ScreenshotFormat.PNG
    jpeg_quality: Annotated[
        int,
        Field(ge=1, le=100, description="JPEG quality when screenshot_format=jpeg."),
    ] = 90
    async_mode: bool | None = Field(
        default=None,
        description=(
            "When true, return a job_id immediately. When omitted, large export types "
            "complete_page and single_file are queued asynchronously; small exports run synchronously."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "url": "https://example.com",
                    "engine": "webview",
                    "timeout_ms": 30000,
                    "ignore_https_errors": False,
                    "wait": {"until": "network_idle", "after_load_ms": 500, "network_idle_ms": 800},
                    "proxy": {"server": "http://127.0.0.1:7890"},
                    "cookies": [{"name": "sessionid", "value": "abc123", "domain": "example.com"}],
                    "user_agent": "Mozilla/5.0 Beak/0.1",
                    "viewport": {"width": 1365, "height": 768, "device_scale_factor": 1},
                    "output": "screenshot",
                    "screenshot_format": "png",
                }
            ]
        }
    }

    @field_validator("user_agent")
    @classmethod
    def strip_user_agent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("output_dir", mode="before")
    @classmethod
    def normalize_output_dir(cls, value: object) -> Path | None:
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        return Path(value)


class Artifact(BaseModel):
    name: str = Field(description="Stable artifact name inside the job.")
    content_type: str
    path: str = Field(description="Server-side artifact path.")
    download_url: str | None = Field(default=None, description="HTTP download URL when available.")
    size_bytes: int | None = None


class RenderResult(BaseModel):
    job_id: str
    output: OutputType
    html: str | None = Field(default=None, description="Rendered DOM HTML for rendered_html output.")
    content_type: str | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobAccepted(BaseModel):
    job_id: str
    status: Literal[JobStatus.QUEUED] = JobStatus.QUEUED
    status_url: str


class JobInfo(BaseModel):
    job_id: str
    status: JobStatus
    output: OutputType
    created_at: str
    updated_at: str
    error: str | None = None
    result: RenderResult | None = None


class WorkerArtifact(BaseModel):
    name: str
    content_type: str
    path: str


class WorkerResult(BaseModel):
    success: bool
    output: OutputType
    html_path: str | None = None
    artifacts: list[WorkerArtifact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class HealthResponse(BaseModel):
    service: str = "beak"
    ok: bool
    worker_configured: bool
    worker_path: str
    webview_worker_configured: bool
    webview_worker_path: str
    edge_playwright_configured: bool
    edge_executable_path: str | None = None
