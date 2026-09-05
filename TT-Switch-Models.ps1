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
        Write-Host "Cannot connect to $base" -ForegroundColor Red
        Write-Host "Make sure DeepSeek Harness Web is running on this computer." -ForegroundColor Yellow
        Write-Host "Details: $($_.Exception.Message)" -ForegroundColor DarkGray
        exit 1
    }

    $envelope = $resp.Content | ConvertFrom-Json
    if (-not $envelope.result.ok) {
        $err = $envelope.result.error
        throw "RPC $Method failed: $($err.code) $($err.message)"
    }
    return $envelope.result.value
}

Write-Host "== TT Switch model setup =="
Write-Host "   harness: $base"
Write-Host "   baseURL: $BaseURL"
Write-Host ""

Invoke-DshRpc "credentials.set" @{ ref = "TT_SWITCH_API_KEY"; value = $ApiKey } | Out-Null
Write-Host " [1/3] API key saved." -ForegroundColor Green

$modelNames = @{
    "claude-sonnet-5"          = "Claude Sonnet 5 (1.12x)"
    "claude-sonnet-5-1m"       = "Claude Sonnet 5 1M (1.12x)"
    "claude-sonnet-4.6"        = "Claude Sonnet 4.6 (1.72x)"
    "claude-sonnet-4.6-1m"     = "Claude Sonnet 4.6 1M (1.72x)"
    "claude-opus-5"            = "Claude Opus 5 (2.86x)"
    "claude-opus-4.8"          = "Claude Opus 4.8 (2.86x)"
    "claude-opus-4.8-1m"       = "Claude Opus 4.8 1M (2.86x)"
    "claude-opus-4.7"          = "Claude Opus 4.7 (2.86x)"
    "claude-opus-4.7-1m"       = "Claude Opus 4.7 1M (2.86x)"
    "claude-opus-4.6"          = "Claude Opus 4.6 (2.86x)"
    "claude-opus-4.6-1m"       = "Claude Opus 4.6 1M (2.86x)"
    "gemini-3.1-pro"           = "Gemini 3.1 Pro (1.16x)"
    "gemini-3.5-flash"         = "Gemini 3.5 Flash (0.87x)"
    "gpt-5.6-sol"              = "GPT-5.6 Sol (2.9x)"
    "gpt-5.6-terra"            = "GPT-5.6 Terra (1.16x)"
    "gpt-5.6-luna"             = "GPT-5.6 Luna (0.12x)"
    "gpt-5.5"                  = "GPT-5.5 (2.9x)"
    "gpt-5.4"                  = "GPT-5.4 (1.45x)"
    "gpt-5.3-codex"            = "GPT-5.3 Codex (1.3x)"
    "glm-5.3-ioa"              = "GLM 5.3 (0.49x)"
    "glm-5.2-ioa"              = "GLM 5.2 (0.49x)"
    "glm-5.2-internal-ioa"     = "GLM 5.2 Internal (0.49x)"
    "glm-5v-turbo-ioa"         = "GLM-5V Turbo (0.36x)"
    "minimax-m3-ioa"           = "MiniMax M3 (0.14x)"
    "minimax-m2.7-ioa"         = "MiniMax M2.7 (0.14x)"
    "kimi-k3-ioa"              = "Kimi K3 (1.59x)"
    "kimi-k2.7-ioa"            = "Kimi K2.7 (0.45x)"
    "kimi-k2.6-ioa"            = "Kimi K2.6 (0.45x)"
    "hy3-ioa"                  = "Hunyuan 3 (0x)"
    "deepseek-v4-flash-ioa"    = "DeepSeek V4 Flash (0.08x)"
    "deepseek-v4-pro-ioa"      = "DeepSeek V4 Pro (0.24x)"
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
    displayName         = "TT Switch Models"
    apiKeyEnv           = "TT_SWITCH_API_KEY"
    api                 = "openai-completions"
    baseURL             = $BaseURL
    streamIdleTimeoutMs = 600000
    reasoning           = "high"
    retryPolicy         = @{
        mode       = "normal"
        maxRetries = 3
        backoff    = @{
            initialDelayMs = 500
            maxDelayMs     = 10000
            jitterRatio    = 0.1
        }
    }
    models              = @($models)
}

Invoke-DshRpc "settings.mutate" @{
    ns  = "llm-pi-ai"
    ops = @(@{ op = "set"; path = @("providers", "tt-switch"); value = $provider })
} | Out-Null
Write-Host " [2/3] Provider tt-switch registered with 31 models." -ForegroundColor Green

$providers = Invoke-DshRpc "llm.providers" @{}
$tt = $providers.providers | Where-Object { $_.provider -eq "tt-switch" }
if ($null -eq $tt -or -not $tt.active) {
    Write-Host " [3/3] Check failed: tt-switch is not active." -ForegroundColor Red
    exit 1
}

Write-Host " [3/3] Check passed: tt-switch active=$($tt.active)" -ForegroundColor Green
Write-Host ""
Write-Host "Model setup complete."
