# -*- coding: utf-8 -*-
"""钉住 3.9.1 修掉的答案错配。

每一条用例在修复前都是红的（已用脚本实测复现过）：
- 数值选项「2.5」被前缀正则削成「5」，与「1.5」撞车后按 DOM 顺序点错
- Unicode 负号「−2」匹配到无符号选项「2」
- 模型重复输出同一选项编号，多选被连点两次（第二次是取消勾选）
- 多选全部低分（模型没把握）也照点不误
- 复核轮缺席的选项按 0.85 衰减后被拉下 0.5 阈值，稳定丢一个正确项
"""

import unittest

from api.exam import ScoredAnswer, _merge_scored
from api.matcher import resolve, score_to_keys

NUMERIC = [
    {"key": "A", "text": "1.5", "shown": "A"},
    {"key": "B", "text": "2.5", "shown": "B"},
]

SIGNED = [
    {"key": "A", "text": "2", "shown": "A"},
    {"key": "B", "text": "-2", "shown": "B"},
]

PREFIX_PAIR = [
    {"key": "k1", "text": "计算机网络", "shown": "A"},
    {"key": "k2", "text": "计算机网络原理", "shown": "B"},
]


class DecimalPrefixTest(unittest.TestCase):
    def test_two_point_five_not_stripped_to_five(self):
        r = resolve(["2.5"], NUMERIC, "single")
        self.assertEqual(r.keys, ["B"])

    def test_ambiguous_numeric_returns_empty_not_first(self):
        # 「1.5 / 2.5」的答案文本被削成同一个「5」时，宁可空也不要瞎点
        r = resolve(["5"], NUMERIC, "single")
        self.assertFalse(r.keys)


class UnicodeMinusTest(unittest.TestCase):
    def test_math_minus_maps_to_signed_option(self):
        r = resolve(["\u22122"], SIGNED, "single")
        self.assertEqual(r.keys, ["B"])


class ScoreToKeysTest(unittest.TestCase):
    def test_duplicate_key_collapsed(self):
        r = score_to_keys([{"k": "A", "v": 0.9}, {"k": "A", "v": 0.8}], "multiple")
        self.assertEqual(r.keys, ["A"])

    def test_multi_all_low_confidence_returns_empty(self):
        r = score_to_keys([{"k": "A", "v": 0.42}, {"k": "B", "v": 0.31}], "multiple")
        self.assertFalse(r.keys)
        self.assertTrue(r.ambiguous)


class PrefixPairTest(unittest.TestCase):
    def test_both_exact_prefix_pair_kept(self):
        r = resolve(["计算机网络#计算机网络原理"], PREFIX_PAIR, "multiple")
        self.assertEqual(set(r.keys), {"k1", "k2"})


class MergeDecayTest(unittest.TestCase):
    def test_missing_option_decay_keeps_threshold(self):
        # 第一轮 B=0.55（已入选），复核轮没提 B：衰减后不能掉出选择集
        a = ScoredAnswer(items=[{"k": "A", "v": 0.8}, {"k": "B", "v": 0.55}],
                         confidence=0.7)
        b = ScoredAnswer(items=[{"k": "A", "v": 0.75}], confidence=0.6)
        merged = _merge_scored(a, b)
        got = {i["k"]: i["v"] for i in merged.items}
        self.assertGreaterEqual(got["B"], 0.5)
        self.assertTrue(merged.ambiguous)


if __name__ == "__main__":
    unittest.main()