"""Tests unitaires de ordo/situation.py.

Isoles par ORDO_HOME, comme test_carte.py. Aucun test ne touche tmux : le module lit
l'etat par carte.model() sans jamais demander la vivacite d'un pane.

Ce que cette suite protege, et qui est la raison d'etre du module : un humain qui revient
apres deux heures d'absence doit pouvoir se resituer sur le seul texte qu'Ordo lui donne.
Le defaut a empecher est l'identifiant nu, `t-33` sans son titre, qui oblige l'humain a
aller chercher ailleurs de quoi on parle. test_render_ne_cite_aucun_identifiant_nu est le
garde-fou : il echoue des qu'un identifiant de tache sort sans son titre.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import chantier, situation, store


class SituationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-situation-test-")
        self._prev_home = os.environ.get("ORDO_HOME")
        os.environ["ORDO_HOME"] = self._tmp
        projet = Path(self._tmp) / "projet"
        projet.mkdir()
        self.chantier = chantier.start(projet, "livrer le module Tapes")["id"]

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("ORDO_HOME", None)
        else:
            os.environ["ORDO_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _add(self, titre, why="", depends_on=None, checklist=None):
        return chantier.add_task(
            self.chantier,
            titre,
            "prompt",
            depends_on=depends_on or [],
            checklist=checklist or [],
            why=why,
        )

    def _set_state(self, task_id: str, **champs) -> None:
        with store.locked() as state:
            state["taches"][task_id].update(champs)

    def _ask(self, task_id: str, question: str, pour_humain: bool) -> str:
        with store.locked() as state:
            qid = store.next_id(state, "question")
            state["questions"][qid] = {
                "id": qid,
                "chantier": self.chantier,
                "tache": task_id,
                "question": question,
                "options": [],
                "pourHumain": pour_humain,
                "answer": None,
                "askedAt": store.now(),
                "answeredAt": None,
            }
        return qid


# ---------------------------------------------------------------------------
# Position dans le graphe


class PositionTest(SituationTestCase):
    def test_progression_globale_compte_les_taches_faites(self):
        a = self._add("0.1 socle")
        self._add("0.2 suite")
        self._set_state(a["id"], state="done")
        m = situation.model(self.chantier)
        self.assertEqual(m["progress"], {"done": 1, "total": 2})

    def test_phase_de_la_tache_vivante_porte_son_libelle_et_sa_progression(self):
        chantier.set_group(self.chantier, "4", "Ecrans", why="les huit ecrans")
        faite = self._add("4.1 editeur")
        vivante = self._add("4.2 en-tete")
        self._add("4.3 pipeline")
        self._set_state(faite["id"], state="done")
        self._set_state(vivante["id"], state="running")

        m = situation.model(self.chantier)
        phases = {p["key"]: p for p in m["phases"]}
        self.assertEqual(phases["4"]["label"], "Ecrans")
        self.assertEqual((phases["4"]["done"], phases["4"]["total"]), (1, 3))
        self.assertTrue(phases["4"]["live"])

    def test_phase_sans_tache_vivante_n_est_pas_marquee_live(self):
        chantier.set_group(self.chantier, "0", "Fondation")
        chantier.set_group(self.chantier, "4", "Ecrans")
        faite = self._add("0.1 socle")
        self._set_state(faite["id"], state="done")
        vivante = self._add("4.1 editeur")
        self._set_state(vivante["id"], state="running")

        m = situation.model(self.chantier)
        phases = {p["key"]: p for p in m["phases"]}
        self.assertFalse(phases["0"]["live"])
        self.assertTrue(phases["4"]["live"])

    def test_deux_phases_vivantes_sont_toutes_les_deux_signalees(self):
        chantier.set_group(self.chantier, "1", "Donnees")
        chantier.set_group(self.chantier, "2", "Serveur")
        a = self._add("1.1 collections")
        b = self._add("2.1 depot")
        self._set_state(a["id"], state="running")
        self._set_state(b["id"], state="running")

        m = situation.model(self.chantier)
        live = sorted(p["key"] for p in m["phases"] if p["live"])
        self.assertEqual(live, ["1", "2"])


# ---------------------------------------------------------------------------
# Les taches vivantes, avec de quoi elles parlent


class RunningTest(SituationTestCase):
    def test_tache_vivante_porte_son_titre_et_son_objet(self):
        t = self._add("4.3 pipeline", why="la colonne Preselectionnes, et la dette B8")
        self._set_state(t["id"], state="running")

        m = situation.model(self.chantier)
        self.assertEqual(len(m["running"]), 1)
        vue = m["running"][0]
        self.assertEqual(vue["id"], t["id"])
        self.assertEqual(vue["titre"], "4.3 pipeline")
        self.assertEqual(vue["why"], "la colonne Preselectionnes, et la dette B8")
        self.assertFalse(vue["whyMissing"])

    def test_objet_absent_est_signale_et_non_tu(self):
        t = self._add("4.3 pipeline")
        self._set_state(t["id"], state="running")

        m = situation.model(self.chantier)
        self.assertTrue(m["running"][0]["whyMissing"])

    def test_tache_vivante_porte_l_avancement_de_sa_liste_de_controle(self):
        t = self._add("4.3 pipeline", checklist=["c1", "c2", "c3"])
        self._set_state(t["id"], state="running")
        with store.locked() as state:
            state["taches"][t["id"]]["checklist"][0]["done"] = True

        m = situation.model(self.chantier)
        self.assertEqual(
            (m["running"][0]["checkDone"], m["running"][0]["checkTotal"]), (1, 3)
        )

    def test_aucune_tache_vivante_donne_une_liste_vide(self):
        t = self._add("4.1 editeur")
        self._set_state(t["id"], state="done")
        self.assertEqual(situation.model(self.chantier)["running"], [])


# ---------------------------------------------------------------------------
# Ce qui attend l'humain, et rien d'autre


class ForHumanTest(SituationTestCase):
    def test_question_pour_l_humain_remonte_avec_le_titre_de_sa_tache(self):
        t = self._add("4.3 pipeline")
        self._set_state(t["id"], state="waiting")
        qid = self._ask(t["id"], "quel libelle pour la pastille ?", pour_humain=True)

        m = situation.model(self.chantier)
        entrees = [e for e in m["forHuman"] if e["kind"] == "question"]
        self.assertEqual(len(entrees), 1)
        self.assertEqual(entrees[0]["id"], qid)
        self.assertEqual(entrees[0]["task"], t["id"])
        self.assertEqual(entrees[0]["taskTitle"], "4.3 pipeline")
        self.assertIn("pastille", entrees[0]["detail"])

    def test_question_pour_l_orchestratrice_n_atteint_pas_l_humain(self):
        t = self._add("4.3 pipeline")
        self._set_state(t["id"], state="waiting")
        self._ask(t["id"], "quelle route pour la demande ?", pour_humain=False)

        m = situation.model(self.chantier)
        self.assertEqual([e for e in m["forHuman"] if e["kind"] == "question"], [])

    def test_question_repondue_disparait(self):
        t = self._add("4.3 pipeline")
        qid = self._ask(t["id"], "quel libelle ?", pour_humain=True)
        with store.locked() as state:
            state["questions"][qid]["answer"] = "Preselectionne(e)"

        m = situation.model(self.chantier)
        self.assertEqual([e for e in m["forHuman"] if e["kind"] == "question"], [])

    def test_tache_bloquee_attend_l_humain_avec_sa_raison(self):
        t = self._add("4.3 pipeline")
        self._set_state(t["id"], state="blocked", error="rapport illisible")

        m = situation.model(self.chantier)
        entrees = [e for e in m["forHuman"] if e["kind"] == "blocked"]
        self.assertEqual(len(entrees), 1)
        self.assertEqual(entrees[0]["taskTitle"], "4.3 pipeline")
        self.assertIn("illisible", entrees[0]["detail"])

    def test_rien_a_signaler_donne_une_liste_vide(self):
        t = self._add("4.1 editeur")
        self._set_state(t["id"], state="running")
        self.assertEqual(situation.model(self.chantier)["forHuman"], [])


# ---------------------------------------------------------------------------
# Ce qui vient ensuite


class NextTest(SituationTestCase):
    def test_tache_lancable_maintenant_est_annoncee_avec_son_titre(self):
        a = self._add("4.1 editeur")
        b = self._add("4.2 en-tete", why="la fiche casting", depends_on=[a["id"]])
        self._set_state(a["id"], state="done")

        m = situation.model(self.chantier)
        self.assertEqual([n["id"] for n in m["next"]], [b["id"]])
        self.assertEqual(m["next"][0]["titre"], "4.2 en-tete")
        self.assertEqual(m["next"][0]["why"], "la fiche casting")

    def test_tache_en_attente_d_une_dependance_n_est_pas_annoncee(self):
        a = self._add("4.1 editeur")
        b = self._add("4.2 en-tete", depends_on=[a["id"]])
        annoncees = [n["id"] for n in situation.model(self.chantier)["next"]]
        self.assertIn(a["id"], annoncees)
        self.assertNotIn(b["id"], annoncees)


# ---------------------------------------------------------------------------
# Le rendu texte, qui est le livrable reel du module


class RenderTest(SituationTestCase):
    _ID = re.compile(r"(?<![\w-])(t-\d+)(?![\w-])")

    def test_render_ne_cite_aucun_identifiant_nu(self):
        """Chaque `t-xx` du rendu est suivi de son titre sur la meme ligne.

        Echouerait si le module imprimait un identifiant seul, ce qui est exactement le
        defaut de lecture que ce module existe pour supprimer.
        """
        chantier.set_group(self.chantier, "4", "Ecrans")
        a = self._add("4.1 editeur", why="les six types de livrable")
        b = self._add("4.2 en-tete", why="la fiche casting", depends_on=[a["id"]])
        c = self._add("4.3 pipeline", why="la colonne Preselectionnes")
        self._set_state(a["id"], state="done")
        self._set_state(b["id"], state="running")
        self._set_state(c["id"], state="blocked", error="rapport illisible")
        self._ask(c["id"], "quel libelle pour la pastille ?", pour_humain=True)

        texte = situation.render(situation.model(self.chantier))
        titres = {a["id"]: "4.1 editeur", b["id"]: "4.2 en-tete", c["id"]: "4.3 pipeline"}
        for ligne in texte.splitlines():
            for tid in self._ID.findall(ligne):
                self.assertIn(
                    titres[tid],
                    ligne,
                    f"identifiant nu {tid} sans son titre : {ligne!r}",
                )

    def test_render_ne_laisse_pas_passer_un_titre_vide(self):
        """chantier.add_task() accepte un titre vide, et la chaine vide est contenue dans
        n'importe quelle ligne : test_render_ne_cite_aucun_identifiant_nu ne peut donc PAS
        voir ce cas. Sans ce test, l'invariant tombait sur `t-01 «  »` sans que rien ne
        l'attrape. Echouerait si _nomme() cessait de replier sur un libelle explicite."""
        t = self._add("", why="une tache sans titre")
        self._set_state(t["id"], state="running")

        texte = situation.render(situation.model(self.chantier))
        self.assertIn(f"{t['id']} « no title »", texte)
        self.assertNotIn(f"{t['id']} «  »", texte)

    def test_render_cite_la_phase_et_la_progression(self):
        chantier.set_group(self.chantier, "4", "Ecrans")
        faite = self._add("4.1 editeur")
        vivante = self._add("4.2 en-tete", why="la fiche casting")
        self._add("4.3 pipeline")
        self._set_state(faite["id"], state="done")
        self._set_state(vivante["id"], state="running")

        texte = situation.render(situation.model(self.chantier))
        self.assertIn("Ecrans", texte)
        self.assertRegex(texte, r"1\s*/\s*3|1 of 3")

    def test_render_dit_l_objet_de_chaque_tache_vivante(self):
        t = self._add("4.3 pipeline", why="la colonne Preselectionnes, et la dette B8")
        self._set_state(t["id"], state="running")

        texte = situation.render(situation.model(self.chantier))
        self.assertIn("la colonne Preselectionnes, et la dette B8", texte)

    def test_render_signale_un_objet_manquant_par_la_commande_qui_le_repare(self):
        t = self._add("4.3 pipeline")
        self._set_state(t["id"], state="running")

        texte = situation.render(situation.model(self.chantier))
        self.assertIn(f"ordo why {t['id']}", texte)

    def test_render_dit_explicitement_que_rien_n_attend_l_humain(self):
        t = self._add("4.1 editeur", why="les six types")
        self._set_state(t["id"], state="running")

        texte = situation.render(situation.model(self.chantier))
        self.assertRegex(texte, r"for you\s*:\s*nothing")

    def test_render_dit_qu_aucune_executante_n_est_vivante(self):
        t = self._add("4.1 editeur")
        self._set_state(t["id"], state="done")

        texte = situation.render(situation.model(self.chantier))
        self.assertRegex(texte, r"running\s*:\s*nothing alive")


class ChantierAbsentTest(SituationTestCase):
    def test_chantier_inconnu_refuse(self):
        with self.assertRaises(chantier.ChantierError):
            situation.model("c-99")


if __name__ == "__main__":
    unittest.main()
