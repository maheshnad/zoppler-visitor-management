$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Read-Settings([string]$path) {
    $settings = [ordered]@{}
    Get-Content -LiteralPath $path | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#')) {
            $name, $value = $line -split '=', 2
            $settings[$name] = $value
        }
    }
    return $settings
}

$localSettings = Read-Settings (Join-Path $projectRoot '.env')
$targetUrl = (Read-Host 'Paste the Neon pooled PostgreSQL connection string').Trim()
if ($targetUrl -notmatch '^postgres(ql)?://' -or $targetUrl -notmatch '\.neon\.tech/') {
    throw 'That does not look like a Neon PostgreSQL connection string.'
}
if ($targetUrl -notmatch 'sslmode=require') {
    throw 'Use the Neon connection string that includes sslmode=require.'
}

$cloudSettings = [ordered]@{
    DATABASE_URL = $targetUrl
    SECRET_KEY = (& (Join-Path $projectRoot '.venv\Scripts\python.exe') -c 'import secrets; print(secrets.token_urlsafe(48))')
    COOKIE_SECURE = '1'
    SMTP_HOST = $localSettings.SMTP_HOST
    SMTP_PORT = $localSettings.SMTP_PORT
    SMTP_TLS = $localSettings.SMTP_TLS
    SMTP_USERNAME = $localSettings.SMTP_USERNAME
    SMTP_PASSWORD = $localSettings.SMTP_PASSWORD
    SMTP_FROM = $localSettings.SMTP_FROM
}

$cloudPath = Join-Path $projectRoot '.cloud.env'
$lines = foreach ($entry in $cloudSettings.GetEnumerator()) { $entry.Key + '=' + $entry.Value }
[System.IO.File]::WriteAllLines($cloudPath, $lines)

$env:SOURCE_DATABASE_URL = $localSettings.DATABASE_URL
$env:TARGET_DATABASE_URL = $targetUrl
$env:SECRET_KEY = $cloudSettings.SECRET_KEY
$env:COOKIE_SECURE = '1'
& (Join-Path $projectRoot '.venv\Scripts\python.exe') (Join-Path $projectRoot 'migrate_to_cloud.py')
if ($LASTEXITCODE -ne 0) { throw 'Cloud database migration failed.' }

$env:SOURCE_DATABASE_URL = $null
$env:TARGET_DATABASE_URL = $null
$targetUrl = $null
Write-Host 'Cloud settings saved securely in .cloud.env. Do not share or upload this file.' -ForegroundColor Green
