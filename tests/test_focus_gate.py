"""Boundary, migration, and documented hook-contract checks for FocusGate."""

import copy
import hashlib
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

MESSAGE = "当前处于限制使用时段，请等待限制结束"


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


def current_state(configured=None, timer=None):
    return {"version": gate.VERSION, "policy": policy() if configured is None else configured, "timer": timer}


def legacy_state(configured, timer=None):
    state = current_state(configured, timer)
    fixture_bytes = "A" * 43 + "="
    fingerprint = hashlib.sha256(
        json.dumps(configured, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    state.update({
        "version": 1,
        "password": {
            "algorithm": "pbkdf2-sha256",
            "iterations": 600000,
            "salt": fixture_bytes,
            "digest": fixture_bytes,
        },
        "auth_failures": {"count": 2},
        "unlock": {
            "issued_at": "2026-11-14T09:55:00+08:00",
            "until": "2026-11-14T10:10:00+08:00",
            "policy_hash": fingerprint,
        },
    })
    return state


class StateTests(unittest.TestCase):
    def setUp(self):
        self.configured = policy(daily=[{"days": list(range(7)), "start": "09:00", "end": "12:00"}])
        self.state = current_state(self.configured)
        self.now = instant("2026-11-14T10:00:00+08:00")

    def test_missing_state_does_not_restrict(self):
        self.assertFalse(gate.evaluate_state(None, self.now)["restricted"])

    def test_configured_state_restricts(self):
        status = gate.evaluate_state(self.state, self.now)
        self.assertTrue(status["restricted"])
        self.assertEqual(status["message"], MESSAGE)

    def test_legacy_unlock_fields_cannot_override_an_active_schedule(self):
        saved = legacy_state(self.configured)
        for version in (1, gate.VERSION):
            with self.subTest(version=version):
                saved["version"] = version
                status = gate.evaluate_state(saved, self.now)
                self.assertTrue(status["restricted"])
                self.assertNotIn("unlocked_until", status)

    def test_corrupt_state_cannot_silently_allow(self):
        try:
            status = gate.evaluate_state({"version": 999, "policy": {}}, self.now)
        except ValueError:
            return
        self.assertTrue(status["restricted"])

    def test_outside_interval_allows(self):
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
    def setUp(self):
        self.now = instant("2026-11-14T10:00:00+08:00")
        self.state = current_state(timer={
            "started_at": "2026-11-14T09:00:00+08:00",
            "allowed_until": "2026-11-14T09:30:00+08:00",
            "lock_until": "2026-11-14T10:30:00+08:00",
        })

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


    def test_legacy_unlock_cannot_override_a_cooldown(self):
        saved = legacy_state(self.state["policy"], self.state["timer"])
        self.assertTrue(gate.evaluate_state(saved, self.now)["restricted"])

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
        cls.run_root = Path(tempfile.mkdtemp(prefix="agent-focus-gate-tests-"))

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="controller-", dir=self.run_root))
        self.store = gate.StateStore(self.directory)
        self.controller = gate.Controller(self.store)
        self.now = instant("2026-11-14T10:00:00+08:00")
        self.state = current_state(policy(daily=[{"days": list(range(7)), "start": "09:00", "end": "12:00"}]))
        with self.store.locked():
            self.store.write(self.state)

    def test_configure_cannot_shorten_or_disable_an_active_schedule(self):
        before = self.store.path.read_bytes()
        candidates = (
            policy(),
            policy(enabled=False),
            policy(daily=[{"days": list(range(7)), "start": "09:00", "end": "10:30"}]),
        )
        for configured in candidates:
            with self.subTest(configured=configured), self.assertRaises(ValueError):
                self.controller.configure(configured, now=self.now)
            self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(self.store.status(instant("2026-11-14T11:59:59+08:00"))["restricted"])

    def test_configure_can_change_daily_rules_after_the_previous_window(self):
        end = instant("2026-11-14T12:00:00+08:00")
        configured = policy(daily=[{"days": list(range(7)), "start": "13:00", "end": "14:00"}])
        self.controller.configure(configured, now=end)
        self.assertEqual(self.store.read()["policy"], configured)
        self.assertFalse(self.store.status(end)["restricted"])
        self.assertTrue(self.store.status(instant("2026-11-14T13:00:00+08:00"))["restricted"])

    def test_configure_preserves_pending_one_off_rules(self):
        next_day = {"start": "2026-11-15T09:00:00+08:00", "end": "2026-11-15T10:30:00+08:00"}
        future_rule = policy(once=[next_day])["once"][0]
        before_window = instant("2026-11-14T08:00:00+08:00")
        self.controller.configure(policy(once=[next_day]), now=before_window)
        self.controller.configure(policy(), now=before_window)
        self.assertIn(future_rule, self.store.read()["policy"]["once"])
        self.assertTrue(self.store.status(instant("2026-11-15T10:00:00+08:00"))["restricted"])

    def test_configure_cannot_disable_pending_one_off_rules(self):
        start = instant("2026-11-14T08:00:00+08:00")
        next_day = {"start": "2026-11-15T09:00:00+08:00", "end": "2026-11-15T10:30:00+08:00"}
        self.controller.configure(policy(once=[next_day]), now=start)
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            self.controller.configure(policy(enabled=False), now=start)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_migration_preserves_all_rules_and_removes_legacy_credentials(self):
        timer = {
            "started_at": "2026-11-14T09:00:00+08:00",
            "allowed_until": "2026-11-14T09:30:00+08:00",
            "lock_until": "2026-11-14T10:30:00+08:00",
        }
        saved = legacy_state(self.state["policy"], timer)
        saved["folder_restrictions"] = [{
            "folder": str(self.directory.resolve()),
            "start": "2026-11-14T09:00:00+08:00",
            "end": "2026-11-14T11:00:00+08:00",
        }]
        self.store.path.write_text(json.dumps(saved), encoding="utf-8")
        before = self.store.path.read_bytes()
        self.assertTrue(self.store.status(self.now, folder=self.directory)["restricted"])
        self.assertEqual(self.store.path.read_bytes(), before)

        self.store.migrate()
        migrated = json.loads(self.store.path.read_bytes())
        self.assertEqual(migrated["version"], 2)
        self.assertEqual(migrated["policy"], saved["policy"])
        self.assertEqual(migrated["timer"], saved["timer"])
        self.assertEqual(migrated["folder_restrictions"], saved["folder_restrictions"])
        self.assertFalse({"password", "auth_failures", "unlock"} & migrated.keys())
        self.assertTrue(self.store.status(self.now, folder=self.directory)["restricted"])

        after = self.store.path.read_bytes()
        self.store.migrate()
        self.assertEqual(self.store.path.read_bytes(), after)

    def test_invalid_legacy_rules_cannot_be_replaced_by_migration(self):
        self.store.path.write_text(json.dumps(legacy_state({"enabled": True})), encoding="utf-8")
        before = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.migrate()
        self.assertEqual(self.store.path.read_bytes(), before)

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
        cls.run_root = Path(tempfile.mkdtemp(prefix="agent-focus-gate-tests-"))

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="commands-", dir=self.run_root))
        self.store = gate.StateStore(self.directory)
        self.controller = gate.Controller(self.store)
        self.now = instant("2026-11-14T10:00:00+08:00")
        self.state = current_state()
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

    def test_restrict_duration_is_exact(self):
        self.controller.restrict(30, now=self.now)
        saved = self.store.read()
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


    def test_folder_restriction_and_global_block_are_independent(self):
        target = self.directory / "Target"
        other = self.directory / "Other"
        target.mkdir()
        other.mkdir()
        self.controller.restrict(15, now=self.now)
        self.controller.restrict(30, now=self.now, folder=str(target))
        after_global_end = instant("2026-11-14T10:15:00+08:00")
        self.assertTrue(self.store.status(self.now, folder=target)["restricted"])
        self.assertTrue(self.store.status(self.now, folder=other)["restricted"])
        self.assertTrue(self.store.status(after_global_end, folder=target)["restricted"])
        self.assertFalse(self.store.status(after_global_end, folder=other)["restricted"])

    def test_first_timer_can_start_when_unrestricted(self):
        self.controller.start_timer(30, now=self.now)
        saved = self.store.read()
        self.assertIsNotNone(saved["timer"])
        self.assertEqual(instant(saved["timer"]["allowed_until"]) - self.now, gate.timedelta(minutes=30))
        self.assertFalse(gate.evaluate_state(saved, self.now)["restricted"])


    def test_ongoing_timer_cannot_be_reset(self):
        self.controller.start_timer(30, now=self.now)
        before = self.store.path.read_bytes()
        for minutes in (15, 60):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                self.controller.start_timer(minutes, now=instant("2026-11-14T10:10:00+08:00"))
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_cooldown_cannot_be_bypassed_by_starting_another_timer(self):
        self.controller.start_timer(30, now=self.now)
        original = copy.deepcopy(self.store.read()["timer"])
        with self.assertRaises(ValueError):
            self.controller.start_timer(60, now=instant("2026-11-14T10:30:00+08:00"))
        self.assertEqual(self.store.read()["timer"], original)
        self.assertTrue(self.store.status(instant("2026-11-14T10:30:00+08:00"))["restricted"])

    def test_finished_timer_can_start_again(self):
        self.controller.start_timer(30, now=self.now)
        self.controller.start_timer(15, now=instant("2026-11-14T11:30:00+08:00"))
        self.assertEqual(instant(self.store.read()["timer"]["allowed_until"]), instant("2026-11-14T11:45:00+08:00"))


    def test_configure_does_not_change_a_timer(self):
        self.controller.start_timer(30, now=self.now)
        original = copy.deepcopy(self.store.read()["timer"])
        self.controller.configure(policy(), now=instant("2026-11-14T10:30:00+08:00"))
        self.assertEqual(self.store.read()["timer"], original)
        self.assertTrue(self.store.status(instant("2026-11-14T10:45:00+08:00"))["restricted"])

    def test_clock_rollback_locks_timer_instead_of_extending_usage(self):
        self.controller.start_timer(30, now=self.now)
        rolled_back = instant("2026-11-14T09:59:59+08:00")
        status = self.store.status(rolled_back)
        self.assertTrue(status["restricted"])
        self.assertEqual(status["timer"]["phase"], "cooldown")
        with self.assertRaises(ValueError):
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
        cls.run_root = Path(tempfile.mkdtemp(prefix="agent-focus-gate-tests-"))

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="cli-", dir=self.run_root))
        self.store = gate.StateStore(self.directory)

    def configure(self):
        with self.store.locked():
            self.store.write(current_state())

    def command(self, args):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", [str(MODULE_PATH)] + args + ["--data-dir", str(self.directory)]), mock.patch.object(sys, "stdout", output):
            result = gate.main()
        return result, json.loads(output.getvalue())

    def test_invalid_duration_leaves_no_state(self):
        for command, minutes in [("restrict", "0"), ("timer", "241")]:
            with self.subTest(command=command):
                result, status = self.command([command, "--minutes", minutes])
                self.assertEqual(result, 1)
                self.assertFalse(status["ok"])
                self.assertFalse(self.store.path.exists())

    def test_removed_commands_cannot_modify_an_active_timer(self):
        result, _ = self.command(["timer", "--minutes", "30"])
        self.assertEqual(result, 0)
        before = self.store.path.read_bytes()
        for command in ("setup", "unlock", "cancel-timer", "lock", "password"):
            with self.subTest(command=command), mock.patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.command([command])
                self.assertEqual(error.exception.code, 2)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_first_timer_initializes_without_credentials(self):
        self.assertFalse(self.store.path.exists())
        result, status = self.command(["timer", "--minutes", "30"])
        self.assertEqual(result, 0)
        self.assertEqual(status["timer"]["phase"], "using")
        self.assertEqual(self.store.read()["version"], 2)
        self.assertNotIn("password", self.store.read())

    def test_cli_cannot_replace_an_ongoing_timer(self):
        first_result, _ = self.command(["timer", "--minutes", "30"])
        self.assertEqual(first_result, 0)
        before = self.store.path.read_bytes()
        result, status = self.command(["timer", "--minutes", "60"])
        self.assertEqual(result, 1)
        self.assertFalse(status["ok"])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_status_migrates_existing_state_in_place(self):
        saved = legacy_state(policy())
        self.store.path.write_text(json.dumps(saved), encoding="utf-8")
        result, status = self.command(["status"])
        self.assertEqual(result, 0)
        self.assertEqual(status["policy"], saved["policy"])
        migrated = json.loads(self.store.path.read_bytes())
        self.assertEqual(migrated["version"], 2)
        self.assertFalse({"password", "auth_failures", "unlock"} & migrated.keys())

    def test_restrict_does_not_read_chat_prompt(self):
        class NoPromptInput:
            @property
            def buffer(self):
                raise AssertionError("restrict must not read a chat prompt")
            def read(self, *_args):
                raise AssertionError("restrict must not read a chat prompt")
            def readline(self, *_args):
                raise AssertionError("restrict must not read a chat prompt")
        with mock.patch.object(sys, "stdin", NoPromptInput()):
            result, status = self.command(["restrict", "--minutes", "30"])
        self.assertEqual(result, 0)
        self.assertTrue(status["restricted"])
        self.assertEqual(self.store.read()["version"], 2)

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

    def test_invalid_folder_does_not_mutate(self):
        self.configure()
        before = self.store.path.read_bytes()
        for folder in ["relative", str(self.directory / "Missing"), str(self.store.path)]:
            with self.subTest(folder=folder):
                result, status = self.command(["restrict", "--minutes", "20", "--folder", folder])
                self.assertEqual(result, 1)
                self.assertFalse(status["ok"])
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_configure_from_stdin_initializes_and_saves_policy_and_selected_timezone(self):
        self.assertFalse(self.store.path.exists())
        raw = {
            "enabled": True,
            "timezone": "Asia/Tokyo",
            "daily": [{"days": [0, 2, 4], "start": "09:00", "end": "10:30"}],
            "once": [{"start": "2099-11-16T09:00", "end": "2099-11-16T10:00"}],
        }
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(raw))):
            result, status = self.command(["configure", "--policy-file", "-"])
        self.assertEqual(result, 0)
        saved_policy = self.store.read()["policy"]
        self.assertEqual(saved_policy, gate.normalize_policy(raw))
        self.assertEqual(saved_policy["timezone"], "Asia/Tokyo")
        self.assertEqual(instant(saved_policy["once"][0]["start"]), instant("2099-11-16T00:00:00+00:00"))
        self.assertEqual(status["policy"], saved_policy)

    def test_invalid_or_oversized_stdin_policy_does_not_mutate(self):
        self.configure()
        before = self.store.path.read_bytes()
        for raw in ["{broken-json", json.dumps({"enabled": True, "timezone": "Not/A_Timezone", "daily": [], "once": []}), " " * (gate.MAX_BODY + 1)]:
            with self.subTest(length=len(raw)), mock.patch.object(sys, "stdin", io.StringIO(raw)):
                result, status = self.command(["configure", "--policy-file", "-"])
                self.assertEqual(result, 1)
                self.assertFalse(status["ok"])
                self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
