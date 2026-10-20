# 外部组件

发行包只包含本工具源码，不分发Premiere、Codex、WorkBuddy、FFmpeg、Python、模型权重或Adobe CSInterface SDK。

安装器从Python包索引安装sounddevice、faster-whisper及其依赖；可选从Hugging Face下载Systran/faster-whisper模型。各组件保留自身许可证和模型条款，不由本项目MIT许可证覆盖。运行环境依赖清单见requirements.txt，实际安装版本写入pip日志。用户自行准备FFmpeg及代理产品。

CEP 面板通过项目自带的最小 `cep_adapter.js` 调用宿主原生接口。该适配层覆盖本工具所需的调用，已在 Premiere Pro 25.6.4 中测试；它不包含完整 Adobe SDK。

本项目为独立第三方扩展，未获Adobe官方认证或背书。Adobe and Adobe Premiere Pro are either registered trademarks or trademarks of Adobe in the United States and/or other countries. 分发方式见docs/LICENSING_AND_PRIVACY.md。
