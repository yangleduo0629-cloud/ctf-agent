[CmdletBinding()]
param([string]$Distribution = 'Ubuntu-24.04')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$wslExe = Join-Path $env:SystemRoot 'System32\wsl.exe'

$stateDir = Join-Path $env:LOCALAPPDATA 'CTF-Platform'
$pidFile = Join-Path $stateDir 'wsl-keepalive.pid'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

$keepalive = $null
if (Test-Path -LiteralPath $pidFile) {
    $storedPid = [int](Get-Content -Raw -LiteralPath $pidFile)
    $keepalive = Get-Process -Id $storedPid -ErrorAction SilentlyContinue
}

if (-not $keepalive) {
    $keepalive = Start-Process -FilePath $wslExe -ArgumentList @(
        '-d', $Distribution,
        '-u', 'root',
        '--', 'sleep', 'infinity'
    ) -WindowStyle Hidden -PassThru
    [System.IO.File]::WriteAllText($pidFile, [string]$keepalive.Id, [System.Text.Encoding]::ASCII)
}

$healthScript = '/srv/ctf-platform/app/infra/wsl/scripts/health-check.sh --skip-gpu'
for ($attempt = 1; $attempt -le 60; $attempt++) {
    & $wslExe -d $Distribution -u root -- bash -lc $healthScript *> $null
    if ($LASTEXITCODE -eq 0) {
        [pscustomobject]@{
            Distribution = $Distribution
            KeepalivePid = $keepalive.Id
            Frontend = 'http://127.0.0.1:3000'
            Api = 'http://127.0.0.1:8080/readyz'
            Status = 'healthy'
        }
        exit 0
    }
    Start-Sleep -Seconds 5
}

throw 'CTF platform services did not become healthy before the timeout.'
