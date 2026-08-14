"""Unit tests for ordo/plan.py.

Isolated by ORDO_HOME, like test_store.py and test_chantier.py. The real `claude` binary
is never invoked here: every propose() call injects a fake runner via the `runner`
parameter, including runners that return malformed or cyclic output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import chantier, plan, store


def _t(ref, titre="titre", prompt="prompt", depends_on=(), touches=(),
       checklist=(), parallelisable=False):
    """Build one schema-shaped proposed task, as the model would return it."""
    return {
        "ref": ref,
        "titre": titre,
        "prompt": prompt,
        "dependsOn": list(depends_on),
        "touches": list(touches),
        "checklist": list(checklist),
        "parallelisable": parallelisable,
    }


def _envelope(taches, use_result_fallback=False):
    """Build a --output-format json envelope carrying `taches` as the schema-forced payload.

    use_result_fallback exercises the code path where structured_output is absent and the
    schema-forced content must be decoded from the "result" string instead.
    """
    structured = {"taches": taches}
    if use_result_fallback:
        return json.dumps({"result": json.dumps(structured), "total_cost_usd": 0.01})
    return json.dumps({"structured_output": structured, "total_cost_usd": 0.01})


def _runner(stdout: str, returncode: int = 0, stderr: str = ""):
    """Fake model launcher. Records every call so tests can assert on cmd/pave."""
    calls: list[tuple[list[str], str]] = []

    def run(cmd: list[str], pave: str) -> subprocess.CompletedProcess:
        calls.append((cmd, pave))
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=stderr
        )

    run.calls = calls  # type: ignore[attr-defined]
    return run


class PlanTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-plan-test-")
        self._prev_home = os.environ.get("ORDO_HOME")
        os.environ["ORDO_HOME"] = self._tmp

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("ORDO_HOME", None)
        else:
            os.environ["ORDO_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _projet(self, nom: str = "p") -> Path:
        p = Path(self._tmp) / nom
        p.mkdir(exist_ok=True)
        return p


class TestPropose(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]

    def test_propose_sends_pave_via_stdin_never_in_argv(self):
        # I4 : un pave commencant par un tiret serait lu comme un flag s'il finissait en
        # argument positionnel. Le seul moyen de le prouver est de verifier qu'il n'est
        # nulle part dans cmd et qu'il arrive intact cote stdin.
        runner = _runner(_envelope([_t("n1")]))
        prop = plan.propose(self.chantier_id, "-rm -rf /", runner=runner)
        cmd, pave = runner.calls[0]
        self.assertEqual(pave, "-rm -rf /")
        self.assertNotIn("-rm -rf /", cmd)
        self.assertEqual(prop["state"], "pending")

    def test_propose_builds_expected_command(self):
        runner = _runner(_envelope([_t("n1")]))
        plan.propose(self.chantier_id, "pave", model="opus", runner=runner)
        cmd, _ = runner.calls[0]
        self.assertEqual(
            cmd,
            ["claude", "-p", "--output-format", "json",
             "--json-schema", plan.PLAN_SCHEMA, "--model", "opus"],
        )

    def test_propose_defaults_to_sonnet(self):
        runner = _runner(_envelope([_t("n1")]))
        plan.propose(self.chantier_id, "pave", runner=runner)
        cmd, _ = runner.calls[0]
        self.assertEqual(cmd[-1], "sonnet")

    def test_propose_unknown_chantier_raises_before_calling_runner(self):
        runner = _runner(_envelope([_t("n1")]))
        with self.assertRaises(plan.PlanError):
            plan.propose("c-99", "pave", runner=runner)
        self.assertEqual(runner.calls, [])

    def test_propose_persists_proposition_en_attente_with_future_deadline(self):
        runner = _runner(_envelope(
            [_t("n1", titre="t", prompt="p", touches=["z"], checklist=["c1"])]
        ))
        prop = plan.propose(self.chantier_id, "pave", runner=runner)
        self.assertEqual(prop["chantier"], self.chantier_id)
        self.assertEqual(prop["state"], "pending")
        self.assertIsNone(prop["decidedAt"])
        self.assertIsNone(prop["refus"])
        self.assertEqual(len(prop["taches"]), 1)
        self.assertGreater(prop["deadline"], prop["proposedAt"])
        reloaded = store.load()
        self.assertIn(prop["id"], reloaded["propositions"])

    def test_propose_deadline_matches_auto_accept_after(self):
        with mock.patch.object(plan, "AUTO_ACCEPT_AFTER", 0.0):
            runner = _runner(_envelope([_t("n1")]))
            prop = plan.propose(self.chantier_id, "pave", runner=runner)
        # delai nul : la proposition est deja due au moment ou elle est deposee.
        self.assertGreaterEqual(store.now(), prop["deadline"])

    def test_propose_nonzero_returncode_raises_and_stores_nothing(self):
        runner = _runner("", returncode=1, stderr="boom")
        with self.assertRaises(plan.PlanError):
            plan.propose(self.chantier_id, "pave", runner=runner)
        self.assertEqual(store.load()["propositions"], {})

    def test_propose_malformed_json_raises_and_stores_nothing(self):
        runner = _runner("ceci n'est pas du json")
        with self.assertRaises(plan.PlanError):
            plan.propose(self.chantier_id, "pave", runner=runner)
        self.assertEqual(store.load()["propositions"], {})

    def test_propose_missing_taches_field_raises(self):
        runner = _runner(json.dumps({"structured_output": {}}))
        with self.assertRaises(plan.PlanError):
            plan.propose(self.chantier_id, "pave", runner=runner)
        self.assertEqual(store.load()["propositions"], {})

    def test_propose_empty_taches_raises(self):
        runner = _runner(_envelope([]))
        with self.assertRaises(plan.PlanError):
            plan.propose(self.chantier_id, "pave", runner=runner)

    def test_propose_task_missing_required_field_raises(self):
        bad = _t("n1")
        del bad["parallelisable"]
        runner = _runner(json.dumps({"structured_output": {"taches": [bad]}}))
        with self.assertRaises(plan.PlanError):
            plan.propose(self.chantier_id, "pave", runner=runner)

    def test_propose_dangling_dependency_raises_and_stores_nothing(self):
        runner = _runner(_envelope([_t("n1", depends_on=["n-fantome"])]))
        with self.assertRaises(plan.PlanError):
            plan.propose(self.chantier_id, "pave", runner=runner)
        self.assertEqual(store.load()["propositions"], {})

    def test_propose_duplicate_refs_raises(self):
        runner = _runner(_envelope([_t("n1"), _t("n1")]))
        with self.assertRaises(plan.PlanError):
            plan.propose(self.chantier_id, "pave", runner=runner)

    def test_propose_accepts_result_string_fallback(self):
        runner = _runner(_envelope([_t("n1")], use_result_fallback=True))
        prop = plan.propose(self.chantier_id, "pave", runner=runner)
        self.assertEqual(prop["state"], "pending")


class TestAccept(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]

    def _propose(self, taches):
        runner = _runner(_envelope(taches))
        return plan.propose(self.chantier_id, "pave", runner=runner)

    def test_accept_materializes_tasks_with_explicit_dependency(self):
        prop = self._propose([
            _t("n1", titre="premiere", prompt="fait ceci",
               touches=["zone-a"], checklist=["ok"]),
            _t("n2", titre="seconde", prompt="fait cela",
               depends_on=["n1"], touches=["zone-b"]),
        ])
        result = plan.accept(prop["id"])
        self.assertEqual(result["state"], "accepted")
        self.assertIsNotNone(result["decidedAt"])

        state = store.load()
        tasks = [t for t in state["taches"].values() if t["chantier"] == self.chantier_id]
        self.assertEqual(len(tasks), 2)
        by_title = {t["titre"]: t for t in tasks}
        first, second = by_title["premiere"], by_title["seconde"]
        self.assertEqual(first["dependsOn"], [])
        self.assertEqual(second["dependsOn"], [first["id"]])
        self.assertEqual(first["checklist"], [{"id": "c1", "label": "ok", "done": False}])
        self.assertEqual(first["state"], "queued")
        self.assertEqual(first["touches"], ["zone-a"])

    def test_accept_translates_zone_conflict_into_real_dependency(self):
        # Le point le plus important du module : deux taches que le modele declare
        # parallelisables mais qui touchent la MEME zone doivent recevoir une vraie
        # dependance, sinon la separation de zone est perdue des qu'un ordonnanceur ne
        # regarde plus que dependsOn.
        prop = self._propose([
            _t("n1", titre="ecrit-schema", touches=["db-test"], parallelisable=True),
            _t("n2", titre="ecrit-migration", touches=["db-test"], parallelisable=True),
        ])
        plan.accept(prop["id"])
        tasks = {t["titre"]: t for t in store.load()["taches"].values()}
        first, second = tasks["ecrit-schema"], tasks["ecrit-migration"]
        self.assertEqual(second["dependsOn"], [first["id"]])

    def test_accept_unrelated_zones_stay_independent(self):
        prop = self._propose([
            _t("n1", titre="a", touches=["zone-a"], parallelisable=True),
            _t("n2", titre="b", touches=["zone-b"], parallelisable=True),
        ])
        plan.accept(prop["id"])
        tasks = {t["titre"]: t for t in store.load()["taches"].values()}
        self.assertEqual(tasks["a"]["dependsOn"], [])
        self.assertEqual(tasks["b"]["dependsOn"], [])

    def test_i7_cycle_abandonne_la_proposition(self):
        prop = self._propose([
            _t("n1", titre="a", depends_on=["n2"], touches=["zone-a"]),
            _t("n2", titre="b", depends_on=["n1"], touches=["zone-b"]),
        ])
        with self.assertRaises(plan.PlanError) as ctx:
            plan.accept(prop["id"])
        self.assertIn("cycle", str(ctx.exception).lower())

        state = store.load()
        self.assertEqual(state["propositions"][prop["id"]]["state"], "rejected")
        self.assertTrue(state["propositions"][prop["id"]]["refus"])
        # Abandon total (I8) : aucune tache n'a ete materialisee, meme partiellement.
        # Un graphe a moitie accepte aurait des dependances orphelines.
        chantier_tasks = [
            t for t in state["taches"].values() if t["chantier"] == self.chantier_id
        ]
        self.assertEqual(chantier_tasks, [])

    def test_i8_accept_deja_decidee_refuse_sans_dupliquer(self):
        prop = self._propose([_t("n1", titre="a")])
        plan.accept(prop["id"])
        count_before = len(store.load()["taches"])
        with self.assertRaises(plan.PlanError) as ctx:
            plan.accept(prop["id"])
        self.assertIn(prop["id"], str(ctx.exception))
        self.assertEqual(len(store.load()["taches"]), count_before)

    def test_accept_unknown_proposition_raises(self):
        with self.assertRaises(plan.PlanError):
            plan.accept("p-99")


class TestRefuse(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]
        runner = _runner(_envelope([_t("n1", titre="a")]))
        self.prop = plan.propose(self.chantier_id, "pave", runner=runner)

    def test_refuse_marks_state_and_reason(self):
        result = plan.refuse(self.prop["id"], "hors scope")
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(result["refus"], "hors scope")
        self.assertIsNotNone(result["decidedAt"])
        self.assertEqual(store.load()["taches"], {})

    def test_refuse_already_accepted_raises_and_does_not_overwrite(self):
        plan.accept(self.prop["id"])
        with self.assertRaises(plan.PlanError):
            plan.refuse(self.prop["id"], "trop tard")
        self.assertEqual(
            store.load()["propositions"][self.prop["id"]]["state"], "accepted"
        )

    def test_refuse_unknown_proposition_raises(self):
        with self.assertRaises(plan.PlanError):
            plan.refuse("p-99", "raison")


class TestExpireDue(PlanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]

    def test_expire_due_auto_accepts_after_delay(self):
        with mock.patch.object(plan, "AUTO_ACCEPT_AFTER", 0.0):
            runner = _runner(_envelope([_t("n1", titre="a")]))
            prop = plan.propose(self.chantier_id, "pave", runner=runner)
        with store.locked() as state:
            due = plan.expire_due(state)
        self.assertEqual(due, [prop["id"]])
        reloaded = store.load()
        self.assertEqual(reloaded["propositions"][prop["id"]]["state"], "accepted")
        self.assertEqual(len(reloaded["taches"]), 1)

    def test_expire_due_ignores_not_yet_due(self):
        runner = _runner(_envelope([_t("n1", titre="a")]))
        prop = plan.propose(self.chantier_id, "pave", runner=runner)  # deadline a 45 s
        with store.locked() as state:
            due = plan.expire_due(state)
        self.assertEqual(due, [])
        self.assertEqual(
            store.load()["propositions"][prop["id"]]["state"], "pending"
        )

    def test_expire_due_refuses_cyclic_proposal_and_continues_for_others(self):
        with mock.patch.object(plan, "AUTO_ACCEPT_AFTER", 0.0):
            runner_bad = _runner(_envelope([
                _t("n1", titre="a", depends_on=["n2"]),
                _t("n2", titre="b", depends_on=["n1"]),
            ]))
            bad = plan.propose(self.chantier_id, "pave", runner=runner_bad)
            runner_ok = _runner(_envelope([_t("n1", titre="ok")]))
            ok = plan.propose(self.chantier_id, "pave", runner=runner_ok)

        with store.locked() as state:
            due = plan.expire_due(state)

        self.assertEqual(set(due), {bad["id"], ok["id"]})
        reloaded = store.load()
        self.assertEqual(reloaded["propositions"][bad["id"]]["state"], "rejected")
        self.assertEqual(reloaded["propositions"][ok["id"]]["state"], "accepted")


class TestWaves(unittest.TestCase):
    def test_disjoint_parallelisable_tasks_share_a_wave(self):
        tasks = [
            _t("n1", touches=["zone-a"], parallelisable=True),
            _t("n2", touches=["zone-b"], parallelisable=True),
        ]
        waves = plan.waves(tasks)
        self.assertIn({"n1", "n2"}, [set(w) for w in waves])

    def test_same_zone_keeps_parallelisable_tasks_apart(self):
        # Le modele est systematiquement optimiste sur le parallelisme : deux taches
        # qu'il declare parallelisables mais qui touchent la meme zone ne doivent JAMAIS
        # partager une vague.
        tasks = [
            _t("n1", touches=["app/src"], parallelisable=True),
            _t("n2", touches=["app/src"], parallelisable=True),
        ]
        waves = plan.waves(tasks)
        for wave in waves:
            self.assertLessEqual(len(wave), 1)

    def test_non_parallelisable_task_never_shares_a_wave(self):
        tasks = [
            _t("n1", touches=["zone-a"], parallelisable=True),
            _t("n2", touches=["zone-b"], parallelisable=False),
        ]
        waves = plan.waves(tasks)
        for wave in waves:
            if "n2" in wave:
                self.assertEqual(wave, ["n2"])

    def test_dependency_forces_a_later_wave(self):
        tasks = [
            _t("n1", touches=["zone-a"]),
            _t("n2", touches=["zone-b"], depends_on=["n1"]),
        ]
        waves = plan.waves(tasks)
        index = {ref: i for i, w in enumerate(waves) for ref in w}
        self.assertLess(index["n1"], index["n2"])

    def test_zone_is_a_free_string_not_a_path(self):
        # db-test et staging sont des zones valides : une zone n'est jamais supposee
        # etre un chemin de fichier.
        tasks = [
            _t("n1", touches=["db-test"], parallelisable=True),
            _t("n2", touches=["staging"], parallelisable=True),
        ]
        waves = plan.waves(tasks)
        self.assertIn({"n1", "n2"}, [set(w) for w in waves])

    def test_different_project_keeps_tasks_apart(self):
        n1 = _t("n1", touches=["zone-a"], parallelisable=True)
        n1["project"] = "/repo/a"
        n2 = _t("n2", touches=["zone-b"], parallelisable=True)
        n2["project"] = "/repo/b"
        waves = plan.waves([n1, n2])
        for wave in waves:
            self.assertLessEqual(len(wave), 1)

    def test_raises_on_cycle(self):
        tasks = [
            _t("n1", depends_on=["n2"]),
            _t("n2", depends_on=["n1"]),
        ]
        with self.assertRaises(plan.PlanError):
            plan.waves(tasks)


if __name__ == "__main__":
    unittest.main()
