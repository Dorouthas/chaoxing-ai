# 闭环改造方案（含 ocsjs 借鉴）

配套：[体检-考试流程.md](体检-考试流程.md)

---

## 一、目标与验收

**目标**：让用户在任何一步都能回答三个问题——「现在在哪」「为什么停」「我该点哪里」。

| 验收项 | 判据 |
|---|---|
| 不再有「静默等待」 | 任何超过 3 秒的等待，界面必须显示阶段文字 + 已用时 |
| 失败必须可行动 | 每条错误至少带一个按钮：去登录 / 重试 / 打开独立窗口 / 复制诊断信息 |
| 登录不再丢状态 | 登录失败后 `cookies.txt` 与原来一致（自动回滚） |
| 进场判定不依赖单一 URL 参数 | URL 之外还有 DOM 判据，任意一个命中即算进场 |
| 开窗可见 | 用户能在 2 秒内看到「窗口已打开」的确定反馈（不是"应该打开了"） |

---

## 二、状态机（先把"状态"这件事定死）

现在状态散在三处：`_state["exam"]`、`_exam_browser`、前端 `loginPhase`。改成**服务端唯一状态源**，前端只渲染。

```
session: unknown → no_cookie → logging_in → logged_in ─┐
                                    ↘ login_failed    │
                                                        ▼
exam: idle → opening → intro → waiting_human(验证码/人脸/平台闸门)
                  ↘ open_failed        ↓
                                   answering → submitting → done
                                        ↘ error(可重试)
```

- 端点：`GET /api/session` 返回 `{session, exam, since, detail}`；
- 前端不再自己维护 `loginPhase`，只读这个端点；
- 所有长动作（拉列表、开窗、登录校验）返回 `job_id`，前端轮询 `/api/job/<id>`，**按钮立刻恢复可点**。

这一步做完，「点其他也不行」的观感会直接消失——因为界面不再有同步等待。

---

## 三、五个改造点

### A. 登录事务化（止血，半天）

**现状**：`webui/server.py:850-891` 打开登录页前先 `unlink()` 掉 `cookies.txt` / `login.json`。

**改成**：

1. 先把现有登录态挪到 `cookies.bak` / `login.bak`（同目录，原子 `os.replace`）；
2. 清 WebView2 cookie，打开登录页；
3. 只有 `read_cookies` + `_probe_login` + `_whoami` 三步都通过，才把新登录态写正式文件、删备份；
4. 任何一步失败 → 恢复备份 + 返回「已恢复你原来的登录态（仍可刷课，只是拉不到考试）」。

**验收**：手动制造登录失败（断网打开登录页），确认 `cookies.txt` 字节级不变。

### B. 同步失败必须说话（止血，半天）

**现状**：`server.py:431` 忽略 `sync_cookies` 返回值；`browser_unlock.py:220` 拿不到 core 就 `return 0`。

**改成**：`sync_cookies` 返回 `(count, reason)`；count==0 时**不要导航**，直接回：

> 没能把登录态送进考场窗口（原因：窗口还没初始化 / cookies.txt 是空的）。
> 请点「登录学习通」重新登录一次，或点「打开独立窗口」手动进考场。

**验收**：清空 `cookies.txt` 后点「开始」，必须看到这句，而不是一个登录页。

### C. 开窗链路可观测 + 分类失败（1–2 天）

**现状**：`server.py:386-413` 每秒 `get_current_url()` 找 `enc`；`app-exam.js:96-127` 420 次后超时。

**改成**三件事：

1. **阶段打点**：`open_request → cookie_sync(n) → navigate → dom_ready → gate_detected → enc_detected`，每步写日志（带毫秒）；
2. **DOM 判据优先**，URL 兜底。判定表：

   | 目标状态 | 判据 |
   |---|---|
   | 登录页 | host 含 `passport2` 或存在 `#loginName` |
   | 考场介绍页 | 正文含「开始考试」/「答题时长」 |
   | 平台闸门 | 正文含「低版本」/「不在考试时间」/「已结束」/「无权限」 |
   | 验证码 | 存在 `iframe[src*="captcha"]`、`.yidun_popup` |
   | 人脸 | 正文含「人脸」「活体」 |
   | 已进答题页 | URL 有 `enc` **或** 存在答题卡容器 |

3. **超时分层**：30 秒没有 `dom_ready` → 报「页面没加载出来，可能是网络」；90 秒停在登录页 → 报「登录态失效」；120 秒没有任何变化 → 报「平台没有响应」并给「打开独立窗口」按钮。

**验收**：把 hosts 指到不存在的 IP 模拟「页面打不开」，5 秒内（不是 420 秒）给出可行动提示。

### D. 用 CDP 替掉跨线程调 UI（3–5 天，收益最大）

**现状**：`browser_unlock` 用阻塞 `form.Invoke`；`_watch_exam_browser` 每秒跨线程调 `get_current_url()`；`exam_embed` 每 0.25 秒 Win32 摆窗。三处都在跟 WebView2 的线程模型较劲。

**已验证事实**：WebView2 支持远程调试端口。启动前设

```
WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=<随机空闲端口>
```

就能用标准 CDP 拿到两个 target（实测成功）：

```
http://127.0.0.1:<port>/json/list
→ {title:"超星 AI 答题", url:"http://127.0.0.1:5000/"}
→ {title:"考试",        url:"https://mooc1.chaoxing.com/exam-ans/..."}
```

**改成**：

1. `desktop.py` 启动时选一个空闲端口，写进 `%LOCALAPPDATA%\超星AI答题\cdp.json`（含 pid，退出即作废）；
2. 新增 `api/cdp.py`：`navigate(url, timeout)`、`eval_js(expr, timeout)`、`wait_dom(selector/text, timeout)`、`cookies()`——**每个调用都有超时**，从任意线程调都不碰 UI 线程；
3. `_watch_exam_browser` 退役，换成 `wait_dom()`；
4. `sync_cookies` 改用 `Network.setCookie`（一条 CDP 调用，替代 48 次 `AddOrUpdateCookie`）；
5. `exam_embed` **保留**（外观不变），但解耦：它只管摆窗，不再承担通信。

**风险与兜底**：端口只绑 127.0.0.1 + 随机；CDP 连不上（老系统 / 端口被占）时自动退回现在的实现。**这是一次纯增益替换，可以灰度**。

### E. 人工闸门 human_gate（1 天）

**现状**：验证码 / 人脸 / 平台闸门，程序要么不知道，要么只说一句「请你在窗口里完成」。

**改成**（照抄 ocsjs 的模式，见下）：

```python
human_gate(kind, tip, timeout=600, resume_when=lambda: not exists(sel))
```

- 进入时：置顶窗口 + 界面横幅 + 系统通知（**15 秒节流**，别刷屏）+ 日志一条；
- 期间：按钮变「我已完成，继续 / 放弃本次」；
- 条件消失：自动续跑，横幅自动收起；
- 超时：不是「失败」，而是「还在等你，要放弃吗」。

这条做完，「等验证码」和「卡死」在用户眼里就彻底分开了。

---

## 四、从 ocsjs 借的六条

| # | ocsjs 怎么做 | 我们怎么用 |
|---|---|---|
| 1 | **不开新页，附着平台导航**：优先调平台自己的 `PCount.next()` / `vue.preNext()`，URL 跳转只作兜底（注释写明"用 URL 跳学习记录会不一致"） | 考场入口已经用平台 URL，但**进场判定**要跟着平台走（DOM 优先），不要自己猜 URL 参数 |
| 2 | **统一 `waitFor(pred, {timeout, period, message})`**，不传 timeout 就永不超时，让调用方显式选择 | 替掉散落的 `time.sleep` / `get_current_url` 轮询；我们现在的「420 次 × 1 秒」就是没有这个原语的后果 |
| 3 | **人工闸门**：元素在就挂起（不抛错、不退出），元素消失自动 resolve，通知只发一次 + 桌面端 15s 节流 | 就是上面的改造点 E |
| 4 | **任务级事件** `start / done / stop / continuate / close`，暂停不 kill 进程 | 我们现在"停止"= `kill_task_process()`。改成软暂停：子进程检查 `PAUSED` 事件，恢复不用重跑 |
| 5 | **声明式脚本 + 配置 schema**：`Project.create({domains, scripts})`，`@match` 由 domains 推导，配置项自带 `label/tag/options`，面板零手写 | 远期：把"超星"抽成一个适配器，配置用 `dataclass` 声明，Flask 把 schema 直接吐给前端渲染，设置页不再手写每个控件 |
| 6 | **配置命名空间 + 幂等迁移**：`namespace.key` 存储，只有用户改过的值才落盘，`migrateNamespace()` 幂等搬迁 | 我们 `config.ini` 改结构就丢设置。改成 `settings(namespace,key,value)` 单表 + 启动迁移 |

> 反向提醒：ocsjs **没有**通用任务队列，也没有工作流引擎——它靠"页面级钩子 + 任务级事件"两套状态机撑住。我们现在不需要引入队列，需要的是**把已有状态收敛成一处**。

---

## 五、分期

| 阶段 | 内容 | 工作量 | 风险 |
|---|---|---|---|
| **0 止血** | 发 3.8.9（考试页自动拉取）；A 登录事务化；B 同步失败提示；按钮状态机 | 1–2 天 | 低，纯增量 |
| **1 可观测** | C 阶段打点 + DOM 判据 + 分层超时 + 错误文案统一 | 2 天 | 低 |
| **2 解耦** | D 接 CDP（灰度 + 自动回退），退役 `_watch_exam_browser`，`sync_cookies` 改 `Network.setCookie` | 3–5 天 | 中，建议先做开关，跑一周再默认开 |
| **3 闭环** | E 人工闸门；会话状态机 `/api/session`；软暂停 | 2–3 天 | 中 |
| **4 架构** | 声明式平台适配器 + 配置 schema + settings 表迁移 | 1–2 周 | 低（但收益在扩展性，不急） |

**建议顺序**：0 → 1 → 3 → 2 → 4。先把「说得清」做完，再动「怎么开窗」。因为阶段 0/1 就能消掉用户 80% 的抱怨，而 D 是给后面所有平台适配打地基的。

---

## 六、明确不做

- 不破解图形验证码、不绕人脸、不改平台签名（和 README 里的边界一致）；
- 不为了"看起来能进"去伪造 UA 骗过「禁止低版本APP」——那是平台风控，识别出来提示用户即可；
- 不在阶段 0/1 动 `exam_embed` 的 Win32 逻辑（它虽然脆，但现在是能用的，先别拆）。