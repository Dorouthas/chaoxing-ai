# -*- coding: utf-8 -*-
"""考场开窗与登录链路的安全阀（离线）。

覆盖两条 P0：
1. 本地有登录态、却没推进考场窗口时，不允许继续导航（否则用户落在登录页，
   只会看到「考场打不开」）；
2. 登录会先清掉旧登录态，失败时必须原样恢复 —— 不能因为一次失败的登录
   把本来能用的会话弄丢。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("CX_UI_TOKEN", "test-token")

import webui.server as server          # noqa: E402  (必须在设好 token 后导入)
from api import browser_unlock        # noqa: E402

TOKEN = {"X-Token": "test-token"}


class FakeWindow:
    def __init__(self):
        self.urls = []
        self.shown = 0
        self.destroyed = 0

    def load_url(self, url):
        self.urls.append(url)

    def show(self):
        self.shown += 1

    def get_current_url(self):
        return "about:blank"

    def destroy(self):
        self.destroyed += 1


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved_root = server.ROOT
        self._saved_config = server.CONFIG_PATH
        server.ROOT = Path(self.tmp.name)
        server.CONFIG_PATH = Path(self.tmp.name) / "config.ini"
        server.CONFIG_PATH.write_text("[common]\nuse_cookies = true\n", encoding="utf-8")

        def _restore_paths():
            server.ROOT = self._saved_root
            server.CONFIG_PATH = self._saved_config

        self.addCleanup(_restore_paths)

        self.win = FakeWindow()
        server._exam_browser.update(window=self.win, url="", enc="", answer_id="", exam_id="")
        self.addCleanup(lambda: server._exam_browser.update(window=None))

        server._pending_login_backup = {}
        self.client = server.app.test_client()

        self._orig = (browser_unlock.sync_cookies, browser_unlock.read_cookies,
                      browser_unlock.clear_cookies, server._probe_login, server._whoami)
        self.addCleanup(self._restore)

    def _restore(self):
        (browser_unlock.sync_cookies, browser_unlock.read_cookies,
         browser_unlock.clear_cookies, server._probe_login, server._whoami) = self._orig


class ExamBrowserOpenTest(_Base):
    def test_blocks_when_cookie_exists_but_sync_fails(self):
        (server.ROOT / "cookies.txt").write_text("UID=1; _uid=1;", encoding="utf-8")
        browser_unlock.sync_cookies = lambda *a, **k: 0

        resp = self.client.post("/api/exam/browser", json={"url": "https://example.com/exam"},
                                headers=TOKEN)

        self.assertEqual(resp.status_code, 502)
        self.assertIn("登录态没能送进考场窗口", resp.get_json()["error"])
        self.assertEqual(self.win.urls, [], "同步失败时不应该导航，否则会落在登录页")

    def test_navigates_when_sync_ok(self):
        (server.ROOT / "cookies.txt").write_text("UID=1; _uid=1;", encoding="utf-8")
        browser_unlock.sync_cookies = lambda *a, **k: 12

        resp = self.client.post("/api/exam/browser", json={"url": "https://example.com/exam"},
                                headers=TOKEN)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.win.urls, ["https://example.com/exam"])

    def test_navigates_when_no_cookie_file(self):
        """还没登录时也允许打开：用户可以在窗口里自己登录。"""
        browser_unlock.sync_cookies = lambda *a, **k: 0

        resp = self.client.post("/api/exam/browser", json={"url": "https://example.com/login"},
                                headers=TOKEN)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.win.urls, ["https://example.com/login"])


class LoginRollbackTest(_Base):
    def test_open_moves_state_to_backup(self):
        (server.ROOT / "cookies.txt").write_text("UID=1;", encoding="utf-8")
        (server.ROOT / "login.json").write_text(json.dumps({"uid": "1"}), encoding="utf-8")
        browser_unlock.clear_cookies = lambda *a, **k: True

        resp = self.client.post("/api/login/open", json={}, headers=TOKEN)

        self.assertEqual(resp.status_code, 200)
        self.assertFalse((server.ROOT / "cookies.txt").exists())
        self.assertTrue((server.ROOT / "cookies.txt.bak").exists())
        self.assertTrue((server.ROOT / "login.json.bak").exists())

    def test_failed_collect_restores_state(self):
        (server.ROOT / "cookies.txt").write_text("UID=old;", encoding="utf-8")
        browser_unlock.clear_cookies = lambda *a, **k: True
        self.client.post("/api/login/open", json={}, headers=TOKEN)

        # 窗口里没读到登录信息
        browser_unlock.read_cookies = lambda *a, **k: {}
        resp = self.client.post("/api/login/collect", json={}, headers=TOKEN)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("已恢复", resp.get_json()["error"])
        self.assertEqual((server.ROOT / "cookies.txt").read_text(encoding="utf-8"), "UID=old;")
        self.assertFalse((server.ROOT / "cookies.txt.bak").exists())

    def test_successful_collect_drops_backup(self):
        (server.ROOT / "cookies.txt").write_text("UID=old;", encoding="utf-8")
        browser_unlock.clear_cookies = lambda *a, **k: True
        self.client.post("/api/login/open", json={}, headers=TOKEN)

        browser_unlock.read_cookies = lambda *a, **k: {"_uid": "9", "UID": "9"}
        server._probe_login = lambda cookies: {"status": True, "msg": "ok"}
        server._whoami = lambda: {"ok": True, "uid": "9", "name": "测试同学"}

        resp = self.client.post("/api/login/collect", json={}, headers=TOKEN)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("测试同学", resp.get_json()["message"])
        self.assertTrue((server.ROOT / "cookies.txt").exists())
        self.assertFalse((server.ROOT / "cookies.txt.bak").exists())


if __name__ == "__main__":
    unittest.main()