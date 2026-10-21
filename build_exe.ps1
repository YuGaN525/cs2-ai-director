# Build a single Windows executable for CS2 AI Director.
# Usage: powershell -ExecutionPolicy Bypass -File .\build_exe.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --upgrade pyinstaller
python -m PyInstaller `
  --noconfirm --clean --onefile --windowed `
  --name CS2AIDirector `
  --hidden-import obsws_python `
  --hidden-import websocket `
  --collect-submodules obsws_python `
  --exclude-module sklearn `
  --exclude-module joblib `
  --exclude-module demoparser2 `
  --exclude-module pandas `
  --exclude-module matplotlib `
  cs2_ai_director.py

$exe = Join-Path $PSScriptRoot "dist\CS2AIDirector.exe"
if (!(Test-Path $exe)) { throw "Build failed: $exe not found" }
Write-Host ""
Write-Host "Built: $exe"
