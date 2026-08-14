"""Tests unitaires de ordo/journal.py.

Isoles par ORDO_HOME, comme test_store.py et test_chantier.py. Aucun test ne touche le
vrai ~/.claude/ordo.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import chantier, journal, store


class JournalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-journal-test-")
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

    def _chantier(self, **kw) -> str:
        defaults = dict(objectif="objectif clair", perimetre="dedans", hors_scope="dehors")
        defaults.update(kw)
        return chantier.start(str(self._projet()), **defaults)["id"]


class TestWrite(JournalTestCase):
    def test_write_creates_journal_file(self):
        cid = self._chantier()
        journal.write(cid, "ORDO", "graphe accepte, 8 taches")
        path = store.home() / "journal" / f"{cid}.md"
        self.assertTrue(path.exists())

    def test_write_line_format_matches_spec(self):
        cid = self._chantier()
        journal.write(cid, "ORDO", "graphe accepte, 8 taches")
        raw = (store.home() / "journal" / f"{cid}.md").read_text(encoding="utf-8")
        line = raw.splitlines()[0]
        # heure sur 5 caracteres HH:MM, deux espaces, auteur, deux espaces, texte
        self.assertRegex(line, r"^\d{2}:\d{2}  ORDO  graphe accepte, 8 taches$")

    def test_write_pads_toi_to_align_with_four_letter_authors(self):
        cid = self._chantier()
        journal.write(cid, "USER", "pas touche a la prod")
        raw = (store.home() / "journal" / f"{cid}.md").read_text(encoding="utf-8")
        line = raw.splitlines()[0]
        # ORDO, ORCH et USER font tous 4 caracteres : la colonne reste alignee sans padding
        # pour que le texte de tous les auteurs commence a la meme colonne.
        self.assertRegex(line, r"^\d{2}:\d{2}  USER  pas touche a la prod$")

    def test_write_appends_multiple_entries_in_order(self):
        cid = self._chantier()
        journal.write(cid, "ORDO", "premier fait")
        journal.write(cid, "ORCH", "deuxieme fait")
        journal.write(cid, "USER", "troisieme fait")
        raw = (store.home() / "journal" / f"{cid}.md").read_text(encoding="utf-8")
        lines = raw.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("premier fait", lines[0])
        self.assertIn("deuxieme fait", lines[1])
        self.assertIn("troisieme fait", lines[2])

    def test_write_unknown_chantier_raises(self):
        with self.assertRaises(chantier.ChantierError):
            journal.write("c-99", "ORDO", "texte")

    def test_i11_journal_refuse_auteur_inconnu(self):
        # I11 : le journal distingue ses trois auteurs, ORDO, ORCH, USER. Dans le projet
        # d'origine, le verbe qui laissait l'orchestratrice ecrire un POURQUOI journalisait
        # sous USER : ses arbitrages quittaient la section "journal des decisions" pour
        # "derniers messages humains", et au redemarrage suivant elle relisait ses propres
        # decisions comme des ordres de l'utilisateur. write() doit refuser tout le reste,
        # explicitement, en francais.
        cid = self._chantier()
        with self.assertRaises(chantier.ChantierError) as ctx:
            journal.write(cid, "AUTRE", "texte")
        message = str(ctx.exception)
        self.assertIn("ORDO", message)
        self.assertIn("ORCH", message)
        self.assertIn("USER", message)
        # aucune ligne n'a ete ecrite malgre le refus
        path = store.home() / "journal" / f"{cid}.md"
        self.assertFalse(path.exists() and path.read_text(encoding="utf-8").strip())

    def test_i11_journal_refuse_minuscule_toi(self):
        # variante du meme invariant : la casse compte, "toi" n'est pas "USER".
        cid = self._chantier()
        with self.assertRaises(chantier.ChantierError):
            journal.write(cid, "toi", "texte")

    def test_write_accepts_all_three_valid_authors(self):
        cid = self._chantier()
        for auteur in ("ORDO", "ORCH", "USER"):
            journal.write(cid, auteur, f"fait de {auteur}")
        raw = (store.home() / "journal" / f"{cid}.md").read_text(encoding="utf-8")
        self.assertEqual(len(raw.splitlines()), 3)


class TestRead(JournalTestCase):
    def test_read_empty_when_no_journal_file(self):
        cid = self._chantier()
        self.assertEqual(journal.read(cid), [])

    def test_read_returns_dicts_with_heure_auteur_texte(self):
        cid = self._chantier()
        journal.write(cid, "ORDO", "graphe accepte, 8 taches")
        entries = journal.read(cid)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertRegex(entry["heure"], r"^\d{2}:\d{2}$")
        self.assertEqual(entry["auteur"], "ORDO")
        self.assertEqual(entry["texte"], "graphe accepte, 8 taches")

    def test_read_respects_limit_keeps_most_recent(self):
        cid = self._chantier()
        for i in range(5):
            journal.write(cid, "ORDO", f"fait {i}")
        entries = journal.read(cid, limit=2)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["texte"], "fait 3")
        self.assertEqual(entries[1]["texte"], "fait 4")

    def test_read_multiline_texte_roundtrips(self):
        # un texte multiligne ne doit pas casser le reparsing : chaque entree reste UNE
        # ligne sur le disque (le format impose une ligne par fait), donc les retours a la
        # ligne du texte original sont echappes a l'ecriture puis restaures a la lecture.
        cid = self._chantier()
        texte = "premiere ligne\ndeuxieme ligne\ntroisieme ligne"
        journal.write(cid, "ORCH", texte)
        raw = (store.home() / "journal" / f"{cid}.md").read_text(encoding="utf-8")
        # sur le disque, l'entree tient sur une seule ligne physique
        self.assertEqual(len(raw.splitlines()), 1)
        entries = journal.read(cid)
        self.assertEqual(entries[0]["texte"], texte)

    def test_read_does_not_confuse_two_entries_after_multiline_write(self):
        cid = self._chantier()
        journal.write(cid, "ORDO", "avant")
        journal.write(cid, "ORCH", "ligne un\nligne deux")
        journal.write(cid, "USER", "apres")
        entries = journal.read(cid)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["texte"], "avant")
        self.assertEqual(entries[1]["texte"], "ligne un\nligne deux")
        self.assertEqual(entries[2]["texte"], "apres")


def _run_record(valeur: int, at: str = "2026-08-09T14:00:00Z") -> dict:
    """Enregistrement de run a la forme EXACTE que capteur.run() persiste.

    La sortie du script vit sous "output", jamais a la racine du run. Un test qui
    fabriquait la forme plate validait le brief contre des donnees que la production ne
    produit jamais : il restait vert alors que la section capteur du brief affichait
    "(aucune mesure)" en permanence. Tout test touchant capteur["runs"] passe par ici.
    """
    return {
        "at": at,
        "ok": True,
        "error": None,
        "timedOut": False,
        "truncated": False,
        "output": {
            "at": at,
            "ok": True,
            "measured": [{"name": "stories", "value": valeur, "unit": "fichiers"}],
            "declared": [],
            "drift": [],
            "unknown": [],
        },
    }


class TestBrief(JournalTestCase):
    def _task_with_report(self, cid: str, report_state: str, note: str) -> str:
        tid = chantier.add_task(cid, "titre tache", "prompt", checklist=["ok"])["id"]
        with store.locked() as state:
            state["taches"][tid]["state"] = "done" if report_state == "done" else "blocked"
            state["taches"][tid]["report"] = {
                "task": tid,
                "state": report_state,
                "note": note,
                "checked": [],
                "question": None,
                "touched": [],
            }
        return tid

    def test_brief_unknown_chantier_raises(self):
        with self.assertRaises(chantier.ChantierError):
            journal.brief("c-99")

    def test_brief_contains_objectif_perimetre_hors_scope(self):
        cid = self._chantier(objectif="tests verts sur /stocks", perimetre="stocks",
                              hors_scope="pas les reservations")
        texte = journal.brief(cid)
        self.assertIn("tests verts sur /stocks", texte)
        self.assertIn("stocks", texte)
        self.assertIn("pas les reservations", texte)

    def test_brief_contains_graph_with_states_and_dependencies(self):
        cid = self._chantier()
        a = chantier.add_task(cid, "migration", "prompt")["id"]
        chantier.add_task(cid, "suite", "prompt", depends_on=(a,))
        texte = journal.brief(cid)
        self.assertIn("migration", texte)
        self.assertIn("suite", texte)
        self.assertIn(a, texte)

    def test_brief_contains_notes_of_terminal_reports_only(self):
        cid = self._chantier()
        done_id = self._task_with_report(cid, "done", "migration terminee, 3 fichiers touches")
        # une tache dont le rapport est encore "progress" n'est pas terminee : sa note
        # ne doit pas apparaitre dans "notes des rapports termines".
        progress_id = chantier.add_task(cid, "en cours", "prompt")["id"]
        with store.locked() as state:
            state["taches"][progress_id]["report"] = {
                "task": progress_id, "state": "progress", "note": "avancement partiel",
                "checked": [], "question": None, "touched": [],
            }
        texte = journal.brief(cid)
        self.assertIn("migration terminee, 3 fichiers touches", texte)
        self.assertNotIn("avancement partiel", texte)

    def test_brief_capteur_non_adopte_dit_inconnu_et_ne_montre_aucun_chiffre(self):
        # point de vigilance explicite : le brief ne sert jamais une mesure d'un capteur
        # non adopte, meme si des runs existent deja (phase des 3 executions concordantes
        # avant validation humaine).
        cid = self._chantier()
        with store.locked() as state:
            state["chantiers"][cid]["capteur"]["runs"] = [_run_record(4242)]
            state["chantiers"][cid]["capteur"]["adopted"] = False
        texte = journal.brief(cid)
        self.assertIn("unknown", texte)
        self.assertNotIn("4242", texte)
        self.assertNotIn("stories", texte)

    def test_brief_capteur_adopte_montre_les_derniers_cycles(self):
        cid = self._chantier()
        with store.locked() as state:
            state["chantiers"][cid]["capteur"]["adopted"] = True
            state["chantiers"][cid]["capteur"]["adoptedAt"] = "2026-08-09T15:00:00Z"
            state["chantiers"][cid]["capteur"]["runs"] = [_run_record(12)]
        texte = journal.brief(cid)
        self.assertIn("12", texte)
        self.assertIn("stories", texte)

    def test_brief_journal_des_decisions_ne_contient_que_elle(self):
        cid = self._chantier()
        journal.write(cid, "ORDO", "fait observable")
        journal.write(cid, "ORCH", "t-04 avant t-03 : t-03 attend la migration")
        journal.write(cid, "USER", "pas touche a la prod")
        texte = journal.brief(cid)
        decisions_idx = texte.index("Decision journal")
        messages_idx = texte.index("Latest human messages")
        decisions_section = texte[decisions_idx:messages_idx]
        self.assertIn("t-04 avant t-03", decisions_section)
        self.assertNotIn("pas touche a la prod", decisions_section)

    def test_brief_derniers_messages_humain_ne_contient_que_toi(self):
        cid = self._chantier()
        journal.write(cid, "ORDO", "fait observable")
        journal.write(cid, "ORCH", "arbitrage")
        journal.write(cid, "USER", "pas touche a la prod")
        texte = journal.brief(cid)
        messages_idx = texte.index("Latest human messages")
        messages_section = texte[messages_idx:]
        self.assertIn("pas touche a la prod", messages_section)
        self.assertNotIn("arbitrage", messages_section)

    def test_brief_section_order_is_the_imposed_order(self):
        cid = self._chantier()
        chantier.add_task(cid, "tache", "prompt")
        journal.write(cid, "ORCH", "decision")
        journal.write(cid, "USER", "message humain")
        texte = journal.brief(cid)
        i_objectif = texte.index("Goal")
        i_graphe = texte.index("Graph")
        i_rapports = texte.index("reports")
        i_capteur = texte.index("Sensor")
        i_decisions = texte.index("Decision journal")
        i_messages = texte.index("Latest human messages")
        self.assertTrue(
            i_objectif < i_graphe < i_rapports < i_capteur < i_decisions < i_messages
        )


if __name__ == "__main__":
    unittest.main()
