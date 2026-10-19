<h1 style='text-align: center; margin-bottom: 1rem'> Open Avatar Chat </h1>

<p align="center">
<strong><a href="README.md">中文</a> | English</strong>
</p>

<p align="center">
<strong>TL;DR: a self-hosted digital-human API service for <a href="https://github.com/QwenAudio/qwen-audio-agent">Qwen-Audio-Agent</a>.</strong>
</p>

An **unofficial downstream fork** of [HumanAIGC-Engineering/OpenAvatarChat](https://github.com/HumanAIGC-Engineering/OpenAvatarChat), with no affiliation to it.

Upstream is a complete digital-human conversation system, with VAD, ASR, LLM and TTS all built in. This fork takes just the rendering layer and turns it into an API, so that a voice agent you **already have** can use it as a backend. An agent like [qwen-audio-agent](https://github.com/QwenAudio/qwen-audio-agent) goes on listening, thinking and synthesising speech on its own; it only hands the resulting PCM audio to this service, which hands back audio and face video **already paired** with each other, in real time, lips in sync. There is no VAD, ASR, LLM or TTS on this path — only digital-human rendering.

The README comes in two halves: **Quick start** is for whoever deploys this, getting the service up on a GPU box; **What your downstream agent needs** is for whoever integrates with it, usually from a different machine.

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

# The renderer, on :8284; roughly 40–60 s to load the model
.venv/bin/python src/demo.py --config config/chat_with_flashhead_passthrough.yaml --host 127.0.0.1
```

In a second shell, start the bridge:

```bash
.venv/bin/python -m dhav_bridge --video-size 256
```

That is enough — the service is now on `http://127.0.0.1:18910`, and you can skip straight to the next section to wire up your agent. If you would rather first confirm that it really produces video, and not merely that the process is alive, run the self-check. `--input` takes any audio file in a format librosa can read:

```bash
.venv/bin/python -m dhav_bridge.smoke_client --base-url http://127.0.0.1:18910 \
  --input ~/reply.wav --out ~/dhav-check --realtime
```

If it works, it prints `status ok`, `publish_rate_hz` ≈ 25 and `realtime_factor` ≈ 1.0, and leaves `output.wav` and `frames/*.png` in `~/dhav-check/` — ready to listen to and look at.

Two pitfalls worth mentioning up front:

- **After installing, always use `.venv/bin/python`, never `uv run`** (the `uv run install.py` above is the one exception — that is the installer itself). `uv run` performs an implicit sync against the lockfile and **uninstalls** the handler dependencies that `install.py` put in via `uv pip install`, because those are not in the root `pyproject.toml`. The symptom is a handler silently missing, or an import error, on the next start.
- **The certificate must carry SANs**, so do not use the bundled `scripts/create_ssl_certs.sh`. Its `openssl req` has neither `-subj` nor `-addext`, so it blocks on interactive prompts; and even if you answer them all, the certificate it produces has no SANs and always fails hostname verification.

### Reaching it from another machine

Once the above is running, `:18910` is bound to `127.0.0.1` only, so nothing outside the box can reach it. To let your gateway connect from another machine, generate a token first and then restart the bridge with it:

```bash
mkdir -p ~/.config/dhav-bridge && chmod 700 ~/.config/dhav-bridge
openssl rand -hex 32 > ~/.config/dhav-bridge/token && chmod 600 ~/.config/dhav-bridge/token

DHAV_AUTH_TOKEN=$(cat ~/.config/dhav-bridge/token) \
  .venv/bin/python -m dhav_bridge --host 0.0.0.0 --port 18910 --video-size 256
```

There is nothing mysterious about this token: it is just the 64 hex characters that `openssl rand -hex 32` happens to produce. Generate it once, keep it in that file, and read it back on every start. It is not optional either — if the bridge finds itself binding a non-loopback address without a token, it refuses to start and exits with code 2, because otherwise anyone who can route to that port could create sessions and hold the GPU. Binding `127.0.0.1` skips the check, which is why the quick start needs no token.

One more thing: `:18910` is plain HTTP, so the token is the only line of defence. If you would rather not have it cross a wire, do not open the port at all — use an SSH tunnel instead, and the bridge needs no changes whatsoever:

```bash
# run this on the gateway's machine
ssh -N -L 18910:127.0.0.1:18910 <gpu-host>
```

Your gateway then connects to `http://127.0.0.1:18910`: no port to open, no TLS to configure, and no token needed.

Worth noting too — if your `~` lives somewhere that gets reset (a container's `/root` is the classic case), put the token on persistent storage and symlink it back. Otherwise a reboot wipes the token and your gateway has to be reconfigured to match.

## What your downstream agent needs

This section is for the integrating side, i.e. the machine running [qwen-audio-agent](https://github.com/QwenAudio/qwen-audio-agent).

**You need two things from whoever deployed this: the service URL, and that token.** Put them in `examples/digital-human/.env.local`:

```bash
DH_RENDERER_URL=http://<gpu-host>:18910
DH_PERSONA=flashhead-default
DH_RENDERER_TOKEN=<the 64 hex characters in ~/.config/dhav-bridge/token>
```

As soon as `DH_RENDERER_URL` is non-empty, `bootstrap/start.mjs` stops launching the local Mock renderer. It has to be `http://`, not `ws://`, because preflight does `fetch(<url>/health/ready)` with it. If you are going through an SSH tunnel, use `http://127.0.0.1:18910` and you can leave out `DH_RENDERER_TOKEN`.

**Be aware that the example's stock code never passes the token down**, so the moment the server has auth on you will get a 401. Two edits in your own copy of `qwen-audio-agent`:

- `bootstrap/env.mjs`: read one more key in `readConfig()` — `rendererToken: env.DH_RENDERER_TOKEN || null`.
- `bootstrap/compose-gateway.mjs`: pass `authToken` into `createDhavProvider()`; and add `flashhead-default` to the persona allow-list, since the stock code only knows `MOCK_PERSONAS` and otherwise throws `persona ... is not in the server allowlist`. When you add it, `avatarAssetId` must be a non-empty string (a hard requirement of `assertDigitalHumanOptions`) — the value itself is arbitrary, because the actual appearance is decided server-side.

The protocol layer needs no changes: `dhav.mjs`, `dhav-provider.mjs` and `framed-av-source.mjs` work as they are. Afterwards start it as usual with `node bootstrap/start.mjs` and open `http://<gateway-host>:18895/?avatar=flashhead-default` in a browser.

### How to tell whether the service is alive

```bash
curl -s http://<gpu-host>:18910/health/ready
```

| Endpoint | Question it answers | How to read it |
| --- | --- | --- |
| `GET /health/live` | Is the bridge process up? | `{"live": true}` |
| `GET /health/ready` | Can it render right now? | the `ready` field in the body |

Neither needs a token. But `/health/ready` has a counter-intuitive property: **its HTTP status code is always 200**, and the verdict lives only in the body:

```json
{"ready": true, "fault": null, "renderer": "http 200"}
```

`ready: true` means the bridge actually probed the `:8284` renderer and got a response, which makes it far more trustworthy than the renderer's own `/readiness` — that one only reports that handlers registered, and keeps returning 200 while the renderer is wedged. When `ready` is false, `fault` and `renderer` tell you which layer broke. This is the same endpoint the gateway's preflight uses.

Everything below that — the byte-level DHAV v1 wire format, the turn state machine, what `start` / `finish` / `cancel` mean, what a downlink `PAIRED_AV` record looks like, and 24 pitfalls — is in [`dhav_bridge/README.md`](dhav_bridge/README.md).

## Which service should I use?

All four ports need a GPU. For this fork's main purpose (acting as the digital-human backend for [qwen-audio-agent](https://github.com/QwenAudio/qwen-audio-agent)), only `:18910` and `:8284` come into it.

| Port | What it is | DashScope key | Input → output | Docs |
| --- | --- | --- | --- | --- |
| **18910** | DHAV v1 renderer service, for an external agent gateway | not needed | PCM audio of the assistant's reply → paired I420 video + audio, 25 fps | [`dhav_bridge/README.md`](dhav_bridge/README.md) |
| 8284 | Pure rendering passthrough; what 18910 sits on | not needed | `MIC_AUDIO` → `AVATAR_AUDIO` + `JVID` video frames | [`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md) |
| 8283 | Full conversation chain + FlashHead (kept for capability only; unrelated to this fork's main purpose) | **required** | Microphone / text → avatar video | upstream |
| 8282 | Full conversation chain + LiteAvatar (kept for capability only; unrelated to this fork's main purpose) | **required** | Microphone / text → avatar video | upstream |

`:8284` has **no authentication at all** — no token, no Origin check, no allow-list — so it can only ever be bound to `127.0.0.1`, which is what the quick start does. To reach it from another machine, use an SSH tunnel rather than exposing it. Of the four ports, only `:18910` has Bearer-token auth.

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

## Changes to upstream code

3 files, `+28/−5` (9 of those lines are the change-notice comments), all under `src/handlers/avatar/flashhead/`, adding exactly one config option:

| File | Change |
| --- | --- |
| `flashhead_config.py` | New `idle_inference: bool = Field(default=True, ...)` |
| `avatar_handler_flashhead.py` | Passes that value into `FlashHeadProcessor(...)` |
| `flashhead_processor.py` | Creation and start of the idle-inference thread now gated on that switch |


<details>
  <summary>Known pitfalls</summary>


1. **The uplink is really 16 kHz, not the 24 kHz the protocol declares.** Upstream hardcodes the `MIC_AUDIO` sample rate to 16000 and ignores the declared value; the bridge resamples internally. **Send 24 kHz as the protocol says** and do not try to "fix" it — sending 16 kHz yourself yields speech 1.5× too fast and 1.5× too low in pitch.
2. **The renderer's downlink is never quiet.** FlashHead's frame collector is a strict 25 fps metronome: with nothing to say it emits a still frame plus all-zero audio, until the session ends. So "how long since I last got data" can never detect end-of-utterance; the bridge relies on upstream's structural `ChatSignal stream_end` marker.
3. **`GET /readiness` returning 200 does not mean video will come out.** It only reflects that handlers registered, not inference throughput, and it keeps returning 200 while the renderer is wedged. To find out whether it can actually render, use the bridge's `/health/ready` and read its body.
4. **Rendering occasionally slows down.** The cause is inside the renderer process; restarting `:8284` fixes it. The tell is `publish_rate_hz` (should be ≈25) and `max_unit_gap_s` from `smoke_client`.

The rest are in [`dhav_bridge/README.md`](dhav_bridge/README.md#pitfalls) and [`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md).

</details>



## Upstream and licence

Thanks to this repository's upstream project: <https://github.com/HumanAIGC-Engineering/OpenAvatarChat> (v0.6.0).
This repository is distributed under **Apache-2.0** (see [`LICENSE`](LICENSE)); the name and marks "OpenAvatarChat" remain the property of the upstream project's owner.
