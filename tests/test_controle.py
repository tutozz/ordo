"""Tests unitaires de ordo/controle.py.

Isoles par ORDO_HOME, comme test_store.py et test_chantier.py. Les tests qui touchent des
panes utilisent de vrais panes tmux (bash --noprofile --norc, jamais claude), nommes avec
un prefixe propre a ce fichier, et detruits un par un dans tearDown, comme test_panes.py.
Aucun test ne touche le vrai ~/.claude/ordo, ni un vrai process claude, ni ne lance
"tmux kill-server".
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import capteur, chantier, controle, journal, panes, plan, report, store

SESSION_PREFIX = "ordo-test-controle-"
TEST_SHELL = "bash --noprofile --norc"


def _unique_session() -> str:
    return f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"


def _kill_session_quietly(session: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


def _wait_for(predicate, timeout=3.0, interval=0.1):
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


def tearDownModule():
    """Filet de securite : nettoie toute session ordo-test-controle- oubliee par un test
    qui aurait plante avant son tearDown. Ne touche jamais rien d'autre (jamais
    kill-server), exactement comme test_panes.py."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True
    )
    for name in result.stdout.splitlines():
        if name.startswith(SESSION_PREFIX):
            _kill_session_quietly(name)


def _write_script(path: Path, shebang: str, body: str) -> Path:
    path.write_text(f"{shebang}\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class ControleTestCase(unittest.TestCase):
    """Isolation ORDO_HOME. Sessions tmux crees par un test sont suivies dans
    self._sessions et detruites une par une dans tearDown, jamais via kill-server."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-controle-test-")
        self._prev_home = os.environ.get("ORDO_HOME")
        os.environ["ORDO_HOME"] = self._tmp
        self._scripts = Path(tempfile.mkdtemp(prefix="ordo-controle-scripts-"))
        self._sessions: list[str] = []

    def tearDown(self) -> None:
        for session in self._sessions:
            _kill_session_quietly(session)
        if self._prev_home is None:
            os.environ.pop("ORDO_HOME", None)
        else:
            os.environ["ORDO_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._scripts, ignore_errors=True)

    # -- fabriques --------------------------------------------------------

    def _projet(self, nom: str = "p") -> Path:
        p = Path(self._tmp) / nom
        p.mkdir(exist_ok=True)
        return p

    def _chantier(self, **kw) -> str:
        nom = kw.pop("nom", "p")
        defaults = dict(
            objectif="objectif clair", perimetre="dedans", hors_scope="dehors",
            # Point D (contrat, cote chantier.py) : un ORDO_HOME n'accepte plus qu'un
            # seul projet OUVERT a la fois, sauf home_partage=True. Plusieurs tests de
            # ce fichier ouvrent volontairement des chantiers sur des projets distincts
            # (nom="a"/"b") dans le meme ORDO_HOME temporaire pour verifier l'isolation
            # entre chantiers ; ce n'est pas ce que le point D existe pour interdire.
            home_partage=True,
        )
        defaults.update(kw)
        return chantier.start(str(self._projet(nom)), **defaults)["id"]

    def _task(self, chantier_id: str, **kw) -> str:
        defaults = dict(titre="titre", prompt="prompt")
        defaults.update(kw)
        return chantier.add_task(chantier_id, **defaults)["id"]

    def _set_task(self, task_id: str, **fields) -> dict:
        with store.locked() as state:
            task = state["taches"][task_id]
            task.update(fields)
        return store.load()["taches"][task_id]

    def _make_running(self, task_id: str, pane_id: str | None = None, started_ago: float = 0.0) -> None:
        started = store.now()
        if started_ago:
            started = _iso_ago(started_ago)
        self._set_task(task_id, state="running", startedAt=started, paneId=pane_id)

    def _write_report(self, task_id: str, payload: dict) -> None:
        chantier_id = store.load()["taches"][task_id]["chantier"]
        report.path(task_id, chantier_id).write_text(json.dumps(payload), encoding="utf-8")

    def _real_pane(self) -> tuple[str, str]:
        """Cree une vraie session tmux a un seul pane bash, suivie pour destruction."""
        session = _unique_session()
        self._sessions.append(session)
        panes.ensure_session(session)
        pane_id = panes.spawn(session, os.path.expanduser("~"), TEST_SHELL)
        panes.relayout(session)
        _wait_for(lambda: panes.alive(pane_id), timeout=2.0)
        return session, pane_id

    def _python_sensor(self, name: str, measured: list, declared: list, ok: bool = True) -> Path:
        body = (
            "import json\n"
            "payload = {\n"
            '  "at": "2026-08-09T10:00:00Z", "ok": ' + ("True" if ok else "False") + ",\n"
            f"  \"measured\": {measured!r},\n"
            f"  \"declared\": {declared!r},\n"
            '  "drift": [], "unknown": [],\n'
            "}\n"
            "print(json.dumps(payload))\n"
        )
        return _write_script(self._scripts / name, "#!/usr/bin/env python3", body)

    def _python_failing(self, name: str = "fail.py") -> Path:
        body = "import sys\nsys.exit(1)\n"
        return _write_script(self._scripts / name, "#!/usr/bin/env python3", body)

    def _adopted_capteur(self, chantier_id: str, measured: list, declared: list) -> None:
        src = self._python_sensor("sensor.py", measured, declared)
        capteur.install(chantier_id, src)
        for _ in range(capteur.ADOPTION_RUNS_REQUIRED):
            capteur.run(chantier_id)
        capteur.adopt(chantier_id)


def _iso_ago(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


# ===========================================================================
# scope_drift
# ===========================================================================


class TestScopeDrift(ControleTestCase):
    def test_unknown_chantier_raises(self):
        with self.assertRaises(controle.ControleError):
            controle.scope_drift("c-99")

    def test_no_touches_declared_means_no_drift(self):
        # Une tache sans "touches" declare ne peut pas deriver : on ne peut pas deriver
        # d'un perimetre qu'on n'a pas declare.
        cid = self._chantier()
        tid = self._task(cid, touches=[])
        self._write_report(tid, {"task": tid, "state": "done", "note": "ok",
                                  "touched": ["app/anywhere.py"]})
        self._set_task(tid, report={"state": "done", "touched": ["app/anywhere.py"]})
        self.assertEqual(controle.scope_drift(cid), [])

    def test_touched_inside_declared_zone_is_not_drift(self):
        cid = self._chantier()
        tid = self._task(cid, touches=["app/src/api"])
        self._set_task(tid, report={"state": "done", "touched": ["app/src/api/routes.py"]})
        self.assertEqual(controle.scope_drift(cid), [])

    def test_touched_outside_declared_zone_is_drift(self):
        cid = self._chantier()
        tid = self._task(cid, touches=["app/src/api"])
        self._set_task(tid, report={"state": "done", "touched": ["infra/dns.tf"]})
        drift = controle.scope_drift(cid)
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["task"], tid)
        self.assertEqual(drift[0]["touched"], "infra/dns.tf")

    def test_opaque_zone_name_matches_by_substring(self):
        # Piege explicite de la spec : une zone est une chaine libre, pas forcement un
        # chemin ("db-test", "staging" sont des zones valides).
        cid = self._chantier()
        tid = self._task(cid, touches=["db-test"])
        self._set_task(tid, report={"state": "done", "touched": ["db-test"]})
        self.assertEqual(controle.scope_drift(cid), [])

    def test_task_without_report_produces_no_drift(self):
        cid = self._chantier()
        self._task(cid, touches=["app"])
        self.assertEqual(controle.scope_drift(cid), [])

    def test_multiple_touched_files_report_one_drift_entry_each(self):
        cid = self._chantier()
        tid = self._task(cid, touches=["app/src"])
        self._set_task(
            tid,
            report={"state": "done", "touched": ["app/src/x.py", "infra/y.tf", "app/src/z.py"]},
        )
        drift = controle.scope_drift(cid)
        self.assertEqual([d["touched"] for d in drift], ["infra/y.tf"])


# ===========================================================================
# fausse_completion
# ===========================================================================


class TestFausseCompletion(ControleTestCase):
    def test_unknown_chantier_raises(self):
        with self.assertRaises(controle.ControleError):
            controle.fausse_completion("c-99")

    def test_capteur_not_adopted_says_so_explicitly(self):
        # I12 : pas de mesure disponible avant adoption -> pas de liste vide silencieuse,
        # un signal explicite qui dit qu'on n'a rien pu verifier.
        cid = self._chantier()
        result = controle.fausse_completion(cid)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "unavailable")

    def test_capteur_installed_but_not_yet_adopted_still_says_indisponible(self):
        cid = self._chantier()
        src = self._python_sensor("sensor.py", [{"name": "m", "value": 1}], [])
        capteur.install(cid, src)
        capteur.run(cid)  # une seule execution : pas assez pour l'adoption
        result = controle.fausse_completion(cid)
        self.assertEqual(result, [{"kind": "unavailable",
                                    "detail": result[0]["detail"]}])

    def test_declared_without_matching_measured_is_flagged(self):
        cid = self._chantier()
        self._adopted_capteur(
            cid,
            measured=[{"name": "stories", "value": 12, "unit": "fichiers"}],
            declared=[{"name": "prouve", "value": "42/379", "source": "docs/mission.md"}],
        )
        result = controle.fausse_completion(cid)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "declared-unmeasured")
        self.assertEqual(result[0]["name"], "prouve")

    def test_declared_with_matching_measured_name_is_not_flagged(self):
        cid = self._chantier()
        self._adopted_capteur(
            cid,
            measured=[{"name": "prouve", "value": "42/379", "unit": None}],
            declared=[{"name": "prouve", "value": "42/379", "source": "docs/mission.md"}],
        )
        self.assertEqual(controle.fausse_completion(cid), [])

    def test_no_declared_entries_is_simply_empty(self):
        cid = self._chantier()
        self._adopted_capteur(cid, measured=[{"name": "m", "value": 1}], declared=[])
        self.assertEqual(controle.fausse_completion(cid), [])


# ===========================================================================
# wake_reasons
# ===========================================================================


class TestWakeReasons(ControleTestCase):
    def test_unknown_chantier_raises(self):
        with self.assertRaises(controle.ControleError):
            controle.wake_reasons("c-99")

    def test_no_reasons_on_a_freshly_opened_chantier(self):
        cid = self._chantier()
        self.assertEqual(controle.wake_reasons(cid), [])

    def test_terminal_report_wakes(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="done", report={"state": "done", "touched": []})
        reasons = controle.wake_reasons(cid)
        kinds = [r["kind"] for r in reasons]
        self.assertIn("terminal-report", kinds)

    def test_pane_mort_wakes_as_its_own_kind_not_rapport_terminal(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="blocked", error=f"{controle.PANE_MORT_RAISON} (99 s after launch)")
        reasons = controle.wake_reasons(cid)
        kinds = [r["kind"] for r in reasons]
        self.assertIn("pane-dead", kinds)
        self.assertNotIn("terminal-report", kinds)

    def test_confiance_attendue_wakes_as_its_own_kind_not_rapport_terminal(self):
        """Defaut 2 : le dialogue de confiance doit reveiller l'orchestratrice, avec un
        motif distinct d'un pane mort et d'une derive de scope."""
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(
            tid,
            state="blocked",
            error=f"{controle.panes.TRUST_DIALOG_BLOCK_RAISON} in pane %42 "
            "(session ordo-s): human decision required",
        )
        reasons = controle.wake_reasons(cid)
        kinds = [r["kind"] for r in reasons]
        self.assertIn("trust-expected", kinds)
        self.assertNotIn("terminal-report", kinds)
        self.assertNotIn("pane-dead", kinds)

    def test_dependency_propagated_block_does_not_wake_on_its_own(self):
        # La cause racine a deja son propre motif de reveil ; le blocage en cascade ne
        # doit pas produire un doublon trompeur ("terminal-report" alors qu'aucun
        # rapport n'est jamais arrive pour cette tache-la).
        cid = self._chantier()
        a = self._task(cid, titre="a")
        b = self._task(cid, titre="b", depends_on=[a])
        self._set_task(a, state="failed")
        with store.locked() as state:
            chantier.propagate_failures(state, cid)
        reasons = controle.wake_reasons(cid)
        self.assertEqual([r for r in reasons if r.get("task") == b], [])

    def test_question_pour_humain_non_repondue_wakes(self):
        cid = self._chantier()
        tid = self._task(cid)
        with store.locked() as state:
            qid = store.next_id(state, "question")
            state["questions"][qid] = {
                "id": qid, "chantier": cid, "tache": tid, "question": "quoi ?",
                "options": [], "pourHumain": True, "answer": None,
                "askedAt": store.now(), "answeredAt": None,
            }
        reasons = controle.wake_reasons(cid)
        self.assertTrue(any(r["kind"] == "human-question" for r in reasons))

    def test_question_not_pour_humain_does_not_wake(self):
        cid = self._chantier()
        tid = self._task(cid)
        with store.locked() as state:
            qid = store.next_id(state, "question")
            state["questions"][qid] = {
                "id": qid, "chantier": cid, "tache": tid, "question": "quoi ?",
                "options": [], "pourHumain": False, "answer": None,
                "askedAt": store.now(), "answeredAt": None,
            }
        reasons = controle.wake_reasons(cid)
        self.assertFalse(any(r["kind"] == "human-question" for r in reasons))

    def test_scope_drift_wakes(self):
        cid = self._chantier()
        tid = self._task(cid, touches=["app"])
        self._set_task(tid, report={"state": "done", "touched": ["infra/x.tf"]})
        reasons = controle.wake_reasons(cid)
        self.assertTrue(any(r["kind"] == "scope-drift" for r in reasons))

    def test_capteur_double_echec_wakes(self):
        cid = self._chantier()
        src = self._python_failing()
        capteur.install(cid, src)
        # adoption impossible pour un capteur qui echoue toujours ; on force l'etat
        # "adopte" + deux echecs consecutifs pour isoler ce que wake_reasons doit lire.
        with store.locked() as state:
            state["chantiers"][cid]["capteur"]["adopted"] = True
        capteur.run(cid)
        capteur.run(cid)
        self.assertEqual(capteur.status(cid)["signal"], "waking")
        reasons = controle.wake_reasons(cid)
        self.assertTrue(any(r["kind"] == "sensor-double-failure" for r in reasons))

    def test_tour_de_controle_after_idle_delay(self):
        cid = self._chantier()
        with mock.patch.object(controle, "WAKE_IDLE_AFTER_S", 0.01):
            time.sleep(0.05)
            reasons = controle.wake_reasons(cid)
        self.assertTrue(any(r["kind"] == "control-round" for r in reasons))

    def test_no_tour_de_controle_before_idle_delay(self):
        cid = self._chantier()
        with mock.patch.object(controle, "WAKE_IDLE_AFTER_S", 900.0):
            reasons = controle.wake_reasons(cid)
        self.assertFalse(any(r["kind"] == "control-round" for r in reasons))

    def test_reasons_are_scoped_to_the_requested_chantier(self):
        cid_a = self._chantier(nom="a")
        cid_b = self._chantier(nom="b")
        tid = self._task(cid_b)
        self._set_task(tid, state="done", report={"state": "done", "touched": []})
        self.assertEqual(controle.wake_reasons(cid_a), [])
        self.assertTrue(controle.wake_reasons(cid_b))


# ===========================================================================
# wake_reasons : motif actionnable (pane_id + commande d'attache)
# ===========================================================================


class TestWakeReasonsActionnable(ControleTestCase):
    """Motifs 2 : seul confiance-attendue portait deja pane + commande d'attache.
    pane-mort, rapport-terminal et derive-scope doivent desormais porter les deux,
    des lors que la tache concernee a un pane_id."""

    def _session(self, cid: str) -> str:
        return store.load()["chantiers"][cid]["tmuxSession"]

    def test_pane_mort_detail_porte_pane_id_et_commande_dattache(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(
            tid, state="blocked", paneId="%42",
            error=f"{controle.PANE_MORT_RAISON} (99 s après le lancement)",
        )
        motif = next(r for r in controle.wake_reasons(cid) if r["kind"] == "pane-dead")
        self.assertIn("%42", motif["detail"])
        self.assertIn(f"tmux attach -t {self._session(cid)}", motif["detail"])

    def test_rapport_terminal_detail_porte_pane_id_et_commande_dattache(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(
            tid, state="done", paneId="%43", report={"state": "done", "touched": []}
        )
        motif = next(r for r in controle.wake_reasons(cid) if r["kind"] == "terminal-report")
        self.assertIn("%43", motif["detail"])
        self.assertIn(f"tmux attach -t {self._session(cid)}", motif["detail"])

    def test_derive_scope_detail_porte_pane_id_et_commande_dattache(self):
        cid = self._chantier()
        tid = self._task(cid, touches=["app"])
        self._set_task(
            tid, paneId="%44", report={"state": "done", "touched": ["infra/x.tf"]}
        )
        motif = next(r for r in controle.wake_reasons(cid) if r["kind"] == "scope-drift")
        self.assertIn("%44", motif["detail"])
        self.assertIn(f"tmux attach -t {self._session(cid)}", motif["detail"])

    def test_motif_sur_tache_sans_pane_ne_porte_rien(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="done", paneId=None, report={"state": "done", "touched": []})
        motif = next(r for r in controle.wake_reasons(cid) if r["kind"] == "terminal-report")
        self.assertNotIn("tmux attach", motif["detail"])
        self.assertNotIn("pane", motif["detail"])

    def test_commande_dattache_prefixee_env_tmux_quand_variable_definie(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="done", paneId="%45", report={"state": "done", "touched": []})
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1234,0"}):
            motif = next(r for r in controle.wake_reasons(cid) if r["kind"] == "terminal-report")
        self.assertIn(f"env -u TMUX tmux attach -t {self._session(cid)}", motif["detail"])

    def test_commande_dattache_sans_prefixe_quand_variable_absente(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="done", paneId="%46", report={"state": "done", "touched": []})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            motif = next(r for r in controle.wake_reasons(cid) if r["kind"] == "terminal-report")
        self.assertIn(f"tmux attach -t {self._session(cid)}", motif["detail"])
        self.assertNotIn("env -u TMUX", motif["detail"])


# ===========================================================================
# wake_reasons : un chantier clos n'emet plus aucun motif
# ===========================================================================


class TestWakeReasonsChantierClos(ControleTestCase):
    def test_wake_reasons_chantier_clos_ne_produit_aucun_motif(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="done", report={"state": "done", "touched": []})
        self.assertTrue(controle.wake_reasons(cid))  # sanity : motif tant que ouvert
        chantier.close(cid)
        self.assertEqual(controle.wake_reasons(cid), [])

    def test_wake_new_chantier_clos_ne_produit_aucun_motif(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="done", report={"state": "done", "touched": []})
        chantier.close(cid)
        self.assertEqual(controle.wake_new(cid), [])


# ===========================================================================
# tick() : ordre des etapes, isolation par chantier, effets de bord
# ===========================================================================


class TestTickReportFirst(ControleTestCase):
    def test_i2_le_rapport_prime_sur_un_pane_mort(self):
        # Invariant I2 : pour une tache running, le rapport est lu EN PREMIER. Un pane
        # signale mort ne doit jamais l'emporter sur un rapport "done" deja arrive.
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999")
        self._write_report(tid, {"task": tid, "state": "done", "note": "fini", "touched": []})
        with mock.patch.object(panes, "alive", return_value=False):
            controle.tick(cid)
        self.assertEqual(store.load()["taches"][tid]["state"], "done")

    def test_rapport_illisible_bloque_sans_regarder_le_pane(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999")
        report.path(tid, cid).write_text("pas du json du tout", encoding="utf-8")
        with mock.patch.object(panes, "alive", return_value=True):
            controle.tick(cid)
        task = store.load()["taches"][tid]
        self.assertEqual(task["state"], "blocked")
        self.assertIn("unreadable", task["error"])


class TestTickPaneDeath(ControleTestCase):
    def test_pane_mort_au_dela_du_delai_sans_rapport_bloque(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999", started_ago=999.0)
        with mock.patch.object(panes, "alive", return_value=False), \
             mock.patch.object(controle, "PANE_DEAD_GRACE_S", 1.0):
            controle.tick(cid)
        task = store.load()["taches"][tid]
        self.assertEqual(task["state"], "blocked")
        self.assertIn(controle.PANE_MORT_RAISON, task["error"])

    def test_pane_mort_sous_le_delai_de_grace_ne_bloque_pas_encore(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999", started_ago=0.0)
        with mock.patch.object(panes, "alive", return_value=False), \
             mock.patch.object(controle, "PANE_DEAD_GRACE_S", 999.0):
            controle.tick(cid)
        self.assertEqual(store.load()["taches"][tid]["state"], "running")

    def test_pane_vivant_inactif_sans_rapport_nest_pas_un_echec(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999", started_ago=9999.0)
        with mock.patch.object(panes, "alive", return_value=True):
            controle.tick(cid)
        self.assertEqual(store.load()["taches"][tid]["state"], "running")

    def test_pane_mort_message_porte_pane_id_et_chemins_brief_et_rapport(self):
        # Point 3 du contrat : "pane mort, aucun rapport recu" ne disait pas ou
        # chercher la trace. Le message doit desormais nommer le pane et les deux
        # chemins sur disque (brief envoye, rapport attendu).
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999", started_ago=999.0)
        with mock.patch.object(panes, "alive", return_value=False), \
             mock.patch.object(controle, "PANE_DEAD_GRACE_S", 1.0):
            controle.tick(cid)
        error = store.load()["taches"][tid]["error"]
        self.assertIn("%999", error)
        self.assertIn(str(store.home() / "briefs" / cid / f"{tid}.md"), error)
        self.assertIn(str(report.path(tid, cid)), error)


class TestTickAsking(ControleTestCase):
    def test_rapport_asking_cree_une_question_et_passe_en_waiting(self):
        # Piege explicite : report.apply() met la tache en "waiting" mais ne cree
        # AUCUNE entree dans state["questions"]. C'est le travail de tick().
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(
            tid, {"task": tid, "state": "asking", "note": "", "question": "quelle valeur ?"}
        )
        controle.tick(cid)
        state = store.load()
        self.assertEqual(state["taches"][tid]["state"], "waiting")
        questions = [q for q in state["questions"].values() if q["tache"] == tid]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["question"], "quelle valeur ?")
        self.assertFalse(questions[0]["pourHumain"])
        self.assertIsNone(questions[0]["answer"])


class TestTickWaitingReportReread(ControleTestCase):
    """Défaut principal mesuré sur la production (docs/diagnostic-envoi.md) : seule la
    liste "running" était relue à l'étape 2, donc une tâche passée "waiting" ne voyait
    plus jamais son rapport relu, et rien ne pouvait plus l'en sortir sauf l'injection
    d'une réponse. Cas réels : loko t-56 (rapport done sur disque, jamais vu) et camcast
    t-74 (rapport asking relu sans qu'aucune question n'existe)."""

    def test_rapport_dune_tache_waiting_est_relu(self):
        # Un rapport "progress" est le plus discret : il ne change l'état de lui-même,
        # il ne prouve donc la relecture QUE si son contenu est bien appliqué et le
        # fichier consommé.
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999")
        with store.locked() as state:
            state["taches"][tid]["state"] = "waiting"
        self._write_report(tid, {"task": tid, "state": "progress", "note": "toujours la"})
        controle.tick(cid)
        task = store.load()["taches"][tid]
        self.assertEqual(task["state"], "waiting")
        self.assertEqual(task["report"]["note"], "toujours la")
        self.assertFalse(report.path(tid, cid).exists(), "le rapport relu doit être consommé")

    def test_rapport_done_sort_la_tache_de_waiting(self):
        # Cas loko t-56 : rapport state=done sur disque, tâche toujours "waiting" dans
        # l'état d'Ordo, pane vivant au repos. Rien d'autre que la relecture du rapport
        # ne peut l'en sortir.
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999")
        with store.locked() as state:
            state["taches"][tid]["state"] = "waiting"
        self._write_report(tid, {"task": tid, "state": "done", "note": "fini", "touched": []})
        controle.tick(cid)
        self.assertEqual(store.load()["taches"][tid]["state"], "done")

    def test_rapport_asking_sur_tache_waiting_cree_une_question(self):
        # Cas camcast t-74 : rapport state=asking relu pour une tâche déjà waiting, sans
        # qu'aucune question n'existe encore pour elle. Personne ne pouvait plus
        # répondre à un état totalement fermé.
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999")
        with store.locked() as state:
            state["taches"][tid]["state"] = "waiting"
        self._write_report(
            tid, {"task": tid, "state": "asking", "note": "", "question": "et maintenant ?"}
        )
        controle.tick(cid)
        state = store.load()
        self.assertEqual(state["taches"][tid]["state"], "waiting")
        questions = [q for q in state["questions"].values() if q["tache"] == tid]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["question"], "et maintenant ?")


class TestTickBlockedReportReread(ControleTestCase):
    """Jumeau exact du défaut waiting (TestTickWaitingReportReread ci-dessus) pour
    l'état blocked : seules les tâches "running" puis "running"+"waiting" étaient
    relues à l'étape 2, donc une tâche "blocked" ne voyait plus jamais son rapport
    relu. Cas réel (brief t-35, constaté trois fois sur t-31) : une exécutante
    débloquée par `ordo say` reprend, finit son travail, coche ses critères et écrit
    un rapport "done" -- rien ne le lisait, la tâche restait "blocked" pour toujours
    et ses dépendantes ne partaient jamais."""

    def test_rapport_dune_tache_bloquee_est_relu(self):
        # Un rapport "progress" est le plus discret : il ne change l'état de lui-même,
        # il ne prouve donc la relecture QUE si son contenu est bien appliqué et le
        # fichier consommé (garde-fou 2 du brief : aucun rapport n'est lu deux fois).
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="blocked", error="obstacle réel rencontré")
        self._write_report(tid, {"task": tid, "state": "progress", "note": "toujours bloquée"})
        controle.tick(cid)
        task = store.load()["taches"][tid]
        self.assertEqual(task["state"], "blocked")
        self.assertEqual(task["report"]["note"], "toujours bloquée")
        self.assertFalse(report.path(tid, cid).exists(), "le rapport relu doit être consommé")

    def test_rapport_done_sur_tache_bloquee_la_fait_passer_a_done(self):
        # Séquence vécue trois fois sur t-31 : l'orchestratrice débloque par `ordo say`,
        # l'exécutante reprend, coche ses critères et écrit "done". Rien d'autre que la
        # relecture du rapport ne peut l'en sortir.
        cid = self._chantier()
        tid = self._task(cid, checklist=["c1"])
        self._set_task(tid, state="blocked", error="obstacle réel rencontré")
        self._write_report(
            tid, {"task": tid, "state": "done", "note": "fini", "checked": ["c1"], "touched": []}
        )
        controle.tick(cid)
        task = store.load()["taches"][tid]
        self.assertEqual(task["state"], "done")
        self.assertTrue(all(item["done"] for item in task["checklist"]))

    def test_rapport_encore_blocked_reste_bloquee(self):
        # Garde-fou 1 du brief t-35 : une tâche qui rend ENCORE "blocked" reste
        # bloquée. On lit son rapport, on met à jour sa note et sa cause, mais on ne
        # la ressuscite jamais -- seule l'orchestratrice décide qu'un blocage est levé.
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="blocked", error="premier obstacle")
        self._write_report(
            tid, {"task": tid, "state": "blocked", "note": "toujours le même obstacle"}
        )
        controle.tick(cid)
        task = store.load()["taches"][tid]
        self.assertEqual(task["state"], "blocked")
        self.assertEqual(task["error"], "toujours le même obstacle")
        self.assertFalse(report.path(tid, cid).exists(), "le rapport relu doit être consommé")

    def test_dependante_debloquee_apres_que_la_bloquee_passe_a_done(self):
        # Garde-fou 3 : sans la relecture, la dépendante d'une tâche "blocked" bloquée
        # par propagation ne repart jamais, même après que la tâche-cause ait vraiment
        # fini -- c'est tout l'intérêt du correctif (le graphe reste sinon figé en
        # silence pour une moitié du chantier).
        cid = self._chantier()
        a = self._task(cid, titre="a", checklist=["c1"])
        b = self._task(cid, titre="b", depends_on=[a])
        self._set_task(a, state="blocked", error="obstacle réel rencontré")
        controle.tick(cid)
        self.assertEqual(store.load()["taches"][b]["state"], "blocked")
        self.assertEqual(
            store.load()["taches"][b]["blockedCause"], chantier.BLOCKED_CAUSE_PROPAGATION
        )

        self._write_report(
            a, {"task": a, "state": "done", "note": "fini", "checked": ["c1"], "touched": []}
        )
        controle.tick(cid)

        state = store.load()
        self.assertEqual(state["taches"][a]["state"], "done")
        self.assertEqual(state["taches"][b]["state"], "queued")
        self.assertIsNone(state["taches"][b]["blockedCause"])


class TestTickJournalMachineCycleDeVie(ControleTestCase):
    """t-51 : quatre faits machine de plus, jumeaux de checklist-doing/checklist-coche
    (t-34/t-36) mais pour le cycle de vie de la tâche au-delà de sa checklist."""

    def test_rapport_lu_journalise_ecrit_at_depuis_le_mtime_du_fichier(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(tid, {"task": tid, "state": "progress", "note": "en cours"})
        mtime_attendu = report.path(tid, cid).stat().st_mtime
        controle.tick(cid)
        evenements = journal.lire_evenements(cid, "rapport-lu")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)
        self.assertEqual(
            evenements[0]["ecrit_at"],
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime_attendu)),
        )

    def test_tache_terminee_journalisee_sur_done(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(tid, {"task": tid, "state": "done", "note": "fini", "touched": []})
        controle.tick(cid)
        evenements = journal.lire_evenements(cid, "tache-terminee")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)
        self.assertEqual(evenements[0]["etat"], "done")

    def test_tache_terminee_journalisee_sur_blocage_par_pane_mort(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999", started_ago=999.0)
        with mock.patch.object(panes, "alive", return_value=False), \
             mock.patch.object(controle, "PANE_DEAD_GRACE_S", 1.0):
            controle.tick(cid)
        evenements = journal.lire_evenements(cid, "tache-terminee")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)
        self.assertEqual(evenements[0]["etat"], "blocked")

    def test_tache_terminee_ne_double_journalise_pas_un_rapport_encore_blocked(self):
        # Garde-fou (jumeau de test_rapport_encore_blocked_reste_bloquee ci-dessus) :
        # une tâche déjà bloquée qui reçoit un nouveau rapport "blocked" ne redevient
        # pas "terminée" une seconde fois, ce ne serait pas une nouvelle terminaison.
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="blocked", error="premier obstacle")
        self._write_report(tid, {"task": tid, "state": "blocked", "note": "encore bloquée"})
        controle.tick(cid)
        self.assertEqual(journal.lire_evenements(cid, "tache-terminee"), [])


class TestTacheBloqueeCategorie(ControleTestCase):
    """t-53 : le fait "tache-bloquee" porte désormais une catégorie, déduite du point
    d'émission (jamais d'un parsing du texte de cause) -- pane mort d'un côté, rapport
    d'échec (illisible ou explicitement "blocked") de l'autre."""

    def test_categorie_rapport_sur_blocage_rapporte_par_lexecutante(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(tid, {"task": tid, "state": "blocked", "note": "obstacle réel"})
        controle.tick(cid)
        evenements = journal.lire_evenements(cid, "tache-bloquee")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)
        self.assertEqual(evenements[0]["cause"], "obstacle réel")
        self.assertEqual(evenements[0]["categorie"], controle.CATEGORIE_RAPPORT)

    def test_categorie_rapport_sur_rapport_illisible(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999")
        report.path(tid, cid).write_text("pas du json du tout", encoding="utf-8")
        with mock.patch.object(panes, "alive", return_value=True):
            controle.tick(cid)
        evenements = journal.lire_evenements(cid, "tache-bloquee")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["categorie"], controle.CATEGORIE_RAPPORT)

    def test_categorie_pane_mort_sur_blocage_par_pane_mort(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999", started_ago=999.0)
        with mock.patch.object(panes, "alive", return_value=False), \
             mock.patch.object(controle, "PANE_DEAD_GRACE_S", 1.0):
            controle.tick(cid)
        evenements = journal.lire_evenements(cid, "tache-bloquee")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)
        self.assertEqual(evenements[0]["categorie"], controle.CATEGORIE_PANE_MORT)
        self.assertIn(controle.PANE_MORT_RAISON, evenements[0]["cause"])

    def test_ne_double_journalise_pas_la_meme_tache_et_cause(self):
        # Même garde-fou anti-répétition que derive-perimetre (derives_deja_journalisees)
        # : un rapport qui reconfirme EXACTEMENT le même blocage, tour après tour, ne
        # doit pas noyer le journal machine d'autant de faits identiques que de tours.
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(tid, {"task": tid, "state": "blocked", "note": "même obstacle"})
        controle.tick(cid)
        self._write_report(tid, {"task": tid, "state": "blocked", "note": "même obstacle"})
        controle.tick(cid)
        evenements = journal.lire_evenements(cid, "tache-bloquee")
        self.assertEqual(len(evenements), 1)


class TestTacheDebloquee(ControleTestCase):
    """t-53 : le fait "tache-debloquee" s'écrit quand une tâche "blocked" en sort par un
    des mécanismes que controle.py observe directement -- jamais par relance (cli.py, un
    autre fichier, hors de son périmètre), qui mute state["running"] sans jamais passer
    par tick()."""

    def test_verbe_say_quand_un_rapport_leve_le_blocage(self):
        # Séquence vécue trois fois sur t-31 (voir TestTickBlockedReportReread) :
        # l'orchestratrice débloque par `ordo say`, l'exécutante reprend et écrit "done".
        cid = self._chantier()
        tid = self._task(cid, checklist=["c1"])
        self._set_task(tid, state="blocked", error="obstacle réel rencontré")
        self._write_report(
            tid, {"task": tid, "state": "done", "note": "fini", "checked": ["c1"], "touched": []}
        )
        controle.tick(cid)
        evenements = journal.lire_evenements(cid, "tache-debloquee")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)
        self.assertEqual(evenements[0]["cause"], "obstacle réel rencontré")
        self.assertEqual(evenements[0]["verbe"], controle.VERBE_SAY)

    def test_pas_de_tache_debloquee_si_le_rapport_reconfirme_le_blocage(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="blocked", error="premier obstacle")
        self._write_report(
            tid, {"task": tid, "state": "blocked", "note": "toujours le même obstacle"}
        )
        controle.tick(cid)
        self.assertEqual(journal.lire_evenements(cid, "tache-debloquee"), [])

    def test_verbe_unblock_propagated_quand_la_dependance_redevient_saine(self):
        cid = self._chantier()
        a = self._task(cid, titre="a", checklist=["fait"])
        b = self._task(cid, titre="b", depends_on=[a])
        self._set_task(a, state="failed")
        controle.tick(cid)  # b bloquée par propagation

        chantier.check(a, "c1")
        self._set_task(a, state="done", error=None)
        controle.tick(cid)  # b levée

        evenements = journal.lire_evenements(cid, "tache-debloquee")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], b)
        self.assertEqual(evenements[0]["cause"], f"dependency {a} dead (failed)")
        self.assertEqual(evenements[0]["verbe"], controle.VERBE_UNBLOCK_PROPAGATED)


class TestTacheCout(ControleTestCase):
    """t-52 : le coût d'une tâche (jetons lus depuis son transcript via usage.pour) doit
    s'écrire au journal machine dès qu'elle atteint un état terminal -- jumeau de
    tache-terminee (t-51), sinon ce coût n'est plus récupérable une fois le transcript
    disparu du disque."""

    _JETONS = {"input": 10, "output": 20, "cacheCreation": 5, "cacheRead": 100,
               "turns": 3, "dernierContexte": 40}

    def test_tache_cout_journalise_sur_done(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(tid, {"task": tid, "state": "done", "note": "fini", "touched": []})
        with mock.patch.object(controle.usage, "pour", return_value=self._JETONS):
            controle.tick(cid)
        evenements = journal.lire_evenements(cid, "tache-cout")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)
        self.assertEqual(evenements[0]["input"], 10)
        self.assertEqual(evenements[0]["output"], 20)
        self.assertEqual(evenements[0]["cacheCreation"], 5)
        self.assertEqual(evenements[0]["cacheRead"], 100)
        self.assertEqual(evenements[0]["source"], "transcript")

    def test_tache_cout_journalise_sur_blocage_par_pane_mort(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999", started_ago=999.0)
        with mock.patch.object(panes, "alive", return_value=False), \
             mock.patch.object(controle, "PANE_DEAD_GRACE_S", 1.0), \
             mock.patch.object(controle.usage, "pour", return_value=self._JETONS):
            controle.tick(cid)
        evenements = journal.lire_evenements(cid, "tache-cout")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)
        self.assertEqual(evenements[0]["source"], "transcript")

    def test_tache_cout_transcript_introuvable_dit_source_explicite_jamais_zero(self):
        # L'invariant du docstring de usage.py : l'absence se dit absente, jamais zéro.
        # Aucun champ de jetons n'est écrit quand le transcript est introuvable -- un 0
        # silencieux se lirait comme "cette tâche n'a rien consommé", ce qui est faux.
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(tid, {"task": tid, "state": "done", "note": "fini", "touched": []})
        with mock.patch.object(controle.usage, "pour", return_value=None):
            controle.tick(cid)
        evenements = journal.lire_evenements(cid, "tache-cout")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["source"], "transcript introuvable")
        self.assertNotIn("input", evenements[0])
        self.assertNotIn("output", evenements[0])
        self.assertNotIn("cacheCreation", evenements[0])
        self.assertNotIn("cacheRead", evenements[0])

    def test_tache_cout_ne_relit_pas_deux_fois_le_meme_transcript(self):
        # Le brief interdit une seconde lecture du transcript : un seul appel à la
        # fonction publique de usage.py pour écrire ce fait.
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(tid, {"task": tid, "state": "done", "note": "fini", "touched": []})
        with mock.patch.object(controle.usage, "pour", return_value=self._JETONS) as m:
            controle.tick(cid)
        m.assert_called_once()

    def test_tache_cout_ecriture_jamais_bloquante(self):
        # Même contrat que enregistrer_evenement (best-effort) : une lecture de transcript
        # qui lève ne doit jamais faire tomber le tick qui vient de terminer une tâche.
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(tid, {"task": tid, "state": "done", "note": "fini", "touched": []})
        with mock.patch.object(controle.usage, "pour", side_effect=RuntimeError("boom")):
            result = controle.tick(cid)
        self.assertIsNone(result["chantiers"][cid]["error"])
        self.assertEqual(store.load()["taches"][tid]["state"], "done")

    def test_tache_cout_ne_double_journalise_pas_un_rapport_encore_blocked(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="blocked", error="premier obstacle")
        self._write_report(tid, {"task": tid, "state": "blocked", "note": "encore bloquée"})
        with mock.patch.object(controle.usage, "pour", return_value=self._JETONS):
            controle.tick(cid)
        self.assertEqual(journal.lire_evenements(cid, "tache-cout"), [])


class TestTickAnswerInjection(ControleTestCase):
    def test_reponse_injectee_dans_le_pane_reel_et_tache_reprend(self):
        cid = self._chantier()
        tid = self._task(cid)
        session, pane_id = self._real_pane()
        self._make_running(tid, pane_id=pane_id)
        with store.locked() as state:
            task = state["taches"][tid]
            task["state"] = "waiting"
            qid = store.next_id(state, "question")
            state["questions"][qid] = {
                "id": qid, "chantier": cid, "tache": tid, "question": "combien ?",
                "options": [], "pourHumain": False, "answer": "quarante-deux",
                "askedAt": store.now(), "answeredAt": store.now(),
            }
        controle.tick(cid)
        self.assertEqual(store.load()["taches"][tid]["state"], "running")
        captured = _wait_for(lambda: "quarante-deux" in panes.capture(pane_id, lines=40))
        self.assertTrue(captured, "la reponse aurait du etre injectee dans le pane")

    def test_waiting_sans_reponse_reste_en_attente(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999")
        with store.locked() as state:
            state["taches"][tid]["state"] = "waiting"
            qid = store.next_id(state, "question")
            state["questions"][qid] = {
                "id": qid, "chantier": cid, "tache": tid, "question": "combien ?",
                "options": [], "pourHumain": False, "answer": None,
                "askedAt": store.now(), "answeredAt": None,
            }
        controle.tick(cid)
        self.assertEqual(store.load()["taches"][tid]["state"], "waiting")

    def test_reponse_a_la_question_repondue_injectee_malgre_une_plus_recente_sans_reponse(self):
        # Reproduction 2 du diagnostic (docs/diagnostic-envoi.md) : une tâche accumule
        # deux questions, la plus ancienne répondue, la plus récente pas encore.
        # L'ancienne _latest_question() ciblait la plus récente et, la trouvant sans
        # réponse, n'injectait rien -- pour toujours : la réponse à q-01 restait
        # invisible tant que q-02 elle-même n'était pas répondue.
        cid = self._chantier()
        tid = self._task(cid)
        session, pane_id = self._real_pane()
        self._make_running(tid, pane_id=pane_id)
        with store.locked() as state:
            state["taches"][tid]["state"] = "waiting"
            q1 = store.next_id(state, "question")
            state["questions"][q1] = {
                "id": q1, "chantier": cid, "tache": tid, "question": "la première ?",
                "options": [], "pourHumain": False, "answer": "réponse-ancienne",
                "askedAt": store.now(), "answeredAt": store.now(), "injectedAt": None,
            }
            q2 = store.next_id(state, "question")
            state["questions"][q2] = {
                "id": q2, "chantier": cid, "tache": tid, "question": "la seconde ?",
                "options": [], "pourHumain": False, "answer": None,
                "askedAt": store.now(), "answeredAt": None, "injectedAt": None,
            }
        controle.tick(cid)
        self.assertEqual(store.load()["taches"][tid]["state"], "running")
        captured = _wait_for(lambda: "réponse-ancienne" in panes.capture(pane_id, lines=40))
        self.assertTrue(
            captured, "la réponse à la question la plus ancienne aurait dû être injectée"
        )
        self.assertIsNotNone(store.load()["questions"][q1]["injectedAt"])

    def test_departage_par_identifiant_pas_par_ordre_texte(self):
        # store.py:66 : store.now() est horodaté à la seconde. _question_a_injecter()
        # ne doit jamais départager par cet horodatage (deux questions nées dans la
        # même seconde y sont indiscernables), et jamais par un tri texte des
        # identifiants ("q-100" < "q-99" en chaînes, l'inverse en nombres).
        cid = self._chantier()
        tid = self._task(cid)
        with store.locked() as state:
            for qid_texte in ("q-99", "q-100"):
                state["questions"][qid_texte] = {
                    "id": qid_texte, "chantier": cid, "tache": tid, "question": "?",
                    "options": [], "pourHumain": False, "answer": qid_texte,
                    "askedAt": store.now(), "answeredAt": store.now(), "injectedAt": None,
                }
        q = controle._question_a_injecter(store.load(), tid)
        self.assertEqual(
            q["id"], "q-99",
            "q-99 est numériquement plus ancienne que q-100, même si le tri texte les inverse",
        )


class TestTickJournalMachineQuestions(ControleTestCase):
    """t-54 : deux faits machine de plus, jumeaux du reste du journal machine (voir
    TestTickJournalMachineCycleDeVie ci-dessus) mais pour le cycle de vie d'une
    question "waiting" : sa création (étape 2, rapport "asking") et l'injection de sa
    réponse dans le pane (étape 4). Les deux événements survivent déjà dans state.json
    tant qu'il n'est jamais purgé, mais restent invisibles à qui ne relit que le
    .jsonl -- même raisonnement que rapport-lu/tache-terminee (t-51)."""

    def test_question_posee_journalisee_a_la_creation(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(
            tid, {"task": tid, "state": "asking", "note": "", "question": "quelle valeur ?"}
        )
        controle.tick(cid)
        evenements = journal.lire_evenements(cid, "question-posee")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)
        self.assertEqual(evenements[0]["question"], "quelle valeur ?")
        self.assertFalse(evenements[0]["pourHumain"])

    def test_question_posee_texte_tronque_a_200_caracteres(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        longue_question = "x" * 250
        self._write_report(
            tid, {"task": tid, "state": "asking", "note": "", "question": longue_question}
        )
        controle.tick(cid)
        evenements = journal.lire_evenements(cid, "question-posee")
        self.assertEqual(len(evenements[0]["question"]), 200)
        self.assertEqual(evenements[0]["question"], longue_question[:200])

    def test_reponse_injectee_journalisee(self):
        cid = self._chantier()
        tid = self._task(cid)
        session, pane_id = self._real_pane()
        self._make_running(tid, pane_id=pane_id)
        with store.locked() as state:
            task = state["taches"][tid]
            task["state"] = "waiting"
            qid = store.next_id(state, "question")
            state["questions"][qid] = {
                "id": qid, "chantier": cid, "tache": tid, "question": "combien ?",
                "options": [], "pourHumain": False, "answer": "quarante-deux",
                "askedAt": store.now(), "answeredAt": store.now(), "injectedAt": None,
            }
        controle.tick(cid)
        evenements = journal.lire_evenements(cid, "reponse-injectee")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)
        self.assertEqual(evenements[0]["question"], qid)
        self.assertEqual(evenements[0]["reponse"], "quarante-deux")

    def test_reponse_injectee_texte_tronque_a_200_caracteres(self):
        cid = self._chantier()
        tid = self._task(cid)
        session, pane_id = self._real_pane()
        self._make_running(tid, pane_id=pane_id)
        longue_reponse = "y" * 250
        with store.locked() as state:
            task = state["taches"][tid]
            task["state"] = "waiting"
            qid = store.next_id(state, "question")
            state["questions"][qid] = {
                "id": qid, "chantier": cid, "tache": tid, "question": "combien ?",
                "options": [], "pourHumain": False, "answer": longue_reponse,
                "askedAt": store.now(), "answeredAt": store.now(), "injectedAt": None,
            }
        controle.tick(cid)
        evenements = journal.lire_evenements(cid, "reponse-injectee")
        self.assertEqual(len(evenements[0]["reponse"]), 200)
        self.assertEqual(evenements[0]["reponse"], longue_reponse[:200])

    def test_pas_de_reponse_injectee_journalisee_sans_reponse(self):
        # Garde-fou jumeau de test_waiting_sans_reponse_reste_en_attente ci-dessus :
        # une question posée mais pas encore répondue ne doit produire aucun fait, le
        # pane n'a rien reçu.
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999")
        with store.locked() as state:
            state["taches"][tid]["state"] = "waiting"
            qid = store.next_id(state, "question")
            state["questions"][qid] = {
                "id": qid, "chantier": cid, "tache": tid, "question": "combien ?",
                "options": [], "pourHumain": False, "answer": None,
                "askedAt": store.now(), "answeredAt": None, "injectedAt": None,
            }
        controle.tick(cid)
        self.assertEqual(journal.lire_evenements(cid, "reponse-injectee"), [])


class TestTickAttenteInteractive(ControleTestCase):
    """t-54 (c6) : le journal machine dit QUAND une tâche s'est arrêtée d'avancer
    (checklist-silent) mais rien n'y distingue jusqu'ici "bloquée sur un dialogue" de
    "réfléchit longuement" -- mesuré en pratique sur t-57 (16 minutes de trou dues au
    dialogue de confiance tmux, découvertes seulement par une lecture manuelle du
    pane). Deux briques déjà existantes, aucune troisième : panes.busy() (t-25)
    distingue occupé d'inerte, panes.TRUST_DIALOG_MARKERS (déjà lu par
    _is_confiance_block au lancement) reconnaît le dialogue."""

    def test_attente_interactive_journalisee_si_dialogue_et_pane_inerte(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%42")
        with mock.patch.object(panes, "alive", return_value=True), \
             mock.patch.object(panes, "busy", return_value=False), \
             mock.patch.object(panes, "capture", return_value="Quick safety check: ..."):
            controle.tick(cid)
        evenements = journal.lire_evenements(cid, "attente-interactive")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], tid)

    def test_pas_dattente_interactive_si_pane_occupe(self):
        # busy() rend True -> une session qui travaille réellement, même silencieuse
        # à l'œil, n'est jamais confondue avec un dialogue bloquant.
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%42")
        with mock.patch.object(panes, "alive", return_value=True), \
             mock.patch.object(panes, "busy", return_value=True), \
             mock.patch.object(panes, "capture", return_value="Quick safety check: ..."):
            controle.tick(cid)
        self.assertEqual(journal.lire_evenements(cid, "attente-interactive"), [])

    def test_pas_dattente_interactive_sans_marqueur_de_dialogue(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%42")
        with mock.patch.object(panes, "alive", return_value=True), \
             mock.patch.object(panes, "busy", return_value=False), \
             mock.patch.object(panes, "capture", return_value="rien de special ici"):
            controle.tick(cid)
        self.assertEqual(journal.lire_evenements(cid, "attente-interactive"), [])

    def test_attente_interactive_ne_se_reecrit_pas_tant_que_le_dialogue_persiste(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%42")
        with mock.patch.object(panes, "alive", return_value=True), \
             mock.patch.object(panes, "busy", return_value=False), \
             mock.patch.object(panes, "capture", return_value="Quick safety check: ..."):
            controle.tick(cid)
            controle.tick(cid)
        self.assertEqual(len(journal.lire_evenements(cid, "attente-interactive")), 1)

    def test_attente_interactive_redeclenche_apres_resolution(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%42")
        with mock.patch.object(panes, "alive", return_value=True), \
             mock.patch.object(panes, "busy", return_value=False), \
             mock.patch.object(panes, "capture", return_value="Quick safety check: ..."):
            controle.tick(cid)
        with mock.patch.object(panes, "alive", return_value=True), \
             mock.patch.object(panes, "busy", return_value=True), \
             mock.patch.object(panes, "capture", return_value="esc to interrupt"):
            controle.tick(cid)
        with mock.patch.object(panes, "alive", return_value=True), \
             mock.patch.object(panes, "busy", return_value=False), \
             mock.patch.object(panes, "capture", return_value="Quick safety check: ..."):
            controle.tick(cid)
        self.assertEqual(len(journal.lire_evenements(cid, "attente-interactive")), 2)


class TestTickPropagation(ControleTestCase):
    def test_propagate_failures_est_appele(self):
        cid = self._chantier()
        a = self._task(cid, titre="a")
        b = self._task(cid, titre="b", depends_on=[a])
        self._set_task(a, state="failed")
        controle.tick(cid)
        self.assertEqual(store.load()["taches"][b]["state"], "blocked")


class TestTickUnblockPropagated(ControleTestCase):
    """Sequence reelle qui a motive ce correctif, observee sur ~/.claude/ordo : t-01
    meurt, tick() bloque t-02 (qui en depend) en cascade ; t-01 est relancee et reussit
    vraiment, coche sa checklist ; sans la levee, t-02 restait bloquee pour toujours."""

    def test_tick_relance_ce_quil_avait_bloque_par_propagation(self):
        cid = self._chantier()
        a = self._task(cid, titre="a", checklist=["fait"])
        b = self._task(cid, titre="b", depends_on=[a])
        self._set_task(a, state="failed")

        controle.tick(cid)
        self.assertEqual(store.load()["taches"][b]["state"], "blocked")

        # a est relancee, reussit vraiment : done + checklist cochee.
        chantier.check(a, "c1")
        self._set_task(a, state="done", error=None)
        result = controle.tick(cid)

        state = store.load()
        self.assertEqual(state["taches"][b]["state"], "queued")
        self.assertIsNone(state["taches"][b]["error"])
        self.assertIsNone(state["taches"][b]["blockedCause"])

        events = result["chantiers"][cid]["events"]
        self.assertTrue(any("unblocked" in e for e in events))
        entries = journal.read(cid)
        self.assertTrue(any("unblocked" in e["texte"] and e["auteur"] == "ORDO" for e in entries))

    def test_tick_ne_debloque_pas_une_tache_bloquee_pour_sa_propre_raison(self):
        # LE point sur lequel la levee est jugee : un pane mort (raison propre a la
        # tache) ne doit jamais etre releve automatiquement par la levee de blocage par
        # propagation, meme sans aucune dependance declaree (le cas le plus favorable a
        # un faux positif : le predicat de dependances est trivialement satisfait).
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id="%999", started_ago=999.0)
        with mock.patch.object(panes, "alive", return_value=False), \
             mock.patch.object(controle, "PANE_DEAD_GRACE_S", 1.0):
            controle.tick(cid)
        self.assertEqual(store.load()["taches"][tid]["state"], "blocked")
        self.assertIn(controle.PANE_MORT_RAISON, store.load()["taches"][tid]["error"])

        # un second tick ne doit rien y changer : ce blocage n'est pas une propagation.
        controle.tick(cid)
        task = store.load()["taches"][tid]
        self.assertEqual(task["state"], "blocked")
        self.assertIn(controle.PANE_MORT_RAISON, task["error"])


class TestTickCapteur(ControleTestCase):
    def test_capteur_du_est_execute(self):
        cid = self._chantier()
        src = self._python_sensor("sensor.py", [{"name": "m", "value": 1}], [])
        capteur.install(cid, src)
        controle.tick(cid)
        self.assertEqual(len(store.load()["chantiers"][cid]["capteur"]["runs"]), 1)

    def test_deux_echecs_consecutifs_produisent_un_evenement_de_reveil(self):
        cid = self._chantier()
        src = self._python_failing()
        capteur.install(cid, src)
        with store.locked() as state:
            state["chantiers"][cid]["capteur"]["adopted"] = True
        with mock.patch.object(controle.capteur, "due", return_value=True):
            controle.tick(cid)
            result = controle.tick(cid)
        events = result["chantiers"][cid]["events"]
        self.assertTrue(any("waking" in e for e in events))


class TestTickExpireDue(ControleTestCase):
    def test_proposition_expiree_est_acceptee_et_journalisee(self):
        cid = self._chantier()
        with store.locked() as state:
            pid = store.next_id(state, "proposition")
            state["propositions"][pid] = {
                "id": pid, "chantier": cid,
                "taches": [{"ref": "n1", "titre": "t", "prompt": "p",
                            "dependsOn": [], "touches": [], "checklist": []}],
                "state": "pending",
                "proposedAt": store.now(),
                "deadline": _iso_ago(1),  # deja passe
                "decidedAt": None, "refus": None,
            }
        result = controle.tick(cid)
        state = store.load()
        self.assertEqual(state["propositions"][pid]["state"], "accepted")
        self.assertEqual(len(state["taches"]), 1)
        events = result["chantiers"][cid]["events"]
        self.assertTrue(any("auto-accepted" in e for e in events))


class TestTickJournal(ControleTestCase):
    def test_evenements_journalises_sous_ordo(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._set_task(tid, state="done", report={"state": "done", "touched": []})
        # forcer un evenement reel : on part d'une tache running avec rapport a lire
        tid2 = self._task(cid, titre="b")
        self._make_running(tid2)
        self._write_report(tid2, {"task": tid2, "state": "done", "note": "ok", "touched": []})
        controle.tick(cid)
        entries = journal.read(cid)
        self.assertTrue(entries)
        for e in entries:
            self.assertEqual(e["auteur"], "ORDO")

    def test_last_event_only_updated_when_events_happened(self):
        cid = self._chantier()
        self.assertIsNone(store.load()["chantiers"][cid]["lastEvent"])
        controle.tick(cid)
        self.assertIsNone(store.load()["chantiers"][cid]["lastEvent"], "rien ne s'est passe")

        tid = self._task(cid)
        self._make_running(tid)
        self._write_report(tid, {"task": tid, "state": "done", "note": "ok", "touched": []})
        controle.tick(cid)
        self.assertIsNotNone(store.load()["chantiers"][cid]["lastEvent"])

    def test_evenement_purement_repetitif_ne_rafraichit_pas_last_event(self):
        # docs/diagnostic-envoi.md : lastEvent est rafraîchi dès qu'au moins un
        # événement est produit, sans distinguer un fait neuf d'un fait déjà journalisé
        # au tour précédent. Une dérive de scope non corrigée se recalcule à l'identique
        # à chaque tick (étape 7) : c'est un exemple réel, indépendant du bug des
        # questions dupliquées déjà refermé par report.clear(), d'un événement purement
        # répétitif qui désarmerait control-round (WAKE_IDLE_AFTER_S) s'il continuait à
        # repousser lastEvent indéfiniment.
        cid = self._chantier()
        tid = self._task(cid, touches=["dedans"])
        self._set_task(tid, state="done", report={"state": "done", "touched": ["dehors"]})

        controle.tick(cid)
        premier = store.load()["chantiers"][cid]["lastEvent"]
        self.assertIsNotNone(premier, "la derive est neuve au premier tour, lastEvent doit bouger")

        sentinelle = "2000-01-01T00:00:00Z"
        with store.locked() as state:
            state["chantiers"][cid]["lastEvent"] = sentinelle
        controle.tick(cid)  # même dérive, rien de neuf
        self.assertEqual(
            store.load()["chantiers"][cid]["lastEvent"], sentinelle,
            "un evenement identique au tour precedent ne doit pas rafraichir lastEvent",
        )


class TestTickReturnShape(ControleTestCase):
    def test_tick_sans_argument_traite_tous_les_chantiers_ouverts(self):
        cid_a = self._chantier(nom="a")
        cid_b = self._chantier(nom="b")
        result = controle.tick()
        self.assertIn(cid_a, result["chantiers"])
        self.assertIn(cid_b, result["chantiers"])

    def test_tick_ignore_les_chantiers_clos(self):
        cid_a = self._chantier(nom="a")
        chantier.close(cid_a)
        cid_b = self._chantier(nom="b")
        result = controle.tick()
        self.assertNotIn(cid_a, result["chantiers"])
        self.assertIn(cid_b, result["chantiers"])

    def test_tick_avec_chantier_id_ne_traite_que_celui_la(self):
        cid_a = self._chantier(nom="a")
        cid_b = self._chantier(nom="b")
        result = controle.tick(cid_a)
        self.assertIn(cid_a, result["chantiers"])
        self.assertNotIn(cid_b, result["chantiers"])


class TestTickChantierIsolation(ControleTestCase):
    """LE defaut a ne pas reproduire : une exception levee par un chantier casse ne doit
    jamais empecher la reconciliation des autres."""

    def test_un_chantier_casse_ninterrompt_pas_les_autres(self):
        broken = self._chantier(nom="broken")
        healthy = self._chantier(nom="healthy")

        t_broken = self._task(broken, titre="cassee")
        self._make_running(t_broken, pane_id="%broken-pane")

        t_healthy = self._task(healthy, titre="saine")
        self._make_running(t_healthy)
        self._write_report(
            t_healthy, {"task": t_healthy, "state": "done", "note": "ok", "touched": []}
        )

        def _alive_side_effect(pane_id):
            if pane_id == "%broken-pane":
                raise RuntimeError("commande tmux échouée (simulation de test)")
            return True

        with mock.patch.object(panes, "alive", side_effect=_alive_side_effect):
            result = controle.tick()

        self.assertIsNotNone(result["chantiers"][broken]["error"])
        self.assertIsNone(result["chantiers"][healthy]["error"])
        self.assertEqual(store.load()["taches"][t_healthy]["state"], "done")

        # le chantier casse laisse une trace au journal, sous ORDO
        entries = journal.read(broken)
        self.assertTrue(any("failed" in e["texte"] for e in entries))
        self.assertTrue(all(e["auteur"] == "ORDO" for e in entries))


if __name__ == "__main__":
    unittest.main()


class TestCompaction(ControleTestCase):
    """La compaction automatique d'une exécutante au contexte trop lourd.

    Mesuré sur cinquante-neuf sessions réelles de deux chantiers : le nombre de tours et
    le contexte portés sont corrélés (r = 0,92) mais pas assez pour servir de proxy l'un
    à l'autre -- le contexte par tour varie d'un facteur 5 d'une session à l'autre. Un
    seuil en tours en a raté 16 sur 59, qui atteignaient un gros contexte en peu de tours.
    Le déclencheur lit donc le CONTEXTE du dernier tour (voir usage.SEUIL_CONTEXTE), et
    les tours ne servent plus qu'à détecter qu'un nouveau tour est arrivé depuis la
    dernière compaction.
    """

    def _executante(self, pane: str = "%1", compactee_a: int | None = None):
        """Une tâche qui tourne. Rend son identifiant."""
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid, pane_id=pane)
        champs = {"claudeSessionId": "s-" + tid}
        if compactee_a is not None:
            champs["compactedAtTurn"] = compactee_a
            champs["compactions"] = 1
        self._set_task(tid, **champs)
        return cid, tid

    def _tick(self, cid, mesures, busy=False, seuil=75000):
        """Lance la compaction avec des mesures simulées : {task_id: (tours, contexte)}."""
        def faux_usage(task):
            m = mesures.get(task["id"])
            if m is None:
                return None
            tours, contexte = m
            return {"turns": tours, "dernierContexte": contexte, "input": 0, "output": 0,
                    "cacheCreation": 0, "cacheRead": 0}
        envois = []
        with mock.patch.object(controle.usage, "pour", side_effect=faux_usage), \
             mock.patch.object(controle.panes, "busy", return_value=busy), \
             mock.patch.object(controle.panes, "send",
                               side_effect=lambda p, t: envois.append((p, t))), \
             mock.patch.object(controle, "SEUIL_CONTEXTE", seuil):
            evenements = controle._compacter(cid)
        return envois, evenements

    def test_une_executante_au_dela_du_seuil_recoit_la_compaction(self):
        cid, tid = self._executante()
        envois, evenements = self._tick(cid, {tid: (80, 80000)})
        self.assertEqual(envois, [("%1", "/compact")])
        self.assertTrue(any("compact" in e for e in evenements))

    def test_une_executante_en_deca_du_seuil_est_laissee_tranquille(self):
        cid, tid = self._executante()
        envois, _ = self._tick(cid, {tid: (80, 70000)})
        self.assertEqual(envois, [])

    def test_une_session_courte_a_gros_contexte_est_compactee(self):
        # Exactement le cas que l'ancien seuil en tours ratait : 10 tours, très loin des
        # 75 d'avant, mais un contexte déjà au-delà du seuil.
        cid, tid = self._executante()
        envois, _ = self._tick(cid, {tid: (10, 200000)})
        self.assertEqual(envois, [("%1", "/compact")])

    def test_beaucoup_de_tours_mais_petit_contexte_n_est_pas_compacte(self):
        # Le garde-fou du garde-fou : si le déclencheur repassait au comptage de tours,
        # ce test échouerait, puisque 500 tours dépassait déjà largement l'ancien seuil
        # de 75.
        cid, tid = self._executante()
        envois, _ = self._tick(cid, {tid: (500, 20000)})
        self.assertEqual(envois, [])

    def test_le_meme_palier_ne_declenche_pas_deux_fois(self):
        # Sans cette mémoire, chaque battement de la veille renverrait /compact à une
        # session déjà compactée, et la session ne ferait plus que se compacter.
        cid, tid = self._executante()
        self._tick(cid, {tid: (80, 80000)})
        etat = store.load()["taches"][tid]
        self.assertEqual(etat["compactions"], 1)
        self.assertEqual(etat["compactedAtTurn"], 80)
        # Le transcript n'a pas avancé (toujours au tour 80) : même si le contexte lu
        # reste au-delà du seuil, rien n'est renvoyé.
        envois, _ = self._tick(cid, {tid: (80, 80000)})
        self.assertEqual(envois, [])

    def test_le_palier_suivant_declenche_une_seconde_compaction(self):
        cid, tid = self._executante(compactee_a=80)
        envois, _ = self._tick(cid, {tid: (90, 78000)})
        self.assertEqual(envois, [("%1", "/compact")])
        self.assertEqual(store.load()["taches"][tid]["compactions"], 2)

    def test_un_pane_occupe_est_epargne_et_retente_plus_tard(self):
        # Envoyer /compact au milieu d'un outil couperait le travail en cours. Le
        # battement suivant retentera, et rien n'est marqué : la tâche reste due.
        cid, tid = self._executante()
        envois, _ = self._tick(cid, {tid: (80, 80000)}, busy=True)
        self.assertEqual(envois, [])
        self.assertEqual(store.load()["taches"][tid].get("compactions", 0), 0)
        envois, _ = self._tick(cid, {tid: (80, 80000)}, busy=False)
        self.assertEqual(envois, [("%1", "/compact")])

    def test_un_seuil_nul_desactive_tout(self):
        cid, tid = self._executante()
        envois, _ = self._tick(cid, {tid: (500, 500000)}, seuil=0)
        self.assertEqual(envois, [])

    def test_une_tache_qui_ne_tourne_pas_n_est_jamais_compactee(self):
        cid, tid = self._executante()
        self._set_task(tid, state="done")
        envois, _ = self._tick(cid, {tid: (200, 200000)})
        self.assertEqual(envois, [])

    def test_un_transcript_illisible_ne_fait_rien_echouer(self):
        # usage.pour rend None quand il ne trouve pas le transcript. Sans transcript on
        # ne sait pas quel contexte la session porte, et on ne compacte pas au hasard.
        cid, tid = self._executante()
        envois, _ = self._tick(cid, {})
        self.assertEqual(envois, [])

    def test_une_tache_sans_pane_est_ignoree(self):
        cid, tid = self._executante(pane=None)
        self._set_task(tid, paneId=None)
        envois, _ = self._tick(cid, {tid: (200, 200000)})
        self.assertEqual(envois, [])

    def test_le_tick_declenche_la_compaction(self):
        # Le câblage : sans cet appel dans tick(), _compacter() serait du code mort que
        # ses tests vérifieraient consciencieusement sans que rien ne l'appelle.
        cid, tid = self._executante()
        envois = []
        with mock.patch.object(controle.usage, "pour",
                               return_value={"turns": 200, "dernierContexte": 200000,
                                             "input": 0, "output": 0,
                                             "cacheCreation": 0, "cacheRead": 0}), \
             mock.patch.object(controle.panes, "busy", return_value=False), \
             mock.patch.object(controle.panes, "alive", return_value=True), \
             mock.patch.object(controle.panes, "send",
                               side_effect=lambda p, t: envois.append((p, t))):
            resultat = controle.tick(cid)
        self.assertEqual(envois, [("%1", "/compact")])
        self.assertTrue(any("compacted" in e for e in resultat["chantiers"][cid]["events"]))


# ===========================================================================
# deduce_current_item (brief t-25, volet 2)
# ===========================================================================


class TestDeduceCurrentItem(unittest.TestCase):
    """Fonction pure : pas d'ORDO_HOME, pas de store, des dicts de tâche construits à
    la main. Les trois garde-fous du brief, chacun dans son propre test."""

    def _tache(self, *, state="running", current_item=None, checklist=()) -> dict:
        return {"state": state, "currentItem": current_item, "checklist": list(checklist)}

    def test_rien_sur_une_tache_qui_nest_pas_running(self):
        t = self._tache(state="blocked", checklist=[{"id": "c1", "done": True}])
        self.assertIsNone(controle.deduce_current_item(t))

    def test_une_declaration_explicite_nest_jamais_ecrasee(self):
        # Le cas mesuré sur trois exécutantes réelles : le seul des trois garde-fous où
        # une régression serait invisible à l'œil (le champ resterait rempli, mais avec
        # la mauvaise valeur).
        t = self._tache(
            current_item="c3",
            checklist=[
                {"id": "c1", "done": True},
                {"id": "c2", "done": False},
                {"id": "c3", "done": False},
            ],
        )
        self.assertIsNone(controle.deduce_current_item(t))

    def test_deduit_le_premier_critere_non_coche(self):
        t = self._tache(
            checklist=[
                {"id": "c1", "done": True},
                {"id": "c2", "done": False},
                {"id": "c3", "done": False},
            ],
        )
        self.assertEqual(controle.deduce_current_item(t), "c2")

    def test_rend_none_quand_tout_est_coche(self):
        t = self._tache(
            checklist=[{"id": "c1", "done": True}, {"id": "c2", "done": True}],
        )
        self.assertIsNone(controle.deduce_current_item(t))

    def test_rien_sur_une_tache_sans_checklist(self):
        self.assertIsNone(controle.deduce_current_item(self._tache()))


# ===========================================================================
# wake_reasons : famille checklist-silent (brief t-25, volet 1)
# ===========================================================================


class TestChecklistSilentWakeReasons(ControleTestCase):
    def _seed_watch(self, cid: str, tid: str, since_ago: float, signature=None) -> None:
        with store.locked() as state:
            ch = state["chantiers"][cid]
            task = state["taches"][tid]
            ch.setdefault("checklistWatch", {})[tid] = {
                "signature": signature if signature is not None else controle._checklist_signature(task),
                "since": _iso_ago(since_ago),
            }

    def test_une_tache_sans_checklist_nest_jamais_signalee(self):
        # Garde-fou 2 du brief : même avec un cache figé de longue date, une tâche sans
        # checklist ne peut pas produire ce motif (sa barre ne peut pas bouger).
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        self._seed_watch(cid, tid, since_ago=9999.0, signature=())
        with mock.patch.object(controle, "CHECKLIST_SILENT_AFTER_S", 1.0):
            reasons = controle.wake_reasons(cid)
        self.assertFalse(any(r["kind"] == "checklist-silent" for r in reasons))

    def test_pas_de_signal_avant_le_seuil(self):
        cid = self._chantier()
        tid = self._task(cid, checklist=["a", "b"])
        self._make_running(tid)
        self._seed_watch(cid, tid, since_ago=5.0)
        with mock.patch.object(controle, "CHECKLIST_SILENT_AFTER_S", 600.0):
            reasons = controle.wake_reasons(cid)
        self.assertFalse(any(r["kind"] == "checklist-silent" for r in reasons))

    def test_signal_quand_la_barre_est_figee_au_dela_du_seuil(self):
        cid = self._chantier()
        tid = self._task(cid, checklist=["a", "b"])
        self._make_running(tid)
        self._seed_watch(cid, tid, since_ago=900.0)
        with mock.patch.object(controle, "CHECKLIST_SILENT_AFTER_S", 600.0):
            reasons = controle.wake_reasons(cid)
        motifs = [r for r in reasons if r["kind"] == "checklist-silent"]
        self.assertEqual(len(motifs), 1)
        self.assertEqual(motifs[0]["task"], tid)
        # Marge de quelques secondes : le temps réel s'écoule entre le seed et l'appel.
        self.assertRegex(motifs[0]["detail"], r"checklist unchanged for 90\d s")

    def test_le_point_de_reference_est_le_dernier_mouvement_pas_le_lancement(self):
        # Exemple exact du brief : une tâche lancée depuis 45 min qui a coché un item à
        # la 30e minute (donc "since" = 15 min) doit se déclencher à un seuil de 10 min,
        # PAS être jugée calme depuis les 45 minutes écoulées depuis le lancement.
        cid = self._chantier()
        tid = self._task(cid, checklist=["a", "b"])
        self._make_running(tid, started_ago=45 * 60)
        self._seed_watch(cid, tid, since_ago=15 * 60)
        with mock.patch.object(controle, "CHECKLIST_SILENT_AFTER_S", 10 * 60):
            reasons = controle.wake_reasons(cid)
        self.assertTrue(any(r["kind"] == "checklist-silent" for r in reasons))

    def test_pas_de_signal_si_le_mouvement_est_recent_meme_tache_ancienne(self):
        # Contrôle négatif du test précédent : même tâche lancée il y a 45 minutes, mais
        # la checklist a bougé il y a 2 minutes seulement -> aucun signal, alors qu'une
        # référence sur le lancement en produirait un à tort.
        cid = self._chantier()
        tid = self._task(cid, checklist=["a", "b"])
        self._make_running(tid, started_ago=45 * 60)
        self._seed_watch(cid, tid, since_ago=2 * 60)
        with mock.patch.object(controle, "CHECKLIST_SILENT_AFTER_S", 10 * 60):
            reasons = controle.wake_reasons(cid)
        self.assertFalse(any(r["kind"] == "checklist-silent" for r in reasons))

    def test_le_detail_porte_le_critere_declare_en_cours(self):
        cid = self._chantier()
        tid = self._task(cid, checklist=["a", "b"])
        self._make_running(tid)
        self._set_task(tid, currentItem="c2")
        self._seed_watch(cid, tid, since_ago=900.0)
        with mock.patch.object(controle, "CHECKLIST_SILENT_AFTER_S", 600.0):
            reasons = controle.wake_reasons(cid)
        motif = next(r for r in reasons if r["kind"] == "checklist-silent")
        self.assertIn("c2", motif["detail"])

    def test_le_detail_ne_mentionne_rien_sans_critere_declare(self):
        cid = self._chantier()
        tid = self._task(cid, checklist=["a", "b"])
        self._make_running(tid)
        self._seed_watch(cid, tid, since_ago=900.0)
        with mock.patch.object(controle, "CHECKLIST_SILENT_AFTER_S", 600.0):
            reasons = controle.wake_reasons(cid)
        motif = next(r for r in reasons if r["kind"] == "checklist-silent")
        self.assertNotIn("declared on", motif["detail"])


class TestChecklistSilentWakeNew(ControleTestCase):
    """wake_new() sert le motif une fois par période de silence, jamais à chaque tour
    (contrainte 3 du brief), et un nouveau signal redevient légitime une fois que la
    checklist est repartie puis s'est figée à nouveau (contrainte 4)."""

    def test_un_seul_signal_tant_que_rien_ne_bouge_puis_de_nouveau_apres_un_mouvement(self):
        cid = self._chantier()
        tid = self._task(cid, checklist=["a", "b"])
        self._make_running(tid)
        with mock.patch.object(controle, "CHECKLIST_SILENT_AFTER_S", 1.0):
            with store.locked() as state:
                ch = state["chantiers"][cid]
                task = state["taches"][tid]
                ch.setdefault("checklistWatch", {})[tid] = {
                    "signature": controle._checklist_signature(task), "since": _iso_ago(5.0),
                }
            premiers = controle.wake_new(cid)
            self.assertTrue(any(m["kind"] == "checklist-silent" for m in premiers))

            # Même état, tour suivant : déjà servi, ne doit pas revenir.
            suivants = controle.wake_new(cid)
            self.assertFalse(any(m["kind"] == "checklist-silent" for m in suivants))

            # La checklist bouge : nouvelle signature, "since" tout frais -> pas encore
            # au-delà du seuil, rien ne doit se déclencher.
            chantier.check(tid, "c1")
            with store.locked() as state:
                task = state["taches"][tid]
                state["chantiers"][cid]["checklistWatch"][tid] = {
                    "signature": controle._checklist_signature(task), "since": store.now(),
                }
            frais = controle.wake_new(cid)
            self.assertFalse(any(m["kind"] == "checklist-silent" for m in frais))

            # Elle se fige à nouveau assez longtemps : nouveau signal légitime, pas
            # avalé par le souvenir du premier (clé différente, nouvelle signature).
            with store.locked() as state:
                state["chantiers"][cid]["checklistWatch"][tid]["since"] = _iso_ago(5.0)
            reveil = controle.wake_new(cid)
            self.assertTrue(any(m["kind"] == "checklist-silent" for m in reveil))


class TestChecklistWatchCache(ControleTestCase):
    """tick() maintient le cache checklistWatch ; wake_reasons() ne fait que le lire
    (voir son docstring, famille 8)."""

    def test_tick_cree_une_entree_pour_une_tache_running_a_checklist(self):
        cid = self._chantier()
        tid = self._task(cid, checklist=["a"])
        self._make_running(tid)
        controle.tick(cid)
        watch = store.load()["chantiers"][cid].get("checklistWatch") or {}
        self.assertIn(tid, watch)

    def test_tick_ne_reinitialise_pas_since_quand_rien_ne_bouge(self):
        # Bug réel trouvé et corrigé pendant ce chantier : state.json n'a pas de
        # tuple, donc une signature stockée comme tuple revient en liste après le
        # passage par store.locked() (JSON) -- comparée telle quelle à la signature
        # fraîchement recalculée (un tuple), elle paraissait "changée" à CHAQUE tick,
        # y compris quand la checklist n'avait pas bougé. Un sommeil réel entre les
        # deux tick() est nécessaire : sans lui, deux appels dans la même seconde
        # auraient masqué le bug (store.now() est horodaté à la seconde).
        cid = self._chantier()
        tid = self._task(cid, checklist=["a", "b"])
        self._make_running(tid)
        controle.tick(cid)
        premier_since = store.load()["chantiers"][cid]["checklistWatch"][tid]["since"]
        time.sleep(1.1)
        controle.tick(cid)
        second_since = store.load()["chantiers"][cid]["checklistWatch"][tid]["since"]
        self.assertEqual(premier_since, second_since)

    def test_tick_ne_cree_rien_pour_une_tache_sans_checklist(self):
        cid = self._chantier()
        tid = self._task(cid)
        self._make_running(tid)
        controle.tick(cid)
        watch = store.load()["chantiers"][cid].get("checklistWatch") or {}
        self.assertNotIn(tid, watch)

    def test_tick_change_la_signature_quand_un_item_est_coche(self):
        cid = self._chantier()
        tid = self._task(cid, checklist=["a", "b"])
        self._make_running(tid)
        controle.tick(cid)
        avant = store.load()["chantiers"][cid]["checklistWatch"][tid]["signature"]
        chantier.check(tid, "c1")
        controle.tick(cid)
        apres = store.load()["chantiers"][cid]["checklistWatch"][tid]["signature"]
        self.assertNotEqual(avant, apres)

    def test_tick_retire_lentree_dune_tache_qui_nest_plus_running(self):
        cid = self._chantier()
        tid = self._task(cid, checklist=["a"])
        self._make_running(tid)
        controle.tick(cid)
        self.assertIn(tid, store.load()["chantiers"][cid]["checklistWatch"])
        self._set_task(tid, state="done")
        controle.tick(cid)
        watch = store.load()["chantiers"][cid].get("checklistWatch") or {}
        self.assertNotIn(tid, watch)


# ===========================================================================
# CHECKLIST_SILENT_AFTER_S : seuil nommé, surchargeable par variable d'environnement
# (brief t-25, contrainte 1 -- même convention que usage.SEUIL_CONTEXTE)
# ===========================================================================


class TestChecklistSilentAfterSeuil(unittest.TestCase):
    """Lu à l'import du module, comme usage.SEUIL_CONTEXTE (voir test_usage.py) : chaque
    test change l'environnement puis recharge controle, et restaure les deux en sortant
    pour ne pas polluer les tests suivants."""

    def setUp(self) -> None:
        self._avant = os.environ.get("ORDO_CHECKLIST_SILENT_AFTER_S")

    def tearDown(self) -> None:
        if self._avant is None:
            os.environ.pop("ORDO_CHECKLIST_SILENT_AFTER_S", None)
        else:
            os.environ["ORDO_CHECKLIST_SILENT_AFTER_S"] = self._avant
        importlib.reload(controle)

    def test_valeur_par_defaut_de_dix_minutes(self):
        os.environ.pop("ORDO_CHECKLIST_SILENT_AFTER_S", None)
        importlib.reload(controle)
        self.assertEqual(controle.CHECKLIST_SILENT_AFTER_S, 600.0)

    def test_la_variable_d_environnement_surcharge_le_seuil(self):
        os.environ["ORDO_CHECKLIST_SILENT_AFTER_S"] = "42"
        importlib.reload(controle)
        self.assertEqual(controle.CHECKLIST_SILENT_AFTER_S, 42.0)
