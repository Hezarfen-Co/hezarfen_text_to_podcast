[CmdletBinding()]
param(
  [string]$Source = "C:\PROJECTS\podcast",
  [switch]$WindowsBinaries,
  [switch]$Clean
)
$ErrorActionPreference = "Stop"

$repo   = Split-Path -Parent $PSScriptRoot
$target = Join-Path $repo "vendor\pipeline"

if (-not (Test-Path -LiteralPath $Source)) {
  throw "Hat kaynagi bulunamadi: $Source  (-Source <yol> ile ver)"
}
$Source = (Resolve-Path -LiteralPath $Source).Path
foreach ($signature in @("router\hat.py", "config\router.toml", "router\yonlendirici.py",
                         "script\plan.py", "ses\montaj.py", "ingest\__init__.py",
                         "requirements.txt", "requirements-ses.txt", "requirements-ocr.txt")) {
  $p = Join-Path $Source $signature
  if (-not (Test-Path -LiteralPath $p)) {
    throw "Kaynak hat gibi gorunmuyor: $signature yok ($Source)"
  }
}
Write-Host "[pipeline] kaynak dogrulandi: $Source"

if ($Clean) {
  if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
  Write-Host "[pipeline] vendor/pipeline silindi. Cikiliyor."
  return
}

$directories = @("router", "script", "ses", "ingest", "bilgi", "config")
$files = @("requirements.txt", "requirements-ocr.txt", "requirements-ses.txt")

if ($WindowsBinaries) { $directories += "tools\bin" }

$excluded = @("__pycache__", ".venv", ".git", "out", "*.pyc", "*.pyo", "*.yedek")

if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
New-Item -ItemType Directory -Force -Path $target | Out-Null

Write-Host "[pipeline] kopyalaniyor -> $target"
foreach ($d in $directories) {
  $src = Join-Path $Source $d
  if (-not (Test-Path -LiteralPath $src)) { Write-Host "    ATLANDI (yok): $d"; continue }
  $dst = Join-Path $target $d
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
  Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
  Write-Host "    $d"
}
foreach ($f in $files) {
  Copy-Item -LiteralPath (Join-Path $Source $f) -Destination (Join-Path $target $f) -Force
  Write-Host "    $f"
}

Get-ChildItem -LiteralPath $target -Recurse -Force -Directory -Filter "__pycache__" |
  Sort-Object { $_.FullName.Length } -Descending |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force }
Get-ChildItem -LiteralPath $target -Recurse -Force -File |
  Where-Object { $_.Extension -in @(".pyc", ".pyo", ".yedek") } |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

foreach ($signature in @("router\hat.py", "config\router.toml", "requirements-ses.txt")) {
  if (-not (Test-Path -LiteralPath (Join-Path $target $signature))) {
    throw "Kopyalama eksik: $signature vendor/pipeline altinda yok."
  }
}
$fileCount = (Get-ChildItem -LiteralPath $target -Recurse -Force -File).Count
$size = (Get-ChildItem -LiteralPath $target -Recurse -Force -File |
         Measure-Object -Property Length -Sum).Sum
Write-Host ""
Write-Host ("[pipeline] TAMAM: {0} dosya, {1:N2} MB" -f $fileCount, ($size / 1MB))
Write-Host "[pipeline] hedef: $target"
if (-not $WindowsBinaries) {
  Write-Host "[pipeline] tools/bin/*.exe ATLANDI (Windows ikilileri, ~220 MB). Container ffmpeg'i apt'tan kurar."
}
$py = $null
  foreach ($candidate in @((Join-Path (Join-Path (Join-Path $Source ".venv") "Scripts") "python.exe"), "python")) {
  if ($candidate -eq "python") { if (Get-Command python -ErrorAction SilentlyContinue) { $py = "python" } }
  elseif (Test-Path -LiteralPath $candidate) { $py = $candidate }
  if ($py) { break }
}
if ($py) {
  Write-Host ""
  & $py (Join-Path (Join-Path (Split-Path -Parent $PSScriptRoot) "tools") "dependency_scanner.py") $target
  if ($LASTEXITCODE -ne 0) {
    throw "Beyan edilmemis bagimlilik var (yukari bkz). Imaj derlenmeden ONCE requirements*.txt duzeltilmeli."
  }
} else {
  Write-Host "[pipeline] UYARI: python bulunamadi, bagimlilik taramasi ATLANDI."
}

Write-Host ""
Write-Host "[pipeline] siradaki: pwsh -File deploy/setup-models.ps1"
