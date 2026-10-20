# 智能体兼容说明

Cuthere 通过本地文件和命令行交接意见。智能体需要访问运行 Premiere 的电脑、当前工程和 Cuthere 工具目录，并能执行 Python 与 PowerShell 命令。

| 智能体 | 状态 |
|---|---|
| Codex 桌面应用 | 已在 Windows 完成录音、意见交接、剪辑写回与结果核对测试 |
| WorkBuddy | 可按文件与命令行方式接入，完整剪辑流程待验证 |
| 其他本地智能体或 CLI | 需要验证文件访问、命令执行与 Premiere 连接 |
| 仅在云端运行的智能体 | 需要另配本地执行器，才能访问本机工程和 Premiere |

视觉判断取决于所用模型和可用的媒体工具。具体测试环境见[测试记录](RELEASE_STATUS.md)。

## 交接方法

1. 在 Cuthere 面板点 **发送本批意见**。
2. 从工具目录运行 `f8.ps1 handoff --project-root '<当前工程文件夹>'`。
3. 把返回的 `AGENT_HANDOFF.md` 路径交给智能体，要求按意见生成新版本并核对。

Codex 可直接读取仓库中的 `AGENTS.md`。支持本地技能的智能体也可加载 `.agents/skills/cuthere/`，并读取[智能体工作流](AGENT_WORKFLOW.md)。

## 接入检查

在独立的 25 fps 测试工程中，依次验证：读取意见、生成修改计划、预检、写回新版本、核对与恢复。记录智能体、模型和 Premiere 版本，再验证复杂意见。

## 参考

- [Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [WorkBuddy 技能](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)
- [WorkBuddy 连接器与 CLI](https://open.workbuddy.cn/docs/connector)
