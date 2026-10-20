# 代理兼容

F8采音与意见文件不依赖特定模型。代理需要访问同一台Windows电脑上的工程和工具目录，并能执行本地Python/PowerShell命令；视觉判断还取决于该代理所用模型与可用媒体工具。

| 入口 | 当前状态 |
|---|---|
| Codex本地应用/CLI | 已在 Windows 的 Codex 桌面环境完成 F8 录音、意见交接、剪辑和 Premiere 读回测试；详见 [测试状态](RELEASE_STATUS.md) |
| WorkBuddy本地 | 官方支持本地技能与脚本/CLI；已准备交接说明，尚未完成实机剪辑验收 |
| 其他本地代理 | 可按相同CLI与文件契约接手，逐个验收 |
| 仅云端沙箱 | 无法直接读取本机CEP文件桥，需另行建立本地执行器；本版本不提供远程传输 |

Codex可打开本源码目录读取AGENTS.md和仓库技能。WorkBuddy可按其官方说明导入 `.agents/skills/cuthere/` 技能目录；若被复制至独立目录，先向代理提供本工具源码/安装位置及工程目录。也可直接将`AGENT_HANDOFF.md`交给代理，并要求阅读工具目录中的`docs/AGENT_WORKFLOW.md`。

当前“发送本批意见”只生成文件包；`handoff`生成接手提示。两者都不会自动启动代理、创建新聊天或发送消息。

### 验证其他代理

建议使用独立的 25 fps 测试工程，依次测试：读取意见批次、明确短区间删除的 dry-run、生成新版本、桥接写回、读回核对及回退。记录代理、模型和 Premiere 版本，再测试复杂意见的处理结果。

参考文档：

- [Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [WorkBuddy技能](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)
- [WorkBuddy连接器与CLI](https://open.workbuddy.cn/docs/connector)
