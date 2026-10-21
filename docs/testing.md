# 测试与检查 / Testing

单元测试覆盖保留的教学、客户端和运维能力，使用模拟供应商、合成账号、临时 SQLite 及合成课标索引。它们不读取私有 .env，不需要真实课标、模型密钥、浏览器或 nsjail，不调用付费接口。真实本地集成由显式标记独立启用。

## 干净源码回归

在只有公开源码的目录中执行以下命令。系统需要 Python 3.12、uv 和 Git；首次 uv sync 会下载锁文件中的依赖，测试本身不需要外部服务。

```bash
uv venv .venv --python 3.12
uv sync --frozen
python -c "from pathlib import Path; Path('test-results').mkdir(exist_ok=True)"
uv run --frozen ruff check api auth core domain llm runtime storage tools tests main.py
uv run --frozen pytest -q --basetemp=test-results/unit-temp -W error::RuntimeWarning
uv run --frozen python -m runtime.openapi_contract --check
```

可选覆盖率检查：

```bash
uv run --frozen pytest --basetemp=test-results/coverage-temp --cov=api --cov=auth --cov=core --cov=domain --cov=llm --cov=runtime --cov=storage --cov=tools --cov-report=term-missing
```

pytest --basetemp 会重建指定目录，不能指向业务数据、备份或已有验收材料。嵌套路径的父目录必须先创建，上述命令和 CI 已包含这一步。Windows/WSL 分别创建自己的环境；Linux 测试临时目录应位于 Linux 原生文件系统。并行生成覆盖率时，为每个进程设置独立的 COVERAGE_FILE。

| 功能 | 测试模块 |
|---|---|
| JWT、密码、学生 Token 撤销与用户隔离 | test_api, test_units, test_client_api |
| 用户维护 CLI、公开配置与私密文件忽略规则 | test_admin_cli, test_publication |
| Planner/Expert/Formatter、学科路由、追问/取消/工具循环 | test_api, test_skills, test_boundaries |
| 历史、公开消息、主动追问、幂等提交、分页/偏好/刷新登录 | test_client_api, test_client_store |
| 累计预算、原子预留、未知费用、账本滚动与迁移 | test_client_store, test_client_api, test_boundaries, test_units |
| JSON 提示、模型上限、Low/Off、可选 Max、展示校验/Formatter | test_v030, test_v031, test_processor |
| 学科专精、原位产物、思考流/日志、SVG 精度、峰谷费率 | test_v032 |
| Formatter 私有 SSE、MathJax 修复、富块保留与重连 | test_v032_hotfix1 |
| 通用供应商、工具/图片/思考、用量、未知费用和密钥隔离 | test_v040_providers |
| 管理员 TLS/Origin/CSRF、限流、Cookie、CRUD、配置与重启 | test_v040_admin |
| JSON 优先级、保留周期、双确认重置与备份失败保护 | test_v040_runtime |
| 单实例锁、启动参数、监督重启与内部 worker 边界 | test_v040_launcher |
| nsjail 输入/AST、隔离参数、输出限额与执行开关 | test_v040_sandbox |
| 课标完整性、词项/向量/RRF、过滤、导入、离线与取消边界 | test_v050_syllabus |
| 环境变量隔离、合成测试输入与外连阻断 | test_isolation |
| 版本元信息、当前 OpenAPI 基线及保守兼容检查 | test_release_contract |
| 源码白名单、秘密扫描、链接拒绝和可重复导出 | test_publication_runtime |

## API 契约

contracts/openapi.json 是从当前后端生成的公开基线，供客户端生成类型和核对契约。修改 API 后，先检查保留操作、请求/响应及 SSE 字段的兼容性，再有意更新基线；不能仅靠路径数量一致判断兼容。

```bash
uv run --frozen python -m runtime.openapi_contract --check
uv run --frozen python -m runtime.openapi_contract --check --against /absolute/path/to/previous-openapi.json
uv run --frozen python -m runtime.openapi_contract --write
```

--write 用于维护者更新基线，--check 仅核对当前源码与提交的基线是否一致。--against 额外比较先前消费者保存的 OpenAPI：旧操作、鉴权、参数、请求、成功响应和 SSE 结构变更要求审阅；新增路径等兼容增加允许通过。该保守检查不是全部业务语义的兼容证明，不代替客户端回归。契约生成不启动服务，不创建业务数据库，也不依赖私有配置或真实语料。

前端可使用当前基线生成类型；后端检查不代表前端实现、打包或实体设备已验收。源码导出命令与秘密扫描边界统一维护在 [部署指引](deployment.md)。导出回归检查仓库根 README 保持不变、入口链接存在、只保留后端工作流，以及解压后再次导出内容一致。

将导出包解压到新的目录，重新运行本页“干净源码回归”，不复制工作区 .env、data、test-results 或 archive。根级 .github/workflows/backend.yml 定义 Windows/Linux、Python 3.12、uv 0.13.0 检查，涵盖单元、Ruff、契约和导出秘密扫描；Linux 还会在独立解压目录重新安装与检查。重型集成不默认进入普通 PR 检查，云端通过状态以实际 Actions 运行结果为准。源码扫描只证明被检查的文件未命中当前规则，真实 Git 历史仍须单独审查。

## Linux HTTPS / 浏览器 / 沙盒集成

按 [deployment.md](deployment.md) 准备本机 nsjail、专用 rootfs、Playwright Chromium 和 openssl。以下命令在仓库根目录运行，路径由部署者按实际环境填写：

```bash
TEST_RUNTIME=/absolute/path/to/test-runtime
mkdir -p "$TEST_RUNTIME/tmp"
SYNAXIS_TEST_NSJAIL="$TEST_RUNTIME/nsjail/nsjail" \
SYNAXIS_TEST_ROOTFS="$TEST_RUNTIME/rootfs" \
uv run --frozen pytest tests/integration/test_local_runtime.py -m integration -q --basetemp="$TEST_RUNTIME/tmp/runtime"
```

integration 默认排除；显式启用后缺少依赖会失败，不能把跳过当成通过。测试生成临时自签证书，仅测试客户端忽略信任错误，正式局域网部署应使用受信任证书。

测试启动根 main.py 的监督进程与 HTTPS 应用，通过浏览器管理合成账号，使用本地 HTTP 模拟供应商完成 Planner、流式 Expert、Render→PythonBox→Formatter、追问、幂等重放、SSE 续传以及保存 CORS/重启。配置、账号、日志和证书位于临时目录，真实 .env 和数据库不参与，结束后关闭测试服务。

Linux runner 需要用户 namespace 等隔离能力。不要通过禁用隔离或让 API 以特权身份运行来使测试通过。网关模拟通过不保证某个实际供应商行为完全一致；接入供应商时，使用合成输入另做受控真实验收。

## 离线课标集成

syllabus_integration 默认排除。先按部署指引导入完整 2017 年版课标包及本地 ONNX 模型，再显式执行：

```bash
SYNAXIS_TEST_SYLLABUS=/absolute/path/to/imported-syllabus \
uv run --frozen pytest tests/integration/test_syllabus.py -m syllabus_integration -q --basetemp=test-results/syllabus-integration
```

未设置 SYNAXIS_TEST_SYLLABUS 时读取 data/syllabus；公开源码中没有这些资源。测试阻断 Python TCP 外连，检查真实 9 科、567 个片段、261 个章节的 lexical/semantic/hybrid、过滤、父章节/页码/哈希、查询窗口及 Expert 工具续轮。文本模型仍为合成响应，不发送学生数据或产生云端模型费用。Linux 同时运行两类集成时使用 -m 'integration or syllabus_integration'，并保留两类资源路径。

普通单元使用合成小型 SQLite 与 512 维向量，覆盖导入不覆盖、失败原子性、文件完整性、共享课程范围、离线加载和取消门锁；它们不依赖私人验收报告。真实 PDF 的哈希可在导入时校验，检索测试不代替逐字人工校对或资源授权核查。

## 支持边界

本页描述可重复执行的检查方法；通过状态以相应 CI 运行或发布附件为准，不在维护文档中累积个人机器验收成绩。普通单元通过不代表真实供应商、Linux namespace、浏览器、课标资产或外部服务器均已验证。生成的 JUnit、覆盖率、截图、证书和日志放在忽略目录，不作为公开测试输入。
