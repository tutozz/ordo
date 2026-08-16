"""Tests unitaires de ordo/carte.py.

Isoles par ORDO_HOME, comme test_chantier.py. Aucun de ces tests ne touche tmux : la
vivacite d'un pane entre par injection (parametre `alive`), exactement pour que le modele
de la carte reste verifiable sans serveur tmux.
"""

from __future__ import annotations

import colorsys
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import carte, chantier, journal, store, usage


class CarteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-carte-test-")
        self._prev_home = os.environ.get("ORDO_HOME")
        os.environ["ORDO_HOME"] = self._tmp
        projet = Path(self._tmp) / "projet"
        projet.mkdir()
        self.chantier = chantier.start(projet, "livrer les phases 0 a 6")["id"]

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("ORDO_HOME", None)
        else:
            os.environ["ORDO_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _add(self, titre, prompt="p", depends_on=None, touches=None, checklist=None, why=""):
        return chantier.add_task(
            self.chantier,
            titre,
            prompt,
            depends_on=depends_on or [],
            touches=touches or [],
            checklist=checklist or [],
            why=why,
        )

    def _set_state(self, task_id: str, **champs) -> None:
        with store.locked() as state:
            state["taches"][task_id].update(champs)

    def _warnings(self, model: dict, kind: str) -> list[dict]:
        return [w for w in model["warnings"] if w["kind"] == kind]


# ---------------------------------------------------------------------------
# Niveaux
# ---------------------------------------------------------------------------


class TestNiveaux(CarteTestCase):
    def test_une_tache_sans_dependance_est_au_niveau_zero(self):
        t = self._add("0.1 socle")
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][t["id"]]["level"], 0)

    def test_une_chaine_empile_les_niveaux(self):
        a = self._add("0.1 a")
        b = self._add("0.2 b", depends_on=[a["id"]])
        c = self._add("0.3 c", depends_on=[b["id"]])
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][a["id"]]["level"], 0)
        self.assertEqual(model["nodes"][b["id"]]["level"], 1)
        self.assertEqual(model["nodes"][c["id"]]["level"], 2)

    def test_le_niveau_suit_la_dependance_la_plus_profonde(self):
        a = self._add("0.1 a")
        b = self._add("0.2 b", depends_on=[a["id"]])
        d = self._add("0.4 d", depends_on=[a["id"], b["id"]])
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][d["id"]]["level"], 2)

    def test_levels_liste_les_identifiants_colonne_par_colonne(self):
        a = self._add("0.1 a")
        b = self._add("0.2 b", depends_on=[a["id"]])
        model = carte.model(self.chantier)
        self.assertEqual(model["levels"], [[a["id"]], [b["id"]]])

    def test_un_cycle_ne_boucle_pas_indefiniment_et_se_signale(self):
        # add_task refuse un cycle ; on l'installe donc directement dans l'etat, ce qui
        # est exactement ce qu'une edition a la main de state.json produirait.
        a = self._add("0.1 a")
        b = self._add("0.2 b", depends_on=[a["id"]])
        self._set_state(a["id"], dependsOn=[b["id"]])
        model = carte.model(self.chantier)
        self.assertTrue(self._warnings(model, "cycle"))
        # Tous les noeuds restent rendus : une carte qui disparait sur un cycle prive
        # justement du seul dessin qui montrerait le cycle.
        self.assertEqual(set(model["nodes"]), {a["id"], b["id"]})

    def test_une_dependance_inexistante_ne_casse_pas_le_niveau(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], dependsOn=["t-999"])
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][a["id"]]["level"], 0)
        self.assertTrue(self._warnings(model, "dependance-inexistante"))


# ---------------------------------------------------------------------------
# Groupes
# ---------------------------------------------------------------------------


class TestGroupes(CarteTestCase):
    def test_le_groupe_vient_du_prefixe_numerique_du_titre(self):
        a = self._add("0.1 socle")
        b = self._add("1.2 modele")
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][a["id"]]["group"], "0")
        self.assertEqual(model["nodes"][b["id"]]["group"], "1")

    def test_un_suffixe_de_lettre_reste_dans_sa_phase(self):
        # "0.4b" est une tache inseree apres coup dans la phase 0 ; elle ne doit pas
        # fabriquer un groupe a elle toute seule.
        a = self._add("0.4b EMAIL_MODE sans defaut")
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][a["id"]]["group"], "0")

    def test_un_titre_commencant_par_un_nombre_sans_point_n_est_pas_une_phase(self):
        # Le point est ce qui separe "0.3 dette B1" d'un titre qui commence par une annee.
        # Sans lui, ce chantier fabriquerait une phase 2026.
        a = self._add("2026 audit des routes publiques")
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][a["id"]]["group"], "")

    def test_un_titre_sans_prefixe_tombe_dans_un_groupe_hors_phase(self):
        a = self._add("reparer le silence de l extraction")
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][a["id"]]["group"], "")
        groupes = {g["key"]: g for g in model["groups"]}
        self.assertIn("", groupes)

    def test_le_libelle_par_defaut_nomme_la_phase(self):
        self._add("0.1 socle")
        model = carte.model(self.chantier)
        groupes = {g["key"]: g["label"] for g in model["groups"]}
        self.assertEqual(groupes["0"], "Phase 0")

    def test_un_libelle_pose_a_la_main_remplace_le_defaut(self):
        self._add("0.1 socle")
        chantier.set_group(self.chantier, "0", "Socle, rien ne commence avant")
        model = carte.model(self.chantier)
        groupes = {g["key"]: g["label"] for g in model["groups"]}
        self.assertEqual(groupes["0"], "Socle, rien ne commence avant")

    def test_un_groupe_porte_ses_taches_et_ses_bornes_de_niveau(self):
        a = self._add("0.1 a")
        b = self._add("0.2 b", depends_on=[a["id"]])
        c = self._add("1.1 c", depends_on=[b["id"]])
        model = carte.model(self.chantier)
        groupes = {g["key"]: g for g in model["groups"]}
        self.assertEqual(groupes["0"]["tasks"], [a["id"], b["id"]])
        self.assertEqual((groupes["0"]["minLevel"], groupes["0"]["maxLevel"]), (0, 1))
        self.assertEqual(groupes["1"]["tasks"], [c["id"]])
        self.assertEqual((groupes["1"]["minLevel"], groupes["1"]["maxLevel"]), (2, 2))

    def test_les_groupes_sortent_dans_l_ordre_des_phases(self):
        self._add("2.1 c")
        self._add("0.1 a")
        self._add("10.1 d")
        self._add("1.1 b")
        model = carte.model(self.chantier)
        self.assertEqual([g["key"] for g in model["groups"]], ["0", "1", "2", "10"])

    def test_set_group_refuse_un_chantier_inconnu(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.set_group("c-999", "0", "x")

    def test_une_phase_nommee_sans_tache_est_une_phase_annoncee(self):
        # Le vrai manque : une orchestratrice qui n'a materialise que la phase 0 laisse
        # croire que le chantier tient en une phase. Nommer les phases a venir des le
        # depart, meme vides, est ce qui rend le decoupage lisible.
        self._add("0.1 a")
        chantier.set_group(self.chantier, "1", "Modele de donnees")
        chantier.set_group(self.chantier, "2", "Architecture serveur")
        model = carte.model(self.chantier)
        groupes = {g["key"]: g for g in model["groups"]}
        self.assertEqual(set(groupes), {"0", "1", "2"})
        self.assertFalse(groupes["0"]["planned"])
        self.assertTrue(groupes["1"]["planned"])
        self.assertEqual(groupes["1"]["tasks"], [])

    def test_une_phase_annoncee_ne_casse_ni_les_niveaux_ni_le_dessin(self):
        self._add("0.1 a")
        chantier.set_group(self.chantier, "3", "Routes")
        model = carte.model(self.chantier)
        self.assertEqual(model["levels"], [["t-01"]])
        page = carte.html(model)
        self.assertIn("Routes", page)

    def test_le_pourquoi_d_une_phase_se_pose_et_se_relit(self):
        self._add("0.1 a")
        chantier.set_group(
            self.chantier, "0", "Socle", why="rien ne peut sortir sur une base fausse"
        )
        model = carte.model(self.chantier)
        groupes = {g["key"]: g for g in model["groups"]}
        self.assertEqual(groupes["0"]["why"], "rien ne peut sortir sur une base fausse")

    def test_renommer_une_phase_sans_pourquoi_ne_l_efface_pas(self):
        # Corriger un libelle ne doit pas faire disparaitre en silence l'explication qui
        # etait deja ecrite : c'est la seule chose que personne ne reecrira.
        self._add("0.1 a")
        chantier.set_group(self.chantier, "0", "Socle", why="la base doit etre juste")
        chantier.set_group(self.chantier, "0", "Socle technique")
        model = carte.model(self.chantier)
        groupes = {g["key"]: g for g in model["groups"]}
        self.assertEqual(groupes["0"]["label"], "Socle technique")
        self.assertEqual(groupes["0"]["why"], "la base doit etre juste")


# ---------------------------------------------------------------------------
# Comptes, phases declarees, entete
# ---------------------------------------------------------------------------


class TestEntete(CarteTestCase):
    def test_les_comptes_suivent_les_etats(self):
        a = self._add("0.1 a")
        self._add("0.2 b")
        self._set_state(a["id"], state="done")
        model = carte.model(self.chantier)
        self.assertEqual(model["counts"]["done"], 1)
        self.assertEqual(model["counts"]["queued"], 1)
        self.assertEqual(model["counts"]["total"], 2)

    def test_les_phases_observees_sont_celles_du_graphe(self):
        self._add("0.1 a")
        self._add("1.1 b")
        model = carte.model(self.chantier)
        self.assertEqual(model["phases"]["observed"], ["0", "1"])

    def test_l_objectif_qui_annonce_une_plage_de_phases_est_lu(self):
        # "phases 0 a 6" dans l'objectif : le graphe n'en materialise que deux, et c'est
        # exactement l'ecart qu'une orchestratrice ne dit jamais d'elle-meme.
        self._add("0.1 a")
        self._add("1.1 b")
        model = carte.model(self.chantier)
        self.assertEqual(model["phases"]["declared"], ["0", "1", "2", "3", "4", "5", "6"])
        self.assertEqual(model["phases"]["missing"], ["2", "3", "4", "5", "6"])

    def test_un_objectif_sans_plage_de_phases_ne_declare_rien(self):
        with store.locked() as state:
            state["chantiers"][self.chantier]["objectif"] = "livrer le module"
        self._add("0.1 a")
        model = carte.model(self.chantier)
        self.assertIsNone(model["phases"]["declared"])
        self.assertEqual(model["phases"]["missing"], [])

    def test_les_terminales_sont_les_taches_que_rien_n_attend(self):
        a = self._add("0.1 a")
        b = self._add("0.2 b", depends_on=[a["id"]])
        c = self._add("1.1 c", depends_on=[a["id"]])
        model = carte.model(self.chantier)
        self.assertEqual(model["terminals"], [b["id"], c["id"]])

    def test_une_terminale_annulee_ne_cloture_rien(self):
        a = self._add("0.1 a")
        b = self._add("0.2 b", depends_on=[a["id"]])
        chantier.cancel(b["id"])
        model = carte.model(self.chantier)
        self.assertEqual(model["terminals"], [])

    def test_un_chantier_inconnu_est_refuse(self):
        with self.assertRaises(chantier.ChantierError):
            carte.model("c-999")

    def test_un_chantier_sans_tache_rend_un_modele_vide_mais_valide(self):
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"], {})
        self.assertEqual(model["levels"], [])
        self.assertEqual(model["counts"]["total"], 0)


# ---------------------------------------------------------------------------
# Detail d'un noeud
# ---------------------------------------------------------------------------


class TestNoeud(CarteTestCase):
    def test_un_noeud_porte_ses_zones_et_sa_checklist(self):
        a = self._add("0.1 a", touches=["index.js"], checklist=["c un", "c deux"])
        self._set_state(a["id"], **{"checklist": [
            {"id": "c1", "label": "c un", "done": True},
            {"id": "c2", "label": "c deux", "done": False},
        ]})
        model = carte.model(self.chantier)
        node = model["nodes"][a["id"]]
        self.assertEqual(node["touches"], ["index.js"])
        self.assertEqual((node["checkDone"], node["checkTotal"]), (1, 2))

    def test_un_noeud_liste_ses_dependants(self):
        a = self._add("0.1 a")
        b = self._add("0.2 b", depends_on=[a["id"]])
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][a["id"]]["dependants"], [b["id"]])

    def test_une_tache_en_cours_porte_sa_duree_lue_en_utc(self):
        # L'horodatage est UTC, suffixe Z. Le lire en heure locale ferait afficher deux
        # heures d'anciennete a Paris l'ete sur une tache lancee a l'instant, ou une duree
        # negative de l'autre cote de Greenwich. Une seule assertion "> 0" laisserait
        # passer les deux.
        a = self._add("0.1 a")
        depart = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 90))
        self._set_state(a["id"], state="running", startedAt=depart)
        ecoule = carte.model(self.chantier)["nodes"][a["id"]]["elapsedS"]
        self.assertGreaterEqual(ecoule, 88)
        self.assertLessEqual(ecoule, 120)

    def test_une_tache_terminee_porte_la_duree_entre_debut_et_fin(self):
        a = self._add("0.1 a")
        self._set_state(
            a["id"], state="done",
            startedAt="2020-01-01T00:00:00Z", finishedAt="2020-01-01T00:02:30Z",
        )
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][a["id"]]["elapsedS"], 150)

    def test_sans_horodatage_de_depart_la_duree_est_inconnue(self):
        a = self._add("0.1 a")
        model = carte.model(self.chantier)
        self.assertIsNone(model["nodes"][a["id"]]["elapsedS"])

    def test_la_vivacite_du_pane_est_injectee_jamais_devinee(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", paneId="%7")
        sans = carte.model(self.chantier)
        self.assertIsNone(sans["nodes"][a["id"]]["paneAlive"])
        avec = carte.model(self.chantier, alive=lambda pid: pid == "%7")
        self.assertIs(avec["nodes"][a["id"]]["paneAlive"], True)

    def test_le_noeud_porte_l_etat_et_la_note_de_son_rapport(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], report={"state": "done", "note": "vert 8/8"})
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][a["id"]]["reportState"], "done")
        self.assertEqual(model["nodes"][a["id"]]["reportNote"], "vert 8/8")


# ---------------------------------------------------------------------------
# Raisons d'attente
# ---------------------------------------------------------------------------


class TestPourquoi(CarteTestCase):
    def test_le_pourquoi_d_une_tache_se_pose_a_la_creation(self):
        t = self._add("0.5 D25 identifiants stables", why="les index de role bougent")
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][t["id"]]["why"], "les index de role bougent")

    def test_le_pourquoi_se_pose_apres_coup_sur_une_tache_existante(self):
        # Sans ce verbe, une campagne deja lancee resterait muette pour toujours.
        t = self._add("0.5 D25")
        chantier.explain(t["id"], "les index de role bougent a chaque migration")
        model = carte.model(self.chantier)
        self.assertIn("chaque migration", model["nodes"][t["id"]]["why"])

    def test_une_tache_sans_pourquoi_le_dit_au_lieu_de_faire_semblant(self):
        t = self._add("0.5 D25")
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][t["id"]]["why"], "")
        self.assertIn(t["id"], model["missingWhy"])

    def test_une_tache_avec_pourquoi_ne_figure_pas_dans_les_manquants(self):
        t = self._add("0.5 D25", why="parce que")
        self.assertNotIn(t["id"], carte.model(self.chantier)["missingWhy"])

    def test_une_tache_annulee_n_est_pas_reprochee_pour_son_pourquoi(self):
        t = self._add("0.5 D25")
        chantier.cancel(t["id"])
        self.assertNotIn(t["id"], carte.model(self.chantier)["missingWhy"])

    def test_la_page_signale_le_decoupage_non_explique(self):
        self._add("0.1 a")
        page = carte.html(carte.model(self.chantier))
        self.assertIn("sans explication", page.lower())

    def test_explain_refuse_une_tache_inconnue(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.explain("t-999", "x")


class TestRaisonsDAttente(CarteTestCase):
    def test_une_tache_en_file_dit_quelle_dependance_la_bloque(self):
        a = self._add("0.1 a")
        b = self._add("0.2 b", depends_on=[a["id"]])
        model = carte.model(self.chantier)
        blocked = model["nodes"][b["id"]]["blockedBy"]
        self.assertEqual([x["id"] for x in blocked], [a["id"]])
        self.assertIn("queued", blocked[0]["reason"])

    def test_une_dependance_finie_mais_checklist_incomplete_bloque_toujours(self):
        a = self._add("0.1 a", checklist=["c un"])
        b = self._add("0.2 b", depends_on=[a["id"]])
        self._set_state(a["id"], state="done")
        model = carte.model(self.chantier)
        self.assertIn("checklist", model["nodes"][b["id"]]["blockedBy"][0]["reason"])

    def test_une_tache_prete_n_a_aucune_raison_d_attente(self):
        a = self._add("0.1 a", checklist=["c un"])
        b = self._add("0.2 b", depends_on=[a["id"]])
        self._set_state(a["id"], state="done", checklist=[
            {"id": "c1", "label": "c un", "done": True}
        ])
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][b["id"]]["blockedBy"], [])
        self.assertIs(model["nodes"][b["id"]]["ready"], True)


# ---------------------------------------------------------------------------
# Decisions de l'orchestratrice
# ---------------------------------------------------------------------------


class TestDecisions(CarteTestCase):
    def test_une_decision_ORCH_qui_nomme_une_tache_est_rattachee_au_noeud(self):
        a = self._add("0.1 a")
        journal.write(self.chantier, "ORCH", f"{a['id']} attend desormais autre chose")
        model = carte.model(self.chantier)
        textes = [d["texte"] for d in model["nodes"][a["id"]]["decisions"]]
        self.assertEqual(len(textes), 1)
        self.assertIn("attend desormais", textes[0])

    def test_une_ligne_ORDO_n_est_pas_une_decision(self):
        a = self._add("0.1 a")
        journal.write(self.chantier, "ORDO", f"launch {a['id']} pane=%1")
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"][a["id"]]["decisions"], [])

    def test_une_decision_qui_ne_nomme_aucune_tache_reste_au_chantier(self):
        self._add("0.1 a")
        journal.write(self.chantier, "ORCH", "la suite complete est destructrice")
        model = carte.model(self.chantier)
        self.assertEqual(len(model["decisions"]), 1)

    def test_un_identifiant_ne_capte_pas_son_homonyme_plus_long(self):
        # t-10 ne doit pas ramasser une decision qui parle de t-100. Les identifiants sont
        # poses directement dans l'etat plutot que par cent add_task : la collision ne
        # commence qu'a trois chiffres, et un test qui s'arrete a t-13 resterait vert meme
        # sans les bornes de la regex -- il ne prouverait rien.
        court = self._add("0.1 court")
        with store.locked() as state:
            long = dict(state["taches"][court["id"]])
            long["id"] = "t-100"
            long["titre"] = "0.2 long"
            state["taches"]["t-100"] = long
            state["taches"][court["id"]]["id"] = "t-10"
            state["taches"]["t-10"] = state["taches"].pop(court["id"])
        journal.write(self.chantier, "ORCH", "t-100 pose un piege")
        model = carte.model(self.chantier)
        self.assertEqual(model["nodes"]["t-10"]["decisions"], [])
        self.assertEqual(len(model["nodes"]["t-100"]["decisions"]), 1)


# ---------------------------------------------------------------------------
# Avertissements
# ---------------------------------------------------------------------------


class TestAvertissements(CarteTestCase):
    def test_une_tache_finie_a_checklist_incomplete_est_signalee(self):
        a = self._add("0.1 a", checklist=["c un"])
        self._add("0.2 b", depends_on=[a["id"]])
        self._set_state(a["id"], state="done")
        model = carte.model(self.chantier)
        self.assertTrue(self._warnings(model, "checklist-incomplete"))

    def test_un_pane_mort_sur_une_tache_en_cours_est_signale(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", paneId="%7")
        model = carte.model(self.chantier, alive=lambda pid: False)
        self.assertTrue(self._warnings(model, "pane-mort"))

    def test_sans_verificateur_de_pane_aucun_pane_mort_n_est_invente(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", paneId="%7")
        model = carte.model(self.chantier)
        self.assertEqual(self._warnings(model, "pane-mort"), [])

    def test_deux_taches_du_meme_niveau_qui_partagent_une_zone_sont_signalees(self):
        self._add("0.1 a", touches=["index.js"])
        self._add("0.2 b", touches=["index.js"])
        model = carte.model(self.chantier)
        conflits = self._warnings(model, "zone-partagee")
        self.assertTrue(conflits)
        self.assertIn("index.js", conflits[0]["detail"])

    def test_deux_taches_de_niveaux_differents_ne_sont_pas_en_conflit(self):
        a = self._add("0.1 a", touches=["index.js"])
        self._add("0.2 b", depends_on=[a["id"]], touches=["index.js"])
        model = carte.model(self.chantier)
        self.assertEqual(self._warnings(model, "zone-partagee"), [])

    def test_un_chantier_clos_avec_des_taches_vivantes_est_signale(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running")
        with store.locked() as state:
            state["chantiers"][self.chantier]["state"] = "closed"
        model = carte.model(self.chantier)
        self.assertTrue(self._warnings(model, "chantier-clos-taches-vivantes"))

    def test_l_ancienne_valeur_ouvert_reste_lue_comme_un_chantier_ouvert(self):
        # Un state.json ecrit par une version anterieure porte "ouvert" et non "open".
        # Le lire comme ferme crierait au chantier clos sur un chantier parfaitement vivant,
        # a chaque rafraichissement. C'est toute la raison d'etre du tuple a deux entrees.
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running")
        with store.locked() as state:
            state["chantiers"][self.chantier]["state"] = "ouvert"
        model = carte.model(self.chantier)
        self.assertEqual(self._warnings(model, "chantier-clos-taches-vivantes"), [])

    def test_deux_taches_finies_qui_partagent_une_zone_ne_sont_pas_un_conflit(self):
        # Une tache finie n'ecrit plus rien : la signaler en conflit noierait les vrais
        # conflits sous du bruit qui grossit a chaque tache terminee.
        a = self._add("0.1 a", touches=["index.js"])
        b = self._add("0.2 b", touches=["index.js"])
        self._set_state(a["id"], state="done")
        self._set_state(b["id"], state="done")
        model = carte.model(self.chantier)
        self.assertEqual(self._warnings(model, "zone-partagee"), [])


# ---------------------------------------------------------------------------
# Rendu HTML
# ---------------------------------------------------------------------------


class TestHtml(CarteTestCase):
    """La page ne rend plus de balisage par tache : elle porte les donnees en JSON et les
    pose dans le DOM par textContent. Ces tests portent donc sur ce qui traverse la page,
    pas sur des balises ; la structure visible est verifiee au navigateur, la ou elle
    existe reellement."""

    def _page(self) -> str:
        return carte.html(carte.model(self.chantier))

    def test_la_page_porte_les_identifiants_et_les_titres(self):
        a = self._add("0.1 Recette locale")
        page = self._page()
        self.assertIn(a["id"], page)
        self.assertIn("Recette locale", page)

    def test_la_page_porte_le_libelle_et_le_pourquoi_de_la_phase(self):
        self._add("0.1 a")
        chantier.set_group(self.chantier, "0", "Socle sequentiel", why="la base d abord")
        page = self._page()
        self.assertIn("Socle sequentiel", page)
        self.assertIn("la base d abord", page)

    def test_le_rafraichissement_automatique_depend_de_l_intervalle(self):
        self._add("0.1 a")
        model = carte.model(self.chantier)
        self.assertIn('http-equiv="refresh"', carte.html(model, interval=5))
        self.assertNotIn('http-equiv="refresh"', carte.html(model, interval=0))

    def test_la_page_porte_le_calque_des_arcs(self):
        # Les arcs sont traces par le navigateur depuis la position reelle des lignes :
        # Python ne peut pas les calculer, il pose le calque et les identifiants.
        a = self._add("0.1 a")
        self._add("0.2 b", depends_on=[a["id"]])
        self.assertIn('id="wires"', self._page())

    def test_le_selecteur_de_vue_et_le_filtre_de_recherche_ont_disparu(self):
        # t-48 : la vue liste, la recherche et le filtre reste/tout n'étaient jamais
        # utilisés -- retirés au profit du graphe, seul mode désormais.
        self._add("0.1 a")
        page = self._page()
        for cle in ('id="v-graphe"', 'id="v-liste"', 'id="f-reste"', 'id="f-tout"', 'id="q"'):
            self.assertNotIn(cle, page)

    def test_la_page_annonce_les_phases_declarees_mais_non_decoupees(self):
        self._add("0.1 a")
        chantier.set_group(self.chantier, "3", "Routes")
        page = self._page()
        self.assertIn("phases annonc", page)
        self.assertIn("Routes", page)

    def test_la_page_dit_combien_de_taches_restent_sans_explication(self):
        self._add("0.1 a")
        self.assertIn("sans explication", self._page())

    def test_un_chantier_sans_tache_rend_une_page_qui_le_dit(self):
        self.assertIn("aucune tâche", self._page().lower())

    def test_la_page_est_du_html_complet(self):
        self._add("0.1 a")
        page = self._page()
        self.assertTrue(page.lstrip().lower().startswith("<!doctype html>"))
        self.assertIn("</html>", page)


class TestPastilleDAlertes(CarteTestCase):
    """t-48 : les alertes ne s'étalent plus en bandeau -- une pastille comptée (triangle
    SVG, nombre) ouvre leur liste complète dans le calque déjà utilisé par les questions
    (#ask/#warn partagent leur CSS, paintTop() reprend la construction de paintAsk()),
    jamais un second calque écrit pour la même idée. Deux états seulement comptent : aucune
    alerte, où la pastille doit rester injoignable, et plusieurs, où le compte affiché et
    la liste ouverte au clic doivent venir du même tableau -- ce sont eux qui feraient
    disparaître une information sans que personne ne le voie."""

    def _page(self) -> str:
        return carte.html(carte.model(self.chantier))

    def test_sans_alerte_la_pastille_reste_masquee_par_construction(self):
        # Aucune tâche créée : aucun avertissement possible côté modèle, et côté JS la
        # pastille ne se démasque jamais hors de la condition D.warnings.length.
        self.assertEqual(carte.model(self.chantier)["warnings"], [])
        page = self._page()
        self.assertIn('id="warnchip" hidden>', page)
        self.assertIn("wc.hidden=!D.warnings.length;", page)

    def test_avec_des_alertes_le_modele_les_porte_toutes_jusqu_au_json_servi(self):
        # Une checklist non cochée sur une tâche déclarée "done" : un avertissement réel
        # produit par _avertissements(), pas injecté à la main -- la même source que la
        # page servie en vrai.
        a = self._add("0.1 a", checklist=["c un"])
        self._set_state(a["id"], state="done")
        m = carte.model(self.chantier)
        self.assertGreaterEqual(len(m["warnings"]), 1)
        # vue() est ce qui atterrit dans window.ORDO : le compte que verra la pastille et
        # la liste qu'ouvrira le calque viennent tous deux de ce même tableau JSON.
        self.assertEqual(len(carte.vue(m)["warnings"]), len(m["warnings"]))

    def test_le_compte_de_la_pastille_et_la_liste_du_calque_viennent_du_meme_tableau(self):
        page = self._page()
        self.assertIn("if(nw)nw.textContent=D.warnings.length;", page)
        self.assertIn(
            'D.warnings.forEach(function(w){listeW.appendChild(el("div",null,w.detail))});',
            page,
        )

    def test_le_calque_des_alertes_reutilise_celui_des_questions(self):
        page = self._page()
        self.assertIn("#ask,#warn{position:fixed;inset:0;", page)
        self.assertIn('var boiteW=el("div","askbox");', page)
        self.assertIn('var teteW=el("div","askhead");', page)

    def test_le_triangle_d_alerte_est_du_svg_inline_pas_une_police_d_icones(self):
        page = self._page()
        i = page.index('id="warnchip"')
        j = page.index("</span>", i)
        chip = page[i:j]
        self.assertIn("<svg", chip)
        self.assertIn('id="n-warn"', chip)
        self.assertNotIn("font-family", chip)

    def test_le_display_pose_sur_warnchip_ne_court_circuite_pas_hidden(self):
        # Régression trouvée en mesure réelle (t-48) : #warnchip{display:inline-flex}
        # prime, par spécificité d'identifiant, sur la règle [hidden] du navigateur --
        # sans cette règle dédiée, le triangle restait visible même à zéro alerte.
        page = self._page()
        self.assertIn("#warnchip[hidden]{display:none}", page)

    def test_le_calque_des_alertes_ne_s_ouvre_pas_tout_seul_au_chargement(self):
        # Régression trouvée en mesure réelle (t-48) : hérité de l'ancien bandeau,
        # `cache` par défaut valait false, donc un calque plein écran s'ouvrait tout seul
        # dès qu'il y avait au moins une alerte, avant même que le clic n'ait eu lieu.
        page = self._page()
        self.assertIn("var cache=true;", page)


class TestDetailDeTache(CarteTestCase):
    """Le panneau de détail transpose une maquette dessinée à la main (t-28) : chaîne sans
    maillon central redessiné, jauges comparatives au chantier, composition des jetons,
    frise, zones en chips, critère en cours désigné. Ces tests portent sur le texte de la
    page comme le reste de TestHtml : le rendu réel se vérifie au navigateur."""

    def _page(self) -> str:
        return carte.html(carte.model(self.chantier))

    def test_le_maillon_central_n_est_plus_redessine(self):
        # La carte elle-même EST déjà le maillon central sous la chaîne attend/débloque :
        # la boîte qui répétait id et titre a disparu au profit d'un simple séparateur.
        self._add("0.1 a")
        page = self._page()
        self.assertNotIn("selfbox", page)
        self.assertIn('el("div","sep")', page)

    def test_les_faits_bruts_ne_sont_plus_boucles_en_grille(self):
        # L'ancien détail listait Object.keys(t.facts) en grille clé/valeur ; chaque fait a
        # désormais son affichage dédié (bandeau, jauges, frise, zones), et ce code mort a
        # disparu.
        self._add("0.1 a")
        page = self._page()
        self.assertNotIn("Object.keys(t.facts)", page)

    def test_le_detail_porte_les_jauges_comparatives_et_la_composition_des_jetons(self):
        self._add("0.1 a")
        page = self._page()
        for marqueur in ("function ordoContexte(", 'el("div","gauges")', "function jauge(",
                          'el("div","jetons")', "jetons entres", "jetons sortis",
                          "cache cree", "cache relu"):
            self.assertIn(marqueur, page, marqueur)

    def test_le_detail_designe_le_critere_en_cours_par_une_case_dediee(self):
        # currentItem (ordo check --doing) est comparé au texte de chaque critère : la case
        # correspondante prend l'état "doing", distinct du simple "on" d'un critère coché.
        self._add("0.1 a")
        page = self._page()
        self.assertIn('"box"+st', page)
        self.assertIn("i===doingIdx", page)

    def test_chaque_critere_affiche_sa_propre_duree(self):
        # t-31 : la moitié manquante du contrat -- la composition du total, critère par
        # critère, jamais seulement le total.
        self._add("0.1 a")
        page = self._page()
        self.assertIn('el("span","cdur",c.duree)', page)

    def test_le_total_du_detail_reutilise_restant_jamais_un_second_calcul(self):
        # c4 : le total du détail DOIT être le même nombre que celui de la case fermée
        # (span .rest, brief t-27) -- en réutilisant t.restant tel quel, jamais une somme
        # refaite en JS sur cl[i].dureeMin qui finirait par diverger.
        self._add("0.1 a")
        page = self._page()
        bloc = page[page.index("function detailNode(t){"):page.rindex("return d;")]
        self.assertIn("t.restant", bloc)
        self.assertNotIn("dureeMin", bloc)

    def test_les_couleurs_ajoutees_sont_des_variables_pas_du_dur(self):
        # Les teintes qui viennent de la maquette et n'avaient pas d'équivalent dans le
        # thème existant sont posées en variables sur :root, jamais collées en dur dans les
        # règles du détail.
        self._add("0.1 a")
        page = self._page()
        for var in ("--done2:", "--lien:", "--relu:", "--badge:"):
            self.assertIn(var, page, var)


# ---------------------------------------------------------------------------
# Projection consommee par la page
# ---------------------------------------------------------------------------


class TestVue(CarteTestCase):
    """carte.vue() : la forme que la page lit, distincte du modele qu'Ordo calcule.

    Deux formes et pas une seule parce qu'elles servent deux lecteurs : model() est le
    modele d'Ordo, stable, teste, et sert aussi `--json` ; vue() est ce que le rendu
    consomme, aplati pour lui. Les melanger obligerait a changer le contrat de `--json`
    a chaque retouche de la page.
    """

    def _vue(self) -> dict:
        return carte.vue(carte.model(self.chantier))

    def _tache(self, vue: dict, tid: str) -> dict:
        return next(t for t in vue["tasks"] if t["id"] == tid)

    def test_chaque_tache_du_modele_est_dans_la_vue(self):
        self._add("0.1 a")
        self._add("1.1 b")
        vue = self._vue()
        self.assertEqual([t["id"] for t in vue["tasks"]], ["t-01", "t-02"])

    def test_une_tache_porte_ce_que_la_page_consomme(self):
        a = self._add("0.1 a", why="parce que", touches=["index.js"], checklist=["c un"])
        self._add("0.2 b", depends_on=[a["id"]])
        t = self._tache(self._vue(), a["id"])
        for cle in ("id", "title", "status", "ready", "meta", "facts", "deps",
                    "dependants", "why", "whyAbsent", "waits", "checklist", "docs"):
            self.assertIn(cle, t, cle)
        self.assertEqual(t["deps"], [])
        self.assertEqual(t["dependants"], ["t-02"])
        self.assertEqual(
            t["checklist"], [{"text": "c un", "done": False, "duree": "", "attrs": []}]
        )
        self.assertIn("index.js", t["facts"]["zones"])

    def test_une_tache_sans_pourquoi_le_declare_au_lieu_de_rendre_du_vide(self):
        # whyAbsent est ce qui permet a la page de dire "aucune explication" plutot que
        # d'afficher un cadre vide, qu'un lecteur prendrait pour un defaut d'affichage.
        a = self._add("0.1 a")
        t = self._tache(self._vue(), a["id"])
        self.assertTrue(t["whyAbsent"])
        self.assertEqual(t["why"], "")

    def test_les_blocages_sont_des_liens_cliquables_pas_du_texte(self):
        a = self._add("0.1 a")
        b = self._add("0.2 b", depends_on=[a["id"]])
        t = self._tache(self._vue(), b["id"])
        self.assertEqual([w["id"] for w in t["waits"]], [a["id"]])
        self.assertIn("queued", t["waits"][0]["text"])

    def test_les_documents_longs_sont_ranges_a_part_du_reste(self):
        a = self._add("0.1 a", prompt="CONTEXTE COMMUN\nfais ceci")
        self._set_state(a["id"], report={"state": "done", "note": "vert 8/8"})
        journal.write(self.chantier, "ORCH", f"{a['id']} passe avant le reste")
        docs = {d["k"]: d["text"] for d in self._tache(self._vue(), a["id"])["docs"]}
        self.assertIn("fais ceci", docs["prompt"])
        self.assertIn("vert 8/8", docs["rapport"])
        self.assertIn("passe avant le reste", docs["decisions"])

    def test_une_tache_sans_rapport_n_expose_pas_un_document_vide(self):
        a = self._add("0.1 a")
        cles = [d["k"] for d in self._tache(self._vue(), a["id"])["docs"]]
        self.assertEqual(cles, ["prompt"])

    def test_les_phases_gardent_leur_ordre_et_portent_leurs_taches(self):
        self._add("1.1 b")
        self._add("0.1 a")
        chantier.set_group(self.chantier, "0", "Socle", why="la base")
        vue = self._vue()
        self.assertEqual([p["key"] for p in vue["phases"]], ["0", "1"])
        self.assertEqual(vue["phases"][0]["name"], "Socle")
        self.assertEqual(vue["phases"][0]["why"], "la base")
        self.assertEqual(vue["phases"][0]["order"], ["t-02"])

    def test_une_phase_annoncee_traverse_la_vue_avec_zero_tache(self):
        self._add("0.1 a")
        chantier.set_group(self.chantier, "3", "Routes")
        vue = self._vue()
        phase = next(p for p in vue["phases"] if p["key"] == "3")
        self.assertEqual(phase["order"], [])
        self.assertTrue(phase["planned"])


class TestPageDeRendu(CarteTestCase):
    def test_les_donnees_voyagent_en_json_dans_la_page(self):
        self._add("0.1 Recette locale")
        page = carte.html(carte.model(self.chantier))
        self.assertIn("window.ORDO=", page)
        self.assertIn("Recette locale", page)

    def test_aucune_donnee_ne_peut_fermer_le_script_qui_la_porte(self):
        # Le piege du JSON dans une page : "</script>" dans un prompt termine la balise et
        # tout ce qui suit devient du balisage. La page se casserait, et une donnee
        # choisie fermerait la balise puis en ouvrirait une autre.
        self._add("0.1 a", prompt='fin </script><script>alert(1)</script> suite')
        page = carte.html(carte.model(self.chantier))
        self.assertNotIn("</script><script>alert(1)", page)
        self.assertIn("\\u003c", page)

    def test_un_titre_hostile_ne_peut_pas_s_executer(self):
        self._add("<img src=x onerror=alert(1)>")
        page = carte.html(carte.model(self.chantier))
        self.assertNotIn("<img src=x", page)

    def test_la_page_ne_charge_aucune_police_ni_ressource_distante(self):
        # Le design d'origine appelait Google Fonts. Une carte doit rester lisible sur une
        # machine sans reseau, et ne rien dire a personne de ce qu'on regarde.
        self._add("0.1 a")
        page = carte.html(carte.model(self.chantier))
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<link", page)
        self.assertNotIn("@import", page)


class TestJetons(CarteTestCase):
    """Les jetons consommes, sur la carte. usage.pour() est injecte, comme la vivacite des
    panes : le modele ne va pas lire les transcripts de la machine par lui-meme."""

    def test_un_noeud_porte_les_jetons_quand_on_sait_les_lire(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running")
        model = carte.model(
            self.chantier,
            usage_de=lambda t: {"input": 7, "output": 112221, "cacheCreation": 0,
                                "cacheRead": 25480808, "turns": 30},
        )
        self.assertEqual(model["nodes"][a["id"]]["usage"]["output"], 112221)

    def test_sans_lecteur_de_jetons_le_noeud_ne_pretend_rien(self):
        # None et pas zero : zero se lirait comme "cette tache n'a rien consomme".
        a = self._add("0.1 a")
        self.assertIsNone(carte.model(self.chantier)["nodes"][a["id"]]["usage"])

    def test_les_jetons_arrivent_dans_la_vue_en_clair_et_abreges(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running")
        vue = carte.vue(carte.model(
            self.chantier,
            usage_de=lambda t: {"input": 0, "output": 112221, "cacheCreation": 3000,
                                "cacheRead": 25480808, "turns": 30, "dernierContexte": 48000},
        ))
        t = vue["tasks"][0]
        # 48k, le contexte du dernier tour, et non 112k qui est la sortie cumulée de la
        # session : c'est exactement le chiffre que ce module s'est trompé d'afficher.
        self.assertEqual(t["tokens"], "48k")
        self.assertIn("112221", t["facts"]["jetons sortis"])
        self.assertIn("25480808", t["facts"]["cache relu"])
        self.assertEqual(t["facts"]["tours"], "30")

    def test_une_tache_sans_jetons_n_encombre_pas_ses_faits(self):
        self._add("0.1 a")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["tokens"], "")
        self.assertNotIn("jetons sortis", t["facts"])


class TestDureeSurLaCarte(CarteTestCase):
    def test_la_duree_d_une_tache_finie_est_dans_la_vue(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="done", startedAt="2020-01-01T00:00:00Z",
                        finishedAt="2020-01-01T00:07:00Z")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["duree"], "7m")
        self.assertEqual(t["elapsedS"], 420)

    def test_une_tache_jamais_lancee_n_affiche_aucune_duree(self):
        # "-" se lirait comme une duree nulle ; la verite est qu'elle n'a pas commence.
        self._add("0.1 a")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["duree"], "")
        self.assertIsNone(t["elapsedS"])

    def test_la_duree_ne_depend_pas_de_la_vue_choisie(self):
        # Elle etait auparavant dans la ligne de meta, cachee en vue graphe : une carte qui
        # ne dit pas depuis combien de temps une tache tourne ne sert a rien pendant qu'elle
        # tourne, c'est-a-dire au seul moment ou on la regarde.
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", startedAt="2020-01-01T00:00:00Z")
        page = carte.html(carte.model(self.chantier))
        self.assertIn('"duree"', page)


class TestRestantEtDepassementSurLaCarte(CarteTestCase):
    """Restant et dépassement (brief t-27) : l'estimation vit sur CHAQUE critère, le total
    et le restant de la tâche se calculent, ils ne se saisissent jamais. Le temps passé
    réutilise elapsedS, déjà mesuré depuis startedAt (voir TestDureeSurLaCarte) : jamais
    un second champ que l'exécutante pourrait déclarer elle-même (c5)."""

    def test_restant_est_la_somme_des_criteres_non_coches(self):
        a = self._add("0.1 a", checklist=["c un", "c deux", "c trois"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {"id": cl[0]["id"], "label": "c un", "done": True, "dureeMin": 5},
            {"id": cl[1]["id"], "label": "c deux", "done": False, "dureeMin": 8},
            {"id": cl[2]["id"], "label": "c trois", "done": False, "dureeMin": 12},
        ])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["restant"], "20m")

    def test_restant_vide_quand_un_critere_non_coche_na_pas_destimation(self):
        # Un restant partiel comprendrait le vrai restant, jamais l'inverse : mieux vaut
        # ne rien dire que sous-estimer (c8, critère sans durée toléré).
        a = self._add("0.1 a", checklist=["c un", "c deux"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {"id": cl[0]["id"], "label": "c un", "done": False, "dureeMin": 5},
            {"id": cl[1]["id"], "label": "c deux", "done": False},
        ])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["restant"], "")

    def test_restant_zero_quand_tout_est_coche(self):
        a = self._add("0.1 a", checklist=["c un"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {"id": cl[0]["id"], "label": "c un", "done": True, "dureeMin": 5},
        ])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["restant"], "0m")

    def test_restant_vide_sur_une_checklist_sans_aucune_duree(self):
        # Les 349 critères existants : aucun n'a de durée, la case ne doit rien inventer.
        self._add("0.1 a", checklist=["c un", "c deux"])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["restant"], "")

    def test_chaque_critere_porte_sa_duree_formatee(self):
        # Le détail montre la durée de CHAQUE critère, pas seulement le total (t-31) --
        # même formatage que le restant, via _duree_min, jamais une seconde formule.
        a = self._add("0.1 a", checklist=["c un", "c deux"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {"id": cl[0]["id"], "label": "c un", "done": False, "dureeMin": 5},
            {"id": cl[1]["id"], "label": "c deux", "done": False, "dureeMin": 75},
        ])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["checklist"][0]["duree"], "5m")
        self.assertEqual(t["checklist"][1]["duree"], "1h15")

    def test_un_critere_sans_estimation_ne_porte_aucune_duree(self):
        # Cas MAJORITAIRE aujourd'hui (t-31, c3/c7) : chaîne vide, jamais un tiret ni un
        # zéro qui se liraient comme une valeur connue.
        a = self._add("0.1 a", checklist=["c un"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {"id": cl[0]["id"], "label": "c un", "done": False},
        ])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["checklist"][0]["duree"], "")

    def test_depassement_vide_tant_que_le_passe_ne_depasse_pas_lestime(self):
        a = self._add("0.1 a", checklist=["c un"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(
            a["id"], state="done",
            startedAt="2020-01-01T00:00:00Z", finishedAt="2020-01-01T00:10:00Z",
            checklist=[{"id": cl[0]["id"], "label": "c un", "done": True, "dureeMin": 30}],
        )
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["depassement"], "")

    def test_depassement_affiche_quand_le_passe_depasse_le_total_estime(self):
        # c7/c10 : un restant négatif affiché en zéro serait un mensonge -- ce test échoue
        # si le dépassement cesse d'apparaître une fois le budget dépassé. state="running"
        # et non "done" (retouche t-38) : une tâche TERMINÉE montre désormais l'écart,
        # jamais le dépassement -- voir TestExclusiviteEcartEtDepassement ci-dessous.
        a = self._add("0.1 a", checklist=["c un"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(
            a["id"], state="running",
            startedAt="2020-01-01T00:00:00Z", finishedAt="2020-01-01T00:12:00Z",
            checklist=[{"id": cl[0]["id"], "label": "c un", "done": True, "dureeMin": 5}],
        )
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["depassement"], "+7m")

    def test_depassement_vide_sans_aucune_estimation_connue(self):
        a = self._add("0.1 a", checklist=["c un"])
        self._set_state(
            a["id"], state="done",
            startedAt="2020-01-01T00:00:00Z", finishedAt="2020-01-01T01:00:00Z",
        )
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["depassement"], "")

    def test_depassement_vide_sur_une_tache_jamais_lancee(self):
        self._add("0.1 a", checklist=[{"label": "c un", "dureeMin": 5}])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["depassement"], "")

    def test_le_chip_de_depassement_est_inconditionnel_comme_la_duree(self):
        # Même traitement que t.duree (voir TestCondensationDesTachesReglees) : visible
        # même sur une case réglée et repliée, jamais soumis à `condense`.
        page = carte.html(carte.model(self.chantier))
        bloc = page[page.index("function rowNode(t){"):page.index("function detailNode(t){")]
        garde_checklist = bloc.index("if(!condense&&!jamaisLancee){")
        self.assertLess(bloc.index('el("span","dur",durTxt)'), garde_checklist)
        self.assertLess(bloc.index("t.depassement"), garde_checklist)

    def test_ecart_ne_repete_pas_le_reel_deja_affiche_par_la_duree(self):
        # Retouche demandée après coup (brief t-33) : t.duree affiche déjà le temps
        # passé juste à côté. Le rappel de l'estimé ("ecart") a lui-même disparu de la
        # case (retouche t-41) : seule la différence signée reste, directement à côté de
        # la durée -- "23m -1h27", jamais "23m estimé 1h50 -1h27", pour que le nom de
        # tâche, le modèle et ces deux nombres tiennent sur une seule ligne.
        a = self._add("0.1 a", checklist=["c un"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(
            a["id"], state="done",
            startedAt="2026-08-01T00:00:00Z", finishedAt="2026-08-01T00:23:00Z",
            checklist=[{"id": cl[0]["id"], "label": "c un", "done": True, "dureeMin": 110}],
        )
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertNotIn("ecart", t)
        self.assertEqual(t["ecartValeur"], "-1h27")

    def test_ecart_valeur_vide_sans_aucune_estimation_connue(self):
        a = self._add("0.1 a", checklist=["c un"])
        self._set_state(
            a["id"], state="done",
            startedAt="2026-08-01T00:00:00Z", finishedAt="2026-08-01T01:00:00Z",
        )
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["ecartValeur"], "")

    def test_ecart_valeur_vide_sur_une_tache_pas_terminee(self):
        a = self._add("0.1 a", checklist=[{"label": "c un", "dureeMin": 5}])
        self._set_state(a["id"], startedAt="2026-08-01T00:00:00Z")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["ecartValeur"], "")


class TestAttributsSurLaCarte(CarteTestCase):
    """Marqueur compact par attribut (brief t-39) : t-36 pose jusqu'à cinq attributs de
    nature par critère (item["attributs"]), rien ne les montrait avant cette tâche. c8 :
    un critère qui n'a jamais reçu d'attribut (cas MAJORITAIRE, 201 critères de camcast
    aujourd'hui) ne doit produire aucun placeholder, la liste "attrs" reste vide."""

    def test_un_critere_sans_attribut_ne_porte_aucun_marqueur(self):
        a = self._add("0.1 a", checklist=["c un"])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["checklist"][0]["attrs"], [])

    def test_un_critere_porte_un_marqueur_par_attribut_renseigne(self):
        a = self._add("0.1 a", checklist=["c un"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {
                "id": cl[0]["id"], "label": "c un", "done": False,
                "attributs": {"geste": "lire", "dependance": "aucune"},
            },
        ])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(
            t["checklist"][0]["attrs"],
            [
                {"cle": "geste", "valeur": "lire"},
                {"cle": "dependance", "valeur": "aucune"},
            ],
        )

    def test_lordre_des_marqueurs_suit_toujours_attributs_valeurs_jamais_la_pose(self):
        # Posés dans l'ordre inverse de chantier.ATTRIBUTS_VALEURS : la vue doit quand
        # même les rendre dans l'ordre canonique, pas l'ordre d'écriture du dict Python
        # (qui suit l'ordre d'insertion), sans quoi deux exécutantes qui posent les mêmes
        # attributs dans un ordre différent feraient bouger les marqueurs à l'écran.
        a = self._add("0.1 a", checklist=["c un"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {
                "id": cl[0]["id"], "label": "c un", "done": False,
                "attributs": {"validation": "assertion", "geste": "tester"},
            },
        ])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(
            [a["cle"] for a in t["checklist"][0]["attrs"]], ["geste", "validation"]
        )

    def test_le_marqueur_ecrit_la_cle_et_la_valeur_en_clair_jamais_une_initiale(self):
        # Retouche demandée par l'humain, même passe que t-44 : "M" puis la valeur seule
        # (t-41) ne disaient pas de quoi il s'agissait sans déjà connaître les cinq clés
        # par cœur -- la clé traverse maintenant à l'écran, à côté de sa valeur.
        self._add("0.1 a")
        page = carte.html(carte.model(self.chantier))
        self.assertIn('mk.appendChild(el("span","cattrk m",a.cle))', page)
        self.assertIn('mk.appendChild(el("span","cattrv m"," : "+a.valeur))', page)
        # charAt(0) existe ailleurs dans le fichier (classeEcart, sur le signe de l'écart) :
        # la garde porte donc sur le bloc des marqueurs, pas sur la page entière.
        bloc = page[page.index("c.attrs.forEach"):page.index("ca.appendChild(mk)")]
        self.assertNotIn("charAt(0)", bloc)

    def test_seule_la_cle_peut_se_couper_jamais_la_valeur(self):
        # Retouche de l'humain : "si une paire ne tient pas, abrège la clé et jamais la
        # valeur". .cattrk (la clé) peut donc se réduire et se couper à l'ellipse ;
        # .cattrv (le séparateur et la valeur) reste flex:none, jamais réduit.
        page = carte.html(carte.model(self.chantier))
        self.assertIn(
            ".cattrk{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;"
            "white-space:nowrap}",
            page,
        )
        self.assertIn(".cattrv{flex:none;white-space:nowrap}", page)

    def test_la_cle_reste_lisible_au_survol_et_sans_bouger_la_souris(self):
        # Invariant 3 du brief : title au survol au minimum, aria-label en plus pour qui
        # navigue au clavier ou au lecteur d'écran -- jamais besoin de bouger la souris
        # pour retrouver la clé, y compris si elle s'est coupée à l'écran.
        self._add("0.1 a")
        page = carte.html(carte.model(self.chantier))
        self.assertIn('var titre=a.cle+" : "+a.valeur', page)
        self.assertIn("mk.title=titre", page)
        self.assertIn('mk.setAttribute("aria-label",titre)', page)

    def test_les_marqueurs_sont_sous_le_libelle_jamais_a_cote(self):
        # Invariant 1 du brief : le libellé du critère ne cède jamais sa place. La coche
        # et le libellé vivent dans .ctop, les marqueurs dans .cattrs juste après, à
        # l'extérieur de .ctop -- ils tombent donc sur leur propre ligne, jamais à côté du
        # libellé qu'ils décrivent.
        self._add("0.1 a")
        page = carte.html(carte.model(self.chantier))
        top = page[page.index('var top=el("div","ctop")'):page.index('line.appendChild(top)')]
        self.assertNotIn("cattrs", top)
        apres = page[page.index('line.appendChild(top)'):page.index("ck.appendChild(line)")]
        self.assertIn("cattrs", apres)

    def test_les_marqueurs_passent_a_la_ligne_plutot_que_de_deborder(self):
        # c4/c5 du brief : mesuré sur camcast, cinq valeurs concaténées peuvent dépasser
        # la largeur d'une colonne de mur (340px, sa plus étroite) -- flex-wrap plutôt
        # qu'une troncature, pour ne jamais couper une valeur au milieu.
        page = carte.html(carte.model(self.chantier))
        self.assertIn(".cattrs{display:flex;flex-wrap:wrap;", page)

    def test_le_detail_ne_rend_le_bloc_de_marqueurs_que_sil_y_a_au_moins_un_attribut(self):
        # Point 1 du brief : l'absence est le cas courant, jamais un tiret ni une case
        # grise -- la garde en JS ne doit rien afficher tant qu'aucun attribut n'existe.
        self._add("0.1 a")
        page = carte.html(carte.model(self.chantier))
        self.assertIn("if(c.attrs&&c.attrs.length)", page)

    def test_le_pire_cas_de_longueur_mesure_sur_camcast_ne_deloge_pas_le_libelle(self):
        # c8 du brief : pire cas RÉEL mesuré sur les 201 critères qualifiés de camcast
        # (t-41), pas le pire cas théorique -- un libellé de 41 caractères et ses cinq
        # attributs concaténés à 47 caractères. Le libellé traverse tel quel, jamais
        # tronqué ni raccourci : c'est aux attributs de céder de la place, jamais à lui
        # (invariant 1).
        libelle = "Les quatre exemples s'accordent entre eux"
        self.assertEqual(len(libelle), 41)
        a = self._add("0.1 a", checklist=[libelle])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        chantier.set_checklist_attribut(a["id"], cl[0]["id"], "geste", "mesurer")
        chantier.set_checklist_attribut(a["id"], cl[0]["id"], "etendue", "module")
        chantier.set_checklist_attribut(a["id"], cl[0]["id"], "dependance", "navigateur")
        chantier.set_checklist_attribut(a["id"], cl[0]["id"], "incertitude", "connu")
        chantier.set_checklist_attribut(a["id"], cl[0]["id"], "validation", "observation")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["checklist"][0]["text"], libelle)
        valeurs = " ".join(x["valeur"] for x in t["checklist"][0]["attrs"])
        self.assertEqual(len(valeurs), 43)
        page = carte.html(carte.model(self.chantier))
        # Le libellé traverse la page intact, jamais coupé : aucune troncature côté
        # Python, la seule limite de largeur vit en CSS (flex-wrap), pas dans la donnée.
        self.assertIn(libelle, page)

    def test_la_plus_longue_paire_cle_valeur_reelle_reste_courte(self):
        # Calculée depuis chantier.ATTRIBUTS_VALEURS, source unique (jamais une seconde
        # liste dupliquée ici) : la paire "clé : valeur" la plus longue qu'un marqueur
        # puisse réellement porter aujourd'hui. Le repli qui coupe la clé à l'ellipse
        # (.cattrk, voir CSS) existe pour ce qui dépasserait un jour ce maximum réel,
        # jamais déclenché par lui.
        paires = [
            f"{cle} : {valeur}"
            for cle, valeurs in chantier.ATTRIBUTS_VALEURS.items()
            for valeur in valeurs
        ]
        pire = max(paires, key=len)
        self.assertEqual(pire, "validation : observation")
        self.assertEqual(len(pire), 24)


class TestExclusiviteEcartEtDepassement(CarteTestCase):
    """Retouche demandée par l'humain (brief t-38) : sur une tâche TERMINÉE en
    dépassement, _depassement_min et _ecart_valeur_texte disaient le même fait deux fois
    -- "+20m" ici, "estimé 1h50 (+20m)" là. Les deux champs sont désormais exclusifs,
    décidés par l'état de la tâche."""

    def test_le_depassement_disparait_sur_une_tache_terminee_au_profit_de_lecart(self):
        # Exactement le scénario rapporté : une tâche finie qui a dépassé son estimé.
        a = self._add("0.1 a", checklist=["c un"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(
            a["id"], state="done",
            startedAt="2020-01-01T00:00:00Z", finishedAt="2020-01-01T00:12:00Z",
            checklist=[{"id": cl[0]["id"], "label": "c un", "done": True, "dureeMin": 5}],
        )
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["depassement"], "")
        self.assertEqual(t["ecartValeur"], "+7m")

    def test_lecart_reste_absent_sur_une_tache_en_cours_en_depassement(self):
        # Symétrique : une tâche encore en cours montre le dépassement, jamais l'écart,
        # qui n'existe pas tant que la tâche n'est pas terminée (chantier.ecart_estime_reel).
        a = self._add("0.1 a", checklist=["c un"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(
            a["id"], state="running",
            startedAt="2020-01-01T00:00:00Z", finishedAt="2020-01-01T00:12:00Z",
            checklist=[{"id": cl[0]["id"], "label": "c un", "done": True, "dureeMin": 5}],
        )
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["depassement"], "+7m")
        self.assertEqual(t["ecartValeur"], "")

    def test_le_total_estime_vit_dans_len_tete_jamais_dans_la_progression(self):
        # Retouche demandée pendant t-41 : le restant mangeait la place du libellé du
        # critère en cours dans .rprog ; il en est retiré au profit du total estimé,
        # affiché à côté de la durée écoulée dans l'en-tête (rlinks), pour que le libellé
        # récupère toute la largeur de sa ligne.
        page = carte.html(carte.model(self.chantier))
        rprog = page[page.index('el("div","rprog")'):page.index('if(sel){')]
        self.assertNotIn("t.restant", rprog)
        entete = page[page.index('var links=el("span","rlinks m")'):page.index("if(t.depassement){")]
        self.assertIn("t.totalEstime", entete)


class TestRestantEtFinDuChantierDansLEnTete(CarteTestCase):
    """En-tête de colonne (brief t-38) : le restant de TOUT le chantier et l'heure de
    fin, à gauche du pourcentage déjà présent. Réutilise _restant_min de chaque tâche,
    additionné ici -- jamais un second calcul du même nombre (_restant_chantier_min)."""

    def test_additionne_les_restants_connus_des_taches_actives(self):
        a = self._add("0.1 a", checklist=["c un"])
        cl_a = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {"id": cl_a[0]["id"], "label": "c un", "done": False, "dureeMin": 20},
        ])
        b = self._add("0.2 b", checklist=["c un"])
        cl_b = carte.model(self.chantier)["nodes"][b["id"]]["checklist"]
        self._set_state(b["id"], checklist=[
            {"id": cl_b[0]["id"], "label": "c un", "done": False, "dureeMin": 40},
        ])
        v = carte.vue(carte.model(self.chantier))
        self.assertEqual(v["restantChantierMin"], 60)

    def test_ignore_les_taches_finies_ou_annulees(self):
        a = self._add("0.1 a", checklist=["c un"])
        cl_a = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {"id": cl_a[0]["id"], "label": "c un", "done": False, "dureeMin": 20},
        ])
        b = self._add("0.2 b", checklist=["c un"])
        cl_b = carte.model(self.chantier)["nodes"][b["id"]]["checklist"]
        self._set_state(
            b["id"], state="done",
            checklist=[{"id": cl_b[0]["id"], "label": "c un", "done": False, "dureeMin": 999}],
        )
        c = self._add("0.3 c", checklist=["c un"])
        cl_c = carte.model(self.chantier)["nodes"][c["id"]]["checklist"]
        self._set_state(
            c["id"], state="cancelled",
            checklist=[{"id": cl_c[0]["id"], "label": "c un", "done": False, "dureeMin": 999}],
        )
        v = carte.vue(carte.model(self.chantier))
        self.assertEqual(v["restantChantierMin"], 20)

    def test_ignore_une_tache_partiellement_inconnue_sans_taire_les_autres(self):
        # a : un critère non coché sans durée -> _restant_min(a) répond None, la tâche
        # entière est exclue -- pas ses 5 minutes connues comptées à part.
        a = self._add("0.1 a", checklist=["c un", "c deux"])
        cl_a = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {"id": cl_a[0]["id"], "label": "c un", "done": False, "dureeMin": 5},
            {"id": cl_a[1]["id"], "label": "c deux", "done": False},
        ])
        b = self._add("0.2 b", checklist=["c un"])
        cl_b = carte.model(self.chantier)["nodes"][b["id"]]["checklist"]
        self._set_state(b["id"], checklist=[
            {"id": cl_b[0]["id"], "label": "c un", "done": False, "dureeMin": 15},
        ])
        v = carte.vue(carte.model(self.chantier))
        self.assertEqual(v["restantChantierMin"], 15)

    def test_absent_quand_rien_nest_estime(self):
        self._add("0.1 a", checklist=["c un"])
        v = carte.vue(carte.model(self.chantier))
        self.assertIsNone(v["restantChantierMin"])

    def test_len_tete_porte_les_spans_restant_et_fin_avant_le_pourcentage(self):
        page = carte.html(carte.model(self.chantier))
        h1 = page[page.index("<h1>"):page.index("</h1>")]
        self.assertLess(h1.index('id="hrest"'), h1.index('id="pct"'))
        self.assertLess(h1.index('id="hfin"'), h1.index('id="pct"'))

    def test_le_panneau_de_colonne_porte_aussi_les_spans(self):
        p = carte.panneau("/tmp/ordo-home", "c-01")
        h1 = p[p.index("<h1>"):p.index("</h1>")]
        self.assertLess(h1.index('id="hrest"'), h1.index('id="pct"'))
        self.assertLess(h1.index('id="hfin"'), h1.index('id="pct"'))


class TestFinDeChantierSousNode(CarteTestCase):
    """L'heure de fin et son exposant de jour (brief t-38) se calculent au rendu, côté
    navigateur (voir paintHeure() dans _JS) : aucune assertion de chaîne ne peut prouver
    un calcul de date, seule son exécution le peut -- même principe que
    TestMurDistingueTroisEtats pour _MUR_JS."""

    def _fonctions(self) -> str:
        if shutil.which("node") is None:
            self.skipTest("node absent, impossible d'exécuter le JS de l'en-tête")
        js = carte._JS
        debut = js.index("function dureeMin")
        fin = js.index("function paintHeure")
        return js[debut:fin]

    def _appelle(self, expr: str):
        script = self._fonctions() + f"\nprocess.stdout.write(JSON.stringify({expr}));"
        sortie = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=10, check=True,
        )
        return json.loads(sortie.stdout)

    def test_duree_min_sous_lheure_donne_des_minutes(self):
        self.assertEqual(self._appelle("dureeMin(45)"), "45m")

    def test_duree_min_au_dela_dune_heure_donne_heures_et_minutes(self):
        self.assertEqual(self._appelle("dureeMin(75)"), "1h15")

    def test_fin_sans_franchir_minuit(self):
        r = self._appelle("finChantier(new Date(2026,7,16,10,0,0),60)")
        self.assertEqual(r["heure"], "11h00")
        self.assertEqual(r["jours"], 0)

    def test_fin_franchit_un_seul_minuit(self):
        # Le cas qui compte le plus (c8) : dix minutes avant minuit, vingt minutes de
        # restant, l'exposant doit dire "+1", jamais "0" -- lire "0h10" comme "dans dix
        # minutes" serait faux d'une nuit entière.
        r = self._appelle("finChantier(new Date(2026,7,16,23,50,0),20)")
        self.assertEqual(r["heure"], "0h10")
        self.assertEqual(r["jours"], 1)

    def test_fin_franchit_deux_minuits(self):
        r = self._appelle("finChantier(new Date(2026,7,16,23,50,0),1460)")
        self.assertEqual(r["jours"], 2)

    def test_fin_franchit_trois_minuits(self):
        r = self._appelle("finChantier(new Date(2026,7,16,23,50,0),2900)")
        self.assertEqual(r["jours"], 3)

    def test_classe_ecart_vert_sur_un_gain(self):
        self.assertEqual(self._appelle('classeEcart("-1h27")'), "gain")

    def test_classe_ecart_alerte_sur_un_depassement(self):
        self.assertEqual(self._appelle('classeEcart("+20m")'), "depasse")

    def test_classe_ecart_neutre_sans_ecart(self):
        self.assertEqual(self._appelle('classeEcart("0m")'), "")


class TestModeleSurLaCarte(CarteTestCase):
    """Le modele qui executera la tache, lisible sur sa case sans l'ouvrir.

    Une case qui ne dit pas quel modele va la prendre oblige a relancer `ordo routage`
    dans un terminal pour le savoir, alors que c'est exactement le chiffre qui decide si
    on relit le brief avant de lancer.
    """

    def test_une_tache_non_lancee_annonce_le_modele_prevu(self):
        # Titre de conception : le routage envoie sur opus, et rien n'a encore tourne.
        self._add("0.1 Concevoir le schema")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["model"], "opus")
        self.assertTrue(t["modelPredit"])

    def test_le_modele_prevu_est_celui_du_routage_pas_un_defaut_fixe(self):
        # Geste mecanique borne : haiku. Si la carte affichait un defaut fixe, ce test
        # verrait "opus" ici.
        self._add("0.1 Renommer le module", touches=["ordo/x.py"], checklist=["fait"])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["model"], "haiku")

    def test_le_modele_prevu_monte_avec_les_tentatives_ratees(self):
        # L'escalade de routage.pour_lancement doit se voir sur la carte : sans elle, la
        # case promettrait haiku alors que le prochain lancement partira sur opus.
        a = self._add("0.1 Renommer le module", touches=["ordo/x.py"], checklist=["fait"])
        self._set_state(a["id"], attempts=2)
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["model"], "opus")
        self.assertTrue(t["modelPredit"])

    def test_le_modele_reellement_lance_prime_sur_la_prediction(self):
        a = self._add("0.1 Concevoir le schema")
        self._set_state(a["id"], state="running", model="sonnet",
                        startedAt="2020-01-01T00:00:00Z")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["model"], "sonnet")
        self.assertFalse(t["modelPredit"])

    def test_une_tache_lancee_sans_modele_impose_ne_predit_plus_rien(self):
        # Lancee avec --model herite : claude a applique sa propre configuration. Afficher
        # la prediction du routage inventerait un modele qui n'a pas tourne.
        a = self._add("0.1 Concevoir le schema")
        self._set_state(a["id"], state="done", model=None,
                        startedAt="2020-01-01T00:00:00Z",
                        finishedAt="2020-01-01T00:01:00Z")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["model"], "defaut")
        self.assertFalse(t["modelPredit"])

    def test_le_motif_du_routage_accompagne_le_modele(self):
        # Le nom seul ne se conteste pas : ce qui se conteste est le signal qui a tranche.
        self._add("0.1 Concevoir le schema")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertIn("conception", t["modelWhy"])

    def test_le_modele_figure_dans_les_faits_de_la_tache(self):
        self._add("0.1 Concevoir le schema")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["facts"]["modele"], "opus")

    def test_la_page_de_fichier_porte_aussi_le_modele(self):
        self._add("0.1 Concevoir le schema")
        page = carte.html(carte.model(self.chantier))
        self.assertIn('"model"', page)
        self.assertIn("modelPredit", page)


class TestMur(CarteTestCase):
    """Le mur : une colonne par chantier, cote a cote, sur un seul ecran.

    Le mur ne porte AUCUNE donnee de chantier. Il pose des colonnes et leur donne une
    adresse ; chaque colonne va chercher son etat elle-meme. C'est ce qui permet a une
    colonne de se rafraichir sans que les autres perdent leur defilement.
    """

    def test_le_mur_est_du_html_complet(self):
        mur = carte.page()
        self.assertTrue(mur.lstrip().lower().startswith("<!doctype html>"))
        self.assertIn("</html>", mur)

    def test_le_mur_porte_le_conteneur_de_colonnes_et_le_bouton_d_ajout(self):
        mur = carte.page()
        self.assertIn('id="cols"', mur)
        self.assertIn('id="plus"', mur)

    def test_le_mur_ne_porte_aucune_donnee_de_chantier(self):
        # Un mur qui embarquerait l'etat serait fige des sa livraison, et il faudrait le
        # recharger en entier pour voir bouger une seule colonne.
        self._add("0.1 Recette locale")
        mur = carte.page()
        self.assertNotIn("Recette locale", mur)
        self.assertNotIn("window.ORDO=", mur)

    def test_le_mur_offre_le_plein_ecran(self):
        self.assertIn("requestFullscreen", carte.page())

    def test_le_mur_ne_charge_aucune_ressource_distante(self):
        mur = carte.page()
        self.assertNotIn("http://", mur)
        self.assertNotIn("https://", mur)
        self.assertNotIn("<link", mur)


class TestPanneau(CarteTestCase):
    """Une colonne du mur : un seul chantier, servi par le serveur local."""

    def test_une_colonne_cible_le_home_et_le_chantier_demandes(self):
        p = carte.panneau("/tmp/ordo-home", "c-7")
        self.assertIn("/tmp/ordo-home", p)
        self.assertIn("c-7", p)

    def test_une_colonne_porte_le_plateau(self):
        p = carte.panneau("/tmp/ordo-home", "c-7")
        for cle in ('id="board"', 'id="wires"'):
            self.assertIn(cle, p)

    def test_une_colonne_ne_porte_plus_le_selecteur_de_vue(self):
        # t-48 : le graphe est le seul mode, retiré au profit d'une barre plus courte.
        p = carte.panneau("/tmp/ordo-home", "c-7")
        for cle in ('id="v-graphe"', 'id="v-liste"'):
            self.assertNotIn(cle, p)

    def test_une_colonne_ne_porte_pas_le_selecteur_de_chantier(self):
        # Le choix du chantier appartient au mur : deux selecteurs pour une meme colonne
        # se contrediraient des le premier changement.
        self.assertNotIn('id="sel"', carte.panneau("/tmp/ordo-home", "c-7"))

    def test_un_home_hostile_ne_peut_pas_fermer_le_script_qui_le_porte(self):
        # Le home vient de l'URL : il est ecrit par qui forme le lien, donc jamais sur.
        p = carte.panneau('/tmp/x</script><script>alert(1)</script>', "c-7")
        self.assertNotIn("</script><script>alert(1)", p)

    def test_deux_colonnes_ne_partagent_pas_leur_etat_de_lecture(self):
        # sessionStorage est commun a toute l'origine : sans cloisonnement, ouvrir une
        # tache dans une colonne l'ouvrirait dans toutes les autres.
        a = re.search(r'ORDO_NS=("[^"]*")', carte.panneau("/tmp/a", "c-1"))
        b = re.search(r'ORDO_NS=("[^"]*")', carte.panneau("/tmp/b", "c-2"))
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertNotEqual(a.group(1), b.group(1))


class TestDefilementRestaure(CarteTestCase):
    """Le defilement d'une colonne survit a la reouverture du mur.

    Il etait ecrit dans sessionStorage a chaque mouvement et jamais relu hors du mode
    fichier : une colonne rouverte revenait toujours en haut, et un mur de trois colonnes
    demandait de tout refaire defiler a chaque ouverture.
    """

    def test_la_restauration_est_exposee_par_le_rendu(self):
        # Elle ne peut pas etre appelee depuis le rendu lui-meme : en colonne, le premier
        # dessin a lieu a la reponse du serveur, que le rendu ne voit pas.
        self.assertIn("window.ordoRestoreScroll=function", carte.html(
            carte.model(self.chantier)))

    def test_une_colonne_restaure_son_defilement_au_premier_rendu(self):
        p = carte.panneau("/tmp/ordo-home", "c-7")
        self.assertIn("window.ordoRestoreScroll()", p)


class TestQuestionsSurLaCarte(CarteTestCase):
    """Ce qui attend un choix de l'humain, visible sur la carte.

    Une orchestratrice qui s'arrête pour poser une question le fait dans son terminal.
    L'humain, lui, regarde la carte : la question ne l'atteint jamais, et la campagne
    reste en attente sans que rien ne le dise à l'endroit qu'il a sous les yeux.
    """

    def _ask(self, cible, texte, pour_humain=True, options=None):
        with store.locked() as state:
            qid = store.next_id(state, "question")
            tache = cible if cible.startswith("t-") else None
            state["questions"][qid] = {
                "id": qid,
                "chantier": self.chantier,
                "tache": tache,
                "question": texte,
                "options": options or [],
                "pourHumain": pour_humain,
                "answer": None,
                "askedAt": store.now(),
                "answeredAt": None,
            }
        return qid

    def _repondre(self, qid, texte="oui"):
        with store.locked() as state:
            state["questions"][qid]["answer"] = texte
            state["questions"][qid]["answeredAt"] = store.now()

    def test_une_question_pour_l_humain_est_sur_la_carte(self):
        a = self._add("0.1 a")
        qid = self._ask(a["id"], "relancer sur opus ou découper ?")
        q = carte.model(self.chantier)["questions"]
        self.assertEqual([x["id"] for x in q], [qid])
        self.assertEqual(q[0]["task"], a["id"])
        self.assertIn("découper", q[0]["text"])

    def test_une_question_repondue_disparait_de_la_carte(self):
        # Sinon l'alerte resterait allumee apres coup, et une alerte qui ne s'eteint
        # jamais cesse d'etre lue.
        a = self._add("0.1 a")
        qid = self._ask(a["id"], "q ?")
        self._repondre(qid)
        self.assertEqual(carte.model(self.chantier)["questions"], [])

    def test_une_question_qui_n_est_pas_pour_l_humain_reste_hors_de_la_carte(self):
        # Celle-la appartient a l'orchestratrice : la montrer a l'humain le ferait
        # repondre a la place de la session, ce qui est exactement l'inverse du contrat.
        a = self._add("0.1 a")
        self._ask(a["id"], "q ?", pour_humain=False)
        self.assertEqual(carte.model(self.chantier)["questions"], [])

    def test_une_question_de_chantier_n_a_pas_de_tache(self):
        self._add("0.1 a")
        qid = self._ask("c-01", "parallèle ou série ?", options=["parallèle", "série"])
        q = carte.model(self.chantier)["questions"]
        self.assertEqual([x["id"] for x in q], [qid])
        self.assertEqual(q[0]["task"], "")
        self.assertEqual(q[0]["options"], ["parallèle", "série"])

    def test_une_question_porte_le_titre_de_sa_tache(self):
        # L'identifiant seul oblige a le traduire mentalement, et c'est precisement ce
        # qu'un humain revenu a froid ne peut pas faire.
        a = self._add("0.1 Recette locale")
        self._ask(a["id"], "q ?")
        self.assertEqual(carte.model(self.chantier)["questions"][0]["taskTitle"],
                         "0.1 Recette locale")

    def test_la_vue_porte_la_commande_qui_repond(self):
        # Repondre exige de viser le bon ORDO_HOME : un `ordo answer` tape depuis un
        # autre projet ne trouve pas la question et ne dit pas pourquoi.
        self._add("0.1 a")
        qid = self._ask("c-01", "q ?")
        cmd = carte.vue(carte.model(self.chantier))["questions"][0]["answerCmd"]
        self.assertIn(qid, cmd)
        self.assertIn("ordo answer", cmd)
        self.assertIn(self._tmp, cmd)

    def test_un_home_a_espaces_ne_casse_pas_la_commande(self):
        self._add("0.1 a")
        self._ask("c-01", "q ?")
        m = carte.model(self.chantier)
        m["campaign"]["home"] = "/mon dossier/o"
        self.assertIn("'/mon dossier/o'", carte.vue(m)["questions"][0]["answerCmd"])

    def test_la_page_porte_le_calque_de_la_question(self):
        self._add("0.1 a")
        self._ask("c-01", "q ?")
        self.assertIn('id="ask"', carte.html(carte.model(self.chantier)))


class TestMurEtQuestions(CarteTestCase):
    def test_le_mur_porte_le_compteur_de_choix_a_faire(self):
        # Le mur ne charge aucune carte : ce compteur est le seul endroit ou il peut dire
        # qu'une colonne, peut-etre sortie de l'ecran, attend un arbitrage.
        self.assertIn('id="asks"', carte.page())

    def test_une_colonne_distingue_le_serveur_muet_du_chantier_introuvable(self):
        # Les deux se ressemblent a l'ecran et ne se reparent pas pareil : un chantier
        # sorti du registre repond 403, ce qui n'est pas un serveur en panne.
        p = carte.panneau("/tmp/ordo-home", "c-7")
        self.assertIn("chantier introuvable", p)
        self.assertIn("serveur muet", p)


class TestMurDistingueTroisEtats(CarteTestCase):
    """Une exécutante vit, ou la campagne attend son orchestratrice, ou elle est finie ou
    bloquée : trois situations que /api/state doit rendre visibles sans se confondre (t-17).

    La logique vit dans du JS embarqué dans une chaîne Python (_MUR_JS) : aucune assertion
    de chaîne ne peut prouver son comportement, seule son exécution le peut. Ces tests
    extraient les fonctions réellement servies et les exécutent sous Node avec de vraies
    campagnes, exactement le payload que /api/state produit.
    """

    def _signal(self, campagne: dict) -> dict:
        if shutil.which("node") is None:
            self.skipTest("node absent, impossible d'exécuter le JS du mur")
        js = carte._MUR_JS
        debut = js.index("function ouvert")
        fin = js.index("// La disposition survit")
        script = (
            js[debut:fin]
            + "\nconst c = " + json.dumps(campagne) + ";"
            + "\nprocess.stdout.write(JSON.stringify({signal: signal(c), libelle: libelle(c)}));"
        )
        sortie = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=10, check=True,
        )
        return json.loads(sortie.stdout)

    def _campagne(self, **kwargs) -> dict:
        base = {
            "id": "c-01", "slug": "loko", "state": "open", "asking": 0,
            "running": 0, "ready": 0, "total": 1, "done": 0,
        }
        base.update(kwargs)
        return base

    def test_une_executante_vivante_ne_declenche_aucun_signal(self):
        r = self._signal(self._campagne(running=1, ready=0))
        self.assertIsNone(r["signal"])
        self.assertNotIn("ATTEND", r["libelle"])
        self.assertNotIn("TERMINEE", r["libelle"])

    def test_rien_ne_tourne_mais_quelque_chose_est_lancable_attend_l_orchestratrice(self):
        # Le cas qui a coûté vingt minutes à l'humain sur loko c-05 : c'est celui qui doit
        # se voir le plus.
        r = self._signal(self._campagne(running=0, ready=1))
        self.assertEqual(r["signal"], "wait")
        self.assertIn("ATTEND ORCHESTRATRICE", r["libelle"])

    def test_rien_ne_tourne_et_rien_n_est_lancable_campagne_finie_ou_bloquee(self):
        r = self._signal(self._campagne(running=0, ready=0, done=1))
        self.assertEqual(r["signal"], "stall")
        self.assertIn("TERMINEE OU BLOQUEE", r["libelle"])

    def test_l_etat_legacy_ouvert_est_reconnu_comme_ouvert(self):
        # lavuln c-03 et loko c-02, les deux campagnes réelles citées par le mandat,
        # portent encore cet état francophone qu'une version antérieure écrivait.
        r = self._signal(self._campagne(state="ouvert", running=0, ready=0, done=1))
        self.assertEqual(r["signal"], "stall")

    def test_un_chantier_ferme_ne_declenche_jamais_ce_signal(self):
        r = self._signal(self._campagne(state="closed", running=0, ready=0))
        self.assertIsNone(r["signal"])

    def test_une_question_humaine_pendante_prime_sur_le_nouveau_signal(self):
        # "asking" est déjà porté par sa propre alerte (.ask) : cumuler un second signal
        # ferait dire deux choses différentes de la même immobilité.
        r = self._signal(self._campagne(running=0, ready=1, asking=1))
        self.assertIsNone(r["signal"])

    def test_une_campagne_sans_aucune_tache_ne_signale_rien(self):
        # Juste créée, pas encore peuplée : rien à signaler, ce n'est pas "à l'arrêt".
        r = self._signal(self._campagne(running=0, ready=0, total=0, done=0))
        self.assertIsNone(r["signal"])

    def test_le_mur_porte_les_classes_css_des_deux_nouveaux_signaux(self):
        page = carte.page()
        self.assertIn(".col.wait", page)
        self.assertIn(".col.stall", page)


class TestOrdreDesChantiersDansLOnglet(CarteTestCase):
    """L'onglet fusionné (t-49) groupe les chantiers avant de les proposer : ceux qui ont
    quelque chose EN COURS -- une exécutante vivante ou une tâche lançable -- d'abord,
    toujours dans cet ordre, même quand ce premier groupe est vide (c3). grouper() est une
    fonction pure de _MUR_JS, dans la même plage que ouvert()/signal()/libelle() déjà
    exécutée sous Node par TestMurDistingueTroisEtats -- même technique ici."""

    def _grouper(self, campagnes: list[dict]) -> dict:
        if shutil.which("node") is None:
            self.skipTest("node absent, impossible d'exécuter le JS du mur")
        js = carte._MUR_JS
        debut = js.index("function ouvert")
        fin = js.index("// La disposition survit")
        script = (
            js[debut:fin]
            + "\nconst cs = " + json.dumps(campagnes) + ";"
            + "\nconst g = grouper(cs);"
            + "\nprocess.stdout.write(JSON.stringify({"
            + "enCours: g.enCours.map(c => c.id), autres: g.autres.map(c => c.id)}));"
        )
        sortie = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=10, check=True,
        )
        return json.loads(sortie.stdout)

    def _campagne(self, **kwargs) -> dict:
        base = {
            "id": "c-01", "slug": "loko", "state": "open", "asking": 0,
            "running": 0, "ready": 0, "total": 1, "done": 0,
        }
        base.update(kwargs)
        return base

    def test_une_executante_vivante_va_dans_en_cours(self):
        g = self._grouper([self._campagne(id="c-01", running=1)])
        self.assertEqual(g["enCours"], ["c-01"])
        self.assertEqual(g["autres"], [])

    def test_une_tache_lancable_va_dans_en_cours(self):
        g = self._grouper([self._campagne(id="c-01", ready=1)])
        self.assertEqual(g["enCours"], ["c-01"])
        self.assertEqual(g["autres"], [])

    def test_rien_ne_tourne_ni_ne_se_lance_va_dans_autres(self):
        g = self._grouper([self._campagne(id="c-01", running=0, ready=0, done=1)])
        self.assertEqual(g["enCours"], [])
        self.assertEqual(g["autres"], ["c-01"])

    def test_un_chantier_ferme_va_dans_autres_meme_si_ready_est_pose(self):
        # N'arrive jamais en pratique (un chantier fermé n'a plus de tâche lançable), mais
        # la garde ouvert() dans enCours() doit tenir si ce chiffre était incohérent.
        g = self._grouper([self._campagne(id="c-01", state="closed", ready=1)])
        self.assertEqual(g["enCours"], [])
        self.assertEqual(g["autres"], ["c-01"])

    def test_le_groupe_en_cours_reste_premier_meme_vide(self):
        g = self._grouper([self._campagne(id="c-01", running=0, ready=0, done=1)])
        self.assertEqual(list(g.keys()), ["enCours", "autres"])
        self.assertEqual(g["enCours"], [])

    def test_lordre_relatif_est_preserve_a_linterieur_de_chaque_groupe(self):
        g = self._grouper([
            self._campagne(id="c-01", running=1),
            self._campagne(id="c-02", done=1),
            self._campagne(id="c-03", ready=1),
            self._campagne(id="c-04", state="closed"),
        ])
        self.assertEqual(g["enCours"], ["c-01", "c-03"])
        self.assertEqual(g["autres"], ["c-02", "c-04"])


class TestOngletFusionne(CarteTestCase):
    """Un seul bandeau par colonne du mur (t-49), à la place du select natif et de la
    ligne identifiant/projet/état/session que répétait le panneau juste en dessous."""

    def test_le_select_de_chantier_a_disparu_du_mur(self):
        self.assertNotIn("<select", carte.page())

    def test_longlet_est_un_bouton_qui_ouvre_une_liste_accessible(self):
        page = carte.page()
        self.assertIn('aria-haspopup","listbox"', page)
        self.assertIn('role","listbox"', page)
        self.assertIn('role","option"', page)

    def test_longlet_souvre_au_clavier_et_se_deplace_dans_la_liste(self):
        page = carte.page()
        self.assertIn("function onTabKeydown", page)
        for touche in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
            self.assertIn(touche, page)

    def test_le_focus_du_bouton_donglet_reste_visible(self):
        self.assertIn(".chead .tab:focus-visible{", carte.page())

    def test_le_popover_porte_deux_groupes_separes_par_un_separateur_visible(self):
        page = carte.page()
        self.assertIn('"opt-group"', page)
        self.assertIn('role","separator"', page)
        self.assertIn(".tablist .opt-sep{", page)

    def test_la_mention_eta_precede_lheure_sur_londlget(self):
        self.assertIn('"ETA "', carte._MUR_JS)

    def test_longlet_porte_le_pourcentage_le_restant_et_leta(self):
        # Les trois valeurs viennent du dernier message reçu (td), jamais d'un nouveau
        # calcul sur les données brutes du mur (voir peindreTab()).
        debut = carte._MUR_JS.index("function peindreTab")
        fin = carte._MUR_JS.index("function optionsListe")
        corps = carte._MUR_JS[debut:fin]
        self.assertIn("col.tpct.textContent=td?td.pct:", corps)
        self.assertIn("col.trest.textContent=connu?td.restant:", corps)
        self.assertIn('"ETA "+td.heure', corps)

    def test_londlget_ne_recalcule_jamais_les_trois_valeurs_du_panneau(self):
        # Elles voyagent par message (annoncerOnglet, dans _JS) : le mur ne doit jamais
        # relire finChantier() ni dureeMin(), sous peine de les recalculer une seconde
        # fois avec une autre horloge que celle du panneau.
        self.assertNotIn("finChantier(", carte._MUR_JS)
        self.assertNotIn("dureeMin(", carte._MUR_JS)
        self.assertIn("ordoOnglet", carte._MUR_JS)

    def test_le_panneau_masque_son_propre_bandeau_une_fois_dans_le_mur(self):
        # t-57 : #top disparaît en entier dans le mur (h1, #segs et #bar), pas seulement
        # son h1 -- la progression et les alertes vivent désormais dans l'onglet.
        self.assertIn('classList.add("embarque")', carte._JS)
        self.assertIn("html.embarque #top{display:none}", carte._CSS)

    def test_le_panneau_narrete_pas_dannoncer_quand_le_restant_est_inconnu(self):
        # paintHeure() sortait tôt sans rien dire côté mur quand restantChantierMin est
        # None (brief t-38) : l'onglet resterait alors bloqué sur la dernière valeur
        # connue au lieu de se vider.
        js = carte._JS
        debut = js.index("function paintHeure")
        fin = js.index("function paintTop")
        corps = js[debut:fin]
        self.assertIn("annoncerOnglet(connu", corps)
        self.assertNotIn("if(!connu)return", corps)


class TestSecondeLigneDeLOnglet(CarteTestCase):
    """t-57 : la progression par phase et les alertes qui vivaient au-dessus du graphe,
    cernées de marges et sur deux bandeaux distincts, deviennent la seconde ligne de
    l'onglet -- portée par la colonne du mur, jamais par le panneau qu'elle embarque."""

    def _peindre(self, td: dict | None) -> dict:
        if shutil.which("node") is None:
            self.skipTest("node absent, impossible d'exécuter le JS du mur")
        js = carte._MUR_JS
        debut = js.index("function peindreProgres")
        fin = js.index("// Construit les options du popover")
        script = (
            "function makeEl(){var e={hidden:false,textContent:'',title:'',className:'',"
            "style:{},children:[],firstChild:null};"
            "e.appendChild=function(c){this.children.push(c);"
            "this.firstChild=this.children[0]||null;return c};"
            "e.removeChild=function(c){var i=this.children.indexOf(c);"
            "if(i>=0)this.children.splice(i,1);this.firstChild=this.children[0]||null};"
            "return e}\n"
            "var document={createElement:function(){return makeEl()}};\n"
            + js[debut:fin]
            + "\nvar col={trow2:makeEl(),tsegs:makeEl(),twarn:makeEl(),tdemande:makeEl()};"
            + "\npeindreProgres(col," + json.dumps(td) + ");"
            + "\nprocess.stdout.write(JSON.stringify({"
            + "trow2Hidden:col.trow2.hidden,segCount:col.tsegs.children.length,"
            + "warnHidden:col.twarn.hidden,warnText:col.twarn.textContent,"
            + "askHidden:col.tdemande.hidden,askText:col.tdemande.textContent}));"
        )
        sortie = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=10, check=True,
        )
        return json.loads(sortie.stdout)

    # -- c9 : colonne sans donnée -----------------------------------------------------

    def test_colonne_sans_message_encore_recu_naffiche_rien(self):
        r = self._peindre(None)
        self.assertTrue(r["trow2Hidden"])
        self.assertEqual(r["segCount"], 0)
        self.assertTrue(r["warnHidden"])
        self.assertTrue(r["askHidden"])

    def test_chantier_sans_phase_ni_alerte_naffiche_rien(self):
        r = self._peindre({"phases": [], "warn": 0, "ask": 0})
        self.assertTrue(r["trow2Hidden"])
        self.assertEqual(r["segCount"], 0)

    # -- c3 : progression par phase portée par l'onglet --------------------------------

    def test_la_progression_par_phase_arrive_sur_londlget(self):
        td = {"phases": [
            {"key": "1", "nom": "Fondation", "dn": 2, "total": 5},
            {"key": "2", "nom": "Exécution", "dn": 0, "total": 3},
        ], "warn": 0, "ask": 0}
        r = self._peindre(td)
        self.assertFalse(r["trow2Hidden"])
        self.assertEqual(r["segCount"], 2)

    # -- c4 : alertes sur la même ligne que la progression ------------------------------

    def test_les_alertes_partagent_le_conteneur_de_la_progression(self):
        # trow2 est l'unique conteneur des deux : segments et pastilles y sont des
        # enfants directs, jamais deux lignes distinctes ni deux hidden séparés.
        td = {"phases": [{"key": "1", "dn": 1, "total": 1}], "warn": 3, "ask": 2}
        r = self._peindre(td)
        self.assertFalse(r["trow2Hidden"])
        self.assertEqual(r["segCount"], 1)
        self.assertFalse(r["warnHidden"])
        self.assertEqual(r["warnText"], 3)
        self.assertFalse(r["askHidden"])
        self.assertEqual(r["askText"], "2 choix")

    def test_une_pastille_sans_alerte_reste_cachee_meme_avec_de_la_progression(self):
        td = {"phases": [{"key": "1", "dn": 1, "total": 1}], "warn": 0, "ask": 0}
        r = self._peindre(td)
        self.assertFalse(r["trow2Hidden"])
        self.assertTrue(r["warnHidden"])
        self.assertTrue(r["askHidden"])

    # -- c6 : la seconde ligne vit dans l'onglet, jamais dans le panneau ----------------

    def test_la_seconde_ligne_est_construite_dans_l_onglet_du_mur(self):
        js = carte._MUR_JS
        debut = js.index("function creer(col)")
        fin = js.index("function envoyerAuPanneau")
        corps = js[debut:fin]
        self.assertIn('row2.className="trow2"', corps)
        # DANS l'onglet, plus sous lui : le bandeau ne doit montrer qu'un seul objet
        # portant tout l'état du chantier, pas un sélecteur puis une barre séparée.
        # C'est aussi ce qui oblige .tab à être un div role=button -- les chips d'alerte
        # que row2 porte sont des boutons, et un bouton dans un bouton ne reçoit
        # jamais son clic.
        self.assertIn("tab.appendChild(row2);", corps)
        self.assertNotIn("h.appendChild(row2)", corps)
        self.assertIn('tab.setAttribute("role","button")', corps)
        # Jamais posée sur l'iframe ni renvoyée au panneau : construite et tenue par le
        # mur, qui ne fait que RECEVOIR les valeurs du panneau (annoncerOnglet), jamais
        # l'inverse.
        self.assertNotIn("f.appendChild", corps)

    def test_le_panneau_ne_construit_plus_sa_propre_ligne_dans_le_mur(self):
        # Toujours vrai après t-57 : #top (identité, progression, alertes) disparaît en
        # entier une fois embarqué -- voir TestOngletFusionne.
        self.assertIn("html.embarque #top{display:none}", carte._CSS)

    # -- c5 : les alertes restent cliquables à leur nouvelle place ---------------------

    def test_les_pastilles_de_londlget_relaient_le_clic_au_panneau(self):
        js = carte._MUR_JS
        debut = js.index("function creer(col)")
        fin = js.index("function envoyerAuPanneau")
        corps = js[debut:fin]
        self.assertIn('envoyerAuPanneau(col,"warn")', corps)
        self.assertIn('envoyerAuPanneau(col,"ask")', corps)
        debut2 = js.index("function envoyerAuPanneau")
        fin2 = js.index("function dessiner")
        self.assertIn('postMessage({ordoOuvrir:quoi}', js[debut2:fin2])

    def test_le_panneau_ouvre_le_meme_calque_quand_le_mur_le_demande(self):
        # ordoOuvrir déclenche basculerWarn()/basculerAsk(), les MÊMES fonctions que le
        # clic local sur #warnchip/#askchip (t-48) : jamais un second mécanisme écrit
        # pour la même idée.
        js = carte._JS
        self.assertIn('if(msg.ordoOuvrir==="warn")basculerWarn();', js)
        self.assertIn('else if(msg.ordoOuvrir==="ask")basculerAsk();', js)
        self.assertIn('if(wc)wc.addEventListener("click",basculerWarn);', js)
        self.assertIn('if(ac)ac.addEventListener("click",basculerAsk);', js)

    # -- transmission des données (préalable à c3/c4) -----------------------------------

    def test_le_panneau_transmet_les_phases_et_les_compteurs_dalerte(self):
        js = carte._JS
        debut = js.index("function annoncerOnglet")
        fin = js.index("function paintTop")
        corps = js[debut:fin]
        self.assertIn("phases: resumePhases(),", corps)
        self.assertIn("warn: (D&&D.warnings&&D.warnings.length)||0,", corps)
        self.assertIn("ask: (D&&D.questions&&D.questions.length)||0,", corps)

    def test_segs_et_londlget_partagent_le_meme_resume_de_phases(self):
        # #segs (page seule ou panneau visité hors mur) et l'onglet du mur lisent le même
        # calcul : jamais deux décomptes qui pourraient diverger.
        js = carte._JS
        debut = js.index("function paintTop")
        fin = js.index('document.getElementById("cid")')
        corps = js[debut:fin]
        self.assertIn("resumePhases().forEach(function(p){", corps)
        self.assertNotIn("D.phases.forEach", corps)


# ---------------------------------------------------------------------------
# Options du popover au style de l'onglet (t-56, retouche indépendante)
# ---------------------------------------------------------------------------


class TestOptionsDuPopoverAuStyleDeLOnglet(CarteTestCase):
    """t-49 affichait un simple libelle() en texte brut par option -- un autre genre que
    l'onglet soigné qu'elle produit une fois choisie (nom, pourcentage, restant, ETA,
    seconde ligne de progression depuis t-57). Chaque option reprend désormais
    exactement cette structure, avec les mêmes classes CSS (donc les mêmes teintes),
    sauf ce qu'aucune iframe fermée ne peut connaître -- restant et ETA d'un chantier
    qu'aucune colonne de CE mur n'a déjà ouvert."""

    def _fonctions(self) -> str:
        if shutil.which("node") is None:
            self.skipTest("node absent, impossible d'exécuter le JS du mur")
        js = carte._MUR_JS
        debut = js.index("var cols=[], seq=0, campagnes=[], monte=false, timer=null;")
        fin = js.index("function optionsListe(col){")
        return js[debut:fin]

    def _peindre(self, c: dict, cols_ouvertes: list[dict]) -> dict:
        script = (
            "function makeEl(){var e={hidden:false,textContent:'',title:'',className:'',"
            "style:{},children:[],firstChild:null};"
            "e.appendChild=function(ch){this.children.push(ch);"
            "this.firstChild=this.children[0]||null;return ch};"
            "e.removeChild=function(ch){var i=this.children.indexOf(ch);"
            "if(i>=0)this.children.splice(i,1);this.firstChild=this.children[0]||null};"
            "return e}\n"
            "var document={createElement:function(){return makeEl()},"
            "createTextNode:function(t){var e=makeEl();e.textContent=t;return e}};\n"
            + self._fonctions()
            + "\ncols=" + json.dumps(cols_ouvertes) + ";"
            + "\nvar li=makeEl();"
            + "\npeindreOption(li," + json.dumps(c) + ");"
            + "\nvar row1=li.children[0], row2=li.children[1];"
            + "\nvar dot=row1.children[0], nom=row1.children[1], fin=row1.children[2];"
            + "\nvar trest=fin.children[0], teta=fin.children[1], tpct=fin.children[2];"
            + "\nprocess.stdout.write(JSON.stringify({"
            + "row1Classe:row1.className,row2Classe:row2.className,"
            + "dotClasse:dot.className,nomTexte:nom.textContent,"
            + "trestCache:trest.hidden,tetaCache:teta.hidden,tpctTexte:tpct.textContent,"
            + "trestTexte:trest.textContent,"
            + "tsegsEnfants:row2.children[0].children.length,"
            + "twarnCache:row2.children[1].hidden,tdemandeCache:row2.children[2].hidden}));"
        )
        sortie = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=10, check=True,
        )
        return json.loads(sortie.stdout)

    def _campagne(self, **kw) -> dict:
        base = {
            "home": "/tmp/ordo-home", "id": "c-1", "slug": "demo", "state": "open",
            "asking": 0, "running": 0, "ready": 0, "done": 3, "total": 10,
        }
        base.update(kw)
        return base

    # -- structure : mêmes classes que l'onglet, donc mêmes teintes et espacements -----

    def test_loption_reprend_les_deux_rangees_de_londlget(self):
        c = self._campagne()
        r = self._peindre(c, [])
        self.assertEqual(r["row1Classe"], "orow1")
        self.assertEqual(r["row2Classe"], "trow2")

    def test_loption_porte_les_memes_classes_terminales_que_londlget(self):
        js = carte._MUR_JS
        debut = js.index("function peindreOption(li,c){")
        fin = js.index("function optionsListe(col){")
        corps = js[debut:fin]
        for classe in (
            'dot.className="tdot ', 'nom.className="tnom"', 'fin.className="tfin"',
            'trest.className="trest"', 'teta.className="teta"', 'tpct.className="tpct"',
            'tsegs.className="tsegs"', 'twarn.className="tchip"', 'tdemande.className="tchip"',
        ):
            self.assertIn(classe, corps)

    def test_le_nom_de_loption_est_le_slug_ou_lid(self):
        c = self._campagne(slug="mon-chantier")
        r = self._peindre(c, [])
        self.assertEqual(r["nomTexte"], "mon-chantier")

    # -- rien d'estimé : ni restant, ni ETA, exactement comme l'onglet (DEUX LIMITES) --

    def test_un_chantier_ouvert_nulle_part_sur_ce_mur_na_rien_destime(self):
        c = self._campagne()
        r = self._peindre(c, [])
        self.assertEqual(r["tpctTexte"], "")
        self.assertTrue(r["trestCache"])
        self.assertTrue(r["tetaCache"])
        self.assertEqual(r["tsegsEnfants"], 0)
        self.assertTrue(r["twarnCache"])
        self.assertTrue(r["tdemandeCache"])

    def test_une_option_najoute_jamais_de_restant_que_londlget_ne_connait_pas(self):
        # c9 de t-57, DEUX LIMITES du brief : td connu=false -> pct porté, restant/ETA
        # masqués, exactement le contrat de peindreTab().
        c = self._campagne()
        col_ouverte = {
            "home": "/tmp/ordo-home", "campaign": "c-1",
            "tabData": {"pct": "30%", "connu": False, "phases": [], "warn": 0, "ask": 0},
        }
        r = self._peindre(c, [col_ouverte])
        self.assertEqual(r["tpctTexte"], "30%")
        self.assertTrue(r["trestCache"])
        self.assertTrue(r["tetaCache"])

    # -- un chantier déjà ouvert ailleurs sur ce mur porte son tabData réel ------------

    def test_un_chantier_deja_ouvert_ailleurs_porte_son_tabdata_reel(self):
        c = self._campagne()
        col_ouverte = {
            "home": "/tmp/ordo-home", "campaign": "c-1",
            "tabData": {
                "pct": "42%", "connu": True, "restant": "1h20", "heure": "17h05", "jours": 0,
                "phases": [{"key": "1", "nom": "Fondation", "dn": 2, "total": 5}],
                "warn": 1, "ask": 0,
            },
        }
        r = self._peindre(c, [col_ouverte])
        self.assertEqual(r["tpctTexte"], "42%")
        self.assertFalse(r["trestCache"])
        self.assertEqual(r["trestTexte"], "1h20")
        self.assertFalse(r["tetaCache"])
        self.assertEqual(r["tsegsEnfants"], 1)
        self.assertFalse(r["twarnCache"])
        self.assertTrue(r["tdemandeCache"])

    # -- point du brief : jamais interactive au-delà du clic global sur l'option -------

    def test_les_pastilles_dune_option_ne_sont_jamais_des_boutons(self):
        js = carte._MUR_JS
        debut = js.index("function peindreOption(li,c){")
        fin = js.index("function optionsListe(col){")
        corps = js[debut:fin]
        self.assertNotIn('document.createElement("button")', corps)
        self.assertNotIn("addEventListener", corps)

    def test_peindreoption_reutilise_peindreprogres_sans_le_dupliquer(self):
        js = carte._MUR_JS
        debut = js.index("function peindreOption(li,c){")
        fin = js.index("function optionsListe(col){")
        corps = js[debut:fin]
        self.assertIn(
            "peindreProgres({trow2:trow2,tsegs:tsegs,twarn:twarn,tdemande:tdemande},td);",
            corps,
        )
        self.assertNotIn("p.total||1", corps)  # la boucle de segments n'est pas recopiée

    # -- limites du brief : le groupement en cours/autres ne bouge pas -----------------

    def test_le_groupement_en_cours_autres_ne_bouge_pas(self):
        js = carte._MUR_JS
        self.assertIn('bloc("en cours",g.enCours);', js)
        self.assertIn('bloc("autres",g.autres);', js)
        self.assertIn('sep.className="opt-sep";', js)


class TestSessionSurLaCase(CarteTestCase):
    """Le contexte du dernier tour d'une session en cours, lisible sans ouvrir la case.

    Le nombre de tours a longtemps tenu cette place : mesuré sur cinquante-neuf sessions
    réelles, il ratait seize d'entre elles, qui atteignaient un gros contexte en peu de
    tours (voir le commentaire de usage.SEUIL_TOURS). Le contexte du dernier tour est le
    chiffre qui prédit réellement ce qu'une session va coûter, et c'est lui que la
    compaction lit pour décider (usage.SEUIL_CONTEXTE) -- la case doit donc dire le même
    chiffre, pas un autre.
    """

    def test_une_tache_lancee_porte_le_contexte_du_dernier_tour(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", paneId="%126",
                        claudeSessionId="ce85c8b5-0000")
        t = carte.vue(carte.model(
            self.chantier,
            usage_de=lambda x: {"input": 0, "output": 900000, "cacheCreation": 0,
                                "cacheRead": 0, "turns": 180, "dernierContexte": 48000},
        ))["tasks"][0]
        # 48k et non 900k : la sortie cumulée de la session grossit avec sa durée sans
        # jamais redescendre, elle ne dit rien de ce qu'un tour de plus va coûter à relire.
        self.assertEqual(t["tokens"], "48k")
        # Le pane reste joignable, dans les faits : on ne va voir un pane qu'après avoir
        # ouvert la tâche, jamais en balayant le mur du regard.
        self.assertIn("%126", t["facts"]["pane"])

    def test_un_contexte_qui_depasse_le_seuil_de_compaction_est_signale(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", paneId="%1")
        lourd = carte.vue(carte.model(
            self.chantier,
            usage_de=lambda x: {"input": 0, "output": 0, "cacheCreation": 0, "cacheRead": 0,
                                "turns": 5, "dernierContexte": usage.SEUIL_CONTEXTE + 1},
        ))["tasks"][0]
        self.assertTrue(lourd["contexteLourd"])

    def test_un_contexte_sous_le_seuil_de_compaction_n_est_pas_signale(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", paneId="%1")
        leger = carte.vue(carte.model(
            self.chantier,
            usage_de=lambda x: {"input": 0, "output": 0, "cacheCreation": 0, "cacheRead": 0,
                                "turns": 5, "dernierContexte": usage.SEUIL_CONTEXTE - 1},
        ))["tasks"][0]
        self.assertFalse(leger["contexteLourd"])

    def test_le_seuil_qui_allume_le_contexte_vient_de_usage_jamais_d_une_copie(self):
        # Patch usage.SEUIL_CONTEXTE lui-même, pas une copie éventuelle dans carte.py : si
        # carte.py définissait son propre seuil, ce patch n'aurait aucun effet et le test
        # échouerait. C'est la preuve que le seuil est bien lu là où vit la compaction.
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", paneId="%1")
        with mock.patch.object(usage, "SEUIL_CONTEXTE", 500):
            t = carte.vue(carte.model(
                self.chantier,
                usage_de=lambda x: {"input": 0, "output": 0, "cacheCreation": 0,
                                    "cacheRead": 0, "turns": 5, "dernierContexte": 600},
            ))["tasks"][0]
        self.assertTrue(t["contexteLourd"])

    def test_une_tache_jamais_lancee_n_affiche_aucun_contexte(self):
        # Chaîne vide et pas zéro : zéro se lirait comme "cette tâche n'a rien lu", la
        # vérité est qu'il n'y a pas de session du tout.
        self._add("0.1 a")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["tokens"], "")
        self.assertFalse(t["contexteLourd"])

    def test_les_compactions_deja_faites_sont_dans_les_faits(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", paneId="%1", compactions=2,
                        compactedAtTurn=150)
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["facts"]["compactions"], "2")

    def test_la_page_porte_le_contexte_et_son_seuil_d_alerte(self):
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", paneId="%126")
        page = carte.html(carte.model(
            self.chantier,
            usage_de=lambda x: {"input": 0, "output": 0, "cacheCreation": 0,
                                "cacheRead": 0, "turns": 5, "dernierContexte": 999999999},
        ))
        self.assertIn("contexteLourd", page)

    def test_le_nombre_de_tours_a_quitte_l_en_tete_de_la_case(self):
        # Le nombre de tours n'est plus le critère de compaction : il n'a plus à occuper
        # l'en-tête. Il reste lisible en détail, dans les faits de la tâche (voir _facts).
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", paneId="%126")
        page = carte.html(carte.model(
            self.chantier,
            usage_de=lambda x: {"input": 0, "output": 0, "cacheCreation": 0,
                                "cacheRead": 0, "turns": 200, "dernierContexte": 0},
        ))
        self.assertNotIn("sessionLongue", page)
        self.assertNotIn('el("span","turns"', page)

    def test_la_case_ne_repete_ni_l_etat_ni_le_pane(self):
        # La barre de couleur porte déjà l'état, et le répéter en toutes lettres coûtait
        # la moitié de la largeur d'une colonne : le reste débordait de la case.
        self._add("0.1 a")
        page = carte.html(carte.model(self.chantier))
        self.assertNotIn('el("span","rst"', page)
        self.assertNotIn('el("span","pane"', page)
        # L'information n'est pas perdue : elle passe en infobulle sur la barre.
        self.assertIn("sb.title=LAB[k]", page)


# ---------------------------------------------------------------------------
# Progression de checklist et critère en cours, visibles sans ouvrir la case
# ---------------------------------------------------------------------------


class TestProgressionSurLaCase(CarteTestCase):
    """La progression vivait dans meta, que la vue graphe (le défaut) n'affiche que sur la
    case sélectionnée : une checklist entièrement cochée restait invisible tant qu'on
    n'ouvrait pas la tâche. Ces tests portent le compteur et le libellé du critère en cours
    hors de meta, dans leur propre ligne, lue dans les deux vues."""

    def test_le_libelle_du_critere_en_cours_sort_de_currentitem(self):
        a = self._add("0.1 a", checklist=["c un", "c deux"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], currentItem=cl[1]["id"])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["doing"], "c deux")

    def test_sans_critere_en_cours_le_libelle_est_vide(self):
        # Vide, jamais un tiret ni un zéro : une case qui n'a rien déclaré ne doit rien
        # afficher qui se lise comme une information.
        self._add("0.1 a", checklist=["c un"])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["doing"], "")

    def test_un_currentitem_qui_ne_correspond_plus_a_aucun_item_ne_fabrique_rien(self):
        a = self._add("0.1 a", checklist=["c un"])
        self._set_state(a["id"], currentItem="item-disparu")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["doing"], "")

    def test_le_compteur_et_le_libelle_sortent_de_meta(self):
        # meta ne s'affiche pas en vue graphe sur une case fermée (voir la classe
        # ci-dessus) : le compteur ne peut plus vivre là s'il doit être vu sans ouvrir.
        a = self._add("0.1 a", checklist=["c un", "c deux"])
        cl = carte.model(self.chantier)["nodes"][a["id"]]["checklist"]
        self._set_state(a["id"], checklist=[
            {"id": cl[0]["id"], "label": "c un", "done": True},
            {"id": cl[1]["id"], "label": "c deux", "done": False},
        ], currentItem=cl[1]["id"])
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual((t["checkDone"], t["checkTotal"]), (1, 2))
        self.assertNotIn("1/2", t["meta"])

    def test_la_ligne_de_progression_n_est_pas_reservee_a_la_vue_selectionnee(self):
        # C'est la case n°1 du brief : contrairement à rmeta, rprog ne doit dépendre ni de
        # S.view ni de sel, sans quoi la régression revient telle quelle.
        self._add("0.1 a", checklist=["c un"])
        page = carte.html(carte.model(self.chantier))
        self.assertIn('el("div","rprog")', page)
        avant_meta = page.index('el("div","rprog")')
        garde_meta = page.index('if(sel){')
        self.assertLess(avant_meta, garde_meta)

    def test_le_libelle_du_critere_est_a_cote_du_compteur_dans_la_meme_ligne(self):
        self._add("0.1 a", checklist=["c un"])
        page = carte.html(carte.model(self.chantier))
        bloc = page[page.index('el("div","rprog")'):page.index('if(sel){')]
        self.assertIn('el("span","cnt",t.checkDone+"/"+t.checkTotal)', bloc)
        self.assertIn('el("span","doing",t.doing)', bloc)

    def test_le_libelle_du_critere_passe_par_textcontent_jamais_par_du_balisage(self):
        # el() pose toujours txt en textContent (voir sa définition) : c'est cet appel-là,
        # jamais innerHTML, qui doit porter un libellé écrit par un modèle.
        page = carte.html(carte.model(self.chantier))
        self.assertIn('function el(tag,cls,txt){var e=document.createElement(tag)', page)
        self.assertIn('e.textContent=txt', page)
        self.assertNotIn("doing.innerHTML", page)

    def test_le_compteur_ne_prend_le_vert_du_fini_qu_a_l_etat_done(self):
        page = carte.html(carte.model(self.chantier))
        self.assertIn(".row.done .rprog .cnt{color:var(--done)}", page)
        # Hors de l'état "done", le compteur reste dans la teinte neutre des faits courts,
        # jamais celle du succès : une checklist à 100% sur une tâche encore en cours ne
        # doit pas se lire comme une tâche finie.
        self.assertIn(".rprog .cnt{color:var(--dim2)", page)

    def test_un_libelle_long_deborde_a_l_ellipse_jamais_hors_de_la_case(self):
        page = carte.html(carte.model(self.chantier))
        self.assertIn(
            ".rprog .doing{color:var(--dim);min-width:0;overflow:hidden;"
            "text-overflow:ellipsis;", page,
        )


# ---------------------------------------------------------------------------
# Écriture du rapport en cours : troisième état visuel, ni "en cours" ni "fini"
# ---------------------------------------------------------------------------


class TestEcritureDuRapportEnCours(CarteTestCase):
    """Une tâche running dont tous les critères sont cochés n'a plus de libellé de
    critère à montrer : la case le dit en clair plutôt que de laisser un silence qui a
    déjà fait croire une fois qu'une session était bloquée (t-16). Trois cas, jamais
    confondus : running+partiel -> libellé, running+complet -> mention, done -> ni
    l'un ni l'autre."""

    def test_toutes_les_cases_cochees_et_running_affiche_la_mention_rapport(self):
        # c1 : le libellé du critère en cours n'existe plus une fois tout coché -- la case
        # dit alors en clair qu'elle termine, dans son propre texte, jamais deviné d'un
        # champ vide.
        self._add("0.1 a", checklist=["c un"])
        page = carte.html(carte.model(self.chantier))
        bloc = page[page.index('el("div","rprog")'):page.index('if(sel){')]
        self.assertIn('if(k==="finishing"){', bloc)
        self.assertIn(
            'el("span","doing rapport","Écriture du rapport en cours")', bloc,
        )

    def test_la_mention_rapport_porte_sa_propre_couleur_violette(self):
        # Troisième état, pas une nuance du deuxième (jaune "en cours") ni du premier
        # (vert "fini") : sa propre variable de thème, la même que celle qui teint déjà la
        # barre de couleur de la case entière via kind()/COL.
        page = carte.html(carte.model(self.chantier))
        self.assertIn("--finishing:#8b5cf6", page)
        self.assertIn(
            ".rprog .doing.rapport{color:var(--finishing);font-weight:600}", page,
        )
        self.assertIn('finishing:"#8b5cf6"', page)

    def test_le_libelle_du_critere_reste_l_alternative_quand_rien_n_est_fini(self):
        # c2 : la mention rapport et le libellé du critère en cours sont le if/else d'une
        # seule et même condition -- jamais deux blocs indépendants qui pourraient un jour
        # diverger ou s'afficher ensemble.
        page = carte.html(carte.model(self.chantier))
        bloc = page[page.index('el("div","rprog")'):page.index('if(sel){')]
        self.assertIn(
            'if(k==="finishing"){\n'
            '        prog.appendChild(el("span","doing rapport",'
            '"Écriture du rapport en cours"));\n'
            '      }else if(t.doing){\n'
            '        var doing=el("span","doing",t.doing);',
            bloc,
        )

    def test_ni_libelle_ni_mention_rapport_hors_d_une_tache_en_cours(self):
        # c3 : done, blocked, cancelled, ready, queued -- aucun des deux ne doit sortir.
        # Prouvé structurellement : les deux branches vivent SOUS if(t.status==="running"),
        # donc inatteignables ailleurs, sans dépendre d'un currentItem qui pourrait rester
        # périmé après la fin de la tâche (voir chantier.check, qui ne le vide que si
        # l'item déclaré est celui qu'on coche).
        page = carte.html(carte.model(self.chantier))
        bloc = page[page.index('el("div","rprog")'):page.index('if(sel){')]
        garde = bloc.index('if(t.status==="running"){')
        rapport = bloc.index("doing rapport")
        libelle = bloc.index('el("span","doing",t.doing)')
        fin_garde = bloc.index('    }\n    row.appendChild(prog);')
        self.assertLess(garde, rapport)
        self.assertLess(garde, libelle)
        self.assertLess(rapport, fin_garde)
        self.assertLess(libelle, fin_garde)

    def test_une_tache_sans_critere_ne_construit_aucune_ligne_de_progression(self):
        # c4 : aucun critère, donc aucune ligne -- ni compteur, ni libellé, ni mention. Le
        # garde-fou if(t.checkTotal) enveloppe la construction entière de rprog, y compris
        # la mention de fin de rapport.
        page = carte.html(carte.model(self.chantier))
        bloc = page[page.index("function rowNode(t){"):page.index('if(sel){')]
        garde = bloc.index("if(t.checkTotal){")
        prog = bloc.index('el("div","rprog")')
        rapport = bloc.index("doing rapport")
        self.assertLess(garde, prog)
        self.assertLess(garde, rapport)


# ---------------------------------------------------------------------------
# Tâche jamais commencée : une ligne, rien de plus (brief t-58, points 4 et 5)
# ---------------------------------------------------------------------------


class TestTacheJamaisCommencee(CarteTestCase):
    """Une tâche jamais commencée n'a rien à dire qu'un titre : sa checklist vaut 0/N, ses
    jetons et son temps écoulé n'existent pas -- un "0/9" n'apprend rien que le titre ne
    dise déjà. Attention, "jamais commencée" n'est pas "sans critère coché" : une tâche
    relancée après un échec a pu tourner sans rien cocher, elle garde son affichage
    complet, son temps écoulé est une information (voir jamaisCommencee())."""

    def _page(self) -> str:
        self._add("0.1 a", checklist=["c un", "c deux"])
        return carte.html(carte.model(self.chantier))

    def _bloc_rownode(self, page: str) -> str:
        return page[page.index("function rowNode(t){"):page.index("function detailNode(t){")]

    def test_jamais_commencee_exige_aucun_critere_coche_et_aucun_depart(self):
        # c10 : les deux conditions ensemble, jamais l'une sans l'autre -- c'est ce qui
        # distingue une tâche vierge d'une tâche relancée après un échec (checkDone à 0
        # mais elapsedS déjà posé par le premier passage).
        page = self._page()
        self.assertIn(
            "function jamaisCommencee(t){return t.checkDone===0&&t.elapsedS==null}", page,
        )

    def test_une_tache_jamais_lancee_perd_sa_ligne_de_progression(self):
        # c10 : jamaisLancee vient s'ajouter au garde-fou existant de rprog (!condense),
        # sans toucher à sa condition interne ni à celle des tests qui la découpent au
        # caractère près (TestEcritureDuRapportEnCours).
        page = self._page()
        bloc = self._bloc_rownode(page)
        self.assertIn("if(!condense&&!jamaisLancee){", bloc)

    def test_lidentifiant_et_le_titre_rejoignent_la_meme_ligne_si_jamais_lancee(self):
        # c10 : le titre rejoint l'en-tête (même ligne que l'identifiant) au lieu
        # d'ouvrir sa propre ligne, seulement pour une tâche vierge et fermée.
        page = self._page()
        bloc = self._bloc_rownode(page)
        self.assertIn('head.appendChild(el("span","rtitle",t.title));', bloc)
        self.assertIn('row.appendChild(el("div","rtitle",t.title));', bloc)

    def test_la_case_vierge_repasse_len_tete_en_flux_normal(self):
        # c10 : .rhead reste flex pour toute case active (id + liens), mais redevient un
        # flux de texte normal pour une case vierge -- c'est ce flux, pas un calcul de
        # largeur, qui fait passer le titre à la ligne suivante seulement s'il ne tient
        # pas.
        page = self._page()
        self.assertIn(".row.vierge:not(.sel) .rhead{display:block}", page)

    def test_un_espace_franc_separe_lidentifiant_et_le_titre(self):
        # c10, constaté par l'humain sur le premier rendu : le flux normal ne pose AUCUN
        # espace entre deux éléments en ligne -- sans marge explicite, le titre collerait
        # à l'identifiant. Même valeur que le gap habituel de .rhead en mode flex (7px),
        # jamais une valeur inventée. Vérifié en pixels réels (pas à l'oeil) par le
        # script Playwright dédié à la mesure de hauteur (voir le rapport de la tâche).
        page = self._page()
        self.assertIn(".row.vierge:not(.sel) .rtitle{margin-left:7px}", page)

    def test_une_tache_relancee_apres_echec_garde_son_affichage_complet(self):
        # Le point de prudence du brief : checkDone à 0 mais un premier passage déjà
        # daté (startedAt posé) -- jamaisCommencee() doit rendre faux, prouvé au niveau
        # des données que rowNode() consomme (voir la garde ci-dessus, qui lit ces deux
        # mêmes champs).
        a = self._add("0.1 a", checklist=["c un"])
        self._set_state(a["id"], state="failed", startedAt="2020-01-01T00:00:00Z")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["checkDone"], 0)
        self.assertIsNotNone(t["elapsedS"])

    def test_jetons_et_temps_absents_dune_tache_jamais_lancee(self):
        # c11 : durée et jetons sont des chaînes vides dès que la tâche n'a jamais
        # tourné (elapsedS/usage tous deux absents) -- rowNode ne les affiche que si la
        # chaîne est non vide (voir "if(durTxt)" et "if(t.tokens&&!condense)").
        a = self._add("0.1 a")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertEqual(t["duree"], "")
        self.assertEqual(t["tokens"], "")
        page = carte.html(carte.model(self.chantier))
        bloc = self._bloc_rownode(page)
        self.assertIn("if(durTxt)links.appendChild(", bloc)
        self.assertIn('if(t.tokens&&!condense){', bloc)

    def test_jetons_et_temps_presents_sur_une_tache_qui_tourne(self):
        # c11, l'autre sens : dès que la tâche tourne, sa durée redevient non vide -- la
        # garde plus haut n'est jamais un silence permanent, seulement conditionnel.
        a = self._add("0.1 a")
        self._set_state(a["id"], state="running", startedAt="2020-01-01T00:00:00Z")
        t = carte.vue(carte.model(self.chantier))["tasks"][0]
        self.assertNotEqual(t["duree"], "")

    def test_une_tache_annulee_avant_tout_depart_nest_pas_vierge(self):
        # Régression : une tâche ANNULÉE sans jamais avoir tourné est à la fois
        # jamaisCommencee() (checkDone à 0, elapsedS nul) ET settled() -- sans cette
        # exclusion, elle recevrait à la fois la mise en page du repli de tâche (t-44,
        # brief t-58 point 2 : "vérifie que les trois cohabitent") et celle de la case
        # vierge, deux mises en page incompatibles sur la même case. jamaisLancee doit
        # rester faux dans ce cas -- le repli de tâche la prend déjà en charge.
        page = carte.html(carte.model(self.chantier))
        bloc = page[page.index("function rowNode(t){"):page.index("function detailNode(t){")]
        self.assertIn(
            "var jamaisLancee=jamaisCommencee(t)&&!sel&&!settled(t);", bloc,
        )


# ---------------------------------------------------------------------------
# Pliage des phases : ouverture par défaut, suivi automatique, forçage humain
# ---------------------------------------------------------------------------


class TestPliageDesPhases(CarteTestCase):
    """Ces tests portent sur le rendu JS embarqué dans la page (voir TestHtml et
    TestMurQuota pour le précédent) : il n'y a pas de navigateur ici, donc pas de DOM à
    faire tourner. Ce qui est vérifié est le CONTRAT que la page embarque, assez précis
    pour casser si le calcul d'ouverture ou le regroupement condensé régressent."""

    def _page(self) -> str:
        self._add("0.1 a")
        return carte.html(carte.model(self.chantier))

    def test_le_defaut_d_ouverture_est_linverse_de_fini(self):
        # c12/c13 (brief t-58, point 6) : au premier rendu, avant tout choix de l'humain,
        # seule une phase FINIE (toutes ses tâches réglées) part fermée -- toute autre,
        # y compris celle dont aucune tâche n'a encore démarré, part OUVERTE. Fini le
        # filet à une seule phase (t-18) : chaque phase décide pour elle-même, sans
        # dépendre de ce qui tourne ailleurs dans la campagne.
        page = self._page()
        self.assertIn("var defautOuvert=!fini;", page)
        self.assertIn("forced===undefined?defautOuvert:!forced", page)

    def test_le_forcage_manuel_decide_seul_quand_il_est_pose(self):
        # La même expression que ci-dessus le prouve structurellement : quand `forced` est
        # défini, seul `!forced` compte dans la branche -- jamais `defautOuvert`. Un
        # changement d'état qui rouvrirait ou refermerait une phase forcée casserait
        # cette chaîne.
        page = carte.panneau("/tmp/ordo-home", "c-7")
        self.assertIn("forced===undefined?defautOuvert:!forced", page)

    def test_une_phase_entierement_finie_reste_fermee(self):
        # c13 : `fini` (déjà calculé pour la condensation) vaut aussi de défaut
        # d'ouverture -- une phase où tout est fait ou annulé n'a plus rien à montrer en
        # tête de mur.
        page = self._page()
        self.assertIn(
            "var fini=all.length>0&&all.every(settled);", page,
        )
        avant_fini = page.index("var fini=all.length>0&&all.every(settled);")
        avant_defaut = page.index("var defautOuvert=!fini;")
        self.assertLess(avant_fini, avant_defaut)

    def test_une_phase_jamais_commencee_souvre(self):
        # c12 : aucune tâche démarrée ne rend pas `fini` vrai (aucune n'est réglée), donc
        # `defautOuvert` reste vrai -- exactement le cas que camcast montrait fermé à
        # tort avant cette retouche.
        page = self._page()
        self.assertIn("var defautOuvert=!fini;", page)
        # Régression : l'ancien filet à une seule phase (anyRunning/defaultKey) a disparu,
        # pas seulement contourné -- sinon une phase jamais commencée resterait fermée
        # dès qu'une autre phase de la campagne tourne.
        self.assertNotIn("anyRunning", page)
        self.assertNotIn("defaultKey", page)

    def test_les_phases_finies_repliees_se_regroupent_en_bande_condensee(self):
        page = self._page()
        self.assertIn('el("div","condensed")', page)
        self.assertIn('el("div","pchip"', page)
        self.assertIn(".condensed{display:flex;flex-wrap:wrap", page)

    def test_une_puce_condensee_garde_sa_cle_son_libelle_et_son_compte(self):
        page = self._page()
        self.assertIn('chip.appendChild(el("span","pkey m",p.key||"-"))', page)
        self.assertIn('chip.appendChild(el("span","cname",p.name))', page)
        self.assertIn('chip.appendChild(el("span","pcount m",faits+"/"+all.length))', page)

    def test_une_puce_condensee_se_rouvre_par_le_meme_mecanisme_que_l_en_tete(self):
        # Le même geste "S.closed[p.key]=open;render()" doit apparaître exactement deux
        # fois : une pour l'en-tête d'une phase normale, une pour la puce condensée. Toute
        # divergence romprait la promesse que la réouverture ne change rien à l'affichage.
        page = self._page()
        self.assertEqual(page.count("S.closed[p.key]=open;render()"), 2)

    def test_une_puce_condensee_ne_deborde_pas_sur_un_libelle_long(self):
        page = self._page()
        self.assertIn(
            '.pchip .cname{font-size:11px;color:var(--txt2);min-width:0;overflow:hidden;',
            page,
        )

    def test_un_espace_de_defilement_suit_la_fin_du_plateau(self):
        # De la place après le contenu, pas un défilement : c'est l'humain qui scrolle.
        page = self._page()
        self.assertIn("#board{position:relative;padding:14px 14px 70vh}", page)
        colonne = carte.panneau("/tmp/ordo-home", "c-7")
        self.assertIn("#board{padding:11px 11px 70vh}", colonne)

    def test_aucun_defilement_automatique_n_accompagne_le_nouvel_espace(self):
        # Compte figé des appels qui déplacent le défilement tout seuls : select() en garde
        # un (recentrer la tâche choisie), ordoSetData et ordoRestoreScroll deux autres
        # (revenir où on était). Un troisième site de scrollTo ou un second de scrollBy
        # signalerait un défilement automatique ajouté à tort.
        self.assertEqual(carte._JS.count("scrollTo("), 2)
        self.assertEqual(carte._JS.count("scrollBy("), 1)
        self.assertNotIn("scrollIntoView", carte._JS)


# ---------------------------------------------------------------------------
# Rang sans voisin : la case reprend la largeur libre (brief t-58, point 7)
# ---------------------------------------------------------------------------


class TestRangSansVoisinReprendLaLargeur(CarteTestCase):
    """Le placement par rang (une colonne par niveau de dépendance, de gauche à droite,
    retour à la ligne quand ça ne tient plus) ne change pas : lui seul décide QUELS
    rangs partagent une ligne. Seule la LARGEUR d'un rang qui n'a personne à sa droite
    sur sa ligne change -- mesurée sur le DOM réel, puisque le flex-wrap ne dit lui-même
    jamais où une ligne casse (voir elargirRangsSansVoisin())."""

    def _page(self) -> str:
        self._add("0.1 a")
        return carte.html(carte.model(self.chantier))

    def test_la_fonction_de_mesure_existe_et_est_appelee_au_rendu(self):
        # c14 : appelée pour chaque phase OUVERTE une fois ses rangs attachés au DOM --
        # jamais sur une phase fermée (display:none), où la mesure ne voudrait rien dire.
        page = self._page()
        self.assertIn("function elargirRangsSansVoisin(rangsEl){", page)
        self.assertIn("if(open)elargirRangsSansVoisin(rangs);", page)

    def test_le_placement_par_rang_et_niveau_nest_pas_touche(self):
        # Précaution 1 du brief : le calcul des rangs (parNiveau, le tri par niveau, le
        # flex-wrap qui revient à la ligne) reste exactement celui d'avant -- seule la
        # largeur d'un rang change, jamais son contenu ni son ordre.
        page = self._page()
        self.assertIn(
            "reste.forEach(function(t){(parNiveau[t.level]=parNiveau[t.level]||[]).push(t)});",
            page,
        )

    def test_un_rang_sans_voisin_a_sa_droite_recoit_la_classe_etire(self):
        # c14 : le dernier rang d'une ligne qui ne se remplit pas -- seul ou non --
        # reçoit la classe qui lève son plafond de largeur ; flex-grow (déjà posé sur
        # .rang) fait le reste, aucun calcul de pixel n'est écrit à la main ici.
        page = self._page()
        bloc = page[
            page.index("function elargirRangsSansVoisin(rangsEl){"):
            page.index("function render(){")
        ]
        self.assertIn('rangs[j].classList.add("etire")', bloc)
        self.assertIn(".view-graphe .rang.etire{max-width:none}", page)

    def test_un_rang_plein_ne_recoit_jamais_la_classe_etire(self):
        # c15 : la classe n'est posée QUE quand il reste de la place après le dernier
        # rang d'une ligne (un seuil de tolérance couvre l'arrondi des sous-pixels) --
        # jamais inconditionnellement.
        page = self._page()
        bloc = page[
            page.index("function elargirRangsSansVoisin(rangsEl){"):
            page.index("function render(){")
        ]
        self.assertIn("if(cadre.right-droite>2)", bloc)
        avant_condition = bloc.index("if(cadre.right-droite>2)")
        avant_classe = bloc.index('rangs[j].classList.add("etire")')
        self.assertLess(avant_condition, avant_classe)


# ---------------------------------------------------------------------------
# Compteurs de liens : partis de la case fermée, gardés au détail (brief t-58)
# ---------------------------------------------------------------------------


class TestCompteursDeLiensSurLaCase(CarteTestCase):
    """Le nombre de dépendances et de dépendantes (les flèches ↑/↓) n'a de lecteur
    qu'une fois la tâche ouverte : personne ne les lit sans ouvrir la case, et ils
    occupaient le coin haut droit au moment où la place manque le plus (point 1)."""

    def _page(self) -> str:
        self._add("0.1 a")
        return carte.html(carte.model(self.chantier))

    def test_la_case_fermee_ne_construit_plus_les_compteurs_de_liens(self):
        # c2 : ni la classe "nup"/"ndown"/"nnone", ni le chiffre "↑"/"↓" ne sont plus
        # construits par rowNode -- recherché sur le seul corps de la fonction, pour ne
        # pas confondre avec le chip .col de detailNode plus bas (qui, lui, doit rester).
        page = self._page()
        bloc = page[page.index("function rowNode(t){"):page.index("function detailNode(t){")]
        self.assertNotIn('t.deps.length?"nup"', bloc)
        self.assertNotIn('t.dependants.length?"ndown"', bloc)
        self.assertNotIn('"nnone"', bloc)

    def test_les_teintes_nup_ndown_nnone_ont_quitte_la_feuille_de_style(self):
        # c2, pas de couleur morte : une classe qui ne se pose plus nulle part n'a plus
        # sa règle dans _CSS.
        page = self._page()
        self.assertNotIn(".nup{", page)
        self.assertNotIn(".ndown{", page)
        self.assertNotIn(".nnone{", page)

    def test_le_detail_garde_le_compte_des_dependances_et_des_dependantes(self):
        # c3 : la chaîne attend/débloque du panneau de détail (déjà existante) porte
        # toujours ces deux chiffres -- rien à construire, seulement vérifier qu'ils n'ont
        # pas disparu avec le retrait ci-dessus.
        page = self._page()
        self.assertIn('el("div","k up","attend ("+t.deps.length+")")', page)
        self.assertIn('el("div","k down","débloque ("+t.dependants.length+")")', page)


class TestCondensationDesTachesReglees(CarteTestCase):
    """Une case finie ou annulée, fermée, occupait la même hauteur qu'une case en cours et
    affichait des chiffres qui n'ont plus de lecteur (contexte de jetons figé, progression
    de checklist redondante avec l'état "fait"). Même principe que la condensation des
    phases (voir TestPliageDesPhases), un cran plus bas : reste cliquable, retrouve sa
    présentation entière une fois ouverte."""

    def test_le_repli_se_declenche_sur_une_case_reglee_et_fermee(self):
        # Repose sur settled(), déjà partagée avec le repli des phases : une tâche annulée
        # (c5) suit donc mécaniquement le même sort qu'une tâche finie, sans code séparé.
        page = carte.html(carte.model(self.chantier))
        self.assertIn("var condense=settled(t)&&!sel;", page)

    def test_le_contexte_de_jetons_disparait_sur_une_case_condensee(self):
        # c2 : la session qui a produit ce chiffre est morte, il ne bouge plus.
        page = carte.html(carte.model(self.chantier))
        self.assertIn('if(t.tokens&&!condense){', page)

    def test_la_progression_de_checklist_disparait_sur_une_case_condensee(self):
        # c3 : "7/7" ne dit rien de plus que l'état "fait" et la couleur de la barre. Le
        # bloc entier (compteur, critère en cours, mention de fin de rapport) est enveloppé
        # d'un seul garde-fou, sans toucher à sa condition interne ni à son indentation --
        # les tests de TestEcritureDuRapportEnCours, qui découpent ce bloc au caractère
        # près, doivent rester valables tels quels.
        page = carte.html(carte.model(self.chantier))
        bloc = page[page.index("function rowNode(t){"):page.index('if(sel){')]
        garde = bloc.index("if(!condense&&!jamaisLancee){")
        interne = bloc.index("if(t.checkTotal){")
        self.assertLess(garde, interne)

    def test_la_duree_et_le_modele_restent_inconditionnels(self):
        # c4 (brief t-24) : ce que la case garde n'est jamais soumis à `condense`. Preuve
        # structurelle -- les deux constructions sont posées avant le seul garde-fou qui
        # enveloppe un bloc entier (la checklist) ; l'identifiant est même construit avant
        # que `condense` soit déclaré. Les compteurs de dépendances ont quitté la case
        # fermée (brief t-58, point 1) : ils ne figurent plus ici, voir
        # TestCompteursDeLiensSurLaCase.
        page = carte.html(carte.model(self.chantier))
        bloc = page[page.index("function rowNode(t){"):page.index("function detailNode(t){")]
        garde_checklist = bloc.index("if(!condense&&!jamaisLancee){")
        self.assertLess(bloc.index('el("span","dur",durTxt)'), garde_checklist)
        self.assertLess(bloc.index('head.appendChild(rid)'), garde_checklist)

    def test_une_case_condensee_reste_cliquable_et_repliee_seulement_fermee(self):
        # c6 : `condense` vaut faux dès que sel est vrai -- la case ouverte reconstruit tout
        # le bloc de checklist et de contexte, exactement comme avant t-24. Et rowNode ne
        # perd ni son data-row ni son écouteur de clic, quel que soit l'état.
        page = carte.html(carte.model(self.chantier))
        self.assertIn("var condense=settled(t)&&!sel;", page)
        self.assertIn('row.setAttribute("data-row",t.id)', page)
        self.assertIn('row.addEventListener("click",function(e){e.stopPropagation();select(t.id)});', page)

    def test_une_case_annulee_suit_le_meme_repli_qu_une_case_finie(self):
        # c5, au niveau du modèle cette fois : settled() traite done et cancelled à
        # l'identique, donc condense aussi, sans qu'aucune tâche annulée n'ait besoin de son
        # propre garde-fou.
        page = carte.html(carte.model(self.chantier))
        self.assertIn(
            'function settled(t){var k=kind(t);return k==="done"||k==="cancelled"}', page,
        )


# ---------------------------------------------------------------------------
# Repli des tâches réglées : t-13 et t-24 un cran plus bas encore (t-44)
# ---------------------------------------------------------------------------


class TestRepliDesTachesSousNode(CarteTestCase):
    """La règle de repli dépend de données (l'état des dépendantes directes), pas d'un
    seul contrat de chaîne : aucune assertion de chaîne ne peut la prouver, seule son
    exécution le peut -- même principe que TestFinDeChantierSousNode et
    TestMurDistingueTroisEtats pour leurs propres calculs.

    Les quatre cas tranchés par l'orchestratrice : finie sans dépendante (se replie),
    finie avec toutes ses dépendantes réglées (se replie), finie avec une dépendante
    encore vivante (NE se replie PAS -- c'est le repère de progression, celui qui montre
    d'où vient ce qui tourne), et non finie (ne se replie jamais, quel que soit l'état de
    ses dépendantes)."""

    def _fonctions(self) -> str:
        if shutil.which("node") is None:
            self.skipTest("node absent, impossible d'exécuter le JS de la carte")
        js = carte._JS
        debut = js.index("function finReport(t){")
        fin = js.index("function focusId(){return S.hover||S.sel}")
        return js[debut:fin]

    def _tache(self, id_, status, dependants=None, ready=False):
        return {
            "id": id_, "status": status, "dependants": dependants or [],
            "ready": ready, "checkTotal": 0, "checkDone": 0,
        }

    def _repliable(self, byid: dict, id_: str) -> bool:
        script = (
            "var byId=" + json.dumps(byid) + ";\n"
            + self._fonctions()
            + f'\nprocess.stdout.write(JSON.stringify(repliable(byId["{id_}"])));'
        )
        sortie = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=10, check=True,
        )
        return json.loads(sortie.stdout)

    def test_finie_sans_dependante_se_replie(self):
        byid = {"a": self._tache("a", "done")}
        self.assertTrue(self._repliable(byid, "a"))

    def test_annulee_sans_dependante_se_replie(self):
        byid = {"a": self._tache("a", "cancelled")}
        self.assertTrue(self._repliable(byid, "a"))

    def test_finie_avec_toutes_dependantes_reglees_se_replie(self):
        byid = {
            "a": self._tache("a", "done", dependants=["b", "c"]),
            "b": self._tache("b", "done"),
            "c": self._tache("c", "cancelled"),
        }
        self.assertTrue(self._repliable(byid, "a"))

    def test_finie_avec_une_dependante_encore_vivante_ne_se_replie_pas(self):
        # Le cas qui protège le repère (c7) : une seule dépendante encore vivante suffit
        # à garder la case, peu importe l'état exact de cette dépendante.
        for etat_vivant in ("running", "queued", "blocked"):
            with self.subTest(etat_vivant=etat_vivant):
                byid = {
                    "a": self._tache("a", "done", dependants=["b"]),
                    "b": self._tache("b", etat_vivant),
                }
                self.assertFalse(self._repliable(byid, "a"))

    def test_une_seule_dependante_vivante_parmi_plusieurs_suffit_a_bloquer_le_repli(self):
        byid = {
            "a": self._tache("a", "done", dependants=["b", "c"]),
            "b": self._tache("b", "done"),
            "c": self._tache("c", "running"),
        }
        self.assertFalse(self._repliable(byid, "a"))

    def test_non_finie_ne_se_replie_jamais(self):
        for status in ("running", "queued", "blocked"):
            with self.subTest(status=status):
                byid = {"a": self._tache("a", status)}
                self.assertFalse(self._repliable(byid, "a"))

    def test_non_finie_avec_toutes_dependantes_reglees_ne_se_replie_pas_non_plus(self):
        # L'état de la tâche elle-même prime toujours : ses dépendantes réglées n'avancent
        # rien tant qu'elle ne l'est pas elle-même.
        byid = {
            "a": self._tache("a", "running", dependants=["b"]),
            "b": self._tache("b", "done"),
        }
        self.assertFalse(self._repliable(byid, "a"))

    def test_une_dependante_disparue_du_graphe_ne_bloque_pas_le_repli(self):
        # Référence pendante (tâche supprimée à la main de state.json) : défensif, comme
        # related() ailleurs dans ce même fichier -- absente vaut réglée, jamais vivante.
        byid = {"a": self._tache("a", "done", dependants=["fantome"])}
        self.assertTrue(self._repliable(byid, "a"))


class TestRepliDesTachesEnTeteDePhase(CarteTestCase):
    """Le câblage du repli dans render() : regroupe les tâches repliables en tête de
    phase sans toucher ni au repli de phase (t-13) ni à la condensation de case (t-24).
    Depuis t-48, le graphe est le seul mode : le repli s'applique sans condition de vue."""

    def _page(self) -> str:
        self._add("0.1 a")
        return carte.html(carte.model(self.chantier))

    def test_le_repli_s_applique_sans_condition_de_vue(self):
        page = self._page()
        self.assertIn("var repliees=shown.filter(repliable);", page)
        self.assertIn(
            "var reste=shown.filter(function(t){return !repliable(t)});", page,
        )

    def test_les_taches_repliables_se_groupent_en_une_liste_en_tete_de_phase(self):
        page = self._page()
        self.assertIn('el("div","rlist repli")', page)
        # La liste repliée se construit et s'ajoute à la section AVANT la grille de cases
        # (`rangs`) : c'est ce qui la met en tête de phase, pas seulement dans son style.
        i_repli = page.index('el("div","rlist repli")')
        i_rangs = page.index('var rangs=el("div","rangs");')
        self.assertLess(i_repli, i_rangs)

    def test_les_taches_repliables_gardent_l_ordre_numerique_du_groupe(self):
        # c5 : aucun tri propre au repli -- `shown` vient de `p.order`, déjà trié par
        # _num_id() côté Python (voir model()), et `filter` préserve cet ordre tel quel.
        page = self._page()
        self.assertIn("var repliees=shown.filter(repliable);", page)
        self.assertNotIn("repliees.sort(", page)

    def test_une_case_repliee_garde_rowNode_intact_data_row_et_clic_compris(self):
        # c6 : aucun paramètre ajouté à rowNode, aucune branche conditionnelle sur son
        # data-row ou son écouteur de clic -- la case repliée reste cliquable et
        # sélectionnable exactement comme une case de la grille.
        page = self._page()
        self.assertIn("repliees.forEach(function(t){listeRepli.appendChild(rowNode(t))});", page)

    def test_une_case_selectionnee_n_est_plus_stylee_par_repli(self):
        # c6 : `.repli` ne s'applique qu'à `.row:not(.sel)` -- une case repliée puis
        # sélectionnée retrouve sa présentation entière au lieu de rester tassée sur une
        # ligne trop étroite pour son détail ouvert.
        page = self._page()
        self.assertIn(".repli .row:not(.sel){display:flex;", page)

    def test_une_ligne_repliee_ne_revient_jamais_a_la_ligne(self):
        # c4 : le titre est le seul champ de longueur imprévisible sur cette ligne : c'est
        # lui qui se coupe à l'ellipse, jamais le conteneur qui l'entoure.
        page = self._page()
        self.assertIn(
            ".repli .row:not(.sel) .rtitle{flex:1 1 auto;min-width:0;margin:0;"
            "font-size:10.5px;\noverflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
            page,
        )

    def test_le_repli_de_phase_de_t_13_reste_intact(self):
        page = self._page()
        self.assertIn('el("div","condensed")', page)
        self.assertIn('el("div","pchip"', page)

    def test_la_condensation_de_case_de_t_24_reste_intacte(self):
        page = self._page()
        self.assertIn("var condense=settled(t)&&!sel;", page)
        self.assertIn('if(t.tokens&&!condense){', page)


# ---------------------------------------------------------------------------
# Condensation de la liste repliée : t-44 un cran plus loin (t-56)
# ---------------------------------------------------------------------------


class TestCondensationDeLaListeRepliee(CarteTestCase):
    """Une phase ancienne pouvait replier vingt tâches (t-44), une ligne chacune, qui
    mangeaient la moitié de l'écran et repoussaient hors de vue la tâche en cours plus
    bas -- sur un mur de colonnes, il fallait défiler dans chaque colonne pour trouver
    ce qui travaille. Au-delà de cinq, cette liste se condense à son tour en une seule
    ligne portant leur nombre, dépliable et repliable au clic, sans jamais toucher aux
    tâches non repliables (point 4 du brief)."""

    def _fonctions(self) -> str:
        if shutil.which("node") is None:
            self.skipTest("node absent, impossible d'exécuter le JS de la carte")
        js = carte._JS
        debut = js.index("function el(tag,cls,txt){")
        fin = js.index("function focusId(){return S.hover||S.sel}")
        return js[debut:fin]

    def _construire(self, n: int, ouvert) -> dict:
        script = (
            "function makeEl(){var e={hidden:false,textContent:'',title:'',className:'',"
            "type:'',style:{},children:[],firstChild:null,_h:{}};"
            "e.appendChild=function(c){this.children.push(c);"
            "this.firstChild=this.children[0]||null;return c};"
            "e.addEventListener=function(ev,fn){this._h[ev]=fn};"
            "e.click=function(){if(this._h.click)this._h.click()};"
            "return e}\n"
            "var document={createElement:function(){return makeEl()}};\n"
            + self._fonctions()
            + "\nvar repliees=[];for(var i=0;i<" + str(n) + ";i++)repliees.push({id:'t'+i});"
            + "\nvar bascules=[];"
            + "\nfunction rowNodeFaux(t){var e=makeEl();e.textContent=t.id;return e}"
            + "\nvar liste=construireListeRepli(repliees," + json.dumps(ouvert)
            + ",rowNodeFaux,function(v){bascules.push(v)});"
            + "\nvar bouton=(liste.children[0]&&"
            + "liste.children[0].className.indexOf('rrepli')===0)?liste.children[0]:null;"
            + "\nif(bouton)bouton.click();"
            + "\nprocess.stdout.write(JSON.stringify({"
            + "enfants:liste.children.length,"
            + "boutonPresent:!!bouton,"
            + "boutonOuvert:bouton?bouton.className.indexOf('open')>=0:null,"
            + "boutonTexte:bouton?bouton.children[1].textContent:null,"
            + "bascules:bascules}));"
        )
        sortie = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=10, check=True,
        )
        return json.loads(sortie.stdout)

    # -- c6 : cinq ou moins, rien ne change ---------------------------------------------

    def test_cinq_taches_repliees_ne_condensent_jamais(self):
        r = self._construire(5, None)
        self.assertFalse(r["boutonPresent"])
        self.assertEqual(r["enfants"], 5)

    def test_cinq_taches_repliees_ne_condensent_pas_meme_forcees_fermees(self):
        # Le seuil prime : en dessous, il n'y a même pas de bouton à ouvrir ou fermer.
        r = self._construire(5, False)
        self.assertFalse(r["boutonPresent"])
        self.assertEqual(r["enfants"], 5)

    # -- c9 : le seuil testé dans les deux sens -----------------------------------------

    def test_six_taches_repliees_condensent(self):
        r = self._construire(6, None)
        self.assertTrue(r["boutonPresent"])
        self.assertEqual(r["enfants"], 1)

    # -- c3 : la ligne condensée porte le nombre, lisible comme cliquable --------------

    def test_la_ligne_condensee_porte_le_nombre(self):
        r = self._construire(12, None)
        self.assertTrue(r["boutonPresent"])
        self.assertEqual(r["boutonTexte"], "12 tâches réglées")
        self.assertEqual(r["enfants"], 1)

    # -- c4 : dépliage et repliage au clic, même élément dans les deux sens ------------

    def test_le_clic_sur_la_ligne_fermee_la_deplie(self):
        r = self._construire(9, False)
        self.assertFalse(r["boutonOuvert"])
        self.assertEqual(r["bascules"], [True])

    def test_le_clic_sur_la_ligne_ouverte_la_replie(self):
        r = self._construire(9, True)
        self.assertTrue(r["boutonOuvert"])
        self.assertEqual(r["bascules"], [False])
        self.assertEqual(r["enfants"], 10)  # le bouton (ouvert) + les 9 cases

    # -- point 4 du brief : jamais de tâche non repliable dans ce compte --------------

    def test_ne_recoit_et_ne_construit_jamais_que_les_repliees(self):
        js = carte._JS
        self.assertIn(
            "var listeRepli=construireListeRepli(repliees,S.repliOuvert[p.key],rowNode,",
            js,
        )
        self.assertNotIn("construireListeRepli(reste", js)
        self.assertNotIn("construireListeRepli(shown", js)

    # -- c5 : le choix manuel se garde dans S, jamais réinitialisé ---------------------

    def test_le_choix_repose_sur_S_et_ne_se_reinitialise_jamais(self):
        # Même mécanisme que S.closed pour le repli de phase (t-13) : une clé par phase
        # dans un objet initialisé une seule fois, mutée seulement par le clic qui la
        # bascule -- jamais recréée ni vidée par ordoSetData ou ailleurs dans render().
        js = carte._JS
        self.assertIn("repliOuvert:{}", js)
        self.assertEqual(js.count("S.repliOuvert[p.key]=v;render()"), 1)
        self.assertNotIn("S.repliOuvert={}", js)
        debut = js.index("window.ordoSetData=function(data){")
        fin = js.index("};", debut)
        self.assertNotIn("repliOuvert", js[debut:fin])


class TestPastilleDeModeleSurLaCase(CarteTestCase):
    """Le modèle teintait le TEXTE de l'identifiant (brief t-46). Retouche demandée par
    l'humain (brief t-58, point 2) : ça abîmait la lisibilité de l'identifiant, ce qu'on
    cherche en premier, pour porter une information secondaire. À la place, une pastille
    ronde de la couleur du modèle, et l'identifiant rendu en blanc comme n'importe quel
    texte -- exactement le rendu déjà en place au détail (voir .mdot dans
    TestPastilleDeModeleDansLeDetail), réutilisé tel quel sur la case compacte.
    """

    def _page(self) -> str:
        self._add("0.1 a")
        return carte.html(carte.model(self.chantier))

    def test_le_javascript_ne_construit_plus_de_badge_texte_sur_la_case(self):
        # Avant cette retouche, rowNode() écrivait le nom du modèle en toutes lettres à
        # côté de l'identifiant (ancien badge .mdl). Il a disparu de la case compacte.
        page = self._page()
        self.assertNotIn('el("span","mdl m"+connu', page)

    def test_lidentifiant_ne_porte_plus_aucune_classe_de_teinte(self):
        # c5 : le texte de l'identifiant redevient un texte comme un autre -- rowNode ne
        # lui pose plus la classe de modèle, c'est la pastille qui la porte désormais.
        page = self._page()
        bloc = page[page.index("function rowNode(t){"):page.index("function detailNode(t){")]
        self.assertIn('var rid=el("span","rid",t.id);', bloc)

    def test_lidentifiant_est_blanc_comme_nimporte_quel_texte(self):
        # c5 : même variable que .rtitle/.cid, jamais la grise --dim2 qui servait de
        # fond neutre avant que le modèle ne s'y pose (brief t-46).
        page = self._page()
        self.assertIn(
            ".rid{font-size:10.5px;font-weight:600;color:var(--txt);flex:none}", page)

    def test_aucune_teinte_de_modele_sur_lidentifiant_dans_la_feuille_de_style(self):
        # Régression : .rid.haiku/.sonnet/.opus (brief t-46) ne doivent plus exister --
        # la teinte vit uniquement sur .mdot désormais, jamais recalculée deux fois.
        page = self._page()
        for modele in ("haiku", "sonnet", "opus"):
            self.assertNotIn(f".rid.{modele}{{color:var(--m-{modele})}}", page)

    def test_une_pastille_ronde_reprend_les_teintes_du_modele_sur_la_case(self):
        # c4 : rowNode construit désormais un .mdot -- même classe, mêmes couleurs que
        # celles déjà posées au détail (partagées, jamais recopiées, voir .mdot.haiku
        # etc. dans TestPastilleDeModeleDansLeDetail) -- avant l'identifiant.
        page = self._page()
        bloc = page[page.index("function rowNode(t){"):page.index("function detailNode(t){")]
        self.assertIn('var mdot=el("span","mdot"+mconnu);', bloc)
        # Ordre d'ATTACHEMENT au DOM, pas d'écriture du code : la pastille doit se voir
        # avant l'identifiant sur la case, même si sa construction est lue plus bas dans
        # la source (elle dépend de t.model, connu seulement après `rid`).
        self.assertLess(bloc.index('head.appendChild(mdot)'), bloc.index('head.appendChild(rid)'))

    def test_le_survol_porte_le_modele_en_clair_sur_la_pastille_connue(self):
        # c4 : title HTML, atteignable au doigt comme à la souris (minimum demandé par le
        # brief) -- posé sur la pastille, qui porte désormais la teinte du modèle.
        page = self._page()
        bloc = page[page.index("function rowNode(t){"):page.index("function detailNode(t){")]
        self.assertIn('mdot.title=titreModele;', bloc)

    def test_aucune_pastille_pour_un_modele_herite_ou_absent(self):
        # c9, point de prudence du brief : {haiku:1,sonnet:1,opus:1}[t.model] est
        # undefined pour "herite", "defaut" et l'absence de modèle -- AUCUNE pastille ne
        # se construit alors, pas une pastille grise. La construction du .mdot est sous
        # la garde `if(mconnu)`, jamais posée inconditionnellement.
        page = self._page()
        bloc = page[page.index("function rowNode(t){"):page.index("function detailNode(t){")]
        self.assertIn('var mconnu={haiku:1,sonnet:1,opus:1}[t.model]?" "+t.model:"";', bloc)
        garde = bloc.index("if(mconnu){")
        construction = bloc.index('var mdot=el("span","mdot"+mconnu);')
        self.assertLess(garde, construction)

    def test_un_modele_non_reconnu_garde_son_infobulle_sur_lidentifiant(self):
        # Un modèle tapé à la main hors table (donc sans pastille, voir ci-dessus) ne
        # perd pas pour autant son infobulle -- elle retombe sur l'identifiant, seul
        # porteur restant de l'information pour ce cas.
        page = self._page()
        bloc = page[page.index("function rowNode(t){"):page.index("function detailNode(t){")]
        self.assertIn("rid.title=titreModele", bloc)

    def test_aucune_classe_de_teinte_pour_un_modele_herite_ou_absent(self):
        # c9 : {haiku:1,sonnet:1,opus:1}[t.model] est undefined pour "herite", "defaut" et
        # l'absence de modèle -- mconnu reste vide dans ces trois cas, jamais une sixième
        # teinte inventée pour "on ne sait pas".
        page = self._page()
        self.assertIn(
            'var mconnu={haiku:1,sonnet:1,opus:1}[t.model]?" "+t.model:"";', page)


class TestPastilleDeModeleDansLeDetail(CarteTestCase):
    """Une fois la case dépliée (sélection ou panneau de détail), il y a la place : le
    nom du modèle s'écrit en entier à côté de sa pastille de couleur (retouche demandée
    par l'humain en cours de tâche, brief t-46).
    """

    def _page(self) -> str:
        self._add("0.1 a")
        return carte.html(carte.model(self.chantier))

    def test_le_detail_ecrit_le_nom_du_modele_a_cote_de_sa_pastille(self):
        page = self._page()
        self.assertIn('md.appendChild(el("span","mdot"+connu));', page)
        self.assertIn('md.appendChild(document.createTextNode(t.model));', page)

    def test_la_pastille_du_detail_partage_les_memes_teintes_que_lidentifiant(self):
        page = self._page()
        for modele in ("haiku", "sonnet", "opus"):
            self.assertIn(f".mdot.{modele}{{background:var(--m-{modele})}}", page)


class TestLegendeDesEtatsEtDesModeles(CarteTestCase):
    """La légende (bloc #legend) enseigne le code couleur au lieu de le laisser
    deviner au survol -- retouche demandée par l'humain en cours de tâche, brief t-46.
    """

    def _page(self) -> str:
        self._add("0.1 a")
        return carte.html(carte.model(self.chantier))

    def test_la_pastille_de_redaction_du_rapport_rejoint_les_trois_deja_en_legende(self):
        # --finishing existe depuis t-23 et n'avait jamais rejoint la légende.
        page = self._page()
        self.assertIn(
            '<span class="item"><span class="sw" style="background:var(--finishing)">'
            '</span>rédaction du rapport</span>',
            page,
        )

    def test_les_pastilles_de_la_legende_viennent_des_variables_css_pas_dun_hex_recopie(self):
        # Deux tables recopiées à la main finiraient par diverger : la légende doit lire
        # la même source que .sb (états) et .rid/.mdot (modèles), jamais un hex à part.
        page = self._page()
        bloc = re.search(r'<div id="legend">.*?</div>', page, re.S).group(0)
        self.assertNotIn("#", bloc.split("legend")[-1].replace('id="legend"', ""))
        for var in ("--done", "--running", "--finishing", "--ready", "--queued",
                    "--m-haiku", "--m-sonnet", "--m-opus"):
            self.assertIn(f"var({var})", bloc)

    def test_la_legende_des_modeles_suit_celle_des_etats_avec_un_separateur(self):
        page = self._page()
        bloc = re.search(r'<div id="legend">.*?</div>', page, re.S).group(0)
        self.assertIn('<span class="lsep"></span>', bloc)
        self.assertLess(bloc.index('background:var(--queued)'), bloc.index('class="lsep"'))
        self.assertLess(bloc.index('class="lsep"'), bloc.index('background:var(--m-haiku)'))

    def test_les_pastilles_de_modele_sont_rondes_pour_ne_pas_se_confondre_avec_les_etats(self):
        page = self._page()
        for modele in ("haiku", "sonnet", "opus"):
            self.assertIn(f'<span class="sw rond" style="background:var(--m-{modele})">'
                          f'</span>{modele}</span>', page)


class TestTeintesDeModeleNeCollisionnentPasAvecLEtat(CarteTestCase):
    """Le cœur du risque nommé par le brief t-46 : ajouter une seconde grille de couleur
    (le modèle) sur une case qui utilise déjà la couleur pour l'état peut rendre les deux
    illisibles. Ce test compare les VALEURS, il ne se fie jamais à l'oeil.
    """

    _SEUIL_DEGRES = 25  # en dessous, deux teintes se lisent comme une seule couleur

    @staticmethod
    def _hex_vers_hsl(hexcode: str) -> tuple[float, float, float]:
        h = hexcode.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        teinte, luminosite, saturation = colorsys.rgb_to_hls(r, g, b)
        return teinte * 360, saturation, luminosite

    @staticmethod
    def _distance_de_teinte(a: float, b: float) -> float:
        d = abs(a - b) % 360
        return min(d, 360 - d)

    def _etats(self) -> dict[str, str]:
        motif = r"--(done|running|finishing|ready|blocked|queued|cancelled):(#[0-9a-fA-F]{6})"
        return dict(re.findall(motif, carte._CSS))

    def _modeles(self) -> dict[str, str]:
        motif = r"--m-(haiku|sonnet|opus):(#[0-9a-fA-F]{6})"
        return dict(re.findall(motif, carte._CSS))

    def test_les_trois_teintes_de_modele_sont_definies(self):
        self.assertEqual(set(self._modeles()), {"haiku", "sonnet", "opus"})

    def test_aucune_teinte_de_modele_ne_collisionne_avec_une_couleur_detat(self):
        etats = self._etats()
        modeles = self._modeles()
        for nom_etat, hex_etat in etats.items():
            teinte_etat, sat_etat, _ = self._hex_vers_hsl(hex_etat)
            if sat_etat < 0.2:
                continue  # gris (queued/cancelled) : aucune teinte ne s'y confond
            for nom_modele, hex_modele in modeles.items():
                teinte_modele, _, _ = self._hex_vers_hsl(hex_modele)
                distance = self._distance_de_teinte(teinte_etat, teinte_modele)
                self.assertGreaterEqual(
                    distance, self._SEUIL_DEGRES,
                    f"{nom_modele} ({hex_modele}) à {distance:.0f}° de {nom_etat} "
                    f"({hex_etat}) : trop proche pour rester distinguable une fois le nom "
                    "du modèle retiré de la case compacte.",
                )

    def test_les_teintes_de_modele_ne_se_confondent_pas_entre_elles(self):
        modeles = self._modeles()
        noms = list(modeles)
        for i, a in enumerate(noms):
            for b in noms[i + 1:]:
                ta, _, _ = self._hex_vers_hsl(modeles[a])
                tb, _, _ = self._hex_vers_hsl(modeles[b])
                self.assertGreaterEqual(self._distance_de_teinte(ta, tb), self._SEUIL_DEGRES)

    def test_les_anciennes_teintes_de_badge_ne_sont_plus_dans_la_feuille_de_style(self):
        # Régression : #7fb08a/#6fa8dc/#b892e0 étaient les couleurs du badge avant t-46,
        # presque identiques à done/ready/finishing -- exactement le risque nommé par le
        # brief. Si elles réapparaissent un jour sur .rid ou .mdot, la collision revient.
        for ancienne in ("#7fb08a", "#6fa8dc", "#b892e0"):
            self.assertNotIn(ancienne, carte._CSS)
