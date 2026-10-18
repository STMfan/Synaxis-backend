from copy import deepcopy

from fastapi.openapi.docs import get_swagger_ui_html

OPERATIONS = {
    "search_syllabus": ("检索本地课标", "Search local curriculum standards"),
    "syllabus_section": ("读取课标原章节", "Read original curriculum section"),
    "health": ("服务状态", "Service health"),
    "token": ("用户登录", "User login"),
    "experts": ("专家与工具", "Experts and skills"),
    "create_session": ("开始教学会话", "Start tutoring session"),
    "get_session": ("读取会话结果", "Read session results"),
    "events": ("订阅教学事件", "Subscribe to tutoring events"),
    "answer": ("回答追问并继续", "Answer and continue"),
    "cancel": ("取消会话", "Cancel session"),
    "usage": ("查询调用与费用", "Query calls and costs"),
    "artifact": ("下载中间文件", "Download artifact"),
    "list_sessions": ("查询历史会话", "List session history"),
    "patch_session": ("重命名、置顶或归档", "Rename, pin or archive"),
    "delete_session": ("删除会话目录记录", "Delete session history entry"),
    "messages": ("读取公开对话记录", "Read public conversation"),
    "send_message": ("主动追问并继续", "Send a follow-up"),
    "message_image": ("读取学生上传的图片", "Read student image"),
    "event_history": ("分页读取结构化事件", "Read typed event history"),
    "capabilities": ("客户端能力发现", "Discover client capabilities"),
    "budget": ("读取会话消费额度", "Read session budget"),
    "usage_summary": ("按会话、日期与币种汇总", "Summarize by session, date and currency"),
    "me": ("读取个人资料", "Read profile"),
    "get_preferences": ("读取学习偏好", "Read learning preferences"),
    "set_preferences": ("保存学习偏好", "Save learning preferences"),
    "change_password": ("修改密码并撤销登录", "Change password and revoke logins"),
    "refresh": ("轮换刷新令牌", "Rotate refresh token"),
    "logout": ("退出全部设备", "Log out all devices"),
    "error_codes": ("错误码与恢复动作", "Error codes and recovery actions"),
}
DESCRIPTIONS = {
    "search_syllabus": (
        "离线检索指定2017年版9科课标。subject使用英文ID；支持必修/选择性必修过滤及lexical/semantic/hybrid，默认hybrid。source.pdf_pages为1起始PDF页序，printed_pages为印刷页，sha256为原PDF哈希。分数不是置信度；保留共用范围、分类依据和质量提示。不包含独立高考考纲或各省细则，未命中不能推断不考。不会调用云端embedding或计费接口。",
        "Offline search of the designated 2017 standards for nine subjects. Use English subject IDs; filter compulsory/selective-compulsory content. Modes: lexical/semantic/hybrid (default). source.pdf_pages is 1-based PDF order; printed_pages is printed numbering; sha256 identifies the source PDF. Scores are rankings, not confidence. Preserve shared scope and quality warnings. No separate exam/provincial syllabus; no hit does not imply exclusion. No cloud embedding or paid call.",
    ),
    "syllabus_section": (
        "使用检索命中的section_id读取完整父章节、逐页原文及课程分类依据。不存在返回404，未就绪返回503。",
        "Use a search hit's section_id to read its parent, page fragments and classification evidence. Missing: 404; unavailable: 503.",
    ),
    "token": (
        "管理员直接维护 users 表；无开放注册。Swagger Authorize 使用用户名和密码。",
        "Administrators maintain the users table. No public registration. Use username/password in Swagger Authorize.",
    ),
    "create_session": (
        "返回 queued；推荐每次提交发送唯一 Idempotency-Key。相同键与请求返回原始 202 回执，不重复执行；改动请求返回 409，已删除会话返回 410。省略教学参数时采用账号偏好。",
        "Returns queued. Send a unique Idempotency-Key per submission. Matching retries return the original 202 receipt without rerunning; changed payload gives 409, deleted session gives 410. Omitted teaching parameters use account preferences.",
    ),
    "events": (
        "正文用 output_start/delta/end；思考用 reasoning_start/delta/end，按 stream_id、sequence 拼接。完整 presentation 才能渲染，blocks 顺序决定图文位置。artifact 是轮次日志；thinking 仅为 Max 升级原因。15秒注释心跳；after/Last-Event-ID 续传，轮次结束正常关闭。断开不会取消教学；切回前台先查询状态，再为运行中的会话续订。结构见 EventPage。",
        "Text uses output_start/delta/end; reasoning uses reasoning_start/delta/end. Append by stream_id/sequence. Render validated presentation in block order. artifact is a round log; thinking reports Max escalation only. Comment heartbeats every 15s. Resume via after/Last-Event-ID; a turn ends with normal EOF. Disconnect does not cancel teaching. On foreground, sync state and resubscribe only while running. See EventPage.",
    ),
    "answer": (
        "question_id 必须匹配当前追问。支持 Idempotency-Key；没有键的重复提交返回 409。恢复现有 Expert 上下文与同一消费额度。",
        "question_id must match the pending question. Supports Idempotency-Key; unkeyed duplicates return 409. Resumes Expert context under the same budget.",
    ),
    "usage": (
        "仅查自己的调用，token 为供应商值，cost 为估算或 null。DeepSeek 按调用开始时的北京时间峰谷费率，pricing_basis 含时间、费率快照；预算按峰时预留。USD/CNY 分开，不等同供应商账单。",
        "Own calls, provider token counts; cost is estimated or null. DeepSeek uses Beijing peak/off-peak rates at request start, snapshotted in pricing_basis; budget reserves peak rates. Keep USD/CNY separate; not a supplier invoice.",
    ),
    "artifact": (
        "文件 ID 按用户鉴权，禁止通过路径读取。文本/PNG 以附件返回。",
        "Artifact IDs are owner-authorized. Arbitrary paths are not accepted. Text/PNG returned as attachments.",
    ),
    "list_sessions": (
        "按置顶、更新时间、ID 降序分页；游标绑定筛选条件。默认排除归档，archived=only/all 可调整；q 搜索标题、题目摘要和学生公开消息。更新或置顶会改变实时目录顺序。",
        "Keyset pagination by pin, updated time and ID descending; cursors bind filters. Archives excluded by default; use archived=only/all. q searches titles, summaries and student public messages. Updates can move entries in the live directory.",
    ),
    "messages": (
        "按消息 ID 升序分页；after=0 从首条开始。公开消息含学生题目/回答/追问和教学展示；图片是带 JWT 下载的引用。旧记录推断的时间标为 inferred。",
        "Paginate ascending message IDs; after=0 starts at the beginning. Includes student questions/answers/follow-ups and teaching presentations. Images are JWT-protected references. Reconstructed legacy timestamps are marked inferred.",
    ),
    "send_message": (
        "completed/failed/cancelled 可继续，保留 Expert 上下文；waiting 必须先调用 answers，运行中返回 409。支持 Idempotency-Key。本轮付费绘图默认不授权；后续追问共享整个会话 5 元额度。",
        "Continue completed/failed/cancelled sessions using Expert context. waiting requires answers; running gives 409. Supports Idempotency-Key. Paid images default to no consent per follow-up. All turns share the session CNY 5 budget.",
    ),
    "delete_session": (
        "先取消执行，再软删除；重复删除返回 204。目录、详情、消息、事件与附件均不可再读取。调用账本和底层数据保留供管理审计；不是物理擦除。幂等旧回执返回 410。",
        "Cancels execution then soft-deletes; repeated deletion gives 204. Hides directory entries, details, messages, events and files. Ledger and underlying data remain for administration; no physical erasure. Old idempotent receipts give 410.",
    ),
    "refresh": (
        "不需要有效 access token。每个 refresh token 仅使用一次；原子轮换。重用旧 token 会撤销该账号所有登录；客户端必须串行刷新，不能自动重试已完成的刷新。",
        "No valid access token required. Refresh tokens are single-use and rotate atomically. Reuse revokes all account logins. Serialize refresh operations; never automatically retry completed rotations.",
    ),
    "change_password": (
        "验证原密码后更新 Argon2id 哈希，撤销全部 access/refresh token；成功后重新登录。",
        "Verifies the current password, updates Argon2id and revokes all access/refresh tokens. Log in again after success.",
    ),
    "logout": (
        "撤销该账号全部设备的 access/refresh token，返回 204。",
        "Revokes access/refresh tokens on all account devices; returns 204.",
    ),
    "capabilities": (
        "返回图片限制、专家支持、供应商、工具状态、绘图单价与消费上限。configured 表示配置齐全，未代表实时验收。syllabus包含本地课标版本、范围、模式与就绪情况；PythonBox使用本机Linux nsjail。",
        "Returns image limits, experts, providers, tool status, image price and budget. configured does not mean live-verified. syllabus reports the local corpus edition, scope, modes and readiness; PythonBox uses local Linux nsjail.",
    ),
    "budget": (
        "整个会话累计上限 5 元，包含追问、联网助理与绘图。调用前保守预留，按人民币高峰价估算；结果未知时保留 uncertain，不视为免费。独立持久化，不因账本清理重置；不是供应商结算账单。",
        "CNY 5 total per session including follow-ups, assistants and images. Conservative reservations precede dispatch; estimates use CNY peak rates. Unknown outcomes retain uncertain charges. Persisted independently of ledger retention; not a provider invoice.",
    ),
    "usage_summary": (
        "UTC 自然日，含起止日期，最多 366 天；默认最近 30 天。分币种汇总十进制费用，unknown_cost_calls 单列，不把未知费用视为 0。支持 day/session/total 与游标。",
        "Inclusive UTC dates, at most 366 days; defaults to the last 30 days. Decimal costs stay separated by currency; unknown_cost_calls is explicit. Supports day/session/total with cursors.",
    ),
    "set_preferences": (
        "PUT 完整替换偏好；缺省字段恢复默认。只作为新会话的默认值，显式提交优先；不授予付费绘图权限。",
        "PUT replaces preferences; omitted fields reset to defaults. Applies to new-session defaults, with explicit request fields taking precedence. Does not grant paid-image consent.",
    ),
}


def localized_schema(schema: dict, lang: str):
    result = deepcopy(schema)
    index = 0 if lang == "zh" else 1
    result["info"]["title"] = "Synaxis 教学 API" if lang == "zh" else "Synaxis Teaching API"
    result["info"]["description"] = (
        "轻量教学 Harness：Planner → Expert/Skills → 格式校验，按需 Formatter。"
        if lang == "zh"
        else "Lightweight teaching harness: Planner → Expert/Skills → validation, Formatter when needed."
    )
    for path in result["paths"].values():
        for operation in path.values():
            if isinstance(operation, dict):
                key = operation.get("operationId")
                if key in OPERATIONS:
                    operation["summary"] = OPERATIONS[key][index]
                if key in DESCRIPTIONS:
                    operation["description"] = DESCRIPTIONS[key][index]
                if "tags" in operation:
                    operation["tags"] = [
                        tag.split(" / ")[1 if lang == "zh" else 0] if " / " in tag else tag
                        for tag in operation["tags"]
                    ]

    def translate_descriptions(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "description" and isinstance(item, str) and " / " in item:
                    parts = item.split(" / ", 1)
                    value[key] = parts[1 if lang == "zh" else 0]
                else:
                    translate_descriptions(item)
        elif isinstance(value, list):
            for item in value:
                translate_descriptions(item)

    translate_descriptions(result)
    event_schema = (
        result["components"]["schemas"]
        .get("EventPage", {})
        .get("properties", {})
        .get("items", {})
        .get("items")
    )
    if event_schema:
        result["paths"]["/v1/sessions/{session_id}/events"]["get"]["x-sse-event-schema"] = deepcopy(
            event_schema
        )
    return result


def swagger_html(lang: str):
    page = get_swagger_ui_html(
        openapi_url=f"/openapi.json?lang={lang}",
        title="Synaxis · Swagger UI",
        swagger_ui_parameters={
            "persistAuthorization": False,
            "defaultModelsExpandDepth": 1,
            "displayRequestDuration": True,
            "docExpansion": "list",
        },
    )
    nav = '<nav style="padding:12px 24px;font:16px system-ui;background:#f5f7fa">Synaxis · <a href="/docs?lang=zh">中文</a> / <a href="/docs?lang=en">English</a></nav>'
    return page.body.decode().replace("<body>", "<body>" + nav)
