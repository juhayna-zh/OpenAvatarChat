"""DHAV renderer service: HTTP session management + input WS + media WS.

Same protocol surface as a DHAV v1 reference renderer, so any conforming
gateway provider drives this bridge without knowing it is not the reference.
The media itself comes from FlashHead via the audio-passthrough service.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import uuid
from typing import Callable

import aiohttp
from aiohttp import web

from . import dhav
from .renderer_client import PassthroughRenderer
from .session import AvatarSession

logger = logging.getLogger("dhav_bridge.server")

_SESSION_PATH = "/v1/avatar/sessions"
_CHANNELS = ("input", "media")


class BridgeServer:
    def __init__(
        self,
        *,
        renderer_base_url: str,
        ca_file: str | None,
        auth_token: str | None = None,
        personas: dict[str, dict] | None = None,
        width: int = 256,
        height: int = 256,
        fps: int = 25,
        max_sessions: int = 1,
        attach_timeout_s: float = 60.0,
        sample_rate: int = 24000,
        uplink_chunk_samples: int = 2400,
        renderer_max_session_age_s: float = 800.0,
        log: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._renderer_base_url = renderer_base_url.rstrip("/")
        self._ca_file = ca_file
        self._auth_token = auth_token
        self._personas = personas or {"flashhead-default": {"avatarAssetId": None}}
        self._width = width
        self._height = height
        self._fps = fps
        self._max_sessions = max_sessions
        self._attach_timeout = attach_timeout_s
        self._sample_rate = sample_rate
        self._uplink_chunk_samples = uplink_chunk_samples
        self._renderer_max_age = renderer_max_session_age_s
        self._log = log or (lambda name, fields: logger.info("%s %s", name, fields))
        self.sessions: dict[str, AvatarSession] = {}
        self.app = self._build_app()

    # --------------------------------------------------------------------- app

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health/live", self._health_live)
        app.router.add_get("/health/ready", self._health_ready)
        app.router.add_post(_SESSION_PATH, self._create_session)
        app.router.add_delete(_SESSION_PATH + "/{session_id}", self._delete_session)
        app.router.add_get(_SESSION_PATH + "/{session_id}/{channel}", self._channel)
        app.on_cleanup.append(self._cleanup)
        return app

    @staticmethod
    def _json(status: int, body: dict) -> web.Response:
        return web.json_response(body, status=status)

    async def _health_live(self, request: web.Request) -> web.Response:
        return self._json(200, {"live": True})

    async def _health_ready(self, request: web.Request) -> web.Response:
        """Unlike the renderer service's own /readiness, this means "can render
        now": it probes the passthrough service rather than trusting handler
        registration."""
        ready, detail = await self._probe_renderer()
        return self._json(200, {"ready": ready, "fault": None, "renderer": detail})

    async def _probe_renderer(self) -> tuple[bool, str]:
        http_base = self._renderer_base_url.replace("wss://", "https://").replace("ws://", "http://")
        connector = None
        if http_base.startswith("https://"):
            context = ssl.create_default_context(cafile=self._ca_file) if self._ca_file else ssl.create_default_context()
            connector = aiohttp.TCPConnector(ssl=context)
        try:
            async with aiohttp.ClientSession(connector=connector) as client:
                async with client.get(http_base.rstrip("/") + "/readiness", timeout=aiohttp.ClientTimeout(total=5)) as response:
                    return response.status == 200, f"http {response.status}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def _authorized(self, request: web.Request) -> bool:
        return not self._auth_token or request.headers.get("Authorization") == f"Bearer {self._auth_token}"

    def _public_base_url(self, request: web.Request) -> str:
        configured = request.app.get("public_base_url")
        if configured:
            return configured.rstrip("/")
        return f"{request.scheme}://{request.host}"

    async def _create_session(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._json(401, {"error": "unauthorized"})
        try:
            body = await request.json()
        except Exception:
            return self._json(400, {"error": "malformed body"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "malformed body"})
        persona_id = body.get("personaId")
        if persona_id not in self._personas:
            return self._json(400, {"error": "unknown persona", "personas": sorted(self._personas)})
        if body.get("protocolVersion") != dhav.VERSION:
            return self._json(400, {"error": "unsupported protocolVersion"})
        live = {sid: s for sid, s in self.sessions.items() if not s.destroyed}
        if len(live) >= self._max_sessions:
            return self._json(409, {"error": "capacity_exhausted"})

        session_id = str(uuid.uuid4())
        session = AvatarSession(
            session_id=session_id,
            persona_id=persona_id,
            avatar_asset_id=body.get("avatarAssetId"),
            renderer_factory=lambda owner: PassthroughRenderer(
                base_url=self._renderer_base_url,
                ca_file=self._ca_file,
                session_prefix=f"dhav-{session_id[:8]}",
                sample_rate=self._sample_rate,
                uplink_chunk_samples=self._uplink_chunk_samples,
                max_session_age_s=self._renderer_max_age,
                on_audio=owner.on_renderer_audio,
                on_frame=owner.on_renderer_frame,
                on_state=owner.on_renderer_state,
                on_utterance=owner.on_renderer_utterance,
                is_busy=lambda: owner.busy,
            ),
            width=self._width,
            height=self._height,
            fps=self._fps,
            log=self._log,
        )
        self.sessions[session_id] = session
        self._log(
            "session.created",
            {"id": session_id, "personaId": persona_id, "video": f"{self._width}x{self._height}@{self._fps}"},
        )
        asyncio.create_task(self._reap_if_unattached(session_id), name=f"attach-{session_id[:8]}")
        return self._json(201, session.public_description(self._public_base_url(request)))

    async def _reap_if_unattached(self, session_id: str) -> None:
        """Reclaim the slot of a session whose client never connected.

        Nothing else can: ``channel_lost`` only fires for a channel that was
        attached, so a create followed by a vanished client used to hold the
        only session slot forever and every later create got 409.
        """
        await asyncio.sleep(self._attach_timeout)
        session = self.sessions.get(session_id)
        if session is None or session.destroyed or session.channels_attached:
            return
        self.sessions.pop(session_id, None)
        self._log("session.reaped", {"id": session_id, "reason": "attach_timeout", "afterS": self._attach_timeout})
        await session.destroy("attach_timeout")

    async def _delete_session(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._json(401, {"error": "unauthorized"})
        session = self.sessions.pop(request.match_info["session_id"], None)
        if session is not None:
            await session.destroy("session_deleted")
        return web.Response(status=204)

    async def _channel(self, request: web.Request) -> web.StreamResponse:
        session_id = request.match_info["session_id"]
        channel = request.match_info["channel"]
        session = self.sessions.get(session_id)
        if channel not in _CHANNELS or session is None or session.destroyed:
            return self._json(404, {"error": "not_found"})
        token = session.input_token if channel == "input" else session.media_token
        if request.headers.get("Authorization") != f"Bearer {token}":
            return self._json(401, {"error": "unauthorized"})

        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        await session.attach(channel, ws)
        try:
            async for message in ws:
                if message.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING, web.WSMsgType.ERROR):
                    break
                if channel != "input":
                    continue  # the media channel is write-only for the renderer
                await session.handle_input(message)
        finally:
            await session.channel_lost(channel)
        return ws

    async def _cleanup(self, app: web.Application) -> None:
        for session in list(self.sessions.values()):
            await session.destroy("server_close")
        self.sessions.clear()


def build_app(bridge: BridgeServer, public_base_url: str | None = None) -> web.Application:
    bridge.app["public_base_url"] = public_base_url
    return bridge.app


def make_ssl_context(cert_file: str, cert_key: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, cert_key)
    return context


async def serve(
    bridge: BridgeServer,
    *,
    host: str,
    port: int,
    public_base_url: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> web.AppRunner:
    build_app(bridge, public_base_url)
    runner = web.AppRunner(bridge.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port, ssl_context=ssl_context)
    await site.start()
    return runner
