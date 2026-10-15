"""DHAV v1 wire codec.

Authoritative definition of the DHAV v1 wire format for this service.
``test_dhav_bridge.py`` pins the layout; change both together.

Record layout (one record per WebSocket binary message)::

    16-byte prefix: magic[4]='DHAV' | version u8 | kind u8 | flags u16
                    | headerLength u32 | payloadLength u32      (big endian)
    UTF-8 JSON header
    binary payload

Control frames and renderer events are WebSocket *text* messages carrying
``{"protocol": "dhav", "version": 1, ...}``.
"""

from __future__ import annotations

import json
import struct
from typing import Any, Mapping

MAGIC = b"DHAV"
VERSION = 1
PREFIX_LENGTH = 16
_PREFIX = struct.Struct(">BBHII")

HEADER_BYTES_LIMIT = 4096
RECORD_BYTES_LIMIT = 1024 * 1024
CONTROL_BYTES_LIMIT = 16 * 1024
VIDEO_WIDTH_LIMIT = 512
VIDEO_HEIGHT_LIMIT = 512

INPUT_PCM = 1
PAIRED_AV = 2
MEDIA_END = 3
_RECORD_KINDS = (INPUT_PCM, PAIRED_AV, MEDIA_END)

OP_START = "start"
OP_FINISH = "finish"
OP_CANCEL = "cancel"
OP_CLOSE = "close"
CONTROL_OPS = (OP_START, OP_FINISH, OP_CANCEL, OP_CLOSE)

INPUT_AUDIO_FORMAT: Mapping[str, Any] = {
    "encoding": "pcm_s16le",
    "sampleRate": 24000,
    "channels": 1,
}


class DhavError(Exception):
    """Protocol violation.  ``code`` is the DHAV provider error taxonomy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _reject(code: str, message: str) -> None:
    raise DhavError(code, message)


def i420_byte_length(width: int, height: int) -> int:
    return width * height * 3 // 2


def assert_turn_ref(turn: Any) -> dict:
    if not isinstance(turn, Mapping):
        _reject("protocol_error", "TurnRef required")
    session_id = turn.get("avatarSessionId")
    response_id = turn.get("responseId")
    generation = turn.get("generation")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        _reject("protocol_error", "TurnRef.avatarSessionId required")
    if not isinstance(response_id, str) or not response_id or len(response_id) > 256:
        _reject("protocol_error", "TurnRef.responseId required")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        _reject("protocol_error", "TurnRef.generation must be a non-negative integer")
    return {
        "avatarSessionId": session_id,
        "responseId": response_id,
        "generation": generation,
    }


def assert_audio_format(fmt: Any) -> dict:
    if (
        not isinstance(fmt, Mapping)
        or fmt.get("encoding") != "pcm_s16le"
        or not isinstance(fmt.get("sampleRate"), int)
        or isinstance(fmt.get("sampleRate"), bool)
        or fmt["sampleRate"] <= 0
        or fmt.get("channels") != 1
    ):
        _reject("protocol_error", "audio format must be pcm_s16le mono")
    return {"encoding": "pcm_s16le", "sampleRate": fmt["sampleRate"], "channels": 1}


def _non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _reject("protocol_error", f"{field} must be a non-negative integer")
    return value


def encode_record(kind: int, header: Mapping[str, Any], payload: bytes = b"") -> bytes:
    header_bytes = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()
    if len(header_bytes) > HEADER_BYTES_LIMIT:
        _reject("protocol_error", f"header exceeds {HEADER_BYTES_LIMIT} bytes")
    payload = bytes(payload)
    total = PREFIX_LENGTH + len(header_bytes) + len(payload)
    if total > RECORD_BYTES_LIMIT:
        _reject("protocol_error", f"record exceeds {RECORD_BYTES_LIMIT} bytes")
    return (
        MAGIC
        + _PREFIX.pack(VERSION, kind, 0, len(header_bytes), len(payload))
        + header_bytes
        + payload
    )


def encode_input_pcm(
    *,
    turn: Mapping[str, Any],
    sequence: int,
    sample_offset: int,
    samples: int,
    payload: bytes,
    fmt: Mapping[str, Any] = INPUT_AUDIO_FORMAT,
) -> bytes:
    ref = assert_turn_ref(turn)
    _non_negative_int(sequence, "sequence")
    _non_negative_int(sample_offset, "sampleOffset")
    _non_negative_int(samples, "samples")
    audio = assert_audio_format(fmt)
    payload = bytes(payload)
    if len(payload) != samples * 2 * audio["channels"]:
        _reject(
            "protocol_error",
            f"input PCM payload {len(payload)} does not match {samples} samples",
        )
    return encode_record(
        INPUT_PCM,
        {
            "turn": ref,
            "sequence": sequence,
            "sampleOffset": sample_offset,
            "samples": samples,
            "format": audio,
        },
        payload,
    )


def encode_paired_av(
    *,
    turn: Mapping[str, Any],
    sequence: int,
    pts_us: int,
    duration_us: int,
    sample_offset: int,
    samples: int,
    video_width: int,
    video_height: int,
    video_data: bytes,
    audio: bytes,
    audio_format: Mapping[str, Any] = INPUT_AUDIO_FORMAT,
    padding_samples: int = 0,
    silence: bool = False,
    idle: bool = False,
) -> bytes:
    ref = assert_turn_ref(turn)
    _non_negative_int(sequence, "sequence")
    _non_negative_int(pts_us, "ptsUs")
    _non_negative_int(sample_offset, "sampleOffset")
    _non_negative_int(samples, "samples")
    if not isinstance(duration_us, int) or isinstance(duration_us, bool) or duration_us <= 0:
        _reject("protocol_error", "durationUs must be a positive integer")
    if (
        not isinstance(padding_samples, int)
        or isinstance(padding_samples, bool)
        or padding_samples < 0
        or padding_samples > samples
    ):
        _reject("protocol_error", "paddingSamples must be within [0, samples]")
    fmt = assert_audio_format(audio_format)
    for name, value, limit in (
        ("width", video_width, VIDEO_WIDTH_LIMIT),
        ("height", video_height, VIDEO_HEIGHT_LIMIT),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            or value % 2
            or value > limit
        ):
            _reject(
                "protocol_error",
                f"video must be even-sized within {VIDEO_WIDTH_LIMIT}x{VIDEO_HEIGHT_LIMIT}",
            )
    audio = bytes(audio)
    if len(audio) != samples * 2 * fmt["channels"]:
        _reject(
            "protocol_error",
            f"paired audio {len(audio)} does not match {samples} samples",
        )
    video_data = bytes(video_data)
    expected = i420_byte_length(video_width, video_height)
    if len(video_data) != expected:
        _reject(
            "protocol_error",
            f"I420 payload {len(video_data)} does not match "
            f"{video_width}x{video_height} ({expected})",
        )
    header = {
        "turn": ref,
        "sequence": sequence,
        "ptsUs": pts_us,
        "durationUs": duration_us,
        "sampleOffset": sample_offset,
        "samples": samples,
        "paddingSamples": padding_samples,
        "silence": bool(silence),
        "idle": bool(idle),
        "format": fmt,
        "video": {"format": "i420", "width": video_width, "height": video_height},
        "audioLength": len(audio),
        "videoLength": len(video_data),
    }
    return encode_record(PAIRED_AV, header, audio + video_data)


def encode_media_end(*, turn: Mapping[str, Any], last_sequence: int, reason: str = "finished") -> bytes:
    ref = assert_turn_ref(turn)
    if not isinstance(last_sequence, int) or isinstance(last_sequence, bool) or last_sequence < -1:
        _reject("protocol_error", "lastSequence must be an integer >= -1")
    if not isinstance(reason, str) or not reason or len(reason) > 100:
        _reject("protocol_error", "reason required")
    return encode_record(
        MEDIA_END, {"turn": ref, "lastSequence": last_sequence, "reason": reason}
    )


def decode_record(message: bytes) -> dict:
    raw = bytes(message)
    if len(raw) < PREFIX_LENGTH:
        _reject("protocol_error", "record shorter than DHAV prefix")
    if raw[:4] != MAGIC:
        _reject("protocol_error", "bad DHAV magic")
    version, kind, flags, header_len, payload_len = _PREFIX.unpack(raw[4:PREFIX_LENGTH])
    if version != VERSION:
        _reject("protocol_error", f"unsupported DHAV version {version}")
    if kind not in _RECORD_KINDS:
        _reject("protocol_error", f"unknown record kind {kind}")
    if flags != 0:
        _reject("protocol_error", f"unsupported flags {flags}")
    if header_len > HEADER_BYTES_LIMIT:
        _reject("protocol_error", "headerLength over limit")
    if PREFIX_LENGTH + header_len + payload_len > RECORD_BYTES_LIMIT:
        _reject("protocol_error", "record over limit")
    if len(raw) != PREFIX_LENGTH + header_len + payload_len:
        _reject("protocol_error", "record length mismatch")
    try:
        header = json.loads(raw[PREFIX_LENGTH : PREFIX_LENGTH + header_len].decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        _reject("protocol_error", "malformed JSON header")
    if not isinstance(header, dict):
        _reject("protocol_error", "header must be a JSON object")
    payload = raw[PREFIX_LENGTH + header_len :]
    if kind == INPUT_PCM:
        return _validate_input_pcm(header, payload)
    if kind == PAIRED_AV:
        return _validate_paired_av(header, payload)
    return _validate_media_end(header)


def _validate_input_pcm(header: Mapping[str, Any], payload: bytes) -> dict:
    turn = assert_turn_ref(header.get("turn"))
    for field in ("sequence", "sampleOffset", "samples"):
        _non_negative_int(header.get(field), field)
    fmt = assert_audio_format(header.get("format"))
    if len(payload) != header["samples"] * 2 * fmt["channels"]:
        _reject("protocol_error", "input PCM payload length mismatch")
    return {
        "kind": INPUT_PCM,
        "header": {
            "turn": turn,
            "sequence": header["sequence"],
            "sampleOffset": header["sampleOffset"],
            "samples": header["samples"],
            "format": fmt,
        },
        "payload": payload,
    }


def _validate_paired_av(header: Mapping[str, Any], payload: bytes) -> dict:
    turn = assert_turn_ref(header.get("turn"))
    for field in ("sequence", "ptsUs", "sampleOffset", "samples", "paddingSamples"):
        _non_negative_int(header.get(field), field)
    duration_us = header.get("durationUs")
    if not isinstance(duration_us, int) or isinstance(duration_us, bool) or duration_us <= 0:
        _reject("protocol_error", "missing durationUs")
    if header["paddingSamples"] > header["samples"]:
        _reject("protocol_error", "paddingSamples over samples")
    fmt = assert_audio_format(header.get("format"))
    video = header.get("video")
    if (
        not isinstance(video, Mapping)
        or video.get("format") != "i420"
        or not isinstance(video.get("width"), int)
        or not isinstance(video.get("height"), int)
        or video["width"] <= 0
        or video["height"] <= 0
        or video["width"] % 2
        or video["height"] % 2
        or video["width"] > VIDEO_WIDTH_LIMIT
        or video["height"] > VIDEO_HEIGHT_LIMIT
    ):
        _reject("invalid_media", "bad video descriptor")
    audio_length = header.get("audioLength")
    video_length = header.get("videoLength")
    for value in (audio_length, video_length):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _reject("protocol_error", "missing segment lengths")
    if audio_length != header["samples"] * 2 * fmt["channels"]:
        _reject("protocol_error", "audioLength/samples mismatch")
    if video_length != i420_byte_length(video["width"], video["height"]):
        _reject("invalid_media", "videoLength/size mismatch")
    if len(payload) != audio_length + video_length:
        _reject("protocol_error", "paired payload length mismatch")
    return {
        "kind": PAIRED_AV,
        "header": {
            "turn": turn,
            "sequence": header["sequence"],
            "ptsUs": header["ptsUs"],
            "durationUs": duration_us,
            "sampleOffset": header["sampleOffset"],
            "samples": header["samples"],
            "paddingSamples": header["paddingSamples"],
            "silence": header.get("silence") is True,
            "idle": header.get("idle") is True,
            "format": fmt,
            "video": {"format": "i420", "width": video["width"], "height": video["height"]},
            "audioLength": audio_length,
            "videoLength": video_length,
        },
        "audio": payload[:audio_length],
        "video": payload[audio_length:],
        "payload": payload,
    }


def _validate_media_end(header: Mapping[str, Any]) -> dict:
    turn = assert_turn_ref(header.get("turn"))
    last_sequence = header.get("lastSequence")
    if not isinstance(last_sequence, int) or isinstance(last_sequence, bool) or last_sequence < -1:
        _reject("protocol_error", "missing lastSequence")
    reason = header.get("reason")
    if not isinstance(reason, str) or not reason:
        _reject("protocol_error", "missing reason")
    return {
        "kind": MEDIA_END,
        "header": {"turn": turn, "lastSequence": last_sequence, "reason": reason},
        "payload": b"",
    }


def encode_control(
    *,
    op: str,
    request_id: str,
    turn: Mapping[str, Any] | None = None,
    reason: str | None = None,
    last_audio_sequence: int | None = None,
    total_samples: int | None = None,
    input_format: Mapping[str, Any] | None = None,
) -> str:
    if op not in CONTROL_OPS:
        _reject("protocol_error", f"unknown control op {op}")
    if not isinstance(request_id, str) or not request_id:
        _reject("protocol_error", "control.requestId required")
    frame: dict[str, Any] = {"protocol": "dhav", "version": VERSION, "op": op, "requestId": request_id}
    if turn is not None:
        frame["turn"] = assert_turn_ref(turn)
    if op == OP_FINISH:
        if not isinstance(last_audio_sequence, int) or isinstance(last_audio_sequence, bool) or last_audio_sequence < -1:
            _reject("protocol_error", "finish requires lastAudioSequence >= -1")
        if not isinstance(total_samples, int) or isinstance(total_samples, bool) or total_samples < 0:
            _reject("protocol_error", "finish requires totalSamples")
        frame["lastAudioSequence"] = last_audio_sequence
        frame["totalSamples"] = total_samples
    if op == OP_CANCEL:
        if not isinstance(reason, str):
            _reject("protocol_error", "cancel requires reason")
        frame["reason"] = reason
    if input_format is not None:
        frame["inputFormat"] = assert_audio_format(input_format)
    text = json.dumps(frame, separators=(",", ":"), ensure_ascii=False)
    if len(text.encode()) > CONTROL_BYTES_LIMIT:
        _reject("protocol_error", "control frame over limit")
    return text


def decode_control(text: str) -> dict:
    if not isinstance(text, str):
        _reject("protocol_error", "control frame must be text")
    if len(text.encode()) > CONTROL_BYTES_LIMIT:
        _reject("protocol_error", "control frame over limit")
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        _reject("protocol_error", "malformed control frame")
    if not isinstance(frame, dict) or frame.get("protocol") != "dhav":
        _reject("protocol_error", "control.protocol must be dhav")
    if frame.get("version") != VERSION:
        _reject("protocol_error", f"unsupported control version {frame.get('version')}")
    if frame.get("op") not in CONTROL_OPS:
        _reject("protocol_error", f"unknown control op {frame.get('op')}")
    if not isinstance(frame.get("requestId"), str) or not frame["requestId"]:
        _reject("protocol_error", "control.requestId required")
    if frame.get("turn") is not None:
        frame["turn"] = assert_turn_ref(frame["turn"])
    return frame


def encode_media_event(**event: Any) -> str:
    frame = {"protocol": "dhav", "version": VERSION, **event}
    text = json.dumps(frame, separators=(",", ":"), ensure_ascii=False, default=str)
    if len(text.encode()) > CONTROL_BYTES_LIMIT:
        _reject("protocol_error", "media event over limit")
    return text


def decode_media_event(text: str) -> dict:
    if not isinstance(text, str):
        _reject("protocol_error", "media event must be text")
    if len(text.encode()) > CONTROL_BYTES_LIMIT:
        _reject("protocol_error", "media event over limit")
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        _reject("protocol_error", "malformed media event")
    if (
        not isinstance(frame, dict)
        or frame.get("protocol") != "dhav"
        or frame.get("version") != VERSION
    ):
        _reject("protocol_error", "bad media event envelope")
    if not isinstance(frame.get("event"), str) or not frame["event"]:
        _reject("protocol_error", "media event name required")
    return frame


def same_turn(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    return bool(
        a
        and b
        and a.get("avatarSessionId") == b.get("avatarSessionId")
        and a.get("responseId") == b.get("responseId")
        and a.get("generation") == b.get("generation")
    )
