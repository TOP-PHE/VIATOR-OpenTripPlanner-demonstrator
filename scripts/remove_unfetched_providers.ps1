#Requires -Version 7.5

<#
.SYNOPSIS
    Remove providers that were configured but never downloaded.

.DESCRIPTION
    A bulk "Import from NAP" can add hundreds of providers (e.g. every French
    urban bus network) that are never refreshed. They take no part in a build
    (the build only reads files present in the inbox), but the session-wide
    "Refresh providers" button would download all of them, and they clutter
    every list in the admin page.

    This removes exactly the providers that are:
      - source "url" (not nap / upload / derived), AND
      - "pending" or "error" in /providers/status, i.e. no file in the inbox
        (never fetched, or only failed attempts).
    Every provider that has a file (fresh or stale) is kept, so no build loses
    data. Optional -LabelLike narrows it further (e.g. "Réseau urbain*"), and
    -Keep lists provider ids that are never removed.

    Dry run by default: lists what would go and writes nothing. -Apply saves
    the session config (same PATCH and validation as "Save config").

.EXAMPLE
    .\remove_unfetched_providers.ps1                         # dry run, eu19
    .\remove_unfetched_providers.ps1 -Apply
    .\remove_unfetched_providers.ps1 -LabelLike "Réseau urbain*" -Apply
    .\remove_unfetched_providers.ps1 -Keep RGIONHAUTS-DE-FR -Apply
#>

[CmdletBinding()]
param(
    [string]$BaseUrl = "https://vmi3259514.contaboserver.net",
    [string]$AdminEmail = "patrick.heuguet@trackonpath.com",
    [string]$SessionId = "eu19-transit-motis",
    [string]$LabelLike = "*",
    [string[]]$Keep = @(),
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$securePw = Read-Host -Prompt "Password for $AdminEmail" -AsSecureString
$plainPw = [System.Net.NetworkCredential]::new("", $securePw).Password
$login = Invoke-WebRequest -Uri "$BaseUrl/api/auth/login" -Method Post -ContentType "application/json" `
    -Body (@{ email = $AdminEmail; password = $plainPw } | ConvertTo-Json -Compress) `
    -SessionVariable "web" -SkipHttpErrorCheck
if ($login.StatusCode -ne 200) { throw "Login failed ($($login.StatusCode)): $($login.Content)" }

# -DateKind String: keep config._meta timestamps byte-for-byte on the PATCH.
$r = Invoke-WebRequest -Uri "$BaseUrl/api/sessions" -WebSession $web -SkipHttpErrorCheck
if ($r.StatusCode -ne 200) { throw "GET /api/sessions -> $($r.StatusCode): $($r.Content)" }
$session = @($r.Content | ConvertFrom-Json -AsHashtable -DateKind String) | Where-Object { $_.id -eq $SessionId }
if (-not $session) { throw "Session $SessionId not found" }
$config = $session.config
$providers = @($config.sources.providers)

$r = Invoke-WebRequest -Uri "$BaseUrl/api/sessions/$SessionId/providers/status" -WebSession $web -SkipHttpErrorCheck
if ($r.StatusCode -ne 200) { throw "GET providers/status -> $($r.StatusCode): $($r.Content)" }
$status = $r.Content | ConvertFrom-Json -AsHashtable -DateKind String

$remove = @($providers | Where-Object {
    $s = $status[$_.id]
    $src = if ($_.timetable.source) { $_.timetable.source } else { "url" }
    $s -and $s.state -in @("pending", "error") -and $src -eq "url" -and ("$($_.label)" -like $LabelLike) -and
        $_.id -notin $Keep
})
$keep = @($providers | Where-Object { $_.id -notin @($remove | ForEach-Object { $_.id }) })

foreach ($id in $Keep) {
    if ($id -notin @($providers | ForEach-Object { $_.id })) {
        Write-Host "  warning: -Keep $id is not a provider of $SessionId (check the spelling)" -ForegroundColor Yellow
    }
}
$remove | Sort-Object { $_.country_iso }, { $_.id } |
    ForEach-Object { Write-Host ("  remove  {0,-3} {1,-20} {2}" -f $_.country_iso, $_.id, $_.label) }
Write-Host "[plan] remove $($remove.Count) URL providers with no file; keep $($keep.Count) of $($providers.Count)"

if (-not $Apply) {
    Write-Host "Dry run - nothing written. Re-run with -Apply to save." -ForegroundColor Cyan
    return
}
if ($remove.Count -eq 0) { return }

$config.sources.providers = $keep
$body = @{ config = $config } | ConvertTo-Json -Depth 30 -Compress
$r = Invoke-WebRequest -Uri "$BaseUrl/api/sessions/$SessionId" -Method Patch -ContentType "application/json" `
    -Body $body -WebSession $web -SkipHttpErrorCheck
if ($r.StatusCode -ge 400) { throw "PATCH session $SessionId -> $($r.StatusCode): $($r.Content)" }
Write-Host "[ok] removed $($remove.Count) providers. Reload the admin page to see the shorter list."
