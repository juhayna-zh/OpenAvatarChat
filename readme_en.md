<h1 style='text-align: center; margin-bottom: 1rem'> Open Avatar Chat </h1>

<p align="center">
<strong><a href="README.md">中文</a> | English</strong>
</p>

<p align="center">
<strong>A modular interactive digital human conversation implementation.</strong>
</p>

<p align="center" style="display: flex; flex-direction: row; justify-content: center">
 🤗 <a href="https://huggingface.co/spaces/HumanAIGC-Engineering-Team/open-avatar-chat">Demo</a>&nbsp&nbsp|&nbsp&nbsp<img alt="Static Badge" style="height: 10px;" src="./assets/images/modelscope_logo.png"> <a href="https://www.modelscope.cn/studios/HumanAIGC-Engineering/open-avatar-chat">Demo</a>&nbsp&nbsp|&nbsp&nbsp💬 <a href="https://github.com/HumanAIGC-Engineering/OpenAvatarChat/blob/main/assets/images/OpenAvatarChat.png">WeChat</a>&nbsp&nbsp|&nbsp&nbsp📖 <a href="https://humanaigc-engineering.github.io/OpenAvatarChat/en/">Docs</a>
</p>

> [!NOTE]
> **This is an unofficial downstream fork.** It is not affiliated with, nor endorsed by, HumanAIGC-Engineering.
>
> Upstream is a complete "speech → talking avatar" application. This fork adds a **rendering backend API meant to sit behind an external voice agent**:
> your agent keeps owning the conversation and the TTS, hands the reply's PCM audio to this service, and gets back
> audio and face video that are **already paired** (lip-synced), in real time.
> There is no VAD, ASR, LLM or TTS on this path.
>
> Upstream code is untouched apart from one `idle_inference` switch; everything added here is a self-contained module.
> See [The only change to upstream code](#the-only-change-to-upstream-code).

## What this fork adds

| Module | Size | Purpose |
| --- | --- | --- |
| [`dhav_bridge/`](dhav_bridge/) | 9 files / ~3400 lines / 44 tests | The **DHAV v1** renderer service (`:18910`): binary WebSocket protocol, Bearer auth, turn state machine, paired audio+video downlink |
| [`src/handlers/logic/audio_passthrough/`](src/handlers/logic/audio_passthrough/) | 2 files / 210 lines | Pure rendering passthrough handler: `MIC_AUDIO` in, `AVATAR_AUDIO` out, utterances split by uplink gaps |
| [`src/handlers/client/ws_video_client/`](src/handlers/client/ws_video_client/) | 4 files / 285 lines + a 480-line protocol spec | WebSocket video downlink handler (upstream only ships WebRTC, which a Node gateway cannot peer with) |
| 3 new configs + `idle_inference` | — | `:8284` passthrough rendering; WS-video variants of the full chain on `:8282` / `:8283` |

## Which service should I use?

| Port | What it is | DashScope key | GPU | Input | Output | Docs |
| --- | --- | --- | --- | --- | --- | --- |
| **18910** | DHAV v1 renderer service, for an external agent gateway | not needed | required | PCM audio of the assistant's reply | Paired I420 video + audio, 25 fps | [`dhav_bridge/README.md`](dhav_bridge/README.md) |
| 8284 | Pure rendering passthrough; what 18910 sits on | not needed | required | `MIC_AUDIO` | `AVATAR_AUDIO` + `JVID` video frames | [`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md) |
| 8283 | Full conversation chain + FlashHead | **required** | required | Microphone / text | Avatar video | [upstream docs](https://humanaigc-engineering.github.io/OpenAvatarChat/en/) |
| 8282 | Full conversation chain + LiteAvatar | **required** | required | Microphone / text | Avatar video | [upstream docs](https://humanaigc-engineering.github.io/OpenAvatarChat/en/) |

**Just want a rendering backend for an agent you already have** → port 18910, follow [Quick start A](#quick-start-a-the-dhav-renderer-backend-no-dashscope-key-needed).
**Want a ready-to-use voice-conversation avatar** → ports 8282/8283, follow [Quick start B](#quick-start-b-the-upstream-full-conversation-app).

## Quick start A: the DHAV renderer backend (no DashScope key needed)

```bash
# 1. Clone with submodules (every avatar implementation lives in a submodule)
git clone --recursive <this repo's URL>
cd OpenAvatarChat
# If you already cloned it:
git submodule update --init --recursive --depth 1

# 2. Install dependencies. install.py itself is meant to run under uv run;
#    this is the normal upstream installation flow.
uv run install.py --config config/chat_with_flashhead_passthrough.yaml

# 3. Download models (SoulX-FlashHead-1_3B + wav2vec2-base-960h)
.venv/bin/python scripts/download_models.py --config config/chat_with_flashhead_passthrough.yaml

# 4. Generate a self-signed certificate WITH SANs. The bundled
#    scripts/create_ssl_certs.sh will not do: its openssl req has neither
#    -subj nor -addext, so it blocks on interactive prompts and produces a
#    certificate with no SANs, which always fails hostname verification.
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout ssl_certs/localhost.key -out ssl_certs/localhost.crt \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

# 5. Start the renderer (:8284, ~40 s to load the model)
.venv/bin/python src/demo.py \
  --config config/chat_with_flashhead_passthrough.yaml --host 127.0.0.1

# 6. In a second shell, start the bridge (:18910). Loopback needs no token.
.venv/bin/python -m dhav_bridge --video-size 256

# 7. End-to-end self-check
.venv/bin/python -m dhav_bridge.smoke_client \
  --base-url http://127.0.0.1:18910 \
  --input /path/to/reply.wav --out ~/dhav-check --realtime
```

If it works, the last lines print `status ok`, `publish_rate_hz` ≈ 25 and `realtime_factor` ≈ 1.0,
and `~/dhav-check/` contains `output.wav`, `frames/*.png` and `report.json` — ready to listen to and look at.

> [!WARNING]
> **After installing, always use `.venv/bin/python`, never `uv run`.**
> `uv run` performs an implicit sync against the lockfile and **uninstalls** the handler dependencies that
> `install.py` put in via `uv pip install` (funasr, dashscope, `transformers==4.44.2`, …), because they are not
> in the root `pyproject.toml`. The symptom is a handler silently missing, or an import error, on the next start.
> Step 2's `uv run install.py` is the one exception — that is the installer itself.

For a gateway on another machine you must supply a token (without one the bridge refuses to bind a
non-loopback address and exits with code 2):

```bash
mkdir -p ~/.config/dhav-bridge && chmod 700 ~/.config/dhav-bridge
openssl rand -hex 32 > ~/.config/dhav-bridge/token && chmod 600 ~/.config/dhav-bridge/token

DHAV_AUTH_TOKEN=$(cat ~/.config/dhav-bridge/token) \
  .venv/bin/python -m dhav_bridge --host 0.0.0.0 --port 18910 --video-size 256
```

An SSH tunnel is preferable to opening a port: on the gateway side run
`ssh -N -L 18910:127.0.0.1:18910 <gpu-host>` and then just connect to `http://127.0.0.1:18910` —
no firewall change, no TLS to configure, and the token never crosses a wire.

The protocol, endpoints, authentication, turn lifecycle, measured numbers and all 24 pitfalls are in
[`dhav_bridge/README.md`](dhav_bridge/README.md).

## Quick start B: the upstream full conversation app

```bash
git submodule update --init --recursive --depth 1
uv run install.py --config config/chat_with_liteavatar_ws_video.yaml
.venv/bin/python scripts/download_models.py --handler liteavatar

export DASHSCOPE_API_KEY=sk-...   # used by both LLM (qwen-plus) and TTS (cosyvoice-v1)
.venv/bin/python src/demo.py --config config/chat_with_liteavatar_ws_video.yaml --host 127.0.0.1
```

The first session on this chain needs ~75 s of warm-up (LiteAvatar), and it takes ~190 s from launch to
`/readiness` returning 200. For the full upstream walkthrough see
[Getting Started](https://humanaigc-engineering.github.io/OpenAvatarChat/en/getting-started/).

> [!WARNING]
> `:8282` / `:8283` / `:8284` have **no authentication** — no token, no Origin check, no allow-list.
> Anyone who can reach the port can open a session and spend your DashScope quota. Keep them bound to `127.0.0.1`.
> Only `:18910` has Bearer-token auth.

## The three new configs

| CONFIG | Port | Handler chain |
| --- | --- | --- |
| `chat_with_flashhead_passthrough.yaml` | 8284 | WsVideoClient → InterruptHandler → **AudioPassthrough** → FlashHead |
| `chat_with_liteavatar_ws_video.yaml` | 8282 | WsVideoClient → InterruptHandler → SileroVad → SenseVoice → CosyVoice → LLM → LiteAvatar |
| `chat_with_flashhead_ws_video.yaml` | 8283 | Same, with FlashHead instead of LiteAvatar |

The last two are upstream's `chat_with_openai_compatible_bailian_cosyvoice.yaml` with `RtcClient` swapped for `WsVideoClient`.

Knobs worth knowing about:

- `FlashHead.idle_inference: false` (off only in the passthrough config) — idle animation inference holds the GPU
  and `_inference_lock` continuously, delaying speech inference by 2–5 s. With it off, silence degrades to a static
  reference frame, which is fine because the DHAV bridge only sends media inside a turn and never uses idle frames.
- `AudioPassthrough.gap_seconds: 0.4` — how long an uplink pause counts as end-of-utterance; it is also the extra
  latency appended to every utterance. Note that splitting looks at **whether you stopped sending packets**, not at
  whether the audio content contains silence.
- `service.concurrent_limit: 1` — FlashHead on a single GPU can only render one session at a time.

## The only change to upstream code

3 files, `+28/−5` (9 of those lines are the change-notice comments), all under `src/handlers/avatar/flashhead/`, adding exactly one config option:

| File | Change |
| --- | --- |
| `flashhead_config.py` | New `idle_inference: bool = Field(default=True, ...)` |
| `avatar_handler_flashhead.py` | Passes that value into `FlashHeadProcessor(...)` |
| `flashhead_processor.py` | Creation and start of the idle-inference thread now gated on that switch |

**The default `True` is upstream's original behaviour**, character for character: any config that does not set this
key is unaffected, and only `chat_with_flashhead_passthrough.yaml` turns it off. All three files carry a change
notice at the top as required by Apache-2.0 §4(b), and every modified hunk has a trailing `# [fork]` marker, so
`git diff upstream/main` shows the whole story at a glance.

Everything else added here is a purely additive, self-contained directory that does not intersect upstream files.

## Tests

44 unit tests, needing neither a GPU nor a network (the renderer and both DHAV channels are stubbed):

```bash
.venv/bin/python -m pytest dhav_bridge -q
# If pytest is not installed:
.venv/bin/python -m unittest dhav_bridge.test_dhav_bridge -v
```

`test_dhav_bridge.py` pins the byte layout of the wire format, so a change to the codec fails immediately rather
than leaving some gateway silently unable to connect.

## Known pitfalls (top 6)

1. **The uplink is really 16 kHz, not the 24 kHz the protocol declares.** Upstream hardcodes the `MIC_AUDIO` sample
   rate to 16000 and ignores the declared value; the bridge resamples internally. **Send 24 kHz as the protocol
   says** and do not try to "fix" it — sending 16 kHz yourself yields speech 1.5× too fast and 1.5× too low in pitch.
2. **The renderer's downlink is never quiet.** FlashHead's frame collector is a strict 25 fps metronome: with
   nothing to say it emits a still frame plus all-zero audio, until the session ends. So "how long since I last got
   data" can never be used to detect end-of-utterance; the bridge relies on upstream's structural
   `ChatSignal stream_end` marker.
3. **`GET /readiness` returning 200 does not mean video will come out.** It only reflects that handlers registered,
   not inference throughput. It keeps returning 200 while the renderer is wedged.
4. **`uv run` uninstalls the handler dependencies** — see the WARNING above.
5. **The certificate must carry SANs**; the bundled generation script does not produce them — see step 4 of Quick start A.
6. **INFO-level logs print the DashScope key in plaintext** (handler registration dumps the fully resolved config).
   The passthrough config has no DashScope component so it does not matter there, but do not ship INFO logs from the
   full chain.

The other 18 (including triage criteria and measured thresholds) are in
[`dhav_bridge/README.md`](dhav_bridge/README.md#pitfalls) and
[`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md).

---

## Upstream project

Everything below is upstream OpenAvatarChat's own content.

### 💡 Core Highlights

- **Multimodal Interaction**: Supports text, audio, video and other interaction methods for natural human-machine dialogue
- **Modular Architecture**: Highly modular design with flexible ASR, LLM, TTS, and Avatar component replacement
- **Diverse Avatar Options**: Supports LiteAvatar, LAM, MuseTalk, FlashHead and other digital human technologies
- **Low Latency**: Optimized through VAD detection, audio buffering, and frame rate control with ~2.2s average response time

### 📢 News

- [2026.04] ⭐️⭐️⭐️ Version 0.6.0 Released:
  - Architecture refactored with frontend/backend separation: [OpenAvatarChat-WebUI](https://github.com/HumanAIGC-Engineering/OpenAvatarChat-WebUI)
  - All avatars now support manual interrupt and duplex interrupt modes
  - Optimized installation, deployment, and model download workflow
  - Integrated [SoulX-FlashHead](https://github.com/Soul-AILab/SoulX-FlashHead) diffusion-based real-time streaming talking head
- [2025.08.19] ⭐️⭐️⭐️ Version 0.5.1 Released:
  - LiteAvatar multi-session support
  - Added Qwen-Omni multimodal model support

> 📋 [Full Release Notes](https://humanaigc-engineering.github.io/OpenAvatarChat/en/releases/release-notes)

### Preset Modes

| CONFIG Name | ASR | LLM | TTS | AVATAR |
|---|---|:-:|:-:|---|
| chat_with_lam.yaml | SenseVoice | API | API | LAM |
| chat_with_qwen_omni.yaml | Qwen-Omni | Qwen-Omni | Qwen-Omni | lite-avatar |
| chat_with_openai_compatible_bailian_cosyvoice.yaml | SenseVoice | API | API | lite-avatar |
| chat_with_openai_compatible_bailian_cosyvoice_flashhead.yaml | SenseVoice | API | API | FlashHead |
| chat_with_openai_compatible_bailian_cosyvoice_flashhead_duplex.yaml | SenseVoice | API | API | FlashHead (Duplex) |
| chat_with_openai_compatible_bailian_cosyvoice_flashhead_duplex_agent.yaml | SenseVoice | **Agent** | API | FlashHead (Duplex+Agent) Beta |

> 📖 [View all preset modes](https://humanaigc-engineering.github.io/OpenAvatarChat/en/reference/preset-modes)

### 🧪 Beta Features: Chat Agent Mode (OpenClaw Integration)

> [!WARNING]
> This feature is currently in **Beta**. APIs and configuration formats may change at any time.

Chat Agent mode replaces the traditional LLM Handler with a multi-turn tool-calling Agent, providing:

- **Tool Calling**: Invoke tools across multiple turns (get time, system info, etc.)
- **Persona & Long-term Memory**: Persistent persona through OpenClaw's Agent Profile
- **Context Compression**: Automatically compresses long conversation history
- **Background Task Collaboration**: Execute complex tasks via OpenClaw in the background
- **Visual Perception**: Camera input processing via PerceptionAgent

> 📖 [Full Chat Agent documentation](https://humanaigc-engineering.github.io/OpenAvatarChat/en/beta/chat-agent)

### Component Dependencies

| Type | Open Source Project | GitHub Link | Model Link |
|----------|-------------------------------------|---|---|
| RTC      | HumanAIGC-Engineering/gradio-webrtc |[<img src="https://img.shields.io/badge/github-white?logo=github&logoColor=black"/>](https://github.com/HumanAIGC-Engineering/gradio-webrtc)||
| WebUI      | HumanAIGC-Engineering/OpenAvatarChat-WebUI |[<img src="https://img.shields.io/badge/github-white?logo=github&logoColor=black"/>](https://github.com/HumanAIGC-Engineering/OpenAvatarChat-WebUI)||
| VAD      | snakers4/silero-vad                 |[<img src="https://img.shields.io/badge/github-white?logo=github&logoColor=black"/>](https://github.com/snakers4/silero-vad)||
| Avatar   | HumanAIGC/lite-avatar               |[<img src="https://img.shields.io/badge/github-white?logo=github&logoColor=black"/>](https://github.com/HumanAIGC/lite-avatar)||
| TTS      | FunAudioLLM/CosyVoice               |[<img src="https://img.shields.io/badge/github-white?logo=github&logoColor=black"/>](https://github.com/FunAudioLLM/CosyVoice)||
|Avatar|aigc3d/LAM_Audio2Expression|[<img src="https://img.shields.io/badge/github-white?logo=github&logoColor=black"/>](https://github.com/aigc3d/LAM_Audio2Expression)|[🤗](https://huggingface.co/3DAIGC/LAM_audio2exp)|
||facebook/wav2vec2-base-960h||[🤗](https://huggingface.co/facebook/wav2vec2-base-960h)&nbsp;&nbsp;[<img src="./assets/images/modelscope_logo.png" width="20px"></img>](https://modelscope.cn/models/AI-ModelScope/wav2vec2-base-960h)|
|Avatar|TMElyralab/MuseTalk|[<img src="https://img.shields.io/badge/github-white?logo=github&logoColor=black"/>](https://github.com/TMElyralab/MuseTalk)||
|Avatar|Soul-AILab/SoulX-FlashHead|[<img src="https://img.shields.io/badge/github-white?logo=github&logoColor=black"/>](https://github.com/Soul-AILab/SoulX-FlashHead)|[🤗](https://huggingface.co/Soul-AILab/SoulX-FlashHead-1_3B)|

### Community (upstream)

* WeChat Group

<img alt="community_wechat.png" height="200" src="https://raw.githubusercontent.com/HumanAIGC-Engineering/OpenAvatarChat/main/assets/images/OpenAvatarChat.png" width="200"/>

* Official video tutorial: [Bilibili](https://www.bilibili.com/video/BV1sv8QzLEC2)
* 🚨 [FAQ](https://humanaigc-engineering.github.io/OpenAvatarChat/en/community/faq)

### Citation

If you found OpenAvatarChat helpful in your research/project, please consider giving **upstream** a Star⭐ and citation✏️

```
@software{avatarchat2025,
  author = {Gang Cheng, Tao Chen, Feng Wang, Binchao Huang, Hui Xu, Guanqiao He, Yi Lu, Shengyin Tan},
  title = {OpenAvatarChat},
  year = {2025},
  publisher = {GitHub},
  url = {https://github.com/HumanAIGC-Engineering/OpenAvatarChat}
}
```

## Licence and attribution

This repository is distributed under **Apache-2.0** (see [`LICENSE`](LICENSE)) and is an **unofficial downstream
fork** of [HumanAIGC-Engineering/OpenAvatarChat](https://github.com/HumanAIGC-Engineering/OpenAvatarChat) v0.6.0.

- Copyright in the upstream code remains with its original authors.
- Copyright in what this fork adds (`dhav_bridge/`, `audio_passthrough/`, `ws_video_client/`, the 3 new configs)
  belongs to Haina Zhu, likewise distributed under Apache-2.0.
- Changes to upstream files are limited to the 3 files under `src/handlers/avatar/flashhead/`, each carrying a
  change notice at the top as required by Apache-2.0 §4(b).

This project is not affiliated with, nor endorsed by, HumanAIGC-Engineering. Apache-2.0 §6 grants no trademark
rights; the name and marks "OpenAvatarChat" remain the property of their owner.
