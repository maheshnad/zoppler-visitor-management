$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$envPath = Join-Path $projectRoot '.env'

$sender = (Read-Host 'SMTP sender email').Trim().ToLowerInvariant()
if ($sender -notmatch '^[^@\s]+@(zopplersystems\.com|zoppler\.com)$') {
    throw 'Use an existing @zopplersystems.com or @zoppler.com mailbox.'
}

$securePassword = Read-Host 'SMTP password or app password' -AsSecureString
$credential = [System.Net.NetworkCredential]::new('', $securePassword)
$appPassword = $credential.Password.Replace(' ', '')
if ($appPassword.Length -lt 8) {
    throw 'The SMTP password must contain at least 8 characters.'
}

$settings = [ordered]@{
    SMTP_HOST = $(if ($sender.EndsWith('@zoppler.com')) { 'mail.zoppler.com' } else { 'smtp.gmail.com' })
    SMTP_PORT = '587'
    SMTP_TLS = '1'
    SMTP_USERNAME = $sender
    SMTP_PASSWORD = $appPassword
    SMTP_FROM = $sender
}

$lines = [System.Collections.Generic.List[string]](Get-Content -LiteralPath $envPath)
foreach ($entry in $settings.GetEnumerator()) {
    $prefix = $entry.Key + '='
    $index = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].StartsWith($prefix)) { $index = $i; break }
    }
    $newLine = $prefix + $entry.Value
    if ($index -ge 0) { $lines[$index] = $newLine } else { $lines.Add($newLine) }
}

[System.IO.File]::WriteAllLines($envPath, $lines)
$appPassword = $null
$credential = $null
Write-Host 'SMTP settings saved. Restart the visitor application to enable email.' -ForegroundColor Green
