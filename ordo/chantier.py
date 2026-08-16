"""Chantiers, taches, graphe de dependances, cycles, disponibilite (ready).

Chaque fonction qui mute l'etat passe par store.locked() : c'est lui qui garantit l'ecriture
atomique et le verrou. Les fonctions de lecture seule (ready, graph_ascii, has_cycle) lisent
sans verrou, has_cycle() n'accedant meme pas au disque : c'est une fonction pure sur un dict.
"""

from __future__ import annotations

import calendar
import json
import re
import shutil
import statistics
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from pathlib import Path

from . import store

# Horodatage ISO 8601 UTC, même format que store.now() et carte._ISO -- pas de fonction
# commune entre les deux modules, carte.py important déjà chantier.py (l'inverse créerait
# un cycle), donc ce petit parseur reste local à ce fichier plutôt que partagé.
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _epoch(horodatage: str | None) -> float | None:
    if not horodatage:
        return None
    try:
        return float(calendar.timegm(time.strptime(horodatage, _ISO)))
    except (ValueError, TypeError):
        return None

# Valeurs valides du champ chantier["permissions"] (point G). "skip" est le défaut :
# comportement actuel, ajoute --dangerously-skip-permissions au lancement d'une
# executante. "normal" n'ajoute rien, l'executante repasse par les prompts d'autorisation
# habituels de claude.
PERMISSIONS_VALUES = ("skip", "normal")

# Etats d'une tache qui rendent ses dependantes definitivement bloquees pour ce tour.
# "blocked" en fait partie : une dependante d'une tache deja bloquee ne peut pas non plus
# avancer, et l'omettre romprait la cascade transitive.
_DEAD_STATES = ("failed", "cancelled", "blocked")

# Valeur de task["blockedCause"] posee UNIQUEMENT par propagate_failures(). C'est une
# marque structuree, jamais un texte libre : le champ "error" reste un message a
# destination d'un humain, reformulable sans consequence ; blockedCause est le seul
# signal dont unblock_propagated() se sert pour savoir si une tache "blocked" peut etre
# relancee automatiquement. Toute tache bloquee pour sa propre raison (pane mort,
# rapport illisible, dialogue de confiance) laisse ce champ a None.
BLOCKED_CAUSE_PROPAGATION = "propagation"


class ChantierError(Exception):
    """Refus explicite d'une operation sur un chantier ou une tache (I8).

    Toute levee porte l'identifiant concerne et la raison dans son message : un refus
    muet est pire qu'un echec, il fait croire que l'operation a eu lieu.
    """


def _get_chantier(state: dict, chantier_id: str) -> dict:
    chantier = state["chantiers"].get(chantier_id)
    if chantier is None:
        raise ChantierError(f"campaign not found: {chantier_id}")
    return chantier


def _get_task(state: dict, task_id: str) -> dict:
    task = state["taches"].get(task_id)
    if task is None:
        raise ChantierError(f"task not found: {task_id}")
    return task


def _slug(canon_project: str) -> str:
    name = Path(canon_project).name if canon_project else "chantier"
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "chantier"


def _normalize_checklist(items: Iterable, defaut_min: float | None = None) -> list[dict]:
    """defaut_min (brief t-33) : médiane historique à poser sur tout item SANS dureeMin
    explicite. None par défaut, donc un appel sans ce paramètre reproduit exactement le
    comportement d'avant t-33 -- les 349 critères déjà normalisés sans lui doivent
    continuer de fonctionner (c8). La clé "dureeDefaut" n'apparaît QUE quand un défaut a
    réellement été posé : jamais sur un item dont la durée est explicite, jamais quand
    aucune médiane n'est disponible -- une valeur posée explicitement n'est jamais écrasée
    (piège 6 du brief)."""
    normalized = []
    for i, item in enumerate(items, start=1):
        if isinstance(item, dict):
            label = item.get("label", "")
            item_id = item.get("id") or f"c{i}"
            done = bool(item.get("done", False))
            duree_min = item.get("dureeMin")
        else:
            label = str(item)
            item_id = f"c{i}"
            done = False
            duree_min = None
        normalized_item = {"id": item_id, "label": label, "done": done, "dureeMin": duree_min}
        if duree_min is None:
            calculee = _duree_calculee(defaut_min)
            if calculee is not None:
                normalized_item["dureeMin"] = calculee
                normalized_item["dureeDefaut"] = True
        normalized.append(normalized_item)
    return normalized


# Longueur maximale conventionnelle d'un label de checklist. Un plafond, jamais un refus
# (voir checklist_hors_convention) : la convention vaut pour la lisibilité d'une carte ou
# d'un plan, pas pour la validité d'une tâche, et rien dans Ordo ne doit la faire respecter
# de force. Quarante, pas soixante : une case dans une colonne de mur fait quelques
# centaines de pixels, et le libellé y voisine déjà l'identifiant, le badge de modèle, la
# durée écoulée et la taille du contexte porté.
CHECKLIST_LABEL_MAX = 40


def checklist_hors_convention(checklist: Iterable[dict]) -> list[dict]:
    """Items d'une checklist déjà normalisée dont le label dépasse CHECKLIST_LABEL_MAX.

    Pure et sans effet de bord : ne tronque rien, ne rejette rien, se contente de mesurer.
    C'est à l'appelant (CLI, matérialisation d'un plan) de décider s'il en fait un
    avertissement -- jamais un refus, la convention ne bloque personne.
    """
    return [
        {"id": item["id"], "length": len(item["label"])}
        for item in checklist
        if len(item["label"]) > CHECKLIST_LABEL_MAX
    ]


def checklist_sans_duree(checklist: Iterable[dict]) -> list[dict]:
    """Items d'une checklist déjà normalisée dont l'estimation (dureeMin) est absente
    (brief t-27).

    Pure et sans effet de bord, même contrat que checklist_hors_convention() ci-dessus :
    ne pose rien, ne refuse rien, se contente de mesurer. .get() et non [] : un critère
    créé avant ce champ n'a jamais porté la clé "dureeMin" sur disque, et l'absence de la
    clé doit se lire exactement comme une valeur None -- les 349 critères existants sans
    estimation doivent continuer de fonctionner (c8), jamais lever de KeyError.
    """
    return [{"id": item["id"]} for item in checklist if item.get("dureeMin") is None]


def _duree_reelle_min(task: dict) -> float | None:
    """Minutes-Claude réellement écoulées entre lancement et rapport d'une tâche (brief
    t-33) : startedAt jusqu'à finishedAt, l'horodatage posé par report.apply() au moment
    exact où le rapport final est appliqué.

    None dès que le couple manque ou est incohérent (fin <= début) : une tâche pareille
    doit être IGNORÉE par l'appelant, jamais comptée comme zéro -- une valeur fausse
    contaminerait la médiane de toutes les autres (piège du brief).
    """
    debut = _epoch(task.get("startedAt"))
    fin = _epoch(task.get("finishedAt"))
    if debut is None or fin is None or fin <= debut:
        return None
    return (fin - debut) / 60


def _duree_reelle_par_critere(task: dict) -> float | None:
    """Minutes-Claude réellement passées, en moyenne, sur chaque critère réellement
    franchi (coché) d'une tâche (brief t-33) : sa durée réelle divisée par son nombre de
    critères cochés. None si la tâche n'a aucun critère coché, ou si sa durée réelle
    elle-même est inconnue (voir _duree_reelle_min).
    """
    duree = _duree_reelle_min(task)
    if duree is None:
        return None
    franchis = sum(1 for item in task["checklist"] if item.get("done"))
    if franchis == 0:
        return None
    return duree / franchis


def mediane_duree_par_critere(state: dict) -> float | None:
    """Médiane -- PAS la moyenne (brief t-33, piège 1) -- des minutes-Claude réellement
    passées par critère, calculée sur TOUTES les tâches "done" de ce home, tous chantiers
    confondus : c'est le seul repère qui ne dépend pas d'une intuition humaine, qu'aucun
    humain n'a sur cette échelle. Une tâche non "done" (en cours, bloquée, annulée) n'est
    jamais mesurée : sa durée n'est pas encore un fait acquis.

    None sur un home neuf sans aucune tâche terminée mesurable (piège 2) : ne pas planter,
    ne jamais poser zéro, laisser l'appelant s'abstenir de toute estimation par défaut.
    """
    valeurs = [
        v
        for t in state["taches"].values()
        if t.get("state") == "done"
        for v in (_duree_reelle_par_critere(t),)
        if v is not None
    ]
    if not valeurs:
        return None
    return statistics.median(valeurs)


def _duree_calculee(defaut_min: float | None) -> int | None:
    """Durée par défaut à poser sur un critère créé sans estimation (brief t-33) : la
    médiane arrondie à l'entier le plus proche, jamais à zéro (round() peut y tomber sur
    une médiane très basse ; zéro se lirait comme une absence d'estimation plutôt que
    comme une très courte -- voir dureeMin=None). None tant qu'aucune médiane n'est
    disponible (home neuf, piège 2) : rien à poser, la case reste vide comme aujourd'hui.
    """
    if defaut_min is None:
        return None
    return max(1, round(defaut_min))


def ecart_estime_reel(task: dict) -> dict | None:
    """Confrontation estimé/réel d'une tâche TERMINÉE, en minutes-Claude (brief t-33) :
    l'estimé est la somme des dureeMin connues de sa checklist (cochées comprises), le
    réel est mesuré entre lancement et rapport (_duree_reelle_min). C'est cette
    confrontation, DANS LES DEUX SENS, qui rend visible une tâche sur-estimée aussi bien
    qu'une tâche sous-estimée : une tâche annoncée 110 minutes et faite en 17 doit le dire
    (brief t-33), pas seulement un dépassement au-delà de l'estimé (voir
    carte._depassement_min, brief t-27, qui ne regarde que le sens inverse).

    None tant que la tâche n'est pas terminée, que son couple d'horodatages est absent ou
    incohérent, ou qu'aucun critère de sa checklist ne porte d'estimation -- rien à
    comparer.
    """
    if task.get("state") != "done":
        return None
    reel = _duree_reelle_min(task)
    if reel is None:
        return None
    estimes = [
        item["dureeMin"] for item in task["checklist"] if item.get("dureeMin") is not None
    ]
    if not estimes:
        return None
    estime_min = sum(estimes)
    reel_min = round(reel)
    return {"estimeMin": estime_min, "reelMin": reel_min, "ecartMin": reel_min - estime_min}


# Types d'événements du journal machine (t-34, voir journal.enregistrer_evenement) que
# duree_mesuree_par_critere() sait lire. Écrits par cli.py à chaque `ordo check` reçu, en
# direct : "checklist-doing" quand un item est déclaré attaqué, "checklist-coche" quand il
# est vraiment franchi.
_EVENEMENTS_DUREE = ("checklist-doing", "checklist-coche")


def duree_mesuree_par_critere(task: dict, evenements: Iterable[dict]) -> dict[str, float]:
    """Durée réelle mesurée INDIVIDUELLEMENT par critère (brief t-36), à partir des faits
    "checklist-doing" et "checklist-coche" du journal machine (t-34) : l'écart entre la
    déclaration --doing d'un critère (ou, à défaut, la coche précédente de la même tâche,
    ou au tout début son lancement) et sa propre coche.

    Remplace, pour qui veut la durée d'UN critère précis, la seule mesure disponible
    jusqu'ici (_duree_reelle_par_critere ci-dessus) : celle-ci divise la durée totale de la
    tâche par son nombre de critères et suppose donc qu'ils se valent tous -- ils ne se
    valent pas, les mesures réelles vont de 1,3 à 11 minutes selon le critère (brief t-36).

    Fonction PURE, comme le reste de ce fichier : evenements est fourni par l'appelant
    (typiquement journal.lire_evenements(chantier_id)), jamais lu ici -- chantier.py ne
    peut pas importer journal.py, qui l'importe déjà, sous peine de cycle. evenements n'a
    besoin d'être ni filtré ni trié par l'appelant : seuls les faits de CETTE tâche et des
    types "checklist-doing"/"checklist-coche" sont retenus, retriés par "at" plutôt que de
    faire confiance à l'ordre d'entrée.

    Un critère jamais coché, ou coché sans aucun repère de départ exploitable (horodatage
    illisible, ou coche antérieure ou égale à son repère -- horloges qui divergent),
    n'apparaît pas dans le résultat plutôt que d'y porter une valeur fausse ou négative qui
    fausserait toute médiane calculée dessus plus tard.
    """
    pertinents = sorted(
        (
            evt
            for evt in evenements
            if evt.get("tache") == task["id"] and evt.get("type") in _EVENEMENTS_DUREE
        ),
        key=lambda evt: evt.get("at") or "",
    )
    depart_doing: dict[str, float] = {}
    repere = _epoch(task.get("startedAt"))
    durees: dict[str, float] = {}
    for evt in pertinents:
        item_id = evt.get("item")
        at = _epoch(evt.get("at"))
        if not item_id or at is None:
            continue
        if evt["type"] == "checklist-doing":
            depart_doing[item_id] = at
            continue
        depart = depart_doing.pop(item_id, repere)
        if depart is not None and at > depart:
            durees[item_id] = (at - depart) / 60
        repere = at
    return durees


# Plafond d'un creux plausible entre deux repères d'une même tâche (brief t-45). Choisi en
# mesurant les deux seuls journaux machine accessibles (ordo-public, camcast, brief t-45) :
# les creux réels entre checklist-doing/checklist-coche vont de 0,02 à 14,2 minutes, et la
# plus grosse estimation JAMAIS posée à la main sur un critère (brief t-33/t-36) est de 25
# minutes. Une heure laisse une marge large sur ces deux mesures tout en écartant nettement
# toute vraie pause (repas, nuit) : dans les mêmes journaux, l'écart suivant le plus proche
# au-dessus de 14 minutes se compte en heures, jamais en dizaines de minutes -- pas de creux
# "moyen" entre les deux à départager.
CREUX_MAX_MIN = 60.0


def _reperes_tache(task: dict, evenements: Iterable[dict]) -> list[float] | None:
    """Repères chronologiques (epoch UTC) d'une tâche, du lancement au rapport, en passant
    par chaque checklist-doing/checklist-coche du journal machine (t-34) qui la concerne et
    tombe strictement entre les deux (brief t-45).

    None dès que startedAt ou finishedAt manque ou est incohérent -- même garde que
    _duree_reelle_min : sans ce couple, il n'y a rien à découper.
    """
    debut = _epoch(task.get("startedAt"))
    fin = _epoch(task.get("finishedAt"))
    if debut is None or fin is None or fin <= debut:
        return None
    reperes_internes = sorted(
        at
        for evt in evenements
        if evt.get("tache") == task["id"] and evt.get("type") in _EVENEMENTS_DUREE
        for at in (_epoch(evt.get("at")),)
        if at is not None and debut < at < fin
    )
    return [debut, *reperes_internes, fin]


def duree_travail_min(task: dict, evenements: Iterable[dict]) -> float | None:
    """Temps de TRAVAIL d'une tâche terminée, en minutes-Claude (brief t-45) : distinct du
    délai d'horloge brut (_duree_reelle_min), qui compte les nuits et les pauses comme si
    c'était du travail -- exactement ce qui a fait échouer l'estimation à l'aveugle de t-42
    (erreur médiane 118% de la durée médiane).

    Principe : découper le délai brut de la tâche en repères chronologiques (lancement,
    chaque checklist-doing/checklist-coche du journal machine t-34, rapport), plafonner
    CHAQUE intervalle entre deux repères consécutifs à CREUX_MAX_MIN et sommer les
    intervalles plafonnés. Un intervalle anormalement long (nuit, repas, changement de
    session) ne contribue jamais plus de CREUX_MAX_MIN minutes au total ; le reste est
    retranché, jamais compté comme du travail.

    Une tâche SANS repère intermédiaire (antérieure au journal machine t-34, ou jamais
    marquée --doing/coché en cours de route) n'a qu'un seul intervalle : son délai brut
    entier, de bout en bout. Rien à plafonner en confiance dans ce cas précis -- plafonner
    un délai qu'on ne peut pas découper serait deviner un temps de travail, pas le mesurer
    (invariant 2 du brief t-45). Un délai brut déjà sous CREUX_MAX_MIN reste tel quel : un
    travail continu plausible. Au-delà, la tâche est EXCLUE (None) plutôt que devinée --
    exactement le cas d'une tâche lancée le soir et rendue le lendemain sans aucune trace
    intermédiaire.
    """
    reperes = _reperes_tache(task, evenements)
    if reperes is None:
        return None
    if len(reperes) == 2:
        brut = (reperes[1] - reperes[0]) / 60
        return brut if brut <= CREUX_MAX_MIN else None
    return sum(
        min((apres - avant) / 60, CREUX_MAX_MIN) for avant, apres in zip(reperes, reperes[1:])
    )


def _session_unique(state: dict, slug: str, chantier_id: str) -> str:
    """Point A : nom de session tmux, unique par construction.

    "ordo-<slug>" si aucun chantier EXISTANT, quel que soit son etat (ouvert ou clos), ne
    porte deja ce tmuxSession, sinon "ordo-<slug>-<chantier_id>". Sans ce garde-fou, deux
    projets de meme basename (/a/api et /b/api) recoivent la meme session et leurs
    executantes atterrissent dans la meme fenetre tmux. Le nom est fige a la creation et
    ne se recalcule jamais : les appels suivants a start() ne relisent que les chantiers
    deja presents dans state au moment de cet appel-ci.
    """
    base = f"ordo-{slug}"
    deja_pris = any(c.get("tmuxSession") == base for c in state["chantiers"].values())
    return f"{base}-{chantier_id}" if deja_pris else base


def _refuse_si_home_partage_invalide(state: dict, canon_project: str, home_partage: bool) -> None:
    """Point D : un ORDO_HOME par projet. Refuse un second projet different tant qu'un
    chantier OUVERT du meme home le porte deja, sauf home_partage=True (I8 : l'echappatoire
    ne contourne jamais en silence, elle est un parametre explicite de l'appelant).
    """
    if home_partage:
        return
    conflit = next(
        (
            c
            for c in state["chantiers"].values()
            if c["state"] == "open" and c["project"] != canon_project
        ),
        None,
    )
    if conflit is not None:
        raise ChantierError(
            f"campaign {conflit['id']} already open on {conflit['project']} in this "
            "ORDO_HOME; the convention is one ORDO_HOME per project "
            "(export ORDO_HOME=$PWD/.ordo); pass home_partage=True to share "
            "this home between several projects knowingly"
        )


def start(
    project: str | Path,
    objectif: str,
    perimetre: str = "",
    hors_scope: str = "",
    home_partage: bool = False,
    permissions: str = "skip",
    keep_panes: bool = False,
) -> dict:
    """Ouvre un chantier. Le chemin du projet passe par canon() avant d'etre stocke (I3).

    Le repertoire doit EXISTER. canon() d'un chemin relatif inexistant rend un chemin
    absolu plausible mais faux : "skill-test" lance depuis /a/skill-test a donne
    /a/skill-test/skill-test, et le chantier s'est ouvert sans un mot sur un dossier
    fantome. Toute executante lancee dessus aurait echoue au demarrage, loin de la cause.
    Trouve par une vraie session le 9 aout 2026, en recette du skill.
    """
    canon_project = store.canon(project)
    if not canon_project:
        raise ChantierError("the project path is empty")
    if not Path(canon_project).is_dir():
        raise ChantierError(
            f"project {canon_project} is not an existing directory; "
            "give a path, not a name (for example $PWD)"
        )
    if permissions not in PERMISSIONS_VALUES:
        raise ChantierError(
            f"invalid permissions: {permissions!r}, expected {' or '.join(PERMISSIONS_VALUES)}"
        )
    with store.locked() as state:
        _refuse_si_home_partage_invalide(state, canon_project, home_partage)
        chantier_id = store.next_id(state, "chantier")
        slug = _slug(canon_project)
        chantier = {
            "id": chantier_id,
            "slug": slug,
            "project": canon_project,
            "objectif": objectif,
            "perimetre": perimetre,
            "horsScope": hors_scope,
            "state": "open",
            "tmuxSession": _session_unique(state, slug, chantier_id),
            "tmuxWindow": None,
            "permissions": permissions,
            "keepPanes": keep_panes,
            "createdAt": store.now(),
            "closedAt": None,
            "capteur": {
                "path": None,
                "runs": [],
                "adopted": False,
                "adoptedAt": None,
                "lastSuccess": None,
                "lastError": None,
                "consecutiveFailures": 0,
                "identicalCycles": 0,
            },
            "lastWake": None,
            "lastEvent": None,
        }
        state["chantiers"][chantier_id] = chantier
    return chantier


def _alive_tasks(
    state: dict, chantier_id: str, alive_check: Callable[[str], bool] | None
) -> list[str]:
    """Tache par tache "running" du chantier, filtrees par alive_check si fourni.

    Point d'injection documente : panes.py n'est pas visible depuis ce module. Sans
    alive_check, toute tache "running" compte comme vivante (le seul signal disponible
    ici). Un appelant qui a acces a panes.alive(pane_id) peut passer cette fonction pour
    vérifier la vivacité réelle du pane plutôt que de se fier au seul état déclaré.
    """
    alive = []
    for task in state["taches"].values():
        if task["chantier"] != chantier_id or task["state"] != "running":
            continue
        if alive_check is not None:
            pane_id = task.get("paneId")
            if pane_id is None or not alive_check(pane_id):
                continue
        alive.append(task["id"])
    return alive


def _move_if_exists(src: Path, dest_dir: Path) -> str | None:
    """Deplace src sous dest_dir si src existe ; sinon ne fait rien (I8 : un fichier absent
    n'est pas une erreur, ce n'est pas un refus a signaler). Renvoie le chemin de
    destination en chaine, ou None si rien n'a ete deplace.
    """
    if not src.exists():
        return None
    dest = dest_dir / src.name
    shutil.move(str(src), str(dest))
    return str(dest)


def _archive_chantier(chantier_id: str, task_ids: Iterable[str]) -> list[str]:
    """Deplace briefs/<chantier>/<tache>.md, reports/<chantier>/<tache>.json,
    journal/<chantier>.md et sensors/<chantier>* sous archives/<chantier>/, en creant le
    repertoire. Un fichier absent n'est pas une erreur (point E). Renvoie la liste des
    chemins reellement deplaces.

    Les briefs et les rapports sont ranges par chantier a la source : aucun fichier d'un
    autre chantier ne peut se trouver sur le chemin balaye ici, la garantie ne repose plus
    sur la seule unicite des identifiants de tache.
    """
    home = store.home()
    dest_dir = home / "archives" / chantier_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    archives: list[str] = []
    briefs_dir = home / "briefs" / chantier_id
    reports_dir = home / "reports" / chantier_id
    for task_id in task_ids:
        for src in (briefs_dir / f"{task_id}.md", reports_dir / f"{task_id}.json"):
            moved = _move_if_exists(src, dest_dir)
            if moved is not None:
                archives.append(moved)
    # Les deux repertoires du chantier n'ont plus de raison d'exister une fois vides.
    # rmdir echoue si quelque chose y reste ; ce cas se tait, il n'y a rien a corriger.
    for vide in (briefs_dir, reports_dir):
        with suppress(OSError):
            vide.rmdir()
    moved = _move_if_exists(home / "journal" / f"{chantier_id}.md", dest_dir)
    if moved is not None:
        archives.append(moved)
    capteurs_dir = home / "sensors"
    if capteurs_dir.is_dir():
        # Frontiere exacte, pas un simple prefixe : un glob "c-1*" matcherait a tort
        # "c-10.py" en fermant le chantier "c-1". next_id() ne produit jamais un tel id
        # (toujours 2 chiffres mini), mais au-dela de 99 chantiers "c-10" redevient un
        # prefixe litteral de "c-100" ; on exige donc soit une egalite exacte du nom,
        # soit un point immediatement apres l'id (install() nomme "<chantier_id><suffix>",
        # suffix commencant par le point de l'extension).
        for capteur_file in sorted(capteurs_dir.iterdir()):
            if capteur_file.name == chantier_id or capteur_file.name.startswith(f"{chantier_id}."):
                moved = _move_if_exists(capteur_file, dest_dir)
                if moved is not None:
                    archives.append(moved)
    return archives


def close(
    chantier_id: str,
    force: bool = False,
    alive_check: Callable[[str], bool] | None = None,
) -> tuple[dict, dict]:
    """Ferme un chantier et l'archive. Refuse si des executantes vivent, sauf force=True.

    Voir _alive_tasks pour le point d'injection alive_check. Ne tue rien elle-meme : ce
    module n'importe jamais panes.py (E), c'est cli.py qui tuera les panes avec la liste
    rendue ici. Renvoie (chantier, info) ou info = {"panes": [...], "session": "...",
    "archives": [...]} : panes liste les pane_id des taches du chantier qui en portent un,
    pour que l'appelant sache quoi tuer ; archives liste les fichiers reellement deplaces.
    """
    with store.locked() as state:
        chantier = _get_chantier(state, chantier_id)
        if not force:
            alive = _alive_tasks(state, chantier_id, alive_check)
            if alive:
                raise ChantierError(
                    f"close refused for {chantier_id}: executors alive on "
                    f"{', '.join(alive)} (force=True to force)"
                )
        chantier["state"] = "closed"
        chantier["closedAt"] = store.now()
        taches_du_chantier = [t for t in state["taches"].values() if t["chantier"] == chantier_id]
        panes = [t["paneId"] for t in taches_du_chantier if t.get("paneId")]
        session = chantier.get("tmuxSession")
        task_ids = [t["id"] for t in taches_du_chantier]

    archives = _archive_chantier(chantier_id, task_ids)
    return chantier, {"panes": panes, "session": session, "archives": archives}


def add_task(
    chantier_id: str,
    titre: str,
    prompt: str,
    depends_on: Sequence[str] = (),
    touches: Sequence[str] = (),
    checklist: Sequence = (),
    why: str = "",
) -> dict:
    """Cree une tache. Chaque dependance declaree doit deja exister ET appartenir au meme
    chantier (I8, point F) : sans ce garde-fou, un `add t --depend-on t-09` entre deux
    projets sans rapport etait accepte en silence.

    `why` est la raison d'etre de la tache, en clair : pourquoi elle existe, pourquoi ici
    dans le decoupage, pas ce qu'elle fait. Ce n'est pas un doublon du titre ni du prompt.
    Le titre nomme la tache, le prompt dit comment la faire, et personne, pas meme l'humain
    qui a lance le chantier, ne peut deduire de l'un ou de l'autre pourquoi
    l'orchestratrice a decoupe ainsi. Facultatif dans la signature parce qu'un etat ecrit
    avant ce champ reste lisible ; ce qui l'exige est le brief d'orchestratrice, et
    carte.model() liste sous "missingWhy" toute tache vivante qui s'en passe.
    """
    with store.locked() as state:
        _get_chantier(state, chantier_id)
        for dep_id in depends_on:
            dep_task = _get_task(state, dep_id)
            if dep_task["chantier"] != chantier_id:
                raise ChantierError(
                    f"dependency refused: {dep_id} belongs to campaign "
                    f"{dep_task['chantier']}, not to {chantier_id}"
                )
        task_id = store.next_id(state, "tache")
        task = {
            "id": task_id,
            "chantier": chantier_id,
            "titre": titre,
            "prompt": prompt,
            "why": why,
            "state": "queued",
            "dependsOn": list(depends_on),
            "touches": list(touches),
            "checklist": _normalize_checklist(checklist, mediane_duree_par_critere(state)),
            "currentItem": None,
            "priority": 0,
            "attempts": 0,
            "model": None,
            "paneId": None,
            "cwd": None,
            "createdAt": store.now(),
            "startedAt": None,
            "finishedAt": None,
            "lastReportAt": None,
            "report": None,
            "error": None,
            "blockedCause": None,
            "notes": [],
        }
        state["taches"][task_id] = task
    return task


def depend(task_id: str, on_id: str) -> dict:
    """Ajoute une dependance. Refuse si elle fermerait un cycle (I7), ou si les deux taches
    n'appartiennent pas au meme chantier (I8, point F), sans rien laisser.
    """
    with store.locked() as state:
        task = _get_task(state, task_id)
        on_task = _get_task(state, on_id)
        if on_id == task_id:
            raise ChantierError(f"dependency refused: {task_id} cannot depend on itself")
        if task["chantier"] != on_task["chantier"]:
            raise ChantierError(
                f"dependency refused: {task_id} belongs to campaign {task['chantier']}, "
                f"{on_id} belongs to campaign {on_task['chantier']}"
            )
        if on_id in task["dependsOn"]:
            return task
        task["dependsOn"].append(on_id)
        cycle = has_cycle(state["taches"])
        if cycle:
            task["dependsOn"].remove(on_id)
            raise ChantierError(
                "dependency refused, cycle detected: " + " -> ".join(cycle)
            )
    return task


def cancel(task_id: str) -> dict:
    """Annule une tache. Refuse si elle est deja dans un etat terminal (I8)."""
    with store.locked() as state:
        task = _get_task(state, task_id)
        if task["state"] in ("done", "cancelled"):
            raise ChantierError(f"cancel refused: {task_id} is already {task['state']}")
        task["state"] = "cancelled"
        task["finishedAt"] = store.now()
        task["currentItem"] = None
    return task


def prioritize(task_id: str, n: int) -> dict:
    """Fixe la priorite d'une tache."""
    with store.locked() as state:
        task = _get_task(state, task_id)
        task["priority"] = n
    return task


def amend(task_id: str, prompt: str) -> dict:
    """Modifie le prompt d'une tache. Refuse si elle tourne deja (I8).

    Une exécutante qui a deja recu son brief travaille sur l'ancien texte ; changer le
    prompt sous ses pieds sans le lui dire romprait l'alignement que le brief garantit.
    """
    with store.locked() as state:
        task = _get_task(state, task_id)
        if task["state"] == "running":
            raise ChantierError(
                f"amend refused: {task_id} is currently running"
            )
        task["prompt"] = prompt
    return task


def check(task_id: str, item_id: str, done: bool = True, doing: bool = False) -> dict:
    """Coche ou décoche un item de la checklist d'une tâche, ou déclare l'item attaqué.

    doing=True pose task["currentItem"] = item_id SANS toucher à item["done"] : c'est le
    "je m'y mets", distinct du "j'ai fini" (done). Sans lui, la carte ne montre qu'un
    compteur ("checks 2/5") qui n'avance qu'à la fin de chaque item, et rien ne dit sur
    quoi l'exécutante travaille entre deux coches.

    doing=False (l'usage normal) coche ou décoche l'item comme avant, et libère
    currentItem si l'item ainsi coché/décoché est celui déclaré en cours : une fois
    l'item fini, il n'y a plus d'item "en cours" tant qu'un nouveau n'est pas déclaré.
    Cocher un AUTRE item que celui déclaré ne touche pas currentItem.
    """
    with store.locked() as state:
        task = _get_task(state, task_id)
        for item in task["checklist"]:
            if item["id"] == item_id:
                if doing:
                    task["currentItem"] = item_id
                else:
                    item["done"] = done
                    if task.get("currentItem") == item_id:
                        task["currentItem"] = None
                break
        else:
            raise ChantierError(
                f"item not found: {item_id} does not exist in the checklist of {task_id}"
            )
    return task


# Motif des id générés par _normalize_checklist() ("c1", "c2", ...). Sert uniquement à
# repérer le plus grand numéro déjà pris ; un id qui ne suit pas cette forme (checklist
# écrite à la main avec des id personnalisés) n'entre simplement pas dans le calcul.
_CHECKLIST_ID_RE = re.compile(r"^c(\d+)$")


def _next_checklist_id(checklist: Iterable[dict]) -> str:
    """Id frais pour un nouvel item de checklist, jamais une renumérotation (brief t-22).

    Une exécutante en cours a lu "c3" dans son brief et coche "c3" : quoi qu'on ajoute ou
    découpe ensuite, cet identifiant doit continuer à désigner exactement le même critère.
    Le nouvel id reprend donc toujours au-delà du plus grand numéro "c<N>" déjà vu dans la
    checklist, jamais en comblant un trou ni en renommant un voisin. La boucle finale est
    la garantie ultime, pas le chemin normal : elle protège du seul cas où le calcul par
    numéro ne suffirait pas (un id personnalisé qui porte déjà, par coïncidence, le
    prochain numéro attendu).
    """
    plus_grand = 0
    ids_pris = set()
    for item in checklist:
        ids_pris.add(item["id"])
        trouve = _CHECKLIST_ID_RE.match(item["id"])
        if trouve:
            plus_grand = max(plus_grand, int(trouve.group(1)))
    n = plus_grand + 1
    candidat = f"c{n}"
    while candidat in ids_pris:
        n += 1
        candidat = f"c{n}"
    return candidat


def add_checklist_item(task_id: str, label: str) -> dict:
    """Ajoute un critère en fin de checklist d'une tâche déjà créée (brief t-22).

    Geste ouvert à l'exécutante elle-même : c'est en travaillant qu'on découvre qu'un pan
    du travail n'avait pas été prévu, et le contrat lui interdit de se déclarer finie en
    passant ce pan sous silence. Toujours ajouté en DERNIÈRE position d'affichage, jamais
    inséré entre deux items existants : un item ne change donc jamais de voisin une fois
    créé, seul son propre id est nouveau (voir _next_checklist_id). Ne vérifie pas la
    convention de longueur (CHECKLIST_LABEL_MAX) : c'est un plafond signalé, jamais un
    refus, à l'appelant (cli.py) de le rapporter après coup, comme pour add_task.

    Reçoit, comme add_task, la médiane historique du home en défaut (brief t-33) : un
    critère ajouté après coup n'a pas plus d'intuition sur sa propre durée qu'un critère
    posé à la création.
    """
    with store.locked() as state:
        task = _get_task(state, task_id)
        new_id = _next_checklist_id(task["checklist"])
        item = {"id": new_id, "label": label, "done": False, "dureeMin": None}
        calculee = _duree_calculee(mediane_duree_par_critere(state))
        if calculee is not None:
            item["dureeMin"] = calculee
            item["dureeDefaut"] = True
        task["checklist"].append(item)
    return task


def split_checklist_item(task_id: str, item_id: str, label_un: str, label_deux: str) -> dict:
    """Découpe un critère existant en deux (brief t-22).

    item_id GARDE son identifiant et prend label_un : toute référence déjà lue par
    l'exécutante dans son brief continue de désigner la même case. label_deux part sous un
    id frais (_next_checklist_id), ajouté en fin de liste, jamais inséré entre deux items
    déjà présents : le seul rang qui bouge est celui du nouveau venu.

    Les deux moitiés repartent à done=False, même si l'item d'origine était déjà coché :
    l'état "fait" d'un critère fusionné ne prouve rien sur chacune des deux moitiés prises
    séparément, et le conserver aurait permis de gonfler le compteur sans travail réel.

    La durée SE RÉPARTIT entre les deux moitiés, elle ne se duplique jamais (brief t-27) :
    un split qui laisserait chaque moitié porter la durée entière de l'original ferait
    gonfler le total estimé de la tâche à chaque découpage, sans qu'aucun travail
    supplémentaire n'ait été prévu. La division entière perd au plus une minute, reversée à
    la première moitié plutôt que perdue en silence (10+11 sur 21, jamais 10+10).
    """
    with store.locked() as state:
        task = _get_task(state, task_id)
        for item in task["checklist"]:
            if item["id"] == item_id:
                duree_min = item.get("dureeMin")
                if duree_min is None:
                    duree_un, duree_deux = None, None
                else:
                    duree_deux = duree_min // 2
                    duree_un = duree_min - duree_deux
                item["label"] = label_un
                item["done"] = False
                item["dureeMin"] = duree_un
                break
        else:
            raise ChantierError(
                f"item not found: {item_id} does not exist in the checklist of {task_id}"
            )
        new_id = _next_checklist_id(task["checklist"])
        task["checklist"].append(
            {"id": new_id, "label": label_deux, "done": False, "dureeMin": duree_deux}
        )
    return task


def set_checklist_duree(task_id: str, item_id: str, minutes: int) -> dict:
    """Pose ou corrige l'estimation d'un critère, en MINUTES-CLAUDE (brief t-27).

    Minutes-Claude, jamais minutes humaines : le temps qu'une session Claude Code met à
    franchir ce critère, pas celui qu'un humain y passerait -- deux échelles sans rapport
    l'une avec l'autre. Ouvert à l'exécutante elle-même, comme add/split/reword : une
    estimation posée avant d'avoir lu le code est fausse, celle posée après vaut quelque
    chose, et rien ne doit empêcher de la corriger à tout moment.

    minutes doit être strictement positif : zéro ou négatif n'est pas une estimation basse,
    c'est une absence d'estimation qui a déjà sa représentation propre (dureeMin=None) --
    les confondre ferait lire un vrai zéro là où on ne sait simplement rien.
    """
    if minutes <= 0:
        raise ChantierError(
            f"duration refused: {minutes} is not a positive number of minutes"
        )
    with store.locked() as state:
        task = _get_task(state, task_id)
        for item in task["checklist"]:
            if item["id"] == item_id:
                item["dureeMin"] = minutes
                break
        else:
            raise ChantierError(
                f"item not found: {item_id} does not exist in the checklist of {task_id}"
            )
    return task


# Jeu d'attributs ARRETE (brief t-36), sur décision d'architecture de l'humain : un
# critère porte des attributs qui décrivent sa NATURE, posés dès la création ou révisés
# après coup -- jamais une prédiction de durée, seulement une description objective sur
# laquelle une durée mesurée (voir duree_mesuree_par_critere) pourra un jour s'entraîner.
#
# Cinq clés, chacune fermée à un petit jeu de valeurs -- jamais du texte libre, sinon
# l'attribut redevient un jugement, exactement ce qu'il doit remplacer :
#
# - geste : la nature du travail que fait l'exécutante sur ce critère. lire/ecrire
#   distinguent la lecture pure de la production de code ; tester isole la preuve
#   automatisée ; mesurer couvre toute observation empirique (capture, log, sortie d'une
#   commande) qui n'est pas un test au sens strict ; publier couvre tout geste externe
#   (déploiement, envoi) dont le coût n'a rien à voir avec le reste.
# - etendue : le nombre de fichiers concernés par le critère, tel qu'écrit dans son
#   libellé ou son brief -- un fichier nommé, plusieurs (un module), ou le chantier
#   entier (une invariante qui traverse tout le dépôt).
# - dépendance : ce dont le critère a besoin en dehors du code source lui-même. Distincte
#   de "geste" : un même geste "tester" peut ne dépendre de rien (assertion pure) ou d'un
#   pane tmux bien réel.
# - incertitude : le chemin de la correction ou de l'ajout est-il déjà nommé dans le
#   brief (connu), ou faut-il d'abord chercher/diagnostiquer avant d'agir (a_trouver) --
#   c'est l'écart le plus net observé entre les critères les plus lents et les plus
#   rapides du dépôt (brief t-36, section "regarde les critères qui ont pris 11 minutes").
# - validation : ce qui tranche que le critère est vraiment franchi. assertion quand un
#   test suffit, déterministe ; observation quand il faut regarder un résultat (rendu,
#   capture, sortie) et en juger la conformité -- orthogonal à "geste", un critère "tester"
#   peut relever de l'un ou de l'autre selon qu'il s'agit d'une assertion ou d'un témoin à
#   lire.
#
# Validés contre les 350 critères réels du dépôt (mesure de durée moyenne par tâche,
# 0,8 à 11 minutes) plutôt que devinées : voir le rapport de t-36 pour le détail des
# exemples retenus et rejetés. Volontairement PAS de septième ni huitième attribut :
# au-delà de sept personne ne les pose correctement (brief t-36), la donnée deviendrait du
# bruit plutôt qu'un signal.
ATTRIBUTS_VALEURS = {
    "geste": ("lire", "ecrire", "tester", "mesurer", "publier"),
    "etendue": ("fichier", "module", "chantier"),
    "dependance": ("aucune", "tmux", "reseau", "navigateur"),
    "incertitude": ("connu", "a_trouver"),
    "validation": ("assertion", "observation"),
}


def set_checklist_attribut(task_id: str, item_id: str, cle: str, valeur: str) -> dict:
    """Pose ou corrige UN attribut d'un critère (brief t-36), même régime que
    set_checklist_duree juste au-dessus : posable à la création comme après coup, ouvert à
    l'exécutante elle-même, une seule clé à la fois pour que chaque appel reste explicite
    sur ce qui a changé.

    clé doit être l'une des ATTRIBUTS_VALEURS ci-dessus et valeur l'une des valeurs
    permises pour cette clé -- refuse sinon (I8) : une clé ou une valeur libres rendraient
    l'attribut subjectif, exactement ce que le brief interdit (objectif, vérifiable sans
    jugement).

    Un critère qui n'a jamais reçu cet appel n'a pas la clé "attributs" du tout (piège du
    brief, c6) : les 350 critères déjà sur disque, et tout nouveau critère tant que
    personne ne pose un attribut dessus, restent parfaitement valides sans elle.
    """
    valeurs_permises = ATTRIBUTS_VALEURS.get(cle)
    if valeurs_permises is None:
        raise ChantierError(
            f"attribute refused: {cle!r} is not one of {', '.join(ATTRIBUTS_VALEURS)}"
        )
    if valeur not in valeurs_permises:
        raise ChantierError(
            f"attribute refused: {valeur!r} is not a valid value for {cle} "
            f"({', '.join(valeurs_permises)})"
        )
    with store.locked() as state:
        task = _get_task(state, task_id)
        for item in task["checklist"]:
            if item["id"] == item_id:
                item.setdefault("attributs", {})[cle] = valeur
                break
        else:
            raise ChantierError(
                f"item not found: {item_id} does not exist in the checklist of {task_id}"
            )
    return task


# Estimation algorithmique par attributs (brief t-43), sur demande explicite de l'humain :
# un calcul déterministe, jamais un jugement d'IA, qui se relit et se refait au crayon.
#
# RÉSERVE, à lire avant tout le reste : pour les tâches terminées avant l'horodatage des
# coches (t-34), seule la durée TOTALE de la tâche est mesurée -- aucune durée par critère
# individuelle n'existe dans cet historique. Chaque critère coché d'une tâche "done" reçoit
# donc comme cible d'entraînement le même partage moyen de sa tâche
# (_duree_reelle_par_critere), pas une mesure qui lui soit propre : on apprend sur des
# sommes d'attributs par tâche, jamais sur des critères isolés. Une tâche à beaucoup de
# critères cochés pèse donc plus lourd dans l'entraînement qu'une tâche qui en a peu -- une
# limite du jeu de données actuel, pas un choix, à corriger le jour où l'historique aura
# assez de couples doing/coche pour que duree_mesuree_par_critere() serve de cible à la
# place.
SEUIL_OBSERVATIONS_ATTRIBUT = 10
# Sous ce nombre d'observations pour une VALEUR d'attribut donnée (ex. geste=publier), son
# facteur n'est pas retenu (contrainte 3 du brief) : "vue trois fois" ne permet aucune
# inférence, dix est le plus petit palier rond qui laisse une marge large au-dessus -- sur
# les deux seuls homes mesurés pour ce brief, les facteurs retenus sont rigoureusement les
# mêmes pour tout seuil entre 8 et 20, signe que 10 tombe au milieu d'un plateau stable et
# non sur une cassure arbitraire.

K_PLIS = 5
SEUIL_TACHES_CV = 15
# Plancher de tâches (groupées, pas de critères isolés) avant de même tenter la validation
# croisée : avec K_PLIS=5, 15 tâches donnent trois tâches de test par pli en moyenne. En
# dessous, une seule tâche inhabituelle dans un pli suffit à faire basculer le verdict --
# ce serait un artefact du découpage, pas un signal. Sous ce plancher,
# validation_croisee_estimation() rend None et estimation_critere() s'en tient à la
# médiane, exactement le "on n'estime pas" de la contrainte 3.


def observations_par_critere(state: dict) -> list[tuple[str, list[tuple[dict, float]]]]:
    """Jeu d'observations d'entraînement (brief t-43, c1) : pour chaque tâche "done" de ce
    home dont la durée réelle est exploitable, la liste de ses critères COCHÉS qui portent
    des attributs (brief t-36), chacun associé à la cible partagée de sa tâche (voir la
    réserve ci-dessus). Groupé par tâche, jamais à plat, parce que la validation croisée a
    besoin de garder tous les critères d'une même tâche du même côté d'un pli -- les
    disperser romprait le découpage (une tâche "facile" verrait une partie de ses critères
    servir à s'évaluer elle-même).

    Trié par identifiant de tâche : reproductible d'un appel à l'autre, indépendant de
    l'ordre d'insertion dans state["taches"].

    Une tâche sans critère attribué, ou dont aucun critère coché ne porte d'attributs, est
    simplement absente du résultat -- rien à en tirer, pas une anomalie à signaler.
    """
    groupes = []
    for task_id in sorted(state["taches"]):
        task = state["taches"][task_id]
        if task.get("state") != "done":
            continue
        cible = _duree_reelle_par_critere(task)
        if cible is None:
            continue
        paires = [
            (item["attributs"], cible)
            for item in task.get("checklist", [])
            if item.get("done") and item.get("attributs")
        ]
        if paires:
            groupes.append((task_id, paires))
    return groupes


def _paires_a_plat(groupes: list[tuple[str, list[tuple[dict, float]]]]) -> list[tuple[dict, float]]:
    return [paire for _, paires in groupes for paire in paires]


def _facteurs(
    paires: list[tuple[dict, float]], mediane: float, seuil: int
) -> dict[str, dict[str, float]]:
    """Facteur multiplicatif médiane(cibles de cette valeur) / médiane globale, par valeur
    d'attribut retenue (brief t-43, c2) -- un attribut dont la valeur ne change rien à la
    durée rend un facteur proche de 1, un attribut qui sépare vraiment les critères s'en
    écarte. Valeur sous SEUIL_OBSERVATIONS_ATTRIBUT : absente du résultat plutôt que posée
    sur un échantillon trop mince (contrainte 3)."""
    par_cle: dict[str, dict[str, list[float]]] = {}
    for attributs, cible in paires:
        for cle, valeur in attributs.items():
            par_cle.setdefault(cle, {}).setdefault(valeur, []).append(cible)
    facteurs: dict[str, dict[str, float]] = {}
    for cle, par_valeur in par_cle.items():
        retenus = {
            valeur: statistics.median(cibles) / mediane
            for valeur, cibles in par_valeur.items()
            if len(cibles) >= seuil
        }
        if retenus:
            facteurs[cle] = retenus
    return facteurs


def facteurs_par_attribut(
    state: dict, seuil: int = SEUIL_OBSERVATIONS_ATTRIBUT
) -> dict[str, dict[str, float]] | None:
    """Écart mesuré par valeur d'attribut sur tout l'historique de ce home (brief t-43,
    c2) : c'est ce tableau qui dit quels attributs séparent vraiment les durées (facteur
    loin de 1, beaucoup d'observations) et lesquels n'expliquent rien (facteur proche de 1)
    -- une lecture directe, sans devoir relancer une validation croisée pour la voir.

    None si ce home n'a encore aucune tâche terminée mesurable (pas de médiane à rapporter
    contre) ; dict vide si des tâches existent mais qu'aucune valeur d'attribut n'atteint
    le seuil.
    """
    mediane = mediane_duree_par_critere(state)
    if mediane is None:
        return None
    paires = _paires_a_plat(observations_par_critere(state))
    return _facteurs(paires, mediane, seuil)


def _estimation_par_attributs(attributs: dict, mediane: float, facteurs: dict) -> float:
    """Produit des facteurs des attributs FOURNIS et retenus par l'entraînement -- une clé
    absente de `attributs`, une valeur jamais vue, ou un facteur sous le seuil (absent de
    `facteurs`) ne modifient pas l'estimation : la médiane reste la valeur neutre par
    défaut de chaque facteur manquant, jamais une supposition."""
    estimation = mediane
    for cle, valeur in attributs.items():
        facteur = facteurs.get(cle, {}).get(valeur)
        if facteur is not None:
            estimation *= facteur
    return estimation


def validation_croisee_estimation(
    state: dict, seuil: int = SEUIL_OBSERVATIONS_ATTRIBUT, plis: int = K_PLIS
) -> dict | None:
    """Erreur du modèle par attributs comparée à celle de la seule médiane, en validation
    croisée GROUPÉE PAR TÂCHE (brief t-43, c4) : chaque pli entraîne médiane et facteurs sur
    les autres plis, puis mesure l'écart absolu des deux méthodes sur les tâches qu'il n'a
    jamais vues à l'entraînement. Le découpage en plis est déterministe (indice de la tâche
    triée modulo `plis`), jamais tiré au hasard -- deux appels sur le même state rendent
    exactement le même résultat (contrainte 1).

    None si ce home a moins de SEUIL_TACHES_CV tâches groupées exploitables (contrainte 3) :
    en dessous, aucune comparaison n'est assez stable pour trancher, voir la justification
    de la constante.

    Rend {"taches": n, "maeMediane": ..., "maeAttributs": ..., "gagnant": "attributs" ou
    "mediane"} -- MAE (mean absolute error) en minutes-Claude, la même unité que dureeMin,
    directement lisible.
    """
    groupes = observations_par_critere(state)
    if len(groupes) < SEUIL_TACHES_CV:
        return None
    erreurs_mediane: list[float] = []
    erreurs_attributs: list[float] = []
    for pli in range(plis):
        entrainement = [g for i, g in enumerate(groupes) if i % plis != pli]
        test = [g for i, g in enumerate(groupes) if i % plis == pli]
        paires_entrainement = _paires_a_plat(entrainement)
        if not paires_entrainement:
            continue
        mediane_pli = statistics.median(cible for _, cible in paires_entrainement)
        facteurs_pli = _facteurs(paires_entrainement, mediane_pli, seuil)
        for _, paires in test:
            for attributs, cible in paires:
                erreurs_mediane.append(abs(mediane_pli - cible))
                estime = _estimation_par_attributs(attributs, mediane_pli, facteurs_pli)
                erreurs_attributs.append(abs(estime - cible))
    if not erreurs_mediane:
        return None
    mae_mediane = statistics.mean(erreurs_mediane)
    mae_attributs = statistics.mean(erreurs_attributs)
    return {
        "taches": len(groupes),
        "maeMediane": mae_mediane,
        "maeAttributs": mae_attributs,
        "gagnant": "attributs" if mae_attributs < mae_mediane else "mediane",
    }


def estimation_critere(state: dict, attributs: dict) -> int | None:
    """LA fonction du brief t-43 : pour un critère donné par ses attributs (brief t-36),
    rend une durée estimée en minutes-Claude, calculée sur l'historique de CE home.

    Ne livre le modèle par attributs QUE si validation_croisee_estimation() le mesure
    réellement meilleur que la médiane seule (contrainte du brief : "si ta formule ne fait
    pas mieux que la médiane, tu ne la livres pas") -- ce verdict est recalculé à CHAQUE
    appel à partir de l'état courant (contrainte 2, "ça s'ajuste tout seul") : aucun
    drapeau écrit sur disque ne fige jamais la décision, une tâche terminée de plus peut la
    faire basculer sans qu'on touche à ce fichier. Au moment d'écrire ceci, mesuré sur les
    deux seuls homes disponibles (voir le rapport de t-43), le modèle par attributs PERD
    contre la médiane en validation croisée groupée par tâche -- cette fonction rend donc
    la médiane, un verdict mesuré à chaque appel, pas une valeur câblée.

    None si ce home n'a encore aucune tâche terminée mesurable (contrainte 3, home neuf) :
    rien à estimer. Sinon toujours un entier, jamais zéro (même convention que
    _duree_calculee).
    """
    mediane = mediane_duree_par_critere(state)
    if mediane is None:
        return None
    verdict = validation_croisee_estimation(state)
    if verdict is None or verdict["gagnant"] != "attributs":
        return _duree_calculee(mediane)
    facteurs = facteurs_par_attribut(state) or {}
    return max(1, round(_estimation_par_attributs(attributs, mediane, facteurs)))


# Facteur par projet et rejeu chronologique (brief t-47), deuxième étage au-dessus du
# modèle par attributs de t-43 : "ce qu'un critère coûte en général" (facteurs_par_attribut,
# appris sur tout le home) ne dit rien de "ce que CE projet coûte en plus ou en moins" --
# un gros dépôt lent fait durer toutes ses tâches, quels que soient leurs attributs. La
# cible d'entraînement ici est le temps de TRAVAIL (duree_travail_min, brief t-45), jamais
# le délai brut : c'est la même correction nuit/pause que t-45 a apportée à
# _duree_reelle_par_critere, appliquée à ce second étage plutôt que de continuer à apprendre
# sur des pauses. Les fonctions par attributs de t-43 ci-dessus restent, elles, sur le délai
# brut -- les faire migrer vers duree_travail_min est un chantier à part, hors zone de ce
# brief, signalé dans le rapport plutôt que fait en douce.


def _cible_travail_par_critere(task: dict, evenements: Iterable[dict]) -> float | None:
    """Temps de travail (duree_travail_min, brief t-45) réparti à parts égales entre les
    critères COCHÉS d'une tâche -- même partage que _duree_reelle_par_critere (brief t-43)
    sur le délai brut, appliqué ici à la mesure corrigée des nuits. Cible commune du
    facteur par projet et du rejeu chronologique (brief t-47).

    None si le temps de travail est inconnu (tâche exclue faute de repère, brief t-45) ou
    si aucun critère de la tâche n'est coché.
    """
    travail = duree_travail_min(task, evenements)
    if travail is None:
        return None
    franchis = sum(1 for item in task.get("checklist", []) if item.get("done"))
    if franchis == 0:
        return None
    return travail / franchis


def _projet_de_tache(state: dict, task: dict) -> str:
    """Étiquette de projet d'une tâche : le chemin canonique posé par chantier.start() sur
    son chantier (state["chantiers"][...]["project"]), pas la tâche elle-même qui ne le
    porte pas. Un home_partage (décision D) peut porter plusieurs projets à la fois -- c'est
    justement ce que ce brief distingue. Retombe sur l'identifiant du chantier si celui-ci a
    disparu (archive, brief t-25) plutôt que de planter : mieux vaut une étiquette imparfaite
    qu'une exception qui casse tout le calcul.
    """
    chantier = state.get("chantiers", {}).get(task.get("chantier"), {})
    return chantier.get("project") or task.get("chantier") or "?"


def observations_projet(
    state: dict, evenements: Iterable[dict]
) -> dict[str, list[tuple[dict, float]]]:
    """Jeu d'observations du facteur par projet (brief t-47) : comme observations_par_critere()
    (t-43) mais regroupé par PROJET plutôt que par tâche, et sur la cible en temps de
    travail (_cible_travail_par_critere) plutôt que le délai brut -- c'est le HOME entier,
    tous projets confondus s'il en porte plusieurs (home_partage), qui sert de fond commun ;
    chaque projet en est la subdivision qu'on corrige.

    Chaque critère COCHÉ de chaque tâche exploitable contribue une paire (attributs -- {} si
    le critère n'en porte aucun --, cible), pour que la médiane par projet et par home
    compte TOUS les critères francs, exactement comme mediane_duree_par_critere : un projet
    qui coche beaucoup de critères sans attributs pèse quand même sur son propre facteur.
    """
    par_projet: dict[str, list[tuple[dict, float]]] = {}
    for task_id in sorted(state["taches"]):
        task = state["taches"][task_id]
        if task.get("state") != "done":
            continue
        cible = _cible_travail_par_critere(task, evenements)
        if cible is None:
            continue
        projet = _projet_de_tache(state, task)
        paires = [
            (item.get("attributs") or {}, cible)
            for item in task.get("checklist", [])
            if item.get("done")
        ]
        par_projet.setdefault(projet, []).extend(paires)
    return par_projet


SEUIL_OBSERVATIONS_PROJET = 8
# Plancher de critères cochés d'un projet avant de lui reconnaître un facteur (brief t-47,
# "sous un seuil que tu fixes et justifies, pas de facteur du tout"). Plus bas que
# SEUIL_OBSERVATIONS_ATTRIBUT (10) parce que le facteur projet ne compare qu'UNE médiane à
# UNE autre (deux valeurs), pas dix valeurs de gestes différentes en concurrence sur le même
# budget d'observations : une médiane sur 8 points reste lisible et se resserre vite --
# lire une seule fois, en dessous, une majorité de tâches viendrait d'une poignée de critères
# voisins dans le temps, pas d'un vrai échantillon du projet. Mesuré sur les trois homes de
# ce brief (camcast 54, loko 33, lavuln 17 tâches "done" au moment du rejeu) : les trois
# dépassent ce seuil dès le premier tiers de leur histoire, ce qui laisse au rejeu de quoi
# comparer les deux tiers suivants avec un facteur déjà formé.


def facteur_projet(
    state: dict,
    evenements: Iterable[dict],
    canon_project: str,
    seuil: int = SEUIL_OBSERVATIONS_PROJET,
) -> float | None:
    """Facteur correctif propre à UN projet de ce home (brief t-47, c2) : médiane des
    cibles (temps de travail, t-45) des critères de ce projet, rapportée à la médiane du
    HOME ENTIER. >1 : ce projet coûte systématiquement plus cher que le reste du home, quels
    que soient les attributs de ses critères (dépôt lent, tests longs, machine chargée...) ;
    <1 : moins cher. Facteur neutre implicite = 1 pour tout projet absent du résultat.

    None si ce home n'a encore aucune cible mesurable (rien à rapporter), ou si CE projet a
    moins de `seuil` critères cochés observés (contrainte du brief, voir
    SEUIL_OBSERVATIONS_PROJET) : sous ce seuil, pas de facteur du tout plutôt qu'un chiffre
    instable formé sur une poignée de critères.
    """
    par_projet = observations_projet(state, evenements)
    toutes = [paire for paires in par_projet.values() for paire in paires]
    if not toutes:
        return None
    paires_projet = par_projet.get(canon_project, [])
    if len(paires_projet) < seuil:
        return None
    mediane_home = statistics.median(cible for _, cible in toutes)
    mediane_projet = statistics.median(cible for _, cible in paires_projet)
    return mediane_projet / mediane_home


_FICHIER_CALIBRATION_PROJET = "calibration_projet.json"


def _chemin_calibration_projet(home: Path | None = None) -> Path:
    return (home if home is not None else store.home()) / _FICHIER_CALIBRATION_PROJET


def charger_calibration_projet(home: Path | None = None) -> dict[str, dict]:
    """Relit le petit JSON de calibration par projet de ce home (brief t-47, c3) : quelques
    clés par projet, lisible et éditable à la main -- pas un modèle sérialisé que personne
    ne pourrait corriger. {} si le fichier n'existe pas encore (home neuf, ou tous ses
    projets sous le seuil) ou est illisible (JSON corrompu) : une calibration absente
    équivaut à "aucun facteur", jamais une exception qui casse l'estimation appelante.
    """
    chemin = _chemin_calibration_projet(home)
    if not chemin.exists():
        return {}
    try:
        with chemin.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def sauver_calibration_projet(calibration: dict[str, dict], home: Path | None = None) -> None:
    """Écrit le JSON de calibration par projet -- indenté et trié par clé, pour rester
    lisible et diffable à la main (brief t-47, c3)."""
    chemin = _chemin_calibration_projet(home)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8") as f:
        json.dump(calibration, f, indent=2, ensure_ascii=False, sort_keys=True)


def recalculer_calibration_projet(
    state: dict,
    evenements: Iterable[dict],
    seuil: int = SEUIL_OBSERVATIONS_PROJET,
    home: Path | None = None,
) -> dict[str, dict]:
    """Recalcule le facteur de TOUS les projets de ce home et PERSISTE le résultat (brief
    t-47, c3) -- à appeler quand une tâche se termine (contrat du brief ; le point d'appel
    côté report.py/cli.py reste hors zone de cette tâche, voir le rapport de t-47). Relu à
    chaque estimation via charger_calibration_projet(), jamais mis en cache en mémoire.

    Un projet qui retombe sous le seuil (tâches annulées, historique qui change) voit sa
    clé DISPARAÎTRE du fichier plutôt que d'y garder un chiffre devenu moins mesurable --
    la relecture humaine du JSON reste honnête sur ce qui est vraiment établi.
    """
    par_projet = observations_projet(state, evenements)
    toutes = [paire for paires in par_projet.values() for paire in paires]
    calibration: dict[str, dict] = {}
    if toutes:
        mediane_home = statistics.median(cible for _, cible in toutes)
        for projet, paires in par_projet.items():
            if len(paires) < seuil:
                continue
            calibration[projet] = {
                "facteur": statistics.median(cible for _, cible in paires) / mediane_home,
                "observations": len(paires),
                "recalculeLe": store.now(),
            }
    sauver_calibration_projet(calibration, home)
    return calibration


def _stats_rejeu(erreurs: list[float]) -> dict:
    """Statistiques d'une série d'erreurs absolues DANS L'ORDRE DU REJEU (brief t-47, c5) :
    ensemble, premier tiers, dernier tiers -- c'est la comparaison de ces deux derniers qui
    dit si une stratégie s'améliore avec l'usage. Sous 3 erreurs, premier et dernier tiers
    se recouvrent (au moins un élément commun) : lu en connaissance de cause plutôt que
    masqué, un rejeu aussi court n'a de toute façon rien de concluant à offrir.
    """
    n = len(erreurs)
    if n == 0:
        return {
            "n": 0,
            "mae": None,
            "medianeErreur": None,
            "maePremierTiers": None,
            "maeDernierTiers": None,
        }
    tiers = max(1, n // 3)
    return {
        "n": n,
        "mae": statistics.mean(erreurs),
        "medianeErreur": statistics.median(erreurs),
        "maePremierTiers": statistics.mean(erreurs[:tiers]),
        "maeDernierTiers": statistics.mean(erreurs[-tiers:]),
    }


def preparer_rejeu(state: dict, evenements: Iterable[dict]) -> list[dict]:
    """Assemble, pour CE home, le jeu d'entrée de rejeu_chronologique() (brief t-47) : une
    entrée par tâche "done" exploitable (temps de travail connu, au moins un critère coché),
    triées par ordre RÉEL de complétion (finishedAt croissant -- format ISO 8601 de ce
    fichier, donc comparable directement en chaîne, sans reparser).

    Un rejeu qui combine plusieurs homes (les trois chantiers du brief t-47, chacun son
    state.json) appelle cette fonction une fois par home puis trie l'UNION des listes
    rendues sur "finishedAt", jamais chaque liste séparément -- sinon l'ordre inter-home se
    perd et le rejeu entraînerait parfois sur un futur qu'il n'aurait pas dû connaître.
    """
    resultat = []
    for task_id in sorted(state["taches"]):
        task = state["taches"][task_id]
        if task.get("state") != "done":
            continue
        travail = duree_travail_min(task, evenements)
        if travail is None:
            continue
        criteres = [
            item.get("attributs") or {} for item in task.get("checklist", []) if item.get("done")
        ]
        if not criteres:
            continue
        resultat.append(
            {
                "projet": _projet_de_tache(state, task),
                "dureeTravailMin": travail,
                "criteres": criteres,
                "finishedAt": task.get("finishedAt") or "",
            }
        )
    resultat.sort(key=lambda o: o["finishedAt"])
    return resultat


def rejeu_chronologique(
    observations: list[dict],
    seuil_attribut: int = SEUIL_OBSERVATIONS_ATTRIBUT,
    seuil_projet: int = SEUIL_OBSERVATIONS_PROJET,
) -> dict[str, dict]:
    """LE REJEU du brief t-47, le livrable qui compte : rejoue `observations` (triées par
    l'appelant, voir preparer_rejeu, dans l'ordre RÉEL de complétion) et mesure, à CHAQUE
    tâche, ce que chacune des trois stratégies aurait estimé en ne connaissant que ce qui la
    précède -- jamais un entraînement sur le futur de cette même tâche.

    Chaque élément de `observations` : {"projet": <étiquette>, "dureeTravailMin": <float>,
    "criteres": [<attributs>, ...]} -- un dict d'attributs (éventuellement {}) par critère
    coché de la tâche ; la même liste sert à répartir également dureeTravailMin entre eux
    pour former sa cible d'entraînement UNE FOIS la tâche intégrée (même convention que
    _cible_travail_par_critere).

    Trois stratégies comparées à chaque pas :
      A. médiane globale seule (attributs ignorés) ;
      B. attributs seuls (médiane globale + facteurs par attribut, comme t-43) ;
      C. B, multiplié par le facteur du projet de la tâche s'il est déjà formé (seuil_projet
         critères cochés antérieurs pour CE projet), sinon C == B pour cette tâche -- un
         projet neuf part sans facteur, au modèle global, exactement la règle du brief.

    La toute première tâche du rejeu ne peut être estimée par AUCUNE stratégie (aucune
    observation antérieure, pas même une médiane) : elle est intégrée mais absente des
    erreurs, jamais devinée à zéro observation.

    Rend {"A": {...}, "B": {...}, "C": {...}}, une entrée par stratégie -- voir
    _stats_rejeu() pour la forme de chaque valeur : n, mae, medianeErreur,
    maePremierTiers, maeDernierTiers.
    """
    paires_globales: list[tuple[dict, float]] = []
    paires_par_projet: dict[str, list[tuple[dict, float]]] = {}
    erreurs: dict[str, list[float]] = {"A": [], "B": [], "C": []}

    for obs in observations:
        criteres = obs["criteres"]
        reel = obs["dureeTravailMin"]

        if paires_globales:
            mediane = statistics.median(cible for _, cible in paires_globales)
            facteurs = _facteurs(paires_globales, mediane, seuil_attribut)
            estime_a = mediane * len(criteres)
            estime_b = sum(
                _estimation_par_attributs(attrs, mediane, facteurs) for attrs in criteres
            )
            paires_projet = paires_par_projet.get(obs["projet"], [])
            facteur_proj = (
                statistics.median(cible for _, cible in paires_projet) / mediane
                if len(paires_projet) >= seuil_projet
                else None
            )
            estime_c = estime_b * facteur_proj if facteur_proj is not None else estime_b

            erreurs["A"].append(abs(estime_a - reel))
            erreurs["B"].append(abs(estime_b - reel))
            erreurs["C"].append(abs(estime_c - reel))

        cible = reel / len(criteres)
        for attrs in criteres:
            paires_globales.append((attrs, cible))
            paires_par_projet.setdefault(obs["projet"], []).append((attrs, cible))

    return {strategie: _stats_rejeu(liste) for strategie, liste in erreurs.items()}


def reword_checklist_item(task_id: str, item_id: str, label: str) -> dict:
    """Reformule le libellé d'un critère existant, sans toucher à son id ni à son état
    (brief t-22).

    Corrige un libellé qui s'avère faux ou trop gros une fois le travail commencé.
    L'état done n'a aucune raison de bouger : reformuler n'est pas désavouer un travail
    déjà vérifié.
    """
    with store.locked() as state:
        task = _get_task(state, task_id)
        for item in task["checklist"]:
            if item["id"] == item_id:
                item["label"] = label
                break
        else:
            raise ChantierError(
                f"item not found: {item_id} does not exist in the checklist of {task_id}"
            )
    return task


def set_group(chantier_id: str, key: str, label: str, why: str = "") -> dict:
    """Nomme une phase du chantier, ex. "0" -> "Socle sequentiel", et dit a quoi elle sert.

    L'appartenance d'une tache a une phase se LIT de son titre (prefixe "0.3", "1.4") et ne
    se stocke nulle part : c'est deja la convention que les orchestratrices suivent, et la
    dupliquer dans l'etat ferait deux verites pour un seul fait. Seuls le LIBELLE et le
    POURQUOI se stockent, parce qu'eux n'existent nulle part ailleurs : "0" ne dit a
    personne ce que la phase 0 sert.

    Nommer une phase qui n'a encore AUCUNE tache est le geste normal, pas un cas limite :
    c'est ce qui permet d'annoncer les six phases d'un chantier des le depart et de
    montrer, sur la carte, que la phase 3 existe et reste a decouper. carte.model() les
    rend avec planned=True.

    why absent ne remplace jamais un why deja ecrit : corriger un libelle est frequent,
    reecrire l'explication ne l'est pas, et l'ecraser en silence la ferait disparaitre au
    moment ou on croit juste corriger une faute de frappe.
    """
    with store.locked() as state:
        ch = _get_chantier(state, chantier_id)
        groupes = ch.setdefault("groupes", {})
        ancien = groupes.get(key)
        ancien_why = ancien.get("why", "") if isinstance(ancien, dict) else ""
        groupes[key] = {"label": label, "why": why or ancien_why}
    return ch


def explain(task_id: str, why: str) -> dict:
    """Pose ou remplace la raison d'etre d'une tache, apres coup.

    Existe parce qu'un chantier deja lance ne peut pas revenir en arriere : sans ce verbe,
    les taches creees avant que quiconque pense au pourquoi resteraient muettes pour
    toujours, et la carte n'aurait rien a montrer la ou elle en a le plus besoin. Autorise
    sur une tache en cours, contrairement a amend() : expliquer ne change pas le contrat
    deja envoye a l'executante, donc rien ne se desaligne sous ses pieds.
    """
    with store.locked() as state:
        task = _get_task(state, task_id)
        task["why"] = why
    return task


def has_cycle(tasks: dict) -> list[str] | None:
    """DFS colore sur dependsOn ; renvoie le cycle trouve (liste d'ids), sinon None.

    Renvoyer le chemin, pas un simple booleen, est ce qui permet au message d'erreur de
    nommer les taches en cause (I8) au lieu de dire juste "refuse".
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in tasks}
    path: list[str] = []

    def visit(tid: str) -> list[str] | None:
        color[tid] = GRAY
        path.append(tid)
        for dep in tasks[tid].get("dependsOn", []):
            if dep not in tasks:
                continue
            if color[dep] == GRAY:
                start_idx = path.index(dep)
                return path[start_idx:] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        color[tid] = BLACK
        return None

    for tid in tasks:
        if color[tid] == WHITE:
            found = visit(tid)
            if found:
                return found
    return None


def _dependencies_satisfied(task: dict, tasks: dict) -> bool:
    """Toutes les deps sont done ET toutes leurs checklists sont cochees.

    La checklist de TASK lui-meme n'entre jamais dans ce calcul : c'est exactement I1.
    """
    for dep_id in task.get("dependsOn", []):
        dep = tasks.get(dep_id)
        if dep is None or dep["state"] != "done":
            return False
        if not all(item["done"] for item in dep["checklist"]):
            return False
    return True


def ready(chantier_id: str) -> list[dict]:
    """Taches en file dont toutes les dependances sont done et leurs checklists cochees.

    N'inspecte jamais la checklist de la tache elle-meme (I1) : sinon, le seul acteur
    capable de la cocher serait la session que la checklist empeche de demarrer.
    """
    state = store.load()
    _get_chantier(state, chantier_id)
    tasks = state["taches"]
    return [
        task
        for task in tasks.values()
        if task["chantier"] == chantier_id
        and task["state"] == "queued"
        and _dependencies_satisfied(task, tasks)
    ]


def propagate_failures(state: dict, chantier_id: str) -> list[str]:
    """Bascule en blocked, en cascade, les dependantes d'une tache morte, DANS LE SEUL
    chantier vise (point F).

    Ne parcourt que les taches de chantier_id, et n'honore une dependance que si elle
    appartient elle aussi a ce chantier : une ancienne version parcourait TOUTES les
    taches sans filtre, si bien qu'une dependance illegitime entre deux chantiers (voir
    add_task/depend, meme point F) pouvait faire qu'un echec dans le chantier A bloque une
    tache du chantier B. Les deux filtres se recoupent par prudence : le premier protege
    les taches d'un AUTRE chantier d'un appel scope ailleurs, le second protege une tache
    du chantier vise contre une dependance illegitime heritee d'un etat anterieur a ce
    correctif.

    Mute state en place (l'appelant est responsable de la persister, typiquement via
    store.locked()). La boucle tourne jusqu'a point fixe : un dependant fraichement
    bloque peut a son tour bloquer ses propres dependants dans le meme appel.
    """
    tasks = state["taches"]
    changed: list[str] = []
    progressed = True
    while progressed:
        progressed = False
        for task in tasks.values():
            if task["chantier"] != chantier_id:
                continue
            if task["state"] not in ("queued", "waiting"):
                continue
            for dep_id in task.get("dependsOn", []):
                dep = tasks.get(dep_id)
                if dep is None or dep["chantier"] != chantier_id:
                    continue
                if dep["state"] in _DEAD_STATES:
                    task["state"] = "blocked"
                    task["error"] = f"dependency {dep_id} dead ({dep['state']})"
                    task["blockedCause"] = BLOCKED_CAUSE_PROPAGATION
                    changed.append(task["id"])
                    progressed = True
                    break
    return changed


def unblock_propagated(state: dict) -> list[str]:
    """Repasse en "queued" les taches bloquees PAR PROPAGATION dont les dependances sont
    redevenues saines (done, checklist entierement cochee).

    Bug reel corrige ici, observe sur le socle ~/.claude/ordo : t-01 meurt, tick() bloque
    t-02 (qui en depend) en cascade via propagate_failures(). t-01 est relancee et reussit
    vraiment, coche sa checklist ; sans cette fonction, t-02 restait bloquee pour
    toujours, alors que sa seule dependance etait terminee.

    Ne debloque JAMAIS une tache bloquee pour sa propre raison (pane mort, rapport
    illisible, dialogue de confiance tmux) : le discriminant est le champ structure
    task["blockedCause"], jamais le texte de task["error"]. Se fier au texte est fragile,
    il suffit de le reformuler pour que la levee cesse silencieusement de fonctionner, ou
    pire, qu'elle se mette a debloquer des taches qu'elle ne devrait pas.

    Une tache bloquee AVANT l'introduction de blockedCause n'a pas cette cle : elle n'est
    jamais debloquee automatiquement (task.get(...) rend None, different de
    BLOCKED_CAUSE_PROPAGATION). Decision deliberee : une reprise manuelle sur un blocage
    d'origine inconnue est preferable a un deblocage fonde sur un texte reformulable.

    Reutilise _dependencies_satisfied(), le meme predicat que ready() (I1) : une seconde
    definition de "dependance satisfaite" divergerait forcement de la premiere avec le
    temps.

    Mute state en place (l'appelant est responsable de la persister, typiquement via
    store.locked()), comme propagate_failures(). Une seule passe suffit : redevenir
    "queued" n'est jamais "done", donc debloquer une tache ne peut pas rendre une autre
    tache satisfaite dans le meme appel (pas de cascade a chainer, contrairement au
    blocage qui, lui, se propage).
    """
    tasks = state["taches"]
    unblocked: list[str] = []
    for task in tasks.values():
        if task["state"] != "blocked":
            continue
        if task.get("blockedCause") != BLOCKED_CAUSE_PROPAGATION:
            continue
        if _dependencies_satisfied(task, tasks):
            task["state"] = "queued"
            task["error"] = None
            task["blockedCause"] = None
            unblocked.append(task["id"])
    return unblocked


def graph_ascii(chantier_id: str) -> str:
    """Rendu texte lisible en terminal du graphe de taches d'un chantier."""
    state = store.load()
    _get_chantier(state, chantier_id)
    tasks = sorted(
        (t for t in state["taches"].values() if t["chantier"] == chantier_id),
        key=lambda t: t["id"],
    )
    if not tasks:
        return "(no tasks)"
    lines = []
    for t in tasks:
        deps = ", ".join(t["dependsOn"]) if t["dependsOn"] else "-"
        lines.append(f"{t['id']}  [{t['state']:<8}]  {t['titre']}  (depends on: {deps})")
    return "\n".join(lines)
