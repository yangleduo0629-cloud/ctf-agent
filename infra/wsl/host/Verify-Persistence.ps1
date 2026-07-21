[CmdletBinding()]
param([string]$Distribution = 'Ubuntu-24.04')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$wslExe = Join-Path $env:SystemRoot 'System32\wsl.exe'
$token = "wsl-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
$root = '/srv/ctf-platform/app/infra/wsl/scripts'

& $wslExe -d $Distribution -u root -- bash -lc "$root/prepare-restore-fixture.sh '$token'"
if ($LASTEXITCODE -ne 0) { throw 'Fixture creation failed.' }

& $wslExe --shutdown
Start-Sleep -Seconds 5
& $wslExe -d $Distribution -- true
if ($LASTEXITCODE -ne 0) { throw 'Distribution restart failed.' }

& $wslExe -d $Distribution -u root -- bash -lc "for i in {1..60}; do $root/health-check.sh --skip-gpu && exit 0; sleep 5; done; exit 1"
if ($LASTEXITCODE -ne 0) { throw 'Services did not recover after WSL restart.' }
& $wslExe -d $Distribution -u root -- bash -lc "$root/verify-fixture.sh '$token'"
if ($LASTEXITCODE -ne 0) { throw 'Persistence fixture verification failed.' }

Write-Host "Persistence verification passed: $token"
