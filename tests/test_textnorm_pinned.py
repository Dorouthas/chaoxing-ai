# -*- coding: utf-8 -*-
"""两套归一化**不能合并** —— 这条测试就是用来拦住「顺手统一一下」的重构。

背景：
- `api.base.normalize_text` 给刷课/作业用，历史行为会剥掉开头的拉丁字母
  （连分隔符都可以没有）：`TCP/IP协议` → `cpip协议`、`yes` → `es`。
  裸字母答案（`B`）因此会被归一成空串，靠 `match_option_exact` 开头的
  字母分支单独兜住。
- `api.matcher` 给考试用，不做这种剥离，靠 `strip_prefix` 显式处理 `A.` 前缀。

两边都是承重的：把 base 改成 matcher 的规则，会动到刷课的匹配行为；
把 matcher 改成 base 的规则，英文答案会被吃掉首字母。
所以这里把两边的差异钉死，谁改谁先看见这条测试。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.base import match_option_exact, normalize_text  # noqa: E402
from api.matcher import normalize, strip_prefix  # noqa: E402

SINGLE = ["A 人民群众", "B 人民军队", "C 中国共产党", "D 民兵"]


class BaseNormalizePinnedTest(unittest.TestCase):
    def test_strips_leading_latin_letter(self):
        self.assertEqual(normalize_text("TCP/IP协议"), "cpip协议")
        self.assertEqual(normalize_text("yes"), "es")
        self.assertEqual(normalize_text("A. 人民军队"), "人民军队")

    def test_bare_letter_becomes_empty(self):
        # 这是已知缺陷，由 match_option_exact 的字母分支兜住；改动前先来这里看
        self.assertEqual(normalize_text("B"), "")

    def test_bare_letter_still_matches(self):
        self.assertEqual(match_option_exact("B", SINGLE), "B")
        self.assertEqual(match_option_exact("b", SINGLE), "B")

    def test_normal_punctuation(self):
        self.assertEqual(normalize_text("人民群众，是历史的创造者。"),
                         "人民群众是历史的创造者")


class MatcherNormalizePinnedTest(unittest.TestCase):
    def test_keeps_leading_letter(self):
        self.assertEqual(strip_prefix("TCP/IP协议"), "TCP/IP协议")
        # matcher 刻意不去掉 `/`：公式和单位里它是有效字符
        self.assertEqual(normalize("TCP/IP协议"), "tcp/ip协议")

    def test_fullwidth(self):
        self.assertEqual(normalize("Ａ．人民群众"), normalize("A.人民群众"))


if __name__ == "__main__":
    unittest.main()