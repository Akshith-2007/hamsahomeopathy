# Prepare the online (Vercel) database from this PC: apply migrations, build family links,
# create the first admin, and optionally load demo data. Run after the first deploy and
# again whenever an update adds migrations.
#
#   powershell -ExecutionPolicy Bypass -File scripts\vercel_migrate.ps1
#
# Paste the Neon DATABASE_URL (Vercel project -> Settings -> Environment Variables) when asked.
param(
    [switch]$SeedDemo
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not $env:DATABASE_URL) {
    $env:DATABASE_URL = Read-Host "Paste the Neon DATABASE_URL (postgres://...)"
}
# Point this run at the cloud database, not the local Wi-Fi one configured in .env.
$env:DB_ENGINE = "postgres"
Remove-Item Env:SQLITE_PATH -ErrorAction SilentlyContinue

& $python manage.py migrate --noinput
& $python manage.py rebuild_family_links

if (-not $env:DJANGO_ADMIN_PASSWORD) {
    $secure = Read-Host "Password for the online 'admin' account (skip with Enter if it already exists)" -AsSecureString
    $env:DJANGO_ADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
& $python manage.py ensure_admin

if ($SeedDemo) {
    & $python manage.py seed_demo
}
Write-Host "Online database is ready."
