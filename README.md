<h1 style='text-align: center; margin-bottom: 1rem'> Open Avatar Chat </h1>

<p align="center">
<strong>中文 | <a href="readme_en.md">English</a></strong>
</p>

<p align="center">
<strong>TL;DR: 为 <a href="https://github.com/QwenAudio/qwen-audio-agent">Qwen-Audio-Agent</a> 提供自部署的数字人 API 服务。</strong>
</p>

这是 [HumanAIGC-Engineering/OpenAvatarChat](https://github.com/HumanAIGC-Engineering/OpenAvatarChat) 的一个**非官方下游分支**，与上游没有隶属关系。

上游是一套完整的数字人对话系统，VAD、ASR、LLM、TTS 都在里面。这个分支只取了渲染那一层，把它做成一个 API，好让外面**已经有的**语音 agent 拿来当后台。所以 [qwen-audio-agent](https://github.com/QwenAudio/qwen-audio-agent) 这样的 agent 照常自己听懂、自己想、自己合成语音，只是把合成出来的 PCM 音频递给这个服务，服务实时把音频和人脸视频**配好对**还回来，嘴型是对得上的。这条链路里没有 VAD、ASR、LLM、TTS，只有数字人渲染。

这份 README 分成两半：**快速开始**是给部署方的，在 GPU 机上把服务跑起来；**下游 agent 要配什么**是给接入方的，通常在另一台机器上。

## 快速开始

需要一张 GPU，不需要 DashScope key。

```bash
git clone --recursive https://github.com/juhayna-zh/OpenAvatarChat.git
cd OpenAvatarChat

uv run install.py --config config/chat_with_flashhead_passthrough.yaml
.venv/bin/python scripts/download_models.py --config config/chat_with_flashhead_passthrough.yaml

openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout ssl_certs/localhost.key -out ssl_certs/localhost.crt \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

# 渲染器，:8284，模型加载约 40~60 秒
.venv/bin/python src/demo.py --config config/chat_with_flashhead_passthrough.yaml --host 127.0.0.1
```

再开一个 shell，起桥接：

```bash
.venv/bin/python -m dhav_bridge --video-size 256
```

到这一步服务已经在 `http://127.0.0.1:18910` 上了，可以直接跳去看下一节怎么接。想先确认它真的能出画面、而不只是进程活着，就跑一次自检。`--input` 随便给一个音频文件，librosa 能读的格式都行：

```bash
.venv/bin/python -m dhav_bridge.smoke_client --base-url http://127.0.0.1:18910 \
  --input ~/reply.wav --out ~/dhav-check --realtime
```

跑通会打印 `status ok`、`publish_rate_hz` ≈ 25、`realtime_factor` ≈ 1.0，`~/dhav-check/` 里留下可以直接听和看的 `output.wav` 和 `frames/*.png`。

有两个坑值得先说：

- **装完之后一律用 `.venv/bin/python`，别用 `uv run`**（上面那条 `uv run install.py` 是唯一的例外，它就是安装器本身）。`uv run` 会对着 lockfile 做一次隐式 sync，把 `install.py` 用 `uv pip install` 装进去的 handler 依赖**卸掉**——那些依赖不在根 `pyproject.toml` 里。症状是下次启动时 handler 无声消失，或者 import 直接报错。
- **证书必须带 SAN**，所以别用仓库自带的 `scripts/create_ssl_certs.sh`。它跑的 `openssl req` 既没有 `-subj` 也没有 `-addext`，会卡在交互提问上；就算你一路答完，生成的证书也没有 SAN，主机名校验必然失败。

### 让别的机器连进来

上面跑完之后 `:18910` 只绑在 `127.0.0.1`，本机以外谁都连不到。要让你的网关从另一台机器直连，得先生成一个 token，再带着它重启桥接：

```bash
mkdir -p ~/.config/dhav-bridge && chmod 700 ~/.config/dhav-bridge
openssl rand -hex 32 > ~/.config/dhav-bridge/token && chmod 600 ~/.config/dhav-bridge/token

DHAV_AUTH_TOKEN=$(cat ~/.config/dhav-bridge/token) \
  .venv/bin/python -m dhav_bridge --host 0.0.0.0 --port 18910 --video-size 256
```

这个 token 没什么玄机，就是 `openssl rand -hex 32` 随机出来的 64 位十六进制串，生成一次存进文件，之后每次启动都从文件里读。它不是可选项：桥接发现自己要绑非 loopback 地址却没拿到 token，会拒绝启动并返回退出码 2——因为真那样一来，任何能路由到这个端口的人都能建会话、把 GPU 占住。绑 `127.0.0.1` 的时候不做这个检查，所以快速开始里不需要 token。

还有一件事：`:18910` 是明文 HTTP，token 是唯一防线。如果不希望 token 走网线，那就别开端口，改走 SSH 隧道，桥接这边一个字都不用改：

```bash
# 在网关那台机器上执行
ssh -N -L 18910:127.0.0.1:18910 <gpu-host>
```

之后网关连 `http://127.0.0.1:18910`，不开端口、不配 TLS，也不需要 token。

顺带一提，如果你的 `~` 落在会被重置的地方（容器里的 `/root` 就很典型），把 token 放到持久盘上再软链回来，否则机器一重启 token 就没了，网关那边也得跟着换。

## 下游 agent 要配什么

这一节写给接入方，也就是跑 [qwen-audio-agent](https://github.com/QwenAudio/qwen-audio-agent) 的那台机器。

**你需要从部署方拿到两样东西：服务地址，和那个 token。** 写进 `examples/digital-human/.env.local`：

```bash
DH_RENDERER_URL=http://<GPU 服务器>:18910
DH_PERSONA=flashhead-default
DH_RENDERER_TOKEN=<~/.config/dhav-bridge/token 文件里的 64 位 hex>
```

`DH_RENDERER_URL` 一旦非空，`bootstrap/start.mjs` 就不会再去起本地 Mock 渲染器。它必须写 `http://` 而不是 `ws://`，因为 preflight 会拿它去 `fetch(<url>/health/ready)`。走 SSH 隧道的话就写 `http://127.0.0.1:18910`，`DH_RENDERER_TOKEN` 那一行可以不要。

**注意 example 现成的代码不会把 token 传下去**，所以服务端一开鉴权就会 401。要在你自己那份 `qwen-audio-agent` 里改两处：

- `bootstrap/env.mjs`：`readConfig()` 里多读一个键，`rendererToken: env.DH_RENDERER_TOKEN || null`。
- `bootstrap/compose-gateway.mjs`：把 `authToken` 传进 `createDhavProvider()`；再把 `flashhead-default` 加进 persona 白名单——现成代码只认 `MOCK_PERSONAS`，不加会抛 `persona ... is not in the server allowlist`。加的时候 `avatarAssetId` 必须是非空字符串（`assertDigitalHumanOptions` 的硬要求），值本身随意，形象由服务端决定。

协议层的代码不用动，`dhav.mjs` / `dhav-provider.mjs` / `framed-av-source.mjs` 原样可用。改完照原样 `node bootstrap/start.mjs`，浏览器开 `http://<gateway-host>:18895/?avatar=flashhead-default`。

### 怎么判断服务活着

```bash
curl -s http://<GPU 服务器>:18910/health/ready
```

| 端点 | 回答的问题 | 判据 |
| --- | --- | --- |
| `GET /health/live` | 桥接进程活着吗 | `{"live": true}` |
| `GET /health/ready` | 现在能出画面吗 | 看 body 里的 `ready` |

两个都不需要 token。但 `/health/ready` 有个反直觉的地方：**它的 HTTP 状态码永远是 200**，结论只在 body 里：

```json
{"ready": true, "fault": null, "renderer": "http 200"}
```

`ready: true` 说明桥接真的去探了一下 `:8284` 渲染器并拿到了响应，所以它比渲染器自己的 `/readiness` 可信得多——后者只反映 handler 注册成功，渲染器卡死时照样返回 200。`ready: false` 时，`fault` 和 `renderer` 会告诉你断在哪一层。网关的 preflight 用的就是这个端点。

再往下的事——DHAV v1 的字节级线协议、轮次状态机、`start`/`finish`/`cancel` 的语义、下行 `PAIRED_AV` 长什么样、24 条坑——都在 [`dhav_bridge/README.md`](dhav_bridge/README.md)。

## 我该用哪个服务？

四个端口都需要 GPU。对这个分支的主要目的（给 [qwen-audio-agent](https://github.com/QwenAudio/qwen-audio-agent) 当数字人后台）来说，只用得上 `:18910` 和 `:8284`。

| 端口 | 是什么 | DashScope key | 输入 → 输出 | 文档 |
| --- | --- | --- | --- | --- |
| **18910** | DHAV v1 渲染服务，给外部 agent 网关用 | 不需要 | 助手回复的 PCM 音频 → 配好对的 I420 视频 + 音频，25 fps | [`dhav_bridge/README.md`](dhav_bridge/README.md) |
| 8284 | 纯渲染透传，是 18910 的底座 | 不需要 | `MIC_AUDIO` → `AVATAR_AUDIO` + `JVID` 视频帧 | [`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md) |
| 8283 | 完整对话链路 + FlashHead（仅作为功能保留、与本项目的主要目的无关） | **需要** | 麦克风 / 文本 → 数字人视频 | 上游 |
| 8282 | 完整对话链路 + LiteAvatar（仅作为功能保留、与本项目的主要目的无关） | **需要** | 麦克风 / 文本 → 数字人视频 | 上游 |

`:8284` **没有任何鉴权**——没有 token、不校验 Origin、也没有白名单，所以只能绑 `127.0.0.1`，快速开始里已经是这么做的。要跨机就走 SSH 隧道，别把它直接暴露出去。四个端口里只有 `:18910` 有 Bearer token 鉴权。

## 这个分支加了什么

| 模块 | 规模 | 作用 |
| --- | --- | --- |
| [`dhav_bridge/`](dhav_bridge/) | 9 文件 / ~3400 行 / 44 测试 | **DHAV v1** 渲染服务（`:18910`）：二进制 WebSocket 协议、Bearer 鉴权、轮次状态机、音视频配对下发 |
| [`src/handlers/logic/audio_passthrough/`](src/handlers/logic/audio_passthrough/) | 2 文件 / 210 行 | 纯渲染透传 handler：`MIC_AUDIO` 进、`AVATAR_AUDIO` 出，按上行停包断句 |
| [`src/handlers/client/ws_video_client/`](src/handlers/client/ws_video_client/) | 4 文件 / 285 行 + 480 行协议文档 | WebSocket 视频下行 handler（上游只有 WebRTC，Node 网关当不了 peer） |
| 3 个新 config | 176 行 | `:8284` 透传渲染；`:8282`/`:8283` 完整链路的 WS 视频版 |

有三个旋钮值得知道：

- `FlashHead.idle_inference: false` —— 空闲动画推理会一直占着 GPU 和 `_inference_lock`，把语音推理推迟 2~5 秒。只在透传配置里关掉它，因为桥接本来就只在轮次内下发媒体，用不上空闲帧。
- `AudioPassthrough.gap_seconds: 0.4` —— 上行停包多久算一句话说完，同时也是每句结尾多出来的那点延迟。断句看的是**你有没有停止发包**，不是音频内容里有没有静音。
- `service.concurrent_limit: 1` —— 单卡 FlashHead 一次只能渲染一个会话。

## 对上游的改动

3 个文件，`+28/−5`（其中 9 行是变更声明注释），全在 `src/handlers/avatar/flashhead/` 下，只加了一个配置项：

| 文件 | 改动 |
| --- | --- |
| `flashhead_config.py` | 新增 `idle_inference: bool = Field(default=True, ...)` |
| `avatar_handler_flashhead.py` | 把该值传给 `FlashHeadProcessor(...)` |
| `flashhead_processor.py` | 空闲推理线程的创建与启动改为受该开关控制 |


<details>
  <summary>已知坑</summary>


1. **上行实际是 16 kHz，不是协议声明的 24 kHz。** 上游把 `MIC_AUDIO` 的采样率写死成 16000 并忽略声明值，桥接内部已重采样。**照协议发 24 kHz 就对了**，别去"修"——自己发 16 kHz 的结果是语速快 1.5 倍、音调低 1.5 倍。
2. **渲染器的下行永远不安静。** FlashHead 的帧收集器是严格 25 fps 的节拍器，没内容就发静止帧 + 全零音频，一直到会话结束。所以「多久没收到数据」永远不能用来判断一句话说完了；桥接靠上游结构性的 `ChatSignal stream_end` 标记。
3. **`GET /readiness` 返回 200 不等于能出画面。** 它只反映 handler 注册成功，不反映推理吞吐，渲染器卡死时照样返回 200。要判断能不能出画面，用桥接的 `/health/ready` 并读它的 body。
4. **渲染偶发变慢**，原因在渲染器进程内部，重启 `:8284` 就好。判据是 `smoke_client` 报的 `publish_rate_hz`（应 ≈25）和 `max_unit_gap_s`。

其余在 [`dhav_bridge/README.md`](dhav_bridge/README.md#pitfalls) 和 [`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md)。

</details>



## 上游与许可

感谢本仓库的上游项目：<https://github.com/HumanAIGC-Engineering/OpenAvatarChat>（v0.6.0）。
本仓库依据 **Apache-2.0** 分发（见 [`LICENSE`](LICENSE)），"OpenAvatarChat" 名称与标识仍归上游项目所有者所有。
