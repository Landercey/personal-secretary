"""新待办默认带哪几档提醒。

用户原话："加一个提醒力度默认是全选，不然都不提醒"。
以前 `remind_leads` 是空的，而空 = 只在每日汇总里出现 —— 新待办一声不响，
用户会得出"这软件不提醒我"的结论（而事实是没人给它选过档）。

现在入库时按来源填默认值：**待办一套、课表一套**（一天四五节课，
配成和待办一样的四档会变成轰炸）。这里盯四件事：

  1. 新待办真的拿到了默认档；
  2. 课表走自己那套（`[remind] timetable_leads`）；
  3. 想安静就把默认清空（留空 = 回到老行为）；
  4. 下拉里没有的档会被丢掉 —— 不然详情里那个勾选框根本不显示，
     用户会以为设置没生效。
"""

from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import (                                  # noqa: E402
    SOURCE_MANUAL, SOURCE_TIMETABLE, Message, TaskDraft,
)
from app.runtime.config import Config as _Config                    # noqa: E402
from app.store.repository import SqliteStore, open_database         # noqa: E402

repo = importlib.import_module("app.store.repository")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = _Config.load(self.root / "config.toml")
        self._old_config = repo.config
        repo.config = self.config          # 待办入库时读的是这一个
        self.store = SqliteStore(open_database(self.root / "t.db"))

    def tearDown(self):
        self.store.close()
        repo.config = self._old_config
        self.tmp.cleanup()

    def leads_of(self, source_kind: str) -> str:
        message = Message(msg_id=f"m:{source_kind}", source_kind=source_kind,
                          source_name="测试", text="22月22日 10:00 交作业")
        self.store.save_message(message)
        # 草稿自己带 source_kind（真管线里规则解析器就是这么填的）——
        # TaskDraft 的默认值是 manual，不显式给就永远是待办那一套。
        self.store.save_drafts(message, [TaskDraft(title="交作业",
                                                   due_at="2026-12-20 10:00:00",
                                                   source_kind=source_kind)])
        task = self.store.list_tasks("open")[0]
        return str(task.remind_leads or "")


class TestDefaults(Base):
    def test_new_task_gets_the_default_chips(self):
        """默认就是"全选"（[60, 30, 10, 3]）—— 用户要的就是这个。"""
        self.assertEqual(self.leads_of(SOURCE_MANUAL), "60,30,10,3")

    def test_timetable_has_its_own_set(self):
        self.assertEqual(self.leads_of(SOURCE_TIMETABLE), "10")

    def test_can_be_emptied_back_to_silent(self):
        self.config.set("remind", "default_leads", [])
        self.assertEqual(self.leads_of(SOURCE_MANUAL), "")

    def test_quiet_timetable(self):
        self.config.set("remind", "timetable_leads", [])
        self.assertEqual(self.leads_of(SOURCE_TIMETABLE), "")

    def test_value_outside_options_is_dropped(self):
        """下拉里没有 15，就别悄悄写进待办 —— 详情里看不见等于没生效。"""
        self.config.set("remind", "timetable_leads", [15, 10])
        self.assertEqual(self.leads_of(SOURCE_TIMETABLE), "10")

    def test_custom_pair_is_kept(self):
        self.config.set("remind", "default_leads", [30, 10])
        self.assertEqual(self.leads_of(SOURCE_MANUAL), "30,10")


if __name__ == "__main__":
    unittest.main()
