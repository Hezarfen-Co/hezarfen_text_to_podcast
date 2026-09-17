from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from . import api_engine, config, jobs

MODE_SIMULATE = "simulate"
MODE_REAL = "real"
MODES = (MODE_SIMULATE, MODE_REAL)

ENGINE_API = "api"
ENGINE_LOCAL = "local"
ENGINES = config.PIPELINE_ENGINES

REAL_STAGES = ("ingest", "scriptler", "quiz", "ses")
REQUIRED_PIPELINE_FILES = (
    "router/hat.py",
    "router/yonlendirici.py",
    "config/router.toml",
    "ingest/__init__.py",
    "script/plan.py",
    "ses/montaj.py",
)
PIPELINE_MISCONFIGURED = "pipeline_misconfigured"
REAL_ETA_SECS = 2700.0

PIPELINE_MODULE = "router.hat"

SOURCE_NOT_FOUND = "source_not_found"
NO_AUDIO = "no_audio"

PROVISION_COMMAND = "bash deploy/setup-local-engine.sh"
LOCAL_SOURCE_NOTE = (
    "lokal motorun vendored Hat kaynagi (router/hat.py) artik hicbir repoda yok; "
    "yol secilebilir ama kaynak geri gelmedikce kosulamaz"
)

FAKE_WARNING = "SAHTE hat kosuyor, uretilen ses GERCEK DEGIL"


class PipelineUnavailable(Exception):
    pass


def check_pipeline_path(pipeline_path: str) -> Path:
    if not isinstance(pipeline_path, str) or not pipeline_path.strip():
        raise PipelineUnavailable(
            "PODCAST_PIPELINE_PATH tanimsiz; PODCAST_MODE=real hat kaynaginin "
            "kokunu ister (ornek: /opt/podcast)"
        )
    root = Path(pipeline_path.strip()).expanduser()
    try:
        root = root.resolve()
    except OSError as exc:
        raise PipelineUnavailable(
            f"PODCAST_PIPELINE_PATH cozulemedi ({pipeline_path}): {exc}"
        ) from exc
    if not root.is_dir():
        raise PipelineUnavailable(f"PODCAST_PIPELINE_PATH bir dizin degil: {root}")
    for part in REQUIRED_PIPELINE_FILES:
        if not (root / part).is_file():
            raise PipelineUnavailable(
                f"PODCAST_PIPELINE_PATH altinda {part} yok: {root}"
            )
    return root


def check_local_venv(venv_path: str) -> Path:
    if not isinstance(venv_path, str) or not venv_path.strip():
        raise PipelineUnavailable(
            "PODCAST_LOCAL_VENV tanimsiz; lokal motorun bagimliliklari bir hacimde "
            f"beklenir. Kurulum (imaj DEGISMEZ): {PROVISION_COMMAND}"
        )
    root = Path(venv_path.strip()).expanduser()
    if not root.is_dir():
        raise PipelineUnavailable(
            f"PODCAST_LOCAL_VENV dizini yok: {root}. Bagimliliklar imaja GIRMEZ; "
            f"hacme kurulur. Kurulum (imaj DEGISMEZ, workflow KOSTURMAZ): "
            f"{PROVISION_COMMAND}. NOT: {LOCAL_SOURCE_NOTE}"
        )
    return root


def prepare_path(pipeline_path: str, venv_path: str = "") -> Path:
    root = check_pipeline_path(pipeline_path)
    paths = [str(root)]
    if venv_path:
        paths.append(str(check_local_venv(venv_path)))
    for text in paths:
        if text not in sys.path:
            sys.path.append(text)
    return root


def load_pipeline(pipeline_path: str, venv_path: str = "") -> tuple[Any, Any, Any, Any]:
    root = prepare_path(pipeline_path, venv_path)
    try:
        from router.hat import Hat, HatHatasi, KasitliKesme
        from router.kayit import Kayit
    except Exception as exc:
        raise PipelineUnavailable(
            f"{PIPELINE_MODULE} iceri alinamadi ({root}): {type(exc).__name__}: {exc}"
        ) from exc
    return Hat, HatHatasi, KasitliKesme, Kayit


def probe_pipeline(pipeline_path: str, venv_path: str = "") -> dict[str, Any]:
    prepare_path(pipeline_path, venv_path)
    try:
        from router.yonlendirici import Katman, Yonlendirici
    except Exception as exc:
        raise PipelineUnavailable(
            f"router.yonlendirici iceri alinamadi: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        router = Yonlendirici()
        resolution = router.katman_cozumu(Katman.GUCLU)
        general = router.ayarlar.genel
    except Exception as exc:
        raise PipelineUnavailable(
            f"hat yapilandirmasi okunamadi: {type(exc).__name__}: {exc}"
        ) from exc
    return {
        "llm_ready": bool(resolution.hazir),
        "llm_reason": str(resolution.gerekce),
        "maks_usd": float(general.maks_usd),
        "maks_cagri_usd": float(general.maks_cagri_usd),
        "onbellek": bool(general.onbellek),
    }


def resolve_pdf(media_root: str, source_id: str) -> Path:
    path = jobs.resolve_source(media_root, source_id)
    if not path.is_file():
        raise FileNotFoundError(f"kaynak dosya yok: {path}")
    return path


def normalize_limit(chapter_limit: int) -> int | None:
    return None if int(chapter_limit) <= 0 else int(chapter_limit)


def _relative_id(value: Any, output_root: Path | None) -> str:
    path = Path(str(value))
    if output_root is not None:
        try:
            return path.resolve().relative_to(output_root.resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return path.name


def _names(values: Any, output_root: Path | None = None) -> list[str]:
    if not values:
        return []
    return [_relative_id(value, output_root) for value in values]


def _chapter_key(identifier: str) -> str:
    base = identifier.rsplit("/", 1)[-1]
    for suffix in (".script.json", ".mp3", ".json"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _align_scripts(audio_ids: list[str], script_ids: list[str]) -> list[str]:
    if not audio_ids or not script_ids:
        return script_ids
    chapters = {_chapter_key(a) for a in audio_ids}
    matched = [s for s in script_ids if _chapter_key(s) in chapters]
    if not matched:
        config.log(
            "warn",
            "script kimlikleri ses bolumleriyle eslesmedi; filtrelenmeden "
            "donuluyor (audio=%d script=%d)" % (len(audio_ids), len(script_ids)),
        )
        return script_ids
    return matched


def derive_outputs(result: Any, output_root: Any = None) -> dict[str, Any]:
    root = Path(output_root) if output_root else None
    audio_ids = _names(getattr(result, "mp3_yollari", None), root)
    script_ids = _align_scripts(
        audio_ids, _names(getattr(result, "script_yollari", None), root)
    )
    try:
        duration = float(getattr(result, "ses_toplam_sn", 0.0) or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "audio_id": audio_ids[0] if audio_ids else "",
        "script_id": script_ids[0] if script_ids else "",
        "duration_secs": duration,
        "audio_ids": audio_ids,
        "script_ids": script_ids,
    }


def build_log_hook(
    ctx: Any,
    steps: Callable[[], int],
    stages: tuple[str, ...],
    interrupt: Any,
) -> Callable[[str], None]:
    names = tuple(stages) if stages else REAL_STAGES
    total = max(1, len(names))

    def hook(message: str) -> None:
        if ctx.cancelled():
            raise interrupt("iptal istendi")
        try:
            done = int(steps())
        except Exception:
            done = 0
        if done < 0:
            done = 0
        ctx.progress(names[min(done, total - 1)], min(float(done) / float(total), 1.0))
        config.log("debug", f"hat: {str(message).strip()}")

    return hook


def make_runner(
    media_root: str,
    output_root: str,
    chapter_limit: int,
    tts_engine: str,
    pipeline_factory: Any,
    pipeline_error: Any,
    interrupt: Any,
    ledger_factory: Any = None,
    ledger_path: str = "",
    budget: dict[str, Any] | None = None,
) -> Callable[[jobs.JobContext], None]:
    limit = normalize_limit(chapter_limit)
    output_dir = Path(output_root)
    budget = budget or {}

    def runner(ctx: jobs.JobContext) -> None:
        stages = tuple(ctx.stages) if ctx.stages else REAL_STAGES
        try:
            pdf = resolve_pdf(media_root, ctx.source_id)
        except (ValueError, FileNotFoundError, OSError) as exc:
            config.log("error", f"is {ctx.job_id} kaynagi cozulemedi: {exc}")
            ctx.store.fail(ctx.job_id, SOURCE_NOT_FOUND)
            return

        ctx.check()
        ctx.progress(stages[0], 0.0)

        holder: list[Any] = []

        def steps() -> int:
            if not holder:
                return 0
            return len(getattr(getattr(holder[0], "sonuc", None), "adimlar", ()) or ())

        hook = build_log_hook(ctx, steps, stages, interrupt)

        try:
            ledger = None
            if ledger_factory is not None and ledger_path:
                ledger = ledger_factory(
                    ledger_path,
                    maks_usd=float(budget.get("maks_usd", 5.0)),
                    maks_cagri_usd=float(budget.get("maks_cagri_usd", 0.50)),
                    onbellek_acik=bool(budget.get("onbellek", True)),
                )
            pipeline = pipeline_factory(
                pdf=str(pdf),
                format=ctx.format,
                gunluk=hook,
                cikti_koku=output_dir,
                bolum_limiti=limit,
                motor_adi=tts_engine,
                ses_uret=True,
                kayit=ledger,
            )
        except pipeline_error as exc:
            config.log("error", f"is {ctx.job_id} hat kurulamadi: {exc}")
            ctx.store.fail(ctx.job_id, PIPELINE_MISCONFIGURED)
            return
        except Exception as exc:
            config.log(
                "error",
                f"is {ctx.job_id} hat kurulamadi ({type(exc).__name__}): {exc}",
            )
            ctx.store.fail(ctx.job_id, PIPELINE_MISCONFIGURED)
            return
        holder.append(pipeline)

        config.log("info", f"is {ctx.job_id} gercek hatta veriliyor: {pdf.name}")
        try:
            result = pipeline.kos()
        except interrupt as exc:
            if ctx.cancelled():
                raise jobs.JobCancelled(ctx.job_id) from exc
            config.log("error", f"is {ctx.job_id} hat tarafindan bilerek kesildi: {exc}")
            raise

        outputs = derive_outputs(result, output_dir)
        if outputs["audio_ids"] and outputs["duration_secs"] <= 0.0:
            config.log(
                "warn",
                f"is {ctx.job_id} mp3 uretti ama sure 0.0 olctu; "
                f"duration_secs guvenilmez",
            )
        if not outputs["audio_ids"]:
            config.log(
                "error",
                f"is {ctx.job_id} hicbir mp3 uretmedi; bos cevap donulmuyor",
            )
            ctx.store.fail(ctx.job_id, NO_AUDIO)
            return

        record = ctx.finish(
            audio_id=outputs["audio_id"],
            duration_secs=outputs["duration_secs"],
            script_id=outputs["script_id"],
            audio_ids=outputs["audio_ids"],
            script_ids=outputs["script_ids"],
        )
        if record["state"] == jobs.STATE_DONE:
            config.log(
                "info",
                f"is {ctx.job_id} tamamlandi (gercek hat): "
                f"{len(outputs['audio_ids'])} mp3, "
                f"{outputs['duration_secs']:.1f}s ses",
            )
        else:
            config.log("info", f"is {ctx.job_id} bitis aninda iptal edildi")

    return runner


def build_runner(settings: Any) -> tuple[Callable[[jobs.JobContext], None], dict[str, Any]]:
    venv = getattr(settings, "local_venv", "")
    pipeline_factory, pipeline_error, interrupt, ledger_factory = load_pipeline(
        settings.pipeline_path, venv
    )
    probe = probe_pipeline(settings.pipeline_path, venv)
    runner = make_runner(
        settings.media_root,
        settings.output_root,
        settings.chapter_limit,
        settings.tts_engine,
        pipeline_factory,
        pipeline_error,
        interrupt,
        ledger_factory=ledger_factory,
        ledger_path=settings.ledger_db,
        budget=probe,
    )
    return runner, probe


def job_secs_for(settings: Any) -> float | None:
    if settings.eta_secs > 0:
        return float(settings.eta_secs)
    if settings.mode != MODE_REAL:
        return None
    if settings.engine == ENGINE_API:
        return api_engine.ETA_SECS
    return REAL_ETA_SECS


def stages_for(settings: Any) -> tuple[str, ...] | None:
    if settings.mode != MODE_REAL:
        return None
    return api_engine.STAGES if settings.engine == ENGINE_API else REAL_STAGES
