"""开机补收：程序关着那段时间的消息，能不能补回来。

用户原话："现在能实现读取打开程序前的发的消息吗"。
做法：刚连上时问 NapCat 要每个会话的最近几条历史，转成事件后走同一条管线
（所以"收不收这个会话、算不算待办、去重"全都复用现成的规矩）。

这里盯四件事：

  1. 两种回执形状都认，而且**从旧到新**排好（"60 秒内连着发的合成一条"靠顺序）；
  2. 太老的、超出上限的**不许灌进来** —— 开着 AI 时每条都要过一次模型，
     一次补 200 条会让开机卡很久；
  3. 没勾的会话**不许问**（"收所有私聊"尤其不能问：那等于给每个好友各发一次
     历史请求，动作太多也太像扫号）；
  4. 关掉开关就一次请求都不发。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime.config import Config as _Config                   # noqa: E402
from app.sources.qq import QQSettings, QQSource, history_events    # noqa: E402

qq_module = importlib.import_module("app.sources.qq")

GROUP = 1107370711
FRIEND = 41753719


def message(mid: str, text: str, *, when: float | None = None,
            user: int = 203554647) -> dict:
    return {"message_id": mid, "real_id": int(mid), "user_id": user,
            "time": int(when if when is not None else time.time()),
            "sender": {"nickname": "班长", "user_id": user},
            "message": [{"type": "text", "data": {"text": text}}]}


class TestHistoryShape(unittest.TestCase):
    def test_wrapper_shape(self):
        reply = {"status": "ok", "data": {"messages": [message("1", "交作业")]}}
        events = history_events(reply, "group", GROUP)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["post_type"], "message")
        self.assertEqual(events[0]["message_type"], "group")
        self.assertEqual(events[0]["group_id"], GROUP)

    def test_bare_list_shape(self):
        # 私聊消息的 user_id 是"对方"（自己发的那条 user_id 也是对方，
        # sender.user_id 才是自己）—— 所以这里按对方来造。
        reply = {"status": "ok", "data": [message("2", "开会", user=FRIEND)]}
        events = history_events(reply, "private", FRIEND)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["user_id"], FRIEND)

    def test_oldest_first(self):
        """历史常常是倒着给的，正着排才对 —— 顺序错了合并窗口就会看错。"""
        now = time.time()
        reply = {"data": {"messages": [message("3", "第三", when=now),
                                       message("1", "第一", when=now - 120),
                                       message("2", "第二", when=now - 60)]}}
        found = [e["message_id"] for e in history_events(reply, "group", GROUP)]
        self.assertEqual(found, ["1", "2", "3"])

    def test_garbage_is_not_an_error(self):
        for reply in (None, {}, {"data": None}, {"data": {"messages": None}},
                      {"data": {"messages": ["不是字典", 1]}}):
            with self.subTest(reply=reply):
                self.assertEqual(history_events(reply, "group", GROUP), [])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = _Config.load(Path(self.tmp.name) / "config.toml")
        self._old = qq_module.config
        qq_module.config = self.config

    def tearDown(self):
        qq_module.config = self._old
        self.tmp.cleanup()

    def source(self, settings: QQSettings, delivered: list) -> QQSource:
        return QQSource(settings=settings, on_message=delivered.append)


class TestWhichChatsGetAsked(Base):
    def test_only_checked_chats(self):
        settings = QQSettings(enabled=True, group_ids=[GROUP],
                              private_ids=[FRIEND], inbox_ids=[999],
                              receive_private_all=True)
        targets = self.source(settings, [])._catch_targets()
        self.assertEqual(targets, [("group", GROUP), ("private", FRIEND),
                                   ("private", 999)])
        # "收所有私聊"不算：不能给每个好友都发一次历史请求
        self.assertNotIn(("private", 0), targets)

    def test_duplicates_are_asked_once(self):
        settings = QQSettings(enabled=True, group_ids=[GROUP, GROUP],
                              private_ids=[FRIEND], inbox_ids=[FRIEND])
        self.assertEqual(self.source(settings, [])._catch_targets(),
                         [("group", GROUP), ("private", FRIEND)])

    def test_nothing_checked_means_no_requests(self):
        source = self.source(QQSettings(enabled=True), [])
        called: list = []

        async def stub(action, params, timeout=8.0):
            called.append(action)
            return {}

        source.call_api = stub
        asyncio.run(source._catch_up())
        self.assertEqual(called, [])


class TestCatchUp(Base):
    def run_catch_up(self, payloads: dict, settings: QQSettings,
                     delivered: list) -> list:
        """跑一次补收。payloads: {action: 回执}。返回发出的请求。"""
        source = self.source(settings, delivered)
        calls: list = []

        async def stub(action, params, timeout=8.0):
            calls.append((action, dict(params)))
            return payloads.get(action, {})

        source.call_api = stub
        asyncio.run(source._catch_up())
        return calls

    def test_brings_back_messages_in_order(self):
        now = time.time()
        payload = {"data": {"messages": [message("2", "第二", when=now),
                                         message("1", "交作业", when=now - 30)]}}
        delivered: list = []
        calls = self.run_catch_up({"get_group_msg_history": payload},
                                  QQSettings(enabled=True, group_ids=[GROUP]),
                                  delivered)

        self.assertEqual([c[0] for c in calls], ["get_group_msg_history"])
        self.assertEqual(calls[0][1]["group_id"], GROUP)
        self.assertEqual([m.text for m in delivered], ["交作业", "第二"])

    def test_private_chats_use_the_friend_action(self):
        payload = {"data": {"messages": [message("1", "私聊内容", user=FRIEND)]}}
        delivered: list = []
        calls = self.run_catch_up(
            {"get_friend_msg_history": payload},
            QQSettings(enabled=True, private_ids=[FRIEND]), delivered)
        self.assertEqual([c[0] for c in calls], ["get_friend_msg_history"])
        self.assertEqual(calls[0][1]["user_id"], FRIEND)
        self.assertEqual(len(delivered), 1)

    def test_too_old_messages_are_skipped(self):
        now = time.time()
        payload = {"data": {"messages": [
            message("1", "两天前的", when=now - 48 * 3600),
            message("2", "刚才的", when=now - 60)]}}
        delivered: list = []
        self.run_catch_up({"get_group_msg_history": payload},
                          QQSettings(enabled=True, group_ids=[GROUP]), delivered)
        self.assertEqual([m.text for m in delivered], ["刚才的"])

    def test_total_cap_is_respected(self):
        now = time.time()
        payload = {"data": {"messages": [
            message(str(i), f"第{i}条", when=now - 300 + i) for i in range(1, 41)]}}
        self.config.set("qq", "catch_up_total", 5)
        self.config.set("qq", "catch_up_count", 40)
        delivered: list = []
        self.run_catch_up({"get_group_msg_history": payload},
                          QQSettings(enabled=True, group_ids=[GROUP]), delivered)
        self.assertEqual(len(delivered), 5)

    def test_switch_off_means_no_request(self):
        self.config.set("qq", "catch_up", False)
        calls = self.run_catch_up(
            {"get_group_msg_history": {"data": {"messages": [message("1", "x")]}}},
            QQSettings(enabled=True, group_ids=[GROUP]), [])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
