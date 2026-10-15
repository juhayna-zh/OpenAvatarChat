import threading
from typing import Dict, Optional, cast

import numpy as np
from loguru import logger
from pydantic import BaseModel, Field

from chat_engine.common.handler_base import (
    HandlerBase, HandlerBaseInfo, HandlerDataInfo, HandlerDetail,
)
from chat_engine.contexts.handler_context import HandlerContext
from chat_engine.contexts.session_context import SessionContext
from chat_engine.data_models.chat_data.chat_data_model import ChatData
from chat_engine.data_models.chat_data_type import ChatDataType
from chat_engine.data_models.chat_engine_config_data import (
    ChatEngineConfigModel, HandlerBaseConfigModel,
)
from chat_engine.data_models.chat_stream_config import ChatStreamConfig
from chat_engine.data_models.runtime_data.data_bundle import (
    DataBundle, DataBundleDefinition, DataBundleEntry,
)

END_FRAME_MS = 10


class AudioPassthroughConfig(HandlerBaseConfigModel, BaseModel):
    sample_rate: int = Field(
        default=24000,
        description="Sample rate declared on the AVATAR_AUDIO stream this handler emits. "
                    "Uplink audio arriving at a different rate is resampled to this.",
    )
    gap_seconds: float = Field(
        default=0.4,
        description="Uplink silence (no audio packet at all) that closes the current "
                    "utterance. The renderer only flushes its tail on stream end, so "
                    "this is also the extra latency at the end of each utterance.",
    )


class PassthroughContext(HandlerContext):
    def __init__(self, session_id: str):
        super().__init__(session_id)
        self.stream_open = False
        self.lock = threading.Lock()
        self.gap_timer: Optional[threading.Timer] = None
        self.utterances = 0
        self.chunks = 0


class HandlerAudioPassthrough(HandlerBase):
    """MIC_AUDIO in, AVATAR_AUDIO out, one stream per utterance burst."""

    def __init__(self):
        super().__init__()
        self.sample_rate = 24000
        self.gap_seconds = 0.4

    def get_handler_info(self) -> HandlerBaseInfo:
        return HandlerBaseInfo(config_model=AudioPassthroughConfig)

    def get_handler_detail(self, session_context: SessionContext,
                           context: HandlerContext) -> HandlerDetail:
        definition = DataBundleDefinition()
        definition.add_entry(DataBundleEntry.create_audio_entry(
            "passthrough_audio", 1, self.sample_rate))
        return HandlerDetail(
            # MIC_AUDIO is the raw client uplink; HUMAN_AUDIO is what the VAD
            # emits after endpointing, which is exactly what we must not use.
            inputs=[HandlerDataInfo(type=ChatDataType.MIC_AUDIO)],
            outputs=[HandlerDataInfo(type=ChatDataType.AVATAR_AUDIO,
                                     definition=definition)],
        )

    def load(self, engine_config: ChatEngineConfigModel,
             handler_config: Optional[BaseModel] = None):
        config = cast(AudioPassthroughConfig, handler_config)
        self.sample_rate = config.sample_rate
        self.gap_seconds = config.gap_seconds

    def create_context(self, session_context, handler_config=None) -> HandlerContext:
        return PassthroughContext(session_context.session_info.session_id)

    def start_context(self, session_context, context: HandlerContext):
        pass

    def handle(self, context: HandlerContext, inputs: ChatData,
               output_definitions: Dict[ChatDataType, HandlerDataInfo]):
        if inputs.type != ChatDataType.MIC_AUDIO:
            return
        context = cast(PassthroughContext, context)

        audio = inputs.data.get_main_data()
        if audio is None or np.asarray(audio).size == 0:
            return
        audio = np.asarray(audio)
        if np.issubdtype(audio.dtype, np.integer):
            # Uplink PCM arrives as int16; AVATAR_AUDIO is float32 in [-1, 1].
            # Skipping this makes every sample clip to a full-scale square wave.
            audio = audio.astype(np.float32) / 32768.0
        elif audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        audio = audio.squeeze()

        entry = inputs.data.get_main_definition_entry()
        in_rate = entry.sample_rate if entry is not None else self.sample_rate
        if in_rate != self.sample_rate:
            import librosa
            audio = librosa.resample(audio, orig_sr=in_rate, target_sr=self.sample_rate)

        with context.lock:
            if not context.stream_open:
                self._open_stream(context)
            if not context.stream_open:
                return
            audio = audio[np.newaxis, ...]
            bundle = DataBundle(self._definition(context))
            bundle.set_main_data(audio)
            context.submit_data(bundle)
            context.chunks += 1
            self._arm_gap_timer(context)
            if inputs.is_last_data:
                self._close_utterance(context, "uplink stream ended")

    def _definition(self, context: PassthroughContext) -> DataBundleDefinition:
        streamer = context.data_submitter.get_streamer(ChatDataType.AVATAR_AUDIO)
        return streamer.data_definition

    def _open_stream(self, context: PassthroughContext):
        streamer = context.data_submitter.get_streamer(ChatDataType.AVATAR_AUDIO)
        if streamer is None:
            logger.error("AudioPassthrough: no AVATAR_AUDIO streamer; is an avatar "
                         "handler configured?")
            return
        # Deliberately no `sources=`: the uplink MIC_AUDIO stream is one
        # continuous stream for the whole session while we open one output
        # stream per utterance. Linking them would drop the input stream's
        # refcount to zero when the first utterance finishes and let the
        # stream manager recycle a stream that is still receiving audio.
        streamer.new_stream(sources=[], name="audio_passthrough",
                            config=ChatStreamConfig(cancelable=True))
        context.stream_open = True
        context.utterances += 1
        logger.info(f"AudioPassthrough: utterance #{context.utterances} started "
                    f"(session {context.session_id})")

    def _arm_gap_timer(self, context: PassthroughContext):
        # handle() runs on an engine worker thread, so asyncio timers are not
        # available here; threading.Timer is, and submit_data is safe off-loop
        # (the TTS handlers call it from their callback threads).
        if context.gap_timer is not None:
            context.gap_timer.cancel()
        timer = threading.Timer(self.gap_seconds, self._on_gap_timeout,
                                args=(context,))
        timer.daemon = True
        context.gap_timer = timer
        timer.start()

    def _on_gap_timeout(self, context: PassthroughContext):
        with context.lock:
            self._close_utterance(context, f"{self.gap_seconds}s of uplink silence")

    def _close_utterance(self, context: PassthroughContext, reason: str):
        if context.gap_timer is not None:
            context.gap_timer.cancel()
            context.gap_timer = None
        if not context.stream_open:
            return
        context.stream_open = False
        # At least 2 samples: avatar handlers squeeze() the array before
        # resampling, so a 1-sample frame collapses to 0-d and librosa rejects it.
        n = max(2, int(self.sample_rate * END_FRAME_MS / 1000))
        end = np.zeros((1, n), dtype=np.float32)
        bundle = DataBundle(self._definition(context))
        bundle.set_main_data(end)
        context.submit_data(bundle, finish_stream=True)
        logger.info(f"AudioPassthrough: utterance #{context.utterances} ended "
                    f"after {context.chunks} chunks ({reason}); "
                    f"session {context.session_id}")
        context.chunks = 0

    def destroy_context(self, context: HandlerContext):
        context = cast(PassthroughContext, context)
        with context.lock:
            if context.gap_timer is not None:
                context.gap_timer.cancel()
                context.gap_timer = None
            if context.stream_open:
                try:
                    self._close_utterance(context, "session teardown")
                except Exception as e:
                    logger.warning(f"AudioPassthrough: failed to flush on destroy: {e}")
