<h1 style='text-align: center; margin-bottom: 1rem'> Open Avatar Chat </h1>

<p align="center">
<strong>中文 | <a href="readme_en.md">English</a></strong>
</p>

<p align="center">
<strong>TL;DR: 为 <a href="https://github.com/QwenAudio/qwen-audio-agent">Qwen-Audio-Agent</a> 提供自部署的数字人 API 服务。</strong>
</p>

这是 [HumanAIGC-Engineering/OpenAvatarChat](https://github.com/HumanAIGC-Engineering/OpenAvatarChat) 的一个**非官方下游分支**，与其无隶属关系。

本分支在原版 OpenAvatarChat 之上加了一层**给外部语音 agent 当后台的渲染 API**：你的 agent（如 [qwen-audio-agent](https://github.com/QwenAudio/qwen-audio-agent)）自己负责对话与 TTS，把回复的 PCM 音频交给这个服务，它实时返回**已经配好对**的音频与人脸视频，嘴型对得上。这条链路里没有 VAD、ASR、LLM、TTS，只提供数字人 API 服务。

## 快速开始

要一张 GPU，不需要 DashScope key。

```bash
git clone --recursive https://github.com/juhayna-zh/OpenAvatarChat.git
cd OpenAvatarChat

uv run install.py --config config/chat_with_flashhead_passthrough.yaml
.venv/bin/python scripts/download_models.py --config config/chat_with_flashhead_passthrough.yaml

openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout ssl_certs/localhost.key -out ssl_certs/localhost.crt \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

# 渲染器，:8284，模型加载约 40 秒
.venv/bin/python src/demo.py --config config/chat_with_flashhead_passthrough.yaml --host 127.0.0.1
```

另开一个 shell，起桥接并自检：

```bash
.venv/bin/python -m dhav_bridge --video-size 256

.venv/bin/python -m dhav_bridge.smoke_client --base-url http://127.0.0.1:18910 \
  --input /path/to/reply.wav --out ~/dhav-check --realtime
```

跑通会打印 `status ok`、`publish_rate_hz` ≈ 25、`realtime_factor` ≈ 1.0，`~/dhav-check/` 里有可以直接听和看的 `output.wav` 与 `frames/*.png`。

两个坑需要注意：

- **装完之后一律用 `.venv/bin/python`，别用 `uv run`**（上面那条 `uv run install.py` 是唯一例外，它就是安装器本身）。`uv run` 会对着 lockfile 做一次隐式 sync，把 `install.py` 用 `uv pip install` 装进去的 handler 依赖**卸掉**——它们不在根 `pyproject.toml` 里。症状是下次启动时 handler 无声消失或 import 报错。
- **证书必须带 SAN**，所以别用仓库自带的 `scripts/create_ssl_certs.sh`：它跑的 `openssl req` 既没有 `-subj` 也没有 `-addext`，会卡在交互提问，且生成的证书没有 SAN，主机名校验必然失败。

要让别的机器连进来必须给 token（不给就不让绑非 loopback，进程直接退出码 2）：

```bash
mkdir -p ~/.config/dhav-bridge && chmod 700 ~/.config/dhav-bridge
openssl rand -hex 32 > ~/.config/dhav-bridge/token && chmod 600 ~/.config/dhav-bridge/token
DHAV_AUTH_TOKEN=$(cat ~/.config/dhav-bridge/token) \
  .venv/bin/python -m dhav_bridge --host 0.0.0.0 --port 18910 --video-size 256
```

不过更推荐 SSH 隧道：网关侧 `ssh -N -L 18910:127.0.0.1:18910 <gpu-host>`，之后直接连 `http://127.0.0.1:18910`——不开端口、不配 TLS、token 也不上网线。

协议、端点、鉴权、轮次生命周期、实测数字和 24 条坑都在 [`dhav_bridge/README.md`](dhav_bridge/README.md)。

## 我该用哪个服务？

四个端口都需要 GPU。对于本项目的主要目的，即为 [qwen-audio-agent](https://github.com/QwenAudio/qwen-audio-agent) 提供数字人 API 服务来说，只需要配置 `:18910` 和 `:8284`。

| 端口 | 是什么 | DashScope key | 输入 → 输出 | 文档 |
| --- | --- | --- | --- | --- |
| **18910** | DHAV v1 渲染服务，给外部 agent 网关用 | 不需要 | 助手回复的 PCM 音频 → 配好对的 I420 视频 + 音频，25 fps | [`dhav_bridge/README.md`](dhav_bridge/README.md) |
| 8284 | 纯渲染透传，是 18910 的底座 | 不需要 | `MIC_AUDIO` → `AVATAR_AUDIO` + `JVID` 视频帧 | [`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md) |
| 8283 | 完整对话链路 + FlashHead（仅作为功能保留、与本项目的主要目的无关） | **需要** | 麦克风 / 文本 → 数字人视频 | 上游 |
| 8282 | 完整对话链路 + LiteAvatar（仅作为功能保留、与本项目的主要目的无关） | **需要** | 麦克风 / 文本 → 数字人视频 | 上游 |

其中 `:8284` **没有任何鉴权**（无 token、不校验 Origin、无白名单），务必只绑 `127.0.0.1`——上面的快速开始已经是这么做的；要跨机访问就走 SSH 隧道，别把它直接暴露出去。只有 `:18910` 有 Bearer token 鉴权。


## 这个分支加了什么

| 模块 | 规模 | 作用 |
| --- | --- | --- |
| [`dhav_bridge/`](dhav_bridge/) | 9 文件 / ~3400 行 / 44 测试 | **DHAV v1** 渲染服务（`:18910`）：二进制 WebSocket 协议、Bearer 鉴权、轮次状态机、音视频配对下发 |
| [`src/handlers/logic/audio_passthrough/`](src/handlers/logic/audio_passthrough/) | 2 文件 / 210 行 | 纯渲染透传 handler：`MIC_AUDIO` 进、`AVATAR_AUDIO` 出，按上行停包断句 |
| [`src/handlers/client/ws_video_client/`](src/handlers/client/ws_video_client/) | 4 文件 / 285 行 + 480 行协议文档 | WebSocket 视频下行 handler（上游只有 WebRTC，Node 网关当不了 peer） |
| 3 个新 config | 176 行 | `:8284` 透传渲染；`:8282`/`:8283` 完整链路的 WS 视频版 |

值得知道的三个旋钮：

- `FlashHead.idle_inference: false` —— 空闲动画推理会一直占着 GPU 和 `_inference_lock`，把语音推理推迟 2~5 秒。只在透传配置里关掉，因为桥接本来就只在轮次内下发媒体，用不上空闲帧。
- `AudioPassthrough.gap_seconds: 0.4` —— 上行停包多久算一句话说完，也是每句结尾多出来的延迟。断句看的是**你有没有停止发包**，不是音频内容里有没有静音。
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
3. **`GET /readiness` 返回 200 不等于能出画面。** 它只反映 handler 注册成功，不反映推理吞吐，渲染器卡死时照样返回 200。
4. **渲染偶发变慢**，原因在渲染器进程内部，重启 `:8284` 就好。判据是 `smoke_client` 报的 `publish_rate_hz`（应 ≈25）和 `max_unit_gap_s`。

其余在 [`dhav_bridge/README.md`](dhav_bridge/README.md#pitfalls) 和 [`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md)。

</details>



## 上游与许可

感谢本仓库的上游项目：<https://github.com/HumanAIGC-Engineering/OpenAvatarChat>（v0.6.0）。
本仓库依据 **Apache-2.0** 分发（见 [`LICENSE`](LICENSE)），"OpenAvatarChat" 名称与标识仍归上游项目所有者所有。
