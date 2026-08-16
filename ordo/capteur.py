"""Contrat du capteur : installation, execution bornee, signal filtre, adoption.

Un capteur est un script externe, ecrit par l'orchestratrice pour un chantier donne,
qui distingue ce qui est MESURE (le disque, le reseau, un conteneur) de ce qui est
DECLARE (ce qu'un document ou une executante affirme). Voir
skills/ordo/references/capteur.md pour le contrat de sortie complet et un exemple.

Cle de stockage, non negociable : toute l'etat d'un capteur vit sous
chantier["capteur"], indexe par chantier["id"] (ex. "c-01"), jamais par le chemin du
projet. Un chemin peut changer de casse, de lien symbolique ou de trailing slash sans
que canon() soit applique partout de la meme facon ; une cle qui divergerait ne
leverait aucune erreur, elle rendrait juste la detection muette. L'identifiant de
chantier est stable par construction (store.next_id) : c'est la seule cle utilisee
dans ce module, du debut a la fin.

Respect du shebang : run() invoque le script directement (jamais prefixe par sh ou
bash), afin que l'OS lise sa premiere ligne et choisisse le bon interprete. Un
lanceur qui supposerait sh ferait echouer silencieusement tout capteur non-shell a
chaque cycle : defaut deja trouve en revue dans le projet d'origine.

Invariant I9 : measured et declared ne se melangent jamais, a aucun etage. Ce module
les transporte comme deux listes distinctes de bout en bout : dans le run() brut,
dans lastSuccess, et dans la vue filtree de status(). Aucune fonction ici ne les
fusionne ni ne les reindexe par un nom commun.

Invariant I10 : aucune valeur par defaut. _parse_output() refuse un JSON auquel il
manque une des cles mesured/declared/drift/unknown : la cle manquante est nommee dans
l'erreur, jamais comblee par une liste vide silencieuse. Un capteur qui oublie une
cle doit casser visiblement, pas mentir par omission.

Invariant I12 : status() sert le signal FILTRE, jamais l'etat brut. Tant que le
capteur n'est pas adopte, status() ne rend que {"adopted": False, "signal": "unknown"}
et ne laisse fuiter aucune mesure, meme si des executions ont deja reussi. Servir
lastSuccess avant l'adoption ferait passer pour une mesure ce qu'un capteur non
valide a produit.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from shutil import copyfile

from . import store

# Sortie bornee a 256 Kio : un capteur qui inonde stdout ne doit ni saturer la
# memoire d'Ordo ni retenir le cycle plus longtemps que le timeout dur.
MAX_OUTPUT_BYTES = 256 * 1024

# Timeout par defaut d'une execution, en secondes. Un capteur lent ne bloque jamais
# le cycle au-dela de ce delai : il est tue et l'execution compte comme un echec.
DEFAULT_TIMEOUT = 20.0

# Nombre d'executions conservees dans l'historique d'un capteur. La concordance et
# la detection de figement ne regardent jamais plus loin que les 3-4 dernieres ; le
# reste ne sert qu'a l'audit, borne pour ne pas faire grossir state.json sans fin
# sur un chantier long.
MAX_RUN_HISTORY = 30

# Adoption : nombre d'executions consecutives qui doivent etre concordantes.
# Definition verifiee par TestAdoption : les ADOPTION_RUNS_REQUIRED dernieres
# executions doivent avoir toutes reussi (ok=True) ET porter le meme ENSEMBLE de
# noms mesures/declares que les autres. La concordance porte sur la FORME, jamais
# sur les valeurs : un capteur vivant est cense rendre des nombres differents d'un
# cycle a l'autre (une duree, un compte de fichiers) ; exiger des valeurs identiques
# rendrait l'adoption d'un capteur qui fonctionne reellement impossible. Ce qui doit
# etre stable avant qu'un humain ne valide, c'est ce que le capteur PRETEND mesurer,
# pas ce qu'il mesure.
ADOPTION_RUNS_REQUIRED = 3

# Capteur fige : le meme payload measured+declared reapparait sur ce nombre
# d'executions reussies consecutives. Contrairement a la concordance d'adoption, ici
# la comparaison porte sur les VALEURS elles-memes (measured + declared, jamais
# "at" qui varie toujours) : une sortie identique sur plusieurs cycles signale que
# le capteur ne mesure plus rien de vivant, pas qu'il a echoue.
FROZEN_AFTER = 3

# Reveil : nombre d'echecs consecutifs (execution ratee, timeout, sortie
# inexploitable) qui declenchent le signal de reveil pour que l'orchestratrice
# corrige son script.
WAKE_AFTER_FAILURES = 2

_REQUIRED_OUTPUT_KEYS = ("at", "ok", "measured", "declared", "drift", "unknown")
_REQUIRED_LIST_KEYS = ("measured", "declared", "drift", "unknown")


class CapteurError(Exception):
    """Refus explicite d'une operation sur un capteur (meme esprit que I8 : toute
    operation refusee dit laquelle et pourquoi, jamais un echec muet)."""


def _get_chantier(state: dict, chantier_id: str) -> dict:
    chantier = state["chantiers"].get(chantier_id)
    if chantier is None:
        raise CapteurError(f"campaign not found: {chantier_id}")
    return chantier


def _fresh_capteur_state() -> dict:
    return {
        "path": None,
        "runs": [],
        "adopted": False,
        "adoptedAt": None,
        "lastSuccess": None,
        "lastError": None,
        "consecutiveFailures": 0,
        "identicalCycles": 0,
    }


def install(chantier_id: str, script_path: str | Path) -> dict:
    """Copie script_path dans ORDO_HOME/sensors/, hors du repo audite, et le rend
    executable. Reinitialise entierement l'etat du capteur : un nouveau script n'a
    pas herite de la confiance accordee au precedent, il doit regagner ses 3
    executions concordantes et une nouvelle validation humaine.
    """
    src = Path(script_path)
    if not src.is_file():
        raise CapteurError(f"sensor script not found: {src}")

    with store.locked() as state:
        chantier = _get_chantier(state, chantier_id)
        dest_dir = store.home() / "sensors"
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / f"{chantier_id}{src.suffix}"
        copyfile(src, dest)
        os.chmod(dest, 0o755)
        chantier["capteur"] = _fresh_capteur_state()
        chantier["capteur"]["path"] = str(dest)
    return chantier


def _parse_output(stdout: bytes) -> dict:
    """Valide la sortie JSON d'un capteur contre son contrat impose (I10).

    N'est PAS le parseur indulgent de report.py : le script est ecrit par
    l'orchestratrice elle-meme, sous son controle, donc une sortie qui ne respecte
    pas exactement le contrat ("un objet JSON et rien d'autre") est un bug du
    capteur a corriger, pas une deformation de modele a encaisser. Toute cle
    manquante est nommee dans l'erreur ; aucune n'est jamais comblee par [].
    """
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise CapteurError("empty output, no JSON produced")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CapteurError(f"non-JSON output: {exc}") from exc
    if not isinstance(data, dict):
        raise CapteurError(
            f"invalid JSON output: object expected, got {type(data).__name__}"
        )

    missing = [k for k in _REQUIRED_OUTPUT_KEYS if k not in data]
    if missing:
        raise CapteurError(
            "missing key(s) in sensor output: " + ", ".join(missing)
        )
    for key in _REQUIRED_LIST_KEYS:
        if not isinstance(data[key], list):
            raise CapteurError(
                f"field {key}: list expected, got {type(data[key]).__name__}"
            )
    if not isinstance(data["ok"], bool):
        raise CapteurError(f"field ok: boolean expected, got {type(data['ok']).__name__}")

    return data


def _execute(script_path: Path, timeout: float) -> tuple[bytes, int | None, bool, bool, str | None]:
    """Lance script_path directement (jamais via un shell interpose), lit stdout par
    petits blocs avec un budget de temps et un plafond de taille, et tue le
    processus des que l'un des deux est depasse.

    Renvoie (stdout, code_retour, timed_out, truncated, erreur_lancement). Le
    lancement direct via subprocess.Popen([str(script_path)]) est ce qui garantit
    le respect du shebang : un `sh script.py` ignorerait la premiere ligne et
    executerait le script Python comme s'il etait un script shell, echouant a
    chaque cycle sans jamais le dire.
    """
    try:
        proc = subprocess.Popen(
            [str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        return b"", None, False, False, str(exc)

    deadline = time.monotonic() + timeout
    buf = bytearray()
    timed_out = False
    truncated = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.5))
            if not ready:
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break  # EOF : le capteur a ferme stdout, execution normale terminee
            buf.extend(chunk)
            if len(buf) >= MAX_OUTPUT_BYTES:
                truncated = True
                break
    finally:
        if timed_out or truncated:
            with suppress(ProcessLookupError):
                proc.kill()
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        if proc.stdout:
            proc.stdout.close()

    return bytes(buf[:MAX_OUTPUT_BYTES]), proc.returncode, timed_out, truncated, None


def run(chantier_id: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Execute le capteur installe pour chantier_id, une fois, et enregistre le
    resultat. Timeout dur, sortie bornee (voir _execute), lecture seule imposee par
    contrat au script lui-meme (ce module ne verifie pas cette regle, il ne peut
    que constater ce qui sort sur stdout).

    L'execution a lieu HORS du verrou d'etat : detenir store.locked() pendant tout
    le timeout bloquerait toute autre operation d'Ordo (CLI, tick d'un autre
    chantier) pour la duree de l'appel. Seules la lecture du chemin du script et
    l'ecriture du resultat sont verrouillees, separement.
    """
    with store.locked() as state:
        chantier = _get_chantier(state, chantier_id)
        script = chantier["capteur"]["path"]
        if not script:
            raise CapteurError(f"no sensor installed for {chantier_id}")
        script_path = Path(script)

    stdout, returncode, timed_out, truncated, spawn_error = _execute(script_path, timeout)
    at = store.now()

    error: str | None = None
    output: dict | None = None
    if spawn_error is not None:
        error = f"launch failed: {spawn_error}"
    elif timed_out:
        error = f"timeout exceeded ({timeout} s)"
    elif truncated:
        error = f"output truncated beyond {MAX_OUTPUT_BYTES} bytes"
    elif returncode != 0:
        error = f"nonzero exit code ({returncode})"
    else:
        try:
            output = _parse_output(stdout)
        except CapteurError as exc:
            error = str(exc)

    ok = error is None
    record = {
        "at": at,
        "ok": ok,
        "error": error,
        "timedOut": timed_out,
        "truncated": truncated,
        "output": output,
    }

    with store.locked() as state:
        chantier = _get_chantier(state, chantier_id)
        cap = chantier["capteur"]
        cap["runs"].append(record)
        if len(cap["runs"]) > MAX_RUN_HISTORY:
            cap["runs"] = cap["runs"][-MAX_RUN_HISTORY:]
        if ok:
            cap["consecutiveFailures"] = 0
            previous = cap["lastSuccess"]
            if previous is not None and _same_payload(previous["output"], output):
                cap["identicalCycles"] += 1
            else:
                cap["identicalCycles"] = 1
            cap["lastSuccess"] = {"at": at, "output": output}
        else:
            cap["consecutiveFailures"] += 1
            cap["identicalCycles"] = 0
            cap["lastError"] = {"at": at, "reason": error}

    return record


def _same_payload(a: dict | None, b: dict | None) -> bool:
    """Egalite structurelle sur measured et declared uniquement (jamais "at", qui
    varie a chaque execution meme quand rien n'a change). Sert exclusivement a la
    detection de figement, jamais a la concordance d'adoption (voir _concordant)."""
    if a is None or b is None:
        return False
    return a.get("measured") == b.get("measured") and a.get("declared") == b.get("declared")


def _report_shape(output: dict) -> tuple[frozenset, frozenset]:
    """Ensemble des noms mesures et declares dans une sortie de capteur : c'est la
    "forme" comparee par _concordant, independamment des valeurs."""
    measured = frozenset(m.get("name") for m in output.get("measured", []))
    declared = frozenset(d.get("name") for d in output.get("declared", []))
    return measured, declared


def _concordant(runs: list[dict]) -> bool:
    """Voir ADOPTION_RUNS_REQUIRED pour la definition complete de "concordant"."""
    if len(runs) < ADOPTION_RUNS_REQUIRED:
        return False
    recent = runs[-ADOPTION_RUNS_REQUIRED:]
    if not all(r["ok"] and r["output"] is not None for r in recent):
        return False
    shapes = {_report_shape(r["output"]) for r in recent}
    return len(shapes) == 1


def adopt(chantier_id: str) -> dict:
    """Adopte le capteur d'un chantier : 3 executions concordantes (voir
    ADOPTION_RUNS_REQUIRED) PLUS l'appel explicite a cette fonction, qui EST la
    validation humaine (SPEC.md et capteur.md la decrivent comme un geste
    delibere, jamais automatique). Refuse, avec raison, si la precondition
    automatique n'est pas remplie (I8 : aucun refus silencieux).
    """
    with store.locked() as state:
        chantier = _get_chantier(state, chantier_id)
        cap = chantier["capteur"]
        if not _concordant(cap["runs"]):
            raise CapteurError(
                f"adoption refused for {chantier_id}: fewer than "
                f"{ADOPTION_RUNS_REQUIRED} consecutive matching runs "
                "(same measured/declared names, all successful)"
            )
        cap["adopted"] = True
        cap["adoptedAt"] = store.now()
    return chantier


def status(chantier_id: str) -> dict:
    """Signal FILTRE d'un capteur (I12). Avant adoption, ne rend jamais rien
    d'autre que {"adopted": False, "signal": "unknown"} : aucune mesure, meme si
    des executions ont deja reussi, ne doit fuiter avant la validation humaine.

    Apres adoption, le signal distingue explicitement :
      - "ok"     : dernier succes recent, rien de particulier a signaler
      - "failed"  : le dernier run a echoue, mais pas encore deux fois de suite
      - "waking" : WAKE_AFTER_FAILURES echecs consecutifs, le capteur est casse et
                   l'orchestratrice doit corriger son script
      - "frozen"   : FROZEN_AFTER succes consecutifs au payload identique, le capteur
                   ne mesure plus rien de vivant

    "waking" et "frozen" ne sont jamais le meme signal : un capteur fige est une
    sortie qui ne bouge pas malgre des executions reussies, un capteur en reveil
    est une sortie qui ne peut meme plus etre produite. Les confondre ferait lire
    un chantier a l'arret la ou seul le capteur est en cause, ou inversement.
    """
    state = store.load()
    chantier = _get_chantier(state, chantier_id)
    cap = chantier["capteur"]

    if not cap["adopted"]:
        return {"adopted": False, "signal": "unknown"}

    if cap["consecutiveFailures"] >= WAKE_AFTER_FAILURES:
        signal = "waking"
    elif cap["identicalCycles"] >= FROZEN_AFTER:
        signal = "frozen"
    elif cap["consecutiveFailures"] > 0:
        signal = "failed"
    else:
        signal = "ok"

    last = cap["lastSuccess"]
    output = last["output"] if last else None
    return {
        "adopted": True,
        "adoptedAt": cap["adoptedAt"],
        "signal": signal,
        "consecutiveFailures": cap["consecutiveFailures"],
        "identicalCycles": cap["identicalCycles"],
        "lastSuccessAt": last["at"] if last else None,
        "measured": list(output["measured"]) if output else [],
        "declared": list(output["declared"]) if output else [],
        "drift": list(output["drift"]) if output else [],
        "unknown": list(output["unknown"]) if output else [],
        "lastError": cap["lastError"],
    }


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def due(chantier_id: str, every: int = 120) -> bool:
    """True si au moins `every` secondes se sont ecoulees depuis la derniere
    execution du capteur. Un capteur jamais installe n'est jamais du (rien a
    executer). Un capteur installe mais jamais execute est du immediatement.
    """
    state = store.load()
    chantier = _get_chantier(state, chantier_id)
    cap = chantier["capteur"]
    if not cap["path"]:
        return False
    if not cap["runs"]:
        return True
    elapsed = (datetime.now(timezone.utc) - _parse_iso(cap["runs"][-1]["at"])).total_seconds()
    return elapsed >= every


# ===========================================================================
# pane_silencieux : mesure d'activité tmux, distincte du capteur-script ci-dessus
# ===========================================================================
#
# Un second sens de "capteur" cohabite dans ce fichier depuis ici : la mesure de
# l'activité d'un pane tmux (verbe en cours, chronomètre, compteur de jetons -- voir
# ordo/panes.py:BUSY_MARKERS et panes.busy()), sans lien avec le script installé/adopté
# par le reste de ce module. Les invariants I9/I10/I12 documentés en tête de fichier
# (mesuré vs déclaré, aucune valeur par défaut, signal filtré avant adoption) sont
# propres au capteur-script et ne s'appliquent pas ici.


def pane_silencieux(occupe: bool, ecoule: float, seuil: float) -> bool:
    """True si un pane vivant doit être signalé silencieux, jamais quand `occupe` est
    vrai -- même si `ecoule` (secondes depuis le dernier changement observé) dépasse
    `seuil`.

    `occupe` vient de `panes.busy()`, qui lit les marqueurs d'activité de Claude Code
    (BUSY_MARKERS : verbe en cours, chronomètre, compteur de jetons) dans le texte
    récemment capturé du pane. Ce garde-fou existe parce qu'une session en pleine
    réflexion (effort xhigh) peut afficher ces marqueurs en continu pendant dix à
    quinze minutes sans que le reste du texte du pane ne change -- exactement ce que le
    seul appelant de cette fonction, `ordo/cli.py`'s `cmd_watch`, confondait avec un
    pane mort avant ce correctif : quatre alertes "silent" mesurées le 16 août 2026 sur
    trois exécutantes réelles de ce dépôt, toutes en pleine réflexion, donc toutes
    fausses. Un signal faux à chaque fois est pire qu'absent : il apprend à ignorer
    l'alerte, et le jour où un pane meurt vraiment, le signal se noie dans les
    précédents.

    Vérifié le 16 août 2026 sur les panes vivants réels de la campagne c-01 (%27, %28,
    %135) : les trois affichaient un compteur de jetons ("↓ NN.Nk tokens") en continu
    pendant une réflexion prolongée (jusqu'à 22 min sans interruption sur %28), déjà
    capté par BUSY_MARKERS ("tokens", "· ↓") -- aucun marqueur n'y manquait, seule la
    garde ci-dessous manquait à l'appelant.
    """
    if occupe:
        return False
    return ecoule >= seuil
