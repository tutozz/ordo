"""Serveur local unique qui sert les cartes de TOUS les chantiers en cours.

Pourquoi un serveur alors qu'une page ecrite sur disque suffisait. Trois raisons, toutes
mesurees sur l'usage reel.

Un fichier par chantier oblige a savoir OU il est. Trois projets ouverts font trois
fichiers dans trois ORDO_HOME differents, et il n'existait aucun endroit d'ou les voir
tous. Le serveur en est un, a une adresse fixe qu'on peut mettre en favori.

Une page rafraichie par meta refresh perd le defilement, la tache ouverte, la recherche en
cours. Servie en HTTP, elle interroge son propre serveur et se met a jour sans se
recharger : l'etat de lecture survit.

Enfin un fichier ne dit jamais qu'il est perime. Une carte figee ressemble trait pour trait
a une carte a jour, et c'est exactement le defaut qui a coute une demi-heure de doute.

DEMARRAGE. Personne ne lance ce serveur a la main : `ordo watch`, la veille qu'une
orchestratrice arme de toute facon, appelle ensure() qui le demarre s'il ne repond pas. Le
premier chantier de la journee l'allume, les suivants le trouvent debout et ne font que
s'inscrire au registre. S'il meurt, le prochain `watch` le rallume.

REGISTRE. Le serveur doit voir plusieurs ORDO_HOME, donc la liste ne peut pas vivre dans
l'un d'eux. Elle vit dans ~/.claude/ordo-serve.json, et n'est qu'un fichier de confort :
corrompu il se remplace, un home efface en sort tout seul.

CE QUE CE SERVEUR N'EST PAS. Il ne pilote rien. Aucune route n'ecrit, ni dans state.json ni
ailleurs ; il n'y a pas de POST. Un tableau de bord qui pourrait lancer une executante
serait une commande a distance sans authentification sur une machine de developpement.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import carte, chantier, panes, store, usage

# Port fixe, et fixe volontairement. Un port tire au hasard obligerait a le retrouver a
# chaque fois, ce qui interdit le favori qui fait tout l'interet de la chose.
PORT = 9123

# Deux noms seulement dans l'en-tete Host. Voir _host_ok() pour le defaut que ca ferme.
HOSTS_AUTORISES = ("127.0.0.1", "localhost", "[::1]")

# Delai du controle de vivacite. Court : il est paye a chaque `ordo watch`, et un serveur
# local qui ne repond pas en une seconde est un serveur mort.
HEALTH_TIMEOUT = 1.0

# Signature rendue par /health. Un port peut etre occupe par n'importe quel programme ;
# c'est la reponse, jamais l'ouverture du port, qui prouve que c'est bien Ordo.
SIGNATURE = "ordo-map-server"

# os.environ n'est pas thread-safe et ORDO_HOME est global au process. Le serveur lit
# plusieurs homes, potentiellement en meme temps : ce verrou est ce qui empeche deux
# requetes concurrentes de se voler leur home en plein milieu d'un store.load().
_VERROU = threading.Lock()


def registry_path() -> Path:
    """Le registre des ORDO_HOME connus. Hors de tout ORDO_HOME, par construction."""
    raw = os.environ.get("ORDO_REGISTRY")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".claude" / "ordo-serve.json"


def _lire_registre() -> dict:
    chemin = registry_path()
    try:
        data = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"homes": {}}
    if not isinstance(data, dict) or not isinstance(data.get("homes"), dict):
        return {"homes": {}}
    return data


def homes() -> list[str]:
    """Les ORDO_HOME connus qui existent encore sur le disque, tries.

    Le filtre sur l'existence n'est pas une precaution : un projet efface resterait sinon
    dans le menu pour toujours, sans aucun moyen de l'en sortir.
    """
    registre = _lire_registre()
    return sorted(h for h in registre["homes"] if Path(h, "state.json").exists())


def register(home: str | Path) -> None:
    """Inscrit un ORDO_HOME au registre. Idempotent, et sans consequence s'il echoue.

    Une ecriture ratee ne doit jamais faire echouer la commande qui l'a demandee : ne pas
    apparaitre dans un menu est un desagrement, ne pas pouvoir lancer sa veille est un
    chantier a l'arret.

    Chaque ecriture purge les homes disparus. Sans cette purge le fichier ne fait que
    grossir : mesure faite, une seule execution de la suite de tests y avait laisse
    dix-huit repertoires temporaires effaces depuis longtemps.
    """
    canon = store.canon(home)
    if not canon:
        return
    registre = _lire_registre()
    registre["homes"] = {
        h: quand for h, quand in registre["homes"].items()
        if Path(h, "state.json").exists()
    }
    registre["homes"][canon] = store.now()
    chemin = registry_path()
    with suppress(OSError):
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(
            json.dumps(registre, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


def is_up(port: int = PORT) -> bool:
    """True seulement si un serveur ORDO repond sur ce port.

    La signature est lue, pas seulement le port teste. Un port ouvert par un autre
    programme repondrait autre chose, et le prendre pour Ordo ferait renoncer a demarrer un
    serveur qui n'existe pas : la page ne s'ouvrirait jamais, et rien ne dirait pourquoi.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=HEALTH_TIMEOUT
        ) as reponse:
            return json.loads(reponse.read()).get("service") == SIGNATURE
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def _spawn(port: int) -> None:
    """Demarre le serveur dans un process detache qui survit a son lanceur.

    Detache, parce que celui qui l'allume est un `ordo watch` qui s'arretera bien avant la
    fin de la journee, et que le serveur doit rester debout pour les chantiers suivants.
    start_new_session le sort du groupe de processus de son parent, donc un Ctrl-C sur la
    veille ne l'emporte pas avec elle.
    """
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parent.parent / "bin" / "ordo"),
         "serve", "--foreground", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure(port: int = PORT) -> str:
    """Inscrit le home courant, et demarre le serveur s'il ne repond pas.

    L'inscription a lieu DANS LES DEUX CAS, et c'est le cas frequent qui l'exige : le
    serveur tourne deja pour un autre projet, il ne connait pas celui-ci, et sans cette
    ligne on ne pourrait jamais naviguer vers le second chantier.

    ORDO_NO_SERVE coupe tout, sans rien inscrire ni rien demarrer. Cette porte existe parce
    qu'un demarrage automatique de daemon est exactement ce qu'on ne veut pas voir arriver
    depuis une suite de tests ou une machine partagee : mesure faite, la suite de ce depot
    avait allume un vrai serveur sur le port de production avant que cette variable existe.
    """
    if os.environ.get("ORDO_NO_SERVE"):
        return "disabled"
    register(store.home())
    if is_up(port):
        return "already-running"
    _spawn(port)
    # Laisse au process le temps d'ouvrir son port avant de rendre la main, sinon
    # l'appelant annonce une adresse qui refuse encore la connexion.
    for _ in range(20):
        time.sleep(0.1)
        if is_up(port):
            return "started"
    return "starting"


@contextmanager
def _dans(home: str):
    """Execute le bloc avec ORDO_HOME pointant sur `home`, puis restaure.

    C'est le seul mecanisme qu'ordo ait pour viser un autre home, et il passe par une
    variable d'environnement globale au process. D'ou le verrou : sans lui, deux requetes
    simultanees sur deux projets liraient chacune le state.json de l'autre.
    """
    with _VERROU:
        precedent = os.environ.get("ORDO_HOME")
        os.environ["ORDO_HOME"] = home
        try:
            yield
        finally:
            if precedent is None:
                os.environ.pop("ORDO_HOME", None)
            else:
                os.environ["ORDO_HOME"] = precedent


def _resume_home(home: str) -> tuple[list[dict], dict | None]:
    """Ligne de menu de chaque chantier d'un home, sans jamais charger son detail.

    Un chantier de soixante taches pese plusieurs centaines de kilo-octets une fois sa vue
    construite ; en charger trois pour afficher un menu de trois lignes serait absurde, et
    la liste est relue a chaque battement.
    """
    try:
        with _dans(home):
            state = store.load()
            resume = []
            for ch in state["chantiers"].values():
                taches = [
                    t for t in state["taches"].values() if t["chantier"] == ch["id"]
                ]
                resume.append({
                    "home": home,
                    "id": ch["id"],
                    "slug": ch.get("slug") or "",
                    "project": ch.get("project") or "",
                    "state": ch["state"],
                    "tmuxSession": ch.get("tmuxSession") or "",
                    "total": len(taches),
                    "done": sum(1 for t in taches if t["state"] == "done"),
                    "running": sum(1 for t in taches if t["state"] == "running"),
                    "createdAt": ch.get("createdAt"),
                })
            return resume, None
    except Exception as exc:  # noqa: BLE001 - un home casse ne doit pas casser le menu
        return [], {"home": home, "error": f"{type(exc).__name__}: {exc}"}


def snapshot() -> dict:
    """L'etat servi a la page : un menu de chantiers, et ce qui n'a pas pu etre lu.

    Les homes illisibles sortent dans "problems" au lieu d'etre tus. Un projet absent du
    menu sans explication se lit comme un projet termine, ce qu'il n'est pas.
    """
    campagnes: list[dict] = []
    problemes: list[dict] = []
    for home in homes():
        resume, probleme = _resume_home(home)
        campagnes.extend(resume)
        if probleme:
            problemes.append(probleme)
    campagnes.sort(key=lambda c: (c["state"] != "open", c["slug"], c["id"]))
    return {
        "service": SIGNATURE,
        "campaigns": campagnes,
        "problems": problemes,
        "at": store.now(),
    }


def carte_de(home: str, campaign: str) -> dict:
    """La vue complete d'un chantier. Refuse un home hors registre.

    Sans ce refus, le parametre `home` ferait lire n'importe quel state.json de la machine a
    qui sait former une URL, y compris depuis une page ouverte dans le navigateur.
    """
    if store.canon(home) not in homes():
        raise PermissionError(f"home not registered: {home}")
    with _dans(store.canon(home)):
        return carte.vue(carte.model(campaign, alive=_vivants(), usage_de=usage.pour))


def _vivants() -> Callable[[str], bool] | None:
    """Verificateur de panes vivants, ou None si tmux n'est pas la.

    Un serveur qui tourne sur une machine sans tmux doit continuer a servir des cartes :
    ce qui devient inconnu, c'est la vivacite d'un pane, pas le graphe.
    """
    try:
        ids = panes.live_ids()
    except RuntimeError:
        return None
    return lambda pane_id: pane_id in ids


class _Handler(BaseHTTPRequestHandler):
    server_version = "ordo"
    sys_version = ""

    def log_message(self, format, *args):  # noqa: A002 - signature imposee par la stdlib
        """Silence. Le serveur tourne detache, ses journaux ne vont nulle part, et une
        page qui interroge toutes les trois secondes en produirait des milliers."""

    def _host_ok(self) -> bool:
        """Refuse tout en-tete Host qui ne soit pas la boucle locale.

        C'est la seule defense qui tienne contre le rebinding DNS : un site visite dans le
        navigateur fait resoudre son propre nom vers 127.0.0.1, puis lit l'etat des
        chantiers avec les droits de la page. Ecouter sur la boucle locale ne suffit pas,
        parce que la requete part bel et bien de la machine. Le nom demande, lui, trahit
        l'attaque.
        """
        hote = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return hote in ("127.0.0.1", "localhost", "::1")

    def _envoyer(self, code: int, corps: bytes, mime: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        # Aucun Access-Control-Allow-Origin, deliberement : une page d'une autre origine
        # peut emettre la requete, elle ne peut pas en lire la reponse.
        self.end_headers()
        self.wfile.write(corps)

    def _json(self, code: int, data: object) -> None:
        self._envoyer(
            code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:  # noqa: N802 - nom impose par la stdlib
        if not self._host_ok():
            self._json(403, {"error": "host not allowed"})
            return
        route = urlparse(self.path)
        params = parse_qs(route.query)

        if route.path == "/health":
            self._json(200, {"service": SIGNATURE, "at": store.now()})
            return
        if route.path in ("/", "/index.html"):
            self._envoyer(200, carte.page().encode("utf-8"), "text/html; charset=utf-8")
            return
        if route.path == "/api/state":
            self._json(200, snapshot())
            return
        if route.path == "/api/map":
            home = (params.get("home") or [""])[0]
            campaign = (params.get("campaign") or [""])[0]
            try:
                self._json(200, carte_de(home, campaign))
            except PermissionError as exc:
                self._json(403, {"error": str(exc)})
            except chantier.ChantierError as exc:
                self._json(404, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - frontiere HTTP, voir docstring
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(404, {"error": "unknown route"})


def build(port: int = PORT) -> ThreadingHTTPServer:
    """Cree le serveur, lie a la boucle locale uniquement.

    127.0.0.1 et pas 0.0.0.0 : l'etat d'un chantier contient les prompts, les chemins du
    projet et les notes de l'orchestratrice. Rien de tout cela n'a de raison de quitter la
    machine, et un serveur de developpement qui ecoute sur toutes les interfaces finit un
    jour sur un reseau qu'il ne connait pas.
    """
    serveur = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    serveur.daemon_threads = True
    return serveur


def serve(port: int = PORT) -> None:
    """Boucle du serveur, jusqu'a interruption. Utilise par `ordo serve --foreground`."""
    httpd = build(port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def stop(port: int = PORT) -> bool:
    """Arrete le serveur en tuant ce qui ecoute sur le port. True s'il y avait quelqu'un.

    Passe par lsof plutot que par un fichier de pid : un pid ecrit sur disque ment des
    qu'un process meurt sans se nettoyer, et un serveur detache meurt exactement comme ca.
    Ce qui ecoute vraiment sur le port, lui, ne ment jamais.
    """
    if not is_up(port):
        return False
    resultat = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True, text=True,
    )
    tue = False
    for ligne in resultat.stdout.split():
        with suppress(ValueError, ProcessLookupError, PermissionError):
            os.kill(int(ligne), 15)
            tue = True
    return tue


def _port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
