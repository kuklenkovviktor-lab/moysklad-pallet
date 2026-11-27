param([ValidateSet('Check','Enroll','Print','Auto','PhysicalOne')][string]$Mode = 'Check')
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    $binding = Join-Path $env:LOCALAPPDATA 'GRIT-WMS\USB-Bridge-Config\binding.dpapi'
    if ($Mode -eq 'Enroll' -or ($Mode -eq 'Check' -and -not (Test-Path -LiteralPath $binding))) {
        py -m services.printing.grit_windows_usb_agent --enroll
        if ($LASTEXITCODE -ne 0) { throw 'FAILED: enrollment refused; no print' }
        if ($Mode -eq 'Enroll') { return }
    }
    if ($Mode -eq 'PhysicalOne') {
        # Existing verified DPAPI binding and dedicated USB test printer first.
        # PREPARATION NEVER PRINTS. A durable PG stand marker forbids retries.
        py -m services.printing.grit_windows_usb_agent --check
        if ($LASTEXITCODE -ne 0) { throw 'PhysicalOne: check failed; NO PRINT' }
        $remote = 'APP=$(sed -n "s/^APP=//p" /tmp/grit-pr208-demo.state); case "$APP" in grit-ui-pr192demo-*) docker exec "$APP" python /app/deploy/prepare_pr210_physical_one.py ;; *) exit 1 ;; esac'
        & ssh grit $remote
        if ($LASTEXITCODE -ne 0) { throw 'PhysicalOne: remote synthetic preparation refused; NO PRINT. NEVER RETRY' }
        py -m services.printing.grit_windows_usb_agent --auto-one --confirm GRIT-BRIDGE-ONE-SYNTHETIC-LABEL
        if ($LASTEXITCODE -ne 0) { throw 'PhysicalOne: UNKNOWN/FAILED; NEVER RETRY' }
        Write-Host 'PR210 PHYSICAL ONE: SUBMITTED TO WINDOWS, visually verify ONE 58x40 label, NOT WB'
        return
    }
    if ($Mode -eq 'Print') {
        # Legacy one-shot synthetic test, never a production WB label.
        py -m services.printing.grit_windows_usb_agent --once --confirm GRIT-BRIDGE-ONE-SYNTHETIC-LABEL
    } elseif ($Mode -eq 'Auto') {
        # Opt-in only. At most 16 synthetically approved tickets / 5 minutes;
        # USB journal enforces no automatic retries after uncertainty.
        py -m services.printing.grit_windows_usb_agent --auto --confirm GRIT-BRIDGE-ONE-SYNTHETIC-LABEL
    } else {
        py -m services.printing.grit_windows_usb_agent --check
    }
    if ($LASTEXITCODE -ne 0) { throw 'FAILED/UNKNOWN: see agent status; do not retry printing' }
} finally {
    Pop-Location
}
