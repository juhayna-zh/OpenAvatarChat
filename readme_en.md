<h1 style='text-align: center; margin-bottom: 1rem'> Open Avatar Chat </h1>

<p align="center">
<strong><a href="README.md">中文</a> | English</strong>
</p>

<p align="center">
<strong>Turn audio into a talking face.</strong>
</p>

An **unofficial downstream fork** of [HumanAIGC-Engineering/OpenAvatarChat](https://github.com/HumanAIGC-Engineering/OpenAvatarChat). Not affiliated with, nor endorsed by, HumanAIGC-Engineering.

Upstream is a complete "speech → talking avatar" application. This fork adds a **rendering backend API that sits behind an external voice agent**: your agent keeps owning the conversation and the TTS, hands the reply's PCM audio to this service, and gets back audio and face video that are **already paired** (lip-synced), in real time. There is no VAD, ASR, LLM or TTS on this path — they all stay on your side.

Upstream code is untouched apart from one `idle_inference` switch; everything added here is a self-contained module.

## Quick start

Needs a GPU. Does not need a DashScope key.

```bash
git clone --recursive https://github.com/juhayna-zh/OpenAvatarChat.git
cd OpenAvatarChat

uv run install.py --config config/chat_with_flashhead_passthrough.yaml
.venv/bin/python scripts/download_models.py --config config/chat_with_flashhead_passthrough.yaml

openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout ssl_certs/localhost.key -out ssl_certs/localhost.crt \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

# The renderer, on :8284; ~40 s to load the model
.venv/bin/python src/demo.py --config config/chat_with_flashhead_passthrough.yaml --host 127.0.0.1
```

In a second shell, start the bridge and run the self-check:

```bash
.venv/bin/python -m dhav_bridge --video-size 256

.venv/bin/python -m dhav_bridge.smoke_client --base-url http://127.0.0.1:18910 \
  --input /path/to/reply.wav --out ~/dhav-check --realtime
```

If it works, it prints `status ok`, `publish_rate_hz` ≈ 25 and `realtime_factor` ≈ 1.0, and `~/dhav-check/` contains `output.wav` and `frames/*.png` — ready to listen to and look at.

Two pitfalls, up front:

- **After installing, always use `.venv/bin/python`, never `uv run`** (the `uv run install.py` above is the one exception — that is the installer itself). `uv run` performs an implicit sync against the lockfile and **uninstalls** the handler dependencies that `install.py` put in via `uv pip install`, because they are not in the root `pyproject.toml`. The symptom is a handler silently missing, or an import error, on the next start.
- **The certificate must carry SANs**, so do not use the bundled `scripts/create_ssl_certs.sh`: its `openssl req` has neither `-subj` nor `-addext`, so it blocks on interactive prompts and produces a certificate with no SANs, which always fails hostname verification.

For a gateway on another machine you must supply a token (without one the bridge refuses to bind a non-loopback address and exits with code 2):

```bash
mkdir -p ~/.config/dhav-bridge && chmod 700 ~/.config/dhav-bridge
openssl rand -hex 32 > ~/.config/dhav-bridge/token && chmod 600 ~/.config/dhav-bridge/token
DHAV_AUTH_TOKEN=$(cat ~/.config/dhav-bridge/token) \
  .venv/bin/python -m dhav_bridge --host 0.0.0.0 --port 18910 --video-size 256
```

An SSH tunnel is preferable, though: on the gateway side run `ssh -N -L 18910:127.0.0.1:18910 <gpu-host>` and then just connect to `http://127.0.0.1:18910` — no port to open, no TLS to configure, and the token never crosses a wire.

The protocol, endpoints, authentication, turn lifecycle, measured numbers and all 24 pitfalls are in [`dhav_bridge/README.md`](dhav_bridge/README.md).

## Which service should I use?

All four ports need a GPU.

| Port | What it is | DashScope key | Input → output | Docs |
| --- | --- | --- | --- | --- |
| **18910** | DHAV v1 renderer service, for an external agent gateway | not needed | PCM audio of the assistant's reply → paired I420 video + audio, 25 fps | [`dhav_bridge/README.md`](dhav_bridge/README.md) |
| 8284 | Pure rendering passthrough; what 18910 sits on | not needed | `MIC_AUDIO` → `AVATAR_AUDIO` + `JVID` video frames | [`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md) |
| 8283 | Full conversation chain + FlashHead | **required** | Microphone / text → avatar video | upstream |
| 8282 | Full conversation chain + LiteAvatar | **required** | Microphone / text → avatar video | upstream |

Just want a rendering backend for an agent you already have → 18910. Want a ready-to-use voice-conversation avatar → 8282/8283: swap the config above for `chat_with_liteavatar_ws_video.yaml` or `chat_with_flashhead_ws_video.yaml` and `export DASHSCOPE_API_KEY=sk-...`.

> [!WARNING]
> `:8282` / `:8283` / `:8284` have **no authentication** — no token, no Origin check, no allow-list. Anyone who can reach the port can open a session and spend your DashScope quota. Keep them bound to `127.0.0.1`. Only `:18910` has Bearer-token auth.

## What this fork adds

| Module | Size | Purpose |
| --- | --- | --- |
| [`dhav_bridge/`](dhav_bridge/) | 9 files / ~3400 lines / 44 tests | The **DHAV v1** renderer service (`:18910`): binary WebSocket protocol, Bearer auth, turn state machine, paired audio+video downlink |
| [`src/handlers/logic/audio_passthrough/`](src/handlers/logic/audio_passthrough/) | 2 files / 210 lines | Pure rendering passthrough handler: `MIC_AUDIO` in, `AVATAR_AUDIO` out, utterances split by uplink gaps |
| [`src/handlers/client/ws_video_client/`](src/handlers/client/ws_video_client/) | 4 files / 285 lines + a 480-line protocol spec | WebSocket video downlink handler (upstream only ships WebRTC, which a Node gateway cannot peer with) |
| 3 new configs | 176 lines | `:8284` passthrough rendering; WS-video variants of the full chain on `:8282` / `:8283` |

Three knobs worth knowing about:

- `FlashHead.idle_inference: false` — idle animation inference holds the GPU and `_inference_lock` continuously, delaying speech inference by 2–5 s. Off only in the passthrough config, because the bridge only sends media inside a turn and never uses idle frames.
- `AudioPassthrough.gap_seconds: 0.4` — how long an uplink pause counts as end-of-utterance; also the extra latency appended to every utterance. Splitting looks at **whether you stopped sending packets**, not at whether the audio content contains silence.
- `service.concurrent_limit: 1` — FlashHead on a single GPU can only render one session at a time.

## The only change to upstream code

3 files, `+28/−5` (9 of those lines are the change-notice comments), all under `src/handlers/avatar/flashhead/`, adding exactly one config option:

| File | Change |
| --- | --- |
| `flashhead_config.py` | New `idle_inference: bool = Field(default=True, ...)` |
| `avatar_handler_flashhead.py` | Passes that value into `FlashHeadProcessor(...)` |
| `flashhead_processor.py` | Creation and start of the idle-inference thread now gated on that switch |

**The default `True` is upstream's original behaviour**, character for character: any config that does not set this key is unaffected, and only `chat_with_flashhead_passthrough.yaml` turns it off. All three files carry a change notice at the top as required by Apache-2.0 §4(b), and every modified hunk has a trailing `# [fork]` marker, so `git diff upstream/main` shows the whole story at a glance. Everything else added here is a purely additive, self-contained directory.

## Tests

44 unit tests, needing neither a GPU nor a network (the renderer and both DHAV channels are stubbed):

```bash
.venv/bin/python -m pytest dhav_bridge -q
```

`test_dhav_bridge.py` pins the byte layout of the wire format, so a change to the codec fails immediately rather than leaving some gateway silently unable to connect.

## Known pitfalls

1. **The uplink is really 16 kHz, not the 24 kHz the protocol declares.** Upstream hardcodes the `MIC_AUDIO` sample rate to 16000 and ignores the declared value; the bridge resamples internally. **Send 24 kHz as the protocol says** and do not try to "fix" it — sending 16 kHz yourself yields speech 1.5× too fast and 1.5× too low in pitch.
2. **The renderer's downlink is never quiet.** FlashHead's frame collector is a strict 25 fps metronome: with nothing to say it emits a still frame plus all-zero audio, until the session ends. So "how long since I last got data" can never detect end-of-utterance; the bridge relies on upstream's structural `ChatSignal stream_end` marker.
3. **`GET /readiness` returning 200 does not mean video will come out.** It only reflects that handlers registered, not inference throughput. It keeps returning 200 while the renderer is wedged.
4. **INFO-level logs print the DashScope key in plaintext** (handler registration dumps the fully resolved config). The passthrough config has no DashScope component so it does not matter there, but do not ship INFO logs from the full chain.
5. **Rendering occasionally slows down** (0.38× realtime has been observed, and the video stutters). The cause is inside the renderer process; restarting `:8284` fixes it. The tell is `publish_rate_hz` (should be ≈25) and `max_unit_gap_s` from `smoke_client`.

The rest are in [`dhav_bridge/README.md`](dhav_bridge/README.md#pitfalls) and [`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md).

## Upstream and licence

Upstream project: <https://github.com/HumanAIGC-Engineering/OpenAvatarChat> (v0.6.0). The full conversation application, preset modes, component dependencies, documentation site and WeChat group all live there.

This repository is distributed under **Apache-2.0** (see [`LICENSE`](LICENSE)).

- Copyright in the upstream code remains with its original authors.
- Copyright in what this fork adds (`dhav_bridge/`, `audio_passthrough/`, `ws_video_client/`, the 3 new configs) belongs to Haina Zhu, likewise distributed under Apache-2.0.
- Changes to upstream files are limited to the 3 files under `src/handlers/avatar/flashhead/`, each carrying a change notice at the top as required by Apache-2.0 §4(b).

This project is not affiliated with, nor endorsed by, HumanAIGC-Engineering. Apache-2.0 §6 grants no trademark rights; the name and marks "OpenAvatarChat" remain the property of their owner.
