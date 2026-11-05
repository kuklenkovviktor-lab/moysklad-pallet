# GRIT physical PDF test: isolated SQLite, no live marketplace credentials.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
  throw 'Python 3.10+ is required on this test PC.'
}
if (-not (Test-Path '.venv')) { python -m venv .venv }
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $py -m pip install -r requirements.txt
$env:GRIT_PDF_PHYSICAL_TEST = '1'
$env:WB_STRICT_MATCHING = '1'
$env:WMS_DB_PATH = Join-Path $PSScriptRoot 'grit_pdf_physical_test.db'
$env:WMS_REQUIRE_POSTGRES = '0'
$env:DATABASE_URL = ''
$env:WB_API_TOKEN = ''
$env:MOYSKLAD_TOKEN = ''
$env:MOYSKLAD_API_TOKEN = ''
$env:MS_TOKEN = ''
$env:SECRET_KEY = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$env:WMS_BADGE_SECRET = [guid]::NewGuid().ToString('N')
$env:WMS_TERMINAL_SECRET = [guid]::NewGuid().ToString('N')
$env:MANAGER_WEB_PASSWORD = [guid]::NewGuid().ToString('N').Substring(0,20)
$env:PORT = '5001'
Write-Host 'ISOLATED GRIT PDF TEST ONLY - no production DB / tokens'
Write-Host ('Manager test password: ' + $env:MANAGER_WEB_PASSWORD)
Write-Host 'PC: http://127.0.0.1:5001/manager-login'
Write-Host 'Phone: http://<PC LAN IP>:5001/fbs (trusted private Wi-Fi only)'
& $py app.py
