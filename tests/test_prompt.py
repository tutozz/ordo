"""Tests unitaires de ordo/prompt.py.

Isoles par ORDO_HOME, comme les autres suites. Aucun test ne touche le vrai
~/.claude/ordo ni un vrai process claude.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import chantier, prompt, store


class PromptTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-prompt-test-")
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
        defaults = dict(
            objectif="tests verts sur /stocks",
            perimetre="le module stocks",
            hors_scope="pas les reservations, pas la prod",
        )
        defaults.update(kw)
        return chantier.start(str(self._projet()), **defaults)["id"]

    def _task(self, cid: str, **kw) -> str:
        defaults = dict(
            titre="migrer le schema",
            prompt="migrer la table stocks vers le nouveau schema",
            touches=["db/schema.sql", "app/src/api"],
            checklist=["tests verts", "migration reversible"],
        )
        defaults.update(kw)
        return chantier.add_task(cid, **defaults)["id"]


class TestBriefExecutante(PromptTestCase):
    def test_writes_file_under_briefs_and_returns_its_path(self):
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        self.assertTrue(Path(chemin).exists())
        self.assertEqual(Path(chemin).parent.name, cid)
        self.assertEqual(Path(chemin).parent.parent.name, "briefs")

    def test_deux_chantiers_ne_partagent_jamais_un_fichier_de_brief(self):
        """Le prefixe de chantier est ce qui empeche deux projets de se disputer t-01.

        Les identifiants de tache sont uniques par ORDO_HOME, mais un home partage entre
        plusieurs projets melangeait leurs briefs a plat : rien dans le chemin ne disait
        de quel chantier venait briefs/t-01.md.
        """
        a = self._chantier()
        autre_projet = Path(self._tmp) / "autre"
        autre_projet.mkdir(exist_ok=True)
        b = chantier.start(
            str(autre_projet),
            objectif="autre objectif",
            perimetre="autre perimetre",
            hors_scope="autre hors scope",
            home_partage=True,
        )["id"]
        chemin_a = Path(prompt.brief_executante(self._task(a)))
        chemin_b = Path(prompt.brief_executante(self._task(b)))
        self.assertNotEqual(chemin_a.parent, chemin_b.parent)
        self.assertEqual(chemin_a.parent.name, a)
        self.assertEqual(chemin_b.parent.name, b)

    def test_returned_path_is_absolute(self):
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        self.assertTrue(Path(chemin).is_absolute())

    def test_unknown_task_raises(self):
        with self.assertRaises(chantier.ChantierError):
            prompt.brief_executante("t-99")

    def test_brief_contains_le_fond_donne_par_orchestratrice(self):
        # le fond (objectif, perimetre, pieges) vient du chantier et de la tache, pas
        # d'Ordo : l'orchestratrice l'a deja ecrit en demarrant le chantier et la tache.
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn("tests verts sur /stocks", contenu)
        self.assertIn("le module stocks", contenu)
        self.assertIn("pas les reservations, pas la prod", contenu)
        self.assertIn("migrer la table stocks vers le nouveau schema", contenu)

    def test_brief_contains_zones_declarees(self):
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn("db/schema.sql", contenu)
        self.assertIn("app/src/api", contenu)

    def test_brief_contains_checklist_items(self):
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn("tests verts", contenu)
        self.assertIn("migration reversible", contenu)

    def test_brief_contains_report_file_path_as_absolute(self):
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        rapport_attendu = store.home() / "reports" / cid / f"{tid}.json"
        # le chemin est ecrit dans le brief TEL QUEL, absolu : une executante tourne dans
        # le repertoire du projet, pas dans le dossier d'Ordo, et un chemin relatif ne
        # resout nulle part (defaut signale par une vraie session du projet d'origine).
        self.assertIn(str(store.canon(rapport_attendu)), contenu)
        self.assertTrue(Path(store.canon(rapport_attendu)).is_absolute())

    def test_brief_contains_report_json_schema_with_task_id(self):
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn(tid, contenu)
        self.assertIn("state", contenu)
        self.assertIn("checked", contenu)
        self.assertIn("touched", contenu)

    def test_askuserquestion_interdit_dans_le_brief(self):
        # garde-fou mesure : AskUserQuestion n'existe pas hors interactif, la session le
        # cherche puis abandonne sans rien livrer. Le brief doit lui donner l'alternative
        # explicite : ecrire state: "asking" dans son rapport et terminer son tour.
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn("AskUserQuestion", contenu)
        self.assertIn("asking", contenu)

    def test_autovalidation_interdite_dans_le_brief(self):
        # garde-fou mesure : une executante qui declare elle-meme son travail conforme a
        # l'intention ne garantit plus rien. Elle rapporte des faits verifiables.
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn("self-valid", contenu.lower())

    def test_irreversible_sans_question_interdit_dans_le_brief(self):
        # troisieme interdit, dicte par le mode tmux : rien d'irreversible ni d'externe
        # sans passer par le canal de rapport, puisque le graphe part automatiquement au
        # bout de 45 secondes.
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn("irreversible", contenu.lower())
        self.assertIn("push", contenu.lower())

    def test_brief_is_deterministic_content_for_same_task_state(self):
        cid = self._chantier()
        tid = self._task(cid)
        premier = Path(prompt.brief_executante(tid)).read_text(encoding="utf-8")
        second = Path(prompt.brief_executante(tid)).read_text(encoding="utf-8")
        self.assertEqual(premier, second)


class TestBriefEntete(PromptTestCase):
    """Point H du contrat : le brief dit ou il se situe (chantier, projet, cwd,
    session tmux, chemin du rapport) avant meme la section objectif."""

    def test_entete_precede_la_section_objectif(self):
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        entete = f"# Executor {tid}, campaign {cid}"
        self.assertIn(entete, contenu)
        self.assertLess(contenu.index(entete), contenu.index("## Campaign goal"))

    def test_entete_mentionne_session_claude_code_reelle_dans_un_pane_tmux(self):
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn("real Claude Code session", contenu)
        self.assertIn("tmux pane", contenu)
        self.assertIn("sub-agent", contenu)

    def test_tableau_de_situation_porte_projet_session_et_rapport(self):
        cid = self._chantier()
        tid = self._task(cid)
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        state = store.load()
        ch = state["chantiers"][cid]
        rapport_attendu = str(store.canon(store.home() / "reports" / cid / f"{tid}.json"))
        self.assertIn(ch["project"], contenu)
        self.assertIn(ch["tmuxSession"], contenu)
        self.assertIn(rapport_attendu, contenu)

    def test_cwd_none_affiche_le_projet_du_chantier(self):
        # tant que la tache n'a pas ete lancee, task["cwd"] est None : le brief doit
        # afficher le projet du chantier, valeur effectivement utilisee au lancement.
        cid = self._chantier()
        tid = self._task(cid)
        state = store.load()
        self.assertIsNone(state["taches"][tid]["cwd"])
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        ch = state["chantiers"][cid]
        self.assertIn(f"| Working directory | {ch['project']} |", contenu)

    def test_cwd_defini_affiche_le_cwd_reel_de_la_tache(self):
        cid = self._chantier()
        tid = self._task(cid)
        autre_cwd = str(self._projet("ailleurs"))
        with store.locked() as state:
            state["taches"][tid]["cwd"] = autre_cwd
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn(f"| Working directory | {autre_cwd} |", contenu)
        state = store.load()
        ch = state["chantiers"][cid]
        # le projet du chantier reste affiche par ailleurs, mais pas comme cwd ici
        self.assertNotIn(f"| Working directory | {ch['project']} |", contenu)


class TestNotesRendering(PromptTestCase):
    def test_notes_rendered_as_readable_lines_not_raw_dicts(self):
        # correction 2 : notes est une liste de dicts {"at", "state", "note"} (SPEC.md
        # section 3), jamais une liste de chaines. Un rendu brut via f"- {n}" afficherait
        # "- {'at': '...', 'state': 'blocked', 'note': '...'}", illisible pour
        # l'executante a qui le brief est destine. Ce test echoue si _format_note()
        # est retire et _section_fond revient a f"- {n}" for n in task["notes"].
        cid = self._chantier()
        tid = self._task(cid)
        with store.locked() as state:
            state["taches"][tid]["notes"] = [
                {"at": "2026-08-09T14:00:00Z", "state": "blocked", "note": "manque une cle API"},
            ]
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn("2026-08-09T14:00:00Z", contenu)
        self.assertIn("blocked", contenu)
        self.assertIn("manque une cle API", contenu)
        self.assertNotIn("{'at'", contenu)
        self.assertNotIn("'state':", contenu)

    def test_notes_rendering_tolerant_to_plain_string(self):
        # tolerance explicite : une note qui ne serait qu'une simple chaine ne doit
        # jamais faire planter le brief, elle s'affiche telle quelle.
        cid = self._chantier()
        tid = self._task(cid)
        with store.locked() as state:
            state["taches"][tid]["notes"] = ["note libre en texte simple"]
        chemin = prompt.brief_executante(tid)
        contenu = Path(chemin).read_text(encoding="utf-8")
        self.assertIn("note libre en texte simple", contenu)


class TestContratRole(PromptTestCase):
    def test_returns_nonempty_string(self):
        texte = prompt.contrat_role()
        self.assertIsInstance(texte, str)
        self.assertTrue(texte.strip())

    def test_mentions_no_self_validation(self):
        texte = prompt.contrat_role().lower()
        self.assertIn("self-valid", texte)

    def test_mentions_orchestrator_does_not_produce_code(self):
        texte = prompt.contrat_role().lower()
        self.assertIn("code", texte)

    def test_does_not_touch_disk(self):
        # rappel de role statique : aucun fichier ne doit apparaitre dans ORDO_HOME du
        # seul fait de l'appeler.
        before = set((store.home() / "briefs").iterdir())
        prompt.contrat_role()
        after = set((store.home() / "briefs").iterdir())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()


class TestDisciplineDeContexte(PromptTestCase):
    """La section qui dit à une exécutante de ne pas noyer sa propre conversation.

    Mesure sur soixante transcripts réels : Bash apporte 47 % de ce qui entre dans le
    contexte d'une session et Read 32 %, et chaque jeton entré est ensuite relu cent
    fois. Une sortie de test déversée en entier dans la conversation n'est pas payée une
    fois, elle est payée à chaque tour restant.
    """

    def test_le_brief_dit_de_rediriger_les_sorties_longues(self):
        cid = self._chantier()
        tid = self._task(cid)
        contenu = Path(prompt.brief_executante(tid)).read_text(encoding="utf-8")
        self.assertIn("## Context discipline", contenu)
        self.assertIn("tail", contenu)

    def test_le_brief_dit_de_lire_par_tranches(self):
        cid = self._chantier()
        tid = self._task(cid)
        contenu = Path(prompt.brief_executante(tid)).read_text(encoding="utf-8")
        self.assertIn("offset", contenu)

    def test_le_brief_chiffre_la_raison(self):
        # Une consigne sans sa raison se contourne des que l'executante croit bien
        # faire ; le chiffre est ce qui la rend defendable.
        cid = self._chantier()
        tid = self._task(cid)
        contenu = Path(prompt.brief_executante(tid)).read_text(encoding="utf-8")
        self.assertIn("re-read on every later turn", contenu)
        self.assertIn("100 times", contenu)
