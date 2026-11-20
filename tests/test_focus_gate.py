"""Boundary, authentication, and documented hook-contract checks for FocusGate."""

import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "focus_gate.py"
SPEC = importlib.util.spec_from_file_location("focus_gate", MODULE_PATH)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

MESSAGE = "当前处于限制使用时段，请使用Chat模式或输入密码"


def instant(value):
    return datetime.fromisoformat(value)


def policy(daily=None, once=None, enabled=True, timezone="Asia/Shanghai"):
    return gate.normalize_policy({
        "enabled": enabled,
        "timezone": timezone,
        "daily": [] if daily is None else daily,
        "once": [] if once is None else once,
    })


class ScheduleTests(unittest.TestCase):
    def test_daily_interval_is_left_closed_right_open(self):
        configured = policy(daily=[{"days": list(range(7)), "start": "09:00", "end": "12:00"}])
        for value, expected in [
            ("2026-11-16T08:59:59+08:00", False),
            ("2026-11-16T09:00:00+08:00", True),
            ("2026-11-16T11:59:59+08:00", True),
            ("2026-11-16T12:00:00+08:00", False),
        ]:
            with self.subTest(value=value):
                self.assertEqual(gate.is_scheduled(configured, instant(value)), expected)

    def test_weekdays_use_monday_zero(self):
        configured = policy(daily=[{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "12:00"}])
        self.assertTrue(gate.is_scheduled(configured, instant("2026-11-16T10:00:00+08:00")))
        self.assertFalse(gate.is_scheduled(configured, instant("2026-11-14T10:00:00+08:00")))
        self.assertFalse(gate.is_scheduled(configured, instant("2026-11-15T10:00:00+08:00")))

    def test_overnight_uses_day_the_interval_started(self):
        configured = policy(daily=[{"days": [4], "start": "22:00", "end": "02:00"}])
        for value, expected in [
            ("2026-11-13T01:00:00+08:00", False),
            ("2026-11-13T21:59:59+08:00", False),
            ("2026-11-13T22:00:00+08:00", True),
            ("2026-11-14T01:59:59+08:00", True),
            ("2026-11-14T02:00:00+08:00", False),
            ("2026-11-14T22:00:00+08:00", False),
        ]:
            with self.subTest(value=value):
                self.assertEqual(gate.is_scheduled(configured, instant(value)), expected)

    def test_overnight_wraps_sunday_to_monday(self):
        configured = policy(daily=[{"days": [6], "start": "23:00", "end": "01:00"}])
        self.assertTrue(gate.is_scheduled(configured, instant("2026-11-16T00:30:00+08:00")))
        self.assertFalse(gate.is_scheduled(configured, instant("2026-11-15T00:30:00+08:00")))

    def test_now_is_converted_to_policy_timezone(self):
        configured = policy(daily=[{"days": [0], "start": "09:00", "end": "10:00"}])
        self.assertTrue(gate.is_scheduled(configured, instant("2026-11-16T01:30:00+00:00")))
        self.assertFalse(gate.is_scheduled(configured, instant("2026-11-16T02:00:00+00:00")))

    def test_one_off_naive_dates_use_selected_timezone(self):
        configured = policy(once=[{"start": "2026-11-14T09:00", "end": "2026-11-14T10:00"}])
        self.assertFalse(gate.is_scheduled(configured, instant("2026-11-14T00:59:59+00:00")))
        self.assertTrue(gate.is_scheduled(configured, instant("2026-11-14T01:00:00+00:00")))
        self.assertFalse(gate.is_scheduled(configured, instant("2026-11-14T02:00:00+00:00")))

    def test_one_off_explicit_offsets_compare_absolute_time(self):
        configured = policy(once=[{"start": "2026-11-14T09:00:00+08:00", "end": "2026-11-14T02:00:00+00:00"}])
        self.assertTrue(gate.is_scheduled(configured, instant("2026-11-14T01:30:00+00:00")))
        self.assertFalse(gate.is_scheduled(configured, instant("2026-11-15T01:30:00+00:00")))

    def test_multiple_intervals_form_a_union(self):
        configured = policy(
            daily=[{"days": [0], "start": "09:00", "end": "10:00"}],
            once=[{"start": "2026-11-14T14:00+08:00", "end": "2026-11-14T15:00+08:00"}],
        )
        self.assertTrue(gate.is_scheduled(configured, instant("2026-11-14T14:30:00+08:00")))
        self.assertTrue(gate.is_scheduled(configured, instant("2026-11-16T09:30:00+08:00")))
        self.assertFalse(gate.is_scheduled(configured, instant("2026-11-16T14:30:00+08:00")))

    def test_disabled_policy_never_restricts(self):
        configured = policy(daily=[{"days": list(range(7)), "start": "09:00", "end": "12:00"}], enabled=False)
        self.assertFalse(gate.is_scheduled(configured, instant("2026-11-16T10:00:00+08:00")))

    def test_empty_schedule_never_restricts(self):
        self.assertFalse(gate.is_scheduled(policy(), instant("2026-11-16T10:00:00+08:00")))

    def test_invalid_daily_intervals_rejected(self):
        for interval in [
            {"days": [0], "start": "09:00", "end": "09:00"},
            {"days": [0], "start": "24:00", "end": "10:00"},
            {"days": [0], "start": "09:60", "end": "10:00"},
            {"days": [0], "start": "9:00", "end": "10:00"},
            {"days": [7], "start": "09:00", "end": "10:00"},
            {"days": [-1], "start": "09:00", "end": "10:00"},
            {"days": [], "start": "09:00", "end": "10:00"},
        ]:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                policy(daily=[interval])

    def test_invalid_one_off_intervals_rejected(self):
        for interval in [
            {"start": "2026-11-14T10:00", "end": "2026-11-14T09:00"},
            {"start": "2026-11-14T10:00", "end": "2026-11-14T10:00"},
            {"start": "2026-02-30T10:00", "end": "2026-03-01T10:00"},
            {"start": "not-a-date", "end": "2026-11-14T10:00"},
        ]:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                policy(once=[interval])

    def test_unknown_timezone_rejected(self):
        with self.assertRaises(ValueError):
            policy(timezone="Not/A_Timezone")

    def test_validation_does_not_mutate_caller_data(self):
        original = {"enabled": True, "timezone": "Asia/Shanghai", "daily": [{"days": [4, 0], "start": "09:00", "end": "10:00"}], "once": []}
        snapshot = copy.deepcopy(original)
        gate.normalize_policy(original)
        self.assertEqual(original, snapshot)


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "private-local-test-password"
        cls.record = gate.make_password(cls.password)

    def test_password_hash_verification(self):
        self.assertTrue(gate.verify_password(self.password, self.record))
        self.assertFalse(gate.verify_password("incorrect-password", self.record))

    def test_password_not_stored_as_plaintext(self):
        self.assertNotIn(self.password, repr(self.record))

    def test_password_salt_is_random(self):
        self.assertNotEqual(self.record, gate.make_password(self.password))

    def test_policy_fingerprint_is_stable(self):
        configured = policy(daily=[{"days": [0], "start": "09:00", "end": "10:00"}])
        self.assertEqual(gate.policy_fingerprint(configured), gate.policy_fingerprint(copy.deepcopy(configured)))
        changed = copy.deepcopy(configured)
        changed["enabled"] = False
        self.assertNotEqual(gate.policy_fingerprint(configured), gate.policy_fingerprint(changed))


class StateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = gate.make_password("private-local-state-password")

    def setUp(self):
        self.configured = policy(daily=[{"days": list(range(7)), "start": "09:00", "end": "12:00"}])
        self.state = {"version": 1, "password": copy.deepcopy(self.record), "policy": self.configured, "unlock": None}
        self.now = instant("2026-11-14T10:00:00+08:00")

    def unlock(self, issued="2026-11-14T09:55:00+08:00", until="2026-11-14T10:10:00+08:00", minutes=15):
        self.state["unlock"] = {"issued_at": issued, "until": until, "minutes": minutes, "policy_hash": gate.policy_fingerprint(self.configured)}

    def test_missing_state_is_unconfigured_and_allows(self):
        status = gate.evaluate_state(None, self.now)
        self.assertFalse(status["configured"])
        self.assertFalse(status["restricted"])

    def test_configured_locked_state_restricts(self):
        status = gate.evaluate_state(self.state, self.now)
        self.assertTrue(status["configured"])
        self.assertTrue(status["restricted"])
        self.assertEqual(status["message"], MESSAGE)

    def test_valid_unlock_allows(self):
        self.unlock()
        status = gate.evaluate_state(self.state, self.now)
        self.assertFalse(status["restricted"])
        self.assertIsNotNone(status["unlocked_until"])

    def test_expiration_is_exclusive(self):
        self.unlock()
        self.assertTrue(gate.evaluate_state(self.state, instant("2026-11-14T10:10:00+08:00"))["restricted"])

    def test_clock_rollback_cannot_precede_unlock_issue(self):
        self.unlock(issued="2026-11-14T10:01:00+08:00", until="2026-11-14T10:16:00+08:00")
        self.assertTrue(gate.evaluate_state(self.state, self.now)["restricted"])

    def test_policy_change_invalidates_unlock(self):
        self.unlock()
        self.state["policy"] = policy(daily=[{"days": list(range(7)), "start": "08:00", "end": "12:00"}])
        self.assertTrue(gate.evaluate_state(self.state, self.now)["restricted"])

    def test_overlong_unlock_is_rejected(self):
        self.unlock(issued="2026-11-14T09:55:00+08:00", until="2026-11-14T12:55:00+08:00", minutes=180)
        self.assertTrue(gate.evaluate_state(self.state, self.now)["restricted"])

    def test_corrupt_state_cannot_silently_allow(self):
        try:
            status = gate.evaluate_state({"version": 999, "policy": {}}, self.now)
        except ValueError:
            return
        self.assertTrue(status["restricted"])

    def test_outside_interval_does_not_require_unlock(self):
        self.assertFalse(gate.evaluate_state(self.state, instant("2026-11-14T13:00:00+08:00"))["restricted"])


class HookContractTests(unittest.TestCase):
    def test_user_prompt_is_blocked(self):
        result = gate.hook_output("UserPromptSubmit", True, MESSAGE)
        self.assertEqual(result["decision"], "block")
        self.assertEqual(result["reason"], MESSAGE)

    def test_tool_uses_supported_deny_shape(self):
        result = gate.hook_output("PreToolUse", True, MESSAGE)
        output = result["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertEqual(output["permissionDecisionReason"], MESSAGE)
        self.assertNotIn("continue", result)

    def test_permission_request_uses_supported_deny_shape(self):
        output = gate.hook_output("PermissionRequest", True, MESSAGE)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PermissionRequest")
        self.assertEqual(output["decision"]["behavior"], "deny")
        self.assertEqual(output["decision"]["message"], MESSAGE)

    def test_session_start_stops(self):
        result = gate.hook_output("SessionStart", True, MESSAGE)
        self.assertIs(result["continue"], False)
        self.assertEqual(result["stopReason"], MESSAGE)

    def test_unrestricted_events_emit_no_policy_change(self):
        for event in ["SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest"]:
            with self.subTest(event=event):
                self.assertEqual(gate.hook_output(event, False, MESSAGE), {})


class CountdownTests(unittest.TestCase):
    def message(self, now, configured, timer=None):
        now = instant(now).astimezone(gate.UTC)
        status = {"policy": configured, "timer": gate._timer_status(timer, now)}
        return gate._restriction_message(status, now)

    def test_cross_midnight_schedule_joins_contiguous_once_and_cooldown(self):
        configured = policy(
            daily=[{"days": [6], "start": "23:00", "end": "00:05"}],
            once=[{"start": "2026-11-16T00:05+08:00", "end": "2026-11-16T00:30+08:00"},
                  {"start": "2026-11-16T01:31+08:00", "end": "2026-11-16T02:00+08:00"}],
        )
        timer = {"started_at": "2026-11-15T23:30+08:00", "allowed_until": "2026-11-16T00:30+08:00",
                 "lock_until": "2026-11-16T01:30+08:00"}
        self.assertEqual(self.message("2026-11-16T00:00+08:00", configured, timer),
                         MESSAGE + "。剩余 1 小时 30 分钟")

    def test_reports_seconds_in_last_minute_of_cooldown(self):
        timer = {"started_at": "2026-11-16T00:00+08:00", "allowed_until": "2026-11-16T00:30+08:00",
                 "lock_until": "2026-11-16T01:30+08:00"}
        self.assertEqual(self.message("2026-11-16T01:29:38.250000+08:00", policy(enabled=False), timer),
                         MESSAGE + "。剩余 22 秒")

    def test_rollback_ends_at_timer_start(self):
        timer = {"started_at": "2026-11-16T00:00+08:00", "allowed_until": "2026-11-16T00:30+08:00",
                 "lock_until": "2026-11-16T01:30+08:00"}
        self.assertEqual(self.message("2026-11-15T23:44+08:00", policy(), timer),
                         MESSAGE + "。剩余 16 分钟")

    def test_daily_schedule_observes_repeated_local_time(self):
        configured = policy(daily=[{"days": [6], "start": "01:30", "end": "02:30"}],
                            timezone="America/New_York")
        self.assertEqual(self.message("2026-11-01T01:40-04:00", configured), MESSAGE + "。剩余 20 分钟")

    def test_continuous_weekly_rules_have_no_automatic_end(self):
        configured = policy(daily=[{"days": list(range(7)), "start": "00:00", "end": "12:00"},
                                   {"days": list(range(7)), "start": "12:00", "end": "00:00"}])
        self.assertEqual(self.message("2026-11-16T00:00+08:00", configured),
                         MESSAGE + "。当前规则没有自动结束时间")


class TimerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.record = gate.make_password("private-local-timer-password")

    def setUp(self):
        self.state = {
            "version": 1,
            "password": copy.deepcopy(self.record),
            "policy": policy(),
            "unlock": None,
            "timer": {
                "started_at": "2026-11-14T09:00:00+08:00",
                "allowed_until": "2026-11-14T09:30:00+08:00",
                "lock_until": "2026-11-14T10:30:00+08:00",
            },
        }

    def test_timer_allowance_and_cooldown_boundaries(self):
        for value, expected, phase in [
            ("2026-11-14T09:00:00+08:00", False, "using"),
            ("2026-11-14T09:29:59+08:00", False, "using"),
            ("2026-11-14T09:30:00+08:00", True, "cooldown"),
            ("2026-11-14T10:29:59+08:00", True, "cooldown"),
            ("2026-11-14T10:30:00+08:00", False, "finished"),
        ]:
            with self.subTest(value=value):
                status = gate.evaluate_state(self.state, instant(value))
                self.assertEqual(status["restricted"], expected)
                self.assertEqual(status["timer"]["phase"], phase)

    def test_timer_is_one_hour_in_absolute_time(self):
        self.state["timer"] = {
            "started_at": "2026-11-14T01:00:00+00:00",
            "allowed_until": "2026-11-14T01:30:00+00:00",
            "lock_until": "2026-11-14T02:30:00+00:00",
        }
        self.assertTrue(gate.evaluate_state(self.state, instant("2026-11-14T09:30:00+08:00"))["restricted"])
        self.assertFalse(gate.evaluate_state(self.state, instant("2026-11-14T10:30:00+08:00"))["restricted"])

    def test_timer_allowance_does_not_override_fixed_schedule(self):
        self.state["policy"] = policy(daily=[{"days": list(range(7)), "start": "09:00", "end": "10:00"}])
        self.assertTrue(gate.evaluate_state(self.state, instant("2026-11-14T09:15:00+08:00"))["restricted"])

    def test_valid_password_unlock_overrides_timer_cooldown(self):
        self.state["unlock"] = {
            "issued_at": "2026-11-14T09:35:00+08:00",
            "until": "2026-11-14T09:50:00+08:00",
            "minutes": 15,
            "policy_hash": gate.policy_fingerprint(self.state["policy"]),
        }
        self.assertFalse(gate.evaluate_state(self.state, instant("2026-11-14T09:40:00+08:00"))["restricted"])
        self.assertTrue(gate.evaluate_state(self.state, instant("2026-11-14T09:50:00+08:00"))["restricted"])

    def test_malformed_timer_does_not_silently_allow(self):
        self.state["timer"]["lock_until"] = "2026-11-14T09:45:00+08:00"
        try:
            status = gate.evaluate_state(self.state, instant("2026-11-14T09:40:00+08:00"))
        except ValueError:
            return
        self.assertTrue(status["restricted"])


class MutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "private-local-controller-password"
        cls.record = gate.make_password(cls.password)
        cls.run_root = Path(tempfile.mkdtemp(prefix="agent-focus-gate-tests-"))

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="controller-", dir=self.run_root))
        self.store = gate.StateStore(self.directory)
        self.controller = gate.Controller(self.store)
        self.now = instant("2026-11-14T10:00:00+08:00")
        self.state = {
            "version": 1,
            "password": copy.deepcopy(self.record),
            "policy": policy(daily=[{"days": list(range(7)), "start": "09:00", "end": "12:00"}]),
            "unlock": None,
            "timer": None,
        }
        with self.store.locked():
            self.store.write(self.state)

    def test_wrong_password_cannot_modify_policy_or_timer(self):
        for route, body in [
            ("/api/policy", {"password": "wrong-password", "policy": policy(enabled=False)}),
            ("/api/timer", {"password": "wrong-password", "minutes": 30}),
            ("/api/timer/cancel", {"password": "wrong-password"}),
            ("/api/unlock", {"password": "wrong-password", "minutes": 15}),
        ]:
            with self.subTest(route=route), self.assertRaises(gate.AuthError):
                self.controller.action(route, body, self.now)
            saved = self.store.read()
            self.assertEqual(saved["policy"], self.state["policy"])
            self.assertIsNone(saved["timer"])
            self.assertIsNone(saved["unlock"])

    def test_five_wrong_passwords_impose_thirty_second_wait(self):
        for _ in range(5):
            with self.assertRaises(gate.AuthError):
                self.controller.action("/api/unlock", {"password": "wrong-password", "minutes": 15}, self.now)
        for value in ["2026-11-14T10:00:00+08:00", "2026-11-14T10:00:29+08:00"]:
            with self.subTest(value=value), self.assertRaises(gate.AuthError):
                self.controller.action("/api/unlock", {"password": self.password, "minutes": 15}, instant(value))
        # A new controller must observe the persisted lockout too.
        with self.assertRaises(gate.AuthError):
            gate.Controller(gate.StateStore(self.directory)).action(
                "/api/unlock", {"password": self.password, "minutes": 15}, instant("2026-11-14T10:00:15+08:00"))
        status = self.controller.action(
            "/api/unlock", {"password": self.password, "minutes": 15}, instant("2026-11-14T10:00:30+08:00"))
        self.assertFalse(status["restricted"])
        self.assertEqual(self.store.read()["auth_failures"]["count"], 0)

    def test_changing_policy_clears_temporary_unlock(self):
        self.controller.action("/api/unlock", {"password": self.password, "minutes": 15}, self.now)
        self.assertIsNotNone(self.store.read()["unlock"])
        status = self.controller.action("/api/policy", {"password": self.password, "policy": self.state["policy"]}, self.now)
        self.assertIsNone(self.store.read()["unlock"])
        self.assertTrue(status["restricted"])

    def test_changing_or_cancelling_timer_clears_unlock(self):
        self.controller.action("/api/unlock", {"password": self.password, "minutes": 15}, self.now)
        self.controller.action("/api/timer", {"password": self.password, "minutes": 30}, self.now)
        saved = self.store.read()
        self.assertIsNone(saved["unlock"])
        self.assertEqual(instant(saved["timer"]["lock_until"]) - instant(saved["timer"]["allowed_until"]), gate.timedelta(hours=1))
        self.controller.action("/api/unlock", {"password": self.password, "minutes": 15}, self.now)
        self.controller.action("/api/timer/cancel", {"password": self.password}, self.now)
        saved = self.store.read()
        self.assertIsNone(saved["timer"])
        self.assertIsNone(saved["unlock"])

    def test_manual_relock_does_not_require_password(self):
        self.controller.action("/api/unlock", {"password": self.password, "minutes": 15}, self.now)
        status = self.controller.action("/api/lock", {}, self.now)
        self.assertIsNone(self.store.read()["unlock"])
        self.assertTrue(status["restricted"])

    def test_changing_password_invalidates_unlock_and_old_password(self):
        self.controller.action("/api/unlock", {"password": self.password, "minutes": 15}, self.now)
        new_password = "another-private-controller-password"
        self.controller.action("/api/password", {"password": self.password, "new_password": new_password}, self.now)
        saved = self.store.read()
        self.assertIsNone(saved["unlock"])
        self.assertFalse(gate.verify_password(self.password, saved["password"]))
        self.assertTrue(gate.verify_password(new_password, saved["password"]))

    def test_state_files_and_directory_are_private(self):
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_existing_configuration_blocks_every_hook(self):
        for contents in ["{broken-json", "null", "[]", '{"version":999}']:
            self.store.path.write_text(contents, encoding="utf-8")
            for event in ["UserPromptSubmit", "SessionStart", "PreToolUse", "PermissionRequest"]:
                with self.subTest(contents=contents, event=event):
                    env = os.environ.copy()
                    env.pop("FOCUS_GATE_EVENT", None)
                    result = subprocess.run(
                        [sys.executable, "-B", str(MODULE_PATH), "hook", "--data-dir", str(self.directory), "--event", event],
                        input=json.dumps({"hook_event_name": event}), text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, env=env,
                    )
                    self.assertEqual(json.loads(result.stdout), gate.hook_output(event, True, MESSAGE))

    def test_running_hooks_include_remaining_time_during_a_restriction(self):
        self.state["policy"] = policy()
        with self.store.locked():
            self.store.write(self.state)
        self.controller.restrict(20)
        message = MESSAGE + "。剩余 20 分钟"
        for event in ["UserPromptSubmit", "SessionStart", "PreToolUse", "PermissionRequest"]:
            with self.subTest(event=event):
                result = subprocess.run(
                    [sys.executable, "-B", str(MODULE_PATH), "hook", "--data-dir", str(self.directory), "--event", event],
                    input=json.dumps({"hook_event_name": event}), text=True, capture_output=True, check=True,
                )
                self.assertEqual(json.loads(result.stdout), gate.hook_output(event, True, message))


class NativeCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "private-native-command-password"
        cls.record = gate.make_password(cls.password)
        cls.run_root = Path(tempfile.mkdtemp(prefix="agent-focus-gate-tests-"))

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="commands-", dir=self.run_root))
        self.store = gate.StateStore(self.directory)
        self.controller = gate.Controller(self.store)
        self.now = instant("2026-11-14T10:00:00+08:00")
        self.state = {"version": 1, "password": copy.deepcopy(self.record), "policy": policy(), "timer": None, "unlock": None}
        with self.store.locked():
            self.store.write(self.state)

    def test_restrict_preserves_existing_rules_and_adds_union(self):
        existing = policy(
            daily=[{"days": list(range(7)), "start": "09:00", "end": "09:30"}],
            once=[{"start": "2026-11-15T14:00+08:00", "end": "2026-11-15T15:00+08:00"}],
        )
        self.state["policy"] = copy.deepcopy(existing)
        with self.store.locked():
            self.store.write(self.state)
        self.controller.restrict(30, now=self.now)
        saved = self.store.read()
        self.assertEqual(saved["policy"]["daily"], existing["daily"])
        self.assertIn(existing["once"][0], saved["policy"]["once"])
        self.assertEqual(len(saved["policy"]["once"]), 2)
        for value in ["2026-11-14T10:15:00+08:00", "2026-11-15T14:30:00+08:00", "2026-11-16T09:15:00+08:00"]:
            with self.subTest(value=value):
                self.assertTrue(gate.evaluate_state(saved, instant(value))["restricted"])

    def test_restrict_duration_is_exact_and_clears_existing_unlock(self):
        self.controller.action("/api/unlock", {"password": self.password, "minutes": 15}, self.now)
        self.controller.restrict(30, now=self.now)
        saved = self.store.read()
        self.assertIsNone(saved["unlock"])
        for value, expected in [
            ("2026-11-14T09:59:59+08:00", False),
            ("2026-11-14T10:00:00+08:00", True),
            ("2026-11-14T10:29:59+08:00", True),
            ("2026-11-14T10:30:00+08:00", False),
        ]:
            with self.subTest(value=value):
                self.assertEqual(gate.evaluate_state(saved, instant(value))["restricted"], expected)

    def test_folder_restriction_composes_with_global_timer_without_enabling_disabled_policy(self):
        target = self.directory / "Project"
        child = target / "src"
        sibling = self.directory / "Project Archive"
        child.mkdir(parents=True)
        sibling.mkdir()
        configured = policy(
            daily=[{"days": list(range(7)), "start": "09:00", "end": "12:00"}],
            enabled=False,
        )
        self.state["policy"] = configured
        with self.store.locked():
            self.store.write(self.state)
        timer = self.controller.start_timer(30, now=self.now)["timer"]
        result = self.controller.restrict(20, now=self.now, folder=str(target))
        self.assertTrue(result["restricted"])
        self.assertEqual(result["policy"], configured)
        self.assertEqual(result["timer"], timer)

        for value, expected in [
            ("2026-11-14T09:59:59+08:00", (True, True, True)),
            ("2026-11-14T10:00:00+08:00", (True, True, False)),
            ("2026-11-14T10:19:59+08:00", (True, True, False)),
            ("2026-11-14T10:20:00+08:00", (False, False, False)),
            ("2026-11-14T10:30:00+08:00", (True, True, True)),
        ]:
            with self.subTest(value=value):
                actual = tuple(
                    self.store.status(instant(value), folder=str(folder))["restricted"]
                    for folder in (target, child, sibling)
                )
                self.assertEqual(actual, expected)

    def test_new_folder_restriction_supersedes_only_that_folders_existing_unlock(self):
        target = self.directory / "Target"
        other = self.directory / "Other"
        target.mkdir()
        other.mkdir()
        self.controller.restrict(30, now=self.now)
        self.controller.action("/api/unlock", {"password": self.password, "minutes": 15}, self.now)
        after_unlock = instant("2026-11-14T10:01:00+08:00")
        self.controller.restrict(30, now=after_unlock, folder=str(target))
        self.assertTrue(self.store.status(after_unlock, folder=str(target))["restricted"])
        self.assertFalse(self.store.status(after_unlock, folder=str(other))["restricted"])
        self.assertTrue(self.store.status(
            instant("2026-11-14T10:15:00+08:00"), folder=str(other)
        )["restricted"])

        after_restrict = instant("2026-11-14T10:02:00+08:00")
        self.controller.action("/api/unlock", {"password": self.password, "minutes": 15}, after_restrict)
        self.assertFalse(self.store.status(after_restrict, folder=str(target))["restricted"])

    def test_first_timer_can_start_without_password_when_unrestricted(self):
        self.controller.start_timer(30, now=self.now)
        saved = self.store.read()
        self.assertIsNotNone(saved["timer"])
        self.assertEqual(instant(saved["timer"]["allowed_until"]) - self.now, gate.timedelta(minutes=30))
        self.assertFalse(gate.evaluate_state(saved, self.now)["restricted"])

    def test_resetting_running_timer_requires_password_and_wrong_password_preserves_it(self):
        self.controller.start_timer(30, now=self.now)
        original = copy.deepcopy(self.store.read()["timer"])
        for password in [None, "wrong-password"]:
            with self.subTest(password=password), self.assertRaises(gate.AuthError):
                self.controller.start_timer(60, password=password, now=instant("2026-11-14T10:10:00+08:00"))
            self.assertEqual(self.store.read()["timer"], original)
        self.controller.start_timer(60, password=self.password, now=instant("2026-11-14T10:10:00+08:00"))
        self.assertEqual(instant(self.store.read()["timer"]["allowed_until"]), instant("2026-11-14T11:10:00+08:00"))

    def test_cooldown_cannot_be_bypassed_by_starting_another_timer(self):
        self.controller.start_timer(30, now=self.now)
        original = copy.deepcopy(self.store.read()["timer"])
        with self.assertRaises(gate.AuthError):
            self.controller.start_timer(60, now=instant("2026-11-14T10:30:00+08:00"))
        self.assertEqual(self.store.read()["timer"], original)
        self.assertTrue(self.store.status(instant("2026-11-14T10:30:00+08:00"))["restricted"])

    def test_finished_timer_can_start_again_without_password(self):
        self.controller.start_timer(30, now=self.now)
        self.controller.start_timer(15, now=instant("2026-11-14T11:30:00+08:00"))
        self.assertEqual(instant(self.store.read()["timer"]["allowed_until"]), instant("2026-11-14T11:45:00+08:00"))

    def test_fixed_restriction_requires_password_to_start_timer(self):
        self.controller.restrict(30, now=self.now)
        with self.assertRaises(gate.AuthError):
            self.controller.start_timer(15, now=self.now)
        self.assertIsNone(self.store.read()["timer"])

    def test_active_password_unlock_does_not_grant_passwordless_timer_reset(self):
        self.controller.action("/api/unlock", {"password": self.password, "minutes": 15}, self.now)
        with self.assertRaises(gate.AuthError):
            self.controller.start_timer(15, now=self.now)
        self.assertIsNone(self.store.read()["timer"])

    def test_clock_rollback_locks_timer_instead_of_extending_usage(self):
        self.controller.start_timer(30, now=self.now)
        rolled_back = instant("2026-11-14T09:59:59+08:00")
        status = self.store.status(rolled_back)
        self.assertTrue(status["restricted"])
        self.assertEqual(status["timer"]["phase"], "cooldown")
        with self.assertRaises(gate.AuthError):
            self.controller.start_timer(60, now=rolled_back)

    def test_invalid_durations_leave_schedule_and_timer_unchanged(self):
        for minutes in [0, -1, 10081, True, "30"]:
            with self.subTest(command="restrict", minutes=minutes), self.assertRaises(ValueError):
                self.controller.restrict(minutes, now=self.now)
            self.assertEqual(self.store.read(), self.state)
        for minutes in [0, -1, 241, True, "30"]:
            with self.subTest(command="timer", minutes=minutes), self.assertRaises(ValueError):
                self.controller.start_timer(minutes, now=self.now)
            self.assertEqual(self.store.read(), self.state)


class NativeCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "private-native-cli-password"
        cls.record = gate.make_password(cls.password)
        cls.run_root = Path(tempfile.mkdtemp(prefix="agent-focus-gate-tests-"))

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="cli-", dir=self.run_root))
        self.store = gate.StateStore(self.directory)

    def configure(self):
        with self.store.locked():
            self.store.write({"version": 1, "password": copy.deepcopy(self.record), "policy": policy(), "timer": None, "unlock": None})

    def command(self, args):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", [str(MODULE_PATH)] + args + ["--data-dir", str(self.directory)]), mock.patch.object(sys, "stdout", output):
            result = gate.main()
        return result, json.loads(output.getvalue())

    def test_invalid_duration_does_not_open_password_dialog(self):
        for command, minutes in [("restrict", "0"), ("timer", "241"), ("unlock", "17")]:
            with self.subTest(command=command), mock.patch.object(gate, "native_password", side_effect=AssertionError("unexpected password dialog")) as password:
                result, status = self.command([command, "--minutes", minutes])
                self.assertEqual(result, 1)
                self.assertFalse(status["ok"])
                password.assert_not_called()
                self.assertFalse(self.store.path.exists())

    def test_cancelled_initial_setup_leaves_no_state(self):
        with mock.patch.object(gate, "native_password", side_effect=ValueError("已取消密码输入")) as password:
            result, status = self.command(["setup"])
        self.assertEqual(result, 1)
        self.assertFalse(status["ok"])
        self.assertFalse(self.store.path.exists())
        password.assert_called_once()

    def test_first_timer_with_configured_state_does_not_open_password_dialog(self):
        self.configure()
        with mock.patch.object(gate, "native_password", side_effect=AssertionError("unexpected password dialog")) as password:
            result, status = self.command(["timer", "--minutes", "30"])
        self.assertEqual(result, 0)
        self.assertEqual(status["timer"]["phase"], "using")
        password.assert_not_called()

    def test_resetting_timer_uses_native_password_and_preserves_secret(self):
        self.configure()
        self.command(["timer", "--minutes", "30"])
        with mock.patch.object(gate, "native_password", return_value=self.password) as password:
            result, status = self.command(["timer", "--minutes", "60"])
        self.assertEqual(result, 0)
        password.assert_called_once()
        self.assertNotIn(self.password, repr(status))
        self.assertEqual(instant(status["timer"]["allowed_until"]) - instant(status["timer"]["started_at"]), gate.timedelta(minutes=60))

    def test_restrict_does_not_read_chat_prompt_or_ask_password(self):
        self.configure()
        class NoPromptInput:
            @property
            def buffer(self):
                raise AssertionError("restrict must not read a chat prompt")
            def read(self, *_args):
                raise AssertionError("restrict must not read a chat prompt")
            def readline(self, *_args):
                raise AssertionError("restrict must not read a chat prompt")
        with mock.patch.object(sys, "stdin", NoPromptInput()), mock.patch.object(gate, "native_password", side_effect=AssertionError("unexpected password dialog")) as password:
            result, status = self.command(["restrict", "--minutes", "30"])
        self.assertEqual(result, 0)
        self.assertTrue(status["restricted"])
        password.assert_not_called()

    def test_folder_cli_and_hooks_use_the_session_directory(self):
        self.configure()
        target = self.directory / "Project Folder"
        child = target / "src"
        sibling = self.directory / "Project Folder Archive"
        alias = self.directory / "Alias"
        other = self.directory / "Other"
        child.mkdir(parents=True)
        sibling.mkdir()
        other.mkdir()
        alias.symlink_to(target, target_is_directory=True)

        result, status = self.command(["restrict", "--minutes", "20", "--folder", str(alias)])
        self.assertEqual(result, 0)
        self.assertTrue(status["restricted"])
        restriction, = status["folder_restrictions"]
        self.assertEqual(restriction["folder"], str(target.resolve()))
        self.assertEqual(instant(restriction["end"]) - instant(restriction["start"]),
                         gate.timedelta(minutes=20))
        result, status = self.command(["status", "--folder", str(sibling)])
        self.assertEqual(result, 0)
        self.assertFalse(status["restricted"])
        result, status = self.command(["restrict", "--minutes", "40", "--folder", str(sibling)])
        self.assertEqual(result, 0)
        self.assertTrue(status["restricted"])

        command = [sys.executable, "-B", str(MODULE_PATH), "--data-dir", str(self.directory)]
        for folder, restricted in [(child, True), (other, False)]:
            with self.subTest(folder=folder):
                result = subprocess.run(command + ["status"], cwd=folder, text=True,
                                        capture_output=True, check=True)
                self.assertEqual(json.loads(result.stdout)["restricted"], restricted)
            for event in ["SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest"]:
                with self.subTest(folder=folder, event=event):
                    result = subprocess.run(
                        command + ["hook", "--event", event], cwd=folder,
                        input=json.dumps({"hook_event_name": event, "cwd": str(folder)}),
                        text=True, capture_output=True, check=True,
                    )
                    self.assertEqual(json.loads(result.stdout), gate.hook_output(
                        event, restricted, MESSAGE + "。剩余 20 分钟"
                    ))

    def test_invalid_folder_does_not_prompt_or_mutate(self):
        self.configure()
        before = self.store.path.read_bytes()
        for folder in ["relative", str(self.directory / "Missing"), str(self.store.path)]:
            with self.subTest(folder=folder), mock.patch.object(
                gate, "native_password", side_effect=AssertionError("unexpected password dialog")
            ) as password:
                result, status = self.command(["restrict", "--minutes", "20", "--folder", folder])
                self.assertEqual(result, 1)
                self.assertFalse(status["ok"])
                password.assert_not_called()
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_configure_from_stdin_saves_policy_and_selected_timezone(self):
        self.configure()
        raw = {
            "enabled": True,
            "timezone": "Asia/Tokyo",
            "daily": [{"days": [0, 2, 4], "start": "09:00", "end": "10:30"}],
            "once": [{"start": "2026-11-16T09:00", "end": "2026-11-16T10:00"}],
        }
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(raw))), mock.patch.object(gate, "native_password", return_value=self.password) as password:
            result, status = self.command(["configure", "--policy-file", "-"])
        self.assertEqual(result, 0)
        password.assert_called_once()
        saved_policy = self.store.read()["policy"]
        self.assertEqual(saved_policy, gate.normalize_policy(raw))
        self.assertEqual(saved_policy["timezone"], "Asia/Tokyo")
        self.assertEqual(instant(saved_policy["once"][0]["start"]), instant("2026-11-16T00:00:00+00:00"))
        self.assertEqual(status["policy"], saved_policy)
        self.assertNotIn(self.password, repr(status))

    def test_invalid_or_oversized_stdin_policy_does_not_prompt_or_mutate(self):
        self.configure()
        before = self.store.path.read_bytes()
        for raw in ["{broken-json", json.dumps({"enabled": True, "timezone": "Not/A_Timezone", "daily": [], "once": []}), " " * (gate.MAX_BODY + 1)]:
            with self.subTest(length=len(raw)), mock.patch.object(sys, "stdin", io.StringIO(raw)), mock.patch.object(gate, "native_password", side_effect=AssertionError("unexpected password dialog")) as password:
                result, status = self.command(["configure", "--policy-file", "-"])
                self.assertEqual(result, 1)
                self.assertFalse(status["ok"])
                password.assert_not_called()
                self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
