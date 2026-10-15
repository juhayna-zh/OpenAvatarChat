"""WebSocket client handler that also streams server-rendered avatar video.

A thin extension of the stock `ws_client` handler: same `/ws/session/{id}`
route and same uplink protocol, with `AVATAR_VIDEO` added to the downlink.
Everything lives in this directory; no existing file is modified.

Note on the aliased import: the engine's `import_class()` picks the first
HandlerBase subclass it finds in `inspect.getmembers()` order, which is
alphabetical. Importing `WsClientHandler` by name would sort before
`WsVideoClientHandler` and the engine would instantiate the wrong class.
"""
from typing import cast

from pydantic import Field

from chat_engine.common.client_handler_base import ClientSessionDelegate
from chat_engine.common.handler_base import HandlerBaseInfo, HandlerDataInfo
from chat_engine.contexts.handler_context import HandlerContext
from chat_engine.contexts.session_context import SessionContext
from chat_engine.data_models.chat_data_type import ChatDataType

from client.ws_client import ws_client_handler as _ws_client

from .ws_video_session import WsVideoSessionDelegate


class WsVideoClientConfigModel(_ws_client.WsClientConfigModel):
    jpeg_quality: int = Field(default=85, ge=1, le=100, description="JPEG quality for avatar frames")
    video_fps: int = Field(default=25, ge=1, description="Advertised avatar frame rate; should match the avatar handler's fps")


class WsVideoClientHandler(_ws_client.WsClientHandler):

    def get_handler_info(self) -> HandlerBaseInfo:
        return HandlerBaseInfo(
            config_model=WsVideoClientConfigModel,
            client_session_delegate_class=WsVideoSessionDelegate,
        )

    def create_handler_detail(self, _session_context: SessionContext, _handler_context: HandlerContext):
        detail = super().create_handler_detail(_session_context, _handler_context)
        detail.inputs[ChatDataType.AVATAR_VIDEO] = HandlerDataInfo(
            type=ChatDataType.AVATAR_VIDEO,
            input_priority=-1,
        )
        return detail

    def on_setup_session_delegate(self, session_context: SessionContext, handler_context: HandlerContext,
                                  session_delegate: ClientSessionDelegate):
        super().on_setup_session_delegate(session_context, handler_context, session_delegate)
        delegate = cast(WsVideoSessionDelegate, session_delegate)
        delegate.configure_video(self.handler_config.jpeg_quality, self.handler_config.video_fps)

    def build_frontend_init_config(self):
        config = super().build_frontend_init_config()
        config["chat_mode"] = "ws_video"
        config["video"] = {
            "codec": "JPEG",
            "fps": self.handler_config.video_fps,
            "subscription": "avatar_video",
        }
        return config
