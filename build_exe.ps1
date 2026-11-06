# Build the unified director and trainer executable. The optional file is public trust only.
param([string]$PublicTrustConfig = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if ($PublicTrustConfig) {
    $env:DIRECTOR_PUBLIC_TRUST_CONFIG = (Resolve-Path -LiteralPath $PublicTrustConfig).Path
} else {
    Remove-Item Env:DIRECTOR_PUBLIC_TRUST_CONFIG -ErrorAction SilentlyContinue
}
python -m pip install -r requirements.txt -r requirements-train.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
python -m PyInstaller --noconfirm --clean Yuzi_CS2_AIDirector.spec
if ($LASTEXITCODE -ne 0) { throw "Build failed" }
$exe = Join-Path $PSScriptRoot "dist\Yuzi_CS2_AIDirector.exe"
if (!(Test-Path -LiteralPath $exe)) { throw "Build output missing" }
Write-Host "Built: $exe"
