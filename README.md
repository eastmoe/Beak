# Beak

Windows 原生的无头浏览器爬取服务，不依赖 Docker，也不指望你在类 UNIX 环境下跑。底层是 Python + FastAPI，页面加载和导出走的是 WebView2 或 Playwright 驱动的 Edge，标准 HTTP API 调用即可。

## 这是什么

市面上大部分无头浏览器爬虫方案默认你在 Linux / Docker 里跑 Chromium。Beak 反过来，直接在 Windows 上用系统自带或已安装的 Microsoft Edge 干活，两种引擎二选一：

- **webview**（默认）：跑在独立的 WebView2 worker 进程里，每个任务一个进程，代理配置互不干扰。
- **edge**：Playwright 控制的无头 Edge（`channel=msedge`），系统装了 Edge 就能用，不用额外拉浏览器。

支持的产出：

- `rendered_html` —— JS 渲染完的 DOM
- `screenshot` —— PNG/JPEG 截图
- `single_file` —— DevTools `Page.captureSnapshot` 生成的 MHTML 单文件
- `complete_page` —— 渲染后的 `index.html` 加上抓到的资源文件，打包成 ZIP

小任务直接同步返回，导出型任务（比如 `complete_page`）走异步 job 流程，扔个 `job_id` 回来自己轮询。每个任务用独立的 worker 进程 / Edge profile 跑，代理不会在并发任务之间串。

## 环境要求

- Windows 10 / 11
- Python 3.11+
- Microsoft Edge WebView2 Runtime
- 用 `engine=edge` 的话还得装 Microsoft Edge

如果你要自己编译 worker，还需要：

- .NET 8 SDK

## 安装

### 直接下载预编译包

去 [Releases](https://github.com/eastmoe/Beak/releases) 页面下载 Windows wheel，里面已经带了编译好的 WebView2 worker，不用装 .NET SDK：

```powershell
pip install beak-0.0.2-py3-none-win_amd64.whl
```

### 自己编译

```powershell
git clone <repo-url> D:\Beak
cd D:\Beak
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

再编译 WebView2 worker：

```powershell
dotnet publish .\workers\Beak.WebView2Worker -c Release -r win-x64 --self-contained true
```

worker 发布到别的位置了，告诉 Beak 去哪找：

```powershell
$env:BEAK_WEBVIEW2_WORKER = "D:\path\to\Beak.WebView2Worker.exe"
```

## 运行

```powershell
beak server
```

默认只监听本地。想自定义端口 / 对外暴露：

```powershell
beak server --host 127.0.0.1 --port 8000   # 指定地址
beak server --host all --port 8000         # 监听所有 IPv4
beak server --host all-v6 --port 8000      # 监听所有 IPv6
beak server --host ::1 --port 8000         # 仅 IPv6 本地回环
```

启动后 API 文档在：

```
http://127.0.0.1:8000/docs
```

简易 WebUI 在：

```
http://127.0.0.1:8000/
```

## 快速试一下

```powershell
Invoke-RestMethod http://127.0.0.1:8000/render `
  -Method Post `
  -ContentType "application/json" `
  -Body '{
    "url": "https://example.com",
    "engine": "webview",
    "output": "rendered_html",
    "timeout_ms": 30000,
    "ignore_https_errors": false,
    "wait": { "until": "network_idle", "after_load_ms": 500, "network_idle_ms": 800 }
  }'
```

异步导出：

```powershell
$job = Invoke-RestMethod http://127.0.0.1:8000/render `
  -Method Post -ContentType "application/json" `
  -Body '{"url":"https://example.com","output":"complete_page"}'

Invoke-RestMethod "http://127.0.0.1:8000/jobs/$($job.job_id)"
```

也可以显式添加异步任务、查看队列、终止任务：

```powershell
$job = Invoke-RestMethod http://127.0.0.1:8000/jobs `
  -Method Post -ContentType "application/json" `
  -Body '{"url":"https://example.com","output":"screenshot"}'

Invoke-RestMethod "http://127.0.0.1:8000/jobs?active_only=true"
Invoke-RestMethod "http://127.0.0.1:8000/jobs/$($job.job_id)/cancel" -Method Post
```

每个请求还可以单独控制超时、跳过证书错误，或者把 artifact 保存到指定服务端目录：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/render `
  -Method Post -ContentType "application/json" `
  -Body '{
    "url": "https://self-signed.badssl.com/",
    "engine": "edge",
    "output": "screenshot",
    "timeout_ms": 60000,
    "ignore_https_errors": true,
    "output_dir": "captures/self-signed"
  }'
```

`output_dir` 是服务端路径；相对路径会放到 `BEAK_DATA_DIR` 下面，绝对路径则按原样使用。

## WebUI

根路径 `/` 提供一个轻量操作面板：

- 添加异步任务。
- 开关实时刷新，或手动非实时刷新。
- 查询任务队列和任务状态。
- 终止排队或运行中的任务。
- 打开任务产物。
- 查询、删除、清空历史记录。

## Python SDK

安装包后可以直接使用同步客户端：

```python
from beak import BeakClient

client = BeakClient("http://127.0.0.1:8000")

job = client.add_job(
    url="https://example.com",
    output="screenshot",
    timeout_ms=30000,
)
print(client.get_job(job["job_id"]))

client.download_artifact(job["job_id"], "screenshot", "screenshot.png")
```

常用方法包括 `render`、`capture`、`add_job`、`list_jobs`、`get_job`、`cancel_job`、`list_history`、`delete_history`、`clear_history` 和 `download_artifact`。

## 历史记录

任务历史会写入 `BEAK_DATA_DIR/history.json`。API 支持：

- `GET /history?limit=200`
- `DELETE /history/{job_id}`
- `DELETE /history`

## MCP

Beak 同时暴露 `/mcp`，可以作为 Streamable HTTP MCP server 供支持 MCP 的 LLM 客户端调用：

- `GET /mcp`：查看 Beak 的 MCP 声明和工具 schema。
- `POST /mcp`：发送 MCP JSON-RPC 请求，支持 `initialize`、`tools/list`、`tools/call`。

当前暴露两个工具：

- `beak_render`：参数与 `/render` 请求体一致。
- `beak_get_job`：按 `job_id` 查询异步任务状态。

更多参数（代理、UA、viewport、等待策略），请直接翻API的 `/docs`。

## 项目结构

```
Beak/
├── src/
│   └── beak/                   # Python 主体包
│       ├── main.py             # FastAPI 应用和 /render /capture /jobs 路由
│       ├── client.py           # Python client SDK
│       ├── history.py          # 简易历史记录持久化
│       ├── cli.py              # `beak` 命令行入口
│       ├── schemas.py          # 请求、响应、job、artifact 等 Pydantic 模型
│       ├── jobs.py             # 同步 / 异步任务调度和运行时状态管理
│       ├── browser.py          # webview / edge 引擎路由
│       ├── webui/              # 内置简易 WebUI
│       ├── worker.py           # WebView2 worker 进程调用封装
│       └── playwright_edge.py  # Playwright 驱动的 Microsoft Edge 后端
├── workers/
│   └── Beak.WebView2Worker/    # C# WebView2 worker 项目，dotnet publish 产出独立进程
│       ├── Program.cs
│       └── Beak.WebView2Worker.csproj
├── tests/
│   └── test_schemas.py         # Python 侧 schema 测试
├── .github/
│   └── workflows/
│       └── release.yml         # 手动触发的发布流水线
├── data/                       # 本地运行时 job / profile 数据，已被 .gitignore 忽略
├── requirements.txt
├── pyproject.toml
└── README.md
```

## To do

- 基于域名匹配策略的配置文件
- 体积/时间策略的自动清理
- URL 安全策略
- 浏览器可见语义DOM提取转Markdown或JSONL
- 基于Playwright ARIA snapshot的accessbility tree导出为YAML




## 许可证

Apache
