# Third-party components / 外部组件

Cuthere itself is MIT-licensed (see `LICENSE`). The source package does not
redistribute Adobe Premiere Pro, Codex, WorkBuddy, FFmpeg, Python, model
weights or Adobe's CSInterface SDK. The components below are installed by the
user or by the installer from package indexes; each keeps its own licence and
terms.

## Runtime and optional components

| Component | Used for | Licence (upstream) |
|---|---|---|
| Python 3.11 | Runtime, installer venv | PSF License |
| sounddevice / PortAudio | Microphone capture | MIT |
| faster-whisper | Local speech recognition | MIT |
| CTranslate2 | Whisper inference backend | MIT |
| PyAV (`av`) | Media decode/encode helpers | BSD-3-Clause |
| NumPy | Audio sample handling | BSD-3-Clause |
| huggingface_hub / hf-xet | Model cache access/download | Apache-2.0 |
| tokenizers, onnxruntime, and the other pinned dependencies | Transitive runtime | Their published licences; see `requirements-common.txt` and the installed metadata |
| FFmpeg (user-provided) | Media probing, decoding, encoding | LGPL-2.1-or-later, or GPL if the build enables GPL parts — check the build you install |
| PyObjC (`Quartz`, `AppKit`, optional on macOS) | Global listen-only event tap for the recording shortcut | MIT |
| Systran/faster-whisper model weights (downloaded only on request) | Transcription | The terms on the model card; weights are not bundled |

The installer writes pip output to `pip.log` / `pip-optional.log` in the
versioned runtime folder, so the exact installed versions are recorded on the
machine. `requirements-lock.txt`, `requirements-common.txt`,
`requirements-macos.txt` and `requirements-macos-optional.txt` pin the
versions this project was prepared with.

## Adobe

The CEP panel uses this repository's own minimal
`cep_adapter.js` to call the host APIs it needs. This is not the complete Adobe
CSInterface SDK, and no Adobe SDK file is redistributed. The adapter was
exercised against Premiere Pro 25.6.4 for the calls this tool uses. This is an
independent third-party extension and is not certified, sponsored or endorsed
by Adobe. Adobe and Adobe Premiere Pro are either registered trademarks or
trademarks of Adobe in the United States and/or other countries.

See `docs/en/LICENSING_AND_PRIVACY.md` for distribution and privacy details.

## 中文摘要

本发行包只包含本工具源码，不分发Premiere、Codex、WorkBuddy、FFmpeg、Python、模型权重或Adobe CSInterface SDK。

安装器从Python包索引安装sounddevice、faster-whisper及其依赖；仅在显式要求时从Hugging Face下载Systran/faster-whisper模型。各组件保留自身许可证和模型条款，不由本项目MIT许可证覆盖。运行依赖清单见`requirements-common.txt`等文件，实际安装版本写入安装目录的pip日志。用户自行准备FFmpeg及代理产品。

macOS可选的PyObjC（Quartz/AppKit）按MIT许可；缺失时面板按钮仍可录音，只是不能全局按住录音快捷键。CEP面板使用本包自己实现的最小`cep_adapter.js`调用宿主原生接口，该适配层并非完整Adobe SDK。本项目为独立第三方扩展，未获Adobe官方认证或背书。Adobe and Adobe Premiere Pro are either registered trademarks or trademarks of Adobe in the United States and/or other countries.
