"""Audio-in / video-out passthrough for the avatar renderer.

Feeds the client's uplink speech audio straight into whatever avatar handler
is configured (FlashHead, LiteAvatar, ...), skipping VAD, ASR, LLM and TTS.
The avatar's video and its aligned playback audio come back over the normal
ws_video downlink, so a caller that already owns its own dialogue and TTS
stack (e.g. an external voice-agent gateway) can use the renderer as a pure
audio-to-video service.

Utterance boundaries are derived from the *arrival* of uplink audio, not from
its content: a gap of `gap_seconds` with no audio packet closes the current
AVATAR_AUDIO stream (which is what makes the renderer flush its tail), and the
next packet opens a fresh one. This needs no protocol change and no VAD.

Everything lives in this directory; no existing file is modified.
"""
from .passthrough_handler import AudioPassthroughConfig, HandlerAudioPassthrough

__all__ = ["AudioPassthroughConfig", "HandlerAudioPassthrough"]
