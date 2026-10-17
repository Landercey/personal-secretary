"""M1：错误词典、结构化日志、诊断包、`--replay`。

这一层的价值不在"跑起来"，而在**出事的时候能查**：
  · 编号稳定（能 grep、能当 Issue 标题）；
  · 每条编号都有"是什么/为什么/怎么办"；
  · 日志里有完整堆栈，且秘密（token/key）已经打码；
  · 一条命令能把库里的原话重跑一遍，看规则怎么判的。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = str(self.home)
        (self.home / "data").mkdir()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old
        self.tmp.cleanup()


class TestErrorDictionary(unittest.TestCase):
    def test_every_code_has_what_why_and_fix(self):
        from app.runtime import errors

        self.assertGreaterEqual(len(errors.ERRORS), 10)
        for code, item in errors.ERRORS.items():
            with self.subTest(code=code):
                self.assertTrue(item.what, "缺'发生了什么'")
                self.assertTrue(item.why, "缺'为什么'")
                self.assertTrue(item.fix, "缺'怎么办'")

    def test_human_text_names_the_fix(self):
        from app.runtime import errors

        text = errors.human("QQ.BIND.PORT_BUSY")
        self.assertIn("端口", text)
        self.assertIn("怎么办", text)

    def test_unknown_code_still_says_something_useful(self):
        from app.runtime import errors

        text = errors.human("SOMETHING.NEW", "boom")
        self.assertIn("SOMETHING.NEW", text)
        self.assertIn("boom", text)


class TestEventLog(Base):
    def test_a_note_lands_in_the_jsonl_with_context(self):
        from app.runtime import logger

        text = logger.note("error", "PLUGIN.RUN.FAILED", stage="understand",
                           detail="timetable: boom", plugin="timetable",
                           msg_id="qq:1")
        self.assertIn("怎么办", text)          # 返回的是人话

        files = list((self.home / "data" / "logs").glob("events-*.jsonl"))
        self.assertEqual(len(files), 1)
        row = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(row["code"], "PLUGIN.RUN.FAILED")
        self.assertEqual(row["stage"], "understand")
        self.assertEqual(row["plugin"], "timetable")
        self.assertEqual(row["msg_id"], "qq:1")

    def test_the_traceback_is_kept(self):
        from app.runtime import logger

        try:
            raise ValueError("坏掉了")
        except ValueError as exc:
            logger.note("error", "APP.CRASH.UNHANDLED", exc=exc)

        row = json.loads(
            (self.home / "data" / "logs").glob("events-*.jsonl").__next__()
            .read_text(encoding="utf-8").splitlines()[-1])
        self.assertIn("ValueError", row["traceback"])
        self.assertIn("坏掉了", row["traceback"])

    def test_secrets_are_masked_before_writing(self):
        from app.runtime import logger

        logger.note("warn", "PUSH.DELIVER.FAILED",
                    detail="url=https://qyapi.weixin.qq.com/x?key=SECRET123&a=1")
        text = (self.home / "data" / "logs").glob("events-*.jsonl").__next__() \
            .read_text(encoding="utf-8")
        self.assertNotIn("SECRET123", text)
        self.assertIn("***", text)

    def test_only_the_newest_days_are_kept(self):
        from app.runtime import logger

        logs = self.home / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        for day in range(1, 11):
            (logs / f"events-202610{day:02d}.jsonl").write_text("{}\n", encoding="utf-8")
        logger.purge_old()
        self.assertEqual(len(list(logs.glob("events-*.jsonl"))), logger.KEEP_DAYS)


class TestDiagnosticsBundle(Base):
    def test_the_zip_has_what_you_need_and_no_secrets(self):
        from app.runtime import diagnostics

        (self.home / "config.toml").write_text(
            'api_key = "sk-SECRET"\n[qq]\ntoken = "ZQK-SECRET"\n', encoding="utf-8")
        (self.home / "data" / "background.log").write_text(
            "已经连上\nws://127.0.0.1:18080/x?access_token=ZQK-SECRET\n", encoding="utf-8")
        (self.home / "data" / "logs").mkdir(parents=True)
        (self.home / "data" / "logs" / "events-20261017.jsonl").write_text(
            '{"code": "X", "detail": "key=SECRET"}\n', encoding="utf-8")

        path = diagnostics.build("反馈原文")
        self.assertTrue(path.is_file())
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            self.assertTrue({"说明.txt", "反馈.txt", "事件日志.jsonl",
                             "后台日志.txt", "配置-脱敏.toml", "系统.txt"} <= names)
            everything = "".join(zf.read(n).decode("utf-8", "replace") for n in names)
        self.assertNotIn("ZQK-SECRET", everything)
        self.assertNotIn("sk-SECRET", everything)
        self.assertIn("反馈原文", everything)


class TestReplay(Base):
    def test_replay_shows_what_the_rules_would_do_now(self):
        """拿库里那条原话重跑一遍 —— 不出现在界面上的判断过程，这里全看得见。"""
        import io
        from contextlib import redirect_stdout

        import run
        from app.contracts.models import SOURCE_QQ_GROUP, Message
        from app.pipeline.chain import Pipeline
        from app.pipeline.parse_enricher import RuleParseEnricher
        from app.store.repository import SqliteStore, open_database

        store = SqliteStore(open_database(self.home / "data" / "secretary.db"))
        try:
            store.save_message(Message(
                msg_id="qq:9", source_kind=SOURCE_QQ_GROUP, source_name="某班群",
                sender="班长", sent_at="2026-10-16 09:00:00",
                text="明天下午 3 点在 612 开会"))

            pipeline = Pipeline(draft_enrichers=[RuleParseEnricher()])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = run.replay_message(store, pipeline, "qq:9")
            out = buffer.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("解析结果：1 条", out)
            self.assertIn("开会", out)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.assertEqual(run.replay_message(store, pipeline, "qq:nope"), 1)
            self.assertIn("没有这条消息", buffer.getvalue())
        finally:
            store.close()          # 必须在 tearDown 删临时目录**之前**关掉
