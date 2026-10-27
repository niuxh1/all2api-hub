#!/bin/bash
# ===========================================================
#  Fusion hub - LAN mode (macOS / Linux)
#
#  Listens on every network interface so phones, laptops and
#  other PCs on the same network can use it.
#
#  An API key is REQUIRED in this mode: pass it as the second
#  argument, or let the hub generate one on first start (it is
#  printed in the startup output / data/hub/logs/hub.log).
#
#  Usage: ./start-hub-lan.sh [port] [api-key]
# ===========================================================

set -u

HERE="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HUB_LAUNCH_LAN=1
if [ -n "${2:-}" ]; then
  export HUB_API_KEY="$2"
fi

exec "$HERE/start-hub.sh" "${1:-8787}"
