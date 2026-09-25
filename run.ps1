$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Get-Content (Join-Path $projectRoot '.env') | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#')) {
        $name, $value = $line -split '=', 2
        [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
}

& (Join-Path $projectRoot '.venv\Scripts\python.exe') (Join-Path $projectRoot 'server.py')
