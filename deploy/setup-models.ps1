[CmdletBinding()]
param(
  [string]$Volume = "podcast-models",
  [string]$VoiceModels = "C:\PROJECTS\podcast\data\ses_modelleri",
  [string]$HfCache  = "$env:USERPROFILE\.cache\huggingface\hub",
  [string]$HelperImage = "docker.io/library/python:3.10-slim",
  [switch]$Check,
  [switch]$Refresh
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
$PODMAN = Find-Podman
Write-Host "[model] podman: $PODMAN"
$previousEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $PODMAN machine start *> $null
$ErrorActionPreference = $previousEAP

$writer = "podcast-model-yazici"

function Start-Writer {
  Invoke-Quiet $PODMAN @('rm','-f',$writer) | Out-Null
  & $PODMAN run -d --name $writer -v "${Volume}:/models" $HelperImage sleep 7200 | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Yazici container baslatilamadi ($HelperImage cekilmemis olabilir: podman pull $HelperImage)" }
  & $PODMAN exec $writer mkdir -p /models/ses_modelleri /models/hf/hub /models/torch | Out-Null
}
function Stop-Writer {
  Invoke-Quiet $PODMAN @('rm','-f',$writer) | Out-Null
}

Invoke-Quiet $PODMAN @('volume','create',$Volume) | Out-Null

if ($Check) {
  Start-Writer
  try {
    Write-Host "`n[model] '$Volume' hacminin icerigi:"
    & $PODMAN exec $writer sh -c "du -sh /models/* /models/ses_modelleri/* /models/hf/hub/* 2>/dev/null | sort -k2"
  } finally { Stop-Writer }
  return
}

$jobs = @(
  @{ name = "supertonic-3";
     source = (Join-Path $VoiceModels "supertonic-3");
     target = "/models/ses_modelleri";
     probe = "/models/ses_modelleri/supertonic-3/onnx/vocoder.onnx";
     download = "python -c ""from huggingface_hub import snapshot_download; snapshot_download('Supertone/supertonic-3', local_dir='data/ses_modelleri/supertonic-3')""" },
  @{ name = "faster_whisper (medium)";
     source = (Join-Path $VoiceModels "faster_whisper");
     target = "/models/ses_modelleri";
     probe = "/models/ses_modelleri/faster_whisper/models--Systran--faster-whisper-medium";
     download = "python -c ""from faster_whisper import WhisperModel; WhisperModel('medium', device='cpu', compute_type='int8', download_root='data/ses_modelleri/faster_whisper')""" },
  @{ name = "multilingual-e5-large";
     source = (Join-Path $HfCache "models--intfloat--multilingual-e5-large");
     target = "/models/hf/hub";
     probe = "/models/hf/hub/models--intfloat--multilingual-e5-large";
     download = "python -c ""from huggingface_hub import snapshot_download; snapshot_download('intfloat/multilingual-e5-large')""" }
)

Start-Writer
$missing = @()
try {
  foreach ($job in $jobs) {
    $name = $job.name
    $null = Invoke-Quiet $PODMAN @('exec',$writer,'test','-e',$job.probe)
    $exists = ($LASTEXITCODE -eq 0)
    if ($exists -and -not $Refresh) {
      Write-Host "[model] $name -> hacimde ZATEN VAR, atlandi (-Refresh ile ez)"
      continue
    }
    if (-not (Test-Path -LiteralPath $job.source)) {
      Write-Host "[model] $name -> YEREL KOPYA YOK: $($job.source)"
      $missing += $job
      continue
    }
    $mb = ((Get-ChildItem -LiteralPath $job.source -Recurse -Force -File |
            Measure-Object -Property Length -Sum).Sum) / 1MB
    Write-Host ("[model] $name -> kopyalaniyor ({0:N0} MB, WSL uzerinden dakikalar surebilir)..." -f $mb)
    & $PODMAN cp $job.source "${writer}:$($job.target)"
    if ($LASTEXITCODE -ne 0) { throw "podman cp basarisiz: $name" }
  }

  Write-Host "[model] izinler aciliyor (a+rX)..."
  & $PODMAN exec $writer sh -c "chmod -R a+rX /models" | Out-Null

  Write-Host "`n[model] hacim icerigi:"
  & $PODMAN exec $writer sh -c "du -sh /models/ses_modelleri/* /models/hf/hub/* 2>/dev/null"
} finally {
  Stop-Writer
}

if ($missing.Count -gt 0) {
  Write-Host "`n[model] !!! EKSIK AGIRLIKLAR - betik BILEREK indirmedi (4 GB'a operator karar verir):"
  foreach ($job in $missing) {
    Write-Host "`n  * $($job.name)"
    Write-Host "    beklenen yerel yol : $($job.source)"
    Write-Host "    indirme komutu     : cd C:\PROJECTS\podcast ; $($job.download)"
  }
  Write-Host "`n    Indirdikten sonra bu betigi TEKRAR calistir."
  exit 1
}

Write-Host "`n[model] TAMAM. Siradaki: pwsh -File deploy/run-stack.ps1"
