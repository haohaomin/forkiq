"""Low-noise, in-panel notifications derived from actionable events."""

ACTIONABLE_KINDS = frozenset(("blocked", "error", "auth"))
USER_ACTION_MARKERS = (
    "已暂停", "已恢复", "已请求回滚", "同步方式切换为", "批量操作", "已移出管理",
)


def is_actionable(event):
    """Return whether an event needs the administrator's attention.

    A manual pause is an audit event, not an alert. Engine-generated blocked
    events remain visible as notifications, including deletion protection,
    branch changes, and uncertain force pushes.
    """
    kind = event["kind"] if isinstance(event, dict) else event["kind"]
    if kind in ("error", "auth"):
        return True
    if kind != "blocked":
        return False
    message = event["message"] or ""
    return not any(marker in message for marker in USER_ACTION_MARKERS)


def _events(db, limit=100):
    rows = db.all(
        "SELECT e.*, r.full_name FROM events e "
        "LEFT JOIN repositories r ON r.id=e.repo_id "
        "WHERE e.kind IN ('blocked','error','auth') ORDER BY e.id DESC LIMIT ?",
        (max(1, min(int(limit), 500)),),
    )
    return [row for row in rows if is_actionable(row)]


def seen_id(db):
    try:
        return max(0, int(db.get_setting("notifications_seen_event", "0")))
    except (TypeError, ValueError):
        return 0


def list_notifications(db, limit=100):
    return _events(db, limit)


def unread_notifications(db, limit=100):
    seen = seen_id(db)
    return [row for row in _events(db, limit) if row["id"] > seen]


def unread_count(db):
    return len(unread_notifications(db, 500))


def mark_all_read(db):
    rows = _events(db, 500)
    if rows:
        db.set_setting("notifications_seen_event", rows[0]["id"])
    return len(rows)
