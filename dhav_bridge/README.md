# DHAV v1 renderer bridge

Serves a FlashHead talking head as a **DHAV v1** renderer, so an external
voice-agent gateway can drive it over a plain WebSocket.

You stream it the PCM audio of your assistant's reply; it streams back
**frame-paired** audio and video with the mouth already matched. There is no
VAD, ASR, LLM or TTS anywhere in this path — the bridge owns dialogue turns and
media framing, and nothing else.

```
your gateway ──DHAV v1──> :18910 dhav_bridge ──JVID/ws──> :8284 audio-passthrough renderer ──> FlashHead
```

Related documents:

- [`../README.md`](../README.md) — what this fork adds, which service you want, install.
- [`../src/handlers/client/ws_video_client/PROTOCOL.md`](../src/handlers/client/ws_video_client/PROTOCOL.md) — byte-level contract of the `:8284` renderer the bridge sits on.
- [`dhav.py`](dhav.py) — the authoritative DHAV v1 codec; `test_dhav_bridge.py` pins the layout.

---

## Quick start

Assumes the repository is already installed (`uv run install.py --config config/chat_with_flashhead_passthrough.yaml`)
and the FlashHead models are downloaded. See the root README for that part.

> **Never launch with `uv run`.** Use `.venv/bin/python`. `uv run` performs an
> implicit sync against the lockfile and will *uninstall* the handler
> dependencies `install.py` added, because they are not in the root
> `pyproject.toml`. Symptom: handlers silently disappear on the next start.

```bash
cd OpenAvatarChat

# 1. A cert with SANs — scripts/create_ssl_certs.sh produces one without, which
#    fails hostname validation.
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout ssl_certs/localhost.key -out ssl_certs/localhost.crt \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

# 2. The renderer (needs no DashScope key; ~40 s to load the model).
.venv/bin/python src/demo.py \
  --config config/chat_with_flashhead_passthrough.yaml --host 127.0.0.1

# 3. The bridge, in another shell. Loopback needs no token.
.venv/bin/python -m dhav_bridge --video-size 256

# 4. Prove it end to end.
.venv/bin/python -m dhav_bridge.smoke_client \
  --base-url http://127.0.0.1:18910 \
  --input /path/to/reply.wav --out ~/dhav-check --realtime
```

A healthy run ends with `status ok`, `publish_rate_hz` ≈ 25,
`realtime_factor` ≈ 1.0 and a non-empty `media_end`. `~/dhav-check/` holds
`output.wav`, `frames/*.png` and `report.json`.

To expose the bridge beyond loopback you must supply a token — the process
refuses to start otherwise:

```bash
mkdir -p ~/.config/dhav-bridge && chmod 700 ~/.config/dhav-bridge
openssl rand -hex 32 > ~/.config/dhav-bridge/token && chmod 600 ~/.config/dhav-bridge/token

DHAV_AUTH_TOKEN=$(cat ~/.config/dhav-bridge/token) \
  .venv/bin/python -m dhav_bridge --host 0.0.0.0 --port 18910 --video-size 256
```

Prefer an SSH tunnel to opening the port: `ssh -N -L 18910:127.0.0.1:18910 <gpu-host>`
lets the gateway use `http://127.0.0.1:18910` with no firewall rule, no TLS and
no token on the wire.

---

## CLI

`python -m dhav_bridge` — run from the repository root, since `--renderer-ca` is
a relative path.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bind address. Non-loopback requires `--auth-token`. |
| `--port` | `18910` | Bind port. |
| `--renderer-url` | `wss://127.0.0.1:8284` | Passthrough renderer base URL. Env `DHAV_RENDERER_UPSTREAM`. |
| `--renderer-ca` | `ssl_certs/localhost.crt` | CA that signed the renderer's certificate. Must exist or startup fails. |
| `--auth-token` | — | Bearer token for `POST`/`DELETE`. Env `DHAV_AUTH_TOKEN`. |
| `--public-base-url` | — | Base URL echoed in the session description. Env `DHAV_PUBLIC_BASE_URL`. **Leave unset**: URLs are derived from the request's `Host` header, which makes both loopback self-check and external access work. Set it only behind a Host-rewriting proxy. |
| `--video-size` | `256` | I420 size, `"N"` or `"WxH"`. Must be even and ≤ 512. |
| `--fps` | `25` | *Advertised* frame rate only. The renderer really emits 25; any other value logs a warning. |
| `--persona` | `flashhead-default` | Accepted `personaId`. Repeatable. |
| `--max-sessions` | `1` | Concurrent avatar sessions. One GPU renders one session at a time. |
| `--attach-timeout` | `60.0` | Seconds a created-but-unattached session holds its slot before being reaped. |
| `--cert` / `--key` | — | Serve the bridge over TLS. Must be given together; session WS URLs then become `wss://` automatically. |
| `--log-level` | `INFO` | Unknown values fall back to INFO. |

Startup validation returns exit code `2` for: `--cert`/`--key` given singly, a
non-loopback `--host` with no token, an unparseable or oversized `--video-size`,
and a missing `--renderer-ca` file.

The bridge keeps no state between runs. Restarting it drops every live session.

---

## Endpoints and authentication

Two independent auth tiers. The control plane uses one shared bearer token; the
two WebSocket channels use per-session random tokens that are only ever handed
out in the create response.

| Method and path | Auth | Purpose |
| --- | --- | --- |
| `GET /health/live` | none | `{"live": true}`. Process is up. |
| `GET /health/ready` | none | Probes the renderer's `GET /readiness` over TLS. `{"ready", "fault", "renderer"}`. |
| `POST /v1/avatar/sessions` | `Authorization: Bearer <token>` | Create a session. |
| `DELETE /v1/avatar/sessions/{id}` | `Authorization: Bearer <token>` | Destroy it, interrupting any in-flight render. Always `204`. |
| `GET /v1/avatar/sessions/{id}/input` | `Authorization: Bearer <input.token>` | Bidirectional: control frames in, events out. |
| `GET /v1/avatar/sessions/{id}/media` | `Authorization: Bearer <media.token>` | Server writes only; anything you send is ignored. |

`/health/ready` says the renderer's HTTP endpoint answered. It does **not** mean
the renderer can still hit 25 fps — see "Pitfalls".

Create request:

```json
{"personaId": "flashhead-default", "protocolVersion": 1}
```

`201` response:

```json
{
  "avatarSessionId": "3119f954-…",
  "protocolVersion": 1,
  "personaId": "flashhead-default",
  "mediaAdapterId": "dhav-framed-av",
  "input": {"url": "ws://…/v1/avatar/sessions/3119f954-…/input", "token": "…"},
  "media": {"url": "ws://…/v1/avatar/sessions/3119f954-…/media", "token": "…"},
  "video": {"format": "i420", "width": 256, "height": 256, "fps": 25},
  "inputFormat": {"encoding": "pcm_s16le", "sampleRate": 24000, "channels": 1}
}
```

Errors: `400` malformed body, unknown `personaId`, or `protocolVersion != 1`;
`401` missing or wrong bearer token; `404` unknown session or channel;
`409 capacity_exhausted` when all `--max-sessions` slots are taken.

Both channels must attach within `--attach-timeout` or the session is reaped
(`session.reaped reason=attach_timeout`) and its slot freed.

---

## Wire format

Binary records are one WebSocket binary message each: a 16-byte big-endian
prefix, a UTF-8 JSON header, then the payload.

```
magic[4]='DHAV' | version u8 | kind u8 | flags u16 | headerLength u32 | payloadLength u32
header JSON
payload
```

Kinds: `INPUT_PCM = 1`, `PAIRED_AV = 2`, `MEDIA_END = 3`. Limits: header 4 KiB,
whole record 1 MiB, control text 16 KiB, video 512×512.

Text frames are control (client → server) or events (server → client), both
`{"protocol": "dhav", "version": 1, …}`.

Every record and control frame carries a **TurnRef**:

```json
{"avatarSessionId": "…", "responseId": "…", "generation": 0}
```

`avatarSessionId` and `responseId` are non-empty strings ≤ 256 chars;
`generation` is a non-negative integer. Bump `generation` when you supersede a
turn you are still producing audio for — the bridge uses it to tell the old
turn's draining output apart from the new turn's.

### Control ops (client → server, on `input`)

| `op` | Required fields | Effect |
| --- | --- | --- |
| `start` | `requestId`, `turn`, optional `inputFormat` | Opens a turn. Supersedes any live turn, which is cancelled first. |
| `finish` | `requestId`, `turn`, `lastAudioSequence` (≥ −1), `totalSamples` (≥ 0) | No more audio for this turn. Wrong values → `protocol_error`. |
| `cancel` | `requestId`, `turn`, `reason` | Abandons the turn. |
| `close` | `requestId` | Ends the session. |

### Records you send (client → server, on `input`)

`INPUT_PCM` — header `{turn, sequence, sampleOffset, samples, format}`, payload
`samples × 2 × channels` bytes of `pcm_s16le`. `sequence` must be contiguous
from 0; a gap is a `protocol_error`. Use 1920 samples (40 ms) per record. At
most `ADMISSION_SECONDS` (120 s) of audio is admitted per turn.

### Records you receive (server → client, on `media`)

`PAIRED_AV` — header
`{turn, sequence, ptsUs, durationUs, sampleOffset, samples, paddingSamples, silence, idle, format, video{format,width,height}, audioLength, videoLength}`,
payload is `audioLength` bytes of PCM followed by `videoLength` bytes of I420.
`durationUs` is 40000 and `samples` is 960 at 24 kHz, so one record is exactly
one 25 fps frame and its audio.

`MEDIA_END` — header `{turn, lastSequence, reason}`. `reason` is `finished`,
`timeout` or `no_output`. Not sent after a `cancel`.

### Events (server → client, on both channels)

| Event | Fields | Meaning |
| --- | --- | --- |
| `session.ready` | `avatarSessionId` | Both channels attached; the renderer is connected. |
| `heartbeat` | `ts` | Every 1 s. |
| `turn.first_media` | `turn` | First `PAIRED_AV` of the turn is on the wire. |
| `turn.progress` | `turn`, `unitsPublished` | Periodic count of published units. |
| `turn.completed` | `turn`, `lastSequence` | The bridge stopped producing for this turn. |
| `turn.cancelled` | `turn`, `reason` | The turn was abandoned. |
| `provider.error` | `turn`, `code`, `message`, `rebuild` | `code` is `protocol_error` (your fault, `rebuild=false`) or `unavailable` (renderer-side, `rebuild=true`). |

`turn.completed` means *the server finished producing*. It says nothing about
whether the browser finished playing — keep your own playback receipt for that.

---

## Turn lifecycle

```
start ──> INPUT_PCM × N ──> finish ──> PAIRED_AV × M ──> MEDIA_END ──> turn.completed
  │                                                            ▲
  └──────────────────────> cancel ──> turn.cancelled (no MEDIA_END)
```

Three properties of the renderer decide how the bridge detects the end of a
turn, and they are the reason this is not a trivial passthrough:

1. **The downlink never goes quiet.** FlashHead's frame collector is a strict
   25 fps metronome: whenever the queue empties it emits a still frame paired
   with a block of exact zeros, for the whole session. "No data for N seconds"
   is therefore *never* a valid end-of-turn test. Turn end comes from the
   renderer's structural `ChatSignal stream_end` markers instead:
   `input_closed` (the 0.4 s uplink gap fired, so everything you sent is now
   inside the renderer) and `avatar_done` (the avatar emitted this utterance's
   last frame — authoritative, but ≈2 s late, because the marker has to traverse
   chunked inference).
2. **Each utterance starts with ≈1 s of exactly-zero audio blocks** — the
   renderer's own buffering delay. Up to `MAX_LEADING_SILENCE_BLOCKS` (50, i.e.
   2 s) of them are dropped along with their idle frames, which removes that
   second from perceived latency.
3. **Renderer lag is 0.7–1.5 s and intra-reply pauses reach 0.56 s**, so after
   `input_closed` the bridge waits for the reply's own duration to play out
   *plus* `END_GRACE_S` (1.2 s) of continuous silence. Without the duration gate
   a sentence gets cut in half. If nothing was ever published,
   `NO_OUTPUT_GRACE_S` (6 s) applies instead, and the turn ends `no_output`.

`TURN_TIMEOUT_S` (180 s) is an unconditional per-turn ceiling — a long-idle turn
dies at ≈178 s regardless of health.

After a `cancel`, the abandoned utterance's remaining output is still in flight.
The bridge suppresses publishing for `FLUSH_QUIET_S` (0.4 s), extended by
*audible* blocks only and capped at `FLUSH_MAX_S` (3 s). Extending on audible
blocks is what stops a cancelled turn's tail from leaking into the next one;
capping is what stops a renderer that ignores `Interrupt` from starving the new
turn. Silent blocks never extend the window — since the renderer is never quiet,
they would otherwise hold it open for the full 3 s and swallow the head of the
next reply.

### Tunables

All in [`session.py`](session.py) unless noted:

| Constant | Value | Meaning |
| --- | --- | --- |
| `SAMPLE_RATE` | 24000 | Downlink audio rate. |
| `FRAME_SAMPLES` / `FRAME_BYTES` | 960 / 1920 | One 40 ms block (`renderer_client.py`). |
| `FRAME_DURATION_US` | 40000 | 40 ms ⇒ 25 fps. |
| `MAX_LEADING_SILENCE_BLOCKS` | 50 | Cap on dropping the renderer's zero lead-in. |
| `SILENCE_RMS` | 0.005 | Normalised RMS silence gate — ~4× above idle dither (0.0013), ~4× below the quietest speech (≥ 0.02). |
| `MAX_AUDIO_QUEUE` / `MAX_FRAME_QUEUE` | 250 / 64 | Runaway valves, ≈10 s of audio. |
| `END_GRACE_S` | 1.2 | Silence accepted as "media ended" after `input_closed`. |
| `NO_OUTPUT_GRACE_S` | 6.0 | Grace when nothing has been published. |
| `TURN_TIMEOUT_S` | 180.0 | Hard per-turn ceiling. |
| `FLUSH_QUIET_S` / `FLUSH_MAX_S` | 0.4 / 3.0 | Post-cancel suppression window and its cap. |
| `ADMISSION_SECONDS` | 120 | Max uplink audio admitted per turn. |
| `UPLINK_SAMPLE_RATE` | 16000 | What the renderer actually wants (`renderer_client.py`). |
| `uplink_chunk_samples` | 2400 | Uplink packet size, 150 ms (`server.py`). Deliberately not 960, so the renderer's echo stream is distinguishable from its frame-aligned stream by packet size. |
| `renderer_max_session_age_s` | 800.0 | Recycle the renderer session before the server's own TTL. |

---

## Media format

- **Video**: raw I420, full-range BT.601, `width × height × 3 / 2` bytes
  (98304 at 256×256), one complete frame per `PAIRED_AV`.
- **Audio**: `pcm_s16le`, 24 kHz, mono, 960 samples = 1920 bytes per record,
  covering exactly the same 40 ms as the frame it is paired with.
- One record ≈ 100.6 KB on the wire, ≈ 2.5 MB/s, ≈ **20 Mbps** at 256×256.
  At 512×512 it is ≈ 395 KB/frame ≈ **79 Mbps** — not recommended across
  machines.

The conversion in [`video.py`](video.py) is hand-rolled on purpose.
`cv2.COLOR_BGR2YUV_I420` emits **limited-range** (studio swing) YUV, which comes
out visibly washed and darker than any other DHAV renderer. Symmetrically, if
you decode with `cv2.COLOR_YUV2BGR_I420` your debug PNGs will look slightly
off-gamut — that is the decoder's range being wrong, not the service's.

Silent frames are not published: `silence` and `idle` are set on records the
renderer produced with no speech, and the bridge drops them rather than pairing
them.

---

## The renderer underneath (`:8284`)

The bridge does not talk to FlashHead directly. It drives
`config/chat_with_flashhead_passthrough.yaml`:

```
WsVideoClient → InterruptHandler → AudioPassthrough → FlashHead
```

`AudioPassthrough` ([`../src/handlers/logic/audio_passthrough/`](../src/handlers/logic/audio_passthrough/))
takes raw `MIC_AUDIO` and emits `AVATAR_AUDIO` unchanged. Two things about it
shape everything above:

- **Utterance boundaries are uplink packet gaps, not content silence.** A gap of
  `gap_seconds` (0.4 s) with no packet closes the utterance; the next packet
  opens a new one. Silence *inside* the audio does not split it. So the way to
  say "sentence over" is to stop sending — and `gap_seconds` is also 0.4 s of
  added tail latency on every reply.
- **The uplink is 16 kHz, not the 24 kHz the protocol declares.**
  `ws_client_handler.py` hard-codes the `MIC_AUDIO` sample rate to 16000 and
  ignores whatever `InitializeAvatarSession` said. The bridge resamples
  24 kHz → 16 kHz itself (stateful `soxr`, reset per turn). **Keep sending
  24 kHz.** If you "fix" this by sending 16 kHz, speech comes out 1.5× too fast
  and 1.5× too low in pitch.

`WsVideoClient` ([`../src/handlers/client/ws_video_client/`](../src/handlers/client/ws_video_client/))
is what makes the video reachable at all: upstream only offers server-rendered
avatar video over WebRTC, which a Node gateway cannot peer with. It sends
self-describing `JVID` binary frames (32-byte little-endian header + one
complete independent JPEG each) instead of headerless chunks. See
[`PROTOCOL.md`](../src/handlers/client/ws_video_client/PROTOCOL.md) for the byte
layout, the full downlink message table and a minimal Node reference client.

The `:8284` service has **no authentication**. Bind it to `127.0.0.1` and reach
it through this bridge.

---

## Measured numbers

Single GPU, 256×256@25 fps, input paced at realtime. Your numbers will vary.

| Metric | Value |
| --- | --- |
| First media, cold (first uplink block → first `PAIRED_AV`) | ≈ 2.0 s |
| First media, in real conversation | ≈ 1 s (TTS audio has almost no leading silence to skip) |
| `publish_rate_hz` | 25.0 |
| `realtime_factor` | 1.006 |
| `max_unit_gap_s` | 0.092–0.102 |
| One `PAIRED_AV` | ≈ 100.6 KB ⇒ ≈ 20 Mbps |
| Last block → `turn.completed` | ≈ 1.3 s |
| `cancel` → `turn.cancelled` | ≈ 1 ms |
| Renderer model load | ≈ 40 s |
| FlashHead inference | 45–55 FPS (≈2× headroom over the 25 fps requirement) |
| Renderer sustained output | 25.01 fps, no dropped frames |
| Audio fidelity, 8.66 s input | 7.2 s / 180 frames out — the 1.44 s difference is trailing silence, which is correctly not published |
| Voiced-segment fidelity | 5.96 s in → 5.94 s out; peak 0.572 → 0.574; RMS 0.132 → 0.136 |
| Pitch/duration, 440 Hz sine 1.00 s | 440.0 Hz, 1.00 s |
| Audible sound-to-lipsync delay | ≈ 1.5–2 s (FlashHead's own audio buffer, not pipeline overhead) |
| After 25 h continuous uptime | still `realtime_factor` 1.006, `publish_rate_hz` 25.0 |

For comparison, the `:8282` full ASR/LLM/TTS stack takes ≈190 s from boot to
`/readiness` 200 and ≈75 s to warm the first avatar session. The passthrough
path has no such warm-up.

Restart on metrics, not on uptime.

---

## Self-check

```bash
# Liveness and full-chain readiness (ready really probes the renderer).
curl -s http://127.0.0.1:18910/health/live
curl -s http://127.0.0.1:18910/health/ready
#   {"ready": true, "fault": null, "renderer": "http 200"}

# Token check: 401 = wrong, 201 = correct. Delete the session afterwards.
curl -s -i -X POST http://127.0.0.1:18910/v1/avatar/sessions \
  -H 'content-type: application/json' \
  -H "Authorization: Bearer $DHAV_AUTH_TOKEN" \
  -d '{"personaId":"flashhead-default","protocolVersion":1}' | head -1

# Renderer readiness. Trust the CA rather than passing -k: disabling
# verification is exactly the habit this README tells you not to build.
curl -s --cacert ssl_certs/localhost.crt https://127.0.0.1:8284/readiness
```

End-to-end:

```bash
.venv/bin/python -m dhav_bridge.smoke_client \
  --base-url http://127.0.0.1:18910 --token "$(cat ~/.config/dhav-bridge/token)" \
  --input ~/samples/reply.wav --out ~/dhav-check --realtime

# Interrupt path: expect turn.cancelled and no MEDIA_END.
.venv/bin/python -m dhav_bridge.smoke_client … --cancel-after 4

# Supersede path: an idle turn is started and cancelled just before the real
# one, which is how a gateway keeps the avatar breathing between replies.
.venv/bin/python -m dhav_bridge.smoke_client … --idle-lead 7
```

`smoke_client` flags: `--base-url`, `--token`, `--persona`, `--response-id`,
`--input` (required, any format librosa reads), `--out`, `--chunk-samples`
(1920), `--realtime`, `--cancel-after`, `--idle-lead`, `--timeout` (120),
`--every` (save every Nth frame as PNG), `--keep-open`, `--ca`.

A healthy `report.json` has `status: ok`, `sequence_continuous: true`,
`pts_monotonic: true`, non-empty `media_end`, `publish_rate_hz` ≈ 25,
`realtime_factor` ≈ 1.0 and `max_unit_gap_s` < 0.2. Read perceived latency from
`first_media_after_send_s` — **not** `first_media_at_s`, which counts from
process start and includes the client's own audio loading time.

`publish_rate_hz` averaging below 20 means the renderer cannot keep up; restart
it. `audio_seconds` much larger than the input duration means the end-of-turn
rule regressed — look for `turn.renderer_marker` lines in the bridge log.

A `409 capacity_exhausted` during a smoke run is not a fault: there is one
session slot. Disconnect your gateway first, or run a second bridge on another
`--port`.

---

## Pitfalls

**Install and environment**

1. Launching with `uv run` uninstalls the handler dependencies. Always
   `.venv/bin/python`.
2. `scripts/create_ssl_certs.sh` runs a bare `openssl req` with neither `-subj`
   nor `-addext`: it blocks on an interactive prompt and produces a cert with no
   SANs, which then fails hostname validation. Use the `openssl` one-liner above.
3. Trust the CA. Never `rejectUnauthorized: false` / `verify=False` — that
   disables validation process-wide in a process that also talks to your LLM
   provider.
4. `models/SoulX-FlashHead-1_3B` and `models/wav2vec2-base-960h` are gitignored.
   Fetch them with
   `.venv/bin/python scripts/download_models.py --config config/chat_with_flashhead_passthrough.yaml`.
5. `--renderer-ca` defaults to a relative path, so the bridge must be launched
   from the repository root.

**Renderer**

6. `GET /readiness` returning 200 does not mean it can render. It gates handler
   registration, not throughput. A wedged renderer still answers 200.
7. Throughput below realtime is usually *inside* the `:8284` process, not GPU
   contention: residual background inference plus idle-frame generation fighting
   over `_inference_lock`. Restart the renderer, and keep
   `FlashHead.idle_inference: false` in the passthrough config. Diagnose with
   `publish_rate_hz` and whether `max_unit_gap_s` falls inside a voiced segment;
   only then look at `nvidia-smi`.
8. The renderer ignores `SIGTERM` while a session is open. It needs `SIGKILL`.
   Every restart hard-cuts the live session; the bridge reconnects on its own
   (look for `renderer connected … attempt=N`).
9. `:8284` has no auth. Bind `127.0.0.1` only.
10. The server closes a session after 30 s with no uplink
    (`HEARTBEAT_TIMEOUT`). Sending audio counts as traffic; ping explicitly
    during silence.
11. INFO-level logs print the resolved handler config, including any
    `api_key`, in plaintext. Not an issue for the passthrough config (it has no
    DashScope component), but never ship an INFO log from a full-stack config.

**Protocol**

12. Speech 1.5× too fast and 1.5× too low in pitch: you sent 16 kHz. Send
    24 kHz; the bridge resamples.
13. Two consecutive replies rendered as one sentence: segmentation is by uplink
    gap. Leave ≥ 0.5 s between turns.
14. A turn that never ends: you waited for the media to go quiet. It never
    does. Wait for `MEDIA_END` / `turn.completed`; until then the only thing
    that will stop the turn is `TURN_TIMEOUT_S` (180 s), which reports
    `reason: timeout`.
15. `protocol_error` right after `finish`: `lastAudioSequence` or `totalSamples`
    did not match what you actually sent.
16. `sequence` gaps in `INPUT_PCM` are a `protocol_error`, not a dropped packet.
17. Your gateway times out waiting for first media: it is ≈2 s cold. Keep the
    first-media timeout ≥ 5 s.
18. Debug PNGs look washed out: `cv2.COLOR_YUV2BGR_I420` is limited-range, the
    service is full-range. The decoder is wrong.
19. `--video-size` must be even and ≤ 512.
20. A `start` sent before the renderer has connected is rejected as
    `unavailable`. If you drive the bridge from a script, wait for
    `session.ready`.

**Operations**

21. Every create returns `409` forever: one session slot. Check whether the last
    `session.created` in the log has a matching `channel.attached`; unattached
    sessions are reaped after `--attach-timeout`.
22. `turn.completed` is not playback completion. Keep your own receipt.
23. As deployed the bridge is plaintext HTTP/WS — audio and token are sniffable
    on a public network. Add `--cert`/`--key`, or use an SSH tunnel.
24. `--public-base-url` should stay unset. Set it and loopback self-checks break
    whenever the value disagrees with the `Host` header the client actually sent.

---

## Tests

44 unit tests, no GPU and no network — the renderer and both DHAV channels are
stubbed:

```bash
.venv/bin/python -m pytest dhav_bridge -q
# or, with no pytest installed:
.venv/bin/python -m unittest dhav_bridge.test_dhav_bridge -v
```

`test_dhav_bridge.py` pins the wire layout, so a codec change fails loudly
rather than silently breaking a gateway.

---

## Licence

Apache-2.0, same as the repository root. This package is part of an unofficial
downstream fork of [HumanAIGC-Engineering/OpenAvatarChat](https://github.com/HumanAIGC-Engineering/OpenAvatarChat);
new code here is © Haina Zhu. Not affiliated with or endorsed by
HumanAIGC-Engineering.
