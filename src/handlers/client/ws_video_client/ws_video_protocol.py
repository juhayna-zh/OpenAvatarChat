"""Self-describing binary framing for server-rendered avatar video.

Stdlib-only on purpose: this module is the wire contract shared with
non-Python clients, so it must be liftable out of the project and re-readable
without pulling in any of the chat engine. PROTOCOL.md describes the same
bytes for implementers in other languages.
"""
import struct
from enum import IntEnum
from typing import NamedTuple

MAGIC = b"JVID"
VERSION = 1

# magic(4) version(1) codec(1) width(2) height(2) frame_index(4)
# batch_size(4) timestamp_ms(8) data_size(4) reserved(2)
_HEADER = struct.Struct("<4sBBHHIIQI2x")
HEADER_SIZE = _HEADER.size


class VideoCodec(IntEnum):
    JPEG = 0


class VideoFrameHeader(NamedTuple):
    version: int
    codec: int
    width: int
    height: int
    frame_index: int
    batch_size: int
    timestamp_ms: int
    data_size: int


def pack_frame(codec: VideoCodec, width: int, height: int, frame_index: int,
               batch_size: int, timestamp_ms: int, payload: bytes) -> bytes:
    header = _HEADER.pack(
        MAGIC, VERSION, int(codec), width, height,
        frame_index, batch_size, timestamp_ms, len(payload),
    )
    return header + payload


def unpack_frame(packet: bytes) -> tuple[VideoFrameHeader, bytes]:
    """Split one WebSocket binary message into its header and JPEG payload."""
    header = VideoFrameHeader(*_HEADER.unpack(packet[:HEADER_SIZE])[1:])
    return header, packet[HEADER_SIZE:HEADER_SIZE + header.data_size]


def is_video_packet(packet: bytes) -> bool:
    return len(packet) >= HEADER_SIZE and packet[:4] == MAGIC
