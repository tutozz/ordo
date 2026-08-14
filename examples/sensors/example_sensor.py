#!/usr/bin/env python3
"""Example sensor for one campaign.

A sensor answers one question: what is actually true right now, independently of what
executor sessions claim in their reports. The graph says what is declared, the sensor says
what is measured. Confusing the two is how false completion gets manufactured.

Copy this file, point it at your own project, install it with:

    ordo sensor install c-01 ./my_sensor.py
    ordo sensor run c-01

Three hard rules, and they are the whole point:

  1. Never fill a gap with a default. Anything you could not measure goes into "unknown"
     with the reason. A zero that means "I could not reach the endpoint" is a lie that
     looks like a measurement.
  2. Never run a build or a test suite from here. A sensor observes, it does not produce.
     A sensor that takes two minutes stops being run.
  3. Keep "measured" and "declared" apart. "measured" is what nobody can fake: a file
     mtime, a process exit status, an HTTP status, a commit hash. "declared" is what a
     document or a report claims about itself.

Output contract: one JSON object on stdout, nothing else. No banner, no progress line.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Point these at your own project before using this file.
REPO = Path("/path/to/your/project")
ENDPOINT = "http://localhost:8080/health"
MISSION_DOC = REPO / "docs" / "mission.md"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


out: dict = {
    "at": utc_now(),
    "ok": True,
    "measured": [],
    "declared": [],
    "drift": [],
    "unknown": [],
}

# --- measured: the commit actually checked out -------------------------------
# A hash nobody can argue with. Failure to read it is unknown, never "none".
try:
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    out["measured"].append({"name": "head", "value": head.stdout.strip()})
except (subprocess.SubprocessError, OSError) as exc:
    out["unknown"].append({"name": "head", "why": str(exc)[:120]})

# --- measured: is the service answering --------------------------------------
# A timeout is an absent measurement, never a zero and never a false.
try:
    with urllib.request.urlopen(ENDPOINT, timeout=5) as response:
        out["measured"].append({"name": "health-http", "value": response.status})
except (urllib.error.URLError, OSError) as exc:
    out["unknown"].append({"name": "health-http", "why": str(exc)[:120]})

# --- measured: how stale the mission document is -----------------------------
# Age in seconds. This is the cheapest lie detector there is: a document claiming
# progress that has not been touched in an hour contradicts itself.
if MISSION_DOC.exists():
    age = int(time.time() - MISSION_DOC.stat().st_mtime)
    out["measured"].append({"name": "doc-age", "value": age, "unit": "s"})
else:
    out["unknown"].append({"name": "doc-age", "why": "mission document not found"})

# --- declared: what the document says about itself ---------------------------
# Read, never trusted. Ordo displays it next to the measurements and flags the gap.
if MISSION_DOC.exists():
    for line in MISSION_DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("Proven:"):
            out["declared"].append(
                {
                    "name": "proven",
                    "value": line.split(":", 1)[1].strip(),
                    "source": str(MISSION_DOC),
                }
            )
            break

# --- drift: a contradiction the sensor can see and the graph cannot ----------
declared_proven = next((d for d in out["declared"] if d["name"] == "proven"), None)
doc_age = next((m for m in out["measured"] if m["name"] == "doc-age"), None)
if declared_proven and doc_age and doc_age["value"] > 3600:
    out["drift"].append(
        {
            "kind": "contradiction",
            "task": None,
            "detail": (
                f"document claims {declared_proven['value']} but has not changed "
                f"in {doc_age['value']} s"
            ),
        }
    )

print(json.dumps(out))
