"""FlashHead DHAV renderer bridge.

Speaks the DHAV v1 renderer protocol used by an external voice-agent gateway,
and produces the media with FlashHead through OpenAvatarChat's
audio-passthrough service.  Nothing here modifies ``src/``; the package only
needs the ``audio_passthrough`` and ``ws_video_client`` handlers to be on the
handler search path.
"""

from . import dhav
from .renderer_client import PassthroughRenderer
from .server import BridgeServer, make_ssl_context, serve
from .session import AvatarSession

__all__ = [
    "AvatarSession",
    "BridgeServer",
    "PassthroughRenderer",
    "dhav",
    "make_ssl_context",
    "serve",
]
