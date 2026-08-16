"""Jetons consommes par une executante, lus dans son propre transcript Claude Code.

Ordo lance chaque executante avec un `--session-id` qu'il choisit lui-meme et range sous
`claudeSessionId`. Claude Code ecrit alors la conversation dans
`~/.claude/projects/<projet-encode>/<session-id>.jsonl`, une ligne par evenement, et chaque
reponse du modele y porte son `usage`. Ce module additionne ces usages ; il n'y a rien a
demander a personne, tout est deja sur le disque.

TROIS CHOIX PORTENT CE FICHIER.

Le transcript se CHERCHE, il ne se deduit pas. Le nom du dossier est un encodage du chemin
du projet, et reproduire cet encodage serait parier sur une regle qui ne nous appartient
pas. Un identifiant de session est unique : le chercher est plus sur, et le resultat se
retient.

La lecture est INCREMENTALE. Un transcript d'executante fait plusieurs mega-octets, la page
interroge toutes les trois secondes, et un chantier porte des dizaines de taches ; tout
relire a chaque battement ferait payer des centaines de mega-octets par minute. Un JSONL ne
fait que croitre, donc il suffit de reprendre la ou on s'etait arrete.

L'absence se dit ABSENTE, jamais zero. Une tache dont le transcript est introuvable n'a pas
consomme zero jeton : on ne sait pas. Afficher 0 sur une executante qui tourne depuis vingt
minutes serait un mensonge, et exactement le genre que ce depot passe son temps a traquer.

UN TOUR N'EST PAS UNE LIGNE. Claude Code écrit un même tour d'assistant sur plusieurs
lignes, une par bloc de contenu -- texte, réflexion, appel d'outil -- et chacune RÉPÈTE le
même `usage`. Additionner les lignes compte donc le même tour autant de fois qu'il a de
blocs. Mesure sur les soixante transcripts réels de ce dépôt : la sortie était comptée deux
fois (+102 %), le cache relu +59 %, le nombre de tours +68 %. Le dédoublonnage se fait sur
`message.id`, et il suffit de retenir le DERNIER vu : vérifié sur ces mêmes soixante
transcripts, les lignes d'un même tour se suivent toujours. Retenir le dernier plutôt que
tous garde l'état constant, ce qu'un serveur qui tourne des jours sur des dizaines de
sessions ne peut pas se permettre de perdre.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Cache par identifiant de session : ou est le fichier, jusqu'ou on l'a lu, et les totaux
# accumules. Vit dans le process ; le serveur etant long, il sert a chaque battement.
_CACHE: dict[str, dict] = {}

# Compteur d'octets reellement lus depuis le disque. Existe pour que la lecture
# incrementale soit verifiable : sans lui, "on ne relit que le nouveau" ne serait qu'une
# affirmation.
_OCTETS = 0

# Tours d'une session au-dela desquels son contexte merite d'etre remis a plat. 0 desactive.
#
# Mesure sur soixante transcripts reels de deux chantiers : chaque jeton entre dans une
# session est ensuite relu CENT DEUX FOIS, et le dernier tiers d'une session coute 2,3 fois
# son premier tiers. Le contexte n'est pas gros, il est porte trop longtemps. Simule sur ces
# memes series, compacter tous les 75 tours rend 21 % du contexte total facture, une fois
# payee l'ecriture qu'une compaction coute elle-meme.
#
# 75 plutot que 50, qui rendait 4 points de plus : la mediane des sessions est a 92 tours,
# donc a 75 la moitie d'entre elles ne compactent jamais. Chaque compaction est une occasion
# de perdre du contexte que la tache aurait utilise, et cette perte-la n'est mesurable dans
# aucun transcript. On prend le gain qui se voit, pas le dernier point qui se paierait en
# travail refait.
#
# Vit ICI et pas dans controle.py parce que deux modules le lisent -- la boucle qui compacte
# et la carte qui signale une session longue -- et qu'une carte qui alerterait a 100 pendant
# qu'Ordo compacte a 75 raconterait une autre histoire que celle qui se joue.
SEUIL_TOURS = float(os.environ.get("ORDO_COMPACT_EVERY") or 75)

_CHAMPS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cacheCreation",
    "cache_read_input_tokens": "cacheRead",
}


def racine() -> Path:
    """Ou Claude Code range ses transcripts. ORDO_TRANSCRIPTS surcharge, pour les tests."""
    raw = os.environ.get("ORDO_TRANSCRIPTS")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".claude" / "projects"


def forget() -> None:
    """Vide le cache. Pour les tests, et pour un process qui tournerait des jours."""
    global _OCTETS
    _CACHE.clear()
    _OCTETS = 0


def octets_lus() -> int:
    return _OCTETS


def _trouver(session_id: str) -> Path | None:
    base = racine()
    if not base.is_dir():
        return None
    for chemin in base.glob(f"*/{session_id}.jsonl"):
        return chemin
    return None


def _vide() -> dict:
    return {"input": 0, "output": 0, "cacheCreation": 0, "cacheRead": 0, "turns": 0}


# Valeur du « dernier identifiant vu » quand il n'y en a pas encore. None ne convient pas :
# un transcript écrit par une version qui ne pose pas d'`id` porterait None sur chaque
# ligne, et tous ses tours seraient pris pour des doublons du premier. Absence
# d'identifiant ne veut pas dire doublon.
_AUCUN = object()


def _avaler(entree: dict, chemin: Path) -> None:
    """Lit ce qui a ete ajoute depuis la derniere fois et l'additionne aux totaux.

    Un fichier qui a RETRECI est relu depuis le debut : un transcript remplace sous nos
    pieds laisserait sinon le decalage de lecture au milieu d'une ligne, et le total serait
    faux pour toujours, sans que rien ne le signale.
    """
    global _OCTETS
    try:
        taille = chemin.stat().st_size
    except OSError:
        return
    if taille < entree["offset"]:
        entree["offset"] = 0
        entree["totaux"] = _vide()
        entree["dernier"] = _AUCUN
    if taille == entree["offset"]:
        return
    try:
        with chemin.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(entree["offset"])
            nouveau = f.read()
    except OSError:
        return
    _OCTETS += len(nouveau.encode("utf-8", errors="replace"))

    # La derniere ligne peut etre incomplete : le fichier est en cours d'ecriture. On
    # s'arrete au dernier retour a la ligne et on reprendra le reste au prochain passage.
    coupe = nouveau.rfind("\n")
    if coupe < 0:
        return
    entree["offset"] += len(nouveau[: coupe + 1].encode("utf-8", errors="replace"))

    totaux = entree["totaux"]
    for ligne in nouveau[:coupe].splitlines():
        if not ligne.strip():
            continue
        try:
            evenement = json.loads(ligne)
        except (json.JSONDecodeError, ValueError):
            continue
        message = evenement.get("message")
        if not isinstance(message, dict):
            continue
        u = message.get("usage")
        if not isinstance(u, dict):
            continue
        # Le même tour arrive sur plusieurs lignes consécutives qui répètent son `usage` :
        # seule la première compte. Un tour sans identifiant compte toujours, faute de quoi
        # un transcript sans `id` disparaîtrait entier des compteurs.
        identifiant = message.get("id")
        if identifiant is not None and identifiant == entree["dernier"]:
            continue
        entree["dernier"] = _AUCUN if identifiant is None else identifiant
        vu = False
        for brut, nom in _CHAMPS.items():
            valeur = u.get(brut)
            if isinstance(valeur, int):
                totaux[nom] += valeur
                vu = True
        if vu:
            totaux["turns"] += 1


def pour(task: dict) -> dict | None:
    """Jetons consommes par une tache, ou None si son transcript est introuvable.

    Le total est rendu tel que l'API le compte, sans agregation arbitraire : entree, sortie,
    cache cree, cache relu, chacun a part. Les additionner en un seul nombre ferait passer
    vingt-cinq millions de jetons de cache relu pour l'equivalent de vingt-cinq millions de
    jetons ecrits, ce qu'ils ne sont ni en cout ni en sens.
    """
    session_id = task.get("claudeSessionId")
    if not session_id:
        return None
    entree = _CACHE.get(session_id)
    if entree is None:
        chemin = _trouver(session_id)
        if chemin is None:
            return None
        entree = {"path": chemin, "offset": 0, "totaux": _vide(), "dernier": _AUCUN}
        _CACHE[session_id] = entree
    _avaler(entree, entree["path"])
    return dict(entree["totaux"])


def court(n: int) -> str:
    """Nombre abrege pour une carte etroite : 999, 1.5k, 112k, 25.5M."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        milliers = n / 1000
        return f"{milliers:.1f}k" if milliers < 10 else f"{round(milliers)}k"
    # Les millions gardent leur decimale, les milliers non : entre 112k et 113k il n'y a
    # rien a lire, entre 25.5M et 26.0M il y a un demi-million de jetons.
    return f"{n / 1_000_000:.1f}M"
