"""Session delegate that adds a server-rendered video downlink to the WS client."""
import asyncio
from typing import Optional
from uuid import uuid4

import cv2
import numpy as np
from loguru import logger

from chat_engine.data_models.chat_data.chat_data_model import ChatData
from chat_engine.data_models.chat_data_type import ChatDataType
from chat_engine.data_models.engine_channel_type import EngineChannelType

from client.ws_client.ws_input_delegate import WsInputSessionDelegate

from .ws_video_protocol import VideoCodec, pack_frame

VIDEO_SUBSCRIPTION = "avatar_video"


class WsVideoSessionDelegate(WsInputSessionDelegate):
    """WsInputSessionDelegate plus an `avatar_video` downlink.

    Video frames are sent as self-describing binary packets rather than the
    "announce in JSON, then N bare binary chunks" convention the audio and
    motion-data downlinks use. Those two already race for the send lock, and a
    third segmented producer would make the interleaving undecodable on the
    client; a magic-prefixed packet can be demultiplexed on arrival instead.
    """

    AVAILABLE_SUBSCRIPTIONS = WsInputSessionDelegate.AVAILABLE_SUBSCRIPTIONS | {
        VIDEO_SUBSCRIPTION
    }

    def __init__(self, heartbeat_timeout: float = 30.0):
        super().__init__(heartbeat_timeout=heartbeat_timeout)
        self.jpeg_quality: int = 85
        self.video_fps: int = 25
        self.output_queues[EngineChannelType.VIDEO] = asyncio.Queue()
        self._video_frame_index = 0
        self._video_welcome_sent = False

    def configure_video(self, jpeg_quality: int, video_fps: int):
        self.jpeg_quality = int(jpeg_quality)
        self.video_fps = int(video_fps)

    async def _serve_primary_connection(self, info) -> bool:
        self._video_welcome_sent = False
        self._video_frame_index = 0
        video_task = asyncio.create_task(self._ws_video_output_task())
        try:
            return await super()._serve_primary_connection(info)
        finally:
            video_task.cancel()
            await asyncio.gather(video_task, return_exceptions=True)

    async def _ws_video_output_task(self):
        logger.info(f"Video output task started for session {self.session_id}")

        while not self.quit.is_set():
            try:
                chat_data = await asyncio.wait_for(
                    self.get_data(EngineChannelType.VIDEO),
                    timeout=0.1
                )

                if chat_data is None:
                    continue

                if chat_data.type == ChatDataType.AVATAR_VIDEO:
                    await self._send_avatar_video(chat_data)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error in video output task for session {self.session_id}: {e}")
                break

        logger.info(f"Video output task ended for session {self.session_id}")

    async def _send_avatar_video(self, chat_data: ChatData):
        if VIDEO_SUBSCRIPTION not in self.subscriptions:
            return

        frames = chat_data.data.get_main_data()
        if frames is None:
            return

        video_np = np.asarray(frames)
        if video_np.ndim == 3:
            video_np = video_np[np.newaxis, ...]
        if video_np.ndim != 4 or video_np.shape[0] == 0 or video_np.shape[-1] != 3:
            logger.warning(f"Unexpected avatar video shape {video_np.shape}, dropping batch.")
            return

        stream_key = self._video_stream_key(chat_data)
        if not self._video_welcome_sent:
            logger.info(
                f"Avatar video stream started: shape={video_np.shape} "
                f"dtype={video_np.dtype} stream_key={stream_key}"
            )
            await self._broadcast_json({
                "header": {"name": "AvatarVideoWelcome", "request_id": str(uuid4())},
                "payload": {
                    "codec": VideoCodec.JPEG.name,
                    "width": int(video_np.shape[2]),
                    "height": int(video_np.shape[1]),
                    "fps": self.video_fps,
                    "stream_key": stream_key,
                },
            })
            self._video_welcome_sent = True

        encoded = await asyncio.to_thread(self._encode_frames, video_np)
        timestamp_ms = self._timestamp_ms(chat_data)
        for payload in encoded:
            await self._broadcast_bytes(pack_frame(
                codec=VideoCodec.JPEG,
                width=int(video_np.shape[2]),
                height=int(video_np.shape[1]),
                frame_index=self._video_frame_index,
                batch_size=int(video_np.shape[0]),
                timestamp_ms=timestamp_ms,
                payload=payload,
            ))
            self._video_frame_index += 1

        if chat_data.is_last_data:
            await self._broadcast_json({
                "header": {"name": "AvatarVideoEnd", "request_id": str(uuid4())},
                "payload": {"stream_key": stream_key},
            })
            self._video_welcome_sent = False

    def _encode_frames(self, video_np: np.ndarray):
        if video_np.dtype != np.uint8:
            video_np = np.clip(video_np, 0, 255).astype(np.uint8)
        params = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        encoded = []
        for frame in video_np:
            # LiteAvatar emits BGR24 (liteavatar_worker.py -> to_ndarray(format="bgr24")),
            # which is the layout cv2.imencode already expects.
            ok, buf = cv2.imencode(".jpg", frame, params)
            if ok:
                encoded.append(buf.tobytes())
        return encoded

    @staticmethod
    def _video_stream_key(chat_data: ChatData) -> Optional[str]:
        return chat_data.stream_id.stream_key_str if chat_data.stream_id is not None else None

    @staticmethod
    def _timestamp_ms(chat_data: ChatData) -> int:
        timestamp = getattr(chat_data, "timestamp", None)
        if not timestamp:
            return 0
        value, base = timestamp
        return int(value) * 1000 // int(base) if base else 0
