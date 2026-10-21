# 部署与配置 / Deployment and configuration

部署后，Synaxis 提供教学 API、实时讲解、学生账号和管理面板；搜索、隔离计算、图形渲染与本地课标检索按需配置。本文覆盖安装、模型配置、工具启用、常驻服务、备份和更新，适用 API 版本 1.0.0-hotfix1。

服务入口为仓库根 `main.py`，需要 Python 3.12 与 uv。完整工具环境使用 Linux；nsjail 与 API 在同一台机器。客户端请求与展示格式见 [frontend-contract.md](frontend-contract.md)。

## 1. 环境与依赖

新部署复制公开源码到例如 /srv/synaxis，使用普通服务账号。系统包安装需要 sudo，应用和沙盒构建/执行都使用普通账号。安装 uv 参见 [官方指引](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
cd /srv/synaxis
uv venv .venv --python 3.12
uv sync --frozen
```

Windows 开发使用同样的 uv 命令，随后 python main.py 会自动使用 .venv。Linux 使用 .venv/bin/python main.py。Windows 与 WSL 不共享虚拟环境；WSL 的环境、沙盒根目录、数据和测试临时目录应放在 Linux 原生文件系统。建议将源码也复制到发行版文件系统，使用上述相同命令；源码必须位于挂载盘时，可显式选择独立环境：

```bash
SOURCE_DIR=/absolute/path/to/synaxis
RUNTIME_DIR=$HOME/synaxis-runtime
uv venv "$RUNTIME_DIR/venv" --python 3.12
UV_PROJECT_ENVIRONMENT="$RUNTIME_DIR/venv" uv sync --project "$SOURCE_DIR" --frozen --group sandbox
"$RUNTIME_DIR/venv/bin/python" "$SOURCE_DIR/main.py" --check
```

避免在 Windows 挂载盘上执行 chmod、复制 Linux 文件权限或建立 Linux venv。首次配置时只复制 .env.example，升级时保留已有 .env。

## 2. 私有配置和模型

开发默认读取 .env 与 config。生产建议复制完整 config 到私有数据目录，将敏感环境文件独立保存：

```bash
mkdir -p /srv/synaxis-data
cp -r config /srv/synaxis-data/config
cp .env.example /srv/synaxis-data/service.env
chmod 600 /srv/synaxis-data/service.env
```

填入密钥后，设置 SYNAXIS_CONFIG_DIR=/srv/synaxis-data/config、SYNAXIS_ENV_FILE=/srv/synaxis-data/service.env、DATA_DIR=/srv/synaxis-data，或使用 main.py --config-dir 和 --env-file。环境变量应由 shell 或服务管理器注入；路径变量也可放进私有环境文件，但 SYNAXIS_CONFIG_DIR 必须在加载 JSON 前通过 shell/启动参数提供。

配置优先级：启动/构造参数 > 环境变量 > 私有 .env > JSON > 字段默认值。重启后重新读取；后台网络页面会同时显示保存值与实际值，环境覆盖不会静默丢失。

升级旧配置时，先保留自定义提示词与配置备份，再合入完整的 runtime 和 models 目录。Models.json、Pricing.json 位于 models，而非 ExpertsPrompts；供应商、模型上限及处理模型价格声明统一放在 providers.json。阶段的 model_role 选择 main/literature/process，旧 provider 字段不决定路由；不要直接用旧配置目录替换完整的新配置。

| 配置文件 | 内容 |
|---|---|
| config/runtime/network.json | 监听 IP、端口、精确 CORS Origin、TLS 文件、管理员访问/会话时间 |
| config/runtime/storage.json | data_dir（null 使用 data）、账本 365 天/2GB、产物/日志保留天数、单文件上限、维护间隔 |
| config/runtime/execution.json | 工具/轮次/并发、请求/会话超时、输出上限、5 元预算、学生 Token 时间、峰谷模式 |
| config/runtime/sandbox.json | 启用标志、同机 nsjail 二进制/rootfs、超时及并发 |
| config/runtime/render.json | Render 启用状态、可选 Chromium 路径 |
| config/runtime/syllabus.json | 本地课标路径、启用标志、检索模式、top-k 与 CPU 线程数 |
| config/models/Models.json | 默认角色模型、官方 DeepSeek 上限与人民币价格、旧变量映射 |
| config/models/providers.json | 角色供应商、兼容协议、端点、图像/工具/JSON 能力、思考参数映射、模型上限、CNY 价格 |
| config/models/Pricing.json | 官方 DeepSeek 峰谷窗口、节假日、费率倍率与来源 |
| config/ExpertsPrompts/*.json | 学科专用提示、共享缓存前缀及 Planner/Formatter/工具描述 |

主模型默认 Low，文学模型 Off，Planner/Formatter Off。敏感内容只放环境变量或私有文件：TEXT_MAIN_API_KEY、TEXT_LITERATURE_API_KEY、TEXT_PROCESS_API_KEY、TEXT_SEARCH_API_KEY、IMAGE-KEY、可选 JWT_SECRET/ADMIN_PASSWORD_HASH。后者是 Argon2 哈希而非明文密码。旧 DEEPSEEK-APIKEY、DEEPSEEK-MODELID-*、SILICONFLOW-APIKEY、SILICONFLOW-MODELID-PROCESS 继续兼容；新 TEXT_* 变量优先。旧 SANDBOX_URL/KEY 已弃用，不会发起远程 worker 请求。

### OpenAI 兼容供应商

在 providers.json 新增供应商，或调整 openai-compatible 项；再把 main/literature/process 中需要更换的角色指向它，并填对应角色密钥及 TEXT_*_MODEL。每个模型必须提供输出和上下文上限；收费模型必须提供三项 CNY/百万 token 单价：[缓存命中输入, 未命中输入, 输出]。不要把美元单价直接填为人民币。未知上限/价格在派发前报错；不自动切换供应商或重试收费请求。

兼容协议为 openai_chat，即 POST base_url/chat/completions。端点应含所需 /v1。支持串流文本、网关实际返回的 reasoning_content、function/tool 多轮消息、base64 图片和 usage。参照 [官方 Chat Completions 流契约](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)。配置 max_tokens_field 为 max_tokens 或 max_completion_tokens；支持程度由具体网关/模型决定，不等同于实现所有 OpenAI Responses 专属功能。

reasoning_parameter 可为 none、reasoning_effort 或 enable_thinking，reasoning_values 显式映射 off/low/high/max。不支持 Max 的模型不会在专家能力中开放 Max；布尔 enable_thinking 本身不代表具备不同强度，只有经过核验的映射才应配置。supports_images/tools/json_mode 与 send_reasoning_history 按供应商要求声明。stream_usage=false 可关闭网关不接受的 stream_options；没有有效用量时保留未知预算，不能显示成免费。

默认 DeepSeek 保留官方 Anthropic 格式及原生 Search。换用其他教学供应商时，Search 仍需要独立官方 DeepSeek 凭据和 TEXT_SEARCH_MODEL。主供应商为 deepseek 且没有搜索专用密钥时，可复用主角色密钥。SiliconFlow Formatter 默认使用 V3.2 纯文本 Off；代金券排除是明确配置，其他供应商默认计入 5 元会话累计预算。配置一个新的实际供应商后，应以合成输入做一次真实验收。

### 本地课标库

使用指定的 2017 年版检索包，不替换为其它年份：9 科、261 个父章节、567 个片段，范围为必修、选择性必修及适用的共用课程内容。课程方案在源包中为 not_applicable；不包含独立高考考纲或各省考试细则。数学公式和部分图形提取有局限，原有 quality_warnings 会随命中返回，精确表达需查原 PDF。

提取、索引和本地生成步骤统一维护在独立仓库 [Synaxis-Syllabus2017](https://github.com/STMfan/Synaxis-Syllabus2017)。先在该仓库按授权要求取得 PDF 并生成便携包；然后在本后端仓库根目录导入：

```bash
uv sync --frozen
uv run --frozen python -m runtime.import_syllabus "/absolute/path/to/curriculum-package"
```

默认目标为当前 DATA_DIR/syllabus；干净源码中不包含该目录，须由部署者提供有权使用的完整课标包。支持独立仓库生成的扁平 `data/`、`models/`、`import.json` 便携包，也兼容旧版 HF 缓存包；扁平包会核对收据、数据及模型哈希，并保留模型的独立 MIT 许可全文；源包中的历史 validation.json 不再复制。导入校验源库、向量/文本哈希、ID 和来源对应关系，存在原 PDF 时也校验其哈希；不会重新提取或重新生成向量。索引及元数据复制到 data/，配套 ONNX/tokenizer 复制成 models/ 下的普通文件，原 HF 缓存链接不会成为部署依赖。import.json 保存导入收据、模型 revision 和文件哈希。原包、原 PDF、业务库均不改动，已存在的目标目录拒绝覆盖。

迁移时复制整个 syllabus 目录，不能只复制 curriculum.sqlite3；它还依赖 chunks/sections/manifest、vectors.npy/vectors.json 与配套模型。数据/模型与源码分开，不提交到 Git；没有把下载目录或原包的 .venv 写入运行配置。只读访问，不另起 HTTP 服务，不增加端口，不调用云端 embedding。FastEmbed 固定 0.9.0，沿用 BAAI/bge-small-zh-v1.5 的 512 维 ONNX 模型与 query_embed 编码；参考 [官方模型表](https://qdrant.github.io/fastembed/examples/Supported_Models/)。

config/runtime/syllabus.json 的路径为 null 时，使用 DATA_DIR/syllabus/data 与 DATA_DIR/syllabus/models；自定义路径可指向导入后对应目录，建议绝对路径。默认 hybrid 以 RRF 合并 SQLite FTS5 和向量余弦排序，也可选择 lexical 或 semantic；top-k 为 1–10，默认 5。返回分数仅用于排序，不是正确率。lexical 模式不加载模型；semantic/hybrid 的查询超过 512 token 明确拒绝，不静默截断。

升级已有私有 config 时合入新增的 syllabus.json。停止应用后，用 --destination 指定一个新目录重新导入，再修改两个路径并重启；不覆盖正在使用的索引。应用启动会校验数据并初始化默认模式，/health.syllabus 和 /v1/capabilities.syllabus 公布实际状态。缺失、损坏或模型不可用时不伪装 ready，也不自动降级混合检索；客户端可明确请求 lexical（索引仍有效时）。未命中不代表知识点不考。

完成后可通过 Expert 的 Syllabus 工具，或带学生 JWT 的 POST /v1/syllabus/search 和 GET /v1/syllabus/sections/{section_id} 使用。正文来源是指定 PDF，PDF 页序从 1 开始，印刷页码单独列出；共享范围、分类证据、表格元数据与质量提示均保留。双确认数据库重置不会删除 syllabus 目录，账本和产物清理也不会处理课标资产。

## 3. 同机 nsjail

这是 Linux namespace/chroot 沙盒，不需要 Docker 服务或额外监听端口。每次 PythonBox 调用由 API 创建独立 nsjail 子进程：AST 白名单、独立工作区、只读 Python 根目录、默认隔离网络、用户/进程/文件/内存/CPU/时间限制。只接受所属会话 Render 的 PNG 输入；回传有限 PNG/SVG 与日志。主进程不直接执行模型 Python。

安装构建依赖，再以普通服务账号构建固定版本：

```bash
sudo apt-get update
sudo apt-get install -y build-essential git pkg-config protobuf-compiler libprotobuf-dev libnl-route-3-dev libseccomp-dev flex bison
mkdir -p /srv/synaxis-runtime
cd /srv/synaxis-runtime
git clone --recursive https://github.com/google/nsjail.git nsjail
git -C nsjail checkout 4ff54a6e0d5b65a0e1633d6e2fd1425ba6c882ce
git -C nsjail submodule update --init --recursive
make -C nsjail -j2
```

使用 uv 提供的独立 Python 创建专用 rootfs；目录应为空，不得使用主机 / 或复制应用环境/密钥进去：

```bash
uv python install 3.12
mkdir -p /srv/synaxis-runtime/rootfs/{opt/python,usr/bin,work/input,work/output,tmp,dev,proc}
chmod 1777 /srv/synaxis-runtime/rootfs/tmp
PYBIN=$(uv python find --system 3.12)
PYDIR=$(dirname "$(dirname "$PYBIN")")
cp -a "$PYDIR/." /srv/synaxis-runtime/rootfs/opt/python/
ln -s /opt/python/bin/python3.12 /srv/synaxis-runtime/rootfs/usr/bin/python3
cd /srv/synaxis
uv export --frozen --only-group sandbox --no-hashes --no-emit-project > /srv/synaxis-runtime/sandbox-requirements.txt
uv pip install --python /srv/synaxis-runtime/rootfs/opt/python/bin/python3.12 --break-system-packages -r /srv/synaxis-runtime/sandbox-requirements.txt
.venv/bin/python deploy/copy_runtime.py /srv/synaxis-runtime/rootfs
```

sandbox.json 填 nsjail_binary=/srv/synaxis-runtime/nsjail/nsjail、nsjail_root=/srv/synaxis-runtime/rootfs。普通服务账号需有 namespace 创建权限；不要通过关闭 network/mount/user 隔离或把 API 改成 root 来解决失败。rootfs 更新后再次执行 copy_runtime.py 补齐系统动态库。

Render 需 Chromium：以普通账号执行 .venv/bin/python -m playwright install chromium。Debian/Ubuntu 系统依赖使用 sudo .venv/bin/python -m playwright install-deps chromium。默认 Playwright 浏览器路径即可，或在 render.json 指定 browser_path。

## 4. HTTPS、账号与启动

默认只监听 127.0.0.1:5667。需要局域网时，在 network.json 设 host=0.0.0.0，并配置 tls_certfile 与 tls_keyfile；证书需覆盖访问域名/IP、被客户端信任，私钥仅服务账号可读。API 与管理面板共用该 TLS 端口。防火墙只需放行 5667（或自定义 API 端口），不需要 8766。无需额外管理端口。

受管理启动禁用 X-Forwarded-* 信任；原生 TLS 提供明确的 HTTPS 边界。局域网管理要求 HTTPS，不通过未验证的代理头放宽限制。若另设反向代理，管理员路径的 TLS、Host 和来源控制需另行验证，不能简单开启全网 forwarded 信任。

CORS 填完整 Origin，例如 https://student.example 或 http://localhost:5173，不含路径/末尾斜杠，不用星号。它用于学生客户端；管理员写接口始终同源并要求 CSRF。没有管理员密码时面板不可登录，不使用学生账号或默认密码。

```bash
cd /srv/synaxis
.venv/bin/python main.py --check
.venv/bin/python main.py --set-admin-password
.venv/bin/python main.py
```

--set-admin-password 在终端输入两次密码，不把明文放命令行/聊天。访问 https://你的地址:5667/admin 登录，可搜索/分页、新建/改名/重设密码/停用/删除学生账号、修改网络 JSON、重启。删账号保留会话与计费历史，ID 不复用；密码或状态变更立即撤销旧学生登录。当前有 queued/running 教学请求时禁止停用/删除用户或重启服务。

修改网络设置仅保存，重启后生效；TLS 文件会在保存时验证。改变地址/端口后使用新地址。新应用进程使管理员会话失效，需要重新登录。后台不提供任意命令、SQL、密钥查询或多管理员账号功能。

可用 deploy/synaxis.service 作为 systemd 服务模板，替换绝对路径、服务账号与 Environment 设置；启动目标始终是根 main.py。模板不代替上述安装配置步骤。部署日志交给 systemd journal，业务思考/工具日志以产物记录并按 JSON 保留周期清理。

## 5. 更新、备份与重置

先停止服务，再把私有数据目录（两份数据库、JWT key、jwt-generation、管理员哈希、附件）与私有配置/环境文件备份到项目之外的受保护目录；检查两份 SQLite PRAGMA quick_check。使用同一 uv.lock 做 uv sync --frozen，运行 --check 和单元测试后启动。升级迁移原地补列，不替换账号、会话、事件 ID、累计预算或密钥。保留匹配旧代码与配置的备份用于回滚；未经核验不要仅把较旧数据库覆盖到运行中的服务。

```bash
.venv/bin/python main.py --initialization
```

该操作持有单实例锁，要求先输入 RESET DATABASES，再输入所显示的完整数据目录。任一步不符即取消且不启动服务。两份数据库先经 SQLite backup API 复制到 data/backups/reset-*，通过 quick_check 并记录 SHA256；全部成功后才删除 synaxis.sqlite3、calls.sqlite3 及其 WAL/SHM，随后正常启动。附件、日志和密钥保留；jwt-generation 更新使旧登录失效。保留的孤立文件不会自动绑定新账号。该参数不会在管理员重启时重复执行。

实际运行服务只能通过受管理 main.py；不要绕过启动器同时启动另一个 uvicorn 访问同一数据目录。账本清理不重置 5 元累计预算；产物过期返回 410，不删除会话目录。日志与附件保留周期分别配置，活动任务不清理。

## 6. 检查、契约与源码导出

按 [testing.md](testing.md) 运行单元和本地集成验收。后端配置错误必须在使用前解决；/health 与 /v1/capabilities 的 configured 仅指配置成立，不等同于新供应商的真实调用已经验收。Syllabus 就绪状态以实际索引、校验和本地模型初始化结果为准，见上文课标部署步骤。

根目录 .gitignore 忽略 .env/自定义 *.env、数据库、密钥/私钥、运行目录、缓存、测试产物、部署备份和 archive；.env.example、配置模板及合成测试输入可提交。忽略规则只影响未跟踪文件，不能移除已提交的秘密；维护者还须检查暂存文件、真实 Git 历史和远程仓库。没有历史可查时不能宣称历史从未泄漏。

导出前扫描包含高置信秘密格式，以及当前 .env 和默认 data/jwt.key、admin-password.hash 的已知值精确匹配；报告只显示位置和类型。它不读取 SQLite，也不自动追踪自定义数据目录或其它机器上的秘密；不能替代完整的秘密管理与 Git 历史扫描。

在仓库根目录生成后端公开源码包：

```bash
uv run --frozen python -m runtime.openapi_contract --check
uv run --frozen python -m runtime.publication --check
uv run --frozen python -m runtime.publication --output dist/synaxis-backend.zip
```

导出器只接受维护的后端源码范围，包含根 main.py；导出前执行秘密扫描，拒绝把任意工作目录作为发布内容。--check 只扫描，--output 创建新 ZIP 且拒绝覆盖已有文件；重复导出使用新文件名。dist 为忽略的生成目录。解压到新目录后，按第 1 节安装，并运行 testing.md 的单元及契约检查；不能用原工作区的 .env、data 或 test-results 补齐单元测试。可选集成另行提供明确的外部依赖。

根级 .github/workflows/backend.yml 定义 PR 和推送检查：Windows/Linux 单元测试、Ruff、OpenAPI 基线及源码导出扫描，Linux 额外检查独立解压的源码。实际云端结果以 Actions 运行记录为准。该流程覆盖后端；前端字体、签名、构建和测试输入须由前端维护流程单独处理。CI 通过不代表课标数据、模型权重或其他资源可以再分发。

后端源码与根 main.py 采用 MIT，见 LICENSE 和 NOTICE。API 标签为 1.0.0-hotfix1，Python 分发元数据使用其 PEP 440 规范形式 1.0.0.post1。该许可不自动覆盖前端、第三方依赖、字体、课标或 embedding 模型；这些资源保留各自授权，源码导出不替代其分发授权审查。
