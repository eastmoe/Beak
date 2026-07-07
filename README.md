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

去 [Releases](https://github.com/eastmoe/Beak/releases) 页面下载对应 Python 版本（3.11 / 3.12 / 3.13 / 3.14）的 wheel，里面已经带了编译好的 WebView2 worker，不用装 .NET SDK：

```powershell
pip install beak-0.0.1-cp311-cp311-win_amd64.whl
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
dotnet publish .\workers\Beak.WebView2Worker -c Release -r win-x64 --self-contained false
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

## 快速试一下

```powershell
Invoke-RestMethod http://127.0.0.1:8000/render `
  -Method Post `
  -ContentType "application/json" `
  -Body '{
    "url": "https://example.com",
    "engine": "webview",
    "output": "rendered_html",
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

更多参数（代理、UA、viewport、等待策略），请直接翻API的 `/docs`。

## 项目结构

```
Beak/
├── src/
│   └── beak/                   # Python 主体包
│       ├── main.py             # FastAPI 应用和 /render /capture /jobs 路由
│       ├── cli.py              # `beak` 命令行入口
│       ├── schemas.py          # 请求、响应、job、artifact 等 Pydantic 模型
│       ├── jobs.py             # 同步 / 异步任务调度和运行时状态管理
│       ├── browser.py          # webview / edge 引擎路由
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

（上面只列源码、测试、构建配置和本地运行时目录；`.venv/`、`.pytest_cache/`、`test_outputs/`、`bin/`、`obj/` 等生成物不属于项目源码结构。）

## 许可证

Apache
