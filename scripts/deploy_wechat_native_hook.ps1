param(
    [string]$SourceDll = "vendor\artifacts\wechat-native-411053\version.dll",
    [string]$WeixinRoot = "",
    [switch]$AllowRunning
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
if ([System.IO.Path]::IsPathRooted($SourceDll)) {
    $source = Resolve-Path $SourceDll
} else {
    $source = Resolve-Path (Join-Path $repo $SourceDll)
}

$runningCandidates = @(
    Get-Process Weixin -ErrorAction SilentlyContinue |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_.Path) }
)
if ([string]::IsNullOrWhiteSpace($WeixinRoot)) {
    if ($runningCandidates.Count -gt 0) {
        $WeixinRoot = Split-Path -Parent $runningCandidates[0].Path
    } elseif (-not [string]::IsNullOrWhiteSpace($env:WECHAT_NATIVE_WEIXIN_ROOT)) {
        $WeixinRoot = $env:WECHAT_NATIVE_WEIXIN_ROOT
    } else {
        throw "WeixinRoot was not provided and no running Weixin.exe path was found. Pass -WeixinRoot or set WECHAT_NATIVE_WEIXIN_ROOT."
    }
}

$weixinRootPath = Resolve-Path $WeixinRoot
$target = Join-Path $weixinRootPath "version.dll"
$weixinExe = Join-Path $weixinRootPath "Weixin.exe"

$running = @(
    $runningCandidates |
        Where-Object { $_.Path -eq $weixinExe }
)
if ($running.Count -gt 0 -and -not $AllowRunning) {
    $pids = ($running | ForEach-Object { $_.Id }) -join ","
    throw "Weixin.exe is running from $weixinRootPath (pid=$pids). Close WeChat first, or pass -AllowRunning if you intentionally want to try a live copy."
}

if (-not (Test-Path -LiteralPath $source)) {
    throw "source DLL not found: $source"
}
if (-not (Test-Path -LiteralPath $weixinExe)) {
    throw "Weixin.exe not found under: $weixinRootPath"
}

if (Test-Path -LiteralPath $target) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backup = "$target.codex-backup-$stamp"
    Copy-Item -LiteralPath $target -Destination $backup -Force
    Write-Output "backup=$backup"
}

Copy-Item -LiteralPath $source -Destination $target -Force
$deployed = Get-Item -LiteralPath $target
Write-Output "deployed=$($deployed.FullName)"
Write-Output "length=$($deployed.Length)"
Write-Output "last_write_time=$($deployed.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"
