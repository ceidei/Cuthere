# 开发与测试

## 运行检查

在 Windows 的仓库根目录执行：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
node tests/premiere_lifecycle.cjs
node --test tests/review_ptt_release.cjs tests/cep_adapter.cjs tests/single_panel.cjs tests/panel_platform.cjs tests/panel_shortcut.cjs
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

## 持续集成

两个工作流在推送到 `main`、`feature/macos-english` 以及创建拉取请求时运行。各项检查相互独立：某一项失败时其他检查仍会执行，任何一项失败都会使工作流失败。没有任何步骤使用 `continue-on-error`；自身依赖安装失败的检查会按名称跳过，而不是静默通过。

| 工作流 | 运行器 | 检查内容 |
|---|---|---|
| `windows.yml` | `windows-latest` | Python 测试、Node 面板/桥接测试、PowerShell 语法与安装/恢复文件系统用例、Windows 白名单 dry-run、`update_manifests.py --check` |
| `macos.yml` | `macos-15`（Apple silicon） | 真实 Darwin 解释器上的 Python 测试、Node 测试、macOS 运行依赖与 PyObjC 安装导入、`install_macos.py --check-only`、shell 语法、沙箱安装/升级/恢复/清理测试、macOS 白名单 dry-run、`update_manifests.py --check` |

功能候选提交 [`7bb307a2d88e6dfa0cfc0353f776d7af30080fd8`](https://github.com/ceidei/cuthere/commit/7bb307a2d88e6dfa0cfc0353f776d7af30080fd8) 两个工作流均通过：[Windows 运行 39449546296](https://github.com/ceidei/cuthere/actions/runs/39449546296) 通过 204 项 Python 与 15 项 Node 测试；[macOS 运行 39449546127](https://github.com/ceidei/cuthere/actions/runs/39449546127) 通过 204 项 Python 测试、15 项 Node 测试、运行依赖与 PyObjC 导入，以及 48 项沙箱安装检查（0 项失败）。CI 不涉及麦克风、真实功能键、TCC 授权提示，也不在 Premiere 中加载面板；仍需人工验证的部分见[测试与兼容范围](RELEASE_STATUS.md)。

`FILE_MANIFEST.json` 用 `python scripts/update_manifests.py --check` 校验；文本按内容识别并以 LF 换行计算哈希，因此全新检出与清单一致。CI 只校验清单、从不在测试时重新生成，修改随包发布的文件后请自行运行 `python scripts/update_manifests.py`。

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
