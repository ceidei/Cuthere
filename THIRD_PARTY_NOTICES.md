# 第三方组件

Cuthere 需要以下软件或组件。它们分别遵循自身许可证和使用条款。

| 组件 | 用途 | 获取方式 |
|---|---|---|
| Adobe Premiere Pro | 剪辑宿主 | 用户自行安装并取得授权 |
| Python 3.11 | 本地运行环境 | 用户安装 Python，Cuthere 安装器创建独立环境 |
| sounddevice 及依赖 | 麦克风录音 | 安装器从 Python 包索引获取 |
| faster-whisper 及依赖 | 本地语音转写 | 安装器从 Python 包索引获取 |
| Systran/faster-whisper 模型 | 语音识别权重 | 按需从 Hugging Face 下载 |
| FFmpeg | 音视频处理与检查 | 用户或智能体按需安装 |
| Codex、WorkBuddy 等智能体 | 理解意见并调用编辑工具 | 用户自行选择和配置 |

Python 依赖及锁定版本见 [requirements.txt](requirements.txt) 和 [requirements-lock.txt](requirements-lock.txt)。模型和软件的许可不由本项目 MIT 许可替代。

项目自带的 `cep_adapter.js` 用于调用 Premiere 的 CEP 宿主接口；发行包不包含 Adobe CSInterface SDK。

本项目为独立第三方扩展，未获 Adobe 官方认证或背书。Adobe and Adobe Premiere Pro are either registered trademarks or trademarks of Adobe in the United States and/or other countries.
