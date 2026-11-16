# -*- coding: utf-8 -*-
"""考试答题主链路的单元测试（不联网）。

重点验证「省 token」的那条规则：
- 一次打分就够的时候，只能调用一次模型；
- 只有低置信 / 有歧义 / 没选出时，才允许补第二次。

用假的 openai 客户端顶替真实请求，所以这些用例可以离线跑。
"""
import json
import sys
import types
import unittest

import api.exam as exam


class _Usage:
    prompt_tokens = 100
    completion_tokens = 20


class _Message:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = ""


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Completion:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _FakeCompletions:
    def __init__(self, responses, state):
        self.responses = responses
        self.state = state          # 跨 client 共享调用游标，模拟「同一个模型连着答两次」

    def create(self, **kwargs):
        index = min(self.state["calls"], len(self.responses) - 1)
        self.state["calls"] += 1
        return _Completion(json.dumps(self.responses[index], ensure_ascii=False))


class _FakeClient:
    def __init__(self, responses, state):
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(responses, state))


def _install_fake_openai(responses):
    """把 openai 模块换成本地假实现。返回的 state["calls"] 是真实调用次数。"""
    state = {"calls": 0, "responses": responses}

    def factory(**kwargs):
        return _FakeClient(responses, state)

    module = types.ModuleType("openai")
    module.OpenAI = factory
    sys.modules["openai"] = module
    return state


def _q(qtype="single", options=None):
    return exam.ExamQuestion(
        index=0, qid="q1", qtype=qtype, title="我国的武装力量由哪几部分组成？",
        options=options if options is not None else {"A": "人民军队", "B": "民兵", "C": "群众"},
    )


class AskScoredTest(unittest.TestCase):
    def setUp(self):
        self._saved_conf = exam._ai_conf
        self._saved_openai = sys.modules.get("openai")
        exam._ai_conf = lambda: {"endpoint": "http://x", "key": "k",
                                 "model": "m", "reasoning_effort": ""}
        exam._CLIENTS.clear()          # 客户端按 endpoint+key 缓存，测试之间必须清掉
        exam.reset_exam_stats()

    def tearDown(self):
        # 不还原会把假 openai 泄漏给后面的测试模块（unittest 按字母序跑），
        # test_exam_session 之后的用例会拿到上一组 fake JSON 当答案。
        exam._ai_conf = self._saved_conf
        exam._CLIENTS.clear()
        if self._saved_openai is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = self._saved_openai

    def test_parses_items_and_confidence(self):
        _install_fake_openai([{"items": [{"k": "A", "v": 0.92}, {"k": "B", "v": 0.05},
                                         {"k": "C", "v": 0.03}], "c": 0.88}])
        got = exam.ask_scored(_q())
        self.assertIsNotNone(got)
        self.assertEqual(len(got.items), 3)
        self.assertAlmostEqual(got.confidence, 0.88, places=3)
        self.assertEqual(exam.exam_stats()["calls"], 1)
        self.assertEqual(exam.exam_stats()["prompt"], 100)

    def test_clamps_and_drops_unknown_labels(self):
        _install_fake_openai([{"items": [{"k": "A", "v": 1.8}, {"k": "Z", "v": 0.9}]}])
        got = exam.ask_scored(_q())
        self.assertEqual([i["k"] for i in got.items], ["A"])
        self.assertEqual(got.items[0]["v"], 1.0)
        # 漏了 B/C，属于「答案不完整」，置信度必须压到 0 去走复核
        self.assertEqual(got.confidence, 0.0)

    def test_bad_json_returns_none(self):
        module = types.ModuleType("openai")
        module.OpenAI = lambda **kw: types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(
                create=lambda **_: _Completion("这不是 JSON"))))
        sys.modules["openai"] = module
        self.assertIsNone(exam.ask_scored(_q()))

    def test_partial_items_force_recheck(self):
        """模型只回了一部分选项：不能按高置信处理，必须压到 0 触发复核。"""
        _install_fake_openai([{"items": [{"k": "A", "v": 0.95}], "c": 0.95}])
        got = exam.ask_scored(_q())
        self.assertIsNotNone(got)
        self.assertEqual(got.confidence, 0.0)


class ScoredToKeysTest(unittest.TestCase):
    def test_judgement_labels_map_to_platform_keys(self):
        q = _q("judgement", {"true": "正确", "false": "错误"})
        scored = exam.ScoredAnswer(items=[{"k": "A", "v": 0.9}, {"k": "B", "v": 0.1}],
                                  confidence=0.9)
        self.assertEqual(exam.scored_to_keys(scored, q).keys, ["true"])

    def test_multiple_keeps_all_above_threshold(self):
        q = _q("multiple", {"k1": "人民军队", "k2": "民兵", "k3": "群众"})
        scored = exam.ScoredAnswer(
            items=[{"k": "A", "v": 0.9}, {"k": "B", "v": 0.8}, {"k": "C", "v": 0.1}],
            confidence=0.9)
        self.assertEqual(sorted(exam.scored_to_keys(scored, q).keys), ["k1", "k2"])


class AnswerObjectiveTest(unittest.TestCase):
    def setUp(self):
        self._saved_conf = exam._ai_conf
        self._saved_openai = sys.modules.get("openai")
        exam._ai_conf = lambda: {"endpoint": "http://x", "key": "k",
                                 "model": "m", "reasoning_effort": ""}
        exam._CLIENTS.clear()          # 客户端按 endpoint+key 缓存，测试之间必须清掉
        exam.reset_exam_stats()

    def tearDown(self):
        exam._ai_conf = self._saved_conf
        exam._CLIENTS.clear()
        if self._saved_openai is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = self._saved_openai

    def test_single_call_when_confident(self):
        state = _install_fake_openai([
            {"items": [{"k": "A", "v": 0.95}, {"k": "B", "v": 0.02}, {"k": "C", "v": 0.01}],
             "c": 0.93},
        ])
        scored, result = exam.answer_objective(_q(), log=lambda *_: None, gate=0.78)
        self.assertEqual(result.keys, ["A"])
        self.assertEqual(scored.calls, 1)
        self.assertEqual(state["calls"], 1)
        self.assertEqual(exam.exam_stats()["calls"], 1)

    def test_recheck_when_ambiguous(self):
        state = _install_fake_openai([
            {"items": [{"k": "A", "v": 0.62}, {"k": "B", "v": 0.58}, {"k": "C", "v": 0.01}],
             "c": 0.80},
            {"items": [{"k": "A", "v": 0.90}, {"k": "B", "v": 0.10}, {"k": "C", "v": 0.01}],
             "c": 0.90},
        ])
        scored, result = exam.answer_objective(_q(), log=lambda *_: None, gate=0.78)
        self.assertEqual(result.keys, ["A"])
        self.assertEqual(scored.calls, 2)
        self.assertEqual(state["calls"], 2)

    def test_recheck_when_low_confidence(self):
        state = _install_fake_openai([
            {"items": [{"k": "A", "v": 0.90}, {"k": "B", "v": 0.05}, {"k": "C", "v": 0.05}],
             "c": 0.50},
            {"items": [{"k": "A", "v": 0.85}, {"k": "B", "v": 0.10}, {"k": "C", "v": 0.05}],
             "c": 0.84},
        ])
        _, result = exam.answer_objective(_q(), log=lambda *_: None, gate=0.78)
        self.assertEqual(result.keys, ["A"])
        self.assertEqual(state["calls"], 2)

    def test_incomplete_first_pass_triggers_recheck(self):
        state = _install_fake_openai([
            {"items": [{"k": "A", "v": 0.95}], "c": 0.95},          # 漏了 B/C
            {"items": [{"k": "A", "v": 0.90}, {"k": "B", "v": 0.10},
                       {"k": "C", "v": 0.05}], "c": 0.90},
        ])
        scored, result = exam.answer_objective(_q(), log=lambda *_: None, gate=0.78)
        self.assertEqual(result.keys, ["A"])
        self.assertEqual(scored.calls, 2)
        self.assertEqual(state["calls"], 2)

    def test_merge_does_not_zero_missing_option(self):
        """一轮漏答 0.8、另一轮给了分：不能被平均成 0.4 丢掉。"""
        a = exam.ScoredAnswer(items=[{"k": "A", "v": 0.9}], confidence=0.9)
        b = exam.ScoredAnswer(items=[{"k": "A", "v": 0.9}, {"k": "B", "v": 0.8}],
                              confidence=0.9)
        merged = exam._merge_scored(a, b)
        scores = {i["k"]: i["v"] for i in merged.items}
        self.assertGreaterEqual(scores["B"], 0.5)

    def test_merge_averages_scores(self):
        a = exam.ScoredAnswer(items=[{"k": "A", "v": 0.9}, {"k": "B", "v": 0.1}], confidence=0.9)
        b = exam.ScoredAnswer(items=[{"k": "A", "v": 0.5}, {"k": "B", "v": 0.7}], confidence=0.6)
        merged = exam._merge_scored(a, b)
        scores = {i["k"]: i["v"] for i in merged.items}
        self.assertAlmostEqual(scores["A"], 0.7, places=3)
        self.assertAlmostEqual(scores["B"], 0.4, places=3)
        self.assertEqual(merged.calls, 2)

    def test_no_config_returns_empty(self):
        exam._ai_conf = lambda: {}
        scored, result = exam.answer_objective(_q(), log=lambda *_: None)
        self.assertIsNone(scored)
        self.assertEqual(result.keys, [])


if __name__ == "__main__":
    unittest.main()