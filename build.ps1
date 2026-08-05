# CountBot build script (Windows PowerShell)
# Usage: .\build.ps1
# Output: release\CountBot-yyyyMMdd-HHmmss.tar.gz

$ErrorActionPreference = "Stop"

# 1. Build frontend
Write-Host ">>> Building frontend..." -ForegroundColor Cyan
npm --prefix frontend ci
npm --prefix frontend run build

# 2. Package
$releaseVersion = Get-Date -Format 'yyyyMMdd-HHmmss'
$releaseName = "CountBot-$releaseVersion"
$releaseRoot = Join-Path $PWD 'release'
$stagingDir = Join-Path $releaseRoot $releaseName
$archivePath = Join-Path $releaseRoot "$releaseName.tar.gz"

Write-Host ">>> Packaging: $releaseName" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

robocopy . $stagingDir /E /XD .git .idea .pytest_cache __pycache__ node_modules data workspace release config /XF *.pyc CountBot-*.tar.gz
if ($LASTEXITCODE -gt 7) {
    throw "robocopy failed, exit code: $LASTEXITCODE"
}

tar.exe -czf $archivePath -C $releaseRoot $releaseName

# 3. Verify
Write-Host ">>> Verifying package..." -ForegroundColor Cyan
$hasFrontend = tar.exe -tzf $archivePath | Select-String 'frontend/dist/index.html'
$hasData = tar.exe -tzf $archivePath | Select-String '/data/|/workspace/'
if (-not $hasFrontend) { throw "ERROR: frontend build output missing in package" }
if ($hasData)       { Write-Host "WARNING: package contains data/workspace dirs, please check" -ForegroundColor Yellow }

# 4. Output
$hash = (Get-FileHash $archivePath -Algorithm SHA256).Hash
Write-Host ""
Write-Host "BUILD SUCCESS" -ForegroundColor Green
Write-Host "  File:   $archivePath"
Write-Host "  SHA256: $hash"
Write-Host ""
Write-Host "Deploy on server:"
Write-Host "  bash deploy.sh /opt/countbot/incoming/$releaseName.tar.gz" -ForegroundColor Yellow
Write-Host "  (first deploy add --init: bash deploy.sh ... --init)" -ForegroundColor Yellow
