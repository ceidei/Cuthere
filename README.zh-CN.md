# Cuthere · 剪这儿

**在 Premiere 里说出修改意见，让智能体接着剪。**

按住 F8 说话，松开后自动保存录音、转写和时间线位置。提完一批意见，交给本地 Codex 等智能体，结合工程与音视频内容生成新的剪辑版本。

当前版本：**1.1.0-alpha.4**——Windows 1.0.1 为发布路径，macOS 为实验性候选。macOS 候选只有自动化检查，**没有**在真实 Mac 上配合 Premiere、麦克风和 F8 运行过。[下载安装包](https://github.com/ceidei/cuthere/releases/latest) · [测试与兼容范围](docs/RELEASE_STATUS.md) · [English README](README.md)

## 功能

- **边看边提意见**：F8/F9 按住录音、松开保存，关联播放头、所选片段或时间区间。
- **自动转写**：在本机将语音转成文字，支持回听和修订。
- **批量交给智能体**：保留每条意见的录音、文字和位置，方便统一处理。
- **按意见改片**：智能体结合工程、相关音视频和上下文制定修改方案，调用编辑工具生成新版本。
- **保留原片**：使用新的工作工程与序列，支持结果核对和恢复。

意见可以涉及声音、画面或音画关系。具体能完成的修改取决于所用智能体、可读取的媒体和可执行的编辑动作，详见[智能体工作流](docs/AGENT_WORKFLOW.md)。

## 安装

### 环境要求

| 组件 | 要求 |
|---|---|
| 操作系统 | Windows 10/11 |
| 剪辑软件 | 支持 CEP 扩展的 Adobe Premiere Pro；已测试 25.6.4 |
| Python | 3.11，包含 `py` 启动器 |
| 媒体处理 | FFmpeg，供智能体处理和检查音视频 |
| 智能体 | 可访问本机文件并执行命令的 Codex 或其他本地智能体 |

### 交给 Codex 安装

下载并解压[安装包](https://github.com/ceidei/cuthere/releases/latest)，把文件夹交给本地 Codex，发送：

> 请阅读 README.md，帮我安装 Cuthere，准备转写模型，并检查录音、转写和 Premiere 连接。使用独立测试工程验证，保留现有工程和插件备份。

按提示保存当前工作、重启 Premiere、选择麦克风，并按 F8 试录一次。首次安装需要联网下载依赖和转写模型。

### 手动安装

在解压目录运行 PowerShell：

```powershell
# 查看安装计划
.\install.ps1
# 安装并下载默认转写模型
.\install.ps1 -Apply -DownloadModel
```

已有模型缓存时可省略 `-DownloadModel`。安装后重启 Premiere，打开 **窗口 → 扩展 → Cuthere · 剪这儿**，等待录音助手与剪辑连接就绪。部分 Premiere 版本将菜单显示为“扩展（旧版）”。

安装器使用当前用户的独立 Python 环境，并备份已有安装。当前版本以未签名的 CEP 扩展提供，安装时会启用相应的开发扩展加载设置。[卸载与恢复](docs/TROUBLESHOOTING.md#卸载与恢复)

### macOS（实验性）

候选源码包解压后，在包含 `install.sh` 的目录运行：

```bash
unzip cuthere-1.1.0-alpha.4-macos-source.zip -d cuthere-1.1.0-alpha.4-macos-source
cd cuthere-1.1.0-alpha.4-macos-source

./install.sh --check-only   # 检查 Python 3.11、FFmpeg、CEP 目录、磁盘与主机范围
./install.sh                # 默认：只打印计划，不写盘
./install.sh --apply        # 为当前用户安装
```

本候选仅面向 **Apple silicon（arm64）且系统为 macOS 14 或更新版本** 的 Mac。锁定的运行依赖无法安装到 Intel Mac，因此安装器会在写入任何文件之前拒绝不支持的 CPU 或系统版本。权限、功能键冲突与排查见 [docs/MACOS.md](docs/MACOS.md)。

录音无论用 F8/F9 还是面板自己的“开始/停止”按钮，都需要 macOS 的**麦克风**权限。按住说话还需要系统为按键监听要求的权限——通常是**辅助功能**，部分系统还会要求**输入监控**。缺少按键监听权限时，面板按钮仍可录音；缺少麦克风权限时则无法录音。Cuthere 不能代替用户授权，请按系统提示或面板“连接诊断”中请求的权限授予。

## 使用

1. 打开已保存的 Premiere 工程，并打开 Cuthere 面板。
2. 播放或停在需要修改的位置，按住 **F8** 说出意见，松开保存。
3. 回听并校对文字，继续记录其他意见。
4. 点 **发送本批意见**，在工具目录生成交接说明：

   ```powershell
   .\f8.ps1 handoff --project-root 'D:\Projects\Example'
   ```

5. 把命令返回的 `AGENT_HANDOFF.md` 路径交给本地智能体，告诉它：**“意见提完了，请按这批意见修改，生成新版本并核对结果。”**

`--project-root` 为当前 `.prproj` 所在文件夹。智能体先列出修改计划，再处理意见。剪辑时请保持 Cuthere 面板打开。

“发送本批意见”会生成本地意见包；接下来需要在智能体中发出修改指令。录音和转写默认在本机处理，交给智能体后的数据处理方式取决于所选产品与模型。

## 支持范围

- **已测试流程**：Windows、Premiere Pro 25.6.4、Python 3.11、Codex 桌面应用；覆盖录音、自动转写、意见交接、剪辑写回、结果核对与恢复。
- **内置固定命令**：25 fps 普通序列中，最长 10 秒的明确区间删除。
- **复杂意见**：由智能体检查相关音视频、制定方案并调用工具处理，完成后需要审片核对。
- **其他智能体**：提供通用文件与命令行交接方式；WorkBuddy 等产品尚未完成相同流程测试。见[兼容说明](docs/AGENT_COMPATIBILITY.md)。

语音转写可能有误，请先校对。其他帧率、复杂效果、嵌套、多机位及变速工程需要单独验证。完整测试范围见[测试记录](docs/RELEASE_STATUS.md)。后续版本计划完善镜头分析、素材检索与跨镜头剪辑规划，见[更新记录](CHANGELOG.md)。

## 帮助与贡献

- [安装与运行排查](docs/TROUBLESHOOTING.md)
- [智能体工作流](docs/AGENT_WORKFLOW.md)
- [开发与测试](docs/DEVELOPMENT.md)
- [反馈问题](https://github.com/ceidei/cuthere/issues)

反馈时请附软件版本、复现步骤、预期与实际结果。分享日志前请移除个人路径、客户信息和密钥。

## 许可

[MIT](LICENSE)。Premiere、智能体产品及第三方依赖分别遵循各自条款。本项目为独立第三方扩展。详见[许可证与隐私说明](docs/LICENSING_AND_PRIVACY.md)和[第三方组件](THIRD_PARTY_NOTICES.md)。
