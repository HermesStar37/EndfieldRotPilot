"""Exercise the real overlay lifecycle without importing app_web or using input devices."""

import ast
import copy
import queue
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "app_web.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8-sig"), filename=str(SOURCE))
OVERLAY_NODE = next(node for node in TREE.body
                    if isinstance(node, ast.ClassDef) and node.name == "Overlay")


class FakeClock:
    now = 1.0

    def time(self):
        return self.now


class FakeRoot:
    def __init__(self, clock):
        self.clock = clock
        self.jobs = {}
        self.next_job = 0
        self.hidden = False
        self.destroyed = False

    def after(self, delay, callback):
        self.next_job += 1
        job = "after%d" % self.next_job
        self.jobs[job] = (self.clock.now + delay / 1000, callback)
        return job

    def after_cancel(self, job):
        self.jobs.pop(job, None)

    def advance(self, milliseconds):
        until = self.clock.now + milliseconds / 1000
        while self.jobs:
            job = min(self.jobs, key=lambda key: self.jobs[key][0])
            when, callback = self.jobs[job]
            if when > until + 0.000001:
                break
            del self.jobs[job]
            self.clock.now = when
            callback()
        self.clock.now = until

    def withdraw(self):
        self.hidden = True

    def deiconify(self):
        self.hidden = False

    def destroy(self):
        self.destroyed = True

    def quit(self):
        pass

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeFrame:
    def __init__(self, *args, **kwargs):
        pass

    def pack(self):
        pass

    def bind(self, *args, **kwargs):
        pass


def load_overlay(clock):
    namespace = {
        "queue": queue, "time": clock, "log": lambda *args: None,
        "normalize_keys": lambda raw: {}, "normalize_pad": lambda raw: raw or {},
        "clamp_alpha": float, "pick_screen": lambda: 0,
        "_rep_times": lambda value: int(value or 2),
    }
    module = ast.Module(body=[OVERLAY_NODE], type_ignores=[])
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["Overlay"]


TARGET = {"enabled": True, "button": [0.5, 0.5],
          "context": {"sample": "test"}, "size": [1920, 1080],
          "exe": "endfield.exe"}


class OverlayRestartTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.Overlay = load_overlay(self.clock)
        keyboard = types.SimpleNamespace(unhook_all=lambda: None)
        self.keyboard_patch = patch.dict(sys.modules, {"keyboard": keyboard})
        self.keyboard_patch.start()
        self.addCleanup(self.keyboard_patch.stop)

    def overlay(self, steps=None, target=TARGET):
        root = FakeRoot(self.clock)
        tk = types.SimpleNamespace(Tk=lambda: root, Frame=FakeFrame)
        pil = types.ModuleType("PIL")
        pil.Image = pil.ImageTk = types.SimpleNamespace()
        with patch.dict(sys.modules, {"tkinter": tk, "PIL": pil}), \
                patch.object(self.Overlay, "_render", lambda ov: ov._settle_passive()), \
                patch.object(self.Overlay, "_place", lambda ov: None), \
                patch.object(self.Overlay, "_bind_drag", lambda *args: None), \
                patch.object(self.Overlay, "_paint", lambda ov: None), \
                patch.object(self.Overlay, "_hook", lambda ov: None), \
                patch.object(self.Overlay, "_tick", lambda ov: None), \
                patch.object(self.Overlay, "_keep_top", lambda ov: None):
            ov = self.Overlay(steps or [{"key": "1"}], [],
                              restart_button=copy.deepcopy(target))
        ov._render = lambda: ov._settle_passive()
        ov._place = lambda: None
        ov._bind_drag = lambda *args: None
        ov._paint = lambda: None
        return ov

    def test_finished_axis_waits_hidden_and_restarts_on_battle_restart(self):
        ov = self.overlay()
        ov._handle("down", "skill1")
        ov._handle("up", "skill1")
        ov.root.advance(400)
        self.assertTrue(ov.running)
        self.assertTrue(ov.root.hidden)
        ov._handle("down", "skill1")
        ov._handle("up", "skill1")
        self.assertEqual(ov.idx, 1)
        ov._handle("battle_restart", "")
        ov.root.advance(150)
        self.assertTrue(ov.running)
        self.assertFalse(ov.root.hidden)
        self.assertEqual((ov.seg, ov.idx), (0, 0))

    def test_unconfigured_disabled_or_incomplete_targets_keep_completion_close(self):
        for target in (None, {}, dict(TARGET, enabled=False),
                       {"enabled": True, "button": [0.5, 0.5]}):
            with self.subTest(target=target):
                ov = self.overlay(target=target)
                ov._fire("skill", 1)
                ov._handle("battle_restart", "")
                self.assertEqual(ov.idx, 1)
                ov.root.advance(400)
                self.assertFalse(ov.running)
                self.assertTrue(ov.root.destroyed)

    def test_manual_resets_cancel_previous_completion_close(self):
        for reset in (lambda ov: ov._handle("down", "reset"),
                      lambda ov: ov._goto_start()):
            with self.subTest(reset=reset):
                ov = self.overlay(target=None)
                ov._fire("skill", 1)
                stale_callbacks = [callback for _, callback in ov.root.jobs.values()]
                reset(ov)
                ov.root.advance(500)
                for callback in stale_callbacks:
                    callback()
                self.assertTrue(ov.running)
                self.assertFalse(ov.root.destroyed)
                self.assertEqual((ov.seg, ov.idx), (0, 0))

    def test_restart_restores_repeat_counts_and_clears_held_inputs(self):
        steps = [{"rep": 1, "link": "repeat"}, {"key": "1"},
                 {"rep": 1, "link": "repeat", "end": True, "times": 3},
                 {"divider": 1, "name": "second"}, {"key": "2"}]
        ov = self.overlay(steps)
        ov._fire("skill", 1)
        self.assertEqual(list(ov.left.values()), [2])
        ov.rep_done["repeat#0"] = 4
        ov.passes = 5
        ov.pad_mod = ov.pad_ult_mod = True
        ov.pad_faces[1] = self.clock.time()
        ov.pad_link_hit.add(1)
        ov.pad_ult_hit.add(2)
        ov.long_fired.add("skill1")
        ov.press_at["skill1"] = self.clock.time()
        ov._handle("battle_restart", "")
        self.assertEqual((ov.seg, ov.idx, ov.passes), (0, 0, 0))
        self.assertEqual(list(ov.left.values()), [3])
        self.assertEqual(ov.rep_done, {})
        self.assertEqual(ov.press_at, {})
        self.assertEqual(ov.pad_faces, {})
        self.assertFalse(ov.pad_mod or ov.pad_ult_mod)
        self.assertFalse(ov.pad_link_hit or ov.pad_ult_hit or ov.long_fired)

    def test_old_long_press_callback_cannot_fire_for_new_press(self):
        ov = self.overlay([{"key": "1", "hold": True}, {"key": "2"}])
        ov._handle("down", "skill1")
        old_callbacks = [callback for _, callback in ov.root.jobs.values()]
        ov._handle("battle_restart", "")
        ov.root.advance(150)
        ov._handle("down", "skill1")
        self.clock.now += 1
        for callback in old_callbacks:
            callback()
        self.assertEqual(ov.idx, 0)
        ov._check_long("skill1")
        self.assertEqual(ov.idx, 1)

    def test_old_pad_combination_wait_cannot_duplicate_new_ultimate(self):
        ov = self.overlay([{"key": "1", "hold": True},
                           {"key": "1", "hold": True}, {"key": "2"}])
        ov._handle("down", "slot1")
        old_callbacks = [callback for _, callback in ov.root.jobs.values()]
        ov._handle("battle_restart", "")
        ov._handle("down", "slot1")
        ov._handle("down", "pad_ult_mod")
        ov._handle("down", "pad_mod")
        self.assertEqual(ov.idx, 1)
        for callback in old_callbacks:
            callback()
        self.assertEqual(ov.idx, 1)
        ov.root.advance(150)
        self.assertEqual(ov.idx, 1)

    def test_pending_segment_render_cannot_show_finished_waiting_axis(self):
        ov = self.overlay([{"key": "1"}, {"divider": 1}, {"key": "2"}])
        ov._fire("skill", 1)
        old_callbacks = [callback for _, callback in ov.root.jobs.values()]
        ov._fire("skill", 2)
        ov.root.advance(400)
        self.assertTrue(ov.root.hidden)
        for callback in old_callbacks:
            callback()
        self.assertTrue(ov.root.hidden)
        self.assertTrue(ov.running)

    def test_input_queues_and_generations_do_not_leak_into_restarted_axis(self):
        ov = self.overlay([{"key": "1"}, {"key": "2"}])
        another = self.overlay()
        self.assertIsNot(ov.q, another.q)
        old_generation = ov._input_generation
        ov.q.put(("battle_restart", ""))
        ov.q.put((old_generation, "down", "skill1"))
        ov.q.put((old_generation, "up", "skill1"))
        ov._tick()
        self.assertEqual(ov.idx, 0)
        ov._queue_input("down", "skill1", old_generation)
        ov.q.put((old_generation, "down", "skill1"))
        ov.q.put((old_generation, "up", "skill1"))
        ov._queue_input("down", "skill1", ov._input_generation)
        ov._queue_input("up", "skill1", ov._input_generation)
        ov._tick()
        self.assertEqual(ov.idx, 1)
        self.assertEqual(another.idx, 0)

    def test_exit_and_stop_close_waiting_axis_and_stop_watcher(self):
        for stop in (lambda ov: ov._handle("down", "exit"),
                     lambda ov: (setattr(ov, "_stop_req", True), ov._tick())):
            with self.subTest(stop=stop):
                ov = self.overlay()
                stopped = []
                ov._restart_watcher = types.SimpleNamespace(stop=lambda: stopped.append(True))
                ov._fire("skill", 1)
                ov.root.advance(400)
                stop(ov)
                ov._handle("battle_restart", "")
                ov.root.advance(400)
                self.assertFalse(ov.running)
                self.assertTrue(ov.root.destroyed)
                self.assertEqual(stopped, [True])

    def test_queued_exit_survives_restart_queue_clear(self):
        ov = self.overlay()
        ov.q.put(("battle_restart", ""))
        ov.q.put((ov._input_generation, "down", "exit"))
        ov._tick()
        self.assertFalse(ov.running)
        self.assertTrue(ov.root.destroyed)


if __name__ == "__main__":
    unittest.main()
