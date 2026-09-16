param(
    [Parameter(Mandatory = $true)]
    [string]$Username,

    [Parameter(Mandatory = $true)]
    [ValidateSet("campus", "telecom", "unicom", "mobile", "staff")]
    [string]$Network,

    [ValidateSet("auto", "hefei", "huainan")]
    [string]$Campus = "auto",

    [ValidateSet("wifi", "wired")]
    [string]$Connection = "wifi",

    [int]$Interval = 60
)

$ErrorActionPreference = "Stop"

if ($Interval -lt 5) {
    throw "Interval must be at least 5 seconds."
}

$scriptPath = Join-Path $PSScriptRoot "aust_autologin.py"
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Script not found: $scriptPath"
}

$python = (Get-Command python.exe -ErrorAction Stop).Source
$password = Read-Host "Enter campus network password (hidden)" -AsSecureString
$plainPassword = [System.Net.NetworkCredential]::new("", $password).Password
if ([string]::IsNullOrEmpty($plainPassword)) {
    throw "Password cannot be empty."
}

$suffix = switch ($Network) {
    "campus"  { "" }
    "telecom" { "@aust" }
    "unicom"  { "@unicom" }
    "mobile"  { if ($Campus -eq "huainan") { "@cmcc" } else { "@hfcmcc" } }
    "staff"   { "@jzg" }
}

if (($Network -eq "unicom" -or $Network -eq "staff") -and $Campus -eq "hefei") {
    throw "The selected network is only documented for Huainan campus."
}

# Store credentials with Windows DPAPI. Only the current Windows user can
# decrypt the credential file. The runner injects it into the child process.
$configDir = Join-Path $env:LOCALAPPDATA "AUST-AutoLogin"
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
$credentialPath = Join-Path $configDir "credential.xml"
$credential = [PSCredential]::new($Username, $password)
$credential | Export-Clixml -LiteralPath $credentialPath

$runnerPath = Join-Path $configDir "run.ps1"
$escapedPython = $python.Replace("'", "''")
$escapedScript = $scriptPath.Replace("'", "''")
$escapedCredential = $credentialPath.Replace("'", "''")
$escapedSuffix = $suffix.Replace("'", "''")
$escapedCampus = $Campus.Replace("'", "''")
$escapedConnection = $Connection.Replace("'", "''")

$runner = @"
`$ErrorActionPreference = 'Stop'
`$credential = Import-Clixml -LiteralPath '$escapedCredential'
`$env:AUST_USERNAME = `$credential.UserName
`$env:AUST_PASSWORD = `$credential.GetNetworkCredential().Password
`$env:AUST_EXIT_SUFFIX = '$escapedSuffix'
`$env:AUST_CAMPUS = '$escapedCampus'
`$env:AUST_CONNECTION = '$escapedConnection'
& '$escapedPython' '$escapedScript' --daemon --interval $Interval
"@
Set-Content -LiteralPath $runnerPath -Value $runner -Encoding UTF8

$taskName = "AUST Campus Network Auto Login"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runnerPath`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "AUST Dr.COM campus network auto login and reconnect" `
    -Force | Out-Null

$plainPassword = $null
Write-Host "Scheduled task installed: $taskName"
Write-Host "Credential file: $credentialPath (protected by Windows DPAPI)"
Write-Host "You can start, stop, or remove the task in Task Scheduler."
