# 一键启用本地模型（Ollama）
#
# 用法（在项目根目录）：
#   powershell -ExecutionPolicy Bypass -File scripts\setup_local_model.ps1
# 小显存或想快一点：
#   powershell -ExecutionPolicy Bypass -File scripts\setup_local_model.ps1 -Model qwen2.5:3b-instruct-q4_K_M

param(
    [string]$Model = "qwen2.5:7b-instruct-q4_K_M"
)

$ErrorActionPreference = "Stop"

function Step($n, $msg) {
    Write-Host ""
    Write-Host "[$n] $msg" -ForegroundColor Cyan
}

Step 1 "检查 Ollama"
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    Write-Host "未安装，开始通过 winget 安装（安装包约 1.3GB，取决于网速）..." -ForegroundColor Yellow
    winget install --id Ollama.Ollama -e --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) { throw "Ollama 安装后仍未找到命令，请重开一个终端再试" }
}
Write-Host "已找到：$($ollama.Source)" -ForegroundColor Green

Step 2 "启动服务并拉取模型 $Model"
try {
    Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null
    Write-Host "服务已在运行" -ForegroundColor Green
} catch {
    Write-Host "服务未启动，尝试拉起 ollama serve ..." -ForegroundColor Yellow
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 5
}
ollama pull $Model
if ($LASTEXITCODE -ne 0) {
    throw "模型拉取失败：检查网络，或换小模型 -Model qwen2.5:3b-instruct-q4_K_M"
}

Step 3 "常驻内存，避免首次调用冷启动 10 秒"
[Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "-1", "User")
Write-Host "已设置 OLLAMA_KEEP_ALIVE=-1（新开终端后生效）" -ForegroundColor Green

Step 4 "自检"
$tags = Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 5
Write-Host "已安装模型："
foreach ($m in $tags.models) {
    Write-Host ("  - " + $m.name + "  (" + [math]::Round($m.size / 1GB, 1) + " GB)")
}

Write-Host ""
Write-Host "下一步：" -ForegroundColor White
Write-Host "  1. 打开 http://127.0.0.1:5100/admin/ai/settings"
Write-Host "  2. 「AI 通道」选「本地模型（Ollama，数据不出店）」"
Write-Host "  3. 「本地模型名」填：$Model"
Write-Host "  4. 点「测试本地模型」，看到延迟就说明通了"
Write-Host ""
Write-Host "说明：本项目的本地请求会绕过系统代理直连 127.0.0.1，" -ForegroundColor DarkGray
Write-Host "      所以开着全局代理也不影响；云端调用仍走代理。" -ForegroundColor DarkGray
