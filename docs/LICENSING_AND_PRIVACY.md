# 公开范围、许可证与安装方式

## 公开内容与数据

本发行包包含工具源码、安装脚本、测试、通用示例与脱敏验收摘要。未包含个人身份信息、私人项目名称、真实录音、转录意见、源媒体、Premiere工程、密钥、已安装运行配置、个人路径或旧Git历史。示例工程与路径使用通用名称。

默认语音转写使用本地faster-whisper。安装依赖和首次准备模型需要联网；模型权重单独下载，不随源码包分发。用户日后生成的录音、转写、工程路径、截图与日志可能含私人信息，发布问题反馈前应先脱敏。交给代理处理时，数据使用方式取决于所选代理、模型与工具。

## MIT许可

本工具原创源码按根目录LICENSE中的MIT许可开放，版权署名为AutoVideoEdit contributors。允许使用、修改、复制、再分发和商业使用，也允许将修改版用于闭源产品。分发副本或软件的重要部分时，应保留原版权声明和许可声明。软件按现状提供；具体内容以LICENSE为准。

MIT不会自动覆盖外部软件、依赖、模型或商标；它们遵循各自条款。详见THIRD_PARTY_NOTICES.md。

## Adobe与安装方式

这是独立开发的第三方扩展。用户自行取得并安装获授权的Adobe Premiere Pro软件。本包不分发Adobe软件、产品图标或Adobe CSInterface SDK文件；使用自行实现的最小CEP适配层调用宿主接口。

当前GitHub源码试用版由PowerShell安装器复制CEP扩展，并在当前Windows用户的CSXS配置中设置PlayerDebugMode=1，以加载未签名的开发扩展。安装器支持先查看计划。此安装方式尚未经过Adobe商店审核，也不是已签名的ZXP安装包。面向常规成品分发时，应按Adobe的CEP文档另做ZXP打包与签名；如申请Adobe市场上架，还需遵循其提交流程。

本项目与Adobe不存在从属、赞助或官方认证关系。Adobe and Adobe Premiere Pro are either registered trademarks or trademarks of Adobe in the United States and/or other countries.

## 参考

- MIT正文：https://opensource.org/license/mit
- Adobe CEP分发与签名：https://developer.adobe.com/developer-distribution/experience-cloud/docs/guides/zxp/resources
- Adobe商标规则：https://www.adobe.com/legal/permissions/trademarks.html
