# -*- coding: utf-8 -*-
"""考场作答的收尾逻辑（离线，不碰真浏览器）。

这里守两条规矩：

1. **程序永远不交卷**。答完只置 `ready` 并提示，交卷由使用者手动完成。
2. **多选回读要按选项集合比对**。只判「字段非空」会把「只点中一个」当成答完，
   而少选在平台上是 0 分。

`browser_exam.py` 里绝大多数函数都要真实 WebView2 才能跑，这两条是纯逻辑，
用假窗口就能测，值得钉死。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.browser_exam as be  # noqa: E402

PACE = {"gap": 0, "accuracy": 100, "conf_gate": 0.78}


class PickedOkTest(unittest.TestCase):
    def test_single_ok(self):
        self.assertTrue(be._picked_ok("A", ["A"], "single"))

    def test_multi_all_selected(self):
        self.assertTrue(be._picked_ok("A,B", ["A", "B"], "multiple"))
        self.assertTrue(be._picked_ok("AB", ["A", "B"], "multiple"))
        self.assertTrue(be._picked_ok("A、B", ["A", "B"], "multiple"))

    def test_multi_missing_one_is_failure(self):
        """只点中 A、期望 A+B：必须判失败，不能当成答完。"""
        self.assertFalse(be._picked_ok("A", ["A", "B"], "multiple"))

    def test_multi_extra_selection_is_failure(self):
        self.assertFalse(be._picked_ok("A,B,C", ["A", "B"], "multiple"))

    def test_judgement_non_empty(self):
        self.assertTrue(be._picked_ok("true", ["true"], "judgement"))
        self.assertFalse(be._picked_ok("", ["true"], "judgement"))

    def test_unknown_format_falls_back(self):
        """平台如果把答案写成认不出的格式，退回「非空即成功」，不误杀。"""
        self.assertTrue(be._picked_ok("人民军队", ["A"], "single"))

    def test_empty_is_failure(self):
        self.assertFalse(be._picked_ok("", ["A"], "single"))
        self.assertFalse(be._picked_ok(None, ["A"], "single"))


class NoAutoSubmitTest(unittest.TestCase):
    def test_submit_paper_is_gone(self):
        """自动交卷已经整段删除，这里防止有人又加回来。"""
        self.assertFalse(hasattr(be, "submit_paper"))
        self.assertFalse(hasattr(be, "SUBMIT_FIND"))


class RunPassWrapUpTest(unittest.TestCase):
    def _run(self, card):
        logs = []
        with mock.patch.object(be, "exam_pace", lambda: PACE), \
                mock.patch.object(be, "read_card", lambda win: card), \
                mock.patch.object(be, "wait_ready", lambda win, timeout=8: False), \
                mock.patch.object(be, "install_guard", lambda win: None), \
                mock.patch.object(be, "drain_dialogs", lambda win, log: None), \
                mock.patch.object(be, "goto_first", lambda win, log=None: False):
            stats = be.run_pass(None, log=logs.append, rounds=1)
        return stats, logs

    def test_all_answered_marks_ready_without_submitting(self):
        stats, logs = self._run({"ok": True, "total": 2, "done": 2, "undone": []})
        self.assertTrue(stats["ready"])
        self.assertFalse(stats["submitted"])
        self.assertEqual(stats["left"], 0)
        self.assertTrue(any("手动交卷" in line for line in logs))

    def test_unfinished_is_not_ready(self):
        stats, logs = self._run({"ok": True, "total": 2, "done": 1, "undone": ["2"]})
        self.assertFalse(stats["ready"])
        self.assertFalse(stats["submitted"])
        self.assertEqual(stats["left"], 1)
        self.assertTrue(any("补齐后再交卷" in line for line in logs))

    def test_unreadable_card_is_reported(self):
        stats, logs = self._run({"ok": False})
        self.assertFalse(stats["ready"])
        self.assertFalse(stats["submitted"])


if __name__ == "__main__":
    unittest.main()