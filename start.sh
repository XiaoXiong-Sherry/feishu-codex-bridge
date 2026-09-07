#!/usr/bin/env bash
set -u

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
config_home=${XDG_CONFIG_HOME:-$HOME/.config}
env_file=${CODEX_FEISHU_ENV:-$config_home/codex-feishu/env}
state_dir=${XDG_STATE_HOME:-$HOME/.local/state}
log_file=$state_dir/codex-feishu.log
previous_log_file=$state_dir/codex-feishu.log.previous
pid_file=$state_dir/codex-feishu.pid
python_bin=$script_dir/.pixi/envs/default/bin/python
bridge=$script_dir/bridge.py

get_pid() {
    local pid cmdline
    [[ -f "$pid_file" ]] || return 1
    pid=$(<"$pid_file")
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    if [[ -r "/proc/$pid/cmdline" ]]; then
        cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline")
    else
        cmdline=$(ps -p "$pid" -o command= 2>/dev/null) || return 1
    fi
    [[ "$cmdline" == *"$bridge"* ]] || return 1
    printf '%s\n' "$pid"
}

file_mode() {
    if stat -c '%a' "$1" >/dev/null 2>&1; then
        stat -c '%a' "$1"
    else
        stat -f '%Lp' "$1"
    fi
}

load_env() {
    [[ -f "$env_file" ]] || { echo "凭证文件不存在：$env_file" >&2; exit 1; }
    [[ $(file_mode "$env_file") == 600 ]] || { echo "凭证文件权限必须是 600" >&2; exit 1; }
    set -a
    source "$env_file"
    set +a
    [[ -n "${FEISHU_APP_ID:-}" && -n "${FEISHU_APP_SECRET:-}" ]] || { echo "App ID 或 App Secret 未填写" >&2; exit 1; }
    [[ -n "${FEISHU_ALLOWED_OPEN_ID:-}" ]] || { echo "FEISHU_ALLOWED_OPEN_ID 未填写" >&2; exit 1; }
}

start() {
    local pid
    mkdir -p "$state_dir"
    umask 077
    if pid=$(get_pid); then
        echo "机器人已经运行，PID：$pid"
        return
    fi
    rm -f "$pid_file"
    [[ -x "$python_bin" ]] || { echo "项目依赖尚未安装" >&2; exit 1; }
    load_env
    if [[ -f "$log_file" ]]; then
        mv -f "$log_file" "$previous_log_file"
    fi
    touch "$log_file"
    chmod 600 "$log_file"
    if command -v setsid >/dev/null 2>&1; then
        nohup setsid "$python_bin" -u "$bridge" </dev/null >>"$log_file" 2>&1 &
    else
        # macOS has no setsid, so leave the caller's session from Python before
        # exec'ing the bridge; otherwise closing the caller takes the bridge down.
        nohup "$python_bin" -c 'import os, sys
try:
    os.setsid()
except OSError:
    pass
os.execv(sys.argv[1], [sys.argv[1], "-u", sys.argv[2]])' "$python_bin" "$bridge" </dev/null >>"$log_file" 2>&1 &
    fi
    pid=$!
    printf '%s\n' "$pid" > "$pid_file"
    sleep 2
    if get_pid >/dev/null; then
        echo "机器人已启动，PID：$pid"
        echo "当前启动日志：$log_file"
        echo "上一次启动日志：$previous_log_file"
    else
        rm -f "$pid_file"
        echo "机器人启动失败" >&2
        tail -10 "$log_file" >&2
        exit 1
    fi
}

stop() {
    local pid
    if ! pid=$(get_pid); then
        rm -f "$pid_file"
        echo "机器人未运行"
        return
    fi
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in {1..50}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
    echo "机器人已停止"
}

status() {
    local pid
    if pid=$(get_pid); then
        echo "机器人正在运行，PID：$pid"
    else
        echo "机器人未运行"
    fi
    echo "当前启动日志："
    if [[ -s "$log_file" ]]; then
        tail -10 "$log_file"
    else
        echo "当前启动暂无错误日志。"
    fi
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    status) status ;;
    *) echo "用法：start.sh [start|stop|status]" >&2; exit 2 ;;
esac
