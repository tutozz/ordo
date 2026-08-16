"""Tests unitaires de ordo/chantier.py.

Isoles par ORDO_HOME, comme test_store.py. propagate_failures() opere sur un dict
construit a la main : ces cas-la n'ont pas besoin de filesystem.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import chantier, store


class ChantierTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-chantier-test-")
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


class TestStartRefuseUnProjetFantome(ChantierTestCase):
    """start() refuse un repertoire inexistant, quel que soit le detour par canon().

    canon() d'un chemin relatif inexistant rend un absolu plausible mais faux, et le
    chantier s'ouvrait alors en silence sur un dossier fantome : toute executante lancee
    dessus aurait echoue au demarrage, loin de la cause. Trouve par une vraie session en
    recette du skill, pas par une suite unitaire.
    """

    def test_refuse_un_nom_relatif_qui_nest_pas_un_repertoire(self):
        with self.assertRaises(chantier.ChantierError) as ctx:
            chantier.start("nom-qui-nexiste-pas", "objectif")
        self.assertIn("existing directory", str(ctx.exception))

    def test_refuse_un_chemin_absolu_inexistant(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.start(str(Path(self._tmp) / "absent"), "objectif")

    def test_refuse_un_fichier_qui_nest_pas_un_repertoire(self):
        fichier = Path(self._tmp) / "fichier.txt"
        fichier.write_text("pas un répertoire", encoding="utf-8")
        with self.assertRaises(chantier.ChantierError):
            chantier.start(str(fichier), "objectif")

    def test_refuse_un_chemin_vide(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.start("", "objectif")

    def test_aucun_chantier_nest_cree_par_un_refus(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.start("nom-qui-nexiste-pas", "objectif")
        self.assertEqual(store.load()["chantiers"], {})
        self.assertEqual(store.load()["seq"]["chantier"], 0)


class TestStart(ChantierTestCase):
    def test_start_creates_chantier_with_expected_fields(self):
        projet = self._projet("mon-projet")
        c = chantier.start(str(projet), "objectif clair", perimetre="tout", hors_scope="rien")
        self.assertEqual(c["id"], "c-01")
        self.assertEqual(c["slug"], "mon-projet")
        self.assertEqual(c["project"], store.canon(projet))
        self.assertEqual(c["objectif"], "objectif clair")
        self.assertEqual(c["perimetre"], "tout")
        self.assertEqual(c["horsScope"], "rien")
        self.assertEqual(c["state"], "open")
        self.assertEqual(c["tmuxSession"], "ordo-mon-projet")
        self.assertIsNone(c["tmuxWindow"])
        self.assertEqual(c["permissions"], "skip")
        self.assertIsNone(c["closedAt"])
        self.assertFalse(c["capteur"]["adopted"])
        self.assertEqual(c["capteur"]["runs"], [])

    def test_start_persists_the_chantier(self):
        projet = self._projet()
        c = chantier.start(str(projet), "obj")
        reloaded = store.load()
        self.assertIn(c["id"], reloaded["chantiers"])

    def test_start_canonicalizes_project_path(self):
        projet = self._projet()
        c = chantier.start(f"{projet}/../{projet.name}", "obj")
        self.assertEqual(c["project"], store.canon(projet))


class TestStartSessionUniqueness(ChantierTestCase):
    """Point A : tmuxSession unique par construction.

    Deux projets de meme basename (/a/api et /b/api) ne doivent jamais recevoir la meme
    session tmux, sans quoi leurs executantes atterrissent dans la meme fenetre.
    """

    def _projet_sous(self, sous_dossier: str, nom: str) -> Path:
        p = Path(self._tmp) / sous_dossier / nom
        p.mkdir(parents=True, exist_ok=True)
        return p

    def test_deux_projets_meme_basename_recoivent_des_sessions_differentes(self):
        proj_a = self._projet_sous("a", "api")
        proj_b = self._projet_sous("b", "api")
        c1 = chantier.start(str(proj_a), "obj")
        c2 = chantier.start(str(proj_b), "obj", home_partage=True)
        self.assertEqual(c1["tmuxSession"], "ordo-api")
        self.assertEqual(c2["tmuxSession"], f"ordo-api-{c2['id']}")
        self.assertNotEqual(c1["tmuxSession"], c2["tmuxSession"])

    def test_le_nom_de_session_est_fige_a_la_creation(self):
        # Ferme le premier chantier "api" : le nom fige du deuxieme ne doit pas se
        # recalculer vers "ordo-api" a la relecture.
        proj_a = self._projet_sous("a", "api")
        proj_b = self._projet_sous("b", "api")
        chantier.start(str(proj_a), "obj")
        c2 = chantier.start(str(proj_b), "obj", home_partage=True)
        reloaded = store.load()["chantiers"][c2["id"]]
        self.assertEqual(reloaded["tmuxSession"], c2["tmuxSession"])

    def test_considere_les_chantiers_fermes_aussi(self):
        # "quel que soit son etat" : un chantier ferme portant deja "ordo-api" compte
        # toujours pour la collision, pas seulement les chantiers ouverts.
        proj_a = self._projet_sous("a", "api")
        proj_b = self._projet_sous("b", "api")
        c1 = chantier.start(str(proj_a), "obj")
        chantier.close(c1["id"])
        c2 = chantier.start(str(proj_b), "obj")
        self.assertEqual(c2["tmuxSession"], f"ordo-api-{c2['id']}")


class TestStartHomeUnique(ChantierTestCase):
    """Point D : un home par projet. I8, aucun refus silencieux."""

    def test_refuse_un_second_projet_different_dans_le_meme_home(self):
        proj_a = self._projet("a")
        proj_b = self._projet("b")
        c1 = chantier.start(str(proj_a), "obj")
        with self.assertRaises(chantier.ChantierError) as ctx:
            chantier.start(str(proj_b), "obj")
        message = str(ctx.exception)
        self.assertIn(c1["id"], message)
        self.assertIn(store.canon(proj_a), message)
        self.assertIn("ORDO_HOME", message)

    def test_aucun_chantier_nest_cree_par_le_refus(self):
        proj_a = self._projet("a")
        proj_b = self._projet("b")
        chantier.start(str(proj_a), "obj")
        with self.assertRaises(chantier.ChantierError):
            chantier.start(str(proj_b), "obj")
        self.assertEqual(len(store.load()["chantiers"]), 1)

    def test_home_partage_leve_le_refus(self):
        proj_a = self._projet("a")
        proj_b = self._projet("b")
        chantier.start(str(proj_a), "obj")
        c2 = chantier.start(str(proj_b), "obj", home_partage=True)
        self.assertEqual(c2["project"], store.canon(proj_b))

    def test_second_projet_autorise_une_fois_le_premier_ferme(self):
        proj_a = self._projet("a")
        proj_b = self._projet("b")
        c1 = chantier.start(str(proj_a), "obj")
        chantier.close(c1["id"])
        c2 = chantier.start(str(proj_b), "obj")
        self.assertEqual(c2["project"], store.canon(proj_b))

    def test_meme_projet_reouvert_nest_pas_un_conflit(self):
        proj_a = self._projet("a")
        chantier.start(str(proj_a), "obj")
        c2 = chantier.start(str(proj_a), "obj second")
        self.assertEqual(c2["project"], store.canon(proj_a))


class TestStartPermissions(ChantierTestCase):
    """Point G : permissions "skip" (defaut) ou "normal", validees a la creation."""

    def test_defaut_est_skip(self):
        c = chantier.start(str(self._projet()), "obj")
        self.assertEqual(c["permissions"], "skip")

    def test_accepte_normal(self):
        c = chantier.start(str(self._projet()), "obj", permissions="normal")
        self.assertEqual(c["permissions"], "normal")

    def test_refuse_une_valeur_invalide(self):
        with self.assertRaises(chantier.ChantierError) as ctx:
            chantier.start(str(self._projet()), "obj", permissions="yolo")
        self.assertIn("yolo", str(ctx.exception))

    def test_valeur_invalide_ne_cree_aucun_chantier(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.start(str(self._projet()), "obj", permissions="yolo")
        self.assertEqual(store.load()["chantiers"], {})


class TestAddTask(ChantierTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]

    def test_add_task_normalizes_string_checklist(self):
        t = chantier.add_task(self.chantier_id, "titre", "prompt", checklist=["tests verts"])
        self.assertEqual(
            t["checklist"],
            [{"id": "c1", "label": "tests verts", "done": False, "dureeMin": None}],
        )
        self.assertEqual(t["state"], "queued")
        self.assertEqual(t["chantier"], self.chantier_id)
        self.assertEqual(t["priority"], 0)
        self.assertEqual(t["attempts"], 0)

    def test_add_task_accepte_une_duree_par_critere_en_minutes_claude(self):
        # brief t-27 : l'estimation vit sur le critère, en minutes-Claude, jamais sur la
        # tâche entière.
        t = chantier.add_task(
            self.chantier_id,
            "titre",
            "prompt",
            checklist=[{"label": "lire le code", "dureeMin": 15}],
        )
        self.assertEqual(
            t["checklist"],
            [{"id": "c1", "label": "lire le code", "done": False, "dureeMin": 15}],
        )

    def test_add_task_sans_duree_reste_none_pas_zero(self):
        # Un critère sans estimation ne doit jamais se lire comme "zéro minute" : les 349
        # critères existants sans durée doivent continuer de fonctionner (c8).
        t = chantier.add_task(self.chantier_id, "titre", "prompt", checklist=["sans estimation"])
        self.assertIsNone(t["checklist"][0]["dureeMin"])

    def test_add_task_unknown_chantier_raises(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.add_task("c-99", "t", "p")

    def test_add_task_unknown_dependency_raises(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.add_task(self.chantier_id, "t", "p", depends_on=("t-99",))

    def test_add_task_accepts_valid_dependency(self):
        a = chantier.add_task(self.chantier_id, "a", "prompt")["id"]
        b = chantier.add_task(self.chantier_id, "b", "prompt", depends_on=(a,))
        self.assertEqual(b["dependsOn"], [a])


class TestChecklistHorsConvention(unittest.TestCase):
    """checklist_hors_convention() est pure : aucun ORDO_HOME requis."""

    def test_aucun_label_trop_long_rend_liste_vide(self):
        checklist = [
            {"id": "c1", "label": "ok", "done": False, "dureeMin": None},
            {"id": "c2", "label": "toujours ok", "done": False, "dureeMin": None},
        ]
        self.assertEqual(chantier.checklist_hors_convention(checklist), [])

    def test_un_label_trop_long_est_signale_avec_sa_longueur_reelle(self):
        checklist = [{"id": "c1", "label": "x" * 63, "done": False, "dureeMin": None}]
        self.assertEqual(
            chantier.checklist_hors_convention(checklist), [{"id": "c1", "length": 63}]
        )

    def test_la_limite_pile_au_seuil_ne_declenche_rien(self):
        checklist = [
            {"id": "c1", "label": "x" * chantier.CHECKLIST_LABEL_MAX, "done": False,
             "dureeMin": None}
        ]
        self.assertEqual(chantier.checklist_hors_convention(checklist), [])

    def test_un_cran_au_dessus_du_seuil_declenche(self):
        checklist = [
            {"id": "c1", "label": "x" * (chantier.CHECKLIST_LABEL_MAX + 1), "done": False,
             "dureeMin": None}
        ]
        self.assertEqual(
            chantier.checklist_hors_convention(checklist),
            [{"id": "c1", "length": chantier.CHECKLIST_LABEL_MAX + 1}],
        )

    def test_ne_modifie_pas_la_checklist_recue(self):
        checklist = [{"id": "c1", "label": "x" * 70, "done": False, "dureeMin": None}]
        avant = [dict(item) for item in checklist]
        chantier.checklist_hors_convention(checklist)
        self.assertEqual(checklist, avant)


class TestChecklistSansDuree(unittest.TestCase):
    """checklist_sans_duree() : pure, comme checklist_hors_convention() (brief t-27)."""

    def test_aucune_absence_rend_liste_vide(self):
        checklist = [
            {"id": "c1", "label": "a", "done": False, "dureeMin": 10},
            {"id": "c2", "label": "b", "done": False, "dureeMin": 5},
        ]
        self.assertEqual(chantier.checklist_sans_duree(checklist), [])

    def test_signale_chaque_item_sans_duree(self):
        checklist = [
            {"id": "c1", "label": "a", "done": False, "dureeMin": 10},
            {"id": "c2", "label": "b", "done": False, "dureeMin": None},
        ]
        self.assertEqual(chantier.checklist_sans_duree(checklist), [{"id": "c2"}])

    def test_un_item_sans_la_cle_du_tout_compte_comme_sans_duree(self):
        # Un critère créé avant t-27 n'a jamais eu cette clé : absente, pas seulement
        # None, doit être tolérée à l'identique (c8).
        checklist = [{"id": "c1", "label": "vieux critère", "done": False}]
        self.assertEqual(chantier.checklist_sans_duree(checklist), [{"id": "c1"}])


class TestDependConfinedToChantier(ChantierTestCase):
    """Point F : une dependance ne peut jamais traverser deux chantiers.

    Aujourd'hui _get_task() cherche dans le dict global des taches : un `dep t-04 --on
    t-09` entre deux projets sans rapport est accepte en silence. Ce n'est plus le cas.
    """

    def setUp(self) -> None:
        super().setUp()
        self.c1 = chantier.start(str(self._projet("p1")), "obj1")["id"]
        self.c2 = chantier.start(str(self._projet("p2")), "obj2", home_partage=True)["id"]

    def test_add_task_refuse_une_dependance_dun_autre_chantier(self):
        t1 = chantier.add_task(self.c1, "a", "prompt")["id"]
        with self.assertRaises(chantier.ChantierError) as ctx:
            chantier.add_task(self.c2, "b", "prompt", depends_on=(t1,))
        message = str(ctx.exception)
        self.assertIn(self.c1, message)
        self.assertIn(self.c2, message)

    def test_add_task_refuse_ne_cree_pas_la_tache(self):
        t1 = chantier.add_task(self.c1, "a", "prompt")["id"]
        with self.assertRaises(chantier.ChantierError):
            chantier.add_task(self.c2, "b", "prompt", depends_on=(t1,))
        taches_c2 = [t for t in store.load()["taches"].values() if t["chantier"] == self.c2]
        self.assertEqual(taches_c2, [])

    def test_depend_refuse_une_dependance_dun_autre_chantier(self):
        t1 = chantier.add_task(self.c1, "a", "prompt")["id"]
        t2 = chantier.add_task(self.c2, "b", "prompt")["id"]
        with self.assertRaises(chantier.ChantierError) as ctx:
            chantier.depend(t2, t1)
        message = str(ctx.exception)
        self.assertIn(self.c1, message)
        self.assertIn(self.c2, message)
        state = store.load()
        self.assertNotIn(t1, state["taches"][t2]["dependsOn"])


class TestDependAndCycle(ChantierTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]

    def _task(self, titre: str = "t") -> str:
        return chantier.add_task(self.chantier_id, titre, "prompt")["id"]

    def test_depend_adds_dependency(self):
        a, b = self._task("a"), self._task("b")
        updated = chantier.depend(b, a)
        self.assertIn(a, updated["dependsOn"])

    def test_depend_refuses_self_dependency(self):
        a = self._task("a")
        with self.assertRaises(chantier.ChantierError):
            chantier.depend(a, a)

    def test_depend_unknown_task_raises(self):
        a = self._task("a")
        with self.assertRaises(chantier.ChantierError):
            chantier.depend(a, "t-99")

    def test_has_cycle_none_on_acyclic_graph(self):
        tasks = {
            "t-01": {"id": "t-01", "dependsOn": []},
            "t-02": {"id": "t-02", "dependsOn": ["t-01"]},
        }
        self.assertIsNone(chantier.has_cycle(tasks))

    def test_has_cycle_finds_the_loop(self):
        tasks = {
            "t-01": {"id": "t-01", "dependsOn": ["t-03"]},
            "t-02": {"id": "t-02", "dependsOn": ["t-01"]},
            "t-03": {"id": "t-03", "dependsOn": ["t-02"]},
        }
        cycle = chantier.has_cycle(tasks)
        self.assertIsNotNone(cycle)
        self.assertEqual(set(cycle), {"t-01", "t-02", "t-03"})

    def test_i7_cycle_abandonne_le_graphe(self):
        # I7 : un cycle abandonne le graphe entier. L'aplatir produirait un plan valide
        # en apparence qui s'execute dans le mauvais ordre.
        a = self._task("a")
        b = chantier.add_task(self.chantier_id, "b", "prompt", depends_on=(a,))["id"]
        with self.assertRaises(chantier.ChantierError) as ctx:
            chantier.depend(a, b)  # fermerait a -> b -> a
        message = str(ctx.exception)
        self.assertIn(a, message)
        self.assertIn(b, message)
        # le refus doit etre total : aucune dependance partielle n'est restee en place
        state = store.load()
        self.assertNotIn(b, state["taches"][a]["dependsOn"])


class TestReady(ChantierTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]

    def _mark_done(self, task_id: str) -> None:
        with store.locked() as state:
            state["taches"][task_id]["state"] = "done"

    def test_i1_checklist_est_une_post_condition(self):
        # I1 : la checklist d'une tache conditionne le lancement de ses DEPENDANTS,
        # jamais le sien. L'inverse interbloque : le seul acteur capable de cocher est la
        # session que la checklist empecherait de demarrer.
        a = chantier.add_task(self.chantier_id, "a", "prompt", checklist=["tests verts"])["id"]
        b = chantier.add_task(self.chantier_id, "b", "prompt", depends_on=(a,))["id"]

        # A n'a pas de dependance : sa propre checklist non cochee ne doit jamais
        # l'empecher d'etre prete.
        ready_ids = {t["id"] for t in chantier.ready(self.chantier_id)}
        self.assertIn(a, ready_ids)

        # A est termine mais sa checklist reste non cochee : B ne doit pas etre pret.
        self._mark_done(a)
        ready_ids = {t["id"] for t in chantier.ready(self.chantier_id)}
        self.assertNotIn(b, ready_ids)

        # la case cochee : B devient pret.
        chantier.check(a, "c1")
        ready_ids = {t["id"] for t in chantier.ready(self.chantier_id)}
        self.assertIn(b, ready_ids)

    def test_ready_ignores_non_queued_tasks(self):
        a = chantier.add_task(self.chantier_id, "a", "prompt")["id"]
        self._mark_done(a)
        ready_ids = {t["id"] for t in chantier.ready(self.chantier_id)}
        self.assertNotIn(a, ready_ids)

    def test_ready_unknown_chantier_raises(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.ready("c-99")


class TestTaskEdits(ChantierTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]
        self.task_id = chantier.add_task(self.chantier_id, "t", "prompt", checklist=["c"])["id"]

    def test_cancel_sets_state_and_finished_at(self):
        t = chantier.cancel(self.task_id)
        self.assertEqual(t["state"], "cancelled")
        self.assertIsNotNone(t["finishedAt"])

    def test_cancel_already_terminal_raises(self):
        chantier.cancel(self.task_id)
        with self.assertRaises(chantier.ChantierError):
            chantier.cancel(self.task_id)

    def test_prioritize_sets_priority(self):
        t = chantier.prioritize(self.task_id, 5)
        self.assertEqual(t["priority"], 5)

    def test_check_marks_item_done(self):
        t = chantier.check(self.task_id, "c1")
        self.assertTrue(t["checklist"][0]["done"])

    def test_check_unknown_item_raises(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.check(self.task_id, "c-inconnu")

    def test_amend_updates_prompt_when_not_running(self):
        t = chantier.amend(self.task_id, "nouveau prompt")
        self.assertEqual(t["prompt"], "nouveau prompt")

    def test_i8_aucun_refus_silencieux(self):
        # I8 : toute operation refusee dit laquelle et pourquoi. amend() refuse une tache
        # en cours d'execution : le seul acteur capable d'amender ne doit jamais court-
        # circuiter silencieusement l'exécutante en train de travailler sur l'ancien texte.
        with store.locked() as state:
            state["taches"][self.task_id]["state"] = "running"
        with self.assertRaises(chantier.ChantierError) as ctx:
            chantier.amend(self.task_id, "tentative pendant l'execution")
        message = str(ctx.exception)
        self.assertIn(self.task_id, message)
        # le refus est reel, pas cosmetique : le prompt original n'a pas bouge
        state = store.load()
        self.assertEqual(state["taches"][self.task_id]["prompt"], "prompt")


class TestChecklistGrowth(ChantierTestCase):
    """add_checklist_item / split_checklist_item / reword_checklist_item (brief t-22).

    Ce que ces trois fonctions ouvrent à l'exécutante elle-même : elle découvre en
    travaillant qu'un critère en valait deux, ou qu'un pan de travail n'avait pas été
    prévu. Ce qu'elles n'ouvrent JAMAIS, et c'est le cœur du contrat : aucune des trois
    ne peut faire disparaître un id existant, voir TestChecklistNeverShrinks plus bas.
    """

    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]
        self.task_id = chantier.add_task(
            self.chantier_id, "t", "prompt", checklist=["premier critère"]
        )["id"]

    def test_add_appends_a_fresh_id_at_the_end(self):
        t = chantier.add_checklist_item(self.task_id, "second critère")
        self.assertEqual(
            t["checklist"],
            [
                {"id": "c1", "label": "premier critère", "done": False, "dureeMin": None},
                {"id": "c2", "label": "second critère", "done": False, "dureeMin": None},
            ],
        )

    def test_add_ne_touche_pas_aux_items_existants(self):
        chantier.check(self.task_id, "c1")
        t = chantier.add_checklist_item(self.task_id, "second critère")
        self.assertEqual(
            t["checklist"][0],
            {"id": "c1", "label": "premier critère", "done": True, "dureeMin": None},
        )

    def test_add_sur_tache_inconnue_leve(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.add_checklist_item("t-99", "x")

    def test_add_deux_fois_ne_reutilise_jamais_un_id(self):
        chantier.add_checklist_item(self.task_id, "second")
        t = chantier.add_checklist_item(self.task_id, "troisième")
        self.assertEqual([i["id"] for i in t["checklist"]], ["c1", "c2", "c3"])

    def test_split_garde_lid_original_sur_la_premiere_moitie(self):
        t = chantier.split_checklist_item(self.task_id, "c1", "moitié a", "moitié b")
        self.assertEqual(
            t["checklist"][0], {"id": "c1", "label": "moitié a", "done": False, "dureeMin": None}
        )
        self.assertEqual(
            t["checklist"][1], {"id": "c2", "label": "moitié b", "done": False, "dureeMin": None}
        )

    def test_split_repartit_la_duree_sans_la_dupliquer(self):
        # brief t-27 : un split doit répartir la durée, pas la dupliquer -- les deux
        # moitiés ne peuvent pas valoir chacune la durée entière de l'original.
        chantier.set_checklist_duree(self.task_id, "c1", 20)
        t = chantier.split_checklist_item(self.task_id, "c1", "moitié a", "moitié b")
        self.assertEqual(t["checklist"][0]["dureeMin"], 10)
        self.assertEqual(t["checklist"][1]["dureeMin"], 10)

    def test_split_repartit_une_duree_impaire_sans_en_perdre(self):
        chantier.set_checklist_duree(self.task_id, "c1", 21)
        t = chantier.split_checklist_item(self.task_id, "c1", "moitié a", "moitié b")
        self.assertEqual(
            t["checklist"][0]["dureeMin"] + t["checklist"][1]["dureeMin"], 21
        )

    def test_split_dun_item_sans_duree_laisse_les_deux_moities_sans_duree(self):
        t = chantier.split_checklist_item(self.task_id, "c1", "moitié a", "moitié b")
        self.assertIsNone(t["checklist"][0]["dureeMin"])
        self.assertIsNone(t["checklist"][1]["dureeMin"])

    def test_split_dun_item_deja_coche_repart_a_false_sur_les_deux_moities(self):
        # Un id fusionné coché ne prouve rien sur chacune des deux moitiés séparément :
        # le garder aurait permis de gonfler le compteur sans travail réel derrière.
        chantier.check(self.task_id, "c1")
        t = chantier.split_checklist_item(self.task_id, "c1", "moitié a", "moitié b")
        self.assertFalse(t["checklist"][0]["done"])
        self.assertFalse(t["checklist"][1]["done"])

    def test_split_ne_renumerote_pas_un_troisieme_item_deja_present(self):
        chantier.add_checklist_item(self.task_id, "deuxième critère")
        t = chantier.split_checklist_item(self.task_id, "c1", "moitié a", "moitié b")
        # c2 (le "deuxième critère" ajouté avant le split) garde son id et son rang ; le
        # fragment du split arrive après lui, en c3, jamais inséré devant.
        self.assertEqual(t["checklist"][1]["id"], "c2")
        self.assertEqual(t["checklist"][1]["label"], "deuxième critère")
        self.assertEqual(
            t["checklist"][2], {"id": "c3", "label": "moitié b", "done": False, "dureeMin": None}
        )

    def test_split_item_inconnu_leve(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.split_checklist_item(self.task_id, "c-inconnu", "a", "b")

    def test_reword_change_le_libelle_garde_lid_et_letat(self):
        chantier.check(self.task_id, "c1")
        t = chantier.reword_checklist_item(self.task_id, "c1", "libellé corrigé")
        self.assertEqual(
            t["checklist"][0],
            {"id": "c1", "label": "libellé corrigé", "done": True, "dureeMin": None},
        )

    def test_reword_item_inconnu_leve(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.reword_checklist_item(self.task_id, "c-inconnu", "x")

    def test_reword_ne_touche_pas_a_la_duree(self):
        chantier.set_checklist_duree(self.task_id, "c1", 12)
        t = chantier.reword_checklist_item(self.task_id, "c1", "libellé corrigé")
        self.assertEqual(t["checklist"][0]["dureeMin"], 12)


class TestChecklistDuree(ChantierTestCase):
    """set_checklist_duree() : la révision de l'estimation par l'exécutante (brief t-27,
    c4). Le seul champ que reword/split ne couvrent pas : corriger une durée sans changer
    ni le libellé ni l'état coché du critère."""

    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]
        self.task_id = chantier.add_task(
            self.chantier_id, "t", "prompt", checklist=["premier critère"]
        )["id"]

    def test_pose_la_duree_en_minutes(self):
        t = chantier.set_checklist_duree(self.task_id, "c1", 25)
        self.assertEqual(t["checklist"][0]["dureeMin"], 25)

    def test_ne_touche_ni_au_libelle_ni_a_letat(self):
        chantier.check(self.task_id, "c1")
        t = chantier.set_checklist_duree(self.task_id, "c1", 25)
        self.assertEqual(t["checklist"][0]["label"], "premier critère")
        self.assertTrue(t["checklist"][0]["done"])

    def test_corrige_une_duree_deja_posee(self):
        chantier.set_checklist_duree(self.task_id, "c1", 25)
        t = chantier.set_checklist_duree(self.task_id, "c1", 8)
        self.assertEqual(t["checklist"][0]["dureeMin"], 8)

    def test_item_inconnu_leve(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.set_checklist_duree(self.task_id, "c-inconnu", 10)

    def test_tache_inconnue_leve(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.set_checklist_duree("t-99", "c1", 10)

    def test_duree_nulle_ou_negative_leve(self):
        # Zéro ou négatif n'est pas une estimation, c'est une absence d'estimation
        # déguisée : le champ existe déjà pour ça (None), inutile de le confondre avec 0.
        with self.assertRaises(chantier.ChantierError):
            chantier.set_checklist_duree(self.task_id, "c1", 0)
        with self.assertRaises(chantier.ChantierError):
            chantier.set_checklist_duree(self.task_id, "c1", -5)


class TestChecklistAttribut(ChantierTestCase):
    """set_checklist_attribut() : les attributs d'un critère (brief t-36), posables à la
    création comme révisables après coup -- même régime que set_checklist_duree ci-dessus,
    une clé à la fois. Objectifs par construction : clé et valeur sont pris dans
    ATTRIBUTS_VALEURS, jamais du texte libre."""

    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]
        self.task_id = chantier.add_task(
            self.chantier_id, "t", "prompt", checklist=["premier critère"]
        )["id"]

    def test_pose_un_attribut(self):
        t = chantier.set_checklist_attribut(self.task_id, "c1", "geste", "ecrire")
        self.assertEqual(t["checklist"][0]["attributs"], {"geste": "ecrire"})

    def test_pose_plusieurs_attributs_sur_le_meme_critere(self):
        chantier.set_checklist_attribut(self.task_id, "c1", "geste", "tester")
        t = chantier.set_checklist_attribut(self.task_id, "c1", "etendue", "fichier")
        self.assertEqual(
            t["checklist"][0]["attributs"], {"geste": "tester", "etendue": "fichier"}
        )

    def test_corrige_un_attribut_deja_pose(self):
        chantier.set_checklist_attribut(self.task_id, "c1", "geste", "lire")
        t = chantier.set_checklist_attribut(self.task_id, "c1", "geste", "publier")
        self.assertEqual(t["checklist"][0]["attributs"]["geste"], "publier")

    def test_ne_touche_ni_au_libelle_ni_a_letat_ni_a_la_duree(self):
        chantier.check(self.task_id, "c1")
        chantier.set_checklist_duree(self.task_id, "c1", 12)
        t = chantier.set_checklist_attribut(self.task_id, "c1", "geste", "lire")
        self.assertEqual(t["checklist"][0]["label"], "premier critère")
        self.assertTrue(t["checklist"][0]["done"])
        self.assertEqual(t["checklist"][0]["dureeMin"], 12)

    def test_cle_inconnue_leve(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.set_checklist_attribut(self.task_id, "c1", "humeur", "bonne")

    def test_valeur_invalide_pour_une_cle_connue_leve(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.set_checklist_attribut(self.task_id, "c1", "geste", "danser")

    def test_item_inconnu_leve(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.set_checklist_attribut(self.task_id, "c-inconnu", "geste", "lire")

    def test_tache_inconnue_leve(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.set_checklist_attribut("t-99", "c1", "geste", "lire")

    def test_critere_sans_attribut_reste_valide(self):
        # c6 du brief t-36 : 350 critères existent déjà sans "attributs" au monde, et rien
        # ne doit lever ni sur leur lecture ni sur leur passage dans les autres fonctions
        # de checklist -- l'absence est le cas courant, pas une erreur.
        t = store.load()["taches"][self.task_id]
        self.assertIsNone(t["checklist"][0].get("attributs"))
        # cocher, découper, reformuler, dater fonctionnent toujours sans qu'aucun attribut
        # n'ait jamais été posé.
        chantier.check(self.task_id, "c1")
        chantier.reword_checklist_item(self.task_id, "c1", "autre libellé")
        t = store.load()["taches"][self.task_id]
        self.assertTrue(t["checklist"][0]["done"])


class TestChecklistNeverShrinks(ChantierTestCase):
    """c4/c8 : rien, dans le contrat public de chantier.py, ne peut retirer un critère.

    Une exécutante qui pourrait supprimer un critère pourrait se déclarer finie en
    retirant celui qui la gêne ; le contrat le lui interdit formellement. Le refus doit
    être PROUVÉ, pas seulement l'absence d'un verbe dans l'aide (brief t-22) : ce test
    vérifie que la surface publique du module ne porte littéralement aucune fonction de
    suppression, quel que soit le nom qu'elle prendrait.
    """

    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]
        self.task_id = chantier.add_task(
            self.chantier_id, "t", "prompt", checklist=["a", "b", "c"]
        )["id"]

    def test_aucune_fonction_de_suppression_nexiste(self):
        noms_interdits = (
            "remove_checklist_item",
            "delete_checklist_item",
            "drop_checklist_item",
            "remove_item",
            "delete_item",
        )
        for nom in noms_interdits:
            self.assertFalse(
                hasattr(chantier, nom), f"chantier.{nom} ne doit pas exister (brief t-22)"
            )

    def test_add_split_reword_ne_font_jamais_baisser_le_nombre_ditems(self):
        avant = len(store.load()["taches"][self.task_id]["checklist"])
        chantier.add_checklist_item(self.task_id, "d")
        chantier.split_checklist_item(self.task_id, "c2", "b1", "b2")
        chantier.reword_checklist_item(self.task_id, "c1", "a corrigé")
        après = len(store.load()["taches"][self.task_id]["checklist"])
        self.assertGreater(après, avant)

    def test_tous_les_ids_dorigine_survivent_a_un_ajout_et_un_decoupage(self):
        ids_avant = {i["id"] for i in store.load()["taches"][self.task_id]["checklist"]}
        chantier.add_checklist_item(self.task_id, "d")
        chantier.split_checklist_item(self.task_id, "c2", "b1", "b2")
        ids_après = {i["id"] for i in store.load()["taches"][self.task_id]["checklist"]}
        self.assertTrue(ids_avant.issubset(ids_après))


class TestPropagateFailures(unittest.TestCase):
    def test_cascades_transitively(self):
        state = {
            "taches": {
                "t-01": {"id": "t-01", "chantier": "c-01", "state": "failed", "dependsOn": []},
                "t-02": {
                    "id": "t-02",
                    "chantier": "c-01",
                    "state": "queued",
                    "dependsOn": ["t-01"],
                },
                "t-03": {
                    "id": "t-03",
                    "chantier": "c-01",
                    "state": "queued",
                    "dependsOn": ["t-02"],
                },
            }
        }
        changed = chantier.propagate_failures(state, "c-01")
        self.assertEqual(set(changed), {"t-02", "t-03"})
        self.assertEqual(state["taches"]["t-02"]["state"], "blocked")
        self.assertEqual(state["taches"]["t-03"]["state"], "blocked")
        self.assertTrue(state["taches"]["t-02"]["error"])

    def test_does_not_touch_unrelated_tasks(self):
        state = {
            "taches": {
                "t-01": {"id": "t-01", "chantier": "c-01", "state": "failed", "dependsOn": []},
                "t-02": {"id": "t-02", "chantier": "c-01", "state": "queued", "dependsOn": []},
            }
        }
        changed = chantier.propagate_failures(state, "c-01")
        self.assertEqual(changed, [])
        self.assertEqual(state["taches"]["t-02"]["state"], "queued")

    def test_marks_blocked_cause_as_propagation(self):
        # La marque structuree qu'unblock_propagated() relit : sans elle, la levee de
        # blocage n'a aucun moyen fiable de distinguer ce blocage-ci d'un blocage pour
        # raison propre (voir TestUnblockPropagated).
        state = {
            "taches": {
                "t-01": {"id": "t-01", "chantier": "c-01", "state": "failed", "dependsOn": []},
                "t-02": {"id": "t-02", "chantier": "c-01", "state": "queued", "dependsOn": ["t-01"]},
            }
        }
        chantier.propagate_failures(state, "c-01")
        self.assertEqual(
            state["taches"]["t-02"]["blockedCause"], chantier.BLOCKED_CAUSE_PROPAGATION
        )

    def test_ignore_les_taches_dun_autre_chantier(self):
        # Point F : propagate_failures() ne parcourt que les taches du chantier vise.
        state = {
            "taches": {
                "t-01": {"id": "t-01", "chantier": "c-01", "state": "failed", "dependsOn": []},
                "t-02": {
                    "id": "t-02",
                    "chantier": "c-02",
                    "state": "queued",
                    "dependsOn": ["t-01"],
                },
            }
        }
        changed = chantier.propagate_failures(state, "c-01")
        self.assertEqual(changed, [])
        self.assertEqual(state["taches"]["t-02"]["state"], "queued")

    def test_ignore_une_dependance_illegitime_dun_autre_chantier(self):
        # Scenario exact du contrat : "dep t-04 --on t-09" entre deux projets sans
        # rapport, accepte en silence par une ancienne version. t-04 (chantier c-02)
        # depend illegitimement de t-09 (chantier c-01, mort). Meme cible sur c-02 (le
        # chantier de t-04 lui-meme), l'echec de c-01 ne doit jamais le bloquer.
        state = {
            "taches": {
                "t-09": {"id": "t-09", "chantier": "c-01", "state": "failed", "dependsOn": []},
                "t-04": {
                    "id": "t-04",
                    "chantier": "c-02",
                    "state": "queued",
                    "dependsOn": ["t-09"],
                },
            }
        }
        changed = chantier.propagate_failures(state, "c-02")
        self.assertEqual(changed, [])
        self.assertEqual(state["taches"]["t-04"]["state"], "queued")


class TestUnblockPropagated(unittest.TestCase):
    """Lieve le blocage pose par propagate_failures() une fois la dependance saine.

    Bug reel corrige ici, observe sur le socle ~/.claude/ordo : t-01 meurt, t-02 (qui en
    depend) est bloquee en cascade ; t-01 est relancee et reussit vraiment, coche sa
    checklist ; sans unblock_propagated(), t-02 restait bloquee pour toujours alors que
    sa seule dependance etait terminee.
    """

    def _state(self, tache_a: dict | None = None, tache_b: dict | None = None) -> dict:
        a = {
            "id": "t-01",
            "chantier": "c-01",
            "state": "done",
            "dependsOn": [],
            "checklist": [{"id": "c1", "label": "x", "done": True}],
            "error": None,
        }
        a.update(tache_a or {})
        b = {
            "id": "t-02",
            "chantier": "c-01",
            "state": "blocked",
            "dependsOn": ["t-01"],
            "checklist": [],
            "error": "dependency t-01 dead (blocked)",
            "blockedCause": chantier.BLOCKED_CAUSE_PROPAGATION,
        }
        b.update(tache_b or {})
        return {"taches": {"t-01": a, "t-02": b}}

    def test_unblocks_when_dependency_is_healthy_again(self):
        state = self._state()
        unblocked = chantier.unblock_propagated(state)
        self.assertEqual(unblocked, ["t-02"])
        t2 = state["taches"]["t-02"]
        self.assertEqual(t2["state"], "queued")
        self.assertIsNone(t2["error"])
        self.assertIsNone(t2["blockedCause"])

    def test_reuses_dependencies_satisfied_predicate_checklist_incomplete(self):
        # Meme predicat que ready() (I1) : une dependance "done" mais dont la checklist
        # n'est pas entierement cochee n'est pas saine. Deux definitions concurrentes de
        # "dependance satisfaite" divergeraient forcement avec le temps.
        state = self._state(tache_a={"checklist": [{"id": "c1", "label": "x", "done": False}]})
        unblocked = chantier.unblock_propagated(state)
        self.assertEqual(unblocked, [])
        self.assertEqual(state["taches"]["t-02"]["state"], "blocked")

    def test_stays_blocked_when_dependency_still_dead(self):
        state = self._state(tache_a={"state": "failed"})
        unblocked = chantier.unblock_propagated(state)
        self.assertEqual(unblocked, [])
        self.assertEqual(state["taches"]["t-02"]["state"], "blocked")

    def test_never_unblocks_a_task_blocked_for_its_own_reason(self):
        # LE point sur lequel la levee est jugee : une tache bloquee pour sa propre
        # raison (ici, un pane mort simule) ne doit JAMAIS etre relancee automatiquement,
        # meme quand le predicat de dependances est trivialement vrai (aucune dependance
        # du tout : le cas le plus favorable a un faux positif). Seule la marque
        # blockedCause == "propagation" autorise la levee, jamais le texte de error.
        state = {
            "taches": {
                "t-01": {
                    "id": "t-01",
                    "chantier": "c-01",
                    "state": "blocked",
                    "dependsOn": [],
                    "checklist": [],
                    "error": "dead pane, no report received (99 s after launch)",
                    "blockedCause": None,
                },
            }
        }
        unblocked = chantier.unblock_propagated(state)
        self.assertEqual(unblocked, [])
        self.assertEqual(state["taches"]["t-01"]["state"], "blocked")

    def test_legacy_blocked_task_without_marker_is_left_alone(self):
        # Decision : une tache bloquee par propagate_failures AVANT l'introduction de
        # blockedCause n'a pas la marque (cle absente du dict). On ne la debloque jamais
        # automatiquement sur la seule foi du texte de error, fragile par construction ;
        # une reprise manuelle est preferee a un deblocage fonde sur un texte reformulable.
        state = {
            "taches": {
                "t-01": {
                    "id": "t-01", "chantier": "c-01", "state": "done",
                    "dependsOn": [], "checklist": [], "error": None,
                },
                "t-02": {
                    "id": "t-02",
                    "chantier": "c-01",
                    "state": "blocked",
                    "dependsOn": ["t-01"],
                    "checklist": [],
                    "error": "dependency t-01 dead (blocked)",
                    # pas de cle "blockedCause" : simule une tache bloquee avant ce
                    # correctif, quand seul le texte de error portait la cause.
                },
            }
        }
        unblocked = chantier.unblock_propagated(state)
        self.assertEqual(unblocked, [])
        self.assertEqual(state["taches"]["t-02"]["state"], "blocked")

    def test_ignores_tasks_not_currently_blocked(self):
        state = {
            "taches": {
                "t-01": {
                    "id": "t-01", "chantier": "c-01", "state": "queued", "dependsOn": [],
                    "checklist": [], "blockedCause": chantier.BLOCKED_CAUSE_PROPAGATION,
                },
            }
        }
        self.assertEqual(chantier.unblock_propagated(state), [])


class TestClose(ChantierTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]
        self.task_id = chantier.add_task(self.chantier_id, "t", "prompt")["id"]

    def _mark_running(self, pane_id: str = "%12") -> None:
        with store.locked() as state:
            state["taches"][self.task_id]["state"] = "running"
            state["taches"][self.task_id]["paneId"] = pane_id

    def test_close_refuses_when_task_running_default_alive_check(self):
        self._mark_running()
        with self.assertRaises(chantier.ChantierError) as ctx:
            chantier.close(self.chantier_id)
        self.assertIn(self.task_id, str(ctx.exception))

    def test_close_succeeds_when_no_running_task(self):
        c, info = chantier.close(self.chantier_id)
        self.assertEqual(c["state"], "closed")
        self.assertIsNotNone(c["closedAt"])
        self.assertEqual(info["panes"], [])
        self.assertEqual(info["session"], c["tmuxSession"])
        self.assertEqual(info["archives"], [])

    def test_close_force_ignores_alive_tasks(self):
        self._mark_running()
        c, info = chantier.close(self.chantier_id, force=True)
        self.assertEqual(c["state"], "closed")

    def test_close_alive_check_injection_point_can_unblock(self):
        # point d'injection documente dans chantier.close() : sans panes.py disponible ici,
        # un alive_check(pane_id) -> bool permet a l'appelant de verifier la vivacite reelle
        # du pane plutot que de se fier au seul champ state de la tache.
        self._mark_running()
        c, info = chantier.close(self.chantier_id, alive_check=lambda pane_id: False)
        self.assertEqual(c["state"], "closed")

    def test_close_alive_check_can_still_block(self):
        self._mark_running()
        with self.assertRaises(chantier.ChantierError):
            chantier.close(self.chantier_id, alive_check=lambda pane_id: True)

    def test_close_unknown_chantier_raises(self):
        with self.assertRaises(chantier.ChantierError):
            chantier.close("c-99")

    def test_close_lists_panes_of_tasks_that_carry_one(self):
        # Deuxieme tache, sans pane : ne doit pas apparaitre dans la liste.
        chantier.add_task(self.chantier_id, "sans pane", "prompt")
        self._mark_running(pane_id="%7")
        c, info = chantier.close(self.chantier_id, force=True)
        self.assertEqual(info["panes"], ["%7"])


class TestCloseArchive(ChantierTestCase):
    """Point E : close() archive dans tous les cas, ne tue rien elle-meme.

    chantier.py n'importe jamais panes.py : c'est cli.py qui fera le geste tmux avec la
    liste de panes rendue par close(). Ici on ne teste que le deplacement de fichiers.
    """

    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]
        self.task_id = chantier.add_task(self.chantier_id, "t", "prompt")["id"]
        self.home = store.home()

    def _write(self, relatif: str, contenu: str = "x") -> Path:
        chemin = self.home / relatif
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(contenu, encoding="utf-8")
        return chemin

    def test_deplace_brief_report_journal_capteur_sous_archives(self):
        brief = self._write(f"briefs/{self.chantier_id}/{self.task_id}.md", "brief")
        report = self._write(f"reports/{self.chantier_id}/{self.task_id}.json", "{}")
        journal = self._write(f"journal/{self.chantier_id}.md", "14:00  ORDO  x")
        capteur_script = self._write(f"sensors/{self.chantier_id}.py", "#!/usr/bin/env python3")

        c, info = chantier.close(self.chantier_id)

        dest_dir = self.home / "archives" / self.chantier_id
        self.assertTrue(dest_dir.is_dir())
        self.assertFalse(brief.exists())
        self.assertFalse(report.exists())
        self.assertFalse(journal.exists())
        self.assertFalse(capteur_script.exists())
        self.assertTrue((dest_dir / f"{self.task_id}.md").exists())
        self.assertTrue((dest_dir / f"{self.task_id}.json").exists())
        self.assertTrue((dest_dir / f"{self.chantier_id}.md").exists())
        self.assertTrue((dest_dir / f"{self.chantier_id}.py").exists())
        self.assertEqual((dest_dir / f"{self.task_id}.md").read_text(encoding="utf-8"), "brief")
        self.assertEqual(len(info["archives"]), 4)
        for chemin_archive in info["archives"]:
            self.assertTrue(Path(chemin_archive).exists())

    def test_fichier_absent_nest_pas_une_erreur(self):
        # Aucun fichier ecrit sur disque pour cette tache : close() ne doit pas lever.
        c, info = chantier.close(self.chantier_id)
        self.assertEqual(c["state"], "closed")
        self.assertEqual(info["archives"], [])

    def test_archive_partielle_quand_seuls_certains_fichiers_existent(self):
        brief = self._write(f"briefs/{self.chantier_id}/{self.task_id}.md", "brief")
        c, info = chantier.close(self.chantier_id)
        dest_dir = self.home / "archives" / self.chantier_id
        self.assertTrue((dest_dir / f"{self.task_id}.md").exists())
        self.assertEqual(len(info["archives"]), 1)

    def test_ne_confond_pas_deux_chantiers_dont_lid_est_prefixe_de_lautre(self):
        # Glob "c-1*" matcherait a tort "c-10.py" en fermant "c-1" : impossible avec le
        # format next_id() (toujours 2 chiffres mini), mais reste un piege si le compteur
        # depasse 99 ("c-1" n'existe jamais, mais "c-10" est prefixe de "c-100"). On force
        # la limite ici en fabriquant directement les fichiers pour deux id voisins.
        proche = f"{self.chantier_id}0"  # ex. "c-010" si self.chantier_id == "c-01"
        garde = self._write(f"sensors/{proche}.py", "ne doit pas bouger")

        c, info = chantier.close(self.chantier_id)

        self.assertTrue(garde.exists())
        dest_dir = self.home / "archives" / self.chantier_id
        self.assertFalse((dest_dir / f"{proche}.py").exists())

    def test_narchive_pas_les_fichiers_dun_autre_chantier(self):
        autre = chantier.start(str(self._projet("autre")), "obj2", home_partage=True)["id"]
        autre_task = chantier.add_task(autre, "t", "prompt")["id"]
        garde = self._write(f"briefs/{autre}/{autre_task}.md", "ne doit pas bouger")

        chantier.close(self.chantier_id)

        self.assertTrue(garde.exists())
        self.assertFalse((self.home / "archives" / self.chantier_id / f"{autre_task}.md").exists())


class TestGraphAscii(ChantierTestCase):
    def test_graph_ascii_lists_tasks_and_dependencies(self):
        cid = chantier.start(str(self._projet()), "obj")["id"]
        a = chantier.add_task(cid, "premiere", "prompt")["id"]
        b = chantier.add_task(cid, "seconde", "prompt", depends_on=(a,))["id"]
        rendu = chantier.graph_ascii(cid)
        self.assertIn(a, rendu)
        self.assertIn(b, rendu)
        self.assertIn("premiere", rendu)
        self.assertIn("seconde", rendu)

    def test_graph_ascii_empty_chantier(self):
        cid = chantier.start(str(self._projet("p2")), "obj")["id"]
        rendu = chantier.graph_ascii(cid)
        self.assertTrue(rendu)


class TestMedianeDureeParCritere(ChantierTestCase):
    """mediane_duree_par_critere() : calibration sur l'historique réel (brief t-33).

    "durée par critère" d'une tâche = sa durée réelle (finishedAt - startedAt) divisée par
    le nombre de critères réellement cochés -- chaque tâche de ces tests ne coche qu'UN
    seul critère, pour que sa durée réelle en minutes SOIT directement la valeur qui entre
    dans la médiane, sans calcul supplémentaire à refaire dans le test.
    """

    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]

    def _tache_finie(self, minutes: int, debut: str = "2026-08-01T00:00:00Z") -> str:
        t = chantier.add_task(self.chantier_id, "t", "prompt", checklist=["c"])
        h, m, s = (int(x) for x in debut[11:19].split(":"))
        fin = f"{debut[:11]}{h:02d}:{(m + minutes) % 60:02d}:{s:02d}Z"
        if m + minutes >= 60:
            fin = f"{debut[:11]}{h + (m + minutes) // 60:02d}:{(m + minutes) % 60:02d}:{s:02d}Z"
        with store.locked() as state:
            item = state["taches"][t["id"]]
            item["state"] = "done"
            item["startedAt"] = debut
            item["finishedAt"] = fin
            item["checklist"][0]["done"] = True
        return t["id"]

    def test_home_neuf_sans_historique_rend_none(self):
        # Piège 2 : ne pas planter, ne pas poser zéro, s'abstenir.
        self.assertIsNone(chantier.mediane_duree_par_critere(store.load()))

    def test_mediane_pas_moyenne(self):
        # Piège 1 : [1, 2, 2, 3, 10] a pour médiane 2 et pour moyenne 3.6 -- une
        # implémentation qui ferait la moyenne par erreur romprait cette assertion.
        for minutes in (1, 2, 2, 3, 10):
            self._tache_finie(minutes)
        self.assertEqual(chantier.mediane_duree_par_critere(store.load()), 2)

    def test_mediane_paire_moyenne_des_deux_valeurs_du_milieu(self):
        for minutes in (1, 2, 3, 4):
            self._tache_finie(minutes)
        self.assertEqual(chantier.mediane_duree_par_critere(store.load()), 2.5)

    def test_ignore_les_taches_non_terminees(self):
        self._tache_finie(1)
        t = chantier.add_task(self.chantier_id, "en cours", "prompt", checklist=["c"])
        with store.locked() as state:
            state["taches"][t["id"]]["state"] = "running"
            state["taches"][t["id"]]["startedAt"] = "2026-08-01T00:00:00Z"
        self.assertEqual(chantier.mediane_duree_par_critere(store.load()), 1)

    def test_ignore_une_tache_sans_horodatage_de_lancement(self):
        self._tache_finie(1)
        t = chantier.add_task(self.chantier_id, "sans départ", "prompt", checklist=["c"])
        with store.locked() as state:
            item = state["taches"][t["id"]]
            item["state"] = "done"
            item["finishedAt"] = "2026-08-01T00:05:00Z"
            item["checklist"][0]["done"] = True
        self.assertEqual(chantier.mediane_duree_par_critere(store.load()), 1)

    def test_ignore_une_tache_a_lhorodatage_incoherent(self):
        # finishedAt avant startedAt : incohérent, jamais estimé à zéro (contaminerait la
        # médiane de toutes les autres tâches).
        self._tache_finie(1)
        t = chantier.add_task(self.chantier_id, "incohérente", "prompt", checklist=["c"])
        with store.locked() as state:
            item = state["taches"][t["id"]]
            item["state"] = "done"
            item["startedAt"] = "2026-08-01T00:10:00Z"
            item["finishedAt"] = "2026-08-01T00:00:00Z"
            item["checklist"][0]["done"] = True
        self.assertEqual(chantier.mediane_duree_par_critere(store.load()), 1)

    def test_ignore_une_tache_terminee_sans_aucun_critere_coche(self):
        self._tache_finie(1)
        t = chantier.add_task(self.chantier_id, "rien de coché", "prompt", checklist=["c"])
        with store.locked() as state:
            item = state["taches"][t["id"]]
            item["state"] = "done"
            item["startedAt"] = "2026-08-01T00:00:00Z"
            item["finishedAt"] = "2026-08-01T00:07:00Z"
        self.assertEqual(chantier.mediane_duree_par_critere(store.load()), 1)


class TestEstimationParDefaut(ChantierTestCase):
    """Quand un critère est créé sans durée, Ordo lui pose la médiane calculée sur ce home
    (brief t-33). Une valeur posée explicitement, elle, n'est jamais écrasée (piège 6)."""

    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]
        # Une seule tâche de référence, coche un seul critère en 6 minutes : la médiane de
        # ce home vaut exactement 6, sans ambiguïté d'arrondi.
        ref = chantier.add_task(self.chantier_id, "ref", "prompt", checklist=["c"])
        with store.locked() as state:
            item = state["taches"][ref["id"]]
            item["state"] = "done"
            item["startedAt"] = "2026-08-01T00:00:00Z"
            item["finishedAt"] = "2026-08-01T00:06:00Z"
            item["checklist"][0]["done"] = True

    def test_add_task_pose_le_defaut_sur_un_critere_sans_duree(self):
        t = chantier.add_task(self.chantier_id, "t", "prompt", checklist=["nouveau"])
        item = t["checklist"][0]
        self.assertEqual(item["dureeMin"], 6)
        self.assertTrue(item["dureeDefaut"])

    def test_add_task_necrase_jamais_une_duree_explicite(self):
        t = chantier.add_task(
            self.chantier_id, "t", "prompt", checklist=[{"label": "x", "dureeMin": 99}]
        )
        item = t["checklist"][0]
        self.assertEqual(item["dureeMin"], 99)
        self.assertNotIn("dureeDefaut", item)

    def test_add_checklist_item_pose_le_defaut(self):
        t = chantier.add_task(self.chantier_id, "t", "prompt")["id"]
        after = chantier.add_checklist_item(t, "ajouté après coup")
        item = after["checklist"][0]
        self.assertEqual(item["dureeMin"], 6)
        self.assertTrue(item["dureeDefaut"])

    def test_home_neuf_sans_historique_ne_pose_rien(self):
        # Un VRAI home neuf, pas un second chantier du même home (celui du setUp porte déjà
        # une tâche de référence) : un autre ORDO_HOME, sans aucun historique.
        autre_home = tempfile.mkdtemp(prefix="ordo-chantier-test-neuf-")
        try:
            os.environ["ORDO_HOME"] = autre_home
            cid = chantier.start(str(self._projet("neuf")), "obj")["id"]
            t = chantier.add_task(cid, "t", "prompt", checklist=["c"])
        finally:
            os.environ["ORDO_HOME"] = self._tmp
        item = t["checklist"][0]
        self.assertIsNone(item["dureeMin"])
        self.assertNotIn("dureeDefaut", item)


class TestEcartEstimeReel(ChantierTestCase):
    """ecart_estime_reel() : confrontation, sur une tâche terminée, entre l'estimation
    totale de sa checklist et sa durée réellement mesurée (brief t-33)."""

    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]

    def test_tache_plus_rapide_que_prevu(self):
        # L'exemple du brief : 110 minutes annoncées, 17 réelles.
        t = chantier.add_task(
            self.chantier_id, "t", "prompt", checklist=[{"label": "c", "dureeMin": 110}]
        )["id"]
        with store.locked() as state:
            item = state["taches"][t]
            item["state"] = "done"
            item["startedAt"] = "2026-08-01T00:00:00Z"
            item["finishedAt"] = "2026-08-01T00:17:00Z"
        ecart = chantier.ecart_estime_reel(store.load()["taches"][t])
        self.assertEqual(ecart, {"estimeMin": 110, "reelMin": 17, "ecartMin": -93})

    def test_tache_plus_lente_que_prevu(self):
        t = chantier.add_task(
            self.chantier_id, "t", "prompt", checklist=[{"label": "c", "dureeMin": 5}]
        )["id"]
        with store.locked() as state:
            item = state["taches"][t]
            item["state"] = "done"
            item["startedAt"] = "2026-08-01T00:00:00Z"
            item["finishedAt"] = "2026-08-01T00:12:00Z"
        ecart = chantier.ecart_estime_reel(store.load()["taches"][t])
        self.assertEqual(ecart, {"estimeMin": 5, "reelMin": 12, "ecartMin": 7})

    def test_tache_pas_terminee_rend_none(self):
        t = chantier.add_task(
            self.chantier_id, "t", "prompt", checklist=[{"label": "c", "dureeMin": 5}]
        )["id"]
        with store.locked() as state:
            state["taches"][t]["startedAt"] = "2026-08-01T00:00:00Z"
        self.assertIsNone(chantier.ecart_estime_reel(store.load()["taches"][t]))

    def test_aucun_critere_estime_rend_none(self):
        t = chantier.add_task(self.chantier_id, "t", "prompt", checklist=["c"])["id"]
        with store.locked() as state:
            item = state["taches"][t]
            item["state"] = "done"
            item["startedAt"] = "2026-08-01T00:00:00Z"
            item["finishedAt"] = "2026-08-01T00:12:00Z"
        self.assertIsNone(chantier.ecart_estime_reel(store.load()["taches"][t]))

    def test_horodatage_manquant_rend_none(self):
        t = chantier.add_task(
            self.chantier_id, "t", "prompt", checklist=[{"label": "c", "dureeMin": 5}]
        )["id"]
        with store.locked() as state:
            state["taches"][t]["state"] = "done"
        self.assertIsNone(chantier.ecart_estime_reel(store.load()["taches"][t]))


class TestDureeMesureeParCritere(ChantierTestCase):
    """duree_mesuree_par_critere() : la durée réelle INDIVIDUELLE de chaque critère (brief
    t-36), à partir des faits "checklist-doing" et "checklist-coche" du journal machine
    (t-34) -- remplace la seule mesure disponible jusqu'ici (_duree_reelle_par_critere),
    qui divise la durée totale d'une tâche par son nombre de critères et suppose donc
    qu'ils se valent tous.

    Fonction pure : les événements sont passés en paramètre, jamais lus sur disque ici
    (chantier.py ne peut pas importer journal.py, qui l'importe déjà -- voir le
    commentaire d'en-tête du fichier) ; c'est à l'appelant (cli.py, un test) de les
    obtenir via journal.lire_evenements()."""

    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = chantier.start(str(self._projet()), "obj")["id"]
        self.task_id = chantier.add_task(
            self.chantier_id, "t", "prompt", checklist=["premier", "second", "troisième"]
        )["id"]
        with store.locked() as state:
            state["taches"][self.task_id]["startedAt"] = "2026-08-01T00:00:00Z"

    def _task(self) -> dict:
        return store.load()["taches"][self.task_id]

    def test_ecart_entre_le_doing_et_sa_propre_coche(self):
        evenements = [
            {"tache": self.task_id, "type": "checklist-doing", "item": "c1",
             "at": "2026-08-01T00:01:00Z"},
            {"tache": self.task_id, "type": "checklist-coche", "item": "c1",
             "at": "2026-08-01T00:04:00Z"},
        ]
        durees = chantier.duree_mesuree_par_critere(self._task(), evenements)
        self.assertEqual(durees, {"c1": 3.0})

    def test_sans_doing_utilise_la_coche_precedente(self):
        evenements = [
            {"tache": self.task_id, "type": "checklist-coche", "item": "c1",
             "at": "2026-08-01T00:02:00Z"},
            {"tache": self.task_id, "type": "checklist-coche", "item": "c2",
             "at": "2026-08-01T00:07:00Z"},
        ]
        durees = chantier.duree_mesuree_par_critere(self._task(), evenements)
        # c1 : startedAt (00:00) -> sa coche (00:02) = 2 minutes.
        # c2 : coche précédente, celle de c1 (00:02) -> sa coche (00:07) = 5 minutes.
        self.assertEqual(durees, {"c1": 2.0, "c2": 5.0})

    def test_premier_critere_sans_doing_utilise_le_lancement_de_la_tache(self):
        evenements = [
            {"tache": self.task_id, "type": "checklist-coche", "item": "c1",
             "at": "2026-08-01T00:05:00Z"},
        ]
        durees = chantier.duree_mesuree_par_critere(self._task(), evenements)
        self.assertEqual(durees, {"c1": 5.0})

    def test_criteres_de_taches_differentes_ne_se_melangent_pas(self):
        autre = chantier.add_task(self.chantier_id, "autre", "prompt", checklist=["x"])["id"]
        evenements = [
            {"tache": autre, "type": "checklist-doing", "item": "c1",
             "at": "2026-08-01T00:00:30Z"},
            {"tache": autre, "type": "checklist-coche", "item": "c1",
             "at": "2026-08-01T00:09:00Z"},
            {"tache": self.task_id, "type": "checklist-doing", "item": "c1",
             "at": "2026-08-01T00:01:00Z"},
            {"tache": self.task_id, "type": "checklist-coche", "item": "c1",
             "at": "2026-08-01T00:02:00Z"},
        ]
        durees = chantier.duree_mesuree_par_critere(self._task(), evenements)
        self.assertEqual(durees, {"c1": 1.0})

    def test_evenements_hors_ordre_decriture_sont_retries(self):
        # Le journal est append-only donc déjà chronologique, mais la fonction ne DOIT
        # pas en dépendre : elle retrie par "at" plutôt que de faire confiance à l'ordre
        # d'entrée, qu'un futur appelant pourrait un jour fournir mélangé.
        evenements = [
            {"tache": self.task_id, "type": "checklist-coche", "item": "c1",
             "at": "2026-08-01T00:02:00Z"},
            {"tache": self.task_id, "type": "checklist-doing", "item": "c1",
             "at": "2026-08-01T00:01:00Z"},
        ]
        durees = chantier.duree_mesuree_par_critere(self._task(), evenements)
        self.assertEqual(durees, {"c1": 1.0})

    def test_critere_jamais_coche_absent_du_resultat(self):
        evenements = [
            {"tache": self.task_id, "type": "checklist-doing", "item": "c1",
             "at": "2026-08-01T00:01:00Z"},
        ]
        durees = chantier.duree_mesuree_par_critere(self._task(), evenements)
        self.assertEqual(durees, {})

    def test_aucun_evenement_rend_un_dict_vide(self):
        self.assertEqual(chantier.duree_mesuree_par_critere(self._task(), []), {})

    def test_ignore_les_evenements_dun_autre_type(self):
        evenements = [
            {"tache": self.task_id, "type": "tache-bloquee", "item": "c1",
             "at": "2026-08-01T00:01:00Z"},
        ]
        durees = chantier.duree_mesuree_par_critere(self._task(), evenements)
        self.assertEqual(durees, {})

    def test_coche_anterieure_ou_egale_a_son_repere_est_ignoree(self):
        # Horodatage incohérent (coche au même instant ou avant son point de départ,
        # ex. horloges qui divergent) : jamais une durée de zéro ou négative, qui
        # fausserait toute médiane ultérieure -- même parti pris que _duree_reelle_min.
        evenements = [
            {"tache": self.task_id, "type": "checklist-doing", "item": "c1",
             "at": "2026-08-01T00:05:00Z"},
            {"tache": self.task_id, "type": "checklist-coche", "item": "c1",
             "at": "2026-08-01T00:05:00Z"},
        ]
        durees = chantier.duree_mesuree_par_critere(self._task(), evenements)
        self.assertEqual(durees, {})


if __name__ == "__main__":
    unittest.main()
