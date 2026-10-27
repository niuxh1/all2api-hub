#!/bin/bash
# ===========================================================
#  Fusion hub - macOS double-click launcher
#
#  Finder runs .command files in Terminal; this simply forwards
#  to start-hub.sh (same arguments, same behaviour).
#
#  Usage: double-click, or  ./start-hub.command [port]
# ===========================================================

set -u

HERE="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/start-hub.sh" "$@"
