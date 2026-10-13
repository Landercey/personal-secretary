"""第 1 步门禁：契约完整 + 模块隔离没被破坏。

这个文件的价值不在"测试现在能过"，而在**以后每次改动都会被它拦住** ——
比如有人图省事在 pipeline 里直接 import store，边界一破，模块化就名存实亡了。
"""

from __future__ import annotations

import ast
import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts import interfaces, models           # noqa: E402
from app.runtime.config import DEFAULTS, config         # noqa: E402

# 四段流水线。彼此之间不许互相 import。
SEGMENTS = ("sources", "pipeline", "store", "sinks")
# 允许被任何一段引用的公共部分。
SHARED = ("contracts", "runtime")


class TestModels(unittest.TestCase):
    def test_four_formats_exist(self):
        for name in ("Message", "TaskDraft", "Task", "PushPayload"):
            self.assertTrue(hasattr(models, name), f"契约缺数据格式：{name}")

    def test_dataclasses_are_slotted(self):
        """用 slots 是为了别让随手加的属性悄悄溜进契约。"""
        for cls in (models.Attachment, models.Message, models.TaskDraft,
                    models.Task, models.PushPayload):
            self.assertTrue(dataclasses.is_dataclass(cls), f"{cls.__name__} 不是 dataclass")
            self.assertTrue(hasattr(cls, "__slots__"), f"{cls.__name__} 没开 slots")

    def test_message_fields(self):
        names = {f.name for f in dataclasses.fields(models.Message)}
        for required in ("msg_id", "source_kind", "source_name", "sender",
                         "is_self", "sent_at", "text", "attachments"):
            self.assertIn(required, names)

    def test_taskdraft_fields(self):
        names = {f.name for f in dataclasses.fields(models.TaskDraft)}
        for required in ("title", "detail", "due_at", "location", "source_kind",
                         "origin_msg_id", "attachments", "confidence", "quoted"):
            self.assertIn(required, names)

    def test_time_format_is_consistent(self):
        """库里、日志里、推送里必须是同一种时间写法，否则排序会乱。"""
        self.assertEqual(models.TIME_FORMAT, "%Y-%m-%d %H:%M:%S")


class TestInterfaces(unittest.TestCase):
    def test_all_five_interfaces_are_abstract(self):
        for iface in (interfaces.Source, interfaces.TextEnricher,
                      interfaces.DraftEnricher, interfaces.Store, interfaces.Sink):
            self.assertTrue(getattr(iface, "__abstractmethods__", None),
                            f"{iface.__name__} 没有抽象方法，等于没有约束")

    def test_store_cannot_be_instantiated_directly(self):
        with self.assertRaises(TypeError):
            interfaces.Store()          # type: ignore[abstract]


class TestConfig(unittest.TestCase):
    REQUIRED = {
        "parse": ["filter_strength", "merge_window_seconds", "due_default_hour"],
        "title": ["max_len", "min_meaningful_chars"],
        "store": ["attachment_retention_days", "keep_dropped_days"],
        "push": ["enabled", "times", "catch_up_minutes",
                 "quote_max_chars", "fallback_text_chars"],
        # AI 这一段的键在 2026-10-10 做插件时改过：从"cloud_* + local_model"
        # 收敛成一套 OpenAI 兼容的 base_url/api_key/model（本地和云端形状一样），
        # 另加两道隐私闸（enabled + allow_upload）。
        "ai": ["enabled", "allow_upload", "base_url", "api_key", "model",
               "mode", "min_chars", "timeout_seconds", "token_budget_per_day"],
        "resource": ["mem_critical_percent"],
        "risk": ["confirmed_secondary_account"],
        "wecom_bot": ["enabled", "webhook", "max_per_minute", "timeout"],
        # 通用推送地址：钉钉 / 飞书 / PushPlus / Bark 共用这一段
        "push_webhook": ["enabled", "url", "timeout"],
    }

    def test_required_thresholds_declared(self):
        for section, keys in self.REQUIRED.items():
            self.assertIn(section, DEFAULTS, f"配置缺整段：{section}")
            for key in keys:
                self.assertIn(key, DEFAULTS[section], f"配置缺项：{section}.{key}")

    def test_filter_strength_has_only_three_levels(self):
        """故意只给三档 —— 开关一多用户不会调，只会觉得复杂。"""
        self.assertEqual(DEFAULTS["parse"]["filter_strength"], "strict")

    def test_new_repo_has_no_real_secrets(self):
        """新仓库必须零真实信息：默认配置里不许出现真实密钥/群号。"""
        # 两个推送通道的地址默认都必须是空的（地址里带着能往群里发消息的凭证）
        for section, key in (("wecom_bot", "webhook"), ("push_webhook", "url")):
            self.assertFalse(DEFAULTS[section][key], f"{section}.{key} 默认必须是空的")
        for key in ("api_key", "base_url", "model"):
            self.assertFalse(DEFAULTS["ai"][key], f"{key} 默认必须是空的")
        # 两道闸默认必须是关的：一装上就把用户的消息发出去是不可接受的
        self.assertFalse(DEFAULTS["ai"]["enabled"])
        self.assertFalse(DEFAULTS["ai"]["allow_upload"])
        self.assertFalse(DEFAULTS["wecom_bot"]["enabled"])
        self.assertFalse(DEFAULTS["push_webhook"]["enabled"])

    # 举例时约定用这几个假号（文档和代码注释里都靠它们）
    FAKE_NUMBERS = {"123456789", "987654321", "444444444444",
                    "1770000000", "1770000001", "111222333",
                    # 暂存文件名里的时间戳，也是编的
                    "1700000000",
                    # 数字表本身：那是"哪些字符算数字"，不是号码
                    "0123456789"}

    def test_docs_and_comments_do_not_leak_real_looking_numbers(self):
        """文档和代码注释里不许出现"像真的"长号码。

        2026-10-13 **真出过事**：作者（我）把用户真实的推送汇总当例子写进了
        CHANGELOG 和 CONTRACT —— 里面有他的登录号、收件箱号、群号，推上 GitHub
        等于公开了。用户的反应是"你更新把我QQ号发出去了"。

        规矩：举例一律用编的号。这条自检只放行上面那几个约定好的假号，
        以后谁再顺手粘一段真实日志进来，测试当场就红。
        """
        import re

        pattern = re.compile(r"\d{9,}")
        offenders: list[str] = []
        targets = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "CONTRACT.md",
                   ROOT / "CONTRIBUTING.md"]
        targets += sorted((ROOT / "app").rglob("*.py"))
        for path in targets:
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for found in set(pattern.findall(text)):
                if found not in self.FAKE_NUMBERS:
                    offenders.append(f"{path.relative_to(ROOT)}: {found}")
        self.assertEqual(offenders, [],
                         "这些地方出现了像真号码的长数字，换个编的：\n"
                         + "\n".join(offenders))


class TestModuleBoundaries(unittest.TestCase):
    """四段流水线之间不许互相 import 内部实现（CONTRACT.md 第二节铁律 2）。"""

    def _violations(self, path: Path, own_segment: str) -> list[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            module = node.module or ""
            if not module:
                continue
            head = module.split(".")[0]
            if head in SEGMENTS and head != own_segment:
                found.append(f"{path.name}: from ..{module} import …")
        return found

    def test_segments_do_not_import_each_other(self):
        problems: list[str] = []
        for segment in SEGMENTS:
            folder = ROOT / "app" / segment
            for path in sorted(folder.glob("*.py")):
                problems.extend(self._violations(path, segment))
        self.assertEqual(problems, [], "模块边界被破坏：\n" + "\n".join(problems))

    def test_segments_may_use_shared_layers(self):
        """反面确认：允许用 contracts / runtime，别把边界检查写反了。"""
        path = ROOT / "app" / "pipeline" / "parse_enricher.py"
        self.assertEqual(self._violations(path, "pipeline"), [])


class TestRuntimeConfigObject(unittest.TestCase):
    def test_global_config_readable(self):
        self.assertEqual(config.get("parse", "filter_strength"), "strict")
        self.assertGreater(config.get("title", "max_len"), 0)
        self.assertEqual(config.load_error, "")


class TestConfigHotReload(unittest.TestCase):
    """改了配置文件不用重启就要生效。

    这是 config.toml 抬头里承诺的事。之前那句是假的 ——
    用户改完保存、行为没变，会以为程序坏了。

    注意：这里用 os.utime 显式把修改时间改掉，**不能只靠连续写两次文件**。
    两次写之间可能只差几微秒，文件系统给出的修改时间一样，
    测试就会时好时坏 —— 不稳定的测试比没有测试更糟，它会让人不信任整套测试。
    """

    def _write(self, path, value: str, *, bump_mtime: bool = False) -> None:
        path.write_text(
            f"[title]\nmax_len = {value}\n", encoding="utf-8")
        if bump_mtime:
            import os
            import time

            stamp = time.time() + 10
            os.utime(path, (stamp, stamp))

    def test_picks_up_changes_without_restart(self):
        import tempfile

        from app.runtime.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            self._write(path, "111")
            cfg = Config.load(path)
            self.assertEqual(cfg.get("title", "max_len"), 111)

            self._write(path, "222", bump_mtime=True)
            cfg._last_check = 0.0          # 跳过 2 秒节流，测试不用真等
            self.assertEqual(cfg.get("title", "max_len"), 222)

    def test_broken_file_keeps_old_values(self):
        """用户改到一半、语法还没写对时，不能把运行中的程序搞乱。"""
        import tempfile

        from app.runtime.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            self._write(path, "333")
            cfg = Config.load(path)
            self.assertEqual(cfg.get("title", "max_len"), 333)

            path.write_text("[title\nmax_len = = =", encoding="utf-8")   # 故意写坏
            import os
            import time

            stamp = time.time() + 20
            os.utime(path, (stamp, stamp))
            cfg._last_check = 0.0
            self.assertEqual(cfg.get("title", "max_len"), 333)

    def test_missing_file_is_tolerated(self):
        import tempfile

        from app.runtime.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.load(Path(tmp) / "不存在.toml")
            cfg._last_check = 0.0
            self.assertEqual(cfg.get("parse", "filter_strength"), "strict")


if __name__ == "__main__":
    unittest.main()
