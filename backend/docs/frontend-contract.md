# Frontend contract / 前端契约：API 1.0.0，展示格式 1.0

当前机器可读契约为 backend/contracts/openapi.json，由后端生成并由 CI 核对；可用于客户端生成类型。字段、事件和状态语义需一并检查，不能仅用路径数量判断兼容。本文描述后端接口，不代表某个前端构建或设备已完成验收。

Bearer JWT 放在 `Authorization` 请求头。禁止把 token 放 URL；浏览器 SSE 用 `fetch` 的 ReadableStream，原生 EventSource 不能设置该请求头。每条 SSE 的 `id` 是持久化事件 ID；重连传 `after` 或 `Last-Event-ID`，按 ID 去重。`waiting/completed/failed/cancelled` 后流关闭，回答追问后重新订阅。断开流不会取消教学调用，取消需使用 API。

`presentation` 事件：

```json
{
  "version":"1.0",
  "blocks":[
    {"type":"branch","selected":"hint","tabs":[
      {"id":"hint","label":"提示","blocks":[{"type":"markdown","text":"先考虑因式分解。"}]},
      {"id":"answer","label":"答案","blocks":[{"type":"latex","source":"x=2\\quad\\text{或}\\quad x=3","display":true}]}
    ]}
  ]
}
```

| type | 数据 | 展示要求 |
|---|---|---|
| markdown | text | 默认格式；禁用原始 HTML；过滤危险链接；行内 `\(...\)`，块 `\[...\]` |
| latex | source, display | 使用 MathJax v4 的 TeX 输入组件；不开启 require、HTML、网络或自定义可执行扩展 |
| svg | source | 后端已校验静态子集；前端再次清洗，禁止脚本、外部资源、foreignObject |
| html | source | 独立 sandbox iframe，禁脚本/导航/同源访问；只允许 CSS、details、checkbox/radio、滚动；CSP 禁止外部网络 |
| question | id, prompt, options | 一轮最多一个追问，可选项或自由输入；原样提交 id 和 answer，不推断回答 |
| branch | selected, tabs | 每个 tab 有 id/label/blocks，selected 必须存在；点击切换，只渲染当前 tab；不嵌套 branch |
| localfile | artifact_id, label | 带 JWT 下载文件；PNG 可转 Blob/ObjectURL 展示，日志显示为下载链接；没有本地路径 |

`plan` 事件包含科目、专家、任务和技能；`tool` 是进度数据，不作为可信 HTML 渲染。只展示公开教学结果。`warning` 可能表示 Formatter 失败：后端会提供仍符合 JSON schema 的回退并保留追问。在结构可识别时保留合法叶块及原位文件，只对错误叶块引用原文；`fallback=markdown` 表示存在降级叶块，不能假定整篇只有文字。回退不能被展示为“严格 Formatter 验收成功”。

## 实时输出 / Live output

每个 Expert 模型轮次增加以下持久化 SSE 事件，均可按事件 ID 重连续传：

| event | data | 客户端处理 / Client behavior |
|---|---|---|
| output_start | stream_id, round, effort | 创建本轮纯文本预览 / start a plain-text preview |
| output_delta | stream_id, sequence, text | 按 stream_id 和连续 sequence 拼接；重复事件不追加 / append once in sequence |
| output_end | stream_id, status | received 表示上游完整接收，尚不代表展示校验成功；aborted 表示该预览被中断 / received is not rendering approval; aborted is interrupted |
| reasoning_start | stream_id, round, effort | 独立思考面板；仅供应商确实返回 thinking 时创建 / start separate provider reasoning panel |
| reasoning_delta | stream_id, sequence, text | 纯文本拼接，独立于正文序列 / append provider thinking as plain text |
| reasoning_end | stream_id, status | received/aborted；中断不代表完整推理 / mark complete or partial |
| artifact | round, status, artifact_id, name, mime, bytes | 每轮日志；工具轮无正文或调用中断也提供 / downloadable round log, including partial rounds |
| thinking | effort=max, source=planner/expert, reason | 仍是简短升级原因，不是 reasoning_delta / remains a brief escalation notice |

预览只作为 textContent 展示，禁止解析或执行半成品 JSON、HTML、SVG、TeX；若开头为展示 JSON，可暂不显示原始预览。收到 `presentation` 后用其替换本轮预览，不能将两者当成两条答案。一次工具循环可能有多个模型轮次，每次都有新的 stream_id。最终公开对话仍只保存完整 Presentation；没有 final presentation 的 aborted 预览不能保存为完成答案。SSE 断开不会取消调用。

Only validated `presentation` is authoritative. Preview deltas are plain text and may contain incomplete markup or JSON. Replace previews on presentation; do not duplicate them in chat history. Reconnect using durable event IDs. The same contracts appear as typed events in OpenAPI and events/history.

Expert 合格输出会跳过 Formatter，因此一次简单教学通常只包含 Planner + Expert 两次模型调用；主动追问通常只需一次 Expert。仅在格式不能通过检查时额外调用 Formatter。`warning` 后的回退是代码块中的原文，不执行 HTML、链接或公式。前端对 Markdown 继续禁用原始 HTML。

创建及主动追问可传 `allow_max_thinking`（默认 true），false 禁止本回合升级。主模型默认 Low，诗云默认 Off。八位专家均可按需申请一次 Max；Planner 仅能为 Euclid/Turing/Nomos 的极复杂问题提前选择 Max。`plan` 新字段：thinking=default/max、complexity=simple/normal/hard/extreme、max_reason。历史记录缺少这些字段时使用 default/normal/空字符串。

`queued → running → waiting/completed/failed/cancelled` 是一轮执行的状态。waiting 通过 answers 回到 queued；completed/failed/cancelled 通过 messages 主动追问回到 queued，保留原专家与已成功的上下文。归档会话需先取消归档。服务重启把执行中的请求标记 `PROCESS_RESTARTED`，追问会话保留；不要自动重发失败的模型调用。

费用记录 `cost` 是十进制字符串或 null，`currency` 为 USD/CNY，`pricing_basis` 指明估算依据。`input_tokens` 按 Anthropic 口径不包含缓存读/写；读取 `cache_read_tokens/cache_write_tokens` 时不要重复扣费。未知费用显示“未知”，不能显示 ¥0。只向登录用户提供自己的调用记录。

## 历史、消息与可靠提交 / History, messages and reliable submission

`GET /v1/sessions` 是会话目录，支持 `limit/cursor/subject/status/q/archived/pinned`。默认排除归档；`archived=only/all` 可读取归档。排序为置顶优先、更新时间和 ID 降序，游标绑定筛选条件。跨页过程中修改会话可能使其移动，刷新目录即可重新同步。目录不返回完整展示结果或 Base64 图片。

详情返回初始题目、摘要、教学参数、创建/更新时间、置顶/归档与当前状态。完整聊天界面使用 `GET /v1/sessions/{id}/messages?after=0&limit=20`：按公开消息 ID 升序分页，`next_after=null` 表示已读完当前记录。公开消息包含 role（student/assistant）、kind（question/follow_up/answer/presentation）、text、question_id、图片引用、presentation、created_at。旧答案无法精确还原提交时间时标记 `time_accuracy=inferred`；新消息是 exact。公开消息仍只含正式教学结果。供应商 thinking 通过本会话 SSE/事件历史及 .log 读取，系统提示词、签名和原始工具协议不通过这些渠道公开。

学生图片通过消息的 `images[].url` 带 JWT 读取，前端使用 fetch → Blob → ObjectURL。图片必须是真实 PNG/JPEG/WebP/GIF，MIME 与内容匹配；每张 ≤4 MiB、≤400万像素，每次 ≤4 张。语文专家仅接受文本。裁剪压缩在客户端执行。

创建、messages、answers 均支持 `Idempotency-Key`：每次**新的**学生提交生成一个 UUID，重试相同请求时复用同一键。键限定登录用户与具体操作；同键同请求始终返回原始 202 接收回执，`Idempotency-Replayed:true` 表示没有重跑。回执中的 `submission_message_id` 可与公开消息去重。它可能仍显示 queued，后续真实状态必须重新 GET 详情或读事件。相同键改变请求返回 `409 IDEMPOTENCY_CONFLICT`；会话删除后旧回执返回 `410 SESSION_DELETED`。省略幂等键仍兼容旧客户端，但创建和主动追问可能重复执行。

`POST /v1/sessions/{id}/messages` 接受 `message/images/allow_paid_images`。每次追问默认不允许付费绘图，需显式授权；回答老师追问沿用本轮授权。运行中不能追加；waiting 只能提交当前 question_id 的答案。取消或失败后，主动追问是学生明确发起的下一轮，不是自动重试旧收费请求；残缺工具协议会被修复，但不会自动重新执行旧工具。

`PATCH /v1/sessions/{id}` 可改 title/pinned/archived。`DELETE` 先取消运行，再软删除；重复删除返回 204。已删除会话的详情、消息、图片、事件和附件均不可读。账本和底层记录保留供管理员审计，不是物理擦除，不提供恢复 API。

## 事件契约 / Event contract

| event | data | 含义 |
|---|---|---|
| queued / running / completed | `{}` | 本轮排队、执行、完成 |
| plan | Plan | 路由后的科目与专家 |
| message | `{message: PublicMessage}` | 持久化的学生消息或教师展示 |
| presentation | Presentation | 兼容原有教学展示客户端 |
| tool | `{name,result}` | 工具进度，不是可执行代码或 HTML |
| waiting | `{question: QuestionBlock}` | 等待老师当前追问的答案 |
| warning | `{code,fallback:"markdown"}` | 展示回退，包括 Formatter 额度不足 |
| failed / cancelled | `{code}` | 本轮失败或取消 |

新聊天客户端按 message.id 渲染并去重；presentation 是同一教师输出的兼容通知，不要把两者渲染成两个气泡。`GET .../events/history` 返回可分页的结构化 EventPage；OpenAPI 的 SSE 操作也提供 `x-sse-event-schema`。SSE 的 `id` 与消息 ID 是两套游标，不要混用。鉴权失效或会话删除时 SSE 可发送无持久化 id 的 failed 信号后关闭。错误码与恢复动作见 `GET /v1/error-codes`，未识别的新错误码应显示通用错误并保留诊断信息。

## 登录、偏好与 CORS / Login, preferences and CORS

登录返回 access_token 和 refresh_token。access 默认60分钟，refresh默认30天。`POST /v1/auth/refresh` 不要求有效 access_token；每次返回新的 refresh_token，旧令牌只可用一次。客户端必须串行刷新，失败且响应未知时重新登录；重用旧 refresh_token 会撤销该账号所有设备登录。`POST /v1/auth/logout` 撤销全部设备；修改密码需验证原密码，成功后全部令牌失效并重新登录。服务重启不会清空刷新状态。

`GET /v1/me` 读取账号与学习偏好；`GET/PUT /v1/me/preferences` 查询或完整替换 grade/province/textbook/subject。新会话省略的教学字段采用偏好，显式字段优先；偏好不会自动授权付费绘图。没有开放学生注册 API，管理员通过独立管理面板或本地维护 CLI 管理账号。

默认允许 `http://localhost:5173` 和 `http://127.0.0.1:5173`，可在 `config/runtime/network.json` 或管理面板调整并重启服务；私有 `.env` 的 `CORS_ORIGINS` JSON 数组可覆盖 JSON 配置。支持 Authorization、Content-Type、Idempotency-Key、Last-Event-ID 预检；向前端暴露 Idempotency-Replayed。Swagger 同源访问不需要额外 Origin。

## 能力与额度 / Capabilities and budget

`GET /v1/capabilities` 给出实际限制、专家图片支持、供应商、工具状态、绘图模型和 CNY 单价、输出/步骤上限与预算。ready 表示本地执行条件已确认；configured 表示配置齐全但没有在该请求中调用供应商或 nsjail；unavailable/disabled 不应在客户端显示为可用。syllabus 给出指定课标版本、范围、9科列表与模式；ready 表示索引已校验且默认模式可执行，未导入/校验失败/模型缺失均有明确 reason。

`expert_max_output_tokens` 和 `max_output_tokens` 默认均为393216（384K）；实际值同时受供应商模型上限、剩余上下文和会话预算限制。`stage_efforts` 与 `stage_max_output_tokens` 给出阶段配置，Planner/Formatter 默认 Off。思考 token 包含在输出上限中。`streaming=true`、stream_preview_format=text、planner_max_experts 和各专家 allow_max_thinking 可用于能力发现。Markdown 单块最多 2,000,000 字符，整个展示限制 8 MiB；客户端按需渲染长内容。

后端会移除整篇正文的 markdown/md 围栏，接受可直接规范化的 JSON 围栏，并区分公式不等式、TeX行间距和HTML。正式 presentation 的 Markdown 应交给 Markdown 解析器；output_delta 仍只是尚未验证的纯文本预览，不能当正式 Markdown/HTML 执行。校验及 Formatter 都失败时的安全纯文本降级仍使用代码块，伴随 warning；客户端应展示格式降级提示。已保存的旧降级结果保留，不自动收费重新格式化。

Markdown normalization and validation avoid unnecessary formatting calls. Raw streaming text remains an untrusted preview. A warning marks literal fallback when validation and Formatter both fail; historical fallback records are preserved.

`GET /v1/usage` 只返回精简账本，不携带系统提示词、thinking 或供应商原始 request/response。会话所有者可在独立 reasoning 事件和轮次 .log 中读取供应商实际返回的 thinking；完整 request/response 与签名仍仅在管理员 SQLite 审计库。教学内容通过公开消息读取。`GET /v1/usage/summary` 支持 group_by=day/session/total、UTC 日期范围（含起止日，最多366天）、session_id、limit/cursor；币种分别合计，未知费用调用单独计数。旧 USD 记录保持 USD；新 DeepSeek 调用与绘图使用 CNY。

整个会话及其后续追问共用 **5元**，从 `GET /v1/sessions/{id}/budget` 获取 limit/spent/uncertain/reserved/available。新 DeepSeek 调用依据[官方人民币高峰价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)保守估算；不用汇率换算，也不自动假定当前处于低谷。价格可由管理员 PRICE_RATES_CNY 调整。调用前原子预留输入与最大输出费用；图片采用完整模型上下文上界，原生搜索每次最多1次搜索并按服务内往返上界预留。文本使用 UTF-8 字节保守界及协议余量，未知模型或价格会阻止收费调用。

成功且使用量明确时结算并释放差额；超时、取消、缺少使用量等不确定结果将预留额计入 uncertain，不能当作免费。服务重启同样保留未完成调用的保守费用；账本滚动清理不重置预算。旧 USD 调用仅在预算初始化时按已有 token 重估 CNY，原账本不改币种。预算按配置价格与供应商 token/上下文上限执行，金额是保守估算，不是供应商结算账单；供应商改价时管理员需更新单价。不足时阻止新增收费请求，并可能先展示已有答案的 Markdown 回退。

`capabilities.stage_models` 提供各阶段的 provider/model/context_limit/max_output_tokens/effort/consumption_excluded/availability。provider 为可扩展字符串；角色可使用 OpenAI Chat Completions 兼容供应商。按能力响应处理图像、工具、输出和 Max 支持，不根据品牌推断。配置上限不等于可用上下文空间；available token 在调用前裁减，configured 不是实时可用承诺。

Formatter 的 `cost=0`、`pricing_basis` 前缀 `excluded:siliconflow:` 表示按部署配置不计应用消费。不要在费用界面将其说成供应商免费服务；token 照常汇总，预算仍约束未排除的教学与绘图。PROCESSOR_* 错误码表示格式服务回退，详见 error-codes。可信 question 可由服务在格式化后附加，不能依靠模型自行创造。


## 图文、思考与计费 / Inline artifacts, reasoning and pricing

`blocks` 及 `tabs[].blocks` 是有序序列，严格按顺序渲染；不要先提取所有 localfile 再统一放到答案末尾。例如 markdown（图前说明）→localfile（工具返回的真实ID）→markdown（图后解释）。带 JWT 获取 `/v1/artifacts/{artifact_id}`，按响应 Content-Type 识别 PNG 并创建 Blob/ObjectURL；日志为下载附件。产物ID只能来自当前会话工具结果，前端不能将 Markdown 中出现的任意字符串当作文件引用。无图片或文件的普通回答仍为 Markdown。

`reasoning_*` 独立于 `output_*`；两个 channel 有不同 stream_id，各自 sequence 从1开始。按持久化事件ID去重并重连，发现 sequence 缺口先续传，不能静默拼接。思考文本作为可折叠的 textContent 展示，不执行其中的代码、HTML、SVG、链接或工具指令；不得混入正式答案。Off 或供应商未返回 thinking 时没有 reasoning 事件，界面可显示未提供。不存在从 API 取得完整不可见内部推理的保证。签名不会发送。

每个 Expert 模型轮次保存 `.log`，含 effort、received/aborted 状态、供应商返回的 thinking 和当轮正文。调用中断时保留已收到的部分，artifact 事件用于发现日志，即便没有 presentation；只有成功正式展示才在 blocks 末尾追加日志附件。日志按附件大小上限截断并明确标记，不能称截断日志为完整思考。日志只属于本会话用户，并沿用软删除及 JWT 校验。旧日志保持原内容，不回溯补造思考。

`capabilities` 提供 reasoning_streaming、reasoning_source=provider_returned_thinking、reasoning_format=text、artifact_placement=ordered_blocks，以及 pricing（mode/current_band/timezone/peak_weekdays/peak_windows/off_peak_multiplier/holiday_calendar_years/holiday_calendar_complete/version/settlement_clock/reservation_band）。weekday采用 Monday=0。current_band 是能力查询时的时间，不代表后来调用的费率。

DeepSeek 默认 PRICING_MODE=auto；当前 JSON 配置以北京时间周一至周五09:00≤t<12:00、14:00≤t<18:00为峰时，其他时间、周末与所列中国节假日为谷时，谷时倍率为0.5。调用开始时间、所选单价、规则版本和模式写入 pricing_basis，同一调用跨边界不重选费率。费用仍是估算，允许排队与供应商计费时钟的轻微误差；上游usage包含思考token时已经计在output_tokens，不能再次相加。预留按峰时，最终按所选费率结算释放差额；未知/取消调用保留未知额度。历史费用不重新计价，供应商改价时须更新配置。

`config/models/Pricing.json` 包含节假日日历；新年度需维护 holiday_ranges 和 holiday_calendar_years 并重启。未维护年度沿用周一至周五/周末规则，holiday_calendar_complete=false，可能在节假日高估费用。调休周末依所配置的工作日口径仍作谷时。人工peak/off_peak只用于显式部署覆盖。

SVG 的高分辨率与光滑曲线规范由专家提示词约束；Render 清除SVG浏览器默认边距，按请求尺寸截图。方图至少1024×1024，精确图用对应viewBox和解析C/Q/A路径，间断处分段。提示词与静态校验不保证每幅图的数学精度，关键科学图仍需教学内容检查。

客户端应容忍未识别的附加事件并保留正文及最终展示。实现思考视图时使用独立 channel，保留 thinking 升级事件的简短原因语义；后端升级不会自动完成客户端界面改造。

## 配置与保留周期

PythonBox 由后端同机 Linux nsjail 执行，不需要独立 8766 服务。账本、日志和图像保留周期由管理员 JSON 配置控制；已过期产物下载返回 410 ARTIFACT_EXPIRED，可显示“文件已过期”，会话正文/公开消息仍可读取，预算不会重置。

## 本地课标检索

`POST /v1/syllabus/search` 使用学生 JWT，输入 `query`，可选 `subject`（原有九科英文 ID）、`course_category`（必修课程/选择性必修课程）、`mode`（lexical/semantic/hybrid）、`top_k`（1–10）。默认 hybrid；成功返回 `status=ok`、`edition`、`mode`、`count`、`results`。不可用返回 503；向量查询超过 512 token 返回 400 `SYLLABUS_QUERY_TOO_LONG`。课程类别不限年级、教材版本或省份，不能据此推断它们的独立要求。

命中包含原文 `text`、`heading_path`、`section_id`、实际/原始课程类别、共享 `scope`、`quality_warnings`、排序分数与方法。`source.file` 为 PDF 文件名，`source.pdf_pages` 为从 1 开始的 PDF 页序，`source.printed_pages` 为印刷页，`source.sha256` 为来源 PDF 的 SHA-256。`GET /v1/syllabus/sections/{section_id}` 取得完整父章节、逐页 fragments、分类依据和表格元数据；不存在返回 404。这些不是产物 ID，不使用附件下载接口，原 PDF 未随服务复制。

`capabilities.syllabus` 报告实际状态、版本、科目、片段/章节数、模式与原因码。支持的 2017 年版包为九科、567 个片段/261 个章节；数据与模型不随公开源码分发，未导入时为 unavailable。其内容非独立的高考考纲或省级细则；分数不是置信度，无命中不代表不考。按原样展示质量提示，共用内容不得擅自改成专属必修或选必。该检索在本机运行，无云端 embedding 费用。

/admin 是独立管理员面板，使用单独密码与 Cookie/CSRF，学生 Bearer JWT 无效。局域网管理需 HTTPS。更新网络配置后由管理员重启；正在教学时拒绝重启，空闲重启后仍可根据已有事件 ID 恢复历史 SSE，不自动重做或重复计费。
