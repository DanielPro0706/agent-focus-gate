"""Isolated regression checks for overlapping project and global timers."""

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "focus_gate.py"
SPEC = importlib.util.spec_from_file_location("focus_gate_overlap", MODULE_PATH)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class TimerOverlapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="focus-gate-overlap-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = datetime.fromisoformat("2026-11-14T10:00:00+08:00").astimezone(gate.UTC)

    def at(self, minutes):
        return self.now + timedelta(minutes=minutes)

    def new_scope(self, name):
        root = self.root / name
        parent = root / "Project"
        child = parent / "src"
        descendant = child / "nested"
        sibling = parent / "tests"
        other = root / "Project Archive"
        descendant.mkdir(parents=True)
        sibling.mkdir()
        other.mkdir()
        store = gate.StateStore(root / "state")
        return SimpleNamespace(
            store=store, controller=gate.Controller(store), parent=parent,
            child=child, descendant=descendant, sibling=sibling, other=other,
        )

    def assert_restriction(self, scope, folder, now, end, remaining):
        status = scope.store.status(now, folder=folder)
        self.assertTrue(status["restricted"])
        self.assertEqual(gate._restriction_end(status, now, folder), end)
        self.assertEqual(
            gate._restriction_message(status, now, folder),
            gate.MESSAGE + "。剩余 " + remaining,
        )

    def test_parent_first_rejects_child_without_changing_state(self):
        scope = self.new_scope("parent-first")
        scope.controller.start_timer(30, now=self.now, folder=scope.parent)
        before = scope.store.path.read_bytes()
        for elapsed in (5, 30, 89):
            for folder in (scope.child, scope.descendant):
                with self.subTest(elapsed=elapsed, folder=folder):
                    with self.assertRaisesRegex(ValueError, "计时已开始"):
                        scope.controller.start_timer(60, now=self.at(elapsed), folder=folder)
                    self.assertEqual(scope.store.path.read_bytes(), before)

        result = scope.controller.start_timer(60, now=self.at(90), folder=scope.child)
        self.assertEqual(result["folder_timers"][0]["phase"], "using")
        self.assertFalse(result["restricted"])

    def test_child_first_preserves_its_timer_when_parent_starts(self):
        for elapsed, child_restricted in ((5, False), (45, True)):
            with self.subTest(parent_start=elapsed):
                scope = self.new_scope("child-first-%d" % elapsed)
                scope.controller.start_timer(30, now=self.now, folder=scope.child)
                original = scope.store.read()["folder_timers"][0]
                result = scope.controller.start_timer(60, now=self.at(elapsed), folder=scope.parent)
                saved = scope.store.read()["folder_timers"]
                self.assertEqual(len(saved), 2)
                self.assertEqual(saved[0], original)
                self.assertFalse(result["restricted"])
                self.assertEqual(result["folder_timers"][0]["phase"], "using")
                self.assertEqual(
                    scope.store.status(self.at(elapsed), folder=scope.child)["restricted"],
                    child_restricted,
                )
                if child_restricted:
                    self.assert_restriction(scope, scope.child, self.at(45), self.at(90), "45 分钟")

    def test_cooldown_union_stops_at_the_first_available_instant(self):
        # The child cools from 10:30 to 11:30; the parent starts at 10:05.
        cases = (
            ("overlap", 60, 45, 125, 30, "1 小时 20 分钟"),
            ("touching", 85, 45, 150, 30, "1 小时 45 分钟"),
            ("gap", 120, 45, 90, 30, "45 分钟"),
            ("parent-finishes-first", 15, 25, 90, 20, "1 小时 5 分钟"),
        )
        for name, duration, query, end, first_start, remaining in cases:
            with self.subTest(case=name):
                scope = self.new_scope(name)
                scope.controller.start_timer(30, now=self.now, folder=scope.child)
                scope.controller.start_timer(duration, now=self.at(5), folder=scope.parent)
                self.assertFalse(scope.store.status(
                    self.at(first_start) - timedelta(microseconds=1), folder=scope.child,
                )["restricted"])
                self.assertTrue(scope.store.status(self.at(first_start), folder=scope.child)["restricted"])
                self.assert_restriction(scope, scope.child, self.at(query), self.at(end), remaining)
                self.assert_restriction(
                    scope, scope.child, self.at(end) - timedelta(seconds=22), self.at(end), "22 秒",
                )
                self.assert_restriction(
                    scope, scope.child, self.at(end) - timedelta(microseconds=1), self.at(end), "1 秒",
                )
                self.assertFalse(scope.store.status(self.at(end), folder=scope.child)["restricted"])
                if name == "gap":
                    self.assertFalse(scope.store.status(self.at(124), folder=scope.child)["restricted"])
                    self.assert_restriction(scope, scope.child, self.at(125), self.at(185), "1 小时")

    def test_parent_child_sibling_and_unrelated_scopes_remain_independent(self):
        scope = self.new_scope("scope-isolation")
        scope.controller.start_timer(30, now=self.now, folder=scope.child)
        scope.controller.start_timer(60, now=self.at(5), folder=scope.parent)
        for folder in (scope.child, scope.descendant):
            self.assert_restriction(scope, folder, self.at(45), self.at(125), "1 小时 20 分钟")
        for folder in (scope.parent, scope.sibling, scope.other):
            with self.subTest(folder=folder, elapsed=45):
                self.assertFalse(scope.store.status(self.at(45), folder=folder)["restricted"])

        for folder in (scope.parent, scope.child, scope.descendant, scope.sibling):
            with self.subTest(folder=folder, elapsed=80):
                self.assert_restriction(scope, folder, self.at(80), self.at(125), "45 分钟")
                self.assertFalse(scope.store.status(self.at(125), folder=folder)["restricted"])
        unrelated = scope.store.status(self.at(80), folder=scope.other)
        self.assertFalse(unrelated["restricted"])
        self.assertNotIn("folder_timers", unrelated)
        self.assert_restriction(scope, scope.child, self.at(95), self.at(125), "30 分钟")

    def test_symbolic_link_aliases_use_the_same_timers_and_conflicts(self):
        scope = self.new_scope("symbolic-links")
        alias = self.root / "Project Alias"
        alias.symlink_to(scope.parent, target_is_directory=True)
        scope.controller.start_timer(30, now=self.now, folder=alias / "src")
        scope.controller.start_timer(60, now=self.at(5), folder=alias)
        self.assertEqual(
            [timer["folder"] for timer in scope.store.read()["folder_timers"]],
            [str(scope.child.resolve()), str(scope.parent.resolve())],
        )
        for folder in (scope.child, alias / "src", alias / "src/nested"):
            with self.subTest(folder=folder):
                self.assert_restriction(scope, folder, self.at(45), self.at(125), "1 小时 20 分钟")
        before = scope.store.path.read_bytes()
        for folder in (alias, alias / "src"):
            with self.subTest(restart=folder):
                with self.assertRaisesRegex(ValueError, "计时已开始"):
                    scope.controller.start_timer(60, now=self.at(45), folder=folder)
                self.assertEqual(scope.store.path.read_bytes(), before)

    def test_global_timer_started_later_preserves_project_cooldown(self):
        scope = self.new_scope("late-global")
        scope.controller.start_timer(30, now=self.now, folder=scope.child)
        original = scope.store.read()["folder_timers"]
        result = scope.controller.start_timer(60, now=self.at(45))
        self.assertEqual(scope.store.read()["folder_timers"], original)
        self.assertEqual(result["timer"]["phase"], "using")
        self.assertFalse(result["restricted"])
        self.assert_restriction(scope, scope.child, self.at(45), self.at(90), "45 分钟")
        for folder in (scope.parent, scope.sibling, scope.other):
            self.assertFalse(scope.store.status(self.at(45), folder=folder)["restricted"])
        self.assertFalse(scope.store.status(self.at(90), folder=scope.child)["restricted"])
        for folder in (scope.parent, scope.child, scope.sibling, scope.other):
            with self.subTest(folder=folder):
                self.assert_restriction(scope, folder, self.at(105), self.at(165), "1 小时")
                self.assertFalse(scope.store.status(self.at(165), folder=folder)["restricted"])


if __name__ == "__main__":
    unittest.main()
