[CmdletBinding()]
param(
  [string]$Root = "C:\PROJECTS\podcast",
  [string]$BackendProject = "hezarfen_backend",
  [switch]$Rebuild,
  [switch]$PodcastOnly
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
  throw "podman.exe bulunamadi. winget install RedHat.Podman"
}
$PODMAN  = Find-Podman
$podcast = Split-Path -Parent $PSScriptRoot
Write-Host "[stack] podman  : $PODMAN"
Write-Host "[stack] podcast : $podcast"

if (-not (Test-Path -LiteralPath $Root)) { throw "Arama koku yok: $Root" }
Write-Host "[stack] repolar araniyor: $Root"
$candidates = Get-ChildItem -LiteralPath $Root -Recurse -Depth 3 -File -Filter "compose.yaml" `
                -ErrorAction SilentlyContinue |
              Where-Object { $_.FullName -notmatch '\\(\.venv|node_modules|vendor|out|target)\\' }

function Find-Repo([string]$signature, [string]$label) {
  $found = @()
  foreach ($c in $candidates) {
    if ($c.DirectoryName -eq $podcast) { continue }
    if ((Get-Content -LiteralPath $c.FullName -Raw) -match $signature) { $found += $c.DirectoryName }
  }
  $found = @($found | Sort-Object -Unique)
  if ($found.Count -eq 0) {
    throw "$label reposu bulunamadi ($Root altinda '$signature' iceren compose.yaml yok). -Root ile arama kokunu ver."
  }
  if ($found.Count -gt 1) {
    throw "$label icin BIRDEN COK aday bulundu; hangisi oldugu belirsiz:`n  " + ($found -join "`n  ")
  }
  Write-Host ("[stack] {0,-9}: {1}" -f $label, $found[0])
  return $found[0]
}

if (-not $PodcastOnly) {
  $backend  = Find-Repo 'container_name:\s*hezarfen-backend'        "backend"
  $frontend = Find-Repo 'container_name:\s*hezarfen-frontend'       "frontend"
  $chatbot  = Find-Repo 'container_name:\s*hezarfen-chatbot-bridge' "chatbot"
}

$vendor = Join-Path $podcast "vendor\pipeline\router\hat.py"
if (-not (Test-Path -LiteralPath $vendor)) {
  throw "vendor/pipeline yok. Once calistir: pwsh -File deploy/setup-pipeline.ps1"
}
if (-not $env:AI_SHARED_TOKEN) {
  $envFile = Join-Path $podcast ".env"
  if (-not (Test-Path -LiteralPath $envFile) -or
      -not ((Get-Content -LiteralPath $envFile -Raw) -match '(?m)^\s*AI_SHARED_TOKEN\s*=\s*\S')) {
    throw "AI_SHARED_TOKEN yok. `$env:AI_SHARED_TOKEN='...' ver ya da .env doldur (backend ile AYNI deger)."
  }
}
$probeFile = "/models/ses_modelleri/supertonic-3/onnx/vocoder.onnx"
$null = Invoke-Quiet $PODMAN @('volume','exists','podcast-models')
if ($LASTEXITCODE -ne 0) {
  Write-Host "[stack] UYARI: 'podcast-models' hacmi YOK. TTS ve CER kapisi CALISMAZ."
  Write-Host "        pwsh -File deploy/setup-models.ps1"
} else {
  & $PODMAN run --rm -v podcast-models:/models:ro docker.io/library/python:3.10-slim `
      test -e $probeFile
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[stack] UYARI: 'podcast-models' hacmi var ama BOS/EKSIK gorunuyor."
    Write-Host "        beklenen: $probeFile"
    Write-Host "        pwsh -File deploy/setup-models.ps1 -Refresh"
  }
}

$previousEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $PODMAN machine start *> $null
$ErrorActionPreference = $previousEAP

if (-not $PodcastOnly) {
  Write-Host "`n[stack] 1/5 base imajlar pre-pull..."
  $bases = @("docker.io/surrealdb/surrealdb:v3")
  foreach ($d in @($backend, $frontend, $chatbot, $podcast)) {
    $cf = Join-Path $d "Containerfile"
    if (Test-Path -LiteralPath $cf) {
      Select-String -LiteralPath $cf -Pattern '^\s*FROM\s+(\S+)' | ForEach-Object {
        $bases += $_.Matches[0].Groups[1].Value
      }
    }
  }
  foreach ($img in ($bases | Where-Object { $_ -notmatch '^\$' } | Sort-Object -Unique)) {
    Write-Host "    pull $img"; Invoke-Quiet $PODMAN @('pull',$img) | Out-Null
  }
}

function Compose-Up([string]$dir, [string[]]$preArgs) {
  Push-Location $dir
  try {
    & $PODMAN compose @preArgs up -d --build
    if ($LASTEXITCODE -ne 0) { throw "compose up basarisiz: $dir" }
  } finally { Pop-Location }
}

if (-not $PodcastOnly) {
  Write-Host "`n[stack] 2/5 backend (-p $BackendProject ; Rust ilk seferde uzun surer)..."
  Compose-Up $backend  @("-p", $BackendProject)
  Write-Host "`n[stack] 3/5 frontend..."
  Compose-Up $frontend @()
  Write-Host "`n[stack] 4/5 chatbot..."
  Compose-Up $chatbot  @()
}

$net = (& $PODMAN inspect hezarfen-backend --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>$null)
$net = ($net -split '\s+' | Where-Object { $_ }) | Select-Object -First 1
$_raw = & $PODMAN inspect hezarfen-backend --format json 2>$null
$_mounts = @()
if ($_raw) { try { $_mounts = ($_raw | ConvertFrom-Json)[0].Mounts } catch { $_mounts = @() } }
$vol = ($_mounts | Where-Object { $_.Destination -eq '/data' } |
        Select-Object -First 1 -ExpandProperty Name -ErrorAction SilentlyContinue)
$vol = ($vol | Out-String).Trim()

if ($net) { $env:HEZARFEN_NET = $net }
else { Write-Host "[stack] UYARI: backend agi okunamadi; compose varsayilani kullanilacak." }
if ($vol) { $env:HEZARFEN_DATA_VOLUME = $vol }
else { Write-Host "[stack] UYARI: backend /data hacmi okunamadi; compose varsayilani kullanilacak." }
Write-Host "`n[stack] backend agi   : $($env:HEZARFEN_NET)"
Write-Host "[stack] backend hacmi : $($env:HEZARFEN_DATA_VOLUME)  (PDF'ler burada, :ro baglanir)"

$image = "localhost/hezarfen-podcast"

$previousId = (& $PODMAN images "${image}:current" --format "{{.ID}}" 2>$null |
               Select-Object -First 1)

Write-Host "`n[stack] 5/5 podcast imaji build ediliyor (${image}:current)"
Push-Location $podcast
try {
  $buildArgs = @("build", "-t", "${image}:current", "-f", "Containerfile", ".")
  if ($Rebuild) { $buildArgs = @("build", "--no-cache") + $buildArgs[1..($buildArgs.Count - 1)] }
  & $PODMAN @buildArgs
  if ($LASTEXITCODE -ne 0) { throw "podcast imaji build edilemedi." }

  $currentId = (& $PODMAN images "${image}:current" --format "{{.ID}}" 2>$null |
                Select-Object -First 1)
  if ($currentId -and $currentId -ne $previousId) {
    $tag = Get-Date -Format "yyyyMMdd-HHmmss"
    & $PODMAN tag "${image}:current" "${image}:$tag"
    if ($LASTEXITCODE -ne 0) { throw "surum etiketi konulamadi." }
    Write-Host "[stack] imaj DEGISTI -> yeni surum etiketi: $tag ($currentId)"
  } else {
    Write-Host "[stack] imaj degismedi ($currentId) - yeni surum etiketi ACILMADI"
  }

  & $PODMAN compose --profile product up -d
  if ($LASTEXITCODE -ne 0) { throw "podcast compose up basarisiz." }
} finally { Pop-Location }

Write-Host "`n[stack] dogrulama..."
Start-Sleep -Seconds 8
& $PODMAN ps --format "{{.Names}}`t{{.Status}}`t{{.Image}}"

foreach ($p in 8080, 5173) {
  try {
    $code = (Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:$p/" -TimeoutSec 8).StatusCode
    Write-Host "    localhost:$p -> HTTP $code"
  } catch {
    Write-Host "    localhost:$p -> ERISILEMEDI ($($_.Exception.Message))"
  }
}

Write-Host "    podcast kaydi bekleniyor (en fazla 30 sn)..."
$registered = $false
for ($i = 0; $i -lt 15; $i++) {
  $log = (& $PODMAN logs --tail 200 hezarfen-podcast-bridge 2>&1 | Out-String)
  if ($log -match "kayit basarili") { $registered = $true; break }
  Start-Sleep -Seconds 2
}
if ($registered) {
  ((& $PODMAN logs --tail 200 hezarfen-podcast-bridge 2>&1 | Out-String) -split "`n" |
    Where-Object { $_ -match "kayit basarili|GERCEK hat bagli|acik formatlar|SAHTE hat" }) |
    ForEach-Object { Write-Host "    $($_.Trim())" }
  Write-Host "`n[stack] HAZIR. Frontend: http://localhost:5173  (admin / admin123)"
  Write-Host "[stack] geri alma  : pwsh -File deploy/rollback.ps1 -List"
} else {
  Write-Host "`n[stack] !!! Podcast koprusu KAYIT OLMADI. Son loglar:"
  & $PODMAN logs --tail 40 hezarfen-podcast-bridge
  Write-Host "`n[stack] sorun giderme: deploy/OKU.md"
  exit 1
}
