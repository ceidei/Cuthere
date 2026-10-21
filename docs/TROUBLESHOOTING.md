# 安装与运行排查

macOS 专属问题（功能键与媒体键、辅助功能/输入监控/麦克风权限）见 [macOS 安装说明](MACOS.md#troubleshooting)。

## Premiere 或面板显示空白

保存工程，退出并重新打开 Premiere，再打开 **窗口 → 扩展 → Cuthere · 剪这儿**。检查面板中的录音助手与剪辑连接是否就绪。如果是脚本或自动化启动器拉起的 Premiere，先退出再用平时的桌面方式启动一次。

## 按住录音快捷键没有录到声音

保持 Premiere 在前台，确认面板显示助手已连接，并检查所选麦克风与输入电平。先尝试系统默认输入，再选择具体麦克风和输入通道。

执行 `cuthere shortcut show`，把其中的当前快捷键和你实际按下的键对照：Windows 默认 F8、macOS 默认右 Option，不支持的值会被明确拒绝而不是悄悄替换。意见录音保存在工程文件夹的 `work/premiere-review/` 中。回听录音，区分麦克风问题与转写问题。助手每次在就绪状态看到按键都会写入 `ptt_key_diagnostic.json`；如果该文件始终不出现，说明按键没有到达助手：可能被其他应用占用、面板未就绪，或 macOS 上功能键未按到（见 [macOS 安装说明](MACOS.md)）。

Windows 上如果诊断文件显示 `state: registration_failed`，说明 `RegisterHotKey` 没能拿到该组合（已被其他程序占用或被系统保留）。换一个组合，或关闭占用它的程序；此时 Cuthere 不会在未注册的快捷键上开始录音。Windows 不接受 F12 及以 F12 为主键的组合，请用 F1–F11 或其他按键。

`cuthere shortcut show` 的 `live.helper_running` 只有在助手心跳不超过 10 秒、且 `shortcut_config` 与平台都匹配时才为 true；退出助手后留下的旧 `status.json` 会显示为过期，不能证明设置已应用。写入成功看 `saved`，是否被助手应用看 `applied`/`pending`。配置文件被截断或手改坏时，会从 `shortcut.last-good.json` 恢复最近一次成功生效的值；`status: recovered` 表示发生了这次恢复。

## 有录音，没有转写

确认转写模型已下载。可在工具目录重新运行：

```powershell
.\install.ps1 -Apply -DownloadModel
```

查看对应意见目录下的 `transcription/job.json`，把错误信息交给智能体排查。原始录音会保留，转写文字可以修订。若语言不对，检查运行配置中记录的转写语言（默认 `auto`；显式设置过的值会被保留）。

## 智能体找不到意见

先在面板点 **发送本批意见**，再从工具目录运行：

```powershell
.\f8.ps1 handoff --project-root 'D:\Projects\Example'
```

使用当前 `.prproj` 所在文件夹，向智能体提供命令返回的 `AGENT_HANDOFF.md` 路径，并确认它有权访问该目录。命令以非零状态退出时，先读取 `work/premiere-review/intake-*` 下的报告。

## 剪辑连接断开

保持 Cuthere 面板打开。关闭面板会暂停连接，重新打开后等待状态恢复；仍未连接时，检查面板底部的诊断信息。

## 导入剪辑卡住或超时

检查 Premiere 是否有转换报告或媒体链接对话框。处理提示后，让智能体检查当前时间线与执行记录，再决定下一步。超时可能发生在 Premiere 已开始执行之后，请先确认结果，再重试。

## 卸载与恢复

```powershell
# Windows：查看卸载计划，然后卸载
.\uninstall.ps1
.\uninstall.ps1 -Apply
# 卸载并恢复安装前版本（需要已有备份）
.\uninstall.ps1 -Apply -RestorePrevious
```

```bash
# macOS
./uninstall.sh                                   # 只看计划
./uninstall.sh --apply                            # 卸载
./uninstall.sh --apply --restore-previous        # 卸载并恢复安装前版本
```

卸载保留工程、源媒体及工程目录中的意见文件。完成后重启 Premiere。macOS 的 `--purge-runtime` 只清理本工具自己的运行文件，可与 `--restore-previous` 分开执行；两者同时使用会被拒绝。

## 反馈问题

请提供操作系统、Premiere、Python 和 Cuthere 版本，以及操作步骤与错误信息。分享记录前移除个人路径、客户信息和密钥，无需上传原始媒体或工程。
