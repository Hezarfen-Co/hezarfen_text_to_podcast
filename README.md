# hezarfen_podcast_service

Hezarfen okul sisteminin dördüncü servisi: ders içeriğinden podcast üreten AI
servisi. Kablo çalışır, **iş yaşam döngüsü gerçektir** (kuyruk, diskte kalıcı
durum, işbirlikçi iptal, açılış süpürmesi), **gerçek hat bağlıdır**
(`PODCAST_MODE=real`) ve **dağıtım katmanı yazılmıştır** (`deploy/`).



## Nasıl konumlanır

Servis **HTTP sunucusu açmaz, port dinlemez**. Rust backend (`hezarfen_backend`)
QUIC *sunucusudur*; bu servis ona **dışarıdan bağlanır** (dial-out), yeteneklerini
kaydeder ve backend'in açtığı akışlardan iş alır. Rule-based chatbot ile birebir
aynı konumlanma.

## Protokol (`hab/2`)

Çerçeveleme: 4 bayt big-endian uzunluk + o kadar bayt JSON. Tavan 8 MiB.

1. `GET {AI_BACKEND_URL}/ai/certificate` → dönen PEM QUIC güven deposuna
   **pinlenir**. Backend self-signed sertifikayı her açılışta yenileyebildiği için
   sertifika **her yeniden bağlanmada tekrar çekilir**; salt redial yanlıştır.
   `AI_TLS_FINGERPRINT` doluysa PEM'den DER çıkarılıp SHA-256'sı **kendimiz**
   hesaplanır ve pinlenen değerle karşılaştırılır; tutmazsa bağlanılmaz, loglanır
   ve geri çekilerek yeniden denenir. Boşsa bağlantı TOFU'dur ve bu **her
   açılışta uyarı olarak loglanır** — sessiz bir varsayılan değil, bilinçli bir
   taviz. Sunucunun bildirdiği `fingerprint_sha256` alanı pin yerine geçmez
   (sertifikayı uyduran taraf yanındaki izi de uydurur); yalnızca loglanır ve
   bizim hesabımızla çapraz denetlenir.
2. QUIC bağlantısı, ALPN `hab/2`, idle timeout 30 sn. Sürüm kimliği iki yerde
   birden denetlenir: ALPN pazarlığı ve `Hello.protocol` alanı.
3. İlk client-initiated çift yönlü akış = **kontrol akışı**: tek `Hello` yazılır,
   tek `Greeting` okunur, akış ömür boyu açık tutulur (kapanması = kayıttan
   düşme). Heartbeat çerçevesi yok; 10 sn'de bir QUIC PING ile canlı tutulur.
4. Her istek backend'in açtığı server-initiated akıştır (`stream_id % 4 == 1`):
   tek `Request` okunur, tek `Response` yazılır, akış biter.

**Okul kapsamı:** filo tüm okullara ortaktır, bu yüzden `Hello` okul taşımaz;
her `Request` kendi okulunu tireli uuid ile adlandırır ve her `Response` onu **aynen
yankılar**. Okulsuz istek `bad_request` ile reddedilir — varsayılan ya da geri
düşme yoktur, çünkü yanlış okulun verisinden cevaplanan bir okuma tam olarak bu
alanın var olma sebebidir. Aynı kural servisin açtığı okuma akışında da geçerli:
`ApiRequest` okulu taşır, `ApiResponse` onu yankılar.

Cevaplar: `{"status":"ok","id":...,"school":...,"payload":{...}}` veya
`{"status":"err","id":...,"school":...,"code":...,"message":...}`. Kullanılan
kararlı kodlar: `unsupported_capability`, `bad_request`, `not_found`, `not_ready`,
`llm_unavailable`, `frame_too_large`, `timed_out`, `busy`, `internal`.

**Kalıcı red döngüyü bitirmez.** `unauthorized` ve `unsupported_protocol`
yapılandırma değişmeden düzelmez; servis yine de **çıkmaz** (exit 2 yok). Çünkü
`restart: unless-stopped` altında çıkmak sonsuz bir crash-loop olur ve backend
düzeltildiğinde servis kendiliğinden toparlanmaz — operatörün elle müdahalesi
gerekir. Bunun yerine bekleme ikiye katlanır, üzerine jitter eklenir ve
`AI_RECONNECT_MAX_SECS` tavanında durur.

## Neden yetenek işleyicileri anında döner

Backend'in her `hab/2` isteği için bir deadline'ı var ve **tavanı 60 saniyedir**
(`hezarfen_backend/src/constant.rs`, `AI_MAX_REQUEST_TIMEOUT_SECS`). Gerçek
podcast üretimi ise **~45 dakika** sürer. Bu iki sayı aynı çağrının içine sığmaz.

Bu yüzden mimari ikiye bölünmüştür:

- `podcast.submit` / `podcast.cancel` **yalnızca yerel çalışma önbelleğine
  dokunur ve anında döner.** Hiçbiri uzun işi beklemez. İşin `status`/`result`
  cevapları artık **backend'in kendi satırından** gelir: kimliği backend üretir,
  servis her geçişi `podcast.report` ile geri bildirir ve biten mp3'ü
  `BlobUploadRequest` ile yükler. `done` raporu, ses yüklenmeden
  `audio_missing` ile reddedilir — yani "bitti" demek ile "ses hazır" demek
  aynı şeydir.
- Uzun iş **ayrı, `daemon=True` işaretli işçi thread'lerinde** koşar; işler
  `queue.Queue` üzerinden dağıtılır. asyncio'nun varsayılan executor'ı bilerek
  kullanılmaz: dakikalarca süren bir iş o havuzu tıkar ve milisaniyelik `status`
  çağrıları da kuyruğa girerdi.

`ThreadPoolExecutor` **bilinçli olarak kullanılmaz.** `shutdown(wait=False,
cancel_futures=True)` anında döner ama executor'ın thread'leri daemon değildir;
yorumlayıcı çıkışta onları yine de `join` eder. Ölçüldü: 8 saniyelik bir iş
koşarken `shutdown` 0 ms'de döndü, **süreç 8079 ms yaşadı**. 45 dakikalık gerçek
iş ile bu, SIGTERM → takılma → compose süre aşımı → SIGKILL demektir. Daemon
thread'lerde aynı ölçüm 1465 ms'dir (`shutdown(timeout=1.0)` kadar), işçi
`JobContext.check()` çağırıyorsa 403 ms.

`JobStore.shutdown(timeout=10.0)` sırasıyla: `stopping` bayrağını kaldırır,
koşan tüm işlere iptal bayrağı koyar, her thread için kuyruğa bir `None` sentinel
bırakır, thread'leri **toplam** `timeout` bütçesi içinde `join` eder, hâlâ
yaşayan varsa tek satır uyarı basıp döner. Daemon oldukları için süreç çıkabilir.
Henüz başlamamış `queued` işlere **dokunulmaz** — `begin()` kapanma sırasında
işi başlatmadan `False` döner, iş `queued` kalır ve bir sonraki açılışta yeniden
kuyruğa alınır.

İki eşiği karıştırmayın: `AI_MAX_CONCURRENT` aynı anda işlenen **istek**
sayısıdır (aşılırsa `busy`), `PODCAST_MAX_JOBS` ise kuyruktaki + koşan **iş**
sayısıdır (aşılırsa yine `busy`, ama `podcast.submit` içinden).

## Yetenekler

| Yetenek | İstek payload'u | Başarılı cevap | Hatalar |
| --- | --- | --- | --- |
| `podcast.submit` (backend → servis) | `job_id` (**backend'in ürettiği**, zorunlu), `source_id` (zorunlu), `source_key` (**notun en yeni PDF ekinin blob anahtarı**, zorunlu), `user_id` (zorunlu), `format` (ops.) | `{"job_id":...,"state":"queued","eta_secs":<int>}` | `bad_request`, `conflict`, `busy`, `llm_unavailable` |
| `podcast.cancel` (backend → servis) | `job_id` (zorunlu metin) | `{"job_id":...,"cancelled":<bool>}` | `bad_request`, `not_found` |
| `podcast.report` (servis → backend) | `job_id`, `source_id`, `format`, `user_id`, `state`, `stage`, `progress`, `error_code` | `{"job_id":...,"stored":true}` | `unknown_job`, `not_permitted`, `invalid_payload`, `audio_missing`, `expired`, `unavailable` |
| blob yükleme (servis → backend) | `{id, upload:true, school, job_id, name, content_type, size, duration_secs}` + tam `size` bayt | `{"status":"ok","key":"podcast/<job>.mp3","size":<int>}` | `unknown_job`, `expired`, `malformed` |

`podcast.report` **sıralı** gönderilir: her geçişin cevabı beklenir, sonra
sonraki gider. Reddedilen bir rapor (ya da yükleme) işi yerelde `failed` yapar ve
**reddin kodu** `error_code` olarak yazılır; sessizce yutulmaz. Bağlantı yokken
raporlar beklemeye alınır ve bağlantı gelince sırayla gönderilir — bu durumda iş
asla `done` görünmez, `queued`/`running` kalır.

Payload tipleri katı doğrulanır; uymayan istek `bad_request` alır.

`podcast.status` ve `podcast.result` **artık servis yeteneği değildir**; bu iki
cevabı backend kendi satırından verir. Servis onları ne bildirir ne işler
(bildiren eski bir dağıtım `unsupported_capability` alır).

`podcast.cancel`'ın `cancelled` alanı **"bu çağrı bir şeyi iptal etti mi"**
sorusunun cevabıdır: kuyruktaki ya da koşan bir iş için `true`, zaten
`done`/`failed`/`cancelled` olmuş bir iş için `false`. Var olmayan iş `not_found`
alır, `false` almaz.

**Kablo anahtarları İngilizce.** Backend (Rust) ve frontend (TypeScript) bu
alanları tüketiyor ve o iki tarafın tüm alan adları İngilizce (`status`,
`error_code`, `created_at`). Türkçe olan tek şey log ve hata metinleri.

**`source_id` bir dosya yolu DEĞİLDİR.** Backend'in kayıt kimliğidir; PDF
baytı köprüden geçmez (`AI_API_ALLOWLIST` bayt sunan rotaları bilerek
dışarıda bırakıyor, `constant.rs:612-615`). Dosya paylaşılan hacimde şu yoldan
çözülür: `PODCAST_MEDIA_ROOT/<school>/<source_key>`; `source_key` notun **en
yeni PDF ekinin** blob anahtarıdır (`course_note_file.file`), `<school>` ise
çerçevenin taşıdığı okul uuid'sidir.

## Durum makinesi

```
queued ──▶ running ──▶ done
   │  │        ├─────▶ failed
   │  │        └─────▶ cancelled
   │  └─────────────▶ failed
   └───────────────▶ cancelled
```

`queued → failed` **yasaldır**: iş `begin()`'den önce de patlayabilir (gerçek
hatta örneğin `source_key` dosyaya çözülemez → `HatHatasi`). Bu geçiş yasak
olsaydı `fail()` `InvalidTransition` yer, iş `queued` kalır, her açılışta
yeniden kuyruğa alınır, yine patlar ve `PODCAST_MAX_JOBS` kotasından bir slotu
kalıcı olarak yerdi.

`done`, `failed`, `cancelled` **terminaldir**; çıkış geçişleri yoktur. Geçersiz
bir geçiş denemesi `InvalidTransition` fırlatır — **sessizce yutulmaz**. Bunun
sebebi savunuculuk değil teşhis: sessizce yutulan bir geçiş, ilerideki gerçek
hatta "iş neden hâlâ `running` görünüyor" sorusunu cevaplanamaz hale getirir.

## İş deposu ve kalıcılık

İş durumu **iş başına bir JSON dosyası** olarak `PODCAST_JOB_ROOT` altında
tutulur (`{job_id}.json`). Her yazma **atomiktir**: geçici dosyaya yaz → `flush`
→ `fsync` → `os.replace`. Yarım yazılmış bir JSON dosyası hiçbir zaman görünmez;
`os.replace` aynı dosya sisteminde atomik bir isim değiştirmedir.

Durum diskte tutulur çünkü **servis yeniden başladığında iş durumu
kaybolmamalıdır**. Container'da bu dizin adlandırılmış bir hacme (`podcast-jobs`)
bağlanır; imaj içinde uid 10001'e ait olarak önceden yaratılır, yoksa hacim root
sahipliğiyle oluşur ve servis ilk yazmada `EACCES` alır.

Yazma sırası **önce disk, sonra bellek**tir ve bu iki yerde birden geçerlidir:

- `submit()` kaydı **önce diske yazar**, ancak yazma başarılıysa belleğe koyar.
  Ters sırada bir `OSError`, hiç koşmayacak ve hiç temizlenmeyecek bir `queued`
  kaydı bırakır ve `PODCAST_MAX_JOBS` kotasından bir slotu kalıcı yerdi.
- `transition()` / `update_progress()` / `cancel()` / açılış süpürmesi kaydın bir
  **kopyası** üzerinde çalışır, kopyayı yazar, ancak yazma başarılıysa
  `record.clear()` + `record.update()` ile commit eder (kaydın kimliği korunur).
  Tersi durumda bellek `done`, disk `running` derdi ve iş bir sonraki açılışta
  `interrupted` işaretlenirdi.

Geçici dosya adı iş-id başına sabit değildir; `{job_id}.json.tmp{pid}-{rastgele}`
biçimindedir, böylece iki süreç birbirinin geçici dosyasını ezmez. Açılışta
`*.json.tmp*` artıkları silinir (silinemezse uyarı basılır, açılış çökmez).
`os.replace` sonrası **dizin de fsync'lenir** ki yeniden adlandırma metadata'sı
diske insin; Windows dizin fsync'i desteklemez, oradaki `OSError` sessizce
geçilir (platform farkı, hata değil).

Açılışta kayıtlar **şema doğrulamasından** geçer: zorunlu alanlardan (`job_id`,
`source_id`, `source_key`, `format`, `state`, `stage`, `progress`, `error_code`,
`cancel_requested`, `audio_id`, `duration_secs`, `script_id`, `created_at`,
`updated_at`, `user_id`, `school`) biri eksikse kayıt uyarı ile atlanır. Yerel
depo artık caller'a görünen tek kaynak DEĞİLDİR: bozuk bir kayıt yalnızca bu
makinedeki teşhis bilgisini kaybettirir, çağıranın gördüğü satır backend'de
durur ve yeni bir submit onu yeniden açar.

Dosya adı ile içerideki `job_id` uyuşmayan kayıtlar açılışta atlanır; `job_id`
hiçbir zaman kullanıcı girdisinden dosya yoluna çevrilmez (arama bellekteki
sözlük üzerinden yapılır), bu yüzden yol kaçışı mümkün değildir.

### Açılış süpürmesi

Açılışta `running` durumundaki işler `failed` + `error_code: "interrupted"`
olarak işaretlenir. Gerekçe: o işi koşan iş parçacığı artık yoktur, dolayısıyla o
işi bitirecek kimse kalmamıştır — `running` bırakmak sonsuza kadar cevap
bekleyen bir kayıt üretirdi. Bu, backend'in kendi desenini birebir kopyalar
(`hezarfen_backend/src/migration_sql.rs`, `chatbot_message` boot süpürmesi;
orada da kod `interrupted`'dır).

`queued` işler **süpürülmez** — henüz kimse onlara dokunmadı, bilgi
kaybolmadı — açılışta yeniden kuyruğa alınır.

### İptal işbirlikçidir

`podcast.cancel` bir **bayrak** koyar. İşçi `JobContext.check()` çağırdığı her
noktada bayrağı okur; gördüğü anda `JobCancelled` fırlar ve iş `cancelled`
durumuna geçer. Thread zorla öldürülmez: Python'da bir thread'i dışarıdan
öldürmenin güvenli yolu yoktur ve gerçek hat yarıda kesilirse yarım dosyalar
bırakır.

**İptal kaybolmaz.** İptal, koşan aşamanın bitmesini bekler; ya bir aşama
sınırında (`check()`) ya da **işin bitişinde** yakalanır. `finish()` `done`'a
geçmeden **aynı kilidin altında** `cancel_requested` bayrağını okur; bayrak
konmuşsa iş `done` yerine `cancelled` olur ve `audio_id` yazılmaz. Bu, `cancel`
çağrısına `cancelled=true` deyip işi yine de `done` bitirme yarışını kapatır —
sahte hatta o pencere mikrosaniyeydi, gerçek hatta **45 dakika** genişliğinde
olurdu.

Kuyruktaki bir iş için iptal anında etki eder; işçi işi almadan önce
`cancelled` görüp hiç başlamaz.

### `JobContext` — gerçek hattın iptal ve ilerleme kanalı

`runner` imzası `Callable[[JobContext], None]`'dır. `begin()` çağrısını **işçi
yapar**, runner değil; `begin()` `False` dönerse (iş kuyrukta değil, ya da
başlamadan iptal edildi) runner hiç çağrılmaz.

| Üye | Anlamı |
| --- | --- |
| `ctx.job_id` / `ctx.source_id` / `ctx.source_key` / `ctx.school` / `ctx.format` | kaydın kimliği, kaynak kimliği, kaynak blob anahtarı, okul uuid'si, formatı |
| `ctx.stages` | `JobStore.stages` (aşama adları) |
| `ctx.cancelled()` | iptal bayrağı **veya** servis kapanıyor mu |
| `ctx.check()` | iptal varsa `JobCancelled` fırlatır, yoksa `None` |
| `ctx.progress(stage, value)` | `stage` + `0.0-1.0` ilerleme yazar |
| `ctx.finish(audio_id, duration_secs, script_id, audio_ids=None, script_ids=None)` | işi bitirir (iptal varsa `cancelled` döner). Son iki argüman **isteğe bağlıdır** ve verilmezse tekil kimliklerden türetilir — çok bölümlü koşu için eklendi, eski çağrılar aynen çalışır |

İşçi `JobCancelled`'ı yakalar ve `abort()` çağırır; başka her `Exception`
`failed` + `error_code: "internal"` olur.

## Mod kapısı: `PODCAST_MODE`

Servis **iki modda** koşar ve hangisinde olduğunu açılışta **tek satırla söyler**.
Sessiz sahte üretim yasaktır: sahte modda uyarı basılmadan iş kabul edilmez.

| `PODCAST_MODE` | koşan | açılış satırı |
| --- | --- | --- |
| `simulate` (varsayılan) | `jobs.py::_simulate_pipeline` | `uyari: SAHTE hat kosuyor, uretilen ses GERCEK DEGIL` |
| `real` | `pipeline.py::build_runner(...)` → `router.hat.Hat` | `GERCEK hat bagli: kaynak=... cikti=... tts=...` |

`real` modda **yanlış yapılandırma boot'u çökertir** (exit 2), iki ayrı kapıdan:

- `PODCAST_PIPELINE_PATH` tanımsız, dizin değil, ya da altında `router/hat.py` yok
- yol doğru ama `import router.hat` patlıyor (eksik bağımlılık, sözdizimi hatası…)

Geçersiz bir `PODCAST_MODE` değeri `ConfigError` → exit 2 verir; `config.py`'nin
diğer parser'larıyla aynı davranış.

`import router.hat` **tembeldir**: `pipeline.py` modül seviyesinde hattı import
etmez, yalnızca `load_pipeline()` içinde eder. Bu yüzden `simulate` modda
(ve `--validate` içinde) gerçek hat **hiç yüklenmez** — `--validate` bunu
`sys.modules`'e bakarak doğrular.

## Gerçek hat (`src/pipeline.py`)

Runner `Hat`ı tek parça olarak kurar ve koşturur:

```python
Hat(pdf=..., format=ctx.format, gunluk=<kanca>, cikti_koku=PODCAST_OUTPUT_ROOT,
    bolum_limiti=PODCAST_CHAPTER_LIMIT, motor_adi=PODCAST_TTS_ENGINE,
    ses_uret=True).kos()
```

### Tek dikiş yeri `gunluk` geri çağırımıdır

`Hat`'ın dışarıdan sürülebilir tek noktası `gunluk` geri çağırımıdır. On bir
çağrı yerinin çoğu `hat.py::_adim_ekle`'dendir (yani **her adım
BİTTİĞİNDE**), ama hepsi değil: `kos()` açılış afişi (`:725-733`), OCR
bildirimi (`:258`) ve TTS motor yükleme satırları (`:639`, `:643`) de kancayı
tetikler. Sonuç iyi huylu — `update_progress` idempotent olduğu için aynı oran
tekrar yazılır — ve iptal gecikmesini bir miktar **azaltır**. Kanca:

1. `ctx.cancelled()` ise `KasitliKesme` fırlatır,
2. değilse `hat.sonuc.adimlar` **canlı listesinin uzunluğundan** ilerleme
   hesaplar ve `ctx.progress(<aşama>, <oran>)` yazar,
3. mesajı `config.log("debug", ...)` ile geçirir.

Log satırı **ayrıştırılmaz** — biçimi hattın iç meselesidir, kırılgan olurdu.
Tamamlanan adım sayısı doğrudan `sonuc.adimlar` listesinden okunur.

**SINIR — iptal yalnızca adım sınırında yakalanır.** `script` adımı ölçülen
koşuda tek başına ~34 dakika sürebilir; o adımın *ortasında* iptal görülemez,
çünkü hattın içinde başka bir kanca yoktur. `podcast.cancel` bu yüzden
*hemen* değil, **koşan adım bittiğinde** etki eder. İş yine de doğru biter
(`cancelled`), sadece gecikir. Bunu kapatmanın tek yolu hattın kendisine ara
kontrol noktası eklemektir; bu adım 3'ün kapsamında bilinçli olarak yapılmadı.

**SATIR SIRASI BAĞIMLILIĞI — sahibi olmadığımız bir dosyada.** Kanca
`KasitliKesme` fırlattığında `hat.py:750`'deki `except KasitliKesme` bloğu
çalışır; o blok `kosu_kapat("kesildi")`'i **751'de**, `_adim_ekle`'yi
**753'te** çağırır. `_adim_ekle` kancayı yeniden tetikler ve kanca ikinci kez
fırlatır — ama koşu kaydı o an **zaten kapanmıştır**. 751 ile 753 yer
değiştirirse resume kaydı açık kalır ve `kosu_kapat` hiç çağrılmaz. Bu
sıralama ampirik olarak doğrulandı; `hat.py` değişirse yeniden sınanmalıdır.

### Neden `KasitliKesme`, neden kendi istisnamız değil

`KasitliKesme` (`hat.py:59`) hattın **kendi bilinçli-kesme** istisnasıdır ve
`kos()` onu ayrıca yakalayıp koşuyu `kayit.kosu_kapat("kesildi")` ile kapatır —
`"hata"` ile **değil**. Kancadan kendi istisnamızı fırlatsaydık `kos()` onu
`BaseException` dalında yakalar, koşuyu `"hata"` diye kapatır ve **resume
semantiği bozulurdu**: yeniden başlatılan iş bitmiş adımları atlayamazdı.
Bu yüzden kanca `KasitliKesme` fırlatır; runner onu yakalar ve
`ctx.cancelled()` doğruysa `jobs.JobCancelled`'a çevirir (işçi bunu `cancelled`
olarak kapatır). `ctx.cancelled()` yanlışsa kesme **hattın kendisinden**
gelmiştir (`--kes <adim>` resume sınaması) ve yeniden fırlatılır → `failed`.

### Çıktı eşlemesi

`HatSonucu` → yerel kayıt (çağıranın gördüğü cevap backend satırıdır; ses
`BlobUploadRequest` ile yüklenir):

| cevap alanı | kaynak |
| --- | --- |
| `audio_id` | `mp3_yollari[0]`'ın `PODCAST_OUTPUT_ROOT`'a göre **relatif yolu** (posix) |
| `script_id` | `script_yollari[0]`'ın relatif yolu |
| `duration_secs` | `ses_toplam_sn` |
| `audio_ids` | tüm `mp3_yollari`'nın relatif yolları |
| `script_ids` | `audio_ids`'in bölümlerine **hizalanmış** script yolları |

**Kimlik neden dosya adı DEĞİL — ölçülen çarpışma.** Hat çıktıyı düz durmaz,
`<pdf_stem>/ses/<format>/<pdf_stem>-b01.mp3` şeklinde yazar
(`router/hat.py:187` + `:605`). Sadece dosya adı alınırsa üç dizin seviyesi
kaybolur **ve** aynı PDF'in `duz_okuma` ile `tek_ogretici` koşusu
**AYNI** kimliği döner — `bolum_id` formattan bağımsızdır
(`script/kapsam.py:251`). Backend o kimlikle dosyayı bulamaz, üstelik iki iş
birbirini gölgeler. Bu yüzden kimlik çıktı köküne göre relatif yoldur; kök
dışında kalan bir yol dosya adına düşer. Konteyner içi mutlak yol yine
sızdırılmaz.

**`script_ids` neden hizalanıyor.** `bolum_limiti` yalnızca **sese**
uygulanıyor (`router/hat.py:596`); `script_yollari` her zaman TÜM bölümleri
taşır. Varsayılan `PODCAST_CHAPTER_LIMIT=1` + 3 bölümlü bir PDF'te listeler
1'e 3 çıkardı ve tüketici 1:1 eşleme varsayarsa yanlış script'i yanlış sese
bağlardı. Servis script'leri üretilen ses bölümlerine filtreler; eşleşme hiç
tutmazsa filtrelemeden döner ve **uyarı loglar**.

`PODCAST_CHAPTER_LIMIT` 1'den büyükse (ya da `0`/`-1` = hepsi) birden fazla MP3
üretilir. Kablo sözleşmesi **kırılmadı**: `audio_id`/`script_id` tekil kaldı ve
ilk bölümü gösteriyor; çoğul listeler **eklendi**. Alan eklemek geriye dönük
uyumludur, yeniden adlandırmak değildir.

**`duration_secs` ve önbellek.** `router/hat.py:622`'deki önbellek yolu
MP3'ü `mp3_yollari`'na ekliyor ama `ses_toplam_sn`'i artırmıyordu; taze yol
(`:661-662`) ikisini de yapıyordu. Sonuç: aynı PDF+format ikinci kez
istendiğinde `duration_secs` **0.0** dönüyordu. Önbellek bu hattın tasarım
ilkesi olduğu için bu kenar durum değil, ikinci koşudan itibaren varsayılan
davranıştı. **Hattın kaynağında düzeltildi** (süre `ses.dogrula.sure_olc` ile
ffprobe'dan ölçülüyor). Servis ayrıca `audio_ids` dolu ama `duration_secs`
sıfırsa uyarı loglar.

### Hata kodları

| `error_code` | ne zaman |
| --- | --- |
| `source_not_found` | `resolve_source` reddetti (`source_key` ya da okul uuid'si kaçışı), `PODCAST_MEDIA_ROOT/<school>/<source_key>` dosyası yok, ya da `Hat` kurulurken `HatHatasi` |
| `no_audio` | hat koştu ama `mp3_yollari` **boş** — sessizce boş cevap dönmek yerine iş `failed` olur |
| `internal` | başka her istisna (işçinin genel dalı) |
| `interrupted` | açılış süpürmesi (süreç koşan işin ortasında öldü) |

`no_audio` bilinçli bir kapıdır: `--ses-yok` benzeri bir yapılandırma ya da TTS
çöküşü sessizce "başarılı ama sessiz" bir iş üretmesin diye.

## Sahte hat

`PODCAST_MODE=simulate` iken `src/jobs.py` içindeki
`_simulate_pipeline` fonksiyonu aşamaları taklit eder:

```
ocr → plan → script → tts → mux
```

Her aşama arasında `PODCAST_STAGE_SECS` kadar beklenir ve `progress` güncellenir.
İş bittiğinde `audio_id`, `duration_secs`, `script_id` üretilir — üretilen
kimlikler `sim_` önekiyle başlar, böylece sahte çıktı gerçek çıktıyla asla
karıştırılamaz.

Bu bilinçli bir karardır: **yaşam döngüsü 45 dakika beklemeden
kanıtlanabilsin diye.** `PODCAST_STAGE_SECS=0.0` ile tüm döngü milisaniyeler
içinde koşar, ki `--validate` tam olarak bunu yapar.

`bridge.main()` moda göre `build_store(settings, runner=..., stages=...,
job_secs=...)` çağırır; kuyruk, kalıcılık, iptal ve süpürme her iki modda da
aynıdır. Aşama adları:

| mod | `stages` | otomatik `job_secs` |
| --- | --- | --- |
| `simulate` | `ocr → plan → script → tts → mux` | aşama sayısı × `PODCAST_STAGE_SECS` (en az 1 sn) |
| `real` | `ingest → scriptler → quiz → ses` | **2700 sn (45 dk)** — ölçülen gerçek süre, `pipeline.REAL_ETA_SECS` |

`PODCAST_ETA_SECS` verilmişse her iki modda da o kazanır.

`resolve_source(media_root, school, source_key)` `PODCAST_MEDIA_ROOT` altındaki
`<school>/<source_key>` dosya yolunu üretir: okul uuid'si `^[a-z0-9-]{1,64}$`,
`source_key` ise `^[A-Za-z0-9._-]{1,128}$` desenine uymalıdır; uymayan girdi
`ValueError` alır. Her iki segment için de `Path.resolve()` sonrası sonucun
medya kökünün **altında** kaldığı ayrıca doğrulanır (symlink kaçışı için; `..`
deseni geçse bile burada takılır). Dosyanın **var olup olmadığına bakmaz**;
varlık denetimini `pipeline.resolve_pdf()` yapar ve dosya yoksa iş `failed` +
`error_code: "source_not_found"` olur.

## Kaynak belge biçimleri (`src/extract.py`)

Servis artık PDF'in yanında Word, PowerPoint, OpenDocument ve düz metin okur.
Okunabilen her biçim aynı yoldan geçer: metin çıkarılır, bölümlere ayrılır,
seslendirilir — yani biçim yalnızca **ilk adımı** değiştirir.

### Tür uzantıdan DEĞİL, içerikten belirlenir

Backend kaynağı bir blob anahtarıyla veriyor (`source_key`) ve dosya diskte
`<PODCAST_MEDIA_ROOT>/<okul>/<source_key>` altında **uzantısız** duruyor — anahtar
bir kimlik, dosya adı değil. Bu yüzden `sniff()` sihirli baytlara bakar:

| İmza | Sonuç |
| --- | --- |
| `%PDF-` | `pdf` |
| `PK\x03\x04` + `word/document.xml` | `docx` |
| `PK\x03\x04` + `ppt/slides/slideN.xml` | `pptx` |
| `PK\x03\x04` + `mimetype` = OpenDocument text/presentation | `odt` / `odp` |
| `PK\x03\x04` + `xl/workbook.xml` veya OpenDocument spreadsheet | `sheet` (reddedilir) |
| `{\rtf` | `rtf` (reddedilir) |
| `\xd0\xcf\x11\xe0...` | `ole` — eski ikili `.doc/.ppt`; `antiword`/`catppt` ile okunur |
| NUL içermeyen, çözülebilen metin | `text` |

Bunun ölçülmüş sonucu: `ders.pdf` adlı ama içeriği docx olan bir dosya **docx**
olarak okunur. Test bunu kilitler (`test_the_source_is_never_identified_by_its_name`).

### Uzantı türevleri kendiliğinden çalışır

Tür içerikten geldiği için makro etkin ve şablon türevleri **ek kod istemez**;
iç yapıları aynıdır:

```
.docm .dotx .dotm          -> docx gibi okunur
.pptm .potx .potm .ppsx .ppsm  -> pptx gibi okunur
```

Bunlar varsayım değil, `RelatedExtensionsNeedNoExtraCode` ile sınanır.

### Reddedilen biçimler sebebini ve çıkış yolunu söyler

Sessizce "bilinmeyen" demek yerine her red ne yapılacağını yazar:

| Biçim | Mesaj |
| --- | --- |
| `.xlsx/.ods` | "hesap tablosu anlatima uygun degil; ders metnini belge olarak yukleyin" |
| `.rtf` | "rtf desteklenmiyor; .docx veya .odt olarak kaydedin" |

Hata kodu `unsupported_source`'tur (18 karakter; backend `error_code` için 64
karakter sınırı koyar, test bunu da doğrular).

### Eski ikili .doc/.ppt: antiword + catppt

OLE imzası gören yol `antiword`'ü (.doc), o olmazsa `catppt'i (.ppt)`
çalıştırır; 60 saniye zaman aşımıyla. Çıktı baytları sırayla utf-8 ve
cp1254 çözülür (Türkçe eski belgeler için). İki araç da kurulu değilse
`extractor_unavailable`, ikisi de okumazsa `source_unreadable`, çıktı boşsa
`no_text_layer` döner — çok kaynaklı işte bu bir **atlama**dır (aşağıda).
Araçlar Containerfile'a `antiword` + `catdoc` paketleriyle girer; ikisi de
küçüktür ve LibreOffice gibi ağır bir bağımlılık istemez.

### Yeni pip bağımlılığı YOK

DOCX, PPTX, ODT ve ODP hepsi zip + XML'dir; `zipfile` ve `xml.etree` stdlib'de
vardır. `requirements.txt` değişmedi (`aioquic`, `pymupdf`). Tek bağımlılık
değişimi apt tarafındadır: `antiword`, `catdoc`.

### Zip bombası koruması

Sıkıştırılmış bir belge açıldığında belleği doldurabilir. `_guard_size()`
okumadan ÖNCE bildirilen boyuta bakar: üye başına 16 MiB, toplam 64 MiB. Düz
metin için ayrı 16 MiB tavanı vardır.

### PPTX slayt sırası sayısaldır

`slide10.xml` alfabetik olarak `slide2.xml`'den ÖNCE gelir. Slayt adları
sayıya çevrilip sıralanır; aksi hâlde anlatım 1, 10, 11, 2 sırasıyla akardı.

### Bilinen sınır: görüntü-only sunumlarda OCR yok

PDF yolunda tarama sayfaları için OCR yedeği vardır (`get_textpage_ocr`).
PPTX/ODP yolunda **yoktur**. Ölçülen gerçek örnek: 36 slaytlık bir sunumda
hiç `<a:t>` düğümü yok, 145 medya dosyası var — yani slaytlar görüntü.
Bu durumda iş sessizce boş üretmez, `no_text_layer` ile açıkça düşer.

Konuşmacı notları da okunmaz. Ölçüldü: o sunumdaki 36 notun hepsi doluydu ama
içerikleri yalnızca slayt numarasıydı ("1", "10"); körü körüne eklemek anlatıma
çöp sokardı.

Gövde dışı metin (üstbilgi, altbilgi, dipnot) alınmaz. 90 gerçek docx'te
ölçüldü: gövdede 1.120.077 karakter, gövde dışında 342 düğüm — binde üçten az.

### Gerçek dosyalarla ölçüm

369 gerçek Office dosyası tarandı, **çıkarım hatası sıfır**:

| Uzantı | Dosya | Tanınan | Okuma hatası |
| --- | --- | --- | --- |
| `.docx` | 283 | 257 | 0 |
| `.pptx` | 58 | 56 | 0 |
| `.doc` | 28 | 23 | 0 |
| `.txt` | 400 örnek | 400 | 0 |
| `.md` | 144 | 142 | 0 |

Tanınmayanların tamamı 0–165 baytlık artık dosyalardı, gerçek belge değil.
Ölçüm `.doc` henüz reddedilirken yapıldı; bugün 23 tanınan dosya
`antiword` yoluna girer.

## Çoklu kaynak: N belge, tek podcast

Bir iş birden çok belgeyi birleştirip **tek** podcast üretebilir. Belgeler
farklı biçimlerde olabilir — bir docx, bir pptx ve bir odt aynı işte toplanabilir.

### Sözleşme: sources listesi, tekil alan geriye dönük uyumluluk içindir

`audio_id`/`audio_ids` çiftinde olduğu gibi tekil alan yerinde kaldı:

```
source_key   TEKIL kaldi, ilk belgeyi gosterir (bir surum uyumlulugu)
sources      EKLENDI, tum belgeler: [{key, name, content_type}] (en fazla 10)
```

Servis `sources`'ı tercih eder; gelmezse `source_key`'ten tek girdili bir
liste türetir — yani **eski yük eski davranışı üretir**. `sources`'un ilk
öğesi verildiyse `source_key` ile aynı olmak zorundadır.

`sources` `REQUIRED_FIELDS`'a **eklenmedi**. Eklenseydi, alan eklenmeden önce
diske yazılmış işler açılışta topluca atılırdı; `JobContext` alanı yoksa
tekilden türetir.

### Doğrulama

`podcast.submit` şu durumlarda `bad_request` döner: `sources` liste değilse,
boşsa, bir üye nesne değilse, `key` `source_key` güvenlik desenini
(`^[A-Za-z0-9._-]{1,128}$`) geçmiyorsa, aynı kaynağı tekrar ediyorsa, ilk
öğesi `source_key` ile uyuşmuyorsa, 10'u aşıyorsa — ya da `sources` da
`source_key` da yoksa. `name` boşsa anahtara düşer; `content_type` yalnızca
kayıt amaçlıdır, tür her zaman içerikten gelir.

### Birleştirme

Belgeler **gönderildikleri sırayla** okunur ve metinleri belge başına
`\n\n=== <name> ===\n\n` başlığıyla ayrılarak birleştirilir; sonra mevcut
bölümleme ve seslendirme aynen çalışır. Tek kaynaklı bir iş başlıksız,
`extract_text` ile birebir aynı sonucu üretir — test bunu kilitler.

### Bir belge okunamazsa İŞ DEĞİL, o belge ATLANIR

Kaynağın çıkarımı başarısızsa iş düşmez; o belge atlanır ve iş kalanlarla
sürer. Durum `podcast.report` yüküne eklenen `sources` alanında taşınır:
her girdi `{key, name, status}`; `status` ya `ok` ya `skipped:<kod>`
(`extractor_unavailable` / `source_unreadable` / `no_text_layer`
/ `unsupported_source`). Tümü atlanırsa ya da birleşik metin
`PODCAST_MIN_TEXT_CHARS`'ın altında kalırsa iş mevcut kodlarla düşer
(sırayla: ilk kaynağın atlama kodu, `no_text_layer`). Bu, donmuş sözleşme
kararıdır: on belgeden biri bozuksa kullanıcı dokuz belgelik bölümu ve
hangi belgenin eksildiğini rapordan görür.

İptal belgeler arasında denetlenir; 10 belgelik bir iş iptal edildiğinde
hepsinin bitmesi beklenmez.

### Backend tarafı

Backend şeridi aynı sözleşmeyi uyguluyor: kapı (`POST /podcast/jobs`) yalnızca
notun eki hiç yoksa ya da blob'ları diskte yoksa 409 `source_missing`
der; yük `sources` + tekil `source_key` (ilk kaynağın anahtarı) taşır ve
`podcast.report` artık `sources` alanını içerir. Servis bu sözleşmeye hazırdır
ve alanlar eski haliyle gelirse legacy yolla çalışmayı sürdürür.


## İki motor (`PODCAST_ENGINE`)

Gerçek işi iki motordan biri koşar; seçim **yalnızca `.env`** iledir:

| | `api` — **varsayılan** | `local` — bugünkü yol |
| --- | --- | --- |
| Ne koşar | Yerli hat: PyMuPDF metin çıkarma (+ gerekirse tesseract OCR) → script (OpenAI-uyumlu LLM; `duz_okuma` LLM'siz) → ElevenLabs HTTP TTS → ffmpeg mux | `router.hat.Hat` (vendored ağaç) |
| Ağaç gerekir mi | **hayır** (`vendor/pipeline` olmadan imaj derlenir ve koşar) | **evet**; yoksa `real` modda açılış exit 2 |
| Bağımlılıklar | imajda (yalnız `pymupdf` + tesseract) | `PODCAST_LOCAL_VENV` hacmi; ağır bağımlılıklar (torch/transformers/easyocr) imaja **GİRMEZ**, `bash deploy/setup-local-engine.sh` ile hacme kurulur |
| Anahtarlar | `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` (her iş için), `LLM_API_KEY` (yalnız LLM formatları) | `PODCAST_TTS_ENGINE` + `SES_BULUT_IZINLI`; ağırlıklar `/models/ses_modelleri` altında |

Motor seçimi **yalnızca `.env` + restart** ile değişir: imaj ve workflow aynı
kalır (imajda motor argümanı YOKTUR; `podman run`/compose aynı digest'i kullanır).

**Dürüst sınır:** `local` motorun kendi kaynağı — vendored `router/hat.py` ağacı —
**artık hiçbir repoda yok** (eski kopya `C:\PROJECTS\podcast` ile kayboldu).
Yani `local` yapısal olarak duruyor ve seçilebiliyor, ama bugün **koşulamaz**;
`PODCAST_PIPELINE_PATH` gerçek bir ağacı göstermedikçe açılış `exit 2` verir.
`deploy/setup-local-engine.sh` yalnızca bağımlılıkları/аğırlıkları kurar, kaynağı
getirmez.
| Aşamalar | `kaynak → metin → script → tts → mux` | `ingest → scriptler → quiz → ses` |
| ETA (otomatik) | 300 sn | 2700 sn |

`PODCAST_ENGINE` bilinmeyen bir değer alırsa boot **çöker** (exit 2) — sessizce
diğer motora düşmez. Eksik anahtar boot'u çökertmez: iş, tts/llm aşamasında
**net bir hata koduyla** `failed` olur (`tts_key_missing`, `tts_voice_missing`,
`tts_error`, `tts_timeout`, `tts_bad_audio`, `llm_error`, `llm_unavailable`,
`llm_timeout`, `llm_empty`); yarım mp3 `done` diye işaretlenmez.

## Anahtarsız çalışma

`api` motoru LLM için `LLM_API_KEY` (OpenAI-uyumlu `LLM_BASE_URL` + `LLM_MODEL`
ile birlikte) kullanır. Ev kuralı gereği bu **eksik** yapılandırmadır,
**yanlış** değil — dolayısıyla boot'u çökertmez, yalnızca ilgili özelliği kapatır:

| değer | anlam | anahtarsız |
| --- | --- | --- |
| `duz_okuma` | varsayılan | **çalışır** |
| `tek_ogretici` | tek anlatıcı | `llm_unavailable` |
| `ogrenci_hoca` | iki sesli | `llm_unavailable` |

Gerekçe: `duz_okuma` **düz okuma**dur — `api` motoru bu formatta LLM'e hiç
gitmez (metin doğrudan okunur), `local` motorda da anahtarsız sonuna kadar
koşar. Diğer iki format LLM'e bağlıdır. Hiçbir formatta uydurma içerik üretilmez.

`llm_unavailable` mesajı hangi formatların kullanılabilir olduğunu **söyler**;
çağıran tarafın tahmin etmesi gerekmez. Açılışta da tek satır log basılır:

```
[bridge] acik formatlar: duz_okuma (LLM_API_KEY tanimsiz; tek_ogretici, ogrenci_hoca kapali)
[bridge] acik formatlar: tek_ogretici, ogrenci_hoca, duz_okuma (LLM_API_KEY tanimli)
```

**Anahtarın kendisi hiçbir yerde loglanmaz, hata mesajına girmez, diske
yazılmaz.** `Config` yalnızca `has_llm_key` booleanını tutar; anahtar değeri
process'te hiç saklanmaz. Yapılandırma özetinde görünen tek şey
`llm_anahtari=tanimli` ya da `llm_anahtari=TANIMSIZ`'dır.

**`format` yalnızca şu üç değeri alır** ve bu küme podcast projesinin kilitli
sözleşmesidir; gerçeğin kaynağı `script/plan.py` içindeki `Format` enum'u ve
`router/cli.py`'nin `choices` listesidir. Buradaki liste onun kopyasıdır ve
`--validate` kopyanın bozulmadığını denetler. Enum değişirse **ikisi birden**
değişmelidir.

## Ortam değişkenleri

| Değişken | Varsayılan | Anlamı |
| --- | --- | --- |
| `AI_BRIDGE_HOST` | `hezarfen_backend` | QUIC ile bağlanılacak host (backend konteyner adı) |
| `AI_BRIDGE_PORT` | `8090` | QUIC portu (UDP) |
| `AI_BACKEND_URL` | `http://hezarfen_backend:7656` | Sertifika için HTTP kökü |
| `AI_SHARED_TOKEN` | **yok** | Backend ile aynı paylaşılan sır. Tanımsızsa köprü açılışta çıkar (exit 2) |
| `AI_TLS_SERVER_NAME` | `localhost` | Sertifikanın SAN'ı |
| `AI_SERVICE_NAME` | `podcast` | Hello'daki servis adı |
| `AI_MAX_CONCURRENT` | `2` | Aynı anda kabul edilen **istek**; aşılırsa `busy` |
| `AI_RECONNECT_SECS` | `3` | Kopunca ilk yeniden deneme aralığı; her başarısız denemede ikiye katlanır |
| `AI_RECONNECT_MAX_SECS` | `120` | Geri çekilmenin tavanı (jitter bu tavanın üstüne eklenir) |
| `AI_TLS_FINGERPRINT` | boş | Pinlenen sertifika SHA-256'sı (hex; iki nokta ayraçları atılır). Boşsa TOFU ve uyarı loglanır |
| `LOG_LEVEL` | `info` | `debug`/`info`/`warn`/`error` |
| `PODCAST_JOB_ROOT` | `/data/podcast/jobs` | İş durumu dizini (kalıcı hacim) |
| `PODCAST_WORKERS` | `1` | Eş zamanlı **uzun iş** sayısı (ayrı havuz) |
| `PODCAST_MAX_JOBS` | `8` | Kuyruk + koşan iş tavanı; aşılırsa `busy` |
| `PODCAST_STAGE_SECS` | `0.2` | Sahte aşama süresi (gerçek hat bağlanınca anlamsızlaşır) |
| `PODCAST_ETA_SECS` | `0` | Bir işin tahmini süresi (sn). `0` = otomatik (aşama sayısı × `PODCAST_STAGE_SECS`, en az 1). Gerçek hat için `2700` gibi bir değer verilir; aralık `0..86400` |
| `PODCAST_MEDIA_ROOT` | `/data/files` | `source_key`'in `<school>/<source_key>` altında çözüleceği paylaşılan dosya kökü; backend'in `FILES_PATH` değeriyle **aynı** olmalı |
| `PODCAST_MODE` | `simulate` | `simulate` \| `real`. Başka değer → exit 2 |
| `PODCAST_ENGINE` | `api` | `api` \| `local`. Başka değer → exit 2 |
| `PODCAST_CHAPTER_CHARS` | `6000` | `api`: bolum basina en fazla karakter (`200..100000`) |
| `PODCAST_MIN_TEXT_CHARS` | `200` | `api`: altinda OCR denenir/kalirsa `no_text_layer` |
| `PODCAST_OCR_LANG` | `tur` | `api`: tesseract dili (`tesseract-ocr-tur`) |
| `PODCAST_PIPELINE_PATH` | **boş** | Yalnızca `local` motor: hat kaynağının kökü. `real`+`local` için zorunlu; yoksa/bozuksa exit 2 |
| `PODCAST_OUTPUT_ROOT` | `/data/podcast/out` | `Hat(cikti_koku=...)` — üretilen mp3/script/quiz kökü (kalıcı hacim) |
| `PODCAST_CHAPTER_LIMIT` | `1` | `Hat(bolum_limiti=...)`. `0` veya `-1` = **hepsi**; aralık `-1..4096` |
| `PODCAST_TTS_ENGINE` | `supertonic-3` | Yalnızca `local` motorun ONNX TTS motoru |
| `PODCAST_LEDGER_DB` | `/data/podcast/router.sqlite` | `router.kayit.Kayit` defteri: adım önbelleği, resume, LLM bütçe sayacı. compose'ta `/data/podcast/kayit/router.sqlite` (ayrı kalıcı hacim) |
| `HF_HOME` | *(imajda)* `/models/hf` | HuggingFace önbellek kökü; `podcast-models` hacmi buraya `:ro` bağlanır |
| `TORCH_HOME` | *(imajda)* `/models/torch` | torch önbellek kökü |
| `HF_HUB_OFFLINE` | `1` (compose) | HF hub'a **ağ çağrısı yok**. Ağırlık hacimde yoksa açık hata; sessizce 2,2 GB indirilmez |
| `LLM_BASE_URL` | `https://api.deepseek.com` | `api`: OpenAI-uyumlu LLM ucu (her sağlayıcı, kod değişmez) |
| `LLM_MODEL` | `deepseek-chat` | `api`: model id |
| `LLM_API_KEY` | **yok** | `api`: LLM anahtarı. Yoksa yalnızca `duz_okuma`; boot çökmez |
| `LLM_TIMEOUT_S` | `30` | `api`: tek LLM çağrısının zaman aşımı |
| `LLM_MAX_ATTEMPTS` | `3` | `api`: 429/5xx/timeout için deneme sayısı |
| `ELEVENLABS_API_KEY` | **yok** | `api`: bulut TTS anahtarı (yoksa işler `tts_key_missing` ile düşer) |
| `ELEVENLABS_VOICE_ID` | **boş** | `api`: ses kimliği (yoksa `tts_voice_missing`) |
| `ELEVENLABS_MODEL` | `eleven_flash_v2_5` | `api`: TTS modeli |
| `ELEVENLABS_BASE_URL` | `https://api.elevenlabs.io` | `api`: TTS ucu |
| `ELEVENLABS_LANGUAGE` | `tr` | `api`: `language_code` |
| `ELEVENLABS_OUTPUT_FORMAT` | `mp3_44100_128` | `api`: çıktı biçimi (`pcm_*` 403 verir) |

Yapılandırma kuralı (backend'in kendi kuralı): **yanlış** yapılandırma boot'u
çökertir (geçersiz port, geçersiz `LOG_LEVEL`, `PODCAST_WORKERS=abc`,
`PODCAST_MAX_JOBS=0`, eksik token → exit 2); **eksik** yapılandırma yalnızca
ilgili özelliği sessizce kapatır (`LLM_API_KEY`; `ELEVENLABS_*` eksikse işler
net hata koduyla düşer, sahte ses üretilmez).

Yorumlu şablon: **`.env.example`**. Kopyala, doldur, `.env` olarak kaydet.
`.env` `.gitignore`'dadır; `.env.example` kasten izlenir ve `.containerignore`
sayesinde imaja girmez.

Token asla loglanmaz. Sertifika parmak izinin yalnızca ilk 12 karakteri loglanır.

## Çalıştırma

```sh
python -m src.main --validate        # bütünlük kontrolü, ağ ve aioquic gerekmez
pip install -r requirements.txt
python -m src.bridge                 # köprüyü çalıştır (AI_SHARED_TOKEN şart)
```

```sh
python -m src.main --health          # canlilik: kokler yazilabilir mi, hat import edilir mi, kayit var mi
```

Container (Podman) — **tam yol `deploy/OKU.md`'dedir**:

```powershell
pwsh -File deploy/setup-pipeline.ps1        # hat kaynagi -> vendor/pipeline
pwsh -File deploy/setup-models.ps1   # agirliklar -> podcast-models hacmi
Copy-Item .env.example .env           # sonra icindeki AI_SHARED_TOKEN'i backend ile AYNI yap
                                      # (ortam degiskeni YETMEZ: compose `down`/`ps` de interpolate eder)
pwsh -File deploy/run-stack.ps1        # backend -> frontend -> chatbot -> podcast
podman logs -f hezarfen_text_to_podcast
```

### `--health` neyi denetler

Compose'un `healthcheck`'i budur. **Ağ çağrısı yapmaz**, ölçülen süre
**0,66 sn** (timeout 10 sn). Denetlediği:

- `PODCAST_JOB_ROOT` ve `PODCAST_OUTPUT_ROOT` gerçekten **yazılabilir mi**
  (mkdir + dosya yaz + sil; hacim root sahipliğinde oluştuysa burada patlar)
- `real` modda **hat içeri alınabiliyor mu** (`import router.hat`; ölçüldü 0,12 sn)
- köprü backend'e **kayıtlı mı**

Kayıt durumu iş kökündeki **`.bridge-status`** dosyasından okunur. Köprü bu dosyayı
`jobs.py`'nin **atomik** deseniyle (geçici dosya + `os.replace`) yazar: kayıt
başarılı olunca `registered=true`, bağlantı koptuğunda `false`. Dosya adı
`*.json` desenine uymadığı için açılıştaki iş taramasına karışmaz; artığı
`_purge_temporaries` süpürür.

**Bayatlık (staleness) denetimi bilerek YOK.** Köprü yalnızca durum
*değiştiğinde* yazar; sağlıklı ve saatlerce süren bir bağlantı hiçbir şey
yazmaz. Zaman aşımı koymak sağlıklı köprüyü hasta gösterirdi.

Çıkış kodu: `0` sağlıklı · `1` sağlıksız · `2` yapılandırma hatası.

### `--validate` neyi denetler

Ağ kullanmaz, `aioquic` import etmez, **gerçek hattı import etmez**,
`PODCAST_JOB_ROOT`'a **dokunmaz** (geçici dizinde çalışır) ve token yokken de
çalışır (uyarı basar, 0 döner) — imajın güvenli varsayılan `CMD`'si budur.

`PODCAST_MODE=real` ile çağrılırsa `--validate` de mod kapısını uygular:
`PODCAST_PIPELINE_PATH` yoksa/bozuksa açık bir satır basar ve **2** döner.
Yol geçerli olsa bile `--validate` hattı **import etmez**; yalnızca yolun
varlığını ve `router/hat.py`'nin orada olduğunu denetler.

Denetlediği kapılar:

- format kümesi ve varsayılanın kilitli sözleşmeden sapmadığı
- yetenek defteri, çerçeveleme gidiş-dönüşü, greeting ayrıştırma, deadline payı
- iş deposunun geçici bir dizine yazılıp okunabildiği, geçici dosya bırakmadığı
  ve deponun yeniden açılışta kaydı bulduğu
- durum makinesinin geçersiz geçişi (`queued → done`, `done → running`,
  bilinmeyen hedef) reddettiği
- anahtarsızken `tek_ogretici`/`ogrenci_hoca` submit'inin `llm_unavailable`
  döndüğü ve mesajın kullanılabilir formatları söylediği
- anahtarsızken `duz_okuma` submit'inin **kabul edildiği**
- anahtar varken `tek_ogretici`'nin kabul edildiği
- `PODCAST_MAX_JOBS` aşılınca `busy` döndüğü
- açılış süpürmesinin `running` işi `failed`/`interrupted` yaptığı, `queued` işe
  dokunmadığı
- uçtan uca yaşam döngüsü: submit → sahte hat → `done` → `result` gerçek
  kimlikler döner
- işbirlikçi iptal: koşan bir işin `cancel` sonrası `cancelled`'a geçtiği
- **iptal yarışı**: `finish()` çağrılmadan hemen önce gelen `cancel`'ın işi
  `done` değil `cancelled` bitirdiği ve bunun **diske de** yansıdığı
- **`queued → failed`** geçişinin yasal olduğu (başlamadan patlayan iş)
- **`resolve_source` yol kaçışı**: okul uuid'si ve `source_key` için `..`, `../x`,
  `a/b`, `/etc/passwd`, boş dize, 129 karakterlik ad, `..\x`, `.`, `OKUL` ve
  `okul_a` reddedilir; geçerli çift `<school>/<source_key>` altına çözülür
- **`PODCAST_ETA_SECS`** verildiğinde `estimate_eta`'nın sahte aşama süresini
  değil onu kullandığı, `stages` parametresinin dinlendiği
- **kayıt şeması**: zorunlu alanı eksik kaydın açılışta atlandığı, sağlam
  komşusunun etkilenmediği, `*.json.tmp*` artığının silindiği
- **kapanma**: işçi thread'lerinin `daemon` olduğu ve 8 saniyelik bir iş
  koşarken `shutdown(timeout=0.5)`'in takılmadan döndüğü
- **mod kapısı**: geçersiz `PODCAST_MODE` değerinin `ConfigError` verdiği
- **tembel import**: `simulate` modda `router.hat`'ın `sys.modules`'te
  **olmadığı** (yani `--validate` gerçek hattı hiç yüklemediği)
- **`real` yolu**: boş / var olmayan / `router/hat.py` içermeyen
  `PODCAST_PIPELINE_PATH` değerlerinin açık hata ile reddedildiği
- **bölüm limiti** normalleştirmesi: `0` ve `-1` → "hepsi", pozitif değer korunur
- **çıktı eşlemesi**: sahte bir `HatSonucu` benzeri nesneden `audio_id`,
  `script_id`, `duration_secs`, `audio_ids`, `script_ids` alanlarının doğru
  türetildiği (tam yol değil dosya adı; ilk bölüm tekil alanlarda)
- **iptal kancası**: `ctx.cancelled()` `True` iken `gunluk` kancasının kesme
  istisnası fırlattığı ve ilerleme **yazmadığı**; `False` iken adım sayısından
  doğru aşama/oran ürettiği (gerçek `Hat` olmadan, sahte bir ctx ile)
- **gerçek runner uçtan uca** (sahte bir `Hat` sınıfıyla, gerçek `JobStore` ve
  gerçek `capabilities` üzerinden):
  - iki MP3 üreten koşu → `done`, `audio_ids`/`script_ids` iki elemanlı
  - `mp3_yollari` boş → `failed` + `no_audio`, `audio_id` yazılmamış
  - kaynak dosya yok → `failed` + `source_not_found`

Beklenmedik bir istisna da bir bütünlük hatası olarak raporlanır; `--validate`
traceback ile ölmez.


## Testler

Geliştirme sırasında koşulan test paketi `tests/` altındadır. **Saf `unittest`;
`pytest` kullanılmaz** — ne `import pytest`, ne `conftest.py`, ne `pytest.ini`.
Bu, rule-based chatbot deposundaki desenin birebir kopyasıdır ve bilinçlidir:
testler standart kütüphaneden başka bir şey istemez, imajda ek bağımlılık yoktur.

```sh
python -m unittest discover -s tests -t .      # tüm paket (321 test, ~9 sn)
python -m unittest tests.unit.test_jobs        # tek dosya
python -m unittest tests.regress.regress_cancel_race -v
```

`tests/regress/` dosyaları `regress_*.py` adlandırıldığı için `discover`'ın
varsayılan `test*.py` deseniyle bulunmaz; `tests/regress/__init__.py` içindeki
`load_tests` bunları pakete katar. Tek komut yine de her şeyi koşar.

### `--validate` ile ilişkisi

`python -m src.main --validate` **çalışma-zamanı bütünlük kontrolüdür** ve
öyle kalır: container sağlık akışı onu kullanır, ağ ve `aioquic` istemez, tek
süreçte kendi geçici dizinini kurar. Test paketi onun yerine geçmez, **yanında**
durur: `--validate` "bu imaj şu an tutarlı mı" der, `tests/` ise "bu değişiklik
bilinen bir kusuru geri getirdi mi" der.

### Dizin yapısı

| Dizin | Ne sınar |
| --- | --- |
| `tests/unit/` | Tek modül davranışı: çerceveleme, ortam çözümleme, yetenek defteri, durum makinesi, çıktı eşlemesi (160 test) |
| `tests/integration/` | `submit -> status -> result` uçtan uca; sahte bir `Hat` sınıfı ile ama **gerçek** `JobStore`, gerçek işçi thread'leri, gerçek `capabilities.dispatch` (8 test) |
| `tests/regress/` | Her dosya **ölçülerek bulunmuş bir kusur**; düzeltme geri alınırsa kırmızıya döner (17 dosya, 131 test) |
| `tests/fuzz/` | Hatalı biçimlendirilmiş girdi altında ayrıştırıcı sözleşmesi: altı slayt kategorisi, şablon/bayt mutasyonu, gramer üretimi (13 test) |
| `tests/model/` | `JobStore`'un Mealy makinesi modeline uygunluğu; W yöntemiyle üretilen `V·W ∪ V·X·W` kümesi (9 test) |

### Regresyon dosyaları ve kapattıkları kusur

| Dosya | Kusur |
| --- | --- |
| `regress_format_vocabulary.py` | Format kümesi kilitli sözleşmeden sapmamalı. Gerçeğin kaynağı `script/plan.py` `Format` enum'u ve `router/cli.py` `choices` listesidir; test bu iki dosyayı **okuyup** karşılaştırır. Yokluk ile **kayma** ayrılır: hat kökü bulunup da dosya yoksa **açık hata** (yol değişmiş demektir); hat kaynağı hiçbir yerde yoksa (CI'da `vendor/` gitignore'da olduğu için normaldir) test **atlanır** ve `PIPELINE_REPO`'yu ayarlamayı söyler. Koruma yol kayması için vardır, yokluğu cezalandırmak için değil. |
| `regress_unanswered_stream.py` | Ayrıştırılamayan istek cevapsız bırakılmamalı; backend cevapsız akışı deadline boyunca bekler ve worker kiralamasını tutar. 8 MiB aşan cevap sessizce yutulmamalı, `frame_too_large` err'ine düşürülmeli. |
| `regress_cancel_race.py` | `finish()` iptal bayrağını **aynı kilit altında** okumalı; bayrak setken `done` değil `cancelled` dönmeli ve `audio_id` yazılmamalı. Bellekte ve **diskte** aynı sonuç. Ölçülen: düzeltmeden önce bellek=`done` disk=`done` `audio_id=a1`. |
| `regress_queued_failed.py` | `queued -> failed` geçişi yasal olmalı. Yoksa `begin()` öncesi patlayan iş sonsuza kadar `queued` kalır, her açılışta yeniden kuyruğa alınır ve `max_jobs` kotasından bir slotu kalıcı yer. |
| `regress_daemon_shutdown.py` | İşçi thread'ler `daemon` olmalı ve `shutdown(timeout=)` sınırlı sürede dönmeli. Ölçülen: `ThreadPoolExecutor` ile süreç 8139 ms yaşıyordu, daemon thread'lerle 57 ms. |
| `regress_identifier_path.py` | `audio_id`/`script_id` çıktı köküne göre **relatif yol** olmalı, dosya adı değil. Gerçek yol `<stem>/ses/<format>/<stem>-bNN.mp3`; sadece dosya adı alınırsa üç dizin seviyesi kaybolur **ve** aynı PDF'in `duz_okuma` ile `tek_ogretici` koşusu aynı kimliği döner (bölüm_id formattan bağımsız). Kök dışı kalan yol dosya adına düşer. |
| `regress_script_alignment.py` | `bolum_limiti` yalnızca **sese** uygulanıyor; `script_yollari` her zaman tüm bölümleri taşır. 1 ses + 3 script → `script_ids` 1 elemana filtrelenmeli. Eşleşme hiç tutmazsa filtrelenmeden dönmeli (ve uyarı loglanmalı). |
| `regress_absolute_path.py` | `job_root`/`output_root`/`media_root`/`ledger_db` **mutlak** olmalı. Ölçülen kusur: göreli çıktı kökü verildiğinde hattın ffmpeg concat listesi yolu ikiye katlıyor ve montaj patlıyor. |
| `regress_path_traversal.py` | `resolve_source` şunları reddetmeli: `..`, `../x`, `a/b`, `/etc/passwd`, `..\x`, `.`, boş ad, 129 karakterlik ad, `\\srv\share`, `C:\Windows`, NUL içeren ad; ayrıca okul uuid'si için `OKUL`, `Okul-A`, `okul_a`, `okul.a`, 65 karakterlik ad. Geçerli çift (okul + anahtar) kabul edilir ve sonuç `media_root` **altında** kalır. |
| `regress_schema_validation.py` | Eksik alanlı iş kaydı açılışta atlanmalı (`KeyError` değil). `REQUIRED_FIELDS`'a `audio_ids`/`script_ids` **eklenmemeli** — eklenirse alan eklenmeden önce yazılmış eski kayıtlar topluca atılır. `source_key` ise bilinçli olarak zorunludur: onsuz bir iş kaynağa hiç çözülemez. |
| `regress_tmp_residue.py` | Geçici dosya adı pid içerir; açılışta `*.json.tmp*` artıkları temizlenir; atomik yazma temp + `os.replace` ile yapılır. Ayrıca temizleme mantığı **kopyaladığı dosyaları silmemeli** (PowerShell `-Include` tuzağının Python karşılığı: filtre gerçekten uygulanıyor mu). |
| `regress_secret_leak.py` | `DEEPSEEK_API_KEY` ve `AI_SHARED_TOKEN` değerleri `Config.summary()` çıktısında, iş JSON kayıtlarında ve istisna metinlerinde geçmemeli. Kanarya değerlerle sınanır. |
| `regress_mutation_survivors.py` | Mutasyon testinin hayatta bıraktığı 10 mutantın kapattığı boşluklar: `len(body) > MAX_FRAME_BYTES` sınırı **tam değerinde** (kabul) ve bir bayt üstünde (ret), istisna metinlerinin kod+mesaj taşıması, `ApiResponse` alanlarının okunması, `GREETING_TIMEOUT_SECS < 10` (backend `AI_HANDSHAKE_TIMEOUT_SECS`) ve `KEEPALIVE_SECS < IDLE_TIMEOUT_SECS` bağıntıları. |
| `regress_unreachable_guard.py` | `JobStore.begin()` içindeki "kuyruktayken iptal bayrağı" koruması **erişilemez**: bayrağı yalnızca `cancel` ve `shutdown` doğruya çeker, ikisi de `queued` durumunu geride bırakmaz. AST ile yazıcı kümesi kilitlenir; yeni bir yazıcı eklenirse test kırmızıya döner ve korumanın canlandığını söyler. |

### Slayt teorisine göre denetim

Ders slaytları (`slides/week01-11`) ile paket madde madde karşılaştırıldı. Aşağıdaki
her satır **ölçülmüş** bir sonuçtur, iddia değil.

#### 1. Kapsam bir taban, hedef değil (week01, RIPR)

RIPR modeli: bir kusurun **görünmesi** için dörtlü şart gerekir — kusura *erişilmeli*
(Reached), durum *bozulmalı* (Infected), bozukluk *yayılmalı* (Propagated), sonuç
*gözlenmeli* (Revealed). %100 kapsam yalnızca birinci şartı garanti eder.

Bu depoda kanıtlandı: `protocol.py` satır ve dal kapsamı **%100** iken mutasyon
skoru **%89,4** çıktı. Aynı kod, aynı testler; 10 mutant hayatta kaldı çünkü
erişilen satırlar assert edilmiyordu.

Ölçülen kapsam (`coverage run --branch --source=src`):

| Modül | Satır | Not |
| --- | --- | --- |
| `protocol.py` | %100 satır + %100 dal | mutasyonla da doğrulandı |
| `config.py` | %97 | |
| `capabilities.py` | %96 | |
| `jobs.py` | %91 | erişilemez koruma dahil (aşağıya bak) |
| `pipeline.py` | %76 | gerçek `Hat` yolları sahte sınıfla değiştirilir |
| `main.py` | %68 | unittest **çağırmaz**; `--validate` adımı çağırır |
| `bridge.py` | %34 | **gerçek boşluk**: QUIC istemcisi, ağ ve `aioquic` ister |
| TOPLAM | %76 | unittest + `--validate` birleşik |

`main.py`'yi yalnız unittest ile ölçmek **%0** verir ve bu rakam yanıltıcıdır: dosya
996 satırlık bir çalışma-zamanı doğrulama koşumudur (14 `_check_*` fonksiyonu) ve
CI'da ayrı bir adım olarak koşar. Slaytların "eksik test mi, erişilemez mi"
sınıflandırması gereği: **başka mekanizmayla kapsanıyor**. Geriye kalan tek gerçek
boşluk `bridge.py`'dir.

#### 2. Mutasyon testi (week06 operatörleri)

`mutmut` beş ayrı nedenle bu depoda koşmadı (Windows desteği yok; bind-mount izin
üstverisi `copytree`'yi kırıyor; `pytest` zorunlu tutuyor; root olmayan kullanıcı
kök dizine yazamıyor; üretim imajında `tests/` yok — ki olmaması doğru). Bunun
yerine slayt operatörlerini uygulayan `tools/mutation_test.py` yazıldı: **ROR, AOR,
COR, UOI, SDL**. (Slaytlarda geçen ABS uygulanmadı: `protocol.py` mutlak değer
alınabilecek tek bir aritmetik ifade içermiyor.) Skor slayt formülüyle raporlanır: `killed / (toplam -
eşdeğer)`, eşdeğer mutantlar **elle** işaretlenir.

İlk koşu `protocol.py` üzerinde 123 mutant üretti, 110'u öldü, **10'u hayatta
kaldı**. Slaytların M-1.3b kriteri her hayatta kalan için karar ister; onu da
incelendi ve **hiçbiri eşdeğer çıkmadı** — hepsi gerçek test boşluğuydu:

| Hayatta kalan | Neden hayatta kaldı | Kapatan test |
| --- | --- | --- |
| ROR satır 32, 65 | `> MAX_FRAME_BYTES` sınırı hiç **tam değerinde** sınanmamış; `>=` yazılsa test fark etmezdi | `test_a_frame_of_exactly_the_limit_must_be_accepted` + üç kardeşi |
| SDL satır 20, 26, 142 | `super().__init__(mesaj)` silinince kimse fark etmiyor — istisna **metni** hiç assert edilmemiş | `ExceptionsCarryTheirMessage` |
| SDL satır 148, 150 | `ApiResponse.request_id` ve `.body` hiç okunmamış | `ApiResponseKeepsEveryField` |
| ROR satır 87, 185 | `!= PROTOCOL` ve `!= "ok"` karşılaştırmalarının ters yönü sınanmamış | `ComparisonsRejectTheWrongSide` |
| SDL satır 12 | `GREETING_TIMEOUT_SECS = 8.0` sabiti hiç doğrulanmamış | `TimeoutConstantsStayInsideTheBackendWindow` |

15 test eklendi ve mutasyon yeniden koşuldu. Skor **%89,4 → %91,5** çıktı ama
**4 mutant hâlâ hayattaydı** — üçü yine aynı `super().__init__` satırları. Sebebi
kodda değil, **yeni yazılan testlerin kendisindeydi**:

- `BaseException.__new__` argümanları zaten `args`'a koyar. `super().__init__`
  silinse bile `str(hata)` `"('bad_request', 'alan eksik')"` döner ve
  `assertIn("alan eksik", ...)` **geçer**. Alt dize kontrolü bu mutantı göremez.
- `_coerce_status`'un `bool` dalı silindiğinde `True` doğrudan dönüyordu;
  `assertEqual(status, 1)` yine **geçiyordu**, çünkü Python'da `True == 1`.

Assertion'lar tam eşitliğe çevrildi (`str(hata) == "alan eksik"`, biçimlenmiş
cümlenin tamamı, `len(args) == 1`) ve `assertIs(type(status), int)` eklendi. Üçüncü
tur sonucu:

```
MUTASYON SONUCU: src/protocol.py   (129 mutant: SDL 63, ROR 42, AOR 12, UOI 8, COR 4)
  oldurulen  : 126
  asili kaldi: 3   (sonsuz dongu; paket sonlanmayarak yakaladi)
  hayatta    : 0
  HAM SKOR   : 100.0%
```

| Tur | Skor | Hayatta | Ne gösterdi |
| --- | --- | --- | --- |
| 1 | %89,4 | 10 | %100 kapsamlı kodda sınır ve assertion boşlukları |
| 2 | %91,5 | 4 | Boşluğu kapatmak için yazılan **testlerin kendisi** zayıftı |
| 3 | %100,0 | 0 | — |

Slaytların savı burada iki kez doğrulandı: mutasyon testi önce **kodun** boşluğunu,
sonra o boşluğu kapatmak için yazılan **testlerin** zayıflığını gösterdi. Satır ve
dal kapsamı üç turda da %100'dü ve hiçbir şey söylemedi.

**Asılı kalan üç mutant hakkında.** Üçü de `FrameStream`'in akış-sonu mantığında
(`feed`'in `None` nöbetçisi, `_read_exact`'in `EOFError`'ı ve `_eof` bayrağı).
Silindiklerinde `_read_exact` boş kuyruğu sonsuza kadar bekler: 7,9 saniyelik paket
90 saniyede bitmiyor. Bunlar **öldürülmüş** sayılır — paket onları sonlanmayarak
tespit eder; `mutmut` ve PIT aynı kuralı kullanır. Üçü de ayrı ayrı elle
doğrulandı, varsayılmadı.

İlk koşularda dördüncü bir zaman aşımı daha görünüyordu (`build_api_request`'in
sözlük ataması). Tek başına denendiğinde **1,7 saniyede normal yoldan ölüyor**;
o zaman aşımı, aynı anda koşan başka işlerin yarattığı bir yanlış sınıflandırmaydı.
`tools/mutation_test.py` buna göre düzeltildi: mutant süre sınırı artık sabit değil,
**ölçülen temel koşunun 10 katı**; zaman aşımı görülürse iki kat sınırla bir kez daha
denenir. Ölçüm aracının kendisi de ölçülmelidir.

Mutasyon her pazartesi ve elle tetikle `.github/workflows/mutation.yml` ile koşar;
rapor artefakt olarak saklanır.

#### 3. Fuzzing (week08)

Slayt 11 "Fuzzing Nasıl Yapılır?" altı girdi kategorisi sayar; `tools/fuzz_test.py`
altısını da üretir:

| Kategori | Örnek |
| --- | --- |
| uzunluk | boş, 1, 255, 65535, 70000 karakter; 40000 çok baytlı; 20000 emoji |
| sayı sınırları | 0, -0, ±2^31, ±2^63, 2^70, `1e309`, `NaN`, `Infinity` |
| özel değerler | NUL, gömülü NUL, satır sonu, EOF, BOM, sağdan-sola işareti |
| biçimlendirici | `%s`, `%x`, `%n`, süslü parantezler, JNDI kalıbı |
| noktalama | noktalı virgül, tırnaklar, ters eğik çizgi, `../`, kabuk ikamesi, tüm kontrol karakterleri |
| anahtar kelimeler | `halt`, `return`, `DROP TABLES`, format adları, yetenek adları, `__class__` |

Ayrıca slaytların diğer üç tekniği: **şablon (mutasyon) tabanlı** fuzzing geçerli
çerçeveleri tohum alıp alan/bayt düzeyinde bozar; **gramer tabanlı** üretim bir
context-free grammar'dan JSON çerçevesi türetir (yineleme derinliği sınırlı, slayt
21'deki annotation önerisi); **kapsam geri beslemesi** yeni satır kapsayan girdiyi
tohum havuzuna geri koyar (slayt 13/25, AFL mantığı).

Orakl kesin: `bridge.py::_serve_request` yalnızca `EOFError`, `CapabilityError` ve
`ValueError` yakalar. Bunların dışında bir istisna, akışı **hiç cevaplamadan**
bırakır ve backend tam deadline boyunca asılı kalır. Fuzzer bu sözleşmeyi ihlal eden
her girdiyi bulgu sayar.

**İlk koşu iki gerçek kusur buldu**, ikisi de aynı satırda — `parse_api_response`
içindeki `int(frame.get("status", 0))`:

```
status = []           -> TypeError: int() argument must be ... not 'list'
status = float('inf') -> OverflowError: cannot convert float infinity to integer
```

`call_api` bu çağrıyı hiçbir korumaya sarmıyordu. `_coerce_status` eklendi: artık
kullanılamaz her `status` biçimi ilan edilmiş `ApiRefused("malformed", ...)` ile
reddedilir, kullanılabilir olanlar (`200`, `"404"`, `301.0`) geçer. Düzeltmeden önce
17 test kırmızıydı, sonra hepsi yeşil.

Düzeltme sonrası üç tohumla 50 000'den fazla girdi koşuldu: **sıfır bulgu**. CI'da
sabit tohumlar (1, 2, 3) kapı olarak, koşuya özgü değişken tohum ise bilgilendirme
olarak koşar — fuzzing bir kampanyadır, sabit bir küme değil.

Lab notunun tek katı kuralı — *"The fuzzer must never crash"* — testle korunur:
`TheFuzzerItselfNeverCrashes`.

Fuzzer'ın kendi izleyicisi bir yan etki üretiyordu: `sys.settrace(None)` çağrısı
`coverage`'ın izleyicisini de kapatıyor, dolayısıyla fuzz testleri koştuktan sonra
kapsam ölçümü sessizce duruyordu. Önceki izleyici `sys.gettrace()` ile saklanıp geri
konur; testle doğrulandı.

#### 4. Model tabanlı test (week10, W yöntemi)

`JobStore` biçimsel bir **Mealy makinesi** olarak modellendi. Modelleme sırasında
durum etiketlerinin yetmediği ortaya çıktı: `cancel_requested` bayrağı davranışı
değiştirdiği için `running` ikiye (`R`, `RC`), `failed` ikiye (`F`, `FC`) ayrılıyor.
Sonuç **7 durumlu** bir makine — beş değil.

- **X** (7 girdi): `begin`, `finish`, `fail`, `cancel`, `progress`, `status`,
  `cancel_requested`
- **V** (durum örtüsü): BFS ile üretilir, boş dizi dahil 7 dizi
- **W** (karakterize edici küme): bölüntü inceltme + geriye eleme ile **hesaplanır**,
  elle seçilmez. Sonuç `{[status], [cancel_requested]}` — 2 dizi, slaytın `n-1`
  sınırının çok altında
- **Test kümesi**: `V·W ∪ V·X·W` = **100 dizi**, her dizinin öncesinde sıfırlama
  (taze iş kaydı)

Slayt 12 geçiş turunun **yeterli olmadığını** söyler. Bunu kendi kodumuzda ölçtük.
`fail()` iptal bayrağını silecek şekilde bozuldu — yani `FC` durumu tamamen yok
edildi:

```
eski paket (unit + integration + regress, 272 test)  -> HEPSI YESIL
model paketi (100 dizi)                              -> 9 KIRMIZI
```

272 testin tamamı bu durum geçiş hatasını kaçırdı; yalnızca biçimsel modelin
adlandırdığı `FC` durumu onu görünür kıldı. Slaytların *"durum geçiş hataları ...
fark etmesi daha zor"* uyarısının bu depodaki doğrudan kanıtı budur.

Model ayrıca **erişilemez kod** buldu: `JobStore.begin()` içindeki
`if record["cancel_requested"]` koruması hiçbir genel API dizisiyle çalıştırılamıyor,
çünkü bayrağı doğruya çeken iki fonksiyon (`cancel`, `shutdown`) da işi `queued`
durumunda bırakmıyor. Kapsam ölçümü bunu bağımsız olarak doğruladı
(`jobs.py:578-580` kapsanmamış). Slaytların sınıflandırması gereği: **erişilemez,
eksik test değil**. Kod silinmedi — savunma amaçlı; bunun yerine
`regress_unreachable_guard.py` bayrağı yazan fonksiyon kümesini AST ile kilitledi:
biri eklenirse test kırmızıya döner ve korumanın canlandığını söyler.

#### 5. Açık kalanlar

- `bridge.py` %34 — QUIC istemcisi için sahte bir `aioquic` sunucusu gerekir; kanıt
  bugün elle koşumdan geliyor. Paketin bilinen en büyük boşluğu.
- Mutasyon yalnızca `protocol.py` için tamamlandı; `jobs.py`, `capabilities.py`,
  `config.py`, `pipeline.py` sırada. Haftalık iş akışı bunları zamanla kapsayacak.
- PDF ayrıştırıcı fuzzing'i (slayt 7 PDF'i açıkça sayar) bu depoda **değil**: PDF'i
  hat deposu ayrıştırır, servis yalnızca kimlik ve yol doğrular. Fuzzing oraya ait ve
  orada açık bir maddedir.

### Test yazma kuralları (bu depo)

- **Test kodunda yorum ve docstring yok** — `src/` ile aynı kural. Açıklama test
  **metot adında** ve `assertX` **mesajında** durur.
- Her dosyanın sonunda `if __name__ == "__main__": unittest.main()`.
- Çok vakalı kontroller `subTest` ile; `@unittest.skip` **dekoratörü** kullanılmaz
  (test kalıcı olarak devre dışı bırakılmaz). Çalışma anında `self.skipTest()` yalnızca
  **dış bir kaynak gerçekten yoksa** kullanılır ve mesajı nasıl sağlanacağını söyler;
  tek örneği `regress_format_vocabulary.py`'deki hat kaynağıdır.
- Testler **ağ kullanmaz**, `aioquic` gerektirmez (`bridge.py` sınanırken
  `sys.modules`'a kukla bir `aioquic` konur), gerçek hattı import etmez ve
  `PODCAST_JOB_ROOT`'a yazmaz — hepsi `tempfile` altında çalışır.
- Testler birbirinden bağımsızdır: ortam değişkeni veya `config._active_level`
  değiştiren test `setUp`/`tearDown` ile geri alır.
- Tüm paket 30 saniyenin altında koşar; uzun `sleep` yoktur.

### Neyi KAPSAMAZ

Bunlar bilerek dışarıda bırakılmıştır; kanıtları başka yerden gelir:

- **Container montajı** — imaj build'i, `podman compose`, hacim bağlama, sağlık
  kontrolü akışı. Bunlar `deploy/OKU.md` ve elle koşum ile doğrulanır.
- **Gerçek hat** — `router.hat.Hat`, gerçek OCR/TTS/ffmpeg. Testler `Hat`'ın
  yerine sahte bir sınıf koyar; ağırlıklar ve ffmpeg gerekmez. Gerçek hat kanıtı
  `tools/smoke_test.py` ile üretilir.
- **LLM yolu** — `tek_ogretici`/`ogrenci_hoca` üretimi, DeepSeek çağrıları,
  bütçe muhasebesi. Testler yalnızca **kapının** (anahtar yokken
  `llm_unavailable`) doğru çalıştığını sınar, üretimin kalitesini değil.
- **Çok bölümlü PDF** — çok bölümlü gerçek bir koşunun bölümleme, süre ve montaj
  davranışı. Kimlik/hizalama mantığı sahte yollarla sınanır, gerçek PDF ile değil.
- **QUIC kablosu** — sertifika çekme, TLS pinleme, gerçek bağlantı, yeniden
  bağlanma döngüsü. `bridge.py`'nin yalnızca çerçeve/cevap mantığı sınanır.


## Dağıtım (adım 4)

İnsan rehberi **`deploy/OKU.md`**'dir; burada yalnızca **kararlar ve
gerekçeleri** yazılıdır.

> `deploy/*.ps1` betiklerinde **yorum satırı yoktur.** Koşturarak ölçülmüş beş
> PowerShell/podman tuzağı ve `Invoke-Quiet` yardımcısının gerekçesi
> `deploy/OKU.md` → **"Betiklerdeki tuzaklar (ölçüldü)"** bölümündedir. Bir
> betiği düzenlemeden önce orayı okuyun.

### Hat imaja VENDOR edilir, bind-mount edilmez

Hat ayrı bir repodadır (`C:\PROJECTS\podcast`). İki gereksinim çatışıyordu:
imaj **self-contained** olmalı (bind-mount'lu bir imaj taşınabilir değildir ve
hangi kaynakla koştuğu çalışma anına kadar belirsiz kalır), ama hattın **gerçek
kaynağı tek** kalmalı. Çözüm tek yönlü bir kopyalayıcıdır:

```
deploy/setup-pipeline.ps1   ->  vendor/pipeline/       (kaynak: C:\PROJECTS\podcast)
.gitignore             ->  vendor/ izlenmez
Containerfile          ->  COPY vendor/pipeline /app/pipeline
compose.yaml           ->  PODCAST_PIPELINE_PATH=/app/pipeline
```

Kopyalanan: `router/ script/ ses/ ingest/ bilgi/ config/` + üç
`requirements*.txt` — **ölçüldü: ~1,5 MB**. Vendor içinde yapılan düzenleme bir
sonraki koşuda silinir; gerçek düzenleme hat reposunda yapılır.

**`tools/bin/` bilerek KOPYALANMAZ.** İçindekiler `ffmpeg.exe` + `ffprobe.exe`,
yani **Windows ikilileri**, ve **ölçüldü: 219,8 MB**. Linux container'da
çalışmazlar. `ses/ayar.py:22-23` bu yolu **sabit kodlar** ve ortam değişkeni
kabul etmez, bu yüzden Containerfile ffmpeg'i `apt`'tan kurar ve
`tools/bin/ffmpeg.exe → /usr/bin/ffmpeg` sembolik bağını atar — uzantı
Linux'ta anlamsızdır, yani kaynağa dokunmadan çözülür.

### Ağırlıklar hacimde, imajda DEĞİL

Ağırlık **veridir, kod değildir**. İmaja koymak her `--build`'i 4 GB'lık bir
katman kopyasına çevirir ve `rollback.ps1`in tuttuğu **her** etiketi 4 GB yapardı.

| Ağırlık | Ölçülen | Container yolu | Ne için |
|---|---|---|---|
| `intfloat/multilingual-e5-large` | 2156,99 MB | `/models/hf/hub/` | gömme (`bilgi/erisim.py`) |
| `faster-whisper medium` | 1459,67 MB | `/models/ses_modelleri/faster_whisper/` | round-trip CER kalite kapısı |
| `supertonic-3` | 382,72 MB | `/models/ses_modelleri/supertonic-3/` | TTS |
| **toplam** | **~3,9 GB** | `podcast-models` hacmi, `:ro` | |

`deploy/setup-models.ps1` **yerelden kopyalar** (üçü de bu makinede var);
bulamadığını **indirmez, indirme komutunu basar** — 4 GB'lık bir çekmeye
operatör karar verir. Hacme yazma tekniği chatbot'un
`setup-podman-wsl.ps1:39-42` deseni (`podman cp`), **ağ gerektirmez**.

**İki farklı arama yolu var, karıştırılmamalı:**

- `e5-large` HF hub önbelleğinden gelir → `HF_HOME=/models/hf` işe yarar.
- **TTS ve ASR ağırlıkları HF önbelleğinde DEĞİL.** `ses/ayar.py:26`
  `MODEL_KOKU = <hat_koku>/data/ses_modelleri` diye **sabit kodlar**
  (`:186` `ASR_KOK` da ondan türer) ve **ortam değişkeni yoktur**. Bu yüzden
  Containerfile `/app/pipeline/data/ses_modelleri → /models/ses_modelleri`
  sembolik bağını atar. Kaynağa dokunmadan çözülen ikinci nokta budur.

`HF_HUB_OFFLINE=1`: ağırlık hacimde yoksa hat **açık hata** verir, sessizce
2,2 GB indirmeye başlamaz.

### Medya ve çıktı hacimleri

| Hacim | Bağlantı | Mod | Ne tutar |
|---|---|---|---|
| `hezarfen-data` (**external**) | `/data/files` | **:ro** | Backend'in PDF blob deposu (`FILES_PATH`). `PODCAST_MEDIA_ROOT` ile aynı yol. |
| `podcast-out` | `/data/podcast/out` | rw | Üretilen mp3 / script / quiz (`PODCAST_OUTPUT_ROOT`) |
| `podcast-jobs` | `/data/podcast/jobs` | rw | İş durumu + `.bridge-status` |
| `podcast-kayit` | `/data/podcast/kayit` | rw | `router.sqlite` — adım önbelleği, resume, LLM bütçesi |
| `podcast-models` | `/models` | **:ro** | ~3,9 GB ağırlık |
| `podcast-easyocr` | `/home/podcast/.EasyOCR` | rw | easyocr'ın kendi indirdiği tanıma modelleri |

PDF **okunur**, ses **yazılır**: medya hacmi `:ro` bağlanır. `podcast-kayit`
ayrı bir hacimdir çünkü **önbellek bu hattın tasarım ilkesidir**; defter kalıcı
değilse her koşu her adımı baştan yapar (ölçülen `script` adımı tek başına
~34 dk).

> **OPERATÖR DOĞRULAMALI — `external` adlar.** Bu makinede **podman kurulu
> değil**; `hezarfen-data` hacminin ve backend ağının gerçek adı çalışan bir
> sistemde **doğrulanamadı**. Adlar compose **proje adından** türer ve proje
> adı varsayılan olarak **dizin adıdır**; backend dizini
> `hezarfen_backend-main`'dir, yani ham `podman compose up` ile adlar
> `hezarfen_backend-main_default` / `hezarfen_backend-main_hezarfen-data`
> olurdu. Ama `frontend` ve `chatbot` compose'ları ağı
> **`hezarfen_backend_default` diye sabit yazıyor** — o adla kalkmazlardı.
> Bu yüzden `deploy/run-stack.ps1` backend'i **bilerek `-p hezarfen_backend`**
> ile kaldırır; compose varsayılanları (`hezarfen_backend_default`,
> `hezarfen_backend_hezarfen-data`) buna dayanır. Betik ayrıca gerçek adları
> `podman inspect hezarfen-backend`ten **okur** ve `HEZARFEN_NET` /
> `HEZARFEN_DATA_VOLUME` olarak geçirir. Elle `podman compose up` yapan
> operatör **önce `podman volume ls` / `podman network ls` ile doğrulamalıdır**;
> ayrıca `bridge` profilli olduğu için komut **`--profile product`** taşımalıdır,
> yoksa servis sessizce atlanır.

### Sağlık compose'da, Containerfile'da DEĞİL

Podman'ın OCI biçimi imajdaki `HEALTHCHECK`'i **yok sayar**. Bu yüzden sağlık
`compose.yaml`dadır: `python -m src.main --health`, `interval 30s`,
`timeout 10s`, `retries 3`, `start_period 90s`. `--health`'in ne yaptığı
yukarıda ("Çalıştırma") anlatıldı.

`start_period: 90s` **ölçülmedi**; model yükleme süresine göre seçilmiş bir
tahmindir ve ilk gerçek koşudan sonra ayarlanmalıdır.

### İmaj sürümleme ve geri alma

Ev şablonu (chatbot) `:local` kullanıyor ve her build aynı etiketi eziyor —
**dönülecek bir imaj kalmıyor**. Burada `run-stack.ps1` her build'i
`localhost/hezarfen-podcast:<yyyyMMdd-HHmmss>` olarak etiketler **ve**
`:current` işaretini oraya taşır; `compose.yaml` varsayılan olarak `:current`
kullanır (`${HEZARFEN_TAG:-current}`).

```powershell
podman images localhost/hezarfen-podcast --format "{{.Tag}} {{.ID}} {{.CreatedSince}}"
pwsh -File deploy/rollback.ps1 -List
pwsh -File deploy/rollback.ps1                    # bir onceki etikete don
pwsh -File deploy/rollback.ps1 -Tag 20260831-181530
```

Geri alma **yeniden build etmez**: `podman tag` + `compose up -d
--force-recreate`. `--force-recreate` şarttır — etiket **adı** değişmediği için
compose "değişiklik yok" deyip container'ı bırakabilir.

`bridge` servisi artık **`profiles: ["product"]`** taşır: ağır servis (8 GB
`mem_limit`) kazara bir `podman compose up` ile kalkmasın diye. Profili
`run-stack.ps1`, `rollback.ps1`, systemd unit'i ve CI deploy'un hepsi geçer;
profilsiz bir `up` servisi **atlar** (sessizce hiçbir şey kalkmaz).

**Sunucuda etiket `stack.env`'den gelir.** Deploy, o build'in SHA'sını
`~/hezarfen_text_to_podcast/stack.env` içine `HEZARFEN_TAG` olarak yazar;
compose interpolasyonu onu `localhost/hezarfen-podcast:<sha>` yapar. Yani
Windows'taki `:current` işareti ile sunucudaki SHA etiketi **aynı mekanizmanın**
iki ucudur: env dosyası yoksa `:current`, varsa SHA.

### Kaynak sınırları — ÖLÇÜLMEDİ

`mem_limit: 8g`, `cpus: 4.0`. Adım 1'deki `2g` sahte hat içindi; artık ~4 GB'lık
ağırlık yükleniyor. **Bu iki sayı ölçüm değildir.** Ölçülen tek şey ağırlıkların
**disk** boyutudur (e5-large 2156,99 MB + faster-whisper 1459,67 MB +
supertonic 382,72 MB); RSS ölçülmedi ve üçünün **aynı anda** yüklü olup
olmadığı da bilinmiyor. **İlk gerçek konteyner koşusundan sonra ayarlanmalıdır.**

CI deploy'unun kapasite tabanı (RAM ≥ 8192 MB) bu tavana **bağlıdır**: tavan
ölçümle düzeltilirse taban da onunla gelmelidir. `tests/` bu satırları okumaz,
yani ikisi arasındaki tutarsızlığı yalnız insan fark eder.

### CI deploy (GitHub Actions)

`.github/workflows/main.yml` (`name: VPS deploy`) kardeş servislerle **aynı üç
işli şekli** taşır: `Validate ∥ Build and test -> Deploy`. Amaç "hata toparlama
ucuz olsun": düşen bir deploy, süiti yeniden koşmadan tekrar denenebilir.

| iş | ne zaman | ne yapar |
|---|---|---|
| `Validate` | push→`main` (koşulsuz) | `pip install -r requirements.txt`, `compileall -q src tests`, `python -m src.main --validate` |
| `Build and test` | push→`main` | **her koşuda**: `unittest discover` + **3 sabit tohumlu** fuzz kampanyası + (bilgilendirme) değişken tohum + yorum/docstring/ASCII denetimi + sır taraması. **`PIPELINE_REPO` tanımlıysa ayrıca**: hat checkout'u + `docker build -f Containerfile` + `release` artefaktı (imaj tarball'ı + `compose.yaml` + unit + `tag`, 30 gün) |
| `Deploy` | ikisi de yeşilse (push), ya da elle (dispatch) — **ve `PIPELINE_REPO` tanımlıysa** | SSH ile yukarıdaki artefaktı sunucuya kurar |

**`PIPELINE_REPO` tanımlı değilken ne olur — "yeşil ve atıl".** `vendor/`
`.gitignore`'dadır ve `Containerfile` `COPY vendor/pipeline /app/pipeline`
yapar, yani hat kaynağı olmadan **imaj derlenemez**. Bu yüzden o durumda imaj
build'i, staging, artefakt yükleme ve `Deploy` **atlanır**; test kapıları yine
koşar ve **koşu yeşil kalır** (eksik ön koşul kırmızı bir koşu ya da yanlış bir
kurulum üretmez). Atıl kalmaz: `Build and test` işi `::warning::` satırları ve
**koşu özetine** (`$GITHUB_STEP_SUMMARY`) şunu yazar — imaj `PIPELINE_REPO`
tanımlı olmadığı için derlenemedi, test kapıları koştu, çözüm depo değişkeni
`PIPELINE_REPO` (+ özel depo ise `PIPELINE_TOKEN`), ayarlanır ayarlanmaz bir
sonraki `main` push'u derleyip deploy eder.

**Hangi olayda hangi iş:**

- **push → `main`**: `Validate` + `Build and test` paralel; ikisi de yeşilse
  `Deploy` **otomatik** koşar. Yani **yeşil bir push servisi kendiliğinden
  günceller** — bu bilinçli bir seçim, RAG'ın "elle kapı" duruşunun tersi:
  podcast servisi GPU'ya bağlı bir ürün kapısı taşımıyor, sığmadığı makinede ise
  deploy **reddediyor** (aşağıdaki kapasite kapısı).
- **push → başka branch**: hiçbir iş koşmaz (push kapısı yalnız `main`).
- **pull_request**: yalnız `test.yml` koşar; kapı kümesi `Build and test` ile
  **aynıdır** (unittest + 3 fuzz + denetimler + sır taraması). Push tetiği
  `test.yml`'den **kaldırıldı**: aynı kapı iki dosyada iki kez koşmasın.
  Yeni bir kapı eklerken **iki dosyayı birlikte** güncelle.
- **`workflow_dispatch`** (Actions → Run workflow): süit koşmaz; `Build and
  test` işi **yeşil olan en yeni** push koşusunun artefaktı indirilip deploy
  edilir. Yeni bir değişiklik için değil, "şu anki yeşil sürümü yeniden kur"
  içindir.

Deploy ne yapar (sırayla): paketin bütünlüğünü doğrula → SSH anahtarı kur →
`~/hezarfen_text_to_podcast/` altına dört dosyayı çıkar → operatörün
`hezarfen_text_to_podcast.env` (0600) dosyasını **oku/chmod et, asla yazma** →
**kapasite ön kontrolü** → podman + compose sağlayıcı kontrolü → backend
`/ai/certificate` ucu cevap veriyor mu → `podman load` → tag rotasyonu
(`current_tag`→`previous_tag`) + `stack.env`'e `HEZARFEN_TAG` → compose modeli
çözülüyor mu (`podman compose … config`) → unit'i kur, `daemon-reload`, enable,
**restart** → servisin **kendi** `--health`'i geçene kadar bekle → çalışan
konteynerin imajı bu build mi → yetmezse `previous_tag`e **geri dön** (ilk
deploy'da `down`, `-v` **yok**: hacimler korunur) → bağımsız son doğrulama.

Kapı, servisin **kendisinden** gelir; port yok, köprü dışarı dial-out eder:

```bash
podman exec hezarfen_text_to_podcast python -m src.main --health   # 0 saglikli, 1 sagliksiz
```

**Ön koşullar (operatörün bir kez yapacağı işler — workflow bunları yapamaz):**

1. Repo secret'ları **`SSH_PRIVATE_KEY` / `SSH_HOST` / `SSH_USER`** (backend ve
   frontend deploy'unun kullandığı üç isim). Deploy başka sır okumaz; hiçbir sır
   workflow dosyasına yazılmaz.
2. Repo değişkeni **`PIPELINE_REPO`** (+ özel depo ise `PIPELINE_TOKEN`):
   `vendor/` `.gitignore`'dadır ve `Containerfile` `COPY vendor/pipeline` yapar,
   yani hat kaynağı olmadan **imaj derlenemez**. Yerelde karşılığı
   `pwsh -File deploy/setup-pipeline.ps1`. **Bugün tanımlı değil** (depo
   değişkeni de `PIPELINE_TOKEN` sırrı da yok): koşu yeşil kalır, imaj ve deploy
   atlanır, uyarı koşu özetinde görünür (yukarıya bkz.).
3. Sunucuda `loginctl enable-linger <kullanıcı>` (kullanıcı unit'leri için).
4. Sunucuda podman + bir compose sağlayıcı.
5. `~/hezarfen_text_to_podcast/hezarfen_text_to_podcast.env` (0600) —
   iskelet: `deploy/hezarfen_text_to_podcast.env.example`; **anahtarların tam
   listesi** depo kökündeki `.env.example`. Dosya sunucuda **elle** yazılır;
   deploy onu oluşturmaz, ezmez (yoksa nedeniyle birlikte reddeder).
6. `podcast-models` hacmi **dolu** (~3,9 GB). Ağırlıklar imaja girmez;
   `HF_HUB_OFFLINE=1` ile eksikse hat **açık hata** verir.
7. Backend köprüsü açık ve `AI_SHARED_TOKEN` iki tarafta **aynı** — deploy bunu
   konteynere dokunmadan önce `/ai/certificate` ile kontrol eder. `AI_BRIDGE_HOST`
   / `AI_BACKEND_URL` backend'in **güncel** adını ve portunu göstermeli
   (`hezarfen_backend`, 7656); adlar 2026-09-14'te tireli hâlden bu hâle geçti.

**Kapasite kapısı (rag ile aynı desen, sayılar bu servisin):** deploy,
`MemAvailable >= 8192 MB` ve `disk >= 12 GB` istemezse **nedeniyle birlikte
reddeder, hiçbir şeye dokunmaz**:

- **8192 MB RAM** — `compose.yaml`'daki `mem_limit: 8g` konteynerin **kendi**
  tavanıdır; host bu tavanı karşılayamıyorsa 45 dakikalık gerçek iş yarıda
  OOM'lanır ya da canlı stack sıkıştırılır. (Tavanın kendisi **ölçüm değil**;
  ölçülen tek şey ~3,9 GB'lık ağırlık hacmi. İlk gerçek koşudan sonra ikisi
  birlikte ayarlanmalı.)
- **12 GB disk** — aynı anda ~3,9 GB ağırlık + ~3 GB imaj (podman deposu) +
  ~3 GB `tar.gz` (deploy dizini) ≈ 10 GB geçici yük; 12 GB bu toplamın tavanı.

> Ölçülmüş bir uyarı: kardeş RAG deploy'u 2026-09-15'te aynı geliştirme
> sunucusunda **6 466 MB boşta RAM** ölçtü ve 12 GB'lık kapısından bu yüzden
> geçemiyor. Podcast'in 8192 MB'lık kapısı da o makinede **geçmez**; bu
> bilinçlidir — daha küçük bir host'ta otomatik başlatmak, canlı stack'i RAM
> için sıkıştırıp karşılığında yarıda ölen işler üretirdi.

**Sağlayıcı notu (ölçüldü, bu makinede podman-compose 1.6.0):** `--env-file`
sağlayıcıda **tek değerli**dir, yani iki `--env-file` verildiğinde yalnız
**sonuncusu** okunur (ölçüldü: `podman compose --env-file a --env-file b config`
→ `AI_SHARED_TOKEN` yok, çünkü `b` kazanır). Bu yüzden **iki dosyalı çağrı
YASAK** ve çözüm "iki dosya okuyabilen sağlayıcı beklemek" değil, iki-kanal
desenidir: operatör değerleri **ortamdan** gelir (unit'te
`EnvironmentFile=`, CI'da satır satır sürece alma) ve compose'a giden **TEK**
`--env-file` deploy sahipli `stack.env`'dir (yalnız `HEZARFEN_TAG`). Ortam
değişkeni `--env-file` değerlerini ezer (`podman_compose.py:2562`), yani iki
kanal çakışmaz. Deploy ayrıca unit'i restart etmeden **önce**
`podman compose … config` ile modeli çözer ve çözemezse **hiçbir şeye
dokunmadan** açık hata verir.

Yerel/Windows yolu **değişmedi**: `deploy/OKU.md` ve `deploy/*.ps1`. Sunucunun
elle kurulum özeti OKU.md → **"Sunucu (Linux VPS) — OTOMATIK deploy"**.

## BİLİNEN EKSİKLER / SONRAKİ ADIM

- ~~Üretilen MP3 backend'e ULAŞMIYOR~~ — **KAPANDI (2026-09-17).** Teslim yolu
  artık `BlobUploadRequest`: biten mp3 `done` raporundan ÖNCE ham bayt olarak
  yüklenir, backend onu okulun kendi blob köküne yazar ve `audio_key` +
  `duration_secs` alanlarını satıra damgalar. Servisin kendi `podcast-out`
  hacmi yalnızca yerel çalışma kopyasıdır; `hezarfen-data` hâlâ `:ro` bağlanır.

- ~~Cok bolumlu hizalama sinanmadi~~ - **KAPANDI (2026-09-07).** Eldeki
  orneklerin hicbiri birden fazla bolum uretmiyordu; bunun icin hat deposuna
  `tools/make_cok_bolumlu_pdf.py` yazildi (dort ust duzey bolum, her biri
  kendi sayfasinda). Olculdu: hat **3 script** uretti, `bolum_limiti=1` ile
  **1 ses** cikti ve `script_ids` 3'ten **1'e suzuldu**. Hizalama olmasaydi
  tuketici 1 sese 3 script gorurdu.

- ~~LLM'li formatlar sinanmadi~~ - **KAPANDI (2026-09-07).** `tek_ogretici`
  gercek anahtarla konteyner icinde kostu: script adimi 637,8 sn, 19 replika,
  312,5 sn ses, **quiz 4 madde** (`duz_okuma`'da 0). `audio_id` yolu
  `.../ses/tek_ogretici/...` - `duz_okuma` kosusuyla ayni `bolum_id`, farkli
  yol; format carpismasi gercek ciktiyla da kapandi.
  kez bile build edilmedi, hiçbir betik koşturulmadı. Doğrulanmamış varsayımlar:
  `pip install` çözümlemesinin Linux/py3.10'da geçtiği, `torch` CPU tekerleğinin
  bulunduğu, ağ ve hacim adları, imaj boyutu, build süresi, `start_period: 90s`,
  `mem_limit: 8g`. Betikler yalnızca **sözdizimi** olarak denetlendi
  (`[Parser]::ParseFile`).
- **`transformers` hattın hiçbir `requirements*.txt`inde yazılı değil.**
  `requirements-ses.txt` onu (ve `onnxruntime`, `numpy`, `huggingface_hub`'ı)
  "zaten kurulu" varsayıyor. Containerfile bu boşluğu yereldeki **ölçülen**
  sürümlere pinleyerek kapatır (`transformers==5.15.0`, `onnxruntime==1.23.2`,
  `numpy==2.2.6`, `huggingface_hub==1.27.0`). `torch` **pinlenmedi** — CPU
  tekerlek indeksinde py3.10 için hangi sürümün durduğu doğrulanamadı.
- **easyocr ağırlıkları imajda yok.** İlk taranmış PDF'te easyocr onları kendi
  indirir (`podcast-easyocr` hacmine; kalıcı ama **ağ gerekir**). `HF_HUB_OFFLINE`
  easyocr'ı etkilemez — kendi indiricisi vardır.
- **`--health` bayatlık denetlemez.** Köprü durumu yalnızca *değiştiğinde* yazar;
  asılı kalmış (hung) ama `registered=true` bırakmış bir süreç sağlıklı görünür.
- **İptal adım sınırında.** 34 dakikalık `script` adımının ortasında iptal
  görülemez (yukarıda "SINIR" başlığı). Hattın kendisine ara kontrol noktası
  eklenmedi.
- **`PODCAST_OUTPUT_ROOT` temizlenmiyor.** Üretilen mp3'ler süresiz birikir;
  `PODCAST_JOB_ROOT` ile aynı retention sorunu.
- **`PODCAST_OUTPUT_ROOT` altındaki `_hat` dizini.** `ses/ayar.py`'nin
  `ARA_KOKU` varsayılanı (`<hat_koku>/out/ses/_ara`) imajda
  `/data/podcast/out/_hat`'e sembolik bağlanır. Ölçülen çağrı yollarında
  `Hat` ara dizini **her zaman kendisi verir** (`self.cikti/_ara/<bolum>`),
  yani bu bağ pratikte kullanılmıyor — sigorta olarak duruyor.

### Duman testi (`tools/smoke_test.py`)

`--validate`'in **parçası değildir** ve otomatik koşmaz: tek bir gerçek işi
uçtan uca koşturur, dakikalar sürer (LLM gerekmez ama TTS uzundur).
`samples/kisa_slayt.pdf` en küçük uygun örnektir ve varsayılan kaynaktır.

```sh
cd C:\PROJECTS\podcast\hezarfen_podcast_service
python tools/smoke_test.py
```

Varsayılanlar: `PODCAST_MODE=real`,
`PODCAST_PIPELINE_PATH=C:\PROJECTS\podcast`,
`PODCAST_MEDIA_ROOT=C:\PROJECTS\podcast\samples`,
`--source kisa_slayt.pdf`, `--format duz_okuma`. İş ve çıktı kökleri
`./out/duman/` altına yazılır; `PODCAST_JOB_ROOT`'a dokunmaz.

Faydalı bayraklar:

```sh
python tools/smoke_test.py --source ornek_ders.pdf --format duz_okuma
python tools/smoke_test.py --timeout 3600     # bkz. --help
python tools/smoke_test.py --cancel-after 90   # 90 sn sonra iptal ister
```

`--cancel-after` iptalin **adım sınırında** yakalandığını gözlemlemek içindir:
iptal isteği anında değil, koşan adım bitince etki eder.

Çıkış kodu: `0` = `done`, `1` = `failed`/`cancelled`/zaman aşımı,
`2` = hat yüklenemedi.

### Adım 3: `Hat` nasıl bağlanacak (tarihçe)

Gerçek hat tek parçadır — `Hat(pdf, format=...).kos()`, **~45 dakika**. Bağlantı
noktası `bridge.build_store(settings, runner=..., stages=...)`'tır:

```python
def hat_runner(ctx):
    ctx.check()
    pdf = jobs.resolve_source(settings.media_root, ctx.school, ctx.source_key)

    def gunluk(asama, oran=0.0):
        ctx.check()
        ctx.progress(asama, oran)

    cikti = Hat(pdf, format=ctx.format, gunluk=gunluk).kos()
    ctx.finish(cikti.audio_id, cikti.duration_secs, cikti.script_id)

store = build_store(settings, runner=hat_runner, stages=HAT_ASAMALARI)
```

Dikkat edilecekler:

- **İptal, `Hat(gunluk=...)` geri çağırımının içinden görülür.** `Hat` tek parça
  olduğu için ara kontrol yalnızca oradan yapılabilir; `gunluk` her çağrıldığında
  `ctx.check()` işletilir ve iptal varsa `JobCancelled` hattın **içinden** fırlar.
  `Hat` bu istisnayı yutmamalı. Geri çağırım hiç çağrılmazsa iptal ancak işin
  bitişinde (`ctx.finish()`) yakalanır — kaybolmaz, sadece gecikir.
- `PODCAST_ETA_SECS` gerçek süreye ayarlanmalı (ör. `2700`), yoksa backend'e
  `eta_secs=1` bildirilir.
- `PODCAST_MEDIA_ROOT` backend'in `FILES_PATH`'i ile aynı olmalı. Adım 4'te
  bağlandı: backend'in `hezarfen-data` hacmi `/data/files`'a **`:ro`** monte
  edilir (bkz. "Dağıtım").
- `HatHatasi` gibi hat istisnaları `failed` + `internal` olur; hattın kendi hata
  kodlarını `error_code`'a taşımak istenirse `runner` içinde `store.fail()`
  açıkça çağrılmalıdır.
- ~~Bitmis isler temizlenmiyor~~ - **KAPANDI.** `PODCAST_RETENTION_DAYS`
  (kod varsayilani `0` = asla silme; `compose.yaml` `30` veriyor). Acilista
  bir kez kosar, **yalnizca terminal durumdaki** (done/failed/cancelled)
  isleri ve urettikleri ses/script dosyalarini siler. Kuyruktaki ve **kosan**
  isler yasina bakilmadan korunur. Silinecek her yol cikti kokunun ALTINDA
  olmak zorundadir - is JSON'lari diskten gelir ve kurcalanmis olabilir;
  disari isaret eden kimlik uyari loglanip **atlanir** (`regress_retention.py`
  bunu `../disarida/dokunulmaz.txt` ile sinar).
- ~~Taban imaj digest pin'i yok~~ - **KAPANDI.** `python:3.10-slim@sha256:4101e4a3...`
  ile pinli; yenileme komutu `Containerfile` basindaki yorumda.
- ~~Test paketi yok~~ - **KAPANDI.** `tests/` altinda saf `unittest`, 255 test.
- **API-read yok.** Protokol servis→backend `ApiRequest` akışını destekliyor;
  bu adımda kullanılmıyor.
