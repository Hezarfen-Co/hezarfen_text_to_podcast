from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import capabilities, config, extract, jobs, llm, tts

STAGES = ("kaynak", "metin", "script", "tts", "mux")
ETA_SECS = 300.0

SOURCE_NOT_FOUND = "source_not_found"
NO_TEXT = "no_text_layer"
SOURCE_UNREADABLE = "source_unreadable"
UNSUPPORTED_SOURCE = "unsupported_source"
EXTRACTOR_UNAVAILABLE = "extractor_unavailable"
SCRIPT_EMPTY = "script_empty"
MUX_ERROR = "mux_error"
OUTPUT_UNWRITABLE = "output_unwritable"

OCR_DPI = 200
TITLE_CHARS = 72
WHITESPACE = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES = re.compile(r"\n{3,}")
MARKUP = re.compile(r"[*_`#>]+")

DIR_SCRIPT = "script"
DIR_AUDIO = "ses"
WORK_PREFIX = ".is-"

SYSTEM_PROMPT = (
    "Sen bir Turkce egitim podcast'i icin metin hazirlayan bir yazarsin. "
    "Yalnizca sesli okunacak duz metin dondurursun: baslik, madde imi, "
    "yildiz, emoji ve bicimlendirme KULLANMAZSIN."
)
TEACHER_PROMPT = (
    "Asagidaki ders metnini tek bir ogretmenin akici sesli anlatimina cevir. "
    "Bilgileri koru, yeni bilgi ekleme, Turkce yaz. "
    "Yalnizca anlatim metnini dondur.\n\n{metin}"
)
DIALOGUE_PROMPT = (
    "Asagidaki ders metnini iki konusanli bir diyaloga cevir: ogrenci sorar, "
    "hoca anlatir. Her satir 'Ogrenci: ' ya da 'Hoca: ' ile baslasin. "
    "Yeni bilgi ekleme, Turkce yaz. Yalnizca diyalog metnini dondur.\n\n{metin}"
)
PROMPTS = {
    "tek_ogretici": TEACHER_PROMPT,
    "ogrenci_hoca": DIALOGUE_PROMPT,
}


class EngineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _source_path(media_root: str, school: str, source_key: str) -> Path:
    path = jobs.resolve_source(media_root, school, source_key)
    if not path.is_file():
        raise FileNotFoundError(f"kaynak dosya yok: {path}")
    return path


def _clean_text(text: str) -> str:
    collapsed = WHITESPACE.sub(" ", text)
    collapsed = BLANK_LINES.sub("\n\n", collapsed)
    return collapsed.strip()


def _spoken_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = MARKUP.sub("", line).strip().lstrip("-").strip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines).strip()


def _chapter_title(text: str) -> str:
    head = text[:TITLE_CHARS].strip()
    return head if head else "bolum"


def split_chapters(text: str, limit_chars: int, limit: int | None) -> list[str]:
    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for word in words:
        if current and size + len(word) + 1 > limit_chars:
            chunks.append(" ".join(current))
            current = []
            size = 0
        current.append(word)
        size += len(word) + 1
    if current:
        chunks.append(" ".join(current))
    if limit is not None:
        return chunks[:limit]
    return chunks


def _page_texts(doc: Any, settings: Any, check: Callable[[], None]) -> tuple[list[str], int, bool]:
    tesseract = shutil.which("tesseract")
    texts: list[str] = []
    ocr_pages = 0
    for page in doc:
        check()
        raw = page.get_text("text")
        if len(raw.strip()) < int(settings.ocr_page_min_chars) and tesseract:
            try:
                layer = page.get_textpage_ocr(
                    language=settings.ocr_language, dpi=OCR_DPI
                )
                candidate = page.get_text("text", textpage=layer)
            except Exception as exc:
                config.log("warn", f"sayfa OCR yapilamadi: {exc}")
                candidate = ""
            if len(candidate.strip()) > len(raw.strip()):
                texts.append(candidate)
                ocr_pages += 1
                continue
        texts.append(raw)
    return texts, ocr_pages, bool(tesseract)


def _finish_text(
    blocks: list[str], settings: Any, kind: str, path: str
) -> tuple[str, int, int]:
    text = _clean_text("\n".join(blocks))
    if len(text) < int(settings.min_text_chars):
        raise EngineError(
            NO_TEXT,
            f"{kind} belgesinden metin cikmadi; cikan metin {len(text)} "
            f"karakter, gereken en az {settings.min_text_chars}",
        )
    config.log("info", f"{kind} metni cikarildi: {len(blocks)} blok, {path}")
    return text, len(blocks), 0


def extract_text(
    path: str, settings: Any, check: Callable[[], None]
) -> tuple[str, int, int]:
    try:
        kind = extract.sniff(path)
    except extract.ExtractError as exc:
        raise EngineError(SOURCE_UNREADABLE, str(exc)) from exc
    readers = {
        extract.KIND_DOCX: extract.docx_blocks,
        extract.KIND_PPTX: extract.pptx_slides,
        extract.KIND_ODT: extract.odt_blocks,
        extract.KIND_ODP: extract.odp_slides,
        extract.KIND_TEXT: extract.text_blocks,
    }
    reader = readers.get(kind)
    if reader is not None:
        check()
        try:
            blocks = reader(path)
        except extract.ExtractError as exc:
            raise EngineError(exc.code, str(exc)) from exc
        return _finish_text(blocks, settings, kind, path)
    if kind != extract.KIND_PDF:
        raise EngineError(UNSUPPORTED_SOURCE, extract.unsupported_message(kind))
    return _extract_pdf(path, settings, check)


def _extract_pdf(
    path: str, settings: Any, check: Callable[[], None]
) -> tuple[str, int, int]:
    try:
        import pymupdf
    except ImportError as exc:
        raise EngineError(
            EXTRACTOR_UNAVAILABLE, f"pymupdf kurulu degil; PDF metni cikarilamaz: {exc}"
        ) from exc
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise EngineError(
            SOURCE_UNREADABLE, f"PDF acilamadi ({type(exc).__name__}): {exc}"
        ) from exc
    try:
        pages, ocr_pages, tesseract = _page_texts(doc, settings, check)
    finally:
        doc.close()
    text = _clean_text("\n".join(pages))
    if len(text) < int(settings.min_text_chars):
        reason = (
            "sayfalarda metin katmani yok"
            if tesseract
            else "sayfalarda metin katmani yok ve tesseract kurulu degil "
            "(tesseract-ocr + tesseract-ocr-tur gerekir)"
        )
        raise EngineError(
            NO_TEXT,
            f"{reason}; cikan metin {len(text)} karakter, "
            f"gereken en az {settings.min_text_chars}",
        )
    if ocr_pages:
        config.log("info", f"OCR {ocr_pages} sayfada kullanildi: {path}")
    return text, len(pages), ocr_pages


SOURCE_SEPARATOR = "\n\n"


def extract_sources(
    paths: list[Path], settings: Any, check: Callable[[], None]
) -> tuple[str, int, int]:
    if not paths:
        raise EngineError(SOURCE_NOT_FOUND, "is icin hic kaynak verilmedi")
    if len(paths) == 1:
        return extract_text(str(paths[0]), settings, check)
    parts: list[str] = []
    pages = 0
    ocr_pages = 0
    for index, path in enumerate(paths, 1):
        check()
        try:
            text, page_count, ocr = extract_text(str(path), settings, check)
        except EngineError as exc:
            raise EngineError(
                exc.code,
                f"{index}. kaynak ({path.name}) okunamadi: {exc}",
            ) from exc
        parts.append(text)
        pages += page_count
        ocr_pages += ocr
    return SOURCE_SEPARATOR.join(parts), pages, ocr_pages


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp{os.getpid()}"
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _write_json(path: Path, payload: Any) -> None:
    _write_bytes(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def _script_body(
    raw: str, job_format: str, settings: Any, check: Callable[[], None]
) -> str:
    prompt_template = PROMPTS.get(job_format)
    if prompt_template is None:
        return _spoken_text(raw)
    reply = llm.chat(
        prompt_template.format(metin=raw),
        settings,
        system=SYSTEM_PROMPT,
        check=check,
    )
    body = _spoken_text(_clean_text(reply))
    if not body:
        raise EngineError(SCRIPT_EMPTY, "LLM metni bos dondu")
    return body


def _run(command: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise EngineError(MUX_ERROR, f"komut calistirilamadi: {exc}") from exc


def _mux(parts: list[Path], target: Path, work: Path) -> None:
    listing = work / "concat.txt"
    listing.write_text(
        "".join(f"file '{part.as_posix()}'\n" for part in parts), encoding="utf-8"
    )
    tmp = target.parent / f"{target.name}.tmp{os.getpid()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    completed = _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            "-f",
            "mp3",
            str(tmp),
        ]
    )
    if completed.returncode != 0:
        try:
            tmp.unlink()
        except OSError:
            pass
        detail = (completed.stderr or "").strip().splitlines()
        raise EngineError(
            MUX_ERROR,
            "ffmpeg birlestirme basarisiz: "
            + (detail[-1] if detail else f"cikis kodu {completed.returncode}"),
        )
    os.replace(tmp, target)


def duration_secs(path: Path) -> float:
    try:
        completed = _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ]
        )
    except EngineError:
        return 0.0
    try:
        return max(0.0, float(completed.stdout.strip()))
    except ValueError:
        return 0.0


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.name


def make_runner(settings: Any) -> Callable[[jobs.JobContext], None]:
    output_root = Path(settings.output_root)
    limit = int(settings.chapter_limit)
    limit = None if limit <= 0 else limit
    total = len(STAGES)

    def runner(ctx: jobs.JobContext) -> None:
        try:
            sources = [
                _source_path(settings.media_root, ctx.school, key)
                for key in ctx.source_keys
            ]
        except (ValueError, FileNotFoundError, OSError) as exc:
            config.log("error", f"is {ctx.job_id} kaynagi cozulemedi: {exc}")
            ctx.store.fail(ctx.job_id, SOURCE_NOT_FOUND)
            return
        pdf = sources[0]
        ctx.check()
        ctx.progress(STAGES[0], 0.0)
        try:
            output_root.mkdir(parents=True, exist_ok=True)
            work = Path(
                tempfile.mkdtemp(prefix=WORK_PREFIX + ctx.job_id[-8:] + "-", dir=str(output_root))
            )
        except OSError as exc:
            config.log("error", f"is {ctx.job_id} calisma dizini acilamadi: {exc}")
            ctx.store.fail(ctx.job_id, OUTPUT_UNWRITABLE)
            return
        created: list[Path] = []
        succeeded = False
        try:
            ctx.progress(STAGES[1], 1.0 / total)
            text, pages, ocr_pages = extract_sources(sources, settings, ctx.check)
            config.log(
                "info",
                f"is {ctx.job_id} metin cikarildi: {len(sources)} kaynak, "
                f"{pages} sayfa, {len(text)} karakter, ocr={ocr_pages}",
            )

            chapters = split_chapters(text, int(settings.chapter_chars), limit)
            if not chapters:
                raise EngineError(SCRIPT_EMPTY, "metinden bolum uretilemedi")
            script_dir = output_root / pdf.stem / DIR_SCRIPT / ctx.format
            script_paths: list[Path] = []
            bodies: list[str] = []
            ctx.progress(STAGES[2], 2.0 / total)
            for index, chapter in enumerate(chapters):
                ctx.check()
                body = _script_body(chapter, ctx.format, settings, ctx.check)
                bodies.append(body)
                script_path = script_dir / f"{pdf.stem}-b{index + 1:02d}.script.json"
                _write_json(
                    script_path,
                    {
                        "kaynak": pdf.name,
                        "format": ctx.format,
                        "bolum": index + 1,
                        "baslik": _chapter_title(body),
                        "metin": body,
                    },
                )
                created.append(script_path)
                script_paths.append(script_path)
                ctx.progress(
                    STAGES[2], (2 + (index + 1) / len(chapters)) / total
                )

            audio_dir = output_root / pdf.stem / DIR_AUDIO / ctx.format
            parts: list[Path] = []
            ctx.progress(STAGES[3], 3.0 / total)
            for index, body in enumerate(bodies):
                ctx.check()
                audio = tts.synthesize(body, settings)
                part = work / f"{pdf.stem}-b{index + 1:02d}.mp3"
                _write_bytes(part, audio)
                parts.append(part)
                ctx.progress(
                    STAGES[3], (3 + (index + 1) / len(bodies)) / total
                )

            ctx.progress(STAGES[4], 4.0 / total)
            final = audio_dir / f"{pdf.stem}.mp3"
            _mux(parts, final, work)
            created.append(final)

            record = ctx.finish(
                audio_id=_relative(final, output_root),
                duration_secs=duration_secs(final),
                script_id=_relative(script_paths[0], output_root),
                audio_ids=[_relative(final, output_root)],
                script_ids=[_relative(path, output_root) for path in script_paths],
                transcript="\n\n".join(bodies),
            )
            if record["state"] == jobs.STATE_DONE:
                succeeded = True
                config.log(
                    "info",
                    f"is {ctx.job_id} tamamlandi (api motoru): "
                    f"{len(script_paths)} bolum, "
                    f"{record['duration_secs']:.1f}s ses",
                )
            else:
                config.log("info", f"is {ctx.job_id} bitis aninda iptal edildi")
        except EngineError as exc:
            config.log("error", f"is {ctx.job_id} {exc.code}: {exc}")
            ctx.store.fail(ctx.job_id, exc.code)
        except (llm.LlmError, tts.TtsError) as exc:
            config.log("error", f"is {ctx.job_id} {exc.code}: {exc}")
            ctx.store.fail(ctx.job_id, exc.code)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if not succeeded:
                for path in created:
                    if not path.exists():
                        continue
                    try:
                        path.unlink()
                    except OSError:
                        pass

    return runner


def probe(settings: Any) -> dict[str, Any]:
    llm_ready, llm_reason = llm.ready(settings)
    tts_ready, tts_reason = tts.ready(settings)
    voices = "hazir" if tts_ready else "EKSIK"
    formats = ", ".join(capabilities.allowed_formats(llm_ready))
    if tts_ready and llm_ready:
        reason = f"{llm_reason}; tts {tts_reason}"
    else:
        parts = []
        if not llm_ready:
            parts.append(f"llm: {llm_reason}")
        if not tts_ready:
            parts.append(f"tts: {tts_reason}")
        reason = "; ".join(parts)
    return {
        "llm_ready": llm_ready,
        "llm_reason": llm_reason,
        "tts_ready": tts_ready,
        "tts_reason": tts_reason,
        "tts_voices": voices,
        "formats": formats,
        "reason": reason,
    }


def build_runner(settings: Any) -> tuple[Callable[[jobs.JobContext], None], dict[str, Any]]:
    return make_runner(settings), probe(settings)
