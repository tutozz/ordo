"""Tests unitaires de ordo/report.py.

Isoles par ORDO_HOME, comme test_store.py et test_chantier.py. Les tests de parse()
et apply() n'ont pas besoin de disque : ils construisent des dicts a la main, sur le
meme modele que TestPropagateFailures dans test_chantier.py.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import report, store


class ReportTestCase(unittest.TestCase):
    """Cree un ORDO_HOME temporaire par test et restaure l'environnement apres coup."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-report-test-")
        self._prev_home = os.environ.get("ORDO_HOME")
        os.environ["ORDO_HOME"] = self._tmp

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("ORDO_HOME", None)
        else:
            os.environ["ORDO_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _task(self, task_id: str = "t-01", state: str = "running") -> dict:
        return {
            "id": task_id,
            "chantier": "c-01",
            "titre": "titre",
            "prompt": "prompt",
            "state": state,
            "dependsOn": [],
            "touches": [],
            "checklist": [
                {"id": "c1", "label": "tests verts", "done": False},
                {"id": "c2", "label": "doc a jour", "done": False},
            ],
            "priority": 0,
            "attempts": 0,
            "model": None,
            "paneId": "%1",
            "cwd": None,
            "createdAt": store.now(),
            "startedAt": store.now(),
            "finishedAt": None,
            "lastReportAt": None,
            "report": None,
            "error": None,
            "notes": [],
        }

    def _state_with_task(self, task_id: str = "t-01", state: str = "running") -> dict:
        return {"taches": {task_id: self._task(task_id, state)}}


class TestPath(ReportTestCase):
    def test_path_is_under_the_chantier_subdirectory(self):
        p = report.path("t-04", "c-01")
        self.assertEqual(p, store.home() / "reports" / "c-01" / "t-04.json")

    def test_path_cree_le_repertoire_du_chantier(self):
        """Une executante ecrit son rapport elle-meme : le repertoire doit preexister."""
        p = report.path("t-04", "c-07")
        self.assertTrue(p.parent.is_dir())

    def test_deux_chantiers_ne_partagent_jamais_un_fichier_de_rapport(self):
        """Le prefixe de chantier est ce qui empeche deux projets de se disputer t-01.

        Sans lui, deux chantiers d'un meme ORDO_HOME ecrivent dans reports/t-01.json ;
        le second rapport ecrase le premier et Ordo lit le mauvais signal.
        """
        a = report.path("t-01", "c-01")
        b = report.path("t-01", "c-02")
        self.assertNotEqual(a, b)
        a.write_text('{"state": "done", "note": "chantier A"}', encoding="utf-8")
        b.write_text('{"state": "blocked", "note": "chantier B"}', encoding="utf-8")
        self.assertEqual(report.read("t-01", "c-01")["state"], "done")
        self.assertEqual(report.read("t-01", "c-02")["state"], "blocked")


class TestReadAndClear(ReportTestCase):
    def test_read_returns_none_when_absent(self):
        self.assertIsNone(report.read("t-99", "c-01"))

    def test_read_returns_parsed_dict_when_present(self):
        report.path("t-01", "c-01").write_text(
            '{"task": "t-01", "state": "done", "note": "ras", "checked": ["c1"]}',
            encoding="utf-8",
        )
        parsed = report.read("t-01", "c-01")
        self.assertEqual(parsed["state"], "done")
        self.assertEqual(parsed["checked"], ["c1"])

    def test_read_raises_on_illisible_content(self):
        report.path("t-01", "c-01").write_text(
            "ceci n'est pas du JSON du tout", encoding="utf-8"
        )
        with self.assertRaises(report.RapportError):
            report.read("t-01", "c-01")

    def test_clear_removes_file(self):
        p = report.path("t-01", "c-01")
        p.write_text("{}", encoding="utf-8")
        report.clear("t-01", "c-01")
        self.assertFalse(p.exists())

    def test_clear_ne_touche_pas_au_meme_id_dans_un_autre_chantier(self):
        garde = report.path("t-01", "c-02")
        garde.write_text("{}", encoding="utf-8")
        report.clear("t-01", "c-01")
        self.assertTrue(garde.exists())

    def test_clear_tolerates_absent_file(self):
        report.clear("t-99", "c-01")  # ne doit pas lever


class TestParseWellFormed(unittest.TestCase):
    def test_parse_minimal_valid_json(self):
        r = report.parse('{"state": "done"}')
        self.assertEqual(r["state"], "done")
        self.assertEqual(r["checked"], [])
        self.assertEqual(r["touched"], [])
        self.assertEqual(r["note"], "")
        self.assertIsNone(r["question"])

    def test_parse_full_payload(self):
        raw = (
            '{"task": "t-01", "state": "blocked", "note": "manque une variable env",'
            ' "checked": ["c1", "c2"], "question": null, "touched": ["app/x.py"]}'
        )
        r = report.parse(raw)
        self.assertEqual(r["task"], "t-01")
        self.assertEqual(r["state"], "blocked")
        self.assertEqual(r["note"], "manque une variable env")
        self.assertEqual(r["checked"], ["c1", "c2"])
        self.assertEqual(r["touched"], ["app/x.py"])


class TestParseDeformations(unittest.TestCase):
    """Un modele reel entoure le JSON de prose, change la casse des cles, ecrit une
    chaine la ou une liste est attendue, laisse une virgule finale, ou utilise des
    guillemets typographiques. parse() doit encaisser chacune de ces deformations.
    """

    def test_prose_before_and_after_json(self):
        raw = (
            "Voici mon rapport de tache, j'ai fini le travail demande.\n"
            '{"state": "done", "note": "ok"}\n'
            "Merci de verifier."
        )
        r = report.parse(raw)
        self.assertEqual(r["state"], "done")

    def test_markdown_fence_with_json_tag(self):
        raw = 'Rapport :\n```json\n{"state": "done", "note": "termine"}\n```\n'
        r = report.parse(raw)
        self.assertEqual(r["state"], "done")
        self.assertEqual(r["note"], "termine")

    def test_markdown_fence_without_language_tag(self):
        raw = '```\n{"state": "progress", "note": "en cours"}\n```'
        r = report.parse(raw)
        self.assertEqual(r["state"], "progress")

    def test_uppercase_keys(self):
        raw = '{"STATE": "DONE", "NOTE": "fini", "CHECKED": ["c1"]}'
        r = report.parse(raw)
        self.assertEqual(r["state"], "done")
        self.assertEqual(r["note"], "fini")
        self.assertEqual(r["checked"], ["c1"])

    def test_mixed_case_keys(self):
        raw = '{"State": "Blocked", "Note": "bloque"}'
        r = report.parse(raw)
        self.assertEqual(r["state"], "blocked")

    def test_checked_as_string_instead_of_list(self):
        raw = '{"state": "done", "checked": "c1"}'
        r = report.parse(raw)
        self.assertEqual(r["checked"], ["c1"])

    def test_touched_as_string_instead_of_list(self):
        raw = '{"state": "done", "touched": "app/x.py"}'
        r = report.parse(raw)
        self.assertEqual(r["touched"], ["app/x.py"])

    def test_trailing_comma_in_object(self):
        raw = '{"state": "done", "note": "ok",}'
        r = report.parse(raw)
        self.assertEqual(r["state"], "done")

    def test_trailing_comma_in_array(self):
        raw = '{"state": "done", "checked": ["c1", "c2",]}'
        r = report.parse(raw)
        self.assertEqual(r["checked"], ["c1", "c2"])

    def test_typographic_quotes(self):
        # forme degradee la plus courante : guillemets courbes partout, y compris
        # autour des cles et des valeurs
        raw = "{“state”: “done”, “note”: “ras”}"
        r = report.parse(raw)
        self.assertEqual(r["state"], "done")
        self.assertEqual(r["note"], "ras")

    def test_state_value_has_surrounding_whitespace(self):
        raw = '{"state": "  done  "}'
        r = report.parse(raw)
        self.assertEqual(r["state"], "done")


class TestParseRejectsIllisible(unittest.TestCase):
    """I2 : parse() leve sur ce qu'il ne comprend pas, il n'invente jamais un etat."""

    def test_no_json_object_at_all(self):
        with self.assertRaises(report.RapportError):
            report.parse("j'ai fini, tout est bon")

    def test_missing_state_field_raises(self):
        with self.assertRaises(report.RapportError):
            report.parse('{"note": "fini", "checked": ["c1"]}')

    def test_unknown_state_value_raises(self):
        with self.assertRaises(report.RapportError):
            report.parse('{"state": "termine"}')

    def test_i2_parse_ne_comble_jamais_state_par_defaut(self):
        # I2 : un parse() indulgent au point d'inventer un state: done par defaut est
        # exactement le defaut a ne pas produire. Ce test echoue si parse() se met a
        # renvoyer un state par defaut au lieu de lever.
        with self.assertRaises(report.RapportError):
            report.parse('{"note": "aucun champ state du tout"}')

    def test_unterminated_json_raises(self):
        with self.assertRaises(report.RapportError):
            report.parse('{"state": "done"')

    def test_top_level_array_raises(self):
        with self.assertRaises(report.RapportError):
            report.parse('["state", "done"]')

    def test_checked_wrong_type_raises(self):
        with self.assertRaises(report.RapportError):
            report.parse('{"state": "done", "checked": 42}')

    def test_note_wrong_type_raises(self):
        with self.assertRaises(report.RapportError):
            report.parse('{"state": "done", "note": 42}')

    def test_empty_string_raises(self):
        with self.assertRaises(report.RapportError):
            report.parse("")


class TestApplyDone(ReportTestCase):
    def test_apply_done_sets_task_state_and_finished_at(self):
        state = self._state_with_task()
        raw = '{"state": "done", "note": "termine", "checked": ["c1"]}'
        events = report.apply(state, "t-01", raw)
        task = state["taches"]["t-01"]
        self.assertEqual(task["state"], "done")
        self.assertIsNotNone(task["finishedAt"])
        self.assertIsNotNone(task["lastReportAt"])
        self.assertTrue(task["checklist"][0]["done"])
        self.assertFalse(task["checklist"][1]["done"])
        self.assertEqual(task["report"]["state"], "done")
        self.assertTrue(events)

    def test_apply_done_checks_multiple_items(self):
        state = self._state_with_task()
        raw = '{"state": "done", "checked": ["c1", "c2"]}'
        report.apply(state, "t-01", raw)
        task = state["taches"]["t-01"]
        self.assertTrue(all(item["done"] for item in task["checklist"]))

    def test_apply_ignores_unknown_checklist_id_without_raising(self):
        state = self._state_with_task()
        raw = '{"state": "done", "checked": ["c-inconnu"]}'
        events = report.apply(state, "t-01", raw)
        self.assertTrue(events)


class TestApplyBlocked(ReportTestCase):
    def test_apply_blocked_sets_error_from_note(self):
        state = self._state_with_task()
        raw = '{"state": "blocked", "note": "manque une cle API"}'
        report.apply(state, "t-01", raw)
        task = state["taches"]["t-01"]
        self.assertEqual(task["state"], "blocked")
        self.assertEqual(task["error"], "manque une cle API")
        self.assertIsNone(task["finishedAt"])


class TestApplyAsking(ReportTestCase):
    def test_apply_asking_sets_waiting_state(self):
        state = self._state_with_task()
        raw = '{"state": "asking", "question": "quelle branche cibler ?"}'
        events = report.apply(state, "t-01", raw)
        task = state["taches"]["t-01"]
        self.assertEqual(task["state"], "waiting")
        self.assertEqual(task["report"]["question"], "quelle branche cibler ?")
        self.assertTrue(any("quelle branche cibler" in e for e in events))


class TestApplyProgress(ReportTestCase):
    def test_apply_progress_does_not_change_task_state(self):
        state = self._state_with_task(state="running")
        raw = '{"state": "progress", "note": "moitie faite"}'
        report.apply(state, "t-01", raw)
        task = state["taches"]["t-01"]
        self.assertEqual(task["state"], "running")
        self.assertIsNotNone(task["lastReportAt"])


class TestApplyConsumesReport(ReportTestCase):
    """La racine du bug des questions dupliquées (docs/diagnostic-envoi.md) : un
    rapport reste sur disque après avoir été appliqué et redevient un signal neuf
    dès que la tâche repasse "running" (cas "asking") ou au tick suivant (cas
    "progress", qui ne change pas l'état). apply() doit donc consommer le fichier
    de rapport qu'il vient de lire, pour toute tâche dont le contenu a été compris.
    """

    def test_apply_supprime_le_fichier_de_rapport_sur_disque(self):
        state = self._state_with_task()
        p = report.path("t-01", "c-01")
        raw = '{"state": "done", "note": "termine"}'
        p.write_text(raw, encoding="utf-8")
        report.apply(state, "t-01", raw)
        self.assertFalse(p.exists())

    def test_un_rapport_asking_applique_ne_redevient_jamais_un_signal_neuf(self):
        # Reproduction 1 du diagnostic : le même rapport "asking", relu à un tick
        # suivant, crée une deuxième question identique. La consommation du
        # fichier après application est ce qui l'empêche.
        state = self._state_with_task()
        p = report.path("t-01", "c-01")
        raw = '{"state": "asking", "question": "quelle branche cibler ?"}'
        p.write_text(raw, encoding="utf-8")
        report.apply(state, "t-01", raw)
        self.assertIsNone(report.read("t-01", "c-01"))

    def test_apply_progress_supprime_aussi_le_rapport(self):
        # La tâche reste "running" après un rapport "progress" : sans
        # consommation, le même rapport serait relu et réappliqué à chaque tick
        # suivant, dupliquant les notes indéfiniment.
        state = self._state_with_task(state="running")
        p = report.path("t-01", "c-01")
        raw = '{"state": "progress", "note": "moitie faite"}'
        p.write_text(raw, encoding="utf-8")
        report.apply(state, "t-01", raw)
        self.assertFalse(p.exists())

    def test_apply_ne_supprime_pas_le_rapport_dun_autre_chantier(self):
        state = self._state_with_task()
        garde = report.path("t-01", "c-02")
        garde.write_text('{"state": "done"}', encoding="utf-8")
        report.apply(state, "t-01", '{"state": "done"}')
        self.assertTrue(garde.exists())

    def test_apply_illisible_conserve_le_fichier_pour_inspection(self):
        # I2 : un rapport illisible bloque la tâche et n'est jamais "appliqué" au
        # sens propre. Contrairement au chemin qui réussit, le fichier reste sur
        # disque : c'est la seule trace de ce que l'exécutante a écrit.
        state = self._state_with_task(state="running")
        p = report.path("t-01", "c-01")
        raw = "n'importe quoi, aucun JSON ici"
        p.write_text(raw, encoding="utf-8")
        report.apply(state, "t-01", raw)
        self.assertTrue(p.exists())


class TestApplyIllisible(ReportTestCase):
    def test_i2_rapport_illisible_bloque_jamais_reussi(self):
        # I2, le plus important du module : un rapport illisible bloque la tache, il
        # ne la laisse jamais passer pour reussie. apply() ne doit ni lever, ni
        # laisser la tache dans un etat "running" silencieux, ni la marquer "done".
        state = self._state_with_task(state="running")
        raw = "n'importe quoi, aucun JSON ici"
        events = report.apply(state, "t-01", raw)
        task = state["taches"]["t-01"]
        self.assertEqual(task["state"], "blocked")
        self.assertNotEqual(task["state"], "done")
        self.assertIn("unreadable", task["error"])
        self.assertTrue(events)

    def test_apply_never_sets_done_on_malformed_state_value(self):
        state = self._state_with_task(state="running")
        raw = '{"state": "presque fini"}'
        report.apply(state, "t-01", raw)
        task = state["taches"]["t-01"]
        self.assertEqual(task["state"], "blocked")

    def test_apply_does_not_lock_state_itself(self):
        # apply() doit operer sur le dict passe, sans jamais appeler store.locked()
        # lui-meme : le motif documente par propagate_failures() dans chantier.py.
        # Si apply() se verrouillait, l'appeler depuis un bloc "with store.locked()"
        # deja ouvert par l'appelant (le motif prevu pour tick()) interbloquerait.
        with store.locked() as state:
            state["taches"]["t-01"] = self._task("t-01")
            report.apply(state, "t-01", '{"state": "done"}')
            self.assertEqual(state["taches"]["t-01"]["state"], "done")


if __name__ == "__main__":
    unittest.main()
