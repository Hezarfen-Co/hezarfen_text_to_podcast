[CmdletBinding()]
param(
  [string]$Image = "localhost/hezarfen-podcast",
  [string]$Tag,
  [switch]$List
)
$ErrorActionPreference = "Stop"

function Invoke-Quiet {
  param([Parameter(Mandatory)][string]$Command, [string[]]$Arg = @())
  $previousEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $Command @Arg *> $null } finally { $ErrorActionPreference = $previousEAP }
  return $LASTEXITCODE
}


function Find-Podman {
  $c = (Get-Command podman -ErrorAction SilentlyContinue).Source
  if ($c) { return $c }
  foreach ($p in @("$env:LOCALAPPDATA\Programs\Podman\podman.exe",
                   "$env:ProgramFiles\RedHat\Podman\podman.exe")) {
    if (Test-Path $p) { return $p }
  }
  throw "podman.exe bulunamadi."
}
$PODMAN  = Find-Podman
$podcast = Split-Path -Parent $PSScriptRoot

$lines = & $PODMAN images $Image --format "{{.Tag}}`t{{.ID}}`t{{.CreatedSince}}" 2>$null
if (-not $lines) { throw "Hic '$Image' imaji yok. Once: pwsh -File deploy/run-stack.ps1" }

$records = @()
foreach ($s in $lines) {
  $p = $s -split "`t"
  if ($p.Count -ge 2) {
    $records += [pscustomobject]@{ Tag = $p[0]; Id = $p[1]; Age = $p[2] }
  }
}
$currentId = ($records | Where-Object { $_.Tag -eq "current" } | Select-Object -First 1).Id
$versions = $records | Where-Object { $_.Tag -match '^\d{8}-\d{6}$' } |
            Sort-Object Tag -Descending

if ($List -or -not $versions) {
  Write-Host "`n$Image etiketleri (yeni -> eski):"
  foreach ($k in $versions) {
    $marker = if ($k.Id -eq $currentId) { " <- :current" } else { "" }
    Write-Host ("  {0}  {1}  {2}{3}" -f $k.Tag, $k.Id, $k.Age, $marker)
  }
  if (-not $versions) { Write-Host "  (zaman damgali etiket yok - run-stack.ps1 ile build edilmemis)" }
  if ($List) { return }
  throw "Donulecek etiket yok."
}

if ($Tag) {
  $target = $versions | Where-Object { $_.Tag -eq $Tag } | Select-Object -First 1
  if (-not $target) { throw "Boyle bir etiket yok: $Tag  (-List ile bak)" }
} else {
  $index = 0
  for ($i = 0; $i -lt $versions.Count; $i++) {
    if ($versions[$i].Id -eq $currentId) { $index = $i; break }
  }
  if ($index + 1 -ge $versions.Count) {
    throw "Bir onceki surum yok (yalnizca $($versions.Count) etiket var). -List ile bak."
  }
  $target = $versions[$index + 1]
}

Write-Host "[rollback] :current  $currentId -> $($target.Tag) ($($target.Id), $($target.Age))"
& $PODMAN tag "$($Image):$($target.Tag)" "$($Image):current"
if ($LASTEXITCODE -ne 0) { throw "podman tag basarisiz." }

if (-not $env:AI_SHARED_TOKEN) {
  $envFile = Join-Path $podcast ".env"
  if (-not (Test-Path -LiteralPath $envFile) -or
      -not ((Get-Content -LiteralPath $envFile -Raw) -match '(?m)^\s*AI_SHARED_TOKEN\s*=\s*\S')) {
    throw "AI_SHARED_TOKEN yok. `$env:AI_SHARED_TOKEN='...' ver ya da .env doldur (backend ile AYNI deger)."
  }
}

$null = Invoke-Quiet $PODMAN @('inspect','hezarfen-backend','--format','{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}')
if ($LASTEXITCODE -eq 0) {
  $network = (& $PODMAN inspect hezarfen-backend --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>$null | Select-Object -First 1)
  if ($network) { $env:HEZARFEN_NET = $network.Trim() }
  $_raw = & $PODMAN inspect hezarfen-backend --format json 2>$null
  $_mounts = @()
  if ($_raw) { try { $_mounts = ($_raw | ConvertFrom-Json)[0].Mounts } catch { $_mounts = @() } }
  $volume = ($_mounts | Where-Object { $_.Destination -eq '/data' } |
             Select-Object -First 1 -ExpandProperty Name -ErrorAction SilentlyContinue)
  if ($volume) { $env:HEZARFEN_DATA_VOLUME = $volume.Trim() }
  Write-Host "[rollback] ag=$env:HEZARFEN_NET  hacim=$env:HEZARFEN_DATA_VOLUME"
} else {
  Write-Host "[rollback] UYARI: hezarfen-backend calismiyor; compose varsayilan adlari kullanilacak."
}

Push-Location $podcast
try {
  & $PODMAN compose --profile product up -d --force-recreate --no-build
  if ($LASTEXITCODE -ne 0) { throw "compose up basarisiz." }
} finally { Pop-Location }

Start-Sleep -Seconds 5
& $PODMAN ps --format "{{.Names}}`t{{.Status}}`t{{.Image}}" | Where-Object { $_ -match "podcast" }
Write-Host "`n[rollback] TAMAM. Ileri almak icin: pwsh -File deploy/rollback.ps1 -Tag <yeni-etiket>"
