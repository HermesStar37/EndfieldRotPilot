import copy
import threading
import time
import unittest

from PIL import Image, ImageDraw, ImageFont

import battle_restart as restart


BUTTON = [400, 260, 150, 50]
CONTEXT = [160, 80, 300, 100]


def scene():
    image = Image.new("RGB", (640, 360), (24, 28, 35))
    draw = ImageDraw.Draw(image)
    draw.rectangle((400, 260, 549, 309), fill=(190, 150, 55))
    for x in range(415, 535, 15):
        draw.rectangle((x, 273, x + 6, 296), fill=(20, 25, 32))
    draw.rectangle((160, 80, 459, 179), fill=(42, 45, 52))
    for x in range(175, 445, 18):
        draw.line((x, 100, x + 8, 149), fill=(230, 232, 234), width=5)
        draw.line((x, 149, x + 8, 160), fill=(165, 170, 180), width=3)
    return image


def snapshot(image=None):
    image = scene() if image is None else image
    return {"exe": "Endfield.exe", "size": list(image.size),
            "image": restart._encode(image), "capturedAt": time.time()}


class FakeBackend:
    def __init__(self):
        self.time = 0.0
        self.rect = (-500, 20, 640, 360)
        self.image = scene()
        self.foreground = 77
        self.actual_exe = "endfield.exe"
        self.position = (-25, 305)
        self.down = False
        self.grabs = []
        self.fail_capture = False
        self.press_during_capture = False

    def now(self):
        return self.time

    def client_rect(self, hwnd):
        return self.rect

    def foreground_window(self):
        return self.foreground

    def cursor_pos(self):
        return self.position

    def left_down(self):
        return self.down

    def process_exe(self, hwnd):
        return self.actual_exe

    def grab(self, bounds):
        if self.fail_capture:
            raise OSError("capture unavailable")
        if self.down:
            raise AssertionError("A held mouse must never initiate a screenshot")
        self.grabs.append(bounds)
        x, y, w, h = self.rect
        image = self.image.resize((w, h), Image.Resampling.LANCZOS)
        crop = image.crop((bounds[0] - x, bounds[1] - y, bounds[2] - x, bounds[3] - y))
        if self.press_during_capture:
            self.down = True
        return crop


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.game = {"hwnd": 77, "sure": True, "exe": "Endfield.exe"}

    def test_capture_negative_monitor_client_coordinates(self):
        value = restart.capture_game(self.game, backend=self.backend)
        self.assertEqual(value["size"], [640, 360])
        self.assertEqual(value["exe"], "endfield.exe")
        self.assertEqual(self.backend.grabs, [(-500, 20, 140, 380)])
        self.assertEqual(restart._decode(value["image"]).size, (640, 360))
        self.assertIsInstance(value["capturedAt"], float)

    def test_capture_rejects_unsure_foreign_background_and_black(self):
        for updates in ({"sure": False}, {"hwnd": 0}, {"exe": "other.exe"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                restart.capture_game(dict(self.game, **updates), backend=self.backend)
        self.backend.foreground = 10
        with self.assertRaises(ValueError):
            restart.capture_game(self.game, backend=self.backend)
        self.backend.foreground = 77
        self.backend.image = Image.new("RGB", (640, 360))
        with self.assertRaises(ValueError):
            restart.capture_game(self.game, backend=self.backend)

    def test_make_target_only_persists_local_templates(self):
        target = restart.make_target(snapshot(), BUTTON, CONTEXT)
        self.assertEqual(set(target), {"enabled", "exe", "size", "button", "context"})
        self.assertEqual(restart._decode(target["button"]["image"]).size, (150, 50))
        self.assertEqual(restart._decode(target["context"]["image"]).size, (300, 100))
        self.assertNotIn("capturedAt", target)

    def test_target_rejects_bad_regions_and_plain_color(self):
        for box in ([-1, 0, 20, 20], [630, 1, 20, 20], [0, 0, 2, 2],
                    [0, 0, 20.5, 20], [0, 0, 30, 30]):
            with self.subTest(box=box), self.assertRaises(ValueError):
                restart.make_target(snapshot(), box, CONTEXT)

    def test_template_limit_and_disabled_normalization(self):
        target = restart.make_target(snapshot(), BUTTON, [150, 70, 400, 200])
        self.assertEqual(restart._decode(target["context"]["image"]).size, (320, 160))
        target["enabled"] = False
        target["extraneous"] = "removed"
        normalized = restart.validate_target(target)
        self.assertFalse(normalized["enabled"])
        self.assertNotIn("extraneous", normalized)

    def test_validation_rejects_corrupt_data_bounds_and_template_size(self):
        target = restart.make_target(snapshot(), BUTTON, CONTEXT)
        for mutate in (lambda t: t["button"].update(image="not base64"),
                       lambda t: t["button"].update(box=[640, 260, 150, 50]),
                       lambda t: t["button"].update(image=t["context"]["image"]),
                       lambda t: t.update(enabled="yes"),
                       lambda t: t.update(size=[640.0, 360])):
            value = copy.deepcopy(target)
            mutate(value)
            with self.assertRaises(ValueError):
                restart.validate_target(value)


class FakeEventSource:
    def __init__(self, backend):
        self.backend = backend
        self.callback = None
        self.stopped = False

    def start(self, callback):
        self.callback = callback
        self.stopped = False

    def stop(self):
        self.stopped = True

    def emit(self, kind, *, at=None, pos=None, foreground=None):
        if kind in ("down", "up"):
            self.backend.down = kind == "down"
        if pos is not None:
            self.backend.position = pos
        event = {"kind": kind, "at": self.backend.now() if at is None else at,
                 "pos": self.backend.position,
                 "foreground": self.backend.foreground if foreground is None else foreground}
        if not self.stopped:
            self.callback(event)
        return event


class BlockingBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.capture_entered = threading.Event()
        self.allow_capture = threading.Event()
        self.first_capture_done = threading.Event()

    def now(self):
        return time.monotonic()

    def grab(self, bounds):
        if self.grabs:
            self.capture_entered.set()
            if not self.allow_capture.wait(2):
                raise TimeoutError("test did not release the blocked capture")
        image = super().grab(bounds)
        self.first_capture_done.set()
        return image


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.events, self.logs = [], []
        self.game = {"hwnd": 77, "sure": True, "exe": "endfield.exe"}
        self.target = restart.make_target(snapshot(), BUTTON, CONTEXT)
        self.source = FakeEventSource(self.backend)
        self.watcher = self.build()

    def build(self):
        return restart.RestartButtonWatcher(
            self.target, lambda: self.events.append("restart"), lambda: self.game,
            self.logs.append, backend=self.backend, event_source=self.source)

    def tearDown(self):
        if hasattr(self.backend, "allow_capture"):
            self.backend.allow_capture.set()
        self.watcher.stop()

    def event(self, kind, at=None, pos=None, foreground=None):
        if at is None:
            self.backend.time += 0.02
            at = self.backend.time
        self.watcher._handle_event({"kind": kind, "at": at,
                                   "pos": self.backend.position if pos is None else pos,
                                   "foreground": self.backend.foreground if foreground is None else foreground})

    def click(self, at=None):
        self.event("down", at=at)
        self.event("up", at=None if at is None else at + .04)

    def wait_until(self, predicate, timeout=1):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(predicate(), self.watcher.status())

    def test_complete_click_uses_one_local_union_capture(self):
        self.watcher._capture_once()
        self.assertEqual(len(self.backend.grabs), 1)
        self.event("down")
        self.assertEqual(self.events, [])
        self.event("up")
        self.assertEqual(self.events, ["restart"])
        self.assertEqual(self.backend.grabs, [(-340, 100, 50, 330)])
        self.assertEqual(self.watcher.status()["state"], "restarted")
        self.assertTrue(any(line.startswith("restart button matched") for line in self.logs))

    def test_click_completed_during_capture_is_not_lost(self):
        # Actual watcher workers + event-source queue: input proceeds while the
        # capture worker is blocked, and the screen disappears upon release.
        self.backend = BlockingBackend()
        self.source = FakeEventSource(self.backend)
        self.watcher = self.build()
        self.watcher.start()
        self.wait_until(lambda: self.watcher.status()["state"] == "ready")
        self.assertTrue(self.backend.capture_entered.wait(1))
        self.source.emit("down")
        self.wait_until(lambda: self.watcher._armed is not None)
        self.source.emit("up")
        self.backend.image = Image.new("RGB", (640, 360), (24, 28, 35))
        self.wait_until(lambda: self.events == ["restart"])
        self.assertFalse(self.backend.allow_capture.is_set())
        self.backend.allow_capture.set()

    def test_delayed_input_processing_uses_original_event_times(self):
        self.watcher._capture_once()
        self.backend.time = .5  # Processing is delayed beyond the evidence window.
        self.click(at=.1)  # Actual click occurred while the old evidence was fresh.
        self.assertEqual(self.events, ["restart"])

    def test_new_mismatch_after_click_does_not_erase_original_preclick_evidence(self):
        self.watcher._capture_once()
        self.backend.time = .2
        self.backend.image = Image.new("RGB", (640, 360), (24, 28, 35))
        self.watcher._capture_once()
        self.click(at=.1)
        self.assertEqual(self.events, ["restart"])

    def test_latest_preclick_mismatch_beats_older_match(self):
        self.watcher._capture_once()
        self.backend.time = .1
        self.backend.image = Image.new("RGB", (640, 360), (24, 28, 35))
        self.watcher._capture_once()
        self.click(at=.2)
        self.assertEqual(self.events, [])
        self.assertIn("不匹配", self.watcher.status()["lastClick"])

    def test_incomplete_capture_is_never_preclick_evidence(self):
        self.watcher._capture_once()
        self.watcher._history[0]["completedAt"] = .2
        self.click(at=.1)
        self.assertEqual(self.events, [])
        self.assertIn("尚无完整", self.watcher.status()["lastClick"])

    def test_expiry_uses_real_capture_start_not_completion(self):
        self.watcher._capture_once()
        self.watcher._history[0]["completedAt"] = .3
        self.click(at=.36)
        self.assertEqual(self.events, [])
        self.assertIn("过期", self.watcher.status()["lastClick"])

    def test_foreground_prefetch_supports_quick_move_and_click(self):
        self.backend.position = (-450, 40)
        self.watcher._capture_once()
        self.assertEqual(len(self.backend.grabs), 1)
        self.assertEqual(self.watcher._capture_interval, .2)
        self.backend.position = (-25, 305)
        self.event("move", at=.10)
        self.click(at=.11)
        self.assertEqual(self.events, ["restart"])

    def test_prefetch_unhovered_button_then_quick_hover_click(self):
        image = scene()
        draw = ImageDraw.Draw(image)
        draw.rectangle((400, 260, 549, 309), fill=(35, 40, 48))
        for x in range(415, 535, 15):
            draw.rectangle((x, 273, x + 6, 296), fill=(235, 235, 235))
        self.backend.image = image
        self.backend.position = (-450, 40)
        self.watcher._capture_once()
        self.backend.image = scene()  # Hover returns to the saved button style.
        self.backend.position = (-25, 305)
        self.click(at=.12)  # No second grab has completed yet.
        self.assertEqual(self.events, ["restart"])

    def test_hover_invariance_does_not_accept_changed_button_glyphs(self):
        image = scene()
        draw = ImageDraw.Draw(image)
        draw.rectangle((400, 260, 549, 309), fill=(35, 40, 48))
        for x in range(415, 535, 15):
            draw.rectangle((x, 273, x + 6, 279), fill=(235, 235, 235))
            draw.rectangle((x, 290, x + 6, 296), fill=(235, 235, 235))
        self.backend.image = image
        self.watcher._capture_once()
        self.click(at=.12)
        self.assertEqual(self.events, [])

    def test_scaled_hover_state_keeps_antialiased_real_text_structure(self):
        font = ImageFont.load_default(size=20)
        reference = scene()
        draw = ImageDraw.Draw(reference)
        draw.rectangle((400, 260, 549, 309), fill=(190, 150, 55))
        draw.text((414, 272), "RESTART", font=font, fill=(20, 25, 32))
        self.target = restart.make_target(snapshot(reference), BUTTON, CONTEXT)
        self.watcher = self.build()
        draw.rectangle((400, 260, 549, 309), fill=(45, 50, 58))
        draw.text((414, 272), "RESTART", font=font, fill=(235, 235, 235))
        self.backend.image = reference
        self.backend.rect = (-1400, -200, 1280, 720)
        self.backend.position = (-450, 370)
        self.watcher._capture_once()
        self.click(at=.12)
        self.assertEqual(self.events, ["restart"])

    def test_worker_prefetch_then_fast_move_and_click_between_shots(self):
        self.backend = BlockingBackend()
        self.backend.position = (-450, 40)
        self.source = FakeEventSource(self.backend)
        self.watcher = self.build()
        self.watcher.start()
        self.wait_until(lambda: self.watcher.status()["state"] == "ready")
        self.source.emit("move", pos=(-25, 305))
        self.source.emit("down")
        self.source.emit("up")
        self.wait_until(lambda: self.events == ["restart"])
        self.assertEqual(len(self.backend.grabs), 1)

    def test_background_unsure_and_foreign_games_do_not_capture(self):
        for change in ("foreground", "sure", "exe", "actual_exe"):
            with self.subTest(change=change):
                self.setUp()
                if change == "foreground":
                    self.backend.foreground = 88
                elif change == "actual_exe":
                    self.backend.actual_exe = "other.exe"
                elif change == "sure":
                    self.game["sure"] = False
                else:
                    self.game["exe"] = "other.exe"
                self.watcher._capture_once()
                self.click()
                self.assertEqual(self.events, [])
                self.assertEqual(self.backend.grabs, [])
                self.assertIn(self.watcher.status()["state"], ("background", "waiting_game"))
                self.watcher.stop()

    def test_button_and_context_must_both_match(self):
        for box in (BUTTON, CONTEXT):
            with self.subTest(box=box):
                self.setUp()
                x, y, w, h = box
                ImageDraw.Draw(self.backend.image).rectangle((x, y, x + w, y + h), fill="white")
                self.watcher._capture_once()
                self.click()
                self.assertEqual(self.events, [])
                self.watcher.stop()

    def test_same_dark_background_different_white_text_rejected(self):
        reference = scene()
        draw = ImageDraw.Draw(reference)
        draw.rectangle((160, 80, 459, 179), fill=(32, 34, 38))
        draw.text((172, 110), "Restart the battle?", fill=(235, 235, 235))
        self.target = restart.make_target(snapshot(reference), BUTTON, CONTEXT)
        self.watcher = self.build()
        draw.rectangle((160, 80, 459, 179), fill=(32, 34, 38))
        draw.text((172, 110), "Return to the lobby?", fill=(235, 235, 235))
        self.backend.image = reference
        self.watcher._capture_once()
        self.click()
        self.assertEqual(self.events, [])

    def test_drag_outside_cancels_even_if_returned(self):
        self.watcher._capture_once()
        self.event("down")
        self.event("move", pos=(-300, 100))
        self.event("up")
        self.assertEqual(self.events, [])

    def test_release_outside_or_foreign_foreground_cancels(self):
        for change in ("position", "foreground", "rect"):
            with self.subTest(change=change):
                self.setUp()
                self.watcher._capture_once()
                self.event("down")
                if change == "position":
                    self.event("up", pos=(-400, 40))
                elif change == "foreground":
                    self.event("up", foreground=88)
                else:
                    self.backend.rect = (-490, 20, 640, 360)
                    self.event("up")
                self.assertEqual(self.events, [])
                self.watcher.stop()

    def test_held_mouse_never_completes_until_release(self):
        self.watcher._capture_once()
        self.event("down")
        self.event("move")
        self.assertEqual(self.events, [])
        self.event("up", at=2)
        self.event("up", at=3)
        self.assertEqual(self.events, ["restart"])

    def test_repeat_cooldown_then_recovery(self):
        self.watcher._capture_once()
        self.click(at=.1)
        self.click(at=.2)
        self.assertEqual(self.events, ["restart"])
        self.backend.time = 2
        self.watcher._capture_once()
        self.click(at=2.1)
        self.assertEqual(self.events, ["restart", "restart"])

    def test_uniform_scaling_on_negative_coordinates(self):
        self.backend.rect = (-1400, -200, 1280, 720)
        self.backend.position = (-450, 370)
        self.watcher._capture_once()
        self.click()
        self.assertEqual(self.events, ["restart"])

    def test_changed_aspect_ratio_rejected(self):
        self.backend.rect = (-500, 20, 800, 360)
        with self.assertRaisesRegex(ValueError, "宽高比"):
            self.watcher._capture_once()

    def test_stop_cancels_armed_click_and_is_idempotent(self):
        self.watcher._capture_once()
        self.event("down")
        self.watcher.stop()
        self.event("up")
        self.watcher.stop()
        self.assertEqual(self.events, [])
        self.assertEqual(self.watcher.status()["state"], "stopped")
        self.assertTrue(self.source.stopped)

    def test_thread_start_stop_cleans_both_workers_and_source(self):
        self.watcher.start()
        thread = self.watcher._thread
        self.watcher.start()
        self.assertIs(thread, self.watcher._thread)
        self.watcher.stop()
        self.assertFalse(self.watcher._thread.is_alive())
        self.assertFalse(self.watcher._input_thread.is_alive())
        self.assertTrue(self.source.stopped)

    def test_source_failure_is_synchronous(self):
        def fail(callback):
            raise RuntimeError("hook unavailable")
        self.source.start = fail
        with self.assertRaisesRegex(RuntimeError, "hook unavailable"):
            self.watcher.start()
        self.assertTrue(self.source.stopped)
        self.assertEqual(self.watcher.status()["state"], "stopped")

    def test_capture_errors_are_visible_and_throttled(self):
        self.backend.fail_capture = True
        self.watcher.start()
        self.wait_until(lambda: self.watcher.status()["state"] == "error")
        self.watcher.stop()
        self.assertEqual(len(self.logs), 1)
        self.assertEqual(self.events, [])

    def test_rejected_click_logs_throttled_reason(self):
        self.click(at=.1)
        self.click(at=.2)
        self.assertEqual(len(self.logs), 1)
        self.assertIn("尚无完整", self.logs[0])

    def test_disabled_target_does_not_start(self):
        self.target["enabled"] = False
        self.watcher = self.build()
        self.watcher.start()
        self.assertIsNone(self.watcher._thread)
        self.assertEqual(self.watcher.status()["state"], "stopped")

    def test_native_event_timestamp_wrap(self):
        convert = restart.WindowsMouseEventSource._event_time
        self.assertAlmostEqual(convert(0xfffffff0, 0x100000010, 100), 99.968)
        self.assertAlmostEqual(convert(100, 120, 100), 99.98)


if __name__ == "__main__":
    unittest.main()
