# ============================================================
#  内网模型 TT Switch 一键接入脚本（DeepSeek Harness）
# ============================================================
#  运行前：对方已在本机启动 DeepSeek Harness Web（默认 127.0.0.1:3080）
#
#  用法（token 不内置，默认读环境变量 TT_TOKEN；或用 -ApiKey 显式传入）：
#    pwsh -File 分享-内网模型TT-Switch.ps1
#    pwsh -File 分享-内网模型TT-Switch.ps1 -Port 3080
#    pwsh -File 分享-内网模型TT-Switch.ps1 -ApiKey "自己的token"
#
#  脚本做什么（幂等，可重复运行）：
#    1) credentials.set   -> 写入 TT_SWITCH_API_KEY（只写存储，不进 settings.yaml）
#    2) settings.mutate   -> 在 llm-pi-ai.providers.tt-switch 注册内网网关（31 个模型）
#    3) 验证并打印结果
#  只新增/覆盖 tt-switch 这一条 provider，不影响对方其他任何配置。
# ============================================================
param(
    [int]$Port = 3080,
    # 令牌不写入脚本：默认读环境变量 TT_TOKEN，也可用 -ApiKey 显式传入
    [string]$ApiKey = $env:TT_TOKEN,
    [string]$BaseURL = "http://118.31.105.6:18443/tencent/v1"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    Write-Host "缺少 TT Switch token：请先设置环境变量 TT_TOKEN（或用 -ApiKey 传入）" -ForegroundColor Red
    exit 1
}
$base = "http://127.0.0.1:$Port/api"

function Invoke-DshRpc {
    param([string]$Method, $Payload)

    $body = @{
        type    = "client-request"
        rpcId   = [guid]::NewGuid().ToString("N")
        method  = $Method
        payload = $Payload
    } | ConvertTo-Json -Depth 40

    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)

    try {
        $resp = Invoke-WebRequest `
            -Uri "$base/$Method" `
            -Method Post `
            -ContentType "application/json" `
            -Body $bodyBytes `
            -TimeoutSec 120 `
            -UseBasicParsing
    } catch {
        Write-Host ""
        Write-Host " 无法连接 $base" -ForegroundColor Red
        Write-Host " 请确认 DeepSeek Harness Web 已在本机启动（默认 http://127.0.0.1:$Port），" -ForegroundColor Yellow
        Write-Host " 并用 -Port 指定正确端口后重试。" -ForegroundColor Yellow
        Write-Host " 详情：$($_.Exception.Message)" -ForegroundColor DarkGray
        exit 1
    }

    $env = $resp.Content | ConvertFrom-Json
    if (-not $env.result.ok) {
        $err = $env.result.error
        throw "RPC $Method 失败：$($err.code) $($err.message)"
    }
    return $env.result.value
}

Write-Host "== 内网模型 TT Switch 接入 =="
Write-Host "   harness: $base"
Write-Host "   baseURL: $BaseURL"
Write-Host ""

# ---------- 1) 写入凭据 ----------
Invoke-DshRpc "credentials.set" @{ ref = "TT_SWITCH_API_KEY"; value = $ApiKey } | Out-Null
Write-Host " [1/3] 凭据 TT_SWITCH_API_KEY 已写入（只写存储）" -ForegroundColor Green

# ---------- 2) 组装 provider ----------
$modelNames = @{
    "claude-sonnet-5"          = "Claude Sonnet 5（1.12x）"
    "claude-sonnet-5-1m"       = "Claude Sonnet 5 (1M)（1.12x）"
    "claude-sonnet-4.6"        = "Claude Sonnet 4.6（1.72x）"
    "claude-sonnet-4.6-1m"     = "Claude Sonnet 4.6 (1M)（1.72x）"
    "claude-opus-5"            = "Claude Opus 5（2.86x）"
    "claude-opus-4.8"          = "Claude Opus 4.8（2.86x）"
    "claude-opus-4.8-1m"       = "Claude Opus 4.8 (1M)（2.86x）"
    "claude-opus-4.7"          = "Claude Opus 4.7（2.86x）"
    "claude-opus-4.7-1m"       = "Claude Opus 4.7 (1M)（2.86x）"
    "claude-opus-4.6"          = "Claude Opus 4.6（2.86x）"
    "claude-opus-4.6-1m"       = "Claude Opus 4.6 (1M)（2.86x）"
    "gemini-3.1-pro"           = "Gemini 3.1 Pro（1.16x）"
    "gemini-3.5-flash"         = "Gemini 3.5 Flash（0.87x）"
    "gpt-5.6-sol"              = "GPT-5.6 Sol（2.9x）"
    "gpt-5.6-terra"            = "GPT-5.6 Terra（1.16x）"
    "gpt-5.6-luna"             = "GPT-5.6 Luna（0.12x）"
    "gpt-5.5"                  = "GPT-5.5（2.9x）"
    "gpt-5.4"                  = "GPT-5.4（1.45x）"
    "gpt-5.3-codex"            = "GPT-5.3 Codex（1.3x）"
    "glm-5.3-ioa"              = "GLM 5.3（0.49x）"
    "glm-5.2-ioa"              = "GLM 5.2（0.49x）"
    "glm-5.2-internal-ioa"     = "GLM 5.2 Internal（0.49x）"
    "glm-5v-turbo-ioa"         = "GLM-5V Turbo（0.36x）"
    "minimax-m3-ioa"           = "MiniMax M3（0.14x）"
    "minimax-m2.7-ioa"         = "MiniMax M2.7（0.14x）"
    "kimi-k3-ioa"              = "Kimi K3（1.59x）"
    "kimi-k2.7-ioa"            = "Kimi K2.7（0.45x）"
    "kimi-k2.6-ioa"            = "Kimi K2.6（0.45x）"
    "hy3-ioa"                  = "Hunyuan 3（0x）"
    "deepseek-v4-flash-ioa"    = "DeepSeek V4 Flash（0.08x）"
    "deepseek-v4-pro-ioa"      = "DeepSeek V4 Pro（0.24x）"
}

$modelIds = @(
    "claude-sonnet-5", "claude-sonnet-5-1m", "claude-sonnet-4.6", "claude-sonnet-4.6-1m",
    "claude-opus-5", "claude-opus-4.8", "claude-opus-4.8-1m", "claude-opus-4.7",
    "claude-opus-4.7-1m", "claude-opus-4.6", "claude-opus-4.6-1m",
    "gemini-3.1-pro", "gemini-3.5-flash",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex",
    "glm-5.3-ioa", "glm-5.2-ioa", "glm-5.2-internal-ioa", "glm-5v-turbo-ioa",
    "minimax-m3-ioa", "minimax-m2.7-ioa",
    "kimi-k3-ioa", "kimi-k2.7-ioa", "kimi-k2.6-ioa",
    "hy3-ioa", "deepseek-v4-flash-ioa", "deepseek-v4-pro-ioa"
)

$efforts = @{ off = $null; minimal = "minimal"; low = "low"; medium = "medium"; high = "high" }

$models = foreach ($id in $modelIds) {
    $entry = @{
        id               = $id
        name             = $modelNames[$id]
        reasoningEfforts = $efforts
    }
    if ($id -like "*-1m") { $entry.contextWindow = 1000000 }
    , $entry
}

$provider = @{
    displayName          = "内网模型 TT Switch"
    apiKeyEnv            = "TT_SWITCH_API_KEY"
    api                  = "openai-completions"
    baseURL              = $BaseURL
    streamIdleTimeoutMs  = 600000
    reasoning            = "high"
    retryPolicy          = @{
        mode       = "normal"
        maxRetries = 3
        backoff    = @{
            initialDelayMs = 500
            maxDelayMs     = 10000
            jitterRatio    = 0.1
        }
    }
    models               = @($models)
}

# ---------- 3) 注册 provider ----------
Invoke-DshRpc "settings.mutate" @{
    ns  = "llm-pi-ai"
    ops = @(@{ op = "set"; path = @("providers", "tt-switch"); value = $provider })
} | Out-Null
Write-Host " [2/3] provider 'tt-switch' 已注册（31 个模型，含倍率标注）" -ForegroundColor Green

# ---------- 4) 验证 ----------
$providers = Invoke-DshRpc "llm.providers" @{}
$tt = $providers.providers | Where-Object { $_.provider -eq "tt-switch" }
if ($null -eq $tt -or -not $tt.active) {
    Write-Host " [3/3] 验证失败：tt-switch 未出现在活动 provider 列表" -ForegroundColor Red
    exit 1
}
Write-Host " [3/3] 验证通过：tt-switch active=$($tt.active)" -ForegroundColor Green

Write-Host ""
Write-Host "============================================================"
Write-Host " 接入完成！"
Write-Host " 1) 打开 Web GUI「设置 → 模型」，即可看到「内网模型 TT Switch」"
Write-Host "    （31 个模型，名称已标注倍率）。"
Write-Host " 2) 要把 agent 默认模型切到内网模型："
Write-Host "      - 模型页选择器里选 tt-switch / glm-5.3-ioa 等；或"
Write-Host "      - 改 ~/.dsh/settings.yaml 的 agent-default-model。"
Write-Host " 3) 每人独立 token 时："
Write-Host "      pwsh -File 分享-内网模型TT-Switch.ps1 -ApiKey \"自己的token\""
Write-Host "============================================================"
