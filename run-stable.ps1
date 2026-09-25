$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$domain = 'laboring-grumpily-trembling.ngrok-free.dev'

Get-Content (Join-Path $projectRoot '.env') | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#')) {
        $name, $value = $line -split '=', 2
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

$env:COOKIE_SECURE = '1'
$waitress = Join-Path $projectRoot '.venv\Scripts\waitress-serve.exe'
$ngrok = (Get-Command ngrok.exe -ErrorAction SilentlyContinue).Source
if (-not $ngrok) {
    $ngrok = 'C:\Users\mahes\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe'
}
if (-not (Test-Path -LiteralPath $ngrok)) { throw 'ngrok is not installed.' }

$logDirectory = Join-Path $projectRoot '.logs'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$waitressOut = Join-Path $logDirectory 'waitress.out.log'
$waitressError = Join-Path $logDirectory 'waitress.err.log'
$ngrokOut = Join-Path $logDirectory 'ngrok.out.log'
$ngrokError = Join-Path $logDirectory 'ngrok.err.log'
$ngrokLog = Join-Path $logDirectory 'ngrok.log'

Write-Host "Stable visitor website: https://$domain/" -ForegroundColor Green
while ($true) {
    $server = $null
    $tunnel = $null
    $externalTunnel = $false
    try {
        $server = Start-Process -FilePath $waitress -ArgumentList '--listen=127.0.0.1:5000','server:app' -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $waitressOut -RedirectStandardError $waitressError -PassThru
        Start-Sleep -Seconds 3
        if ($server.HasExited) {
            try { Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/health' -TimeoutSec 5 | Out-Null }
            catch { throw 'Visitor server could not start and no healthy server is already running.' }
            $server = $null
        }

        try {
            $activeTunnels = (Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 3).tunnels
            $externalTunnel = [bool]($activeTunnels | Where-Object { $_.public_url -eq "https://$domain" })
        } catch { $externalTunnel = $false }
        if (-not $externalTunnel) {
            $arguments = @('http',"--domain=$domain",'5000',"--log=$ngrokLog",'--log-format=logfmt')
            $tunnel = Start-Process -FilePath $ngrok -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $ngrokOut -RedirectStandardError $ngrokError -PassThru
        }
        $failedHealthChecks = 0
        while (($externalTunnel -or ($tunnel -and -not $tunnel.HasExited)) -and (-not $server -or -not $server.HasExited)) {
            Start-Sleep -Seconds 10
            if ($externalTunnel) {
                try {
                    $activeTunnels = (Invoke-RestMethod -Uri 'http://127.0.0.1:4040/api/tunnels' -TimeoutSec 3).tunnels
                    if (-not ($activeTunnels | Where-Object { $_.public_url -eq "https://$domain" })) { break }
                } catch { break }
            }
            try {
                Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/health' -TimeoutSec 5 | Out-Null
                $failedHealthChecks = 0
            } catch {
                $failedHealthChecks++
                if ($failedHealthChecks -ge 3) { break }
            }
        }
    } catch {
        Add-Content -LiteralPath (Join-Path $logDirectory 'startup-errors.log') -Value "$(Get-Date -Format o) $($_.Exception.Message)"
    } finally {
        if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue }
        if ($server -and -not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Seconds 5
}
