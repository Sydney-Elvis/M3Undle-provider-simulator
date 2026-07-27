#!/usr/bin/env bash
# Materializes any generate: blocks in a scenario, then runs it. Proves the
# full worker-orchestration pipeline works from a clean checkout with zero
# manual steps.
#
# Usage:
#   ./run-scenario.sh <scenario.yaml>                    # server mode (--scenario)
#   ./run-scenario.sh <scenario.yaml> -- <provider_sim.py args...>
#
# Note: provider_sim.py's --run takes the scenario path as ITS OWN value and
# ignores --scenario, so to self-verify pass the path again in the trailing
# args, exactly as you would calling provider_sim.py directly, e.g.:
#   ./run-scenario.sh scenarios/core/X.yaml -- --run scenarios/core/X.yaml --result-file out.json
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ $# -lt 1 ]; then
  echo "usage: $0 <scenario.yaml> [-- provider_sim.py args...]" >&2
  exit 2
fi

SCENARIO="$1"
shift
EXTRA_ARGS=()
if [ "${1:-}" = "--" ]; then
  shift
  EXTRA_ARGS=("$@")
fi

echo "==> Materializing generate: blocks for $SCENARIO"
python3 src/materialize_fixtures.py "$SCENARIO"

if [ "${#EXTRA_ARGS[@]}" -eq 0 ]; then
  echo "==> Running scenario (server mode)"
  exec python3 src/provider_sim.py --scenario "$SCENARIO"
fi

echo "==> Running scenario"
exec python3 src/provider_sim.py "${EXTRA_ARGS[@]}"
