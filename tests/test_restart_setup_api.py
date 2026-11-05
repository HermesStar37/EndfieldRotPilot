"""Exercise the setup HTTP flow without importing app_web's startup migrations."""
import ast
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, unquote, urlparse

import test_overlay_restart as overlay_tests


SOURCE = Path(__file__).resolve().parents[1] / "app_web.py"


class RestartSetupApiTests(unittest.TestCase):
    def setUp(self):
        self.config = {"steps": [{"key": "1"}]}
        self.target = {"enabled": True, "exe": "game.exe", "size": [800, 600],
                       "button": {"image": "button-patch", "box": [20, 30, 80, 40]},
                       "context": {"image": "restart-text", "box": [20, 10, 100, 20]}}
        self.timers = []
        owner = self

        class Timer:
            def __init__(self, delay, callback):
                self.delay, self.callback = delay, callback
                owner.timers.append(self)

            def start(self):
                pass

        def save(cfg, **kwargs):
            self.config = copy.deepcopy(cfg)

        def validate(target):
            if not isinstance(target, dict) or target.get("exe") != "game.exe":
                raise ValueError("invalid target")
            return copy.deepcopy(target)

        detector = SimpleNamespace(
            capture_game=lambda game: {"image": "full-game-screenshot", "exe": "game.exe",
                                       "size": [800, 600], "capturedAt": time.time()},
            make_target=lambda snapshot, button, context: copy.deepcopy(self.target),
            validate_target=validate)
        namespace = {
            "BaseHTTPRequestHandler": BaseHTTPRequestHandler,
            "json": json, "os": os, "time": time,
            "threading": SimpleNamespace(Timer=Timer), "battle_restart": detector,
            "urlparse": urlparse, "parse_qs": parse_qs, "unquote": unquote,
            "load_cfg": lambda: copy.deepcopy(self.config), "save_cfg": save,
            "game_window": lambda: {"hwnd": 7, "sure": True},
            "log": lambda *args: None, "overlay_busy": lambda: False,
            "clamp_alpha": float, "normalize_keys": lambda keys: keys or {},
            "ALPHA_DEFAULT": 0.94, "_overlay": None,
            "_OVERLAY_LOCK": threading.Lock(), "_OVERLAY_BUSY": [False],
            "raw_pad": lambda: None, "pick_screen": lambda cfg: 0,
            "maybe_quit": lambda why: None,
            "_RESTART_CAPTURE_LOCK": threading.Lock(),
            "_RESTART_CAPTURE": {"state": "idle"}, "_RESTART_CAPTURE_TTL": 300,
        }
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        selected = [node for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                    and (node.name.startswith("_restart_capture_")
                         or node.name == "_restart_target_from_capture"
                         or node.name == "_attach_restart_watcher"
                         or node.name in ("_refresh_restart_watcher", "_replace_restart_watcher",
                                          "_restart_watcher_status", "start_overlay")
                         or node.name == "Handler")]
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)
        self.namespace = namespace
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), namespace["Handler"])
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=1)

    def request(self, path, body=None, origin="http://127.0.0.1"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        headers = {"Content-Type": "application/json", "Origin": origin}
        connection.request("GET" if body is None else "POST", path,
                           None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        status, result = response.status, json.loads(response.read())
        connection.close()
        return status, result

    def capture(self):
        status, response = self.request("/api/battle-restart/capture", {})
        self.assertEqual(status, 200)
        self.assertEqual(response["capture"]["state"], "pending")
        self.assertEqual(self.timers[-1].delay, 8)
        self.timers[-1].callback()
        return response["capture"]["id"]

    def test_capture_then_save_keeps_only_selected_regions(self):
        capture_id = self.capture()
        status, response = self.request("/api/battle-restart/capture")
        self.assertEqual(status, 200)
        self.assertEqual(response["capture"]["image"], "full-game-screenshot")
        status, response = self.request("/api/battle-restart/target", {
            "id": capture_id, "button": [20, 30, 80, 40], "context": [20, 10, 100, 20]})
        self.assertEqual(status, 200)
        self.assertEqual(response["target"], self.target)
        self.assertEqual(self.config["restartButton"], self.target)
        self.assertNotIn("full-game-screenshot", json.dumps(self.config))
        self.assertEqual(self.namespace["_RESTART_CAPTURE"], {"state": "idle"})

    def test_stale_capture_cannot_replace_saved_target(self):
        self.config["restartButton"] = copy.deepcopy(self.target)
        self.capture()
        status, response = self.request("/api/battle-restart/target", {"id": "stale"})
        self.assertEqual(status, 400)
        self.assertFalse(response["ok"])
        self.assertEqual(self.config["restartButton"], self.target)

    def test_expired_screenshot_is_discarded(self):
        self.capture()
        self.namespace["_RESTART_CAPTURE"]["createdAt"] = time.time() - 301
        _, response = self.request("/api/battle-restart/capture")
        self.assertEqual(response["capture"]["state"], "error")
        self.assertNotIn("image", response["capture"])
        self.assertNotIn("snapshot", self.namespace["_RESTART_CAPTURE"])

    def test_new_capture_restarts_countdown_and_invalidates_old_timer(self):
        _, first = self.request("/api/battle-restart/capture", {})
        _, second = self.request("/api/battle-restart/capture", {})
        self.assertNotEqual(first["capture"]["id"], second["capture"]["id"])
        self.assertEqual(len(self.timers), 2)
        self.timers[0].callback()
        self.assertEqual(self.namespace["_RESTART_CAPTURE"]["state"], "pending")
        self.timers[1].callback()
        self.assertEqual(self.namespace["_RESTART_CAPTURE"]["state"], "ready")
        self.assertEqual(self.namespace["_RESTART_CAPTURE"]["id"], second["capture"]["id"])

    def test_disable_and_clear_target_preserve_axis(self):
        disabled = dict(self.target, enabled=False)
        status, _ = self.request("/api/state", {"restartButton": disabled})
        self.assertEqual(status, 200)
        self.assertFalse(self.config["restartButton"]["enabled"])
        status, response = self.request("/api/battle-restart/target", {"clear": True})
        self.assertEqual(status, 200)
        self.assertIsNone(response["target"])
        self.assertEqual(self.config["steps"], [{"key": "1"}])

    def test_invalid_target_and_foreign_origin_leave_settings_unchanged(self):
        before = copy.deepcopy(self.config)
        status, _ = self.request("/api/state", {"restartButton": {"enabled": True}})
        self.assertEqual(status, 400)
        status, _ = self.request("/api/battle-restart/capture", {}, "https://example.com")
        self.assertEqual(status, 403)
        self.assertEqual(self.config, before)
        self.assertEqual(self.timers, [])

    def test_axis_closed_during_startup_does_not_start_a_watcher(self):
        def unexpected_watcher(*args):
            self.fail("closed axis must not start a watcher")

        self.namespace["battle_restart"].RestartButtonWatcher = unexpected_watcher
        overlay = SimpleNamespace(running=False, auto_restart=True)
        self.namespace["_attach_restart_watcher"](overlay, self.target)

    def test_watcher_start_failure_falls_back_to_normal_completion(self):
        def failing_watcher(*args):
            raise RuntimeError("screen capture unavailable")

        self.namespace["battle_restart"].RestartButtonWatcher = failing_watcher
        overlay = SimpleNamespace(running=True, auto_restart=True, q=None)
        self.namespace["_attach_restart_watcher"](overlay, self.target)
        self.assertFalse(overlay.auto_restart)
        self.assertIsNone(overlay._restart_watcher)

    def live_overlay(self, steps=None, publish=True):
        fixture = overlay_tests.OverlayRestartTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        ov = fixture.overlay(steps or [{"key": "1"}, {"key": "2"}], target=None)
        self.addCleanup(ov.close)
        if publish:
            self.namespace["_overlay"] = ov
        self.namespace["overlay_busy"] = lambda: ov.running
        ov._handle.__globals__.update({name: value for name, value in self.namespace.items()
                                      if name in ("_replace_restart_watcher",)})
        self.watchers = []

        class Watcher:
            def __init__(watcher, target, callback, find_game, log):
                watcher.callback = callback
                watcher.stopped = False
                self.watchers.append(watcher)

            def start(watcher):
                return watcher

            def stop(watcher):
                watcher.stopped = True

            def click(watcher):
                watcher.callback()

            def status(watcher):
                return {"state": "watching", "message": "等待点击重开按钮", "captureMs": 12}

        self.namespace["battle_restart"].RestartButtonWatcher = Watcher
        return ov

    def test_save_while_axis_is_running_activates_this_instance_then_click_restarts(self):
        ov = self.live_overlay()
        ov._fire("skill", 1)
        capture_id = self.capture()
        status, response = self.request("/api/battle-restart/target", {
            "id": capture_id, "button": [20, 30, 80, 40], "context": [20, 10, 100, 20]})
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertIsNone(ov._restart_watcher, "HTTP must leave Tk lifecycle to its queue")
        ov._tick()
        self.assertTrue(ov.auto_restart)
        self.assertEqual(ov.idx, 1, "enabling must preserve the current skill")
        self.assertEqual(len(self.watchers), 1)
        self.watchers[0].click()
        ov._tick()
        self.assertEqual(ov.idx, 0)

    def test_status_reports_current_live_watcher(self):
        ov = self.live_overlay()
        status, before = self.request("/api/battle-restart/status")
        self.assertEqual(status, 200)
        self.assertTrue(before["running"])
        self.assertFalse(before["active"])
        self.assertEqual(before["status"]["state"], "inactive")
        self.request("/api/state", {"restartButton": self.target})
        _, applying = self.request("/api/battle-restart/status")
        self.assertEqual(applying["status"]["state"], "starting")
        self.assertNotIn("请开始运行", applying["status"]["message"])
        ov._tick()
        status, after = self.request("/api/battle-restart/status")
        self.assertEqual(status, 200)
        self.assertTrue(after["enabled"] and after["active"])
        self.assertEqual(after["status"]["state"], "watching")
        self.assertEqual(after["status"]["captureMs"], 12)

    def test_status_explains_start_failure_and_reenable_retries_saved_target(self):
        ov = self.live_overlay()
        healthy = self.namespace["battle_restart"].RestartButtonWatcher

        class FailingWatcher(healthy):
            def start(watcher):
                raise RuntimeError("mouse hook unavailable")

        self.namespace["battle_restart"].RestartButtonWatcher = FailingWatcher
        self.request("/api/state", {"restartButton": self.target})
        ov._tick()
        failed_watcher = self.watchers[-1]
        self.assertTrue(failed_watcher.stopped)
        _, status = self.request("/api/battle-restart/status")
        self.assertTrue(status["running"] and status["enabled"])
        self.assertFalse(status["active"])
        self.assertEqual(status["status"]["state"], "error")
        self.assertIn("mouse hook unavailable", status["status"]["message"])
        self.assertNotIn("重新标记", status["status"]["message"])
        self.namespace["battle_restart"].RestartButtonWatcher = healthy
        self.request("/api/state", {"restartButton": self.target})
        ov._tick()
        _, status = self.request("/api/battle-restart/status")
        self.assertTrue(status["active"])
        self.assertEqual(status["status"]["state"], "watching")

    def test_replacing_a_live_target_ignores_old_watcher_callbacks(self):
        ov = self.live_overlay()
        self.request("/api/state", {"restartButton": self.target})
        ov._tick()
        old = self.watchers[-1]
        ov._fire("skill", 1)
        self.request("/api/state", {"restartButton": self.target})
        old.click()  # queued behind the replacement while still using the old token
        ov._tick()
        self.assertTrue(old.stopped)
        self.assertEqual(ov.idx, 1)
        old.click()  # a callback completing after stop must also be ignored
        ov._tick()
        self.assertEqual(ov.idx, 1)
        self.watchers[-1].click()
        ov._tick()
        self.assertEqual(ov.idx, 0)

    def test_disable_and_clear_live_target_stop_watcher_without_resetting_axis(self):
        for clear in (False, True):
            with self.subTest(clear=clear):
                ov = self.live_overlay()
                self.request("/api/state", {"restartButton": self.target})
                ov._tick()
                watcher = self.watchers[-1]
                ov._fire("skill", 1)
                if clear:
                    self.request("/api/battle-restart/target", {"clear": True})
                else:
                    self.request("/api/state", {"restartButton": dict(self.target, enabled=False)})
                self.assertFalse(watcher.stopped)
                ov._tick()
                self.assertTrue(watcher.stopped)
                self.assertFalse(ov.auto_restart)
                self.assertTrue(ov.running)
                self.assertEqual(ov.idx, 1)
                ov.close()

    def test_disable_while_waiting_finishes_the_hidden_axis(self):
        ov = self.live_overlay()
        self.request("/api/state", {"restartButton": self.target})
        ov._tick()
        watcher = self.watchers[-1]
        ov._fire("skill", 1)
        ov._fire("skill", 2)
        ov.root.advance(400)
        self.assertTrue(ov.waiting_restart and ov.root.hidden)
        self.request("/api/battle-restart/target", {"clear": True})
        ov._tick()
        self.assertFalse(ov.running)
        self.assertTrue(ov.root.destroyed and watcher.stopped)

    def test_target_refresh_survives_a_queued_restart_and_f8_still_stops(self):
        ov = self.live_overlay()
        self.request("/api/state", {"restartButton": self.target})
        ov._tick()
        old = self.watchers[-1]
        old.click()
        self.request("/api/state", {"restartButton": self.target})
        ov._tick()
        self.assertTrue(old.stopped)
        self.assertEqual(len(self.watchers), 2)
        current = self.watchers[-1]
        ov._handle("down", "exit")
        current.click()
        ov._tick()
        self.assertFalse(ov.running)
        self.assertTrue(current.stopped)

    def test_enabling_after_final_step_cancels_pending_completion_close(self):
        ov = self.live_overlay()
        ov._fire("skill", 1)
        ov._fire("skill", 2)
        old_callbacks = [callback for _, callback in ov.root.jobs.values()]
        self.request("/api/state", {"restartButton": self.target})
        ov._tick()
        for callback in old_callbacks:
            callback()
        ov.root.advance(400)
        self.assertTrue(ov.running)
        self.assertTrue(ov.waiting_restart and ov.root.hidden)
        self.watchers[-1].click()
        ov._tick()
        ov.root.advance(150)
        self.assertTrue(ov.running)
        self.assertFalse(ov.root.hidden)
        self.assertEqual(ov.idx, 0)

    def test_latest_target_queued_before_sixth_skill_survives_completion(self):
        ov = self.live_overlay([{"key": key} for key in ("1", "2", "3", "4", "1", "2")])
        self.request("/api/state", {"restartButton": self.target})
        ov._tick()
        old = self.watchers[-1]
        for slot in (1, 2, 3, 4, 1):
            ov._fire("skill", slot)
        self.request("/api/state", {"restartButton": self.target})
        ov._fire("skill", 2)
        ov._tick()
        self.assertTrue(old.stopped)
        self.assertEqual(len(self.watchers), 2)
        self.assertTrue(ov.waiting_restart)
        ov.root.advance(400)
        self.watchers[-1].click()
        ov._tick()
        self.assertEqual(ov.idx, 0)
        self.assertTrue(ov.running)

    def test_restart_click_already_queued_at_completion_is_not_lost(self):
        ov = self.live_overlay()
        self.request("/api/state", {"restartButton": self.target})
        ov._tick()
        ov._fire("skill", 1)
        self.watchers[-1].click()
        ov._fire("skill", 2)
        ov._tick()
        self.assertEqual(ov.idx, 0)
        self.assertFalse(ov.waiting_restart)

    def test_mark_saved_during_overlay_construction_activates_before_first_run(self):
        observed = {}

        class ImmediateThread:
            def __init__(thread, *, target, daemon):
                thread.target = target

            def start(thread):
                thread.target()

        def construct_overlay(*args, **kwargs):
            self.assertIsNone(kwargs["restart_button"])
            ov = self.live_overlay(publish=False)
            ov._fire("skill", 1)
            capture_id = self.capture()
            status, response = self.request("/api/battle-restart/target", {
                "id": capture_id, "button": [20, 30, 80, 40], "context": [20, 10, 100, 20]})
            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])
            self.assertIsNone(self.namespace["_overlay"], "construction has not published its instance")

            def run():
                observed["active"] = ov.auto_restart
                observed["watcher"] = ov._restart_watcher
                if ov._restart_watcher is not None:
                    ov._restart_watcher.click()
                ov._tick()
                observed["idx"] = ov.idx

            ov.run = run
            return ov

        self.namespace["threading"].Thread = ImmediateThread
        self.namespace["Overlay"] = construct_overlay
        started = self.namespace["start_overlay"](copy.deepcopy(self.config))
        self.assertTrue(started)
        self.assertTrue(observed["active"])
        self.assertIsNotNone(observed["watcher"])
        self.assertEqual(observed["idx"], 0)


class RestartTargetPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rotpilot-config-test-")
        self.path = Path(self.temp.name) / "config.json"

        def load():
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

        namespace = {"_CFG_WRITE_LOCK": threading.RLock(), "load_cfg": load,
                     "CFG": str(self.path), "os": os, "shutil": shutil,
                     "json": json, "time": time, "log": lambda *args: None}
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name in ("save_cfg", "_save_cfg_locked")]
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)
        self.save, self.load = namespace["save_cfg"], load

    def tearDown(self):
        self.temp.cleanup()

    def test_ordinary_autosave_cannot_restore_an_old_calibration(self):
        self.save({"steps": [{"key": "1"}]})
        stale_autosave = self.load()
        new_target = {"enabled": True, "exe": "game.exe", "button": "new-template"}
        self.save(dict(self.load(), restartButton=new_target), restart_button_changed=True)
        stale_autosave["overlayY"] = 210
        self.save(stale_autosave)
        self.assertEqual(self.load()["restartButton"], new_target)
        self.assertEqual(self.load()["overlayY"], 210)

    def test_clearing_calibration_cannot_be_undone_by_a_stale_position_save(self):
        self.save({"restartButton": {"enabled": True}}, restart_button_changed=True)
        stale_position = self.load()
        self.save({"restartButton": None}, restart_button_changed=True)
        self.save(stale_position)
        self.assertIsNone(self.load()["restartButton"])


if __name__ == "__main__":
    unittest.main()
