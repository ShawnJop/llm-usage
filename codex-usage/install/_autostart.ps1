param([switch]$Uninstall)

$project = Split-Path -Parent $PSScriptRoot
$autostartScript = Join-Path $project 'install_autostart.ps1'

if (-not (Test-Path -LiteralPath $autostartScript)) {
    throw "File not found: $autostartScript"
}

if ($Uninstall) {
    & $autostartScript -Uninstall
} else {
    & $autostartScript
}

exit $LASTEXITCODE
