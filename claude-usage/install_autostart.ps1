param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$valueName = "ClaudeUsageBanana"
$launcher = Join-Path $PSScriptRoot "run.bat"

if ($Uninstall) {
    Remove-ItemProperty -Path $runKey -Name $valueName -ErrorAction SilentlyContinue
    Write-Host "Claude Usage autostart has been removed."
    exit 0
}

New-Item -Path $runKey -Force | Out-Null
Set-ItemProperty -Path $runKey -Name $valueName -Value ('"{0}"' -f $launcher)
Write-Host "Claude Usage autostart has been enabled."
