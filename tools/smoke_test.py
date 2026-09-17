from __future__ import annotations

import argparse
import os
import sys
import time

DEFAULT_PIPELINE = r"C:\PROJECTS\podcast"
SMOKE_SCHOOL = "yerel-denetim"
SMOKE_USER = "yerel-denetci"
DEFAULT_MEDIA = r"C:\PROJECTS\podcast\samples"
DEFAULT_SOURCE = "kisa_slayt.pdf"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gercek hatta tek is kosturur (--validate'in parcasi DEGIL)"
    )
    parser.add_argument("--pipeline-path", default=os.environ.get("PODCAST_PIPELINE_PATH", DEFAULT_PIPELINE))
    parser.add_argument("--media-root", default=os.environ.get("PODCAST_MEDIA_ROOT", DEFAULT_MEDIA))
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--source-key", default="")
    parser.add_argument("--format", default="duz_okuma")
    parser.add_argument("--job-root", default=os.path.join(os.getcwd(), "out", "smoke", "jobs"))
    parser.add_argument("--output-root", default=os.path.join(os.getcwd(), "out", "smoke", "output"))
    parser.add_argument("--ledger", default=os.path.join(os.getcwd(), "out", "smoke", "ledger.sqlite"))
    parser.add_argument("--timeout", type=float, default=7200.0)
    parser.add_argument("--cancel-after", type=float, default=0.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    os.environ["PODCAST_MODE"] = "real"
    os.environ["PODCAST_PIPELINE_PATH"] = args.pipeline_path
    os.environ["PODCAST_MEDIA_ROOT"] = args.media_root
    os.environ["PODCAST_JOB_ROOT"] = args.job_root
    os.environ["PODCAST_OUTPUT_ROOT"] = args.output_root
    os.environ["PODCAST_LEDGER_DB"] = args.ledger
    os.environ.setdefault("LOG_LEVEL", "debug")

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from src import capabilities, config, jobs, pipeline

    settings = config.load(require_token=False)
    print(f"[smoke] yapilandirma: {settings.summary()}", flush=True)

    try:
        runner, probe = pipeline.build_runner(settings)
    except pipeline.PipelineUnavailable as exc:
        print(f"[smoke] gercek hat yuklenemedi: {exc}", flush=True)
        return 2
    print(
        "[smoke] defter=%s  LLM=%s (%s)"
        % (args.ledger, "HAZIR" if probe["llm_ready"] else "YOK",
           probe["llm_reason"][:80]),
        flush=True,
    )

    store = jobs.JobStore(
        root=settings.job_root,
        workers=1,
        max_jobs=2,
        stage_secs=0.0,
        runner=runner,
        stages=pipeline.REAL_STAGES,
        job_secs=pipeline.REAL_ETA_SECS,
    )
    store.start()
    capabilities.configure(store, bool(probe["llm_ready"]))

    job_id = jobs.new_job_id()
    submitted = capabilities.dispatch(
        "podcast.submit",
        SMOKE_SCHOOL,
        {"job_id": job_id, "source_id": args.source,
         "source_key": args.source_key or args.source,
         "user_id": SMOKE_USER, "format": args.format},
    )
    print(f"[smoke] is verildi: {job_id} eta={submitted['eta_secs']}s", flush=True)

    terminal = (jobs.STATE_DONE, jobs.STATE_FAILED, jobs.STATE_CANCELLED)
    began = time.monotonic()
    cancelled = False
    last = ""
    try:
        while True:
            record = store.get(job_id)
            if record["state"] in terminal:
                break
            line = f"{record['state']} {record['stage']} {record['progress']}"
            if line != last:
                print(f"[smoke] {line}", flush=True)
                last = line
            elapsed = time.monotonic() - began
            if args.cancel_after > 0 and not cancelled and elapsed >= args.cancel_after:
                print(f"[smoke] {elapsed:.0f}s sonra iptal isteniyor", flush=True)
                capabilities.dispatch("podcast.cancel", SMOKE_SCHOOL, {"job_id": job_id})
                cancelled = True
            if elapsed > args.timeout:
                print(f"[smoke] zaman asimi {args.timeout}s", flush=True)
                break
            time.sleep(1.0)
    finally:
        store.shutdown()

    record = store.get(job_id)
    print(f"[smoke] durum={record['state']} hata={record['error_code']}", flush=True)
    if record["state"] == jobs.STATE_DONE:
        print(
            "[smoke] backend satiri icin: school=%s job_id=%s user_id=%s"
            % (SMOKE_SCHOOL, job_id, SMOKE_USER),
            flush=True,
        )
        for key in ("audio_id", "script_id", "duration_secs", "audio_ids", "script_ids"):
            print(f"[smoke] {key} = {record.get(key)}", flush=True)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
