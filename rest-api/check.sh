#!/usr/bin/env bash
# Runs the unit tests (100% statement + branch coverage required) and the
# mutation tests (no surviving mutant allowed). Exits non-zero on any failure.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"

"$PY" -m pytest -q

rm -rf mutants
"$PY" -m mutmut run
"$PY" -m mutmut export-cicd-stats >/dev/null

"$PY" - <<'PYEOF'
import json, sys
stats = json.load(open("mutants/mutmut-cicd-stats.json"))
bad = {k: stats[k] for k in ("survived", "suspicious", "timeout", "no_tests", "segfault") if stats[k]}
print(f"mutants: {stats['killed']} killed of {stats['total']}")
if bad:
    print("FAILED - not killed:", bad, "(inspect with: python3 -m mutmut results)")
    sys.exit(1)
PYEOF
