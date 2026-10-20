# 代理兼容

F8采音与意见文件不依赖特定模型。代理需要访问同一台Windows电脑上的工程和工具目录，并能执行本地Python/PowerShell命令；视觉判断还取决于该代理所用模型与可用媒体工具。

| 入口 | 当前状态 |
|---|---|
| Codex本地应用/CLI | 基础工作流已有中文播客实用记录；1.0在本机Codex桌面环境完成真实F8录音到按意见剪辑及宿主读回；详见RELEASE_STATUS |
| WorkBuddy本地 | 官方支持本地技能与脚本/CLI；已准备交接说明，尚未完成实机剪辑验收 |
| 其他本地代理 | 可按相同CLI与文件契约接手，逐个验收 |
| 仅云端沙箱 | 无法直接读取本机CEP文件桥，需另行建立本地执行器；本版本不提供远程传输 |

Codex可打开本源码目录读取AGENTS.md和仓库技能。WorkBuddy可按其官方说明导入 `.agents/skills/cuthere/` 技能目录；若被复制至独立目录，先向代理提供本工具源码/安装位置及工程目录。也可直接将`AGENT_HANDOFF.md`交给代理，并要求阅读工具目录中的`docs/AGENT_WORKFLOW.md`。

当前“发送本批意见”只生成文件包；`handoff`生成接手提示。两者都不会自动启动代理、创建新聊天或发送消息。WorkBuddy本地技能试用与连接器市场上架是不同交付，本版本不声称已经符合市场上架条件。

WorkBuddy验收：在隔离的25fps测试工程中，导入说明→读取一批意见→明确短删除dry-run→新候选→桥发布→读回→回退，再验证一条复杂中文意见，记录软件/模型版本与证据。通过后才能把兼容状态改为已验证。

官方依据（2026-10-20核查）：

- [Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [WorkBuddy技能](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)
- [WorkBuddy连接器与CLI](https://open.workbuddy.cn/docs/connector)
