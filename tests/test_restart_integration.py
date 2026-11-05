"""Cover a live calibration through real capture/input workers to axis recovery."""

import time
from types import SimpleNamespace
import unittest

import battle_restart as restart
import test_battle_restart as detector_tests
import test_restart_setup_api as api_tests


class RestartIntegrationTests(unittest.TestCase):
    def test_saved_mark_and_click_during_capture_restore_finished_axis(self):
        api = api_tests.RestartSetupApiTests()
        api.setUp()
        self.addCleanup(api.tearDown)
        self.addCleanup(api.doCleanups)
        overlay = api.live_overlay()
        overlay._fire("skill", 1)
        backend = detector_tests.BlockingBackend()
        source = detector_tests.FakeEventSource(backend)
        self.addCleanup(backend.allow_capture.set)
        game = {"hwnd": 77, "sure": True, "exe": "endfield.exe"}

        def make_watcher(target, callback, find_game, log):
            return restart.RestartButtonWatcher(
                target, callback, find_game, log,
                backend=backend, event_source=source)

        api.namespace["game_window"] = lambda: game
        api.namespace["battle_restart"] = SimpleNamespace(
            capture_game=lambda game: detector_tests.snapshot(),
            make_target=restart.make_target,
            validate_target=restart.validate_target,
            RestartButtonWatcher=make_watcher)
        capture_id = api.capture()
        status, response = api.request("/api/battle-restart/target", {
            "id": capture_id, "button": detector_tests.BUTTON,
            "context": detector_tests.CONTEXT})
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        overlay._tick()
        self.assertEqual(overlay.idx, 1, "saving must preserve the current step")
        watcher = overlay._restart_watcher
        self.assertIsInstance(watcher, restart.RestartButtonWatcher)

        overlay._fire("skill", 2)
        overlay.root.advance(400)
        self.assertTrue(overlay.waiting_restart and overlay.root.hidden)
        self.wait_until(lambda: watcher.status()["state"] == "ready")
        self.assertTrue(backend.capture_entered.wait(1))
        source.emit("down")
        source.emit("up")
        self.wait_until(lambda: not overlay.q.empty())
        self.assertFalse(backend.allow_capture.is_set())
        overlay._tick()
        overlay.root.advance(150)
        self.assertTrue(overlay.running)
        self.assertFalse(overlay.waiting_restart or overlay.root.hidden)
        self.assertEqual((overlay.seg, overlay.idx), (0, 0))
        self.assertEqual(watcher.status()["state"], "restarted")
        backend.allow_capture.set()
        overlay._handle("down", "exit")
        self.assertTrue(source.stopped)
        self.assertFalse(watcher._thread.is_alive())
        self.assertFalse(watcher._input_thread.is_alive())

    def wait_until(self, predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(predicate())


if __name__ == "__main__":
    unittest.main()
