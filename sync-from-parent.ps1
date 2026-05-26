# Sync Interakt app modules from ../ into Production/ before Cloud Run deploy.
# Does NOT overwrite Production/main.py (keeps Cloud Run Firebase bootstrap).

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$parent = Split-Path -Parent $here

$modules = @(
    "interakt_api.py",
    "bot_shared.py",
    "approval.py",
    "od_request.py",
    "visitor_request.py"
)

foreach ($name in $modules) {
    Copy-Item -Force (Join-Path $parent $name) (Join-Path $here $name)
    Write-Host "Copied $name"
}

Write-Host "Update Production/.env.example manually if new env vars were added in parent."

Write-Host ""
Write-Host "Next: deploy from Interakt/Production/"
Write-Host "  gcloud run deploy ... --source ."
Write-Host ""
Write-Host "Set Cloud Run env vars from Production/.env.example"
Write-Host "Production/main.py is unchanged — merge manually from ../main.py only if routing changed."
