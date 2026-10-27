#!/bin/bash
# ===========================================================
#  Hub proxy - stop (macOS / Linux)
#
#  Stops ONLY the process recorded in data/hub/hub.pid, and only after
#  checking that its command line really contains hub_proxy.py. There
#  is no "pkill -f python" / "pgrep -f hub" style fuzzy matching
#  anywhere in this file: another Python service on this machine must
#  never be touched.
#
#  Usage: ./stop-hub.sh
# ===========================================================

set -u

# --- resolve the directory this script lives in --------------------
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in
    /*) ;;
    *) SOURCE="$DIR/$SOURCE" ;;
  esac
done
HERE="$(cd -P "$(dirname "$SOURCE")" && pwd)"

DATADIR="${HUB_DATA_DIR:-$HERE/data/hub}"
PIDFILE="$DATADIR/hub.pid"

echo "融合网关 Hub - 停止"

if [ ! -f "$PIDFILE" ]; then
  echo "没找到 pid 文件，可能没在运行。"
  echo "  No pid file - the hub does not look like it is running."
  echo "  期望位置: $PIDFILE"
  exit 0
fi

PID="$(head -n 1 "$PIDFILE" 2>/dev/null | tr -dc '0-9' || true)"
if [ -z "$PID" ]; then
  echo "[跳过] pid 文件里没有数字，无法确定要停哪个进程。"
  echo "        文件位置: $PIDFILE"
  exit 1
fi

if ! kill -0 "$PID" 2>/dev/null; then
  echo "进程已经不在运行了，PID $PID。"
  echo "已清理残留的 pid 文件：$PIDFILE"
  rm -f "$PIDFILE"
  exit 0
fi

CMD="$(ps -p "$PID" -o args= 2>/dev/null || true)"
if [ -z "$CMD" ]; then
  CMD="$(ps -p "$PID" -o command= 2>/dev/null || true)"
fi
case "$CMD" in
  *hub_proxy.py*) ;;
  *)
    echo "[跳过] PID $PID 的命令行里没有 hub_proxy.py，不是中枢进程。"
    echo "        command line: $CMD"
    echo "        为避免误杀别的 Python 服务，这里不做任何操作；pid 文件保留。"
    exit 1
    ;;
esac

echo "  发送 TERM 给 PID $PID ..."
if ! kill -TERM "$PID" 2>/dev/null; then
  echo "[失败] 无法发送 TERM，可能需要 sudo，或该进程属于别的用户。"
  exit 1
fi

# 最多等 10 秒（50 × 0.2s），之后升级到 KILL
i=0
while [ "$i" -lt 50 ]; do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.2
  i=$((i + 1))
done

if kill -0 "$PID" 2>/dev/null; then
  echo "  10 秒内没有退出，改用 KILL ..."
  kill -KILL "$PID" 2>/dev/null || true
  sleep 0.5
fi

if kill -0 "$PID" 2>/dev/null; then
  echo "[失败] PID $PID 仍在运行，请手动确认。"
  exit 1
fi

rm -f "$PIDFILE"
echo "已停止中枢，PID $PID。pid 文件已删除：$PIDFILE"
exit 0
