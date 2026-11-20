# Agent Focus Gate

简体中文 · [English](README.en.md)

为 macOS 上的本机 Codex 设置使用限制。可以立即暂停所有项目、按日程自动禁用、先使用一段时间再进入冷却，也可以只暂停某个项目。限制通过 Codex 的管理员受管钩子执行。 [Codex 受管钩子文档](https://learn.chatgpt.com/docs/hooks#managed-hooks-in-local-codex)

> [!IMPORTANT]
> 以下说明适用于 Focus Gate 的命令与接口。
>
> 请注意，一旦开始计时，无法通过任何方式解除限制。须等到使用时段及随后一小时的冷却结束才能重新开始计时。

## 主要功能

| 功能 | 何时使用 | 示例 |
| --- | --- | --- |
| **全部禁用** | 从现在起暂停所有本机 Codex 会话，到期自动解除这条限制 | 全部禁用 2 小时 |
| **定时禁用** | 按星期和时间重复禁用，或预约一次指定起止时间的禁用 | 每天 22:00 到次日 08:00 暂停 |
| **限时使用** | 从现在起允许使用一段时间，到期后全局冷却 1 小时 | 再用 45 分钟，然后冷却 |
| **只禁用某个项目** | 立即暂停指定文件夹及其子目录中的 Codex 会话 | 只暂停当前项目 30 分钟 |
| **查看状态** | 查看当前目录的受限状态、全局计划、计时与项目规则 | 查看当前项目的限制 |

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

在日常运行 Codex 的同一 macOS 账户下打开终端。下面的函数仅在当前终端中为后续命令缩短路径。创建限制和查看状态可直接用该账户运行。

```sh
focus_gate() {
  /usr/bin/python3 -I -B /etc/codex/focus-gate/focus_gate.py "$@"
}
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
- `once` 可以填多条一次性计划，建议时间带上明确的 UTC 偏移。两类计划都包含开始时刻，到结束时刻解除这条限制。重叠的区间合并计算。
- 也可用 `focus_gate configure --policy-file ./schedule.json` 载入文件。当前全局计划正在禁用时，须等本次限制结束才能变更计划。
- 已启用计划中尚未到期的一次性限制会保留，包括通过「全部禁用」追加的规则。有未到期的一次性限制时，`"enabled": false` 会被拒绝。单独的限时使用和项目禁用继续按各自规则生效。

### 先用一段时间，再进入冷却

```sh
focus_gate timer --minutes 45
```

可设 1–240 分钟。倒计时从执行时开始，关掉 Codex、锁屏或睡眠时继续计算。到期后全局冷却 1 小时，已有的全局或项目限制仍按自己的时间生效。在使用时段和冷却期间，Focus Gate 拒绝重设或延长本次计时。

### 只禁用一个项目

```sh
# 在目标项目文件夹中运行，也可以把 "$PWD" 换成目标的绝对路径
focus_gate restrict --minutes 30 --folder "$PWD"
```

规则根据 Codex 会话的工作目录匹配目标文件夹和子目录，并解析符号链接。位于其他路径的 Git worktree 按实际工作目录判断。

### 查看状态

| 命令 | 用途 |
| --- | --- |
| `focus_gate status` | 查看当前目录是否受限，以及全局计划、计时和文件夹规则 |
| `focus_gate status --folder "$PWD"` | 查询指定目录，可把 `"$PWD"` 换成项目的绝对路径 |

## 生效范围

管理员配置负责启用并信任检查钩子。受限时 Codex 会拦截后续请求和受支持的工具调用，并在能计算时显示剩余时间。文件夹规则约束会话的工作目录，文件读写权限由 macOS 和沙盒管理。该机制覆盖本机 Codex 的相关钩子事件。云端会话和已经开始的操作不在检查范围内。 [Codex 钩子覆盖范围](https://learn.chatgpt.com/docs/hooks#tool-coverage)

规则和计时数据保存在当前账户的 `~/Library/Application Support/FocusGate/`。上面的计时提示针对 Focus Gate 提供的命令与接口。账户对自身数据的访问和管理员对系统安装的控制仍由 macOS 权限决定。

## 更新和测试

更新时拉取最新代码，审阅后重新运行安装和检查命令，再运行 `focus_gate status`。旧状态会迁移并保留已设规则与计时，废弃的验证数据和临时覆盖状态会被移除。安装器会归档被替换的系统脚本。

从仓库根目录运行测试。测试使用独立临时状态目录，无需管理员权限。

```sh
/usr/bin/python3 -B -m unittest discover -s tests
```

## 许可证

[MIT](LICENSE)
