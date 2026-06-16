# upload-pad.ps1 — Deploy a Mendix PAD to the SPCS deployment controller
#
# Usage:
#   .\upload-pad.ps1 -AppName manufacturing -PadPath C:\path\to\app.zip -ControllerUrl https://<ingress>.snowflakecomputing.app
#
# The controller URL is the public endpoint of MENDIX_DEPLOY_CONTROLLER.
# Get it with: snow sql -q "SHOW ENDPOINTS IN SERVICE YOUR_DB.PUBLIC.MENDIX_DEPLOY_CONTROLLER;"

param(
    [Parameter(Mandatory)][string]$AppName,
    [Parameter(Mandatory)][string]$PadPath,
    [Parameter(Mandatory)][string]$ControllerUrl,
    [int]$TimeoutSeconds = 360
)

$ErrorActionPreference = "Stop"

$PadPath = Resolve-Path $PadPath
if (-not (Test-Path $PadPath -PathType Leaf)) {
    Write-Error "PAD file not found: $PadPath"
    exit 1
}

$ControllerUrl = $ControllerUrl.TrimEnd("/")
$deployUrl     = "$ControllerUrl/apps/$AppName/deploy"
$statusUrl     = "$ControllerUrl/apps/$AppName"

Write-Host "Uploading PAD to $deployUrl..." -ForegroundColor Cyan

# Multipart POST
$response = Invoke-RestMethod -Uri $deployUrl -Method Post `
    -Form @{ pad_file = Get-Item $PadPath } `
    -ContentType "multipart/form-data" `
    -ErrorVariable restError

if ($restError) {
    Write-Error "Upload failed: $restError"
    exit 1
}

Write-Host "Upload accepted. Polling for status..." -ForegroundColor DarkGray

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10
    try {
        $status = Invoke-RestMethod -Uri $statusUrl -Method Get -ErrorAction SilentlyContinue
        $svcStatus = $status.service_status
        $deployStatus = $status.app.last_deploy_status
        Write-Host "  service=$svcStatus  deploy=$deployStatus" -ForegroundColor DarkGray

        if ($deployStatus -eq "READY") {
            $endpoint = $status.app.endpoint_url
            Write-Host ""
            Write-Host "Done!" -ForegroundColor Green
            Write-Host "  App:      $AppName"
            Write-Host "  Endpoint: $endpoint" -ForegroundColor Yellow
            exit 0
        }

        if ($deployStatus -eq "FAILED") {
            Write-Error "Deploy failed. Check controller logs."
            exit 1
        }
    } catch {
        Write-Host "  (status check failed, retrying...)" -ForegroundColor DarkGray
    }
}

Write-Error "Timed out after ${TimeoutSeconds}s waiting for deploy to complete."
exit 1
