# Build the standalone Windows trainer executable.
# It bundles scikit-learn, so it is much larger than the director exe.
# Usage: powershell -ExecutionPolicy Bypass -File .\build_trainer.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --upgrade pyinstaller
python -m PyInstaller `
  --noconfirm --clean --onefile --windowed `
  --name CS2AIDecisionTrainer `
  --collect-submodules sklearn `
  --collect-submodules joblib `
  --exclude-module matplotlib `
  --exclude-module pandas `
  --exclude-module demoparser2 `
  --exclude-module pytest `
  --exclude-module IPython `
  trainer_gui.py

Write-Host ""
Write-Host "Built: $PSScriptRoot\dist\CS2AIDecisionTrainer.exe"
