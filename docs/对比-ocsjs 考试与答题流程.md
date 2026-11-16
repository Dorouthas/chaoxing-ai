# ocsjs 的考试 / 答题流程拆解，与我们的逐项对比

- 参考版本：`ocsjs/ocsjs` main（`packages/core/src/core/worker`、`packages/scripts/src/projects/cx.ts`）
- 我们的版本：3.8.9（`api/browser_exam.py`、`api/exam.py`）
- 结论一句话：**它的"进考试"不如我们，它的"答题"比我们细。** 值得抄的是答题引擎，不是进场逻辑。

---

## 0. 先把边界说清

ocsjs 的「考试」是**页面驱动**，不是接口驱动：

- 它**不拉考试列表**、**不自动进考场**、**不自动交卷**；
- 它只在「考试页面已经打开」之后接管答题；
- 考试入口靠用户自己点，脚本靠 URL 正则自动挂上去。

所以「怎么进考场」这块我们反而是强的（`api/exam_list.py` 拉列表 + 内嵌浏览器过验证码 + 自动接管）。真正有差距的是**拿到卷面之后怎么答**。

---

## 1. ocsjs 的考试链路：四个脚本接力

| 脚本 | 匹配 | 干什么 |
|---|---|---|
| `versionRedirect` | `exam/test?`、`mooc2=0`、`work/doHomeWorkNew` | 发现旧版考试页 → 等过人脸 → 强制切新版（`mooc2=1&newMooc=true`） |
| `examRedirect` | `exam-ans/exam/test/reVersionTestStartNew`、`mooc-ans/...` | **判断能不能整卷预览**，决定走哪条分支 |
| `work` | `/mooc2/exam/preview` | 整卷模式：一页读完所有题 |
| `work`（非预览） | 同上，`preview_mode=false` | 逐题模式：一题一页，`thread=1` |

### 关键判断（`cx.ts:643-669`）

```ts
if ($gm.unsafeWindow.document.querySelector('.mark_info')?.textContent?.includes('不允许整卷预览')) {
    // 分支 A：逐题
    commonWork({ workerProvider: (opts) =>
        workOrExam(isExam ? 'exam' : 'work', { ...opts, preview_mode: false, thread: 1 }) });
    return;
}
// 分支 B：让它跳到整卷预览（调平台自己的函数，不是拼 URL）
setTimeout(() => $gm.unsafeWindow.topreview(), 3000);
```

两条分支的取舍写得很直白：

- **整卷预览**：一次拿到全部 `.questionLi`，可以并发搜题，答完还能整体复查；
- **逐题**：每条题目一个页面，只能 `[onclick="getTheNextQuestion(1)"]` 翻页，`thread` 必须压到 1，翻页前 `sleep 3s`。而且明确警告用户：

  > 由于当前考试禁止整卷预览，各题为独立新页面，只能一个个答题
  > **在考完前禁止手动切换题目，否则会导致重复答题！**

### 交卷

**它不交卷。** 全是这一句：

```ts
$message.info({ content: '作业/考试完成，请自行检查后保存或提交。', duration: 0 });
```

理由也合理：考场交卷不可逆，脚本只对"答案"负责，不对"交卷"负责。

---

## 2. 答题引擎 `OCSWorker`（`core/worker/worker.ts`）

### 2.1 两条线程

```
┌─ requestThreadHandler ─┐   并发搜题（线程锁池，thread=N）
│   每题 → answerer()    │
└───────────┬────────────┘
            │ result.requested = true
┌───────────▼────────────┐
│    resolverThread      │   串行作答，等 requested 才处理
│   每题 → resolver()    │
└────────────────────────┘
```

- 搜题可以并发，**作答必须串行**（DOM 点击不能并行）；
- 两条线程都受 `isStop`（暂停）/ `isClose`（强制关闭）控制，`waitForContinuate()` 每 200ms 轮询一次。

### 2.2 五个事件

```ts
type WorkerEvents = {
    start: () => void;
    done: () => void;
    close: () => void;      // 强制关闭
    stop: () => void;       // 暂停
    continuate: () => void; // 继续
};
```

### 2.3 元素消失自动收工（`worker.ts:55-88`）

```ts
// 题目容器 1 秒轮询，连续消失超过 3 秒 → 自动 close
this.rootObserverTimer = setInterval(() => {
    const alive = typeof root === 'string' ? !!document.querySelector(root)
                                           : root.some((el) => el.isConnected);
    if (alive) { this.rootLostSince = undefined; return; }
    this.rootLostSince ??= Date.now();
    if (Date.now() - this.rootLostSince >= 3000) {
        $message.warn('⚠️ 题目元素已从页面消失（页面可能已跳转或刷新），答题程序已自动关闭。');
        this.emit('close');
    }
}, 1000);
```

这一条直接对应我们的一个隐患：**页面被踢/超时跳转后，我们还在对着已经脱离文档的旧 DOM 点**。

### 2.4 提交策略（`uploadHandler`）

```ts
const rate = finished / results.length * 100;
if (type === 'force')  return callback(rate, true);          // 强制交
if (type === 'save')   return callback(rate, false);         // 只保存
return callback(rate, rate >= parseFloat(type));             // 达到 X% 才交
```

配置项 `upload` 可以是 `'save' | 'nomove' | 'force' | 10..90 | '100'`。**"答一半也交卷"这件事，它是用阈值挡住的。**

---

## 3. 四类 resolver：它真正值钱的地方

### 3.1 单选题（`resolvers/single.ts`）四阶段

1. **归一化精确匹配**：去标点/空格/全角半角后精确比对；
   并列命中时用 `disambiguateBySign` 做**符号敏感消歧**（`-x` 和 `x`、`-2` 和 `2` 归一化后相同，直接取第一个会选错符号）。
2. **相似匹配**：取所有选项相似度最高且 **> 0.6** 的那个。
3. **纯 ABCD 兜底**：`isPlainAnswer` 校验（长度 ≤ 8、字符递增、不重复）。
4. **多片段合并**：答案被分隔符拆成 `听党指挥 # 能打胜仗 # 作风优良` 时，合并成一个整体再走一遍。
   —— 注释写得很好：合并后"完整覆盖时阶段 1 精确命中；只覆盖部分片段时，既匹配不上也达不到阈值，从而返回未命中"。**宁可漏，不可错。**

### 3.2 多选题（`multiple.ts`）两阶段 + 领先度消歧

- 归一化匹配**只做单向**：只认「答案 ⊇ 选项」，不认「选项 ⊇ 答案」。

  > 不取"选项包含答案"方向，避免 "TCP" 误选 "TCP/IP协议" 等语义不一致的选项

- 相似匹配后做 `disambiguateSimilarOptions`：候选选项两两比相似度，>0.6 就只保留与答案匹配度更高的那个。
  解决"一字之差的选项全选"（`计算机网络` vs `计算机网络原理`）。

### 3.3 判断题（`judgement.ts`）

维护 `CORRECT_WORDS` / `INCORRECT_WORDS` 两组词（是/对/正确/√/true/T/1 ↔ 否/错/×/false/F/0），
**答案和选项都归一到词表再比对**，而不是简单字符串包含。

### 3.4 题型判定（`worker/utils.ts:4-23`）

```ts
radio 数 == 2        → judgement
radio 数 > 2         → single
checkbox 数 > 2      → multiple
textarea 数 >= 1     → completion
```

比读平台给的类型字段更鲁棒——平台字段缺失/错乱时还有兜底。

---

## 4. 我们现在的流程

`api/browser_exam.py`：

```
read_card（读答题卡）
  → goto_first
  → 循环：read_question → 题库命中 / LLM 双答核对 → pick_keys → click_option
       → 回读 input 值（失败重试 3 次）→ next_question
  → 全部答完 → submit_paper（找「交卷」→ 点确认 → 回读「已提交」）
```

`api/exam.py` 负责答案侧：`ask_with_check()` 双答核对、`resolve_answer()` 题库优先、`exam_pace()` 变速/故意答错。

**我们比它强的地方**：

- 会自动交卷（它不敢）；
- LLM 双答核对 + 三答多数表决（它只有题库）；
- 每题点完**回读 input 值**并重试 3 次（它点完 `sleep(500)` 就往下走）；
- 有答题节奏控制（每题间隔、整卷最少用时、目标正确率）；
- 题库按「题干 + 排序后选项」指纹去重。

---

## 5. 逐项对比

| 维度 | ocsjs | 我们 | 判断 |
|---|---|---|---|
| 进场 | 用户手动，脚本按 URL 挂载 | 拉列表 + 内嵌浏览器 + 自动接管 | **我们强** |
| 卷面读取 | 整卷预览一次读完 / 逐题一页一题 | 逐题 + 答题卡跳转 | **它强**（整卷可并发、可复查） |
| 题型判定 | `input[name^=type]` 值映射 + DOM 兜底 | 平台 `typeName` + TEXTAREA | 它更稳 |
| 答案来源 | 多个题库 HTTP，并发 + 超时竞速 | 本地题库 → 共享题库 → LLM 双答 | **我们强** |
| 答案→选项匹配 | 归一化 + 相似度 + 符号消歧 + 领先度消歧 | `pick_keys`：字母映射 + 双向 `in` 包含 | **它强，差距最大** |
| 点选后校验 | 无（handler 内 sleep） | 回读 input，3 次重试 | **我们强** |
| 交卷 | 从不自动交卷 | 自动交卷 | 我们敢，但要加闸 |
| 提交阈值 | `upload` 支持完成率阈值 | 只按"未答=0"判断 | 它更谨慎 |
| 异常处理 | 五事件 + root 观察器 | 每轮 `read_card` 重试 + 25 分钟硬上限 | 各有千秋 |
| 用户可见进度 | 逐题面板（搜索中/已答/失败） | 只有日志 | **它强** |

---

## 6. 最值得抄的五条（按性价比排）

### ① 移植四类 resolver —— 1 天，收益最大

`pick_keys()`（`browser_exam.py:354-387`）现在的匹配逻辑是：

```python
for o in options:
    ot = (o.get("text") or "").strip()
    if ot and (ot in text or text in ot):   # ← 双向包含
        keys.append(o["key"])
```

三个已知会出错的地方：

1. **双向包含**：答案 `TCP`、选项 `TCP/IP协议` → `text in ot` 命中，**误选**。ocsjs 专门只做单向。
2. **没做归一化**：答案 `人民群众是历史的创造者。`、选项 `人民群众是历史的创造者` → 差一个句号就匹配不上。
3. **没做符号消歧**：`-x` 与 `x`、`-2` 与 `2` 会命中同一个，取第一个就错。
4. **多片段答案**：`A#B#C` 这种被分隔符拆开的整体答案，没有合并逻辑。

这几段是**纯函数**（输入答案字符串 + 选项列表，输出索引），可以 1:1 翻成 Python，配 `tests/` 里的金样本直接回归。建议新增 `api/matcher.py`，`pick_keys` 只保留"字母→真实 key"的映射职责，匹配交给 matcher。

### ② 整卷预览模式 —— 3 天，解决"答得慢"

我们现在是**严格串行**：翻一题 → 读题 → 问模型（双答 = 2~3 次调用）→ 点选 → 下一题。
85 题的卷子按每题 10–20 秒算，就是 15–28 分钟——正好顶到 `run_pass` 的 25 分钟上限（代码里已经要"按题数放宽时间上限"了，说明这个矛盾已经出现过）。

抄它的双模式：

- 先试 `topreview()`（或直接进 `/mooc2/exam/preview`）；
- 读到 `.mark_info` 含「不允许整卷预览」→ 退回逐题，并把 `thread` 压到 1；
- 整卷模式：一次读全卷 → 题库/模型**并发** → 统一写入 → 复查 → 再决定交不交。

### ③ 交卷前加完成率阈值 —— 半天，降风险

现在 `run_pass` 只在"答题卡确认全部已答"时交卷，但**部分题的失败会被算作已答吗**？`solve_current` 返回 `fail` 时本轮会继续，下一轮再试；三轮都失败的题，答题卡仍然是"未答"——逻辑上不会误交。但缺少一条像 ocsjs 那样的**显式阈值**：

> 完成率 < X% 时不自动交卷，改为提示"还有 N 题没答上，请在窗口里手动处理"。

建议默认 X=95%，可在设置里调。

### ④ 五事件 + root 观察器 —— 1 天，防"对着空气点"

加 `stop / continuate / close`，以及"题目容器连续 3 秒不存在 → 自动停"。
这一条能直接消掉一类线上问题：考试超时被踢回列表页，脚本还在按旧题号点，日志刷一堆"点了三次都没写进答案"。

### ⑤ 逐题进度面板 —— 1 天，体验

ocsjs 的题型面板是"每题一行：等待搜索中 → 搜索中 → 已答 / 失败"。
我们现在只有右侧日志，用户看不到"第 12 题卡住了"。这块和上一轮方案里的「闭环」是同一件事。

---

## 7. 不建议照搬的

| 它的做法 | 为什么不抄 |
|---|---|
| 从不自动交卷 | 自动交卷是我们的差异点，但要做成开关 + 阈值（见 ③） |
| 纯题库、无模型 | 题库覆盖率有限；我们的双答核对是优势，保留 |
| GM 存储 / 油猴环境 | 架构不同（我们是 Flask + WebView2），只借思路不借实现 |
| 逐题模式 `thread=1` 硬编码 | 我们如果做整卷模式就不需要这个限制 |
| 依赖 `$gm.unsafeWindow.topreview()` | 需要页面里的平台函数，跨域或改版会失效——我们更该用平台 URL + DOM 判据 |

---

## 8. 落地顺序

1. **`api/matcher.py`**（1 天）：四类匹配 + 单测（拿 `tests/` 现有题样本做金标准）
2. **接入 `solve_current`**（0.5 天）：`pick_keys` 只保留 key 映射，匹配走 matcher
3. **完成率阈值**（0.5 天）：`submit_paper` 前判 `done/total`
4. **root 观察器 + 事件**（1 天）：`run_pass` 每轮检查题目容器存活
5. **整卷预览模式**（3 天，单独排期）：双分支 + 并发答题

前 3 项加起来 2 天，能把"答案明明对却选错选项"和"答一半就交卷"这两类硬伤消掉。