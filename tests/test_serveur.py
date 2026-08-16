"""Tests unitaires de ordo/serveur.py.

Aucun test ici n'ouvre le port de production : ils demandent tous le port 0, que le noyau
remplace par un port libre. Un test qui prendrait 9123 entrerait en concurrence avec le
serveur reel de la machine, et le ferait echouer une fois sur deux.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import chantier, serveur, store


class ServeurTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-serveur-test-")
        self._prev_env = {
            cle: os.environ.get(cle)
            for cle in ("ORDO_HOME", "ORDO_REGISTRY", "ORDO_NO_SERVE")
        }
        os.environ["ORDO_HOME"] = str(Path(self._tmp) / "home")
        os.environ["ORDO_REGISTRY"] = str(Path(self._tmp) / "registre.json")
        # ORDO_NO_SERVE est retire, jamais herite : ces tests portent precisement sur ce
        # que fait ensure(), et une variable posee par le shell de la machine leur ferait
        # verifier le contraire de ce qu'ils affirment, en silence. Le test de la porte de
        # sortie, lui, la pose explicitement.
        os.environ.pop("ORDO_NO_SERVE", None)

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

    def _home_avec_chantier(self, nom: str) -> str:
        """Un ORDO_HOME distinct, avec un chantier ouvert dedans. Rend son chemin."""
        home = str(Path(self._tmp) / nom)
        precedent = os.environ["ORDO_HOME"]
        os.environ["ORDO_HOME"] = home
        try:
            chantier.start(self._projet(nom), f"objectif de {nom}")
            return store.canon(home)
        finally:
            os.environ["ORDO_HOME"] = precedent


# ---------------------------------------------------------------------------
# Registre des ORDO_HOME
# ---------------------------------------------------------------------------


class TestRegistre(ServeurTestCase):
    def test_un_home_enregistre_se_relit(self):
        home = self._home_avec_chantier("a")
        serveur.register(home)
        self.assertEqual(serveur.homes(), [home])

    def test_le_meme_home_deux_fois_ne_compte_qu_une_fois(self):
        home = self._home_avec_chantier("a")
        serveur.register(home)
        serveur.register(home)
        self.assertEqual(serveur.homes(), [home])

    def test_deux_projets_coexistent_dans_le_registre(self):
        a, b = self._home_avec_chantier("a"), self._home_avec_chantier("b")
        serveur.register(a)
        serveur.register(b)
        self.assertEqual(sorted(serveur.homes()), sorted([a, b]))

    def test_un_home_efface_du_disque_disparait_du_registre(self):
        # Un projet supprime ne doit pas rester dans la liste pour toujours : la page
        # afficherait un chantier qui n'existe plus, sans moyen de s'en debarrasser.
        a = self._home_avec_chantier("a")
        serveur.register(a)
        shutil.rmtree(a)
        self.assertEqual(serveur.homes(), [])

    def test_un_registre_illisible_ne_fait_pas_echouer_la_lecture(self):
        # Le registre est un fichier de confort, jamais une source de verite. Corrompu, il
        # se remplace ; il ne doit pas empecher un serveur de demarrer.
        Path(os.environ["ORDO_REGISTRY"]).write_text("{ pas du json", encoding="utf-8")
        self.assertEqual(serveur.homes(), [])
        home = self._home_avec_chantier("a")
        serveur.register(home)
        self.assertEqual(serveur.homes(), [home])

    def test_le_registre_vit_hors_de_tout_ORDO_HOME(self):
        # S'il vivait dans un home, chaque projet aurait le sien et aucun ne verrait les
        # autres : la navigation entre chantiers, qui est tout l'objet du serveur, serait
        # impossible.
        os.environ.pop("ORDO_REGISTRY")
        chemin = serveur.registry_path()
        self.assertNotIn(str(store.home()), str(chemin))


# ---------------------------------------------------------------------------
# Vivacite et demarrage
# ---------------------------------------------------------------------------


class TestVivacite(ServeurTestCase):
    def test_un_port_libre_est_vu_comme_eteint(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertFalse(serveur.is_up(port))

    def test_un_port_pris_par_autre_chose_n_est_pas_pris_pour_ordo(self):
        # Un VRAI serveur HTTP etranger, qui repond 200 avec un corps quelconque. Un socket
        # qui se contenterait d'ecouter sans repondre echouerait en delai depasse et ne
        # prouverait rien : c'est la LECTURE de la reponse qui distingue Ordo d'autre chose,
        # et il faut donc une reponse a lire. Verifie : sans ce vrai serveur, le controle de
        # mutation declare ce test decoratif.
        class Etranger(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                corps = b'{"service":"autre-chose"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(corps)))
                self.end_headers()
                self.wfile.write(corps)

        httpd = HTTPServer(("127.0.0.1", 0), Etranger)
        port = httpd.server_address[1]
        fil = threading.Thread(target=httpd.serve_forever, daemon=True)
        fil.start()
        try:
            self.assertFalse(serveur.is_up(port))
        finally:
            httpd.shutdown()
            httpd.server_close()
            fil.join(timeout=5)


class ServeurVivantTestCase(ServeurTestCase):
    """Demarre un vrai serveur sur un port libre, dans un thread, et l'arrete apres."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self._home_avec_chantier("a")
        serveur.register(self.home)
        self.httpd = serveur.build(0)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
        super().tearDown()

    def _get(self, chemin: str, host: str | None = None):
        url = f"http://127.0.0.1:{self.port}{chemin}"
        requete = urllib.request.Request(url)
        if host:
            requete.add_header("Host", host)
        return urllib.request.urlopen(requete, timeout=5)


class TestServeurVivant(ServeurVivantTestCase):
    def test_health_repond_et_se_reconnait(self):
        self.assertTrue(serveur.is_up(self.port))

    def test_la_racine_sert_la_page(self):
        corps = self._get("/").read().decode("utf-8")
        self.assertTrue(corps.lstrip().lower().startswith("<!doctype html>"))

    def test_la_racine_sert_le_mur_des_colonnes(self):
        # La racine est l'adresse mise en favori : c'est elle qui doit ouvrir sur tous les
        # chantiers a la fois, pas sur un seul avec un menu pour changer.
        corps = self._get("/").read().decode("utf-8")
        self.assertIn('id="cols"', corps)
        self.assertIn('id="plus"', corps)

    def test_une_colonne_se_sert_avec_son_home_et_son_chantier(self):
        data = json.loads(self._get("/api/state").read())
        c = data["campaigns"][0]
        url = f"/panel?home={quote(c['home'])}&campaign={quote(c['id'])}"
        corps = self._get(url).read().decode("utf-8")
        self.assertTrue(corps.lstrip().lower().startswith("<!doctype html>"))
        self.assertIn(c["id"], corps)
        self.assertIn('id="board"', corps)

    def test_une_colonne_sans_chantier_est_refusee(self):
        # Une colonne sans cible interrogerait /api/map avec une campagne vide et
        # afficherait une page morte sans jamais dire pourquoi.
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/panel")
        self.assertEqual(cm.exception.code, 400)

    def test_l_etat_liste_les_chantiers_de_tous_les_homes_enregistres(self):
        b = self._home_avec_chantier("b")
        serveur.register(b)
        data = json.loads(self._get("/api/state").read())
        homes = {c["home"] for c in data["campaigns"]}
        self.assertEqual(homes, {self.home, b})

    def test_un_chantier_porte_de_quoi_le_choisir_sans_le_charger(self):
        # La liste sert un menu : elle doit rester legere. Le detail d'un chantier, lui,
        # pese des centaines de kilo-octets, et n'a rien a faire dans un menu.
        data = json.loads(self._get("/api/state").read())
        c = data["campaigns"][0]
        for cle in ("home", "id", "slug", "state", "total", "done", "running"):
            self.assertIn(cle, c)
        self.assertNotIn("tasks", c)

    def _question(self, texte, pour_humain=True, reponse=None):
        """Pose une question dans le home servi. Rend (chantier, question)."""
        precedent = os.environ["ORDO_HOME"]
        os.environ["ORDO_HOME"] = self.home
        try:
            with store.locked() as state:
                cid = next(iter(state["chantiers"]))
                qid = store.next_id(state, "question")
                state["questions"][qid] = {
                    "id": qid, "chantier": cid, "tache": None, "question": texte,
                    "options": [], "pourHumain": pour_humain, "answer": reponse,
                    "askedAt": store.now(),
                    "answeredAt": store.now() if reponse else None,
                }
            return cid, qid
        finally:
            os.environ["ORDO_HOME"] = precedent

    def test_le_menu_dit_combien_de_choix_attendent_l_humain(self):
        # Le mur ne charge aucune carte : sans ce compteur dans le menu, une colonne
        # sortie de l'ecran pourrait attendre un arbitrage sans que rien ne le dise.
        cid, _ = self._question("parallele ou serie ?")
        self._question("deja repondue", reponse="oui")
        self._question("pas pour l humain", pour_humain=False)
        data = json.loads(self._get("/api/state").read())
        c = next(x for x in data["campaigns"] if x["id"] == cid)
        self.assertEqual(c["asking"], 1)

    def test_un_chantier_sans_question_annonce_zero(self):
        data = json.loads(self._get("/api/state").read())
        self.assertEqual(data["campaigns"][0]["asking"], 0)

    def test_le_detail_d_un_chantier_sert_la_vue_complete(self):
        data = json.loads(self._get("/api/state").read())
        c = data["campaigns"][0]
        url = f"/api/map?home={c['home']}&campaign={c['id']}"
        vue = json.loads(self._get(url).read())
        self.assertIn("tasks", vue)
        self.assertIn("phases", vue)
        self.assertEqual(vue["campaign"]["id"], c["id"])

    def test_un_chantier_inconnu_rend_404_et_pas_une_trace(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(f"/api/map?home={self.home}&campaign=c-99")
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 404)

    def test_un_home_hors_registre_est_refuse(self):
        # Sans ce refus, le parametre home ferait lire n'importe quel state.json de la
        # machine a qui sait former une URL.
        etranger = str(Path(self._tmp) / "jamais-enregistre")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(f"/api/map?home={etranger}&campaign=c-01")
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 403)

    def test_un_en_tete_host_etranger_est_refuse(self):
        # Defense contre le rebinding DNS : un site visite dans le navigateur resout son
        # propre nom vers 127.0.0.1 et lit l'etat des chantiers. Le seul controle qui
        # tienne cote serveur est le nom que le client a demande.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/state", host="evil.example.com")
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 403)

    def test_localhost_reste_accepte(self):
        reponse = self._get("/api/state", host=f"localhost:{self.port}")
        self.assertEqual(reponse.status, 200)

    def test_la_reponse_n_autorise_aucune_origine_croisee(self):
        reponse = self._get("/api/state")
        self.assertIsNone(reponse.headers.get("Access-Control-Allow-Origin"))

    def test_un_home_devenu_illisible_ne_casse_pas_la_liste(self):
        b = self._home_avec_chantier("b")
        serveur.register(b)
        Path(b, "state.json").write_text("{ casse", encoding="utf-8")
        data = json.loads(self._get("/api/state").read())
        self.assertTrue(any(c["home"] == self.home for c in data["campaigns"]))
        self.assertTrue(any(p["home"] == b for p in data["problems"]))

    def test_le_serveur_n_ecoute_que_sur_la_boucle_locale(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")


class TestEnsure(ServeurTestCase):
    def test_ensure_ne_relance_rien_quand_le_serveur_repond(self):
        appels = []
        with mock.patch.object(serveur, "is_up", return_value=True), \
             mock.patch.object(serveur, "_spawn", side_effect=appels.append):
            etat = serveur.ensure(9999)
        self.assertEqual(appels, [])
        self.assertEqual(etat, "already-running")

    def test_ensure_relance_quand_le_serveur_est_tombe_et_attend_qu_il_reponde(self):
        # is_up rend False puis True : le premier appel decide de demarrer, le second prouve
        # que ensure() ATTEND que le port reponde avant de rendre la main. Sans cette
        # attente, l'appelant annonce une adresse qui refuse encore la connexion.
        appels = []
        with mock.patch.object(serveur, "is_up", side_effect=[False, True]), \
             mock.patch.object(serveur, "_spawn", side_effect=appels.append):
            etat = serveur.ensure(9999)
        self.assertEqual(appels, [9999])
        self.assertEqual(etat, "started")

    def test_ensure_le_dit_quand_le_serveur_ne_repond_toujours_pas(self):
        # Mentir ici enverrait le lecteur sur une page qui ne s'ouvre pas, sans un mot.
        with mock.patch.object(serveur, "is_up", return_value=False), \
             mock.patch.object(serveur, "_spawn"), \
             mock.patch.object(serveur.time, "sleep"):
            self.assertEqual(serveur.ensure(9999), "starting")

    def test_ensure_enregistre_le_home_courant_meme_si_le_serveur_tournait_deja(self):
        # Le cas normal du deuxieme chantier : le serveur tourne, mais il ne connait pas
        # encore ce projet. Sans cet enregistrement, on ne pourrait jamais naviguer vers
        # le second, et c'est exactement ce que le serveur existe pour permettre.
        home = self._home_avec_chantier("a")
        os.environ["ORDO_HOME"] = home
        with mock.patch.object(serveur, "is_up", return_value=True), \
             mock.patch.object(serveur, "_spawn"):
            serveur.ensure(9999)
        self.assertEqual(serveur.homes(), [home])


if __name__ == "__main__":
    unittest.main()


class TestWatchAllumeLeServeur(ServeurTestCase):
    """Le branchement reel : c'est `ordo watch` qui allume le tableau de bord."""

    def _run(self, argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from ordo import cli
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _chantier(self):
        return chantier.start(self._projet("w"), "objectif")["id"]

    def test_watch_appelle_ensure_et_le_dit_sur_stderr(self):
        cid = self._chantier()
        with mock.patch.object(serveur, "ensure", return_value="started") as ensure:
            code, out, err = self._run(["watch", cid, "--interval", "0"])
        self.assertEqual(code, 0)
        ensure.assert_called_once()
        self.assertIn("map http://127.0.0.1:9123/", err)

    def test_la_sortie_machine_de_watch_reste_intacte(self):
        # Le contrat de watch est UNE LIGNE PAR FAIT NOUVEAU, lue par le surveillant qui
        # reveille l'orchestratrice. Une ligne de confort sur ce canal la reveillerait pour
        # un fait qui n'en est pas un, au premier tour de chaque chantier.
        cid = self._chantier()
        with mock.patch.object(serveur, "ensure", return_value="started"):
            code, out, _ = self._run(["watch", cid, "--interval", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"idle {cid}")

    def test_no_serve_n_allume_rien(self):
        cid = self._chantier()
        with mock.patch.object(serveur, "ensure") as ensure:
            self._run(["watch", cid, "--interval", "0", "--no-serve"])
        ensure.assert_not_called()

    def test_un_serveur_qui_refuse_de_demarrer_ne_tue_pas_la_veille(self):
        # Un tableau de bord absent est un desagrement ; une veille morte est un chantier
        # a l'arret, parce que plus rien ne ramene l'orchestratrice.
        cid = self._chantier()
        with mock.patch.object(serveur, "ensure", side_effect=OSError("port pris")):
            code, out, err = self._run(["watch", cid, "--interval", "0"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"idle {cid}")
        self.assertIn("map unavailable", err)


class TestPorteDeSortie(ServeurTestCase):
    def test_ORDO_NO_SERVE_n_inscrit_rien_et_ne_demarre_rien(self):
        # Un demarrage automatique de daemon est exactement ce qu'une suite de tests ou une
        # machine partagee ne doit pas subir.
        os.environ["ORDO_NO_SERVE"] = "1"
        self.addCleanup(os.environ.pop, "ORDO_NO_SERVE", None)
        with mock.patch.object(serveur, "_spawn") as spawn:
            self.assertEqual(serveur.ensure(9999), "disabled")
        spawn.assert_not_called()
        self.assertEqual(serveur.homes(), [])

    def test_une_inscription_purge_les_homes_disparus(self):
        # Sans purge, le registre ne fait que grossir : une seule execution de la suite y
        # avait laisse dix-huit repertoires temporaires effaces depuis.
        mort = str(Path(self._tmp) / "efface")
        Path(mort).mkdir()
        Path(mort, "state.json").write_text("{}", encoding="utf-8")
        serveur.register(mort)
        shutil.rmtree(mort)
        vivant = self._home_avec_chantier("a")
        serveur.register(vivant)
        self.assertEqual(list(serveur._lire_registre()["homes"]), [vivant])
