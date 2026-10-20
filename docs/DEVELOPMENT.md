# 开发与测试

## 运行检查

在 Windows 的仓库根目录执行：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
node tests/premiere_lifecycle.cjs
node --test tests/review_ptt_release.cjs tests/cep_adapter.cjs tests/single_panel.cjs tests/panel_platform.cjs
```

安装与恢复检查使用独立的临时目录：

```powershell
.\tests\panel_install.ps1 -TestRoot 'D:\CuthereTests\fresh-run'
```

macOS 使用：

```bash
python3.11 -m pytest tests -q
sh tests/shell/installer_tests.sh          # 沙箱目录，不写真实扩展目录或偏好设置
./install.sh                               # 默认只输出计划
```

`-TestRoot` 必须指向尚不存在的目录。这些检查使用合成数据，不应操作真实工程或当前 Premiere 安装。自动化检查覆盖文件格式、安装与恢复逻辑及接口行为；涉及麦克风、按键、Premiere 和智能体的改动，还需在独立测试工程中验证完整流程。

两个 GitHub 工作流中的 Python、Node、原生导入、shell 与打包检查相互独立：某一项失败时其他检查仍会执行，任何一项失败都会使工作流失败。`FILE_MANIFEST.json` 用 `python scripts/update_manifests.py --check` 校验；文本按内容识别并以 LF 换行计算哈希，因此全新检出与清单一致。

## 发布包

源码包用白名单和泄漏扫描生成，默认写入 `output/`：

```powershell
python scripts/build_release.py --platform macos
python scripts/build_release.py --platform windows
```

压缩包不包含测试套件、运行记录或模型权重。每个压缩包旁的 `.sha256` 和 `.manifest.json` 是该压缩包的权威校验记录；仓库根的 `FILE_MANIFEST.json` 只描述公开源码树。

## 提交修改

- 保持修改集中，并附复现步骤或测试结果。
- 使用合成媒体和示例路径。
- 不提交录音、工程、客户素材、模型权重或凭据。
- 修改公开文件后更新 `FILE_MANIFEST.json` 中的大小与 SHA-256。
- 说明实际执行的测试和仍待验证的环境。

## 智能体接口

交接、编辑决策、写回和核对步骤见[智能体工作流](AGENT_WORKFLOW.md)。处理媒体和工程时保留原文件，输出到新的工作工程与任务目录。
