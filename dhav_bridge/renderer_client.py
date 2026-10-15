"""WebSocket client for the OpenAvatarChat audio-passthrough renderer.

The bridge does not talk to FlashHead directly; it drives the verified
``config/chat_with_flashhead_passthrough.yaml`` service (port 8284), whose
wire contract is ``src/handlers/client/ws_video_client/PROTOCOL.md`` and whose
behaviour is summarised in ``README.md`` here, "The renderer underneath".
That service gives us exactly what DHAV needs: speaking audio in, 25 fps JPEG
video plus a frame-aligned audio track out.

Two facts about that service shape this module:

* Utterance boundaries are **uplink packet gaps** (``gap_seconds``, 0.4 s), not
  content silence.  So one DHAV turn is forwarded as one uninterrupted burst of
  packets, and stopping the packets is how the turn ends.
* The downlink carries two audio streams.  The passthrough echo stream mirrors
  whatever packet size we uploaded; the renderer's frame-aligned stream is
  always exactly ``FRAME_BYTES`` per message.  Keeping the uplink packet size
  away from ``FRAME_BYTES`` is what makes the two distinguishable.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import ssl
import struct
import time
import uuid
from typing import Awaitable, Callable

import numpy as np
import soxr
import websockets

logger = logging.getLogger("dhav_bridge.renderer")

# 40 ms @ 24 kHz == 25 fps: the renderer's frame-aligned audio block size.
FRAME_SAMPLES = 960
FRAME_BYTES = FRAME_SAMPLES * 2

# The uplink is 16 kHz, *not* the 24 kHz we declare in InitializeAvatarSession:
# ws_client_handler.py:82 hardcodes the MIC_AUDIO entry at 16000 and ignores the
# declared rate, and the passthrough handler resamples to match that entry.
# Sending 24 kHz therefore stretches every utterance to 1.5x and drops its pitch
# by the same factor. The downlink really is 24 kHz (960 samples per 40 ms frame).
UPLINK_SAMPLE_RATE = 16000

# JVID frame header: magic[4] version codec width height frame_index batch_size
# timestamp_ms data_size reserved[2] -> the JPEG starts at byte 32.
_JVID_HEADER = struct.Struct("<4sBBHHIIQI")
_JVID_MAGIC = b"JVID"
_JVID_PAYLOAD_OFFSET = 32

_UPLINK_BACKPRESSURE_BYTES = 4 * 1024 * 1024

# Utterance boundaries reported by the service's ``ChatSignal`` messages, the
# only *structural* end markers on the wire. They are needed because the
# frame-aligned audio track never goes quiet: FlashHead's frame collector pairs
# every idle frame with a block of exact zeros for the whole session, so "no
# audio for a while" cannot mean "the utterance ended".
#
# ``input_closed``  AudioPassthrough's avatar_audio stream ended, i.e. the 0.4 s
#                   uplink gap fired. Everything the client sent is now inside
#                   the renderer and no further speech can follow, so any
#                   silence from here on is the utterance's tail.
# ``avatar_done``   The avatar handler's client_playback stream ended, i.e. the
#                   collector emitted the utterance's last frame. Authoritative
#                   but ~2 s late: it waits for the end-of-speech marker to
#                   travel through the chunked inference pipeline.
#
# An interrupted utterance is cancelled rather than ended, so ``stream_cancel``
# is deliberately not mapped; barge-in has its own path.
UTTERANCE_INPUT_CLOSED = "input_closed"
UTTERANCE_AVATAR_DONE = "avatar_done"
# ``stream_producer`` is the handler's name in the service config, so this is
# the ``AudioPassthrough`` key of config/chat_with_flashhead_passthrough.yaml.
_PASSTHROUGH_PRODUCER = "AudioPassthrough"


class RendererUnavailable(RuntimeError):
    """The passthrough service is not connected."""


class PassthroughRenderer:
    """One resilient session against the passthrough service.

    ``on_audio``/``on_frame`` are awaited from the receive loop, so a slow
    consumer applies real backpressure to the renderer instead of growing an
    unbounded buffer here.
    """

    def __init__(
        self,
        *,
        base_url: str,
        ca_file: str | None,
        session_prefix: str,
        sample_rate: int = 24000,
        uplink_sample_rate: int = UPLINK_SAMPLE_RATE,
        uplink_chunk_samples: int | None = None,
        on_audio: Callable[[bytes], Awaitable[None]] | None = None,
        on_frame: Callable[[bytes], Awaitable[None]] | None = None,
        on_state: Callable[[bool], Awaitable[None]] | None = None,
        on_utterance: Callable[[str], Awaitable[None]] | None = None,
        is_busy: Callable[[], bool] | None = None,
        heartbeat_interval_s: float = 10.0,
        max_session_age_s: float = 800.0,
        reconnect_delay_s: float = 1.0,
    ) -> None:
        uplink_chunk_samples = uplink_chunk_samples or uplink_sample_rate // 10
        if uplink_chunk_samples * 2 == FRAME_BYTES:
            raise ValueError(
                f"an uplink packet must not be {FRAME_BYTES} bytes; the renderer's "
                "frame-aligned downlink stream is identified by exactly that size"
            )
        self._base_url = base_url.rstrip("/")
        self._ssl = self._build_ssl_context(ca_file) if base_url.startswith("wss://") else None
        self._session_prefix = session_prefix
        self._sample_rate = sample_rate
        self._uplink_sample_rate = uplink_sample_rate
        self._chunk_bytes = uplink_chunk_samples * 2
        self._on_audio = on_audio
        self._on_frame = on_frame
        self._on_state = on_state
        self._on_utterance = on_utterance
        self._is_busy = is_busy or (lambda: False)
        self._heartbeat_interval_s = heartbeat_interval_s
        self._max_session_age_s = max_session_age_s
        self._reconnect_delay_s = reconnect_delay_s

        self._ws: websockets.ClientConnection | None = None
        self._task: asyncio.Task | None = None
        self._closing = False
        self._connected = False
        self._connected_at = 0.0
        self._pending = bytearray()
        self._send_lock = asyncio.Lock()
        self._resampler = self._new_resampler()
        self.attempt = 0
        self.renderer_session_id: str | None = None

    def _new_resampler(self) -> soxr.ResampleStream | None:
        if self._uplink_sample_rate == self._sample_rate:
            return None
        # Streaming (stateful) so chunk boundaries stay click-free.
        return soxr.ResampleStream(self._sample_rate, self._uplink_sample_rate, 1, dtype="int16")

    def reset_uplink(self) -> None:
        """Drop any un-forwarded audio and start the resampler over.

        Called at turn boundaries so one turn's tail cannot leak into the next.
        """
        self._pending.clear()
        self._resampler = self._new_resampler()

    def _resample(self, pcm: bytes, last: bool = False) -> bytes:
        if self._resampler is None:
            return pcm
        samples = np.frombuffer(pcm, dtype=np.int16) if pcm else np.zeros(0, dtype=np.int16)
        return self._resampler.resample_chunk(samples, last=last).tobytes()

    @staticmethod
    def _build_ssl_context(ca_file: str | None) -> ssl.SSLContext:
        if not ca_file:
            raise ValueError("a CA file is required for wss:// renderer URLs")
        return ssl.create_default_context(cafile=ca_file)

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name="renderer-supervisor")

    async def close(self) -> None:
        self._closing = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            await ws.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._connected:
            self._connected = False
            await self._notify_state(False)

    async def _supervise(self) -> None:
        while not self._closing:
            self.attempt += 1
            sid = f"{self._session_prefix}-{uuid.uuid4().hex[:8]}"
            url = f"{self._base_url}/ws/session/{sid}"
            try:
                async with websockets.connect(
                    url, ssl=self._ssl, max_size=None, ping_interval=20, open_timeout=15
                ) as ws:
                    self._ws = ws
                    self.renderer_session_id = sid
                    await self._initialize(ws)
                    self._connected = True
                    self._connected_at = time.monotonic()
                    logger.info("renderer connected session=%s attempt=%d", sid, self.attempt)
                    await self._notify_state(True)
                    await self._run_session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("renderer connection failed (%s): %s", type(exc).__name__, exc)
            finally:
                self._ws = None
                was_connected = self._connected
                self._connected = False
                if was_connected:
                    await self._notify_state(False)
            if self._closing:
                return
            await asyncio.sleep(self._reconnect_delay_s)

    async def _notify_state(self, connected: bool) -> None:
        if self._on_state is None:
            return
        try:
            await self._on_state(connected)
        except Exception:
            logger.exception("on_state callback failed")

    async def _initialize(self, ws) -> None:
        await ws.send(
            json.dumps(
                {
                    "header": {"name": "InitializeAvatarSession", "request_id": "init-1"},
                    "payload": {
                        "audio": {
                            "format": "PCM",
                            "sample_rate": self._uplink_sample_rate,
                            "channels": 1,
                        }
                    },
                }
            )
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            message = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            if isinstance(message, (bytes, bytearray)):
                continue
            payload = json.loads(message)
            if payload.get("header", {}).get("name") == "AvatarSessionInitialized":
                return
        raise RendererUnavailable("timed out waiting for AvatarSessionInitialized")

    async def _run_session(self, ws) -> None:
        """Run the receive and keepalive loops; whichever ends first ends both."""
        tasks = [
            asyncio.create_task(self._receive(ws), name="renderer-recv"),
            asyncio.create_task(self._maintain(ws), name="renderer-keepalive"),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _maintain(self, ws) -> None:
        """Keepalives plus proactive recycling ahead of the service's 900 s TTL."""
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            age = time.monotonic() - self._connected_at
            if age > self._max_session_age_s and not self._is_busy():
                logger.info("recycling renderer session after %.0fs (TTL guard)", age)
                await ws.close()
                return
            await self._send_json(
                ws, {"header": {"name": "TriggerHeartbeat", "request_id": f"hb-{uuid.uuid4().hex[:6]}"}}
            )

    async def _receive(self, ws) -> None:
        while True:
            message = await ws.recv()
            if isinstance(message, (bytes, bytearray)):
                await self._on_binary(bytes(message))
                continue
            await self._on_text(message)

    async def _on_binary(self, raw: bytes) -> None:
        if raw[:4] != _JVID_MAGIC or len(raw) < _JVID_HEADER.size or self._on_frame is None:
            return
        (_magic, _version, _codec, _w, _h, _index, _batch, _ts, size) = _JVID_HEADER.unpack(
            raw[: _JVID_HEADER.size]
        )
        jpeg = raw[_JVID_PAYLOAD_OFFSET : _JVID_PAYLOAD_OFFSET + size]
        if len(jpeg) != size:
            return
        await self._on_frame(jpeg)

    async def _on_text(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return
        name = data.get("header", {}).get("name")
        payload = data.get("payload") or {}
        if name == "Error":
            logger.warning("renderer reported an error: %s", payload)
            return
        if name == "ChatSignal":
            await self._on_chat_signal(payload)
            return
        if name != "EchoAvatarAudio" or self._on_audio is None:
            return
        encoded = payload.get("data_base64")
        if not encoded:
            return
        pcm = base64.b64decode(encoded)
        # Only the renderer's frame-aligned stream is exactly one 40 ms block;
        # the passthrough echo stream mirrors our own uplink chunk size.
        if len(pcm) != FRAME_BYTES:
            return
        try:
            await self._on_audio(pcm)
        except Exception:
            logger.exception("on_audio callback failed")

    async def _on_chat_signal(self, payload: dict) -> None:
        if payload.get("type") != "stream_end" or self._on_utterance is None:
            return
        stream_type = payload.get("stream_type")
        if stream_type == "avatar_audio" and payload.get("stream_producer") == _PASSTHROUGH_PRODUCER:
            stage = UTTERANCE_INPUT_CLOSED
        elif stream_type == "client_playback":
            stage = UTTERANCE_AVATAR_DONE
        else:
            return
        try:
            await self._on_utterance(stage)
        except Exception:
            logger.exception("on_utterance callback failed")

    async def _send_json(self, ws, message: dict) -> None:
        async with self._send_lock:
            if self._ws is not ws:
                return
            while ws.transport is not None and ws.transport.get_write_buffer_size() > _UPLINK_BACKPRESSURE_BYTES:
                await asyncio.sleep(0.02)
            await ws.send(json.dumps(message))

    async def send_audio(self, pcm: bytes) -> None:
        """Queue reply PCM and forward it in fixed-size uplink packets."""
        if not self._connected or self._ws is None:
            raise RendererUnavailable("renderer not connected")
        self._pending.extend(self._resample(pcm))
        ws = self._ws
        while len(self._pending) >= self._chunk_bytes:
            chunk = bytes(self._pending[: self._chunk_bytes])
            del self._pending[: self._chunk_bytes]
            await self._send_audio_chunk(ws, chunk)

    async def flush_audio(self) -> None:
        """Forward the tail of a turn, then stop so the renderer closes it.

        A remainder of exactly ``FRAME_BYTES`` is split in two: an uplink packet
        of that size would be indistinguishable from the renderer's
        frame-aligned downlink stream.
        """
        if not self._connected or self._ws is None:
            self.reset_uplink()
            return
        ws = self._ws
        # last=True drains the resampler's filter delay, and finalizes it, so
        # the next turn starts from a fresh one.
        tail = self._resample(b"", last=True)
        self._resampler = self._new_resampler()
        remainder = bytes(self._pending) + tail
        self._pending.clear()
        if not remainder:
            return
        if len(remainder) == FRAME_BYTES:
            half = len(remainder) // 2
            await self._send_audio_chunk(ws, remainder[:half])
            await self._send_audio_chunk(ws, remainder[half:])
            return
        await self._send_audio_chunk(ws, remainder)

    async def _send_audio_chunk(self, ws, chunk: bytes) -> None:
        await self._send_json(
            ws,
            {
                "header": {"name": "SendHumanAudio", "request_id": f"a-{uuid.uuid4().hex[:8]}"},
                "payload": {"transport": "base64", "data_base64": base64.b64encode(chunk).decode()},
            },
        )

    async def interrupt(self) -> None:
        """Barge-in: ask the renderer to stop the utterance it is working on."""
        ws = self._ws
        if not self._connected or ws is None:
            return
        self.reset_uplink()
        try:
            await self._send_json(
                ws, {"header": {"name": "Interrupt", "request_id": f"i-{uuid.uuid4().hex[:6]}"}}
            )
        except Exception as exc:
            logger.warning("interrupt failed: %s", exc)
