$ErrorActionPreference = "Stop"

$taskName = "AUST Campus Network Auto Login"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$configDir = Join-Path $env:LOCALAPPDATA "AUST-AutoLogin"
if (Test-Path -LiteralPath $configDir) {
    Remove-Item -LiteralPath $configDir -Recurse -Force
}

Write-Host "Scheduled task removed. The encrypted user configuration was deleted."
