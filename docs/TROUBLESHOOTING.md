# 安装与运行排查

## Premiere 或面板显示空白

保存工程，退出并重新打开 Premiere，再打开 **窗口 → 扩展 → Cuthere · 剪这儿**。检查面板中的录音助手与剪辑连接是否就绪。

## 按 F8 没有录到声音

保持 Premiere 在前台，确认面板显示助手已连接，并检查所选麦克风与输入电平。先尝试系统默认输入，再选择具体麦克风和输入通道。

意见录音保存在工程文件夹的 `work/premiere-review/` 中。回听录音，区分麦克风问题与转写问题。

## 有录音，没有转写

确认转写模型已下载。可在工具目录重新运行：

```powershell
.\install.ps1 -Apply -DownloadModel
```

查看对应意见目录下的 `transcription/job.json`，把错误信息交给智能体排查。原始录音会保留，转写文字可以修订。

## 智能体找不到意见

先在面板点 **发送本批意见**，再从工具目录运行：

```powershell
.\f8.ps1 handoff --project-root 'D:\Projects\Example'
```

使用当前 `.prproj` 所在文件夹，向智能体提供命令返回的 `AGENT_HANDOFF.md` 路径，并确认它有权访问该目录。

## 剪辑连接断开

保持 Cuthere 面板打开。关闭面板会暂停连接，重新打开后等待状态恢复；仍未连接时，检查面板底部的诊断信息。

## 导入剪辑卡住或超时

检查 Premiere 是否有转换报告或媒体链接对话框。处理提示后，让智能体检查当前时间线与执行记录，再决定下一步。超时可能发生在 Premiere 已开始执行之后，请先确认结果，再重试。

## 卸载与恢复

在工具目录运行：

```powershell
# 查看卸载计划
.\uninstall.ps1
# 卸载 Cuthere
.\uninstall.ps1 -Apply
# 卸载并恢复安装前版本（需要已有备份）
.\uninstall.ps1 -Apply -RestorePrevious
```

卸载保留工程、源媒体及工程目录中的意见文件。完成后重启 Premiere。

## 反馈问题

请提供 Windows、Premiere、Python 和 Cuthere 版本，以及操作步骤与错误信息。分享记录前移除个人路径、客户信息和密钥，无需上传原始媒体或工程。
