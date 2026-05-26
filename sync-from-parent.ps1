# Copy latest Interakt app code into this folder before Cloud Run deploy.
# Preserves Cloud Run bootstrap in main.py — run this script then verify main.py still has
# _running_on_cloud_run and ADC Firebase init (or re-apply from DEPLOY.md section 6).

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$parent = Split-Path -Parent $here

Copy-Item -Force (Join-Path $parent "interakt_api.py") (Join-Path $here "interakt_api.py")
Write-Host "Copied interakt_api.py"

$prodMain = Join-Path $here "main.py"
$backup = Join-Path $here "main.py.cloudrun.bak"
Copy-Item -Force $prodMain $backup

Copy-Item -Force (Join-Path $parent "main.py") $prodMain
Write-Host "Copied main.py from parent — RE-APPLY Cloud Run blocks in main.py (see DEPLOY.md section 6)"
Write-Host "Backup of previous Production main.py: $backup"
