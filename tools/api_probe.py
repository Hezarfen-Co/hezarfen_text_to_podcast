from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Gercek backend'e ApiRequest atar (tani araci, uretim yolu DEGIL)"
    )
    parser.add_argument("--path", default="/auth/me")
    parser.add_argument("--query", default="")
    parser.add_argument("--on-behalf-of", default="")
    return parser


async def run_probe(args) -> int:
    from functools import partial

    from aioquic.asyncio.client import connect

    from src import bridge, capabilities, config, jobs, protocol

    settings = config.load(require_token=True)
    store = jobs.JobStore(root=settings.job_root, workers=1, max_jobs=2)
    capabilities.configure(store, settings.has_llm_key)

    cert = bridge.fetch_certificate(settings.backend_url)
    quic_config = bridge.build_quic_configuration(settings, cert)
    create = partial(bridge.BridgeProtocol, settings=settings)
    async with connect(
        settings.host, settings.port, configuration=quic_config, create_protocol=create
    ) as connection:
        await connection.wait_connected()
        await connection.register()
        try:
            response = await connection.api_get(
                args.path, args.query or None, args.on_behalf_of or None
            )
        except protocol.ApiRefused as exc:
            print(f"[api] KOPRU REDDETTI code={exc.code} :: {exc}", flush=True)
            return 1
        print(f"[api] yol      = {args.path}", flush=True)
        print(f"[api] status   = {response.status}", flush=True)
        print(f"[api] basarili = {response.ok}", flush=True)
        print(f"[api] body     = {str(response.body)[:300]}", flush=True)
    store.shutdown()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run_probe(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
