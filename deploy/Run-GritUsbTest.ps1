param([ValidateSet('Check','Enroll','Print')][string]$Mode = 'Check')
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    $binding = Join-Path $env:LOCALAPPDATA 'GRIT-WMS\USB-Bridge-Config\binding.dpapi'
    if ($Mode -eq 'Enroll' -or ($Mode -eq 'Check' -and -not (Test-Path -LiteralPath $binding))) {
        py -m services.printing.grit_windows_usb_agent --enroll
        if ($LASTEXITCODE -ne 0) { throw 'FAILED: enrollment refused; no print' }
        if ($Mode -eq 'Enroll') { return }
    }
    if ($Mode -eq 'Print') {
        # This explicit operator command is the ONLY path to physical submission.
        py -m services.printing.grit_windows_usb_agent --once --confirm GRIT-BRIDGE-ONE-SYNTHETIC-LABEL
    } else {
        py -m services.printing.grit_windows_usb_agent --check
    }
    if ($LASTEXITCODE -ne 0) { throw 'FAILED/UNKNOWN: see agent status; do not retry printing' }
} finally {
    Pop-Location
}
