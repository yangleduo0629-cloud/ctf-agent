[CmdletBinding()]
param(
    [int]$SourcePort = 7890,
    [int]$ListenPort = 17890
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$adapter = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object InterfaceAlias -Match '^vEthernet \(WSL' |
    Select-Object -First 1
if (-not $adapter) {
    throw 'WSL virtual network adapter was not found.'
}

$existing = Get-NetTCPConnection -State Listen -LocalAddress $adapter.IPAddress -LocalPort $ListenPort -ErrorAction SilentlyContinue
if (-not $existing) {
    $python = (Get-Command python -ErrorAction Stop).Source
    $relay = Join-Path $PSScriptRoot 'proxy_relay.py'
    $arguments = @(
        ('"{0}"' -f $relay),
        '--listen-host', $adapter.IPAddress,
        '--listen-port', $ListenPort,
        '--target-host', '127.0.0.1',
        '--target-port', $SourcePort
    )
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 1
    if ($process.HasExited) {
        throw 'WSL proxy relay exited during startup.'
    }
}

$proxy = "http://$($adapter.IPAddress):$ListenPort"
Write-Output $proxy
