"""Tests unitaires de ordo/serveur.py.

Aucun test ici n'ouvre le port de production : ils demandent tous le port 0, que le noyau
remplace par un port libre. Un test qui prendrait 9123 entrerait en concurrence avec le
serveur reel de la machine, et le ferait echouer une fois sur deux.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
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
        for cle in ("home", "id", "slug", "state", "total", "done", "running", "ready"):
            self.assertIn(cle, c)
        self.assertNotIn("tasks", c)

    def _tache(self, titre, depends_on=()):
        """Ajoute une tâche au chantier unique de self.home. Rend la tâche créée."""
        precedent = os.environ["ORDO_HOME"]
        os.environ["ORDO_HOME"] = self.home
        try:
            with store.locked() as state:
                cid = next(iter(state["chantiers"]))
            return chantier.add_task(cid, titre, "prompt", depends_on=depends_on)
        finally:
            os.environ["ORDO_HOME"] = precedent

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

    def test_un_chantier_sans_tache_n_a_rien_de_lancable(self):
        data = json.loads(self._get("/api/state").read())
        self.assertEqual(data["campaigns"][0]["ready"], 0)

    def test_le_menu_dit_combien_de_taches_sont_lancables(self):
        # C'est le chiffre qui distingue une campagne à l'arrêt d'une campagne qui avance :
        # sans lui, une colonne sans exécutante ne dit pas si quelque chose peut repartir.
        self._tache("première")
        data = json.loads(self._get("/api/state").read())
        self.assertEqual(data["campaigns"][0]["ready"], 1)

    def test_une_tache_dont_la_dependance_n_est_pas_faite_ne_compte_pas_lancable(self):
        a = self._tache("a")
        self._tache("b", depends_on=[a["id"]])
        data = json.loads(self._get("/api/state").read())
        # a est lançable, b non : sa dépendance n'est pas encore "done". Le compte doit
        # venir de chantier.ready(), pas d'un total de tâches "queued".
        self.assertEqual(data["campaigns"][0]["ready"], 1)

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


class TestEtatPorteLeQuota(ServeurVivantTestCase):
    """La consommation du compte Claude, dans /api/state -- voir ordo/quota.py.

    snapshot() ne fait qu'exposer quota.lire() tel quel : ces tests portent sur la
    traversée, pas sur l'interprétation du fichier, déjà couverte par
    tests/test_quota.py.
    """

    def setUp(self) -> None:
        super().setUp()
        self._quota_tmp = Path(self._tmp) / "quota"
        self._prev_quota = os.environ.get("ORDO_QUOTA_FILE")
        os.environ["ORDO_QUOTA_FILE"] = str(self._quota_tmp)

    def tearDown(self) -> None:
        if self._prev_quota is None:
            os.environ.pop("ORDO_QUOTA_FILE", None)
        else:
            os.environ["ORDO_QUOTA_FILE"] = self._prev_quota
        super().tearDown()

    def test_un_quota_lisible_traverse_snapshot(self):
        maintenant = int(time.time())
        self._quota_tmp.write_text(
            f"34 50 {maintenant} {maintenant + 3600} {maintenant + 3 * 86400}\n",
            encoding="utf-8",
        )
        data = json.loads(self._get("/api/state").read())
        self.assertIsNotNone(data["quota"])
        cles = {f["cle"] for f in data["quota"]["fenetres"]}
        self.assertEqual(cles, {"5h", "7j"})

    def test_un_quota_absent_traverse_null(self):
        # Le fichier n'existe pas encore : la page doit recevoir null, jamais une
        # jauge inventée à zéro.
        data = json.loads(self._get("/api/state").read())
        self.assertIsNone(data["quota"])


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


# ---------------------------------------------------------------------------
# Rechargement de carte.py sans redémarrage
# ---------------------------------------------------------------------------
#
# Ces tests ne touchent JAMAIS le vrai ordo/carte.py : une autre exécutante y travaille en
# même temps. Ils font tenir à `serveur.carte` un faux module, chargé depuis un fichier
# temporaire que chaque test peut réécrire à volonté -- y compris avec du code cassé -- sans
# le moindre risque pour le reste du dépôt.

_CARTE_V1 = (
    'def page(poll=0):\n'
    '    return "<!doctype html><html><body>carte v1</body></html>"\n'
    '\n'
    'def panneau(home, campaign, poll=0):\n'
    '    return "<!doctype html><html><body>panel v1 " + campaign + "</body></html>"\n'
    '\n'
    'def model(campaign, alive=None, usage_de=None):\n'
    '    return {"campaign": {"id": campaign}, "version": "v1"}\n'
    '\n'
    'def vue(m):\n'
    '    return {"tasks": [], "phases": [], "campaign": m["campaign"], "version": m["version"]}\n'
)

_CARTE_V2 = _CARTE_V1.replace("v1", "v2")

# Erreur de syntaxe volontaire : une chaîne jamais refermée, exactement le genre d'accident
# qu'un fichier à moitié écrit produit.
_CARTE_CASSEE = 'def page(poll=0):\n    return "<!doctype html>\n'


class TestRechargementCarte(ServeurVivantTestCase):
    """t-30 : la page servie reflète le code présent sur le disque, sans redémarrage.

    Le faux module vit dans un dossier ajouté à sys.path et s'importe par son nom, comme
    ordo.carte s'importe réellement : importlib.reload() a besoin de retrouver le spec du
    module par ce chemin, un module chargé à la main via spec_from_file_location() ne lui
    suffit pas. sys.dont_write_bytecode coupe le cache .pyc pour la durée du test : son
    horodatage ne garde que la seconde, et deux versions écrites dans la même seconde
    partageraient sinon un cache périmé.
    """

    def setUp(self) -> None:
        super().setUp()
        self._carte_dir = Path(self._tmp) / "faux_carte"
        self._carte_dir.mkdir()
        self._nom_module = f"carte_fake_{id(self)}"
        self._carte_fichier = self._carte_dir / f"{self._nom_module}.py"
        self._carte_fichier.write_text(_CARTE_V1, encoding="utf-8")
        self._mtime_ns = 1_000_000_000
        os.utime(self._carte_fichier, ns=(self._mtime_ns, self._mtime_ns))
        self._bytecode_avant = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(self._carte_dir))
        importlib.invalidate_caches()
        self._vrai_carte = serveur.carte
        serveur.carte = importlib.import_module(self._nom_module)
        # _modules_page() est patché pour ne porter QUE le faux carte : sans ce patch, le
        # cycle de rechargement irait aussi recharger les vrais chantier/journal/store/
        # routage/usage, alors que d'autres exécutantes écrivent certains de ces fichiers
        # au moment même où ce test tourne.
        self._patch_modules = mock.patch.object(
            serveur, "_modules_page", return_value=(serveur.carte,)
        )
        self._patch_modules.start()

    def tearDown(self) -> None:
        module_factice = serveur.carte
        self._patch_modules.stop()
        serveur.carte = self._vrai_carte
        serveur._mtimes.pop(module_factice, None)
        serveur._erreurs.pop(module_factice, None)
        sys.modules.pop(self._nom_module, None)
        try:
            sys.path.remove(str(self._carte_dir))
        except ValueError:
            pass
        sys.dont_write_bytecode = self._bytecode_avant
        super().tearDown()

    def _ecrire_carte(self, contenu: str) -> None:
        """Réécrit le faux carte.py avec un mtime garanti différent du précédent."""
        self._mtime_ns += 1_000_000
        self._carte_fichier.write_text(contenu, encoding="utf-8")
        os.utime(self._carte_fichier, ns=(self._mtime_ns, self._mtime_ns))

    def test_la_page_reflete_le_fichier_modifie_sans_action_humaine(self):
        # c2 : le code sur le disque, pas celui chargé au démarrage du process.
        corps = self._get("/").read().decode("utf-8")
        self.assertIn("carte v1", corps)
        self._ecrire_carte(_CARTE_V2)
        corps = self._get("/").read().decode("utf-8")
        self.assertIn("carte v2", corps)
        self.assertNotIn("carte v1", corps)

    def test_aucun_redemarrage_n_a_lieu_pour_voir_le_changement(self):
        # c3 : même thread, même port, du début à la fin -- la seule action est une requête
        # HTTP ordinaire, jamais un arrêt/relance du serveur.
        thread_avant, port_avant = self._thread.ident, self.port
        self._get("/").read()
        self._ecrire_carte(_CARTE_V2)
        corps = self._get("/").read().decode("utf-8")
        self.assertIn("carte v2", corps)
        self.assertEqual(self._thread.ident, thread_avant)
        self.assertEqual(self.port, port_avant)
        self.assertTrue(self._thread.is_alive())

    def test_une_source_cassee_sert_une_erreur_visible_sans_tuer_le_serveur(self):
        # c5 : le cas qui se produira -- une exécutante en plein milieu d'une écriture.
        self._ecrire_carte(_CARTE_CASSEE)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/")
        self.assertEqual(ctx.exception.code, 500)
        corps = ctx.exception.read().decode("utf-8")
        ctx.exception.close()
        self.assertIn("SyntaxError", corps)
        # Le serveur répond toujours -- l'erreur s'est servie, elle ne l'a pas tué.
        self.assertTrue(serveur.is_up(self.port))
        # Et la carte revient d'elle-même dès que le fichier redevient valide.
        self._ecrire_carte(_CARTE_V2)
        corps = self._get("/").read().decode("utf-8")
        self.assertIn("carte v2", corps)

    def test_une_source_cassee_sur_api_map_rend_du_json_pas_une_trace(self):
        # Même garantie sur la route JSON qu'interroge chaque colonne du mur.
        data = json.loads(self._get("/api/state").read())
        c = data["campaigns"][0]
        self._ecrire_carte(_CARTE_CASSEE)
        url = f"/api/map?home={quote(c['home'])}&campaign={quote(c['id'])}"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(url)
        self.assertEqual(ctx.exception.code, 500)
        data = json.loads(ctx.exception.read())
        ctx.exception.close()
        self.assertIn("carte.py", data["error"])

    def test_des_requetes_concurrentes_ne_cassent_rien_pendant_un_reload(self):
        # c6 : plusieurs requêtes à la fois, un reload qui alterne en continu pendant ce
        # temps -- aucune ne doit jamais recevoir une réponse coupée ou mélangée.
        #
        # Quatre lecteurs, pas huit : mesuré isolément (writer coupé), ThreadingHTTPServer
        # rend déjà des "Connection reset by peer" à partir de six clients qui martèlent le
        # port en boucle serrée, sans le moindre reload en jeu -- une limite de http.server
        # sous cette charge, pas une régression de _carte_a_jour(). Ce test cible ce que
        # _carte_a_jour() doit garantir : une réponse jamais coupée ni mélangée pendant un
        # reload concurrent, pas la tenue de http.server en rafale.
        arret = threading.Event()
        erreurs: list[str] = []

        def ecrivain() -> None:
            n = 0
            while not arret.is_set():
                n += 1
                self._ecrire_carte(_CARTE_V2 if n % 2 else _CARTE_V1)

        def lecteur() -> None:
            try:
                for _ in range(15):
                    corps = self._get("/").read().decode("utf-8")
                    if "carte v1" not in corps and "carte v2" not in corps:
                        erreurs.append(f"réponse incohérente: {corps[:200]!r}")
            except Exception as exc:  # noqa: BLE001 - le test veut voir toute casse
                erreurs.append(f"{type(exc).__name__}: {exc}")

        fil_ecrivain = threading.Thread(target=ecrivain, daemon=True)
        fils_lecteurs = [threading.Thread(target=lecteur) for _ in range(4)]
        fil_ecrivain.start()
        for f in fils_lecteurs:
            f.start()
        for f in fils_lecteurs:
            f.join(timeout=30)
        arret.set()
        fil_ecrivain.join(timeout=5)
        self.assertEqual(erreurs, [])
        self.assertTrue(serveur.is_up(self.port))


# ---------------------------------------------------------------------------
# Rechargement des modules dont carte.py dépend (t-37)
# ---------------------------------------------------------------------------
#
# Panne réelle du 2026-08-16 : carte.py était rechargé, chantier.py non, et le serveur a
# fini par exécuter un carte.py de deux minutes contre un chantier.py de trois heures --
# jusqu'à un 500 AttributeError sur une fonction pourtant bien écrite. Ces tests ne
# touchent JAMAIS le vrai ordo/chantier.py ni le vrai ordo/carte.py : une autre exécutante
# y travaille en même temps. Deux faux modules jouent leurs rôles -- une "dépendance"
# factice, et une "carte" factice qui l'importe et appelle sa fonction, exactement comme
# le vrai carte.py appelait chantier.ecart_estime_reel().

_DEP_V1 = 'def valeur():\n    return "dep v1"\n'
_DEP_V2 = 'def valeur():\n    return "dep v2"\n'
# Erreur de syntaxe volontaire, comme _CARTE_CASSEE plus haut.
_DEP_CASSE = 'def valeur():\n    return "dep casse\n'

_CARTE_CHAINE_TMPL = (
    'import {nom_dep}\n'
    '\n'
    'def page(poll=0):\n'
    '    return "<!doctype html><html><body>" + {nom_dep}.valeur() + "</body></html>"\n'
)


class TestRechargementChaine(ServeurVivantTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._dir = Path(self._tmp) / "faux_chaine"
        self._dir.mkdir()
        self._nom_dep = f"dep_fake_{id(self)}"
        self._nom_carte = f"carte_chaine_fake_{id(self)}"
        self._fichier_dep = self._dir / f"{self._nom_dep}.py"
        self._fichier_carte = self._dir / f"{self._nom_carte}.py"
        self._fichier_dep.write_text(_DEP_V1, encoding="utf-8")
        self._fichier_carte.write_text(
            _CARTE_CHAINE_TMPL.format(nom_dep=self._nom_dep), encoding="utf-8"
        )
        self._mtimes_fake = {"dep": 1_000_000_000, "carte": 1_000_000_000}
        os.utime(self._fichier_dep, ns=(self._mtimes_fake["dep"],) * 2)
        os.utime(self._fichier_carte, ns=(self._mtimes_fake["carte"],) * 2)
        self._bytecode_avant = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(self._dir))
        importlib.invalidate_caches()
        self._vrai_carte = serveur.carte
        self._dep = importlib.import_module(self._nom_dep)
        serveur.carte = importlib.import_module(self._nom_carte)
        # Ordre : dep avant carte, comme _modules_page() place chantier avant carte --
        # carte est seul à dépendre de l'autre.
        self._patch_modules = mock.patch.object(
            serveur, "_modules_page", return_value=(self._dep, serveur.carte)
        )
        self._patch_modules.start()

    def tearDown(self) -> None:
        dep, carte_factice = self._dep, serveur.carte
        self._patch_modules.stop()
        serveur.carte = self._vrai_carte
        serveur._mtimes.pop(dep, None)
        serveur._erreurs.pop(dep, None)
        serveur._mtimes.pop(carte_factice, None)
        serveur._erreurs.pop(carte_factice, None)
        sys.modules.pop(self._nom_dep, None)
        sys.modules.pop(self._nom_carte, None)
        try:
            sys.path.remove(str(self._dir))
        except ValueError:
            pass
        sys.dont_write_bytecode = self._bytecode_avant
        super().tearDown()

    def _ecrire(self, cle: str, fichier: Path, contenu: str) -> None:
        """Réécrit un des deux faux fichiers avec un mtime garanti différent du précédent."""
        self._mtimes_fake[cle] += 1_000_000
        fichier.write_text(contenu, encoding="utf-8")
        os.utime(fichier, ns=(self._mtimes_fake[cle],) * 2)

    def test_une_fonction_ajoutee_dans_le_module_dependant_est_servie_sans_redemarrage(self):
        # c6 : le test qui aurait attrapé la panne -- une fonction neuve dans un module
        # AUTRE que carte.py, appelée depuis carte.py, alors que carte.py lui-même ne
        # change pas.
        corps = self._get("/").read().decode("utf-8")
        self.assertIn("dep v1", corps)
        self._ecrire("dep", self._fichier_dep, _DEP_V2)
        corps = self._get("/").read().decode("utf-8")
        self.assertIn("dep v2", corps)
        self.assertNotIn("dep v1", corps)

    def test_un_module_dependant_casse_bloque_la_chaine_sans_tuer_le_serveur(self):
        # c3 + c5 : l'échec du module dont dépend carte.py arrête la chaîne avant carte.py
        # (l'ordre protège contre un mélange de versions), se sert en erreur explicite (le
        # garde-fou de t-30, étendu), et se répare seul dès que le fichier redevient valide.
        self._ecrire("dep", self._fichier_dep, _DEP_CASSE)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/")
        self.assertEqual(ctx.exception.code, 500)
        corps = ctx.exception.read().decode("utf-8")
        ctx.exception.close()
        self.assertIn(self._nom_dep, corps)
        self.assertIn("SyntaxError", corps)
        self.assertTrue(serveur.is_up(self.port))
        self._ecrire("dep", self._fichier_dep, _DEP_V2)
        corps = self._get("/").read().decode("utf-8")
        self.assertIn("dep v2", corps)

    def test_le_module_dependant_change_seul_ne_derange_pas_carte(self):
        # Le sens inverse de l'ordre : carte.py n'a pas changé, seule sa dépendance a
        # bougé -- ça doit suffire, sans qu'il soit besoin de retoucher carte.py pour
        # déclencher quoi que ce soit.
        self._ecrire("dep", self._fichier_dep, _DEP_V2)
        corps = self._get("/").read().decode("utf-8")
        self.assertIn("dep v2", corps)


class TestCoutRechargement(unittest.TestCase):
    """c4 : le coût mesuré à l'ajout d'un seul module (~0,2 ms, t-30) doit rester
    négligeable une fois les six modules réels dont dépend la page inclus dans le cycle.

    Mesuré à l'écriture sur les six modules réels (store, routage, usage, chantier,
    journal, carte) : environ 0,02 ms/appel en régime stable, contre environ 0,61 ms quand
    les six rechargent à la fois. La borne ci-dessous laisse une marge large contre le
    bruit de la machine qui exécute le test, tout en restant nettement sous le coût d'un
    rechargement complet -- de quoi rougir si le code se met à recharger à chaque appel au
    lieu de se contenter d'un stat().
    """

    def test_regime_stable_reste_negligeable(self):
        with serveur._carte_a_jour():
            pass  # chauffe : aligne le mtime connu de chaque module sur le disque
        n = 500
        debut = time.perf_counter()
        for _ in range(n):
            with serveur._carte_a_jour():
                pass
        duree = (time.perf_counter() - debut) / n
        self.assertLess(duree, 0.0002, f"{duree * 1000:.4f} ms/appel")
