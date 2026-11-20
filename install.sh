#!/bin/bash
set -euo pipefail
umask 022

bundle_dir=$(CDPATH= cd -P -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd)
codex_dir=/etc/codex
hooks_dir="$codex_dir/focus-gate"
policy="$codex_dir/requirements.toml"
hook="$hooks_dir/focus_gate.py"
bundled_policy="$bundle_dir/requirements.toml"
bundled_hook="$bundle_dir/focus_gate.py"

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

exists() {
    [[ -e $1 || -L $1 ]]
}

secure_directory() {
    [[ ! -L $1 && -d $1 && $(/usr/bin/stat -f '%u:%g:%Lp' "$1") == 0:0:755 ]]
}

secure_file() {
    [[ ! -L $1 && -f $1 && $(/usr/bin/stat -f '%u:%g:%Lp' "$1") == 0:0:644 ]]
}

check_paths() {
    secure_directory /private/etc || fail '系统 /private/etc 的所有者或权限异常。'
    if exists "$codex_dir"; then
        secure_directory "$codex_dir" || fail "请检查系统目录 $codex_dir 的所有者和权限。"
    fi
    if exists "$hooks_dir"; then
        secure_directory "$hooks_dir" || fail "请检查受管脚本目录 $hooks_dir 的所有者和权限。"
    fi
    if exists "$policy"; then
        if ! secure_file "$policy" || ! /usr/bin/cmp -s "$bundled_policy" "$policy"; then
            fail "已有其他系统要求文件 ${policy}，请由管理员手动合并受管规则。"
        fi
    fi
    if exists "$hook"; then
        secure_file "$hook" || fail "请检查现有脚本 $hook 的所有者和权限。"
    fi
}

require_root() {
    [[ $EUID -eq 0 ]] || fail '请用 sudo /bin/bash 运行此脚本的 install 或 rollback 命令。'
}

archive_directory() {
    /usr/bin/mktemp -d "$codex_dir/focus-gate-backup.XXXXXXXX"
}

[[ $(/usr/bin/uname -s) == Darwin ]] || fail '此安装脚本适用于 macOS。'
[[ ! -L $bundled_policy && -f $bundled_policy ]] || fail '安装包缺少 requirements.toml。'
[[ $# -eq 1 ]] || fail '用法为 /bin/bash install.sh check、install 或 rollback。'

case "$1" in
    check)
        check_paths
        [[ -f $policy && -f $hook ]] || fail 'FocusGate 受管规则尚未安装完整。'
        /usr/bin/cmp -s "$bundled_hook" "$hook" || fail '已安装脚本与当前安装包版本不同。'
        printf '%s\n' '系统要求和脚本文件通过检查。请在重启后的 Codex Hooks 设置确认它们由管理员管理。'
        ;;
    install)
        require_root
        check_paths
        [[ ! -L $bundled_hook && -f $bundled_hook ]] || fail '安装包缺少 focus_gate.py。'
        /usr/bin/python3 -I -B -c 'import sys; assert sys.version_info >= (3, 9); path = sys.argv[1]; compile(open(path, "rb").read(), path, "exec")' "$bundled_hook"

        if ! exists "$codex_dir"; then
            /usr/bin/install -d -o root -g wheel -m 755 "$codex_dir"
        fi
        if ! exists "$hooks_dir"; then
            /usr/bin/install -d -o root -g wheel -m 755 "$hooks_dir"
        fi
        if exists "$hook" && ! /usr/bin/cmp -s "$bundled_hook" "$hook"; then
            [[ -f $policy ]] || fail "已有其他脚本 ${hook}，请由管理员检查。"
            backup=$(archive_directory)
            /usr/bin/install -S -o root -g wheel -m 644 "$hook" "$backup/focus_gate.py"
            printf '%s\n' "上一版脚本保存到 $backup"
        fi
        /usr/bin/install -CS -o root -g wheel -m 644 "$bundled_hook" "$hook"
        if ! exists "$policy"; then
            /usr/bin/install -S -o root -g wheel -m 644 "$bundled_policy" "$policy"
        fi

        check_paths
        printf '%s\n' 'FocusGate 系统文件已安装。请完全退出并重新打开 Codex，再检查 Hooks 中的受管状态。'
        ;;
    rollback)
        require_root
        check_paths
        if ! exists "$policy" && ! exists "$hook"; then
            printf '%s\n' 'FocusGate 受管规则已停用。'
            exit 0
        fi
        if ! exists "$policy"; then
            /usr/bin/cmp -s "$bundled_hook" "$hook" || fail "已有其他脚本 ${hook}，请由管理员检查。"
        fi
        backup=$(archive_directory)
        if exists "$policy"; then
            /bin/mv "$policy" "$backup/requirements.toml"
        fi
        if exists "$hook"; then
            /bin/mv "$hook" "$backup/focus_gate.py"
        fi
        printf '%s\n' "系统文件移至 ${backup}。请完全退出并重新打开 Codex。"
        ;;
    *) fail '用法为 /bin/bash install.sh check、install 或 rollback。' ;;
esac
