# ONIX WMS · диагностика доступа из локальной сети склада на Windows.
# Не требует Python, администраторских прав, паролей или доступа к базе.
# Выполни на ПК, подключённом к ТОМУ ЖЕ складскому Wi-Fi, что и Android:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\test-onix-network.ps1
# Запусти дважды: VPN включён / выключен, сохрани результаты.
param(
    [string] $Hostname = "onix-wms-frankfurt.onrender.com"
)

Write-Host "===== ONIX NETWORK TEST ====="
Write-Host ("Checked at: " + [DateTime]::UtcNow.ToString("u"))
Write-Host ("Target: " + $Hostname)
Write-Host ("Computer: " + [Environment]::OSVersion.VersionString)
Write-Host "No passwords, cookies or badges are used."

Write-Host "\n1) DNS lookup:"
try {
    $addresses = [System.Net.Dns]::GetHostAddresses($Hostname) |
        ForEach-Object { $_.IPAddressToString }
    if (!$addresses) { throw "No DNS addresses returned" }
    Write-Host ("Resolved: " + ($addresses -join ", "))
} catch {
    Write-Host ("FAIL DNS: " + $_.Exception.Message)
    $addresses = @()
}

Write-Host "\n2) TCP connect, port 443 (max 8 seconds):"
$tcp = New-Object System.Net.Sockets.TcpClient
try {
    $async = $tcp.BeginConnect($Hostname, 443, $null, $null)
    if (-not $async.AsyncWaitHandle.WaitOne(8000)) {
        throw "TCP timed out after 8 seconds"
    }
    $tcp.EndConnect($async)
    Write-Host "OK TCP 443"
} catch {
    Write-Host ("FAIL TCP 443: " + $_.Exception.Message)
} finally {
    $tcp.Close()
}

Write-Host "\n3) HTTPS GET /api/health (max 15 seconds):"
try {
    $url = "https://" + $Hostname + "/api/health"
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $resp = Invoke-WebRequest -Uri $url -TimeoutSec 15 -UseBasicParsing -MaximumRedirection 3
    $watch.Stop()
    Write-Host ("HTTP " + [int]$resp.StatusCode + " in " + $watch.ElapsedMilliseconds + " ms")
} catch {
    $code = "no response"
    if ($_.Exception.Response) {
        $code = [int]$_.Exception.Response.StatusCode
    }
    Write-Host ("FAIL HTTPS: HTTP " + $code + "; " + $_.Exception.Message)
}
Write-Host "\nCompare Wi-Fi without VPN against Wi-Fi with VPN."
Write-Host "Do not send anyone badge codes, cookies, access tokens or passwords."
