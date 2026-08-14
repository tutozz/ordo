# The sensor

A sensor is a script that **you write**, which reads artifacts and returns a JSON object. It
exists for one reason only: to separate what is **measured** from what is **declared**.

The graph tells you what the executor sessions claim. The sensor tells you what the disk, the
network, and the containers actually show. Confusing the two produces false completion, which is
the defect this whole mode exists to catch.

---

## 1. Do you need one

Not always. **If the only usable measure is task progress, the graph is enough and there is no
sensor.** Writing one to measure what Ordo already knows is wasted work, and one more sensor to
maintain.

You need one when the campaign produces something observable outside Ordo:

| Campaign | What a sensor measures |
|---|---|
| documentation audit | file count, last modification date |
| API upgrade | HTTP status code of an endpoint, latency |
| deployment | healthy containers over total, version served |
| legacy test suite | number of tests collected in the last report already produced |

---

## 2. The output contract

Mandatory. The method is free: local, ssh, docker, curl, whatever you want. The script writes a
JSON object to stdout **and nothing else**.

```json
{
  "at": "2026-08-09T14:32:10Z",
  "ok": true,
  "measured": [
    {"name": "stories", "value": 12, "unit": "files"},
    {"name": "dev-api", "value": 200},
    {"name": "healthy", "value": "6/6"},
    {"name": "doc-modified", "value": 240, "unit": "s"}
  ],
  "declared": [
    {"name": "proof", "value": "42/379", "source": "docs/mission.md"}
  ],
  "drift": [
    {"kind": "contradiction", "task": "t-04",
     "detail": "claims +7 while the document has not moved in 38 min"}
  ],
  "unknown": [
    {"name": "coverage", "why": "no report generated"}
  ]
}
```

`measured` is true even if you lie: a modification date, a file count, an HTTP status code, a
healthy container. `declared` is what an executor or you assert in your own documents. Both are
displayed separately, and Ordo flags when they diverge.

---

## 3. The hard rules

**No default values.** What could not be measured goes into `unknown` with its reason. A zero in
place of a missing measurement is a lie, and it propagates.

**Read-only.** The sensor writes nothing, triggers nothing. It **never** launches a build or a
test suite: it reads the report a build has already produced. A sensor that runs a test on every
cycle turns an observer into an actor.

**Bounded output, hard timeout.** Ordo cuts it off. A slow sensor never blocks the cycle.

**Outside the audited repo.** The script lives in `~/.claude/ordo/sensors/`. A sensor committed
inside the repo it measures ends up being modified by an executor.

**The shebang is honored.** A Python sensor runs as Python. This point comes from a real defect: a
launcher that assumed `sh` made every non-shell script fail on every cycle, silently.

---

## 4. Adoption

**Three matching runs plus one human validation.**

Before that, `ordo sensor status` returns `unknown` and **serves no measurement**. You have no
right to draw any conclusion from it, and especially not to write in the journal that a value is
measured.

```bash
ordo sensor install <campaign> /path/to/script
ordo sensor run <campaign>        # to repeat, 3 matching runs
ordo sensor status <campaign> --json
ordo sensor adopt <campaign>      # after human validation
```

The normal time to write it is **after the graph is validated, while the first tasks are
running**. Not before: you do not yet know what needs to be measured.

---

## 5. The two failures, not to be confused

| Symptom | Name | What it means | Reaction |
|---|---|---|---|
| the script exits with an error or times out | failure | the sensor is broken | after **two** consecutive failures, you are woken up to fix it |
| the script returns the same output over N cycles | **frozen sensor** | the sensor no longer measures anything alive | you fix it; this is **not** a blocked task |

Reading a frozen sensor as a stalled campaign is exactly the misreading this contract exists to
prevent. A value that does not move can mean that nothing is moving, or that the sensor is looking
in the wrong place. The two are never told apart by the value alone.

---

## 6. A complete example

```python
#!/usr/bin/env python3
"""Sensor for campaign c-03: stocks API migration."""
import json, subprocess, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

out = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
       "ok": True, "measured": [], "declared": [], "drift": [], "unknown": []}

# Real endpoint. A timeout is a missing measurement, never a zero.
try:
    with urllib.request.urlopen("http://localhost:8080/stocks", timeout=5) as r:
        out["measured"].append({"name": "stocks-http", "value": r.status})
except Exception as e:
    out["unknown"].append({"name": "stocks-http", "why": str(e)[:120]})

# Age of the mission document, in seconds. What no one can falsify.
doc = Path("/home/user/code/myapp/docs/mission.md")
if doc.exists():
    out["measured"].append({"name": "doc-modified",
                            "value": int(time.time() - doc.stat().st_mtime),
                            "unit": "s"})
else:
    out["unknown"].append({"name": "doc-modified", "why": "document missing"})

# What the document CLAIMS. Declared, never measured.
if doc.exists():
    for line in doc.read_text().splitlines():
        if line.startswith("Proof:"):
            out["declared"].append({"name": "proof",
                                    "value": line.split(":", 1)[1].strip(),
                                    "source": str(doc)})
            break

print(json.dumps(out))
```

What it does right, and what to copy: it never fills a gap with a default value, it separates the
file's age (measured) from what the file claims (declared), and it never launches a build or a
test.
