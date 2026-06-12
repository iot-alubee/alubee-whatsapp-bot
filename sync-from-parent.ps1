# Sync Interakt app modules from ../ into Production/ before Cloud Run deploy.

# Production/main.py is synced too (same Cloud Run + local Firebase bootstrap).



$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$parent = Split-Path -Parent $here



$modules = @(

    "main.py",

    "interakt_api.py",

    "bot_shared.py",

    "approval.py",

    "approver_availability.py",

    "od_request.py",

    "visitor_request.py",

    "leave_request.py",

    "permission_request.py",

    "permission_times.py",

    "requirements.txt",

    ".env.example"

)



foreach ($name in $modules) {

    $src = Join-Path $parent $name

    if (-not (Test-Path $src)) {

        Write-Warning "Skip missing parent file: $name"

        continue

    }

    Copy-Item -Force $src (Join-Path $here $name)

    Write-Host "Copied $name"

}



Write-Host ""

Write-Host "Synced: OD, visitor, leave, permission, approval, bot_shared, main.py."

Write-Host ""

Write-Host "Deploy:"

Write-Host "  cd Interakt/Production"

Write-Host "  gcloud run deploy alubee-interakt-od-bot --source . --region asia-south1 --project alubee-prod"

Write-Host ""

Write-Host "Env vars: see CLOUD_RUN_ENV.md and .env.example"

