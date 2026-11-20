#!/usr/bin/env python3
"""Local schedule and countdown gate. Python 3.9+, standard library only."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
from math import ceil
import os
from pathlib import Path
import re
import sys
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MESSAGE = "当前处于限制使用时段，请等待限制结束"
UTC = timezone.utc
VERSION = 3
LEGACY_VERSIONS = (1, 2)
MAX_BODY = 65536


def default_policy():
    return {"enabled": True, "timezone": "Asia/Shanghai", "daily": [], "once": []}


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("%s须为 %d—%d 的整数" % (name, low, high))
    return value


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value


def _iso(value):
    return _aware(value).astimezone(UTC).isoformat()


def _parse_date(value, zone=None):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("日期格式无效")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("日期格式无效") from None
    if result.tzinfo is None:
        if zone is None:
            raise ValueError("日期缺少时区")
        candidates = []
        for fold in (0, 1):
            candidate = result.replace(tzinfo=zone, fold=fold)
            roundtrip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
            if roundtrip == result:
                candidates.append(candidate)
        if not candidates:
            raise ValueError("所选时间在该时区不存在（夏令时跳转）")
        if len({item.utcoffset() for item in candidates}) > 1:
            raise ValueError("所选时间因夏令时重复，请使用含明确 UTC 偏移的时间")
        result = candidates[0]
    return _aware(result)


def _clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("每日时刻须为 HH:MM")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def normalize_policy(raw):
    if not isinstance(raw, dict) or type(raw.get("enabled")) is not bool:
        raise ValueError("策略须包含 enabled 布尔值")
    zone_name = raw.get("timezone")
    if not isinstance(zone_name, str) or len(zone_name) > 100:
        raise ValueError("时区无效")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("找不到时区：%s" % zone_name) from None
    result = {"enabled": raw["enabled"], "timezone": zone_name, "daily": [], "once": []}
    for name in ("daily", "once"):
        if not isinstance(raw.get(name), list) or len(raw[name]) > 100:
            raise ValueError("%s 须为不超过 100 项的列表" % name)
    for item in raw["daily"]:
        if not isinstance(item, dict):
            raise ValueError("每日时段格式无效")
        days = item.get("days")
        if not isinstance(days, list) or not days or len(days) > 7:
            raise ValueError("每日时段须选择星期")
        for day in days:
            _integer(day, 0, 6, "星期")
        start, end = item.get("start"), item.get("end")
        if _clock(start) == _clock(end):
            raise ValueError("每日时段的起止不能相同；跨午夜可将结束时刻设得更早")
        result["daily"].append({"days": sorted(set(days)), "start": start, "end": end})
    for item in raw["once"]:
        if not isinstance(item, dict):
            raise ValueError("一次性时段格式无效")
        start = _parse_date(item.get("start"), zone)
        end = _parse_date(item.get("end"), zone)
        if end.astimezone(UTC) <= start.astimezone(UTC):
            raise ValueError("一次性时段的结束必须晚于开始")
        result["once"].append({"start": _iso(start), "end": _iso(end)})
    return result


def is_scheduled(policy, now):
    now = _aware(now)
    if not policy["enabled"]:
        return False
    local = now.astimezone(ZoneInfo(policy["timezone"]))
    minute = local.hour * 60 + local.minute
    day, previous_day = local.weekday(), (local.weekday() - 1) % 7
    for item in policy["daily"]:
        start, end = _clock(item["start"]), _clock(item["end"])
        if start < end:
            active = day in item["days"] and start <= minute < end
        else:
            active = (day in item["days"] and minute >= start) or (previous_day in item["days"] and minute < end)
        if active:
            return True
    current = now.astimezone(UTC)
    return any(_parse_date(item["start"]) <= current < _parse_date(item["end"]) for item in policy["once"])


def _timer_status(timer, now):
    if timer is None:
        return None
    if not isinstance(timer, dict):
        raise ValueError("计时记录损坏")
    start = _parse_date(timer.get("started_at")).astimezone(UTC)
    allowed = _parse_date(timer.get("allowed_until")).astimezone(UTC)
    locked = _parse_date(timer.get("lock_until")).astimezone(UTC)
    if not timedelta(minutes=1) <= allowed - start <= timedelta(minutes=240):
        raise ValueError("计时时长无效")
    if locked - allowed != timedelta(hours=1):
        raise ValueError("计时冷却时长无效")
    current = now.astimezone(UTC)
    phase = "using" if current < allowed else ("cooldown" if current < locked else "finished")
    # A backwards wall clock must not extend the allowed window.
    if current < start:
        phase = "cooldown"
    return {"started_at": _iso(start), "allowed_until": _iso(allowed), "lock_until": _iso(locked), "phase": phase}


def _absolute_folder(value):
    if not isinstance(value, (str, Path)) or "\0" in str(value):
        raise ValueError("文件夹路径无效")
    folder = Path(value)
    if not folder.is_absolute():
        raise ValueError("请指定文件夹的绝对路径")
    return folder


def _existing_folder(value):
    folder = _absolute_folder(value)
    try:
        folder = folder.resolve(strict=True)
        if folder.is_dir():
            return folder
    except (OSError, RuntimeError):
        pass
    raise ValueError("请指定一个存在且可访问的文件夹")


def _folder_restrictions(raw):
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("文件夹限制须为不超过 100 项的列表")
    for rule in raw:
        if not isinstance(rule, dict):
            raise ValueError("文件夹限制格式无效")
        folder = _absolute_folder(rule.get("folder"))
        start, end = _parse_date(rule.get("start")), _parse_date(rule.get("end"))
        if ".." in folder.parts or end <= start:
            raise ValueError("文件夹限制的路径或时间无效")
    return raw


def _matching_folder_rules(rules, folder):
    if not rules or folder is None:
        return []
    current = _absolute_folder(folder).resolve()
    ancestors = (current, *current.parents)
    matching = []
    for rule in rules:
        root = Path(rule["folder"])
        for ancestor in ancestors:
            matches = ancestor == root
            if not matches and str(ancestor).casefold() == str(root).casefold():
                # macOS volumes can use either case-sensitive or case-insensitive names.
                try:
                    matches = ancestor.samefile(root)
                except OSError:
                    pass
            if matches:
                matching.append(rule)
                break
    return matching


def _folder_intervals(rules, folder):
    return [(_parse_date(rule["start"]), _parse_date(rule["end"]))
            for rule in _matching_folder_rules(rules, folder)]


def _folder_timer_statuses(raw, now):
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("文件夹计时须为不超过 100 项的列表")
    timers = []
    for rule in raw:
        timer = _timer_status(rule, now)
        if timer is None:
            raise ValueError("文件夹计时记录损坏")
        folder = _absolute_folder(rule.get("folder"))
        if ".." in folder.parts:
            raise ValueError("文件夹计时路径无效")
        timers.append({**timer, "folder": str(folder)})
    return timers


def evaluate_state(state, now, folder=None):
    now = _aware(now).astimezone(UTC)
    if state is None:
        return {"configured": False, "restricted": False, "policy": default_policy(),
                "timer": None, "message": "尚未设置限制规则"}
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] not in (*LEGACY_VERSIONS, VERSION):
        raise ValueError("配置版本无效")
    policy = normalize_policy(state.get("policy"))
    timer = _timer_status(state.get("timer"), now)
    rules = _folder_restrictions(state.get("folder_restrictions", []))
    folder_restricted = any(start <= now < end for start, end in _folder_intervals(rules, folder))
    folder_timers = _matching_folder_rules(_folder_timer_statuses(state.get("folder_timers", []), now), folder)
    folder_cooldown = any(item["phase"] == "cooldown" for item in folder_timers)
    restricted = (folder_restricted or is_scheduled(policy, now)
                  or folder_cooldown or (timer is not None and timer["phase"] == "cooldown"))
    status = {"configured": True, "restricted": restricted, "policy": policy, "timer": timer,
              "message": MESSAGE if restricted else "当前允许使用"}
    if rules:
        status["folder_restrictions"] = rules
    if folder_timers:
        status["folder_timers"] = folder_timers
    return status


def hook_output(event, restricted, message=MESSAGE):
    if not restricted:
        return {}
    if event == "UserPromptSubmit":
        return {"decision": "block", "reason": message, "systemMessage": message}
    if event == "PreToolUse":
        return {"systemMessage": message, "hookSpecificOutput": {"hookEventName": event,
                "permissionDecision": "deny", "permissionDecisionReason": message}}
    if event == "PermissionRequest":
        return {"systemMessage": message, "hookSpecificOutput": {"hookEventName": event,
                "decision": {"behavior": "deny", "message": message}}}
    if event == "SessionStart":
        return {"continue": False, "stopReason": message, "systemMessage": message}
    return {}


def _weekly_coverage(daily):
    minutes_per_day = 24 * 60
    week = bytearray(7 * minutes_per_day)
    for item in daily:
        start, end = _clock(item["start"]), _clock(item["end"])
        duration = (end - start) % minutes_per_day
        for day in item["days"]:
            first = day * minutes_per_day + start
            last = min(first + duration, len(week))
            week[first:last] = b"\x01" * (last - first)
            spill = first + duration - last
            week[:spill] = b"\x01" * spill
    return week


def _end_of_daily_restriction(now, zone, week, limit=None):
    # Advance on the absolute clock so repeated or skipped local times use the same
    # weekday and minute semantics as is_scheduled().
    while limit is None or now < limit:
        local = now.astimezone(zone)
        minute = (local.weekday() * 24 + local.hour) * 60 + local.minute
        if not week[minute]:
            return now
        now += timedelta(seconds=60 - local.second, microseconds=-local.microsecond)
    return limit


def _restriction_end(status, now, folder=None):
    policy = status["policy"]
    zone = ZoneInfo(policy["timezone"])
    week = _weekly_coverage(policy["daily"] if policy["enabled"] else [])
    if all(week):
        return None

    intervals = _folder_intervals(status.get("folder_restrictions", []), folder)
    if policy["enabled"]:
        intervals.extend((_parse_date(item["start"]), _parse_date(item["end"])) for item in policy["once"])
    for timer in (status["timer"], *status.get("folder_timers", [])):
        if timer is None or timer["phase"] == "finished":
            continue
        started = _parse_date(timer["started_at"])
        if now < started:
            intervals.append((now, started))
        intervals.append((_parse_date(timer["allowed_until"]), _parse_date(timer["lock_until"])))

    end = now
    for start, stop in sorted(intervals):
        if stop <= end:
            continue
        if start > end:
            end = _end_of_daily_restriction(end, zone, week, limit=start)
            if end < start:
                return end
        end = stop
    return _end_of_daily_restriction(end, zone, week)


def _restriction_message(status, now, folder=None):
    try:
        end = _restriction_end(status, now, folder)
    except (OverflowError, ValueError):
        return MESSAGE
    if end is None:
        return f"{MESSAGE}。当前规则没有自动结束时间"

    seconds = ceil((end - now).total_seconds())
    if seconds < 60:
        remaining = f"{seconds} 秒"
    else:
        hours, minutes = divmod((seconds + 59) // 60, 60)
        days, hours = divmod(hours, 24)
        units = ((days, "天"), (hours, "小时"), (minutes, "分钟"))
        remaining = " ".join(f"{value} {unit}" for value, unit in units if value)
    return f"{MESSAGE}。剩余 {remaining}"


class StateStore:
    def __init__(self, directory=None):
        # Stable across plugin cache versions; never dependent on the conversation cwd.
        self.directory = Path(directory) if directory else Path.home() / "Library/Application Support/FocusGate"
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "state.lock"

    def read(self):
        try:
            with self.path.open("r", encoding="utf-8") as source:
                data = source.read(1024 * 1024 + 1)
        except FileNotFoundError:
            return None
        if len(data) > 1024 * 1024:
            raise ValueError("配置文件过大")
        try:
            state = json.loads(data)
        except (ValueError, TypeError):
            raise ValueError("配置文件无法读取") from None
        if not isinstance(state, dict):
            raise ValueError("配置文件须为对象")
        return state

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def write(self, state):
        # Store only current fields so legacy overrides and credentials are discarded.
        saved = {"version": VERSION, "policy": state["policy"], "timer": state.get("timer")}
        for key in ("folder_restrictions", "folder_timers"):
            if state.get(key):
                saved[key] = state[key]
        # Atomic edit: readers see the previous or the next complete file.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".state-", dir=self.directory, delete=False) as target:
            os.chmod(target.name, 0o600)
            json.dump(saved, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
            name = target.name
        os.replace(name, self.path)

    def status(self, now=None, folder=None):
        return evaluate_state(self.read(), now or datetime.now(UTC), folder)


    def migrate(self):
        state = self.read()
        if state is None or state.get("version") not in LEGACY_VERSIONS:
            return
        with self.locked():
            state = self.read()
            if state is not None and state.get("version") in LEGACY_VERSIONS:
                evaluate_state(state, datetime.now(UTC))
                self.write(state)


class Controller:
    def __init__(self, store):
        self.store = store

    def configure(self, policy, now=None):
        policy = normalize_policy(policy)
        with self.store.locked():
            now = _aware(now or datetime.now(UTC)).astimezone(UTC)
            state = self.store.read()
            current = evaluate_state(state, now)["policy"]
            if is_scheduled(current, now):
                raise ValueError("全局限制生效期间无法修改定时计划，请等待当前禁用时段结束")
            pending = [item for item in current["once"] if current["enabled"] and _parse_date(item["end"]) > now]
            if pending and not policy["enabled"]:
                raise ValueError("已有未结束的一次性全局限制，无法关闭定时计划")
            intervals = {(item["start"], item["end"]): item for item in pending + policy["once"]}
            policy["once"] = list(intervals.values())
            state = state if state is not None else {"version": VERSION}
            state["policy"] = normalize_policy(policy)
            self.store.write(state)
            return evaluate_state(state, now)

    def restrict(self, minutes, now=None, folder=None):
        minutes = _integer(minutes, 1, 10080, "禁止使用分钟数")
        folder = _existing_folder(folder) if folder is not None else None
        with self.store.locked():
            now = _aware(now or datetime.now(UTC)).astimezone(UTC)
            interval = {"start": _iso(now), "end": _iso(now + timedelta(minutes=minutes))}
            state = self.store.read()
            current = evaluate_state(state, now)
            state = state if state is not None else {"version": VERSION, "policy": current["policy"]}
            if folder is None:
                policy = current["policy"]
                if len(policy["once"]) >= 100:
                    # Keep ongoing and future rules when reclaiming expired slots.
                    policy["once"] = [item for item in policy["once"] if _parse_date(item["end"]) > now]
                policy["once"].append(interval)
                policy["enabled"] = True
                state["policy"] = normalize_policy(policy)
            else:
                rules = [rule for rule in current.get("folder_restrictions", []) if _parse_date(rule["end"]) > now]
                rules.append({**interval, "folder": str(folder)})
                state["folder_restrictions"] = _folder_restrictions(rules)
            self.store.write(state)
            return evaluate_state(state, now, folder)

    def start_timer(self, minutes, now=None, folder=None):
        minutes = _integer(minutes, 1, 240, "限时使用分钟数")
        folder = _existing_folder(folder) if folder is not None else None
        with self.store.locked():
            now = _aware(now or datetime.now(UTC)).astimezone(UTC)
            state = self.store.read()
            current = evaluate_state(state, now, folder)
            applicable_timers = (current["timer"], *current.get("folder_timers", []))
            if any(timer is not None and timer["phase"] != "finished" for timer in applicable_timers):
                raise ValueError("计时已开始，请等待使用期和冷却期结束")
            if current["restricted"]:
                raise ValueError("当前范围处于限制使用时段，请等待限制结束")
            state = state if state is not None else {"version": VERSION, "policy": current["policy"]}
            allowed = now + timedelta(minutes=minutes)
            timer = {"started_at": _iso(now), "allowed_until": _iso(allowed),
                     "lock_until": _iso(allowed + timedelta(hours=1))}
            if folder is None:
                state["timer"] = timer
            else:
                timers = [item for item in state.get("folder_timers", [])
                          if _timer_status(item, now)["phase"] != "finished"]
                timers.append({**timer, "folder": str(folder)})
                _folder_timer_statuses(timers, now)
                state["folder_timers"] = timers
            self.store.write(state)
            return evaluate_state(state, now, folder)


def run_hook(store, event):
    # No prompt/transcript parsing, logging, secret handling or UI inside hooks.
    try:
        if not sys.stdin.isatty():
            while sys.stdin.buffer.read(MAX_BODY):
                pass
        now = datetime.now(UTC)
        # Codex runs command hooks with the session's working directory.
        folder = Path.cwd()
        status = store.status(now, folder)
        message = _restriction_message(status, now, folder) if status["restricted"] else MESSAGE
        result = hook_output(event, status["restricted"], message)
    except Exception:
        result = hook_output(event, True)
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="FocusGate 本机使用限制（Skill + Hooks，无网页）")
    parser.add_argument("command", choices=("hook", "restrict", "timer", "configure", "status"))
    parser.add_argument("--data-dir", help="独立测试或部署的数据目录；日常使用请勿指定")
    parser.add_argument("--minutes", type=int, help="restrict 1—10080；timer 1—240")
    parser.add_argument("--folder", help="restrict、timer 或 status 对应的文件夹绝对路径")
    parser.add_argument("--policy-file", help="configure 使用的 JSON 策略文件；- 表示从 stdin 读取")
    parser.add_argument("--event", choices=("SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest"))
    args = parser.parse_args()
    store = StateStore(args.data_dir)
    controller = Controller(store)
    if args.command == "hook":
        run_hook(store, args.event or "UserPromptSubmit")
        return 0
    try:
        if args.folder is not None and args.command not in ("restrict", "timer", "status"):
            raise ValueError("--folder 仅用于 restrict、timer 或 status")
        folder = _existing_folder(args.folder) if args.folder is not None else None
        if args.command == "status":
            store.migrate()
            result = store.status(folder=folder or Path.cwd())
        elif args.command == "restrict":
            result = controller.restrict(args.minutes, folder=folder)
        elif args.command == "timer":
            result = controller.start_timer(args.minutes, folder=folder)
        else:
            if not args.policy_file:
                raise ValueError("configure 需要 --policy-file")
            if args.policy_file == "-":
                raw_policy = sys.stdin.read(MAX_BODY + 1)
            else:
                with Path(args.policy_file).open(encoding="utf-8") as source:
                    raw_policy = source.read(MAX_BODY + 1)
            if len(raw_policy) > MAX_BODY:
                raise ValueError("计划文件过大")
            result = controller.configure(json.loads(raw_policy))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
