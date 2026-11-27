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
        # Stream an isolated shell script over SSH stdin: no remote-shell
        # quote escaping and no manager/demo secret exposed to Windows.
        # The repository is updated ONLY if it is the /tmp PR210 checkout
        # mounted read-only into the designated synthetic demo container.
        $remote = @'
set -Eeuo pipefail
STATE=/tmp/grit-pr208-demo.state
test -f "$STATE"
test ! -L "$STATE"
APP=$(sed -n 's/^APP=//p' "$STATE")
case "$APP" in grit-ui-pr192demo-*) ;; *) exit 1 ;; esac
SRC=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Source}}{{end}}{{end}}' "$APP")
case "$SRC" in /tmp/grit-pr210-*) ;; *) exit 1 ;; esac
test "$(git -C "$SRC" branch --show-current)" = "feature/pr210-auto-usb-synthetic-from-fbs"
git -C "$SRC" pull --ff-only
docker exec "$APP" python /app/deploy/prepare_pr210_physical_one.py
'@
        $remote | & ssh grit 'bash -s'
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
