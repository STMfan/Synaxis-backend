# Synaxis 架构 / Architecture

```text
main.py → runtime.launcher（单实例监督进程）
  └─ uvicorn / api.app（一个应用进程）
       ├─ api：学生 API、持久 SSE、中英文文档、/admin 管理页面
       ├─ auth：学生 JWT/refresh 与独立管理员密码、会话、CSRF
       ├─ core：Planner → Expert/tool loop → 展示校验 → 必要时 Formatter
       ├─ llm：角色路由 → DeepSeek Anthropic / OpenAI Chat Completions
       ├─ tools：Search、AssistantOnWeb、Render、PythonBox、绘图、追问
       │    └─ PythonBox → 每次调用独立 nsjail 子进程与临时工作区
       └─ storage：会话库、调用账本、预算预留、产物与保留周期、只读课标索引
```

启动器持有数据目录互斥锁。管理重启在没有教学任务时关闭应用进程，再由监督进程创建新解释器，重载提示词、模型和网络 JSON。初始化参数不会传入重启的子进程。仅根 main.py 支持受管理重启；不直接用 uvicorn 多 worker 启动。

学生接口实现身份隔离、幂等提交、历史目录、公开消息、追问、刷新/撤销登录及取消语义。SSE 订阅断开不取消教学任务；恢复通过持久事件 ID 完成。output_* 是未经校验的正文预览，reasoning_* 仅转发供应商实际返回的思考。正式 Presentation 替换正文预览，产物按块顺序插入。

模型角色 main/literature/process 决定供应商和凭据。专家仍保留学科专用方法、共享缓存前缀、Poiesis 禁图以及可选 Max。工具调用在通用适配器中转换为 function/tool 消息；参数必须完整组装且为 JSON 对象，再通过工具白名单与参数模型校验。兼容供应商是否支持图像、工具和特定思考参数由配置声明，不推断所有模型都支持。

DeepSeek 原生 Search 独立使用官方 Anthropic 格式；更换教学供应商不会把原生搜索请求发往不支持它的兼容网关。SiliconFlow Formatter 默认为纯文本 Off，模型只接收回答和产物 ID，无图片。通用 Formatter 也只接收这一文本契约。合格 Expert 结果直接渲染；修复失败时保留合法富块、原位产物及追问，仅对错误叶块引用原文。

主数据库 synaxis.sqlite3 保存账号、会话、事件、公开消息和产物元数据；calls.sqlite3 保存调用审计、累计预算和原子预留。官方 DeepSeek 继续在请求开始时确定峰谷费率，按峰价预留。通用兼容供应商需要 CNY/百万 token 价格与上下文/输出上限；未知价格阻止收费请求，未知结果保留 uncertain。硅基流动代金券排除是明确的本项目配置，其他供应商默认计入消费。

清理只滚动账本与已过期产物，保留会话、账号、预算总额。活动教学期间不清理其产物；过期产物返回 410，外部账号访问仍受所属校验。删除用户撤销所有登录，隐藏并匿名化登录入口；保留不可复用的账号 ID、会话和计费历史。

管理面板不接受学生 JWT。密码以 Argon2 哈希存储，管理员会话为 HttpOnly、SameSite=Strict Cookie，HTTPS 下启用 Secure；写入需要同源 Origin 和 CSRF。局域网 HTTP 管理被拒绝，受管理启动禁用 Forwarded 头信任。管理接口不出现在学生 OpenAPI 中，不提供密钥、供应商原始请求或通用 SQL 执行入口。

部署见 [deployment.md](deployment.md)，客户端契约见 [frontend-contract.md](frontend-contract.md)。新增公共字段需同时更新 domain 模型、契约和测试；新增工具需更新 tools 参数/调度、说明 JSON 及专家许可列表。

SyllabusService 由应用启动时创建，供 Expert/Assistant 工具与鉴权查询接口共享。SyllabusIndex 读取 2017 年版课标包的 SQLite FTS5 与 NumPy 向量，启动校验文本/向量/来源和导入收据；LocalEmbedder 使用同版 FastEmbed 与本地 ONNX 查询模型，禁止自动下载。默认 RRF 混合检索，学科和课程类别同时过滤两个排序来源。模型推理在线程内执行，取消请求后等待线程退出才释放门锁，避免 tokenizer 截断设置竞争。正文、页码、源哈希、共享课程范围及提取警告保留；原 PDF 与下载包不受修改。数据和模型独立部署，不进入公开源码；缺失时能力响应报告 unavailable，单元测试使用合成小型索引。

公开 API 标签由 core/version.py 维护；Python 分发版本采用 PEP 440 形式。runtime.openapi_contract 在不读取私有环境、启动服务或加载真实课标的条件下生成公开 OpenAPI，contracts/openapi.json 为提交的基线。--check 核对基线与源码，--against 可对先前消费者契约执行保守的协议结构检查；通过不证明全部业务语义兼容。学生 API 与管理员私有接口保持分离。

runtime.publication 维护公开源码边界和秘密扫描；运行数据、环境文件、课标/模型、沙盒、测试结果与历史材料不属于源码导出。独立后端包只导出根 backend.yml 工作流，并用专属入口 README 替换整仓说明，不依赖其他模块源码。根级 CI 定义干净源码的单元、契约和导出检查，可选真实集成与外部供应商验收独立进行，实际云端状态以运行记录为准。
