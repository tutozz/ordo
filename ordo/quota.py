"""Consommation du compte Claude, lue dans le fichier que publie le hook statusline.

Il n'existe AUCUNE source native lisible à froid : ni fichier de Claude Code, ni
sous-commande CLI, ni endpoint HTTP documenté. La seule donnée fiable est le bloc
`rate_limits` que Claude Code pousse au hook statusline, et il n'existe que pendant
une session active. Sur cette machine, `~/.claude/statusline.sh` le publie déjà,
atomiquement, dans `~/.claude/.tmux-quota` : une seule ligne, cinq champs séparés
par une espace, un "-" marquant une valeur absente (pourcent 5h, pourcent 7j, epoch
d'écriture, epoch de reset 5h, epoch de reset 7j). ORDO_QUOTA_FILE surcharge le
chemin, pour les tests et pour qui voudrait publier la donnée ailleurs.

TROIS CHOIX PORTENT CE MODULE.

CE MODULE NE LÈVE JAMAIS. lire() est appelé par un serveur HTTP à chaque battement ;
une exception sur un fichier de quarante octets ferait tomber la page entière pour
une exécutante qui n'a plus tourné depuis une heure. Fichier absent, vide, tronqué,
champ ni "-" ni entier : tout se traduit en None, jamais en trace qui remonte.

L'ABSENCE SE DIT ABSENTE, JAMAIS ZÉRO -- même principe que ordo/usage.py. Une
fenêtre dont le pourcentage vaut "-" n'a pas consommé zéro : le hook ne l'a
simplement pas mesurée. La rendre à zéro afficherait une jauge vide pour une donnée
qu'on n'a pas, exactement le mensonge que ce dépôt passe son temps à traquer.

UNE FENÊTRE PÉRIMÉE LE DIT. Ce fichier n'est réécrit QUE pendant une session Claude
Code active : hors campagne il fige, et un reset déjà passé (perime=True) veut dire
que la fenêtre a tourné depuis -- le pourcentage lu date d'avant elle. L'afficher
comme s'il était à jour serait pire que ne rien afficher, d'où ce signal plutôt
qu'un silence.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# Jour abrégé en français, indexé par time.struct_time.tm_wday (lundi=0). Écrit en
# dur : la locale du process n'est pas garantie, et strftime("%a") passerait chez
# l'auteur pour rougir sur toute autre machine.
_JOURS_ABREGES = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")

# (clé affichée, libellé lisible, index du pourcentage, index du reset) dans la
# ligne du fichier -- voir la docstring du module pour le format des cinq champs.
_FENETRES = (
    ("5h", "5 heures", 0, 3),
    ("7j", "7 jours", 1, 4),
)

# Sentinelle d'un champ "-" : distincte de None pour ne jamais confondre "absent
# volontairement" (une fenêtre non mesurée) et "illisible" (le fichier entier est
# suspect, voir _valeur).
_ABSENT = object()


def fichier() -> Path:
    """Où le hook statusline publie la consommation. ORDO_QUOTA_FILE surcharge --
    ce qui rend le module testable sans toucher au vrai fichier de l'utilisateur, et
    permet à quelqu'un d'autre de publier la donnée ailleurs."""
    brut = os.environ.get("ORDO_QUOTA_FILE")
    if brut:
        return Path(brut).expanduser()
    return Path.home() / ".claude" / ".tmux-quota"


def _valeur(champ: str) -> object:
    """Un champ de la ligne : "-" devient _ABSENT, un entier se rend tel quel. Un
    texte qui n'est ni l'un ni l'autre lève ValueError, remontée jusqu'à lire() qui
    la traduit en None -- un champ illisible rend tout le fichier suspect, pas
    seulement la fenêtre qui le porte."""
    if champ == "-":
        return _ABSENT
    return int(champ)


def _couleur(pourcent: int) -> str:
    """Seuil décidé ICI, en Python, jamais en JavaScript : c'est ce qui le rend
    testable sans navigateur. La page ne fait que peindre la couleur reçue."""
    if pourcent < 50:
        return "#46a35a"
    if pourcent < 75:
        return "#e3b341"
    if pourcent < 90:
        return "#e0803c"
    return "#e05252"


def _reset_texte(epoch: int, maintenant: float) -> str:
    """Heure du reset, en heure LOCALE : "07:10" s'il tombe dans les vingt-quatre
    heures à venir, sinon "ven. 10:00". Le jour vient de _JOURS_ABREGES, jamais de
    strftime("%a")."""
    local = time.localtime(epoch)
    heure = time.strftime("%H:%M", local)
    delta = epoch - maintenant
    if 0 <= delta < 24 * 3600:
        return heure
    return f"{_JOURS_ABREGES[local.tm_wday]} {heure}"


def _fenetre(cle: str, libelle: str, pourcent: int, reset: int, maintenant: float) -> dict:
    return {
        "cle": cle,
        "libelle": libelle,
        "pourcent": pourcent,
        "reset": reset,
        "resetTexte": _reset_texte(reset, maintenant),
        "couleur": _couleur(pourcent),
        # Périmée : le reset est déjà passé, donc la fenêtre a tourné depuis et ce
        # pourcentage date d'avant. Un chiffre périmé affiché comme s'il était vrai
        # est pire que pas de chiffre du tout.
        "perime": reset < maintenant,
    }


def _construire(champs: list[str], maintenant: float) -> dict:
    ecrit = _valeur(champs[2])
    if ecrit is _ABSENT:
        # L'epoch d'écriture n'est jamais volontairement absent : le hook l'écrit à
        # chaque battement. Un "-" ici est un fichier qu'on ne comprend pas, pas une
        # fenêtre manquante -- lire() doit rendre None, pas un âge inventé.
        raise ValueError("epoch d'écriture absent")

    fenetres = []
    for cle, libelle, i_pourcent, i_reset in _FENETRES:
        pourcent = _valeur(champs[i_pourcent])
        if pourcent is _ABSENT:
            continue  # fenêtre non mesurée : absente du résultat, jamais à zéro
        reset = _valeur(champs[i_reset])
        if reset is _ABSENT:
            raise ValueError(f"reset absent pour la fenêtre {cle} pourtant mesurée")
        fenetres.append(_fenetre(cle, libelle, pourcent, reset, maintenant))

    return {"fenetres": fenetres, "ageSecondes": int(maintenant - ecrit)}


def lire() -> dict | None:
    """Lit et interprète le fichier de quota. None dès que la donnée n'est pas
    fiable : absente, tronquée, ou portant un champ que ce module ne comprend pas.
    Ne lève jamais (voir la docstring du module)."""
    try:
        brut = fichier().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError et pas seulement OSError : une écriture interrompue par
        # un disque plein laisse des octets qui ne sont pas de l'UTF-8, et cette
        # levée-là traverserait le filet posé plus bas, qui ne garde que ValueError
        # autour de l'interprétation. La promesse du module est absolue, pas presque.
        return None
    champs = brut.strip().split()
    if len(champs) != 5:
        return None
    try:
        return _construire(champs, time.time())
    except ValueError:
        return None
