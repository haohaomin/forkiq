import unittest
from pathlib import Path
import tempfile

from app.db import DB, log_event
from app.notifications import is_actionable, list_notifications, mark_all_read, unread_count


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = DB(Path(self.tmp.name) / "notifications.db")
        self.addCleanup(self.db.close)

    def test_only_actionable_events_are_unread_and_manual_actions_are_quiet(self):
        log_event(self.db, None, "blocked", "demo 已暂停")
        log_event(self.db, None, "check", "demo 已修改设置")
        log_event(self.db, None, "blocked", "demo 异常删除保护：删除比例超过 50%")
        log_event(self.db, None, "error", "备份失败")
        self.assertEqual(unread_count(self.db), 2)
        self.assertEqual([row["kind"] for row in list_notifications(self.db)], ["error", "blocked"])

    def test_mark_all_read_is_idempotent_and_new_event_is_unread(self):
        log_event(self.db, None, "error", "第一次错误")
        self.assertEqual(mark_all_read(self.db), 1)
        self.assertEqual(unread_count(self.db), 0)
        self.assertEqual(mark_all_read(self.db), 1)
        log_event(self.db, None, "auth", "认证失败")
        self.assertEqual(unread_count(self.db), 1)

    def test_user_action_filter_does_not_hide_engine_failures(self):
        self.assertFalse(is_actionable({"kind": "blocked", "message": "demo 已恢复"}))
        self.assertTrue(is_actionable({"kind": "blocked", "message": "demo 强制同步结果未确认"}))
        self.assertTrue(is_actionable({"kind": "auth", "message": "认证失败"}))


if __name__ == "__main__":
    unittest.main()
