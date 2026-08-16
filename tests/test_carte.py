"""Tests unitaires de ordo/carte.py.

Isoles par ORDO_HOME, comme test_chantier.py. Aucun de ces tests ne touche tmux : la
vivacite d'un pane entre par injection (parametre `alive`), exactement pour que le modele
de la carte reste verifiable sans serveur tmux.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import carte, chantier, journal, store


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

    def test_la_page_offre_les_deux_vues_et_les_deux_filtres(self):
        self._add("0.1 a")
        page = self._page()
        for cle in ('id="v-graphe"', 'id="v-liste"', 'id="f-reste"', 'id="f-tout"'):
            self.assertIn(cle, page)

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
        self.assertIn("aucune tache", self._page().lower())

    def test_la_page_est_du_html_complet(self):
        self._add("0.1 a")
        page = self._page()
        self.assertTrue(page.lstrip().lower().startswith("<!doctype html>"))
        self.assertIn("</html>", page)
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
        self.assertEqual(t["checklist"], [{"text": "c un", "done": False}])
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
                                "cacheRead": 25480808, "turns": 30},
        ))
        t = vue["tasks"][0]
        self.assertEqual(t["tokens"], "112k")
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

    def test_une_colonne_porte_le_plateau_et_les_deux_vues(self):
        p = carte.panneau("/tmp/ordo-home", "c-7")
        for cle in ('id="board"', 'id="wires"', 'id="v-graphe"', 'id="v-liste"'):
            self.assertIn(cle, p)

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
