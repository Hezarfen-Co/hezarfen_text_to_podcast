# CLAUDE.md — hezarfen_podcast_service

## Ne bu proje
Hezarfen'in dördüncü servisi: podcast üretimi. Kablo + **gerçek iş yaşam döngüsü**
(kuyruk, diskte kalıcı durum, işbirlikçi iptal, açılış süpürmesi) hazır. Gerçek
hat `src/pipeline.py` ile bagli ve konteyner icinde gercek PDF -> MP3 uretimiyle
dogrulandi (her iki formatta, cok bolumlu ornek dahil). `tools/smoke_test.py`
bunu tekrarlar; `tools/mutation_test.py` slayt operatorleriyle mutasyon skoru olcer.

## Değişmez kurallar (gerekçesiyle)
- **Podman, Docker değil.** Dosya adı `Containerfile`; `podman compose` kullanılır.
- **Kodda yorum yok** — ne `#` ne docstring. Açıklama `ReadMe.md`'ye yazılır.
  Kullanıcının açık talimatı.
- **Port açma, HTTP sunucusu açma.** Servis backend'in QUIC'ine *dial-out* eder;
  NAT ardında yaşayabilsin ve backend tek otorite kalsın diye.
- **Sertifikayı her reconnect'te yeniden çek.** Backend self-signed sertifikayı her
  açılışta üretir; sadece redial etmek eski PEM'e güvenmek olur.
- **Sertifika `AI_TLS_FINGERPRINT` ile pinlenebilir.** Doluysa PEM'den DER
  çıkarılıp SHA-256'sı kendimiz hesaplanır ve pinlenen değerle karşılaştırılır;
  tutmazsa bağlanılmaz, loglanır, geri çekilerek yeniden denenir. Boşsa TOFU'dur
  ve her açılışta uyarı loglanır. Sunucunun bildirdiği iz pin yerine geçmez.
- **Sürüm kimliği `hab/2`.** Backend hem ALPN'de hem `Hello.protocol` alanında
  denetler; ikisi de `src/protocol.py` içindeki tek sabitten gelir.
- **Her `Response` okulu yankılar.** Filo tüm okullara ortak olduğu için `Hello`
  okul taşımaz; her istek çerçevesi okulunu tireli uuid ile adlandırır ve her cevap onu
  aynen geri yazar. Okulsuz istek `bad_request`'tir; varsayılan yoktur. Aynı
  kural `ApiRequest`/`ApiResponse` çifti için de geçerlidir.
- **Kalıcı red (`unauthorized`, `unsupported_protocol`) servisi çıkarmaz.**
  `restart: unless-stopped` altında exit 2 sonsuz crash-loop olurdu; bekleme
  ikiye katlanır, jitter eklenir ve `AI_RECONNECT_MAX_SECS` tavanında durur,
  backend düzelince servis kendiliğinden toparlanır.
- **Kontrol akışı ömür boyu açık kalır** (`end_stream=False`). Kapanması backend
  için kayıttan düşme sinyalidir; heartbeat yok, QUIC PING (10 sn) var.
- **Token loglanmaz, koda/belgeye yazılmaz.** Parmak izinin ilk 12 karakteri
  loglanabilir; `compose.yaml` interpolasyon kullanır, literal sır yazmaz.
- **Log mesajları Türkçe ama ASCII**, `[bridge] ` önekli, `print(..., flush=True)`.
  Windows konsolu ASCII dışını bozuyor; `logging` modülü kullanılmaz.
- **Tanımlayıcılar İngilizce**, Python 3.10+ uyumlu.
- **Yanlış yapılandırma boot'u çökertir** (exit 2), eksik yapılandırma özelliği
  sessizce kapatır. Backend'in kendi kuralı; iki repo aynı davranmalı.
  `DEEPSEEK_API_KEY` bunun örneğidir: yoksa yalnızca `duz_okuma` açılır, boot çökmez.
- **Yetenek işleyicileri anında döner.** Backend'in istek deadline tavanı 60 sn
  (`constant.rs::AI_MAX_REQUEST_TIMEOUT_SECS`), gerçek iş ~45 dk. Uzun iş `jobs.py`
  içindeki **ayrı, daemon** işçi thread'lerinde koşar (kuyruk + `None` sentinel);
  asyncio'nun varsayılan executor'ı uzun iş için ASLA kullanılmaz.
- **`ThreadPoolExecutor` kullanılmaz.** Thread'leri daemon değildir; `shutdown`
  anında dönse bile yorumlayıcı çıkışta onları join eder ve 45 dakikalık iş
  SIGTERM'i SIGKILL'e çevirir. İşçi thread'leri `daemon=True` olmak zorunda.
- **Uzun iş `JobContext` üzerinden konuşur.** `runner` imzası
  `Callable[[JobContext], None]`; iptal `ctx.check()`, ilerleme `ctx.progress()`,
  bitiş `ctx.finish()`. `begin()`'i işçi çağırır, runner değil.
- **İş durumu diske atomik yazılır** (geçici dosya + `os.replace`) ve açılışta
  `running` işler `failed`/`interrupted` olur — backend'in boot süpürmesinin kopyası.
- **API anahtarı asla loglanmaz/yazılmaz**; `Config` yalnızca `has_llm_key` tutar.
- **Sessiz sahte üretim yasak.** `PODCAST_MODE=simulate` açılışta tek satır
  "SAHTE hat kosuyor, uretilen ses GERCEK DEGIL" uyarısı basar;
  `PODCAST_MODE=real` ise `PODCAST_PIPELINE_PATH` yok/bozuksa ya da
  `router.hat` import edilemiyorsa **exit 2**.
- **Gerçek hat TEMBEL import edilir.** `pipeline.py` modül seviyesinde
  `router.hat`'ı import etmez; `--validate` ve `simulate` modu onu hiç yüklemez.
- **Kancadan `KasitliKesme` fırlatılır**, kendi istisnamız değil: `Hat.kos()`
  onu ayrıca yakalayıp koşuyu "kesildi" diye kapatır; kendi istisnamız koşuyu
  "hata" yapar ve resume semantiğini bozardı.
- **İptal adım sınırındadır.** `gunluk` çoğunlukla `_adim_ekle`'den çağrılır (hepsi değil);
  34 dakikalık bir adımın ortasında iptal görülemez. Bu bir sınırdır, gizlenmez.
- **Hat imaja VENDOR edilir, bind-mount edilmez.** `deploy/setup-pipeline.ps1`
  kaynağı `vendor/pipeline`'a kopyalar (tek yönlü; `vendor/` gitignore'da),
  Containerfile onu `/app/pipeline`'a alır. Ağırlıklar (~3,9 GB ölçüldü) imaja
  GİRMEZ, `podcast-models` hacminden `:ro` gelir. `ses/ayar.py` ffmpeg ve model
  yollarını SABIT kodlar; ikisi de kaynağa dokunmadan sembolik bağla çözülür.
- **Sağlık compose'da, Containerfile'da DEĞİL.** Podman OCI biçiminde imajdaki
  HEALTHCHECK'i yok sayar. `python -m src.main --health` ağ çağrısı yapmaz.
- **Her build etiketlenir** (`:<zaman damgası>` + `:current`); compose
  `:current` kullanır. `:local` gibi tek etiket geri dönüşü imkânsız kılardı.
- **Kablo sözleşmesi kırılmaz.** Çok bölümlü koşu için `audio_id`/`script_id`
  tekil kaldı; `audio_ids`/`script_ids` **eklendi**. Alan eklemek geriye dönük
  uyumludur, yeniden adlandırmak değildir.

- **Bulut TTS iki bayrakla acilir.** `PODCAST_TTS_ENGINE=elevenlabs-flash-tr`
  **ve** `SES_BULUT_IZINLI=1` birlikte gerekir. Tek bayrak olsaydi anahtari
  vermeyi unuttugun kosu sessizce lokal sesle biterdi; simdi izin kapaliyken
  bulut motoru `BulutYasak` firlatir. ElevenLabs konfigurasyonu (model
  `eleven_flash_v2_5`, `language_code=tr`, kararlilik 0.85) `ses/ayar.py`
  icinde KILITLIDIR, compose'tan degistirilmez. Anahtar yalnizca ortamdan
  okunur; `SesParcasi.ek` sozlugune, loga ve hata metnine YAZILMAZ.
- **`pcm_44100` istenmez.** OLCULDU: ElevenLabs "only available on the Pro
  tier" deyip HTTP 403 veriyor. mp3 istenip `ayar.FFMPEG` ile WAV'a cevrilir;
  ffmpeg zaten hattin sabit bagimliligi, yeni bagimlilik degil.
- **Fuzz yükleri kaçış dizisiyle yazılır.** Depo kuralı `src/tests/tools` altında
  ASCII dışı karakter yasaklar (CI kapısı), ama fuzzing UTF-8 yollarını sınamak için
  ASCII dışı **değerlere** ihtiyaç duyar. Çözüm: dosyada `"ç"`, değerde `ç`.
  Ham karakter yazmak CI'ı kırar.
- **Fuzzer'ın izleyicisi önceki izleyiciyi geri koyar.** `sys.settrace(None)` demek
  `coverage`'ın izleyicisini de kapatmak demektir; ölçüm sessizce durur.
  `sys.gettrace()` ile saklanır, `finally` içinde geri konur.
- **Model tabanlı testte W kümesi elle seçilmez**, bölüntü inceltme + geriye eleme
  ile hesaplanır. Elle yazılan bir W kümesi değişim-dedektörüdür; hesaplanan küme
  modelin özelliğidir. `JobStore` modeli 7 durumludur (`cancel_requested` bayrağı
  `running` ve `failed` durumlarını ikiye böler), 5 değil.

- **Kaynak türü uzantıdan DEĞİL içerikten belirlenir.** Backend blob anahtarı
  gönderir; dosya diskte uzantısız durur, anahtar bir kimliktir. `extract.sniff()`
  sihirli baytlara ve zip üye adlarına bakar. Uzantıya güvenen bir kısayol
  eklenirse `ders.pdf` adlı bir docx yanlış ayrıştırıcıya gider.
- **Yeni pip bağımlılığı eklenmez; sistem aracı apt'tan gelir.** docx/pptx/
  odt/odp zip + XML'dir; `zipfile` ve `xml.etree` yeter. `requirements.txt`
  iki satırdır (`aioquic`, `pymupdf`) ve öyle kalmalı. Eski ikili `.doc/.ppt`
  stdlib ile OKUNMAZ: `antiword` + `catdoc` apt paketleriyle girer (60 sn
  zaman aşımı, utf-8 -> cp1254 sırası), LibreOffice kasıtlı istenmez.
- **Zip üyesi okunmadan ÖNCE boyutu denetlenir.** `_guard_size()` üye başına
  16 MiB, toplam 64 MiB tavanı uygular. Sınırsız `archive.read()` sıkıştırma
  bombasına açık kapıdır.
- **PPTX slaytları sayısal sıralanır.** `slide10.xml` alfabetik olarak
  `slide2.xml`'den önce gelir; sıralama sayıya çevrilmeden yapılırsa anlatım
  1, 10, 11, 2 sırasıyla akar.
- **Okunamayan biçim sebebini ve çıkış yolunu söyler.** `unsupported_source`
  kodu döner ve mesaj ne yapılacağını yazar (".docx olarak kaydedin" gibi).
  OLE yolunda iki araç da yoksa `extractor_unavailable`, ikisi de okumazsa
  `source_unreadable`, çıktı boşsa `no_text_layer` döner.
- **Görüntü-only sunumda OCR yoktur.** PDF yolunda OCR yedeği var, PPTX/ODP
  yolunda yok; iş `no_text_layer` ile açıkça düşer, boş üretmez. Konuşmacı
  notları okunmaz — ölçüldü, çoğu yalnızca slayt numarası taşıyor ve anlatıma
  çöp sokardı.
- **Çoklu kaynakta alan EKLENDİ, yeniden adlandırılmadı.** `source_key` tekil
  kaldı (ilk belge, bir sürüm uyumluluğu), `sources` eklendi:
  `[{key, name, content_type}]`, en fazla 10. Servis `sources`'ı tercih eder,
  gelmezse tekilden türetir. `sources` `REQUIRED_FIELDS`'a **eklenmez** —
  eklenirse alan eklenmeden önce yazılmış kayıtlar açılışta topluca atılır.
- **Çoklu kaynakta okunamayan belge işi DÜŞÜRMEZ, ATLANIR.** Atlama kodu
  `skipped:<kod>` olarak iş kaydındaki `sources` listesine yazılır ve
  `podcast.report` yüküyle backend'e gider (`{key, name, status}`). Tümü
  atlanırsa iş ilk kaynağın atlama koduyla; birleşik metin
  `PODCAST_MIN_TEXT_CHARS` altındaysa `no_text_layer` ile düşer. Bu donmuş
  sözleşme kararıdır; "sessiz sahte üretim" sayılmaz çünkü eksik belge
  raporda adıyla görünür.
- **Belgeler `\n\n=== <name> ===\n\n` başlığıyla birleşir.** Başlık yalnızca
  belgeler ARASINDA gelir; tek kaynaklı iş başlıksızdır ve `extract_text`
  ile birebir aynı sonucu üretir.

## Dosyalar ve komutlar
`config.py` ortam+log · `protocol.py` çerçeveleme+mesajlar · `jobs.py` iş deposu+
durum makinesi+sahte hat · `pipeline.py` gerçek `router.hat.Hat` runner'ı (tembel
import) · `extract.py` belge bicimi tanima + docx/pptx/odt/odp/ole/metin okuma · `capabilities.py` yetenek defteri · `bridge.py` QUIC istemcisi ·
`main.py` `--validate` + `--health` · `tools/smoke_test.py` elle koşulan duman testi ·
`tools/mutation_test.py` slayt operatörleriyle mutasyon skoru · `tools/fuzz_test.py`
altı slayt kategorisi + şablon/gramer/kapsam-geri-besleme fuzzing ·
`deploy/` dağıtım katmanı (`OKU.md` rehber, `setup-pipeline.ps1` hat kaynağını
`vendor/pipeline`'a kopyalar, `setup-models.ps1` ağırlıkları `podcast-models`
hacmine koyar, `run-stack.ps1` dört repoyu sırayla kaldırır, `rollback.ps1`
`:current` etiketini önceki imaja taşır).
- `python -m src.main --validate` — ağ ve `aioquic` GEREKTİRMEZ; import'ları tembel
  tut ki imajın varsayılan `CMD`'si her yerde çalışsın.
- `python -m src.bridge` — köprü (aioquic + `AI_SHARED_TOKEN` şart).
- `python -m unittest discover -s tests -t .` — tüm test paketi (saf `unittest`,
  **pytest YOK**). `tests/unit/` birim, `tests/integration/` uçtan uca (sahte `Hat`,
  gerçek `JobStore`), `tests/regress/` ölçülerek bulunmuş her kusur için bir dosya,
  `tests/fuzz/` ayrıştırıcı sözleşmesi, `tests/model/` W yöntemi uygunluk kümesi.
  `regress_*.py` dosyalarını `tests/regress/__init__.py` içindeki `load_tests` katar.
  **Test kodunda da yorum ve docstring yok**; açıklama test metot adında ve assert
  mesajında durur. Testler ağ kullanmaz, `aioquic` gerektirmez, gerçek hattı import
  etmez, `PODCAST_JOB_ROOT`'a yazmaz (`tempfile`). `--validate` bunun yerine geçmez:
  o çalışma-zamanı bütünlük kontrolüdür ve küçültülmez.

## Referanslar
Kablo sözleşmesi: `hezarfen_backend/src/ai/protocol.rs`; referans istemci
`hezarfen_backend/tests/ai_protocol.rs` (`raw`) ve chatbot'un `src/bridge.py`'si.
Sözleşme değişirse önce oradan doğrula.
