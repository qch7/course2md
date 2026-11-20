<#
.SYNOPSIS
    course2md 一键启动：自检环境 → 启动本地界面（前端页面与 API 由同一个进程提供）。

.EXAMPLE
    .\start.ps1
    .\start.ps1 -Port 8800 -NoOpen
    .\start.ps1 -Cpu            # 没显卡或显存被占用时强制走 CPU
    .\start.ps1 -Check          # 只做环境自检，不启动服务
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [string]$BindHost = "127.0.0.1",
    [switch]$NoOpen,
    [switch]$Cpu,
    [switch]$Check,
    [switch]$SkipCheck,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 控制台按 UTF-8 输出，避免中文注释/日志乱码
try {
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $env:PYTHONIOENCODING = "utf-8"
} catch { }

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Warn($text) { Write-Host "!   $text" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 选择解释器
# 优先使用项目内的 .venv；不存在时用 uv 创建（只在首次创建，之后不会自动同步，
# 以免移除你另外安装的解析器插件）。
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

function Resolve-Python {
    if ($Python) { return $Python }
    if ($env:COURSE2MD_PYTHON) { return $env:COURSE2MD_PYTHON }
    if (Test-Path $venvPython) { return $venvPython }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        $extras = @("--extra", "asr")
        if (-not $Cpu) { $extras += @("--extra", "cuda") }
        Write-Step "首次运行：uv sync $($extras -join ' ')"
        & $uv.Source sync @extras
        if ($LASTEXITCODE -ne 0) { throw "uv sync 失败，请检查网络或代理后重试。" }
        return $venvPython
    }
    foreach ($candidate in @("python", "py")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            Write-Warn "未安装 uv，使用系统 Python；请确认已执行 pip install -e "".[asr]"""
            return $cmd.Source
        }
    }
    throw "找不到 uv 或 Python。推荐安装 uv（https://docs.astral.sh/uv/），或用 -Python <路径> 指定解释器。"
}

$pythonExe = Resolve-Python
$version = & $pythonExe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
if (-not $version -or [version]$version -lt [version]"3.10") {
    throw "需要 Python 3.10+，当前：$version"
}
Write-Step "Python $version（$pythonExe）"

# ---------------------------------------------------------------- 运行参数
if ($Cpu) {
    $env:COURSE2MD_DEVICE = "cpu"
    $env:COURSE2MD_COMPUTE_TYPE = "int8"
    Write-Step "强制使用 CPU"
}

# ---------------------------------------------------------------- 环境自检
if (-not $SkipCheck) {
    Write-Step "环境自检"
    & $pythonExe -m course2md doctor
    if ($LASTEXITCODE -ne 0) {
        throw "自检未通过，请按上面的提示修复后重试（可加 -SkipCheck 跳过）。"
    }
    if ($Check) { return }
}
elseif ($Check) { return }

# ---------------------------------------------------------------- 端口
function Test-PortFree([int]$p) {
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $p)
        $listener.Start(); $listener.Stop(); return $true
    } catch { return $false }
}

if (-not (Test-PortFree $Port)) {
    $start = $Port
    while ($Port -lt $start + 20 -and -not (Test-PortFree $Port)) { $Port++ }
    if (-not (Test-PortFree $Port)) { throw "从 $start 开始的 20 个端口都被占用，请用 -Port 指定其它端口。" }
    Write-Warn "端口 $start 已被占用，改用 $Port"
}

# ---------------------------------------------------------------- 启动
$url = "http://${BindHost}:$Port"
Write-Step "启动界面 $url"
Write-Host "    前端页面与 API 由同一个进程提供；按 Ctrl+C 停止。" -ForegroundColor DarkGray
Write-Host ""

$webArgs = @("-m", "course2md", "web", "--host", $BindHost, "--port", "$Port")
if ($NoOpen) { $webArgs += "--no-open" }

try {
    & $pythonExe @webArgs
} finally {
    Write-Host ""
    Write-Host "服务已停止。" -ForegroundColor DarkGray
}
