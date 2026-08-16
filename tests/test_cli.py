"""Tests unitaires et d'integration de ordo/cli.py et bin/ordo.

Isoles par ORDO_HOME, comme les autres suites. Deux familles de tests :

- en-process, via cli.main(argv), stdout/stderr captures : rapide, couvre chaque verbe.
  Les verbes qui toucheraient un vrai pane tmux ou un vrai `claude` (launch, relaunch,
  say, capture, poll, kill, attach sur leur chemin de succes) mockent lib.panes plutot que
  de spawner quoi que ce soit de reel (I2/I4/consignes du projet : aucun test ne touche un
  vrai process claude).
- en sous-processus, via le vrai executable bin/ordo : seule facon de prouver que la
  resolution du paquet ordo/ depuis un autre repertoire fonctionne reellement (le premier
  defaut qu'une vraie session a signale), que le code de sortie est non nul sur un refus
  au niveau du VRAI binaire, et que chaque verbe de lecture cite dans SKILL.md produit du
  JSON valide en conditions reelles d'invocation.

Aucun test ne cree de session tmux reelle, aucun test n'appelle un vrai `claude`.

Certains tests ci-dessous codent contre des signatures annoncees par le contrat de
publication (docs internes au chantier) mais pas encore livrees par les agents qui
travaillent en parallele sur ordo/chantier.py et ordo/panes.py (chantier.start() acceptant
permissions=, chantier.close() renvoyant un tuple, panes.ensure_session() renvoyant
(session, window_id), panes.kill_session(), panes.set_verbose()). Ces tests sont ecrits
et laisses tels quels : voir le rapport de l'agent cli.py pour la liste exacte de ceux qui
restent rouges tant que l'integration n'a pas eu lieu.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import capteur, chantier, cli, journal, plan, store

ORDO_BIN = str(Path(__file__).resolve().parent.parent / "bin" / "ordo")


class CliTestCase(unittest.TestCase):
    """Isolation ORDO_HOME + helpers de fixture + appel en-process de cli.main()."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-cli-test-")
        self._prev_env = {
            cle: os.environ.get(cle)
            for cle in ("ORDO_HOME", "ORDO_REGISTRY", "ORDO_NO_SERVE")
        }
        os.environ["ORDO_HOME"] = self._tmp
        # Le registre du serveur de cartes est global a la machine, et `watch` demarre ce
        # serveur : sans ces deux lignes la suite ecrivait dans le vrai fichier de
        # l'utilisateur et allumait un daemon sur le port de production.
        os.environ["ORDO_REGISTRY"] = str(Path(self._tmp) / "registre.json")
        os.environ["ORDO_NO_SERVE"] = "1"

    def tearDown(self) -> None:
        for cle, valeur in self._prev_env.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _projet(self, nom: str = "p") -> Path:
        p = Path(self._tmp) / nom
        p.mkdir(exist_ok=True)
        return p

    def _chantier(self, **kw) -> str:
        defaults = {"objectif": "objectif clair", "perimetre": "tout", "hors_scope": "rien"}
        defaults.update(kw)
        projet = kw.get("projet") or self._projet()
        return chantier.start(
            str(projet), defaults["objectif"],
            perimetre=defaults["perimetre"],
            hors_scope=defaults["hors_scope"],
            # Decision D (chantier.py) : un second projet different dans le meme home
            # ouvert est refuse par defaut depuis que l'autre agent l'a livree ; les
            # tests qui veulent volontairement deux projets passent home_partage=True.
            home_partage=kw.get("home_partage", False),
            permissions=kw.get("permissions", "skip"),
        )["id"]

    def _tache(self, chantier_id: str, **kw) -> str:
        titre = kw.pop("titre", "titre")
        prompt = kw.pop("prompt", "prompt")
        return chantier.add_task(chantier_id, titre, prompt, **kw)["id"]

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _run_json(self, argv: list[str]):
        code, out, err = self._run(argv)
        self.assertEqual(code, 0, msg=f"attendu succès, obtenu {code} ; stderr={err!r}")
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            self.fail(f"sortie non JSON pour {argv} : {exc}\nsortie={out!r}")


# ---------------------------------------------------------------------------
# Chantier
# ---------------------------------------------------------------------------


class TestChantierVerbs(CliTestCase):
    def test_start_json(self):
        projet = self._projet()
        data = self._run_json(
            ["start", str(projet), "--goal", "obj", "--scope", "peri",
             "--out-of-scope", "hs", "--json"]
        )
        self.assertEqual(data["id"], "c-01")
        self.assertEqual(data["objectif"], "obj")
        self.assertEqual(data["perimetre"], "peri")
        self.assertEqual(data["horsScope"], "hs")

    def test_start_texte_contient_id_et_projet(self):
        projet = self._projet()
        code, out, _ = self._run(["start", str(projet), "--goal", "obj"])
        self.assertEqual(code, 0)
        self.assertIn("c-01", out)
        self.assertIn(store.canon(projet), out)

    def test_start_permissions_defaut_skip(self):
        """Decision G : 'ordo start' stocke 'skip' par defaut, jamais tue."""
        projet = self._projet()
        data = self._run_json(["start", str(projet), "--goal", "obj", "--json"])
        self.assertEqual(data["permissions"], "skip")

    def test_start_permissions_normal(self):
        projet = self._projet()
        data = self._run_json(
            ["start", str(projet), "--goal", "obj", "--permissions", "normal", "--json"]
        )
        self.assertEqual(data["permissions"], "normal")

    def test_start_texte_affiche_les_permissions_retenues(self):
        projet = self._projet()
        code, out, _ = self._run(["start", str(projet), "--goal", "obj"])
        self.assertEqual(code, 0)
        self.assertIn("permissions", out)

    def test_start_home_partage_leve_le_refus_de_la_decision_d(self):
        """Decision D : le drapeau --shared-home existe et leve le refus, sans le taire."""
        proja = self._projet("proja")
        projb = self._projet("projb")
        self._run(["start", str(proja), "--goal", "obj"])
        code, out, err = self._run(["start", str(projb), "--goal", "obj2"])
        self.assertNotEqual(code, 0)
        self.assertIn("home_partage", err)
        code2, out2, _ = self._run(
            ["start", str(projb), "--goal", "obj2", "--shared-home"]
        )
        self.assertEqual(code2, 0)

    def test_start_permissions_invalide_refuse_par_argparse(self):
        projet = self._projet()
        with self.assertRaises(SystemExit) as ctx:
            self._run(["start", str(projet), "--goal", "obj", "--permissions", "yolo"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_list_json_est_une_liste(self):
        self._chantier()
        self._chantier(projet=self._projet("q"), home_partage=True)
        data = self._run_json(["list", "--json"])
        self.assertEqual(len(data), 2)

    def test_list_json_ne_leake_pas_le_capteur_brut(self):
        cid = self._chantier()
        data = self._run_json(["list", "--json"])
        self.assertNotIn("capteur", data[0])
        self.assertEqual(data[0]["id"], cid)

    def test_show_json_utilise_capteur_status_filtre(self):
        cid = self._chantier()
        data = self._run_json(["show", cid, "--json"])
        # Avant adoption, capteur.status() ne rend jamais de mesure meme si des runs
        # existent (I12) : la seule forme correcte ici est adopted=False, signal=inconnu.
        self.assertEqual(data["sensor"], {"adopted": False, "signal": "unknown"})
        self.assertNotIn("lastSuccess", data["sensor"])
        self.assertEqual(data["tasks"], {})

    def test_show_chantier_absent_refuse(self):
        code, out, err = self._run(["show", "c-99"])
        self.assertNotEqual(code, 0)
        self.assertIn("c-99", err)

    def test_digest_texte_accompagne_chaque_identifiant_de_son_titre(self):
        cid = self._chantier()
        tid = self._tache(cid, titre="4.3 pipeline", why="la colonne Preselectionnes")
        with store.locked() as state:
            state["taches"][tid]["state"] = "running"
        code, out, err = self._run(["digest", cid])
        self.assertEqual(code, 0, msg=err)
        self.assertIn("4.3 pipeline", out)
        self.assertIn("la colonne Preselectionnes", out)
        # Le contrat du verbe : l'humain lit le bloc sans rien aller chercher ailleurs.
        for ligne in out.splitlines():
            if tid in ligne:
                self.assertIn("4.3 pipeline", ligne)

    def test_digest_json(self):
        cid = self._chantier()
        self._tache(cid, titre="4.3 pipeline", why="la colonne Preselectionnes")
        data = self._run_json(["digest", cid, "--json"])
        self.assertEqual(data["campaign"]["id"], cid)
        self.assertEqual([n["titre"] for n in data["next"]], ["4.3 pipeline"])

    def test_digest_chantier_absent_refuse(self):
        code, out, err = self._run(["digest", "c-99"])
        self.assertNotEqual(code, 0)
        self.assertIn("c-99", err)

    def test_close_refuse_avec_tache_running(self):
        cid = self._chantier()
        tid = self._tache(cid)
        with store.locked() as state:
            state["taches"][tid]["state"] = "running"
            state["taches"][tid]["paneId"] = "%999"
        with mock.patch.object(cli.panes, "alive", return_value=True):
            code, out, err = self._run(["close", cid])
        self.assertNotEqual(code, 0)
        self.assertIn(tid, err)

    def test_brief_json(self):
        cid = self._chantier()
        data = self._run_json(["brief", cid, "--json"])
        self.assertEqual(data["campaign"], cid)
        self.assertIn("Goal and scope", data["brief"])


# ---------------------------------------------------------------------------
# Fermeture (point E cote CLI : chantier.close() renvoie desormais un tuple
# (chantier, {"panes", "session", "archives"}) ; cmd_close fait le geste tmux lui-meme.
# Rouge tant que ordo/chantier.py (autre agent) n'a pas livre cette nouvelle signature.
# ---------------------------------------------------------------------------


class TestCloseVerb(CliTestCase):
    def test_close_force_tue_les_panes_rendus_par_close_et_le_dit(self):
        cid = self._chantier()
        tid = self._tache(cid)
        with store.locked() as state:
            state["taches"][tid]["state"] = "running"
            state["taches"][tid]["paneId"] = "%999"
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "kill") as kill, \
             mock.patch.object(cli.panes, "panes", return_value=[]), \
             mock.patch.object(cli.panes, "kill_session", create=True) as kill_session:
            code, out, _ = self._run(["close", cid, "--force"])
        self.assertEqual(code, 0)
        kill.assert_called_once_with("%999")
        kill_session.assert_called_once()
        self.assertIn("%999", out)
        self.assertIn("killed", out)

    def test_close_force_json_porte_panes_tues_et_archives(self):
        cid = self._chantier()
        tid = self._tache(cid)
        with store.locked() as state:
            state["taches"][tid]["state"] = "running"
            state["taches"][tid]["paneId"] = "%999"
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "kill"), \
             mock.patch.object(cli.panes, "panes", return_value=[]), \
             mock.patch.object(cli.panes, "kill_session", create=True):
            data = self._run_json(["close", cid, "--force", "--json"])
        self.assertEqual(data["state"], "closed")
        self.assertEqual(data["panesKilled"], ["%999"])
        self.assertIn("archives", data)

    def test_close_force_sur_panes_deja_morts_ferme_quand_meme_et_ne_ment_pas(self):
        """Trouve par une fumee reelle : la fermeture archivait puis plantait.

        Un chantier dont les panes et la session sont deja morts est le cas NORMAL d'une
        fermeture tardive. L'ancienne version remontait l'erreur tmux brute apres avoir
        deja archive, sortait en code non nul, et n'imprimait rien de ce qu'elle avait
        fait. Elle annoncait par ailleurs comme tues des panes qui ne tournaient plus.
        """
        cid = self._chantier()
        tid = self._tache(cid)
        with store.locked() as state:
            state["taches"][tid]["state"] = "running"
            state["taches"][tid]["paneId"] = "%999"
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "kill", return_value=False) as kill, \
             mock.patch.object(cli.panes, "panes", return_value=[]), \
             mock.patch.object(cli.panes, "kill_session", create=True):
            code, out, err = self._run(["close", cid, "--force"])
        self.assertEqual(code, 0, err)
        kill.assert_called_once_with("%999")
        self.assertIn("no pane killed", out)
        self.assertIn("already dead", out)
        self.assertIn("%999", out)
        self.assertEqual(store.load()["chantiers"][cid]["state"], "closed")

    def test_close_ne_tue_pas_la_session_si_dautres_panes_restent(self):
        cid = self._chantier()
        tid = self._tache(cid)
        with store.locked() as state:
            state["taches"][tid]["state"] = "running"
            state["taches"][tid]["paneId"] = "%999"
        autre_pane = {
            "pane_id": "%1000", "titre": "autre", "largeur": 130, "hauteur": 40,
            "vivant": True, "actif": True, "sousPlancher": False,
        }
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "kill"), \
             mock.patch.object(cli.panes, "panes", return_value=[autre_pane]), \
             mock.patch.object(cli.panes, "kill_session", create=True) as kill_session:
            self._run(["close", cid, "--force"])
        kill_session.assert_not_called()

    def test_close_sans_force_ne_touche_a_aucun_pane_et_le_dit(self):
        cid = self._chantier()
        with mock.patch.object(cli.panes, "kill") as kill, \
             mock.patch.object(cli.panes, "kill_session", create=True) as kill_session:
            code, out, _ = self._run(["close", cid])
        self.assertEqual(code, 0)
        kill.assert_not_called()
        kill_session.assert_not_called()
        self.assertIn("no pane killed", out)

    def test_close_ecrit_une_entree_journal_ordo(self):
        cid = self._chantier()
        self._run(["close", cid])
        from ordo import journal as journal_mod
        entries = journal_mod.read(cid)
        self.assertTrue(any(e["auteur"] == "ORDO" and "close" in e["texte"] for e in entries))


# ---------------------------------------------------------------------------
# Graphe
# ---------------------------------------------------------------------------


class TestGrapheVerbs(CliTestCase):
    def test_add_avec_depends_touches_check(self):
        cid = self._chantier()
        t1 = self._tache(cid, titre="premiere")
        data = self._run_json(
            ["add", cid, "--title", "seconde", "--prompt", "fais Y",
             "--depends", t1, "--touches", "db", "--check", "tests verts", "--json"]
        )
        self.assertEqual(data["dependsOn"], [t1])
        self.assertEqual(data["touches"], ["db"])
        self.assertEqual(
            data["checklist"],
            [{"id": "c1", "label": "tests verts", "done": False, "dureeMin": None}],
        )

    def test_add_avertit_sur_un_label_de_checklist_trop_long(self):
        cid = self._chantier()
        label = "x" * 63
        code, out, err = self._run(
            ["add", cid, "--title", "t", "--prompt", "p", "--check", label]
        )
        self.assertEqual(code, 0)
        self.assertIn("WARNING", err)
        self.assertIn("63", err)

    def test_add_check_avec_suffixe_minutes_pose_la_duree(self):
        # brief t-27 : syntaxe "label:minutes", immunisée contre tout réordonnancement
        # puisque la durée reste attachée à son propre --check plutôt que de s'apparier
        # par position avec une option séparée.
        cid = self._chantier()
        data = self._run_json(
            ["add", cid, "--title", "t", "--prompt", "p", "--check", "lire le code:20", "--json"]
        )
        self.assertEqual(
            data["checklist"],
            [{"id": "c1", "label": "lire le code", "done": False, "dureeMin": 20}],
        )

    def test_add_check_sans_suffixe_reste_sans_duree(self):
        cid = self._chantier()
        data = self._run_json(
            ["add", cid, "--title", "t", "--prompt", "p", "--check", "sans estimation", "--json"]
        )
        self.assertIsNone(data["checklist"][0]["dureeMin"])

    def test_add_check_avec_suffixe_non_numerique_reste_dans_le_libelle(self):
        # ":inconnu" n'est pas des minutes : le libellé est gardé intact, pas coupé à tort.
        cid = self._chantier()
        data = self._run_json(
            ["add", cid, "--title", "t", "--prompt", "p", "--check", "chapitre:trois", "--json"]
        )
        self.assertEqual(data["checklist"][0]["label"], "chapitre:trois")
        self.assertIsNone(data["checklist"][0]["dureeMin"])

    def test_add_plusieurs_check_avec_et_sans_duree_ne_se_meprennent_jamais(self):
        # Le piège explicitement signalé par le brief : --check répété plusieurs fois, une
        # option séparée qui s'apparierait par position serait fragile à l'ordre. Ici
        # chaque durée reste collée à SON libellé, quel que soit l'ordre des --check.
        cid = self._chantier()
        data = self._run_json(
            ["add", cid, "--title", "t", "--prompt", "p",
             "--check", "sans duree", "--check", "avec duree:12", "--check", "encore sans",
             "--json"]
        )
        self.assertEqual(
            [(i["label"], i["dureeMin"]) for i in data["checklist"]],
            [("sans duree", None), ("avec duree", 12), ("encore sans", None)],
        )

    def test_add_avertit_quand_un_critere_na_pas_de_duree(self):
        # même régime que l'avertissement des 40 caractères : jamais bloquant, juste dit.
        cid = self._chantier()
        code, out, err = self._run(
            ["add", cid, "--title", "t", "--prompt", "p", "--check", "sans estimation"]
        )
        self.assertEqual(code, 0)
        self.assertIn("WARNING", err)
        self.assertIn("c1", err)

    def test_add_najoute_aucun_avertissement_de_duree_quand_tout_est_estime(self):
        cid = self._chantier()
        code, out, err = self._run(
            ["add", cid, "--title", "t", "--prompt", "p", "--check", "estime:5"]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("duration", err.lower())
        self.assertNotIn("WARNING", out)

    def test_add_taches_le_label_trop_long_mais_ne_le_refuse_pas(self):
        cid = self._chantier()
        label = "x" * 63
        data = self._run_json(
            ["add", cid, "--title", "t", "--prompt", "p", "--check", label, "--json"]
        )
        self.assertEqual(data["checklist"][0]["label"], label)

    def test_add_json_ne_produit_aucun_avertissement(self):
        cid = self._chantier()
        label = "x" * 63
        code, out, err = self._run(
            ["add", cid, "--title", "t", "--prompt", "p", "--check", label, "--json"]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("WARNING", err)
        self.assertNotIn("WARNING", out)

    def test_add_sans_label_trop_long_ne_dit_rien(self):
        # brief t-27 : un critère sans durée déclenche désormais son propre avertissement
        # (voir test_add_avertit_quand_un_critere_na_pas_de_duree) -- celui-ci ne prouve
        # plus l'absence de TOUT avertissement, seulement l'absence du warning "40-char".
        cid = self._chantier()
        code, out, err = self._run(
            ["add", cid, "--title", "t", "--prompt", "p", "--check", "ok"]
        )
        self.assertEqual(code, 0)
        self.assertNotIn("char", err)

    def test_dep_refuse_sur_cycle(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        t2 = self._tache(cid, depends_on=[t1])
        code, out, err = self._run(["dep", t1, t2])
        self.assertNotEqual(code, 0)
        self.assertIn("cycle", err.lower())

    def test_graph_json_structure(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        data = self._run_json(["graph", cid, "--json"])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], t1)
        self.assertIn("dependsOn", data[0])

    def test_graph_texte(self):
        cid = self._chantier()
        self._tache(cid, titre="ma tache")
        code, out, _ = self._run(["graph", cid])
        self.assertEqual(code, 0)
        self.assertIn("ma tache", out)

    def test_ready_sans_why(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        t2 = self._tache(cid, depends_on=[t1])
        data = self._run_json(["ready", cid, "--json"])
        self.assertEqual([t["id"] for t in data], [t1])

    def test_ready_why_explique_dependance_inexistante(self):
        """Raffinement C-9 : une dependance vers un identifiant inexistant doit se dire.

        add_task() valide l'existence de ses dependances a la creation ; pour simuler
        une reference pendante (donnee corrompue, tache purgee ailleurs) il faut la
        forcer directement dans l'etat, hors API normale.
        """
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["dependsOn"] = ["t-99"]
        data = self._run_json(["ready", cid, "--why", "--json"])
        self.assertEqual(data["ready"], [])
        self.assertEqual(len(data["notReady"]), 1)
        raisons = data["notReady"][0]["reasons"]
        self.assertTrue(any("t-99" in r and "not found" in r for r in raisons))

    def test_ready_why_explique_checklist_incomplete(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        t2 = self._tache(cid, depends_on=[t1])
        with store.locked() as state:
            state["taches"][t1]["state"] = "done"
            # checklist volontairement laissee non cochee : I1, la tache n'est prete
            # que si la checklist de SA dependance est cochee.
        data = self._run_json(["ready", cid, "--why", "--json"])
        self.assertEqual(data["ready"], [])
        raison = data["notReady"][0]["reasons"][0]
        self.assertIn("checklist incomplete", raison)

    def test_cancel(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        data = self._run_json(["cancel", t1, "--json"])
        self.assertEqual(data["state"], "cancelled")

    def test_cancel_texte_dit_que_le_pane_continue_de_tourner(self):
        """Decision J : 'annulee' ne doit jamais laisser croire qu'un process s'arrete."""
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%7"
        code, out, _ = self._run(["cancel", t1])
        self.assertEqual(code, 0)
        self.assertIn("still running", out)
        self.assertIn("%7", out)
        self.assertIn(f"ordo kill {t1}", out)

    def test_cancel_sans_pane_ne_mentionne_pas_de_pane(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        code, out, _ = self._run(["cancel", t1])
        self.assertEqual(code, 0)
        self.assertNotIn("still running", out)

    def test_priority(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        data = self._run_json(["priority", t1, "5", "--json"])
        self.assertEqual(data["priority"], 5)

    def test_amend(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        data = self._run_json(["amend", t1, "--prompt", "nouveau texte", "--json"])
        self.assertEqual(data["prompt"], "nouveau texte")

    def test_amend_refuse_si_running(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["state"] = "running"
        code, out, err = self._run(["amend", t1, "--prompt", "x"])
        self.assertNotEqual(code, 0)

    def test_check_et_undo(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        data = self._run_json(["check", t1, "c1", "--json"])
        self.assertTrue(data["checklist"][0]["done"])
        data = self._run_json(["check", t1, "c1", "--undo", "--json"])
        self.assertFalse(data["checklist"][0]["done"])

    def test_check_doing_declare_l_item_attaque_sans_le_cocher(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts", "doc a jour"])
        data = self._run_json(["check", t1, "c1", "--doing", "--json"])
        self.assertEqual(data["currentItem"], "c1")
        self.assertFalse(data["checklist"][0]["done"])

    def test_check_libere_currentItem_quand_l_item_declare_est_coche(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        self._run_json(["check", t1, "c1", "--doing", "--json"])
        data = self._run_json(["check", t1, "c1", "--json"])
        self.assertIsNone(data["currentItem"])
        self.assertTrue(data["checklist"][0]["done"])

    def test_check_d_un_autre_item_ne_libere_pas_currentItem(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts", "doc a jour"])
        self._run_json(["check", t1, "c1", "--doing", "--json"])
        data = self._run_json(["check", t1, "c2", "--json"])
        self.assertEqual(data["currentItem"], "c1")

    def test_check_doing_item_inconnu_refuse(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        code, out, err = self._run(["check", t1, "c-inconnu", "--doing"])
        self.assertNotEqual(code, 0)

    def test_check_doing_rappelle_de_poser_une_duree_quand_il_nen_a_pas(self):
        # brief t-27, c4/c9 : la révision est DÉDUCTIBLE de --doing, déjà obligatoire,
        # jamais un geste séparé qu'on peut ne jamais lancer.
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        code, out, err = self._run(["check", t1, "c1", "--doing"])
        self.assertEqual(code, 0)
        self.assertIn("no duration estimate", err)
        self.assertIn(f"ordo checklist duree {t1} c1", err)

    def test_check_doing_rappelle_de_revalider_une_duree_deja_posee(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        chantier.set_checklist_duree(t1, "c1", 15)
        code, out, err = self._run(["check", t1, "c1", "--doing"])
        self.assertEqual(code, 0)
        self.assertIn("15", err)
        self.assertIn(f"ordo checklist duree {t1} c1", err)

    def test_check_doing_json_ne_porte_aucun_rappel(self):
        # le contrat --json reste stable : le rappel est un confort humain, pas un champ.
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        code, out, err = self._run(["check", t1, "c1", "--doing", "--json"])
        self.assertEqual(err, "")

    def test_check_doing_journalise_un_evenement_date(self):
        # Source de vérité de chantier.duree_mesuree_par_critere (brief t-36) : chaque
        # --doing doit être daté dans le journal machine, en direct, pas seulement dans
        # task["currentItem"] qui ne garde aucune trace une fois l'item coché.
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        self._run(["check", t1, "c1", "--doing"])
        evenements = journal.lire_evenements(cid, "checklist-doing")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], t1)
        self.assertEqual(evenements[0]["item"], "c1")
        self.assertIn("at", evenements[0])

    def test_check_journalise_un_evenement_date(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        self._run(["check", t1, "c1", "--doing"])
        self._run(["check", t1, "c1"])
        evenements = journal.lire_evenements(cid, "checklist-coche")
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["tache"], t1)
        self.assertEqual(evenements[0]["item"], "c1")

    def test_check_repete_sur_un_item_deja_coche_ne_double_journalise_pas(self):
        # chantier.check() est idempotent (recocher un item déjà coché "n'a aucun effet",
        # voir le protocole de rapport) : un second `ordo check` sur le même item ne doit
        # PAS écrire un second "checklist-coche", qui écraserait la durée déjà mesurée par
        # l'écart quasi nul entre les deux coches.
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        self._run(["check", t1, "c1"])
        self._run(["check", t1, "c1"])
        evenements = journal.lire_evenements(cid, "checklist-coche")
        self.assertEqual(len(evenements), 1)

    def test_check_undo_ne_journalise_rien(self):
        # Décocher n'est jamais une fin de critère : le journaliser fausserait la mesure
        # (une prochaine coche du même item semblerait n'avoir pris aucun temps).
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        self._run(["check", t1, "c1"])
        self._run(["check", t1, "c1", "--undo"])
        self.assertEqual(journal.lire_evenements(cid), [
            e for e in journal.lire_evenements(cid) if e["type"] != "checklist-decoche"
        ])
        types = [e["type"] for e in journal.lire_evenements(cid)]
        self.assertEqual(types.count("checklist-coche"), 1)

    def test_check_deduit_le_suivant_journalise_aussi_son_doing(self):
        # La déclaration automatique de l'item suivant (déduite après une coche réelle,
        # voir cmd_check) est elle aussi une déclaration --doing en bonne et due forme :
        # sans son propre événement, la durée du deuxième critère resterait invisible.
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier", "second"])
        with store.locked() as state:
            state["taches"][t1]["state"] = "running"
        self._run(["check", t1, "c1", "--doing"])
        self._run(["check", t1, "c1"])
        types_et_items = [
            (e["type"], e["item"]) for e in journal.lire_evenements(cid)
            if e["type"] in ("checklist-doing", "checklist-coche")
        ]
        self.assertIn(("checklist-coche", "c1"), types_et_items)
        self.assertIn(("checklist-doing", "c2"), types_et_items)

    def test_cancel_libere_currentItem(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        self._run_json(["check", t1, "c1", "--doing", "--json"])
        data = self._run_json(["cancel", t1, "--json"])
        self.assertIsNone(data["currentItem"])

    def test_brief_explique_comment_declarer_l_item_en_cours(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        from ordo import prompt as prompt_mod
        brief_path = prompt_mod.brief_executante(t1)
        contenu = Path(brief_path).read_text(encoding="utf-8")
        self.assertIn("--doing", contenu)
        # La commande de déclaration est écrite item par item, concrète et copiable, et
        # non sous une forme générique à compléter : une exécutante copie la commande
        # prête et ignore celle où il reste un <item-id> à substituer, ce qui laissait
        # currentItem vide sur toutes les tâches en cours.
        self.assertIn(f"ordo check {t1} c1 --doing", contenu)
        self.assertIn(f"ordo check {t1} c1\n", contenu)

    def test_carte_model_expose_currentItem(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["tests verts"])
        self._run_json(["check", t1, "c1", "--doing", "--json"])
        from ordo import carte as carte_mod
        node = carte_mod.model(cid)["nodes"][t1]
        self.assertEqual(node["currentItem"], "c1")


# ---------------------------------------------------------------------------
# Checklist : ajouter, découper, reformuler -- jamais supprimer (brief t-22)
# ---------------------------------------------------------------------------


class TestChecklistVerbs(CliTestCase):
    def test_add_ajoute_un_critere_a_lid_frais(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        data = self._run_json(["checklist", "add", t1, "second critère", "--json"])
        self.assertEqual(
            data["checklist"][1],
            {"id": "c2", "label": "second critère", "done": False, "dureeMin": None},
        )

    def test_add_journalise_sous_lauteur_par_defaut_ordo(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        self._run_json(["checklist", "add", t1, "second critère", "--json"])
        entries = self._run_json(["journal", "show", cid, "--json"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["auteur"], "ORDO")
        self.assertIn("c2", entries[0]["texte"])

    def test_add_journalise_sous_lauteur_demande(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        self._run_json(
            ["checklist", "add", t1, "second critère", "--author", "ORCH", "--json"]
        )
        entries = self._run_json(["journal", "show", cid, "--json"])
        self.assertEqual(entries[0]["auteur"], "ORCH")

    def test_add_refuse_auteur_invalide(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        with self.assertRaises(SystemExit) as ctx:
            self._run(["checklist", "add", t1, "x", "--author", "AUTRE"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_add_avertit_au_dela_de_la_convention_40_caracteres(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        long_label = "x" * (chantier.CHECKLIST_LABEL_MAX + 1)
        code, out, err = self._run(["checklist", "add", t1, long_label])
        self.assertEqual(code, 0)
        self.assertIn(f"{chantier.CHECKLIST_LABEL_MAX}-char", err)

    def test_add_sous_la_convention_ne_previent_pas(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        code, out, err = self._run(["checklist", "add", t1, "court"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_split_garde_lid_original_et_cree_un_id_frais(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["fait deux choses à la fois"])
        data = self._run_json(
            ["checklist", "split", t1, "c1", "première moitié", "seconde moitié", "--json"]
        )
        self.assertEqual(
            data["checklist"],
            [
                {"id": "c1", "label": "première moitié", "done": False, "dureeMin": None},
                {"id": "c2", "label": "seconde moitié", "done": False, "dureeMin": None},
            ],
        )

    def test_split_journalise_sous_lauteur_par_defaut_ordo(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["à découper"])
        self._run_json(["checklist", "split", t1, "c1", "a", "b", "--json"])
        entries = self._run_json(["journal", "show", cid, "--json"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["auteur"], "ORDO")
        self.assertIn("c1", entries[0]["texte"])
        self.assertIn("c2", entries[0]["texte"])

    def test_split_item_inconnu_refuse(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["a"])
        code, out, err = self._run(["checklist", "split", t1, "c-inconnu", "a", "b"])
        self.assertNotEqual(code, 0)

    def test_reword_change_le_libelle_sans_toucher_lid(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["libellé faux"])
        data = self._run_json(
            ["checklist", "reword", t1, "c1", "libellé corrigé", "--json"]
        )
        self.assertEqual(
            data["checklist"][0],
            {"id": "c1", "label": "libellé corrigé", "done": False, "dureeMin": None},
        )

    def test_reword_ne_journalise_rien(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["libellé faux"])
        self._run_json(["checklist", "reword", t1, "c1", "libellé corrigé", "--json"])
        entries = self._run_json(["journal", "show", cid, "--json"])
        self.assertEqual(entries, [])

    def test_checklist_sans_action_affiche_aide(self):
        code, out, err = self._run(["checklist"])
        self.assertEqual(code, 0)
        self.assertIn("add", out)
        self.assertIn("split", out)
        self.assertIn("reword", out)
        self.assertIn("duree", out)

    def test_duree_pose_lestimation_en_minutes(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        data = self._run_json(["checklist", "duree", t1, "c1", "20", "--json"])
        self.assertEqual(data["checklist"][0]["dureeMin"], 20)

    def test_duree_corrige_une_estimation_deja_posee(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        self._run_json(["checklist", "duree", t1, "c1", "20", "--json"])
        data = self._run_json(["checklist", "duree", t1, "c1", "8", "--json"])
        self.assertEqual(data["checklist"][0]["dureeMin"], 8)

    def test_duree_ne_touche_ni_au_libelle_ni_a_letat(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        chantier.check(t1, "c1")
        data = self._run_json(["checklist", "duree", t1, "c1", "20", "--json"])
        self.assertEqual(data["checklist"][0]["label"], "premier")
        self.assertTrue(data["checklist"][0]["done"])

    def test_duree_item_inconnu_refuse(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        code, out, err = self._run(["checklist", "duree", t1, "c-inconnu", "20"])
        self.assertNotEqual(code, 0)

    def test_duree_valeur_non_positive_refuse(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        code, out, err = self._run(["checklist", "duree", t1, "c1", "0"])
        self.assertNotEqual(code, 0)

    def test_aucun_verbe_de_suppression_nexiste(self):
        # Prouve le refus (brief t-22), ne se contente pas de son absence de l'aide :
        # argparse rejette la sous-commande avant meme d'atteindre un dispatch quelconque.
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["a"])
        for candidat in ("remove", "delete", "drop", "rm"):
            with self.assertRaises(SystemExit) as ctx:
                self._run(["checklist", candidat, t1, "c1"])
            self.assertNotEqual(ctx.exception.code, 0)

    def test_brief_explique_les_gestes_ouverts_et_le_refus_de_suppression(self):
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        from ordo import prompt as prompt_mod
        brief_path = prompt_mod.brief_executante(t1)
        contenu = Path(brief_path).read_text(encoding="utf-8")
        self.assertIn("ordo checklist add", contenu)
        self.assertIn("ordo checklist split", contenu)
        self.assertIn("ordo checklist reword", contenu)
        self.assertIn("ordo checklist duree", contenu)
        self.assertIn("cannot", contenu.lower())

    def test_brief_demande_la_revision_de_chaque_estimation_pas_seulement_ne_lautorise(self):
        # brief t-27, c9 : le geste doit être DEMANDÉ item par item, pas seulement rendu
        # possible dans un paragraphe général -- même raison que --doing, ignoré trois fois
        # sur trois quand documenté loin de l'action.
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier", "second"])
        chantier.set_checklist_duree(t1, "c1", 15)
        from ordo import prompt as prompt_mod
        brief_path = prompt_mod.brief_executante(t1)
        contenu = Path(brief_path).read_text(encoding="utf-8")
        # c1 porte déjà une estimation : le brief demande de la revérifier.
        self.assertIn("estimate: 15", contenu)
        self.assertIn(f"ordo checklist duree {t1} c1", contenu)
        # c2 n'en porte aucune : le brief demande d'en poser une.
        self.assertIn("no duration estimate yet", contenu)
        self.assertIn(f"ordo checklist duree {t1} c2", contenu)

    def test_la_demande_de_revision_precede_la_commande_doing_du_meme_item(self):
        # déductible plutôt que volontaire (brief t-27) : la ligne de révision vit AVANT
        # --doing, au moment où l'exécutante lit ce critère pour la première fois, jamais
        # après, ni dans une section à part qu'on peut sauter.
        cid = self._chantier()
        t1 = self._tache(cid, checklist=["premier"])
        from ordo import prompt as prompt_mod
        brief_path = prompt_mod.brief_executante(t1)
        contenu = Path(brief_path).read_text(encoding="utf-8")
        avant = contenu.index("no duration estimate yet")
        apres = contenu.index(f"ordo check {t1} c1 --doing")
        self.assertLess(avant, apres)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class TestPlanVerbs(CliTestCase):
    def test_plan_refuse_pave_vide(self):
        cid = self._chantier()
        with mock.patch("sys.stdin", io.StringIO("")):
            code, out, err = self._run(["plan", cid])
        self.assertNotEqual(code, 0)
        self.assertIn("empty", err)

    def test_plan_refuse_chantier_absent_sans_toucher_au_modele(self):
        # propose() verifie l'existence du chantier AVANT d'appeler le runner modele :
        # aucun processus claude n'est jamais lance ici.
        with mock.patch("sys.stdin", io.StringIO("un pavé quelconque")):
            code, out, err = self._run(["plan", "c-99"])
        self.assertNotEqual(code, 0)
        self.assertIn("c-99", err)

    def _proposition(self, cid: str, checklist: list[str] | None = None) -> str:
        with store.locked() as state:
            pid = store.next_id(state, "proposition")
            state["propositions"][pid] = {
                "id": pid, "chantier": cid,
                "taches": [{"ref": "n1", "titre": "t", "prompt": "p",
                            "dependsOn": [], "touches": [],
                            "checklist": checklist if checklist is not None else []}],
                "state": "pending",
                "proposedAt": store.now(), "deadline": "2099-01-01T00:00:00Z",
                "decidedAt": None, "refus": None,
            }
        return pid

    def test_propositions_liste_en_attente_par_defaut(self):
        cid = self._chantier()
        pid = self._proposition(cid)
        data = self._run_json(["proposals", "--campaign", cid, "--json"])
        self.assertEqual([p["id"] for p in data], [pid])

    def test_accept_materialise(self):
        cid = self._chantier()
        pid = self._proposition(cid)
        data = self._run_json(["accept", pid, "--json"])
        self.assertEqual(data["state"], "accepted")
        state = store.load()
        self.assertEqual(len(state["taches"]), 1)

    def test_refuse(self):
        cid = self._chantier()
        pid = self._proposition(cid)
        data = self._run_json(["reject", pid, "--reason", "hors scope", "--json"])
        self.assertEqual(data["state"], "rejected")
        self.assertEqual(data["refus"], "hors scope")

    def test_accept_avertit_sur_un_label_de_checklist_trop_long(self):
        cid = self._chantier()
        pid = self._proposition(cid, checklist=["x" * 63])
        code, out, err = self._run(["accept", pid])
        self.assertEqual(code, 0)
        self.assertIn("WARNING", err)
        self.assertIn("63", err)
        self.assertNotIn("WARNING", out)

    def test_accept_json_ne_produit_aucun_avertissement(self):
        cid = self._chantier()
        pid = self._proposition(cid, checklist=["x" * 63])
        code, out, err = self._run(["accept", pid, "--json"])
        self.assertEqual(code, 0)
        self.assertNotIn("WARNING", err)
        self.assertNotIn("WARNING", out)

    def test_accept_sans_label_trop_long_ne_dit_rien(self):
        cid = self._chantier()
        pid = self._proposition(cid, checklist=["ok"])
        code, out, err = self._run(["accept", pid])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")


# ---------------------------------------------------------------------------
# Execution (panes mockes : aucun test ne touche un vrai tmux ni un vrai claude)
# ---------------------------------------------------------------------------


class TestExecutionVerbs(CliTestCase):
    def _mock_panes(self, etat=None):
        etat = etat or cli.panes.PANE_ETAT_PRET
        return (
            # Decision B : ensure_session() renvoie desormais (session, window_id).
            mock.patch.object(cli.panes, "ensure_session", return_value=("ordo-p", "@1")),
            mock.patch.object(cli.panes, "spawn", return_value="%42"),
            mock.patch.object(cli.panes, "relayout", return_value=None),
            mock.patch.object(cli.panes, "wait_ready", return_value=etat),
            mock.patch.object(cli.panes, "send", return_value=None),
        )

    def test_launch_refuse_si_pas_prete(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        t2 = self._tache(cid, depends_on=[t1])
        code, out, err = self._run(["launch", t2])
        self.assertNotEqual(code, 0)
        self.assertIn("not ready", err)

    def test_launch_succes_construit_la_commande_et_mute_la_tache(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1 as ensure_session, m2 as spawn, m3 as relayout, m4, m5 as send:
            data = self._run_json(["launch", t1, "--json"])
        self.assertEqual(data["paneId"], "%42")
        self.assertEqual(data["session"], "ordo-p")
        self.assertEqual(data["window"], "@1")
        self.assertEqual(data["permissions"], "skip")
        self.assertIn("tmux attach -t ordo-p", data["attach"])
        self.assertTrue(Path(data["brief"]).exists())

        state = store.load()
        task = state["taches"][t1]
        self.assertEqual(task["state"], "running")
        self.assertEqual(task["paneId"], "%42")
        self.assertEqual(task["attempts"], 1)
        self.assertIsNotNone(task["startedAt"])

        # send() a recu la consigne de lecture du brief, jamais le prompt lui-meme (I4).
        send.assert_called_once()
        pane_arg, texte_arg = send.call_args[0]
        self.assertEqual(pane_arg, "%42")
        self.assertTrue(texte_arg.startswith("Read "))
        self.assertIn("apply it", texte_arg)

        # Le titre du pane porte l'identifiant ET le titre de la tache, pas l'un sans
        # l'autre : c'est le point explicite de la demande d'ergonomie (t-XX <titre>).
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.kwargs["title"], f"{t1} titre")
        # La fenetre est renommee au slug du chantier, pas laissee au nom par defaut de tmux.
        ensure_session.assert_called_once()
        self.assertEqual(ensure_session.call_args.kwargs.get("label"), "p")

        # Decision B : le window_id revele par ensure_session() est persiste sur le
        # chantier, seul endroit (cli.py) qui a le droit de toucher panes.py.
        ch_state = state["chantiers"][cid]
        self.assertEqual(ch_state.get("tmuxWindow"), "@1")

    def _cmd_de_spawn(self, spawn) -> str:
        """La commande shell reellement passee a tmux par le dernier spawn()."""
        appel = spawn.call_args
        if appel.args:
            for a in appel.args:
                if isinstance(a, str) and "claude" in a:
                    return a
        for v in appel.kwargs.values():
            if isinstance(v, str) and "claude" in v:
                return v
        raise AssertionError(f"aucune commande claude dans spawn{appel}")

    def test_launch_sans_model_route_la_tache_et_passe_le_modele_a_claude(self):
        """Le defaut n'est plus "rien imposer" : sans --model, le modele se deduit."""
        cid = self._chantier()
        t1 = self._tache(
            cid,
            titre="CTRL-DOC 01 Les decisions figees",
            prompt="Verifier docs/03-DECISIONS.md contre index.js.",
            checklist=["a", "b", "c"],
        )
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2 as spawn, m3, m4, m5:
            code, out, _ = self._run(["launch", t1])
        self.assertEqual(code, 0)
        self.assertIn("--model sonnet", self._cmd_de_spawn(spawn))
        # Le motif sort avec le modele : un choix invisible ne se conteste pas.
        self.assertIn("sonnet", out)
        self.assertIn("perimetre nomme", out)

    def test_launch_avec_model_explicite_prime_sur_le_routage(self):
        cid = self._chantier()
        t1 = self._tache(cid, titre="Concevoir le schema", checklist=["a"])
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2 as spawn, m3, m4, m5:
            code, _, _ = self._run(["launch", t1, "--model", "haiku"])
        self.assertEqual(code, 0)
        self.assertIn("--model haiku", self._cmd_de_spawn(spawn))

    def test_launch_model_herite_n_impose_aucun_modele(self):
        """Porte de sortie : la commande d'avant le routage, joignable en un mot."""
        cid = self._chantier()
        t1 = self._tache(cid, titre="CTRL-DOC 01", prompt="Voir a.py.", checklist=["a"])
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2 as spawn, m3, m4, m5:
            code, _, _ = self._run(["launch", t1, "--model", "herite"])
        self.assertEqual(code, 0)
        self.assertNotIn("--model", self._cmd_de_spawn(spawn))

    def test_relaunch_escalade_le_modele_apres_une_tentative(self):
        """Relancer a l'identique referait le meme echec : la seconde tentative monte."""
        cid = self._chantier()
        t1 = self._tache(
            cid,
            titre="CTRL-DOC 01 Les decisions figees",
            prompt="Verifier docs/03-DECISIONS.md contre index.js.",
            checklist=["a", "b", "c"],
        )
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2 as spawn, m3, m4, m5:
            self._run(["launch", t1])
            self.assertIn("--model sonnet", self._cmd_de_spawn(spawn))
            with store.locked() as state:
                state["taches"][t1]["state"] = "blocked"
            code, out, _ = self._run(["relaunch", t1])
        self.assertEqual(code, 0)
        self.assertIn("--model opus", self._cmd_de_spawn(spawn))
        self.assertIn("tentative", out.lower())

    def test_launch_json_contient_toutes_les_cles_du_contrat(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2, m3, m4, m5:
            data = self._run_json(["launch", t1, "--json"])
        for cle in ("task", "paneId", "brief", "session", "window", "attach", "permissions"):
            self.assertIn(cle, data)

    def test_launch_texte_format_exact(self):
        """Decision I : reproduit le format humain impose par le contrat de publication."""
        cid = self._chantier()
        t1 = self._tache(cid, titre="migration du schema")
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2, m3, m4, m5:
            code, out, _ = self._run(["launch", t1])
        self.assertEqual(code, 0)
        lignes = out.splitlines()
        self.assertEqual(
            lignes[0], f"{t1}  launched, real Claude Code session in a tmux pane"
        )
        self.assertEqual(lignes[1], f"  {'session':<12}ordo-p")
        self.assertEqual(lignes[2], f"  {'watch':<12}tmux attach -t ordo-p")
        self.assertEqual(
            lignes[3], f"  {'pane':<12}%42   (title: {t1} migration du schema)"
        )
        self.assertTrue(lignes[4].startswith(f"  {'brief':<12}"))
        self.assertEqual(
            lignes[5], f"  {'permissions':<12}skip (--dangerously-skip-permissions)"
        )

    def test_relaunch_texte_dit_relancee(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["state"] = "blocked"
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2, m3, m4, m5:
            code, out, _ = self._run(["relaunch", t1])
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[0].startswith(f"{t1}  relaunched,"))

    def test_launch_attend_que_le_tui_soit_pret_avant_dinjecter(self):
        """Defaut 1 : launch() ne peut pas envoyer sans etre passe par wait_ready()."""
        cid = self._chantier()
        t1 = self._tache(cid)
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2, m3, m4 as wait_ready, m5 as send:
            self._run(["launch", t1, "--json"])
        wait_ready.assert_called_once_with("%42")
        send.assert_called_once()

    def test_launch_timeout_du_tui_ne_touche_pas_au_pane_et_remonte_erreur(self):
        """Defaut 1 : un depassement de delai remonte une erreur claire, rien n'est envoye."""
        cid = self._chantier()
        t1 = self._tache(cid)
        with mock.patch.object(cli.panes, "ensure_session", return_value=("ordo-p", "@1")), \
             mock.patch.object(cli.panes, "spawn", return_value="%42"), \
             mock.patch.object(cli.panes, "relayout", return_value=None), \
             mock.patch.object(
                 cli.panes, "wait_ready",
                 side_effect=RuntimeError("le TUI de claude n'est jamais apparu prêt"),
             ), \
             mock.patch.object(cli.panes, "send", return_value=None) as send:
            code, out, err = self._run(["launch", t1])
        self.assertNotEqual(code, 0)
        self.assertIn("jamais apparu prêt", err)
        send.assert_not_called()
        # La tache reste dans son etat d'avant tentative : rien n'est ecrit en aveugle
        # sur un pane dont on ne sait pas ce qu'il affiche.
        self.assertEqual(store.load()["taches"][t1]["state"], "queued")

    def test_launch_dialogue_de_confiance_ninjecte_rien_bloque_et_sort_non_zero(self):
        """Defaut 2 : Ordo n'approuve jamais un dossier automatiquement, il escalade."""
        cid = self._chantier()
        t1 = self._tache(cid)
        with mock.patch.object(cli.panes, "ensure_session", return_value=("ordo-s", "@2")), \
             mock.patch.object(cli.panes, "spawn", return_value="%42"), \
             mock.patch.object(cli.panes, "relayout", return_value=None), \
             mock.patch.object(
                 cli.panes, "wait_ready", return_value=cli.panes.PANE_ETAT_CONFIANCE
             ), \
             mock.patch.object(cli.panes, "send", return_value=None) as send:
            code, out, err = self._run(["launch", t1])

        self.assertNotEqual(code, 0)
        self.assertIn(t1, err)
        # Ni Entree, ni Escape, ni la moindre touche : le pane n'est jamais touche.
        send.assert_not_called()

        task = store.load()["taches"][t1]
        self.assertEqual(task["state"], "blocked")
        self.assertEqual(task["paneId"], "%42")
        self.assertIn("trust", task["error"])
        self.assertIn("%42", task["error"])

    def test_launch_bloque_par_confiance_donne_la_commande_dattache_sur_stderr(self):
        """Ce que l'humain voit AU MOMENT du blocage doit suffire a agir.

        Le message long existait deja, mais il n'etait ecrit que dans state.json et ne
        ressortait qu'au tick suivant ; stderr ne portait ni la session ni la commande.
        Un humain qui lance et voit l'echec restait sans rien pour aller regarder.
        """
        cid = self._chantier()
        t1 = self._tache(cid)
        with mock.patch.object(cli.panes, "ensure_session", return_value=("ordo-s", "@2")), \
             mock.patch.object(cli.panes, "spawn", return_value="%42"), \
             mock.patch.object(cli.panes, "relayout", return_value=None), \
             mock.patch.object(
                 cli.panes, "wait_ready", return_value=cli.panes.PANE_ETAT_CONFIANCE
             ), \
             mock.patch.object(cli.panes, "send", return_value=None):
            code, out, err = self._run(["launch", t1])

        self.assertNotEqual(code, 0)
        self.assertIn("ordo-s", err)
        self.assertIn("tmux attach -t ordo-s", err)
        self.assertIn("%42", err)
        self.assertIn("Yes, I trust this folder", err)

    def test_launch_commande_ne_porte_jamais_le_prompt(self):
        cid = self._chantier()
        t1 = self._tache(cid, prompt="SECRET NE DOIT PAS FUITER DANS LA COMMANDE")
        with mock.patch.object(cli.panes, "ensure_session", return_value=("ordo-p", "@1")), \
             mock.patch.object(cli.panes, "spawn", return_value="%42") as spawn, \
             mock.patch.object(cli.panes, "relayout", return_value=None), \
             mock.patch.object(cli.panes, "wait_ready", return_value=cli.panes.PANE_ETAT_PRET), \
             mock.patch.object(cli.panes, "send", return_value=None):
            self._run(["launch", t1])
        cmd = spawn.call_args[0][2]
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertIn("< /dev/null", cmd)
        self.assertNotIn("SECRET", cmd)
        # L'identifiant de session est impose au lancement, jamais devine apres coup :
        # c'est lui qui rend la reprise possible une fois le pane moissonne.
        self.assertRegex(
            cmd, r"--session-id [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        )
        sid = store.load()["taches"][t1]["claudeSessionId"]
        self.assertIn(f"--session-id {sid}", cmd, "l'uuid lance et l'uuid stocke doivent coincider")

    def test_launch_permissions_normal_najoute_rien_a_la_commande(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2 as spawn, m3, m4, m5:
            data = self._run_json(["launch", t1, "--permissions", "normal", "--json"])
        cmd = spawn.call_args[0][2]
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertIn("claude", cmd)
        self.assertEqual(data["permissions"], "normal")

    def test_relaunch_permissions_surcharge_celles_du_chantier(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["state"] = "blocked"
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2 as spawn, m3, m4, m5:
            data = self._run_json(["relaunch", t1, "--permissions", "normal", "--json"])
        cmd = spawn.call_args[0][2]
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(data["permissions"], "normal")

    def test_launch_permissions_invalide_refuse_par_argparse(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with self.assertRaises(SystemExit) as ctx:
            self._run(["launch", t1, "--permissions", "yolo"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_launch_attach_prefixe_env_u_tmux_si_deja_dans_tmux(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        m1, m2, m3, m4, m5 = self._mock_panes()
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1234,0"}):
            with m1, m2, m3, m4, m5:
                data = self._run_json(["launch", t1, "--json"])
        self.assertTrue(data["attach"].startswith("env -u TMUX "))

    def test_launch_attach_sans_prefixe_hors_tmux(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        m1, m2, m3, m4, m5 = self._mock_panes()
        env_sans_tmux = {k: v for k, v in os.environ.items() if k != "TMUX"}
        with mock.patch.dict(os.environ, env_sans_tmux, clear=True):
            with m1, m2, m3, m4, m5:
                data = self._run_json(["launch", t1, "--json"])
        self.assertFalse(data["attach"].startswith("env -u TMUX"))

    def test_launch_refuse_si_claude_absent_du_path(self):
        """Point L : le CLI verifie claude AVANT tout appel tmux (aucune session cree)."""
        cid = self._chantier()
        t1 = self._tache(cid)
        with mock.patch.object(cli.shutil, "which", return_value=None), \
             mock.patch.object(cli.panes, "ensure_session") as ensure_session:
            code, out, err = self._run(["launch", t1])
        self.assertNotEqual(code, 0)
        self.assertIn("claude", err)
        ensure_session.assert_not_called()

    def test_launch_ecrit_une_entree_journal_ordo(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        m1, m2, m3, m4, m5 = self._mock_panes()
        with m1, m2, m3, m4, m5:
            self._run(["launch", t1, "--json"])
        from ordo import journal as journal_mod
        entries = journal_mod.read(cid)
        self.assertTrue(
            any(e["auteur"] == "ORDO" and t1 in e["texte"] and "%42" in e["texte"]
                for e in entries)
        )

    def test_launch_refuse_deux_fois_de_suite_sans_force(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with mock.patch.object(cli.panes, "ensure_session", return_value=("ordo-p", "@1")), \
             mock.patch.object(cli.panes, "spawn", return_value="%42"), \
             mock.patch.object(cli.panes, "relayout", return_value=None), \
             mock.patch.object(cli.panes, "wait_ready", return_value=cli.panes.PANE_ETAT_PRET), \
             mock.patch.object(cli.panes, "send", return_value=None):
            code1, _, _ = self._run(["launch", t1, "--json"])
        self.assertEqual(code1, 0)
        code2, out2, err2 = self._run(["launch", t1])
        self.assertNotEqual(code2, 0)
        self.assertIn("running", err2)

    def test_relaunch_refuse_sur_tache_queued(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        code, out, err = self._run(["relaunch", t1])
        self.assertNotEqual(code, 0)

    def test_relaunch_reussit_sur_blocked(self):
        cid = self._chantier()
        t1 = self._tache(cid, titre="migration du schema")
        with store.locked() as state:
            state["taches"][t1]["state"] = "blocked"
            state["taches"][t1]["error"] = "rapport illisible"
        with mock.patch.object(cli.panes, "ensure_session", return_value=("ordo-p", "@1")), \
             mock.patch.object(cli.panes, "spawn", return_value="%43") as spawn, \
             mock.patch.object(cli.panes, "relayout", return_value=None), \
             mock.patch.object(cli.panes, "wait_ready", return_value=cli.panes.PANE_ETAT_PRET), \
             mock.patch.object(cli.panes, "send", return_value=None):
            data = self._run_json(["relaunch", t1, "--json"])
        self.assertEqual(data["paneId"], "%43")
        state = store.load()
        self.assertEqual(state["taches"][t1]["state"], "running")
        self.assertEqual(state["taches"][t1]["attempts"], 1)
        # Une relance reaffecte un pane (celui de la tentative precedente est mort) :
        # le titre doit refleter la tache courante, jamais rester sur un texte perime.
        self.assertEqual(spawn.call_args.kwargs["title"], f"{t1} migration du schema")

    def test_relaunch_efface_la_marque_de_blocage_par_propagation_perimee(self):
        # Une tache bloquee par propagation (blockedCause="propagation") peut etre
        # relancee directement (--force ou apres correction manuelle). La relance est un
        # tour frais, jamais une propagation : si la marque restait, un futur blocage
        # pour raison propre (ex. pane mort) serait a tort relu comme une propagation et
        # relance en boucle par chantier.unblock_propagated() au tick suivant.
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["state"] = "blocked"
            state["taches"][t1]["error"] = "dependance t-00 morte (blocked)"
            state["taches"][t1]["blockedCause"] = chantier.BLOCKED_CAUSE_PROPAGATION
        with mock.patch.object(cli.panes, "ensure_session", return_value=("ordo-p", "@1")), \
             mock.patch.object(cli.panes, "spawn", return_value="%43"), \
             mock.patch.object(cli.panes, "relayout", return_value=None), \
             mock.patch.object(cli.panes, "wait_ready", return_value=cli.panes.PANE_ETAT_PRET), \
             mock.patch.object(cli.panes, "send", return_value=None):
            self._run_json(["relaunch", t1, "--json"])
        task = store.load()["taches"][t1]
        self.assertEqual(task["state"], "running")
        self.assertIsNone(task["blockedCause"])

    def test_say_refuse_sans_pane(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        code, out, err = self._run(["say", t1, "recadrage"])
        self.assertNotEqual(code, 0)

    def test_say_refuse_si_pane_mort(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
        with mock.patch.object(cli.panes, "alive", return_value=False):
            code, out, err = self._run(["say", t1, "recadrage"])
        self.assertNotEqual(code, 0)

    def test_say_envoie_au_pane_vivant(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "send", return_value=None) as send:
            data = self._run_json(["say", t1, "recadrage", "--json"])
        send.assert_called_once_with("%1", "recadrage")
        self.assertEqual(data["paneId"], "%1")

    def test_capture_json(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
        with mock.patch.object(cli.panes, "capture", return_value="ligne1\nligne2") as cap:
            data = self._run_json(["capture", t1, "--lines", "5", "--json"])
        # warn_floor=True : capture est un verbe humain, la lecture doit signaler visiblement
        # un pane sous le plancher plutot que de rendre un texte tronque comme fiable.
        cap.assert_called_once_with("%1", lines=5, warn_floor=True)
        self.assertEqual(data["capture"], "ligne1\nligne2")
        self.assertIn("session", data)
        self.assertIn("attach", data)

    def test_capture_entete_mentionne_tache_pane_session_et_attache(self):
        """Decision J : capture gagne un en-tete, jamais melange au contenu brut du pane."""
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
        with mock.patch.object(cli.panes, "capture", return_value="contenu du pane"):
            code, out, _ = self._run(["capture", t1])
        self.assertEqual(code, 0)
        self.assertIn(t1, out)
        self.assertIn("%1", out)
        self.assertIn("tmux attach -t", out)
        self.assertIn("contenu du pane", out)

    def test_poll_ne_montre_que_les_panes_vivants(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        t2 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
            state["taches"][t1]["state"] = "running"
            state["taches"][t2]["paneId"] = "%2"
            state["taches"][t2]["state"] = "running"

        def alive(pane_id):
            return pane_id == "%1"

        with mock.patch.object(cli.panes, "alive", side_effect=alive), \
             mock.patch.object(cli.panes, "busy", return_value=True), \
             mock.patch.object(cli.panes, "is_degraded", return_value=False), \
             mock.patch.object(cli.panes, "capture", return_value="dernière activité"):
            data = self._run_json(["poll", "--json"])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["task"], t1)
        self.assertTrue(data[0]["busy"])
        self.assertEqual(data[0]["title"], "titre")
        self.assertFalse(data[0]["belowFloor"])
        self.assertEqual(data[0]["lastLine"], "dernière activité")

    def test_poll_signale_un_pane_sous_le_plancher(self):
        """poll doit exposer un champ structure, jamais un texte a deviner : le meme defaut
        que I12 (jamais l'etat brut servi sans filtre) applique a la geometrie de lecture."""
        cid = self._chantier()
        t1 = self._tache(cid, titre="tache etroite")
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
            state["taches"][t1]["state"] = "running"

        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "busy", return_value=False), \
             mock.patch.object(cli.panes, "is_degraded", return_value=True), \
             mock.patch.object(cli.panes, "capture", return_value="ligne"):
            data = self._run_json(["poll", "--json"])
            _, texte, _ = self._run(["poll"])
        self.assertTrue(data[0]["belowFloor"])
        self.assertIn("BELOW FLOOR", texte)

    def test_poll_texte_affiche_la_colonne_pane_et_la_legende(self):
        """Decision J : le paneId etait deja collecte en JSON mais jete de la ligne humaine."""
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%9"
            state["taches"][t1]["state"] = "running"
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "busy", return_value=True), \
             mock.patch.object(cli.panes, "is_degraded", return_value=False), \
             mock.patch.object(cli.panes, "capture", return_value="ligne"):
            code, out, _ = self._run(["poll"])
        self.assertEqual(code, 0)
        self.assertIn("%9", out)
        self.assertIn("legend", out)

    def test_poll_distingue_tour_fini_et_silence(self):
        """Decision J : remplace 'idle' par une distinction tour termine / silence."""
        cid = self._chantier()
        t1 = self._tache(cid, titre="finie")
        t2 = self._tache(cid, titre="silencieuse")
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
            state["taches"][t1]["state"] = "running"
            state["taches"][t2]["paneId"] = "%2"
            state["taches"][t2]["state"] = "running"
        from ordo import report as report_mod
        report_mod.path(t1, cid).write_text(
            json.dumps({"task": t1, "state": "done", "note": "ok"}), encoding="utf-8"
        )

        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "busy", return_value=False), \
             mock.patch.object(cli.panes, "is_degraded", return_value=False), \
             mock.patch.object(cli.panes, "capture", return_value="ligne"):
            code, out, _ = self._run(["poll"])
        self.assertEqual(code, 0)
        ligne_t1 = next(l for l in out.splitlines() if l.startswith(t1))
        ligne_t2 = next(l for l in out.splitlines() if l.startswith(t2))
        self.assertIn("turn-done", ligne_t1)
        self.assertIn("silence", ligne_t2)

    def test_poll_avertit_si_plusieurs_projets_ouverts(self):
        proja = self._projet("proja")
        projb = self._projet("projb")
        self._chantier(projet=proja)
        self._chantier(projet=projb, home_partage=True)
        code, out, _ = self._run(["poll"])
        self.assertEqual(code, 0)
        self.assertIn("WARNING", out)
        self.assertIn(store.canon(proja), out)
        self.assertIn(store.canon(projb), out)

    def test_kill(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
        with mock.patch.object(cli.panes, "kill", return_value=True) as kill:
            data = self._run_json(["kill", t1, "--json"])
        kill.assert_called_once_with("%1")
        self.assertTrue(data["killed"])

    def test_kill_texte_mentionne_le_process_claude_detruit(self):
        """Decision J : dire que le pane ET le process claude sont detruits."""
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
        with mock.patch.object(cli.panes, "kill", return_value=True):
            code, out, _ = self._run(["kill", t1])
        self.assertEqual(code, 0)
        self.assertIn("destroyed", out)
        self.assertIn("claude process", out)

    def test_kill_ecrit_une_entree_journal_ordo(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
        with mock.patch.object(cli.panes, "kill", return_value=True):
            self._run(["kill", t1])
        from ordo import journal as journal_mod
        entries = journal_mod.read(cid)
        self.assertTrue(any(e["auteur"] == "ORDO" and t1 in e["texte"] for e in entries))


# ---------------------------------------------------------------------------
# Attach (point K) : imprime, n'execute jamais.
# ---------------------------------------------------------------------------


class TestAttachVerb(CliTestCase):
    def test_attach_sur_chantier_json(self):
        cid = self._chantier()
        rows = [
            {"pane_id": "%1", "titre": "t-01 migration", "largeur": 130, "hauteur": 40,
             "vivant": True, "actif": True, "sousPlancher": False},
        ]
        with mock.patch.object(cli.panes, "panes", return_value=rows):
            data = self._run_json(["attach", cid, "--json"])
        self.assertEqual(data["campaign"], cid)
        self.assertIn("tmux attach -t", data["attach"])
        self.assertEqual(data["panes"], [{"paneId": "%1", "title": "t-01 migration"}])

    def test_attach_sur_tache_resout_le_chantier(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with mock.patch.object(cli.panes, "panes", return_value=[]):
            data = self._run_json(["attach", t1, "--json"])
        self.assertEqual(data["campaign"], cid)

    def test_attach_texte_liste_les_panes_avec_titre(self):
        cid = self._chantier()
        rows = [
            {"pane_id": "%3", "titre": "t-02 refonte", "largeur": 130, "hauteur": 40,
             "vivant": True, "actif": False, "sousPlancher": False},
        ]
        with mock.patch.object(cli.panes, "panes", return_value=rows):
            code, out, _ = self._run(["attach", cid])
        self.assertEqual(code, 0)
        self.assertIn("%3", out)
        self.assertIn("t-02 refonte", out)
        self.assertIn("tmux attach -t", out)

    def test_attach_cible_introuvable_refuse(self):
        code, out, err = self._run(["attach", "c-99"])
        self.assertNotEqual(code, 0)
        self.assertIn("c-99", err)

    def test_attach_najamais_de_process_reel(self):
        """Executer l'attache depuis ici detournerait le terminal de l'orchestratrice."""
        cid = self._chantier()
        with mock.patch.object(cli.panes, "panes", return_value=[]) as panes_mock, \
             mock.patch.object(cli.subprocess, "run") as run_mock:
            code, _, _ = self._run(["attach", cid])
        self.assertEqual(code, 0)
        panes_mock.assert_called_once()
        run_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Doctor (point M)
# ---------------------------------------------------------------------------


class TestDoctorVerb(CliTestCase):
    def _tmux_ok(self):
        return mock.patch.object(
            cli.subprocess, "run",
            return_value=subprocess.CompletedProcess(
                args=["tmux", "-V"], returncode=0, stdout="tmux 3.7b\n", stderr=""
            ),
        )

    def test_doctor_json_exit_zero_si_tout_present(self):
        with mock.patch.object(cli.shutil, "which", side_effect=lambda b: f"/usr/bin/{b}"), \
             self._tmux_ok():
            code, out, err = self._run(["doctor", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["tmux"]["present"])
        self.assertTrue(data["claude"]["present"])
        self.assertEqual(data["tmux"]["version"], "tmux 3.7b")
        self.assertIn("ordoHome", data)

    def test_doctor_exit_1_si_tmux_absent(self):
        def which(binaire):
            return None if binaire == "tmux" else f"/usr/bin/{binaire}"
        with mock.patch.object(cli.shutil, "which", side_effect=which):
            code, out, err = self._run(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)

    def test_doctor_exit_1_si_claude_absent(self):
        def which(binaire):
            return None if binaire == "claude" else f"/usr/bin/{binaire}"
        with mock.patch.object(cli.shutil, "which", side_effect=which), self._tmux_ok():
            code, out, err = self._run(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)

    def test_doctor_compte_les_chantiers_ouverts_et_avertit_multi_projet(self):
        self._chantier()
        self._chantier(projet=self._projet("autre"), home_partage=True)
        with mock.patch.object(cli.shutil, "which", side_effect=lambda b: f"/usr/bin/{b}"), \
             self._tmux_ok():
            data = self._run_json(["doctor", "--json"])
        self.assertEqual(data["openCampaigns"], 2)
        self.assertIsNotNone(data["projectWarning"])


# ---------------------------------------------------------------------------
# --version / --verbose (point L)
# ---------------------------------------------------------------------------


class TestVersionEtVerbose(CliTestCase):
    def test_version_affiche_la_version_et_quitte(self):
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(cli.__version__, out.getvalue())

    def test_verbose_branche_panes_trace(self):
        """panes.TRACE est le mecanisme reellement expose (None par defaut, callable
        appele avec la commande complete depuis l'unique choke-point _tmux())."""
        self.assertIsNone(cli.panes.TRACE)
        try:
            self._run(["--verbose", "list"])
            self.assertIsNotNone(cli.panes.TRACE)
            self.assertTrue(callable(cli.panes.TRACE))
        finally:
            cli.panes.TRACE = None

    def test_sans_verbose_ne_touche_pas_a_panes_trace(self):
        cli.panes.TRACE = None
        self._run(["list"])
        self.assertIsNone(cli.panes.TRACE)

    def test_trace_tmux_ecrit_sur_stderr_prefixe_tmux(self):
        err = io.StringIO()
        with redirect_stderr(err):
            cli._trace_tmux(["tmux", "attach", "-t", "ordo-p"])
        self.assertEqual(err.getvalue().strip(), "tmux> tmux attach -t ordo-p")


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------


class TestSignalVerbs(CliTestCase):
    def test_tick_json_sur_controle_reel(self):
        cid = self._chantier()
        self._tache(cid)
        data = self._run_json(["tick", "--json"])
        self.assertIn("chantiers", data)
        self.assertIn(cid, data["chantiers"])

    def test_tick_sert_les_motifs_de_reveil(self):
        """tick() DOIT joindre wake_reasons a la reconciliation, en JSON et en texte.

        Une orchestratrice n'appelle que tick() en reprenant la main. Un motif qu'elle
        doit escalader mais que tick() ne sert pas est invisible : c'est le defaut
        "predicat correct que personne n'appelle" deja paye une fois dans le depot
        d'origine. Trouve en parcours reel, pas par une suite unitaire.
        """
        cid = self._chantier()
        tid = self._tache(cid)
        motif = {"kind": "trust-expected", "task": tid, "detail": "dialogue affiche"}
        with mock.patch("ordo.controle.wake_reasons", return_value=[motif]):
            _, texte, _ = self._run(["tick"])
        self.assertIn("WAKEUP trust-expected", texte)
        self.assertIn("dialogue affiche", texte)

    def test_tick_ne_ressert_pas_un_motif_deja_servi(self):
        """Un motif servi une fois ne revient plus ; --all-wakeups le rend quand meme.

        wake_reasons() recalcule tout l'etat courant a chaque appel. Sans filtrage,
        chaque tour de controle recrachait un motif par tache terminee, et le seul motif
        reellement neuf s'y noyait. Trouve en parcours reel sur un chantier a deux taches.
        """
        cid = self._chantier()
        tid = self._tache(cid)
        motif = {"kind": "trust-expected", "task": tid, "detail": "dialogue affiche"}
        with mock.patch("ordo.controle.wake_reasons", return_value=[motif]):
            premier = self._run_json(["tick", "--json"])
            second = self._run_json(["tick", "--json"])
            complet = self._run_json(["tick", "--all-wakeups", "--json"])
        self.assertEqual(premier["wakeups"][cid], [motif])
        self.assertNotIn(cid, second["wakeups"])
        self.assertEqual(complet["wakeups"][cid], [motif])

    def test_tick_ressert_toujours_le_tour_de_controle(self):
        """Un motif purement temporel n'est jamais marque comme servi.

        Le marquer l'eteindrait des le premier tour, alors qu'il se redeclenche par le
        temps et non par un fait nouveau : l'orchestratrice cesserait d'etre reveillee
        par le silence, ce qui est precisement ce que ce motif surveille.
        """
        cid = self._chantier()
        motif = {"kind": "control-round", "task": None, "detail": "rien remonté"}
        with mock.patch("ordo.controle.wake_reasons", return_value=[motif]):
            premier = self._run_json(["tick", "--json"])
            second = self._run_json(["tick", "--json"])
        self.assertEqual(premier["wakeups"][cid], [motif])
        self.assertEqual(second["wakeups"][cid], [motif])

    def test_tick_module_absent_donne_message_clair(self):
        """Simule ordo/controle.py absent, meme s'il est deja importe dans ce process.

        Une fois qu'un "from . import controle" a reussi une premiere fois (par ex.
        dans test_tick_json_sur_controle_reel, execute avant celui-ci par ordre
        alphabetique), Python met en cache l'attribut "controle" sur l'objet module du
        paquet "ordo" lui-meme : un simple mock.patch.dict(sys.modules, ...) ne suffit
        plus a faire echouer un futur "from . import controle", puisque
        _handle_fromlist() consulte hasattr(lib, "controle") AVANT de retoucher a
        sys.modules. Retirer aussi cet attribut est necessaire pour forcer un vrai
        nouvel essai d'import, qui trouve alors sys.modules["ordo.controle"] = None et
        leve ModuleNotFoundError (I8 : le refus doit se dire, jamais planter en trace).
        """
        ordo_pkg = sys.modules["ordo"]
        had_attr = hasattr(ordo_pkg, "controle")
        prev_attr = getattr(ordo_pkg, "controle", None)
        prev_module = sys.modules.get("ordo.controle")
        if had_attr:
            delattr(ordo_pkg, "controle")
        sys.modules["ordo.controle"] = None
        try:
            code, out, err = self._run(["tick"])
        finally:
            if prev_module is not None:
                sys.modules["ordo.controle"] = prev_module
            else:
                sys.modules.pop("ordo.controle", None)
            if had_attr:
                setattr(ordo_pkg, "controle", prev_attr)
        self.assertNotEqual(code, 0)
        self.assertIn("controle", err)

    def test_tick_avertit_si_plusieurs_projets_ouverts(self):
        """Point I4 : l'annonce en tete de sortie, sans changer la portee par defaut."""
        proja = self._projet("proja")
        projb = self._projet("projb")
        self._chantier(projet=proja)
        self._chantier(projet=projb, home_partage=True)
        data = self._run_json(["tick", "--json"])
        self.assertIsNotNone(data["projectWarning"])
        code, out, _ = self._run(["tick"])
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[0].startswith("WARNING"))

    def test_tick_sans_avertissement_si_un_seul_projet(self):
        cid = self._chantier()
        self._tache(cid)
        data = self._run_json(["tick", "--json"])
        self.assertIsNone(data["projectWarning"])

    def test_report_sans_fichier(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        data = self._run_json(["report", t1, "--json"])
        self.assertFalse(data["rawReportPresent"])
        self.assertIsNone(data["rawReport"])

    def test_report_fichier_illisible_ne_casse_pas(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        from ordo import report as report_mod
        report_mod.path(t1, cid).write_text("ceci n'est pas du JSON", encoding="utf-8")
        data = self._run_json(["report", t1, "--json"])
        self.assertTrue(data["rawReportPresent"])
        self.assertIsNotNone(data["rawReportError"])

    def test_ask_puis_questions_puis_answer(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
        q = self._run_json(["ask", t1, "quelle branche ?", "--option", "a",
                             "--option", "b", "--for-human", "--json"])
        self.assertEqual(q["tache"], t1)
        self.assertTrue(q["pourHumain"])
        self.assertEqual(q["options"], ["a", "b"])

        pending = self._run_json(["questions", "--json"])
        self.assertEqual(len(pending), 1)

        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "send", return_value=None):
            answered = self._run_json(["answer", q["id"], "main", "--json"])
        self.assertEqual(answered["answer"], "main")

        pending_after = self._run_json(["questions", "--json"])
        self.assertEqual(pending_after, [])
        all_after = self._run_json(["questions", "--all", "--json"])
        self.assertEqual(len(all_after), 1)

    def test_ask_accepte_un_chantier_a_la_place_d_une_tache(self):
        # Une orchestratrice escalade le plus souvent une question qui n'appartient a
        # AUCUNE tache : lancer la suite en parallele ou en serie, decouper maintenant ou
        # plus tard. Exiger une tache la forcait a en designer une au hasard.
        cid = self._chantier()
        q = self._run_json(["ask", cid, "parallele ou serie ?", "--for-human", "--json"])
        self.assertEqual(q["chantier"], cid)
        self.assertIsNone(q["tache"])
        self.assertTrue(q["pourHumain"])

    def test_une_question_de_chantier_n_affiche_jamais_le_mot_None(self):
        # Sans cible de repli, la ligne affichait "q-01  None  ...", ce qui se lit comme
        # un defaut de la commande et non comme une question qui porte sur la campagne.
        cid = self._chantier()
        code, out, err = self._run(["ask", cid, "parallele ou serie ?"])
        self.assertEqual(code, 0)
        self.assertNotIn("None", out)
        self.assertIn(cid, out)
        code, out, err = self._run(["questions"])
        self.assertNotIn("None", out)
        self.assertIn(cid, out)

    def test_une_question_de_chantier_se_liste_et_se_repond_comme_les_autres(self):
        cid = self._chantier()
        q = self._run_json(["ask", cid, "parallele ou serie ?", "--json"])
        self.assertEqual(len(self._run_json(["questions", "--json"])), 1)
        self._run_json(["answer", q["id"], "parallele", "--json"])
        self.assertEqual(self._run_json(["questions", "--json"]), [])

    def test_ask_refuse_un_identifiant_qui_n_est_ni_tache_ni_chantier(self):
        self._chantier()
        code, out, err = self._run(["ask", "z-99", "q ?"])
        self.assertNotEqual(code, 0)
        self.assertIn("z-99", err)

    def test_answer_refuse_si_deja_repondue(self):
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%1"
        q = self._run_json(["ask", t1, "q ?", "--json"])
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "send", return_value=None):
            self._run_json(["answer", q["id"], "r1", "--json"])
        code, out, err = self._run(["answer", q["id"], "r2"])
        self.assertNotEqual(code, 0)

    def test_answer_question_introuvable(self):
        code, out, err = self._run(["answer", "q-99", "r"])
        self.assertNotEqual(code, 0)
        self.assertIn("q-99", err)

    def test_answer_pousse_la_reponse_dans_le_pane_de_la_tache(self):
        # c2/c6 : la réponse part réellement dans le pane, comme le ferait un ordo say --
        # c'est exactement l'appel manquant qui a coûté une demi-heure sur t-20.
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%7"
            state["taches"][t1]["state"] = "waiting"
        q = self._run_json(["ask", t1, "quelle branche ?", "--json"])
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "send", return_value=None) as send:
            answered = self._run_json(["answer", q["id"], "main", "--json"])
        send.assert_called_once_with("%7", "Answer: main")
        self.assertIsNotNone(answered.get("injectedAt"))
        task = store.load()["taches"][t1]
        self.assertEqual(task["state"], "running")

    def test_answer_echoue_bruyamment_si_pane_mort_mais_garde_la_reponse(self):
        # c3/c4 : pane mort -> personne n'a rien reçu, la commande le dit et n'affiche
        # jamais "answered" pour ce mensonge précis (le bug réel de t-20).
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%9"
        q = self._run_json(["ask", t1, "quelle branche ?", "--json"])
        with mock.patch.object(cli.panes, "alive", return_value=False):
            code, out, err = self._run(["answer", q["id"], "main"])
        self.assertNotEqual(code, 0)
        self.assertNotIn("answered", out)
        self.assertIn(t1, err)
        stocke = store.load()["questions"][q["id"]]
        self.assertEqual(stocke["answer"], "main")
        self.assertIsNone(stocke.get("injectedAt"))

    def test_answer_sans_pane_jamais_lance_echoue_aussi(self):
        # Même famille que ci-dessus mais sans paneId du tout (tâche jamais lancée).
        cid = self._chantier()
        t1 = self._tache(cid)
        q = self._run_json(["ask", t1, "quelle branche ?", "--json"])
        code, out, err = self._run(["answer", q["id"], "main"])
        self.assertNotEqual(code, 0)
        self.assertIn(t1, err)
        stocke = store.load()["questions"][q["id"]]
        self.assertEqual(stocke["answer"], "main")

    def test_answer_reste_repondable_une_fois_meme_si_lenvoi_echoue(self):
        # c5 : un envoi qui échoue (pane vivant mais send() lève) ne perd pas la réponse
        # et ne la laisse pas non plus "répondable" une seconde fois -- le refus de
        # double réponse tient, la réponse reste enregistrée pour une livraison ultérieure.
        cid = self._chantier()
        t1 = self._tache(cid)
        with store.locked() as state:
            state["taches"][t1]["paneId"] = "%3"
        q = self._run_json(["ask", t1, "quelle branche ?", "--json"])
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "send", side_effect=RuntimeError("boom")):
            code, out, err = self._run(["answer", q["id"], "main"])
        self.assertNotEqual(code, 0)
        stocke = store.load()["questions"][q["id"]]
        self.assertEqual(stocke["answer"], "main")
        self.assertIsNone(stocke.get("injectedAt"))
        code2, out2, err2 = self._run(["answer", q["id"], "autre"])
        self.assertNotEqual(code2, 0)

    def test_answer_de_chantier_ne_tente_aucun_envoi(self):
        # Question sans tâche (portée chantier) : rien à livrer, comportement inchangé.
        cid = self._chantier()
        q = self._run_json(["ask", cid, "parallèle ou série ?", "--json"])
        with mock.patch.object(cli.panes, "alive") as alive, \
             mock.patch.object(cli.panes, "send") as send:
            answered = self._run_json(["answer", q["id"], "parallèle", "--json"])
        alive.assert_not_called()
        send.assert_not_called()
        self.assertEqual(answered["answer"], "parallèle")


# ---------------------------------------------------------------------------
# Capteur
# ---------------------------------------------------------------------------


class TestCapteurVerbs(CliTestCase):
    def _script(self) -> Path:
        # La valeur mesuree doit varier a chaque execution : un capteur qui rend le
        # meme payload sur plusieurs cycles consecutifs est detecte "frozen" (voir
        # FROZEN_AFTER dans ordo/capteur.py), pas "ok". time.time_ns() garantit une
        # valeur differente a chaque appel, ce qui isole ce test de ce signal-la.
        path = Path(self._tmp) / "capteur.py"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, time\n"
            "print(json.dumps({\n"
            '    "at": "2026-08-09T00:00:00Z", "ok": True,\n'
            '    "measured": [{"name": "n", "value": time.time_ns()}],\n'
            '    "declared": [], "drift": [], "unknown": [],\n'
            "}))\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def test_install_status_run_adopt(self):
        cid = self._chantier()
        script = self._script()

        data = self._run_json(["sensor", "install", cid, str(script), "--json"])
        self.assertTrue(data["path"].endswith(".py"))
        self.assertEqual(data["status"], {"adopted": False, "signal": "unknown"})

        # status() jamais brut : aucune mesure avant adoption, meme avec des runs.
        for _ in range(3):
            run = self._run_json(["sensor", "run", cid, "--json"])
            self.assertTrue(run["ok"])
        status_avant = self._run_json(["sensor", "status", cid, "--json"])
        self.assertFalse(status_avant["adopted"])
        self.assertEqual(status_avant["signal"], "unknown")
        self.assertNotIn("measured", status_avant)

        adopt = self._run_json(["sensor", "adopt", cid, "--json"])
        self.assertTrue(adopt["status"]["adopted"])
        self.assertEqual(adopt["status"]["signal"], "ok")
        self.assertEqual(adopt["status"]["measured"][0]["name"], "n")

    def test_adopt_refuse_sans_concordance(self):
        cid = self._chantier()
        script = self._script()
        self._run(["sensor", "install", cid, str(script), "--json"])
        code, out, err = self._run(["sensor", "adopt", cid])
        self.assertNotEqual(code, 0)

    def test_capteur_sans_action_affiche_aide(self):
        code, out, err = self._run(["sensor"])
        self.assertEqual(code, 0)
        self.assertIn("install", out)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class TestJournalVerbs(CliTestCase):
    def test_add_refuse_auteur_invalide(self):
        # "AUTRE" n'est pas dans choices=(ORDO, ORCH, USER) : argparse refuse avant meme
        # d'atteindre le dispatch, via SystemExit(2), pas via un retour de cmd_journal_add.
        cid = self._chantier()
        with self.assertRaises(SystemExit) as ctx:
            self._run(["journal", "add", cid, "--author", "AUTRE", "--note", "x"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_add_puis_show(self):
        cid = self._chantier()
        data = self._run_json(
            ["journal", "add", cid, "--author", "ORCH", "--note", "decision X", "--json"]
        )
        self.assertEqual(data["author"], "ORCH")
        entries = self._run_json(["journal", "show", cid, "--json"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["texte"], "decision X")

    def test_journal_sans_action_affiche_aide(self):
        code, out, err = self._run(["journal"])
        self.assertEqual(code, 0)
        self.assertIn("show", out)


# ---------------------------------------------------------------------------
# Dispatch : aide, code de sortie, --debug
# ---------------------------------------------------------------------------


class TestDispatch(CliTestCase):
    def test_ordo_sans_argument_affiche_aide_sans_trace(self):
        code, out, err = self._run([])
        self.assertEqual(code, 0)
        self.assertIn("usage", out.lower())
        self.assertEqual(err, "")

    def test_verbe_inconnu_argparse_sort_non_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(["verbe-qui-n-existe-pas"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_erreur_sans_debug_pas_de_trace(self):
        code, out, err = self._run(["show", "c-99"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)
        self.assertIn("error", err)

    def test_debug_relance_l_exception_brute(self):
        with mock.patch.object(cli, "cmd_list", side_effect=RuntimeError("boom")):
            code, out, err = self._run(["list"])
            self.assertEqual(code, 1)
            self.assertIn("boom", err)
            self.assertNotIn("Traceback", err)

            with self.assertRaises(RuntimeError):
                self._run(["--debug", "list"])


# ---------------------------------------------------------------------------
# Sous-processus : le vrai bin/ordo
# ---------------------------------------------------------------------------


class BinOrdoTestCase(unittest.TestCase):
    """Isolation par ORDO_HOME transmis a l'environnement du sous-processus."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-bin-test-")
        self._home = str(Path(self._tmp) / "home")
        Path(self._home).mkdir()
        self._env = {**os.environ, "ORDO_HOME": self._home}
        # cwd d'execution du sous-processus : deliberement PAS le depot ordo, pour
        # prouver la resolution du paquet ordo/ par rapport au script (bin/ordo), pas au
        # repertoire courant. Un frere du dossier ordo, jamais un sous-dossier de celui-ci.
        self._cwd = str(Path(self._tmp) / "ailleurs")
        Path(self._cwd).mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _ordo(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [ORDO_BIN, *args], cwd=self._cwd, env=self._env,
            capture_output=True, text=True, timeout=30,
        )


class TestResolutionDepuisAutreRepertoire(BinOrdoTestCase):
    def test_start_fonctionne_depuis_un_repertoire_etranger_a_ordo(self):
        projet = Path(self._tmp) / "mon-projet"
        projet.mkdir()
        result = self._ordo("start", str(projet), "--goal", "obj", "--json")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["id"], "c-01")

    def test_ordo_seul_affiche_aide_exit_zero(self):
        result = self._ordo()
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage", result.stdout.lower())

    def test_capteur_seul_affiche_aide_exit_zero(self):
        result = self._ordo("sensor")
        self.assertEqual(result.returncode, 0)
        self.assertIn("install", result.stdout)

    def test_version_fonctionne_en_sous_processus(self):
        result = self._ordo("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn("0.1.0", result.stdout)


class TestCodeDeSortieSurRefus(BinOrdoTestCase):
    def test_show_chantier_absent_sort_non_zero_avec_raison(self):
        result = self._ordo("show", "c-99")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("c-99", result.stderr)

    def test_dep_cycle_sort_non_zero(self):
        projet = Path(self._tmp) / "mon-projet"
        projet.mkdir()
        self._ordo("start", str(projet), "--goal", "obj")
        self._ordo("add", "c-01", "--title", "t1", "--prompt", "p1")
        self._ordo("add", "c-01", "--title", "t2", "--prompt", "p2", "--depends", "t-01")
        result = self._ordo("dep", "t-01", "t-02")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cycle", result.stderr.lower())


class TestJsonValideSurLesVerbesDeLecture(BinOrdoTestCase):
    """Chaque verbe de lecture cite par SKILL.md, appele en conditions reelles."""

    def setUp(self) -> None:
        super().setUp()
        self._projet_path = Path(self._tmp) / "mon-projet"
        self._projet_path.mkdir()
        r = self._ordo("start", str(self._projet_path), "--goal", "obj",
                        "--scope", "peri", "--out-of-scope", "hs", "--json")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.chantier = json.loads(r.stdout)["id"]

        r = self._ordo("add", self.chantier, "--title", "t1", "--prompt", "p1",
                        "--check", "tests verts", "--json")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.t1 = json.loads(r.stdout)["id"]

        r = self._ordo("add", self.chantier, "--title", "t2", "--prompt", "p2",
                        "--depends", self.t1, "--json")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.t2 = json.loads(r.stdout)["id"]

        r = self._ordo("ask", self.t1, "une question ?", "--json")
        self.assertEqual(r.returncode, 0, msg=r.stderr)

    def _assert_json(self, *args: str):
        result = self._ordo(*args)
        self.assertEqual(result.returncode, 0, msg=f"{args} : {result.stderr}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"{args} n'a pas produit de JSON valide : {exc}\n{result.stdout!r}")

    def test_list(self):
        self._assert_json("list", "--json")

    def test_show(self):
        self._assert_json("show", self.chantier, "--json")

    def test_graph(self):
        self._assert_json("graph", self.chantier, "--json")

    def test_digest(self):
        self._assert_json("digest", self.chantier, "--json")

    def test_ready(self):
        self._assert_json("ready", self.chantier, "--json")

    def test_ready_why(self):
        self._assert_json("ready", self.chantier, "--why", "--json")

    def test_propositions(self):
        self._assert_json("proposals", "--json")

    def test_questions(self):
        self._assert_json("questions", "--json")

    def test_poll(self):
        self._assert_json("poll", "--json")

    def test_tick(self):
        self._assert_json("tick", "--json")

    def test_report(self):
        self._assert_json("report", self.t1, "--json")

    def test_capteur_status(self):
        self._assert_json("sensor", "status", self.chantier, "--json")

    def test_journal_show(self):
        self._assert_json("journal", "show", self.chantier, "--json")

    def test_brief(self):
        self._assert_json("brief", self.chantier, "--json")

    def test_doctor(self):
        self._assert_json("doctor", "--json")

    def test_attach(self):
        self._assert_json("attach", self.chantier, "--json")


class TestReapAndResume(CliTestCase):
    """Une tache terminee ferme son pane ; la rouvrir se fait sur sa session claude.

    But : qu'un `tmux attach` ne montre que du travail vivant. Une fenetre pleine de panes
    termines fait passer pour actif ce qui ne l'est plus, et c'est ce que l'humain regarde
    pour juger l'avancement.
    """

    def _tache_finie(self, cid: str, pane: str = "%42") -> str:
        tid = self._tache(cid)
        with store.locked() as state:
            state["taches"][tid]["state"] = "done"
            state["taches"][tid]["paneId"] = pane
            state["taches"][tid]["claudeSessionId"] = "11111111-2222-3333-4444-555555555555"
            state["chantiers"][cid]["tmuxWindow"] = "@7"
        return tid

    def test_tick_ferme_le_pane_dune_tache_done(self):
        cid = self._chantier()
        tid = self._tache_finie(cid)
        with mock.patch.object(cli.panes, "capture", return_value="derniere ligne"), \
             mock.patch.object(cli.panes, "kill", return_value=True) as kill, \
             mock.patch.object(cli.panes, "relayout") as relayout:
            code, out, err = self._run(["tick", "--campaign", cid])
        self.assertEqual(code, 0, err)
        kill.assert_called_once_with("%42")
        relayout.assert_called_once_with("@7")
        t = store.load()["taches"][tid]
        self.assertIsNone(t["paneId"])
        self.assertTrue(t["reaped"])

    def test_la_queue_decran_est_gardee_avant_de_fermer(self):
        """Moissonner sans garder la capture echangerait du bruit contre de l'amnesie."""
        cid = self._chantier()
        tid = self._tache_finie(cid)
        with mock.patch.object(cli.panes, "capture", return_value="ce que l'executante a dit"), \
             mock.patch.object(cli.panes, "kill", return_value=True), \
             mock.patch.object(cli.panes, "relayout"):
            self._run(["tick", "--campaign", cid])
        self.assertEqual(
            store.load()["taches"][tid]["lastCapture"], "ce que l'executante a dit"
        )
        code, out, _ = self._run(["capture", tid])
        self.assertEqual(code, 0)
        self.assertIn("ce que l'executante a dit", out)
        self.assertIn("pane closed", out)

    def test_une_tache_bloquee_garde_son_pane(self):
        """C'est exactement celle qu'un humain veut aller regarder."""
        cid = self._chantier()
        tid = self._tache_finie(cid)
        with store.locked() as state:
            state["taches"][tid]["state"] = "blocked"
        with mock.patch.object(cli.panes, "capture", return_value="x"), \
             mock.patch.object(cli.panes, "kill") as kill, \
             mock.patch.object(cli.panes, "relayout"):
            self._run(["tick", "--campaign", cid])
        kill.assert_not_called()
        self.assertEqual(store.load()["taches"][tid]["paneId"], "%42")

    def test_keep_panes_desactive_la_moisson(self):
        cid = self._chantier()
        tid = self._tache_finie(cid)
        with store.locked() as state:
            state["chantiers"][cid]["keepPanes"] = True
        with mock.patch.object(cli.panes, "capture", return_value="x"), \
             mock.patch.object(cli.panes, "kill") as kill, \
             mock.patch.object(cli.panes, "relayout"):
            self._run(["tick", "--campaign", cid])
        kill.assert_not_called()
        self.assertEqual(store.load()["taches"][tid]["paneId"], "%42")

    def test_resume_relance_claude_sur_la_meme_session(self):
        cid = self._chantier()
        tid = self._tache_finie(cid)
        with mock.patch.object(cli.panes, "capture", return_value="x"), \
             mock.patch.object(cli.panes, "kill", return_value=True), \
             mock.patch.object(cli.panes, "relayout"):
            self._run(["tick", "--campaign", cid])
        with mock.patch.object(cli.panes, "ensure_session", return_value=("ordo-p", "@7")), \
             mock.patch.object(cli.panes, "spawn", return_value="%99") as spawn, \
             mock.patch.object(cli.panes, "relayout"):
            code, out, err = self._run(["resume", tid])
        self.assertEqual(code, 0, err)
        cmd = spawn.call_args[0][2]
        self.assertIn("claude --resume 11111111-2222-3333-4444-555555555555", cmd)
        # Le brief ne repart JAMAIS : la session reprise a deja tout son contexte.
        self.assertNotIn("briefs/", cmd)
        self.assertEqual(store.load()["taches"][tid]["paneId"], "%99")
        self.assertIn("expensive", out)

    def test_resume_refuse_si_le_pane_vit_encore(self):
        cid = self._chantier()
        tid = self._tache_finie(cid)
        with mock.patch.object(cli.panes, "alive", return_value=True):
            code, _, err = self._run(["resume", tid])
        self.assertNotEqual(code, 0)
        self.assertIn("live pane", err)

    def test_resume_refuse_sans_identifiant_de_session(self):
        cid = self._chantier()
        tid = self._tache_finie(cid)
        with store.locked() as state:
            state["taches"][tid]["claudeSessionId"] = None
            state["taches"][tid]["paneId"] = None
        code, _, err = self._run(["resume", tid])
        self.assertNotEqual(code, 0)
        self.assertIn("relaunch", err)


class TestWatchVerb(CliTestCase):
    """`ordo watch` : flux d'evenements en lecture seule, une ligne par fait nouveau.

    Le defaut qu'il corrige : une orchestratrice qui lance une executante puis termine son
    tour n'a rien qui la ramene. Chaque ligne emise ici est ce qui la reveille.
    """

    def _tache_avec_pane(self, cid: str, pane: str = "%42") -> str:
        tid = self._tache(cid)
        with store.locked() as state:
            state["taches"][tid]["state"] = "running"
            state["taches"][tid]["paneId"] = pane
        return tid

    def _ecrire_rapport(self, tid: str, contenu: str) -> None:
        chantier_id = cli.store.load()["taches"][tid]["chantier"]
        chemin = cli.report.path(tid, chantier_id)
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(contenu, encoding="utf-8")

    def test_chantier_inconnu_refuse(self):
        code, _, err = self._run(["watch", "c-99"])
        self.assertNotEqual(code, 0)
        self.assertIn("c-99", err)

    def test_aucun_pane_vivant_dit_idle_et_sort(self):
        cid = self._chantier()
        self._tache(cid)
        code, out, _ = self._run(["watch", cid, "--interval", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"idle {cid}")

    def test_rapport_qui_apparait_produit_une_ligne_report(self):
        cid = self._chantier()
        tid = self._tache_avec_pane(cid)
        self._ecrire_rapport(tid, json.dumps({"task": tid, "state": "done"}))
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "busy", return_value=False):
            code, out, _ = self._run(["watch", cid, "--interval", "0", "--passes", "1"])
        self.assertEqual(code, 0)
        self.assertIn(f"report {tid} done", out)

    def test_rapport_illisible_produit_unreadable(self):
        cid = self._chantier()
        tid = self._tache_avec_pane(cid)
        self._ecrire_rapport(tid, "{ ceci n'est pas du json")
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "busy", return_value=False):
            _, out, _ = self._run(["watch", cid, "--interval", "0", "--passes", "1"])
        self.assertIn(f"report {tid} unreadable", out)

    def test_pane_qui_meurt_produit_pane_dead(self):
        cid = self._chantier()
        tid = self._tache_avec_pane(cid)
        # Deux passes : la premiere etablit "vivant", la seconde observe la transition.
        # Un fait de transition ne peut pas se voir en une seule passe.
        with mock.patch.object(cli.panes, "alive", side_effect=[True, False]), \
             mock.patch.object(cli.panes, "busy", return_value=False):
            code, out, _ = self._run(["watch", cid, "--interval", "0", "--passes", "5"])
        self.assertEqual(code, 0)
        self.assertIn(f"pane-dead {tid}", out)
        self.assertIn(f"idle {cid}", out)

    def test_tour_fini_sans_rapport_produit_turn_done(self):
        cid = self._chantier()
        tid = self._tache_avec_pane(cid)
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "busy", side_effect=[True, False]):
            _, out, _ = self._run(["watch", cid, "--interval", "0", "--passes", "2"])
        self.assertIn(f"turn-done {tid} no report", out)

    def test_pane_vivant_et_muet_produit_silent(self):
        cid = self._chantier()
        tid = self._tache_avec_pane(cid)
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "busy", return_value=False):
            _, out, _ = self._run(
                ["watch", cid, "--interval", "0", "--silence", "0", "--passes", "1"]
            )
        self.assertRegex(out, rf"silent {tid} \d+s")

    def test_question_sans_reponse_produit_une_ligne(self):
        cid = self._chantier()
        tid = self._tache_avec_pane(cid)
        with store.locked() as state:
            state["questions"]["q-01"] = {
                "id": "q-01", "chantier": cid, "tache": tid,
                "question": "quoi ?", "options": [], "pourHumain": False,
                "answer": None, "askedAt": store.now(), "answeredAt": None,
            }
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "busy", return_value=False):
            _, out, _ = self._run(["watch", cid, "--interval", "0", "--passes", "1"])
        self.assertIn(f"question q-01 {tid}", out)

    def test_un_fait_n_est_emis_qu_une_fois(self):
        cid = self._chantier()
        tid = self._tache_avec_pane(cid)
        self._ecrire_rapport(tid, json.dumps({"task": tid, "state": "done"}))
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "busy", return_value=False):
            _, out, _ = self._run(["watch", cid, "--interval", "0", "--passes", "3"])
        self.assertEqual(out.count(f"report {tid} done"), 1, out)

    def test_watch_n_ecrit_jamais_dans_l_etat(self):
        """Garantie centrale : une veille qui consommerait les motifs de reveil
        affamerait la reconciliation qu'elle declenche. L'orchestratrice, reveillee,
        ne trouverait plus rien a traiter."""
        cid = self._chantier()
        tid = self._tache_avec_pane(cid)
        self._ecrire_rapport(tid, json.dumps({"task": tid, "state": "done"}))
        chemin_etat = store.home() / "state.json"
        avant = chemin_etat.read_bytes()
        avant_mtime = chemin_etat.stat().st_mtime
        with mock.patch.object(cli.panes, "alive", return_value=True), \
             mock.patch.object(cli.panes, "busy", return_value=False):
            self._run(["watch", cid, "--interval", "0", "--passes", "2"])
        self.assertEqual(chemin_etat.read_bytes(), avant, "watch a modifié state.json")
        self.assertEqual(chemin_etat.stat().st_mtime, avant_mtime)
        # Le rapport reste NON reconcilie : c'est tick qui le traite, jamais watch.
        self.assertEqual(store.load()["taches"][tid]["state"], "running")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Carte, phases et pourquoi
# ---------------------------------------------------------------------------


class TestMapVerb(CliTestCase):
    """cmd_map. Aucun test ici ne cree de session tmux : panes.live_ids est mocke.

    La branche --pane est couverte en mockant panes.side_window : ce qui doit etre prouve
    ici est la COMMANDE construite pour la fenetre, pas le comportement de tmux, deja
    couvert par tests/test_panes.py contre un vrai serveur.
    """

    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = self._chantier()
        self._live = mock.patch.object(cli.panes, "live_ids", return_value=set())
        self._live.start()
        self.addCleanup(self._live.stop)

    def _sortie(self) -> Path:
        return Path(self._tmp) / "carte.html"

    def test_ecrit_la_page_au_chemin_demande(self):
        self._tache(self.chantier_id, titre="0.1 a")
        chemin = self._sortie()
        code, out, _ = self._run(
            ["map", self.chantier_id, "--out", str(chemin), "--interval", "0"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(chemin.exists())
        self.assertIn("0.1 a", chemin.read_text(encoding="utf-8"))
        self.assertIn(str(chemin), out)

    def test_le_chemin_par_defaut_vit_sous_ORDO_HOME(self):
        self._tache(self.chantier_id, titre="0.1 a")
        code, out, _ = self._run(["map", self.chantier_id, "--interval", "0"])
        self.assertEqual(code, 0)
        attendu = store.home() / "map" / f"{self.chantier_id}.html"
        self.assertTrue(attendu.exists())
        self.assertIn(str(attendu), out)

    def test_un_chemin_relatif_devient_absolu(self):
        # --pane relance cette commande dans un process dont le repertoire courant n'est
        # pas garanti etre le notre : deux cwd differents ecriraient deux fichiers.
        self._tache(self.chantier_id, titre="0.1 a")
        args = mock.Mock(out="carte.html", campaign=self.chantier_id)
        self.assertTrue(cli._map_path(args).is_absolute())

    def test_json_sert_le_modele(self):
        self._tache(self.chantier_id, titre="0.1 a")
        data = self._run_json(
            ["map", self.chantier_id, "--out", str(self._sortie()),
             "--interval", "0", "--json"]
        )
        self.assertEqual(data["counts"]["total"], 1)
        self.assertEqual([g["key"] for g in data["groups"]], ["0"])

    def test_json_et_watch_sont_refuses_ensemble(self):
        # Une boucle qui rend du texte humain la ou l'appelant attend du JSON casse son
        # parseur sans un mot ; le refus est explicite (I8).
        code, _, err = self._run(
            ["map", self.chantier_id, "--json", "--watch", "--interval", "1", "--passes", "1"]
        )
        self.assertEqual(code, 1)
        self.assertIn("--json", err)

    def test_watch_reecrit_la_page_a_chaque_passe(self):
        self._tache(self.chantier_id, titre="0.1 a")
        chemin = self._sortie()
        code, out, _ = self._run(
            ["map", self.chantier_id, "--out", str(chemin),
             "--watch", "--interval", "1", "--passes", "2"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 2)
        self.assertIn('http-equiv="refresh"', chemin.read_text(encoding="utf-8"))

    def test_sans_watch_la_page_ne_se_rafraichit_pas_toute_seule(self):
        self._tache(self.chantier_id, titre="0.1 a")
        chemin = self._sortie()
        self._run(["map", self.chantier_id, "--out", str(chemin), "--interval", "0"])
        self.assertNotIn('http-equiv="refresh"', chemin.read_text(encoding="utf-8"))

    def test_watch_refuse_un_intervalle_nul(self):
        code, _, err = self._run(["map", self.chantier_id, "--watch", "--interval", "0"])
        self.assertEqual(code, 1)
        self.assertIn("--interval", err)

    def test_un_chantier_inconnu_est_refuse(self):
        code, _, err = self._run(["map", "c-99", "--interval", "0"])
        self.assertEqual(code, 1)
        self.assertIn("c-99", err)

    def test_pane_passe_ORDO_HOME_a_la_fenetre(self):
        # tmux ne transmet pas l'environnement du client a new-window : sans ce passage
        # explicite, la boucle resoudrait ~/.claude/ordo, mourrait sur "campaign not
        # found", et la fenetre se fermerait sans que rien a l'ecran ne le dise.
        self._tache(self.chantier_id, titre="0.1 a")
        with mock.patch.object(cli.panes, "side_window", return_value="@9") as ouvre:
            code, out, _ = self._run(
                ["map", self.chantier_id, "--out", str(self._sortie()), "--pane"]
            )
        self.assertEqual(code, 0)
        session, nom, commande = ouvre.call_args[0]
        self.assertEqual(nom, cli.panes.MAP_WINDOW_NAME)
        self.assertIn(f"ORDO_HOME={store.home()}", commande)
        self.assertIn("--watch", commande)
        self.assertIn(str(self._sortie()), commande)
        self.assertIn("@9", out)

    def test_pane_refuse_un_chantier_sans_session_tmux(self):
        with store.locked() as state:
            state["chantiers"][self.chantier_id]["tmuxSession"] = None
        with mock.patch.object(cli.panes, "side_window") as ouvre:
            code, _, err = self._run([
                "map", self.chantier_id, "--out", str(self._sortie()), "--pane"
            ])
        self.assertEqual(code, 1)
        self.assertIn("tmux", err)
        ouvre.assert_not_called()

    def test_tmux_absent_ne_fait_pas_echouer_la_carte(self):
        # La carte se lit sans serveur tmux ; ce qui devient inconnu, c'est la vivacite des
        # panes, et elle est alors annoncee inconnue plutot que devinee.
        self._tache(self.chantier_id, titre="0.1 a")
        with mock.patch.object(
            cli.panes, "live_ids", side_effect=RuntimeError("tmux introuvable")
        ):
            code, _, _ = self._run(
                ["map", self.chantier_id, "--out", str(self._sortie()), "--interval", "0"]
            )
        self.assertEqual(code, 0)
        self.assertTrue(self._sortie().exists())

    def test_la_carte_n_ecrit_jamais_dans_l_etat(self):
        self._tache(self.chantier_id, titre="0.1 a")
        avant = (store.home() / "state.json").read_text(encoding="utf-8")
        self._run(["map", self.chantier_id, "--out", str(self._sortie()), "--interval", "0"])
        self.assertEqual((store.home() / "state.json").read_text(encoding="utf-8"), avant)


class TestGroupAndWhyVerbs(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.chantier_id = self._chantier()

    def test_group_nomme_une_phase_et_dit_a_quoi_elle_sert(self):
        data = self._run_json(
            ["group", self.chantier_id, "0", "Socle", "--why", "base fausse, tout est faux",
             "--json"]
        )
        self.assertEqual(data["groups"]["0"]["label"], "Socle")
        self.assertEqual(data["groups"]["0"]["why"], "base fausse, tout est faux")

    def test_group_sans_why_le_signale_au_lieu_de_se_taire(self):
        code, out, _ = self._run(["group", self.chantier_id, "0", "Socle"])
        self.assertEqual(code, 0)
        self.assertIn("--why", out)

    def test_group_refuse_un_chantier_inconnu(self):
        code, _, err = self._run(["group", "c-99", "0", "Socle"])
        self.assertEqual(code, 1)
        self.assertIn("c-99", err)

    def test_add_accepte_le_pourquoi(self):
        data = self._run_json(
            ["add", self.chantier_id, "--title", "0.1 a", "--prompt", "p",
             "--why", "sans ca la suite est fausse", "--json"]
        )
        self.assertEqual(data["why"], "sans ca la suite est fausse")

    def test_add_sans_pourquoi_le_dit_a_la_creation(self):
        code, out, _ = self._run(
            ["add", self.chantier_id, "--title", "0.1 a", "--prompt", "p"]
        )
        self.assertEqual(code, 0)
        self.assertIn("ordo why t-01", out)

    def test_why_pose_la_raison_apres_coup(self):
        task_id = self._tache(self.chantier_id, titre="0.1 a")
        data = self._run_json(["why", task_id, "les index de role bougent", "--json"])
        self.assertEqual(data["why"], "les index de role bougent")

    def test_why_refuse_une_tache_inconnue(self):
        code, _, err = self._run(["why", "t-99", "x"])
        self.assertEqual(code, 1)
        self.assertIn("t-99", err)
