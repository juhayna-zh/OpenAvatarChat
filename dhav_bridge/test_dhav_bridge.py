"""Self-tests for the DHAV bridge: codec bytes, colour conversion, turn logic.

Run from the OpenAvatarChat directory::

    .venv/bin/python -m unittest dhav_bridge.test_dhav_bridge -v

No GPU and no running services required — the renderer and both DHAV channels
are stubbed, so this exercises the state machine, not FlashHead.
"""

from __future__ import annotations

import asyncio
import json
import struct
import unittest
from dataclasses import dataclass
from typing import Any
from unittest import mock

import cv2
import numpy as np
from aiohttp import WSMsgType

from . import dhav
from . import session as session_module
from . import video
from .renderer_client import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    UPLINK_SAMPLE_RATE,
    UTTERANCE_AVATAR_DONE,
    UTTERANCE_INPUT_CLOSED,
    PassthroughRenderer,
)
from .session import AvatarSession
from .server import BridgeServer

TURN = {"avatarSessionId": "s1", "responseId": "r1", "generation": 0}


def jpeg_of(color_bgr: tuple[int, int, int], size: int = 64) -> bytes:
    image = np.zeros((size, size, 3), np.uint8)
    image[:, :] = color_bgr
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return encoded.tobytes()


def text_message(payload: str) -> Any:
    @dataclass
    class Message:
        type: Any
        data: Any

    return Message(WSMsgType.TEXT, payload)


def binary_message(payload: bytes) -> Any:
    @dataclass
    class Message:
        type: Any
        data: Any

    return Message(WSMsgType.BINARY, payload)


class FakeWs:
    def __init__(self) -> None:
        self.closed = False
        self.texts: list[str] = []
        self.binaries: list[bytes] = []

    # aiohttp's WebSocketResponse defines __len__ and is therefore falsy; a
    # truthy stand-in would hide `if not ws` bugs from this suite.
    def __len__(self) -> int:
        return 0

    async def send_str(self, text: str) -> None:
        self.texts.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.binaries.append(data)

    async def close(self) -> None:
        self.closed = True

    def events(self) -> list[dict]:
        return [json.loads(t) for t in self.texts]

    def event_names(self) -> list[str]:
        return [e["event"] for e in self.events() if "event" in e]


class StubRenderer:
    """Stands in for PassthroughRenderer; records uplink instead of sending it."""

    def __init__(self, owner: AvatarSession) -> None:
        self.owner = owner
        self.connected = True
        self.uplink = bytearray()
        self.flushed = False
        self.interrupts = 0
        self.resets = 0

    def reset_uplink(self) -> None:
        self.resets += 1
        self.uplink.clear()

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        self.connected = False

    async def send_audio(self, pcm: bytes) -> None:
        self.uplink.extend(pcm)

    async def flush_audio(self) -> None:
        self.flushed = True

    async def interrupt(self) -> None:
        self.interrupts += 1


def make_session(width: int = 8, height: int = 8) -> tuple[AvatarSession, FakeWs, FakeWs]:
    session = AvatarSession(
        session_id="s1",
        persona_id="flashhead-default",
        avatar_asset_id=None,
        renderer_factory=StubRenderer,
        width=width,
        height=height,
        fps=25,
    )
    return session, FakeWs(), FakeWs()


async def ready(session: AvatarSession, input_ws: FakeWs, media_ws: FakeWs) -> None:
    await session.attach("input", input_ws)
    await session.attach("media", media_ws)


async def start(session: AvatarSession, generation: int = 0) -> None:
    await session.handle_input(
        text_message(
            dhav.encode_control(
                op=dhav.OP_START,
                request_id="req-1",
                turn={**TURN, "generation": generation},
            )
        )
    )


async def finish(
    session: AvatarSession, last_sequence: int = 0, total_samples: int = FRAME_SAMPLES,
    generation: int = 0,
) -> None:
    await session.handle_input(
        text_message(
            dhav.encode_control(
                op=dhav.OP_FINISH,
                request_id="r2",
                turn={**TURN, "generation": generation},
                last_audio_sequence=last_sequence,
                total_samples=total_samples,
            )
        )
    )


def input_record(sequence: int, samples: int = FRAME_SAMPLES, offset: int = 0, value: int = 1000,
                 generation: int = 0) -> bytes:
    payload = np.full(samples, value, dtype=np.int16).tobytes()
    return dhav.encode_input_pcm(
        turn={**TURN, "generation": generation}, sequence=sequence, sample_offset=offset,
        samples=samples, payload=payload,
    )


# Renderer downlink blocks. Amplitude matters: silence is an RMS threshold, so a
# one-LSB "non-zero" block reads as silence and would be skipped.
LOUD_BLOCK = np.full(FRAME_SAMPLES, 3000, dtype=np.int16).tobytes()
SILENT_BLOCK = b"\x00" * FRAME_BYTES


class CodecTest(unittest.TestCase):
    def test_prefix_layout_is_network_order(self) -> None:
        record = dhav.encode_record(dhav.MEDIA_END, {"a": 1}, b"xy")
        magic, version, kind, flags, header_len, payload_len = struct.unpack(">4sBBHII", record[:16])
        self.assertEqual(magic, b"DHAV")
        self.assertEqual((version, kind, flags), (1, dhav.MEDIA_END, 0))
        self.assertEqual(header_len, len('{"a":1}'))
        self.assertEqual(payload_len, 2)
        self.assertEqual(record[16:], b'{"a":1}xy')

    def test_input_pcm_round_trip(self) -> None:
        payload = b"\x01\x02" * 480
        record = dhav.encode_input_pcm(
            turn=TURN, sequence=7, sample_offset=960, samples=480, payload=payload
        )
        decoded = dhav.decode_record(record)
        self.assertEqual(decoded["kind"], dhav.INPUT_PCM)
        self.assertEqual(decoded["header"]["sequence"], 7)
        self.assertEqual(decoded["header"]["format"]["sampleRate"], 24000)
        self.assertEqual(decoded["payload"], payload)

    def test_paired_av_splits_audio_and_video(self) -> None:
        audio = b"\x00\x01" * FRAME_SAMPLES
        frame = b"\x10" * dhav.i420_byte_length(4, 4)
        record = dhav.encode_paired_av(
            turn=TURN, sequence=3, pts_us=40000, duration_us=40000, sample_offset=2880,
            samples=FRAME_SAMPLES, video_width=4, video_height=4, video_data=frame, audio=audio,
        )
        decoded = dhav.decode_record(record)
        self.assertEqual(decoded["kind"], dhav.PAIRED_AV)
        self.assertEqual(decoded["audio"], audio)
        self.assertEqual(decoded["video"], frame)
        self.assertEqual(decoded["header"]["video"], {"format": "i420", "width": 4, "height": 4})
        self.assertFalse(decoded["header"]["silence"])

    def test_rejects_bad_length_magic_and_version(self) -> None:
        good = dhav.encode_media_end(turn=TURN, last_sequence=0)
        for mutate, code in (
            (lambda b: b[:10], "protocol_error"),
            (lambda b: b"XH" + b[2:], "protocol_error"),
            (lambda b: b[:4] + bytes([2]) + b[5:], "protocol_error"),
            (lambda b: b[:-1], "protocol_error"),
        ):
            with self.assertRaises(dhav.DhavError) as caught:
                dhav.decode_record(mutate(good))
            self.assertEqual(caught.exception.code, code)

    def test_rejects_odd_or_oversized_video(self) -> None:
        for width, height in ((3, 4), (4, 3), (514, 4), (4, 1024)):
            with self.assertRaises(dhav.DhavError):
                dhav.encode_paired_av(
                    turn=TURN, sequence=0, pts_us=1, duration_us=1, sample_offset=0,
                    samples=FRAME_SAMPLES, video_width=width, video_height=height,
                    video_data=b"\x00" * dhav.i420_byte_length(max(width, 2), max(height, 2)),
                    audio=b"\x00" * FRAME_BYTES,
                )

    def test_control_and_event_envelopes(self) -> None:
        frame = json.loads(
            dhav.encode_control(op=dhav.OP_FINISH, request_id="r", turn=TURN, last_audio_sequence=4, total_samples=4800)
        )
        self.assertEqual(frame, {
            "protocol": "dhav", "version": 1, "op": "finish", "requestId": "r", "turn": TURN,
            "lastAudioSequence": 4, "totalSamples": 4800,
        })
        self.assertEqual(dhav.decode_control(json.dumps(frame)), frame)
        event = json.loads(dhav.encode_media_event(event="heartbeat", ts=1))
        self.assertEqual(event, {"protocol": "dhav", "version": 1, "event": "heartbeat", "ts": 1})
        self.assertEqual(dhav.decode_media_event(json.dumps(event)), event)

    def test_turn_ref_validation(self) -> None:
        for bad in (None, {}, {"avatarSessionId": "", "responseId": "r", "generation": 0},
                    {"avatarSessionId": "s", "responseId": "r", "generation": -1},
                    {"avatarSessionId": "s", "responseId": "r", "generation": 1.0}):
            with self.assertRaises(dhav.DhavError):
                dhav.assert_turn_ref(bad)


class VideoTest(unittest.TestCase):
    def test_full_range_bt601_matches_reference_coefficients(self) -> None:
        # The reference mock renderer uses full-range BT.601; OpenCV's own
        # COLOR_BGR2YUV_I420 is limited-range, which is why video.py converts by hand.
        bgr = np.zeros((4, 4, 3), np.uint8)
        bgr[:, :] = (0, 0, 255)  # pure red
        out = np.frombuffer(video.bgr_to_i420(bgr, 4, 4), dtype=np.uint8)
        self.assertEqual(len(out), 24)
        expect_y = 0.299 * 255
        expect_u = 128 - 0.168736 * 255
        expect_v = 128 + 0.5 * 255
        self.assertLessEqual(abs(int(out[0]) - expect_y), 1)
        self.assertLessEqual(abs(int(out[16]) - expect_u), 1)
        self.assertLessEqual(abs(int(out[20]) - expect_v), 1)
        # OpenCV would have produced limited-range values instead.
        self.assertGreater(abs(int(out[0]) - 82), 2)

    def test_plane_sizes_and_neutral_frame(self) -> None:
        out = np.frombuffer(video.neutral_i420(16, 8), dtype=np.uint8)
        self.assertEqual(len(out), 16 * 8 * 3 // 2)
        self.assertTrue((out[:128] == 16).all())
        self.assertTrue((out[128:] == 128).all())

    def test_jpeg_decode_and_resize(self) -> None:
        frame = video.jpeg_to_i420(jpeg_of((255, 0, 0), 64), 32, 32)
        self.assertEqual(len(frame), 32 * 32 * 3 // 2)
        self.assertIsNone(video.jpeg_to_i420(b"not a jpeg", 32, 32))

    def test_parse_size(self) -> None:
        self.assertEqual(video.parse_size("256"), (256, 256))
        self.assertEqual(video.parse_size("320x240"), (320, 240))
        for bad in ("0", "3", "513", "640x8", "abc"):
            with self.assertRaises(ValueError):
                video.parse_size(bad)


class DescriptionTest(unittest.TestCase):
    def test_public_description_maps_the_base_url_scheme(self) -> None:
        session, _, _ = make_session()
        for base, expected in (
            ("http://host:18910", "ws://host:18910"),
            ("https://host:18910/", "wss://host:18910"),
            ("host:18910", "ws://host:18910"),
        ):
            description = session.public_description(base)
            prefix = f"{expected}/v1/avatar/sessions/{session.id}"
            self.assertEqual(description["input"]["url"], f"{prefix}/input")
            self.assertEqual(description["media"]["url"], f"{prefix}/media")
        description = session.public_description("http://h:1")
        self.assertEqual(description["protocolVersion"], 1)
        self.assertEqual(description["mediaAdapterId"], "dhav-framed-av")
        self.assertEqual(description["inputFormat"], dict(dhav.INPUT_AUDIO_FORMAT))
        self.assertNotEqual(description["input"]["token"], description["media"]["token"])


class RendererUplinkTest(unittest.TestCase):
    """DHAV input is 24 kHz; the renderer's MIC_AUDIO entry is hardcoded to 16 kHz."""

    @staticmethod
    def make(**kwargs) -> PassthroughRenderer:
        return PassthroughRenderer(
            base_url="ws://127.0.0.1:8284", ca_file=None, session_prefix="t", **kwargs
        )

    def test_resample_preserves_duration_and_pitch(self) -> None:
        renderer = self.make()
        self.assertEqual(UPLINK_SAMPLE_RATE, 16000)
        tone = (np.sin(2 * np.pi * 440 * np.arange(24000) / 24000) * 16000).astype("<i2")
        out = b"".join(
            renderer._resample(tone[i : i + 2400].tobytes()) for i in range(0, len(tone), 2400)
        )
        out += renderer._resample(b"", last=True)
        samples = np.frombuffer(out, dtype=np.int16).astype(np.float32)
        # A rate mismatch shows up as a duration change and a pitch change at once.
        self.assertAlmostEqual(len(samples) / UPLINK_SAMPLE_RATE, 1.0, places=3)
        freqs = np.fft.rfftfreq(len(samples), 1 / UPLINK_SAMPLE_RATE)
        magnitude = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
        magnitude[freqs < 60] = 0
        self.assertAlmostEqual(float(freqs[magnitude.argmax()]), 440.0, delta=2.0)

    def test_reset_uplink_replaces_the_stateful_resampler(self) -> None:
        renderer = self.make()
        previous = renderer._resampler
        renderer.reset_uplink()
        self.assertIsNot(renderer._resampler, previous)

    def test_uplink_packet_must_not_collide_with_frame_bytes(self) -> None:
        with self.assertRaises(ValueError):
            self.make(uplink_chunk_samples=FRAME_BYTES // 2)


class SilenceTest(unittest.TestCase):
    def test_idle_dither_counts_as_silence(self) -> None:
        rng = np.random.default_rng(7)
        # The renderer's idle_noise_amplitude is 0.003; measured rms 0.0013.
        dither = (rng.uniform(-140, 140, FRAME_SAMPLES)).astype("<i2")
        self.assertTrue(session_module._is_silence(dither.tobytes()))
        self.assertTrue(session_module._is_silence(SILENT_BLOCK))
        speech = (np.sin(2 * np.pi * 220 * np.arange(FRAME_SAMPLES) / 24000) * 6000).astype("<i2")
        self.assertFalse(session_module._is_silence(speech.tobytes()))


class RendererSignalTest(unittest.IsolatedAsyncioTestCase):
    """Turn end is structural: the frame-aligned track never goes quiet."""

    @staticmethod
    def make(stages: list[str]) -> PassthroughRenderer:
        async def on_utterance(stage: str) -> None:
            stages.append(stage)

        return PassthroughRenderer(
            base_url="ws://127.0.0.1:8284", ca_file=None, session_prefix="t",
            on_utterance=on_utterance,
        )

    @staticmethod
    def signal(kind: str, stream_type: str, producer: str) -> str:
        return json.dumps({
            "header": {"name": "ChatSignal", "request_id": "sig-1"},
            "payload": {
                "type": kind, "source_type": "handler", "stream_type": stream_type,
                "stream_producer": producer, "stream_key": "stream_1_0",
            },
        })

    async def test_stream_end_maps_onto_utterance_markers(self) -> None:
        stages: list[str] = []
        renderer = self.make(stages)
        await renderer._on_text(self.signal("stream_end", "avatar_audio", "AudioPassthrough"))
        await renderer._on_text(self.signal("stream_end", "client_playback", "FlashHead"))
        self.assertEqual(stages, [UTTERANCE_INPUT_CLOSED, UTTERANCE_AVATAR_DONE])

    async def test_unrelated_signals_are_ignored(self) -> None:
        stages: list[str] = []
        renderer = self.make(stages)
        # An interrupted utterance is cancelled rather than ended.
        await renderer._on_text(self.signal("stream_cancel", "client_playback", "FlashHead"))
        await renderer._on_text(self.signal("stream_begin", "avatar_audio", "AudioPassthrough"))
        # The avatar's own frame-aligned stream is session-long, so its end is
        # session teardown; only the passthrough stream marks the client's input.
        await renderer._on_text(self.signal("stream_end", "avatar_audio", "FlashHead"))
        await renderer._on_text(self.signal("stream_end", "mic_audio", "WsVideoClient"))
        self.assertEqual(stages, [])


class TurnStateMachineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        patcher = mock.patch.multiple(
            session_module, END_GRACE_S=0.2, NO_OUTPUT_GRACE_S=0.3, FLUSH_QUIET_S=0.1, FLUSH_MAX_S=0.4
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_ready_needs_both_channels(self) -> None:
        session, input_ws, media_ws = make_session()
        await session.attach("input", input_ws)
        self.assertEqual(input_ws.event_names(), [])
        await session.attach("media", media_ws)
        self.assertEqual(input_ws.event_names(), ["session.ready"])
        self.assertEqual(media_ws.event_names(), [])
        await session.destroy("test")

    async def test_full_turn_pairs_audio_with_frames(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)

        await session.handle_input(binary_message(input_record(0)))
        await session.handle_input(
            text_message(dhav.encode_control(op=dhav.OP_FINISH, request_id="r2", turn=TURN,
                                             last_audio_sequence=0, total_samples=FRAME_SAMPLES))
        )
        renderer: StubRenderer = session.renderer
        self.assertEqual(bytes(renderer.uplink), b"\xe8\x03" * FRAME_SAMPLES)
        self.assertTrue(renderer.flushed)

        frame = jpeg_of((40, 80, 200))
        blocks = 6
        for index in range(blocks):
            await session.on_renderer_audio(SILENT_BLOCK if index == 0 else LOUD_BLOCK)
            await session.on_renderer_frame(frame)
        await asyncio.sleep(0.5)

        self.assertEqual(len(media_ws.binaries) - 1, blocks - 1)  # one leading silence block skipped
        units = [dhav.decode_record(b) for b in media_ws.binaries[:-1]]
        self.assertEqual([u["header"]["sequence"] for u in units], list(range(blocks - 1)))
        self.assertEqual([u["header"]["ptsUs"] for u in units],
                         [round((i + 1) * FRAME_SAMPLES * 1e6 / 24000) for i in range(blocks - 1)])
        self.assertTrue(all(u["header"]["samples"] == FRAME_SAMPLES for u in units))
        self.assertTrue(all(len(u["video"]) == dhav.i420_byte_length(8, 8) for u in units))

        end = dhav.decode_record(media_ws.binaries[-1])
        self.assertEqual(end["kind"], dhav.MEDIA_END)
        self.assertEqual(end["header"]["lastSequence"], blocks - 2)
        names = input_ws.event_names()
        self.assertIn("turn.first_media", names)
        self.assertIn("turn.completed", names)
        self.assertIsNone(session._turn)
        await session.destroy("test")

    async def test_start_before_ready_fails(self) -> None:
        session, input_ws, media_ws = make_session()
        await session.attach("input", input_ws)
        await start(session)
        self.assertIn("provider.error", input_ws.event_names())
        error = next(e for e in input_ws.events() if e["event"] == "provider.error")
        self.assertEqual(error["code"], "protocol_error")
        self.assertFalse(error["rebuild"])
        await session.destroy("test")

    async def test_sequence_gap_is_a_protocol_error(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)
        await session.handle_input(binary_message(input_record(1)))
        error = next(e for e in input_ws.events() if e.get("event") == "provider.error")
        self.assertEqual(error["code"], "protocol_error")
        self.assertIn("sequence gap", error["message"])
        self.assertIsNone(session._turn)
        await session.destroy("test")

    async def test_finish_totals_are_checked(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)
        await session.handle_input(binary_message(input_record(0)))
        await session.handle_input(
            text_message(dhav.encode_control(op=dhav.OP_FINISH, request_id="r2", turn=TURN,
                                             last_audio_sequence=0, total_samples=999))
        )
        error = next(e for e in input_ws.events() if e.get("event") == "provider.error")
        self.assertEqual(error["code"], "protocol_error")
        await session.destroy("test")

    async def test_cancel_stops_output_and_suppresses_residual_audio(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)
        frame = jpeg_of((10, 200, 30))
        await session.on_renderer_audio(LOUD_BLOCK)
        await session.on_renderer_frame(frame)
        published = len(media_ws.binaries)
        self.assertEqual(published, 1)

        await session.handle_input(
            text_message(dhav.encode_control(op=dhav.OP_CANCEL, request_id="r3", turn=TURN,
                                             reason="user_interruption"))
        )
        self.assertEqual(session.renderer.interrupts, 1)
        self.assertIn("turn.cancelled", input_ws.event_names())

        # Residual output from the interrupted utterance must not be published.
        for _ in range(5):
            await session.on_renderer_audio(LOUD_BLOCK)
            await session.on_renderer_frame(frame)
        self.assertEqual(len(media_ws.binaries), published)

        # A new turn starts pairing once the renderer has gone quiet.
        await asyncio.sleep(0.45)
        await start(session, generation=1)
        await session.on_renderer_audio(LOUD_BLOCK)
        await session.on_renderer_frame(frame)
        self.assertEqual(len(media_ws.binaries), published + 1)
        unit = dhav.decode_record(media_ws.binaries[-1])
        self.assertEqual(unit["header"]["turn"]["generation"], 1)
        self.assertEqual(unit["header"]["sequence"], 0)
        await session.destroy("test")

    async def test_a_silent_drain_does_not_swallow_the_next_turn(self) -> None:
        """Regression: CANCEL(idle) + START(real) lost the reply's first seconds.

        The idle downlink is silent blocks at 25 fps forever, so a flush window
        extended by *every* suppressed block always ran out to FLUSH_MAX_S and
        dropped the superseding turn's head along with its markers.
        """
        with mock.patch.multiple(session_module, FLUSH_QUIET_S=0.3, FLUSH_MAX_S=3.0):
            session, input_ws, media_ws = make_session()
            await ready(session, input_ws, media_ws)
            await start(session)
            frame = jpeg_of((10, 200, 30))
            await session.on_renderer_audio(SILENT_BLOCK)
            await session.on_renderer_frame(frame)
            await session.handle_input(
                text_message(dhav.encode_control(op=dhav.OP_CANCEL, request_id="r3", turn=TURN,
                                                 reason="superseded"))
            )
            await start(session, generation=1)

            # Idle output keeps arriving while the new turn starts up: it must
            # neither hold the window open nor be published.
            for _ in range(10):
                await session.on_renderer_audio(SILENT_BLOCK)
                await session.on_renderer_frame(frame)
                await asyncio.sleep(0.1)
            self.assertEqual(media_ws.binaries, [])

            await session.on_renderer_audio(LOUD_BLOCK)
            await session.on_renderer_frame(frame)
            self.assertEqual(len(media_ws.binaries), 1)
            unit = dhav.decode_record(media_ws.binaries[0])
            self.assertEqual(unit["header"]["turn"]["generation"], 1)
            self.assertEqual(unit["header"]["sequence"], 0)
            await session.destroy("test")

    async def test_an_audible_drain_still_suppresses_until_it_goes_quiet(self) -> None:
        with mock.patch.multiple(session_module, FLUSH_QUIET_S=0.5, FLUSH_MAX_S=5.0):
            session, input_ws, media_ws = make_session()
            await ready(session, input_ws, media_ws)
            await start(session)
            frame = jpeg_of((10, 200, 30))
            await session.on_renderer_audio(LOUD_BLOCK)
            await session.on_renderer_frame(frame)
            published = len(media_ws.binaries)
            await session.handle_input(
                text_message(dhav.encode_control(op=dhav.OP_CANCEL, request_id="r3", turn=TURN,
                                                 reason="user_interruption"))
            )
            await start(session, generation=1)

            # The interrupted utterance is still talking, so it keeps the window open.
            for _ in range(3):
                await session.on_renderer_audio(LOUD_BLOCK)
                await session.on_renderer_frame(frame)
                await asyncio.sleep(0.3)
            self.assertEqual(len(media_ws.binaries), published)

            await asyncio.sleep(0.7)
            await session.on_renderer_audio(LOUD_BLOCK)
            await session.on_renderer_frame(frame)
            self.assertEqual(len(media_ws.binaries), published + 1)
            self.assertEqual(dhav.decode_record(media_ws.binaries[-1])["header"]["sequence"], 0)
            await session.destroy("test")

    async def test_frames_are_dropped_while_idle(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        frame = jpeg_of((10, 200, 30))
        for _ in range(300):
            await session.on_renderer_frame(frame)
        self.assertEqual(media_ws.binaries, [])
        await session.destroy("test")

    async def test_silence_skip_is_capped(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)
        frame = jpeg_of((10, 200, 30))
        for _ in range(session_module.MAX_LEADING_SILENCE_BLOCKS + 3):
            await session.on_renderer_audio(SILENT_BLOCK)
            await session.on_renderer_frame(frame)
        # The cap forces pairing so an all-silent reply still produces media.
        self.assertEqual(len(media_ws.binaries), 3)
        first = dhav.decode_record(media_ws.binaries[0])
        self.assertTrue(first["header"]["silence"])
        await session.destroy("test")

    async def test_idle_tail_after_input_closed_ends_the_turn(self) -> None:
        """Regression: the renderer keeps sending silent blocks at 25 fps forever."""
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)
        await session.handle_input(binary_message(input_record(0)))
        await finish(session)
        frame = jpeg_of((40, 80, 200))
        for _ in range(4):
            await session.on_renderer_audio(LOUD_BLOCK)
            await session.on_renderer_frame(frame)
        self.assertEqual(len(media_ws.binaries), 4)

        await session.on_renderer_utterance(UTTERANCE_INPUT_CLOSED)
        # The idle tail: a static frame plus exact zeros, for as long as we listen.
        for _ in range(20):
            await session.on_renderer_audio(SILENT_BLOCK)
            await session.on_renderer_frame(frame)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)

        self.assertIsNone(session._turn)
        self.assertIn("turn.completed", input_ws.event_names())
        # None of the tail was paired into media: 4 units plus media_end.
        self.assertEqual(len(media_ws.binaries), 5)
        end = dhav.decode_record(media_ws.binaries[-1])
        self.assertEqual(end["kind"], dhav.MEDIA_END)
        self.assertEqual(end["header"]["lastSequence"], 3)
        await session.destroy("test")

    async def test_a_pause_inside_the_reply_does_not_end_the_turn(self) -> None:
        """Only the renderer's marker arms the tail rule, so content silence survives."""
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)
        await session.handle_input(binary_message(input_record(0)))
        await finish(session)
        frame = jpeg_of((40, 80, 200))
        await session.on_renderer_audio(LOUD_BLOCK)
        await session.on_renderer_frame(frame)
        for _ in range(15):
            await session.on_renderer_audio(SILENT_BLOCK)
            await session.on_renderer_frame(frame)
            await asyncio.sleep(0.02)
        self.assertIsNotNone(session._turn)
        self.assertNotIn("turn.completed", input_ws.event_names())

        await session.on_renderer_audio(LOUD_BLOCK)
        await session.on_renderer_frame(frame)
        # The pause is part of the reply, so it is published rather than dropped.
        self.assertEqual(len(media_ws.binaries), 17)
        await session.destroy("test")

    async def test_the_tail_rule_waits_for_the_reply_to_have_played(self) -> None:
        """A renderer playing catch-up must not have its pauses read as the tail."""
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)
        records = 100  # 4 s of reply audio
        for sequence in range(records):
            await session.handle_input(
                binary_message(input_record(sequence, offset=sequence * FRAME_SAMPLES))
            )
        await finish(session, last_sequence=records - 1,
                     total_samples=records * FRAME_SAMPLES)
        frame = jpeg_of((40, 80, 200))
        await session.on_renderer_audio(LOUD_BLOCK)
        await session.on_renderer_frame(frame)
        await session.on_renderer_utterance(UTTERANCE_INPUT_CLOSED)
        for _ in range(15):
            await session.on_renderer_audio(SILENT_BLOCK)
            await session.on_renderer_frame(frame)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)
        # 4 s of reply cannot have been rendered in the ~0.5 s since start.
        self.assertIsNotNone(session._turn)
        self.assertNotIn("turn.completed", input_ws.event_names())
        await session.destroy("test")

    async def test_avatar_done_ends_a_turn_whose_tail_is_not_silent(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)
        await session.handle_input(binary_message(input_record(0)))
        await finish(session)
        frame = jpeg_of((40, 80, 200))
        await session.on_renderer_audio(LOUD_BLOCK)
        await session.on_renderer_frame(frame)
        await session.on_renderer_utterance(UTTERANCE_INPUT_CLOSED)
        # An idle animation with audible ambient noise never trips the silence rule.
        for _ in range(10):
            await session.on_renderer_audio(LOUD_BLOCK)
            await session.on_renderer_frame(frame)
            await asyncio.sleep(0.02)
        self.assertIsNotNone(session._turn)

        await session.on_renderer_utterance(UTTERANCE_AVATAR_DONE)
        await asyncio.sleep(0.15)
        self.assertIsNone(session._turn)
        self.assertIn("turn.completed", input_ws.event_names())
        self.assertEqual(len(media_ws.binaries), 12)  # 11 units plus media_end
        await session.destroy("test")

    async def test_an_early_avatar_done_marker_does_not_end_a_fresh_turn(self) -> None:
        """Regression: the service closes a CLIENT_PLAYBACK stream ~40 ms after
        opening it, and that stream_end ended a 93 ms-old turn with no output."""
        with mock.patch.multiple(session_module, END_GRACE_S=5.0, NO_OUTPUT_GRACE_S=5.0):
            session, input_ws, media_ws = make_session()
            await ready(session, input_ws, media_ws)
            await start(session)
            await session.handle_input(binary_message(input_record(0)))
            await finish(session)
            frame = jpeg_of((40, 80, 200))
            await session.on_renderer_utterance(UTTERANCE_AVATAR_DONE)
            await asyncio.sleep(0.3)
            self.assertIsNotNone(session._turn)
            self.assertNotIn("turn.completed", input_ws.event_names())

            await session.on_renderer_audio(LOUD_BLOCK)
            await session.on_renderer_frame(frame)
            await session.on_renderer_utterance(UTTERANCE_INPUT_CLOSED)
            await session.on_renderer_utterance(UTTERANCE_AVATAR_DONE)
            await asyncio.sleep(0.15)
            self.assertIsNone(session._turn)
            self.assertIn("turn.completed", input_ws.event_names())
            end = dhav.decode_record(media_ws.binaries[-1])
            self.assertEqual(end["kind"], dhav.MEDIA_END)
            self.assertEqual(end["header"]["reason"], "finished")
            await session.destroy("test")

    async def test_avatar_done_ends_a_turn_whose_input_closed_marker_was_lost(self) -> None:
        """Published media vouches for the marker, so a turn cannot be stranded
        until TURN_TIMEOUT_S when its input_closed never arrives."""
        with mock.patch.multiple(session_module, END_GRACE_S=5.0, NO_OUTPUT_GRACE_S=5.0):
            session, input_ws, media_ws = make_session()
            await ready(session, input_ws, media_ws)
            await start(session)
            await session.handle_input(binary_message(input_record(0)))
            await finish(session)
            frame = jpeg_of((40, 80, 200))
            await session.on_renderer_audio(LOUD_BLOCK)
            await session.on_renderer_frame(frame)
            self.assertEqual(len(media_ws.binaries), 1)

            await session.on_renderer_utterance(UTTERANCE_AVATAR_DONE)
            await asyncio.sleep(0.15)
            self.assertIsNone(session._turn)
            end = dhav.decode_record(media_ws.binaries[-1])
            self.assertEqual(end["header"]["reason"], "finished")
            await session.destroy("test")

    async def test_markers_before_finish_are_ignored(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)
        await session.handle_input(binary_message(input_record(0)))
        frame = jpeg_of((40, 80, 200))
        await session.on_renderer_audio(LOUD_BLOCK)
        await session.on_renderer_frame(frame)
        # Leftovers from the previous utterance: the gateway is still streaming.
        await session.on_renderer_utterance(UTTERANCE_INPUT_CLOSED)
        await session.on_renderer_utterance(UTTERANCE_AVATAR_DONE)
        for _ in range(8):
            await session.on_renderer_audio(LOUD_BLOCK)
            await session.on_renderer_frame(frame)
            await asyncio.sleep(0.02)
        self.assertIsNotNone(session._turn)
        self.assertNotIn("turn.completed", input_ws.event_names())
        self.assertEqual(len(media_ws.binaries), 9)
        await session.destroy("test")

    async def test_a_second_turn_in_the_same_session_is_unaffected(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        frame = jpeg_of((40, 80, 200))

        await start(session)
        await session.handle_input(binary_message(input_record(0)))
        await finish(session)
        for _ in range(3):
            await session.on_renderer_audio(LOUD_BLOCK)
            await session.on_renderer_frame(frame)
        await session.on_renderer_utterance(UTTERANCE_INPUT_CLOSED)
        for _ in range(12):
            await session.on_renderer_audio(SILENT_BLOCK)
            await session.on_renderer_frame(frame)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)
        self.assertIsNone(session._turn)
        self.assertEqual(len(media_ws.binaries), 4)  # 3 units plus media_end

        # Turn 1's avatar_done lands after the turn it belongs to: harmless.
        await session.on_renderer_utterance(UTTERANCE_AVATAR_DONE)

        await start(session, generation=1)
        await session.handle_input(binary_message(input_record(0, generation=1)))
        await session.on_renderer_audio(LOUD_BLOCK)
        await session.on_renderer_frame(frame)
        unit = dhav.decode_record(media_ws.binaries[-1])
        self.assertEqual(unit["header"]["turn"]["generation"], 1)
        self.assertEqual(unit["header"]["sequence"], 0)
        # A leftover marker must not arm the new turn's tail rule.
        self.assertFalse(session._turn.input_closed)

        await finish(session, generation=1)
        await session.on_renderer_utterance(UTTERANCE_INPUT_CLOSED)
        for _ in range(12):
            await session.on_renderer_audio(SILENT_BLOCK)
            await session.on_renderer_frame(frame)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)
        self.assertIsNone(session._turn)
        self.assertEqual(input_ws.event_names().count("turn.completed"), 2)
        self.assertEqual(len(media_ws.binaries), 6)  # plus 1 unit and media_end
        await session.destroy("test")

    async def test_media_channel_loss_fails_the_turn(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session)
        await session.channel_lost("media")
        error = next(e for e in input_ws.events() if e.get("event") == "provider.error")
        self.assertEqual(error["code"], "unavailable")
        self.assertTrue(error["rebuild"])
        await session.destroy("test")

    async def test_stale_generation_records_are_quarantined(self) -> None:
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        await start(session, generation=2)
        stale = dhav.encode_input_pcm(
            turn={**TURN, "generation": 1}, sequence=0, sample_offset=0,
            samples=FRAME_SAMPLES, payload=b"\x01\x00" * FRAME_SAMPLES,
        )
        await session.handle_input(binary_message(stale))
        self.assertEqual([e["event"] for e in input_ws.events() if e.get("event") == "provider.error"], [])
        self.assertEqual(bytes(session.renderer.uplink), b"")
        await session.destroy("test")


class FakeRequest:
    """Just enough of aiohttp's Request for ``_create_session``."""

    scheme = "http"
    host = "127.0.0.1:18910"
    headers: dict[str, str] = {}
    app: dict[str, Any] = {}
    match_info: dict[str, str] = {}

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


CREATE_BODY = {"personaId": "flashhead-default", "protocolVersion": dhav.VERSION}


class SessionReaperTest(unittest.IsolatedAsyncioTestCase):
    """An abandoned session must give its slot back.

    With ``max_sessions`` 1 a create whose client never connects used to wedge
    the whole service: ``channel_lost`` only fires for a channel that attached,
    so nothing ever destroyed it and every later create got 409.
    """

    @staticmethod
    def bridge(attach_timeout: float = 0.05) -> BridgeServer:
        # ws:// (not wss://) so building a renderer needs no CA, and port 1 so
        # an accidental dial fails instead of reaching a real service.
        return BridgeServer(
            renderer_base_url="ws://127.0.0.1:1",
            ca_file=None,
            max_sessions=1,
            attach_timeout_s=attach_timeout,
        )

    async def test_an_unattached_session_is_reaped(self) -> None:
        bridge = self.bridge()
        session, _, _ = make_session()
        bridge.sessions[session.id] = session
        await bridge._reap_if_unattached(session.id)
        self.assertTrue(session.destroyed)
        self.assertEqual(bridge.sessions, {})

    async def test_a_half_attached_session_is_reaped(self) -> None:
        # One channel up but no session.ready: the client is gone, and the
        # channel that never attached can never report itself lost.
        bridge = self.bridge()
        session, input_ws, _ = make_session()
        await session.attach("input", input_ws)
        bridge.sessions[session.id] = session
        await bridge._reap_if_unattached(session.id)
        self.assertTrue(session.destroyed)
        self.assertEqual(bridge.sessions, {})

    async def test_a_connected_session_survives(self) -> None:
        bridge = self.bridge()
        session, input_ws, media_ws = make_session()
        await ready(session, input_ws, media_ws)
        bridge.sessions[session.id] = session
        await bridge._reap_if_unattached(session.id)
        self.assertFalse(session.destroyed)
        self.assertIn(session.id, bridge.sessions)
        await session.destroy("test")

    async def test_an_already_gone_session_is_not_an_error(self) -> None:
        bridge = self.bridge()
        await bridge._reap_if_unattached("no-such-session")

    async def test_an_abandoned_create_frees_the_slot_for_the_next_one(self) -> None:
        bridge = self.bridge()
        self.assertEqual((await bridge._create_session(FakeRequest(CREATE_BODY))).status, 201)
        self.assertEqual((await bridge._create_session(FakeRequest(CREATE_BODY))).status, 409)
        await asyncio.sleep(0.1)  # the reaper runs on its own timer
        self.assertEqual(bridge.sessions, {})
        self.assertEqual((await bridge._create_session(FakeRequest(CREATE_BODY))).status, 201)

    async def test_a_client_that_connects_in_time_is_not_reaped(self) -> None:
        bridge = self.bridge()
        response = await bridge._create_session(FakeRequest(CREATE_BODY))
        session = next(iter(bridge.sessions.values()))
        session.renderer = StubRenderer(session)  # attaching starts it; no upstream here
        await ready(session, FakeWs(), FakeWs())
        await asyncio.sleep(0.1)
        self.assertFalse(session.destroyed)
        self.assertEqual(response.status, 201)
        await session.destroy("test")


if __name__ == "__main__":
    unittest.main()
