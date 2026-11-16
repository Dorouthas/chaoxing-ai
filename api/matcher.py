# -*- coding: utf-8 -*-
"""答案 → 选项 的匹配层（纯函数，无 IO、无网络、可单测）。

把「模型或题库给了一段答案文本」到「该点哪几个选项」这一步单独抽出来。
设计参考了 ocsjs 的 resolver 实现，但按本项目的调用方式重写：

1. **宁可漏，不可错**：匹配不上返回空，由上层重问或跳过。
   考试里错选是 0 分，漏选还有补答机会，两者代价不对称。
2. **单向包含**：只认「答案包含选项」，不认「选项包含答案」。
   否则答案是「TCP」时会选中「TCP/IP协议」。
3. **先整体、后拆分**：答案本身就是某个选项原文时直接命中，
   避免选项里的「、」「；」被当成答案分隔符切碎。
4. **符号敏感**：`-x` 与 `x`、`-2` 与 `2` 归一化后相同，
   并列命中时用保留符号的严格比较再筛一次。

对外只暴露 `resolve()` 和 `score_to_keys()` 两个入口。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# 常见异体字（超星字体解密后会混进这类字符）。
# 最后一条把 U+2212（数学负号）统一成半角连字符：模型答案常用 −2，
# 不映射的话「−2」会匹配到无符号的「2」而不是「-2」，反向选错。
_VARIANT = str.maketrans({"⻛": "风", "⻔": "门", "⻋": "车", "⻢": "马", "⻅": "见", "⻓": "长", "−": "-"})

# 会被抹掉的标点（保留 + - = . * / 这些算式符号，交给严格模式处理）
_PUNCT = re.compile(r"[，。！？；：,.!?;:()（）\[\]【】{}《》\"“”‘’、|\\_—…~·`]+")
_SPACE = re.compile(r"\s+")
# 选项前缀：A.  A、  (1)  ①  A)  等
# 数字分支带 (?!\d)：否则「2.5」会被削成「5」，数值选项两两撞车
_OPT_PREFIX = re.compile(
    r"^\s*(?:[（(]\s*\d{1,2}\s*[）)](?!\d)|[①-⑳]|[A-Za-z]\s*[.、:：)）]|[0-9]{1,2}\s*[.、)](?!\d))\s*"
)
_LETTERS_ONLY = re.compile(r"^[A-Fa-f](?:[\s,，、/.．]*[A-Fa-f])*$")

# 判断题正误词表（越长越先匹配，避免「不正确」被「正确」抢先）
_TRUE_WORDS = ("正确", "对的", "是的", "对", "是", "√", "true", "t", "yes", "y", "1")
_FALSE_WORDS = ("不正确", "不正确的", "不对", "不是", "错误", "错的", "错", "否", "非", "×", "x", "false", "f", "no", "n", "0")

DEFAULT_SEPARATORS = ("#", "；", ";", "|", "\n", "＆")
SIMILAR_THRESHOLD = 0.60
DISAMBIGUATE_THRESHOLD = 0.85
# 单选/多选里「答案只是选项的一段前缀」时的相似度陷阱阈值，见 _prefix_trap
_PREFIX_TRAP_MIN = 2


@dataclass
class MatchResult:
    """一次匹配的结果。"""

    keys: list = field(default_factory=list)   # 命中的选项 key（原样返回）
    method: str = ""                           # exact / similar / letters / merged / judgement / score
    score: float = 0.0                         # 可信度 0~1，用于日志和门控
    ambiguous: bool = False                    # 命中但存在歧义，建议复核


# ---------- 基础工具 ----------

def normalize(text: str) -> str:
    """归一化：全角转半角、异体字统一、去空白与标点、转小写。"""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).translate(_VARIANT).lower()
    s = _SPACE.sub("", s)
    s = _PUNCT.sub("", s)
    return s.strip()


def normalize_strict(text: str) -> str:
    """保留正负号与小数点，用于「-x / x」「-2 / 2」这类只在符号上有差别的消歧。"""
    s = unicodedata.normalize("NFKC", str(text or "")).translate(_VARIANT).lower()
    s = _SPACE.sub("", s)
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff+\-=*/.^]", "", s)
    return s.strip()


def strip_prefix(text: str) -> str:
    """去掉选项自带的编号前缀：`A. 人民` → `人民`、`(1) 定义` → `定义`。"""
    return _OPT_PREFIX.sub("", str(text or "")).strip()


def is_plain_letters(text: str) -> bool:
    """整段只有选项字母才算（`AC`、`A、C` 算，`yes`、`defense` 不算）。"""
    s = re.sub(r"^(答案|answer)\s*[:：]?", "", str(text or "").strip(), flags=re.I)
    s = s.strip()
    return bool(s) and bool(_LETTERS_ONLY.fullmatch(s)) and len(re.findall(r"[A-Fa-f]", s)) <= 8


def split_answer(answer: str, separators=None) -> list:
    """把多片段答案切开。不传分隔符时用默认集合。"""
    text = str(answer or "").strip()
    if not text:
        return []
    seps = tuple(separators) if separators else DEFAULT_SEPARATORS
    pattern = "|".join(re.escape(s) for s in seps if s)
    if not pattern:
        return [text]
    parts = [p.strip() for p in re.split(pattern, text)]
    return [p for p in parts if p]


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _options(options) -> list:
    """把入参统一成 [{key,text,shown}]。支持 list[dict] 与 list[str]。"""
    out = []
    for item in options or []:
        if isinstance(item, dict):
            out.append({
                "key": str(item.get("key") or ""),
                "text": str(item.get("text") or ""),
                "shown": str(item.get("shown") or ""),
            })
        else:
            out.append({"key": "", "text": str(item), "shown": ""})
    return out


def _key_of(opt: dict, index: int) -> str:
    """取选项的真实 key：优先平台给的 key，其次展示字母，最后用序号。"""
    if opt["key"]:
        return opt["key"]
    if opt["shown"]:
        return opt["shown"]
    return chr(ord("A") + index)


def _letter_to_index(letter: str, opts: list) -> int:
    """把展示字母（A/B/C）映射回选项下标。优先按 shown 匹配。"""
    letter = letter.strip().upper()
    for i, o in enumerate(opts):
        if (o["shown"] or "").strip().upper() == letter:
            return i
    idx = ord(letter) - ord("A")
    return idx if 0 <= idx < len(opts) else -1


# ---------- 判断题 ----------

def _as_bool(text: str) -> str:
    """把一段文本判成 T / F / 空。先长词后短词，避免「不正确」被「正确」抢走。"""
    s = normalize(text)
    if not s:
        return ""
    for word in _FALSE_WORDS:
        if normalize(word) and normalize(word) == s:
            return "F"
    for word in _TRUE_WORDS:
        if normalize(word) and normalize(word) == s:
            return "T"
    for word in _FALSE_WORDS:
        if normalize(word) and normalize(word) in s:
            return "F"
    for word in _TRUE_WORDS:
        if normalize(word) and normalize(word) in s:
            return "T"
    return ""


def resolve_judgement(answers: list, opts: list) -> MatchResult:
    for answer in answers:
        target = _as_bool(answer)
        if not target:
            continue
        for i, opt in enumerate(opts):
            if _as_bool(opt["text"]) == target:
                return MatchResult([_key_of(opt, i)], "judgement", 1.0)
    return MatchResult()


# ---------- 单选 ----------

def _is_prefix_trap(answer: str, option: str) -> bool:
    """答案只是选项的一段前缀时，不让相似度把长选项选中。

    「TCP」对「TCP/IP」相似度 0.75，会误选；「中国」对「中国共产党」同理。
    这类情况宁可判不中、交给复核，也不能选错。
    """
    na, no = normalize(answer), normalize(option)
    if len(na) < _PREFIX_TRAP_MIN or na == no:
        return False
    return no.startswith(na) and (len(no) - len(na)) >= 2


def _exact_hit(answer: str, opts: list) -> list:
    """整段答案与选项归一化后完全相同才算命中。

    答案本身也可能带编号（题库里存的是「B. 人民军队」），所以去前缀后再比一次。
    """
    cands = {normalize(answer), normalize(strip_prefix(str(answer)))}
    cands.discard("")
    if not cands:
        return []
    return [i for i, o in enumerate(opts) if normalize(strip_prefix(o["text"])) in cands]


def _disambiguate_by_sign(answer: str, candidates: list, opts: list) -> list:
    """候选里只差正负号时，用保留符号的比较再筛。

    筛不出来返回空：调用方据此放弃而不是拿 DOM 顺序的第一个——
    选项顺序是平台打乱的，取第一个等于随机点。
    """
    sa = normalize_strict(answer)
    strict = [i for i in candidates if normalize_strict(strip_prefix(opts[i]["text"])) == sa]
    return strict


def resolve_single(answers: list, opts: list, separators=None) -> MatchResult:
    if not opts:
        return MatchResult()

    # 阶段 1：先拿整段答案精确匹配（不切分，避免选项文本里的「、」被误切）
    for answer in answers:
        hits = _exact_hit(answer, opts)
        if len(hits) == 1:
            return MatchResult([_key_of(opts[hits[0]], hits[0])], "exact", 1.0)
        if len(hits) > 1:
            narrowed = _disambiguate_by_sign(answer, hits, opts)
            if narrowed:
                return MatchResult([_key_of(opts[narrowed[0]], narrowed[0])], "exact", 0.9)
            # 多命中且符号筛不出来：宁可漏（交给复核），不能按 DOM 顺序瞎点。
            # 以前取 hits[0]，「1.5 / 2.5」这类同前缀数值题会稳定点错。
            return MatchResult([], "exact", 0.5, ambiguous=True)

    # 阶段 2：多片段答案合并后再比一次（如 "听党指挥 # 能打胜仗 # 作风优良"）
    fragments = []
    for answer in answers:
        fragments.extend(split_answer(answer, separators))
    if len(fragments) > 1:
        merged = "".join(fragments)
        hits = _exact_hit(merged, opts)
        if len(hits) == 1:
            return MatchResult([_key_of(opts[hits[0]], hits[0])], "merged", 0.95)

    # 阶段 3：相似度取最优
    best_i, best_score = -1, 0.0
    for i, opt in enumerate(opts):
        opt_text = strip_prefix(opt["text"])
        for answer in answers:
            for piece in [answer] + split_answer(answer, separators):
                if _is_prefix_trap(piece, opt_text):
                    continue
                sc = similarity(piece, opt_text)
                if sc > best_score:
                    best_score, best_i = sc, i
    if best_i != -1 and best_score > SIMILAR_THRESHOLD:
        tied = [i for i, o in enumerate(opts)
                if any(similarity(a, strip_prefix(o["text"])) >= best_score - 1e-9 for a in answers)]
        if len(tied) > 1:
            narrowed = _disambiguate_by_sign(answers[0], tied, opts) if answers else []
            if narrowed:
                best_i = narrowed[0]
            else:
                # 并列且符号筛不出来（如「5」对「1.5」「2.5」都是 0.67）：
                # 按平台打乱的 DOM 顺序挑等于随机点，宁可漏。
                return MatchResult([], "similar", round(best_score, 3), ambiguous=True)
        return MatchResult([_key_of(opts[best_i], best_i)], "similar", round(best_score, 3),
                           ambiguous=len(tied) > 1)

    # 阶段 4：纯字母兜底（单选只接受单个字母）
    for answer in answers:
        if is_plain_letters(answer):
            letters = re.findall(r"[A-Fa-f]", answer)
            if len(letters) == 1:
                idx = _letter_to_index(letters[0], opts)
                if idx >= 0:
                    return MatchResult([_key_of(opts[idx], idx)], "letters", 0.8)

    return MatchResult()


# ---------- 多选 ----------

def _disambiguate_similar_options(pairs: list, threshold=DISAMBIGUATE_THRESHOLD) -> list:
    """合并「看起来像、但不该同时选」的候选，只处理两种情形：

    1. 一个是另一个的前缀（计算机网络 / 计算机网络原理）；
    2. 文本够像，但匹配分差距明显（说明其中一个只是写得像）。

    关键反例：「社会主义民主」与「社会主义法治」在多选里**都对**，相似度却到 0.67。
    这种两条评分一样高的必须都留下，否则就是稳定的少选 0 分。
    """
    if len(pairs) <= 1:
        return pairs
    eliminated = set()
    for i in range(len(pairs)):
        if i in eliminated:
            continue
        for j in range(i + 1, len(pairs)):
            if j in eliminated:
                continue
            # 两条都是精确命中（1.0）时不动：答案把两项都明确写出来了，
            # 前缀相关的双正确项（计算机网络 + 计算机网络原理）就是这样，
            # 删掉任何一条都是稳定的少选 0 分。
            if pairs[i][0] >= 0.999 and pairs[j][0] >= 0.999:
                continue
            na = normalize(pairs[i][1])
            nb = normalize(pairs[j][1])
            if min(len(na), len(nb)) < 4:
                continue
            prefix = na.startswith(nb) or nb.startswith(na)
            sim = similarity(pairs[i][1], pairs[j][1])
            if not prefix and sim < threshold:
                continue
            gap = abs(pairs[i][0] - pairs[j][0])
            if not prefix and gap < 0.08:
                # 分不出高下就都留着：多选里宁可多选一个相近的正确项，
                # 也不要把两个都对的选项删掉一个
                continue
            if pairs[i][0] >= pairs[j][0]:
                eliminated.add(j)
            else:
                eliminated.add(i)
                break
    return [p for k, p in enumerate(pairs) if k not in eliminated]


def resolve_multiple(answers: list, opts: list, separators=None) -> MatchResult:
    if not opts:
        return MatchResult()

    groups = []   # [(count, score_sum, [(rating, text, key)])]

    for answer in answers:
        text = str(answer or "").strip()
        if not text:
            continue

        # 整段答案恰好等于某个选项 → 按单项处理（题库有时把多选答案写成一项）
        whole = _exact_hit(text, opts)
        if len(whole) == 1 and len(split_answer(text, separators)) <= 1:
            idx = whole[0]
            groups.append((1, 1.0, [(1.0, strip_prefix(opts[idx]["text"]), _key_of(opts[idx], idx))]))
            continue

        picked = []
        texts = [normalize(strip_prefix(o["text"])) for o in opts]
        for piece in [text] + split_answer(text, separators):
            npiece = normalize(piece)
            if not npiece:
                continue

            # 先精确：片段与选项完全一致，最可靠
            exact = [i for i, no in enumerate(texts) if no and no == npiece]
            if exact:
                i = exact[0]
                picked.append((1.0, strip_prefix(opts[i]["text"]), _key_of(opts[i], i)))
                continue

            # 再包含：只认「答案片段包含选项」这一个方向。
            # 命中多个时取最长的那个（最具体），不要按 DOM 顺序随便挑一个 ——
            # 选项顺序是平台打乱的，取第一个等于随机。
            contained = [i for i, no in enumerate(texts) if no and no in npiece]
            if len(contained) == 1:
                i = contained[0]
                picked.append((0.8, strip_prefix(opts[i]["text"]), _key_of(opts[i], i)))
            elif len(contained) > 1:
                i = max(contained, key=lambda x: len(texts[x]))
                picked.append((0.7, strip_prefix(opts[i]["text"]), _key_of(opts[i], i)))

        # 相似度补充（覆盖写法差异）
        for i, opt in enumerate(opts):
            best = max((similarity(p, strip_prefix(opt["text"])) for p in split_answer(text, separators) or [text]),
                       default=0.0)
            if best > SIMILAR_THRESHOLD and not any(k == _key_of(opt, i) for _, _, k in picked):
                picked.append((round(best, 3), strip_prefix(opt["text"]), _key_of(opt, i)))

        if picked:
            picked = _disambiguate_similar_options(picked)
            groups.append((len(picked), sum(p[0] for p in picked), picked))

    if groups:
        groups.sort(key=lambda g: (g[0], g[1]), reverse=True)
        count, total, picked = groups[0]
        keys = []
        for _, _, k in picked:
            if k not in keys:
                keys.append(k)
        ambiguous = len(keys) <= 1 or total / max(1, count) < 0.7
        return MatchResult(keys, "similar", round(total / max(1, count), 3), ambiguous=ambiguous)

    # 纯字母兜底
    keys = []
    for answer in answers:
        if is_plain_letters(answer):
            for ch in re.findall(r"[A-Fa-f]", answer):
                idx = _letter_to_index(ch, opts)
                if idx >= 0:
                    k = _key_of(opts[idx], idx)
                    if k not in keys:
                        keys.append(k)
    if keys:
        return MatchResult(keys, "letters", 0.8)
    return MatchResult()


# ---------- 对外入口 ----------

def resolve(answers, options, qtype: str = "single", separators=None) -> MatchResult:
    """把若干候选答案匹配到选项。

    answers: 候选答案文本列表（题库、模型、多来源都塞进来）
    options: [{"key","text","shown"}] 或 ["A. 文本", ...]
    qtype:   single / multiple / judgement（其他类型直接返回空）
    """
    if isinstance(answers, str):
        answers = [answers]
    answers = [str(a).strip() for a in (answers or []) if str(a or "").strip()]
    if not answers or not options:
        return MatchResult()

    opts = _options(options)
    kind = (qtype or "").lower()

    if kind in ("judgement", "judge"):
        return resolve_judgement(answers, opts)
    if kind in ("multiple", "multi"):
        return resolve_multiple(answers, opts, separators)
    if kind in ("single", "choose"):
        return resolve_single(answers, opts, separators)
    return MatchResult()


def score_to_keys(items, qtype: str = "single", margin: float = 0.15) -> MatchResult:
    """把「选项级打分」聚合成要点击的 key。

    items: [{"k": "A", "v": 0.92}, ...]，v 是模型对该项成立与否的把握 0~1。

    单选：取最高分，但必须领先第二名 margin，否则判为不确定。
    多选：取所有 v >= 0.5 的项；若有落选项的分数落在阈值附近（>= 0.5 - margin），
          标记 ambiguous 交给复核，避免「少选=0 分」。
    """
    pairs = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        k = str(it.get("k") or "").strip()
        try:
            v = float(it.get("v"))
        except (TypeError, ValueError):
            continue
        if k:
            pairs.append((v, k))
    if not pairs:
        return MatchResult()

    # 模型偶发把同一选项输出两遍（A:0.9、A:0.8）。不去重的话 chosen 会拿到
    # 两个 A，多选的第二次点击等于取消勾选，这题就永远点不上。
    merged: dict = {}
    for v, k in pairs:
        if k not in merged or v > merged[k]:
            merged[k] = v
    pairs = sorted(((v, k) for k, v in merged.items()), reverse=True)
    kind = (qtype or "").lower()

    if kind in ("multiple", "multi"):
        # 和单选同一条底线：全部低分说明模型没把握，瞎猜也照点只会把
        #「已答」的假象留给用户。宁可空着交给人工。
        if pairs[0][0] < 0.45:
            return MatchResult([], "score", round(pairs[0][0], 3), ambiguous=True)
        chosen = [k for v, k in pairs if v >= 0.5]
        if not chosen:
            chosen = [pairs[0][1]]
        weakest = min((v for v, k in pairs if k in chosen), default=1.0)
        best_rejected = max((v for v, k in pairs if k not in chosen), default=0.0)
        # 落选项贴着阈值（0.5 - margin/2）时才算歧义：太松会让复核量翻倍
        ambiguous = bool(chosen) and (best_rejected >= 0.5 - margin * 0.5 or weakest < 0.45)
        # 保持稳定顺序：按模型给的顺序，便于日志对照
        order = [str(it.get("k")).strip() for it in items if isinstance(it, dict)]
        chosen.sort(key=lambda k: order.index(k) if k in order else 99)
        return MatchResult(chosen, "score", round(sum(v for v, k in pairs if k in chosen) / len(chosen), 3),
                           ambiguous=ambiguous)

    top_v, top_k = pairs[0]
    # 全项都低分说明模型其实没把握，不能「矮子里拔将军」直接选一个
    if top_v < 0.45:
        return MatchResult([], "score", round(top_v, 3), ambiguous=True)
    second_v = pairs[1][0] if len(pairs) > 1 else 0.0
    if top_v - second_v < margin:
        return MatchResult([], "score", round(top_v, 3), ambiguous=True)
    return MatchResult([top_k], "score", round(top_v, 3))