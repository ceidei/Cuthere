# 测试与兼容范围

当前发布版本：**1.0.1（Windows）**。候选版本：**1.1.0-alpha.6（跨平台源码，macOS 实验性）**。测试日期：2026-10-21。

## 测试环境

| 组件 | 已测试配置 |
|---|---|
| 操作系统 | Windows 11 Pro |
| 剪辑软件 | Adobe Premiere Pro 25.6.4 |
| Python | 3.11 |
| 智能体 | Codex 桌面应用，可执行本地命令 |
| 转写 | faster-whisper，medium 模型 |
| 测试序列 | 25 fps，视频及双声道音频 |

## 已验证项目

| 项目 | 范围 |
|---|---|
| 安装与连接 | 独立 Python 环境、Premiere 面板、录音助手与剪辑连接 |
| 快捷录音 | 按住录音、松开保存，记录播放头和序列位置 |
| 自动转写 | 保存原声、中文转写与文字修订记录 |
| 意见交接 | 一条意见的批次文件通过完整性检查并由 Codex 读取 |
| 音画剪辑 | 同步删除第 5 至第 6 秒的视频与双声道音频；12 秒、300 帧变为 11 秒、275 帧 |
| 写回与核对 | 新工作工程、新序列，检查媒体、轨道、帧率与时长 |
| 导出检查 | Premiere 导出结果的画面帧数、声音及音量对比 |
| 恢复 | 面板重连与工程检查点恢复 |
| 1.0.1 界面 | 扩展名称、深色滚动条及连接状态 |

完整录音与剪辑测试使用 1.0.0；1.0.1 验证了界面、安装与连接，未重新执行完整录音剪辑测试。结构化结果见 [VALIDATION.json](../VALIDATION.json)。

自动化检查覆盖 Python 测试、Node 面板/桥接测试、桥接宿主契约，以及安装与恢复文件系统测试；运行方式见[开发与测试](DEVELOPMENT.md)。

## 1.1.0-alpha.6：自定义录音快捷键

macOS 默认使用右 Option，Windows 默认使用 F8。用户可在面板中设置、保存和恢复录音键，也可让智能体通过 `cuthere shortcut` 命令修改同一份设置。录音进行中改键会延后生效。

### 自动化检查

以下检查对应提交 [`03dd0ded`](https://github.com/ceidei/cuthere/commit/03dd0dedb8e4b6acb26f406063efb84b3e2adf4c)：

| 平台 | 结果 | 覆盖范围 |
|---|---|---|
| [Windows](https://github.com/ceidei/cuthere/actions/runs/39560295016) | 通过 | 299 项 Python 测试、54 项子测试、25 项 Node 测试、PowerShell 安装与恢复检查、发布文件清单 |
| [macOS arm64](https://github.com/ceidei/cuthere/actions/runs/39560295086) | 通过 | 同样的 Python 与 Node 测试、原生依赖安装和导入、安装器预检、shell 语法、48 项沙箱安装/升级/恢复/清理检查、发布文件清单 |

快捷键检查覆盖左右修饰键、组合键松开、短按与重复、前台切换、注册失败、设置同步和损坏恢复。自动化使用合成按键与测试目录，不代表实际键盘或 Premiere 验收。

### 兼容范围与待测项目

- macOS 实验版：Apple silicon、macOS 14 或更新版本。Intel Mac 暂不支持。
- **未执行**：真实 Mac 上的右 Option、麦克风、系统权限提示、Premiere 面板加载及按意见剪辑。
- 自定义 Windows 组合键尚未完成真实 Premiere 使用验收；Windows 1.0.0/1.0.1 的历史验收范围见上文。
- Mac 的按键监听只观察、不拦截；其他程序或 Premiere 使用同一键时仍可能响应。Fn/Globe 和 Escape 不支持绑定；Windows 也不支持 F12、左右区分或单独修饰键。

macOS 保持实验版，真机操作见[验收清单](MACOS.md#acceptance-checklist-for-a-real-mac)。[实验版下载](https://github.com/ceidei/cuthere/releases/tag/v1.1.0-alpha.6)；[开发与测试](DEVELOPMENT.md)。
