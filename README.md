# Agent Focus Gate

为 macOS 上的本机 Codex 设置使用限制。可以立即暂停所有项目、按日程自动禁用、先使用一段时间再进入冷却，也可以只暂停某个项目。限制通过 Codex 的管理员受管钩子执行。 [Codex 受管钩子文档](https://learn.chatgpt.com/docs/hooks#managed-hooks-in-local-codex)

## 主要功能

| 功能 | 何时使用 | 示例 |
| --- | --- | --- |
| **全部禁用** | 从现在起暂停所有本机 Codex 会话，到期自动解除这条限制 | 全部禁用 2 小时 |
| **定时禁用** | 按星期和时间重复禁用，或预约一次指定起止时间的禁用 | 每天 22:00 到次日 08:00 暂停 |
| **限时使用** | 从现在起允许使用一段时间，到期后全局冷却 1 小时 | 再用 45 分钟，然后冷却 |
| **只禁用某个项目** | 立即暂停指定文件夹及其子目录中的 Codex 会话 | 只暂停当前项目 30 分钟 |
| **密码与状态管理** | 查看规则和计时，临时解锁、恢复限制、取消计时或更改密码 | 用密码解锁 15 分钟 |

## 安装

需要 macOS、`/usr/bin/python3` 3.9 或更新版本、支持管理员受管钩子的本机 Codex，以及安装时的 macOS 管理员授权。

```sh
git clone https://github.com/DanielPro0706/agent-focus-gate.git
cd agent-focus-gate
# 审阅 install.sh、requirements.toml 和 focus_gate.py 后执行
sudo /bin/bash ./install.sh install
/bin/bash ./install.sh check
```

安装器将受管配置和脚本分别安装到 `/etc/codex/requirements.toml` 与 `/etc/codex/focus-gate/focus_gate.py`。如果系统已有不同内容的 `requirements.toml`，安装器会停止，由管理员检查并合并现有配置。

完全退出并重开 Codex，在 **设置 → Hooks → 管理员配置** 中确认 `SessionStart`、`UserPromptSubmit`、`PreToolUse` 和 `PermissionRequest` 四项，再打开一个本机会话。

## 使用

在日常运行 Codex 的同一 macOS 账户下打开终端。下面的函数仅在当前终端中为后续命令缩短路径。`setup` 会在本机弹窗中要求输入并确认 Focus Gate 密码。

```sh
focus_gate() {
  /usr/bin/python3 -I -B /etc/codex/focus-gate/focus_gate.py "$@"
}
focus_gate setup
```

### 立即全部禁用

```sh
focus_gate restrict --minutes 120
```

立即对所有受支持的本机 Codex 会话施加 120 分钟的禁用。支持 1–10,080 分钟，可与现有规则叠加。

### 定时禁用

下面的计划每天 22:00 开始禁用，次日 08:00 恢复，并为指定日期额外安排一次两小时的禁用。使用前把一次性日期改成你需要的日期。

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

- `daily` 可以填多条每周重复的计划，星期一为 `0`，星期日为 `6`。跨午夜按开始当天的星期匹配。
- `once` 可以填多条一次性计划，建议时间带上明确的 UTC 偏移。两类计划都包含开始时刻，到结束时刻恢复。重叠的区间合并计算。
- 也可用 `focus_gate configure --policy-file ./schedule.json` 载入文件。保存需要验证密码，并会替换整个全局计划，包括此前通过「全部禁用」追加的一次性规则。更新前可用 `status` 查看并保留仍需生效的条目。
- `"enabled": false` 会关闭全局计划中的重复和一次性限制。限时使用的冷却与按项目禁用各自独立生效。

### 先用一段时间，再进入冷却

```sh
focus_gate timer --minutes 45
```

可设 1–240 分钟。倒计时从执行时开始，关掉 Codex、锁屏或睡眠时继续计算。到期后全局冷却 1 小时，已有的全局或项目限制仍按自己的时间生效。重设正在进行的计时或冷却需要验证密码。

### 只禁用一个项目

```sh
# 在目标项目文件夹中运行，也可以把 "$PWD" 换成目标的绝对路径
focus_gate restrict --minutes 30 --folder "$PWD"
```

规则根据 Codex 会话的工作目录匹配目标文件夹和子目录，并解析符号链接。位于其他路径的 Git worktree 按实际工作目录判断。

### 查看状态与解锁

| 命令 | 用途 |
| --- | --- |
| `focus_gate status` | 查看当前目录是否受限，以及全局计划、计时、文件夹规则和解锁状态 |
| `focus_gate status --folder "$PWD"` | 查询指定目录，可把 `"$PWD"` 换成项目的绝对路径 |
| `focus_gate unlock --minutes 15` | 验证密码后对全部范围临时解锁，支持 15、30 或 60 分钟 |
| `focus_gate lock` | 结束临时解锁，恢复按现有规则判断 |
| `focus_gate cancel-timer` | 验证密码后取消倒计时或冷却，并结束临时解锁 |
| `focus_gate password` | 验证旧密码后更改密码 |

受限时可在终端执行解锁命令。Focus Gate 在本机隐藏弹窗中接收密码。使用规则及加盐的密码验证记录保存在当前用户的 `~/Library/Application Support/FocusGate/`。

## 生效范围

管理员配置负责启用并信任检查钩子，用户级钩子开关无法关闭这四项受管检查。受限时 Codex 会拦截后续请求和受支持的工具调用，并在能计算时显示剩余时间。文件夹规则约束会话的工作目录，文件读写权限由系统和沙盒管理。该机制覆盖本机 Codex 的相关钩子事件，云端会话和已经开始的操作不在检查范围内。 [Codex 钩子覆盖范围](https://learn.chatgpt.com/docs/hooks#tool-coverage)

## 更新、移除与测试

更新时拉取最新代码，审阅后重新运行安装和检查命令。安装器会归档被替换的系统脚本。要移除本项目安装的受管配置，请运行 `sudo /bin/bash ./install.sh rollback`，然后完全退出并重开 Codex。该命令将受管配置和脚本移入 `/etc/codex/` 下的恢复目录。

从仓库根目录运行测试。测试使用独立临时状态目录，无需管理员权限。

```sh
/usr/bin/python3 -B -m unittest discover -s tests
```

## 许可证

[MIT](LICENSE)
