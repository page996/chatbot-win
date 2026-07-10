param(
    [string]$Configuration = "Release",
    [string]$Platform = "x64",
    [string]$PlatformToolset = "v143",
    [string]$ArtifactDir = "vendor\artifacts\wechat-native-411053"
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$project = Join-Path $repo "vendor\reference\WeChat-Hook-aixed\x64_Version_dll.vcxproj"
$msbuild = "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe"
if (-not (Test-Path -LiteralPath $msbuild)) {
    $candidate = Get-ChildItem -Path "C:\Program Files\Microsoft Visual Studio\2022" -Recurse -Filter MSBuild.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like "*\MSBuild\Current\Bin\MSBuild.exe" } |
        Select-Object -First 1
    if ($null -eq $candidate) {
        throw "MSBuild.exe not found"
    }
    $msbuild = $candidate.FullName
}

& $msbuild $project /m /p:Configuration=$Configuration /p:Platform=$Platform /p:PlatformToolset=$PlatformToolset
if ($LASTEXITCODE -ne 0) {
    throw "MSBuild failed with exit code $LASTEXITCODE"
}

$sourceDll = Join-Path $repo "vendor\reference\WeChat-Hook-aixed\$Platform\$Configuration\version.dll"
if (-not (Test-Path -LiteralPath $sourceDll)) {
    $sourceDll = Join-Path $repo "vendor\reference\WeChat-Hook-aixed\$Configuration\version.dll"
}
if (-not (Test-Path -LiteralPath $sourceDll)) {
    throw "Build completed but version.dll was not found"
}

$targetDir = Join-Path $repo $ArtifactDir
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
Copy-Item -LiteralPath $sourceDll -Destination (Join-Path $targetDir "version.dll") -Force

$sourcePdb = [System.IO.Path]::ChangeExtension($sourceDll, ".pdb")
if (Test-Path -LiteralPath $sourcePdb) {
    Copy-Item -LiteralPath $sourcePdb -Destination (Join-Path $targetDir "version.pdb") -Force
}

Write-Output "artifact=$targetDir\version.dll"
