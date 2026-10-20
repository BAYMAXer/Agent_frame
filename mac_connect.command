#!/bin/bash
# Run on the Mac. The SSH tunnel exposes only the Mac Studio service, not macOS SSH.
set -euo pipefail
if [[ "$(uname -s)" != Darwin ]]; then
  echo '请在 Mac 的终端运行此文件，当前机器不是 macOS。' >&2
  exit 1
fi

local_port=${MAC_STUDIO_PORT:-8088}
remote_port=${MAC_TUNNEL_PORT:-18088}
ssh_port=${MAC_SSH_PORT:-22}
for port in "$local_port" "$remote_port" "$ssh_port"; do
  if ! [[ "$port" =~ ^[0-9]+$ ]] || (( ${#port} > 5 )) || (( 10#$port < 1 || 10#$port > 65535 )); then
    echo '端口必须为 1–65535。' >&2; exit 1
  fi
done
server=${1:-}
if [[ -z "$server" ]]; then
  read -r -p '这台 Linux 服务器的 SSH 地址（用户名@IP，使用你平时连接服务器的地址）: ' server
fi
if [[ ! "$server" =~ ^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$ ]]; then
  echo 'SSH 地址格式不正确，请使用 用户名@IP 或 SSH 配置别名。' >&2; exit 1
fi

is_mac_studio() {
  [[ "$1" =~ \"service\"[[:space:]]*:[[:space:]]*\"yaero-studio\" &&
     "$1" =~ \"platform\"[[:space:]]*:[[:space:]]*\"darwin\" ]]
}
port_in_use() {
  curl --noproxy '*' -s --max-time 2 -o /dev/null "http://127.0.0.1:$1/" 2>/dev/null ||
    lsof -nP -iTCP:"$1" -sTCP:LISTEN -t >/dev/null 2>&1
}
base="http://127.0.0.1:$local_port"
info=$(curl --noproxy '*' -fsS --max-time 3 "$base/runtime/info" 2>/dev/null || true)
if ! is_mac_studio "$info"; then
  if [[ -n "$info" ]] || port_in_use "$local_port"; then
    if [[ "$info" =~ \"platform\"[[:space:]]*:[[:space:]]*\"linux\" ]]; then
      echo "端口 $local_port 返回 platform=linux：这是 Linux 后端，可能通过 SSH 转发到了 Mac。"
    else
      echo "端口 $local_port 已被占用，且不是可确认的新版 Mac 框架服务。"
    fi
    if [[ -n "${MAC_STUDIO_PORT:-}" ]]; then
      echo '请用 MAC_STUDIO_PORT 指定另一个空闲端口，再运行脚本。' >&2; exit 1
    fi
    found=false
    for candidate_port in 8090 8091 8092 8093 8094 8095 8096 8097 8098 8099; do
      if ! port_in_use "$candidate_port"; then
        local_port=$candidate_port;found=true;break
      fi
    done
    if [[ "$found" != true ]]; then
      echo '8090–8099 都被占用，请用 MAC_STUDIO_PORT 指定空闲端口。' >&2; exit 1
    fi
    base="http://127.0.0.1:$local_port"
    echo "保留原服务，改用 Mac 本地端口 $local_port 启动框架。"
  fi
  root=$(cd "$(dirname "$0")" && pwd)
  if [[ ! -f "$root/viz/server.py" ]]; then
    read -r -p 'Mac 上框架目录的绝对路径（包含 viz/server.py）: ' root
  fi
  if [[ ! -f "$root/viz/server.py" || ! -f "$root/run_local.py" ]]; then
    echo '未找到完整框架。请先将新版项目放到 Mac，再运行此文件。' >&2; exit 1
  fi
  root=$(cd "$root" && pwd)
  python_bin=${MAC_PYTHON:-}
  if [[ -z "$python_bin" ]]; then
    for candidate in "$root/.venv-mac/bin/python" "$root/.venv/bin/python" "$root/../.venv/bin/python"; do
      if [[ -x "$candidate" ]]; then python_bin=$candidate; break; fi
    done
  fi
  if [[ -z "$python_bin" ]]; then
    echo '没有找到 Python 虚拟环境。请先安装 requirements-mac.txt，或设置 MAC_PYTHON 为已安装依赖的 Python 绝对路径。' >&2; exit 1
  fi
  # Make arbitrary project folder names importable for both Studio and its runners.
  runtime="$root/.runtime/mac-connect"
  mkdir -p "$runtime/imports"
  if [[ -e "$runtime/imports/osworld_agent" || -L "$runtime/imports/osworld_agent" ]]; then
    [[ "$(readlink "$runtime/imports/osworld_agent")" == "$root" ]] || { echo '导入目录已被其他路径占用。' >&2; exit 1; }
  else
    ln -s "$root" "$runtime/imports/osworld_agent"
  fi
  nohup env PYTHONPATH="$runtime/imports${PYTHONPATH:+:$PYTHONPATH}" \
    OSWORLD_RUNNER_PYTHON="$python_bin" OSWORLD_AGENT_ROOT="$root" AGENTS_VIZ_PORT="$local_port" \
    "$python_bin" -m osworld_agent.viz.server >"$runtime/studio.log" 2>&1 </dev/null &
  studio_pid=$!
  echo "$studio_pid" >"$runtime/studio.pid"
  for ((i=0; i<40; i++)); do
    if curl --noproxy '*' -fsS --max-time 1 "$base/runtime/info" >/dev/null 2>&1; then break; fi
    if ! kill -0 "$studio_pid" 2>/dev/null; then break; fi
    sleep 0.5
  done
  echo "框架日志：$runtime/studio.log"
fi
info=$(curl --noproxy '*' -fsS --max-time 4 "$base/runtime/info") || { echo 'Mac 框架没有启动成功。' >&2; exit 1; }
if ! is_mac_studio "$info"; then
  echo "服务 $base 没有返回 service=yaero-studio、platform=darwin。请检查启动日志及端口转发；不建立隧道。" >&2; exit 1
fi

echo "Mac 前端：$base"
echo "正在建立隧道：服务器 127.0.0.1:$remote_port → Mac 127.0.0.1:$local_port"
echo '如提示密码，请在本机终端输入 SSH 密码，不要发给助手。首次连接请核对服务器指纹。'
echo '连接后保持此窗口打开，并告诉助手“Mac 隧道已连接”。按 Ctrl+C 断开；本机框架仍可继续使用。'
open "$base"
exec ssh -N -T -p "$ssh_port" \
  -o "ControlPath=${MAC_SSH_CONTROL_PATH:-none}" \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -R "127.0.0.1:$remote_port:127.0.0.1:$local_port" "$server"
