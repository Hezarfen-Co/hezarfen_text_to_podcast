from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path


def _expect_error(problems: list[str], label: str, code: str, call, *args) -> None:
    from .protocol import CapabilityError

    try:
        call(*args)
    except CapabilityError as exc:
        if exc.code != code:
            problems.append(f"{label}: '{code}' yerine '{exc.code}' dondu")
        return
    problems.append(f"{label}: '{code}' hatasi beklenirken cevap dondu")


def _guard(label: str, call, *args) -> list[str]:
    try:
        return call(*args)
    except Exception as exc:
        from . import capabilities

        capabilities.configure(None, llm_ready=False)
        return [f"{label} kontrolu beklenmedik sekilde patladi: {type(exc).__name__}: {exc}"]


def _await_state(store, job_id: str, wanted: tuple[str, ...], deadline_secs: float):
    limit = time.monotonic() + deadline_secs
    while time.monotonic() < limit:
        record = store.get(job_id)
        if record["state"] in wanted:
            return record
        time.sleep(0.005)
    return store.get(job_id)


def _check_registry(root: str) -> list[str]:
    from . import capabilities, jobs

    problems: list[str] = []
    locked = ("tek_ogretici", "ogrenci_hoca", "duz_okuma")
    if tuple(capabilities.FORMATS) != locked:
        problems.append(
            f"format kumesi projenin kilitli sozlesmesinden sapmis: {capabilities.FORMATS}"
        )
    if capabilities.DEFAULT_FORMAT != "duz_okuma":
        problems.append(
            f"varsayilan format hattinkiyle uyusmuyor: {capabilities.DEFAULT_FORMAT}"
        )
    if capabilities.KEYLESS_FORMATS != ("duz_okuma",):
        problems.append(
            f"anahtarsiz format kumesi beklenenden farkli: {capabilities.KEYLESS_FORMATS}"
        )

    if problems:
        return problems

    expected = {"podcast.submit", "podcast.status", "podcast.result", "podcast.cancel"}
    found = set(capabilities.names())
    if found != expected:
        problems.append(f"yetenek kumesi beklenenden farkli: {sorted(found)}")
    for name, handler in capabilities.REGISTRY.items():
        if not callable(handler):
            problems.append(f"{name} icin isleyici cagrilabilir degil")

    store = jobs.JobStore(root=os.path.join(root, "registry"), workers=1, max_jobs=8, stage_secs=0.0)
    capabilities.configure(store, llm_ready=False)

    submitted = capabilities.dispatch("podcast.submit", {"source_id": "ornek"})
    job_id = str(submitted.get("job_id", ""))
    if submitted.get("state") != "queued" or len(job_id) != 26:
        problems.append(f"anahtarsiz duz_okuma submit'i kabul edilmedi: {submitted}")
    if not isinstance(submitted.get("eta_secs"), int) or submitted["eta_secs"] < 1:
        problems.append(f"eta_secs bir pozitif tamsayi degil: {submitted.get('eta_secs')}")

    reported = capabilities.dispatch("podcast.status", {"job_id": job_id})
    if reported.get("state") != "queued" or reported.get("progress") != 0.0:
        problems.append(f"podcast.status kuyruktaki isi dogru bildirmedi: {reported}")
    if "error_code" not in reported or reported["error_code"] is not None:
        problems.append("podcast.status error_code alanini null olarak dondurmedi")

    _expect_error(
        problems,
        "bitmemis is icin podcast.result",
        "not_ready",
        capabilities.dispatch,
        "podcast.result",
        {"job_id": job_id},
    )

    if capabilities.dispatch("podcast.cancel", {"job_id": job_id}).get("cancelled") is not True:
        problems.append("kuyruktaki is iptal edilemedi")
    if capabilities.dispatch("podcast.status", {"job_id": job_id}).get("state") != "cancelled":
        problems.append("iptal sonrasi durum 'cancelled' olmadi")
    if capabilities.dispatch("podcast.cancel", {"job_id": job_id}).get("cancelled") is not False:
        problems.append("bitmis is icin ikinci iptal True dondu")

    _expect_error(
        problems,
        "anahtarsiz tek_ogretici submit'i",
        "llm_unavailable",
        capabilities.dispatch,
        "podcast.submit",
        {"source_id": "ornek", "format": "tek_ogretici"},
    )
    _expect_error(
        problems,
        "anahtarsiz ogrenci_hoca submit'i",
        "llm_unavailable",
        capabilities.dispatch,
        "podcast.submit",
        {"source_id": "ornek", "format": "ogrenci_hoca"},
    )

    from .protocol import CapabilityError

    try:
        capabilities.dispatch("podcast.submit", {"source_id": "ornek", "format": "tek_ogretici"})
    except CapabilityError as exc:
        if "duz_okuma" not in str(exc):
            problems.append("llm_unavailable mesaji kullanilabilir formatlari soylemiyor")

    for capability in ("podcast.status", "podcast.result", "podcast.cancel"):
        _expect_error(
            problems,
            f"{capability} bilinmeyen is icin",
            "not_found",
            capabilities.dispatch,
            capability,
            {"job_id": "YOKBOYLEBIRIS"},
        )

    for capability, payload in (
        ("podcast.submit", {}),
        ("podcast.submit", {"source_id": "ornek", "format": "opera"}),
        ("podcast.status", {"job_id": 7}),
        ("podcast.cancel", {}),
    ):
        _expect_error(
            problems,
            f"{capability} gecersiz payload {payload}",
            "bad_request",
            capabilities.dispatch,
            capability,
            payload,
        )

    _expect_error(
        problems,
        "bilinmeyen yetenek",
        "unsupported_capability",
        capabilities.dispatch,
        "podcast.bilinmeyen",
        {},
    )

    capabilities.configure(store, llm_ready=True)
    with_key = capabilities.dispatch(
        "podcast.submit", {"source_id": "ornek", "format": "tek_ogretici"}
    )
    if with_key.get("state") != "queued":
        problems.append(f"anahtar varken tek_ogretici submit'i kabul edilmedi: {with_key}")

    small = jobs.JobStore(root=os.path.join(root, "busy"), workers=1, max_jobs=1, stage_secs=0.0)
    capabilities.configure(small, llm_ready=False)
    capabilities.dispatch("podcast.submit", {"source_id": "ilk"})
    _expect_error(
        problems,
        "kuyruk tavani asildiginda submit",
        "busy",
        capabilities.dispatch,
        "podcast.submit",
        {"source_id": "ikinci"},
    )

    capabilities.configure(None, llm_ready=False)
    return problems


def _check_job_store(root: str) -> list[str]:
    from . import jobs

    problems: list[str] = []

    for state, allowed in jobs.TRANSITIONS.items():
        for target in allowed:
            if target not in jobs.STATES:
                problems.append(f"durum makinesinde bilinmeyen hedef: {state} -> {target}")
    for terminal in (jobs.STATE_DONE, jobs.STATE_FAILED, jobs.STATE_CANCELLED):
        if jobs.TRANSITIONS[terminal]:
            problems.append(f"terminal durum '{terminal}' cikis gecisi tanimliyor")

    persist_root = os.path.join(root, "persist")
    store = jobs.JobStore(root=persist_root, workers=1, max_jobs=8, stage_secs=0.0)
    record, _ = store.submit("kaynak-1", "duz_okuma")
    job_id = record["job_id"]

    path = os.path.join(persist_root, f"{job_id}.json")
    if not os.path.exists(path):
        problems.append("is durumu diske yazilmadi")
    else:
        with open(path, "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        if on_disk.get("job_id") != job_id or on_disk.get("state") != "queued":
            problems.append(f"diskteki is kaydi beklenenden farkli: {on_disk}")
    leftovers = [name for name in os.listdir(persist_root) if ".json.tmp" in name]
    if leftovers:
        problems.append(f"atomik yazmadan gecici dosya kaldi: {leftovers}")

    reopened = jobs.JobStore(root=persist_root, workers=1, max_jobs=8, stage_secs=0.0)
    try:
        again = reopened.get(job_id)
    except jobs.JobNotFound:
        problems.append("yeniden acilan depo kuyruktaki isi bulamadi")
        again = {}
    if again.get("state") != "queued":
        problems.append(f"kuyruktaki is yeniden acilista bozuldu: {again.get('state')}")
    if reopened.swept != 0:
        problems.append(f"kuyruktaki is supuruldu, supurulmemeliydi: {reopened.swept}")

    try:
        store.transition(job_id, jobs.STATE_DONE)
        problems.append("queued -> done gecisi reddedilmedi")
    except jobs.InvalidTransition:
        pass
    try:
        store.transition(job_id, "bilinmeyen")
        problems.append("bilinmeyen hedef durum reddedilmedi")
    except jobs.InvalidTransition:
        pass
    store.transition(job_id, jobs.STATE_RUNNING)
    store.finish(job_id, audio_id="a", duration_secs=1.0, script_id="s")
    try:
        store.transition(job_id, jobs.STATE_RUNNING)
        problems.append("done -> running gecisi reddedilmedi")
    except jobs.InvalidTransition:
        pass
    try:
        store.transition("YOKBOYLEBIRIS", jobs.STATE_RUNNING)
        problems.append("bilinmeyen is icin gecis reddedilmedi")
    except jobs.JobNotFound:
        pass

    sweep_root = os.path.join(root, "sweep")
    seed = jobs.JobStore(root=sweep_root, workers=1, max_jobs=8, stage_secs=0.0)
    running_record, _ = seed.submit("kaynak-2", "duz_okuma")
    running_id = running_record["job_id"]
    seed.transition(running_id, jobs.STATE_RUNNING)
    queued_record, _ = seed.submit("kaynak-3", "duz_okuma")
    queued_id = queued_record["job_id"]

    swept_store = jobs.JobStore(root=sweep_root, workers=1, max_jobs=8, stage_secs=0.0)
    if swept_store.swept != 1:
        problems.append(f"acilis supurmesi tam olarak 1 is beklerken {swept_store.swept} buldu")
    revived = swept_store.get(running_id)
    if revived["state"] != jobs.STATE_FAILED:
        problems.append(f"kosan is supurmede 'failed' olmadi: {revived['state']}")
    if revived["error_code"] != jobs.INTERRUPTED_CODE:
        problems.append(f"supurulen isin error_code'u yanlis: {revived['error_code']}")
    if swept_store.get(queued_id)["state"] != jobs.STATE_QUEUED:
        problems.append("kuyruktaki is supurmede bozuldu")

    return problems


def _check_lifecycle(root: str) -> list[str]:
    from . import capabilities, jobs

    problems: list[str] = []

    done_store = jobs.JobStore(
        root=os.path.join(root, "lifecycle"), workers=1, max_jobs=8, stage_secs=0.0
    )
    done_store.start()
    capabilities.configure(done_store, llm_ready=False)
    try:
        submitted = capabilities.dispatch("podcast.submit", {"source_id": "kaynak", "format": "duz_okuma"})
        job_id = submitted["job_id"]
        final = _await_state(done_store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)
        if final["state"] != jobs.STATE_DONE:
            problems.append(f"sahte hat 'done' ile bitmedi: {final['state']} {final['error_code']}")
        else:
            reported = capabilities.dispatch("podcast.status", {"job_id": job_id})
            if reported.get("progress") != 1.0:
                problems.append(f"biten isin ilerlemesi 1.0 degil: {reported.get('progress')}")
            payload = capabilities.dispatch("podcast.result", {"job_id": job_id})
            if not payload.get("audio_id") or not payload.get("script_id"):
                problems.append(f"podcast.result kimlikleri uretmedi: {payload}")
            if not isinstance(payload.get("duration_secs"), float):
                problems.append(f"duration_secs bir ondalik degil: {payload.get('duration_secs')}")
            if payload.get("format") != "duz_okuma":
                problems.append(f"podcast.result format alanini kaybetti: {payload}")
    finally:
        done_store.shutdown()

    cancel_store = jobs.JobStore(
        root=os.path.join(root, "cancel"), workers=1, max_jobs=8, stage_secs=0.1
    )
    cancel_store.start()
    capabilities.configure(cancel_store, llm_ready=False)
    try:
        submitted = capabilities.dispatch("podcast.submit", {"source_id": "kaynak"})
        job_id = submitted["job_id"]
        started = _await_state(cancel_store, job_id, (jobs.STATE_RUNNING,), 10.0)
        if started["state"] != jobs.STATE_RUNNING:
            problems.append(f"is kosmaya baslamadi: {started['state']}")
        else:
            capabilities.dispatch("podcast.cancel", {"job_id": job_id})
            stopped = _await_state(
                cancel_store,
                job_id,
                (jobs.STATE_CANCELLED, jobs.STATE_DONE, jobs.STATE_FAILED),
                10.0,
            )
            if stopped["state"] != jobs.STATE_CANCELLED:
                problems.append(f"kosan is isbirlikci iptali gormedi: {stopped['state']}")
    finally:
        cancel_store.shutdown()

    capabilities.configure(None, llm_ready=False)
    return problems


def _check_cancel_race(root: str) -> list[str]:
    from . import jobs

    problems: list[str] = []
    race_root = os.path.join(root, "race")
    store = jobs.JobStore(root=race_root, workers=1, max_jobs=8, stage_secs=0.0)

    record, _ = store.submit("kaynak-yaris", "duz_okuma")
    job_id = record["job_id"]
    store.transition(job_id, jobs.STATE_RUNNING)
    if store.cancel(job_id) is not True:
        problems.append("kosan is icin cancel True donmedi")
    finished = store.finish(job_id, audio_id="a", duration_secs=1.0, script_id="s")
    if finished["state"] != jobs.STATE_CANCELLED:
        problems.append(
            f"iptal bayragi setken finish() 'done' verdi: {finished['state']}"
        )
    if finished["audio_id"] is not None:
        problems.append("iptal edilen is yine de audio_id yazdi")
    with open(os.path.join(race_root, f"{job_id}.json"), "r", encoding="utf-8") as handle:
        on_disk = json.load(handle)
    if on_disk.get("state") != jobs.STATE_CANCELLED:
        problems.append(f"finish() iptali diske yansitmadi: {on_disk.get('state')}")
    return problems


def _check_early_failure(root: str) -> list[str]:
    from . import jobs

    problems: list[str] = []
    if jobs.STATE_FAILED not in jobs.TRANSITIONS[jobs.STATE_QUEUED]:
        problems.append("queued -> failed gecisi durum makinesinde tanimli degil")
    store = jobs.JobStore(
        root=os.path.join(root, "erken"), workers=1, max_jobs=8, stage_secs=0.0
    )
    record, _ = store.submit("kaynak-erken", "duz_okuma")
    job_id = record["job_id"]
    try:
        failed = store.fail(job_id, "kaynak_yok")
        if failed["state"] != jobs.STATE_FAILED:
            problems.append(f"baslamadan patlayan is 'failed' olmadi: {failed['state']}")
        if failed["error_code"] != "kaynak_yok":
            problems.append(f"hata kodu kaybedildi: {failed['error_code']}")
    except jobs.InvalidTransition as exc:
        problems.append(f"queued -> failed reddedildi, is kotayi kalici yer: {exc}")
    return problems


def _check_source_paths(root: str) -> list[str]:
    from pathlib import Path

    from . import jobs

    problems: list[str] = []
    media_root = os.path.join(root, "media")
    os.makedirs(media_root, exist_ok=True)
    for bad in ("..", "../x", "a/b", "/etc/passwd", "", "x" * 129, "..\\x", ".", "a b"):
        try:
            resolved = jobs.resolve_source(media_root, bad)
            problems.append(f"resolve_source kotu source_id'yi kabul etti: {bad!r} -> {resolved}")
        except ValueError:
            pass
    good = jobs.resolve_source(media_root, "ders-01.pdf")
    if good != Path(media_root).resolve() / "ders-01.pdf":
        problems.append(f"resolve_source gecerli source_id'yi yanlis cozdu: {good}")
    if good.exists():
        problems.append("test kurulumu bozuk: cozulen dosya gercekten var")
    return problems


def _check_eta_source(root: str) -> list[str]:
    from . import config, jobs

    problems: list[str] = []
    fixed = jobs.JobStore(
        root=os.path.join(root, "eta"),
        workers=2,
        max_jobs=8,
        stage_secs=0.0,
        job_secs=2700.0,
    )
    if fixed.estimate_eta(1) != 2700:
        problems.append(f"estimate_eta job_secs'i kullanmiyor: {fixed.estimate_eta(1)}")
    if fixed.estimate_eta(4) != 5400:
        problems.append(f"estimate_eta dalga sayisini kaybetti: {fixed.estimate_eta(4)}")

    auto = jobs.JobStore(
        root=os.path.join(root, "eta-auto"), workers=1, max_jobs=8, stage_secs=0.0
    )
    if auto.job_secs != 1.0:
        problems.append(f"job_secs otomatik tabani 1.0 degil: {auto.job_secs}")
    custom = jobs.JobStore(
        root=os.path.join(root, "eta-stages"),
        workers=1,
        max_jobs=8,
        stage_secs=0.0,
        stages=("ocr", "tts"),
    )
    if custom.stages != ("ocr", "tts"):
        problems.append(f"stages parametresi yok sayildi: {custom.stages}")

    previous = os.environ.get("PODCAST_ETA_SECS")
    os.environ["PODCAST_ETA_SECS"] = "900"
    try:
        settings = config.Config(require_token=False)
    finally:
        if previous is None:
            del os.environ["PODCAST_ETA_SECS"]
        else:
            os.environ["PODCAST_ETA_SECS"] = previous
    if settings.eta_secs != 900.0:
        problems.append(f"PODCAST_ETA_SECS okunmadi: {settings.eta_secs}")
    return problems


def _check_record_schema(root: str) -> list[str]:
    from . import jobs

    problems: list[str] = []
    schema_root = os.path.join(root, "sema")
    seed = jobs.JobStore(root=schema_root, workers=1, max_jobs=8, stage_secs=0.0)
    healthy_id = seed.submit("saglam", "duz_okuma")[0]["job_id"]
    broken_id = seed.submit("bozuk", "duz_okuma")[0]["job_id"]

    broken_path = os.path.join(schema_root, f"{broken_id}.json")
    with open(broken_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload.pop("stage")
    with open(broken_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    leftover = os.path.join(schema_root, f"{broken_id}.json.tmp999-dead")
    with open(leftover, "w", encoding="utf-8") as handle:
        handle.write("{}")

    reopened = jobs.JobStore(root=schema_root, workers=1, max_jobs=8, stage_secs=0.0)
    try:
        reopened.get(broken_id)
        problems.append("eksik alanli kayit acilista atlanmadi")
    except jobs.JobNotFound:
        pass
    try:
        if reopened.get(healthy_id)["state"] != jobs.STATE_QUEUED:
            problems.append("saglam kayit eksik alanli komsusu yuzunden bozuldu")
    except jobs.JobNotFound:
        problems.append("saglam kayit eksik alanli komsusu yuzunden kayboldu")
    if os.path.exists(leftover):
        problems.append("acilista gecici dosya artigi temizlenmedi")
    return problems


def _check_shutdown(root: str) -> list[str]:
    from . import jobs

    problems: list[str] = []
    gate = threading.Event()

    def blocking_runner(ctx) -> None:
        gate.wait(20.0)

    store = jobs.JobStore(
        root=os.path.join(root, "kapanis"),
        workers=1,
        max_jobs=8,
        stage_secs=0.0,
        runner=blocking_runner,
    )
    store.start()
    try:
        job_id = store.submit("uzun-is", "duz_okuma")[0]["job_id"]
        started = _await_state(store, job_id, (jobs.STATE_RUNNING,), 10.0)
        if started["state"] != jobs.STATE_RUNNING:
            problems.append(f"uzun is kosmaya baslamadi: {started['state']}")
            return problems
        workers = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("podcast-job")
        ]
        if not workers:
            problems.append("isci thread bulunamadi")
        if any(not thread.daemon for thread in workers):
            problems.append(
                "isci thread daemon degil; yorumlayici cikista onu join eder"
            )
        began = time.monotonic()
        store.shutdown(timeout=0.5)
        elapsed = time.monotonic() - began
        if elapsed > 3.0:
            problems.append(f"shutdown(timeout=0.5) {elapsed:.1f}s takildi")
        if not store.stopping:
            problems.append("shutdown sonrasi store.stopping False")
        if store.get(job_id)["cancel_requested"] is not True:
            problems.append("kapanista kosan ise iptal bayragi konmadi")
    finally:
        gate.set()
    return problems


def _check_framing() -> list[str]:
    from . import capabilities, protocol

    problems: list[str] = []
    message = protocol.build_hello("podcast", capabilities.names(), "gizli", 2)

    async def round_trip() -> object:
        stream = protocol.FrameStream()
        encoded = protocol.encode_frame(message)
        stream.feed(encoded[:3], end=False)
        stream.feed(encoded[3:], end=True)
        return await stream.read_frame()

    decoded = asyncio.run(round_trip())
    if decoded != message:
        problems.append("cerceve gidis-donusu bozuldu")
    if protocol.encode_frame({})[:4] != b"\x00\x00\x00\x02":
        problems.append("uzunluk oneki big-endian degil")
    if protocol.PROTOCOL != "hab/1":
        problems.append(f"protokol kimligi beklenenden farkli: {protocol.PROTOCOL}")

    try:
        protocol.parse_greeting({"code": "unauthorized", "message": "bad token"})
        problems.append("reddedilen greeting hata uretmedi")
    except protocol.HandshakeRejected:
        pass
    welcome = {"type": "welcome", "worker_id": "w1", "protocol": protocol.PROTOCOL}
    if protocol.parse_greeting(welcome) != "w1":
        problems.append("welcome greeting cozumlenemedi")
    try:
        protocol.parse_greeting({"type": "welcome", "worker_id": "w1"})
        problems.append("protokol alani eksik welcome kabul edildi")
    except protocol.HandshakeRejected:
        pass

    deadline = protocol.parse_request(
        {"id": "r1", "capability": "podcast.status", "payload": {}, "deadline_ms": 60000}
    )[3]
    if deadline is None or deadline >= 60.0:
        problems.append(f"deadline payi birakilmadi: {deadline}")
    return problems


class _FakeResult:
    def __init__(self, mp3: list, scripts: list, duration: float) -> None:
        self.mp3_yollari = mp3
        self.script_yollari = scripts
        self.ses_toplam_sn = duration
        self.adimlar: list = []


class _FakePipelineError(RuntimeError):
    pass


class _FakeInterrupt(RuntimeError):
    pass


class _FakeCtx:
    def __init__(self, cancelled: bool, stages: tuple) -> None:
        self._cancelled = cancelled
        self.stages = stages
        self.seen: list = []

    def cancelled(self) -> bool:
        return self._cancelled

    def progress(self, stage: str, value: float) -> None:
        self.seen.append((stage, value))


def _fake_pipeline(mp3: list, scripts: list, duration: float):
    class FakePipeline:
        def __init__(self, pdf, format, gunluk, cikti_koku, bolum_limiti,
                     motor_adi, ses_uret, kayit=None):
            self.pdf = pdf
            self.format = format
            self.gunluk = gunluk
            self.cikti_koku = cikti_koku
            self.bolum_limiti = bolum_limiti
            self.motor_adi = motor_adi
            self.ses_uret = ses_uret
            self.kayit = kayit
            self.sonuc = _FakeResult(list(mp3), list(scripts), duration)

        def kos(self):
            for name in ("ingest", "scriptler"):
                self.sonuc.adimlar.append(name)
                self.gunluk(f"[KOSTU ] {name}")
            return self.sonuc

    return FakePipeline


def _pipeline_store(root: str, name: str, media_root: str, mp3: list, scripts: list):
    from . import jobs, pipeline

    runner = pipeline.make_runner(
        media_root,
        os.path.join(root, name + "-cikti"),
        1,
        "supertonic-3",
        _fake_pipeline(mp3, scripts, 123.5),
        _FakePipelineError,
        _FakeInterrupt,
    )
    return jobs.JobStore(
        root=os.path.join(root, name),
        workers=1,
        max_jobs=8,
        stage_secs=0.0,
        runner=runner,
        stages=pipeline.REAL_STAGES,
    )


def _check_pipeline(root: str) -> list[str]:
    from . import capabilities, config, jobs, pipeline

    problems: list[str] = []

    if pipeline.MODES != ("simulate", "real"):
        problems.append(f"mod kumesi beklenenden farkli: {pipeline.MODES}")
    if pipeline.REAL_ETA_SECS != 2700.0:
        problems.append(f"gercek hat eta sabiti degismis: {pipeline.REAL_ETA_SECS}")

    previous = os.environ.get("PODCAST_MODE")
    os.environ["PODCAST_MODE"] = "gecersiz-mod"
    try:
        config.Config(require_token=False)
        problems.append("gecersiz PODCAST_MODE degeri kabul edildi")
    except config.ConfigError:
        pass
    finally:
        if previous is None:
            os.environ.pop("PODCAST_MODE", None)
        else:
            os.environ["PODCAST_MODE"] = previous

    simulate = config.Config(require_token=False)
    if simulate.mode == "simulate":
        if pipeline.PIPELINE_MODULE in sys.modules:
            problems.append(
                "simulate modda gercek hat import edildi: "
                f"{pipeline.PIPELINE_MODULE} sys.modules icinde"
            )
        if pipeline.stages_for(simulate) is not None:
            problems.append("simulate modda gercek asama adlari zorlaniyor")
        if pipeline.job_secs_for(simulate) is not None:
            problems.append("simulate modda gercek hat etasi zorlaniyor")

    for bad in ("", "   "):
        try:
            pipeline.check_pipeline_path(bad)
            problems.append(f"bos PODCAST_PIPELINE_PATH kabul edildi: {bad!r}")
        except pipeline.PipelineUnavailable:
            pass
    try:
        pipeline.check_pipeline_path(os.path.join(root, "olmayan-hat"))
        problems.append("var olmayan PODCAST_PIPELINE_PATH kabul edildi")
    except pipeline.PipelineUnavailable:
        pass
    empty_root = os.path.join(root, "hatsiz")
    os.makedirs(empty_root, exist_ok=True)
    try:
        pipeline.check_pipeline_path(empty_root)
        problems.append("router/hat.py icermeyen kok kabul edildi")
    except pipeline.PipelineUnavailable:
        pass

    if pipeline.normalize_limit(0) is not None or pipeline.normalize_limit(-1) is not None:
        problems.append("bolum limiti 0/negatif icin 'hepsi' anlamina gelmiyor")
    if pipeline.normalize_limit(3) != 3:
        problems.append("pozitif bolum limiti bozuldu")

    output_root = os.path.join(root, "hat-cikti")
    def _out_path(*parts: str) -> str:
        return os.path.join(output_root, *parts)

    mapped = pipeline.derive_outputs(
        _FakeResult(
            [_out_path("ders", "ses", "duz_okuma", "ders-b01.mp3"),
             _out_path("ders", "ses", "duz_okuma", "ders-b02.mp3")],
            [_out_path("ders", "script", "duz_okuma", "ders-b01.script.json"),
             _out_path("ders", "script", "duz_okuma", "ders-b02.script.json")],
            321.25,
        ),
        output_root,
    )
    if mapped["audio_id"] != "ders/ses/duz_okuma/ders-b01.mp3":
        problems.append(f"audio_id cikti kokune gore relatif degil: {mapped['audio_id']}")
    if mapped["script_id"] != "ders/script/duz_okuma/ders-b01.script.json":
        problems.append(f"script_id cikti kokune gore relatif degil: {mapped['script_id']}")
    if mapped["duration_secs"] != 321.25:
        problems.append(f"duration_secs ses_toplam_sn'den gelmedi: {mapped['duration_secs']}")
    if len(mapped["audio_ids"]) != 2 or len(mapped["script_ids"]) != 2:
        problems.append(f"kimlik listeleri eksik: {mapped}")

    other = pipeline.derive_outputs(
        _FakeResult(
            [_out_path("ders", "ses", "tek_ogretici", "ders-b01.mp3")],
            [_out_path("ders", "script", "tek_ogretici", "ders-b01.script.json")],
            10.0,
        ),
        output_root,
    )
    if other["audio_id"] == mapped["audio_id"]:
        problems.append(
            f"ayni PDF'in iki formati AYNI audio_id dondu: {other['audio_id']}"
        )

    trimmed = pipeline.derive_outputs(
        _FakeResult(
            [_out_path("ders", "ses", "duz_okuma", "ders-b01.mp3")],
            [_out_path("ders", "script", "duz_okuma", "ders-b01.script.json"),
             _out_path("ders", "script", "duz_okuma", "ders-b02.script.json"),
             _out_path("ders", "script", "duz_okuma", "ders-b03.script.json")],
            10.0,
        ),
        output_root,
    )
    if len(trimmed["script_ids"]) != 1:
        problems.append(
            f"script_ids ses bolumlerine hizalanmadi: {trimmed['script_ids']}"
        )

    outside = pipeline.derive_outputs(
        _FakeResult(["/baska/yer/x.mp3"], ["/baska/yer/x.script.json"], 1.0),
        output_root,
    )
    if outside["audio_id"] != "x.mp3":
        problems.append(f"kok disi yol dosya adina dusmedi: {outside['audio_id']}")

    if pipeline.derive_outputs(_FakeResult([], [], 0.0), output_root)["audio_ids"]:
        problems.append("bos mp3 listesinden audio_ids uretildi")

    for missing in ("router/yonlendirici.py", "config/router.toml", "script/plan.py"):
        fake_root = os.path.join(root, "sahte-hat")
        os.makedirs(os.path.join(fake_root, "router"), exist_ok=True)
        with open(os.path.join(fake_root, "router", "hat.py"), "w") as h:
            h.write("")
        try:
            pipeline.check_pipeline_path(fake_root)
            problems.append(f"eksik {missing} olan kok mod kapisini gecti")
        except pipeline.PipelineUnavailable:
            pass
        break

    cancelled_ctx = _FakeCtx(True, pipeline.REAL_STAGES)
    hook = pipeline.build_log_hook(cancelled_ctx, lambda: 1, pipeline.REAL_STAGES, _FakeInterrupt)
    try:
        hook("[KOSTU ] ingest")
        problems.append("iptal istenmisken gunluk kancasi kesme firlatmadi")
    except _FakeInterrupt:
        pass
    if cancelled_ctx.seen:
        problems.append("iptal edilen iste kanca yine de ilerleme yazdi")

    live_ctx = _FakeCtx(False, pipeline.REAL_STAGES)
    live_hook = pipeline.build_log_hook(live_ctx, lambda: 2, pipeline.REAL_STAGES, _FakeInterrupt)
    live_hook("[KOSTU ] scriptler")
    if live_ctx.seen != [("quiz", 0.5)]:
        problems.append(f"kanca ilerlemeyi adim sayisindan turetmedi: {live_ctx.seen}")

    media_root = os.path.join(root, "hat-medya")
    os.makedirs(media_root, exist_ok=True)
    with open(os.path.join(media_root, "ders.pdf"), "wb") as handle:
        handle.write(b"%PDF-1.4\n")

    ok_store = _pipeline_store(
        root,
        "hat-ok",
        media_root,
        ["/out/ders/bolum-1.mp3", "/out/ders/bolum-2.mp3"],
        ["/out/ders/bolum-1.txt", "/out/ders/bolum-2.txt"],
    )
    ok_store.start()
    capabilities.configure(ok_store, llm_ready=False)
    try:
        job_id = capabilities.dispatch("podcast.submit", {"source_id": "ders.pdf"})["job_id"]
        final = _await_state(ok_store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)
        if final["state"] != jobs.STATE_DONE:
            problems.append(
                "sahte Hat ile kosan gercek runner 'done' vermedi: "
                f"{final['state']} {final['error_code']}"
            )
        else:
            payload = capabilities.dispatch("podcast.result", {"job_id": job_id})
            if payload.get("audio_id") != "bolum-1.mp3":
                problems.append(f"result audio_id yanlis: {payload.get('audio_id')}")
            if payload.get("script_id") != "bolum-1.txt":
                problems.append(f"result script_id yanlis: {payload.get('script_id')}")
            if payload.get("duration_secs") != 123.5:
                problems.append(f"result duration_secs yanlis: {payload.get('duration_secs')}")
            if payload.get("audio_ids") != ["bolum-1.mp3", "bolum-2.mp3"]:
                problems.append(f"result audio_ids yanlis: {payload.get('audio_ids')}")
            if payload.get("script_ids") != ["bolum-1.txt", "bolum-2.txt"]:
                problems.append(f"result script_ids yanlis: {payload.get('script_ids')}")
    finally:
        ok_store.shutdown()

    quiet_store = _pipeline_store(root, "hat-sessiz", media_root, [], ["/out/ders/bolum-1.txt"])
    quiet_store.start()
    capabilities.configure(quiet_store, llm_ready=False)
    try:
        job_id = capabilities.dispatch("podcast.submit", {"source_id": "ders.pdf"})["job_id"]
        final = _await_state(quiet_store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)
        if final["state"] != jobs.STATE_FAILED:
            problems.append(f"mp3 uretilmeyen is 'failed' olmadi: {final['state']}")
        if final["error_code"] != pipeline.NO_AUDIO:
            problems.append(f"sessiz kosunun hata kodu yanlis: {final['error_code']}")
        if final["audio_id"] is not None:
            problems.append("ses uretmeyen is yine de audio_id yazdi")
    finally:
        quiet_store.shutdown()

    missing_store = _pipeline_store(root, "hat-kayip", media_root, ["/out/x.mp3"], ["/out/x.txt"])
    missing_store.start()
    capabilities.configure(missing_store, llm_ready=False)
    try:
        job_id = capabilities.dispatch("podcast.submit", {"source_id": "yok.pdf"})["job_id"]
        final = _await_state(missing_store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)
        if final["state"] != jobs.STATE_FAILED:
            problems.append(f"kaynagi olmayan is 'failed' olmadi: {final['state']}")
        if final["error_code"] != pipeline.SOURCE_NOT_FOUND:
            problems.append(f"kayip kaynagin hata kodu yanlis: {final['error_code']}")
    finally:
        missing_store.shutdown()

    capabilities.configure(None, llm_ready=False)
    return problems


def validate() -> int:
    from . import capabilities, config

    settings = config.load(require_token=False)
    print(f"[bridge] yapilandirma: {settings.summary()}", flush=True)
    if not settings.has_token:
        print(
            "[bridge] uyari: AI_SHARED_TOKEN tanimsiz; kopru bu haliyle acilista cikar",
            flush=True,
        )
    print(f"[bridge] {capabilities.format_summary(settings.has_llm_key)}", flush=True)

    from . import pipeline

    if settings.mode == pipeline.MODE_REAL:
        try:
            source_root = pipeline.check_pipeline_path(settings.pipeline_path)
        except pipeline.PipelineUnavailable as exc:
            print(f"[bridge] PODCAST_MODE=real ama hat kaynagi yok: {exc}", flush=True)
            return 2
        print(f"[bridge] gercek hat kaynagi: {source_root}", flush=True)
    else:
        print(f"[bridge] uyari: {pipeline.FAKE_WARNING}", flush=True)

    problems = _guard("cerceveleme", _check_framing)
    with tempfile.TemporaryDirectory(prefix="podcast-validate-") as root:
        problems += _guard("is deposu", _check_job_store, root)
        problems += _guard("yetenek defteri", _check_registry, root)
        problems += _guard("yasam dongusu", _check_lifecycle, root)
        problems += _guard("iptal yarisi", _check_cancel_race, root)
        problems += _guard("erken hata", _check_early_failure, root)
        problems += _guard("kaynak yolu", _check_source_paths, root)
        problems += _guard("eta kaynagi", _check_eta_source, root)
        problems += _guard("kayit semasi", _check_record_schema, root)
        problems += _guard("kapanma", _check_shutdown, root)
        problems += _guard("gercek hat kablosu", _check_pipeline, root)

    if problems:
        for problem in problems:
            print(f"[bridge] butunluk hatasi: {problem}", flush=True)
        return 1

    print(f"[bridge] yetenekler: {', '.join(capabilities.names())}", flush=True)
    print("[bridge] butunluk kontrolu tamam (ag kullanilmadi)", flush=True)
    return 0


def _check_writable(root: str, label: str, problems: list[str], notes: list[str]) -> None:
    target = Path(root)
    probe = target / f".health-{os.getpid()}"
    try:
        target.mkdir(parents=True, exist_ok=True)
        with open(probe, "w", encoding="ascii") as handle:
            handle.write("ok")
        probe.unlink()
    except OSError as exc:
        problems.append(f"{label} yazilamiyor ({target}): {exc}")
        return
    notes.append(f"{label} yazilabilir: {target}")


def health() -> int:
    from . import config, jobs, pipeline

    settings = config.load(require_token=False)
    problems: list[str] = []
    notes: list[str] = [f"mod={settings.mode}"]

    _check_writable(settings.job_root, "is koku", problems, notes)
    _check_writable(settings.output_root, "cikti koku", problems, notes)

    if settings.mode == pipeline.MODE_REAL:
        try:
            source_root = pipeline.check_pipeline_path(settings.pipeline_path)
            pipeline.load_pipeline(settings.pipeline_path)
            notes.append(f"hat iceri alindi: {source_root}")
        except pipeline.PipelineUnavailable as exc:
            problems.append(f"hat yuklenemedi: {exc}")
    else:
        notes.append("simulate modda hat denetlenmez")

    status = jobs.read_status(settings.job_root)
    if status is None:
        problems.append(
            f"kopru durum dosyasi yok ({jobs.status_path(settings.job_root)}); "
            "kayit henuz yapilmadi"
        )
    elif not status.get("registered"):
        problems.append("kopru backend'e kayitli degil (durum dosyasi registered=false)")
    else:
        notes.append(f"kayitli: worker_id={status.get('worker_id', '?')}")

    for note in notes:
        print(f"[bridge] saglik: {note}", flush=True)
    if problems:
        for problem in problems:
            print(f"[bridge] saglik HATASI: {problem}", flush=True)
        return 1
    print("[bridge] saglik: TAMAM", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hezarfen podcast servisi")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Yapilandirma, yetenek defteri, is deposu ve cerceveleme butunlugunu ag olmadan dogrular.",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Canlilik denetimi: is/cikti koku yazilabilir mi, real modda hat iceri alinabilir mi, kopru kayitli mi.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.health:
        return health()
    if args.validate:
        return validate()

    from .bridge import main as bridge_main

    return bridge_main()


if __name__ == "__main__":
    sys.exit(main())
