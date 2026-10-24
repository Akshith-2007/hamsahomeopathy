# Serve Hamsa to every device on this Wi-Fi (Waitress, production mode).
# Keep this window open while the clinic is using the app; close it (or Ctrl+C) to stop.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path ".env")) {
    Write-Host ".env not found - running first-time setup..."
    & (Join-Path $PSScriptRoot "setup_lan.ps1")
}

$port = 5000
$portLine = Select-String -Path ".env" -Pattern "^HAMSA_PORT=(\d+)" -ErrorAction SilentlyContinue
if ($portLine) { $port = [int]$portLine.Matches[0].Groups[1].Value }

# Windows can let two servers bind the same port, so refuse to start if something already listens.
$busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($busy[0].OwningProcess)"
    Write-Host "Port $port is already in use by: $($owner.CommandLine)" -ForegroundColor Red
    Write-Host "Close that program (or the other Hamsa window) and start again."
    exit 1
}

# Apply any new migrations and refresh static files after an update.
& $python manage.py migrate --noinput | Out-Null
& $python manage.py collectstatic --noinput | Out-Null

$ips = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.InterfaceAlias -notlike "vEthernet*" } |
    Select-Object -ExpandProperty IPAddress

Write-Host ""
Write-Host "  Hamsa Homeopathy is running." -ForegroundColor Green
Write-Host "  Open it on any phone, tablet or PC on the same Wi-Fi:"
foreach ($ip in $ips) { Write-Host "      http://${ip}:$port" -ForegroundColor Cyan }
Write-Host "  On this PC:  http://localhost:$port"
Write-Host "  Keep this window open. Press Ctrl+C to stop."
Write-Host ""

& $python -m waitress --listen="0.0.0.0:$port" --threads=8 hamsa.wsgi:application
