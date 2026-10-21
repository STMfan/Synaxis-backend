# Synaxis 后端

一个面向高中学习的多学科 AI 教学服务。它把问题交给相应的学科专家，在讲解过程中调用搜索、计算、绘图和课标检索工具，再将文字、公式、图示与追问发送给客户端。

后端同时提供学生账号、会话历史、实时输出、费用账本和管理面板。当前版本为 **1.0.0-hotfix1**，使用 Python 3.12、FastAPI、SQLite 和 uv。配套学习界面见 [Synaxis 客户端](https://github.com/STMfan/Synaxis-frontend)。

## 教学服务

- **文字与题图提问**：接收学生问题、图片和上下文，由 Planner 判断学科并选择专家。
- **连续教学**：在同一会话里讲解、追问、收集学生回答和继续讨论，保存完整学习记录。
- **图文讲解**：输出 Markdown、公式、SVG、静态 HTML 和图片，也可提供提示/答案等分支内容。
- **实时输出**：通过持久 SSE 发送正文、供应商返回的思考和工具进度，支持按事件 ID 续接。
- **费用管理**：记录调用用量与费用，按会话累计执行 5 元预算，并在发起付费调用前预留额度。
- **服务管理**：在 `/admin` 管理学生账号、模型配置和服务状态；学生通过独立的鉴权 API 使用教学功能。

## 学科专家

八位专家覆盖九个高中学科，并为数学保留不同的解题侧重：

| 专家 | 学科与侧重 |
|---|---|
| Euclid | 数学：代数、函数、数列、概率统计与符号推理 |
| Turing | 数学：几何关系、建模与数值计算 |
| Nomos | 物理 |
| Nexus | 英语 |
| Meridian | 地理 |
| Chronos | 历史与思想政治 |
| Panopt | 化学与生物学 |
| Poiesis | 语文与写作 |

专家的学科提示与教学方法位于 `config/ExpertsPrompts/`。模型角色分为主教学、文学和处理角色，可分别配置供应商、模型与凭据。

## 教学工具

| 工具 | 提供的能力 |
|---|---|
| Search | 联网搜索资料并返回结构化来源 |
| AssistantOnWeb | 在独立上下文中调查问题细节，将资料交回教学过程 |
| Syllabus | 离线检索指定的 2017 年版课标，返回原文、章节、页码、来源哈希和课程范围 |
| PythonBox | 在本机 Linux nsjail 中执行计算，生成数值结果、PNG/SVG 图形和日志 |
| Render | 将 HTML、SVG 或 CSS3D 内容渲染成教学图片 |
| GPTImage | 经学生单次付费授权后生成教学插图 |
| AskStudent | 提出一个问题，暂停当前教学并等待学生回答 |
| RequestMaxThinking | 为复杂问题申请更高推理级别，继续受会话预算约束 |

搜索、计算、渲染和课标检索可按部署环境分别配置。课标工具的数据准备与本地重建由独立的 [Synaxis-Syllabus2017](https://github.com/STMfan/Synaxis-Syllabus2017) 提供；后端读取其生成的便携包。

## 快速开始

准备 Python 3.12 和 uv，在仓库根目录安装依赖：

```sh
uv venv .venv --python 3.12
uv sync --frozen
```

复制 `.env.example` 为 `.env`，填入教学模型及处理模型的凭据。`config/models/providers.json` 配置供应商和能力，`config/runtime/` 配置监听地址、存储、预算及工具。

```sh
uv run --frozen python main.py --check
uv run --frozen python main.py --set-admin-password
uv run --frozen python main.py
```

启动后可访问：

| 入口 | 地址与用途 |
|---|---|
| 健康状态 | `http://127.0.0.1:5667/health` |
| 管理面板 | `http://127.0.0.1:5667/admin`，创建学生账号和管理服务 |
| 中文 API 文档 | `http://127.0.0.1:5667/docs?lang=zh` |
| 英文 API 文档 | `http://127.0.0.1:5667/docs?lang=en` |

学生账号可用于登录配套客户端，或接入自己的 Web、桌面和移动界面。远程服务配置 HTTPS 与 CORS 后，客户端即可连接对应的服务根地址。

核心 API 可在 Windows 上开发；PythonBox 使用同机 Linux nsjail，Render 使用 Playwright Chromium。启用这些工具、配置真实课标库及部署常驻服务的步骤见 [部署与配置](docs/deployment.md)。

## 接入自己的客户端

公开 API 覆盖登录、题图上传、教学会话、追问与答题、历史消息、费用、课标和产物下载。SSE 将教学过程持续传给界面，展示块保留文字与对应图像的顺序。

- [前端契约](docs/frontend-contract.md)：请求、事件、展示块和断线恢复。
- [OpenAPI](contracts/openapi.json)：机器可读接口定义。
- [架构](docs/architecture.md)：问题路由、工具循环、持久化与服务管理。
- [开发与测试](docs/testing.md)：单元测试、契约检查及本地集成。

## 许可与数据

后端代码、提示词和维护文档采用 [MIT](LICENSE)。第三方组件与可选资源保留各自的许可，详见 [NOTICE](NOTICE)。课标原文和模型由部署者取得并本地配置；课标处理工具采用其独立的 AGPL-3.0-or-later 许可。
