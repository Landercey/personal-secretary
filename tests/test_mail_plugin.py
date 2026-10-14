"""邮箱插件（邮箱 → 待办）。

它是**第二个"主动去拉"的插件**，所以要盯住三件容易出事的地方：

  1. **不标已读**：用 readonly 打开、不发 \\Seen —— 用户手机上没读过的那封，
     同步完还得是没读过。悄悄替人清红点是最招人烦的行为。
  2. **失败要说人话**：邮箱报"认证失败"的时候，十个人里九个填的是登录密码；
     这时必须直接说"要填授权码"，而不是把 IMAP 的英文错误原样甩出来。
  3. **有上限**：一次最多几封、多大的邮件跳过、正文切多长 —— 少了任何一条，
     一封 30 MB 的邮件或两百封历史邮件就能把界面淹掉。

用假 IMAP 服务器测，不连真邮箱。
"""

from __future__ import annotations

import email.message
import importlib.util
import imaplib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.runtime.config import Config as _Config                  # noqa: E402
from app.runtime import plugins as plugin_system                  # noqa: E402


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "plugin_mail", ROOT / "plugins" / "mail.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MAIL = load_plugin()


def build_mail(subject: str, body: str, *, html: bool = False,
               sender: str = "教务处 <office@example.edu>") -> bytes:
    message = email.message.EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Date"] = "Wed, 14 Oct 2026 09:30:00 +0800"
    message.set_content(body, subtype="html" if html else "plain")
    return message.as_bytes()


class FakeImap:
    """够用的假 IMAP：记录被调用过什么，按脚本回话。

    `CONNECTS` 只记"有没有人来连" —— 用来验"没开/没配好时一根网线都不该碰"。
    """

    CONNECTS: list[tuple] = []
    PENDING = None

    def __init__(self, mails: dict[str, bytes], *, sizes=None, fail_login=False):
        self.mails = mails
        self.sizes = sizes or {}
        self.fail_login = fail_login
        self.calls: list[tuple] = []

    @classmethod
    def connect(cls, host, port, timeout=None):
        """替身：imaplib.IMAP4_SSL(host, port, timeout=…)"""
        cls.CONNECTS.append((host, port))
        server = cls.PENDING or cls({})
        cls.PENDING = None
        return server

    def login(self, user, password):
        self.calls.append(("login", user))
        if self.fail_login:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"logged in"]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [str(len(self.mails)).encode()]

    def uid(self, command, *args):
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            order = list(self.mails)
            return "OK", [" ".join(order).encode()]
        if command == "FETCH":
            uid, what = args[0], args[1]
            if "SIZE" in what:
                size = self.sizes.get(uid, len(self.mails.get(uid, b"")))
                return "OK", [f"{uid} (RFC822.SIZE {size})".encode()]
            raw = self.mails.get(uid, b"")
            return "OK", [(f"{uid} (BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        return "NO", [b""]

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", [b""]

    def searches(self) -> list[tuple]:
        return [call for call in self.calls
                if len(call) > 1 and call[0] == "uid" and call[1] == "SEARCH"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("SECRETARY_HOME")
        os.environ["SECRETARY_HOME"] = self.tmp.name
        self.config = _Config.load(Path(self.tmp.name) / "config.toml")
        # 插件模块里的 config 是导入时绑定的全局对象，这里换成测试这份
        self._old_config = MAIL.config
        MAIL.config = self.config
        self._old_imap = MAIL.imaplib.IMAP4_SSL
        self._old_connects = FakeImap.CONNECTS
        self._old_pending = FakeImap.PENDING
        FakeImap.CONNECTS = []
        FakeImap.PENDING = None
        MAIL.imaplib.IMAP4_SSL = FakeImap.connect

    def tearDown(self):
        MAIL.config = self._old_config
        MAIL.imaplib.IMAP4_SSL = self._old_imap
        FakeImap.CONNECTS = self._old_connects
        FakeImap.PENDING = self._old_pending
        if self._old_home is None:
            os.environ.pop("SECRETARY_HOME", None)
        else:
            os.environ["SECRETARY_HOME"] = self._old_home
        self.tmp.cleanup()

    def configure(self, **over):
        values = {"enabled": True, "host": "imap.example.com", "port": 993,
                  "user": "me@example.com", "password": "token", "folder": "INBOX",
                  "keep_days": 7, "max_per_sync": 10}
        values.update(over)
        for key, value in values.items():
            self.config.set("mail", key, value)

    def fake(self, mails, **kwargs) -> FakeImap:
        server = FakeImap(mails, **kwargs)
        FakeImap.PENDING = server          # 下一次 connect 返回它
        return server


class TestParsing(unittest.TestCase):
    def test_strip_html_keeps_the_words(self):
        html_body = ("<html><head><style>p{color:red}</style></head><body>"
                     "<p>请于 10 月 20 日前提交材料</p><br><p>交到教务处</p>"
                     "<script>alert(1)</script></body></html>")
        text = MAIL._strip_html(html_body)
        self.assertIn("请于 10 月 20 日前提交材料", text)
        self.assertIn("交到教务处", text)
        self.assertNotIn("color:red", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("<", text)

    def test_entities_are_unescaped(self):
        self.assertIn("A& B", MAIL._strip_html("<p>A&amp; B</p>"))

    def test_html_only_mail_becomes_text(self):
        raw = build_mail("缴费通知", "<div>请于本周五前缴清学费</div>", html=True)
        self.assertIn("请于本周五前缴清学费", MAIL._plain_text(raw))

    def test_encoded_subject_is_decoded(self):
        raw = build_mail("会议通知", "正文")
        message = MAIL._to_message("1", raw)
        self.assertIn("会议通知", message.text)
        self.assertEqual(message.msg_id, "mail:1")
        self.assertEqual(message.source_kind, "mail")
        self.assertEqual(message.source_name, "邮箱")      # 不带邮箱地址
        self.assertIn("office@example.edu", message.sender)


class TestFetch(Base):
    def test_pulls_new_mail_and_remembers_the_position(self):
        self.configure()
        server = self.fake({"1": build_mail("缴费通知", "请于本周五前缴清学费"),
                            "2": build_mail("面试", "周三下午两点来面试")})
        messages = MAIL.MailSource().fetch()

        self.assertEqual([m.msg_id for m in messages], ["mail:1", "mail:2"])
        self.assertIn("请于本周五前缴清学费", messages[0].text)
        # 记到哪儿了：下次同步只拉新的，不把同一批重跑一遍（开 AI 时尤其费钱）
        marker = Path(self.tmp.name) / "data" / MAIL.MARKER_NAME
        self.assertEqual(marker.read_text(encoding="utf-8"), "2")
        self.assertIn(("logout",), server.calls)

    def test_mail_is_not_marked_as_read(self):
        self.configure()
        server = self.fake({"1": build_mail("通知", "正文")})
        MAIL.MailSource().fetch()

        select = [call for call in server.calls if call[0] == "select"]
        self.assertEqual(select, [("select", "INBOX", True)])     # readonly=True
        self.assertFalse([call for call in server.calls
                          if "STORE" in str(call).upper()
                          or "+FLAGS" in str(call).upper()],
                         "不该动用户的已读状态")

    def test_first_sync_only_looks_at_recent_days(self):
        self.configure(keep_days=3)
        server = self.fake({"1": build_mail("通知", "正文")})
        MAIL.MailSource().fetch()
        criteria = " ".join(str(part) for part in server.searches()[0])
        self.assertIn("SINCE", criteria)        # 第一次没有位置记录，就用"最近 N 天"

    def test_uses_the_saved_position_next_time(self):
        self.configure()
        data = Path(self.tmp.name) / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / MAIL.MARKER_NAME).write_text("7", encoding="utf-8")
        server = self.fake({"8": build_mail("新邮件", "正文")})
        MAIL.MailSource().fetch()
        criteria = " ".join(str(part) for part in server.searches()[0])
        self.assertIn("UID 8:*", criteria)      # 只拉上次之后的新邮件

    def test_big_mail_is_skipped(self):
        self.configure()
        self.fake({"1": build_mail("大邮件", "正文")},
                  sizes={"1": MAIL.MAX_MESSAGE_BYTES + 1})
        self.assertEqual(MAIL.MailSource().fetch(), [])

    def test_too_many_mails_says_so_and_sends_a_note(self):
        self.configure(max_per_sync=1, keep_days=30)
        source = MAIL.MailSource()
        self.fake({"1": build_mail("第一封", "正文"),
                   "2": build_mail("第二封", "正文")})
        messages = source.fetch()
        self.assertEqual([m.msg_id for m in messages], ["mail:2"])   # 只取最新的
        self.assertTrue(any("max_per_sync" in note for note in source.notes))


class TestFailures(Base):
    def test_off_means_no_work_at_all(self):
        self.configure(enabled=False)
        self.fake({})
        self.assertEqual(MAIL.MailSource().fetch(), [])
        self.assertEqual(FakeImap.CONNECTS, [], "没开就不该去连邮箱")

    def test_missing_settings_say_what_is_missing(self):
        self.configure(host="", user="", password="")
        source = MAIL.MailSource()
        self.assertEqual(source.fetch(), [])
        self.assertTrue(any("授权码" in note for note in source.notes))
        self.assertEqual(FakeImap.CONNECTS, [], "缺东西就不该去连")

    def test_wrong_password_note_points_at_the_token(self):
        self.configure()
        self.fake({}, fail_login=True)
        source = MAIL.MailSource()
        self.assertEqual(source.fetch(), [])
        joined = " ".join(source.notes)
        self.assertIn("授权码", joined)          # 最常见的那件事，直接说
        self.assertIn("登录密码", joined)

    def test_connection_error_is_reported_not_raised(self):
        self.configure()

        def boom(*args, **kwargs):
            raise OSError("网络不可达")

        MAIL.imaplib.IMAP4_SSL = boom
        source = MAIL.MailSource()
        self.assertEqual(source.fetch(), [])     # 不能把异常抛给 --sync
        self.assertTrue(source.notes)


class TestPluginSlot(unittest.TestCase):
    def test_mail_registers_into_the_source_slot(self):
        """真去加载 plugins/ 目录一次，确认这个插件在真环境里也装得上。"""
        registry = plugin_system.Registry()
        statuses = plugin_system.load(ROOT, registry)

        by_name = {status.name: status for status in statuses}
        self.assertIn("mail", by_name)
        self.assertTrue(by_name["mail"].ok, by_name["mail"].error)
        self.assertIn("mail", [source.kind for source in registry.sources])


if __name__ == "__main__":
    unittest.main()
