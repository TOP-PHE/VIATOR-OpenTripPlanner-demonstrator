<#
.SYNOPSIS
    Create a VIATOR "Eurostar corridor" multi-country session via API and
    upload all the European NAP timetable files in one shot.

.DESCRIPTION
    End-to-end automation for the multi-country session that would otherwise
    require ~28 clicks through the UI:

      1. Logs into VIATOR (POST /api/auth/login) with email+password.
      2. Creates the session via POST /api/sessions with:
           - osm_countries  = ES,FR,BE,LU,NL,DE,AT,IT,LI,CH,GB
           - osm_scope      = rail-focused
           - sources.providers = 27 providers (all source=upload)
      3. For each local file in -FilesDir, POSTs multipart to
         /api/sessions/<sid>/uploads with declared_standard + provider_id,
         attaching the upload to the matching provider's slot.
      4. Prints a per-file PASS/FAIL summary.

    The OSM PBF is handled separately — once
    scripts/merge_osm_eurostar_corridor.sh finishes on the VPS, copy the
    merged PBF into /opt/viator/data/inbox/<sid>/osm/osm.pbf manually.
    The script prints the exact command at the end.

    The rebuild is NOT triggered — review the upload results in the UI,
    drop the OSM PBF in, then click Rebuild graph yourself.

.PARAMETER BaseUrl
    Base URL of the VIATOR platform. Default: prod VPS.

.PARAMETER AdminEmail
    Admin email for JWT login.

.PARAMETER SessionId
    Slug for the new session (^[a-z][a-z0-9-]+$). Default: eurostar-corridor.

.PARAMETER SessionName
    Human-readable session name. Default: "Eurostar Corridor".

.PARAMETER FilesDir
    Local folder containing the NAP timetable zips. Each file is matched
    to a provider by filename (see $FileToProvider map below). Files not
    in the map are skipped with a warning.

.EXAMPLE
    .\create_eurostar_corridor_session.ps1
    # Prompts for password, runs against prod VPS defaults.

.EXAMPLE
    .\create_eurostar_corridor_session.ps1 -SessionId my-corridor -SessionName "My Corridor"
#>

[CmdletBinding()]
param(
    [string]$BaseUrl = "https://vmi3259514.contaboserver.net",
    [string]$AdminEmail = "patrick.heuguet@trackonpath.com",
    [string]$SessionId = "eurostar-corridor",
    [string]$SessionName = "Eurostar Corridor",
    [string]$FilesDir = "C:\Users\patri\OneDrive\Documents\TrackOnPath\Development\VIATOR-Journey Planning\European NAP TimeTables"
)

$ErrorActionPreference = "Stop"

# ───────────────────── provider map ─────────────────────
# Filename → provider definition. The file's prefix is enough to
# disambiguate; the script matches by exact filename (post-Old-files
# filter). To rename a feed_id, edit the `Id` field here.
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
    "FR-NAP_gtfs_Region-Sud_ZOU.zip"              = @{ Id="ZOU";         Country="FR"; Format="gtfs";       Standard="GTFS";       Label="ZOU Region Sud (Intermetropole + TER)" }
    "FR-NAP_gtfs_Renfe_AVE_Int.zip"               = @{ Id="RENFE-FR";    Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Renfe AVE Int" }
    "FR-NAP_gtfs_SNCF_TGV-Intercite-TER.zip"      = @{ Id="SNCF";        Country="FR"; Format="gtfs";       Standard="GTFS";       Label="SNCF TGV / Intercites / TER" }
    "FR-NAP_gtfs_SNCF_transilien-gtfs.zip"        = @{ Id="TRANSILIEN";  Country="FR"; Format="gtfs";       Standard="GTFS";       Label="SNCF Transilien" }
    "FR-NAP_gtfs_Trenitalia_FR.zip"               = @{ Id="TRENITAL-FR"; Country="FR"; Format="gtfs";       Standard="GTFS";       Label="Trenitalia France" }
    # Italy
    "IT-TRENITALIA-NeTEx_L1.zip"                  = @{ Id="TRENITALIA";  Country="IT"; Format="netex_epip"; Standard="NeTEx-EPIP"; Label="Trenitalia" }
    # Luxembourg
    "LU-NAP-netex-20260618-20260823.zip"          = @{ Id="CFL";         Country="LU"; Format="netex_epip"; Standard="NeTEx-EPIP"; Label="CFL Luxembourg" }
    # Netherlands. The NL-NAP_NeTEx file is actually IFF-format (.dat
    # files, not NeTEx) and is NS-only. VIATOR can't ingest IFF, so we
    # skip the local file and pull the multi-operator national GTFS from
    # OpenOV instead — it covers NS + European Sleeper + Eurostar (NL
    # leg) + ICE International + Arriva + Keolis + all NL transit. Wired
    # below as a url-sourced provider, not an upload.
}

# Extra providers that don't have a local file — pulled by URL during
# the platform's "Refresh sources" step. Keep these separate from
# $FileToProvider so the upload loop ignores them.
#
# Italian regional operators: with osm_scope=transit-focused the urban
# street graph IS present, so urban operators (ATAC tram/metro, Trenord
# regional rail, plus the cross-border tram flows like Saint-Louis<->Basel,
# Strasbourg<->Kehl, Aachen<->Vaals) all route correctly. GTT/TPER are
# excluded per operator request.
#
# Italo (NTV) is NOT listed: they're a private operator with no public
# GTFS or NeTEx feed. Data only exposed via their own booking API and
# commercial resellers (Trainline). No file to plug in. Italy's NeTEx
# Italian profile rollout (data4pt-project) may add them later.
$UrlProviders = @(
    @{
        Id          = "NL-NAP"
        Label       = "NL National GTFS (OpenOV — NS + Eurosleeper + Eurostar NL + Arriva + …)"
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
        Label       = "ATAC Roma — Rome bus / tram / metro (urban only; rail-focused scope drops most routing)"
        Country     = "IT"
        Format      = "gtfs"
        Url         = "https://romamobilita.it/sites/default/files/rome_static_gtfs.zip"
    }
    # GTT (Turin / Piedmont) — direct download URL not exposed; visit
    # http://aperto.comune.torino.it/dataset/feed-gtfs-trasporti-gtt and
    # click the "Download" button to get the .zip. License is non-
    # commercial only — VIATOR usage may not qualify; check with GTT
    # before enabling. Once you have the URL, paste it here:
    # @{ Id = "GTT"; Label = "GTT Torino — Piedmont (mostly urban)";
    #    Country = "IT"; Format = "gtfs"; Url = "<paste here>" }
    #
    # TPER (Emilia-Romagna) — direct URL behind their portal at
    # https://solweb.tper.it/web/tools/open-data/. The dataset is
    # `gommagtfsbo` (Bologna bus). Need to grab the direct URL from
    # the portal page. License: CC-BY 3.0 Italy.
    # @{ Id = "TPER"; Label = "TPER Bologna — mostly bus";
    #    Country = "IT"; Format = "gtfs"; Url = "<paste here>" }
)

# ───────────────────── http helpers ─────────────────────
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
    # PowerShell 7+ has -Form for native multipart. Use it.
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
Write-Host "VIATOR Eurostar Corridor session bootstrap"
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
    include_in_fanout  = $false
    config             = @{
        # transit-focused (not rail-focused) — needed so the urban street
        # graph is built for cross-border tram / metro flows like
        # Saint-Louis (FR) <-> Basel (CH) tram, Aachen DE/Vaals NL bus,
        # Geneva tram, Strasbourg Kehl tram, etc. rail-focused strips
        # driving roads which kills walk-to-stop on those urban modes.
        # Memory cost: ~10 GB filtered PBF on 7+ countries; pair with
        # max-memory rebuild mode if heap is tight (§3 of multi-country
        # runbook).
        osm_scope     = "transit-focused"
        osm_countries = @("AT","BE","CH","DE","ES","FR","GB","IT","LI","LU","NL")
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
Write-Host "  1. Drop the merged OSM PBF into the session's inbox (once merge_osm finishes):"
Write-Host "        ssh viator@vmi3259514.contaboserver.net \"cp /tmp/osm-merge/eurostar-corridor.osm.pbf /opt/viator/data/inbox/$SessionId/osm/osm.pbf\""
Write-Host ""
Write-Host "  2. Open the session in the admin UI to sanity-check provider cards:"
Write-Host "        $BaseUrl/admin/sessions/$SessionId"
Write-Host ""
Write-Host "  3. Click Rebuild graph (or POST /api/sessions/$SessionId/rebuilds)."
