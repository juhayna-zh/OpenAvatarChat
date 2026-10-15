# WebSocket Avatar Video Protocol

Client-side contract for consuming OpenAvatarChat's server-rendered digital
human over a plain WebSocket. Written for an external Node.js voice-agent
gateway; nothing here is Python-specific.

Two layers are stacked on one socket:

- **Inherited** from the stock `ws_client` handler — session init, audio/text
  uplink, avatar audio + text downlink, heartbeats, interrupts. Unchanged.
- **Added** by this module — the `avatar_video` downlink: a JSON welcome, a
  stream of self-describing binary JPEG frames, a JSON end marker.

---

## 1. Endpoint

```
wss://<host>:8282/ws/session/{session_id}
```

`{session_id}` is chosen by the client (any unique string; a UUID is fine).
The server creates the session on first connect.

**TLS.** The dev cert at `ssl_certs/localhost.crt` is self-signed, so the
gateway must trust it explicitly — pass the certificate as the CA rather than
turning verification off:

```js
new WebSocket(url, { ca: fs.readFileSync("/path/to/OpenAvatarChat/ssl_certs/localhost.crt") })
```

Do **not** set `rejectUnauthorized: false`. It disables hostname and chain
validation process-wide for that agent, which is not something you want in a
codebase that also talks to DashScope. If the gateway must reach OpenAvatarChat
over the network rather than loopback, put a real cert on it instead.

The cert must also carry a `subjectAltName` for every name you connect with —
`DNS:localhost` and `IP:127.0.0.1` for loopback. `scripts/create_ssl_certs.sh`
runs a bare `openssl req` with neither `-subj` nor `-addext`, so it blocks on an
interactive prompt and produces a cert with **no SANs at all**, which fails
hostname validation. Generate your own instead:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout ssl_certs/localhost.key -out ssl_certs/localhost.crt \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
```

Add your real hostname or IP to that SAN list if the gateway connects over the
network.

**No authentication.** This endpoint has none — no token, no Origin check, no
allowlist. Anyone who can open a TCP connection to it can start a session and
spend your DashScope quota on ASR, LLM, and TTS. The shipped config binds
`0.0.0.0`, which on a cloud box means "publicly reachable unless the security
group says otherwise". Since the gateway is on the same host, bind the service
to `127.0.0.1` instead:

```yaml
default:
  service:
    host: "127.0.0.1"     # shipped as "0.0.0.0"
```

or override at launch with `--host 127.0.0.1`. Treat any public exposure of
:8282 as a live credential leak, not a demo convenience.

**Connection roles.** The *first* socket to a given `session_id` becomes
`primary`; every later one is a `listener`.

| | primary | listener |
|---|---|---|
| `InitializeAvatarSession` | required, first message | ignored |
| Audio / text / interrupt uplink | allowed | rejected with `Error` |
| Any binary message | allowed | ignored with a warning |
| Downlink (audio, text, video) | received | received |

For a gateway, one primary connection per conversation is all you need.

**Liveness.** *Any* inbound message — JSON or binary — refreshes the session's
heartbeat clock. Steady audio upload therefore keeps the session alive on its
own. The idle timeout is `heartbeat_timeout` (default **30 s**). Send
`TriggerHeartbeat` on a ~10 s timer if you expect silence that long. The
session TTL is `connection_ttl` (default **900 s**).

---

## 2. Uplink (client → server)

Every JSON message has the same envelope:

```json
{ "header": { "name": "<MessageType>", "request_id": "<unique>" }, "payload": { } }
```

`request_id` must be unique per message — for binary transports the server uses
it to correlate the announcement with the chunks that follow.

### 2.1 `InitializeAvatarSession` — must be first

```json
{
  "header": { "name": "InitializeAvatarSession", "request_id": "init-1" },
  "payload": {
    "audio": { "format": "PCM", "sample_rate": 16000, "channels": 1 },
    "subscriptions": ["human_text", "avatar_text", "avatar_audio", "avatar_video"]
  }
}
```

- `audio.format`: `"PCM"` (s16le) or `"OPUS"`. The avatar pipeline expects
  **16 kHz mono PCM**; use that unless you have a reason not to.
- `audio.opus_frame_size_ms`: only for OPUS, default 20.
- `subscriptions`: **omit the field entirely to receive everything**, including
  video. If you do list them, `avatar_video` must be present or no frames will
  be sent. Valid values: `human_text`, `avatar_text`, `avatar_audio`,
  `motion_data`, `avatar_video`. Unknown names are dropped with a warning.

Server replies with `AvatarSessionInitialized`.

### 2.2 `SendHumanAudio` — the main uplink

Two transports. **Use `base64` unless you need the throughput.**

**base64 (simple, recommended for a gateway)** — one message, no ordering
hazard:

```json
{
  "header": { "name": "SendHumanAudio", "request_id": "a-42" },
  "payload": { "transport": "base64", "data_base64": "<base64 of raw PCM>" }
}
```

**binary (segmented)** — an announcement JSON, then that many *bare* binary
WebSocket messages, back to back, with nothing else interleaved:

```json
{
  "header": { "name": "SendHumanAudio", "request_id": "a-43" },
  "payload": { "transport": "binary", "binary_size": 32000, "segment_num": 2 }
}
```
```
<binary chunk 1>   (16000 bytes)
<binary chunk 2>   (16000 bytes)
```

`binary_size` is the total across all chunks; `segment_num` must be ≥ 1. Chunks
are bare — they carry **no header of their own** — and the assembler matches
them to pending announcements **in FIFO order**. If you send two announcements
before their chunks, or interleave anything, they will be matched to the wrong
request. Send announcement + all its chunks as one atomic burst.

Optional `payload.format` overrides the session's audio format for that message.

Chunking advice: 20–200 ms of audio per message (640–6400 bytes at 16 kHz
s16le). Stream continuously — the server's VAD detects end-of-utterance itself
(silence threshold configured server-side); do not wait for a full sentence.

### 2.3 Other uplink messages

| Message | Payload | Purpose |
|---|---|---|
| `SendHumanText` | `stream_key`, `mode` (`increment`\|`full_text`), `text`, `end_of_speech` | Type instead of speak. Bypasses ASR; set `end_of_speech: true` on the last chunk. |
| `Interrupt` | — (header only) | Stop the avatar immediately (barge-in). |
| `EndSpeech` | `stream_key` | Renderer-side signal that playback of a stream finished. |
| `TriggerHeartbeat` | — (header only) | Keep-alive ping; server replies `AvatarHeartbeat`. |
| `SendHumanVideo` | `width`, `height`, `format`, `transport`, `binary_size`, `segment_num` | Webcam uplink. Not needed for a gateway integration. |

---

## 3. Downlink (server → client)

### 3.1 Demultiplexing

One socket, three kinds of traffic. Route on arrival:

```
if (message is text)        -> JSON envelope, switch on header.name
if (message is binary) {
    if (bytes[0..4] == "JVID") -> avatar video frame   (section 3.3)
    else                       -> audio/motion chunk   (section 3.4)
}
```

The `JVID` magic is what makes video separable. Everything else binary is a
bare chunk belonging to a previously announced JSON message.

### 3.2 JSON messages you will receive

| `header.name` | Payload | Meaning |
|---|---|---|
| `AvatarSessionInitialized` | — | Init accepted; you may start sending audio. |
| `EchoHumanText` | `stream_key`, `mode`, `text`, `end_of_speech` | **ASR result** — what the user said. Incremental. |
| `EchoAvatarText` | same shape | **LLM reply text.** Incremental; `end_of_speech: true` closes the stream. |
| `EchoAvatarAudio` | `stream_key`, `transport` (`base64`), `format`, `sample_rate` (24000), `channels`, `data_base64`, `end_of_speech` | TTS audio, base64-inline in the message (§3.4). One per avatar audio slice. |
| `AvatarVideoWelcome` | `codec`, `width`, `height`, `fps`, `stream_key` | **New.** A video stream is starting. |
| `AvatarVideoEnd` | `stream_key` | **New.** That utterance's video is complete. |
| `AvatarHeartbeat` | — | Reply to `TriggerHeartbeat`. |
| `InterruptAccepted` | `target_stream_id`, `stream_key` | Your `Interrupt` was honored. |
| `InterruptNotification` | `stream_key`, `reason` (`user_interrupt`\|`semantic_interrupt`), `interrupted_at` | Server-side barge-in (VAD detected the user speaking). **Flush your audio and video buffers for that `stream_key`.** |
| `ChatSignal` | `type`, `source_type`, `stream_key`, `timestamp`, `signal_data` | Stream lifecycle events (`STREAM_BEGIN`, `STREAM_CANCEL`, …). Optional to consume. |
| `Error` | `code`, `message` | Protocol error; see §5. |

`stream_key` is the join key across all streams — the same value appears on the
text, audio, and video messages belonging to one utterance. Use it to correlate
lip-sync with playback and to drop the right buffers on interrupt.

### 3.3 Avatar video frames — binary, `JVID`

Each binary WebSocket message is **exactly one JPEG frame** with a 32-byte
little-endian header. No segmentation, no announcement, no ordering dependency.

```
Offset  Size  Type      Field          Notes
------  ----  --------  -------------  ------------------------------------
 0       4    char[4]   magic          ASCII "JVID" (0x4A 0x56 0x49 0x44)
 4       1    uint8     version        currently 1
 5       1    uint8     codec          0 = JPEG
 6       2    uint16le  width          pixels
 8       2    uint16le  height         pixels
10       4    uint32le  frame_index    monotonic from 0, resets each session
14       4    uint32le  batch_size     frames in the source batch (see below)
18       8    uint64le  timestamp_ms   session clock, ms since session start
26       4    uint32le  data_size      JPEG payload length in bytes
30       2    —         reserved       ignore; may be non-zero in future
------  ----
32       N    bytes     payload        one complete JPEG image
```

C header for reference:

```c
#pragma pack(push, 1)
typedef struct {
    char     magic[4];      /* "JVID" */
    uint8_t  version;
    uint8_t  codec;         /* 0 = JPEG */
    uint16_t width;
    uint16_t height;
    uint32_t frame_index;
    uint32_t batch_size;
    uint64_t timestamp_ms;
    uint32_t data_size;
    uint8_t  reserved[2];
} jvid_header_t;            /* 32 bytes */
#pragma pack(pop)
```

Decoding notes:

- The payload is a **complete, independent JPEG** — no inter-frame prediction,
  so any single packet can be decoded and displayed on its own. A dropped
  packet costs one frame, nothing more.
- `batch_size` > 1 means the avatar handler emitted several frames at once (it
  batches for throughput). Those frames arrive as consecutive packets sharing
  one `timestamp_ms`, with consecutive `frame_index` values. Pace them out at
  `fps` from `AvatarVideoWelcome` rather than displaying all at once.
- `timestamp_ms` is the **audio** clock for this frame — align it against the
  `EchoAvatarAudio` stream to keep lips in sync with sound.
- Frames are the avatar's rendered face, typically 512×512 for LiteAvatar.

### 3.4 Avatar audio — base64 inside the JSON

Unlike the uplink, the audio **downlink does not use binary chunks.** Each
`EchoAvatarAudio` message carries its PCM inline:

```json
{
  "header": { "name": "EchoAvatarAudio", "request_id": "..." },
  "payload": {
    "stream_key": "stream_7475773_0",
    "transport": "base64",
    "format": "PCM",
    "sample_rate": 24000,
    "channels": 1,
    "data_base64": "<base64 of s16le PCM>",
    "end_of_speech": false,
    "metadata": { }
  }
}
```

Decode `data_base64` and append to that `stream_key`'s playback buffer. The
server emits one message per avatar audio slice (roughly one per video frame
batch), so expect a steady stream of small messages rather than a few large
ones. `end_of_speech: true` closes the stream.

Note the rate is **24 kHz**, not the 16 kHz you sent uplink — the TTS output
rate differs from the mic input rate.

Because audio is inline, the only *binary* traffic on the socket is video
(`JVID`) and, if you subscribe to `motion_data`, motion chunks. The demux in
§3.1 still holds.

### 3.5 Why video is self-describing

The `motion_data` downlink uses "announce in JSON, then N headerless chunks",
and the server takes its send lock **per message**. Headerless chunks are
matched to their announcement **by FIFO order**, so any second segmented
producer on the same socket would make the interleaving undecodable — you
could not tell which announcement a given chunk belonged to. (The audio
downlink sidesteps this by going base64-inline, at the cost of ~33% bandwidth.)

Video packets therefore carry their own header and can be demultiplexed on
arrival no matter how they interleave with anything else.

Practical consequence: **video frames may arrive interleaved between a motion
announcement and its chunks.** That is expected and harmless, precisely because
`JVID` lets you pull them out. Filter them out before feeding bytes to a motion
assembler.

---

## 4. Minimal Node.js client

```js
import fs from "node:fs";
import WebSocket from "ws";

const MAGIC = Buffer.from("JVID");
const HEADER_SIZE = 32;

const ws = new WebSocket(`wss://127.0.0.1:8282/ws/session/${sessionId}`, {
  ca: fs.readFileSync("<Openavatarchat>/ssl_certs/localhost.crt"),
});

ws.on("open", () => {
  ws.send(JSON.stringify({
    header: { name: "InitializeAvatarSession", request_id: "init-1" },
    payload: { audio: { format: "PCM", sample_rate: 16000, channels: 1 } },
    // subscriptions omitted => subscribe to everything, avatar_video included
  }));
});

ws.on("message", (data, isBinary) => {
  if (!isBinary) {
    const msg = JSON.parse(data.toString());
    switch (msg.header.name) {
      case "AvatarSessionInitialized": startSendingAudio(); break;
      case "EchoHumanText":   onAsrText(msg.payload);      break;
      case "EchoAvatarText":  onLlmText(msg.payload);      break;
      case "EchoAvatarAudio":
        if (msg.payload.data_base64) {
          onAudioChunk(msg.payload.stream_key,
                       Buffer.from(msg.payload.data_base64, "base64"),
                       msg.payload.sample_rate);
        }
        break;
      case "AvatarVideoWelcome": onVideoStart(msg.payload); break;
      case "AvatarVideoEnd":     onVideoEnd(msg.payload);   break;
      case "InterruptNotification": flush(msg.payload.stream_key); break;
      case "Error": console.error("protocol error", msg.payload); break;
    }
    return;
  }

  if (data.subarray(0, 4).equals(MAGIC)) {
    const frame = {
      version:      data.readUInt8(4),
      codec:        data.readUInt8(5),
      width:        data.readUInt16LE(6),
      height:       data.readUInt16LE(8),
      frameIndex:   data.readUInt32LE(10),
      batchSize:    data.readUInt32LE(14),
      timestampMs:  Number(data.readBigUInt64LE(18)),
      dataSize:     data.readUInt32LE(26),
    };
    const jpeg = data.subarray(HEADER_SIZE, HEADER_SIZE + frame.dataSize);
    onVideoFrame(frame, jpeg);             // e.g. feed an <img> or canvas
    return;
  }

  // Not video: a headerless motion_data chunk (only if you subscribed to it).
  // FIFO-matched to the most recent MotionData announcement.
  onMotionChunk(data);
});

// Keep-alive: the server drops a session after 30s with no inbound message.
// Audio upload counts as traffic; during silence you must ping explicitly.
setInterval(() => {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      header: { name: "TriggerHeartbeat", request_id: `hb-${Date.now()}` },
    }));
  }
}, 10_000);

// Uplink: base64 transport avoids the segmentation hazard entirely.
function sendPcm(pcmBuffer) {
  ws.send(JSON.stringify({
    header: { name: "SendHumanAudio", request_id: `a-${Date.now()}` },
    payload: { transport: "base64", data_base64: pcmBuffer.toString("base64") },
  }));
}

// Or skip ASR entirely — send text straight to the LLM/TTS/avatar chain.
function sendText(text, turnId) {
  ws.send(JSON.stringify({
    header: { name: "SendHumanText", request_id: `t-${Date.now()}` },
    payload: { stream_key: turnId, mode: "full_text", text, end_of_speech: true },
  }));
}
```

---

## 5. Errors and edge cases

- **`Error` with `code`** — the server reports protocol problems rather than
  closing. Codes include `INVALID_MESSAGE` (wrong message for this role, or an
  unparseable envelope) and `BINARY_DATA_ERROR` (missing `binary_size` /
  `segment_num`, bad base64, `segment_num <= 0`). Read `payload.message`.
- **No video at all** — almost always one of: `subscriptions` was sent without
  `avatar_video`; the config uses the stock `ws_client` handler instead of
  `client/ws_video_client/ws_video_client_handler`; or the avatar worker is
  still cold (next bullet). Otherwise video starts immediately: the avatar
  streams **idle animation continuously from session start**, so there is
  always a face to show even before anyone speaks.
- **First session is slow (~60–90 s).** LiteAvatar loads ~1.2 GB of weights per
  worker on first use (`audio2mouth` ~54 s, then ~20 s of avatar data). During
  that window you get a session, but no frames. Later sessions reuse the warm
  worker and start in ~1 s. **`GET /readiness` returns 200 before this
  finishes** — it only gates handler registration, not avatar warm-up. If you
  need a true "can render now" probe, open a throwaway session and wait for
  `AvatarVideoWelcome`.
- **Session dies after ~30 s of silence** — `HEARTBEAT_TIMEOUT`. Audio upload
  counts as traffic, but once you stop sending you must ping with
  `TriggerHeartbeat` (see §4). The server sends `Error` then closes cleanly.
- **Connection closed right after connect** — you are a `listener` on a
  session_id that already has a primary, or you sent a non-init message before
  `InitializeAvatarSession`.
- **Frame ordering** — trust `frame_index`, not arrival order, if you buffer.
  WebSockets preserve order within a connection, so this only matters across
  reconnects.
- **Backpressure** — measured on this box: 890×1920 JPEG at quality 85 is
  ~90 KB/frame, i.e. **~2.3 MB/s at 25 fps**, plus base64 audio (~1.3× raw).
  Check `ws.bufferedAmount` before sending large uplink bursts, and drop frames
  rather than queueing them unboundedly if you cannot keep up.

---

## 6. Server-side configuration

`config/chat_with_liteavatar_ws_video.yaml`:

```yaml
default:
  chat_engine:
    handler_configs:
      WsVideoClient:
        module: client/ws_video_client/ws_video_client_handler
        connection_ttl: 900      # session lifetime, seconds
        heartbeat_timeout: 30    # idle seconds before the session is dropped
        jpeg_quality: 85         # 1-100; lower = less bandwidth, softer face
        video_fps: 25            # advertised in AvatarVideoWelcome; match the avatar handler's fps
```

Start it with:

```bash
export DASHSCOPE_API_KEY=...        # LLM + CosyVoice TTS read it from the env
.venv/bin/python src/demo.py --config config/chat_with_liteavatar_ws_video.yaml --host 127.0.0.1
```

Use `.venv/bin/python`, **not** `uv run`: `uv run` performs an implicit sync
against the lockfile and will uninstall the handler dependencies that
`install.py` added with `uv pip install` (they are not in the root
`pyproject.toml`).

Health probes: `GET /liveness` → `{"status":"ok"}`; `GET /readiness` → 200 once
every handler has *registered* (500 before that). Note `/readiness` does **not**
cover LiteAvatar warm-up — see §5.

**Security caveat:** at INFO log level the server prints each handler's resolved
config, which includes the DashScope API key in plaintext, to stdout. Either run
with `logger.log_level: WARNING`, redirect the log somewhere private, or scrub
it. Do not ship that log file.
