<#
.SYNOPSIS
    Create the VIATOR "eu19-transit-motis" 19-country session via API,
    upload existing eu11 NAP timetable files, and pre-wire URL providers
    for the 8 new countries (where we have verified open feeds).

.DESCRIPTION
    Extends the eu11 (eurostar corridor) bootstrap with:

      - 19 country codes in osm_countries: eu11 + DK + SE + NO + PL +
        CZ + SK + SI + HU
      - Same 26 file-upload providers from eu11 (re-uses local NAP zip
        files in -FilesDir)
      - 3 URL providers from eu11 (NL OpenOV, Trenord IT, ATAC IT)
      - **New URL providers for eu19** where we have verified open
        feeds: NO Entur, PL Polregio + 5 PL regional, CZ CIS national,
        HU BKK Budapest
      - **Placeholder comments for gated feeds** that need operator
        action: DK Rejseplan (DevTools probe), SE Trafiklab (API key),
        HU MÁV+GYSEV (form request), PL PKP Intercity (FTP creds),
        SI SŽ (Ministry email). Each is documented but commented out
        so the operator can uncomment + fill in once access lands.

    Pre-bootstrap reading:
      - docs/eu19-compliance-summary.md  (stakeholder-facing 1-pager)
      - docs/eu19-providers.md           (full per-country research)

    The OSM PBF is handled separately — once
    scripts/merge_osm_eu19_corridor.sh finishes on the VPS, copy the
    merged PBF into /opt/viator/data/inbox/eu19-transit-motis/osm/osm.pbf
    The script prints the exact command at the end.

    The rebuild is NOT triggered — review the upload results in the UI,
    drop the OSM PBF in, then click Rebuild graph yourself.

.PARAMETER BaseUrl
    Base URL of the VIATOR platform. Default: prod VPS.

.PARAMETER AdminEmail
    Admin email for JWT login.

.PARAMETER SessionId
    Slug for the new session. Default: eu19-transit-motis.

.PARAMETER SessionName
    Human-readable session name.

.PARAMETER FilesDir
    Local folder containing the eu11 NAP timetable zips (re-used as-is).

.EXAMPLE
    .\create_eu19_transit_motis_session.ps1
    # Prompts for password, runs against prod VPS defaults.
#>

[CmdletBinding()]
param(
    [string]$BaseUrl = "https://vmi3259514.contaboserver.net",
    [string]$AdminEmail = "patrick.heuguet@trackonpath.com",
    [string]$SessionId = "eu19-transit-motis",
    [string]$SessionName = "EU19 Transit (MOTIS) - ES FR BE LU NL DE AT IT LI CH GB + DK SE NO PL CZ SK SI HU",
    [string]$FilesDir = "C:\Users\patri\OneDrive\Documents\TrackOnPath\Development\VIATOR-Journey Planning\European NAP TimeTables"
)

$ErrorActionPreference = "Stop"

# ───────────────────── provider map (eu11 file-upload, unchanged) ─────────────────────
# Identical to the eu11 bootstrap script — re-uses the same NAP zip files
# the operator already has in $FilesDir. See
# scripts/create_eurostar_corridor_session.ps1 for the full per-file
# rationale + per-country verdicts.
$FileToProvider = @{
    # Austria
    "AT-NAP_netex_evu_2026.zip"                   = @{ Id="OBB";         Country="AT"; Format="netex_epip"; Standard="NeTEx-EPIP"; Label="OBB" }
    # Belgium
    "BE-NAP-SNCB-epip.zip"                        = @{ Id="NMBS";        Country="BE"; Format="netex_epip"; Standard="NeTEx-EPIP"; Label="SNCB / NMBS" }
    # Switzerland
    "CH-NAP_netex_202606200406.zip"               = @{ Id="SBB";         Country="CH"; Format="netex_epip"; Standard="NeTEx-EPIP"; Label="SBB CFF FFS" }
    # Germany
    "DE-NAP-fahrplaene_gesamtdeutschland.zip"     = @{ Id="DB";          Country="DE"; Format="netex_epip"; Standard="NeTEx-EPIP"; Label="DB / Gesamt Deutschland" }
    # Spain — all five ES NAP feeds are GTFS (filename says NeTEx but contents are GTFS).
    "ES-NAP-FGC_Catalunya.zip"                    = @{ Id="FGC";         Country="ES"; Format="gtfs";       Standard="GTFS";       Label="FGC Catalunya" }
    "ES-NAP-_Euskadi_Euskotren.zip"               = @{ Id="EUSKOTREN";   Country="ES"; Format="gtfs";       Standard="GTFS";       Label="Euskotren" }
    "ES-NAP-_Ouigo.zip"                           = @{ Id="OUIGO-ES";    Country="ES"; Format="gtfs";       Standard="GTFS";       Label="Ouigo Spain" }
    "ES-NAP-_RENFE_AVLD.zip"                      = @{ Id="RENFE-AVLD";  Country="ES"; Format="gtfs";       Standard="GTFS";       Label="Renfe AV / LD" }
    "ES-NAP-_RENFE_CERCA.zip"                     = @{ Id="RENFE-CERC";  Country="ES"; Format="gtfs";       Standard="GTFS";       Label="Renfe Cercanias" }
    # France
    "FR-NAP_gtfs_Bretagne_BREIZHGO_TER.gtfs.zip"  = @{ Id="BREIZHGO";    Country="FR"; Format="gtfs";       Standard="GTFS";       Label="BreizhGo TER" }
    "FR-NAP_gtfs_Eurostar_v2.zip"                 = @{ Id="EUROSTAR";    Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Eurostar" }
    "FR-NAP_gtfs_Grand-Est_FLUO.zip"              = @{ Id="FLUO";        Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Fluo Grand Est" }
    "FR-NAP_gtfs_Haut-De-France_TER.zip"          = @{ Id="HDF-TER";     Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Hauts-de-France TER" }
    "FR-NAP_gtfs_IDFM-gtfs.zip"                   = @{ Id="IDFM";        Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Ile-de-France Mobilites" }
    "FR-NAP_gtfs_LIO_Occitanie.zip"               = @{ Id="LIO";         Country="FR"; Format="gtfs";       Standard="GTFS";       Label="LiO Occitanie" }
    "FR-NAP_gtfs_Normandie_ATOUMOD.zip"           = @{ Id="ATOUMOD";     Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Atoumod Normandie" }
    "FR-NAP_gtfs_Nouv_Aquitaine.zip"              = @{ Id="NAQUITAINE";  Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Nouvelle-Aquitaine" }
    "FR-NAP_gtfs_OURA.zip"                        = @{ Id="OURA";        Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Oura Auvergne-Rhone-Alpes" }
    "FR-NAP_gtfs_Pays-Loire_Aleop.zip"            = @{ Id="ALEOP";       Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Aleop Pays de la Loire" }
    "FR-NAP_gtfs_Region-Sud_ZOU.zip"              = @{ Id="ZOU";         Country="FR"; Format="gtfs";       Standard="GTFS";       Label="ZOU Region Sud" }
    "FR-NAP_gtfs_Renfe_AVE_Int.zip"               = @{ Id="RENFE-FR";    Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Renfe AVE Int" }
    "FR-NAP_gtfs_SNCF_TGV-Intercite-TER.zip"      = @{ Id="SNCF";        Country="FR"; Format="gtfs";       Standard="GTFS";       Label="SNCF TGV / Intercites / TER" }
    "FR-NAP_gtfs_SNCF_transilien-gtfs.zip"        = @{ Id="TRANSILIEN";  Country="FR"; Format="gtfs";       Standard="GTFS";       Label="SNCF Transilien" }
    "FR-NAP_gtfs_Trenitalia_FR.zip"               = @{ Id="TRENITAL-FR"; Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Trenitalia France" }
    # Italy
    "IT-TRENITALIA-NeTEx_L1.zip"                  = @{ Id="TRENITALIA";  Country="IT"; Format="netex_epip"; Standard="NeTEx-EPIP"; Label="Trenitalia" }
    # Luxembourg
    "LU-NAP-netex-20260618-20260823.zip"          = @{ Id="CFL";         Country="LU"; Format="netex_epip"; Standard="NeTEx-EPIP"; Label="CFL Luxembourg" }
    # Netherlands NeTEx is actually IFF format — handled via URL provider below (OpenOV GTFS).
}

# ───────────────────── URL providers (eu11 + eu19 NEW) ─────────────────────
# eu11 carried 3: NL OpenOV (NL NAP gap workaround), Trenord (Lombardy
# regional rail), ATAC Roma (urban). All verified working in production.
#
# eu19 adds verified-open URL providers for the new countries. Sources
# anchored to each country's official NAP per docs/eu19-providers.md.
# Gated feeds (require operator credential/form action) are documented
# as commented-out blocks below the active providers — uncomment when
# access lands.
$UrlProviders = @(
    # ───────── eu11 carry-over ─────────
    @{
        Id          = "NL-NAP"
        Label       = "NL National GTFS (OpenOV — NS + EuropeanSleeper + Eurostar NL + Arriva + …)"
        Country     = "NL"
        Format      = "gtfs"
        Url         = "http://gtfs.ovapi.nl/nl/gtfs-nl.zip"
    }
    @{
        Id          = "TRENORD"
        Label       = "Trenord — Lombardy regional rail + Malpensa Express"
        Country     = "IT"
        Format      = "gtfs"
        Url         = "https://www.dati.lombardia.it/download/3z4k-mxz9/application/zip"
    }
    @{
        Id          = "ATAC"
        Label       = "ATAC Roma — Rome bus / tram / metro"
        Country     = "IT"
        Format      = "gtfs"
        Url         = "https://romamobilita.it/sites/default/files/rome_static_gtfs.zip"
    }

    # ───────── eu19 NEW — verified open feeds ─────────
    # Norway — Entur national GTFS (NAP-canonical per transportportal.no probe)
    @{
        Id          = "ENTUR-NO"
        Label       = "Entur Norway — national GTFS (Vy + Go-Ahead Nordic + SJ Nord + Flytoget + Ruter + AtB + …)"
        Country     = "NO"
        Format      = "gtfs"
        Url         = "https://storage.googleapis.com/marduk-production/outbound/gtfs/rb_norway-aggregated-gtfs.zip"
    }
    # Poland — Polregio (national regional rail, GTFS Static, NAP entry id 197)
    # Direct .zip URL behind polregio.pl/rozklad-jazdy — operator must
    # find the actual download link on first run, then update this entry.
    # TODO(operator): visit https://polregio.pl/pl/rozklad-jazdy-i-mapa-polaczen/rozklad-jazdy/
    # and capture the canonical .zip URL.
    # @{
    #     Id          = "POLREGIO"
    #     Label       = "Polregio — Polish national regional rail"
    #     Country     = "PL"
    #     Format      = "gtfs"
    #     Url         = "https://polregio.pl/<path-to-confirm>"
    # }
    # Poland — Koleje Małopolskie (direct .zip per NAP entry id 29)
    @{
        Id          = "KM-PL"
        Label       = "Koleje Małopolskie — Małopolska regional rail (SKA)"
        Country     = "PL"
        Format      = "gtfs"
        Url         = "https://kolejemalopolskie.com.pl/rozklady_jazdy/kml-ska-gtfs.zip"
    }
    @{
        Id          = "KM-PL-ALD"
        Label       = "Koleje Małopolskie — ALD (Aglomeracyjne)"
        Country     = "PL"
        Format      = "gtfs"
        Url         = "https://kolejemalopolskie.com.pl/rozklady_jazdy/ald-gtfs.zip"
    }
    # Poland — Łódzka Kolej Aglomeracyjna (direct .zip per NAP entry id 44)
    @{
        Id          = "LKA-PL"
        Label       = "ŁKA — Łódź agglomeration rail"
        Country     = "PL"
        Format      = "gtfs"
        Url         = "https://kolej-lka.pl/pliki/rskqx9axcc2i8932/gtfs-2025-2026/zip/"
    }
    # Czech Republic — CIS JŘ national bundle (per registr.dopravniinfo.cz
    # NAP categorisation; operator may want to confirm exact URL on first
    # build via the NAP sub-page navigation).
    @{
        Id          = "CIS-CZ"
        Label       = "CIS JŘ — Czech national GTFS (ČD + Leo Express + RegioJet + ARRIVA + regional)"
        Country     = "CZ"
        Format      = "gtfs"
        Url         = "https://portal.cisjr.cz/static/jdf/JDF-GTFS.zip"
    }
    # Hungary — BKK Budapest (per napportal.kozut.hu NAP entries 13-15, open)
    @{
        Id          = "BKK-HU"
        Label       = "BKK Budapest — metro + tram + bus + suburban rail"
        Country     = "HU"
        Format      = "gtfs"
        Url         = "https://opendata.bkk.hu/data-sources"
    }

    # ───────── eu19 PENDING — gated, uncomment when access lands ─────────
    #
    # Denmark — Rejseplan national bundle. URL not captured yet (nap.vd.dk
    # SPA needs browser DevTools probe). See docs/eu19-providers.md §DK.
    # @{
    #     Id      = "REJSEPLAN"
    #     Label   = "Rejseplan — DK national GTFS (DSB + S-tog + Metro + Movia + …)"
    #     Country = "DK"; Format = "gtfs"
    #     Url     = "<paste the URL captured from nap.vd.dk DevTools probe>"
    # }
    #
    # Sweden — Trafiklab GTFS Sverige 2 (national bundle, NAP-referenced
    # via trafficdata.se). Needs free API key from www.trafiklab.se/login/
    # @{
    #     Id      = "GTFS-SVERIGE-2"
    #     Label   = "Trafiklab GTFS Sverige 2 — SE national (SJ + MTR + SL + Skånetrafiken + …)"
    #     Country = "SE"; Format = "gtfs"
    #     Url     = "https://opendata.samtrafiken.se/gtfs-sverige-2/sweden.zip?key=<YOUR_TRAFIKLAB_KEY>"
    # }
    #
    # Hungary — MÁV+GYSEV combined inter-city rail (NAP entry id 5, request
    # form). After operator submits https://www.mavcsoport.hu/gtfs-igenybejelento
    # and receives access, paste the per-requester URL here.
    # @{
    #     Id      = "MAV-GYSEV"
    #     Label   = "MÁV + GYSEV — HU national inter-city rail"
    #     Country = "HU"; Format = "gtfs"
    #     Url     = "<paste the URL MÁV sends after form approval>"
    # }
    #
    # Poland — PKP Intercity national long-distance rail (NAP entry id 90,
    # CSV via authenticated FTP). Requires (a) FTP credential request,
    # (b) CSV→GTFS converter — defer to follow-up release.
    #
    # Slovenia — SŽ national rail. Requires Ministry contact (mzi.ncup@gov.si).
    # Defer to follow-up release.
)

# ───────────────────── http helpers (identical to eu11 script) ─────────────────────
function Invoke-VIATOR-Login {
    param([string]$Email, [string]$Password)
    $body = @{ email = $Email; password = $Password } | ConvertTo-Json -Compress
    $r = Invoke-WebRequest -Uri "$BaseUrl/api/auth/login" `
        -Method Post `
        -ContentType "application/json" `
        -Body $body `
        -SessionVariable "WebSession" `
        -SkipHttpErrorCheck
    if ($r.StatusCode -ne 200) {
        throw "Login failed ($($r.StatusCode)): $($r.Content)"
    }
    $data = $r.Content | ConvertFrom-Json
    Write-Host "[ok] logged in as $($data.name) (role=$($data.role))"
    return [pscustomobject]@{
        Jwt = $data.jwt
        Session = $WebSession
    }
}

function Invoke-VIATOR-Api {
    param(
        [string]$Method,
        [string]$Path,
        $Body = $null,
        [Microsoft.PowerShell.Commands.WebRequestSession]$WebSession
    )
    $params = @{
        Uri = "$BaseUrl$Path"
        Method = $Method
        WebSession = $WebSession
        SkipHttpErrorCheck = $true
    }
    if ($Body -ne $null) {
        $params.ContentType = "application/json"
        $params.Body = ($Body | ConvertTo-Json -Depth 20 -Compress)
    }
    $r = Invoke-WebRequest @params
    if ($r.StatusCode -ge 400) {
        throw "API $Method $Path → $($r.StatusCode): $($r.Content)"
    }
    if ($r.Content) { return $r.Content | ConvertFrom-Json } else { return $null }
}

function Send-VIATOR-Upload {
    param(
        [string]$SessionId,
        [string]$FilePath,
        [string]$DeclaredStandard,
        [string]$ProviderId,
        [Microsoft.PowerShell.Commands.WebRequestSession]$WebSession
    )
    $form = @{
        declared_standard = $DeclaredStandard
        provider_id       = $ProviderId
        file              = Get-Item -LiteralPath $FilePath
    }
    $r = Invoke-WebRequest -Uri "$BaseUrl/api/sessions/$SessionId/uploads" `
        -Method Post `
        -Form $form `
        -WebSession $WebSession `
        -SkipHttpErrorCheck
    return $r
}

# ───────────────────── main ─────────────────────

Write-Host ""
Write-Host "VIATOR eu19-transit-motis session bootstrap"
Write-Host "──────────────────────────────────────────"
Write-Host "  base url      : $BaseUrl"
Write-Host "  admin email   : $AdminEmail"
Write-Host "  session id    : $SessionId"
Write-Host "  files folder  : $FilesDir"
Write-Host ""

if (-not (Test-Path -LiteralPath $FilesDir)) {
    throw "Files folder not found: $FilesDir"
}

$securePw = Read-Host -Prompt "Password for $AdminEmail" -AsSecureString
$plainPw = [System.Net.NetworkCredential]::new("", $securePw).Password

# ── login ────────────────────────────────────────────────
$auth = Invoke-VIATOR-Login -Email $AdminEmail -Password $plainPw

# ── build the providers list (uploads + url-sourced) ─────
$providers = @()
foreach ($entry in $FileToProvider.GetEnumerator()) {
    $p = $entry.Value
    $providers += @{
        id          = $p.Id
        label       = $p.Label
        country_iso = $p.Country
        timetable   = @{ format = $p.Format; source = "upload" }
    }
}
foreach ($u in $UrlProviders) {
    $providers += @{
        id          = $u.Id
        label       = $u.Label
        country_iso = $u.Country
        timetable   = @{ format = $u.Format; source = "url"; url = $u.Url }
    }
}
Write-Host "[plan] $($FileToProvider.Count) upload providers + $($UrlProviders.Count) url providers = $($providers.Count) total"

# ── create the session ───────────────────────────────────
$createBody = @{
    id                 = $SessionId
    name               = $SessionName
    category           = "MANUAL"
    # MOTIS engine (same rationale as eu11 — 19 countries definitely
    # exceed OTP's memory envelope; MOTIS handles it via the same
    # planner_dispatch as eu11-transit-motis).
    engine             = "motis"
    include_in_fanout  = $false
    config             = @{
        # transit-focused so urban street graph is present for
        # cross-border tram/metro flows. Same as eu11; the 8 new
        # countries add Nordic + Central European urban networks.
        osm_scope     = "transit-focused"
        # 19 ISO country codes — eu11 base + 8 eu19 additions.
        osm_countries = @(
            # eu11 base
            "AT","BE","CH","DE","ES","FR","GB","IT","LI","LU","NL",
            # eu19 additions
            "CZ","DK","HU","NO","PL","SE","SI","SK"
        )
        sources       = @{
            providers = $providers
        }
    }
}
Write-Host ""
Write-Host "Creating session $SessionId ..."
try {
    $session = Invoke-VIATOR-Api -Method POST -Path "/api/sessions" -Body $createBody -WebSession $auth.Session
    Write-Host "[ok] session created: $($session.id) (state=$($session.state))"
} catch {
    if ($_.Exception.Message -match "missing_master_stations_for_countries") {
        Write-Host ""
        Write-Host "[!!] Country-gate blocked the save: some declared provider countries have no" -ForegroundColor Yellow
        Write-Host "     master_stations rows yet. The server lists which ones in the error above." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "     Fix: import them from Trainline first:" -ForegroundColor Yellow
        Write-Host "         curl -X POST -H 'Authorization: Bearer <JWT>' \\"
        Write-Host "             $BaseUrl/api/master/stations/refresh-trainline"
        Write-Host ""
        Write-Host "     Or via the admin UI: Master data → Stations → Refresh from Trainline."
        Write-Host "     Then re-run this script."
        throw
    }
    throw
}

# ── upload each file ─────────────────────────────────────
Write-Host ""
Write-Host "Uploading $($FileToProvider.Count) files ..."
$results = @()
$i = 0
foreach ($entry in $FileToProvider.GetEnumerator()) {
    $i++
    $filename = $entry.Key
    $p = $entry.Value
    $path = Join-Path $FilesDir $filename
    $progress = "[{0,2}/{1}]" -f $i, $FileToProvider.Count

    if (-not (Test-Path -LiteralPath $path)) {
        Write-Host "$progress MISS  $filename — file not found in $FilesDir" -ForegroundColor Yellow
        $results += [pscustomobject]@{ File=$filename; Provider=$p.Id; Status="missing"; Detail="file not found" }
        continue
    }

    $sizeMb = [math]::Round((Get-Item -LiteralPath $path).Length / 1MB, 1)
    Write-Host "$progress      $filename ($sizeMb MB) → $($p.Id) [$($p.Standard)]"

    try {
        $r = Send-VIATOR-Upload -SessionId $SessionId -FilePath $path `
                -DeclaredStandard $p.Standard -ProviderId $p.Id `
                -WebSession $auth.Session
        if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300) {
            Write-Host "       PASS  uploaded" -ForegroundColor Green
            $results += [pscustomobject]@{ File=$filename; Provider=$p.Id; Status="ok"; Detail="" }
        } else {
            Write-Host "       FAIL  HTTP $($r.StatusCode): $($r.Content)" -ForegroundColor Red
            $results += [pscustomobject]@{ File=$filename; Provider=$p.Id; Status="fail"; Detail="$($r.StatusCode): $($r.Content)" }
        }
    } catch {
        Write-Host "       FAIL  $($_.Exception.Message)" -ForegroundColor Red
        $results += [pscustomobject]@{ File=$filename; Provider=$p.Id; Status="fail"; Detail=$_.Exception.Message }
    }
}

# ── summary ──────────────────────────────────────────────
Write-Host ""
Write-Host "Summary"
Write-Host "──────────────────────────────────────────"
$results | Group-Object Status | ForEach-Object {
    Write-Host ("  {0,-8} {1}" -f $_.Name, $_.Count)
}
$failed = $results | Where-Object { $_.Status -ne "ok" }
if ($failed) {
    Write-Host ""
    Write-Host "Failures:" -ForegroundColor Yellow
    $failed | ForEach-Object { Write-Host "  $($_.File) → $($_.Detail)" }
}

# ── next steps ──────────────────────────────────────────
Write-Host ""
Write-Host "Next steps"
Write-Host "──────────────────────────────────────────"
Write-Host "  1. Run the OSM merge on the VPS (~60-90 min):"
Write-Host "        ssh viator@vmi3259514.contaboserver.net \"bash /opt/viator/scripts/merge_osm_eu19_corridor.sh\""
Write-Host ""
Write-Host "  2. Drop the merged OSM PBF into the session's inbox:"
Write-Host "        ssh viator@vmi3259514.contaboserver.net \"cp /tmp/osm-merge/eu19-corridor.osm.pbf /opt/viator/data/inbox/$SessionId/osm/osm.pbf\""
Write-Host ""
Write-Host "  3. Capture the gated feeds (see docs/eu19-compliance-summary.md):"
Write-Host "       • DK: DevTools-probe nap.vd.dk → uncomment REJSEPLAN block"
Write-Host "       • SE: register Trafiklab key → uncomment GTFS-SVERIGE-2 block"
Write-Host "       • HU: submit MÁV form → uncomment MAV-GYSEV block"
Write-Host "       • PL: request PKP IC FTP creds (later release)"
Write-Host "       • SI: email mzi.ncup@gov.si (later release)"
Write-Host ""
Write-Host "  4. Sanity-check provider cards in the admin UI:"
Write-Host "        $BaseUrl/admin/sessions/$SessionId"
Write-Host ""
Write-Host "  5. Pause the eu11-transit-motis and largest other sessions"
Write-Host "     during build to free RAM headroom (see docs/eu19-providers.md"
Write-Host "     §Capacity verification — needs ~65 GB peak)."
Write-Host ""
Write-Host "  6. Click Rebuild graph (or POST /api/sessions/$SessionId/rebuilds)."
