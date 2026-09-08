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

## Dosyalar ve komutlar
`config.py` ortam+log · `protocol.py` çerçeveleme+mesajlar · `jobs.py` iş deposu+
durum makinesi+sahte hat · `pipeline.py` gerçek `router.hat.Hat` runner'ı (tembel
import) · `capabilities.py` yetenek defteri · `bridge.py` QUIC istemcisi ·
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
