$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $projectRoot '.cloud.env'

if (-not (Test-Path -LiteralPath $source)) {
    throw '.cloud.env was not found. Run configure-cloud.ps1 first.'
}

$content = Get-Content -LiteralPath $source -Raw
if ([string]::IsNullOrWhiteSpace($content)) {
    throw '.cloud.env is empty.'
}

Set-Clipboard -Value $content
Write-Host 'Render environment variables copied to the clipboard.' -ForegroundColor Green
Write-Host 'In Render, choose Add from .env and paste with Ctrl+V.'
