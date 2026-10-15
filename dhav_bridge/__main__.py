"""CLI: ``python -m dhav_bridge``.

Run from the OpenAvatarChat directory with ``.venv/bin/python`` (never
``uv run`` — its implicit sync uninstalls the handler dependencies).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from .server import BridgeServer, make_ssl_context, serve
from .video import parse_size

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dhav_bridge",
        description="Serve FlashHead as a DHAV v1 renderer for an external voice-agent gateway.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=18910, help="bind port (default 18910)")
    parser.add_argument(
        "--renderer-url",
        default=os.environ.get("DHAV_RENDERER_UPSTREAM", "wss://127.0.0.1:8284"),
        help="audio-passthrough service base URL (default wss://127.0.0.1:8284)",
    )
    parser.add_argument(
        "--renderer-ca",
        default="ssl_certs/localhost.crt",
        help="CA that signed the passthrough service's certificate",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("DHAV_AUTH_TOKEN"),
        help="bearer token the gateway must send on POST/DELETE (env DHAV_AUTH_TOKEN). "
        "Required unless --host is loopback.",
    )
    parser.add_argument(
        "--public-base-url",
        default=os.environ.get("DHAV_PUBLIC_BASE_URL"),
        help="base URL returned in the session description, e.g. http://203.0.113.7:18910. "
        "Defaults to the Host header of the create request.",
    )
    parser.add_argument(
        "--video-size",
        default="256",
        help='I420 output size, "N" or "WxH", even and <=512 (default 256 => 256x256)',
    )
    parser.add_argument("--fps", type=int, default=25, help="advertised frame rate (default 25)")
    parser.add_argument(
        "--persona",
        action="append",
        default=None,
        help="accepted personaId; repeatable (default flashhead-default)",
    )
    parser.add_argument("--max-sessions", type=int, default=1, help="concurrent avatar sessions (default 1)")
    parser.add_argument(
        "--attach-timeout",
        type=float,
        default=60.0,
        help="seconds a created session may go unused before its slot is reclaimed (default 60)",
    )
    parser.add_argument("--cert", help="serve the bridge over TLS with this certificate")
    parser.add_argument("--key", help="private key for --cert")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("dhav_bridge")

    if bool(args.cert) != bool(args.key):
        log.error("--cert and --key must be given together")
        return 2
    if args.host not in _LOOPBACK and not args.auth_token:
        log.error(
            "refusing to bind %s without --auth-token: the DHAV endpoints would be "
            "open to anyone who can reach this port",
            args.host,
        )
        return 2
    try:
        width, height = parse_size(args.video_size)
    except ValueError as error:
        log.error("%s", error)
        return 2
    if args.fps != 25:
        log.warning(
            "the passthrough renderer emits 25 fps; --fps %d only changes what is advertised",
            args.fps,
        )
    if not os.path.exists(args.renderer_ca):
        log.error("renderer CA file not found: %s (run from the OpenAvatarChat directory)", args.renderer_ca)
        return 2

    personas = {name: {"avatarAssetId": None} for name in (args.persona or ["flashhead-default"])}
    bridge = BridgeServer(
        renderer_base_url=args.renderer_url,
        ca_file=args.renderer_ca,
        auth_token=args.auth_token,
        personas=personas,
        width=width,
        height=height,
        fps=args.fps,
        max_sessions=args.max_sessions,
        attach_timeout_s=args.attach_timeout,
        log=lambda name, fields: log.info("%s %s", name, json.dumps(fields, ensure_ascii=False, default=str)),
    )
    ssl_context = make_ssl_context(args.cert, args.key) if args.cert else None
    scheme = "https" if ssl_context else "http"
    log.info(
        "DHAV renderer on %s://%s:%d  video=%dx%d@%d  upstream=%s  auth=%s",
        scheme, args.host, args.port, width, height, args.fps,
        args.renderer_url, "on" if args.auth_token else "off",
    )

    async def run() -> None:
        runner = await serve(
            bridge,
            host=args.host,
            port=args.port,
            public_base_url=args.public_base_url,
            ssl_context=ssl_context,
        )
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
