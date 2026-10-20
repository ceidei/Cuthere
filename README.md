# Cuthere · 剪这儿

在 Adobe Premiere Pro 软件中按住 F8 说修改意见，把声音与当时的工程、序列和时间线位置一起交给本地代理，按意见生成新剪辑版本并读回核对。

当前发行：**1.0.1，Windows 单插件试用版**。只安装一个“Cuthere · 剪这儿”面板，打开时自动启动录音助手和剪辑桥接。已在 Windows / Premiere Pro 25.6.4 / Python 3.11 中测试真实 F8 录音、自动转写保存、Codex 接手、按意见生成新序列及宿主读回。实际测试范围见 [验收记录](docs/RELEASE_STATUS.md)。

使用方式：在 Premiere 提完一批意见，点“发送本批意见”，再对本地 Codex 说“意见提完了，按意见修改，生成新版本”。代理读取本批意见、检查当前工程、制定编辑决定并执行。

## 能做什么

| 能力 | 1.0 的范围 |
|---|---|
| 收集意见 | F8/F9按住录音、松开保存；关联播放头、已选片段或区间；回听、自动转写、修订文字、批次交接 |
| 精确位置 | 保存工程、序列、时间、快照和版本核查；时间线变化或信息不全时进入复核 |
| 固定动作 | CLI直接支持25fps普通序列、最长10秒的明确区间删除、读回与回退 |
| 复杂修改 | 代理结合录音、对白、音频、源片段或抽帧制定方案，再用脚本生成候选；已有去咳嗽声、补回对白、局部画面修补实例 |
| 实际写回 | Python工具通过CEP桥另存工程、导入新序列、激活、保存、导出读回并保留恢复点 |
| 代理接入 | Codex流程有既有实用记录；WorkBuddy/其他本地代理提供通用文件与CLI接手方法，尚未实际验收 |

全片视觉语义分析和向量素材库属于 Cuthere 2.0。1.0的局部画面修改不要求向量库：向量库帮助在大量素材中找内容；修改已有明确范围可以直接读媒体和工程执行。画面判断可能用抽帧或截图，剪辑执行主要用代码和桥接。Computer Use可用于界面设置、观察及接口未覆盖的操作。

“发送本批意见”会写出意见包，不会自动启动Codex、WorkBuddy或上传源片。使用代理时遵循该代理所选模型和工具的数据处理方式。

## 首次使用（Windows）

### 交给 Codex 安装

把解压后的整个文件夹交给本地 Codex，发送：

> 请阅读这个文件夹的 README.md，帮我安装 Cuthere 并检查录音、转写和 Premiere 桥接。先运行安装预检，再完成安装和模型准备。保留我现有的 Premiere 工程与插件备份，用独立测试工程做一次试录和剪辑验收。

安装说明和脚本都在包内。Codex需要本机命令执行权限；用户配合保存当前工作、重开Premiere、确认麦克风并真实按一次F8。Python或FFmpeg缺失时，由代理先检查并安装所需依赖。首次准备转写模型需要联网。

### 安装命令

准备Python 3.11（含Python Launcher）、支持CEP的Premiere Pro。源工作流实测为Windows/Premiere 25.6.4；其他版本须自行验收。媒体处理还需要FFmpeg。运行安装前先关闭正在录制的意见并保存工程。

在解压目录运行：

```powershell
# 查看安装计划
.\install.ps1
# 建隔离运行环境、安装一个面板，并按需下载公开转写模型
.\install.ps1 -Apply -DownloadModel
```

安装会下载Python依赖和选定转写模型，已有面板移入版本化备份。默认medium模型。若已经缓存该模型，可省略`-DownloadModel`。安装器不会重启Premiere；重启后在“窗口 → 扩展”打开“Cuthere · 剪这儿”（部分版本的菜单称为“扩展（旧版）”）。面板显示“剪辑连接：已就绪”后开始试用；连接诊断在面板底部。

从旧版升级时，安装器会备份并停用原来的独立AutoVideoEdit Bridge，避免两份程序同时接收剪辑指令。`uninstall.ps1 -Apply -RestorePrevious`可恢复此前的录音面板和桥接。面板关闭后剪辑连接暂停；再次打开即自动恢复。

1. 打开已保存的测试工程，选麦克风。
2. 停在问题处，按住F8说意见，松开保存。区间操作参照面板提示；先用一条简单意见验证位置与声音。
3. 校对转写，点“发送本批意见”和“核对当前时间线”。
4. 生成代理交接提示：

```powershell
.\f8.ps1 handoff --project-root 'D:\Podcast\Episode01'
```

`--project-root`是**当前.prproj所在目录**。把返回的`AGENT_HANDOFF.md`路径交给已能访问这些本地目录的Codex/WorkBuddy，要求它处理本批意见。Codex可打开本源码目录读取[AGENTS.md](AGENTS.md)，也可按[代理说明](docs/AGENT_WORKFLOW.md)使用随包技能。

助手收到批次后先做编辑决策表与dry-run，在用户授权范围内生成新版本；复杂意见仍需审片确认。完整命令、能力分级和验收见[代理工作流](docs/AGENT_WORKFLOW.md)、[兼容说明](docs/AGENT_COMPATIBILITY.md)。

## 开发检查

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests/review tests/unit tests/test_release_tools.py tests/test_sequence_selection.py -q
node tests/premiere_lifecycle.cjs
node --test tests/review_ptt_release.cjs tests/cep_adapter.cjs tests/single_panel.cjs
# 使用尚不存在的本地目录，模拟升级和失败回退，不会写入真正的Adobe扩展目录
.\tests\panel_install.ps1 -TestRoot 'D:\F8Tests\fresh-run'
```

这些测试不等于真实麦克风、Premiere或WorkBuddy端到端验收。合并面板版本为1.3.1，内部录音协议1.2.6、桥接协议1.1.0保持兼容；产品发行号单独从1.0开始。

运行依赖锁定在`requirements-lock.txt`，安装器使用本机验收过的具体版本。Premiere导出的完整工程XML先通过`select-sequence`提取目标序列；代理工作流中有命令与核对步骤。

## 开源与反馈

MIT许可，见[LICENSE](LICENSE)。Premiere与代理产品由使用者自行安装。此仓库不包含私人播客、工程、模型权重、凭据或Adobe的CSInterface SDK文件；最小CEP适配层仅覆盖本工具所需的原生调用。

反馈请提供软件版本、操作步骤、预期与实际结果及脱敏后的收据。复杂嵌套、多机位对象、变速、第三方效果、其他帧率完整往返暂不在1.0已验证范围内。欢迎用自己的测试工程验证并报告。

公开范围、MIT许可和Adobe安装方式见[发布说明](docs/LICENSING_AND_PRIVACY.md)。当前为未签名的CEP源码试用版，安装器会启用当前用户的开发扩展加载设置；尚未经过Adobe商店审核。

## 名称与升级

正式产品名为 **Cuthere**，中文名 **剪这儿**，仓库建议名为 `cuthere`。1.0提供F8时间线意见与代理剪辑；后续画面理解能力沿用Cuthere名称。

安装标识、存储路径、文件协议、`f8.ps1`入口及原版权声明沿用此前版本，以兼容既有安装与恢复点。旧记录中的AutoVideoEdit为项目原名。
