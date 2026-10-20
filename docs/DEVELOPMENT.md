# 开发与测试

## 运行检查

在 Windows 的仓库根目录执行：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests/review tests/unit tests/test_release_tools.py tests/test_sequence_selection.py -q
node tests/premiere_lifecycle.cjs
node --test tests/review_ptt_release.cjs tests/cep_adapter.cjs tests/single_panel.cjs
```

安装与恢复检查使用独立的临时目录：

```powershell
.\tests\panel_install.ps1 -TestRoot 'D:\CuthereTests\fresh-run'
```

`-TestRoot` 必须指向尚不存在的目录。这些检查使用合成数据，不应操作真实工程或当前 Premiere 安装。

自动化检查覆盖文件格式、安装与恢复逻辑及接口行为。涉及麦克风、按键、Premiere 和智能体的改动，还需在独立测试工程中验证完整流程。

## 提交修改

- 保持修改集中，并附复现步骤或测试结果。
- 使用合成媒体和示例路径。
- 不提交录音、工程、客户素材、模型权重或凭据。
- 修改公开文件后更新 `FILE_MANIFEST.json` 中的大小与 SHA-256。
- 说明实际执行的测试和仍待验证的环境。

## 智能体接口

交接、编辑决策、写回和核对步骤见[智能体工作流](AGENT_WORKFLOW.md)。处理媒体和工程时保留原文件，输出到新的工作工程与任务目录。
