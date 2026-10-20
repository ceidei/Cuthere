# 许可证与隐私

## MIT 许可

Cuthere 源码采用 [MIT 许可](../LICENSE)，版权署名为 AutoVideoEdit contributors。使用、修改和分发时，请遵循许可证中的版权与许可声明保留要求。

Premiere、智能体产品、第三方依赖与模型分别遵循各自条款，见[第三方组件](../THIRD_PARTY_NOTICES.md)。

## 本地数据

默认语音转写使用本地 faster-whisper。安装依赖与首次下载模型需要联网；模型权重单独下载，不随源码包分发。

录音、转写、时间线位置和编辑记录保存在本机工程目录中。Cuthere 的意见交接功能生成本地文件包。

将意见交给智能体后，媒体或文字是否发送给云端服务，取决于所选智能体、模型与工具的设置。请按项目的数据要求选择和配置这些产品。

反馈问题时，请移除日志或截图中的个人路径、客户信息和密钥。通常只需提供版本、复现步骤与错误信息，无需上传原始媒体或工程。

## Adobe 与安装方式

Cuthere 是独立第三方扩展，需配合用户自行安装并获授权的 Adobe Premiere Pro 使用。

当前版本以未签名的 CEP 扩展提供。Windows 与 macOS 安装器会为当前用户启用开发扩展加载设置，并支持安装前预检、备份和卸载恢复。此版本尚未经过 Adobe 商店审核。

本项目与 Adobe 不存在从属、赞助或官方认证关系。Adobe and Adobe Premiere Pro are either registered trademarks or trademarks of Adobe in the United States and/or other countries.

## 参考

- [MIT 许可](https://opensource.org/license/mit)
- [Adobe CEP 分发与签名](https://developer.adobe.com/developer-distribution/experience-cloud/docs/guides/zxp/resources)
- [Adobe 商标规则](https://www.adobe.com/legal/permissions/trademarks.html)
