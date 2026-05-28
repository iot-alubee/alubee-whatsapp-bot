# Sync Interakt app modules from ../ into Production/ before Cloud Run deploy.
# Production/main.py keeps Cloud Run Firebase bootstrap (not overwritten).

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$parent = Split-Path -Parent $here

$modules = @(
    "interakt_api.py",
    "bot_shared.py",
    "approval.py",
    "od_request.py",
    "visitor_request.py",
    "requirements.txt"
)

foreach ($name in $modules) {
    Copy-Item -Force (Join-Path $parent $name) (Join-Path $here $name)
    Write-Host "Copied $name"
}

Write-Host ""
Write-Host "Synced: visitor message-by-message flow + OTP, OD unchanged."
Write-Host "Production/main.py is NOT overwritten (Cloud Run bootstrap)."
Write-Host ""
Write-Host "Deploy:"
Write-Host "  cd Interakt/Production"
Write-Host "  gcloud run deploy alubee-interakt-od-bot --source . --region asia-south1 --project alubee-prod"
Write-Host ""
Write-Host "Env vars: see CLOUD_RUN_ENV.md and .env.example"
