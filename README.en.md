# Agent Focus Gate

[简体中文](README.md) · English

Set usage limits for local Codex on macOS. Pause all projects immediately, block sessions on a schedule, set a usage timer followed by a cooldown, or pause a single project. Codex's administrator-managed hooks enforce those limits. [Codex managed hooks](https://learn.chatgpt.com/docs/hooks#managed-hooks-in-local-codex)

> [!IMPORTANT]
> Once a timer starts, there is no way to lift the restriction through any Focus Gate command or interface. You can start a new timer after both the usage period and the one-hour cooldown have ended.

## Features

| Feature | When to use it | Example |
| --- | --- | --- |
| **Block all sessions** | Pause all local Codex sessions immediately until the restriction expires | Block all sessions for two hours |
| **Scheduled blocks** | Repeat a block on specific weekdays or schedule a one-off window | Block every day from 22:00 to 08:00 the next day |
| **Usage timer** | Allow a set amount of time, then start a one-hour global cooldown | Use Codex for 45 more minutes, then cool down |
| **Block one project** | Pause sessions in a folder and its subfolders | Block the current project for 30 minutes |
| **Status** | Check restrictions for the current folder, global schedules, timers, and project rules | Check whether the current project is restricted |

## Install

You need macOS, `/usr/bin/python3` 3.9 or later, a local Codex version that supports administrator-managed hooks, and macOS administrator authorization for installation.

```sh
git clone https://github.com/DanielPro0706/agent-focus-gate.git
cd agent-focus-gate
# Review install.sh, requirements.toml, and focus_gate.py before proceeding
sudo /bin/bash ./install.sh install
/bin/bash ./install.sh check
```

The installer puts the managed configuration in `/etc/codex/requirements.toml` and the script in `/etc/codex/focus-gate/focus_gate.py`. If the system already has a different `requirements.toml`, installation stops so an administrator can review and merge the existing configuration.

Quit and reopen Codex, then check **Settings → Hooks → Administrator configuration** for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, and `PermissionRequest`. Open a local session.

## Usage

Open a terminal under the macOS account you use for Codex. The shell function below shortens the commands in this terminal session. Use that account to create restrictions and check status.

```sh
focus_gate() {
  /usr/bin/python3 -I -B /etc/codex/focus-gate/focus_gate.py "$@"
}
```

### Block all sessions immediately

```sh
focus_gate restrict --minutes 120
```

This applies a 120-minute block to all supported local Codex sessions. It accepts 1–10,080 minutes and can overlap with other rules.

### Schedule blocks

This plan blocks sessions every day from 22:00 to 08:00 the next day. It also adds a two-hour block on a specific date. Replace the one-off date with the one you need.

```sh
focus_gate configure --policy-file - <<'JSON'
{
  "enabled": true,
  "timezone": "Asia/Shanghai",
  "daily": [
    {"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "08:00"}
  ],
  "once": [
    {"start": "2030-01-12T14:00:00+08:00", "end": "2030-01-12T16:00:00+08:00"}
  ]
}
JSON
```

- Use `daily` for weekly recurring windows. Monday is `0` and Sunday is `6`. An overnight window is associated with the day on which it starts.
- Use `once` for fixed-date windows. Specify a UTC offset explicitly. Both types include their start and expire at their end. Overlapping windows combine.
- Run `focus_gate configure --policy-file ./schedule.json` to load a file. If the current global schedule is blocking sessions, wait for that restriction to end before changing the schedule.
- Edits retain every unexpired one-off block from an enabled plan, including rules added with the global `restrict` command. Focus Gate rejects `"enabled": false` while one-off blocks are pending. Timers and project restrictions apply independently.

### Allow time before a cooldown

```sh
focus_gate timer --minutes 45
```

Choose 1–240 minutes. The timer starts immediately and keeps counting if Codex closes, macOS locks, or the computer sleeps. A one-hour global cooldown begins when the usage period ends. Existing global and project rules apply on their own schedules. Focus Gate rejects requests to reset or extend a timer during either the usage period or the cooldown.

### Block one project

```sh
# Run in the target project directory or replace "$PWD" with its absolute path
focus_gate restrict --minutes 30 --folder "$PWD"
```

Focus Gate matches the session's working directory against the target folder and its subfolders, resolving symbolic links. A Git worktree elsewhere is evaluated from its own working directory.

### Check status

| Command | What it shows |
| --- | --- |
| `focus_gate status` | Whether the current directory is restricted, global schedules, timers, and folder rules |
| `focus_gate status --folder "$PWD"` | Status for a specified folder. Replace `"$PWD"` with the project's absolute path. |

## Enforcement scope

The administrator configuration enables and trusts the hooks. During a block, Codex rejects subsequent prompts and supported tool calls. The rejection message shows the remaining time when it can be calculated. Folder rules apply to the session's working directory. macOS and the sandbox control file access. Focus Gate covers relevant hooks in local Codex. Cloud sessions and operations that have already started fall outside that scope. [Codex hook coverage](https://learn.chatgpt.com/docs/hooks#tool-coverage)

Rules and timers are stored in the current account's `~/Library/Application Support/FocusGate/`. The timer guarantee above applies to Focus Gate commands and interfaces. macOS permissions still govern account access to its own data and administrator control of the installation.

## Update and test

Pull and review the latest code, rerun the install and check commands, then run `focus_gate status`. Migration preserves existing restrictions and timers while removing obsolete credentials and temporary overrides. The installer archives the previous managed script.

Run the suite from the repository root. It uses a temporary state directory and runs under a regular account.

```sh
/usr/bin/python3 -B -m unittest discover -s tests
```

## License

[MIT](LICENSE)
