"""在考试窗口里自动答题。

不走 HTTP 提交，全部通过内嵌浏览器模拟真人点击：
读题 → 问模型 → 点选项 → 回读答案确认 → 翻页。

漏答的两个真实原因（都已在下面处理）：
1. 判断题的选项 key 是 true/false，不是字母，按字母匹配会落空；
2. 点击偶尔没写进 answer 字段，翻页前必须回读确认。
"""

import json
import random
import re
import threading
import time

from api.exam import (ExamQuestion, answer_objective, ask_ai, exam_pace,
                      exam_stats, reset_exam_stats, set_course_name)
from api.matcher import resolve

TYPE_MAP_CN = {"单选题": "single", "多选题": "multiple", "判断题": "judgement",
               "填空题": "completion", "简答题": "shortanswer",
               "名词解释": "term", "论述题": "essay", "计算题": "calculation"}

SUBJECTIVE = ("简答题", "论述题", "名词解释", "计算题")

_CLEAN = [
    re.compile(r"^.*?[（(]共\s*\d+\s*题[^）)]*[）)]\s*"),
    re.compile(r"^\d+\s*[.、]\s*"),
    re.compile(r"^[（(]\s*\d+(\.\d+)?\s*分\s*[）)]\s*"),
    re.compile(r"^标记\s*"),
]

READ_Q = r"""
(function () {
  var w = document.querySelector('.questionWrap');
  if (!w) return JSON.stringify({ok: false});
  var tit = w.querySelector('div.tit');
  var qidNode = w.querySelector("input[name='questionId']");
  var typeNode = w.querySelector("input[name^='typeName']");
  var opts = [];
  var boxes = w.querySelectorAll('div.answerList');
  for (var i = 0; i < boxes.length; i++) {
    var b = boxes[i];
    var cc = b.querySelector('cc');
    opts.push({
      key: b.getAttribute('name') || '',
      shown: ((b.querySelector('.No') || {}).innerText || '').trim(),
      text: cc ? cc.innerText.trim() : (b.innerText || '').trim().slice(0, 60)
    });
  }
  var ans = w.querySelector("input[id^='answer']");
  var prog = document.body.innerText.match(/(\d+)\s*\/\s*(\d+)/);
  return JSON.stringify({
    ok: true,
    qid: qidNode ? qidNode.value : '',
    typeName: typeNode ? typeNode.value : '',
    title: tit ? tit.innerText.replace(/\s+/g, ' ').trim() : '',
    options: opts,
    answered: ans ? ans.value : '',
    hasTextarea: !!w.querySelector('textarea'),
    progress: prog ? prog[0] : ''
  });
})()
"""

READ_CARD = r"""
(function () {
  var lis = document.querySelectorAll('.answerSheetBox li.nav-revise');
  if (!lis.length) return JSON.stringify({ok: false});
  // 当前停留的那一题只带 current、不带 complated，但它可能已经答好了，
  // 所以单独按 answer 字段判断，避免把它误算成未答。
  var cur = document.querySelector('.answerSheetBox li.nav-revise.current');
  var curNum = cur ? ((cur.querySelector('a') || {}).innerText || '').trim() : '';
  var curAns = (document.querySelector("input[id^='answer']") || {}).value || '';
  var undone = [], done = 0;
  for (var i = 0; i < lis.length; i++) {
    var li = lis[i], a = li.querySelector('a');
    var n = a ? (a.innerText || '').trim() : '';
    if (/(^|\s)complated(\s|$)/.test(li.className)) {
      done++;
    } else if (n === curNum && curAns) {
      done++;
    } else {
      undone.push(n);
    }
  }
  return JSON.stringify({ok: true, total: lis.length, done: done, undone: undone});
})()
"""


def clean_title(text: str) -> str:
    t = (text or "").strip()
    for _ in range(3):
        before = t
        for pat in _CLEAN:
            t = pat.sub("", t).strip()
        if t == before:
            break
    return t


def _js(win, script: str, timeout: float = 15.0):
    """执行页面 JS，带超时。

    evaluate_js 是同步调用：页面弹出原生 alert/confirm 时 JS 主线程被挂起，
    这个调用永远不返回。以前用固定线程池 + future.result(timeout)，超时只是
    「不再等」，卡住的 worker 永远回不来；累计 4 次之后所有 _js 都立刻超时，
    答题链路彻底失效，只能重启程序。
    改成每次调用起一个守护线程：卡住只泄漏一个线程，后续调用不受影响。
    """
    if win is None:
        return None
    box: dict = {}
    done = threading.Event()

    def run():
        try:
            box["value"] = win.evaluate_js(script)
        except Exception:
            box["error"] = True
        finally:
            done.set()

    threading.Thread(target=run, daemon=True, name="cx-eval").start()
    done.wait(timeout)
    return box.get("value")


GUARD_JS = r"""
(function () {
  if (window.__cxGuard) return 'already';
  window.__cxGuard = true;
  window.__cxDialogs = [];
  window.alert = function (m) {
    window.__cxDialogs.push(['alert', String(m)]);
  };
  window.confirm = function (m) {
    window.__cxDialogs.push(['confirm', String(m)]);
    return true;   // 默认同意：阻塞比误操作更致命，所有弹窗都会写进日志
  };
  window.prompt = function (m) {
    window.__cxDialogs.push(['prompt', String(m)]);
    return null;
  };
  return 'ok';
})()
"""


def install_guard(win) -> None:
    """装对话框守卫。页面重载后要重新装，所以每次跳题后都调一次。"""
    _js(win, GUARD_JS, timeout=5.0)


def drain_dialogs(win, log) -> None:
    """把页面弹过的对话框取出来写进日志，方便判断卡在哪。"""
    raw = _js(win,
              "(function(){if(!window.__cxDialogs)return '';"
              "var d=window.__cxDialogs.splice(0);"
              "return d.map(function(x){return x[0]+':'+x[1].slice(0,60);}).join(' | ');})()",
              timeout=5.0)
    if raw:
        log(f"  页面弹窗（已自动同意）：{raw}")


def wait_js(win, expr: str, timeout: float = 8.0, interval: float = 0.25):
    """轮询等某个 JS 表达式变成真值，超时返回 None。

    比固定 sleep 可靠：页面快就立刻过，页面慢也不至于点空。
    """
    import time as _t

    end = _t.time() + timeout
    while _t.time() < end:
        value = _js(win, expr)
        if value:
            return value
        _t.sleep(interval)
    return None


QA_READY = "!!document.querySelector('.questionWrap, .questionLi, .questionContent')"


def current_qid(win) -> str:
    """当前页面的题目 ID。用它判断页面是不是真的换了。"""
    return _js(win, "(function(){var n=document.querySelector(\"input[name='questionId']\");"
                    "return n?n.value:'';})()", timeout=6.0) or ""


def wait_qid_change(win, old: str, timeout: float = 10.0) -> str:
    """等题目真的换成另一道。

    旧题的容器还在 DOM 里，所以不能用「容器存在」判断加载完成，
    必须比对题号。这是之前读错题、答案点错位的根因。
    """
    end = time.time() + timeout
    while time.time() < end:
        q = current_qid(win)
        if q and q != old:
            return q
        time.sleep(0.2)
    return ""


def prev_question(win) -> bool:
    """翻回上一题，以题号变化为准。"""
    old = current_qid(win)
    r = _js(win, "(function(){var b=document.getElementById('preQuestion');"
                 "if(!b)return 'none';b.click();return 'ok';})()", timeout=6.0)
    if r != "ok":
        return False
    return bool(wait_qid_change(win, old, timeout=6.0))


def goto_first(win, log=None) -> bool:
    """回到第 1 题。

    先走答题卡（最快）；答题卡里第 1 题可能因为已完成而点不动，
    这时退回「连续点上一题」，别因为一次点击失败就放弃整轮。
    """
    old = current_qid(win)
    open_card(win)
    js = ("(function(){var lis=document.querySelectorAll('.answerSheetBox li.nav-revise');"
          "for(var i=0;i<lis.length;i++){var a=lis[i].querySelector('a');"
          "if(a&&(a.innerText||'').trim()==='1'){a.click();return 'ok';}}"
          "return 'notfound';})()")
    if _js(win, js, timeout=6.0) == "ok":
        # 点完之后必须确认真的停在第 1 题：以前「题号没变」也当成功，
        # 结果本轮从卷子中间开始扫，前半张卷白跑。
        changed = wait_qid_change(win, old)
        cur = _js(win, "(function(){var c=document.querySelector("
                       "'.answerSheetBox li.nav-revise.current a');"
                       "return c?((c.innerText||'').trim()):'';})()", timeout=4.0) or ""
        if changed or cur.strip() == "1":
            return True

    # 兜底：一路点上一题退回开头
    if log:
        log("  答题卡跳转没生效，改用「上一题」逐步退回开头")
    for _ in range(120):
        before = current_qid(win)
        if not prev_question(win):
            # 没有上一题按钮了，说明已经在第一题
            return True
        if current_qid(win) == before:
            return False
    return False


def next_question(win) -> bool:
    """翻到下一题，以题号变化为准。"""
    old = current_qid(win)
    r = _js(win, "(function(){var b=document.getElementById('nextQuestion');"
                 "if(!b)return 'none';b.click();return 'ok';})()", timeout=6.0)
    if r != "ok":
        return False
    return bool(wait_qid_change(win, old))


def wait_ready(win, timeout: float = 8.0) -> bool:
    """等题目区渲染出来。"""
    return bool(wait_js(win, QA_READY, timeout))


def read_question(win):
    raw = _js(win, READ_Q)
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


def open_card(win) -> None:
    """展开答题卡面板。已经展开时不要再点，否则会把它关掉。"""
    state = _js(win, "(function(){"
                    "var box=document.querySelector('.answerSheetBox');"
                    "var vis=box&&(box.offsetWidth>0||box.offsetHeight>0||box.getClientRects().length>0);"
                    "if(vis)return 'open';"
                    "var b=document.querySelector('div.answerIcon');"
                    "if(b){b.click();return 'clicked';}"
                    "return 'notfound';})()", timeout=5.0)
    if state == "clicked":
        time.sleep(0.6)


def read_card(win):
    open_card(win)
    raw = _js(win, READ_CARD)
    try:
        return json.loads(raw) if raw else {"ok": False}
    except Exception:
        return {"ok": False}


def goto(win, num: str) -> bool:
    open_card(win)
    js = ("(function(){var lis=document.querySelectorAll('.answerSheetBox li.nav-revise');"
          "for(var i=0;i<lis.length;i++){var a=lis[i].querySelector('a');"
          f"if(a&&(a.innerText||'').trim()==='{num}'){{a.click();return 'ok';}}}}"
          "return 'notfound';})()")
    old = current_qid(win)
    if _js(win, js, timeout=6.0) != "ok":
        return False
    # 点了题号必须等到题目真的换掉，否则后面读到的是上一题
    return bool(wait_qid_change(win, old))


def click_option(win, key: str) -> None:
    js = ("(function(){"
          f"var el=document.querySelector('div.answerList[name=\"{key}\"]');"
          "if(!el)return 'missing';"
          "['mousedown','mouseup','click'].forEach(function(t){"
          "el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,button:0,view:window}));});"
          "return 'ok';})()")
    _js(win, js)


FILL_JS = r"""
(function (parts) {
  var box = document.querySelector('.questionWrap') || document;
  var els = [].slice.call(box.querySelectorAll('.fillAnswer input, .fillAnswer textarea'));
  if (!els.length) {
    var only = box.querySelector('textarea') || box.querySelector('input[type=text]');
    if (!only) return 'no-input';
    els = [only];
  }
  // React 受控组件只认原生 setter，直接改 value 不会触发 onChange
  function put(el, text) {
    var proto = el.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    el.dispatchEvent(new Event('focus', {bubbles: true}));
    setter.call(el, text);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
    el.dispatchEvent(new Event('keyup', {bubbles: true}));
  }
  // 多空题不能把整段答案灌进第一个空、其余空全留白（等于答非所问还显示已答）。
  // 按空数把分段依次填进去；段数不够时空着并如实报告。
  for (var i = 0; i < els.length; i++) {
    var text = (parts.length > 1 && i < parts.length) ? parts[i] : parts[0];
    if (parts.length > 1 && i >= parts.length) text = '';
    put(els[i], text || '');
  }
  var filled = els.filter(function (e) { return (e.value || '').length > 0; }).length;
  if (!filled) return 'empty';
  return filled < els.length ? 'partial:' + filled + '/' + els.length : 'ok';
})(%s)
"""


def fill_text(win, text: str):
    """填主观题/填空题的输入框。返回 ok / no-input / empty / partial:n/m。"""
    import json as _json

    if not (text or "").strip():
        return "empty"
    # 多空的答案按行或分号切开，一道一段
    parts = [p.strip() for p in re.split(r"[\n;；]+", text) if p.strip()]
    if len(parts) <= 1:
        parts = [text.strip()]
    return _js(win, FILL_JS % _json.dumps(parts, ensure_ascii=False)) or "no-input"




def _library():
    """题库实例复用，避免每次答题都重开一次数据库。"""
    global _LIB
    if _LIB is None:
        from api.library import Library

        _LIB = Library()
    return _LIB


_LIB = None


def remember(q, info, keys, evidence: str = "model") -> None:
    """把这道题和答案记进题库。

    存答案原文而不是字母：这场考试的选项是被打乱过的，
    同一个字母换个场次就指向别的选项，只有文本能对上。

    只有经过核对的答案才记成已确认（双答一致），单次模型答案一律待验证。
    """
    try:
        from api.library import fingerprint

        picked = [(o.get("text") or "").strip()
                  for o in info["options"] if o["key"] in keys]
        picked = [t for t in picked if t]
        if not picked:
            return
        answer_text = "、".join(picked) if len(picked) > 1 else picked[0]
        fp = fingerprint(q.title, q.options_text, q.qtype)
        _library().upsert(fp, q.title, q.options_text, q.qtype,
                          answer_text, source="exam",
                          # 考试没有平台判分，两次一致只说明模型稳定、不说明答对。
                          # 一律记待验证，别把可能一致的错答案固化成「已确认」
                          status=0, evidence=evidence)
    except Exception:
        pass


_MISS = {"n": 0}
_SHARED = {"dead": False}


def _should_miss(accuracy: float) -> bool:
    """按目标正确率决定这一题要不要故意答错。

    正确率 90% → 每 10 题错 1 题；100% 就是从不故意错。
    """
    if accuracy >= 100:
        return False
    gap = max(2, int(round(100 / max(0.1, 100 - accuracy))))
    _MISS["n"] += 1
    return _MISS["n"] % gap == 0


def _wrong_keys(options, keys) -> list:
    others = [o["key"] for o in options if o["key"] and o["key"] not in keys]
    if not others:
        return keys
    return [random.choice(others)]


def cached_answer(q, fp: str) -> tuple:
    """先问本地题库，再问共享题库。返回 (答案, 依据来源)。"""
    try:
        local = _library().lookup(fp)
    except Exception:
        local = None
    if local:
        return local, "library"

    if _SHARED["dead"]:
        return "", ""
    try:
        from api.remote_library import lookup_batch

        shared = lookup_batch([fp]).get(fp) or ""
    except Exception:
        # 共享库超时或报错：这一场不要再逐题重试，否则 85 题就是 85 次超时
        _SHARED["dead"] = True
        shared = ""
    if not shared:
        return "", ""

    # 落到本地，下次直接命中，不用每题都联网
    try:
        _library().upsert(fp, q.title, q.options_text, q.qtype, shared,
                          source="share", status=1, evidence="shared")
    except Exception:
        pass
    return shared, "shared"


def _picked_ok(cur: str, keys: list, qtype: str = "") -> bool:
    """回读校验：选中的选项必须和期望一致。

    以前只判 `cur` 非空，多选点了 A、B 里只进去 A 也算成功 —— 少选是 0 分，
    而且答题卡会把这题记成已答，最后照常交卷，等于白丢分。
    """
    text = str(cur or "").strip()
    if not text:
        return False
    if (qtype or "").lower() in ("judgement", "judge"):
        return True                     # 判断题字段里是 true/false，非空即已写入
    want = {str(k).strip().upper() for k in keys if str(k).strip()}
    if not want:
        return False
    # 只看 ASCII 字母数字：平台如果把选项原文写进字段，中文会被 isalnum 当成
    # 合法字符，混进集合里变成误判。
    got = {ch.upper() for ch in text if ch.isascii() and ch.isalnum()}
    if not got:
        # 认不出平台把答案写成了什么格式：退回「非空即成功」，
        # 宁可放过一次也不要因为校验把已经写好的答案判死
        return True
    if all(len(k) == 1 for k in want):
        return got == want
    return want.issubset(got)


def solve_current(win, log=print):
    """作答当前这一题并复查。返回 'ok' / 'already' / 'subjective' / 'fail'。"""
    info = read_question(win)
    if not info or not info.get("ok"):
        return "fail"

    prog = info.get("progress", "")
    pace = exam_pace()

    # 主观题 / 填空题：走输入框。
    # 填空题有的是单行 input（没有 textarea），以前落到客观题分支，没选项直接判失败。
    if (info.get("hasTextarea") or info["typeName"] in SUBJECTIVE
            or info["typeName"] == "填空题"):
        if info.get("answered"):
            log(f"  [{prog}] 已有答案，跳过")
            return "already"
        q = ExamQuestion(index=0, qid=info["qid"],
                         qtype=TYPE_MAP_CN.get(info["typeName"], "shortanswer"),
                         title=clean_title(info["title"]), options={},
                         type_name=info["typeName"])
        answer = (ask_ai(q) or "").strip()
        if not answer:
            log(f"  [{prog}] 主观题没写出来")
            return "fail"
        result = fill_text(win, answer)
        if result == "ok":
            log(f"  [{prog}] {info['typeName'] or '主观题'} 已填写 {len(answer)} 字")
            return "ok"
        if result.startswith("partial:"):
            # 多空题只填上了一部分：如实报出来，别让「已答」掩盖空白
            log(f"  [{prog}] {info['typeName'] or '填空题'} 只填上 {result[8:]} 个空，"
                f"缺的空请自己补")
            return "ok"
        log(f"  [{prog}] 主观题填不进去（{result}）")
        return "fail"

    options = {o["key"]: o["text"] for o in info["options"] if o["key"]}
    q = ExamQuestion(index=0, qid=info["qid"],
                     qtype=TYPE_MAP_CN.get(info["typeName"], "single"),
                     title=clean_title(info["title"]), options=options,
                     type_name=info["typeName"])

    answered = str(info.get("answered") or "").strip()
    already_keys: set = set()
    if answered:
        # 多选只勾了一个也会被答题卡算成「已答」，而少选在平台上是 0 分。
        # 所以多选不直接跳过：继续问一次，只补没勾上的，不动已经勾好的。
        single_letter_keys = all(len(str(o.get("key") or "")) == 1 for o in info["options"])
        if q.qtype == "multiple" and single_letter_keys:
            already_keys = {ch.upper() for ch in answered if ch.isalnum()}
            if len(already_keys) >= len(info["options"]):
                log(f"  [{prog}] 已全选（{answered!r}），跳过")
                return "already"
            log(f"  [{prog}] 已有答案 {answered!r}，稍后只补没勾上的")
        else:
            # 不把页面上的既有答案写进题库：它可能是旧版本错位留下的错误答案，
            # 也可能是用户手填的，来源不可验证。入库会让错误一直传下去。
            log(f"  [{prog}] 已有答案 {answered!r}（沿用，不入库）")
            return "already"

    if not options:
        log(f"  [{prog}] 没解析到选项")
        return "fail"

    from api.library import fingerprint

    fp = fingerprint(q.title, q.options_text, q.qtype)

    keys, answer, evidence = [], "", ""
    hit, where = cached_answer(q, fp)
    if hit:
        match = resolve([hit], info["options"], q.qtype)
        keys = list(match.keys)
        if keys:
            answer, evidence = hit, where
            log(f"  [{prog}] 题库命中（{'共享' if where == 'shared' else '本地已确认'}）"
                f"：{hit[:26]}（{match.method}）")

    if not keys:
        # 单答打分：只有低置信 / 有歧义 / 没选出时才补一次复核。
        # 以前是无条件双答 + 最多两轮，最坏 6 次调用，一半 token 花在复核上。
        log(f"  [{prog}] 正在问模型（单答打分）…")
        scored, match = answer_objective(q, log, gate=pace["conf_gate"])
        if scored is not None:
            keys = list(match.keys)
            if keys:
                evidence = "ai_score" if scored.calls == 1 else "ai_recheck"
                by_key = {o["key"]: (o.get("text") or "") for o in info["options"]}
                answer = "、".join(by_key.get(k, "") for k in keys).strip()
                log(f"  [{prog}] 模型作答 把握 {scored.confidence:.2f}"
                    f"{'（复核过）' if scored.calls > 1 else ''}")
    if not keys:
        log(f"  [{prog}] 没拿到可用答案")
        return "fail"

    # 目标正确率低于 100% 时，按比例故意留错，避免整卷满分显得异常
    forced_wrong = False
    if _should_miss(pace["accuracy"]):
        flipped = _wrong_keys(info["options"], keys)
        if flipped != keys:
            keys = flipped
            forced_wrong = True
            log(f"  [{prog}] 按目标正确率 {pace['accuracy']:.0f}% 故意答错这题（不入题库）")

    # 多选已经有部分答案时，只补没勾上的，别把已经勾好的取消掉
    all_keys = list(keys)
    if already_keys:
        missing = [k for k in keys if str(k).upper() not in already_keys]
        if not missing:
            log(f"  [{prog}] 现有答案已经覆盖本题，跳过")
            return "already"
        keys = missing

    # 点完必须回读，没写进去就重试；用轮询等而不是固定延时，
    # 慢页面上也能等到答案真正落到字段里
    for _ in range(3):
        for k in keys:
            click_option(win, k)
            time.sleep(0.15)
        cur = wait_js(
            win,
            "(function(){var w=document.querySelector('.questionWrap');"
            "if(!w)return '';var a=w.querySelector(\"input[id^='answer']\");"
            "return a?a.value:'';})()",
            timeout=3.0, interval=0.2) or ''
        if _picked_ok(cur, all_keys, q.qtype):
            if evidence in ("ai_score", "ai_recheck") and not forced_wrong:
                remember(q, info, all_keys, evidence)
            log(f"  [{prog}] 已作答 {all_keys} = {cur!r} <- {answer[:26]}")
            return "ok"
    log(f"  [{prog}] 点了三次都没写进答案")
    return "fail"


def _log_stats(log=print) -> None:
    """把本次考试的模型用量写进日志，方便回头看「一门课到底花了多少」。"""
    st = exam_stats()
    total = st["prompt"] + st["completion"]
    if not st["calls"]:
        return
    log(f"本次考试模型调用 {st['calls']} 次，约 {total} tokens"
        f"（输入 {st['prompt']} / 输出 {st['completion']}）")


def run_pass(win, log=print, rounds: int = 3, deadline_minutes: float = 25,
             course: str = "") -> dict:
    """按答题卡把所有未答的题补完，每轮都重新读卡核对。

    deadline_minutes 是硬上限：跨线程操作 WebView 万一卡住，
    也要能自己收尾，不能让状态一直挂在「作答中」。
    course 是课程名，带进提示词给模型当学科上下文。
    """
    import time as _t

    if course:
        set_course_name(course)

    deadline = _t.time() + deadline_minutes * 60
    pace = exam_pace()
    reset_exam_stats()
    _MISS["n"] = 0
    _SHARED["dead"] = False
    stats = {"ok": 0, "fail": 0, "left": 0, "error": "", "submitted": False,
             "ready": False}
    for rnd in range(rounds):
        if _t.time() > deadline:
            log(f"已到 {deadline_minutes:g} 分钟上限，先停在这里")
            break
        card = read_card(win)
        if not card.get("ok"):
            # 刚点完「开始」时卷面常常还在渲染，答题卡也还没进 DOM。
            # 先等题目区出现，再读两次，别一秒就判死。
            if wait_ready(win, 15):
                for _ in range(3):
                    _t.sleep(2.0)
                    card = read_card(win)
                    if card.get("ok"):
                        break
        if not card.get("ok"):
            # 读不到卡片说明还没进考卷或页面没加载完，也可能是这个卷面模板
            # 没有答题卡。把页面实际有什么打进日志，下一次才不会瞎猜。
            probe = _js(win, """(function(){
              function n(s){try{return document.querySelectorAll(s).length}catch(e){return -1}}
              return JSON.stringify({
                answerSheet: n('.answerSheetBox li.nav-revise'),
                navRevise: n('li.nav-revise'),
                questionWrap: n('.questionWrap'),
                questionLi: n('.questionLi'),
                nextBtn: n('#nextQuestion'),
                tip: (document.title||'').slice(0,40)
              });
            })()""", timeout=6.0)
            log(f"页面元素统计：{probe}")
            stats["error"] = "读不到答题卡，可能还没进入考卷或页面没加载完"
            log(stats["error"] + "，停止")
            break
        if rnd == 0 and card.get("total"):
            # 长卷 + 每题间隔 + 双答会顶到硬上限，中途停住就不会交卷。
            # 只放宽不收紧，短卷行为不变
            need = card["total"] * (pace["gap"] + 12) + 120
            if _t.time() + need > deadline:
                deadline = _t.time() + need
                log(f"按题数放宽时间上限：{card['total']} 题约需 {int(need / 60)} 分钟")
        undone = card["undone"]
        log(f"第 {rnd + 1} 轮：共 {card['total']} 题，已答 {card['done']}，未答 {len(undone)}")
        install_guard(win)
        drain_dialogs(win, log)
        if not undone:
            stats["left"] = 0
            # 不能在这里 return：底部还有「收尾判定 + 用量统计」。
            # 直接返回的话 stats 不完整，调用方会以为这一轮没跑完。
            break

        # 从第 1 题顺序翻到卷尾：不靠答题卡乱跳，阅读顺序也更符合直觉
        if not goto_first(win, log):
            log("回不到第 1 题，本轮放弃")
            break
        install_guard(win)
        drain_dialogs(win, log)

        seen: set = set()
        while True:
            if _t.time() > deadline:
                log("时间到，本轮中止")
                break
            info = read_question(win)
            qid = (info or {}).get("qid", "")
            if not info or not info.get("ok"):
                log("读不到题目，视为卷尾")
                break
            if qid and qid in seen:
                log("题目回到已处理位置，视为卷尾")
                break
            if qid:
                seen.add(qid)

            status = solve_current(win, log)
            drain_dialogs(win, log)
            if status == "ok":
                stats["ok"] += 1
            elif status == "fail":
                stats["fail"] += 1

            if pace["gap"]:
                _t.sleep(pace["gap"])
            if not next_question(win):
                log("已到最后一题")
                break

        card = read_card(win)
        if card.get("ok"):
            stats["left"] = len(card["undone"])

    card = read_card(win)
    if card.get("ok"):
        stats["left"] = len(card["undone"])
        log(f"复查结果：{card['total']} 题，已答 {card['done']}，未答 {stats['left']} 题")
        if card["undone"]:
            log("仍未作答：" + "、".join(card["undone"][:20]))

    # 交卷一律由人来做：交卷不可逆，而且卷面上还有程序判断不了的东西
    # （主观题写得够不够、图片题、老师临时加的说明）。程序只负责把题答完，
    # 然后明确告诉你「可以交了」，绝不替用户点那个按钮。
    stats["submitted"] = False
    if stats["error"]:
        log("没能读到答题卡，请到考场窗口里确认状态")
        _log_stats(log)
        return stats

    if stats.get("left", 1) == 0:
        stats["ready"] = True
        log("全部题目已作答，请在考场窗口里检查后手动交卷")
    else:
        log(f"还有 {stats.get('left', 0)} 题没答完，请到考场窗口补齐后再交卷")
    _log_stats(log)
    return stats
