param(
    [int]$Port = 3080,
    # 令牌不写入脚本：默认读环境变量 TT_TOKEN，也可用 -ApiKey 显式传入
    [string]$ApiKey = $env:TT_TOKEN,
    [string]$BaseURL = "http://118.31.105.6:18443/tencent/v1"
)

$ErrorActionPreference = "Stop"

# 兜底：未设置环境变量时，自动读取脚本同目录 .env.txt 中的 TT_TOKEN=
# （优先级：-ApiKey / 环境变量 TT_TOKEN > .env.txt；复制整个文件夹分发时无需任何手动输入）
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    $envFile = Join-Path $PSScriptRoot ".env.txt"
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
            if ($line -match '^\s*TT_TOKEN\s*=\s*(\S+)') {
                $ApiKey = $Matches[1]
                break
            }
        }
    }
}

if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    Write-Host "缺少 TT Switch token：在脚本同目录 .env.txt 写入 TT_TOKEN=ttsw-...（或设置环境变量 TT_TOKEN / 用 -ApiKey 传入）" -ForegroundColor Red
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

# 费用口径（2026-09-11，iWiki 刊例价）：-ioa 后缀 = 内网免费；混元系（hy3/hy4，含
# 无后缀的 hy4-dev）HY 系列全免；公费倍率 =（输入+输出）×7÷75（DeepSeek 取空闲时段）。
$modelNames = @{
    "claude-sonnet-5"          = "Claude Sonnet 5 (1.12x)"
    "claude-sonnet-5-1m"       = "Claude Sonnet 5 1M (1.12x)"
    "claude-sonnet-4.6"        = "Claude Sonnet 4.6 (1.68x)"
    "claude-sonnet-4.6-1m"     = "Claude Sonnet 4.6 1M (1.68x)"
    "claude-opus-5"            = "Claude Opus 5 (2.8x)"
    "claude-opus-4.8"          = "Claude Opus 4.8 (2.8x)"
    "claude-opus-4.8-1m"       = "Claude Opus 4.8 1M (2.8x)"
    "claude-opus-4.7"          = "Claude Opus 4.7 (2.8x)"
    "claude-opus-4.7-1m"       = "Claude Opus 4.7 1M (2.8x)"
    "claude-opus-4.6"          = "Claude Opus 4.6 (2.8x)"
    "claude-opus-4.6-1m"       = "Claude Opus 4.6 1M (2.8x)"
    "gemini-3.1-pro"           = "Gemini 3.1 Pro (1.31x)"
    "gemini-3.5-flash"         = "Gemini 3.5 Flash (0.98x)"
    "gpt-6-astra"              = "GPT-6 Astra (5.6x)"
    "gpt-5.6-sol"              = "GPT-5.6 Sol (2.24x)"
    "gpt-5.6-terra"            = "GPT-5.6 Terra (1.31x)"
    "gpt-5.6-luna"             = "GPT-5.6 Luna (0.13x)"
    "gpt-5.5"                  = "GPT-5.5 (3.27x)"
    "gpt-5.4"                  = "GPT-5.4 (1.63x)"
    "gpt-5.3-codex"            = "GPT-5.3 Codex (1.47x)"
    "ttsw-gpt-5.6-sol-272k"    = "GPT-5.6 Sol 272K (2.24x)"
    "ttsw-gpt-5.6-terra-272k"  = "GPT-5.6 Terra 272K (1.31x)"
    "ttsw-gpt-5.6-luna-272k"   = "GPT-5.6 Luna 272K (0.13x)"
    "deepseek-v4.1-flash"      = "DeepSeek V4.1 Flash (0.07x)"
    "glm-5.3-ioa"              = "GLM 5.3 (free)"
    "glm-5.3-flash-ioa"        = "GLM 5.3 Flash (free)"
    "glm-5.2-ioa"              = "GLM 5.2 (free)"
    "glm-5.2-internal-ioa"     = "GLM 5.2 Internal (free)"
    "glm-5v-turbo-ioa"         = "GLM-5V Turbo (free)"
    "minimax-m3-ioa"           = "MiniMax M3 (free)"
    "minimax-m2.7-ioa"         = "MiniMax M2.7 (free)"
    "kimi-k3-ioa"              = "Kimi K3 (free)"
    "kimi-k2.7-ioa"            = "Kimi K2.7 (free)"
    "kimi-k2.6-ioa"            = "Kimi K2.6 (free)"
    "hy3-ioa"                  = "Hunyuan 3 (free)"
    "hy4-dev"                  = "Hunyuan 4 Dev (free)"
    "hy4-preview-ioa"          = "Hunyuan 4 Preview (free)"
    "deepseek-v4-pro-ioa"      = "DeepSeek V4 Pro (free)"
}

$modelIds = @(
    "claude-sonnet-5", "claude-sonnet-5-1m", "claude-sonnet-4.6", "claude-sonnet-4.6-1m",
    "claude-opus-5", "claude-opus-4.8", "claude-opus-4.8-1m", "claude-opus-4.7",
    "claude-opus-4.7-1m", "claude-opus-4.6", "claude-opus-4.6-1m",
    "gemini-3.1-pro", "gemini-3.5-flash",
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex",
    "ttsw-gpt-5.6-sol-272k", "ttsw-gpt-5.6-terra-272k", "ttsw-gpt-5.6-luna-272k",
    "deepseek-v4.1-flash",
    "glm-5.3-ioa", "glm-5.3-flash-ioa", "glm-5.2-ioa", "glm-5.2-internal-ioa", "glm-5v-turbo-ioa",
    "minimax-m3-ioa", "minimax-m2.7-ioa",
    "kimi-k3-ioa", "kimi-k2.7-ioa", "kimi-k2.6-ioa",
    "hy3-ioa", "hy4-dev", "hy4-preview-ioa",
    "deepseek-v4-pro-ioa"
)

$efforts = @{ off = $null; minimal = "minimal"; low = "low"; medium = "medium"; high = "high" }

# 多模态模型：声明 input = [text, image]（Harness 入口按此声明放行图片输入）。
# 未列出的模型（MiniMax M3/M2.7、Hunyuan 3、Hunyuan 4 dev/preview）保持默认仅文本。
# 注意：这只是 Harness 入口的能力声明，网关/上游也需真正支持该模型的图片输入。
$imageInputModels = @(
    "claude-sonnet-5", "claude-sonnet-5-1m", "claude-sonnet-4.6", "claude-sonnet-4.6-1m",
    "claude-opus-5", "claude-opus-4.8", "claude-opus-4.8-1m", "claude-opus-4.7",
    "claude-opus-4.7-1m", "claude-opus-4.6", "claude-opus-4.6-1m",
    "gemini-3.1-pro", "gemini-3.5-flash",
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.3-codex",
    "ttsw-gpt-5.6-sol-272k", "ttsw-gpt-5.6-terra-272k", "ttsw-gpt-5.6-luna-272k",
    "deepseek-v4.1-flash",
    "glm-5.3-ioa", "glm-5.3-flash-ioa", "glm-5.2-ioa", "glm-5.2-internal-ioa", "glm-5v-turbo-ioa",
    "kimi-k3-ioa", "kimi-k2.7-ioa", "kimi-k2.6-ioa",
    "deepseek-v4-pro-ioa"
)

$models = foreach ($id in $modelIds) {
    $entry = @{
        id               = $id
        name             = $modelNames[$id]
        reasoningEfforts = $efforts
    }
    if ($id -like "*-1m") { $entry.contextWindow = 1000000 }
    if ($id -like "*-272k") { $entry.contextWindow = 272000 }
    if ($imageInputModels -contains $id) { $entry.input = @("text", "image") }
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
Write-Host " [2/3] Provider tt-switch registered with 38 models." -ForegroundColor Green

$providers = Invoke-DshRpc "llm.providers" @{}
$tt = $providers.providers | Where-Object { $_.provider -eq "tt-switch" }
if ($null -eq $tt -or -not $tt.active) {
    Write-Host " [3/3] Check failed: tt-switch is not active." -ForegroundColor Red
    exit 1
}

Write-Host " [3/3] Check passed: tt-switch active=$($tt.active)" -ForegroundColor Green
Write-Host ""
Write-Host "Model setup complete."
