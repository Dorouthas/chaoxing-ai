# -*- coding: utf-8 -*-
"""答案 → 选项匹配层的回归测试。

这些用例都是线上踩过的坑：
- 答「TCP」被选成「TCP/IP协议」（双向包含）
- 答「B」因为选项前缀被剥成空串（字母答案丢失）
- 答「不正确」被「正确」抢先命中（判断词表顺序）
- 多选「计算机网络」和「计算机网络原理」被同时选中（一字之差）
- -x 与 x 只差符号，归一化后并列
"""
import unittest

from api.matcher import (
    MatchResult,
    is_plain_letters,
    normalize,
    resolve,
    score_to_keys,
    split_answer,
    strip_prefix,
)

SINGLE = [
    {"key": "A", "text": "A. 人民群众", "shown": "A"},
    {"key": "B", "text": "B. 人民军队", "shown": "B"},
    {"key": "C", "text": "C. 中国共产党", "shown": "C"},
    {"key": "D", "text": "D. 民兵", "shown": "D"},
]

MULTI = [
    {"key": "k1", "text": "人民军队", "shown": "A"},
    {"key": "k2", "text": "中国共产党", "shown": "B"},
    {"key": "k3", "text": "民兵", "shown": "C"},
]

JUDGE = [
    {"key": "true", "text": "正确", "shown": "A"},
    {"key": "false", "text": "错误", "shown": "B"},
]


class NormalizeTest(unittest.TestCase):
    def test_fullwidth_and_punct(self):
        self.assertEqual(normalize("Ａ．人民群众，"), normalize("A.人民群众"))

    def test_variant_char(self):
        self.assertEqual(normalize("⻛险"), normalize("风险"))

    def test_strip_prefix(self):
        self.assertEqual(strip_prefix("A. 人民军队"), "人民军队")
        self.assertEqual(strip_prefix("（1）定义"), "定义")
        self.assertEqual(strip_prefix("① 甲"), "甲")
        self.assertEqual(strip_prefix("B、乙"), "乙")

    def test_plain_letters(self):
        self.assertTrue(is_plain_letters("B"))
        self.assertTrue(is_plain_letters("A、C"))
        self.assertTrue(is_plain_letters("答案：AC"))
        self.assertFalse(is_plain_letters("yes"))
        self.assertFalse(is_plain_letters("defense"))
        self.assertFalse(is_plain_letters("人民军队"))

    def test_split_answer(self):
        self.assertEqual(split_answer("甲#乙#丙"), ["甲", "乙", "丙"])
        self.assertEqual(split_answer("甲；乙"), ["甲", "乙"])
        self.assertEqual(split_answer("只有一段"), ["只有一段"])


class SingleTest(unittest.TestCase):
    def test_exact_with_prefix(self):
        r = resolve(["B. 人民军队"], SINGLE, "single")
        self.assertEqual(r.keys, ["B"])
        self.assertEqual(r.method, "exact")

    def test_bare_letter(self):
        r = resolve(["B"], SINGLE, "single")
        self.assertEqual(r.keys, ["B"])
        self.assertEqual(r.method, "letters")

    def test_letter_with_answer_prefix(self):
        r = resolve(["答案：C"], SINGLE, "single")
        self.assertEqual(r.keys, ["C"])

    def test_text_without_prefix(self):
        r = resolve(["民兵"], SINGLE, "single")
        self.assertEqual(r.keys, ["D"])

    def test_similar_text(self):
        r = resolve(["中国共产党领导"], SINGLE, "single")
        self.assertEqual(r.keys, ["C"])
        self.assertEqual(r.method, "similar")

    def test_multi_fragment_merged(self):
        opts = [{"key": "A", "text": "听党指挥能打胜仗作风优良", "shown": "A"},
                {"key": "B", "text": "别的说法", "shown": "B"}]
        r = resolve(["听党指挥#能打胜仗#作风优良"], opts, "single")
        self.assertEqual(r.keys, ["A"])
        self.assertEqual(r.method, "merged")

    def test_tcp_must_not_match_tcpip(self):
        """单向包含：答 TCP 不能选到 TCP/IP协议。"""
        opts = [{"key": "A", "text": "TCP/IP协议", "shown": "A"},
                {"key": "B", "text": "UDP", "shown": "B"},
                {"key": "C", "text": "HTTP", "shown": "C"}]
        self.assertEqual(resolve(["TCP"], opts, "single").keys, [])

    def test_tcp_ip_is_a_prefix_trap(self):
        """相似度 0.75 也不许命中：答案是选项的前缀时宁可判不中。"""
        opts = [{"key": "A", "text": "TCP/IP", "shown": "A"},
                {"key": "B", "text": "UDP", "shown": "B"}]
        self.assertEqual(resolve(["TCP"], opts, "single").keys, [])
        opts2 = [{"key": "A", "text": "TCP协议", "shown": "A"}]
        self.assertEqual(resolve(["TCP"], opts2, "single").keys, [])

    def test_sign_disambiguation(self):
        """-x 与 x 归一化后相同，必须靠符号筛出正确项。"""
        opts = [{"key": "A", "text": "x", "shown": "A"},
                {"key": "B", "text": "-x", "shown": "B"}]
        r = resolve(["-x"], opts, "single")
        self.assertEqual(r.keys, ["B"])

    def test_no_match(self):
        self.assertEqual(resolve(["完全不相关的内容"], SINGLE, "single").keys, [])

    def test_empty_inputs(self):
        self.assertEqual(resolve([], SINGLE, "single").keys, [])
        self.assertEqual(resolve(["B"], [], "single").keys, [])


class MultipleTest(unittest.TestCase):
    def test_two_answers_one_string(self):
        r = resolve(["人民军队、中国共产党"], MULTI, "multiple")
        self.assertEqual(sorted(r.keys), ["k1", "k2"])

    def test_containment_is_one_way(self):
        """选项包含答案不算命中，避免短答案被长选项吃掉。"""
        opts = [{"key": "A", "text": "TCP/IP协议", "shown": "A"},
                {"key": "B", "text": "网络协议", "shown": "B"}]
        self.assertEqual(resolve(["TCP"], opts, "multiple").keys, [])

    def test_similar_options_deduplicated(self):
        """一字之差的选项不能同时选中，且留下的必须是匹配度更高的那个。"""
        opts = [{"key": "A", "text": "计算机网络", "shown": "A"},
                {"key": "B", "text": "计算机网络原理", "shown": "B"}]
        r = resolve(["计算机网络技术"], opts, "multiple")
        self.assertEqual(r.keys, ["A"])

    def test_both_correct_similar_options_kept(self):
        """「社会主义民主」和「社会主义法治」都对，不能被相似度去重杀掉。"""
        opts = [{"key": "A", "text": "社会主义民主", "shown": "A"},
                {"key": "B", "text": "社会主义法治", "shown": "B"}]
        r = resolve(["社会主义民主、社会主义法治"], opts, "multiple")
        self.assertEqual(sorted(r.keys), ["A", "B"])

    def test_containment_picks_longest_option(self):
        """包含匹配命中多个时取最具体的，不能按 DOM 顺序随便挑。"""
        opts = [{"key": "A", "text": "人民", "shown": "A"},
                {"key": "B", "text": "人民军队", "shown": "B"}]
        r = resolve(["人民军队和民兵"], opts, "multiple")
        self.assertEqual(r.keys, ["B"])

    def test_plain_letters(self):
        r = resolve(["A,C"], MULTI, "multiple")
        self.assertEqual(r.keys, ["k1", "k3"])

    def test_no_match(self):
        self.assertEqual(resolve(["风马牛不相及"], MULTI, "multiple").keys, [])


class JudgementTest(unittest.TestCase):
    def test_false(self):
        self.assertEqual(resolve(["错误"], JUDGE, "judgement").keys, ["false"])

    def test_negated_word_not_stolen_by_correct(self):
        """「不正确」必须判错，不能被「正确」抢走。"""
        self.assertEqual(resolve(["不正确"], JUDGE, "judgement").keys, ["false"])

    def test_cross_mark(self):
        self.assertEqual(resolve(["×"], JUDGE, "judgement").keys, ["false"])

    def test_tick(self):
        self.assertEqual(resolve(["√"], JUDGE, "judgement").keys, ["true"])

    def test_english(self):
        self.assertEqual(resolve(["false"], JUDGE, "judgement").keys, ["false"])
        self.assertEqual(resolve(["T"], JUDGE, "judgement").keys, ["true"])


class ScoreTest(unittest.TestCase):
    def test_single_confident(self):
        r = score_to_keys([{"k": "A", "v": 0.9}, {"k": "B", "v": 0.3}], "single")
        self.assertEqual(r.keys, ["A"])
        self.assertFalse(r.ambiguous)

    def test_single_ambiguous(self):
        r = score_to_keys([{"k": "A", "v": 0.8}, {"k": "B", "v": 0.72}], "single")
        self.assertEqual(r.keys, [])
        self.assertTrue(r.ambiguous)

    def test_multiple_threshold(self):
        r = score_to_keys([{"k": "A", "v": 0.92}, {"k": "B", "v": 0.55},
                           {"k": "C", "v": 0.40}], "multiple")
        self.assertEqual(r.keys, ["A", "B"])
        self.assertFalse(r.ambiguous)

    def test_multiple_borderline_is_ambiguous(self):
        r = score_to_keys([{"k": "A", "v": 0.90}, {"k": "B", "v": 0.55},
                           {"k": "C", "v": 0.47}], "multiple")
        self.assertEqual(r.keys, ["A", "B"])
        self.assertTrue(r.ambiguous)

    def test_single_all_low_scores_is_rejected(self):
        """全项低分说明模型没把握，不能矮子里拔将军。"""
        r = score_to_keys([{"k": "A", "v": 0.30}, {"k": "B", "v": 0.10}], "single")
        self.assertEqual(r.keys, [])
        self.assertTrue(r.ambiguous)

    def test_empty(self):
        self.assertEqual(score_to_keys([], "single").keys, [])
        self.assertIsInstance(score_to_keys([], "single"), MatchResult)


if __name__ == "__main__":
    unittest.main()