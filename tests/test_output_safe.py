"""打印不能因为编码把整个动作带崩。

用户 2026-10-16 报的原文：
    sync_now 出错：UnicodeEncodeError: 'gbk' codec can't encode character
    '\\u2717' in position 2: illegal multibyte sequence

根因是两件事凑一起：
  ① run.py 里那句「  ✗ {来源} 拉取失败」—— ✗ 正好在第 2 个字符；
  ② pywebview 的 http.py 被 import 时发现 stdout 是 None（窗口模式就是这样），
     塞了 `open(os.devnull, "w")` 进去 —— 那是 GBK + errors="strict" 的流，
     把 run.py 加载时设好的 utf-8 顶掉了。
结果：课表拉取失败本来要报一句原因，报错那句话自己先崩，真正的原因反而丢了。
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run                                                    # noqa: E402


class SafeOutput(unittest.TestCase):
    def setUp(self):
        self.saved = sys.stdout

    def tearDown(self):
        sys.stdout = self.saved

    @staticmethod
    def _gbk_stream():
        """照抄 pywebview 那个流：GBK + strict。"""
        return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")

    def test_the_crash_is_real_without_the_fix(self):
        """先钉住"真的会崩" —— 免得以后有人把兜底删了还不知道为什么留着。"""
        sys.stdout = self._gbk_stream()
        with self.assertRaises(UnicodeEncodeError):
            print("  ✗ 课表 拉取失败：HTTPError: 400")

    def test_a_gbk_stream_no_longer_kills_the_print(self):
        sys.stdout = self._gbk_stream()
        run._make_output_safe()
        print("  ✗ 课表 拉取失败：HTTPError: 400")       # 不抛就算过
        sys.stdout.flush()

    def test_emoji_in_a_message_is_survived_too(self):
        """群里发的表情也会走到打印里（待办标题就是这么来的）。"""
        sys.stdout = self._gbk_stream()
        run._make_output_safe()
        print("  ✓ 入待办│ 🎉 明天下午三点开会")
        sys.stdout.flush()

    def test_open_ui_reapplies_it_after_importing_webview(self):
        """光在模块加载时兜一次不够 —— pywebview 是后面才把流换掉的。"""
        import inspect

        source = inspect.getsource(run.open_ui)
        self.assertIn("import webview", source)
        self.assertGreaterEqual(source.count("_make_output_safe()"), 1,
                                "import webview 之后没有重新兜底，GBK 流会回来")
