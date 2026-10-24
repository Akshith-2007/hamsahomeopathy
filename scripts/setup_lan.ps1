# One-time setup for hosting Hamsa on this PC over the local Wi-Fi.
# Creates .env (random secret key, debug off, LAN access), a data folder outside OneDrive,
# the database, and (if none exists) the first administrator account.
param(
    [string]$DataDir = "C:\HamsaData",
    [int]$Port = 5000
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment and installing requirements..."
    python -m venv .venv
    & $python -m pip install -q -r requirements.txt
}

New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "media") | Out-Null

if (-not (Test-Path ".env")) {
    $secret = & $python -c "import secrets; print(secrets.token_urlsafe(50))"
    @"
# Local Wi-Fi hosting (created by scripts\setup_lan.ps1). Never share or commit this file.
DJANGO_SECRET_KEY=$secret
DJANGO_DEBUG=false
DJANGO_ALLOW_LAN=true
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DJANGO_SECURE_COOKIES=false
DB_ENGINE=sqlite
SQLITE_PATH=$DataDir\hamsa.sqlite3
MEDIA_ROOT=$DataDir\media
TIME_ZONE=Asia/Kolkata
CLINIC_NAME=Hamsa Homeopathy
HAMSA_PORT=$Port
"@ | Set-Content -Encoding UTF8 ".env"
    Write-Host "Created .env (data folder: $DataDir)"
} else {
    Write-Host ".env already exists - leaving it unchanged."
}

& $python manage.py migrate --noinput
& $python manage.py collectstatic --noinput | Out-Null
& $python manage.py rebuild_family_links | Out-Null

$users = & $python manage.py shell -c "from django.contrib.auth import get_user_model; print(get_user_model().objects.count())"
if ([int]$users -eq 0) {
    Write-Host ""
    Write-Host "Create the first administrator account:"
    & $python manage.py createsuperuser
}
Write-Host ""
Write-Host "Setup complete. Start the server with start_hamsa.bat"
