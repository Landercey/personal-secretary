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
        "parse": ["filter_strength", "confidence_min", "merge_window_seconds", "due_default_hour"],
        "title": ["max_len", "min_meaningful_chars"],
        "store": ["attachment_retention_days", "keep_dropped_days"],
        "push": ["default_times", "quote_max_chars", "fallback_text_chars"],
        "ai": ["mode", "cloud_priority", "token_budget_per_day"],
        "resource": ["mem_critical_percent", "mem_resume_percent"],
        "risk": ["require_secondary_account"],
        "wecom_bot": ["enabled", "webhook", "max_per_minute", "timeout"],
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
        for key in ("webhook",):
            self.assertFalse(DEFAULTS["wecom_bot"][key], f"{key} 默认必须是空的")
        for key in ("cloud_api_key", "cloud_base_url", "cloud_model", "local_model"):
            self.assertFalse(DEFAULTS["ai"][key], f"{key} 默认必须是空的")
        self.assertFalse(DEFAULTS["wecom_bot"]["enabled"])


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


if __name__ == "__main__":
    unittest.main()
