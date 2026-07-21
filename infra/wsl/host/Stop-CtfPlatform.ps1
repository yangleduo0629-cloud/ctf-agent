[CmdletBinding()]
param([string]$Distribution = 'Ubuntu-24.04')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$wslExe = Join-Path $env:SystemRoot 'System32\wsl.exe'

$stateDir = Join-Path $env:LOCALAPPDATA 'CTF-Platform'
$pidFile = Join-Path $stateDir 'wsl-keepalive.pid'
$compose = 'docker compose --env-file /srv/ctf-platform/app/infra/wsl/.env -f /srv/ctf-platform/app/infra/wsl/compose.yaml stop'

& $wslExe -d $Distribution -u root -- bash -lc $compose
if ($LASTEXITCODE -ne 0) {
    throw 'Compose stop failed.'
}

if (Test-Path -LiteralPath $pidFile) {
    $storedPid = [int](Get-Content -Raw -LiteralPath $pidFile)
    Stop-Process -Id $storedPid -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidFile -Force
}
& $wslExe --terminate $Distribution
Write-Host "Stopped $Distribution and the CTF platform services."
