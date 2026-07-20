param(
    [Parameter(Mandatory = $true)]
    [string]$Repository,
    [string]$Token = $env:GITHUB_TOKEN
)

$ErrorActionPreference = "Stop"
if (-not $Token) {
    throw "Set GITHUB_TOKEN or pass -Token. The token needs repository administration permission."
}
if ($Repository -notmatch '^[^/]+/[^/]+$') {
    throw "Repository must use owner/name format."
}

$headers = @{
    Accept = "application/vnd.github+json"
    Authorization = "Bearer $Token"
    "X-GitHub-Api-Version" = "2022-11-28"
}
$body = Get-Content -Raw "$PSScriptRoot/../.github/rulesets/main-protection.json"
$collectionUri = "https://api.github.com/repos/$Repository/rulesets"
$name = (ConvertFrom-Json $body).name
$existing = Invoke-RestMethod -Method Get -Uri $collectionUri -Headers $headers
$current = $existing | Where-Object { $_.name -eq $name } | Select-Object -First 1
if ($current) {
    $uri = "$collectionUri/$($current.id)"
    $result = Invoke-RestMethod -Method Put -Uri $uri -Headers $headers -Body $body -ContentType "application/json"
    Write-Output "Updated ruleset '$($result.name)' (id $($result.id)) on $Repository"
} else {
    $result = Invoke-RestMethod -Method Post -Uri $collectionUri -Headers $headers -Body $body -ContentType "application/json"
    Write-Output "Created ruleset '$($result.name)' (id $($result.id)) on $Repository"
}
