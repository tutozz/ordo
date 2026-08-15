"""Choix du modele d'une executante, deduit de la tache elle-meme.

Une executante herite par defaut du modele configure dans Claude Code, qui est le plus
souvent le plus cher. Appliquer un brief deja redige et arbitre ne demande pourtant pas
le meme modele que trancher une question ouverte. Ce module regarde la tache et propose
le modele qui lui correspond, sans rien demander a personne.

QUATRE CHOIX PORTENT CE FICHIER.

Le vocabulaire se cherche dans le TITRE, jamais dans le corps du brief. C'est le choix
le moins evident et le plus important, et il vient d'une mesure, pas d'une intuition :
sur un chantier reel de 69 taches, chercher les verbes de conception dans tout le brief
classait 24 taches d'execution sur 24 en "conception". Les mots y etaient bien, mais
dans des INTERDICTIONS -- "ne propose aucune tache de deploiement" -- et dans la methode
-- "va voir le code". Un brief long finit toujours par contenir le vocabulaire qu'on y
cherche, si bien que le critere punissait les briefs les mieux rediges. Le titre, lui,
dit ce que la tache fait ; le corps dit dans quelles conditions. Seul le titre est une
demande.

Le classement se fait par REGLES ORDONNEES, jamais par score. Un score additionne des
signaux heterogenes et rend un nombre : quand il se trompe, on ne sait pas quelle part
du calcul a faute, et on ne peut que le retoucher au hasard. Une regle, elle, se lit,
se conteste et se corrige seule. La premiere qui repond gagne et dit pourquoi.

Le defaut est OPUS, jamais sonnet. Une tache dont rien ne prouve qu'elle est definie
n'est pas une tache bon marche : c'est une tache dont on ignore ce que couterait une
erreur. Le routage n'economise que la ou il peut le justifier, et mal specifier une
tache coute cher -- c'est le bon sens de l'incitation.

La longueur du prompt ne decide RIEN a elle seule. C'est le signal qui vient a l'esprit
en premier et c'est le plus trompeur : un brief long est souvent long parce qu'il est
flou. Ce qui atteste qu'une tache est definie, c'est sa checklist -- des criteres de
fini ecrits d'avance -- et un perimetre nomme. La longueur ne sert qu'a disqualifier
haiku, jamais a promouvoir sonnet.
"""

from __future__ import annotations

import re
import unicodedata

# Du moins cher au plus cher. Sert aussi de contrat : le reste d'Ordo ne doit jamais
# recevoir d'ici une valeur absente de ce tuple.
MODELES = ("haiku", "sonnet", "opus")

DEFAUT = "opus"

# Valeur speciale de --model : ne rien imposer du tout, et laisser Claude Code appliquer
# le modele de sa propre configuration. C'est le comportement d'Ordo d'avant ce module ;
# il reste joignable en un mot, parce qu'un routage automatique dont on ne peut pas
# sortir n'est pas un choix, c'est une contrainte.
HERITE = "herite"

# Seuils de haiku. Ils sont volontairement severes et cumulatifs : haiku ne se merite
# que quand les trois signaux concordent. Se tromper vers le haut coute des jetons ; se
# tromper vers le bas coute une tache ratee, qu'il faudra relancer -- donc payer deux
# fois, plus le temps humain du diagnostic.
HAIKU_PROMPT_MAX = 600
HAIKU_TOUCHES_MAX = 2
HAIKU_CHECKLIST_MAX = 2

# Une decision non prise dans le brief sera prise par l'executante. Autant qu'elle le
# soit par le modele qui decide le mieux.
INCERTITUDE = (
    "a definir",
    "reste a definir",
    "a preciser",
    "a determiner",
    "si besoin",
    "au choix",
    "eventuellement",
    "le cas echeant",
    "a voir",
    "voir avec",
    "a discuter",
    "a trancher",
    "tbd",
    "to be defined",
    "if needed",
    "as appropriate",
    "figure out",
    "decide how",
)

# Une tache qui prononce un verdict de fin de campagne engage un jugement, pas une
# execution, meme quand son titre ne porte aucun verbe de conception. Sur le chantier de
# reference, ce marqueur attrape 4 titres sur 69 et aucun a tort.
VERDICT = (
    "recette",
    "final",
    "finale",
    "verdict",
    "go/no-go",
    "sign-off",
)

# Verbes de conception. Cherches comme mots entiers ou expressions, jamais comme
# radicaux courts : "decid" attraperait "decision", et une tache qui CONTROLE des
# decisions deja figees est de l'execution, pas de la conception. La nuance est exactement
# celle qui separe "les decisions figees" de "synthese des controles".
CONCEPTION = (
    "concevoir",
    "conception",
    "arbitrer",
    "arbitrage",
    "repenser",
    "refondre",
    "imaginer",
    "explorer",
    "synthese",
    "synthetiser",
    "recommander",
    "recommandation",
    "preconiser",
    "decider",
    "choisir",
    "definir",
    "proposer",
    "proposition",
    "evaluer les options",
    "rediger",
    "nommer",
    # "design" est un nom commun au moins aussi souvent qu'un verbe : "design system"
    # ne doit pas envoyer une tache d'integration sur le modele le plus cher. On ne le
    # retient que suivi d'un article, seule forme ou il est vraiment un verbe.
    "design the",
    "design a",
    "design an",
    "redesign",
    "rethink",
    "brainstorm",
    "propose",
    "recommend",
    "decide",
)

# Gestes dont le resultat est entierement dicte par l'enonce.
MECANIQUE = (
    "renommer",
    "corriger",
    "ajouter",
    "supprimer",
    "retirer",
    "deplacer",
    "remplacer",
    "verifier",
    "controler",
    "appliquer",
    "mettre a jour",
    "extraire",
    "formater",
    "installer",
    "bump",
    "rename",
    "fix",
    "add",
    "remove",
    "move",
    "replace",
    "verify",
    "check",
    "apply",
    "update",
    "extract",
    "format",
)

# Un chemin de fichier dans le prompt ancre la tache aussi surement que le champ
# touches. Le point doit etre suivi d'une extension collee : "dans le code." n'est pas
# un chemin, "docs/15-API.md" en est un.
_FICHIER = re.compile(r"\b[\w./-]+\.[a-z]{1,5}\b")


def _plat(texte: str) -> str:
    """Minuscules sans accents : les briefs de ce depot s'ecrivent sans accents, les
    prompts que des humains y collent en portent. Comparer les deux exige de choisir une
    forme, et la forme sans accents est la seule que les deux atteignent."""
    if not texte:
        return ""
    decompose = unicodedata.normalize("NFD", texte)
    sans_accents = "".join(c for c in decompose if not unicodedata.combining(c))
    return sans_accents.lower()


def _contient(foin: str, aiguilles) -> str | None:
    """Rend la premiere expression trouvee comme mot ou groupe de mots entiers."""
    for aiguille in aiguilles:
        if re.search(rf"(?<!\w){re.escape(aiguille)}(?!\w)", foin):
            return aiguille
    return None


def classer(tache: dict) -> tuple[str, str]:
    """Rend (modele, motif) pour une tache. Ne modifie jamais la tache recue.

    Le motif est destine a un humain qui conteste le choix : il tient sur une ligne et
    nomme le signal qui a tranche, pas la regle qui l'a porte.
    """
    # tache["model"] n'est PAS lu ici, et c'est deliberé : ce champ n'est ecrit que par
    # le marquage de lancement, il porte donc le modele du DERNIER tour, pas une consigne
    # humaine. Le relire comme une decision figerait a jamais le premier routage et
    # rendrait l'escalade de pour_lancement() inoperante -- un garde-fou qui ne se
    # declenche jamais au moment ou il compte.
    prompt = tache.get("prompt") or ""
    titre = tache.get("titre") or ""
    # Le vocabulaire ne se cherche QUE la : voir la docstring du module, cette fenetre
    # est le coeur du fichier. L'elargir au prompt casse tests/test_routage.py.
    demande = _plat(titre)
    touches = tache.get("touches") or []
    checklist = tache.get("checklist") or []

    if not demande.strip():
        return "opus", "tache sans titre : sa nature est indeterminable"

    trouve = _contient(demande, VERDICT)
    if trouve:
        return "opus", f"jugement terminal ({trouve!r})"

    trouve = _contient(demande, INCERTITUDE)
    if trouve:
        return "opus", f"le titre laisse une decision ouverte ({trouve!r})"

    trouve = _contient(demande, CONCEPTION)
    if trouve:
        return "opus", f"tache de conception ({trouve!r})"

    if not checklist:
        return "opus", "aucun critere de fini : rien ne dit quand la tache est finie"

    ancre = bool(touches) or bool(_FICHIER.search(prompt))
    geste = _contient(demande, MECANIQUE)

    if (
        geste
        and len(prompt) <= HAIKU_PROMPT_MAX
        and len(touches) <= HAIKU_TOUCHES_MAX
        and len(checklist) <= HAIKU_CHECKLIST_MAX
        and ancre
    ):
        return "haiku", f"geste mecanique ({geste!r}) borne a {len(touches) or 1} fichier(s)"

    if ancre:
        return "sonnet", f"specification verifiable : {len(checklist)} critere(s), perimetre nomme"

    return DEFAUT, "rien ne prouve que la tache soit definie : ni perimetre, ni fichier cite"


def _essais(tache: dict) -> int:
    """Nombre de tentatives deja faites, tolerant a un compteur absent ou abime.

    Un etat ecrit par une version anterieure n'a pas forcement ce champ, et le routage
    n'est pas un endroit ou lever : il vaut mieux router sans escalade que bloquer un
    lancement pour un entier manquant.
    """
    try:
        return max(0, int(tache.get("attempts") or 0))
    except (TypeError, ValueError):
        return 0


def pour_lancement(explicite: str | None, tache: dict) -> tuple[str | None, str]:
    """Modele retenu pour un lancement, et pourquoi. Rend (None, motif) pour HERITE.

    Precedence : ce que l'humain tape au lancement l'emporte toujours ; sinon le routage
    tranche, puis ESCALADE d'autant de crans que la tache compte de tentatives passees.

    L'escalade est la contrepartie assumee du routage. Se tromper vers le bas est un
    risque reel : un brief ambigu qu'Opus rattrapait tout seul fera echouer une
    executante Sonnet. Ce qui serait fautif, ce n'est pas de se tromper, c'est de
    persister -- relancer a l'identique refait exactement le meme echec, en le facturant
    une seconde fois. Une tache mal classee se repare donc d'elle-meme au premier
    relaunch, sans que personne ait a diagnostiquer que le modele etait en cause.
    """
    if explicite == HERITE:
        return None, f"--model {HERITE} : aucun modele impose, claude garde son defaut"
    if explicite:
        return explicite, f"impose au lancement (--model {explicite})"

    modele, motif = classer(tache)
    essais = _essais(tache)
    if not essais:
        return modele, motif

    rang = min(MODELES.index(modele) + essais, len(MODELES) - 1)
    monte = MODELES[rang]
    if monte == modele:
        return modele, f"{motif} ; deja au plus haut apres {essais} tentative(s)"
    return monte, f"{modele} attendu, monte a {monte} apres {essais} tentative(s)"


def explique(tache: dict) -> str:
    """Une ligne prete a afficher, pour les verbes qui montrent le routage."""
    modele, motif = classer(tache)
    return f"{modele:<7} {motif}"
