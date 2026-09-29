<#
.SYNOPSIS
    Switch a session's hand-uploaded NAP providers to automated sources.

.DESCRIPTION
    Reads scripts/eu19_nap_sources.json (provider id -> timetable object) and
    replaces the `timetable` of every matching provider in the session's
    config. Providers not in the file are left untouched (AT OBB, BE SNCB,
    OUIGO-ES stay on manual upload - see docs/nap-feed-resolvers.md).

    Dry run by default: prints what would change and writes nothing. Pass
    -Apply to PATCH the session config. Nothing is downloaded here - click
    "Refresh sources" (or per-provider Refresh) afterwards; each file already
    in the inbox stays live until its replacement downloads and validates.

.EXAMPLE
    .\switch_to_nap_sources.ps1                       # dry run against eu19-transit-motis
    .\switch_to_nap_sources.ps1 -Apply
    .\switch_to_nap_sources.ps1 -SessionId eu11-transit-motis -Apply
#>

[CmdletBinding()]
param(
    [string]$BaseUrl = "https://vmi3259514.contaboserver.net",
    [string]$AdminEmail = "patrick.heuguet@trackonpath.com",
    [string]$SessionId = "eu19-transit-motis",
    [string]$SourcesFile = (Join-Path $PSScriptRoot "eu19_nap_sources.json"),
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

$sources = Get-Content -LiteralPath $SourcesFile -Raw | ConvertFrom-Json -AsHashtable
$sources.Remove("_comment")

$securePw = Read-Host -Prompt "Password for $AdminEmail" -AsSecureString
$plainPw = [System.Net.NetworkCredential]::new("", $securePw).Password
$login = Invoke-WebRequest -Uri "$BaseUrl/api/auth/login" -Method Post -ContentType "application/json" `
    -Body (@{ email = $AdminEmail; password = $plainPw } | ConvertTo-Json -Compress) `
    -SessionVariable "web" -SkipHttpErrorCheck
if ($login.StatusCode -ne 200) { throw "Login failed ($($login.StatusCode)): $($login.Content)" }

$r = Invoke-WebRequest -Uri "$BaseUrl/api/sessions" -WebSession $web -SkipHttpErrorCheck
if ($r.StatusCode -ne 200) { throw "GET /api/sessions -> $($r.StatusCode): $($r.Content)" }
$session = @($r.Content | ConvertFrom-Json -AsHashtable) | Where-Object { $_.id -eq $SessionId }
if (-not $session) { throw "Session $SessionId not found" }
$config = $session.config
$providers = $config.sources.providers

$changed = 0
foreach ($p in $providers) {
    if (-not $sources.ContainsKey($p.id)) { continue }
    $new = $sources[$p.id]
    $old = $p.timetable
    if ($old.format -ne $new.format) {
        Write-Host ("  SKIP  {0,-12} format differs (session {1}, file {2}) - check by hand" -f $p.id, $old.format, $new.format) -ForegroundColor Yellow
        continue
    }
    $target = if ($new.source -eq "nap") { "nap/$($new.resolver.type)" } else { "url" }
    Write-Host ("  {0,-12} {1,-7} -> {2}" -f $p.id, $old.source, $target)
    $p.timetable = $new
    $changed++
}
$missing = @($sources.Keys | Where-Object { $_ -notin @($providers | ForEach-Object { $_.id }) })
if ($missing) { Write-Host "  not in this session: $($missing -join ', ')" -ForegroundColor DarkGray }
Write-Host "[plan] $changed of $($providers.Count) providers change"

if (-not $Apply) {
    Write-Host "Dry run - nothing written. Re-run with -Apply to save." -ForegroundColor Cyan
    return
}
if ($changed -eq 0) { return }

$body = @{ config = $config } | ConvertTo-Json -Depth 30 -Compress
$r = Invoke-WebRequest -Uri "$BaseUrl/api/sessions/$SessionId" -Method Patch -ContentType "application/json" `
    -Body $body -WebSession $web -SkipHttpErrorCheck
if ($r.StatusCode -ge 400) { throw "PATCH session $SessionId -> $($r.StatusCode): $($r.Content)" }
Write-Host "[ok] session config saved. Next: Refresh sources in the admin UI, review the result, then rebuild."
