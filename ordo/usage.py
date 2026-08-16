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
# Sa propre variable d'environnement, distincte de celle de SEUIL_CONTEXTE (voir plus bas
# pourquoi les confondre est dangereux).
#
# DÉPASSÉ : le déclencheur réel vit désormais dans SEUIL_CONTEXTE ci-dessous, mesuré sur le
# contexte porté plutôt que sur les tours. Raison du remplacement, chiffrée sur cinquante-neuf
# sessions réelles de deux chantiers : au seuil de 75 tours, 16 sur 59 étaient RATÉES parce
# qu'elles atteignaient un gros contexte avant d'atteindre 75 tours (exemple : une session à
# 290 505 jetons de contexte en seulement 49 tours). La corrélation tours/contexte est forte
# (r = 0,92) mais pas assez pour servir de proxy : le contexte par tour varie d'un facteur 5
# d'une session à l'autre (médiane 2 419, min 1 188, max 6 251).
#
# Cette constante reste lue par carte.py, qui n'a pas encore basculé sur SEUIL_CONTEXTE ; elle
# reste donc EXACTE et INCHANGÉE pour ne pas casser ce lecteur. controle.py, lui, ne la lit
# plus.
SEUIL_TOURS = float(os.environ.get("ORDO_COMPACT_EVERY") or 75)

# Contexte porté (voir dernierContexte, rendu par pour()) au-delà duquel une session mérite
# d'être remise à plat. 0 désactive. Remplace SEUIL_TOURS comme déclencheur de la compaction :
# une session peut porter un gros contexte en peu de tours, et le nombre de tours ne le voit
# jamais venir (voir le commentaire de SEUIL_TOURS pour les 16 sessions ratées).
#
# Sa propre variable d'environnement, ORDO_COMPACT_EVERY_CONTEXTE -- SURTOUT PAS
# ORDO_COMPACT_EVERY, lue par SEUIL_TOURS ci-dessus. Les deux échelles diffèrent d'un facteur
# mille (tours contre jetons) : les confondre a longtemps mis, en posant ORDO_COMPACT_EVERY
# pour régler la cadence en tours, ce seuil-ci à quelques dizaines de jetons, déclenchant la
# compaction à chaque tour, pour toujours.
#
# Choisi par SIMULATION sur les 55 sessions exploitables (au moins 5 tours) des 59 transcripts
# réels du chantier camcast. Pour chaque seuil candidat, simulation du contexte total facturé
# sur la session entière en repartant à 42 000 jetons (le contexte mesuré d'un premier tour
# réel) après chaque compaction. Sans aucune compaction, ces sessions facturent 1,90 milliard
# de jetons de contexte cumulés (baseline).
#
# Chaque compaction facture 315 000 jetons -- mais d'ÉCRITURE de cache, pas de relecture, et
# les deux n'ont pas le même prix : une écriture coûte 1,25 fois un jeton d'entrée, une
# relecture 0,1 fois. Un jeton écrit coûte donc 12,5 jetons relus. La première version de
# cette simulation facturait les 315 000 jetons d'écriture au prix d'une relecture, ce qui
# sous-évaluait chaque compaction d'un facteur 12,5 et faisait paraître rentable un seuil bien
# trop bas : à 75 000, 345 compactions × 315 000 × 12,5 ≈ 1,36 milliard de jetons
# équivalent-relecture à elles seules, à comparer aux 1,90 milliard de la baseline entière.
#
#      seuil     gain net   compactions   tours moyens entre deux compactions
#     75 000        5,3 %       349                     21   <- ancien seuil, quasi annulé
#    100 000       32,2 %       196                     38
#    125 000       41,3 %       129                     57
#    150 000       44,6 %        94                     79   <- retenu
#    170 000       44,6 %        75                     99
#    200 000       42,8 %        55                    134
#    300 000       32,5 %        19                    389
#
# Le plateau se déplace de 70 000-80 000 à 140 000-180 000 (moins de 1 point d'écart) : le
# coût d'écriture, désormais 12,5 fois plus cher, ne se rentabilise qu'en espaçant nettement
# plus les compactions. 150 000 est un nombre rond au sommet de ce plateau, une cadence d'un
# tour sur environ quatre-vingts.
#
# Repère de plausibilité : la même correction appliquée à la simulation par NOMBRE DE TOURS
# donne 21 % de gain net à 75 tours et 25 % à 50 tours, contre 44,6 % ici. L'écart est réel
# mais s'explique : le déclencheur par tours compacte des sessions au contexte encore léger
# simplement parce qu'elles ont duré, payant des écritures pour rien -- exactement ce que ce
# seuil-ci a été introduit pour éviter (voir le commentaire de SEUIL_TOURS).
SEUIL_CONTEXTE = float(os.environ.get("ORDO_COMPACT_EVERY_CONTEXTE") or 150000)

_CHAMPS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cacheCreation",
    "cache_read_input_tokens": "cacheRead",
}

# Champs de _CHAMPS qui composent le contexte PORTÉ par un tour -- ce que le modèle a
# effectivement lu pour répondre. La sortie en est exclue : elle n'est jamais relue par le
# tour qui l'a produite, seulement par les tours suivants (et elle est alors comptée dans
# leur propre cache_read, pas ici).
_CHAMPS_CONTEXTE = ("input", "cacheCreation", "cacheRead")


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
    return {"input": 0, "output": 0, "cacheCreation": 0, "cacheRead": 0, "turns": 0,
            "dernierContexte": 0}


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
        contexte_tour = 0
        for brut, nom in _CHAMPS.items():
            valeur = u.get(brut)
            if isinstance(valeur, int):
                totaux[nom] += valeur
                vu = True
                if nom in _CHAMPS_CONTEXTE:
                    contexte_tour += valeur
        if vu:
            totaux["turns"] += 1
            # Écrase, n'additionne pas : c'est le contexte de CE tour, pas la somme de tous
            # les tours de la session. Un tour non vu (ligne dupliquée, dédoublonnée plus
            # haut) laisse la valeur précédente en place, ce qui est le comportement voulu.
            totaux["dernierContexte"] = contexte_tour


def pour(task: dict) -> dict | None:
    """Jetons consommes par une tache, ou None si son transcript est introuvable.

    Le total est rendu tel que l'API le compte, sans agregation arbitraire : entree, sortie,
    cache cree, cache relu, chacun a part. Les additionner en un seul nombre ferait passer
    vingt-cinq millions de jetons de cache relu pour l'equivalent de vingt-cinq millions de
    jetons ecrits, ce qu'ils ne sont ni en cout ni en sens.

    Porte aussi "dernierContexte" : la taille du contexte du DERNIER tour lu (input +
    cache_creation + cache_read de ce tour, sortie exclue), pas une somme sur la session.
    C'est ce que lit la compaction (voir SEUIL_CONTEXTE) pour décider si une session porte
    un contexte trop lourd, quel que soit son nombre de tours.
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
