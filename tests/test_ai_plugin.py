"""AI 插件：把长消息交给一个 OpenAI 兼容接口，挑出待办。

测试里起一个**真的本地 HTTP 服务**假装模型接口（不是打桩 urlopen）——
这样连"地址怎么拼、请求体长什么样、返回值怎么解析"都一起验了。
真机上翻车的多半是这三处，而不是 Python 逻辑本身。

守着四条红线：
  · 默认关着 = 一个请求都不发（隐私）
  · 两道闸（enabled + allow_upload）缺一个都不发
  · 接口挂了 / 超时 / 返回垃圾 → 原样交回规则的结果，绝不抛异常
  · 时间解析不出来就不写时间（宁可没有，也不要一条错的）
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import Message, SOURCE_QQ_GROUP, TaskDraft   # noqa: E402
from app.pipeline.chain import Pipeline                                # noqa: E402
from app.runtime import plugins as plugin_system                       # noqa: E402
from app.runtime.config import Config                                  # noqa: E402

LONG_TEXT = (
    "各位同学，本学期《军事理论》课程考核安排如下：一、12 月 20 日前提交课程论文，"
    "字数不少于 3000 字，交到学习通作业区；二、12 月 27 日上午 9:00-11:00 随堂测验，"
    "地点由任课老师通知。请互相转告。"
) * 2


def load_plugin_module():
    """按路径加载 plugins/ai.py —— 用真文件，不用副本。"""
    spec = importlib.util.spec_from_file_location("ai_under_test",
                                                 ROOT / "plugins" / "ai.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AI = load_plugin_module()


class FakeModel(BaseHTTPRequestHandler):
    """假模型接口：回什么由 answers 队列控制，发过什么记在 requests 里。"""

    answers: list = []
    requests: list = []
    status = 200

    def do_POST(self):                                     # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        FakeModel.requests.append({"url": self.path, "body": body,
                                   "auth": self.headers.get("Authorization")})
        if FakeModel.status != 200:
            self.send_response(FakeModel.status)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        answer = FakeModel.answers.pop(0) if FakeModel.answers else "[]"
        data = json.dumps({"choices": [{"message": {"content": answer}}],
                           "usage": {"total_tokens": 120}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass                                   # 别把测试输出弄脏


class Base(unittest.TestCase):
    server: ThreadingHTTPServer | None = None
    port = 0

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()

    def setUp(self):
        FakeModel.answers = []
        FakeModel.requests = []
        FakeModel.status = 200
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name
        self.config = Config.load(Path(self.tmp.name) / "config.toml")
        # 插件模块用的是**全局** config（导入时就绑定了），这里换成测试的这份。
        self._old_config = AI.config
        AI.config = self.config
        self.message = Message(msg_id="qq_group:1", source_kind=SOURCE_QQ_GROUP,
                               source_name="军事理论群", sender="李老师",
                               text=LONG_TEXT)
        self.rules = [TaskDraft(title="字数不少于 3000")]

    def tearDown(self):
        AI.config = self._old_config
        if self._old_home is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old_home
        self.tmp.cleanup()

    def turn_on(self, **over):
        values = {"enabled": True, "allow_upload": True,
                  "base_url": f"http://127.0.0.1:{self.port}/v1",
                  "api_key": "test-key", "model": "qwen2.5:7b",
                  "min_chars": 50}
        values.update(over)
        for key, value in values.items():
            self.config.set("ai", key, value)


class TestPrivacyGates(Base):
    """没点头就一个请求都不发。这是这个插件最要紧的一条。"""

    def test_disabled_by_default(self):
        result = AI.AiEnricher().process(self.message, self.rules)

        self.assertEqual(result, self.rules)
        self.assertEqual(FakeModel.requests, [])

    def test_needs_allow_upload_on_top_of_enabled(self):
        self.turn_on(allow_upload=False)

        AI.AiEnricher().process(self.message, self.rules)

        self.assertEqual(FakeModel.requests, [], "只勾了总开关就把内容发出去了")

    def test_short_message_is_not_sent(self):
        self.turn_on()

        AI.AiEnricher().process(
            Message(msg_id="x", source_kind=SOURCE_QQ_GROUP, text="拿快递"),
            self.rules)

        self.assertEqual(FakeModel.requests, [], "短消息不该麻烦 AI")

    def test_token_budget_stops_more_calls(self):
        self.turn_on(token_budget_per_day=100)
        AI.add_usage(150)                                   # 今天已经用超了

        AI.AiEnricher().process(self.message, self.rules)

        self.assertEqual(FakeModel.requests, [], "预算用完了还在发")


class TestAnswerParsing(Base):
    def test_happy_path_becomes_drafts(self):
        self.turn_on()
        FakeModel.answers = [json.dumps([
            {"title": "交军事理论论文", "due": "2026-12-20 23:59", "location": "学习通"},
            {"title": "随堂测验", "due": "2026-12-27 09:00", "location": None},
        ], ensure_ascii=False)]

        made = AI.AiEnricher().process(self.message, self.rules)

        self.assertEqual([d.title for d in made], ["交军事理论论文", "随堂测验"])
        self.assertEqual(made[0].due_at, "2026-12-20 23:59:00")
        self.assertEqual(made[0].location, "学习通")
        self.assertIsNone(made[1].location)
        # 溯源必须带上，不然推送里看不出这条是哪来的
        self.assertEqual(made[0].source_name, "军事理论群")
        self.assertEqual(made[0].origin_msg_id, "qq_group:1")
        # 可信度 < 1 = 推送会标【AI】
        self.assertLess(made[0].confidence, 1.0)
        # 原文片段留着，用户能看出它为什么这么归纳
        self.assertTrue(made[0].quoted)

    def test_request_goes_to_the_right_url_and_carries_the_key(self):
        self.turn_on()
        FakeModel.answers = ["[]"]

        AI.AiEnricher().process(self.message, self.rules)

        sent = FakeModel.requests[0]
        self.assertEqual(sent["url"], "/v1/chat/completions")
        self.assertEqual(sent["auth"], "Bearer test-key")
        self.assertEqual(sent["body"]["model"], "qwen2.5:7b")
        self.assertIn("明确写了要做的事", sent["body"]["messages"][0]["content"])

    def test_full_endpoint_is_accepted_as_is(self):
        """用户直接把 .../chat/completions 整条贴进来也得认。"""
        self.turn_on(base_url=f"http://127.0.0.1:{self.port}/v1/chat/completions")

        self.assertEqual(
            AI._endpoint(),
            f"http://127.0.0.1:{self.port}/v1/chat/completions")

    def test_code_fence_and_chatter_are_tolerated(self):
        """模型爱在 JSON 外面包一层 ```json 或者先唠两句 —— 得能认。"""
        self.turn_on()
        FakeModel.answers = [
            '好的，这是结果：\n```json\n[{"title":"交论文","due":null,"location":null}]\n```\n']

        made = AI.AiEnricher().process(self.message, self.rules)

        self.assertEqual([d.title for d in made], ["交论文"])

    def test_empty_answer_keeps_the_rule_result(self):
        """模型说"没有要做的事"时**保留规则结果** —— 它漏了比多一条更糟。"""
        self.turn_on()
        FakeModel.answers = ["[]"]

        result = AI.AiEnricher().process(self.message, self.rules)

        self.assertEqual(result, self.rules)

    def test_garbage_due_becomes_no_due_at_all(self):
        """时间解析不出来就不写时间，绝不猜。"""
        self.turn_on()
        FakeModel.answers = [json.dumps(
            [{"title": "交论文", "due": "下周三之前吧", "location": None}],
            ensure_ascii=False)]

        made = AI.AiEnricher().process(self.message, self.rules)

        self.assertIsNone(made[0].due_at)

    def test_date_only_gets_end_of_day(self):
        self.assertEqual(AI._parse_due("2026-12-20"), "2026-12-20 23:59:00")
        self.assertEqual(AI._parse_due("2026/12/20 08:30"), "2026-12-20 08:30:00")
        self.assertIsNone(AI._parse_due(""))
        self.assertIsNone(AI._parse_due(None))


class TestDegradation(Base):
    def test_http_error_keeps_the_rule_result_and_says_why(self):
        self.turn_on()
        FakeModel.status = 500

        result = AI.AiEnricher().process(self.message, self.rules)

        self.assertEqual(result, self.rules, "接口挂了不该把待办弄丢")
        self.assertIn("ai_error", self.message.raw)
        status = json.loads(
            (Path(os.environ["SECRETARY_HOME"]) / "data" / "ai-status.json")
            .read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])
        self.assertIn("AI 用不了", status["text"])

    def test_connection_refused_is_survivable(self):
        """端口上什么都没跑（用户没开 Ollama 就是这么个情况）。"""
        self.turn_on(base_url="http://127.0.0.1:9/v1",
                     timeout_seconds=2)

        result = AI.AiEnricher().process(self.message, self.rules)

        self.assertEqual(result, self.rules)
        self.assertIn("ai_error", self.message.raw)

    def test_fallback_when_ai_is_down_and_rules_found_nothing(self):
        """契约里的保底：AI 用不了、规则又一条都没读出来 → **不能什么都不给**。

        一条长通知，规则没读懂、AI 又断线。以前的结果是"这条就这么没了"，
        用户根本不知道发生过。现在至少摆一条出来让他看一眼原文。
        """
        self.turn_on(base_url="http://127.0.0.1:9/v1", timeout_seconds=2)

        made = AI.AiEnricher().process(self.message, [])       # 规则 0 条

        self.assertEqual(len(made), 1)
        self.assertIn("AI 没连上", made[0].title)
        self.assertEqual(made[0].detail, " ".join(LONG_TEXT.split())[:300])
        self.assertEqual(made[0].confidence, 1.0, "这是原文照抄，不能标成 AI 整理的")
        self.assertEqual(made[0].origin_msg_id, "qq_group:1")

    def test_fallback_length_comes_from_config(self):
        self.turn_on(base_url="http://127.0.0.1:9/v1", timeout_seconds=2)
        self.config.set("push", "fallback_text_chars", 20)

        made = AI.AiEnricher().process(self.message, [])

        self.assertEqual(len(made[0].detail), 20)

    def test_usage_is_counted_so_the_budget_works(self):
        self.turn_on()
        FakeModel.answers = ["[]"]

        AI.AiEnricher().process(self.message, self.rules)

        self.assertEqual(AI.used_today(), 120)      # 假接口每次报 120

    def test_a_huge_broken_response_does_not_blow_up(self):
        """地址填错、指到别的东西上时，对面可能回几十 MB。

        上限是 1 MB：超了就读一半 → JSON 解析失败 → 走失败兜底。
        重点是**不崩、不把内存吃掉**，而不是"一定要读懂"。
        """
        self.turn_on()
        FakeModel.answers = ["x" * (3 * 1024 * 1024)]

        result = AI.AiEnricher().process(self.message, [])

        self.assertEqual(len(result), 1)                  # 保底那条，不是崩掉
        self.assertIn("AI 没连上", result[0].title)

    def test_response_bigger_than_the_cap_is_truncated(self):
        self.assertEqual(AI.MAX_RESPONSE_BYTES, 1024 * 1024)


class TestWiring(unittest.TestCase):
    """插件能不能被插件系统正常加载、注册到管线上。"""

    def test_loads_and_registers_a_draft_enricher(self):
        statuses = plugin_system.load(ROOT, plugin_system.Registry())
        found = {s.name: s for s in statuses}

        self.assertIn("ai", found)
        self.assertTrue(found["ai"].ok, found["ai"].error)
        self.assertEqual(found["ai"].title, "AI 增强")
        self.assertEqual(found["ai"].section, "ai")     # 界面上「去设置」要跳这里
        self.assertTrue(found["ai"].description)

    def test_enricher_sits_after_the_rule_parsers(self):
        """AI 必须排在规则解析后面（先有草稿，再让它加工）。"""
        from app.pipeline.parse_enricher import RuleParseEnricher

        pipeline = Pipeline(draft_enrichers=[AI.AiEnricher(), RuleParseEnricher()])

        self.assertEqual([e.name for e in pipeline.draft_enrichers][-1], "ai")


if __name__ == "__main__":
    unittest.main()
