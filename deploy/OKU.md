# Podcast servisi — dağıtım rehberi (Podman / Windows-WSL2)

Bu dizin `hezarfen_podcast_service`'i **gerçek hattıyla birlikte** container'da
ayağa kaldırır. Ev şablonu chatbot'un `deploy/` dizinidir; farklar ve
**gerekçeleri** aşağıda tek tek yazılıdır.

> **Kernel/machine kurulumu bu repoda TEKRARLANMADI.** Podman'ın WSL2'de
> çalışması için gereken özel kernel + rootless machine adımları **bir kez**
> yapılır ve chatbot reposundadır:
> `hezarfen_rule_based_chatbot-main\...\deploy\PODMAN-WSL-SETUP.md`
> (`setup-podman-wsl.ps1`). İki kopya iki doğruluk kaynağı olurdu.

---

## Ön koşullar

| # | Koşul | Doğrulama |
|---|---|---|
| 1 | Podman Desktop + çalışan bir machine, **rootless** | `podman machine list` |
| 2 | WSL2 özel kernel'i kurulmuş (nft_fib/bridge/veth/tun) | `podman run --rm docker.io/surrealdb/surrealdb:v3 version` netavark hatası vermemeli |
| 3 | Hat kaynağı diskte | `C:\PROJECTS\podcast\router\hat.py` var mı |
| 4 | Model ağırlıkları diskte (ya da indirilecek) | aşağıdaki tablo |
| 5 | `AI_SHARED_TOKEN` — backend'inkiyle **aynı** | **`.env` dosyası** (ortam değişkeni değil — aşağıya bkz.) |
| 6 | Boş disk: imaj ~4-5 GB + hacimler ~4 GB | |

---

## Hızlı başlangıç (dört komut)

```powershell
cd C:\PROJECTS\podcast\hezarfen_podcast_service

pwsh -File deploy/setup-pipeline.ps1        # 1) hat kaynagi -> vendor/pipeline  (~1,5 MB, saniyeler)
pwsh -File deploy/setup-models.ps1   # 2) agirliklar -> podcast-models hacmi (~3,9 GB, DAKIKALAR)
Copy-Item .env.example .env            # 3) sonra .env icindeki
                                       #    AI_SHARED_TOKEN degerini
                                       #    backend ile AYNI yap
pwsh -File deploy/run-stack.ps1        # 4) dort repoyu sirayla kaldirir + dogrular
```

Bittiğinde: frontend **http://localhost:5173** (`admin` / `admin123`),
backend **http://localhost:8080**, podcast köprüsü port açmaz —
kanıtı `podman logs hezarfen-podcast-bridge` içindeki `kayit basarili` satırıdır.

---

## Adım adım

### 1. `setup-pipeline.ps1` — hattı vendor'a kopyalar

Hat **ayrı bir repodadır** (`C:\PROJECTS\podcast`). İmaj self-contained olmalı
ama hattın gerçek kaynağı **tek** kalmalı. Karar: tek yönlü kopyalayıcı.

* Kaynak önce **doğrulanır** (`router/hat.py`, `config/router.toml`,
  `router/yonlendirici.py`, `script/plan.py`, `ses/montaj.py`,
  `ingest/__init__.py`, üç `requirements*.txt`). Yanlış yol sessizce boş bir
  vendor üretemez.
* Kopyalanan: `router/ script/ ses/ ingest/ bilgi/ config/` +
  `requirements.txt requirements-ocr.txt requirements-ses.txt` — **ölçüldü: ~1,5 MB**.
* Kopyalanmayan: `.venv/ out/ data/ models/ samples/ vastai_* backend/ frontend/
  hezarfen_*/ .git/ __pycache__/ *.pyc`.
* **`tools/bin/` de kopyalanmaz.** İçindekiler `ffmpeg.exe` + `ffprobe.exe`,
  yani **Windows ikilileri** ve **ölçüldü: 219,8 MB**. Linux container'da
  çalışmazlar. Containerfile ffmpeg'i `apt`'tan kurar ve
  `tools/bin/ffmpeg.exe → /usr/bin/ffmpeg` sembolik bağını atar
  (`ses/ayar.py:22` yolu **sabit kodlar**; uzantı Linux'ta anlamsızdır).
  `-WindowsBinaries` ile yine de kopyalanabilir.

`vendor/` **`.gitignore`'dadır**: kaynağı orada değil, hat reposundadır.
Vendor içinde yapılan düzenleme bir sonraki koşuda **silinir**.

### 2. `setup-models.ps1` — ağırlıkları `podcast-models` hacmine koyar

Ağırlık **veridir, kod değildir**; imaja koymak her build'i 4 GB'lık bir katman
kopyasına çevirir ve `rollback.ps1`in tuttuğu her etiketi 4 GB yapardı.

| Ağırlık | Ölçülen boyut | Nereye | Ne için |
|---|---|---|---|
| `intfloat/multilingual-e5-large` | **2156,99 MB** | `/models/hf/hub/` | gömme (`bilgi/erisim.py`) |
| `faster-whisper medium` | **1459,67 MB** | `/models/ses_modelleri/faster_whisper/` | round-trip CER kalite kapısı |
| `supertonic-3` | **382,72 MB** | `/models/ses_modelleri/supertonic-3/` | TTS |
| **toplam** | **~3,9 GB** | | |

* **Yerel-önce.** Üç ağırlık da bu makinede zaten var; betik onları kopyalar.
  Bulunmayan için **indirme komutunu basar ama indirmez** — 4 GB'lık bir çekmeye
  operatör karar verir.
* Hacme yazma tekniği: yardımcı bir container + `podman cp` (chatbot'un
  `setup-podman-wsl.ps1:39-42` deseni). **Ağ gerektirmez.** Tek fark: chatbot
  `podman create` kullanır, biz `podman run -d ... sleep` kullanırız —
  `podman cp` **duran** bir container'ın hacim bağlı yoluna yazdığında podman
  sürümüne göre hacme değil rootfs'e yazabilir ve yazma **sessizce kaybolur**.
* Sonda `chmod -R a+rX /models` çalışır: hacim root sahipliğinde oluşur,
  container **uid 10001** ile ve `/models` `:ro` bağlanarak koşar.

Denetleme: `pwsh -File deploy/setup-models.ps1 -Check`

### 3. `run-stack.ps1` — dört repoyu sırayla kaldırır

Sıra: **backend** (ağı ve `hezarfen-data` hacmini yaratır) → **frontend** →
**chatbot** → **podcast**.

**Repolar ada göre aranmaz.** Ev şablonu sabit adlarla arıyor
(`hezarfen_backend` / `hezarfen_frontend` / `Hezarfen-Rule-Based-Chatbot`) ve bu
makinede **hiçbiri tutmuyor**. Bu betik her repoyu `compose.yaml`ın
**varlığına ve içeriğine** göre bulur (`container_name: hezarfen-backend` vb.),
bulamazsa **açık hata** verir; birden çok aday bulursa da hata verir.

**Proje adı kapısı (kritik).** `frontend` ve `chatbot` compose'ları ağı
`hezarfen_backend_default` diye **sabit yazıyor**. compose proje adını dizin
adından türetir; backend dizini `hezarfen_backend-main` olduğu için ağ
`hezarfen_backend-main_default` olurdu ve **frontend/chatbot ayağa kalkmazdı.**
Bu yüzden backend **bilerek** `-p hezarfen_backend` ile kaldırılır.

Sonra betik gerçek adları **tahmin etmez, okur**:

```powershell
podman inspect hezarfen-backend --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
podman inspect hezarfen-backend --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
```

ve bunları `HEZARFEN_NET` / `HEZARFEN_DATA_VOLUME` olarak podcast compose'una
geçirir.

**Doğrulama** (sonda): `podman ps` + `localhost:8080` + `localhost:5173` HTTP +
podcast loglarında **`kayit basarili`** satırı (30 sn boyunca aranır). Kayıt
görülmezse betik son 40 log satırını basar ve **exit 1** verir.

Yararlı bayraklar:

```powershell
pwsh -File deploy/run-stack.ps1 -PodcastOnly   # backend/frontend/chatbot zaten ayakta
pwsh -File deploy/run-stack.ps1 -Rebuild      # podcast imajini --no-cache build et
pwsh -File deploy/run-stack.ps1 -Root D:\baska    # arama kokunu degistir
```

### 4. `rollback.ps1` — önceki imaja dön

Ev şablonu `:local` kullanıyor, her build aynı etiketi eziyor, **geri dönüş yok.**
Biz her build'i `localhost/hezarfen-podcast:<yyyyMMdd-HHmmss>` olarak etiketler
**ve** `:current` işaretini oraya taşırız; compose `:current` kullanır.

```powershell
pwsh -File deploy/rollback.ps1 -List            # etiketler + hangisi :current
pwsh -File deploy/rollback.ps1                     # bir onceki etikete don
pwsh -File deploy/rollback.ps1 -Tag 20260831-181530
```

Etiketleri listeleyen **tek satırlık yol** (betik gerekmez):

```powershell
podman images localhost/hezarfen-podcast --format "{{.Tag}} {{.ID}} {{.CreatedSince}}"
```

Geri alma **yeniden build etmez**: `podman tag` + `compose up -d --force-recreate`
(saniyeler). `--force-recreate` şart — etiket adı aynı kaldığı için compose
"değişiklik yok" deyip container'ı bırakabilir.

---

## Betiklerdeki tuzaklar (ölçüldü)

Bu bölüm `deploy/*.ps1` içinde **koşturarak** bulunmuş, PowerShell 5.1 +
podman 5.8.6 üzerinde ölçülmüş tuzakların **tek kayıtlı yeridir**. Betiklerde
yorum satırı **yoktur**; bilgi burada durur. Bir betiği düzenleyen önce burayı
okumalı — beş maddenin her biri sessiz ya da **ikinci koşuda** ortaya çıkan bir
bozulmadır.

### 1. `Get-ChildItem -LiteralPath -Recurse -Include` → `-Include` YOK SAYILIR

**Betik:** `setup-pipeline.ps1`, vendor kopyasından `*.pyc / *.pyo / *.yedek`
artıklarını temizleyen ikinci geçiş.

**Bozuk satır yapısı:**

```powershell
Get-ChildItem -LiteralPath $target -Recurse -Force -Include *.pyc,*.pyo,*.yedek |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
```

**Ne oluyordu (ölçüldü 2026-08-31):** `-LiteralPath` ile `-Recurse` birlikte
kullanıldığında `-Include` **uygulanmaz** ve filtre yokmuş gibi **her dosya**
eşleşir. Bu satır, `vendor/pipeline`a saniyeler önce kopyalanan dosyaların
**tamamını** siliyordu; geriye yalnızca boş dizinler kalıyordu. Hata sessizdi:
betik "TAMAM" yazıp çıkıyor, bozukluk ancak build'de ya da container içinde
`PODCAST_MODE=real` exit 2 verdiğinde görülüyordu.

**Doğrusu:** uzantı süzmesi `-Include` ile değil, `Where-Object` ile yapılır:

```powershell
Get-ChildItem -LiteralPath $target -Recurse -Force -File |
  Where-Object { $_.Extension -in @(".pyc", ".pyo", ".yedek") } |
  ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
```

### 2. `podman machine start` zaten çalışıyorsa stderr'e yazar → betiği ÖLDÜRÜR

**Betikler:** `setup-models.ps1` ve `run-stack.ps1` — podman machine'i garantiye
alan satır.

**Bozuk satır yapısı:**

```powershell
$ErrorActionPreference = "Stop"
...
& $PODMAN machine start *> $null      # 'zararsiz' sanilan satir
```

**Ne oluyordu (ölçüldü 2026-09-01):** PowerShell 5.1'de bir **native** komutun
stderr'i yönlendirildiğinde **her satır** bir `NativeCommandError` kaydına
sarılır; `$ErrorActionPreference = 'Stop'` altında bu kayıt terminating sayılır
ve betiği düşürür — komutun **çıkış kodu 0 olsa bile**. `podman machine start`
makine **zaten çalışıyorsa** mesajını stderr'e yazar. Yani satır ilk koşuda
geçiyor, **ikinci koşuda** betiği öldürüyordu.

**Yönlendirme biçimi fark etmez:** `*> $null 2>&1`, `*> $null` ve `2>$null`
üçü de ölçüldü, **üçü de** düşürdü. Aynı kusur ev şablonundaki (chatbot)
betikte de vardır.

**Doğrusu:** çağrının etrafında `$ErrorActionPreference` geçici olarak
`'Continue'`ya alınır, sonra geri konur:

```powershell
$previousEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $PODMAN machine start *> $null
$ErrorActionPreference = $previousEAP
```

### 3. `podman volume create` zaten varsa aynı şekilde öldürüyordu

**Betik:** `setup-models.ps1`, `podcast-models` hacmini garantiye alan satır.

Mekanizma **2 ile birebir aynıdır**: `podman volume create <var-olan-hacim>`
"already exists" mesajını **stderr'e** yazar, çıkış kodu 0 olsa da
`NativeCommandError` üretir ve `Stop` altında betiği düşürür. Yine **ilk koşuda
sorun yok, ikinci koşuda betik ölüyordu** — hacim zaten yaratılmış olduğu için.

Aynı sınıf `podman rm -f <olmayan-container>` ve
`podman exec ... test -e <olmayan-yol>` çağrılarında da geçerlidir; ikisi de
**bilerek** başarısız olabilen ve stderr'e yazan çağrılardır.

### `Invoke-Quiet` yardımcısı NEDEN var

2 ve 3 numaralı tuzakların ortak çözümü. `setup-models.ps1`, `run-stack.ps1` ve
`rollback.ps1` içinde **aynen tekrarlanır**:

```powershell
function Invoke-Quiet {
  param([Parameter(Mandatory)][string]$Command, [string[]]$Arg = @())
  $previousEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try { & $Command @Arg *> $null } finally { $ErrorActionPreference = $previousEAP }
  return $LASTEXITCODE
}
```

Görevi tek cümleyle: **çıktısı bastırılacak her native çağrıyı,
`$ErrorActionPreference='Stop'`u geçici olarak devre dışı bırakarak koşturmak;
hata fırlatmak yerine ÇIKIŞ KODUNU döndürmek.** Başarı/başarısızlık kararı
çağıran tarafta `$LASTEXITCODE` ile verilir. `finally` şart: çağrı fırlatsa
bile eski `$ErrorActionPreference` geri konur. Bastırılması gereken **her**
podman çağrısı bundan geçmelidir; doğrudan yazılan bir
`& $PODMAN ... *> $null` satırı 2/3 numaralı tuzağa geri döner.

### 4. `Sort-Object -Unique` tek eleman kaldığında SKALER döner → yol tek karaktere iner

**Betik:** `run-stack.ps1`, `Find-Repo` fonksiyonu (repoları `compose.yaml`
**içeriğinden** bulan tarama).

**Bozuk satır yapısı:**

```powershell
$found = $found | Sort-Object -Unique      # @() ile sarilmamis
...
Push-Location $found[0]
```

**Ne oluyordu (ölçüldü 2026-09-01):** PowerShell tek elemanlı bir sonucu
**diziden çıkarır**; `Sort-Object -Unique` geriye dizi değil **skaler string**
döndürür. Tam da beklenen durumda — her repo için **bir** aday bulunduğunda —
`$found[0]` artık ilk elemanı değil, string'in **ilk karakterini** verir:
`"C:\PROJECTS\podcast\backend\hezarfen_backend-main"` → `"C"`. Ardından
`Push-Location "C"` patlıyordu ve hata mesajı asıl sebebi hiç göstermiyordu.
Aynı sebeple `$found.Count` de 1 yerine **string uzunluğunu** verir.

**Doğrusu:** sonuç `@()` ile diziye zorlanır:

```powershell
$found = @($found | Sort-Object -Unique)
```

Aynı korunma `run-stack.ps1`'deki base-imaj pre-pull listesinde de gerekir:
`$bases | Sort-Object -Unique` bir `foreach` içinde tüketildiği için orada
zararsızdır, ama indekslenirse aynı tuzağa düşer.

### 5. Go şablonundaki gömülü çift tırnak PowerShell 5.1'de bozulur

**Betikler:** `run-stack.ps1` ve `rollback.ps1` — backend'in `/data` hacminin
**gerçek adını** okuyan satır.

**Bozuk satır yapısı:**

```powershell
podman inspect hezarfen-backend --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
```

**Ne oluyordu (ölçüldü 2026-09-01):** PowerShell 5.1, native bir komuta
geçirilen argümanın **içindeki** çift tırnakları bozar. Go şablonundaki
`eq .Destination "/data"` podman'a bozulmuş halde ulaşır ve podman şunu verir:

```
Error: template: unexpected "/" in operand
```

Hacim adı okunamaz, `$env:HEZARFEN_DATA_VOLUME` boş kalır, compose varsayılana
düşer ve yanlış hacim adıyla `volume ... not found` alınır — sebebi hiç
görünmeden.

**Doğrusu:** şablonda **dize sabiti kullanma**. JSON alınır, ayrıştırma
PowerShell tarafında yapılır:

```powershell
$raw = & $PODMAN inspect hezarfen-backend --format json 2>$null
$mounts = @()
if ($raw) { try { $mounts = ($raw | ConvertFrom-Json)[0].Mounts } catch { $mounts = @() } }
$volume = ($mounts | Where-Object { $_.Destination -eq '/data' } |
           Select-Object -First 1 -ExpandProperty Name -ErrorAction SilentlyContinue)
```

Ağ adını okuyan şablon
(`{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}`) **dize sabiti
içermediği için** güvenlidir ve olduğu gibi kullanılabilir.

---

### Betiklerde ayrıca ölçülmüş, kaybolmaması gereken kararlar

* **`Copy-Item -Recurse -Exclude` birlikte ÇALIŞMAZ** (`setup-pipeline.ps1`).
  İkisi bir arada verildiğinde PowerShell dizin **ağacını kurar ama dosyaları
  kopyalamaz**; `vendor/pipeline` boş dizinlerle dolu kalır ve build sessizce
  bozuk imaj üretir. Bu yüzden kopyalama **süzmesiz** yapılır, süzme ikinci
  geçişe bırakılır (bkz. tuzak 1).
* **`Copy-Item -Exclude` yalnızca EN ÜST seviyede süzer.** Alt ağaçlardaki
  `__pycache__` / `*.pyc` kalır; ikinci geçiş bunları temizler
  (**ölçüldü: ~40 dizin**).
* **Bağımlılık kapısı** (`setup-pipeline.ps1` sonunda
  `tools/dependency_scanner.py` çağrısı — **ölçüldü 2026-08-31**): hattın
  `requirements*.txt` dosyaları `rapidfuzz`i **hiç listelemiyordu**;
  container'da `ingest` geçti ve `script` adımında
  `No module named 'rapidfuzz'` ile düştü. Aynı sınıf daha önce `transformers`
  için de yaşandı. Tarayıcı, kodun import ettiği ama **beyan edilmeyen**
  paketleri vendor **anında** yakalar — 20 dakikalık build'den sonra değil.
  Çıkış kodu 0 değilse betik `throw` eder.
* **Hacim VARLIĞI değil İÇERİĞİ denetlenir** (`run-stack.ps1`, denetimde
  bulundu). `podman volume exists podcast-models` yalnızca hacmin **var
  olduğunu** sorar; `setup-models.ps1` ise hacmi kopyalamadan **önce** yaratır.
  Ağırlıklar eksik kalırsa geriye **var ama boş** bir hacim kalır ve bu kapı
  sessizce geçerdi — hata 20 dakikalık build'den sonra TTS'te patlardı. Bu
  yüzden imza dosyası denetlenir:
  `/models/ses_modelleri/supertonic-3/onnx/vocoder.onnx`.
* **Sürüm etiketi yalnızca imaj GERÇEKTEN değiştiyse açılır**
  (`run-stack.ps1`). Build önce `:current` olarak yapılır, sonra imaj kimliği
  build öncesiyle karşılaştırılır. Gerekçe: build zaten katman önbelleklidir,
  ama her koşuda koşulsuz yeni bir zaman damgası üretmek etiket listesini
  şişirir ve `rollback.ps1`i aynı içeriğe işaret eden anlamsız "sürümlerle"
  doldurur — geri alacak bir şey yokken varmış gibi görünürdü.
* **`--force-recreate` ve `--no-build` şart** (`rollback.ps1`).
  `--force-recreate` olmazsa: imaj ID değişti ama **etiket adı aynı kaldığı**
  için compose "değişiklik yok" deyip container'ı bırakabilir. `--no-build`
  olmazsa: `compose.yaml`da `build:` bloğu vardır ve yeniden build eden bir
  podman-compose sürümü `:current`i **yeni** imajla ezip geri almayı **iptal**
  ederdi.
* **`podman cp` için `run -d ... sleep`, `create` DEĞİL** (`setup-models.ps1`).
  `podman cp` **duran** bir container'ın hacim bağlı yollarına yazdığında
  podman sürümüne göre ya hacme ya da rootfs'e yazar; ikincisi **sessizce
  kaybolur**. Koşan container'da bağlama noktası gerçektir, yazma kesin hacme
  gider. Yardımcı imaj build sırasında zaten çekildiği için **ağ gerektirmez**.
* **Base imaj pre-pull** (`run-stack.ps1`): docker.io anonim-pull dalgalanması
  build **ortasında** `unauthorized` verir; önce çekmek o hatayı build'den
  **önce** ve tek başına görünür kılar.
* **`restart: unless-stopped` tek başına YETMEZ.** Makine yeniden başladıktan
  sonra konteynerleri geri getiren `podman-restart.service` birimidir ve o
  **varsayılan olarak devre dışıdır**; ayrıntı ve tek seferlik kurulum için
  aşağıdaki *SUNUCU YENİDEN BAŞLADIĞINDA* bölümüne bakın.

---

## TTS motoru seçimi — lokal mi, ElevenLabs mi

Servis iki TTS motorundan biriyle koşar. Seçim **iki bayrakla** yapılır ve
ikisi de gerekir:

| Değişken | Lokal (varsayılan) | ElevenLabs |
| --- | --- | --- |
| `PODCAST_TTS_ENGINE` | `supertonic-3` | `elevenlabs-flash-tr` |
| `SES_BULUT_IZINLI` | `0` | `1` |
| `ELEVENLABS_API_KEY` | gerekmez | **şart** |
| `ELEVENLABS_VOICE_ID` | gerekmez | boşsa hazır ses |

**İki bayrak neden ayrı?** Tek bayrak olsaydı anahtarı vermeyi unuttuğun koşu
sessizce lokal sesle biter ve bunu ancak sesi dinlerken fark ederdin. Şimdi
`SES_BULUT_IZINLI=0` iken bulut motoru çağrılırsa `BulutYasak` fırlar —
**açık hata, sessiz düşme yok**. Aynı mantığın tersi de geçerli: izin açık ama
anahtar yoksa `MotorYuklenemedi` gelir.

`.env` içine yazılır (dosya `.gitignore`'dadır, `compose.yaml` yalnızca
interpolasyon kullanır, literal sır taşımaz):

```
PODCAST_TTS_ENGINE=elevenlabs-flash-tr
SES_BULUT_IZINLI=1
ELEVENLABS_API_KEY=<panelden alinan anahtar>
ELEVENLABS_VOICE_ID=<panelden alinan ses kimligi>
```

### Kilitli konfigürasyon

Bunlar `ses/ayar.py` içindedir, compose'tan **değiştirilmez**. Kullanıcı
2026-09-16'da örnekleri dinleyip seçti:

| Ayar | Değer | Gerekçe |
| --- | --- | --- |
| model | `eleven_flash_v2_5` | Seçilen; `multilingual_v2` ve `turbo_v2_5` ile yan yana dinlendi |
| `language_code` | `tr` | Dili **zorlar**. `multilingual_v2` bu alanı almaz, otomatik algılar — Türkçe telaffuzda belirleyici fark |
| kararlılık | `0.85` | Ders anlatımı: bölümler arası ton kaymasın |
| benzerlik | `0.85` | — |
| stil | `0.0` | Düz anlatıcı tonu; yüksek stil ifadeyi dalgalandırır |
| çıktı | `mp3_44100_128` → WAV | Aşağıya bakın |

### Neden mp3 isteyip WAV'a çeviriyoruz

`TtsMotoru` sözleşmesi WAV şart koşuyor. İlk tercih `pcm_44100` idi (dönüşüm
gerektirmez), ama **ölçüldü:**

```
HTTP 403  "Output format 'pcm_44100' is only available on the Pro tier and above."
```

Hesap Pro değil. Bu yüzden mp3 istenir ve `ses/ayar.py::FFMPEG` ile WAV'a
çevrilir. ffmpeg zaten bu hattın **sabit** bağımlılığı (montaj, `ses/dogrula.py`
ve `ayar.FFMPEG` hepsi ona dayanıyor) — yeni bağımlılık değil. Pro hesaba
geçilirse `ELEVENLABS_CIKTI_BICIMI` `pcm_44100` yapılır, dönüşüm atlanır.

### ElevenLabs'e geçince ne değişir

- **Ağ şart olur.** Lokal motor ağ kullanmaz; bulut motoru her bölüm için
  istek atar. Konteyner dışarı çıkamıyorsa koşu `MotorHatasi` ile düşer.
- **Ağırlık hacmi TTS için gerekmez.** `podcast-models` (~3,9 GB) yalnızca
  lokal TTS ve ASR içindi. OCR ve gömme yolları hâlâ kullanıyor, bu yüzden
  hacmi kaldırma — ama ElevenLabs tarafında hiçbir ağırlık indirilmez,
  `HF_HUB_OFFLINE=1` bunu etkilemez.
- **Ücret sağlayıcıda sayılır.** Karakter başına. Uzun bir dersin tüm
  bölümlerini üretmeden önce `PODCAST_CHAPTER_LIMIT=1` ile tek bölüm dene.
- **Hız:** ölçülen RTF **0,43** (3,39 sn ses, 1,46 sn üretim) — lokal
  `supertonic-3` ile aynı seviyede, yani hız gerekçesiyle seçim yapılmaz.

### Anahtarın izinleri

Eldeki anahtar **salt TTS**. Ölçüldü:

```
/voices  -> 401  missing the permission voices_read
/models  -> 401  missing the permission models_read
```

Sonuç: ses listesi API'den **çekilemez**. `voice_id` ElevenLabs panelinden elle
alınıp `ELEVENLABS_VOICE_ID`'ye yazılır. Panelde anahtara `voices_read` izni
eklenirse adaptör listeyi kendisi okuyabilir.

### Elle doğrulama

```powershell
# .env yuklenmis bir kabukta
python tools\elevenlabs_test.py --metin "Merhaba"
```

Çıktı `out/elevenlabs/merhaba.mp3`. Bu betik **hattı çalıştırmaz**, yalnızca
API'yi ve anahtarı sınar; motor adaptörünü sınamak için hattı koştur.

## Hacimler

| Hacim | Bağlantı | Mod | Ne tutar |
|---|---|---|---|
| `hezarfen-data` (**external**) | `/data/files` | **:ro** | Backend'in PDF blob deposu. `PODCAST_MEDIA_ROOT`. Servis **yalnızca okur**. |
| `podcast-out` | `/data/podcast/out` | rw | Üretilen mp3 / script / quiz. `PODCAST_OUTPUT_ROOT`. |
| `podcast-jobs` | `/data/podcast/jobs` | rw | İş durumu (JSON/iş) + köprü durum dosyası. |
| `podcast-kayit` | `/data/podcast/kayit` | rw | `router.sqlite` — adım önbelleği, resume, LLM bütçesi. |
| `podcast-models` | `/models` | **:ro** | ~3,9 GB ağırlık. |
| `podcast-easyocr` | `/home/podcast/.EasyOCR` | rw | easyocr'ın kendi indirdiği tanıma modelleri. |

`hezarfen-data` **external**'dır: backend yaratır, biz sadece bağlanırız.
Adı yanlışsa compose **ayağa kalkmaz**.

> **OPERATÖR DOĞRULAMALI.** Bu makinede podman **kurulu değil**, dolayısıyla
> hacmin gerçek adı **çalışan bir sistemde doğrulanamadı**. Varsayılan
> `hezarfen_backend_hezarfen-data`, `run-stack.ps1`in backend'i
> `-p hezarfen_backend` ile kaldırdığı varsayımına dayanır. Elle
> `podman compose up` yapıyorsan **önce doğrula**:
> ```powershell
> podman volume ls  ; podman network ls
> $env:HEZARFEN_DATA_VOLUME = "<gercek-ad>"
> $env:HEZARFEN_NET         = "<gercek-ag>"
> ```

---

## Sağlık kontrolü

Podman'ın OCI biçimi **imajdaki `HEALTHCHECK`'i yok sayar**; bu yüzden sağlık
`compose.yaml`dadır, `Containerfile`da değil.

```yaml
healthcheck:
  test: ["CMD", "python", "-m", "src.main", "--health"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 90s
```

`--health` **ağ çağrısı yapmaz**. Denetlediği: iş kökü ve çıktı kökü
yazılabilir mi · `real` modda hat **içeri alınabiliyor mu** (`import router.hat`
— ölçüldü: **0,12 sn**) · köprü backend'e **kayıtlı mı**. Toplam ölçüm:
**0,66 sn** (10 sn'lik timeout'un çok altında).

Kayıt durumu iş kökündeki `.bridge-status` dosyasından okunur; köprü onu `jobs.py`
ile aynı **atomik** desenle (geçici dosya + `os.replace`) yazar: kayıt başarılı
olunca `registered=true`, bağlantı koptuğunda `false`. Dosya `*.json` desenine
uymadığı için iş taramasına karışmaz.

**Bayatlık denetimi bilerek YOK**: sağlıklı ve uzun süren bir bağlantı saatlerce
hiçbir şey yazmaz; zaman aşımı koymak sağlıklı köprüyü hasta gösterirdi.

`start_period: 90s` **ölçülmedi**, model yükleme süresine göre seçilmiş bir
tahmindir; ilk gerçek koşudan sonra ayarlanmalıdır.

Elle: `podman exec hezarfen-podcast-bridge python -m src.main --health`
(çıkış `0` sağlıklı, `1` sağlıksız). `--health` **asla 2 dönmez** — 2 yalnızca `--validate`'e aittir.

---

## Sorun giderme

| Belirti | Neden / Çözüm |
|---|---|
| `network hezarfen_backend_default not found` | Backend `-p hezarfen_backend` ile kaldırılmadı ya da hiç ayakta değil. `podman network ls` ile gerçek adı bul, `$env:HEZARFEN_NET`'e yaz. |
| `volume ... hezarfen-data not found` | Aynı sebep, hacim tarafı. `podman volume ls`. |
| build: `COPY vendor/pipeline: no such file` | `deploy/setup-pipeline.ps1` çalıştırılmadı. |
| Boot'ta `exit 2` + `PODCAST_MODE=real ama gercek hat yuklenemedi` | vendor eksik kopyalandı ya da hattın bir bağımlılığı imajda yok. `podman run --rm localhost/hezarfen-podcast:current python -c "import router.hat"` ile tek başına dene. |
| `ffmpeg/ffprobe bulunamadi: /app/pipeline/tools/bin/ffmpeg.exe` | Sembolik bağ yok. `podman exec ... ls -l /app/pipeline/tools/bin/`. Vendor `tools/bin` ile kopyalandıysa gerçek `.exe`ler bağın üstüne yazılmıştır → `setup-pipeline.ps1`i `-WindowsBinaries` **olmadan** koştur. |
| TTS: `model dizini yok` / `vocoder.onnx` | `podcast-models` hacmi boş ya da izinler kapalı. `pwsh -File deploy/setup-models.ps1 -Check` |
| `OSError: ... multilingual-e5-large ... offline` | Ağırlık hacimde yok. `HF_HUB_OFFLINE=1` **bilerek** indirmeyi engelliyor. `setup-models.ps1` koştur; ya da geçici olarak `HF_HUB_OFFLINE=0`. |
| ASR `cuda` hatası | Yok — `ses/dogrula.py:352` CUDA yoksa **`cpu/int8`e düşer** ve bunu raporlar. Container'da beklenen davranış budur, hata değil. |
| Konteyner `unhealthy` ama loglar temiz | `podman logs` içinde `kayit basarili` yoksa köprü backend'e bağlanamıyor: token uyuşmuyor ya da backend ayakta değil. |
| `EACCES` — `/data/podcast/...` | Hacim root sahipliğinde oluşmuş. `podman volume rm podcast-jobs` (durum kaybolur) veya `podman unshare chown -R 10001:10001 ...`. |
| İmaj build'i 20 dk sürüyor | Normal: torch + easyocr. `KUR_AGIR=0` ile ~2 GB ve dakikalar kısalır (OCR, e5 gömme ve VITS yedek motoru kapanır). |
| `unable to retrieve auth token ... unauthorized` | docker.io anonim-pull dalgalanması. `run-stack.ps1` pre-pull yapar; tekrar koştur. |

---

## Durdurma / temizlik

```powershell
cd C:\PROJECTS\podcast\hezarfen_podcast_service ; podman compose down
podman compose down -v          # podcast-jobs / out / kayit / models de SILINIR
```

Diğer repolar kendi dizinlerinde (`podman compose down`); backend
`-p hezarfen_backend` ile kaldırıldıysa `podman compose -p hezarfen_backend down`.

---

### Token neden `.env` dosyasında olmalı, ortam değişkeninde değil

`compose.yaml` `${AI_SHARED_TOKEN:?}` kullanıyor ve compose dosyayı **her
komutta** interpolate eder — `up` kadar `down`, `ps`, `logs` da. Token yalnızca
kabuk değişkenindeyse, kabuğu kapatıp yeni bir terminal açan operatör yığıtı
**indiremez**:

```
error while interpolating services.bridge.environment.AI_SHARED_TOKEN:
required variable AI_SHARED_TOKEN is missing a value
```

Bu ölçüldü (2026-09-01, Podman 5.8.6). `.env` dosyası compose tarafından
kendiliğinden okunur ve sorunu tamamen kapatır; `.env` `.gitignore`'dadır.
`deploy/rollback.ps1` ayrıca kendi kapısını koşar ve eksikse açık hata verir.

## SUNUCU YENİDEN BAŞLADIĞINDA — tek seferlik zorunlu ayar

**Ölçüldü (2026-09-04):** makine yeniden başladıktan sonra beş konteynerin
**hiçbiri geri gelmedi**, `restart: unless-stopped` yazılı olmasına rağmen.

Sebep: Podman **daemonsuz**. Restart politikası yalnızca podman süreci/makinesi
ayaktayken geçerlidir; açılışta konteyner geri getirmek `podman-restart.service`
biriminin işi ve o **varsayılan olarak devre dışı**.

```bash
podman machine ssh
sudo chown -R $(id -u):$(id -g) ~/.config/systemd     # dizin root sahipliginde gelir
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user enable podman-restart.service
loginctl show-user $(whoami) --property=Linger        # Linger=yes olmali
```

`chown` satırı şart: makine imajında `~/.config/systemd/user` **root**
sahipliğinde geliyor ve `systemctl --user enable` `Access denied` veriyor.

Doğrulama: `podman machine stop && podman machine start`, 30 saniye sonra beş
konteyner de `Up` olmalı. Ölçülen kurtarma süresi **~30 sn**.

Not: birim `--filter should-start-on-boot=true` kullanıyor ve bu filtre
`unless-stopped`'ı **kapsıyor** (ölçüldü); açıklama satırındaki "Restart Policy
Set To Always" ifadesi yanıltıcıdır.

### Yeniden başlatma sertifikayı DEĞİŞTİRİR

Backend her açılışta yeni self-signed sertifika üretir — ölçüldü:
`f1555574d670` → `9120c487eeef`. Köprü sertifikayı **her yeniden bağlanmada
yeniden çeker** (`bridge.py`, `run_once` içinde); önbelleğe alınsaydı servis
backend restart'ından sonra bir daha bağlanamaz, sessizce ölürdü.

## Bu katmanda BİLEREK yapılmayanlar

* **Betiklerden yalnızca `setup-pipeline.ps1` çalıştırıldı** (yerel, podman
  gerektirmez): 76 dosya / 1,49 MB, ve vendor kopyasından `router.hat`'in
  import edildiği doğrulandı. Podman bu makinede kurulu olmadığı için ağ adı,
  hacim adı, imaj boyutu ve build süresi **ölçülmedi**.
* **`mem_limit: 8g` / `cpus: 4.0` ölçülmedi.** Ölçülen tek şey ağırlıkların
  disk boyutudur (~3,9 GB); RSS değil. İlk gerçek koşudan sonra ayarlanmalı.
* ~~Taban imaj digest'e pinlenmedi~~ - **kapandi**, `@sha256:4101e4a3...`.
* ~~`podcast-out`/`podcast-jobs` temizlenmiyor~~ - **kapandi**,
  `PODCAST_RETENTION_DAYS` (compose'ta `30`, kod varsayilani `0`).
* **easyocr ağırlıkları imajda yok**; ilk taranmış PDF'te kendi indirir
  (`podcast-easyocr` hacmine, kalıcı). Ağ gerekir.
* **`torch` / `transformers` / `easyocr` tek bir ARG'a bağlıdır: `KUR_AGIR`
  (varsayılan `1`).** `transformers` hattın **hiçbir `requirements*.txt`inde
  yazılı değil** — `requirements-ses.txt` onu (ve `onnxruntime`, `numpy`,
  `huggingface_hub`'ı) "zaten kurulu" varsayıyor; bu boşluk Containerfile'da
  açık pin'lerle kapatıldı (`transformers==5.15.0`, `onnxruntime==1.23.2`,
  `numpy==2.2.6`, `huggingface_hub==1.27.0` — yereldeki ölçülen sürümler).
  `torch` **sürüm pinlenmedi**: CPU tekerlek indeksinde py3.10 için hangi
  sürümün duracağı doğrulanamadı, bu yüzden çözücüye bırakıldı.
  `KUR_AGIR=0` ile OCR, e5 gömme ve VITS yedek motoru kapanır.
