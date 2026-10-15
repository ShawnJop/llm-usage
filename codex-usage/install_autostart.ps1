param([switch]$Uninstall)

$appName = 'CodexUsageCat'
$project = $PSScriptRoot
$runScript = Join-Path $project 'run.bat'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'

if ($Uninstall) {
    Remove-ItemProperty -Path $runKey -Name $appName -ErrorAction SilentlyContinue
    Write-Host 'Autostart has been removed.'
    exit 0
}

if (-not (Test-Path -LiteralPath $runScript)) { throw "File not found: $runScript" }
Set-ItemProperty -Path $runKey -Name $appName -Value ('"{0}"' -f $runScript)
Write-Host 'Autostart has been enabled for the current user.'
