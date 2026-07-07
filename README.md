# Beak

Beak is a Windows browser-rendering crawler service. The HTTP layer is Python + FastAPI, while page loading and export can run through either an isolated Microsoft Edge WebView2 worker process or headless Playwright-driven Microsoft Edge.

## Features

- `POST /render` and `POST /capture` APIs with FastAPI OpenAPI docs at `/docs`.
- Per-task browser engine, URL, timeout, wait strategy, proxy, cookies, User-Agent, viewport and output format.
- Browser engines:
  - `webview`: Windows WebView2 worker. This remains the default.
  - `edge`: headless Microsoft Edge controlled by Playwright.
- Output types:
  - `rendered_html`: JavaScript-rendered DOM HTML.
  - `screenshot`: PNG or JPEG screenshot through WebView2 `CapturePreviewAsync`.
  - `single_file`: MHTML generated with Chromium DevTools `Page.captureSnapshot`.
  - `complete_page`: ZIP package containing rendered `index.html` plus resources captured from WebView2 network responses when available.
- Sync response for small tasks and async job flow for large exports.
- Proxy isolation by launching each task in a separate WebView2 worker or Playwright persistent Edge profile.

## Requirements

- Windows 10/11.
- Microsoft Edge WebView2 Runtime.
- Microsoft Edge installed when using `engine=edge`.
- Python 3.11+.
- .NET 8 SDK to build the WebView2 worker.

## Install

```powershell
cd D:\Beak
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Playwright uses the system Microsoft Edge channel for `engine=edge`, so no bundled browser install is required as long as Edge is installed.

Publish the worker:

```powershell
dotnet publish .\workers\Beak.WebView2Worker -c Release -r win-x64 --self-contained false
```

If `dotnet` is installed but the current shell has not refreshed PATH, use the absolute command:

```powershell
& 'C:\Program Files\dotnet\dotnet.exe' publish .\workers\Beak.WebView2Worker -c Release -r win-x64 --self-contained false
```

If you publish the worker somewhere else, point Beak at it:

```powershell
$env:BEAK_WEBVIEW2_WORKER = "D:\path\to\Beak.WebView2Worker.exe"
```

## Run

```powershell
beak
```

or:

```powershell
uvicorn beak.main:app --host 127.0.0.1 --port 8000
```

Open API docs:

```text
http://127.0.0.1:8000/docs
```

## Example

Rendered HTML:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/render `
  -Method Post `
  -ContentType "application/json" `
  -Body '{
    "url": "https://example.com",
    "engine": "webview",
    "output": "rendered_html",
    "wait": { "until": "network_idle", "after_load_ms": 500, "network_idle_ms": 800 },
    "viewport": { "width": 1365, "height": 768 }
  }'
```

Screenshot through a proxy:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/capture `
  -Method Post `
  -ContentType "application/json" `
  -Body '{
    "url": "https://example.com",
    "engine": "edge",
    "output": "screenshot",
    "proxy": { "server": "http://127.0.0.1:7890" },
    "user_agent": "Mozilla/5.0 Beak/0.1",
    "screenshot_format": "png",
    "async_mode": false
  }'
```

Large exports default to async:

```powershell
$job = Invoke-RestMethod http://127.0.0.1:8000/render `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"url":"https://example.com","output":"complete_page"}'

Invoke-RestMethod "http://127.0.0.1:8000/jobs/$($job.job_id)"
```

Download an artifact:

```powershell
Invoke-WebRequest `
  "http://127.0.0.1:8000/jobs/$($job.job_id)/artifact/complete_page" `
  -OutFile complete_page.zip
```

## API behavior

`engine` controls the browser backend:

- `webview`: uses the published Windows WebView2 Worker.
- `edge`: uses Playwright with headless Microsoft Edge (`channel=msedge`).

`async_mode` controls response mode:

- `true`: always returns `202` with `job_id`.
- `false`: waits for completion and returns the result directly.
- omitted: `rendered_html` and `screenshot` run synchronously; `complete_page` and `single_file` run asynchronously.

Wait strategies:

- `load`: wait for WebView2 navigation completion.
- `dom_content_loaded`: wait for DOMContentLoaded.
- `network_idle`: wait for DOMContentLoaded/navigation completion plus a quiet network window.
- `fixed_delay`: wait for navigation completion and a fixed delay.

Proxy support is process/profile-level. Beak starts a separate WebView2 worker/user-data directory or Playwright persistent Edge profile per task so proxy configuration does not leak across concurrent jobs.

## Notes

WebView2 does not expose every Microsoft Edge "Save page as..." behavior as a simple stable API. Beak uses WebView2-native capture for WebView2 screenshots, Playwright screenshot capture for `engine=edge`, DevTools `Page.captureSnapshot` for `single_file` MHTML, and captured network bodies plus rendered DOM rewriting for `complete_page`. Some cross-origin or cache-only resources may not be available after page load; the ZIP still includes the rendered HTML and every captured response body available to the selected engine.
