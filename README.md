# Synaxis backend 1.0.0

源码仓库：[STMfan/Synaxis-backend](https://github.com/STMfan/Synaxis-backend)。学生客户端独立维护于 [STMfan/Synaxis-frontend](https://github.com/STMfan/Synaxis-frontend)。

轻量教学 LLM Harness：Python 3.12、FastAPI、SQLite、uv。支持八个学科专家、工具循环、追问与历史会话、可靠提交、持久 SSE、图文穿插和每会话累计 5 元预算。

本仓库根目录即后端项目根目录，包含启动入口、Python 依赖、服务实现、契约、部署说明和独立 CI。

在干净源码目录中安装并检查；无需真实模型密钥、课标包或沙盒即可运行单元测试：

```powershell
uv venv .venv --python 3.12
uv sync --frozen
python main.py --check
```

运行教学功能前，复制 `.env.example` 为私有配置文件并设置模型凭据。启动和设置管理员密码：

```powershell
python main.py --set-admin-password
python main.py
```

Windows 启动器会使用已有 .venv。Linux 请在对应 uv 虚拟环境中运行根 main.py。PythonBox 需要同机 Linux nsjail；Windows 可先开发 API、使用 WSL 验证沙盒。

- [部署与配置指引](docs/deployment.md)：唯一部署入口，含 TLS、配置迁移、沙盒、备份和更新。
- [前端契约](docs/frontend-contract.md)：学生 API、展示块、流式输出与错误恢复。
- [架构](docs/architecture.md)：模块边界、供应商适配、状态和计费。
- [测试矩阵](docs/testing.md)：单元测试、契约检查、干净源码复现与可选集成验收。

默认地址 http://127.0.0.1:5667，文档 /docs?lang=zh 或 /docs?lang=en，管理面板 /admin。管理员密码独立设置，无默认密码；局域网管理必须使用 HTTPS。

Syllabus 支持已接入的 2017 年版 9 科课标包，提供离线词项、向量与混合检索，保留原文、PDF 页码和来源哈希。源码不分发课标数据或模型；未导入时明确报告 unavailable，其他教学功能仍可使用。导入与迁移见部署指引。

非涉密配置位于 config/runtime 和 config/models；学科提示词位于 config/ExpertsPrompts。密钥保存在私有 .env 或环境变量。旧 DeepSeek/SiliconFlow 变量兼容，新部署使用 .env.example 的 TEXT_* 角色格式。

后端源码和根 main.py 采用 [MIT](LICENSE) 许可，资源与第三方组件说明见 [NOTICE](NOTICE)。公开源码导出使用后端维护命令，不直接打包工作目录；它排除私有配置、数据库、课标包、模型、浏览器、沙盒 rootfs 和测试输出。导出和扫描步骤见部署指引。MIT 授权不替代第三方依赖、数据和模型各自的授权。
