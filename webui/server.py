"""超星 AI 答题控制台 —— 本地 Web 界面。

只负责三件事：写 config.ini、以子进程方式运行 main.py、把日志回传给浏览器。
不改动刷课主流程的业务逻辑，界面崩了也不会影响已启动的任务进程。
"""

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from configparser import ConfigParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import Flask, jsonify, render_template, request

from webui import secret

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent
PYTHON = Path(sys.executable) if FROZEN else ROOT / ".venv" / "Scripts" / "python.exe"
CONFIG_PATH = ROOT / "config.ini"
SECRET_PATH = ROOT / "secret.dat"
RUNTIME_CONFIG = ROOT / ".runtime-config.ini"
DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SEP = "\x1f"
CH_RE = re.compile(r"@@CH\|(.*?)@@(.*)$")
TQDM_RE = re.compile(r"(\d+)%\|[^|]*\|\s*([\d.]+[kKmM]?)/([\d.]+[kKmM]?)")
LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| (\w+)\s*\| [^|]+? - (.*)$")
# 纯内部调试噪音, 对使用者没有信息量
NOISE_RE = re.compile(r"^(unfinished task|Progress logged|Task success|videoEl未加载)")
EVENT_PREFIX = "@@EVENT@@"
MAX_RUN_HOURS = 6
MAX_JOBS = 4
_CHAPTER_DEFAULT = {"name": "", "course": "", "pct": 0, "state": "pending",
                    "done": 0, "total": 0, "job": "", "log_count": 0,
                    "video_pct": 0, "video_cur": "", "video_total": ""}


def _template_path() -> Path:
    for candidate in (
        ROOT / "config_template.ini",
        Path(getattr(sys, "_MEIPASS", ROOT)) / "config_template.ini",
    ):
        if candidate.exists():
            return candidate
    return ROOT / "config_template.ini"

app = Flask(
    __name__,
    template_folder=str(Path(__file__).resolve().parent / "templates"),
)

# 界面令牌：这个服务监听 127.0.0.1 却没有登录，而它的接口能把配置改掉、
# 把题库删空、还能下载并重启「更新包」。没有这道闸，用户在浏览器里打开的
# 任意网页都能对着 5000 端口发 POST。令牌随进程生成，注入页面由前端带着走。
UI_TOKEN = os.environ.get("CX_UI_TOKEN") or secrets.token_urlsafe(18)


@app.before_request
def _guard_ui_token():
    if not (request.path or "").startswith("/api/"):
        return None
    if request.headers.get("X-Token") == UI_TOKEN:
        return None
    return jsonify(ok=False, error="未授权的调用：缺少界面令牌"), 403

_lock = threading.Lock()


# 拉考试串行化：共用 requests.Session 时不允许多轮同时跑
_exam_pull_lock = threading.Lock()


def _run_with_deadline(work, seconds: float):
    """把可能卡住的活放到子线程里做，超时就报错。

    登录、拉课程、读窗口 cookie 这几步都要连外网或跟 WebView2 打交道，
    一旦对端不响应，界面就会一直转圈。宁可回一句「请稍后重试」，
    也不能让用户干等。
    """
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = work()
        except Exception as exc:  # noqa: BLE001 - 原样抛回调用方
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"等了 {int(seconds)} 秒还没有结果，请稍后重试")
    if "error" in box:
        raise box["error"]
    return box.get("value")
# 更新进度：下载在后台线程跑，前端轮询这个状态画进度条
_update_state = {"stage": "idle", "percent": 0, "done": 0, "total": 0,
                 "message": "", "error": "", "version": ""}
# 检查更新时服务端自己发现的版本。下载接口只认这一份，
# 免得任意 URL 都能被塞进来当「更新包」装上。
_pending_update = {"url": "", "sha256": "", "version": ""}

# 内嵌考试浏览器：由桌面入口注入窗口对象，这里负责盯着它的地址栏
_exam_browser = {"window": None, "url": "", "enc": "", "answer_id": "", "exam_id": "",
                 # 清空状态后的一小段时间内，地址栏里还是上一场的 URL，
                 # 后台观察线程会把旧 enc 写回来 → 前端立刻开始答上一场。
                 "ignore_until": 0.0}
# 退出钩子：桌面入口注入，更新要重启时用它走正常的退出流程
_quit_hook = {"fn": None}
_state = {
    "proc": None,
    "starting": False,
    "logs": deque(maxlen=6000),
    "chapters": {},
    "seq": 0,
    "status": "idle",
    "exit_code": None,
    "started_at": None,
    "stopping": False,
    "exam": {
        "status": "idle",      # idle / running / waiting_face / done / error
        "name": "",
        "answered": 0,
        "submitted": False,    # 恒为 False：交卷由使用者手动完成，这里只保留字段兼容前端
        "error": "",
        "face_ok": False,
        "driving": False,
    },
    "agent": {
        "running": False,
        "error": "",
        "result": None,
        "finished_at": 0,
    },
}


def _write_config(cfg: dict, path: Path | None = None, with_secrets: bool = False) -> None:
    parser = ConfigParser(interpolation=None)
    template = _template_path()
    if template.exists():
        parser.read(template, encoding="utf-8")
    for section in ("common", "tiku", "notification", "share", "search", "agent"):
        if section not in parser:
            parser[section] = {}
    parser["common"].update(
        {
            "use_cookies": "true",
            "username": str(cfg.get("username", "")).strip(),
            "password": str(cfg.get("password", "")) if with_secrets else "",
            "course_list": ",".join(str(x) for x in (cfg.get("course_list") or [])),
            "speed": str(cfg.get("speed", 1)),
            "jobs": str(max(1, min(MAX_JOBS, int(cfg.get("jobs", 2) or 2)))),
            "notopen_action": cfg.get("notopen_action", "retry"),
            "retry_interval": "1.0",
            "work_redo_enabled": "true" if cfg.get("work_redo_enabled") else "false",
            "work_max_retries": str(cfg.get("work_max_retries", 3)),
            "add_learning_count": "false",
            "target_count": "100",
            "target_accuracy": str(max(60, min(100, int(cfg.get("target_accuracy", 100) or 100)))),
            "exam_answer_gap": str(max(0.0, min(30.0, float(cfg.get("exam_answer_gap", 3) or 0)))),
            "exam_conf_gate": str(max(0.5, min(0.95, float(cfg.get("exam_conf_gate", 0.78) or 0.78)))),
        }
    )
    parser["tiku"].update(
        {
            "provider": "AI",
            "check_llm_connection": "false",
            "submit": "true" if cfg.get("submit", True) else "false",
            "cover_rate": str(cfg.get("cover_rate", 0.9)),
            "delay": "0",
            "endpoint": _endpoint(cfg),
            "key": str(cfg.get("api_key", "")).strip() if with_secrets else "",
            "model": str(cfg.get("model", "deepseek-chat")).strip(),
            "vision_model": str(cfg.get("vision_model", "")).strip(),
            "reasoning_effort": str(cfg.get("reasoning_effort", "")).strip(),
            "min_interval_seconds": str(cfg.get("min_interval", 2)),
            "http_proxy": str(cfg.get("http_proxy", "")).strip(),
        }
    )
    parser["notification"]["provider"] = ""
    parser["share"].update({"enabled": "true"})
    parser["search"].update(
        {
            "enabled": "true" if cfg.get("search_enabled", True) else "false",
            "url": str(cfg.get("search_url", "")).strip(),
            "key": str(cfg.get("search_key", "")).strip() if with_secrets else "",
        }
    )
    parser["agent"].update(
        {
            "url": str(cfg.get("agent_url", "")).strip(),
            "model": str(cfg.get("agent_model", "")).strip(),
            "key": str(cfg.get("agent_key", "")).strip() if with_secrets else "",
        }
    )
    target = path or CONFIG_PATH
    with open(target, "w", encoding="utf-8") as fp:
        parser.write(fp)


def _app_version() -> str:
    try:
        from api.updater import current_version

        return current_version()
    except Exception:
        return ""


def _num(value, default, cast=float):
    """config.ini 被手工改成非数字时不要让整个界面挂掉。"""
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _read_config() -> dict:
    if not CONFIG_PATH.exists():
        # 首次运行还没有配置文件时，也要把版本等基础信息给前端
        return {"version": _app_version(), "share_enabled": True,
                "password_saved": False, "has_key": False, "course_list": []}
    parser = ConfigParser(interpolation=None)
    parser.read(CONFIG_PATH, encoding="utf-8")
    common = parser["common"] if "common" in parser else {}
    tiku = parser["tiku"] if "tiku" in parser else {}
    search = parser["search"] if "search" in parser else {}
    agent = parser["agent"] if "agent" in parser else {}
    saved_key = _saved_secrets().get("api_key", "")
    return {
        "username": common.get("username", ""),
        "password_saved": bool(_saved_secrets().get("password")),
        "course_list": [x for x in (common.get("course_list", "") or "").split(",") if x],
        "speed": _num(common.get("speed"), 1.0),
        "jobs": _num(common.get("jobs"), 4, int),
        "notopen_action": common.get("notopen_action", "retry"),
        "work_redo_enabled": common.get("work_redo_enabled", "false").lower() == "true",
        "min_interval": _num(tiku.get("min_interval_seconds"), 2, int),
        "target_accuracy": _num(common.get("target_accuracy"), 100, int),
        "exam_answer_gap": _num(common.get("exam_answer_gap"), 3.0),
        "exam_conf_gate": _num(common.get("exam_conf_gate"), 0.78),
        "submit": tiku.get("submit", "true").lower() == "true",
        "model": tiku.get("model", "deepseek-chat"),
        "vision_model": tiku.get("vision_model", ""),
        "reasoning_effort": tiku.get("reasoning_effort", ""),
        "endpoint": tiku.get("endpoint", DEEPSEEK_ENDPOINT),
        "api_key_tail": saved_key[-4:] if len(saved_key) >= 4 else "",
        "has_key": bool(saved_key),
        "search_url": search.get("url", ""),
        "search_enabled": search.get("enabled", "true").lower() == "true",
        "search_key_saved": bool(_saved_secrets().get("search_key")),
        "agent_url": agent.get("url", ""),
        "agent_model": agent.get("model", ""),
        "agent_key_saved": bool(_saved_secrets().get("agent_key")),
        "share_enabled": True,
        "version": _app_version(),
    }


def _save_secret(password: str, api_key: str,
                 search_key: str | None = None,
                 agent_key: str | None = None) -> None:
    old = {}
    if SECRET_PATH.exists():
        try:
            old = json.loads(SECRET_PATH.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    payload = {
        "password": secret.protect(password) if password else "",
        "api_key": secret.protect(api_key) if api_key else "",
    }
    # 传 None 表示「这次没改这个 Key」，保留原值；传空串才是真的要清掉
    for name, value in (("search_key", search_key), ("agent_key", agent_key)):
        if value is None:
            payload[name] = old.get(name, "")
        else:
            payload[name] = secret.protect(value) if value else ""
    SECRET_PATH.write_text(json.dumps(payload), encoding="utf-8")


def _saved_secrets() -> dict:
    """读取本机加密保存的密码和 Key；没有加密文件时从旧版明文配置迁移一次。"""
    if SECRET_PATH.exists():
        try:
            data = json.loads(SECRET_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return {
            "password": secret.unprotect(data.get("password", "")),
            "api_key": secret.unprotect(data.get("api_key", "")),
            "search_key": secret.unprotect(data.get("search_key", "")),
            "agent_key": secret.unprotect(data.get("agent_key", "")),
        }

    if CONFIG_PATH.exists():
        parser = ConfigParser(interpolation=None)
        parser.read(CONFIG_PATH, encoding="utf-8")
        common = parser["common"] if "common" in parser else {}
        tiku = parser["tiku"] if "tiku" in parser else {}
        pwd = common.get("password", "") or ""
        key = tiku.get("key", "") or ""
        if pwd or key:
            _save_secret(pwd, key)
            return {"password": pwd, "api_key": key,
                    "search_key": "", "agent_key": ""}
    return {}


def _fill_secrets(cfg: dict) -> dict:
    """界面留空的密码/Key 沿用上次保存的值，避免每次运行都要重填。"""
    saved = _saved_secrets()
    if not str(cfg.get("password", "")):
        cfg["password"] = saved.get("password", "")
    if not str(cfg.get("api_key", "")).strip():
        cfg["api_key"] = saved.get("api_key", "")
    if not str(cfg.get("search_key", "")).strip():
        cfg["search_key"] = saved.get("search_key", "")
    if not str(cfg.get("agent_key", "")).strip():
        cfg["agent_key"] = saved.get("agent_key", "")
    return cfg


def _endpoint(cfg: dict) -> str:
    value = str(cfg.get("endpoint") or DEEPSEEK_ENDPOINT).strip().rstrip("/")
    return value or DEEPSEEK_ENDPOINT


def _key_of(course: str, chapter: str) -> str:
    return "%s%s%s" % (course, SEP, chapter) if course else chapter


# ---------- 内嵌考试浏览器 ----------
def set_exam_window(window) -> None:
    """桌面入口创建好第二个窗口后交过来。"""
    _exam_browser["window"] = window


def _exam_stage():
    """拿考场窗口的嵌入控制器；拿不到就返回 None。

    以前这里直接写 `exam_embed.get(...)`，但 server.py 从没导入过 exam_embed，
    每次都在 try 里抛 NameError 被吞掉 —— 表现为「登录窗口一直弹成独立窗口」。
    统一走这个函数，失败也不再是静默的。
    """
    try:
        from api import exam_embed

        return exam_embed.get("exam")
    except Exception as exc:  # 非 Windows / 模块缺失
        _append_log(f"嵌入控制器不可用：{exc}", kind="ui")
        return None


def set_quit_hook(fn) -> None:
    """桌面入口把「按正常流程退出」的函数交过来。"""
    _quit_hook["fn"] = fn


def quit_for_update() -> None:
    """更新前自己退出，而不是等更新脚本 Force 硬杀。

    硬杀会让 msedgewebview2 变成孤儿，占着 WebView2 的配置目录不放手，
    下次启动就得排队等它 —— 表现就是开头几秒黑屏。这里走正常退出：
    先关考场窗口，再让桌面入口的 quit_app 关主窗口，进程自然结束。
    """

    def run() -> None:
        # 先让 /api/update/restart 的响应发出去，再动窗口
        time.sleep(0.6)
        _append_log("准备退出以安装更新", kind="ui")
        try:
            win = _exam_browser.get("window")
            if win is not None:
                from api import exam_embed

                exam_embed.detach()
                win.destroy()
        except Exception:
            pass
        fn = _quit_hook.get("fn")
        if fn is not None:
            try:
                fn()
            except Exception:
                pass
        # 窗口关完 webview.start() 会返回、进程自己退；这里是兜底
        time.sleep(3.0)
        os._exit(0)

    threading.Thread(target=run, daemon=True).start()


def _watch_exam_browser() -> None:
    """盯着内嵌浏览器的地址栏。

    进考场必须过图形验证码，这一步交给真浏览器；程序只从地址里取 enc 等参数，
    拿到就能直接逐题答题了。
    """
    while True:
        time.sleep(1)
        win = _exam_browser.get("window")
        if win is None:
            continue
        try:
            url = win.get_current_url() or ""
        except Exception:
            continue
        query = parse_qs(urlparse(url).query)
        enc = (query.get("enc") or [""])[0]
        answer_id = (query.get("id") or [""])[0]
        exam_id = (query.get("tId") or [""])[0]
        with _lock:
            if time.time() < _exam_browser.get("ignore_until", 0):
                continue
            _exam_browser["url"] = url
            if enc:
                _exam_browser["enc"] = enc
                _exam_browser["answer_id"] = answer_id
                _exam_browser["exam_id"] = exam_id


threading.Thread(target=_watch_exam_browser, daemon=True).start()


@app.post("/api/exam/browser")
def api_exam_browser_open():
    """在内嵌浏览器里打开考试入口（验证码和签名都交给浏览器）。"""
    data = request.get_json(force=True, silent=True) or {}
    url = str(data.get("url") or "").strip()
    win = _exam_browser.get("window")
    if win is None:
        return jsonify(ok=False,
                       error="内嵌浏览器不可用，请在浏览器里手动进入考试后点「接管」"), 400
    with _lock:
        _exam_browser.update(url="", enc="", answer_id="", exam_id="",
                             ignore_until=time.time() + 5.0)
    # 用户可能刚在设置里登录过，先把最新的 cookie 推进浏览器再导航
    cookie_count = 0
    try:
        from api import browser_unlock

        # 请求线程不能裸等 WebView2：sync_cookies 内部是阻塞的 form.Invoke，
        # UI 线程一忙就会把 Flask 工作线程一起拖死，最后整个界面都不回。
        # ignore 窗口同步拉长到覆盖这段等待：否则观察线程在 5 秒后就会把
        # 导航前的旧考试 URL（带旧 enc）写回来，前端拿到旧 enc 立即误开作答。
        cookie_count = _run_with_deadline(
            lambda: browser_unlock.sync_cookies(win, "考场浏览器", force=True), 15)
        with _lock:
            _exam_browser["ignore_until"] = time.time() + 5.0
    except TimeoutError:
        return jsonify(ok=False, error="考场窗口 15 秒没有响应，请关掉考场再重新打开"), 504
    except Exception as exc:
        _append_log(f"同步登录态失败：{exc}", kind="ui")

    # 本地明明有登录态，却一条都没推进 WebView2：这时候导航过去只会落在登录页，
    # 用户看到的就是「考场打不开 / 登录不进去」。直接说清楚，别让他干等。
    cookie_file = ROOT / "cookies.txt"
    has_cookie_file = cookie_file.exists() and cookie_file.stat().st_size > 0
    if cookie_count == 0 and has_cookie_file:
        return jsonify(ok=False, error=(
            "登录态没能送进考场窗口（窗口可能还没初始化好）。"
            "请点「登录学习通」重新登录一次，或展开「打开其他页面」手动进入考场")), 502
    if cookie_count > 0:
        _append_log(f"已把 {cookie_count} 条登录态推进考场窗口", kind="ui")

    try:
        _run_with_deadline(lambda: win.load_url(url or "https://mooc1.chaoxing.com/"), 10)
    except TimeoutError:
        return jsonify(ok=False, error="导航超时：窗口可能已经无响应，建议关掉考场重开"), 504
    except Exception as exc:
        return jsonify(ok=False, error=f"打不开浏览器窗口：{exc}"), 500
    # 导航已经发出去：从现在起观察线程看到的 URL 才算数。再给 5 秒忽略窗，
    # 避免把导航前地址栏里残留的旧考试地址（带旧 enc）当成新状态写回来。
    with _lock:
        _exam_browser.update(url="", enc="", answer_id="", exam_id="",
                             ignore_until=time.time() + 5.0)
    # 嵌进页面里时不主动 show：那个窗口现在是主窗口的子窗口，
    # 先显示会以 0,0 贴在左上角盖住标题栏，等前端把区域坐标报上来再摆。
    try:
        from api import exam_embed

        placed = exam_embed.set_active(True)
    except Exception:
        placed = False
    if not placed:
        try:
            win.show()
        except Exception:
            pass
    return jsonify(ok=True, embedded=placed,
                   message="已打开考试窗口，请在里面进入考场")


@app.post("/api/exam/rect")
def api_exam_rect():
    """前端上报考场区域在页面里的位置（CSS 像素），用于摆放嵌入的窗口。"""
    data = request.get_json(force=True, silent=True) or {}
    try:
        from api import exam_embed

        exam_embed.set_rect(data.get("box"))
    except Exception:
        return jsonify(ok=False), 200
    return jsonify(ok=True)


@app.post("/api/exam/browser/close")
def api_exam_browser_close():
    """收起下方的考场页面。嵌进去的窗口没有标题栏，得给个出口。"""
    win = _exam_browser.get("window")
    try:
        from api import exam_embed

        exam_embed.set_active(False)
    except Exception:
        pass
    if win is not None:
        try:
            _run_with_deadline(lambda: win.load_url("about:blank"), 8)
        except Exception:
            pass
    with _lock:
        _exam_browser.update(url="", enc="", answer_id="", exam_id="",
                             ignore_until=time.time() + 5.0)
    # 用户点「登录学习通」后直接收起窗口（放弃登录）时，登录态还躺在 .bak 里。
    # 不搬回来，界面就永远显示未登录。live 存在时这个函数不会动任何东西。
    try:
        _rescue_login_backup()
    except Exception:
        pass
    return jsonify(ok=True)


@app.get("/api/exam/browser")
def api_exam_browser_state():
    with _lock:
        state = dict(url=_exam_browser["url"], enc=_exam_browser["enc"],
                     answer_id=_exam_browser["answer_id"],
                     exam_id=_exam_browser["exam_id"])
    try:
        from api import exam_embed

        state["active"] = bool(exam_embed.embedded()) and exam_embed.is_active()
    except Exception:
        state["active"] = False
    return jsonify(ok=True, **state)


@app.post("/api/exam/autodrive")
def api_exam_autodrive():
    """在考试窗口里自动作答，按答题卡复查补漏。"""
    win = _exam_browser.get("window")
    if win is None:
        return jsonify(ok=False, error="内嵌浏览器不可用"), 400
    with _lock:
        if _state["exam"].get("driving"):
            return jsonify(ok=False, error="已经在自动作答了"), 409
        if _state["proc"] is not None:
            return jsonify(ok=False, error="刷课任务正在运行，请先停止再开始考试"), 409
        # /api/start 从置 starting 到真正 Popen 之间还有几十秒的核对与写盘，
        # 这期间 proc 仍是 None——不挡这一条就能边刷课边答题，两边互踩。
        if _state.get("starting"):
            return jsonify(ok=False, error="刷课任务正在启动，等它开始后再考试"), 409
        _state["exam"].update(driving=True, status="running", answered=0, error="",
                              submitted=False, face_ok=False,
                              gen=_state["exam"].get("gen", 0) + 1)
        gen = _state["exam"]["gen"]

    data = request.get_json(force=True, silent=True) or {}
    course = str(data.get("course") or "").strip()
    worker = threading.Thread(target=_drive_exam, args=(win, course), daemon=True)
    with _lock:
        _state["exam"]["worker"] = worker
    worker.start()
    # 看门狗：答题线程万一卡在原生弹窗上，finally 永远不执行，driving 会一直是
    # True，之后每次点考试都 409「已经在自动作答了」，只能重启程序。
    threading.Thread(target=_exam_watchdog, args=(gen,), daemon=True).start()
    return jsonify(ok=True, message="已开始自动作答")


def _exam_watchdog(gen: int, minutes: float = 35.0) -> None:
    """盯住答题线程。

    以前是固定睡 35 分钟然后无条件解锁。但 run_pass 会按题数把内部时限放宽
    （150 题 × 每题间隔 30s 时要一个多小时），固定 35 分钟会在答题进行到一半时
    解锁 driving，用户再点一次「开始」就变成两条 run_pass 同时驱动同一个
    WebView2。现在线程还活着就继续等，只在它真的死了或换场之后才收尾，
    外加一个 3 小时的绝对上限兜底。
    """
    deadline = time.time() + 3 * 3600
    while time.time() < deadline:
        time.sleep(30)
        with _lock:
            exam = _state["exam"]
            if exam.get("gen") != gen or not exam.get("driving"):
                return
            worker = exam.get("worker")
            if worker is None or not worker.is_alive():
                # 线程已经结束（正常或异常），finally 自己会收尾；这里兜底
                exam.update(driving=False, status="error",
                            error="自动作答线程已退出但状态未复位，已解锁")
                _append_log("自动作答线程退出但状态没复位，已强制解锁", kind="ui")
                return


def _drive_exam(win, course: str = "") -> None:
    def log(msg: str) -> None:
        _append_log(msg)

    # 放在 try 外面：import 失败时 except 里也要用到它，否则会抛 UnboundLocalError
    # 把真正的错误吞掉。
    started = time.time()
    try:
        # import 放在 try 里面：模块缺失时线程会静默死掉，
        # driving 一直为 True，之后再点考试全是 409。
        from api.browser_exam import run_pass

        log("开始在考试窗口里自动作答")
        stats = run_pass(win, log, course=course)
        elapsed = int(time.time() - started)
        with _lock:
            _state["exam"].update(answered=stats.get("ok", 0),
                                  submitted=bool(stats.get("submitted")))
        log(f"自动作答结束（{elapsed} 秒）：作答 {stats.get('ok', 0)} 题，"
            f"未完成 {stats.get('fail', 0)} 题，剩余未答 {stats.get('left', 0)} 题")
        if stats.get("error"):
            # 一开始就没读到卷面，别让「剩余 0 题」看起来像全做完了。
            # 同时把错误落到状态里，前端才不会把它显示成「未交卷」。
            with _lock:
                _state["exam"]["error"] = str(stats["error"])
            log(f"自动作答没能开始：{stats['error']}")
        elif stats.get("ready") or stats.get("left", 0) == 0:
            log("全部题目已作答，请在考场窗口里检查后手动交卷")
        else:
            log(f"还有 {stats.get('left', 0)} 题没答完，请到考场窗口补齐后再交卷")
    except Exception as exc:
        with _lock:
            _state["exam"].update(error=f"{type(exc).__name__}: {exc}")
        log(f"自动作答中断：{type(exc).__name__}: {exc}")
        try:
            from api.feedback import push_error

            push_error("exam.autodrive", exc, f"elapsed={int(time.time() - started)}s")
        except Exception:
            pass
    finally:
        with _lock:
            # 出错时不能把状态改回 done：前端靠 status 区分「没交卷」和
            # 「中途出错」，一律写 done 会把异常说成正常结束。
            failed = bool(_state["exam"].get("error"))
            _state["exam"].update(driving=False,
                                  status="error" if failed else "done")



def _append_log(text: str, key: str = "", course: str = "", chapter: str = "",
                kind: str = "", level: str = "") -> None:
    with _lock:
        if kind == "progress" and key:
            # 每章节只保留最新一条进度, 否则每秒一条会把真正的日志挤出去
            for old in [e for e in _state["logs"]
                        if e.get("kind") == "progress" and e.get("ch") == key]:
                try:
                    _state["logs"].remove(old)
                except ValueError:
                    pass
        _state["seq"] += 1
        entry = {"n": _state["seq"], "t": time.strftime("%H:%M:%S"), "m": text, "ch": key}
        if kind:
            entry["kind"] = kind
        if level:
            entry["lv"] = level
        _state["logs"].append(entry)
        if key:
            info = _state["chapters"].setdefault(key, dict(_CHAPTER_DEFAULT))
            info["name"] = chapter
            info["course"] = course
            info["log_count"] += 1


def _apply_event(data: dict) -> None:
    kind = data.get("type")
    if kind not in ("chapter", "video_confirm"):
        return
    name = str(data.get("chapter") or "未命名章节")
    course = str(data.get("course") or "")
    key = _key_of(course, name)
    with _lock:
        info = _state["chapters"].setdefault(key, dict(_CHAPTER_DEFAULT))
        info["name"] = name
        info["course"] = course
        if kind == "chapter":
            if data.get("state"):
                info["state"] = str(data["state"])
            for key in ("pct", "done", "total"):
                if key in data:
                    info[key] = int(data[key])
            if data.get("job"):
                info["job"] = str(data["job"])
            if data.get("state") in ("done", "error", "notopen"):
                info["video_pct"] = 0
                info["video_cur"] = ""
                info["video_total"] = ""
        else:
            # 只在还没到 100% 时补满, 避免进度条来回跳
            if info["pct"] < 100:
                info["pct"] = 100
            info["state"] = "confirming"
            if data.get("name"):
                info["job"] = str(data["name"])


def _pump(proc: subprocess.Popen) -> None:
    decoder = json.JSONDecoder()
    try:
        for raw in proc.stdout:
            for part in raw.replace("\r", "\n").split("\n"):
                line = ANSI_RE.sub("", part).strip()
                if not line:
                    continue

                # 事件行可能与日志粘在同一行（stdout 与 loguru 异步 sink 交错输出），
                # 所以不能只判断行首，要按标记切开分别处理
                event_raw = ""
                if EVENT_PREFIX in line:
                    head, _, event_raw = line.partition(EVENT_PREFIX)
                    line = head.strip()

                course = ""
                chapter = ""
                matched = CH_RE.search(line)
                if matched:
                    head = matched.group(1)
                    course, _, chapter = head.partition(SEP)
                    # 只摘掉标记本身, 后面的消息必须保留
                    line = (line[:matched.start()] + matched.group(2)).strip()

                key = _key_of(course, chapter)
                if line:
                    if "%|" in line:
                        # tqdm 进度条：更新章节的视频进度，并在日志里原地刷新一行
                        prog = TQDM_RE.search(line)
                        if prog:
                            pct, cur, total = prog.group(1), prog.group(2), prog.group(3)
                            with _lock:
                                info = _state["chapters"].get(key)
                                if info is not None:
                                    info["video_pct"] = int(pct)
                                    info["video_cur"] = cur
                                    info["video_total"] = total
                            # 原样保留 tqdm 进度条: 任务名、进度块、当前/总时长、速率都在里面
                            _append_log(line, key, course, chapter, kind="progress")
                    else:
                        level = ""
                        parsed = LOG_LINE_RE.match(line)
                        if parsed:
                            level = parsed.group(1).upper()
                            line = parsed.group(2).strip()
                        if line and not NOISE_RE.match(line):
                            _append_log(line, key, course, chapter, level=level)

                if event_raw:
                    # 只解析第一个 JSON 对象，忽略其后可能粘上的内容
                    try:
                        payload, _ = decoder.raw_decode(event_raw.strip())
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict):
                        _apply_event(payload)
    finally:
        code = proc.wait()
        _append_log(f"任务进程已结束，退出码 {code}",
                    kind="ui", level="err" if code else "")
        with _lock:
            if _state["proc"] is proc:
                _state["proc"] = None
                _state["exit_code"] = code
                if _state.get("stopping"):
                    _state["status"] = "stopped"
                    _state["stopping"] = False
                else:
                    _state["status"] = "finished" if code == 0 else "error"


def _validate(cfg: dict) -> str | None:
    if not str(cfg.get("username", "")).strip():
        return "请填写手机号"
    if not str(cfg.get("password", "")):
        return "请填写密码"
    if not str(cfg.get("api_key", "")).strip():
        return "请填写 DeepSeek API Key"
    if not FROZEN and not (ROOT / "main.py").exists():
        return "找不到 main.py"
    if not PYTHON.exists():
        return "找不到 .venv 解释器，请先创建虚拟环境"
    return None


@app.get("/")
def index():
    return render_template("index.html", ui_token=UI_TOKEN)


@app.get("/api/config")
def api_get_config():
    return jsonify(_read_config())


@app.post("/api/config")
def api_save_config():
    cfg = _fill_secrets(request.get_json(force=True, silent=True) or {})
    err = _validate(cfg)
    if err:
        return jsonify(ok=False, error=err), 400
    _save_secret(cfg.get("password", ""), cfg.get("api_key", ""),
                 cfg.get("search_key"), cfg.get("agent_key"))
    _write_config(cfg)
    note = "密码与 Key 已加密保存在本机" if secret.available() else "（当前系统不支持加密，密码以原文保存）"
    return jsonify(ok=True, message="配置已保存，" + note)


@app.post("/api/test-search")
def api_test_search():
    """试一次联网搜索，验地址和 Key 通不通。"""
    data = request.get_json(force=True, silent=True) or {}
    query = str(data.get("query") or "人工智能发展史").strip()
    conf = {
        "enabled": True,
        "url": str(data.get("search_url") or "").strip(),
        "key": str(data.get("search_key") or "").strip(),
    }
    if not conf["key"]:
        conf["key"] = _saved_secrets().get("search_key", "")
    if not (conf["url"] and conf["key"]):
        return jsonify(ok=False, error="请先填写搜索地址和 Key"), 400
    try:
        from api import websearch

        results = websearch.search(query, 3, conf=conf)
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500
    if not results:
        return jsonify(ok=False,
                       error="没搜到结果。检查地址是否要填完整路径、Key 是否正确"), 400
    return jsonify(ok=True, results=results,
                   message=f"搜索正常，返回 {len(results)} 条")


@app.post("/api/test-llm")
def api_test_llm():
    cfg = _fill_secrets(request.get_json(force=True, silent=True) or {})
    key = str(cfg.get("api_key", "")).strip()
    model = str(cfg.get("model", "deepseek-chat")).strip() or "deepseek-chat"
    if not key:
        return jsonify(ok=False, error="请先填写 API Key"), 400
    try:
        from openai import OpenAI

        client = OpenAI(base_url=_endpoint(cfg), api_key=key, timeout=30)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "1+1等于几？只回答数字"}],
            max_tokens=16,
        )
        reply = (resp.choices[0].message.content or "").strip()
        return jsonify(ok=True, reply=reply[:60], message=f"{model} 连接正常，返回：{reply[:20]}")
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}")


@app.post("/api/exams")
def api_exams():
    """列出所有课程的考试。

    不再用账号密码自动登录：登录交给用户在考试页的内嵌窗口里手动完成
    （账号密码自己输、真人验证自己过），这里只复用那份登录态。
    """
    if not (ROOT / "cookies.txt").exists():
        return jsonify(ok=False,
                       error="还没有登录：请点考试页的「登录学习通」，"
                             "在窗口里登录一次再来拉取"), 400
    try:
        return jsonify(_run_with_deadline(_pull_exams, 150))
    except TimeoutError as exc:
        return jsonify(ok=False, error=str(exc)), 504
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


def _run_child(flag: str, tag: str, timeout: float) -> dict:
    """在干净子进程里跑一件事，结果从临时文件读回来。

    主进程里有 WebView2 的消息循环，Python 线程常被挤住；同样的活放子进程里
    一两秒就完事。打包版是窗口程序没有 stdout，所以用文件交接。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # 固定文件名会被并发子进程互相覆盖：/api/login/collect 和 /api/start 都会调
    # --whoami，两个子进程写同一个文件时，读的一方可能读到别人的结果甚至半截 JSON。
    fd, out_name = tempfile.mkstemp(prefix=f"chaoxing_{tag}_", suffix=".json")
    os.close(fd)
    out_path = Path(out_name)
    try:
        out_path.unlink()      # 让子进程自己创建，避免读到 mkstemp 留下的空文件
    except OSError:
        pass
    if FROZEN:
        cmd = [sys.executable, flag, str(out_path)]
    else:
        # 源码运行时优先用项目自己的虚拟环境；没有就用当前解释器，
        # 免得开发者机器上少了 .venv 就整条链路报「找不到文件」
        python = str(PYTHON) if Path(PYTHON).exists() else sys.executable
        cmd = [python, str(ROOT / "desktop.py"), flag, str(out_path)]
    try:
        # subprocess.run 超时抛的是 TimeoutExpired，不是 TimeoutError；
        # 不转一道的话调用方的 except TimeoutError 接不住，用户看到的是
        # 500 加一段英文异常名。
        subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            out_path.unlink()
        except OSError:
            pass
        raise TimeoutError(f"子进程 {flag} 超过 {int(timeout)} 秒没有返回，请稍后重试") from exc
    if not out_path.exists():
        try:
            out_path.unlink()
        except OSError:
            pass
        raise RuntimeError("子进程没有写出结果，请重试")
    try:
        return json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass


def _whoami() -> dict:
    """当前 cookies.txt 属于哪个账号。"""
    return _run_child("--whoami", "whoami", 60)


def _pull_exams() -> dict:
    """读考试列表。"""
    if not _exam_pull_lock.acquire(timeout=15):
        raise TimeoutError("上一轮还在读考试列表，等几秒再试")
    try:
        return _run_child("--exam-list", "exam_list", 120)
    finally:
        _exam_pull_lock.release()


# 登录页：让用户在内嵌窗口里自己输账号密码、自己过验证码/人脸
LOGIN_URL = ("https://passport2.chaoxing.com/login"
             "?fid=&refer=https%3A%2F%2Fi.chaoxing.com")

# 登录是一次「有风险的操作」：要先清掉旧登录态才能换号。
# 清之前把文件挪成 .bak，万一登录没成，原样搬回来 —— 不能因为一次失败的登录
# 把用户本来能用的会话弄丢。
_pending_login_backup = {}


def _backup_login_state() -> dict:
    saved = {}
    for name in ("cookies.txt", "login.json"):
        path = ROOT / name
        if not path.exists():
            continue
        bak = ROOT / (name + ".bak")
        try:
            os.replace(path, bak)
            saved[name] = bak
        except OSError as exc:
            _append_log(f"备份 {name} 失败：{exc}", kind="ui")
    return saved


def _restore_login_state(saved: dict) -> bool:
    """把备份搬回原位。返回是否真的恢复了至少一份。"""
    restored = False
    for name, bak in (saved or {}).items():
        try:
            os.replace(bak, ROOT / name)
            restored = True
        except OSError as exc:
            _append_log(f"恢复 {name} 失败：{exc}", kind="ui")
    return restored


def _drop_login_backup(saved: dict) -> None:
    for bak in (saved or {}).values():
        try:
            bak.unlink()
        except OSError as exc:
            # 删不掉不能一声不吭：留着 .bak，下一次回滚就可能把新登录覆盖回旧账号
            _append_log(f"删除登录备份失败（{bak.name}）：{exc}", kind="ui")


def rescue_login_backup_on_quit() -> None:
    """程序退出时把滞留的登录备份搬回原位（live 存在时不动任何东西）。"""
    _rescue_login_backup()


# 服务启动时先救一次：上次点完「登录学习通」没登录就关了程序的话，
# cookies.txt 还躺在 .bak 里，不救的话界面一上来就是「未登录」。
try:
    _rescue_login_backup()
except Exception:
    pass


def _login_fail(message: str, code: int = 400):
    """登录没成功：把清掉的旧登录态还回去，再回一句人话。"""
    global _pending_login_backup
    with _login_lock:
        restored = _restore_login_state(_pending_login_backup)
        _pending_login_backup = {}
    suffix = "（你原来的登录态已恢复，可以直接刷课）" if restored else ""
    return jsonify(ok=False, error=message + suffix), code


# 登录流程互斥：备份文件的搬迁和 _pending_login_backup 的读改写必须串行，
# 否则连点两次「登录学习通」会互相覆盖，把用户原来的登录态弄丢。
_login_lock = threading.Lock()


def _rescue_login_backup() -> None:
    """上次在「打开登录页」和「我已登录」之间退出时，把 .bak 搬回原位。

    不做这一步，用户的登录态就永远躺在备份文件里，界面还显示未登录。
    """
    for name in ("cookies.txt", "login.json"):
        bak = ROOT / (name + ".bak")
        live = ROOT / name
        if bak.exists() and not live.exists():
            try:
                os.replace(bak, live)
                _append_log(f"已恢复上次中断的登录态（{name}）", kind="ui")
            except OSError:
                pass


@app.post("/api/login/open")
def api_login_open():
    """把考场窗口当登录窗口用，打开学习通登录页。

    程序不替用户填任何东西：验证码和人脸必须人来过，这里只负责把窗口打开。
    """
    win = _exam_browser.get("window")
    if win is None:
        return jsonify(ok=False, error="登录窗口不可用，请重启程序"), 400

    from api import browser_unlock

    # 换账号必须先清干净：本地 cookies.txt 和浏览器自己的 cookie 库
    # 任何一处留着旧账号，登录页就会直接以旧账号进去，用户以为换了其实没换。
    # 但清之前先备份，登录失败要能原样还原。
    global _pending_login_backup
    _rescue_login_backup()
    # 刷课子进程手里握着自己的一份会话，收尾时会把 cookie 写回文件。
    # 这时候换账号，等于让子进程用旧号覆盖新号，或者两边同时写出半截文件。
    with _lock:
        if _state["proc"] is not None:
            return jsonify(ok=False, error="刷课任务正在运行，请先停止再换账号"), 409
    # 连点两次「登录学习通」时，第一次已经把 cookies.txt 挪走了，第二次再备份
    # 只会拿到空字典，把第一次的备份变成孤儿 —— 登录失败就再也还原不回来。
    # 检查和备份放在同一把锁里，避免两个请求同时判断「还没有备份」。
    with _login_lock:
        if not _pending_login_backup:
            _pending_login_backup = _backup_login_state()
    try:
        _run_with_deadline(lambda: browser_unlock.clear_cookies(win), 20)
    except Exception as exc:
        _append_log(f"清空旧登录态失败（继续打开登录页）：{exc}", kind="ui")

    try:
        _run_with_deadline(lambda: win.load_url(LOGIN_URL), 10)
    except TimeoutError:
        # 回滚必须和备份、collect 的写盘共用 _login_lock：
        # 否则 collect 恰好写完新 cookie 时，这里的 os.replace 会把新登录
        # 覆盖回旧账号，串号就从这个窗口溜回来。
        with _login_lock:
            _restore_login_state(_pending_login_backup)
            _pending_login_backup = {}
        return jsonify(ok=False, error="登录页导航超时（你原来的登录态已恢复），请重试"), 504
    except Exception as exc:
        with _login_lock:
            _restore_login_state(_pending_login_backup)
            _pending_login_backup = {}
        return jsonify(ok=False, error=f"打不开登录页：{exc}（你原来的登录态已恢复）"), 500
    stage = _exam_stage()
    placed = bool(stage and stage.set_active(True))
    if not placed:
        try:
            win.show()
        except Exception:
            pass
    return jsonify(ok=True, embedded=placed,
                   message="登录窗口已打开：输账号密码、完成验证后点「我已登录」")


def _probe_login(cookies: dict) -> dict:
    """用一份独立会话验证这组 cookie 还有效。

    刻意不碰 SessionManager / Chaoxing：那两个是全局单例，登录流程一跑就会
    和正在进行的拉取互相等，界面就卡住了。
    """
    import requests

    if not (cookies.get("_uid") or cookies.get("UID")):
        return {"status": False, "msg": "cookie 里没有登录标识"}
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/118.0.0.0 Safari/537.36",
    })
    session.cookies.update(cookies)
    try:
        resp = session.post(
            "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata",
            data={"courseType": 1, "courseFolderId": 0, "query": "",
                  "superstarClass": 0},
            timeout=8,
        )
    except Exception as exc:
        return {"status": False, "msg": f"{type(exc).__name__}: {exc}"}
    if resp.status_code != 200:
        return {"status": False, "msg": f"平台返回 {resp.status_code}"}

    # 登录失效时平台往往回一个 200 的登录页，只看状态码会误判成功。
    # 只认强特征（登录表单/跳回 passport）。「用户登录」「请输入手机号」这类
    # 通用词不碰——课程列表正文里出现一次就会把有效登录判死，用户被锁进
    # 登录死循环。认不出强特征就当成功：宁可放过一次，不要错杀。
    body = (resp.text or "")[:4000]
    final_url = str(getattr(resp, "url", "") or "")
    if "passport2.chaoxing.com" in final_url:
        return {"status": False, "msg": "被跳转回登录页，登录态已失效"}
    for marker in ('name="fanyalogin"', 'id="loginName"', 'name="password"'):
        if marker in body:
            return {"status": False, "msg": "平台返回的是登录页，登录态已失效"}
    return {"status": True, "msg": "登录成功"}


@app.get("/api/login/who")
def api_login_who():
    """当前登录的是谁。读 login.json，够快，不进子进程。"""
    try:
        data = json.loads((ROOT / "login.json").read_text(encoding="utf-8"))
        if data.get("uid") or data.get("name"):
            return jsonify(ok=True, uid=str(data.get("uid") or ""),
                           name=str(data.get("name") or ""))
    except Exception:
        pass
    try:
        from api.cookies import use_cookies

        saved = use_cookies()
        if saved.get("_uid"):
            return jsonify(ok=True, uid=str(saved.get("_uid")), name="")
    except Exception:
        pass
    return jsonify(ok=False)


@app.post("/api/login/collect")
def api_login_collect():
    """用户点「我已登录」：把窗口里的登录态取出来存成 cookies.txt 并校验。"""
    win = _exam_browser.get("window")
    if win is None:
        return jsonify(ok=False, error="登录窗口不可用，请重启程序"), 400

    with _lock:
        if _state["proc"] is not None:
            return jsonify(ok=False, error="刷课任务正在运行，请先停止再换账号"), 409

    from api import browser_unlock

    try:
        cookies = _run_with_deadline(lambda: browser_unlock.read_cookies(win), 30)
    except TimeoutError as exc:
        return _login_fail(f"读取窗口登录状态超时：{exc}", 504)
    if not cookies:
        # 不能沿用旧登录态：那样会出现「界面看着是 B，跑起来用的是 A」。
        # 读不到就让用户重来，宁可多一次点击，也不要串号。
        return _login_fail("没读到窗口里的登录信息，请确认窗口里已登录成功后再点一次")
    if not (cookies.get("_uid") or cookies.get("UID")):
        return _login_fail("看起来还没登录成功：窗口里看不到登录后的标识")

    text = ";".join(f"{name}={value}" for name, value in cookies.items())
    # 先把窗口收起来：登录页一直在渲染会占满 GIL，后台的拉取会被拖慢几十倍
    stage = _exam_stage()
    if stage:
        stage.set_active(False)
    # 注意：这里先不落盘。校验通过之后才写，见下面 probe 之后那一段。

    # 之后统一走 cookie 登录：只改这一个键，配置里其他内容原样保留
    if CONFIG_PATH.exists():
        try:
            parser = ConfigParser(interpolation=None)
            parser.read(CONFIG_PATH, encoding="utf-8")
            if "common" not in parser:
                parser["common"] = {}
            parser["common"]["use_cookies"] = "true"
            with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
                parser.write(fp)
        except Exception as exc:
            _append_log(f"写入 use_cookies 失败：{exc}", kind="ui")

    # 校验登录态：只用一份独立会话发一个轻量请求，不走 Chaoxing/SessionManager。
    # 以前这里跑一整套 login(login_with_cookies=True)，会和正在跑的拉取互相等，
    # 表现为点「我已登录」之后界面一直没反应。
    try:
        state = _run_with_deadline(lambda: _probe_login(cookies), 30)
    except TimeoutError as exc:
        return _login_fail(f"校验登录状态超时：{exc}", 504)
    except Exception as exc:
        return _login_fail(f"校验登录状态失败：{exc}", 500)
    if not state.get("status"):
        return _login_fail(f"登录状态用不了：{state.get('msg')}")

    # 校验通过才落盘：先写临时文件再原子替换。以前是「先写再验」，
    # 第一次登录失败时会把一份无效 cookie 留在盘上，下次打开考场反而
    # 把用户刚在窗口里登录好的会话踢下线。
    tmp_path = ROOT / "cookies.txt.tmp"
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, ROOT / "cookies.txt")
    except OSError as exc:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return jsonify(ok=False, error=f"保存登录状态失败：{exc}"), 500
    try:
        from api.base import SessionManager

        SessionManager.get_instance()._session.cookies.update(cookies)
    except Exception:
        pass

    # 新登录态确认可用，旧备份可以删了
    global _pending_login_backup
    with _login_lock:
        _drop_login_backup(_pending_login_backup)
        _pending_login_backup = {}

    _append_log("已读取学习通登录状态", kind="ui")
    # 记下这份登录态属于谁：换账号或跑任务前用它核对，避免 A 的登录态跑 B 的活
    who: dict = {}
    try:
        who = _run_with_deadline(_whoami, 60)
    except Exception as exc:
        _append_log(f"查询登录账号失败：{exc}", kind="ui")
    if who.get("ok"):
        try:
            (ROOT / "login.json").write_text(
                json.dumps({"uid": who.get("uid", ""), "name": who.get("name", ""),
                            "at": time.time()}, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass
    # 登录完了就把窗口收起来，别挡着页面
    stage = _exam_stage()
    if stage:
        stage.set_active(False)
    name = str(who.get("name") or "")
    if name:
        return jsonify(ok=True,
                       message=f"已登录：{name}。现在可以拉取考试、开始刷课了")
    return jsonify(ok=True, message="登录状态已保存，现在可以拉取考试、开始刷课了")


@app.post("/api/exam/run")
def api_exam_run():
    """已废弃：HTTP 直连考试走不通（验证码 + 选项打乱 + 无效操作）。

    保留路由是为了给旧调用一个明确答复，不要让请求静默走进死路径。
    """
    return jsonify(ok=False,
                   error="这条通道已废弃：请用考试页的「开始」按钮，走内嵌浏览器作答"), 410


@app.post("/api/exam/face_done")
def api_exam_face_done():
    """使用者确认已自行完成人脸验证。"""
    with _lock:
        if _state["exam"]["status"] != "waiting_face":
            return jsonify(ok=False, error="当前不在等待人脸验证"), 400
        _state["exam"]["face_ok"] = True
    _append_log("已确认完成人脸验证，继续答题")
    return jsonify(ok=True)


@app.get("/api/exam/status")
def api_exam_status():
    with _lock:
        return jsonify(dict(_state["exam"]))


@app.post("/api/feedback/push")
def api_feedback_push():
    """前端提交反馈，或把最近的日志一并发上来。"""
    data = request.get_json(force=True, silent=True) or {}
    message = str(data.get("message", "")).strip()
    context = str(data.get("context", "")).strip()
    kind = str(data.get("kind", "feedback")).strip() or "feedback"
    module = str(data.get("module", "ui")).strip() or "ui"
    if not message and not context:
        return jsonify(ok=False, error="内容为空"), 400
    try:
        from api.feedback import push

        ok = push(kind, module, message, context)
        if not ok:
            return jsonify(ok=False, error="发送失败，请检查网络后重试"), 502
        return jsonify(ok=True, message="已发送，感谢反馈")
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.get("/api/update/check")
def api_update_check():
    try:
        from api.remote_library import _conf as share_conf
        from api.updater import changelog, check_update, current_version

        cfg = share_conf()
        result = check_update(cfg.get("endpoint", ""), cfg.get("token", ""))
        result["current"] = current_version()
        result.setdefault("current_changelog", changelog())
        if result.get("url"):
            _pending_update.update(url=str(result.get("url") or ""),
                                   sha256=str(result.get("sha256") or ""),
                                   version=str(result.get("version") or ""))
        # 服务端版本变了就把上次下载的包作废：留着它只会让「立即重启」装回旧版本
        try:
            from api.updater import clear_pending, load_pending

            pending = load_pending()
            latest = str(result.get("version") or "")
            if pending and latest and str(pending.get("version") or "") != latest:
                clear_pending()
        except Exception:
            pass
        return jsonify(result)
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.post("/api/update/apply")
def api_update_apply():
    """启动更新。立刻返回，进度由 /api/update/progress 查询。"""
    data = request.get_json(force=True, silent=True) or {}
    url = str(data.get("url", "")).strip()
    sha = str(data.get("sha256", "")).strip()
    # 版本号只认「检查更新」时服务端给的那一份，不能信请求体：
    # /api/update/restart 用它判断「新包是否比当前版本新」，客户端伪造一个
    # version=9999 就能绕过这道检查。
    version = str(_pending_update.get("version") or "").strip()
    if not url:
        return jsonify(ok=False, error="缺少下载地址"), 400
    # 只装「检查更新」发现的那个版本：地址和校验值都由服务端自己存过一份，
    # 客户端传什么都不能构成新的下载目标。
    if not _pending_update["url"] or url != _pending_update["url"]:
        return jsonify(ok=False, error="没有可安装的版本，请先点「检查更新」"), 400
    sha = sha or _pending_update["sha256"]
    if _pending_update["sha256"] and sha != _pending_update["sha256"]:
        return jsonify(ok=False, error="校验值对不上，已拒绝安装"), 400
    if not sha:
        return jsonify(ok=False, error="缺少校验值，已拒绝安装"), 400

    with _lock:
        if _update_state["stage"] in ("downloading", "verifying", "preparing", "restarting"):
            return jsonify(ok=False, error="更新已经开始了，请等它跑完"), 409
        _update_state.update(stage="downloading", percent=0, done=0, total=0,
                             message="正在连接下载服务器", error="", version=version)

    threading.Thread(target=_run_update, args=(url, sha, version), daemon=True).start()
    return jsonify(ok=True, message="已开始下载")


def _run_update(url: str, sha: str, version: str) -> None:
    """后台下载并准备替换。每一步都更新 _update_state 供前端显示。"""

    def progress(done_bytes: int, total_bytes: int) -> None:
        percent = int(done_bytes * 100 / total_bytes) if total_bytes else 0
        mb_done = done_bytes / 1024 / 1024
        if total_bytes:
            message = f"已下载 {mb_done:.1f} / {total_bytes / 1024 / 1024:.0f} MB"
        else:
            message = f"已下载 {mb_done:.1f} MB"
        with _lock:
            _update_state.update(percent=percent, done=done_bytes,
                                 total=total_bytes, message=message)

    try:
        from api.updater import download

        path = download(url, sha, progress=progress)
        with _lock:
            _update_state.update(stage="verifying", percent=100, message="校验通过")
        from api.updater import save_pending

        save_pending(path, version, sha)
        with _lock:
            # 停在这里等用户决定，不自动重启
            _update_state.update(stage="ready", percent=100,
                                 message="已下载完成，点「立即重启」安装")
    except Exception as exc:
        with _lock:
            _update_state.update(stage="error",
                                 error=f"{type(exc).__name__}: {exc}",
                                 message="更新失败，已保留当前版本")
        _append_log(f"更新失败：{type(exc).__name__}: {exc}")
        try:
            from api.feedback import push_error

            push_error("update.apply", exc, f"version={version}")
        except Exception:
            pass


@app.post("/api/update/restart")
def api_update_restart():
    """用户点「立即重启」后才真正替换并重启。"""
    from api.updater import apply_and_restart, clear_pending, load_pending

    pending = load_pending()
    if not pending:
        with _lock:
            _update_state.update(stage="idle", percent=0, message="", error="")
        return jsonify(ok=False, error="没有已下载的更新包，请重新检查更新"), 400

    # 已下载的包必须比当前版本新，否则会陷入
    # 「提示有新版本 → 装完还是旧版 → 又提示」的死循环。
    from api.updater import _newer

    pending_version = str(pending.get("version") or "")
    if pending_version and not _newer(pending_version, _app_version()):
        clear_pending()
        with _lock:
            _update_state.update(stage="idle", percent=0, message="", error="")
        return jsonify(ok=False,
                       error=f"之前下载的 {pending_version} 不比当前版本新，"
                             f"请重新点「检查更新」下载最新版"), 409

    with _lock:
        if _update_state["stage"] in ("preparing", "restarting"):
            return jsonify(ok=False, error="已经在准备了"), 409
        _update_state.update(stage="preparing", percent=0, error="",
                             message="正在解压更新包")

    def run() -> None:
        from api.updater import prepare_package

        def progress(done: int, total: int) -> None:
            with _lock:
                _update_state.update(percent=int(done * 100 / max(1, total)),
                                     message=f"正在解压 {done}/{total} 个文件")

        try:
            stage_dir = prepare_package(pending["zip"], progress=progress)
            with _lock:
                _update_state.update(stage="restarting", percent=100,
                                     message="正在退出并安装，几秒后程序会自动重开")
            restarted = apply_and_restart(stage_dir)
        except Exception as exc:
            with _lock:
                _update_state.update(stage="error", error=f"{type(exc).__name__}: {exc}",
                                     message="安装启动失败，已保留当前版本")
            _append_log(f"更新失败：{type(exc).__name__}: {exc}")
            return

        if not restarted:
            with _lock:
                _update_state.update(stage="ready", percent=100,
                                     message="开发模式下不自动替换，更新包已就绪")
            return
        quit_for_update()

    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True, message="正在准备更新")


@app.get("/api/update/progress")
def api_update_progress():
    with _lock:
        state = dict(_update_state)
    # 进程重启过：内存状态丢了，但磁盘上可能还有已下载的包
    if state.get("stage") in ("idle", ""):
        from api.updater import load_pending

        pending = load_pending()
        if pending:
            state.update(stage="ready", percent=100,
                         message=f"更新包已就绪（v{pending.get('version', '')}），点「立即重启」安装",
                         version=pending.get("version", ""))
    return jsonify(state)


@app.get("/api/library")
def api_library():
    keyword = request.args.get("q", "")
    try:
        from api.library import Library

        lib = Library()
        return jsonify(ok=True, stats=lib.stats(), items=lib.search(keyword))
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.get("/api/library/remote")
def api_library_remote():
    keyword = request.args.get("q", "")
    try:
        from api.remote_library import list_remote

        data = list_remote(60, keyword)
        if not data.get("ok"):
            return jsonify(ok=False, error=data.get("error", "服务器题库不可用")), 200
        return jsonify(ok=True, total=data.get("total", 0), items=data.get("items", []))
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.post("/api/library/confirm")
def api_library_confirm():
    """用户手工确认某道题的答案可信。"""
    data = request.get_json(force=True, silent=True) or {}
    fp = str(data.get("fingerprint", "")).strip()
    if not fp:
        return jsonify(ok=False, error="缺少 fingerprint"), 400
    try:
        from api.library import Library

        Library().confirm(fp, "manual")
        return jsonify(ok=True, message="已确认")
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.post("/api/library/reject")
def api_library_reject():
    """用户手工把某道题标成错的，之后不再复用。"""
    data = request.get_json(force=True, silent=True) or {}
    fp = str(data.get("fingerprint", "")).strip()
    if not fp:
        return jsonify(ok=False, error="缺少 fingerprint"), 400
    try:
        from api.library import Library

        Library().reject(fp)
        return jsonify(ok=True, message="已标错")
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.post("/api/library/delete")
def api_library_delete():
    data = request.get_json(force=True, silent=True) or {}
    fp = str(data.get("fingerprint", "")).strip()
    if not fp:
        return jsonify(ok=False, error="缺少 fingerprint"), 400
    try:
        from api.library import Library

        Library().delete(fp)
        return jsonify(ok=True, message="已删除")
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.post("/api/models")
def api_models():
    cfg = _fill_secrets(request.get_json(force=True, silent=True) or {})
    key = str(cfg.get("api_key", "")).strip()
    if not key:
        return jsonify(ok=False, error="请先填写 API Key"), 400
    base = _endpoint(cfg)
    try:
        from openai import OpenAI

        client = OpenAI(base_url=base, api_key=key, timeout=25)
        listed = client.models.list()
        ids = sorted({m.id for m in listed.data})
        if not ids:
            return jsonify(ok=False, error="接口返回了空模型列表"), 400
        return jsonify(ok=True, models=ids, endpoint=base)
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}")


@app.post("/api/courses")
def api_courses():
    cfg = _fill_secrets(request.get_json(force=True, silent=True) or {})
    username = str(cfg.get("username", "")).strip()
    password = str(cfg.get("password", ""))
    if not username or not password:
        return jsonify(ok=False, error="请先填写手机号和密码"), 400
    try:
        from api.answer import DummyTiku
        from api.base import Account, Chaoxing

        chaoxing = Chaoxing(
            account=Account(username, password),
            tiku=DummyTiku(),
            query_delay=0,
            work_max_retries=0,
            work_redo_enabled=False,
        )
        state = chaoxing.login(login_with_cookies=True)
        if not state.get("status"):
            return jsonify(ok=False, error=f"登录失败：{state.get('msg')}"), 400
        courses = chaoxing.get_course_list()
        out = [
            {"id": str(c.get("courseId")), "title": c.get("title", "")}
            for c in courses
        ]
        return jsonify(ok=True, courses=out, message=f"拉取到 {len(out)} 门课程")
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500


@app.post("/api/start")
def api_start():
    """防止「检查 + 启动」之间被第二个请求钻空子。

    以前 _lock 在检查完就放了，Popen 在几十行之后（中间还有一次最长 60 秒的
    账号核对）。界面上的按钮在 await 期间没有禁用，双击就会起两个刷课进程，
    前一个变成没人管的孤儿继续交章节。
    """
    with _lock:
        if _state["proc"] is not None or _state.get("starting"):
            return jsonify(ok=False, error="已有任务在启动或运行中，请稍后"), 409
        if _state["exam"].get("driving"):
            return jsonify(ok=False, error="考试正在自动作答，请等结束再开始刷课"), 409
        _state["starting"] = True
    try:
        return _start_task_locked()
    finally:
        with _lock:
            _state["starting"] = False


def _start_task_locked():
    cfg = _fill_secrets(request.get_json(force=True, silent=True) or {})
    with _lock:
        if _state["proc"] is not None:
            return jsonify(ok=False, error="已有任务在运行，请先停止"), 409
    err = _validate(cfg)
    if err:
        return jsonify(ok=False, error=err), 400

    # 跑任务前核对登录的账号有没有变：出现过「登录的是 A、跑起来是 B」，
    # 根因是换账号时旧 cookie 没清。这里再做一道保险。
    try:
        saved_login = json.loads((ROOT / "login.json").read_text(encoding="utf-8"))
        saved_uid = str(saved_login.get("uid") or "")
    except Exception:
        saved_uid = ""
    if saved_uid:
        try:
            who = _run_with_deadline(_whoami, 60)
        except Exception:
            who = {}
        now_uid = str(who.get("uid") or "")
        if now_uid and now_uid != saved_uid:
            return jsonify(
                ok=False,
                error=f"当前登录的账号已经不是「{saved_login.get('name') or '刚登录的那个'}」了，"
                      f"请重新点「我已登录，读取状态」再开始"), 409
    _save_secret(cfg.get("password", ""), cfg.get("api_key", ""),
                 cfg.get("search_key"), cfg.get("agent_key"))
    _write_config(cfg)
    _write_config(cfg, path=RUNTIME_CONFIG, with_secrets=True)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["CHAOXING_WEBUI"] = "1"
    # 子进程单独一个日志文件，避免和 UI 进程抢同一个文件的轮转
    env["CHAOXING_LOG_FILE"] = "chaoxing.task.log"
    env["PYTHONUNBUFFERED"] = "1"
    if FROZEN:
        cmd = [sys.executable, "--run-task", "-c", str(RUNTIME_CONFIG)]
    else:
        cmd = [str(PYTHON), str(ROOT / "main.py"), "-c", str(RUNTIME_CONFIG)]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    with _lock:
        _state["proc"] = proc
        _state["stopping"] = False
        _state["logs"] = deque(maxlen=6000)
        _state["chapters"] = {}
        _state["seq"] = 0
        _state["status"] = "running"
        _state["exit_code"] = None
        _state["started_at"] = time.time()
    # 先重置队列和序号，再写这条启动日志。反过来的话它会把自己清掉，
    # 而且前端 since 会停在上一轮的最大值，第二轮起日志面板整段空白。
    _append_log(f"任务进程已启动（PID {proc.pid}）", kind="ui")
    threading.Thread(target=_pump, args=(proc,), daemon=True).start()
    threading.Thread(target=_watchdog, args=(proc,), daemon=True).start()
    # 子进程启动时会读取配置，稍后删除临时文件，避免明文密码长期留在磁盘上
    threading.Timer(5.0, _cleanup_runtime_config).start()
    return jsonify(ok=True, pid=proc.pid)


_EXAM_FACE_TIMEOUT = 300




def _watchdog(proc: subprocess.Popen) -> None:
    """运行时长超过上限时强制结束，避免无人值守时无限跑。"""
    limit = MAX_RUN_HOURS * 3600
    started = time.time()
    while proc.poll() is None:
        if time.time() - started > limit:
            _append_log(f"运行时长超过 {MAX_RUN_HOURS} 小时上限, 正在停止任务")
            with _lock:
                _state["stopping"] = True
            _kill_tree(proc)
            return
        time.sleep(30)


def _cleanup_runtime_config() -> None:
    try:
        if RUNTIME_CONFIG.exists():
            RUNTIME_CONFIG.unlink()
    except Exception:
        pass


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, text=True)
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def kill_task_process() -> None:
    """结束正在跑的刷课任务进程。桌面入口退出时也要调它，
    否则托盘退出后子进程还在答题、提交，重开界面却显示「空闲」。"""
    with _lock:
        proc = _state["proc"]
    if proc is not None:
        _kill_tree(proc)


@app.post("/api/stop")
def api_stop():
    with _lock:
        proc = _state["proc"]
        if proc is not None:
            _state["stopping"] = True
    if proc is None:
        return jsonify(ok=False, error="当前没有运行中的任务"), 400
    _kill_tree(proc)
    return jsonify(ok=True, message="已发送停止指令")


@app.post("/api/client-log")
def api_client_log():
    """前端上报一次操作，写进日志流。

    只记动作名（按钮的 id、页面名或按钮文字），不碰输入框内容，
    所以密码、API Key、题目答案都不会进日志。
    """
    data = request.get_json(force=True, silent=True) or {}
    action = str(data.get("action", "")).strip()[:60]
    page = str(data.get("page", "")).strip()[:20]
    if not action:
        return jsonify(ok=False, error="缺少 action"), 400
    _append_log(f"点击 {page + ' / ' if page else ''}{action}", kind="ui")
    return jsonify(ok=True)


def append_ui_log(text: str) -> None:
    """给桌面入口写界面启停这类日志用。"""
    _append_log(text, kind="ui")


@app.get("/api/logs")
def api_logs():
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0
    with _lock:
        items = [x for x in _state["logs"] if x["n"] > since]
        chapters = [dict(key=k, **v) for k, v in _state["chapters"].items()]
        status = _state["status"]
        exit_code = _state["exit_code"]
    return jsonify(items=items, status=status, exit_code=exit_code, chapters=chapters)


@app.get("/api/status")
def api_status():
    with _lock:
        return jsonify(
            status=_state["status"],
            exit_code=_state["exit_code"],
            running=_state["proc"] is not None,
            log_count=_state["seq"],
        )
# ---------- 助教：题库体检 ----------

def _agent_log(msg: str) -> None:
    _append_log(msg)


@app.get("/api/agent/config")
def api_agent_config():
    """助教状态：模型配好了没、题库审到什么程度。"""
    from api import agent as agent_mod
    from api.library import Library

    c = agent_mod.conf()
    try:
        stats = Library().audit_stats()
    except Exception as exc:
        return jsonify(ok=False, error=f"打不开题库：{exc}"), 500
    with _lock:
        running = bool(_state["agent"]["running"])
    return jsonify(ok=True, ready=c["ready"], own=c["own"],
                   endpoint=c["endpoint"], model=c["model"],
                   has_key=bool(c["key"]), running=running,
                   stats=stats)


@app.post("/api/agent/audit")
def api_agent_audit():
    """跑一轮题库审查。"""
    data = request.get_json(force=True, silent=True) or {}
    max_items = max(1, min(500, int(_num(data.get("max_items"), 80, int))))
    use_model = bool(data.get("use_model", True))
    only_new = bool(data.get("only_unaudited", False))
    use_search = bool(data.get("use_search", True))
    with _lock:
        if _state["agent"]["running"]:
            return jsonify(ok=False, error="上一轮审查还没跑完"), 409
        _state["agent"].update(running=True, error="", result=None)
    threading.Thread(target=_run_agent_audit,
                     args=(max_items, use_model, only_new, use_search),
                     daemon=True).start()
    return jsonify(ok=True, message="开始体检")


def _run_agent_audit(max_items: int, use_model: bool, only_new: bool,
                     use_search: bool = True) -> None:
    try:
        from api import agent as agent_mod

        result = agent_mod.audit(max_items=max_items, use_model=use_model,
                                 log=_agent_log, only_unaudited=only_new,
                                 use_search=use_search)
        with _lock:
            _state["agent"].update(result=result, running=False,
                                   finished_at=time.time())
    except Exception as exc:
        _agent_log(f"体检中断：{type(exc).__name__}: {exc}")
        with _lock:
            _state["agent"].update(error=f"{type(exc).__name__}: {exc}",
                                   running=False, finished_at=time.time())
        try:
            from api.feedback import push_error

            push_error("agent.audit", exc, "")
        except Exception:
            pass


@app.get("/api/agent/result")
def api_agent_result():
    state = request.args.get("state", "").strip()
    try:
        limit = max(1, min(500, int(request.args.get("limit", 200))))
    except ValueError:
        limit = 200
    from api.library import Library

    lib = Library()
    try:
        rows = lib.audit_list(state, limit=limit)
    except Exception as exc:
        return jsonify(ok=False, error=str(exc)), 500
    with _lock:
        running = bool(_state["agent"]["running"])
        error = _state["agent"]["error"]
        last = _state["agent"]["result"]
    return jsonify(ok=True, items=rows, stats=lib.audit_stats(),
                   running=running, error=error,
                   last_total=(last or {}).get("total"))


@app.post("/api/agent/apply")
def api_agent_apply():
    """把一条审查结论落到题库上：确认 / 判错 / 改成新答案。"""
    data = request.get_json(force=True, silent=True) or {}
    fp = str(data.get("fingerprint") or "").strip()
    action = str(data.get("action") or "").strip()
    if not fp:
        return jsonify(ok=False, error="缺少题目指纹"), 400
    from api.library import Library

    lib = Library()
    try:
        if action == "confirm":
            lib.confirm(fp, "manual")
            note = "已标记为确认"
        elif action == "reject":
            lib.reject(fp)
            note = "已标记为错误"
        elif action == "set":
            answer = str(data.get("answer") or "").strip()
            if not answer:
                return jsonify(ok=False, error="新答案不能为空"), 400
            lib.set_answer(fp, answer, "manual")
            note = "答案已更新"
        elif action == "clear":
            lib.set_audit([(fp, "", "")])
            note = "已清除审查结论"
        else:
            return jsonify(ok=False, error="未知操作"), 400
    except Exception as exc:
        return jsonify(ok=False, error=f"{type(exc).__name__}: {exc}"), 500
    return jsonify(ok=True, message=note)


@app.post("/api/agent/upload")
def api_agent_upload():
    """把审查通过、而且原本就有硬证据的题上报共享题库。"""
    from api import agent as agent_mod

    try:
        rows = agent_mod.upload_candidates()
    except Exception as exc:
        return jsonify(ok=False, error=f"筛选失败：{exc}"), 500
    if not rows:
        return jsonify(ok=False,
                       error="还没有可上报的题。需要「审查通过」且原始依据是"
                             "平台判分 / 整卷全对 / 共享库 / 手工确认"), 400
    items = [{"fingerprint": r.get("fingerprint", ""),
              "title": r.get("title", ""),
              "options": r.get("options", ""),
              "qtype": r.get("qtype", ""),
              "answer": r.get("answer", "")} for r in rows]
    username = ""
    try:
        parser = ConfigParser(interpolation=None)
        parser.read(CONFIG_PATH, encoding="utf-8")
        username = parser["common"].get("username", "") if "common" in parser else ""
    except Exception:
        username = ""
    from api.remote_library import submit_items

    try:
        accepted = submit_items(items, username, "high")
    except Exception as exc:
        return jsonify(ok=False, error=f"上报失败：{exc}"), 500
    if not accepted:
        return jsonify(ok=False, error="服务器没有接受任何题目，稍后再试"), 400
    _agent_log(f"助教已上报 {accepted} 道审查通过的题到共享题库")
    return jsonify(ok=True, accepted=accepted,
                   message=f"已上报 {accepted} 道题")
