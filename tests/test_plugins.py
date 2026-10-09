"""第 10 步门禁：插件系统。

门禁只有一句话（CONTRACT.md 第九节）：**坏插件不影响启动**。
所以这里的重点不是"好插件能用"（那是最容易的部分），
而是把插件能坏的每一种方式都造出来，确认程序都还站得住：

  1. 整个文件夹不存在
  2. 语法错，import 就炸
  3. register() 跑到一半抛异常（半个插件留在注册表里）
  4. 注册进来的东西根本不是零件（会让管线排序在启动时炸）

第 3、4 条必须**回滚**：不回滚的话，界面上写着"加载失败"，
可它有半个零件一直混在管线里跑，这种鬼状态最难查。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.contracts.models import Message, TaskDraft               # noqa: E402
from app.pipeline.chain import Pipeline                          # noqa: E402
from app.runtime import plugins                                  # noqa: E402

# 好插件：真去 import 项目的契约层，证明插件拿得到接口定义
GOOD = '''
from app.contracts.interfaces import DraftEnricher

NAME = "demo"
TITLE = "演示插件"

class DemoTagger(DraftEnricher):
    name = "demo-tagger"
    order = 90

    def process(self, message, drafts):
        for draft in drafts:
            draft.detail = (draft.detail or "") + " [demo]"
        return drafts

def register(registry):
    registry.add_draft(DemoTagger())
'''

BAD_SYNTAX = "def register(registry)\n    pass\n"      # 冒号都没写

BAD_HALFWAY = '''
from app.contracts.interfaces import DraftEnricher

class Half(DraftEnricher):
    def process(self, message, drafts):
        return drafts

def register(registry):
    registry.add_draft(Half())
    raise RuntimeError("注册到一半炸了")
'''

BAD_TYPE = '''
def register(registry):
    registry.add_draft("我不是零件，是字符串")
'''


def make_message(text: str = "明天下午三点交作业") -> Message:
    return Message(msg_id="t:1", source_kind="manual", source_name="手动录入",
                   sender="", text=text)


class TestDiscover(unittest.TestCase):
    def test_missing_folder_is_not_an_error(self):
        """用户可能一个插件都没装，plugins/ 文件夹压根不存在。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(plugins.discover(Path(tmp)), [])
            self.assertEqual(plugins.load(Path(tmp), plugins.Registry()), [])

    def test_underscore_files_are_skipped(self):
        """`_` 开头是留着自己放草稿的，不许被当成插件加载。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / plugins.PLUGIN_DIRNAME
            folder.mkdir()
            (folder / "_draft.py").write_text("raise RuntimeError('不该被加载')",
                                              encoding="utf-8")
            (folder / "README.md").write_text("说明文件，不是插件", encoding="utf-8")
            self.assertEqual(plugins.discover(Path(tmp)), [])


class TestLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.folder = self.root / plugins.PLUGIN_DIRNAME
        self.folder.mkdir()
        self.registry = plugins.Registry()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, code: str) -> None:
        (self.folder / name).write_text(code, encoding="utf-8")

    # ------------------------------------------------------------------ 好插件
    def test_good_plugin_loads_and_reaches_the_pipeline(self):
        self.write("demo.py", GOOD)
        statuses = plugins.load(self.root, self.registry)

        self.assertEqual([s.ok for s in statuses], [True])
        self.assertEqual(statuses[0].title, "演示插件")
        self.assertEqual(len(self.registry.draft_enrichers), 1)

        # 注册进来之后，必须真的会在管线上跑到 —— 不然"加载成功"没意义
        pipeline = Pipeline(draft_enrichers=self.registry.draft_enrichers)
        draft = TaskDraft(title="交作业", origin_msg_id="t:1")
        out = pipeline.extract(make_message(), [draft])
        self.assertEqual(out[0].detail, " [demo]")

    def test_broken_plugin_does_not_raise(self):
        """这是本步的门禁：坏插件不许把 load() 炸掉。"""
        self.write("broken.py", BAD_SYNTAX)
        statuses = plugins.load(self.root, self.registry)          # 不抛异常

        self.assertEqual(len(statuses), 1)
        self.assertFalse(statuses[0].ok)
        self.assertTrue(statuses[0].error)
        self.assertIn("broken", statuses[0].describe())

    def test_half_registered_plugin_is_rolled_back(self):
        """register() 跑到一半炸了：已经塞进去的零件必须被撤掉。"""
        self.write("halfway.py", BAD_HALFWAY)
        statuses = plugins.load(self.root, self.registry)

        self.assertFalse(statuses[0].ok)
        self.assertEqual(self.registry.draft_enrichers, [])       # 回滚干净

    def test_registering_a_non_part_is_rejected(self):
        """不是零件的东西会按 order 排序时把启动炸掉，必须在这里拦下。"""
        self.write("wrongtype.py", BAD_TYPE)
        statuses = plugins.load(self.root, self.registry)

        self.assertFalse(statuses[0].ok)
        self.assertIn("DraftEnricher", statuses[0].error)
        self.assertEqual(self.registry.draft_enrichers, [])

    def test_no_register_function(self):
        self.write("nofunc.py", "NAME = 'x'\n")
        statuses = plugins.load(self.root, self.registry)

        self.assertFalse(statuses[0].ok)
        self.assertIn("register", statuses[0].error)

    def test_broken_neighbour_does_not_take_down_the_good_one(self):
        """一个坏的和一个好的放一起：好的照常加载，坏的单独记一笔。"""
        self.write("broken.py", BAD_SYNTAX)
        self.write("demo.py", GOOD)
        statuses = plugins.load(self.root, self.registry)

        by_name = {s.name: s for s in statuses}
        self.assertFalse(by_name["broken"].ok)
        self.assertTrue(by_name["demo"].ok)
        self.assertEqual(len(self.registry.draft_enrichers), 1)

    def test_hyphenated_filename_is_loadable(self):
        """插件文件名里有横杠很常见，模块名得能转成合法的。"""
        self.write("my-plugin.py", GOOD)
        statuses = plugins.load(self.root, self.registry)

        self.assertTrue(statuses[0].ok, statuses[0].error)


if __name__ == "__main__":
    unittest.main()
