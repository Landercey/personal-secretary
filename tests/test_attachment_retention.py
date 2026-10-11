"""附件过期清理（[store] attachment_retention_days）。

**为什么值得单独测**：这个开关在文档和设置界面上都写着"图片/文件保留 N 天，
过期自动删"，可代码从来没读过它 —— 也就是"承诺了不清理"，用半年 `data`
文件夹能长到几个 G。而清理这东西一旦写错方向就是**删用户的文件**，
所以三条安全线都得钉住：

  1. 未完成待办的附件**不能动**（人还没做呢，图先没了？）
  2. 只删附件目录里面的文件（库里那串路径被人改过也不能拿它去删别处）
  3. 记录跟着文件一起删（不然界面上留一个点不开的附件）
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import (                                  # noqa: E402
    KIND_IMAGE, TIME_FORMAT, Attachment, Message, SOURCE_MANUAL, TaskDraft,
)
from app.runtime.config import attachments_dir                      # noqa: E402
from app.store.repository import SqliteStore, open_database         # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name
        self.store = SqliteStore(open_database(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.store.close()
        if self._old_home is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old_home
        self.tmp.cleanup()

    def add_task(self, name: str, *, age_days: float = 0,
                 path: Path | None = None) -> tuple[int, Path]:
        """建一条带附件的待办，返回 (task_id, 附件文件)。"""
        target = path or (attachments_dir() / name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fake-image-bytes")
        message = Message(msg_id=f"manual:{name}", source_kind=SOURCE_MANUAL,
                          text=f"看这个 {name}")
        draft = TaskDraft(title=f"待办 {name}", source_kind=SOURCE_MANUAL,
                          origin_msg_id=message.msg_id,
                          attachments=[Attachment(name=name, path=str(target),
                                                  mime="image/png",
                                                  kind=KIND_IMAGE, size=16)])
        task = self.store.save_drafts(message, [draft])[0]
        if age_days:
            stamp = (datetime.now() - timedelta(days=age_days)).strftime(TIME_FORMAT)
            with self.store.conn:
                self.store.conn.execute(
                    "UPDATE tasks SET created_at=? WHERE task_id=?",
                    (stamp, task.task_id))
        return task.task_id, target

    def rows_for(self, task_id: int) -> int:
        row = self.store.conn.execute(
            "SELECT COUNT(*) n FROM attachments WHERE task_id=?", (task_id,)
        ).fetchone()
        return int(row["n"])


class Retention(Base):
    def test_old_done_attachment_is_removed(self):
        task_id, file = self.add_task("old.png", age_days=40)
        self.store.set_status(task_id, "done", "测试")

        removed = self.store.purge_old_attachments()

        self.assertEqual(removed, 1)
        self.assertFalse(file.exists(), "过期的附件文件没删掉")
        self.assertEqual(self.rows_for(task_id), 0, "记录还在，界面上会留一个点不开的附件")

    def test_open_task_keeps_its_attachment(self):
        """人还没做完，图不能先没了。"""
        task_id, file = self.add_task("still-open.png", age_days=400)

        removed = self.store.purge_old_attachments()

        self.assertEqual(removed, 0)
        self.assertTrue(file.exists())
        self.assertEqual(self.rows_for(task_id), 1)

    def test_young_done_attachment_is_kept(self):
        task_id, file = self.add_task("fresh.png", age_days=3)
        self.store.set_status(task_id, "done", "测试")

        self.assertEqual(self.store.purge_old_attachments(), 0)
        self.assertTrue(file.exists())

    def test_zero_means_never_clean(self):
        from app.runtime.config import config

        task_id, file = self.add_task("forever.png", age_days=999)
        self.store.set_status(task_id, "done", "测试")
        old = config.get("store", "attachment_retention_days")
        config._values.setdefault("store", {})["attachment_retention_days"] = 0
        try:
            self.assertEqual(self.store.purge_old_attachments(), 0)
            self.assertTrue(file.exists())
        finally:
            config._values["store"]["attachment_retention_days"] = old

    def test_file_outside_the_attachment_folder_is_never_touched(self):
        """库里那串路径要是被改成别处（手改库、老数据），也不能拿它去删文件。"""
        outside = Path(self.tmp.name) / "别动我.txt"
        outside.write_text("user data", encoding="utf-8")
        task_id, _ = self.add_task("weird.png", age_days=40, path=outside)
        self.store.set_status(task_id, "done", "测试")

        self.store.purge_old_attachments()

        self.assertTrue(outside.exists(), "删到了附件目录外面的文件")
        self.assertEqual(self.rows_for(task_id), 0)   # 记录还是要清掉

    def test_missing_file_does_not_break_cleanup(self):
        task_id, file = self.add_task("gone.png", age_days=40)
        self.store.set_status(task_id, "done", "测试")
        file.unlink()                                  # 用户自己删了

        self.assertEqual(self.store.purge_old_attachments(), 1)
        self.assertEqual(self.rows_for(task_id), 0)


if __name__ == "__main__":
    unittest.main()
