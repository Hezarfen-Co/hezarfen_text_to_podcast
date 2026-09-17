from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import random
import ssl
import sys
import urllib.error
import urllib.request
from functools import partial
from typing import Any, Callable

from aioquic.asyncio import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import ConnectionTerminated, QuicEvent, StreamDataReceived

from . import api_engine, backend, capabilities, config, jobs, pipeline, protocol
from .protocol import CapabilityError

CERT_FETCH_TIMEOUT_SECS = 10


class BridgeProtocol(QuicConnectionProtocol):
    def __init__(self, *args: Any, settings: config.Config, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._settings = settings
        self._streams: dict[int, protocol.FrameStream] = {}
        self._control_sid: int | None = None
        self._ping_uid = 0
        self._inflight = 0
        self._tasks: set[asyncio.Future] = set()

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, StreamDataReceived):
            sid = event.stream_id
            stream = self._streams.get(sid)
            if stream is None:
                if sid % 4 not in (0, 1):
                    return
                stream = protocol.FrameStream()
                self._streams[sid] = stream
                if sid % 4 == 1:
                    task = asyncio.ensure_future(self._serve_request(sid, stream))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
            stream.feed(event.data, event.end_stream)
        elif isinstance(event, ConnectionTerminated):
            for stream in self._streams.values():
                stream.feed(b"", end=True)

    async def register(self) -> None:
        sid = self._quic.get_next_available_stream_id()
        self._control_sid = sid
        stream = protocol.FrameStream()
        self._streams[sid] = stream
        hello = protocol.build_hello(
            self._settings.service,
            capabilities.names(),
            self._settings.token,
            self._settings.max_concurrent,
        )
        self._send_frame(sid, hello, end=False)
        greeting = await asyncio.wait_for(
            stream.read_frame(), timeout=protocol.GREETING_TIMEOUT_SECS
        )
        worker_id = protocol.parse_greeting(greeting)
        backend.set_client(asyncio.get_running_loop(), self)
        jobs.write_status(self._settings.job_root, True, worker_id)
        config.log(
            "info",
            f"kayit basarili: worker_id={worker_id} protokol={protocol.PROTOCOL} "
            f"yetenekler={','.join(capabilities.names())}",
        )

    async def api_get(
        self,
        school: str,
        path: str,
        query: str | None = None,
        on_behalf_of: str | None = None,
        timeout: float = protocol.API_TIMEOUT_SECS,
    ) -> protocol.ApiResponse:
        sid = self._quic.get_next_available_stream_id()
        stream = protocol.FrameStream()
        self._streams[sid] = stream
        request = protocol.build_api_request(
            jobs.new_job_id(), school, path, query, on_behalf_of
        )
        try:
            self._send_frame(sid, request, end=True)
            frame = await asyncio.wait_for(stream.read_frame(), timeout=timeout)
        finally:
            self._streams.pop(sid, None)
        response = protocol.parse_api_response(frame)
        config.log(
            "debug",
            f"api {path} -> status={response.status} okul={school} "
            f"kim={on_behalf_of or 'servis(ai rolu)'}",
        )
        return response

    async def call_capability(
        self, school: str, capability: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        sid = self._quic.get_next_available_stream_id()
        stream = protocol.FrameStream()
        self._streams[sid] = stream
        request_id = jobs.new_job_id()
        request = protocol.build_capability_call(request_id, school, capability, payload)
        try:
            self._send_frame(sid, request, end=True)
            frame = await stream.read_frame()
        finally:
            self._streams.pop(sid, None)
        return protocol.parse_capability_response(frame, request_id)

    async def upload_blob(
        self,
        school: str,
        job_id: str,
        name: str,
        content_type: str,
        data: bytes,
        duration_secs: float | None = None,
    ) -> dict[str, Any]:
        sid = self._quic.get_next_available_stream_id()
        stream = protocol.FrameStream()
        self._streams[sid] = stream
        request_id = jobs.new_job_id()
        header = protocol.build_upload_request(
            request_id, school, job_id, name, content_type, len(data), duration_secs
        )
        try:
            self._send_frame(sid, header, end=False)
            self._quic.send_stream_data(sid, data, end_stream=True)
            self.transmit()
            frame = await stream.read_frame()
        finally:
            self._streams.pop(sid, None)
        answer = protocol.parse_upload_response(frame)
        config.log(
            "debug",
            f"yukleme {job_id} -> {answer.get('key', '?')} "
            f"({answer.get('size', '?')} bayt)",
        )
        return answer

    def _send_frame(self, sid: int, obj: Any, end: bool) -> None:
        self._quic.send_stream_data(sid, protocol.encode_frame(obj), end_stream=end)
        self.transmit()

    async def _serve_request(self, sid: int, stream: protocol.FrameStream) -> None:
        request_id = ""
        school = ""
        try:
            frame = await stream.read_frame()
            if isinstance(frame, dict):
                if isinstance(frame.get("id"), str):
                    request_id = frame["id"]
                if isinstance(frame.get("school"), str):
                    school = frame["school"]
            request_id, school, capability, payload, timeout = protocol.parse_request(frame)
        except EOFError as exc:
            config.log("warn", f"istek {sid} okunamadi: {exc}")
            self._streams.pop(sid, None)
            return
        except (CapabilityError, ValueError) as exc:
            code = getattr(exc, "code", "bad_request")
            config.log("warn", f"istek {sid} ayristirilamadi: {exc}")
            self._finish(
                sid, request_id, protocol.err_response(request_id, school, code, str(exc))
            )
            return

        if self._inflight >= self._settings.max_concurrent:
            response = protocol.err_response(
                request_id, school, "busy", "worker es zamanli istek sinirinda"
            )
            self._finish(sid, request_id, response)
            return

        self._inflight += 1
        try:
            config.log("debug", f"istek {request_id} yetenek={capability} okul={school}")
            result = await asyncio.wait_for(
                self._handle(capability, school, payload), timeout=timeout
            )
            response = protocol.ok_response(request_id, school, result)
        except CapabilityError as exc:
            response = protocol.err_response(request_id, school, exc.code, str(exc))
        except asyncio.TimeoutError:
            response = protocol.err_response(
                request_id, school, "timed_out", "istek sure icinde tamamlanamadi"
            )
        except Exception as exc:
            config.log("error", f"istek {request_id} islenemedi: {exc}")
            response = protocol.err_response(
                request_id, school, "internal", "beklenmeyen hata"
            )
        finally:
            self._inflight -= 1

        self._finish(sid, request_id, response)

    def _finish(self, sid: int, request_id: str, response: dict[str, Any]) -> None:
        try:
            self._send_frame(sid, response, end=True)
        except CapabilityError as exc:
            config.log("warn", f"cevap {request_id} cerceve sinirini asti: {exc}")
            try:
                self._send_frame(
                    sid,
                    protocol.err_response(
                        request_id,
                        str(response.get("school", "")),
                        "frame_too_large",
                        str(exc),
                    ),
                    end=True,
                )
            except Exception as inner:
                config.log("error", f"cevap {request_id} yazilamadi: {inner}")
        except Exception as exc:
            config.log("error", f"cevap {request_id} yazilamadi: {exc}")
        finally:
            self._streams.pop(sid, None)

    async def _handle(
        self, capability: str, school: str, payload: Any
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, capabilities.dispatch, capability, school, payload
        )

    async def keepalive(self) -> None:
        while True:
            await asyncio.sleep(protocol.KEEPALIVE_SECS)
            self._ping_uid += 1
            try:
                self._quic.send_ping(self._ping_uid)
                self.transmit()
            except Exception:
                return


def _leaf_der_from_pem(pem: str) -> bytes:
    begin = "-----BEGIN CERTIFICATE-----"
    end = "-----END CERTIFICATE-----"
    start = pem.find(begin)
    stop = pem.find(end, start + 1) if start >= 0 else -1
    if start < 0 or stop < 0:
        raise RuntimeError("sertifika PEM govdesi bulunamadi")
    body = pem[start + len(begin) : stop]
    try:
        return base64.b64decode("".join(body.split()))
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"sertifika PEM govdesi base64 degil: {exc}") from exc


def fetch_certificate(backend_url: str, expected_fingerprint: str = "") -> str:
    url = f"{backend_url}{protocol.CERTIFICATE_PATH}"
    with urllib.request.urlopen(url, timeout=CERT_FETCH_TIMEOUT_SECS) as resp:
        data = json.loads(resp.read())
    pem = data.get("certificate_pem")
    if not pem:
        raise RuntimeError(f"{url} beklenen certificate_pem alanini dondurmedi")
    computed = hashlib.sha256(_leaf_der_from_pem(pem)).hexdigest()
    reported = str(data.get("fingerprint_sha256", "")).strip().lower().replace(":", "")
    if reported and reported != computed:
        raise RuntimeError(
            "sertifika tutarsiz: bildirilen parmak izi PEM'den hesaplananla uyusmuyor "
            f"(bildirilen {reported[:12]}, hesaplanan {computed[:12]})"
        )
    if expected_fingerprint:
        if computed != expected_fingerprint:
            raise RuntimeError(
                "sertifika parmak izi PINLENEN degerle uyusmuyor; baglanilmiyor "
                f"(beklenen {expected_fingerprint[:12]}, gelen {computed[:12]})"
            )
        config.log("info", f"sertifika alindi ve PINLENDI (fingerprint {computed[:12]})")
    else:
        config.log(
            "warn",
            f"sertifika alindi (fingerprint {computed[:12]}) -- AI_TLS_FINGERPRINT "
            "tanimsiz, TOFU ile guveniliyor; uretimde parmak izini pinleyin",
        )
    return pem


def build_quic_configuration(settings: config.Config, cert_pem: str) -> QuicConfiguration:
    quic_config = QuicConfiguration(is_client=True, alpn_protocols=[protocol.PROTOCOL])
    quic_config.server_name = settings.server_name
    quic_config.verify_mode = ssl.CERT_REQUIRED
    quic_config.load_verify_locations(cadata=cert_pem.encode("utf-8"))
    quic_config.idle_timeout = protocol.IDLE_TIMEOUT_SECS
    return quic_config


async def run_once(settings: config.Config) -> None:
    cert_pem = fetch_certificate(settings.backend_url, settings.tls_fingerprint)
    quic_config = build_quic_configuration(settings, cert_pem)
    config.log(
        "info",
        f"{settings.host}:{settings.port} adresine baglaniliyor (ALPN {protocol.PROTOCOL})",
    )
    create = partial(BridgeProtocol, settings=settings)
    async with connect(
        settings.host,
        settings.port,
        configuration=quic_config,
        create_protocol=create,
    ) as connection:
        assert isinstance(connection, BridgeProtocol)
        await connection.wait_connected()
        await connection.register()
        keepalive_task = asyncio.ensure_future(connection.keepalive())
        try:
            await connection.wait_closed()
        finally:
            keepalive_task.cancel()
            backend.clear_client()
    config.log("info", "baglanti kapandi")


def next_backoff(current: float, settings: config.Config) -> float:
    return min(current * 2.0, settings.reconnect_max_secs)


async def run_forever(settings: config.Config) -> int:
    config.log("info", f"podcast koprusu basliyor: {settings.summary()}")
    jobs.write_status(settings.job_root, False)
    backoff = settings.reconnect_secs
    while True:
        try:
            await run_once(settings)
            backoff = settings.reconnect_secs
        except asyncio.TimeoutError:
            config.log("warn", "el sikisma zaman asimina ugradi")
            backoff = next_backoff(backoff, settings)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            config.log("warn", f"backend'e ulasilamadi: {exc}")
            backoff = next_backoff(backoff, settings)
        except protocol.HandshakeRejected as exc:
            if exc.permanent:
                config.log(
                    "error",
                    f"{exc} -- yapilandirma degismeden duzelmez; yine de cikmiyoruz, "
                    "geri cekilerek yeniden denenecek",
                )
                backoff = settings.reconnect_max_secs
            else:
                config.log("error", str(exc))
                backoff = next_backoff(backoff, settings)
        except Exception as exc:
            config.log("error", f"baglanti hatasi: {exc}")
            backoff = next_backoff(backoff, settings)
        jobs.write_status(settings.job_root, False)
        delay = backoff + random.uniform(0.0, min(backoff, 5.0))
        config.log("info", f"{delay:.1f}s sonra yeniden denenecek")
        await asyncio.sleep(delay)


def build_store(
    settings: config.Config,
    runner: Callable[[jobs.JobContext], None] | None = None,
    stages: tuple[str, ...] | None = None,
    job_secs: float | None = None,
) -> jobs.JobStore:
    try:
        store = jobs.JobStore(
            root=settings.job_root,
            workers=settings.workers,
            max_jobs=settings.max_jobs,
            stage_secs=settings.stage_secs,
            runner=runner,
            stages=stages,
            job_secs=job_secs,
            retention_days=settings.retention_days,
            output_root=settings.output_root,
            reporter=backend,
        )
    except OSError as exc:
        config.log("error", f"PODCAST_JOB_ROOT kullanilamiyor ({settings.job_root}): {exc}")
        sys.exit(2)
    if store.swept:
        config.log(
            "warn",
            f"acilis supurmesi: {store.swept} kosan is "
            f"'{jobs.INTERRUPTED_CODE}' olarak isaretlendi",
        )
    requeued = store.start()
    if requeued:
        config.log("info", f"acilista {requeued} kuyruktaki is yeniden alindi")
    return store


def select_runner(
    settings: config.Config,
) -> tuple[Callable[[jobs.JobContext], None] | None, bool]:
    if settings.mode != pipeline.MODE_REAL:
        config.log("warn", pipeline.FAKE_WARNING)
        return None, settings.has_llm_key
    if settings.engine == pipeline.ENGINE_API:
        runner, probe = api_engine.build_runner(settings)
        config.log(
            "info",
            f"API motoru bagli: llm={settings.llm_model} "
            f"({settings.llm_base_url}) bulut_tts={settings.elevenlabs_model} "
            f"cikti={settings.output_root}",
        )
        if not probe["llm_ready"]:
            config.log(
                "error",
                f"LLM anahtari yok ({probe['llm_reason']}): "
                "'tek_ogretici' ve 'ogrenci_hoca' reddedilir, 'duz_okuma' kosar",
            )
        if not probe["tts_ready"]:
            config.log(
                "error",
                f"Bulut TTS hazir degil ({probe['tts_reason']}): "
                "her is tts asamasinda net bir hatayla dusecek",
            )
        return runner, probe["llm_ready"]
    try:
        runner, probe = pipeline.build_runner(settings)
    except pipeline.PipelineUnavailable as exc:
        config.log("error", f"PODCAST_MODE=real ama gercek hat yuklenemedi: {exc}")
        sys.exit(2)
    config.log(
        "info",
        f"GERCEK hat bagli: kaynak={settings.pipeline_path} "
        f"cikti={settings.output_root} tts={settings.tts_engine}",
    )
    llm_ready = bool(probe["llm_ready"])
    if llm_ready != settings.has_llm_key:
        config.log(
            "warn",
            "LLM hazirligi ortam degiskeninden FARKLI: hat '%s' diyor "
            "(gerekce: %s). Hattin karari esas alinir."
            % ("HAZIR" if llm_ready else "YOK", probe["llm_reason"][:120]),
        )
    return runner, llm_ready


def main() -> int:
    settings = config.load(require_token=True)
    runner, llm_ready = select_runner(settings)
    store = build_store(
        settings,
        runner=runner,
        stages=pipeline.stages_for(settings),
        job_secs=pipeline.job_secs_for(settings),
    )
    capabilities.configure(store, llm_ready)
    config.log("info", capabilities.format_summary(llm_ready))
    try:
        return asyncio.run(run_forever(settings))
    except KeyboardInterrupt:
        config.log("info", "durduruldu")
        return 0
    finally:
        store.shutdown()


if __name__ == "__main__":
    sys.exit(main())
