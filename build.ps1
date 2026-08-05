# CountBot 本地构建脚本（Windows PowerShell）
# 用法: .\build.ps1
# 产物: release\CountBot-yyyyMMdd-HHmmss.tar.gz

$ErrorActionPreference = "Stop"

# 1. 构建前端
Write-Host ">>> 构建前端..." -ForegroundColor Cyan
npm --prefix frontend ci
npm --prefix frontend run build

# 2. 打包
$releaseVersion = Get-Date -Format 'yyyyMMdd-HHmmss'
$releaseName = "CountBot-$releaseVersion"
$releaseRoot = Join-Path $PWD 'release'
$stagingDir = Join-Path $releaseRoot $releaseName
$archivePath = Join-Path $releaseRoot "$releaseName.tar.gz"

Write-Host ">>> 打包: $releaseName" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null

robocopy . $stagingDir /E /XD .git .idea .pytest_cache __pycache__ node_modules data workspace release config /XF *.pyc CountBot-*.tar.gz
if ($LASTEXITCODE -gt 7) {
    throw "robocopy 失败，exit code: $LASTEXITCODE"
}

tar.exe -czf $archivePath -C $releaseRoot $releaseName

# 3. 验证
Write-Host ">>> 验证打包结果..." -ForegroundColor Cyan
$hasFrontend = tar.exe -tzf $archivePath | Select-String 'frontend/dist/index.html'
$hasData = tar.exe -tzf $archivePath | Select-String '/data/|/workspace/'
if (-not $hasFrontend) { throw "❌ 发布包缺少前端构建产物" }
if ($hasData)       { Write-Host "⚠️  发布包包含持久化目录，请检查" -ForegroundColor Yellow }

# 4. 输出
$hash = (Get-FileHash $archivePath -Algorithm SHA256).Hash
Write-Host ""
Write-Host "✅ 构建完成" -ForegroundColor Green
Write-Host "   文件: $archivePath"
Write-Host "   SHA256: $hash"
Write-Host ""
Write-Host "服务器端部署:"
Write-Host "   bash deploy.sh /opt/countbot/incoming/$releaseName.tar.gz" -ForegroundColor Yellow
Write-Host "   (首次部署加 --init: bash deploy.sh ... --init)" -ForegroundColor Yellow
