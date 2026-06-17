# build-and-push.ps1 - Build the admin UI image and push it to the Snowflake registry
#
# Usage:
#   .\build-and-push.ps1
#   .\build-and-push.ps1 -Config "other-config.json"
#
# Prerequisites:
#   - Docker running (Rancher Desktop or Docker Desktop)
#   - Snowflake CLI (snow) installed and connection configured

param(
    [string]$Config = "admin-ui-config.json"
)

$ErrorActionPreference = "Stop"

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $scriptDir $Config }

if (-not (Test-Path $configPath)) {
    Write-Error "Config not found: $configPath"
    exit 1
}

$cfg  = Get-Content $configPath -Raw | ConvertFrom-Json
$conn = $cfg.snowConnection
$repo = $cfg.snowflake.imageRepo

$imageName = "mendix-admin-ui"

Write-Host "[1/3] Logging into Snowflake image registry..." -ForegroundColor Cyan
& snow spcs image-registry login --connection $conn
if ($LASTEXITCODE -ne 0) { Write-Error "Registry login failed."; exit 1 }

$registry = (& snow spcs image-registry url --connection $conn).Trim()
$repoUrl  = "$registry/$repo"

Write-Host "[2/3] Building image..." -ForegroundColor Cyan
Push-Location $scriptDir
try {
    & docker build -t $imageName .
    if ($LASTEXITCODE -ne 0) { Write-Error "Docker build failed."; exit 1 }

    & docker tag $imageName "$repoUrl/${imageName}:latest"
    if ($LASTEXITCODE -ne 0) { Write-Error "Docker tag failed."; exit 1 }

    Write-Host "[3/3] Pushing $repoUrl/${imageName}:latest ..." -ForegroundColor Cyan
    & docker push "$repoUrl/${imageName}:latest"
    if ($LASTEXITCODE -ne 0) { Write-Error "Docker push failed."; exit 1 }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host "  Image: $repoUrl/${imageName}:latest"
