# Prepare the online (Vercel) database from this PC: apply migrations, build family links,
# create the first admin, and optionally load demo data. Run after the first deploy and
# again whenever an update adds migrations.
#
#   powershell -ExecutionPolicy Bypass -File scripts\vercel_migrate.ps1            # tables + admin
#   powershell -ExecutionPolicy Bypass -File scripts\vercel_migrate.ps1 -SeedDemo  # ... + demo data
#
# Paste the Neon DATABASE_URL (Vercel project -> Settings -> Environment Variables) when asked.
param(
    [switch]$SeedDemo
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"

# PowerShell does not stop when a program fails, so check every step explicitly.
function Invoke-Manage {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $python manage.py @Arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "FAILED: python manage.py $($Arguments -join ' ')" -ForegroundColor Red
        Write-Host "Nothing after this step was done. Copy the red error text above and send it for help."
        exit 1
    }
}

if (-not $env:DATABASE_URL) {
    $env:DATABASE_URL = (Read-Host "Paste the Neon DATABASE_URL (postgres://...)").Trim().Trim('"')
}
if ($env:DATABASE_URL -notmatch '^postgres(ql)?://') {
    Write-Host "That doesn't look like a database address (it should start with postgres:// or postgresql://)." -ForegroundColor Red
    exit 1
}
# Point this run at the cloud database, not the local Wi-Fi one configured in .env.
$env:DB_ENGINE = "postgres"
Remove-Item Env:SQLITE_PATH -ErrorAction SilentlyContinue

Write-Host "Creating/updating tables..." -ForegroundColor Cyan
Invoke-Manage migrate --noinput

if (-not $env:DJANGO_ADMIN_PASSWORD) {
    $secure = Read-Host "Password for the online 'admin' account (just press Enter if it already exists)" -AsSecureString
    $env:DJANGO_ADMIN_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}
Invoke-Manage ensure_admin

if ($SeedDemo) {
    Write-Host "Loading demo data (this takes a minute over the internet)..." -ForegroundColor Cyan
    Invoke-Manage seed_demo
}
Invoke-Manage rebuild_family_links

Write-Host ""
Invoke-Manage shell -c "from django.conf import settings; from django.contrib.auth import get_user_model; from clinic.models import Patient, Visit; d = settings.DATABASES['default']; print('Database   :', d['HOST'], '/', d['NAME']); print('Patients   :', Patient.objects.count(), '| Visits:', Visit.objects.count()); print('Users      :', ', '.join(get_user_model().objects.order_by('id').values_list('username', flat=True)))"
Write-Host "Online database is ready." -ForegroundColor Green
