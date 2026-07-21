[CmdletBinding()]
param(
    [string]$Distribution = 'Ubuntu-24.04',
    [string]$InstallRoot = 'F:\WSL\Ubuntu-24.04',
    [string]$BackupRoot = 'F:\CTF-Agent-Backups',
    [string]$Memory = '10GB',
    [int]$Processors = 12,
    [string]$Swap = '4GB'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$distributions = @(wsl.exe --list --quiet) -replace "`0", '' | ForEach-Object { $_.Trim() }
if ($Distribution -notin $distributions) {
    throw "WSL distribution not found: $Distribution"
}

$vhdx = Join-Path $InstallRoot 'ext4.vhdx'
if (-not (Test-Path -LiteralPath $vhdx -PathType Leaf)) {
    throw "Expected VHDX not found: $vhdx"
}

New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
$swapFile = 'F:\\WSL\\wsl-swap.vhdx'
$config = @"
[wsl2]
memory=$Memory
processors=$Processors
swap=$Swap
swapFile=$swapFile
localhostForwarding=true
autoProxy=false
dnsTunneling=true

[experimental]
autoMemoryReclaim=gradual
sparseVhd=true
"@

$configPath = Join-Path $HOME '.wslconfig'
[System.IO.File]::WriteAllText($configPath, $config.TrimStart(), [System.Text.Encoding]::ASCII)
wsl.exe --set-default $Distribution
wsl.exe --shutdown

Write-Host "Configured $Distribution"
Write-Host "VHDX: $vhdx"
Write-Host "Backups: $BackupRoot"
Write-Host "Resources: memory=$Memory processors=$Processors swap=$Swap"
