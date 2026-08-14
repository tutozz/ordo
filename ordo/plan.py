"""Turn a dictated brain dump into a proposed task graph, review it, materialise it.

propose() calls the `claude` CLI once per pave, with the pave passed on stdin (I4): a
pave beginning with a dash would otherwise be read as a flag, and the CLI's variadic
flags swallow everything that follows a positional argument. The call is injectable so
tests never touch the real binary: propose() always calls `runner(cmd, pave)` and expects
back an object exposing .returncode, .stdout, .stderr, exactly like
subprocess.CompletedProcess (the real runner returns one).

accept() carries the module's real weight: it turns a zone conflict between two proposed
tasks into a genuine dependsOn edge, because a scheduler that only reads dependsOn is
blind to a `touches` overlap the model failed to serialise as a dependency. accept() and
the auto-accept sweep in expire_due() share one internal _materialize(), so the
translation rule and the cycle guard (I7) exist in exactly one place.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable

from . import chantier, store

# Hard timeout on the model round trip. 300 s is what the predecessor project ran in
# production without truncating a legitimate slow answer; a shorter value risks killing a
# real response rather than a stuck one.
MODEL_TIMEOUT = 300

# Delay before an "pending" proposal auto-accepts (see expire_due). Named and
# patchable via mock.patch.object so tests never sleep 45 real seconds to reach the
# auto-accept path. No exception exists here for an irreversible task: the barrier
# against acting on it without asking lives in the executante's brief, not in this delay.
AUTO_ACCEPT_AFTER = 45.0

# Forced on the model. Kept close to the persisted task shape so accept() can build a
# real task straight from one entry, but "parallelisable" never survives into a
# persisted task: it is exactly what waves() consumes before acceptance, not a fact
# about a queued task, so it has no home in the fixed state.json schema.
PLAN_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "taches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "local identifier within this proposal, e.g. n1, n2",
                    },
                    "titre": {"type": "string"},
                    "prompt": {
                        "type": "string",
                        "description": "the exact text to send to the executor session",
                    },
                    "dependsOn": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "local refs, within this proposal, this task depends on",
                    },
                    "touches": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "declared zones; free strings, not necessarily paths",
                    },
                    "checklist": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "post-conditions to tick once the task is done",
                    },
                    "parallelisable": {
                        "type": "boolean",
                        "description": "true if this task can run at the same time as another in its wave",
                    },
                },
                "required": [
                    "ref", "titre", "prompt", "dependsOn", "touches",
                    "checklist", "parallelisable",
                ],
            },
        },
    },
    "required": ["taches"],
})

_REQUIRED_TASK_FIELDS = (
    "ref", "titre", "prompt", "dependsOn", "touches", "checklist", "parallelisable",
)


class PlanError(Exception):
    """A planning operation was refused (I8).

    Every raise carries the id and the reason in its message: a silent refusal is worse
    than a failure, because it looks like nothing happened when in fact nothing did.
    """


def _default_runner(cmd: list[str], pave: str) -> subprocess.CompletedProcess:
    """The real launcher. Tests inject their own `runner` and never reach this one."""
    return subprocess.run(
        cmd, input=pave, capture_output=True, text=True, timeout=MODEL_TIMEOUT
    )


def _in_seconds(seconds: float) -> str:
    """ISO8601 UTC timestamp `seconds` from now, in the same format as store.now().

    Computed from a single time.time() call rather than parsing store.now()'s own string
    back into an epoch: two independent calls would let deadline drift a few
    microseconds away from proposedAt + AUTO_ACCEPT_AFTER for no reason.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


def _validate_taches(taches) -> None:
    """Catch what the JSON schema cannot: referential integrity between refs.

    --json-schema forces field TYPES, not that a dependsOn entry names a ref actually
    declared in the same proposition. A dangling reference would otherwise pass here,
    get persisted as if the proposal were sound, and only blow up later inside
    _materialize() as an opaque KeyError, far from the real cause.
    """
    if not isinstance(taches, list) or not taches:
        raise PlanError("planner response: 'taches' empty or missing")
    refs: list[str] = []
    for tache in taches:
        if not isinstance(tache, dict):
            raise PlanError("planner response: a task is not an object")
        for field in _REQUIRED_TASK_FIELDS:
            if field not in tache:
                raise PlanError(
                    f"planner response: field '{field}' missing on a task"
                )
        if not isinstance(tache["ref"], str) or not tache["ref"]:
            raise PlanError("planner response: a task has an invalid ref")
        refs.append(tache["ref"])
    if len(set(refs)) != len(refs):
        raise PlanError("planner response: duplicate references in the proposal")
    known = set(refs)
    for tache in taches:
        for dep in tache["dependsOn"]:
            if dep not in known:
                raise PlanError(
                    f"planner response: {tache['ref']} depends on '{dep}', "
                    "which does not exist in this proposal"
                )


def _parse_model_output(raw: str) -> list[dict]:
    """Decode the --output-format json envelope, then the schema-forced payload inside it.

    Never returns an empty or half-understood plan: any envelope, encoding, or
    referential problem raises with the reason instead, so a broken model call can never
    be mistaken for a valid, empty proposal.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanError(f"planner output unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlanError("planner output unreadable: the envelope is not an object")

    structured = payload.get("structured_output")
    if structured is None:
        # Checked with "is None", not truthiness: an intentionally empty
        # structured_output ({}) is a legitimate (if malformed downstream) answer, not
        # an absent one, and must not be silently swapped for the "result" fallback.
        raw_result = payload.get("result")
        if raw_result is None:
            raise PlanError(
                "planner output unreadable: neither 'structured_output' nor 'result'"
            )
        try:
            structured = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        except json.JSONDecodeError as exc:
            raise PlanError(f"planner output unreadable: {exc}") from exc

    if not isinstance(structured, dict) or "taches" not in structured:
        raise PlanError("planner response: field 'taches' missing")
    taches = structured["taches"]
    _validate_taches(taches)
    return taches


def propose(
    chantier_id: str,
    pave: str,
    model: str = "sonnet",
    runner: Callable[[list[str], str], subprocess.CompletedProcess] | None = None,
) -> dict:
    """Send `pave` to the model, validate its answer, store an "pending" proposal.

    The model call happens OUTSIDE any store lock: it can take up to MODEL_TIMEOUT
    seconds, and holding state.json.lock for that long would stall every other Ordo
    command for the whole wall-clock duration of one CLI round trip.
    """
    run = runner or _default_runner
    if chantier_id not in store.load()["chantiers"]:
        raise PlanError(f"campaign not found: {chantier_id}")

    cmd = ["claude", "-p", "--output-format", "json", "--json-schema", PLAN_SCHEMA,
           "--model", model]
    completed = run(cmd, pave)
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[:300]
        raise PlanError(f"the planner failed: {detail or 'empty output'}")

    taches = _parse_model_output(completed.stdout)

    with store.locked() as state:
        # Reverifie sous verrou : le chantier a pu etre ferme pendant l'appel modele,
        # qui a dure jusqu'a MODEL_TIMEOUT secondes hors de tout verrou.
        if chantier_id not in state["chantiers"]:
            raise PlanError(f"campaign not found: {chantier_id}")
        prop_id = store.next_id(state, "proposition")
        proposed_at = store.now()
        proposition = {
            "id": prop_id,
            "chantier": chantier_id,
            "taches": taches,
            "state": "pending",
            "proposedAt": proposed_at,
            "deadline": _in_seconds(AUTO_ACCEPT_AFTER),
            "decidedAt": None,
            "refus": None,
        }
        state["propositions"][prop_id] = proposition
    return proposition


def _topo_order(graph: dict[str, dict]) -> list[str]:
    """Dependency-first order over an ACYCLIC graph (has_cycle() must run before this).

    Required because materialised tasks are created one store.next_id() at a time and a
    dependsOn entry must name a task id that already exists: creating a dependant before
    its dependency would either crash resolving the id or silently point at nothing.
    """
    remaining = {ref: list(g["dependsOn"]) for ref, g in graph.items()}
    refs = list(graph.keys())
    order: list[str] = []
    while remaining:
        ready = [r for r in refs if r in remaining and not remaining[r]]
        if not ready:
            # Unreachable once has_cycle() has run clean beforehand. Kept as a hard
            # failure rather than an infinite loop if that invariant is ever broken by a
            # future edit to the call order.
            raise PlanError("topological order impossible, cycle not detected upstream")
        for ref in ready:
            order.append(ref)
            del remaining[ref]
        for deps in remaining.values():
            for ref in ready:
                if ref in deps:
                    deps.remove(ref)
    return order


def _materialize(state: dict, prop_id: str) -> dict:
    """Shared core of accept() and expire_due(): zone-conflict translation, cycle guard
    (I7), then either full materialisation or refusal. Mutates `state` in place and
    returns the proposition's own dict; the caller persists `state`.

    Raises ONLY when nothing has been mutated yet (proposition missing or already
    decided): raising after a mutation would lose it, because store.locked() calls
    save() solely on a normal exit of its `with` block (see
    test_locked_does_not_save_on_exception in test_store.py). The cycle case is
    therefore never raised from here: it mutates the proposition to "rejected" and
    returns normally, so a caller wrapping this call in store.locked() persists the
    refusal to disk before deciding, outside the lock, whether to also raise.
    """
    prop = state["propositions"].get(prop_id)
    if prop is None:
        raise PlanError(f"proposal not found: {prop_id}")
    if prop["state"] != "pending":
        raise PlanError(f"materialisation refused: {prop_id} is already {prop['state']}")

    refs = [t["ref"] for t in prop["taches"]]
    graph = {t["ref"]: {"dependsOn": list(t["dependsOn"])} for t in prop["taches"]}
    zones = {t["ref"]: set(t.get("touches") or []) for t in prop["taches"]}

    # The rule that carries the module: a shared zone becomes a real dependsOn edge, in
    # declaration order. Without this, two tasks the model marked parallelisable but
    # that write the same zone would still run concurrently the moment a scheduler looks
    # only at dependsOn, which is exactly the optimism this function exists to correct.
    for i, ref_a in enumerate(refs):
        for ref_b in refs[i + 1:]:
            if zones[ref_a] & zones[ref_b] and ref_a not in graph[ref_b]["dependsOn"]:
                graph[ref_b]["dependsOn"].append(ref_a)

    cycle = chantier.has_cycle(graph)
    if cycle:
        # I7 : a cycle abandons the whole proposal rather than being flattened, which
        # would produce a plan that looks valid and runs in the wrong order.
        prop["state"] = "rejected"
        prop["refus"] = (
            "cycle detected after adding zone dependencies: " + " -> ".join(cycle)
        )
        prop["decidedAt"] = store.now()
        return prop

    by_ref = {t["ref"]: t for t in prop["taches"]}
    ref_to_id: dict[str, str] = {}
    for ref in _topo_order(graph):
        tache = by_ref[ref]
        task_id = store.next_id(state, "tache")
        ref_to_id[ref] = task_id
        # Shape mirrors chantier.add_task() exactly (see state.json schema, SPEC.md
        # section 3). add_task() itself is not called here: it acquires store.locked()
        # internally, and this function already runs inside the caller's own lock;
        # nesting the two would try to re-acquire the same state.json.lock file and
        # time out after LOCK_WAIT_TIMEOUT, since the lock is not reentrant.
        state["taches"][task_id] = {
            "id": task_id,
            "chantier": prop["chantier"],
            "titre": tache["titre"],
            "prompt": tache["prompt"],
            "state": "queued",
            "dependsOn": [ref_to_id[d] for d in graph[ref]["dependsOn"]],
            "touches": list(tache.get("touches") or []),
            "checklist": [
                {"id": f"c{i}", "label": label, "done": False}
                for i, label in enumerate(tache.get("checklist") or [], start=1)
            ],
            "priority": 0,
            "attempts": 0,
            "model": None,
            "paneId": None,
            "cwd": None,
            "createdAt": store.now(),
            "startedAt": None,
            "finishedAt": None,
            "lastReportAt": None,
            "report": None,
            "error": None,
            "notes": [],
        }

    prop["state"] = "accepted"
    prop["decidedAt"] = store.now()
    return prop


def accept(prop_id: str) -> dict:
    """Materialise a proposal into real tasks, all at once.

    I8 : no partial acceptance. A graph accepted halfway would leave dependsOn edges
    pointing at tasks that were never created. Refuses on a cycle (I7); the refusal is
    persisted to the proposition before being raised, so the reason survives on disk
    too, not just in the exception the immediate caller sees.
    """
    with store.locked() as state:
        result = _materialize(state, prop_id)
    if result["state"] == "rejected":
        raise PlanError(f"proposal {prop_id} refused: {result['refus']}")
    return result


def refuse(prop_id: str, raison: str) -> dict:
    """Explicitly refuse a proposal still "pending".

    Refuses the refusal itself (I8) on a proposition already decided: silently
    overwriting an "accepted" proposition to "rejected" would make its already-created
    tasks look like they were never queued, when they still are.
    """
    with store.locked() as state:
        prop = state["propositions"].get(prop_id)
        if prop is None:
            raise PlanError(f"proposal not found: {prop_id}")
        if prop["state"] != "pending":
            raise PlanError(f"refusal refused: {prop_id} is already {prop['state']}")
        prop["state"] = "rejected"
        prop["refus"] = raison
        prop["decidedAt"] = store.now()
    return prop


def expire_due(state: dict) -> list[str]:
    """Auto-accept every "pending" proposal whose deadline has passed.

    Takes an already-loaded state and mutates it in place, exactly like
    chantier.propagate_failures(): the caller persists it, typically inside its own
    store.locked(). One broken proposal (a cycle, turned into "rejected" by
    _materialize()) never stops the sweep for the others in the same call.
    """
    now = store.now()
    due: list[str] = []
    for prop in list(state["propositions"].values()):
        if prop["state"] != "pending" or now < prop["deadline"]:
            continue
        _materialize(state, prop["id"])
        due.append(prop["id"])
    return due


def waves(tasks: list[dict]) -> list[list[str]]:
    """Group proposal refs into execution waves; same wave = safe to launch together.

    Operates on proposal-shaped dicts (the "ref" / "parallelisable" vocabulary a
    proposition carries before acceptance), not on materialised tasks: "parallelisable"
    never survives into a persisted task (see PLAN_SCHEMA's comment), because it is
    exactly what this function consumes to decide the waves, not a fact about a queued
    task.

    Two refs share a wave only if BOTH declare parallelisable, their touched zones are
    disjoint, and they target the same project (absent "project" on every entry, all
    default to the same None value: true within a single chantier-scoped proposal,
    which is the only shape propose() ever produces). The model is systematically
    optimistic about parallelism; zone recoupment is what catches it.
    """
    remaining = {t["ref"]: set(t.get("dependsOn") or []) for t in tasks}
    safe = {t["ref"]: bool(t.get("parallelisable")) for t in tasks}
    zones = {t["ref"]: set(t.get("touches") or []) for t in tasks}
    project = {t["ref"]: t.get("project") for t in tasks}
    result: list[list[str]] = []

    while remaining:
        ready = [ref for ref, deps in remaining.items() if not (deps & remaining.keys())]
        if not ready:
            raise PlanError("dependency cycle detected while scheduling waves")

        wave: list[str] = []
        claimed: set[str] = set()
        for ref in sorted(r for r in ready if safe.get(r)):
            conflict = zones[ref] & claimed
            same_project = not wave or project[ref] == project[wave[0]]
            if conflict or not same_project:
                continue
            wave.append(ref)
            claimed |= zones[ref]
        if wave:
            result.append(wave)

        for ref in sorted(set(ready) - set(wave)):
            result.append([ref])
        for ref in ready:
            remaining.pop(ref, None)
    return result
