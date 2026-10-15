"""One DHAV avatar session: turn state machine plus A/V pairing.

The mapping this module implements:

===========================  ===================================================
DHAV (gateway side)          Passthrough renderer (port 8284)
===========================  ===================================================
``start(turn)``              nothing yet — wait for reply audio
``INPUT_PCM`` records        ``SendHumanAudio`` packets, forwarded immediately
``finish(turn)``             stop sending packets; the renderer's 0.4 s uplink
                             gap closes the utterance
paired A/V units             frame-aligned audio block + the JVID frame it leads
``media_end``/``completed``  the renderer's utterance-end ``ChatSignal``
``cancel(turn)``             ``Interrupt``
===========================  ===================================================

Three measured properties of the renderer drive the design (see
``README.md`` in this directory, "Measured numbers"):

1. Video *and* the frame-aligned audio track both flow continuously at 25 fps
   for the whole session: FlashHead's frame collector pairs every idle frame
   with a block of exact zeros, so the downlink never goes quiet and a quiet
   gap cannot mean "utterance over".  Idle output is dropped rather than paired
   -- the leading part by the silence skip, the tail once the renderer reports
   the utterance closed -- and turn end comes from the renderer's structural
   ``ChatSignal`` markers (see ``renderer_client``).
2. The utterance's audio starts with roughly a second of *exactly zero*
   blocks — the renderer's own buffering delay.  Dropping them (and their idle
   frames) removes that second from the perceived latency and makes
   ``turn.first_media`` arrive well inside the gateway's timeout.
3. Within an utterance the k-th audio block belongs to the k-th frame, and the
   audio arrives about 2 ms ahead of its frame.  A FIFO pair queue reproduces
   that without any timestamp guessing.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

import numpy as np
from aiohttp import WSMsgType, web

from . import dhav
from .renderer_client import (
    FRAME_SAMPLES,
    UTTERANCE_AVATAR_DONE,
    UTTERANCE_INPUT_CLOSED,
    PassthroughRenderer,
    RendererUnavailable,
)
from .video import jpeg_to_i420

logger = logging.getLogger("dhav_bridge.session")

SAMPLE_RATE = 24000
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_DURATION_US = round(FRAME_SAMPLES * 1_000_000 / SAMPLE_RATE)

# The renderer needs ~1 s to produce its first audio block; cap the leading
# silence skip so an all-silent reply still produces media and first_media.
MAX_LEADING_SILENCE_BLOCKS = 50
# Normalized RMS below which a 40 ms block counts as silence. Sits ~4x above the
# renderer's idle dither and ~4x below the quietest speech it produces.
SILENCE_RMS = 0.005
# Runaway valves. Audio is only dropped after ~10 s of unpublishable output.
MAX_AUDIO_QUEUE = 250
MAX_FRAME_QUEUE = 64
# Turn end. The renderer's structural utterance markers decide *whether* a turn
# is over (see renderer_client); these graces only say how long a gap we accept
# as the end of the media. END_GRACE_S covers both a run of silence after the
# renderer closed the utterance and, as a fallback for a renderer that emits no
# markers at all, a downlink that stops delivering audio entirely.
END_GRACE_S = 1.2
# ... but only if it produced something, otherwise a very short reply would be
# ended before the renderer's first block ever arrives.
NO_OUTPUT_GRACE_S = 6.0
TURN_TIMEOUT_S = 180.0
# After a cancel, ignore renderer output until the interrupted utterance has
# drained, so its tail cannot be republished under the next turn. "Drained"
# means no *audible* block for FLUSH_QUIET_S: the renderer pairs every idle
# frame with a silent block at 25 fps for the whole session, so counting silence
# would keep the window open forever. FLUSH_MAX_S caps it in case a renderer
# ignores Interrupt and keeps talking.
FLUSH_QUIET_S = 0.4
FLUSH_MAX_S = 3.0
ADMISSION_SECONDS = 120


def _is_silence(pcm: bytes) -> bool:
    # The renderer's idle output is low-level dither (measured rms 0.0013,
    # absmax ~420 on int16), never exact zeros, so an all-zero test would
    # publish ~1 s of blank lead-in on every turn. Speech measures >= 0.02.
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return float(np.sqrt((samples * samples).mean())) < SILENCE_RMS


@dataclasses.dataclass
class Turn:
    ref: dict
    expected_sequence: int = 0
    admitted_samples: int = 0
    output_samples: int = 0
    published_units: int = 0
    first_media_sent: bool = False
    finishing: bool = False
    # Renderer-reported utterance markers; see renderer_client for what they mean.
    input_closed: bool = False
    avatar_done: bool = False
    cancel_requested: bool = False
    paired: bool = False
    skipped_silence: int = 0
    seen: dict = dataclasses.field(default_factory=dict)
    audio_q: collections.deque = dataclasses.field(default_factory=collections.deque)
    frame_q: collections.deque = dataclasses.field(default_factory=collections.deque)
    started_at: float = dataclasses.field(default_factory=time.monotonic)
    finished_at: float = 0.0
    last_audio_at: float = 0.0
    last_voice_at: float = 0.0
    input_closed_at: float = 0.0


class AvatarSession:
    """DHAV session state. Owns one passthrough renderer connection."""

    def __init__(
        self,
        *,
        session_id: str,
        persona_id: str,
        avatar_asset_id: str | None,
        renderer_factory: Callable[["AvatarSession"], PassthroughRenderer],
        width: int,
        height: int,
        fps: int,
        log: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.id = session_id
        self.persona_id = persona_id
        self.avatar_asset_id = avatar_asset_id
        self.input_token = uuid.uuid4().hex
        self.media_token = uuid.uuid4().hex
        self.width = width
        self.height = height
        self.fps = fps
        self._log = log or (lambda name, fields: None)

        self._input_ws: web.WebSocketResponse | None = None
        self._media_ws: web.WebSocketResponse | None = None
        self._input_lock = asyncio.Lock()
        self._media_lock = asyncio.Lock()
        self._ready_sent = False
        self._destroyed = False
        self._turn: Turn | None = None
        self._turn_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._flushing_until = 0.0
        self._flush_deadline = 0.0
        self._admission_samples = SAMPLE_RATE * ADMISSION_SECONDS

        self.renderer = renderer_factory(self)

    # ---------------------------------------------------------------- lifecycle

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    @property
    def busy(self) -> bool:
        return self._turn is not None

    @property
    def channels_attached(self) -> bool:
        """Both WS channels are up, i.e. the client actually showed up.

        Explicit ``None`` checks: aiohttp's WebSocketResponse defines __len__,
        so an attached socket is falsy.
        """
        return self._input_ws is not None and self._media_ws is not None

    def public_description(self, base_url: str) -> dict:
        base = base_url.rstrip("/")
        scheme, separator, rest = base.partition("://")
        # The provider rejects a channel URL without a ws/wss scheme, so a
        # bare host:port still has to get one.
        if separator == "://":
            base = f"{'wss' if scheme == 'https' else 'ws'}://{rest}"
        else:
            base = f"ws://{base}"
        path = f"{base}/v1/avatar/sessions/{self.id}"
        return {
            "avatarSessionId": self.id,
            "protocolVersion": dhav.VERSION,
            "personaId": self.persona_id,
            "mediaAdapterId": "dhav-framed-av",
            "input": {"url": f"{path}/input", "token": self.input_token},
            "media": {"url": f"{path}/media", "token": self.media_token},
            "video": {"format": "i420", "width": self.width, "height": self.height, "fps": self.fps},
            "inputFormat": dict(dhav.INPUT_AUDIO_FORMAT),
        }

    async def attach(self, channel: str, ws: web.WebSocketResponse) -> None:
        if channel == "input":
            self._input_ws = ws
        else:
            self._media_ws = ws
        self._log("channel.attached", {"id": self.id, "channel": channel})
        await self._maybe_ready()

    async def _maybe_ready(self) -> None:
        # aiohttp's WebSocketResponse defines __len__, so it is falsy even when
        # attached; these have to be explicit None checks.
        if self._ready_sent or self._destroyed or self._input_ws is None or self._media_ws is None:
            return
        self._ready_sent = True
        await self._send_input(dhav.encode_media_event(event="session.ready", avatarSessionId=self.id))
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name=f"hb-{self.id}")
        await self.renderer.start()
        self._log("session.ready", {"id": self.id})

    async def _heartbeat_loop(self) -> None:
        while not self._destroyed:
            await asyncio.sleep(1.0)
            event = dhav.encode_media_event(event="heartbeat", ts=int(time.time() * 1000))
            await self._send_input(event)
            await self._send_media_text(event)

    async def channel_lost(self, channel: str) -> None:
        if self._destroyed:
            return
        if channel == "input":
            self._input_ws = None
        else:
            self._media_ws = None
        if channel == "media" and self._turn and not self._turn.cancel_requested:
            # Never render into a queue without a consumer.
            await self._fail_turn("unavailable", "media channel lost")
            return
        await self.destroy(f"{channel}_lost")

    async def destroy(self, reason: str) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        turn = self._turn
        self._turn = None
        if self._turn_task:
            self._turn_task.cancel()
            self._turn_task = None
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        await self.renderer.close()
        for ws in (self._input_ws, self._media_ws):
            if ws is not None and not ws.closed:
                await ws.close()
        self._input_ws = self._media_ws = None
        self._log("session.destroyed", {"id": self.id, "reason": reason, "hadTurn": bool(turn)})

    # ------------------------------------------------------------------ sending

    async def _send_input(self, text: str) -> None:
        ws = self._input_ws
        if ws is None or ws.closed:
            return
        async with self._input_lock:
            try:
                await ws.send_str(text)
            except (ConnectionResetError, RuntimeError):
                pass

    async def _send_media_text(self, text: str) -> None:
        ws = self._media_ws
        if ws is None or ws.closed:
            return
        async with self._media_lock:
            try:
                await ws.send_str(text)
            except (ConnectionResetError, RuntimeError):
                pass

    async def _send_media_record(self, record: bytes) -> bool:
        ws = self._media_ws
        if ws is None or ws.closed:
            return False
        async with self._media_lock:
            try:
                await ws.send_bytes(record)
            except (ConnectionResetError, RuntimeError):
                return False
        return True

    # -------------------------------------------------------------------- input

    async def handle_input(self, message: Any) -> None:
        if self._destroyed:
            return
        if message.type == WSMsgType.TEXT:
            await self._on_control(message.data)
        elif message.type == WSMsgType.BINARY:
            await self._on_record(message.data)

    async def _on_record(self, data: bytes) -> None:
        turn = self._turn
        if turn is None or turn.cancel_requested:
            return
        try:
            record = dhav.decode_record(data)
        except dhav.DhavError as error:
            await self._fail_turn(error.code, error.message)
            return
        if record["kind"] != dhav.INPUT_PCM:
            await self._fail_turn(
                "protocol_error", f"unexpected record kind {record['kind']} on input channel"
            )
            return
        header = record["header"]
        if not dhav.same_turn(header["turn"], turn.ref):
            if header["turn"]["generation"] < turn.ref["generation"]:
                return  # stale generation, quarantined
            await self._fail_turn("protocol_error", "TurnRef mismatch on input channel")
            return

        sequence = header["sequence"]
        if sequence == turn.expected_sequence:
            unrendered = turn.admitted_samples - turn.output_samples
            if unrendered + header["samples"] > self._admission_samples:
                await self._fail_turn(
                    "capacity_exhausted", f"input admission queue over {ADMISSION_SECONDS}s bound"
                )
                return
            turn.admitted_samples += header["samples"]
            turn.expected_sequence = sequence + 1
            turn.seen[sequence] = record["payload"]
            try:
                await self.renderer.send_audio(record["payload"])
            except RendererUnavailable as error:
                await self._fail_turn("unavailable", str(error))
            return
        if sequence < turn.expected_sequence:
            if turn.seen.get(sequence) != record["payload"]:
                await self._fail_turn(
                    "protocol_error", f"duplicate sequence {sequence} with different content"
                )
            return
        await self._fail_turn(
            "protocol_error", f"sequence gap: expected {turn.expected_sequence}, got {sequence}"
        )

    async def _on_control(self, text: str) -> None:
        try:
            frame = dhav.decode_control(text)
        except dhav.DhavError as error:
            await self._fail_turn(error.code, error.message)
            return
        op = frame["op"]
        if op == dhav.OP_START:
            await self._start_turn(frame)
            return
        if op == dhav.OP_CLOSE:
            await self.destroy("close_requested")
            return
        turn = self._turn
        if turn is None:
            return
        if op == dhav.OP_FINISH:
            await self._finish_turn(frame, turn)
        elif op == dhav.OP_CANCEL:
            await self._cancel_turn(frame, turn)

    async def _start_turn(self, frame: dict) -> None:
        if self._destroyed:
            return
        if self._turn is not None:
            await self._fail_turn("protocol_error", "start while a turn is active")
            return
        if not self._ready_sent:
            await self._fail_ref("protocol_error", "start before session.ready", frame.get("turn"))
            return
        if not self.renderer.connected:
            await self._fail_ref("unavailable", "renderer not connected", frame.get("turn"))
            return
        turn = Turn(ref=dict(frame["turn"]))
        self._turn = turn
        # Turns must not inherit a half-drained resampler or leftover PCM.
        self.renderer.reset_uplink()
        self._turn_task = asyncio.create_task(self._turn_loop(turn), name=f"turn-{self.id}")
        self._log(
            "turn.started",
            {"id": self.id, "responseId": turn.ref["responseId"], "generation": turn.ref["generation"]},
        )

    async def _finish_turn(self, frame: dict, turn: Turn) -> None:
        if not dhav.same_turn(frame.get("turn"), turn.ref) or turn.finishing:
            return
        last_sequence = frame.get("lastAudioSequence")
        total_samples = frame.get("totalSamples")
        if last_sequence != turn.expected_sequence - 1 or total_samples != turn.admitted_samples:
            await self._fail_turn(
                "protocol_error",
                f"finish totals do not match admitted input: "
                f"lastAudioSequence={last_sequence} totalSamples={total_samples} "
                f"renderer saw {turn.expected_sequence - 1}/{turn.admitted_samples}",
            )
            return
        turn.finishing = True
        turn.finished_at = time.monotonic()
        try:
            # Stopping the packets is what tells the renderer the utterance ended.
            await self.renderer.flush_audio()
        except RendererUnavailable as error:
            await self._fail_turn("unavailable", str(error))

    async def _cancel_turn(self, frame: dict, turn: Turn) -> None:
        if not dhav.same_turn(frame.get("turn"), turn.ref) or turn.cancel_requested:
            return
        turn.cancel_requested = True
        self._turn = None
        if self._turn_task:
            self._turn_task.cancel()
            self._turn_task = None
        # Suppress pairing until the renderer goes quiet, so the interrupted
        # utterance's remaining output cannot leak into the next turn. Capped,
        # because a renderer that ignores Interrupt must not starve the new turn.
        now = time.monotonic()
        self._flush_deadline = now + FLUSH_MAX_S
        self._flushing_until = min(now + FLUSH_QUIET_S, self._flush_deadline)
        await self.renderer.interrupt()
        ref = turn.ref
        await self._send_input(
            dhav.encode_media_event(
                event="turn.cancelled", turn=ref, reason=frame.get("reason") or "user_interruption"
            )
        )
        self._log("turn.cancelled", {"id": self.id, "reason": frame.get("reason")})

    async def _fail_turn(self, code: str, message: str, *, rebuild: bool | None = None) -> None:
        turn = self._turn
        ref = turn.ref if turn else None
        self._turn = None
        if self._turn_task:
            self._turn_task.cancel()
            self._turn_task = None
        self._log("turn.failed", {"id": self.id, "code": code, "message": message})
        await self._fail_ref(code, message, ref, rebuild=rebuild)

    async def _fail_ref(
        self, code: str, message: str, ref: dict | None, *, rebuild: bool | None = None
    ) -> None:
        # Same rule as the reference renderer: a protocol violation is the
        # gateway's fault and retrying the session would not help, everything
        # else is a renderer-side failure the gateway should rebuild for.
        await self._send_input(
            dhav.encode_media_event(
                event="provider.error",
                turn=ref,
                code=code,
                message=message[:200],
                rebuild=code != "protocol_error" if rebuild is None else rebuild,
            )
        )

    async def _turn_loop(self, turn: Turn) -> None:
        """Progress keepalives and turn-end detection for one turn."""
        last_progress = 0.0
        while self._turn is turn and not self._destroyed:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            if turn.first_media_sent and now - last_progress >= 0.5:
                last_progress = now
                await self._send_input(
                    dhav.encode_media_event(
                        event="turn.progress", turn=turn.ref, unitsPublished=turn.published_units
                    )
                )
            if now - turn.started_at > TURN_TIMEOUT_S:
                await self._end_turn(turn, reason="timeout")
                return
            if not turn.finishing:
                continue
            quiet_for = now - max(turn.last_audio_at, turn.finished_at)
            if turn.published_units and quiet_for >= END_GRACE_S:
                await self._end_turn(turn, reason="finished")
                return
            if turn.avatar_done:
                # The avatar emitted the utterance's last frame, so nothing more
                # can arrive. This ends the turn even if its tail was not silent.
                await self._end_turn(
                    turn, reason="finished" if turn.published_units else "no_output"
                )
                return
            if turn.input_closed and turn.published_units:
                tail_for = now - max(turn.last_voice_at, turn.input_closed_at)
                # The output mirrors the input 1:1 behind the renderer's lead-in,
                # so the reply cannot be over before its own duration has elapsed.
                # Until then a silence run is a pause *inside* the reply, which is
                # what keeps this safe where a bare silence threshold is not.
                if (now - turn.started_at >= turn.admitted_samples / SAMPLE_RATE
                        and tail_for >= END_GRACE_S):
                    await self._end_turn(turn, reason="finished")
                    return
            if not turn.published_units and quiet_for >= NO_OUTPUT_GRACE_S:
                await self._end_turn(turn, reason="no_output")
                return

    async def _end_turn(self, turn: Turn, reason: str) -> None:
        if self._turn is not turn:
            return
        self._turn = None
        if self._turn_task and self._turn_task is not asyncio.current_task():
            self._turn_task.cancel()
        self._turn_task = None
        ref = turn.ref
        last_sequence = turn.published_units - 1
        await self._send_media_record(
            dhav.encode_media_end(turn=ref, last_sequence=last_sequence, reason=reason)
        )
        await self._send_input(
            dhav.encode_media_event(event="turn.completed", turn=ref, lastSequence=last_sequence)
        )
        self._log(
            "turn.completed",
            {
                "id": self.id,
                "responseId": ref["responseId"],
                "reason": reason,
                "units": turn.published_units,
                "seconds": round(turn.output_samples / SAMPLE_RATE, 2),
            },
        )

    # ------------------------------------------------------------- media output

    async def on_renderer_audio(self, pcm: bytes) -> None:
        """One 40 ms block of the renderer's frame-aligned audio track."""
        now = time.monotonic()
        if now < self._flushing_until:
            # Post-cancel suppression: the interrupted utterance is still
            # draining, and its blocks must not be paired into the next turn.
            # Only *audible* blocks extend the window. The renderer's downlink
            # never goes quiet -- idle frames arrive with silent audio at 25 fps
            # for the whole session -- so extending on silence too ran every
            # cancel out to FLUSH_MAX_S and swallowed the next turn's first
            # ~2 s of media along with its input_closed marker.
            if not _is_silence(pcm):
                self._flushing_until = min(now + FLUSH_QUIET_S, self._flush_deadline)
            return
        turn = self._turn
        if turn is None or turn.cancel_requested:
            return
        turn.last_audio_at = now
        silent = _is_silence(pcm)
        if silent and turn.input_closed:
            # The renderer closed the utterance, so this is the idle tail it
            # keeps emitting at 25 fps until the session dies: pairing it would
            # publish frozen frames and zeros for as long as we listened.
            return
        if not silent:
            turn.last_voice_at = now
        if not turn.paired:
            if silent and turn.skipped_silence < MAX_LEADING_SILENCE_BLOCKS:
                turn.skipped_silence += 1
                return
            turn.paired = True
        turn.audio_q.append(pcm)
        if len(turn.audio_q) > MAX_AUDIO_QUEUE:
            turn.audio_q.popleft()
            logger.warning("session %s dropped an audio block: consumer too slow", self.id)
        await self._emit_ready_pairs(turn)

    async def on_renderer_frame(self, jpeg: bytes) -> None:
        turn = self._turn
        if turn is None or turn.cancel_requested or not turn.paired:
            return
        if time.monotonic() < self._flushing_until:
            return
        turn.frame_q.append(jpeg)
        while len(turn.frame_q) > MAX_FRAME_QUEUE:
            turn.frame_q.popleft()
        await self._emit_ready_pairs(turn)

    async def _emit_ready_pairs(self, turn: Turn) -> None:
        while turn.audio_q and turn.frame_q and self._turn is turn:
            pcm = turn.audio_q.popleft()
            jpeg = turn.frame_q.popleft()
            i420 = jpeg_to_i420(jpeg, self.width, self.height)
            if i420 is None:
                logger.warning("session %s dropped an undecodable JPEG frame", self.id)
                continue
            await self._publish(turn, pcm, i420)

    async def _publish(self, turn: Turn, pcm: bytes, i420: bytes) -> None:
        sequence = turn.published_units
        turn.output_samples += FRAME_SAMPLES
        try:
            record = dhav.encode_paired_av(
                turn=turn.ref,
                sequence=sequence,
                pts_us=round(turn.output_samples * 1_000_000 / SAMPLE_RATE),
                duration_us=FRAME_DURATION_US,
                sample_offset=turn.output_samples - FRAME_SAMPLES,
                samples=FRAME_SAMPLES,
                video_width=self.width,
                video_height=self.height,
                video_data=i420,
                audio=pcm,
                silence=_is_silence(pcm),
            )
        except dhav.DhavError as error:
            await self._fail_turn(error.code, error.message)
            return
        if not await self._send_media_record(record):
            return
        turn.published_units += 1
        if not turn.first_media_sent:
            turn.first_media_sent = True
            await self._send_input(dhav.encode_media_event(event="turn.first_media", turn=turn.ref))

    async def on_renderer_state(self, connected: bool) -> None:
        if connected:
            # A fresh renderer connection invalidates any post-cancel suppression.
            self._flushing_until = 0.0
            return
        if self._turn:
            await self._fail_turn("unavailable", "renderer connection lost")

    async def on_renderer_utterance(self, stage: str) -> None:
        """Structural utterance markers; see ``renderer_client`` for their meaning.

        Ignored before ``finish``: until then the gateway may still be streaming,
        and a marker left over from the previous utterance must not end this one.
        """
        turn = self._turn
        if turn is None or turn.cancel_requested or not turn.finishing:
            return
        now = time.monotonic()
        if now < self._flushing_until:
            return  # still suppressing the interrupted utterance's output
        if stage == UTTERANCE_INPUT_CLOSED:
            if turn.input_closed:
                return
            turn.input_closed = True
            turn.input_closed_at = now
        elif stage == UTTERANCE_AVATAR_DONE:
            if not turn.input_closed and not turn.published_units:
                # The service opens a CLIENT_PLAYBACK stream as soon as an
                # utterance starts and closes it again ~40 ms later, so a
                # stream_end that early says nothing about the avatar having
                # finished (observed: it ended a 93 ms-old turn with no output).
                # The real marker always follows the utterance's own
                # input_closed, and published media vouches for it as well, so a
                # swallowed input_closed cannot strand the turn until
                # TURN_TIMEOUT_S.
                return
            if turn.avatar_done:
                return
            turn.avatar_done = True
        else:
            return
        self._log("turn.renderer_marker", {"id": self.id, "stage": stage})
