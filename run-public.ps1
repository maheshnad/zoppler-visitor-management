$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Get-Content (Join-Path $projectRoot '.env') | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#')) {
        $name, $value = $line -split '=', 2
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

$env:COOKIE_SECURE = '1'
$waitress = Join-Path $projectRoot '.venv\Scripts\waitress-serve.exe'
$serverOut = Join-Path $projectRoot '.public-server-output.log'
$serverError = Join-Path $projectRoot '.public-server-error.log'
$cloudflared = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
if (-not (Test-Path -LiteralPath $cloudflared)) { $cloudflared = 'cloudflared' }
$server = Start-Process -FilePath $waitress -ArgumentList '--listen=127.0.0.1:5000','server:app' -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $serverOut -RedirectStandardError $serverError -PassThru

try {
    Start-Sleep -Seconds 3
    Write-Host 'Starting public HTTPS tunnel. Share the https://...trycloudflare.com URL shown below.' -ForegroundColor Green
    & $cloudflared tunnel --url http://127.0.0.1:5000 --no-autoupdate --protocol http2
} finally {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}
