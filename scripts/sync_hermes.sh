#!/usr/bin/env bash
set -euo pipefail

# sync_hermes.sh — Synchronize /usr/local/bin/ops, /usr/local/bin/sheets, SKILL.md,
# and purge the poisoned session 20261101_075410_23c3320d in ~/.hermes/state.db.

BRIDGE_URL="${OPS_API_URL:-}"
if [ -z "$BRIDGE_URL" ]; then
  for cand in "http://10.0.4.1:18890" "http://172.17.0.1:18890" "http://bot-controller:18890" "http://127.0.0.1:18890"; do
    if curl -fsS --max-time 2 "${cand}/health" >/dev/null 2>&1; then
      BRIDGE_URL="$cand"
      break
    fi
  done
fi
BRIDGE_URL="${BRIDGE_URL:-http://10.0.4.1:18890}"

echo "[1/4] Installing /usr/local/bin/ops and /usr/local/bin/sheets from ${BRIDGE_URL}..."
curl -fsSL "${BRIDGE_URL}/api/cli/ops" -o /usr/local/bin/ops
chmod +x /usr/local/bin/ops

curl -fsSL "${BRIDGE_URL}/api/cli/sheets" -o /usr/local/bin/sheets
chmod +x /usr/local/bin/sheets

echo "[2/4] Updating SKILL.md in ~/.hermes/skills/..."
HERMES_HOME="${HOME:-/root}/.hermes"
mkdir -p "${HERMES_HOME}/skills/ops"
curl -fsSL "${BRIDGE_URL}/api/cli/skill" -o "${HERMES_HOME}/skills/ops/SKILL.md"

# Also update any existing skill directories that reference ops or video
if [ -d "${HERMES_HOME}/skills" ]; then
  for sdir in "${HERMES_HOME}/skills"/*; do
    if [ -d "$sdir" ] && [ -f "${sdir}/SKILL.md" ]; then
      if grep -qiE "ops video|/api/video|ops scrape" "${sdir}/SKILL.md" 2>/dev/null; then
        cp "${HERMES_HOME}/skills/ops/SKILL.md" "${sdir}/SKILL.md"
        echo "  -> Updated ${sdir}/SKILL.md"
      fi
    fi
  done
fi

echo "[3/4] Resetting stale Telegram session 20261101_075410_23c3320d in ${HERMES_HOME}/state.db..."
python3 - <<'PY'
import glob
import os
import sqlite3

target_session = "20261101_075410_23c3320d"
db_candidates = glob.glob(os.path.expanduser("~/.hermes/*.db")) + glob.glob("/root/.hermes/*.db")
seen = set()
for db_path in db_candidates:
    real_p = os.path.realpath(db_path)
    if real_p in seen or not os.path.isfile(real_p):
        continue
    seen.add(real_p)
    try:
        conn = sqlite3.connect(real_p)
        cur = conn.cursor()
        tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        removed_total = 0
        for tbl in tables:
            cols = [c[1] for c in cur.execute(f"PRAGMA table_info('{tbl}')").fetchall()]
            for col in cols:
                if any(k in col.lower() for k in ("session", "id", "key", "value", "content", "messages")):
                    try:
                        cur.execute(f"DELETE FROM \"{tbl}\" WHERE CAST(\"{col}\" AS TEXT) LIKE ?", (f"%{target_session}%",))
                        if cur.rowcount and cur.rowcount > 0:
                            removed_total += cur.rowcount
                    except Exception:
                        pass
        conn.commit()
        conn.close()
        print(f"  -> Cleaned {removed_total} row(s) referencing {target_session} in {real_p}")
    except Exception as exc:
        print(f"  -> Skipped {real_p}: {exc}")
PY

echo "[4/4] Verifying CLI binaries..."
/usr/local/bin/ops --help >/dev/null
/usr/local/bin/sheets --help >/dev/null
echo "Done! ops and sheets CLIs are active and session 20261101_075410_23c3320d is cleared."
