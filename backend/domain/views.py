def session_view(row):
    state = row["state"]
    request = state["request"]
    return {
        **session_summary(row),
        "question": state["question"],
        "presentations": state["presentations"],
        "error": state["error"],
        "original_message": request.get("message", ""),
    }


def session_summary(row):
    meta = row.get("request_meta") or {}
    if isinstance(meta, str):
        import json

        meta = json.loads(meta)
    state = row.get("state", {})
    request = state.get("request", meta)
    plan = state.get("plan")
    return {
        "id": row["id"],
        "title": row.get("title", ""),
        "summary": row.get("summary", ""),
        "subject": (plan or {}).get("subject") or row.get("subject") or request.get("subject"),
        "grade": request.get("grade", "高三"),
        "province": request.get("province"),
        "textbook": request.get("textbook", "部编版/普通高中教材"),
        "image_count": row.get("image_count", len(request.get("images", []))),
        "status": row["status"],
        "expert": (plan or {}).get("expert") or row.get("expert"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "pinned": bool(row.get("pinned", 0)),
        "archived": bool(row.get("archived", 0)),
    }
