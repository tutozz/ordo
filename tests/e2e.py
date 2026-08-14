#!/usr/bin/env python3
"""End-to-end walkthrough against real tmux panes, isolated by ORDO_HOME.

Drives the real `bin/ordo` CLI as an orchestratrice session would, in a temporary
ORDO_HOME and a temporary project directory -- never the real ~/.claude/ordo. Panes run
plain bash, never `claude`: no token is spent, no model is called. Where the flow needs
an "executante" writing to a real pane, this script spawns a bash pane directly via
lib.panes (the same primitives cli.py's launch verb uses) and injects a shell command
that writes the report file itself, standing in for what a real executante would do
after reading its brief.

Covers, in order: opening a chantier, adding a task graph with a dependency, checking
that `ready` only returns what is launchable, launching a simulated executante in a real
pane, it writing a report to the expected file, reconciliation via `tick`, checking a
checklist item, the dependent task unblocking, installing and adopting a capteur,
detecting a scope drift, writing to the journal, the regenerated brief, and closing the
chantier.

The rule that gives this file its value: every check re-reads the actual artifact on
disk (state.json, the journal file, a report file, a brief file) rather than trusting an
in-process function's return value or a CLI subprocess's stdout alone. A function that
returned True without writing anything would be caught here, because the very next thing
this script does is open the file it was supposed to have written and parse it.

Run: python3 tests/e2e.py
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Only used to spawn/talk to a REAL bash pane standing in for an executante (the one
# thing bin/ordo's own `launch` verb cannot do here: it hardcodes a real `claude`
# invocation) and to compute a couple of expected values independently of the CLI
# subprocess under test. Never used to bypass the CLI for anything the CLI itself does.
from ordo import panes, store  # noqa: E402  (sys.path must be set first)

BIN_ORDO = ROOT / "bin" / "ordo"
SESSION_PREFIX = "ordo-e2e-"
REAL_ORDO_HOME = Path.home() / ".claude" / "ordo"


class E2EError(RuntimeError):
    """A step's on-disk check did not hold. Fatal: the rest of the walkthrough depends
    on earlier steps having actually happened, not just claimed to."""


DISK_CHECKS = 0


def check(condition: bool, label: str) -> None:
    """Records one disk-backed verification. Raises loudly on failure; the caller is
    expected to have just re-read the relevant file or fresh subprocess output, never
    to be trusting a value computed earlier in the process.
    """
    global DISK_CHECKS
    DISK_CHECKS += 1
    if not condition:
        raise E2EError(f"echec au controle #{DISK_CHECKS} : {label}")
    print(f"  [ok] {label}")


def info(label: str) -> None:
    print(f"  -- {label}")


# ---------------------------------------------------------------------------
# Disk readers -- every one of these opens a real file and parses it fresh. No
# result is ever cached and reused across a later check().
# ---------------------------------------------------------------------------


def read_state(home: Path) -> dict:
    return json.loads((home / "state.json").read_text(encoding="utf-8"))


def read_journal(home: Path, chantier_id: str) -> str:
    path = home / "journal" / f"{chantier_id}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_report(home: Path, chantier_id: str, task_id: str) -> dict:
    return json.loads(
        (home / "reports" / chantier_id / f"{task_id}.json").read_text(encoding="utf-8")
    )


def real_home_fingerprint() -> set[str]:
    """Snapshot of every path under the REAL ~/.claude/ordo, to prove it never moves
    during this run. Absent entirely is a valid, common state (returns empty set)."""
    if not REAL_ORDO_HOME.exists():
        return set()
    # __pycache__ is excluded on purpose: importing ordo/ regenerates .pyc files whenever a
    # source file changed since the last run, which made this control fail on every code
    # edit. That is Python bookkeeping, not Ordo writing into the user's state; counting it
    # turned a real guard into a false alarm that the next reader would learn to ignore.
    return {
        str(p)
        for p in REAL_ORDO_HOME.rglob("*")
        if "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


# ---------------------------------------------------------------------------
# CLI driver -- every call is a fresh subprocess against the real bin/ordo, isolated
# by ORDO_HOME in the environment. Return values feed only into building the NEXT
# command (which id to pass where); every actual assertion re-reads a file.
# ---------------------------------------------------------------------------


def run_cli(env: dict, cwd: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BIN_ORDO), *args],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=30,
    )


def run_cli_json(env: dict, cwd: str, args: list[str]) -> dict:
    result = run_cli(env, cwd, [*args, "--json"])
    if result.returncode != 0:
        raise E2EError(
            f"commande CLI en echec ({' '.join(args)}) : {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise E2EError(
            f"sortie JSON illisible pour {' '.join(args)} : {exc}\nstdout={result.stdout!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Simulated executante -- a real bash pane, never `claude`. Mirrors what cli.py's
# _do_launch does (ensure_session, spawn, relayout, mark the task running), except the
# spawned command is a plain shell instead of the hardcoded `claude ...` invocation, so
# no token is ever spent and no model is ever called.
# ---------------------------------------------------------------------------

TEST_SHELL = "bash --noprofile --norc"


def launch_simulated_executante(
    home: Path, session: str, cwd: str, chantier_id: str, task_id: str
) -> str:
    """Spawns a real bash pane for task_id and marks it running, exactly like a real
    launch would leave the state, minus ever invoking `claude`. Returns the pane_id.

    Creates reports/<chantier>/ the way prompt.brief_executante() does at every real
    launch: an executor writes its report itself, at a path it was handed, and never
    creates one of Ordo's directories.
    """
    (home / "reports" / chantier_id).mkdir(parents=True, exist_ok=True)
    panes.ensure_session(session)
    pane_id = panes.spawn(session, cwd, TEST_SHELL, title=task_id)
    panes.relayout(session)

    with store.locked() as state:
        task = state["taches"][task_id]
        task["state"] = "running"
        task["paneId"] = pane_id
        task["cwd"] = cwd
        task["startedAt"] = store.now()
        task["finishedAt"] = None
        task["attempts"] += 1

    return pane_id


def send_report(home: Path, pane_id: str, chantier_id: str, task_id: str, payload: dict) -> None:
    """Injects a single-line shell command into the real pane that writes the report
    file itself -- the pane genuinely performs the write, this script never writes the
    report file directly. shlex.quote makes a heredoc unnecessary: tmux's -l literal
    send does not need embedded newlines to reach bash's parser correctly.
    """
    report_path = home / "reports" / chantier_id / f"{task_id}.json"
    payload_json = json.dumps(payload, ensure_ascii=False)
    cmd = f"printf '%s' {shlex.quote(payload_json)} > {shlex.quote(str(report_path))}"
    panes.send(pane_id, cmd)


def wait_for_report(
    home: Path, chantier_id: str, task_id: str, timeout: float = 10.0, interval: float = 0.2
) -> dict:
    """Polls the real report file until it exists AND parses as JSON, then returns the
    parsed content. Guards against reading a write still in flight in the pane rather
    than assuming a single printf redirection is atomic.
    """
    path = home / "reports" / chantier_id / f"{task_id}.json"
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            try:
                return read_report(home, chantier_id, task_id)
            except (json.JSONDecodeError, OSError) as exc:
                last_error = exc
        time.sleep(interval)
    raise E2EError(f"le rapport attendu n'est jamais devenu lisible : {path} ({last_error})")


def manually_ready(state: dict, task: dict) -> bool:
    """Independent re-derivation of I1's readiness predicate (queued, all deps done,
    every item of each dependency's checklist ticked) straight from a freshly-read
    state dict -- never by calling chantier.ready() or trusting the CLI's own verdict,
    so the CLI's answer has something genuinely independent to be checked against.
    """
    if task["state"] != "queued":
        return False
    for dep_id in task["dependsOn"]:
        dep = state["taches"][dep_id]
        if dep["state"] != "done":
            return False
        if not all(item["done"] for item in dep["checklist"]):
            return False
    return True


# ---------------------------------------------------------------------------
# Capteur fixture -- deterministic, no external dependency, respects the exact
# {at, ok, measured, declared, drift, unknown} contract from SPEC.md section 5.
# ---------------------------------------------------------------------------


def write_capteur_script(scripts_dir: Path) -> Path:
    counter_file = scripts_dir / "capteur.count"
    counter_file.write_text("0", encoding="utf-8")
    script_path = scripts_dir / "capteur.py"
    body = (
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import pathlib\n"
        f"counter_path = pathlib.Path({str(counter_file)!r})\n"
        "n = int(counter_path.read_text().strip()) + 1\n"
        "counter_path.write_text(str(n))\n"
        "payload = {\n"
        '    "at": "2026-08-09T10:00:00Z",\n'
        "    \"ok\": True,\n"
        '    "measured": [{"name": "stories", "value": n, "unit": "cycles"}],\n'
        '    "declared": [{"name": "prouve", "value": "2/2", "source": "journal"}],\n'
        '    "drift": [],\n'
        '    "unknown": [],\n'
        "}\n"
        "print(json.dumps(payload))\n"
    )
    script_path.write_text(body, encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


# ---------------------------------------------------------------------------
# Main walkthrough
# ---------------------------------------------------------------------------


def main() -> int:
    real_home_before = real_home_fingerprint()

    home = Path(tempfile.mkdtemp(prefix="ordo-e2e-home-"))
    project = Path(tempfile.mkdtemp(prefix="ordo-e2e-project-"))
    scripts_dir = Path(tempfile.mkdtemp(prefix="ordo-e2e-scripts-"))
    (project / "app" / "src" / "api").mkdir(parents=True)
    (project / "app" / "src" / "other").mkdir(parents=True)

    session = f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"
    env = {**os.environ, "ORDO_HOME": str(home)}
    # lib.panes/lib.store are called directly by this script (never lib.cli, never a
    # modification of ordo/) for the parts bin/ordo's own launch verb cannot do here;
    # they read ORDO_HOME from this process's own environment, so it must be set here
    # too, not just passed to the CLI subprocesses.
    os.environ["ORDO_HOME"] = str(home)

    print(f"ORDO_HOME (temporaire) : {home}")
    print(f"projet (temporaire)    : {project}")
    print(f"session tmux           : {session}")
    print("")

    try:
        # -- A. ouverture d'un chantier ---------------------------------------
        info("A. ouverture d'un chantier")
        objectif = "tests verts sur /stocks, migration terminee"
        perimetre = "le module stocks"
        hors_scope = "pas les reservations, pas la prod"
        start_data = run_cli_json(
            env, str(project),
            ["start", str(project), "--goal", objectif,
             "--scope", perimetre, "--out-of-scope", hors_scope],
        )
        cid = start_data["id"]

        state = read_state(home)
        ch = state["chantiers"].get(cid)
        check(ch is not None, f"{cid} present dans state.json apres 'start'")
        check(ch["state"] == "open", "chantier a l'etat ouvert sur disque")
        check(ch["objectif"] == objectif, "objectif persiste tel quel sur disque")
        check(ch["project"] == store.canon(project), "chemin projet canonise sur disque")

        # -- B. ajout d'un graphe a dependances --------------------------------
        info("B. ajout d'un graphe a dependances")
        t1_data = run_cli_json(
            env, str(project),
            ["add", cid, "--title", "migrer schema", "--prompt", "migrer stocks",
             "--touches", "app/src/api",
             "--check", "tests verts", "--check", "doc a jour"],
        )
        t1 = t1_data["id"]
        t2_data = run_cli_json(
            env, str(project),
            ["add", cid, "--title", "brancher api", "--prompt", "brancher stocks",
             "--depends", t1, "--touches", "app/src/other", "--check", "integration ok"],
        )
        t2 = t2_data["id"]

        state = read_state(home)
        check(t1 in state["taches"], f"{t1} present dans state.json")
        check(t2 in state["taches"], f"{t2} present dans state.json")
        check(
            {i["id"] for i in state["taches"][t1]["checklist"]} == {"c1", "c2"}
            and all(not i["done"] for i in state["taches"][t1]["checklist"]),
            f"{t1} a deux cases de checklist non cochees sur disque",
        )
        check(
            state["taches"][t2]["dependsOn"] == [t1],
            f"{t2} depend de {t1} sur disque",
        )

        # -- C. ready ne rend que ce qui est lancable --------------------------
        info("C. verification que ready ne rend que ce qui est lancable")
        ready_data = run_cli_json(env, str(project), ["ready", cid])
        ready_ids = {t["id"] for t in ready_data}
        state = read_state(home)
        manual_t1_ready = manually_ready(state, state["taches"][t1])
        manual_t2_ready = manually_ready(state, state["taches"][t2])
        check(
            ready_ids == {t1} and manual_t1_ready and not manual_t2_ready,
            "ready() = {%s} seulement, recompute manuel sur l'etat disque d'accord" % t1,
        )

        # -- D. lancement d'une executante simulee + rapport -------------------
        info("D. lancement d'une executante simulee dans un vrai pane")
        pane_t1 = launch_simulated_executante(home, session, str(project), cid, t1)

        listing = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_id} #{pane_dead}"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        check(
            any(line.split(" ")[0] == pane_t1 and line.split(" ")[1] == "0"
                for line in listing.splitlines()),
            f"le pane {pane_t1} existe et est vivant, requete tmux directe",
        )

        payload_t1 = {
            "task": t1, "state": "done", "note": "migration ecrite et testee",
            "checked": ["c1"], "question": None, "touched": ["app/src/api/schema.sql"],
        }
        send_report(home, pane_t1, cid, t1, payload_t1)
        on_disk = wait_for_report(home, cid, t1)
        check(
            on_disk == payload_t1,
            f"reports/{cid}/{t1}.json ecrit par le pane, contenu relu identique au voulu",
        )

        # -- E. reconciliation par tick -----------------------------------------
        info("E. reconciliation par tick")
        run_cli_json(env, str(project), ["tick", "--campaign", cid])
        state = read_state(home)
        check(state["taches"][t1]["state"] == "done", f"{t1} passe a done sur disque apres tick")
        c1 = next(i for i in state["taches"][t1]["checklist"] if i["id"] == "c1")
        c2 = next(i for i in state["taches"][t1]["checklist"] if i["id"] == "c2")
        check(c1["done"] and not c2["done"], "seule c1 est cochee (celle du rapport), c2 non")
        journal_text = read_journal(home, cid)
        check(
            t1 in journal_text and "done" in journal_text,
            f"journal/{cid}.md mentionne {t1} termine, relu sur disque",
        )

        # -- F. cochage de la checklist + deblocage du dependant ---------------
        info("F. cochage manuel de la checklist, deblocage du dependant")
        run_cli_json(env, str(project), ["check", t1, "c2"])
        state = read_state(home)
        c2 = next(i for i in state["taches"][t1]["checklist"] if i["id"] == "c2")
        check(c2["done"], f"c2 cochee sur disque apres 'ordo check {t1} c2'")

        ready_data = run_cli_json(env, str(project), ["ready", cid])
        ready_ids = {t["id"] for t in ready_data}
        state = read_state(home)
        check(
            t2 in ready_ids and manually_ready(state, state["taches"][t2]),
            f"{t2} debloque (I1) une fois {t1} done ET toute sa checklist cochee, "
            "recompute manuel sur l'etat disque d'accord",
        )

        # -- G. installation et adoption d'un capteur --------------------------
        info("G. installation et adoption d'un capteur")
        capteur_script = write_capteur_script(scripts_dir)
        run_cli_json(env, str(project), ["sensor", "install", cid, str(capteur_script)])
        state = read_state(home)
        check(
            state["chantiers"][cid]["capteur"]["path"] is not None
            and not state["chantiers"][cid]["capteur"]["adopted"],
            "capteur installe, chemin present sur disque, pas encore adopte",
        )

        for _ in range(3):
            run_cli_json(env, str(project), ["sensor", "run", cid])
        state = read_state(home)
        check(
            len(state["chantiers"][cid]["capteur"]["runs"]) == 3
            and all(r["ok"] for r in state["chantiers"][cid]["capteur"]["runs"]),
            "trois executions du capteur enregistrees et reussies sur disque",
        )

        run_cli_json(env, str(project), ["sensor", "adopt", cid])
        state = read_state(home)
        cap = state["chantiers"][cid]["capteur"]
        check(
            cap["adopted"] is True and cap["adoptedAt"] is not None,
            "capteur adopte sur disque, adoptedAt renseigne",
        )

        status_data = run_cli_json(env, str(project), ["sensor", "status", cid])
        check(
            status_data["signal"] == "ok"
            and cap["consecutiveFailures"] == 0
            and cap["identicalCycles"] < 3,
            "signal 'ok' coherent avec consecutiveFailures/identicalCycles relus sur disque",
        )

        # -- G'. detection d'une derive de scope --------------------------------
        info("G'. detection d'une derive de scope (lancement de t2 hors zone)")
        pane_t2 = launch_simulated_executante(home, session, str(project), cid, t2)
        derive_path = "vendor/unrelated/leftover.py"
        payload_t2 = {
            "task": t2, "state": "done", "note": "branchement fait",
            "checked": ["c1"], "question": None, "touched": [derive_path],
        }
        send_report(home, pane_t2, cid, t2, payload_t2)
        on_disk_t2 = wait_for_report(home, cid, t2)
        check(
            on_disk_t2 == payload_t2,
            f"reports/{cid}/{t2}.json ecrit par le pane, contenu relu identique au voulu",
        )

        run_cli_json(env, str(project), ["tick", "--campaign", cid])
        state = read_state(home)
        check(state["taches"][t2]["state"] == "done", f"{t2} passe a done sur disque apres tick")
        journal_text = read_journal(home, cid)
        check(
            "scope drift" in journal_text and derive_path in journal_text,
            f"journal/{cid}.md porte la derive de scope pour {derive_path}, relu sur disque",
        )

        # -- H. ecriture du journal, brief regenere -----------------------------
        info("H. ecriture du journal, brief regenere")
        decision_note = "t2 avant priorite haute : depend de t1, pas de contournement du graphe"
        run_cli_json(env, str(project), ["journal", "add", cid, "--author", "ORCH", "--note", decision_note])
        journal_text = read_journal(home, cid)
        elle_lines = [ln for ln in journal_text.splitlines() if ln[7:11].rstrip() == "ORCH"]
        check(
            any(decision_note in ln for ln in elle_lines),
            f"journal/{cid}.md porte la decision ORCH, relu sur disque, auteur au bon champ",
        )

        brief_data = run_cli_json(env, str(project), ["brief", cid])
        brief_text = brief_data["brief"]
        check(
            objectif in brief_text and decision_note in brief_text,
            "le brief regenere reprend l'objectif et la decision ORCH, chacun "
            "independamment relu sur disque avant comparaison",
        )
        check(
            "adopted on" in brief_text,
            "le brief regenere signale l'adoption du capteur",
        )
        # This one caught a real defect on its first run: _section_capteur() read
        # run["measured"], but run() nests the script output under run["output"]. The
        # section printed "(aucune mesure)" forever, even right after three successful
        # adopted runs. Asserting on the VALUE, not just on the adoption line, is what
        # made the difference; keep it that way.
        check(
            "stories=" in brief_text,
            "le brief regenere affiche la mesure elle-meme, pas seulement l'adoption",
        )

        # -- I. fin de chantier ---------------------------------------------------
        info("I. fin de chantier")
        run_cli_json(env, str(project), ["close", cid])
        state = read_state(home)
        check(
            state["chantiers"][cid]["state"] == "closed"
            and state["chantiers"][cid]["closedAt"] is not None,
            "chantier clos sur disque, closedAt renseigne",
        )

        # -- isolation : le vrai ~/.claude/ordo n'a pas bouge --------------------
        real_home_after = real_home_fingerprint()
        check(
            real_home_before == real_home_after,
            "le vrai ~/.claude/ordo est inchange (aucun fichier ajoute/retire) pendant le parcours",
        )

        print("")
        print(f"Verifications sur disque : {DISK_CHECKS}")
        print("PARCOURS COMPLET : TOUS LES CONTROLES SONT PASSES.")
        return 0

    except Exception as exc:  # noqa: BLE001 -- deliberate boundary, see module docstring
        print("")
        print(f"Verifications sur disque avant echec : {DISK_CHECKS}")
        print(f"ECHEC : {exc}")
        return 1

    finally:
        # Only ever kills the ONE session this run created itself, by its unique
        # name, never anything else already on the tmux server.
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(project, ignore_errors=True)
        shutil.rmtree(scripts_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
