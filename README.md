<h1 style='text-align: center; margin-bottom: 1rem'> Open Avatar Chat </h1>

<p align="center">
<strong>中文 | <a href="readme_en.md">English</a></strong>
</p>

<p align="center">
<strong>模块化的交互数字人对话实现。</strong>
</p>

<p align="center" style="display: flex; flex-direction: row; justify-content: center">
 🤗 <a href="https://huggingface.co/spaces/HumanAIGC-Engineering-Team/open-avatar-chat">Demo</a>&nbsp&nbsp|&nbsp&nbsp<img alt="Static Badge" style="height: 10px;" src="./assets/images/modelscope_logo.png"> <a href="https://www.modelscope.cn/studios/HumanAIGC-Engineering/open-avatar-chat">Demo</a>&nbsp&nbsp|&nbsp&nbsp💬 <a href="https://github.com/HumanAIGC-Engineering/OpenAvatarChat/blob/main/assets/images/OpenAvatarChat.png">WeChat (微信)</a>&nbsp&nbsp|&nbsp&nbsp📖 <a href="https://humanaigc-engineering.github.io/OpenAvatarChat/">文档</a>
</p>

> [!NOTE]
> **这是一个非官方下游分支**，与 HumanAIGC-Engineering 无隶属关系，也未获其背书。
>
> 上游项目是一个完整的「语音 → 数字人对话」应用。本分支在它之上加了一层**给外部语音 agent 当后台的渲染 API**：
> 你的 agent 自己负责对话与 TTS，把回复的 PCM 音频流交给这个服务，它实时返回**已经配好对**的音频与人脸视频（嘴型对得上）。
> 这条链路里没有 VAD、ASR、LLM、TTS。
>
> 除一个 `idle_inference` 开关外，上游代码未被修改；新增内容全部是自包含的独立模块。详见[对上游的唯一改动](#对上游的唯一改动)。

## 这个分支新增了什么

| 模块 | 规模 | 作用 |
| --- | --- | --- |
| [`dhav_bridge/`](dhav_bridge/) | 9 文件 / ~3400 行 / 44 测试 | **DHAV v1** 渲染服务（`:18910`）：二进制 WebSocket 协议、Bearer 鉴权、轮次状态机、音视频配对下发 |
| [`src/handlers/logic/audio_passthrough/`](src/handlers/logic/audio_passthrough/) | 2 文件 / 210 行 | 纯渲染透传 handler：`MIC_AUDIO` 进、`AVATAR_AUDIO` 出，按上行停包断句 |
| [`src/handlers/client/ws_video_client/`](src/handlers/client/ws_video_client/) | 4 文件 / 285 行 + 480 行协议文档 | WebSocket 视频下行 handler（上游只有 WebRTC，Node 网关当不了 peer） |
| 3 个新 config + `idle_inference` | — | `:8284` 透传渲染、`:8282`/`:8283` 完整链路的 WS 视频版 |

## 我该用哪个服务？

| 端口 | 是什么 | DashScope key | GPU | 输入 | 输出 | 文档 |
| --- | --- | --- | --- | --- | --- | --- |
| **18910** | DHAV v1 渲染服务，给外部 agent 网关用 | 不需要 | 需要 | 助手回复的 PCM 音频 | 配好对的 I420 视频 + 音频，25 fps | [`dhav_bridge/README.md`](dhav_bridge/README.md) |
| 8284 | 纯渲染透传，是 18910 的底座 | 不需要 | 需要 | `MIC_AUDIO` | `AVATAR_AUDIO` + `JVID` 视频帧 | [`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md) |
| 8283 | 完整对话链路 + FlashHead | **需要** | 需要 | 麦克风 / 文本 | 数字人视频 | [上游文档](https://humanaigc-engineering.github.io/OpenAvatarChat/) |
| 8282 | 完整对话链路 + LiteAvatar | **需要** | 需要 | 麦克风 / 文本 | 数字人视频 | [上游文档](https://humanaigc-engineering.github.io/OpenAvatarChat/) |

**只想给已有 agent 配一个数字人后台** → 看 18910，走[快速开始 A](#快速开始-adhav-渲染后端不需要-dashscope-key)。
**想要一个开箱即用的语音对话数字人** → 看 8282/8283，走[快速开始 B](#快速开始-b上游完整对话应用)。

## 快速开始 A：DHAV 渲染后端（不需要 DashScope key）

```bash
# 1. 克隆并初始化 submodule（数字人实现都在 submodule 里）
git clone --recursive <本仓库地址>
cd OpenAvatarChat
# 已经克隆过的话：
git submodule update --init --recursive --depth 1

# 2. 装依赖。install.py 本身要用 uv run，这一步是上游的正常安装流程。
uv run install.py --config config/chat_with_flashhead_passthrough.yaml

# 3. 下载模型（SoulX-FlashHead-1_3B + wav2vec2-base-960h）
.venv/bin/python scripts/download_models.py --config config/chat_with_flashhead_passthrough.yaml

# 4. 生成带 SAN 的自签证书。仓库自带的 scripts/create_ssl_certs.sh 不行：
#    它跑的 openssl req 既没有 -subj 也没有 -addext，会卡在交互提问，
#    且生成的证书没有 SAN，主机名校验必然失败。
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout ssl_certs/localhost.key -out ssl_certs/localhost.crt \
  -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

# 5. 起渲染器（:8284，模型加载约 40 秒）
.venv/bin/python src/demo.py \
  --config config/chat_with_flashhead_passthrough.yaml --host 127.0.0.1

# 6. 另开一个 shell，起桥接（:18910）。绑 loopback 不需要 token。
.venv/bin/python -m dhav_bridge --video-size 256

# 7. 端到端自检
.venv/bin/python -m dhav_bridge.smoke_client \
  --base-url http://127.0.0.1:18910 \
  --input /path/to/reply.wav --out ~/dhav-check --realtime
```

跑通的话最后会打印 `status ok`、`publish_rate_hz` ≈ 25、`realtime_factor` ≈ 1.0，
`~/dhav-check/` 里有 `output.wav`、`frames/*.png` 和 `report.json`，可以直接听和看。

> [!WARNING]
> **装完之后一律用 `.venv/bin/python`，不要用 `uv run`。**
> `uv run` 会对着 lockfile 做一次隐式 sync，把 `install.py` 用 `uv pip install` 装进去的
> handler 依赖（funasr、dashscope、`transformers==4.44.2` 等）**卸掉**，因为它们不在根
> `pyproject.toml` 里。症状是下一次启动时 handler 无声消失或 import 报错。
> 第 2 步的 `uv run install.py` 是唯一例外——那正是安装器本身。

要让网关从别的机器连过来，必须给 token（不给就不让绑非 loopback，进程直接退出码 2）：

```bash
mkdir -p ~/.config/dhav-bridge && chmod 700 ~/.config/dhav-bridge
openssl rand -hex 32 > ~/.config/dhav-bridge/token && chmod 600 ~/.config/dhav-bridge/token

DHAV_AUTH_TOKEN=$(cat ~/.config/dhav-bridge/token) \
  .venv/bin/python -m dhav_bridge --host 0.0.0.0 --port 18910 --video-size 256
```

更推荐用 SSH 隧道而不是开端口：网关侧 `ssh -N -L 18910:127.0.0.1:18910 <gpu-host>`
之后直接连 `http://127.0.0.1:18910`——不用改防火墙、不用配 TLS、token 也不上网线。

协议、端点、鉴权、轮次生命周期、实测数字和 24 条坑都在
[`dhav_bridge/README.md`](dhav_bridge/README.md)。

## 快速开始 B：上游完整对话应用

```bash
git submodule update --init --recursive --depth 1
uv run install.py --config config/chat_with_liteavatar_ws_video.yaml
.venv/bin/python scripts/download_models.py --handler liteavatar

export DASHSCOPE_API_KEY=sk-...   # LLM(qwen-plus) + TTS(cosyvoice-v1) 都要用
.venv/bin/python src/demo.py --config config/chat_with_liteavatar_ws_video.yaml --host 127.0.0.1
```

这条链路首次会话要预热约 75 秒（LiteAvatar），启动到 `/readiness` 200 约 190 秒。
上游的完整说明见[快速开始文档](https://humanaigc-engineering.github.io/OpenAvatarChat/getting-started/)。

> [!WARNING]
> `:8282` / `:8283` / `:8284` **都没有鉴权**——没有 token、不校验 Origin、没有白名单。
> 任何能连上端口的人都能起会话并消耗你的 DashScope 额度。务必只绑 `127.0.0.1`。
> 只有 `:18910` 有 Bearer token 鉴权。

## 三个新配置

| CONFIG | 端口 | Handler 链 |
| --- | --- | --- |
| `chat_with_flashhead_passthrough.yaml` | 8284 | WsVideoClient → InterruptHandler → **AudioPassthrough** → FlashHead |
| `chat_with_liteavatar_ws_video.yaml` | 8282 | WsVideoClient → InterruptHandler → SileroVad → SenseVoice → CosyVoice → LLM → LiteAvatar |
| `chat_with_flashhead_ws_video.yaml` | 8283 | 同上，Avatar 换成 FlashHead |

后两个就是上游 `chat_with_openai_compatible_bailian_cosyvoice.yaml` 把 `RtcClient` 换成 `WsVideoClient`。

值得知道的旋钮：

- `FlashHead.idle_inference: false`（仅透传配置里关掉）——空闲动画推理会一直占着 GPU 和
  `_inference_lock`，把语音推理推迟 2~5 秒。关掉之后静默期退化成静态参考帧，而 DHAV 桥接
  本来就只在轮次内下发媒体，用不上空闲帧。
- `AudioPassthrough.gap_seconds: 0.4`——上行停包多久算一句话说完，同时也是每句结尾多出来的延迟。
  注意断句看的是**你有没有停止发包**，不是音频内容里有没有静音。
- `service.concurrent_limit: 1`——单卡 FlashHead 一次只能渲染一个会话。

## 对上游的唯一改动

3 个文件，`+28/−5`（其中 9 行是变更声明注释），全在 `src/handlers/avatar/flashhead/` 下，只加了一个配置项：

| 文件 | 改动 |
| --- | --- |
| `flashhead_config.py` | 新增 `idle_inference: bool = Field(default=True, ...)` |
| `avatar_handler_flashhead.py` | 把该值传给 `FlashHeadProcessor(...)` |
| `flashhead_processor.py` | 空闲推理线程的创建与启动改为受该开关控制 |

**默认值 `True` 就是上游原本的行为**，逐字一致：任何没写这个键的配置都不受影响，只有
`chat_with_flashhead_passthrough.yaml` 主动关掉。三个文件顶部都按 Apache-2.0 第 4(b) 条加了变更声明，
每处改动 hunk 带 `# [fork]` 行尾标记，`git diff upstream/main` 一眼能看全。

其余新增内容都是纯附加的独立目录，不与上游文件交叉。

## 测试

44 个单元测试，不需要 GPU、不需要网络（渲染器和两条 DHAV 通道都是打桩的）：

```bash
.venv/bin/python -m pytest dhav_bridge -q
# 没装 pytest 的话：
.venv/bin/python -m unittest dhav_bridge.test_dhav_bridge -v
```

`test_dhav_bridge.py` 钉住了线格式的字节布局，所以改动编解码会立刻失败，而不是等到某个网关悄悄连不上。

## 已知坑（前 6 条）

1. **上行实际是 16 kHz，不是协议声明的 24 kHz。** 上游把 `MIC_AUDIO` 的采样率写死成 16000 并忽略声明值，
   桥接内部已重采样。**照协议发 24 kHz 就对了**，别去"修"——自己发 16 kHz 的结果是语速快 1.5 倍、音调低 1.5 倍。
2. **渲染器的下行永远不安静。** FlashHead 的帧收集器是严格 25 fps 的节拍器，没内容就发静止帧 + 全零音频，
   一直到会话结束。所以「多久没收到数据」永远不能用来判断一句话说完了；桥接靠上游结构性的
   `ChatSignal stream_end` 标记。
3. **`GET /readiness` 返回 200 不等于能出画面。** 它只反映 handler 注册成功，不反映推理吞吐。
   渲染器卡死时它照样返回 200。
4. **`uv run` 会卸掉 handler 依赖**，见上面的 WARNING。
5. **证书必须带 SAN**，仓库自带的生成脚本不满足，见快速开始 A 第 4 步。
6. **INFO 级日志会明文打印 DashScope key**（handler 注册时会 dump 解析后的完整配置）。
   透传配置没有 DashScope 组件所以无所谓，但完整链路的 INFO 日志不要外发。

其余 18 条（含排障判据和实测阈值）在 [`dhav_bridge/README.md`](dhav_bridge/README.md#pitfalls) 和
[`PROTOCOL.md`](src/handlers/client/ws_video_client/PROTOCOL.md)。

---

## 上游项目

以下是上游 OpenAvatarChat 本身的内容。

### 💡 核心亮点

- **多模态交互支持**：支持文本、语音、视频等多种交互方式，提供自然流畅的人机对话体验
- **模块化架构设计**：采用高度模块化设计，可灵活替换 ASR、LLM、TTS、Avatar 等核心组件
- **多样数字人形象**：支持 LiteAvatar、LAM、MuseTalk、FlashHead 等多种数字人技术
- **低延迟优化**：通过 VAD 检测、语音缓冲、帧率控制等机制优化，平均响应时间仅 2.2 秒

### 📢 最新动态

- [2026.04] ⭐️⭐️⭐️ 版本 0.6.0发布:
  - 架构重构，前后端分离，前端仓库 [OpenAvatarChat-WebUI](https://github.com/HumanAIGC-Engineering/OpenAvatarChat-WebUI)
  - 所有数字人均支持手动打断和双工打断模式
  - 优化安装部署和模型下载流程，统一依赖管理和模型下载脚本
  - 接入 [SoulX-FlashHead](https://github.com/Soul-AILab/SoulX-FlashHead) 数字人，基于扩散模型的实时流式说话头生成
- [2025.08.19] ⭐️⭐️⭐️ 版本 0.5.1发布:
  - LiteAvatar支持单机多session
  - 增加对 Qwen-Omni多模态模型的支持

> 📋 [完整更新日志](https://humanaigc-engineering.github.io/OpenAvatarChat/releases/release-notes)

### 预置模式

| CONFIG名称 | ASR | LLM | TTS | AVATAR |
|----------------------------------------------------|-----|:---------:|:---------:|------------|
| chat_with_lam.yaml | SenseVoice | API | API | LAM |
| chat_with_qwen_omni.yaml | Qwen-Omni | Qwen-Omni | Qwen-Omni | lite-avatar |
| chat_with_openai_compatible_bailian_cosyvoice.yaml | SenseVoice | API | API | lite-avatar |
| chat_with_openai_compatible_bailian_cosyvoice_flashhead.yaml | SenseVoice | API | API | FlashHead |
| chat_with_openai_compatible_bailian_cosyvoice_flashhead_duplex.yaml | SenseVoice | API | API | FlashHead (双工) |
| chat_with_openai_compatible_bailian_cosyvoice_flashhead_duplex_agent.yaml | SenseVoice | **Agent** | API | FlashHead (双工+Agent) Beta |

> 📖 [查看全部预置模式](https://humanaigc-engineering.github.io/OpenAvatarChat/reference/preset-modes)

### 🧪 Beta 功能：Chat Agent 模式（OpenClaw 集成）

> [!WARNING]
> 此功能目前处于 **Beta** 阶段，API 和配置格式可能随时变化。

Chat Agent 模式使用多轮工具调用 Agent 替代传统 LLM Handler，为数字人提供：

- **工具调用**：多轮调用工具（获取时间、系统信息等）
- **人格与长期记忆**：通过 OpenClaw 的 Agent Profile 赋予数字人持久人格
- **对话上下文压缩**：自动压缩过长的对话历史
- **后台任务协作**：通过 OpenClaw 在后台执行复杂任务
- **视觉感知**：结合 PerceptionAgent 处理摄像头输入

> 📖 [查看完整 Chat Agent 文档](https://humanaigc-engineering.github.io/OpenAvatarChat/beta/chat-agent)

### 组件依赖

| 类型       | 开源项目                                |Github地址|模型地址|
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

### 社区（上游）

* 微信群

<img alt="community_wechat.png" height="200" src="https://raw.githubusercontent.com/HumanAIGC-Engineering/OpenAvatarChat/main/assets/images/OpenAvatarChat.png" width="200"/>

* 官方视频教程：[Bilibili](https://www.bilibili.com/video/BV1sv8QzLEC2)
* 🚨 [常见问题](https://humanaigc-engineering.github.io/OpenAvatarChat/community/faq)

### 引用

如果您在您的研究/项目中感到 OpenAvatarChat 为您提供了帮助，期待您能给上游一个 Star⭐和引用✏️

```
@software{avatarchat2025,
  author = {Gang Cheng, Tao Chen, Feng Wang, Binchao Huang, Hui Xu, Guanqiao He, Yi Lu, Shengyin Tan},
  title = {OpenAvatarChat},
  year = {2025},
  publisher = {GitHub},
  url = {https://github.com/HumanAIGC-Engineering/OpenAvatarChat}
}
```

## 许可与署名

本仓库依据 **Apache-2.0** 分发（见 [`LICENSE`](LICENSE)），是
[HumanAIGC-Engineering/OpenAvatarChat](https://github.com/HumanAIGC-Engineering/OpenAvatarChat) v0.6.0
的**非官方下游分支**。

- 上游代码版权归原作者所有。
- 分支新增部分（`dhav_bridge/`、`audio_passthrough/`、`ws_video_client/`、3 个新 config）版权归 Haina Zhu 所有，同样以 Apache-2.0 分发。
- 对上游文件的改动仅限 `src/handlers/avatar/flashhead/` 下 3 个文件，均已按 Apache-2.0 第 4(b) 条在文件顶部标注变更声明。

本项目与 HumanAIGC-Engineering 无隶属关系，亦未获其背书。Apache-2.0 第 6 条不授予任何商标使用权，
"OpenAvatarChat" 名称与标识仍归其所有者所有。
