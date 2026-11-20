# Agent Focus Gate

Set focus windows for local Codex on macOS. You can pause requests in one project folder, pause all local sessions, or set an allowance followed by a cooldown. Codex runs the checks through its administrator-managed hooks, which it trusts by policy. [Codex managed hooks documentation](https://learn.chatgpt.com/docs/hooks#managed-hooks-in-local-codex)

## Requirements

- macOS with `/usr/bin/python3` 3.9 or newer and its standard library.
- A local Codex client that supports administrator-managed hooks.
- A macOS administrator for installation, updates, and removal.

## Install

From a checkout, review `install.sh`, `requirements.toml`, and `focus_gate.py`, then run:

```sh
sudo /bin/bash ./install.sh install
/bin/bash ./install.sh check
```

The installer places the managed policy at `/etc/codex/requirements.toml` and the script at `/etc/codex/focus-gate/focus_gate.py`. It checks ownership and permissions and stops if an existing requirements file differs from the bundled one. Deploying into a shared requirements file requires an administrator to review and manage the merge separately.

Fully quit and reopen Codex after installation. In the desktop app, open **Settings → Hooks** and look under **Administrator configuration** for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, and `PermissionRequest`. Start a new local session to verify the policy.

## Set a focus window

Run these commands from Terminal as the local account that runs Codex. On first use, `setup`, `restrict`, or `timer` opens a hidden macOS dialog to create a Focus Gate password.

```sh
# Set the Focus Gate password.
/usr/bin/python3 -I -B /etc/codex/focus-gate/focus_gate.py setup

# Pause Codex in the current folder and its subfolders for 30 minutes.
/usr/bin/python3 -I -B /etc/codex/focus-gate/focus_gate.py restrict --minutes 30 --folder "$PWD"

# Check the rule as it applies to the current folder.
/usr/bin/python3 -I -B /etc/codex/focus-gate/focus_gate.py status
```

`--folder` takes the absolute path to an existing directory. Focus Gate resolves symbolic links and matches the Codex session's working directory against that directory and its descendants. A Git worktree elsewhere follows its actual location. Existing global rules also apply.

For additional commands, use `/usr/bin/python3 -I -B /etc/codex/focus-gate/focus_gate.py --help`.

| Command | Behavior |
| --- | --- |
| `restrict --minutes N` | Pause all local Codex sessions immediately for 1 to 10,080 minutes. |
| `timer --minutes N` | Allow 1 to 240 minutes of elapsed time, then apply a one-hour global cooldown. Existing schedules can also restrict usage during the allowance. |
| `status --folder /absolute/path` | Check restrictions for a named project folder. |
| `unlock --minutes N` | Temporarily allow all scopes for 15, 30, or 60 minutes after the Focus Gate password is verified. |
| `lock` | End the temporary unlock. |
| `cancel-timer` | Cancel the active allowance or cooldown after password verification. |
| `password` | Verify and change the Focus Gate password. |

The Focus Gate password is entered in the native dialog. Use Terminal for password-protected commands during a restriction, since Codex can reject new prompts at that time. The script stores rules and a salted password verifier under the local account's `~/Library/Application Support/FocusGate/`.

## Scope

Administrator management protects the hook registration and script from ordinary user-level edits and from the user hook toggles. Focus Gate is a self-management tool with per-account settings. The folder rule controls Codex sessions based on their working directory. Filesystem permissions govern access to the files themselves. Cloud sessions and actions that have already started fall outside these checks. [Codex hook coverage](https://learn.chatgpt.com/docs/hooks#tool-coverage)

## Update or remove

To update, review the new checkout and run the installation commands again. The installer archives the previous managed script when it changes. The `check` command compares the installed files with the current checkout.

To remove this managed configuration, run:

```sh
sudo /bin/bash ./install.sh rollback
```

The `rollback` command moves the installed files into a recovery directory under `/etc/codex/`. The local account's rules and stored timer remain available for a future installation. Fully quit and reopen Codex to load the updated configuration.

## Tests

Run the suite without administrator privileges from the repository root. It uses temporary state and checks the hook responses for the target folder and nearby folders.

```sh
/usr/bin/python3 -B -m unittest discover -s tests
```

## License

[MIT](LICENSE)
