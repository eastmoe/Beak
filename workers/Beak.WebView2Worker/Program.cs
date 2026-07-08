using System.Collections.Concurrent;
using System.Diagnostics;
using System.Drawing;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Beak.WebView2Worker;

internal static partial class Program
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = false,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) },
    };

    [STAThread]
    private static int Main(string[] args)
    {
        WorkerResult result;
        try
        {
            string requestPath = ParseRequestPath(args);
            string json = File.ReadAllText(requestPath, Encoding.UTF8);
            WorkerInput input = JsonSerializer.Deserialize<WorkerInput>(json, JsonOptions)
                ?? throw new InvalidOperationException("Request JSON is empty.");

            ApplicationConfiguration.Initialize();
            using WorkerForm form = new(input, JsonOptions);
            Application.Run(form);
            result = form.Result ?? WorkerResult.Fail(input.Output, "Worker closed without producing a result.");
        }
        catch (Exception ex)
        {
            result = WorkerResult.Fail("rendered_html", ex.ToString());
        }

        Console.WriteLine(JsonSerializer.Serialize(result, JsonOptions));
        return result.Success ? 0 : 1;
    }

    private static string ParseRequestPath(string[] args)
    {
        for (int i = 0; i < args.Length - 1; i++)
        {
            if (args[i] == "--request")
            {
                return args[i + 1];
            }
        }

        throw new ArgumentException("Usage: Beak.WebView2Worker --request <worker-request.json>");
    }
}

internal sealed partial class WorkerForm : Form
{
    private readonly WorkerInput _input;
    private readonly JsonSerializerOptions _jsonOptions;
    private readonly WebView2 _webView = new();
    private readonly NetworkTracker _networkTracker = new();

    public WorkerResult? Result { get; private set; }

    public WorkerForm(WorkerInput input, JsonSerializerOptions jsonOptions)
    {
        _input = input;
        _jsonOptions = jsonOptions;
        Width = Math.Max(320, input.Viewport.Width);
        Height = Math.Max(240, input.Viewport.Height);
        StartPosition = FormStartPosition.Manual;
        Location = new Point(32, 32);
        ShowInTaskbar = false;
        Opacity = 0.01;
        FormBorderStyle = FormBorderStyle.FixedToolWindow;

        _webView.Dock = DockStyle.Fill;
        Controls.Add(_webView);
    }

    protected override async void OnShown(EventArgs e)
    {
        base.OnShown(e);
        try
        {
            Result = await RunAsync();
        }
        catch (Exception ex)
        {
            Result = WorkerResult.Fail(_input.Output, ex.ToString());
        }
        finally
        {
            Close();
        }
    }

    private async Task<WorkerResult> RunAsync()
    {
        Directory.CreateDirectory(_input.OutputDir);
        Directory.CreateDirectory(_input.UserDataDir);

        using CancellationTokenSource timeout = new(_input.TimeoutMs);
        await InitializeWebViewAsync();
        await ApplyCookiesAsync();
        await NavigateAndWaitAsync(timeout.Token);

        string html = await GetRenderedHtmlAsync();
        return _input.Output switch
        {
            "rendered_html" => await ExportRenderedHtmlAsync(html),
            "screenshot" => await ExportScreenshotAsync(),
            "single_file" => await ExportSingleFileAsync(),
            "complete_page" => await ExportCompletePageAsync(html),
            _ => throw new NotSupportedException($"Unsupported output type: {_input.Output}"),
        };
    }

    private async Task InitializeWebViewAsync()
    {
        List<string> args = ["--no-first-run", "--disable-features=msSmartScreenProtection"];
        if (_input.IgnoreHttpsErrors)
        {
            args.Add("--ignore-certificate-errors");
            args.Add("--allow-insecure-localhost");
        }
        if (_input.Proxy is not null)
        {
            args.Add($"--proxy-server={_input.Proxy.Server}");
            if (!string.IsNullOrWhiteSpace(_input.Proxy.BypassList))
            {
                args.Add($"--proxy-bypass-list={_input.Proxy.BypassList}");
            }
        }

        CoreWebView2EnvironmentOptions options = new(string.Join(" ", args));
        CoreWebView2Environment env = await CoreWebView2Environment.CreateAsync(
            browserExecutableFolder: null,
            userDataFolder: _input.UserDataDir,
            options: options);
        await _webView.EnsureCoreWebView2Async(env);

        _webView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
        _webView.CoreWebView2.Settings.AreDevToolsEnabled = false;
        if (_input.IgnoreHttpsErrors)
        {
            _webView.CoreWebView2.ServerCertificateErrorDetected += (_, e) =>
            {
                e.Action = CoreWebView2ServerCertificateErrorAction.AlwaysAllow;
            };
        }
        if (!string.IsNullOrWhiteSpace(_input.UserAgent))
        {
            _webView.CoreWebView2.Settings.UserAgent = _input.UserAgent;
        }

        await _networkTracker.AttachAsync(_webView.CoreWebView2);
    }

    private async Task ApplyCookiesAsync()
    {
        Uri target = new(_input.Url);
        foreach (CookieInput cookie in _input.Cookies)
        {
            string domain = string.IsNullOrWhiteSpace(cookie.Domain) ? target.Host : cookie.Domain!;
            CoreWebView2Cookie webCookie = _webView.CoreWebView2.CookieManager.CreateCookie(
                cookie.Name,
                cookie.Value,
                domain,
                string.IsNullOrWhiteSpace(cookie.Path) ? "/" : cookie.Path);
            webCookie.IsSecure = cookie.Secure;
            webCookie.IsHttpOnly = cookie.HttpOnly;
            if (cookie.ExpiresUnix is not null)
            {
                webCookie.Expires = DateTimeOffset.FromUnixTimeSeconds(cookie.ExpiresUnix.Value).DateTime;
            }

            _webView.CoreWebView2.CookieManager.AddOrUpdateCookie(webCookie);
        }

        await Task.CompletedTask;
    }

    private async Task NavigateAndWaitAsync(CancellationToken cancellationToken)
    {
        TaskCompletionSource navigation = new(TaskCreationOptions.RunContinuationsAsynchronously);
        TaskCompletionSource domContentLoaded = new(TaskCreationOptions.RunContinuationsAsynchronously);

        void NavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            if (e.IsSuccess)
            {
                navigation.TrySetResult();
            }
            else
            {
                navigation.TrySetException(new InvalidOperationException($"Navigation failed: {e.WebErrorStatus}"));
            }
        }

        void DomContentLoaded(object? sender, CoreWebView2DOMContentLoadedEventArgs e) => domContentLoaded.TrySetResult();

        _webView.CoreWebView2.NavigationCompleted += NavigationCompleted;
        _webView.CoreWebView2.DOMContentLoaded += DomContentLoaded;
        try
        {
            _webView.CoreWebView2.Navigate(_input.Url);
            Task selected = _input.WaitUntil switch
            {
                "dom_content_loaded" => domContentLoaded.Task,
                _ => navigation.Task,
            };
            await selected.WaitAsync(cancellationToken);

            if (_input.WaitUntil == "network_idle")
            {
                await _networkTracker.WaitForIdleAsync(_input.NetworkIdleMs, cancellationToken);
            }
            else if (_input.WaitUntil == "fixed_delay" && _input.FixedDelayMs > 0)
            {
                await Task.Delay(_input.FixedDelayMs, cancellationToken);
            }

            if (_input.AfterLoadMs > 0)
            {
                await Task.Delay(_input.AfterLoadMs, cancellationToken);
            }
        }
        finally
        {
            _webView.CoreWebView2.NavigationCompleted -= NavigationCompleted;
            _webView.CoreWebView2.DOMContentLoaded -= DomContentLoaded;
        }
    }

    private async Task<string> GetRenderedHtmlAsync()
    {
        string json = await _webView.CoreWebView2.ExecuteScriptAsync("document.documentElement.outerHTML");
        return JsonSerializer.Deserialize<string>(json) ?? string.Empty;
    }

    private async Task<WorkerResult> ExportRenderedHtmlAsync(string html)
    {
        string path = Path.Combine(_input.OutputDir, "rendered.html");
        await File.WriteAllTextAsync(path, html, Encoding.UTF8);
        return WorkerResult.Ok(
            _input.Output,
            htmlPath: path,
            artifacts: [new WorkerArtifact("rendered_html", "text/html; charset=utf-8", path)],
            metadata: BaseMetadata());
    }

    private async Task<WorkerResult> ExportScreenshotAsync()
    {
        string extension = _input.ScreenshotFormat == "jpeg" ? "jpg" : "png";
        string contentType = _input.ScreenshotFormat == "jpeg" ? "image/jpeg" : "image/png";
        string path = Path.Combine(_input.OutputDir, $"screenshot.{extension}");
        CoreWebView2CapturePreviewImageFormat format = _input.ScreenshotFormat == "jpeg"
            ? CoreWebView2CapturePreviewImageFormat.Jpeg
            : CoreWebView2CapturePreviewImageFormat.Png;

        await using FileStream stream = File.Create(path);
        await _webView.CoreWebView2.CapturePreviewAsync(format, stream);

        Dictionary<string, object?> metadata = BaseMetadata();
        metadata["jpeg_quality_requested"] = _input.JpegQuality;
        return WorkerResult.Ok(
            _input.Output,
            htmlPath: null,
            artifacts: [new WorkerArtifact("screenshot", contentType, path)],
            metadata: metadata);
    }

    private async Task<WorkerResult> ExportSingleFileAsync()
    {
        await _webView.CoreWebView2.CallDevToolsProtocolMethodAsync("Page.enable", "{}");
        string response = await _webView.CoreWebView2.CallDevToolsProtocolMethodAsync(
            "Page.captureSnapshot",
            "{\"format\":\"mhtml\"}");
        using JsonDocument doc = JsonDocument.Parse(response);
        string mhtml = doc.RootElement.GetProperty("data").GetString() ?? string.Empty;
        string path = Path.Combine(_input.OutputDir, "page.mhtml");
        await File.WriteAllTextAsync(path, mhtml, Encoding.UTF8);

        return WorkerResult.Ok(
            _input.Output,
            htmlPath: null,
            artifacts: [new WorkerArtifact("single_file", "multipart/related", path)],
            metadata: BaseMetadata());
    }

    private async Task<WorkerResult> ExportCompletePageAsync(string html)
    {
        string packageDir = Path.Combine(_input.OutputDir, "complete_page");
        string resourcesDir = Path.Combine(packageDir, "resources");
        Directory.CreateDirectory(resourcesDir);

        Dictionary<string, string> resourceMap = new(StringComparer.OrdinalIgnoreCase);
        foreach (NetworkResponse response in _networkTracker.Responses)
        {
            if (!Uri.TryCreate(response.Url, UriKind.Absolute, out Uri? resourceUri))
            {
                continue;
            }

            byte[]? body = await _networkTracker.TryGetBodyAsync(response.RequestId);
            if (body is null || body.Length == 0)
            {
                continue;
            }

            string extension = FileExtensionFor(resourceUri, response.MimeType);
            string fileName = $"{Hash(response.Url)}{extension}";
            string absolutePath = Path.Combine(resourcesDir, fileName);
            await File.WriteAllBytesAsync(absolutePath, body);
            resourceMap[resourceUri.AbsoluteUri] = $"resources/{fileName}";
        }

        string rewrittenHtml = RewriteResourceReferences(html, _input.Url, resourceMap);
        string indexPath = Path.Combine(packageDir, "index.html");
        await File.WriteAllTextAsync(indexPath, rewrittenHtml, Encoding.UTF8);

        string zipPath = Path.Combine(_input.OutputDir, "complete_page.zip");
        if (File.Exists(zipPath))
        {
            File.Delete(zipPath);
        }

        ZipFile.CreateFromDirectory(packageDir, zipPath, CompressionLevel.Optimal, includeBaseDirectory: true);

        Dictionary<string, object?> metadata = BaseMetadata();
        metadata["resource_count"] = resourceMap.Count;
        return WorkerResult.Ok(
            _input.Output,
            htmlPath: null,
            artifacts:
            [
                new WorkerArtifact("complete_page", "application/zip", zipPath),
                new WorkerArtifact("complete_page_index", "text/html; charset=utf-8", indexPath),
            ],
            metadata: metadata);
    }

    private Dictionary<string, object?> BaseMetadata() => new()
    {
        ["url"] = _input.Url,
        ["wait_until"] = _input.WaitUntil,
        ["viewport"] = new Dictionary<string, object?>
        {
            ["width"] = _input.Viewport.Width,
            ["height"] = _input.Viewport.Height,
            ["device_scale_factor"] = _input.Viewport.DeviceScaleFactor,
        },
        ["proxy_isolated"] = _input.Proxy is not null,
        ["ignore_https_errors"] = _input.IgnoreHttpsErrors,
        ["user_data_dir"] = _input.UserDataDir,
    };

    private static string RewriteResourceReferences(string html, string baseUrl, Dictionary<string, string> resourceMap)
    {
        Uri baseUri = new(baseUrl);
        return ResourceAttributeRegex().Replace(html, match =>
        {
            string prefix = match.Groups["prefix"].Value;
            string rawUrl = match.Groups["url"].Value;
            string suffix = match.Groups["suffix"].Value;

            if (rawUrl.StartsWith("data:", StringComparison.OrdinalIgnoreCase)
                || rawUrl.StartsWith("javascript:", StringComparison.OrdinalIgnoreCase)
                || rawUrl.StartsWith("mailto:", StringComparison.OrdinalIgnoreCase)
                || rawUrl.StartsWith("#", StringComparison.Ordinal))
            {
                return match.Value;
            }

            if (!Uri.TryCreate(baseUri, rawUrl, out Uri? absolute))
            {
                return match.Value;
            }

            string lookup = absolute.GetLeftPart(UriPartial.Path);
            if (!resourceMap.TryGetValue(absolute.AbsoluteUri, out string? local)
                && !resourceMap.TryGetValue(lookup, out local))
            {
                return match.Value;
            }

            return $"{prefix}{local}{suffix}";
        });
    }

    private static string Hash(string value)
    {
        byte[] bytes = SHA256.HashData(Encoding.UTF8.GetBytes(value));
        return Convert.ToHexString(bytes).ToLowerInvariant()[..24];
    }

    private static string FileExtensionFor(Uri uri, string? mimeType)
    {
        string extension = Path.GetExtension(uri.AbsolutePath);
        if (!string.IsNullOrWhiteSpace(extension) && extension.Length <= 8)
        {
            return extension;
        }

        return mimeType?.ToLowerInvariant() switch
        {
            "text/css" => ".css",
            "application/javascript" or "text/javascript" => ".js",
            "image/png" => ".png",
            "image/jpeg" => ".jpg",
            "image/gif" => ".gif",
            "image/webp" => ".webp",
            "image/svg+xml" => ".svg",
            "font/woff" => ".woff",
            "font/woff2" => ".woff2",
            _ => ".bin",
        };
    }

    [GeneratedRegex("(?<prefix>\\b(?:src|href)\\s*=\\s*[\"'])(?<url>[^\"']+)(?<suffix>[\"'])", RegexOptions.IgnoreCase)]
    private static partial Regex ResourceAttributeRegex();
}

internal sealed class NetworkTracker
{
    private readonly ConcurrentDictionary<string, NetworkResponse> _responses = new();
    private CoreWebView2? _core;
    private int _inFlight;
    private long _lastActivityTicks = Stopwatch.GetTimestamp();

    public IReadOnlyCollection<NetworkResponse> Responses => _responses.Values.ToArray();

    public async Task AttachAsync(CoreWebView2 core)
    {
        _core = core;
        await core.CallDevToolsProtocolMethodAsync("Network.enable", "{}");

        CoreWebView2DevToolsProtocolEventReceiver requestReceiver =
            core.GetDevToolsProtocolEventReceiver("Network.requestWillBeSent");
        requestReceiver.DevToolsProtocolEventReceived += (_, e) =>
        {
            Interlocked.Increment(ref _inFlight);
            MarkActivity();
        };

        CoreWebView2DevToolsProtocolEventReceiver responseReceiver =
            core.GetDevToolsProtocolEventReceiver("Network.responseReceived");
        responseReceiver.DevToolsProtocolEventReceived += (_, e) =>
        {
            try
            {
                using JsonDocument doc = JsonDocument.Parse(e.ParameterObjectAsJson);
                JsonElement root = doc.RootElement;
                string requestId = root.GetProperty("requestId").GetString() ?? string.Empty;
                JsonElement response = root.GetProperty("response");
                string url = response.GetProperty("url").GetString() ?? string.Empty;
                string mimeType = response.TryGetProperty("mimeType", out JsonElement mime)
                    ? mime.GetString() ?? string.Empty
                    : string.Empty;
                if (!string.IsNullOrWhiteSpace(requestId) && !string.IsNullOrWhiteSpace(url))
                {
                    _responses[requestId] = new NetworkResponse(requestId, url, mimeType);
                }
            }
            catch
            {
                // DevTools events are best effort for complete_page packaging.
            }
            finally
            {
                MarkActivity();
            }
        };

        CoreWebView2DevToolsProtocolEventReceiver finishedReceiver =
            core.GetDevToolsProtocolEventReceiver("Network.loadingFinished");
        finishedReceiver.DevToolsProtocolEventReceived += (_, _) => CompleteRequest();

        CoreWebView2DevToolsProtocolEventReceiver failedReceiver =
            core.GetDevToolsProtocolEventReceiver("Network.loadingFailed");
        failedReceiver.DevToolsProtocolEventReceived += (_, _) => CompleteRequest();
    }

    public async Task WaitForIdleAsync(int idleMs, CancellationToken cancellationToken)
    {
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            double elapsedIdleMs = Stopwatch.GetElapsedTime(Interlocked.Read(ref _lastActivityTicks)).TotalMilliseconds;
            if (Volatile.Read(ref _inFlight) <= 0 && elapsedIdleMs >= idleMs)
            {
                return;
            }

            await Task.Delay(100, cancellationToken);
        }
    }

    public async Task<byte[]?> TryGetBodyAsync(string requestId)
    {
        if (_core is null)
        {
            return null;
        }

        try
        {
            string payload = JsonSerializer.Serialize(new { requestId });
            string json = await _core.CallDevToolsProtocolMethodAsync("Network.getResponseBody", payload);
            using JsonDocument doc = JsonDocument.Parse(json);
            string body = doc.RootElement.GetProperty("body").GetString() ?? string.Empty;
            bool base64 = doc.RootElement.TryGetProperty("base64Encoded", out JsonElement base64Element)
                && base64Element.GetBoolean();
            return base64 ? Convert.FromBase64String(body) : Encoding.UTF8.GetBytes(body);
        }
        catch
        {
            return null;
        }
    }

    private void CompleteRequest()
    {
        int current;
        do
        {
            current = Volatile.Read(ref _inFlight);
            if (current <= 0)
            {
                break;
            }
        } while (Interlocked.CompareExchange(ref _inFlight, current - 1, current) != current);

        MarkActivity();
    }

    private void MarkActivity() => Interlocked.Exchange(ref _lastActivityTicks, Stopwatch.GetTimestamp());
}

internal sealed record WorkerInput(
    string JobId,
    string Url,
    int TimeoutMs,
    bool IgnoreHttpsErrors,
    string WaitUntil,
    int AfterLoadMs,
    int NetworkIdleMs,
    int FixedDelayMs,
    ProxyInput? Proxy,
    IReadOnlyList<CookieInput> Cookies,
    string? UserAgent,
    ViewportInput Viewport,
    string Output,
    string ScreenshotFormat,
    int JpegQuality,
    string UserDataDir,
    string OutputDir);

internal sealed record ProxyInput(string Server, string? BypassList);

internal sealed record CookieInput(
    string Name,
    string Value,
    string? Domain,
    string Path,
    bool Secure,
    bool HttpOnly,
    long? ExpiresUnix);

internal sealed record ViewportInput(int Width, int Height, double DeviceScaleFactor);

internal sealed record NetworkResponse(string RequestId, string Url, string? MimeType);

internal sealed record WorkerArtifact(string Name, string ContentType, string Path);

internal sealed record WorkerResult(
    bool Success,
    string Output,
    string? HtmlPath,
    IReadOnlyList<WorkerArtifact> Artifacts,
    Dictionary<string, object?> Metadata,
    string? Error)
{
    public static WorkerResult Ok(
        string output,
        string? htmlPath,
        IReadOnlyList<WorkerArtifact> artifacts,
        Dictionary<string, object?> metadata)
        => new(true, output, htmlPath, artifacts, metadata, null);

    public static WorkerResult Fail(string output, string error)
        => new(false, output, null, [], new Dictionary<string, object?>(), error);
}
