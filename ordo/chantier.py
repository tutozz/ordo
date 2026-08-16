"""Chantiers, taches, graphe de dependances, cycles, disponibilite (ready).

Chaque fonction qui mute l'etat passe par store.locked() : c'est lui qui garantit l'ecriture
atomique et le verrou. Les fonctions de lecture seule (ready, graph_ascii, has_cycle) lisent
sans verrou, has_cycle() n'accedant meme pas au disque : c'est une fonction pure sur un dict.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from pathlib import Path

from . import store

# Valeurs valides du champ chantier["permissions"] (point G). "skip" est le defaut :
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


def _normalize_checklist(items: Iterable) -> list[dict]:
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
        normalized.append(
            {"id": item_id, "label": label, "done": done, "dureeMin": duree_min}
        )
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
    verifier la vivacite reelle du pane plutot que de se fier au seul etat declare.
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
            "checklist": _normalize_checklist(checklist),
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
    """
    with store.locked() as state:
        task = _get_task(state, task_id)
        new_id = _next_checklist_id(task["checklist"])
        task["checklist"].append(
            {"id": new_id, "label": label, "done": False, "dureeMin": None}
        )
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
