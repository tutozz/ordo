"""Carte lisible d'un chantier : niveaux, groupes, blocages, page HTML autonome.

Ce module ne decide rien et n'ecrit rien. Il repond a une seule question, celle qu'un
transcript d'orchestratrice ne repond jamais : qu'est-ce qui a ete decide, comment c'est
groupe, qui attend quoi, et ce qui manque encore pour que le chantier soit fini.

Trois choix portent le fichier.

Le NIVEAU d'une tache est la longueur du plus long chemin de dependances qui y mene. Ce
n'est pas une vague d'execution : deux taches du meme niveau peuvent se partager une zone
et donc ne pas pouvoir tourner ensemble. La carte le dit alors explicitement plutot que de
laisser croire a un parallelisme qui n'existe pas.

La PHASE se lit du prefixe numerique du titre ("0.3" -> phase 0). C'est la convention que
les orchestratrices suivent deja d'elles-memes ; la relire coute une regex et evite un
champ de plus a tenir a jour. Seuls le LIBELLE et le POURQUOI de la phase se stockent, sur
le chantier, parce que "0" ne dit a personne ce que la phase 0 sert. Une phase nommee sans
aucune tache est une phase ANNONCEE : c'est ce qui montre qu'un chantier de six phases n'en
a decoupe qu'une.

Le POURQUOI est la seule chose que ni le titre ni le prompt ne portent. Le titre nomme, le
prompt dit comment faire ; ni l'un ni l'autre ne dit pourquoi l'orchestratrice a decoupe
ainsi, et c'est exactement ce qu'un humain lit une carte pour comprendre. Ce module ne
l'invente pas : ce qui n'a pas ete explique est signale comme tel, jamais comble.

La PAGE est une transposition d'un design fait dans Claude Design. L'original tourne sur
React et le runtime du designer ; une carte Ordo est un fichier unique, sans reseau et sans
dependance, donc rien de tout cela ne peut etre embarque. Ce qui est porte est le
comportement, pas le code : phases repliables, alertes et questions en calque, chaine
attend/debloque, arcs orientes. Deux ecarts assumes par rapport a l'original : les polices
distantes sont remplacees par les piles systeme (une carte doit se lire sans reseau, et ne
rien dire a personne de ce qu'on regarde), et le rendu est en JS nu.

Le partage des roles suit de la : Python calcule et sert des DONNEES, le navigateur les
met en page. Les positions n'existent pas avant le rendu (les rangs passent a la ligne
selon la largeur), donc aucun calcul de mise en page ne peut vivre ici.

Le contenu du chantier voyage en JSON et n'atteint le DOM que par textContent. Jamais par
concatenation dans du balisage : un titre de tache est du texte qu'un modele a ecrit, il
contiendra du HTML tot ou tard.

La VIVACITE d'un pane s'injecte, elle ne se devine pas. Sans verificateur fourni, la carte
rend None et n'invente aucun pane mort : ce module ne parle jamais a tmux, ce qui le rend
testable sans serveur et empeche un dessin de carte de peser sur les sessions en cours.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import shlex
import time
from collections.abc import Callable

from . import chantier, journal, routage, store, usage

# Coupe du titre dans une tuile : largeur en caracteres, nombre de lignes gardees. Le
# titre entier reste lisible en infobulle et dans le panneau de detail, donc rien n'est
# perdu ; ce qui est en jeu ici est l'alignement des tuiles d'une meme phase.
TITLE_COLS = 19
TITLE_LINES = 3

# Etats d'un chantier qui comptent comme ouvert. Deux valeurs, pas une : un state.json
# ecrit par une version anterieure porte "ouvert", et le relire comme "ferme" ferait
# crier au chantier clos sur un chantier parfaitement vivant.
_CHANTIER_OUVERT = ("open", "ouvert")

# Etats de tache qui ne peuvent plus rien ecrire, donc ne peuvent plus entrer en conflit
# de zone avec personne.
_ETATS_INERTES = ("done", "cancelled", "failed")

# Etats de tache qui restent a faire ; leur presence sous un chantier ferme est une
# contradiction qui merite d'etre dite.
_ETATS_VIVANTS = ("running", "queued", "blocked")

# Un titre appartient a une phase quand il commence par un nombre suivi d'un point :
# "0.3", "0.4b", "10.1". Le point est exige : sans lui, un titre commencant par une annee
# ("2026 audit des routes") fabriquerait une phase 2026 qui n'existe pas.
_PHASE = re.compile(r"^\s*(\d+)\.")

# Plage de phases annoncee dans l'objectif, ex. "phases 0 a 6". Volontairement etroite :
# ce qui n'est pas reconnu ne declare rien, plutot que de deviner un perimetre faux.
_PLAGE_PHASES = re.compile(r"phases?\s+(\d+)\s*(?:a|à|to|-|jusqu'a|jusqu'à)\s*(\d+)", re.I)

# Mention d'une tache dans une ligne de journal. Les deux bornes comptent autant l'une que
# l'autre : sans la droite, "t-100 pose un piege" se rattacherait aussi a t-10 ; sans la
# gauche, un identifiant colle a un mot precedent passerait pour une mention.
_MENTION = re.compile(r"(?<![\w-])(t-\d+)(?![\w-])")

_ISO = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------
# Modele
# ---------------------------------------------------------------------------


def _epoch(horodatage: str | None) -> float | None:
    if not horodatage:
        return None
    try:
        return float(calendar.timegm(time.strptime(horodatage, _ISO)))
    except (ValueError, TypeError):
        return None


def _elapsed(task: dict) -> int | None:
    """Secondes ecoulees : depuis le depart si la tache tourne, sinon depuis le depart
    jusqu'a la fin. Sans horodatage de depart, None ; jamais zero, qui se lirait comme
    "instantane" alors que la verite est "on ne sait pas"."""
    debut = _epoch(task.get("startedAt"))
    if debut is None:
        return None
    fin = _epoch(task.get("finishedAt"))
    if fin is None:
        fin = time.time()
    return max(0, int(fin - debut))


def _phase_key(titre: str) -> str:
    match = _PHASE.match(titre or "")
    return match.group(1) if match else ""


def _levels(tasks: dict[str, dict]) -> dict[str, int]:
    """Plus long chemin de dependances menant a chaque tache, cycles compris.

    Une arete arriere de cycle est comptee comme nulle plutot que suivie : c'est ce qui
    fait terminer le calcul sur un graphe cyclique au lieu de partir en recursion infinie.
    Le cycle lui-meme n'est pas avale en silence pour autant, model() l'annonce en
    avertissement. Un identifiant de dependance inconnu est ignore ici, et signale la
    aussi : il ne doit pas decaler le niveau d'une tache qui, de fait, n'attend rien.
    """
    memo: dict[str, int] = {}
    en_cours: set[str] = set()

    def profondeur(tid: str) -> int:
        if tid in memo:
            return memo[tid]
        if tid in en_cours:
            return 0
        en_cours.add(tid)
        best = 0
        for dep in tasks[tid].get("dependsOn") or []:
            if dep in tasks:
                best = max(best, profondeur(dep) + 1)
        en_cours.discard(tid)
        memo[tid] = best
        return best

    bruts = {tid: profondeur(tid) for tid in tasks}
    # Compactage : un cycle peut ne laisser aucune tache au niveau 0, et une colonne vide
    # au milieu du dessin ne veut rien dire pour un lecteur.
    utilises = sorted(set(bruts.values()))
    dense = {niveau: i for i, niveau in enumerate(utilises)}
    return {tid: dense[n] for tid, n in bruts.items()}


def _num_id(task_id: str) -> tuple[int, str]:
    """Cle de tri d'un identifiant : t-9 avant t-10, ce qu'un tri texte inverse."""
    _, _, suffixe = task_id.partition("-")
    try:
        return (int(suffixe), task_id)
    except ValueError:
        return (10**9, task_id)


def _ordre_groupes(cles: set[str]) -> list[str]:
    """Phases par ordre numerique, le groupe hors phase en dernier."""
    numerotees = sorted((c for c in cles if c), key=int)
    return numerotees + ([""] if "" in cles else [])


def _groupes_declares(ch: dict) -> dict[str, dict]:
    """Libelle et pourquoi de chaque phase nommee, quelle que soit la forme sur disque.

    set_group() a d'abord range une simple chaine sous chaque cle, puis un dict
    {label, why}. Les deux formes coexistent sur le disque d'un chantier commence avant ce
    changement ; migrer state.json pour ca coûterait une reecriture de l'etat d'un chantier
    en cours, la lecture tolerante ne coûte rien.
    """
    brut = ch.get("groupes") or {}
    declares: dict[str, dict] = {}
    for cle, valeur in brut.items():
        if isinstance(valeur, dict):
            declares[cle] = {"label": valeur.get("label") or "", "why": valeur.get("why") or ""}
        else:
            declares[cle] = {"label": str(valeur), "why": ""}
    return declares


def _questions(chantier_id: str, state: dict, tasks: dict[str, dict]) -> list[dict]:
    """Ce qui attend un choix de l'humain sur ce chantier, et rien d'autre.

    Deux filtres, et chacun ferme un défaut distinct. Une question déjà répondue sort :
    une alerte qui ne s'éteint jamais cesse d'être lue, et celle-là resterait allumée pour
    toujours. Une question qui n'est pas marquée `pourHumain` sort aussi : elle appartient
    à l'orchestratrice, la montrer ici ferait répondre l'humain à la place de la session,
    ce qui est l'inverse exact du contrat.

    `tache` peut être nul. Une question d'orchestratrice porte le plus souvent sur le
    chantier -- lancer la suite en parallèle ou en série -- et n'appartient à aucune tâche.
    """
    sorties = []
    for qid in sorted(state.get("questions") or {}, key=_num_id):
        q = state["questions"][qid]
        if q.get("chantier") != chantier_id or not q.get("pourHumain"):
            continue
        if q.get("answer") is not None:
            continue
        tid = q.get("tache") or ""
        sorties.append({
            "id": qid,
            "task": tid,
            # Le titre voyage avec l'identifiant, jamais l'identifiant seul : "t-12" oblige
            # à traduire mentalement, et c'est exactement ce qu'un humain revenu à froid ne
            # peut pas faire.
            "taskTitle": (tasks.get(tid) or {}).get("titre", ""),
            "text": q.get("question") or "",
            "options": list(q.get("options") or []),
            "askedAt": q.get("askedAt"),
        })
    return sorties


def _modele(task: dict) -> tuple[str, str, bool]:
    """(modele, motif, predit) d'une tache. `predit` dit que personne ne l'a encore lancee.

    Trois cas, et le troisieme est celui qui merite d'etre nomme.

    Une tache deja lancee porte le modele qu'on lui a impose : c'est un fait mesure, il
    prime sur tout calcul. Une tache jamais lancee n'en a pas, donc la carte annonce celui
    que routage.pour_lancement retiendra, ESCALADE DES TENTATIVES COMPRISE -- sans elle la
    case promettrait haiku la ou le prochain lancement partira sur opus, et c'est
    exactement au moment d'une reprise qu'on regarde ce chiffre.

    Une tache lancee avec `--model herite` n'a ni l'un ni l'autre : claude a applique sa
    propre configuration. Lui recoller la prediction du routage inventerait apres coup un
    modele qui n'a pas tourne.
    """
    impose = task.get("model")
    if impose:
        return impose, "modèle imposé à son lancement", False
    if task.get("startedAt"):
        return "defaut", "lancée sans modèle imposé : celui de claude a servi", False
    modele, motif = routage.pour_lancement(None, task)
    return modele or "defaut", motif, True


def _blocking_reasons(task: dict, tasks: dict[str, dict]) -> list[dict]:
    """Pour chaque dependance qui empeche `task` de partir, la raison, nommee.

    Le troisieme cas est le plus traitre et c'est l'invariant I1 : une dependance `done`
    dont la checklist n'est pas entierement cochee bloque toujours. Le graphe a l'air
    vert, la tache suivante n'a l'air d'attendre personne, et rien ne part.
    """
    raisons = []
    for dep_id in task.get("dependsOn") or []:
        dep = tasks.get(dep_id)
        if dep is None:
            raisons.append({"id": dep_id, "reason": "identifiant inexistant"})
        elif dep["state"] != "done":
            raisons.append({"id": dep_id, "reason": f"en etat {dep['state']}, pas done"})
        elif not all(item["done"] for item in dep.get("checklist") or []):
            manquantes = ", ".join(
                i["id"] for i in dep["checklist"] if not i["done"]
            )
            raisons.append(
                {"id": dep_id, "reason": f"done mais checklist incomplete ({manquantes})"}
            )
    return raisons


def _decisions_par_tache(chantier_id: str, ids: list[str]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Lignes ORCH du journal, et leur rattachement aux taches qu'elles nomment.

    Le rattachement se fait sur mention explicite de l'identifiant, borne des deux cotes :
    sans la borne droite, une decision qui parle de t-100 se collerait aussi a t-10.

    Une seule passe de regex par ligne, pas une par (ligne, tache) : --watch redessine en
    boucle, et un chantier de quarante taches avec un journal de cinq cents lignes paierait
    vingt mille recherches par cycle pour la meme reponse.
    """
    lignes = [e for e in journal.read(chantier_id) if e["auteur"] == "ORCH"]
    connus = set(ids)
    par_tache: dict[str, list[dict]] = {tid: [] for tid in ids}
    for ligne in lignes:
        for mention in set(_MENTION.findall(ligne["texte"])):
            if mention in connus:
                par_tache[mention].append(ligne)
    return lignes, par_tache


def _phases_declarees(objectif: str) -> list[str] | None:
    match = _PLAGE_PHASES.search(objectif or "")
    if not match:
        return None
    debut, fin = int(match.group(1)), int(match.group(2))
    if fin < debut or fin - debut > 50:
        return None
    return [str(n) for n in range(debut, fin + 1)]


def _avertissements(
    ch: dict, tasks: dict[str, dict], niveaux: dict[str, int],
    alive: Callable[[str], bool] | None,
) -> list[dict]:
    """Tout ce qui rend le graphe menteur, dit a voix haute.

    Un graphe qui a l'air sain alors qu'un pane est mort, qu'une checklist n'est pas
    cochee ou que deux taches du meme niveau ecrivent le meme fichier est pire qu'un
    graphe absent : il donne confiance. Chaque entree porte un kind stable pour le
    filtrage, un detail lisible et les taches en cause.
    """
    warnings: list[dict] = []

    cycle = chantier.has_cycle(tasks)
    if cycle:
        warnings.append({
            "kind": "cycle",
            "detail": "cycle de dependances : " + " -> ".join(cycle),
            "tasks": list(dict.fromkeys(cycle)),
        })

    for tid, task in sorted(tasks.items(), key=lambda kv: _num_id(kv[0])):
        inconnues = [d for d in task.get("dependsOn") or [] if d not in tasks]
        if inconnues:
            warnings.append({
                "kind": "dependance-inexistante",
                "detail": f"{tid} depend de {', '.join(inconnues)}, qui n'existe pas",
                "tasks": [tid],
            })

        checklist = task.get("checklist") or []
        if task["state"] == "done" and checklist and not all(i["done"] for i in checklist):
            manquantes = ", ".join(i["id"] for i in checklist if not i["done"])
            warnings.append({
                "kind": "checklist-incomplete",
                "detail": (
                    f"{tid} est done mais sa checklist n'est pas cochee ({manquantes}) ; "
                    "ses dependantes restent bloquees sans que rien ne le dise"
                ),
                "tasks": [tid],
            })

        pane_id = task.get("paneId")
        if alive is not None and pane_id and task["state"] == "running" and not alive(pane_id):
            warnings.append({
                "kind": "pane-mort",
                "detail": f"{tid} est running mais son pane {pane_id} n'existe plus",
                "tasks": [tid],
            })

    # Conflit de zone au sein d'un meme niveau : le niveau dit "rien ne les separe dans le
    # graphe", la zone partagee dit "elles ne peuvent pourtant pas tourner ensemble".
    par_niveau: dict[int, list[str]] = {}
    for tid, niveau in niveaux.items():
        if tasks[tid]["state"] not in _ETATS_INERTES:
            par_niveau.setdefault(niveau, []).append(tid)
    for niveau in sorted(par_niveau):
        ids = sorted(par_niveau[niveau], key=_num_id)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                communes = set(tasks[a].get("touches") or []) & set(tasks[b].get("touches") or [])
                if communes:
                    warnings.append({
                        "kind": "zone-partagee",
                        "detail": (
                            f"{a} et {b} sont au meme niveau mais partagent "
                            f"{', '.join(sorted(communes))} : elles se stomperaient"
                        ),
                        "tasks": [a, b],
                    })

    if ch["state"] not in _CHANTIER_OUVERT:
        vivantes = sorted(
            (tid for tid, t in tasks.items() if t["state"] in _ETATS_VIVANTS), key=_num_id
        )
        if vivantes:
            warnings.append({
                "kind": "chantier-clos-taches-vivantes",
                "detail": (
                    f"le chantier est {ch['state']} mais {len(vivantes)} tache(s) "
                    f"ne sont pas terminees : {', '.join(vivantes)}"
                ),
                "tasks": vivantes,
            })

    return warnings


def model(
    chantier_id: str,
    alive: Callable[[str], bool] | None = None,
    usage_de: Callable[[dict], dict | None] | None = None,
) -> dict:
    """Tout ce qu'il faut savoir d'un chantier pour le dessiner, en lecture seule.

    `alive` recoit un identifiant de pane et rend sa vivacite. Sans lui, paneAlive vaut
    None partout et aucun pane mort n'est signale : le module ne parle jamais a tmux
    lui-meme, l'appelant decide s'il paie ce cout.

    `usage_de` recoit une tache et rend ses jetons consommes, ou None. Injecte pour la meme
    raison : lire un transcript de plusieurs mega-octets est un cout que l'appelant doit
    choisir de payer, et un modele qui irait fouiller le disque de son cote ne serait plus
    testable sans ce disque.
    """
    state = store.load()
    ch = state["chantiers"].get(chantier_id)
    if ch is None:
        raise chantier.ChantierError(f"campaign not found: {chantier_id}")

    tasks = {
        tid: t for tid, t in state["taches"].items() if t["chantier"] == chantier_id
    }
    ids = sorted(tasks, key=_num_id)
    niveaux = _levels(tasks)
    decisions, decisions_par_tache = _decisions_par_tache(chantier_id, ids)

    dependants: dict[str, list[str]] = {tid: [] for tid in ids}
    for tid in ids:
        for dep in tasks[tid].get("dependsOn") or []:
            if dep in dependants:
                dependants[dep].append(tid)

    groupes_declares = _groupes_declares(ch)
    nodes: dict[str, dict] = {}
    for tid in ids:
        t = tasks[tid]
        checklist = t.get("checklist") or []
        rapport = t.get("report") if isinstance(t.get("report"), dict) else {}
        blocked_by = _blocking_reasons(t, tasks)
        pane_id = t.get("paneId")
        modele, motif_modele, modele_predit = _modele(t)
        nodes[tid] = {
            "id": tid,
            "titre": t["titre"],
            "state": t["state"],
            "level": niveaux[tid],
            "group": _phase_key(t["titre"]),
            "deps": list(t.get("dependsOn") or []),
            "dependants": dependants[tid],
            "touches": list(t.get("touches") or []),
            "checklist": checklist,
            "checkDone": sum(1 for i in checklist if i["done"]),
            "checkTotal": len(checklist),
            "currentItem": t.get("currentItem"),
            "paneId": pane_id,
            "paneAlive": None if (alive is None or not pane_id) else bool(alive(pane_id)),
            "attempts": t.get("attempts", 0),
            "compactions": t.get("compactions", 0),
            "model": modele,
            "modelWhy": motif_modele,
            "modelPredit": modele_predit,
            "priority": t.get("priority", 0),
            "createdAt": t.get("createdAt"),
            "startedAt": t.get("startedAt"),
            "finishedAt": t.get("finishedAt"),
            "lastReportAt": t.get("lastReportAt"),
            "elapsedS": _elapsed(t),
            "reportState": rapport.get("state"),
            "reportNote": rapport.get("note"),
            "error": t.get("error"),
            "notes": list(t.get("notes") or []),
            "prompt": t.get("prompt") or "",
            "why": t.get("why") or "",
            "usage": usage_de(t) if usage_de else None,
            "blockedBy": blocked_by,
            "ready": t["state"] == "queued" and not blocked_by,
            "decisions": decisions_par_tache[tid],
        }

    # Les phases nommees sans aucune tache comptent : ce sont les phases annoncees, celles
    # que l'orchestratrice a prevues et pas encore decoupees. Les taire ferait passer un
    # chantier de six phases pour un chantier d'une seule.
    cles = {nodes[tid]["group"] for tid in ids} | set(groupes_declares)
    groupes = []
    for cle in _ordre_groupes(cles):
        membres = [tid for tid in ids if nodes[tid]["group"] == cle]
        declare = groupes_declares.get(cle) or {}
        groupes.append({
            "key": cle,
            "label": declare.get("label") or (f"Phase {cle}" if cle else "Hors phase"),
            "why": declare.get("why") or "",
            "planned": not membres,
            "tasks": membres,
            "done": sum(1 for m in membres if nodes[m]["state"] == "done"),
            "minLevel": min((nodes[m]["level"] for m in membres), default=0),
            "maxLevel": max((nodes[m]["level"] for m in membres), default=0),
        })

    levels: list[list[str]] = []
    for niveau in range(max(niveaux.values()) + 1 if niveaux else 0):
        levels.append([tid for tid in ids if nodes[tid]["level"] == niveau])

    counts = {"total": len(ids)}
    for tid in ids:
        counts[nodes[tid]["state"]] = counts.get(nodes[tid]["state"], 0) + 1

    observees = [g["key"] for g in groupes if g["key"]]
    declarees = _phases_declarees(ch.get("objectif") or "")
    # Ce qui reste a faire pour que le graphe TEL QU IL EST soit epuise : les taches que
    # rien n'attend. C'est la reponse la moins mauvaise a "quand est-ce fini", et elle se
    # lit avec la ligne des phases manquantes, qui dit ce que le graphe ne couvre pas
    # encore. Les annulees en sont exclues : elles ne clotureront jamais rien.
    terminales = [
        tid for tid in ids
        if not nodes[tid]["dependants"] and nodes[tid]["state"] != "cancelled"
    ]

    return {
        "campaign": {
            "id": ch["id"],
            "slug": ch.get("slug"),
            "project": ch.get("project"),
            "objectif": ch.get("objectif") or "",
            "perimetre": ch.get("perimetre") or "",
            "horsScope": ch.get("horsScope") or "",
            "state": ch["state"],
            "tmuxSession": ch.get("tmuxSession"),
            "createdAt": ch.get("createdAt"),
            "closedAt": ch.get("closedAt"),
            # Le home voyage avec la campagne parce que toute commande qu'on affiche à
            # l'humain doit le porter. Un `ordo answer` tapé depuis un autre projet ne
            # trouve pas la question, et ne dit pas pourquoi.
            "home": str(store.home()),
        },
        "counts": counts,
        "phases": {
            "observed": observees,
            "declared": declarees,
            "missing": [p for p in declarees if p not in observees] if declarees else [],
        },
        "terminals": terminales,
        # Le decoupage muet, nomme. Une tache annulee en est exclue : elle ne sera jamais
        # executee, exiger son explication ferait du bruit sans rien eclairer.
        "missingWhy": [
            tid for tid in ids
            if not nodes[tid]["why"] and nodes[tid]["state"] != "cancelled"
        ],
        "groups": groupes,
        "levels": levels,
        "nodes": nodes,
        "edges": [
            {"from": dep, "to": tid}
            for tid in ids
            for dep in nodes[tid]["deps"]
            if dep in nodes
        ],
        "decisions": decisions,
        "questions": _questions(chantier_id, state, tasks),
        "warnings": _avertissements(ch, tasks, niveaux, alive),
        "generatedAt": store.now(),
    }


# ---------------------------------------------------------------------------
# Rendu HTML
# ---------------------------------------------------------------------------


def _e(value: object) -> str:
    """Echappement HTML de tout ce qui vient de l'etat.

    Volontairement local plutot qu'un import de `html.escape` : ce module s'appelle carte
    et expose une fonction html(), les deux noms se marcheraient dessus a la lecture.
    """
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# Ce qu'on ecrit d'un pane selon ce que le verificateur a rendu. "?" et non "mort" quand
# personne n'a verifie : afficher "mort" faute de mesure inventerait un incident.
_VIVACITE = {True: "vivant", False: "MORT", None: "?"}

# Separateur des faits courts d'une carte. Caractere litteral et non entite HTML : la
# chaine passe ensuite par _e(), qui echapperait le "&" d'une entite et l'afficherait tel
# quel a l'ecran.
_SEP = " · "


def _duree(secondes: int | None) -> str:
    if secondes is None:
        return "-"
    if secondes < 60:
        return f"{secondes}s"
    if secondes < 3600:
        return f"{secondes // 60}m"
    return f"{secondes // 3600}h{(secondes % 3600) // 60:02d}"


def _duree_min(minutes: int | None) -> str:
    """Formate une durée exprimée en MINUTES-CLAUDE (estimation de critère, brief t-27),
    jamais en secondes : contrairement à _duree() ci-dessus, cette échelle n'a jamais de
    précision seconde, et un suffixe "s" y laisserait croire à une précision qu'on n'a pas.
    """
    if minutes is None:
        return ""
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}"


def _restant_min(checklist: list[dict]) -> int | None:
    """Minutes-Claude restant : somme des durées des critères NON cochés (brief t-27).

    None, jamais zéro, dès qu'un seul critère non coché porte une durée inconnue : un
    restant partiel se lirait comme le restant complet, alors qu'il le sous-estime. Zéro
    n'est un vrai zéro que lorsque chaque critère non coché (s'il en reste) porte une
    durée connue -- ou qu'il n'en reste aucun. .get() et non [] : un critère créé avant ce
    champ n'a jamais porté la clé "dureeMin" sur disque (c8).
    """
    restants = [item for item in checklist if not item["done"]]
    if any(item.get("dureeMin") is None for item in restants):
        return None
    return sum(item["dureeMin"] for item in restants)


def _total_estime_min(checklist: list[dict]) -> int | None:
    """Minutes-Claude estimées au total : somme de CE QUI EST CONNU, cochés compris (brief
    t-27). Le total de la tâche se calcule, il ne se saisit pas -- jamais une somme
    partielle cachée derrière un None comme _restant_min ci-dessus : un seul critère estimé
    suffit déjà à juger un dépassement sur ce qu'on sait, plutôt que de se taire tant que
    la checklist entière n'est pas estimée.
    """
    valeurs = [item["dureeMin"] for item in checklist if item.get("dureeMin") is not None]
    return sum(valeurs) if valeurs else None


def _restant_chantier_min(nodes: dict[str, dict]) -> int | None:
    """Minutes-Claude restant du CHANTIER ENTIER (brief t-38) : somme, sur les tâches NI
    FINIES NI ANNULÉES (même filtre que settled() côté JS), des restants déjà calculés
    par _restant_min ci-dessous pour chaque case -- jamais un second calcul du même
    nombre, qui finirait par diverger de celui affiché sur chaque tâche.

    Une tâche dont le restant est partiellement inconnu (_restant_min y répond None) est
    ignorée de la somme, exactement comme son propre None l'ignore déjà de sa case : un
    trou d'estimation sur une tâche ne doit pas taire le total des vingt-cinq autres.

    None seulement quand AUCUNE tâche active n'a le moindre restant connu : l'en-tête
    doit alors ne rien afficher plutôt que mentir par un "0h00" qui se lirait comme
    "chantier fini" (point 2 du brief).
    """
    connus = [
        r
        for r in (
            _restant_min(node["checklist"])
            for node in nodes.values()
            if node["state"] not in ("done", "cancelled")
        )
        if r is not None
    ]
    return sum(connus) if connus else None


def _depassement_min(state: str, elapsed_s: int | None, total_estime_min: int | None) -> str:
    """Le passé (elapsedS, mesuré depuis startedAt, jamais déclaré -- voir _elapsed) au-delà
    du total estimé (brief t-27, point 5) : jamais masqué en zéro, ce serait un mensonge
    exactement au moment où l'humain a besoin de l'information. Chaîne vide tant que le
    passé ne dépasse pas l'estimé, ou que l'un des deux est inconnu -- rien à comparer.

    Chaîne vide aussi sur une tâche TERMINÉE (state == "done", retouche t-38) : son écart
    (voir _ecart_valeur_texte ci-dessous) dit déjà le même fait, dans les deux sens en
    plus, et le redire ici doublait le nombre à l'écran -- "+20m" ici, "estimé 1h50
    (+20m)" dans l'écart, pour une seule et même tâche.
    """
    if state == "done":
        return ""
    if elapsed_s is None or total_estime_min is None:
        return ""
    ecart = (elapsed_s // 60) - total_estime_min
    if ecart <= 0:
        return ""
    return "+" + _duree_min(ecart)




def _ecart_valeur_texte(ecart: dict | None) -> str:
    """La différence entre estimé et réel d'une tâche TERMINÉE, SIGNÉE et SEULE (brief
    t-33, retouche t-38) : "-1h27" pour un gain -- plus rapide que prévu --, "+20m" pour
    un dépassement. Le signe porte le sens ; c'est à la carte de le confirmer par une
    couleur (vert pour un gain, couleur d'alerte pour un dépassement).

    Chaîne vide dès que rien n'a été mesuré ou estimé -- même convention que
    _depassement_min et "restant" plus haut.
    """
    if ecart is None:
        return ""
    signe = "+" if ecart["ecartMin"] > 0 else ("-" if ecart["ecartMin"] < 0 else "")
    return f'{signe}{_duree_min(abs(ecart["ecartMin"]))}'




# ---------------------------------------------------------------------------
# Projection consommee par la page
# ---------------------------------------------------------------------------


# Documents longs d'une tache : rangs a part, replies par defaut, parce qu'un prompt fait
# deux mille caracteres et noierait tout le reste de la fiche s'il etait affiche a plat.
def _docs(node: dict) -> list[dict]:
    docs = []
    if node["prompt"]:
        docs.append({"k": "prompt", "text": node["prompt"]})
    rapport = node["reportState"] or node["reportNote"]
    if rapport:
        docs.append({
            "k": "rapport",
            "text": f'{node["reportState"] or "?"}\n\n{node["reportNote"] or ""}'.strip(),
        })
    if node["decisions"]:
        docs.append({
            "k": "decisions",
            "text": "\n".join(f'{d["heure"]}  {d["texte"]}' for d in node["decisions"]),
        })
    if node["error"]:
        docs.append({"k": "erreur", "text": node["error"]})
    return docs


def _facts(node: dict, jetons: dict | None = None) -> dict:
    """Les faits courts d'une tache, ceux qui tiennent sur une ligne chacun.

    "depend de" et "debloque" en font partie alors que la page les affiche deja en chaine
    cliquable : la page les ecarte elle-meme du tableau. Les garder ici rend la vue lisible
    telle quelle, en JSON, pour qui inspecte le fichier a la main.
    """
    faits = {
        "zones": ", ".join(node["touches"]) or "-",
        "modele": node["model"],
        "compactions": str(node["compactions"]),
        "niveau": str(node["level"]),
        "depend de": " ".join(node["deps"]) or "-",
        "debloque": " ".join(node["dependants"]) or "-",
        "duree": _duree(node["elapsedS"]),
        "pane": (
            f'{node["paneId"]} {_VIVACITE[node["paneAlive"]]}' if node["paneId"] else "-"
        ),
        "tentatives": str(node["attempts"]),
        "demarree": node["startedAt"] or "-",
        "finie": node["finishedAt"] or "-",
    }
    if jetons:
        # Chaque compteur a part, jamais additionne : vingt-cinq millions de jetons de
        # cache relu ne valent ni le meme prix ni la meme chose que vingt-cinq millions de
        # jetons ecrits, et les fondre en un total unique le ferait croire.
        faits["jetons sortis"] = f'{jetons["output"]}'
        faits["jetons entres"] = f'{jetons["input"]}'
        faits["cache cree"] = f'{jetons["cacheCreation"]}'
        faits["cache relu"] = f'{jetons["cacheRead"]}'
        faits["tours"] = f'{jetons["turns"]}'
    return faits


def _meta(node: dict) -> str:
    """Faits courts de la ligne de détail. La durée n'y est plus : elle a rejoint l'en-tête
    de la carte, où elle reste visible quelle que soit la vue. La progression de checklist
    non plus : elle vit désormais dans sa propre ligne, visible sans ouvrir la tâche et dans
    les deux vues (voir vue())."""
    bouts = []
    if node["paneId"]:
        bouts.append(f'{node["paneId"]} {_VIVACITE[node["paneAlive"]]}')
    return _SEP.join(bouts)


def _doing(node: dict) -> str:
    """Le libellé du critère que l'exécutante a déclaré en cours (`ordo check --doing`).

    Chaîne vide, jamais un tiret ni un zéro, quand rien n'a été déclaré : une case qui n'a
    rien à dire à cet endroit ne doit rien afficher qui se lise comme une information.
    currentItem est un identifiant d'item, pas son texte -- le retrouver dans la checklist
    est le seul moyen de l'afficher.
    """
    courant = node["currentItem"]
    if not courant:
        return ""
    for item in node["checklist"]:
        if item["id"] == courant:
            return item["label"]
    return ""


def vue(m: dict) -> dict:
    """Aplatit le modele dans la forme que la page consomme.

    Deux formes et pas une seule parce qu'elles servent deux lecteurs. model() est le
    modele d'Ordo : stable, teste, et c'est lui que `--json` publie, donc le contrat d'un
    outil tiers. vue() est ce dont le rendu a besoin, dans l'ordre ou il le lit. Les
    confondre obligerait a casser le contrat de `--json` a chaque retouche de la page.
    """
    tasks = []
    for tid in sorted(m["nodes"], key=_num_id):
        node = m["nodes"][tid]
        jetons = node.get("usage")
        # Passé à "ecartValeur" ci-dessous, calculé ici pour rester à côté de son seul
        # appelant.
        ecart = chantier.ecart_estime_reel(node)
        tasks.append({
            "id": tid,
            "title": node["titre"],
            "status": node["state"],
            "ready": node["ready"],
            "level": node["level"],
            "phase": node["group"],
            "meta": _meta(node),
            # Duree et jetons sortent a part de meta : ce sont les deux seuls chiffres
            # qu'on veut lire SANS ouvrir la tache, et meta ne s'affiche pas en vue graphe.
            # Chaine vide et pas "-" quand on ne sait pas : un tiret se lit comme une duree
            # nulle, alors que la tache n'a simplement pas commence.
            "duree": _duree(node["elapsedS"]) if node["elapsedS"] is not None else "",
            # La progression de checklist sort à part de meta pour la même raison que la
            # durée : elle doit se lire SANS ouvrir la tâche, et dans les DEUX vues -- sur
            # une case fermée en vue graphe, meta ne s'affiche jamais (voir _meta()). doing
            # est le libellé du critère en cours, jamais un identifiant : une case qui n'a
            # rien déclaré rend une chaîne vide, jamais un tiret ni un zéro qui se liraient
            # comme une information.
            "checkDone": node["checkDone"],
            "checkTotal": node["checkTotal"],
            "doing": _doing(node),
            "elapsedS": node["elapsedS"],
            # Restant (somme des critères non cochés) et dépassement (passé au-delà du
            # total estimé) : brief t-27. Chaîne vide et jamais un zéro fabriqué quand on
            # ne sait simplement pas -- même convention que "duree" et "doing" ci-dessus.
            # "restant" ne s'affiche plus dans l'en-tête de la case fermée (retouche
            # demandée pendant t-41, qui lui préfère "totalEstime" à côté de la durée) mais
            # reste consommé tel quel par le détail ouvert (voir detailNode(), _JS).
            "restant": _duree_min(_restant_min(node["checklist"])),
            # Total estimé de la checklist (cochés compris), affiché à côté de la durée
            # écoulée sous la forme "écoulé / total" tant que la tâche n'est pas terminée
            # (retouche demandée pendant t-41) -- jamais un second calcul du restant, qui
            # ne dit que la moitié de l'histoire une fois une partie des critères cochés.
            "totalEstime": _duree_min(_total_estime_min(node["checklist"])),
            # Dépassement et écart sont désormais EXCLUSIFS, décidés par l'état de la
            # tâche (retouche t-38) : _depassement_min se tait lui-même dès que la tâche
            # est "done", pour ne jamais doubler ce que l'écart dit déjà mieux.
            "depassement": _depassement_min(
                node["state"], node["elapsedS"], _total_estime_min(node["checklist"])
            ),
            # Écart estimé/réel (brief t-33), sur une tâche TERMINÉE seulement : la
            # différence signée entre l'estimation totale de la checklist et la durée
            # effectivement mesurée, colorée côté JS selon son signe. Le rappel de
            # l'estimé seul ("estimé 1h50") a disparu de la case (retouche demandée
            # pendant t-41) : il doublait la durée déjà affichée à côté, et empêchait le
            # nom de tâche, le modèle et ces deux nombres de tenir sur une seule ligne.
            "ecartValeur": _ecart_valeur_texte(ecart),
            # Le modele sort a part de facts pour la meme raison que la duree : il se lit
            # SANS ouvrir la tache. C'est lui qui dit s'il faut relire le brief avant de
            # lancer, et le relire apres coup ne sert plus a rien.
            "model": node["model"],
            "modelWhy": node["modelWhy"],
            "modelPredit": node["modelPredit"],
            # Le contexte sort à part de facts, comme la durée et le modèle : il se lit
            # SANS ouvrir la tâche. C'est le contexte du DERNIER tour (voir usage.pour), pas
            # la sortie cumulée de la session : la sortie grossit avec la durée d'une
            # session sans jamais redescendre, elle ne dit rien de ce qu'un tour de plus va
            # coûter à relire. Le nombre de tours a occupé cette place avant lui et s'est
            # trompé sur seize sessions (voir le commentaire de usage.SEUIL_TOURS) : un
            # contexte lourd en peu de tours passait inaperçu. Il reste lisible dans facts,
            # en détail, où le pane vit déjà pour la même raison.
            "tokens": usage.court(jetons["dernierContexte"]) if jetons else "",
            # S'allume au même seuil que la compaction réelle (usage.SEUIL_CONTEXTE), jamais
            # redéfini ici : une carte qui alerterait à une autre valeur que celle qui
            # déclenche _compacter() raconterait une autre histoire que celle qui se joue.
            "contexteLourd": bool(
                jetons and jetons["dernierContexte"] >= (usage.SEUIL_CONTEXTE or float("inf"))
            ),
            "facts": _facts(node, jetons),
            "deps": node["deps"],
            "dependants": node["dependants"],
            "why": node["why"],
            # La page a besoin de distinguer "pas d'explication" de "explication vide" :
            # le premier merite un avertissement, le second n'existe pas.
            "whyAbsent": not node["why"],
            "waits": [
                {"id": r["id"], "text": f'{r["id"]} : {r["reason"]}'}
                for r in node["blockedBy"]
            ],
            # duree : déjà formatée en minutes-Claude par critère (brief t-31), même
            # convention que "restant" juste au-dessus -- chaîne vide et jamais un tiret ni
            # un zéro quand le critère n'a pas encore d'estimation (cas majoritaire).
            #
            # attrs : les cinq attributs de nature posés par t-36 (brief t-39), filtrés à
            # ceux réellement RENSEIGNÉS sur ce critère -- jamais les cinq clés à plat avec
            # une valeur vide, ce qui obligerait le JS à décider lui-même quoi taire (point
            # 1 du brief : l'absence est le cas courant, pas le cas limite). L'ordre vient
            # de chantier.ATTRIBUTS_VALEURS, source unique, plutôt qu'une seconde liste
            # dupliquée ici qui pourrait un jour diverger de celle qui valide les valeurs.
            "checklist": [
                {
                    "text": c["label"],
                    "done": bool(c["done"]),
                    "duree": _duree_min(c.get("dureeMin")),
                    "attrs": [
                        {"cle": cle, "valeur": c["attributs"][cle]}
                        for cle in chantier.ATTRIBUTS_VALEURS
                        if cle in c.get("attributs", {})
                    ],
                }
                for c in node["checklist"]
            ],
            "docs": _docs(node),
        })
    # La commande de réponse se compose ici et pas dans la page : elle porte le home, que
    # seul Python connaît, et son échappement est le genre de détail qu'un JS refait mal.
    # shlex.quote, parce qu'un ORDO_HOME peut contenir une espace, et qu'une commande
    # affichée puis copiée telle quelle doit marcher au premier essai.
    home = shlex.quote(m["campaign"].get("home") or "")
    questions = [
        dict(q, answerCmd=f'ORDO_HOME={home} ordo answer {q["id"]} "votre réponse"')
        for q in m["questions"]
    ]
    vu = {
        "campaign": m["campaign"],
        "questions": questions,
        "counts": m["counts"],
        "phasesInfo": m["phases"],
        "terminals": m["terminals"],
        "missingWhy": m["missingWhy"],
        "warnings": m["warnings"],
        # Restant du CHANTIER entier (brief t-38), en minutes brutes et pas déjà
        # formaté : contrairement à tout le reste de ce dict, ce nombre doit encore
        # traverser une addition côté navigateur ("maintenant" + ce restant, pour
        # l'heure de fin) avant de devenir du texte -- voir paintHeure() et
        # finChantier() dans _JS. None si aucune tâche active n'a de restant connu.
        "restantChantierMin": _restant_chantier_min(m["nodes"]),
        "tasks": tasks,
        "phases": [
            {
                "key": g["key"],
                "name": g["label"],
                "why": g["why"],
                "planned": g["planned"],
                "order": g["tasks"],
            }
            for g in m["groups"]
        ],
    }
    # Empreinte de TOUT sauf l'horodatage. La page interroge son serveur en boucle et ne
    # doit se redessiner que si quelque chose a reellement change ; comparer la reponse
    # entiere ne marcherait pas, puisque generatedAt bouge a chaque appel et rendrait tout
    # cycle different du precedent.
    empreinte = hashlib.sha256(
        json.dumps(vu, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    vu["generatedAt"] = m["generatedAt"]
    vu["fingerprint"] = empreinte
    return vu


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------


def _e(value: object) -> str:
    """Echappement HTML du peu de texte qui traverse encore le squelette de la page.

    Tout le contenu du chantier passe desormais par JSON puis par textContent, donc ne
    peut pas devenir du balisage. Il reste le titre de l'onglet, et c'est tout ; la
    fonction est gardee parce qu'un seul chemin non echappe suffit a rouvrir la porte.
    """
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# Le design d'origine chargeait IBM Plex depuis Google Fonts. Une carte doit rester
# lisible sur une machine sans reseau, et ne rien dire a personne de ce qu'on regarde :
# ces piles reprennent les memes proportions avec ce que le systeme a deja.
_CSS = """
:root{--bg:#0b0d10;--panel:#12151a;--row:#14171c;--rowsel:#171c23;--line:#232830;
--line2:#20252c;--txt:#dfe4ea;--txt2:#c9d1da;--dim:#858e9b;--dim2:#6d7683;--dim3:#5b6470;
--done:#46a35a;--done2:#3c7a4a;--running:#d3a03a;--finishing:#8b5cf6;--ready:#5aa2f0;
--queued:#4a5361;--blocked:#e05252;--cancelled:#333941;--up:#5fa96f;--down:#5aa2f0;
--accent:#8cc0f7;--lien:#2f6ba8;--relu:#3f5d80;--badge:#171b21;
--m-haiku:#addb76;--m-haiku-bd:#4a6529;--m-sonnet:#76dbcd;--m-sonnet-bd:#29655d;
--m-opus:#db76cc;--m-opus-bd:#65295c}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--txt);-webkit-font-smoothing:antialiased;
font:12.5px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.m{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
::selection{background:#1e3a5f}
@keyframes ordopulse{0%,100%{opacity:1}50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}

#top{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--line);
padding:12px 14px 10px}
#top h1{margin:0;font-size:12.5px;font-weight:400;color:var(--dim);
display:flex;align-items:baseline;gap:8px}
/* Colonne du mur (t-49, complété t-57) : l'onglet, hors de cette iframe, porte déjà
   l'identifiant, le pourcentage, le restant, l'heure de fin, et maintenant la progression
   par phase et les alertes -- les répéter ici referait le doublon que la fusion devait
   effacer. #top disparaît donc en entier, pas seulement son h1 : #segs et #bar vivent
   désormais dans la seconde ligne de l'onglet (voir annoncerOnglet() plus bas et _MUR_JS).
   La classe est posée par _JS au chargement (window.parent!==window, voir plus bas) : en
   page de fichier ou en panneau visité seul, elle ne l'est jamais et #top reste la seule
   source, pleinement visible. */
html.embarque #top{display:none}
.cid{font-size:12px;font-weight:600;letter-spacing:.04em;color:var(--txt);
border:1px solid #2b3038;border-radius:5px;padding:1px 6px}
/* ctx (slug/état/session) est le seul élément de l'en-tête à longueur variable : sans ce
   rétrécissement, il repousserait le bloc restant/fin/pourcentage hors de la colonne
   plutôt que de s'effacer devant lui (brief t-38, en-tête étroit). */
#ctx{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
/* Bloc de fin de ligne (restant, heure de fin, pourcentage) : un seul margin-left:auto
   pour les trois, posé sur le conteneur et non sur .pct comme avant (brief t-38). Poser
   la marge sur .pct seul aurait laissé le bloc [restant][fin] livré à lui-même dès que
   l'un des deux se cache (colonne sans rien d'estimé, point 2) : le pourcentage seul se
   serait alors retrouvé collé à ctx, sans le trou que laissait le bloc masqué. */
#hend{margin-left:auto;flex:none;display:flex;align-items:baseline;gap:8px;min-width:0}
#hrest{font-size:11.5px;color:var(--dim2)}
#hfin{font-size:11.5px;font-weight:600;color:var(--txt2)}
#hfin sup{font-size:9px;font-weight:600;color:var(--dim2);margin-left:1px}
.pct{font-size:13px;font-weight:600;color:var(--txt)}
#segs{display:flex;gap:3px;margin-top:9px;align-items:flex-end}
#segs .seg{min-width:12px}
#segs .track{height:6px;border-radius:2px;background:var(--line2);overflow:hidden}
#segs .fill{display:block;height:100%;border-radius:2px}
#segs .lab{font-size:9.5px;color:var(--dim2);margin-top:3px;text-align:center}
#bar{display:flex;gap:6px;margin-top:10px;align-items:center;flex-wrap:wrap}
.pill{border:1px solid var(--line);background:var(--panel);color:#9aa4b1;border-radius:20px;
font:inherit;font-size:11px;padding:4px 9px;cursor:pointer;flex:none}

/* Pastille d'alertes (t-48) : un triangle et un compte, plus jamais le détail étalé sur
   la barre -- il vit dans le calque #warn, ouvert au clic (voir paintTop() et #ask, dont
   ce calque reprend le mécanisme plutôt que d'en écrire un second). Zéro alerte : la
   pastille reste hidden, jamais un compte à zéro affiché pour rien. */
#warnchip{display:inline-flex;align-items:center;gap:4px;border-color:#63541f;color:#e3b341}
/* display:inline-flex ci-dessus prime sur la feuille de style du navigateur pour [hidden]
   (spécificité d'un identifiant contre celle d'un attribut) : sans cette règle, l'attribut
   hidden posé par paintTop() ne masquerait plus rien -- vu en mesure réelle, zéro alerte
   affichait quand même le triangle. Même piège déjà évité ailleurs pour #ask et #warn. */
#warnchip[hidden]{display:none}

/* Le bas est laissé vide exprès : sans cette réserve, la dernière phase ne peut jamais
   monter en haut de l'écran, même en repliant tout ce qui la précède. Aucun script ne s'y
   scrolle tout seul, c'est un espace à faire défiler, pas un mouvement. */
#board{position:relative;padding:14px 14px 70vh}
#wires{position:absolute;left:0;top:0;width:100%;pointer-events:none;z-index:0;
overflow:visible}
section.phase{margin-bottom:14px}
/* Plusieurs phases finies et repliées à la suite mangeaient toute la hauteur du mur, une
   par ligne. Condensées, elles tiennent à plusieurs sur la même bande et passent à la
   ligne quand il n'y a plus de place — un chantier à moitié fait ne demande plus qu'à
   faire défiler des en-têtes vides. */
.condensed{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.pchip{display:flex;align-items:center;gap:6px;min-width:0;max-width:100%;cursor:pointer;
background:var(--row);border:1px solid var(--line2);border-radius:20px;padding:3px 10px 3px 7px}
.pchip.full .pkey{color:var(--done);border-color:#2c4d35;background:#122116}
.pchip .cname{font-size:11px;color:var(--txt2);min-width:0;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.pchip .pcount{min-width:0;flex:none}
.phead{position:relative;z-index:3;background:var(--bg);display:flex;align-items:center;
gap:8px;cursor:pointer;padding:2px 0 6px}
.pkey{font-size:10.5px;font-weight:600;color:#9aa4b1;border:1px solid #2b3038;
border-radius:4px;padding:1px 6px;flex:none}
.phase.full .pkey{color:var(--done);border-color:#2c4d35;background:#122116}
.pname{font-size:13px;font-weight:600;color:var(--txt)}
.pright{margin-left:auto;display:flex;align-items:center;gap:8px;flex:none}
.ptrack{width:54px;height:4px;border-radius:2px;background:var(--line2);overflow:hidden;
display:block}
.pfill{display:block;height:100%}
.pcount{font-size:10.5px;color:var(--dim);min-width:38px;text-align:right}
.chev{color:var(--dim3);font-size:10px;display:inline-block;transition:transform .12s}
.phase.closed .chev{transform:rotate(-90deg)}
.pwhy{position:relative;z-index:3;background:var(--bg);font-size:11.5px;color:var(--dim2);
line-height:1.45;margin:0 0 8px;padding:0 0 4px 1px;text-wrap:pretty}
.pwhy.absent{color:#8a7433}
.phase.closed .pwhy,.phase.closed .rangs{display:none}
.rangs{display:flex;gap:9px}
.view-graphe .rangs{flex-direction:row;flex-wrap:wrap;gap:18px 26px;align-items:flex-start}
.rang{min-width:0}
.view-graphe .rang{flex:1 1 190px;max-width:240px}
.view-graphe .rang.wide{flex:1 1 100%;max-width:none}
/* Un rang sans voisin à sa droite sur sa ligne (brief t-58, point 7) : posé par
   elargirRangsSansVoisin() une fois les rangs mesurés dans le DOM réel -- le placement
   par niveau et le retour à la ligne ne changent pas, seul le plafond de largeur saute.
   flex-grow (déjà sur .rang) fait le reste : un rang qui partage sa ligne à plein
   n'obtient jamais cette classe, donc ne bouge pas (voir la précaution 2 du brief). */
.view-graphe .rang.etire{max-width:none}
.rlist{display:flex;flex-direction:column;gap:7px}

/* Repli de tâche (t-44) : en mode graphe, une case réglée qu'aucune dépendante directe
   n'attend plus rejoint cette liste en tête de phase au lieu de la grille de cases. Elle
   garde exactement le rendu de rowNode() (mêmes champs, mêmes classes, rien de plus créé
   ni retiré côté JS) -- seule cette mise en page change, en tassant les deux blocs que le
   mode graphe construit déjà pour une case réglée fermée (en-tête, titre ; .rmeta ne se
   construit jamais ici, condition inchangée de t-24) sur une seule ligne au lieu de deux.
   Le titre, seul champ de longueur imprévisible, se coupe à l'ellipse plutôt que de
   revenir à la ligne. Rouverte (.sel), plus aucune règle ici ne s'applique : elle
   retrouve la case entière, empilée, telle qu'elle serait dans la grille.
   Le but du repli est de rendre de la place verticale : toute la ligne réduit donc son
   texte, pas seulement l'identifiant, et le gap entre lignes se resserre en conséquence
   -- une tâche réglée n'a plus besoin de se lire d'aussi loin qu'une tâche active. */
.repli.rlist{gap:2px}
.repli{margin-bottom:7px}
.repli .row:not(.sel){display:flex;align-items:baseline;gap:7px;font-size:10px;
padding:3px 10px 3px 13px;overflow:hidden}
.repli .row:not(.sel) .sb{top:50%;transform:translateY(-50%);height:12px}
.repli .row:not(.sel) .rhead{flex:none;flex-wrap:nowrap;margin:0;gap:6px}
.repli .row:not(.sel) .rid{font-size:9px}
.repli .row:not(.sel) .rlinks{font-size:9px;gap:5px}
.repli .row:not(.sel) .rtitle{flex:1 1 auto;min-width:0;margin:0;font-size:10.5px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Condensation de la liste repliée (t-56) : au-delà du seuil, un bouton pleine largeur
   remplace les cases une à une, même chevron pivotant que l'accordéon de pièce jointe
   (.doc .chev2) pour rester dans le même vocabulaire visuel plutôt que d'en inventer un
   second. Le texte porte le nombre en premier, jamais un titre inerte : la couleur
   s'éclaircit au survol pour se lire comme cliquable. */
.rrepli{display:flex;align-items:center;gap:6px;width:100%;background:transparent;
border:none;text-align:left;cursor:pointer;padding:3px 10px 3px 13px;font-size:10px;
color:var(--dim2);font-family:inherit}
.rrepli:hover{color:var(--txt2)}
.rrepli .chev2{display:inline-block;flex:none;transition:transform .12s;color:var(--dim3)}
.rrepli.open .chev2{transform:rotate(90deg)}

.row{position:relative;cursor:pointer;background:var(--row);border:1px solid var(--line2);
border-radius:7px;padding:7px 10px 8px 13px;width:100%;min-width:0;z-index:1;
transition:opacity .12s,border-color .12s}
.row.sel{background:var(--rowsel);z-index:6}
.row.focus{border-color:#3f7fc4;box-shadow:0 0 0 1px #3f7fc4;z-index:6}
.row.up{border-color:#2f5c39}
.row.down{border-color:#2a4a6b}
.row.dim{opacity:.24}
.row.settled{opacity:.4}
.row.sel.settled{opacity:1}
.sb{position:absolute;left:4px;top:9px;width:3px;height:22px;border-radius:2px}
.row.running .sb{animation:ordopulse 1.6s ease-in-out infinite}
/* flex-wrap, parce qu'une colonne de mur fait quelques centaines de pixels : sans lui,
   la derniere pastille sortait de la case au lieu de passer a la ligne. */
.rhead{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap}
/* Le modèle teintait le TEXTE de l'identifiant (brief t-46) : ça abîmait la lisibilité
   de l'identifiant, ce qu'on cherche en premier, pour porter une information secondaire.
   Retouche demandée par l'humain (brief t-58, point 2) : l'identifiant redevient un
   texte comme un autre -- même variable que .rtitle/.cid -- et c'est une pastille .mdot
   (partagée avec le détail, voir plus bas) qui porte la teinte à sa place. */
.rid{font-size:10.5px;font-weight:600;color:var(--txt);flex:none}
/* Badge générique à petite étiquette texte : sert au "niveau N" du détail (profondeur
   dans le graphe) et, avec une pastille .mdot en plus, au modèle du détail ci-dessous. */
.mdl{font-size:9.5px;font-weight:600;letter-spacing:.03em;border:1px solid var(--line);
border-radius:4px;padding:0 4px;color:var(--dim2);flex:none}
/* Pastille du modèle : ronde, posée avant l'identifiant sur la case compacte (brief
   t-58, point 2) et devant le nom en toutes lettres une fois la case dépliée (détail,
   brief t-46) -- les DEUX endroits partagent cette même classe et ces mêmes teintes,
   jamais recopiées. Choisies loin de toute couleur d'état (voir le test de collision qui
   les vérifie), jamais reprises des anciennes couleurs de badge (haiku/sonnet/opus), qui
   partageaient presque le même ton que done/ready/finishing. Hérité, défaut ou l'absence
   de modèle ne posent aucune classe connue : rowNode() ne construit alors AUCUNE
   pastille, jamais une pastille grise par défaut (voir le garde `if(mconnu)`). */
.mdot{width:7px;height:7px;border-radius:50%;display:inline-block;flex:none;
vertical-align:middle;margin-right:4px;background:var(--dim3)}
.mdot.haiku{background:var(--m-haiku)}
.mdot.sonnet{background:var(--m-sonnet)}
.mdot.opus{background:var(--m-opus)}
/* Trait plein quand le modèle a réellement tourné, pointillé quand ce n'est encore
   qu'une prévision du routage : la différence entre un fait et un pronostic doit se voir
   sans lire le mot (même geste que l'ancien badge, avant qu'il ne se scinde en identifiant
   teinté + pastille du détail). */
.mdl.predit{border-style:dashed;opacity:.75}
.rlinks{margin-left:auto;flex:none;display:flex;gap:7px;font-size:10px}
/* Le focus (survol ou sélection) se signale déjà par la bordure et l'ombre de .row.focus
   ci-dessus : grossir la police en plus faisait bouger la taille des stats au simple
   passage de la souris sur une case du graphe, un second signal qui n'ajoutait rien --
   corrigé en même temps que le repli de tâche (t-44), même passe. */
.row.focus .rlinks{font-weight:600}
/* Une tâche jamais commencée (brief t-58, point 4) : ni checklist (0/N n'apprend rien
   que le titre ne dise déjà), ni jetons, ni temps écoulé -- voir jamaisCommencee() et le
   garde !jamaisLancee sur rprog plus bas, qui ne construisent alors plus rien à cet
   endroit. Le titre rejoint l'identifiant sur la même ligne au lieu d'ouvrir la sienne :
   .rhead redevient un flux de texte normal (le flex habituel n'a plus de sens sans les
   liens, eux-mêmes non construits pour cette case), et c'est ce flux, pas un calcul de
   largeur, qui fait passer le titre à la ligne suivante seulement s'il ne tient pas.
   Rouverte (.sel), la case retrouve sa présentation entière, comme n'importe quelle
   autre -- ce garde ne s'applique qu'aux cases fermées. Le flux normal ne pose AUCUN
   espace entre deux éléments en ligne (contrairement au gap de 7px de .rhead en mode
   flex) : sans marge explicite, le titre collerait à l'identifiant. margin-left reprend
   ce même 7px, cohérent avec le reste de la case (constaté par l'humain, mesuré via
   getBoundingClientRect dans le script de mesure dédié). */
.row.vierge:not(.sel) .rhead{display:block}
.row.vierge:not(.sel) .rtitle{margin-left:7px}
.dur{color:var(--dim2)}
/* Dépassement : passé au-delà du total estimé (brief t-27). --blocked, déjà la teinte
   d'alarme du reste de la carte (voir COL/kind()), jamais une nouvelle variable pour ce
   qui est la même famille de signal : quelque chose ne va pas comme prévu. */
.over{color:var(--blocked);font-weight:600}
/* Écart estimé/réel (brief t-33), sur une tâche terminée : neutre par défaut, --done (le
   même vert que le reste de la carte) sur un gain, --blocked (la même alarme que .over)
   sur un dépassement -- jamais une couleur inventée ici. Le rappel de l'estimé, qui avait
   sa propre classe .ecart, a disparu de la case (retouche demandée pendant t-41). */
.ecartv{color:var(--dim2);font-weight:600}
.ecartv.gain{color:var(--done)}
.ecartv.depasse{color:var(--blocked)}
/* Le contexte du dernier tour, pas la sortie cumulée de la session (voir vue() en
   Python). Il ne se colore que sur une tâche en cours, où le chiffre bouge encore ; au-delà
   du seuil de compaction lu dans usage.SEUIL_CONTEXTE, il vire à l'orange : c'est le
   chiffre qui prédit ce qu'une session va coûter à relire, là où son nombre de tours s'est
   trompé sur seize sessions (voir le commentaire de usage.SEUIL_TOURS). */
.tok{color:var(--dim2)}
.row.running .tok{color:var(--running)}
.tok.lourd{color:#e3b341;font-weight:600}
.rtitle{font-size:12.5px;line-height:1.35;color:var(--txt);margin-top:3px;text-wrap:pretty}
.row.cancelled .rtitle{color:var(--dim2);text-decoration:line-through}
.rmeta{display:flex;gap:9px;align-items:baseline;margin-top:3px;font-size:10.5px;
color:var(--dim2)}
.rmeta .zones{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* La checklist ne prend le vert du fini qu'à l'état "done" de la tâche : cochée à 100% sur
   une tâche encore "running", elle reste neutre, sans quoi la case raconterait un succès
   qu'elle n'a pas encore. Le libellé du critère en cours va jusqu'à 60 caractères, il ne
   tient pas dans une colonne de mur : il se coupe à l'ellipse, le texte complet reste
   joignable en infobulle. */
.rprog{display:flex;gap:6px;align-items:baseline;margin-top:3px;font-size:10.5px;min-width:0}
.rprog .cnt{color:var(--dim2);font-weight:600;flex:none}
.row.done .rprog .cnt{color:var(--done)}
.rprog .doing{color:var(--dim);min-width:0;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
/* Troisième état, pas une nuance du deuxième : une checklist à 100% sur une tâche encore
   running remplace le libellé du critère en cours (il n'y en a plus) par cette mention,
   dans le violet propre à cet état -- distinct du jaune "en cours" et du vert "fini",
   visible sans lire sur un mur de colonnes (voir kind() et .sb ci-dessous). */
.rprog .doing.rapport{color:var(--finishing);font-weight:600}

.detail{margin-top:9px;border-top:1px solid #262c34;padding-top:11px}

/* bandeau d'identité : qui exécute, où, combien de fois déjà, à quelle profondeur du
   graphe -- un badge neutre par fait, jamais un paragraphe. */
.strip{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.dtag{display:flex;align-items:center;gap:5px;font-size:10px;color:#9aa4b1;
background:var(--badge);border:1px solid var(--line);border-radius:4px;padding:1px 6px}
.dtag .dot{width:5px;height:5px;border-radius:50%}
.dtag.vivant .dot{background:var(--done);animation:ordopulse 1.8s ease-in-out infinite}
.dtag.mort .dot{background:var(--blocked)}
.dtag .dot.off{background:#3a4049}
.tries{display:flex;gap:3px;align-items:center}
.try{width:5px;height:5px;border-radius:50%;background:#9aa4b1;flex:none;display:inline-block}
.try.ko{background:var(--blocked)}
.try.off{background:transparent;border:1px solid #2b3038}

/* chaîne attend/débloque : le maillon central est la carte elle-même, elle n'est pas
   redessinée -- un simple trait la sépare des deux colonnes. */
.chain{display:flex;gap:10px;align-items:stretch;flex-wrap:wrap;margin-bottom:12px}
.chain .side{flex:1 1 190px;min-width:0}
.chain .sep{flex:none;align-self:stretch;width:1px;background:#262c34}
.k{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--dim2);
margin-bottom:5px}
.k.up{color:var(--up)}.k.down{color:var(--down)}
.k .aval{letter-spacing:0;text-transform:none;color:var(--dim3)}
.col{display:flex;flex-direction:column;gap:4px}
.chip{display:flex;align-items:center;gap:6px;cursor:pointer;background:#191d23;
border:1px solid #262c34;border-radius:5px;padding:4px 7px;min-width:0}
.chip:hover{border-color:#3f4753}
.dot{width:6px;height:6px;border-radius:2px;flex:none;display:inline-block}
.chip .cid2{font-size:10.5px;color:#9aa4b1;flex:none}
.chip .ctitle{font-size:11px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.none{font-size:11px;color:var(--dim3)}
.why{font-size:11.5px;line-height:1.5;color:var(--txt2);border-left:2px solid var(--lien);
padding-left:9px;text-wrap:pretty}
.why.absent{color:#8a7433;border-left-color:#63541f}
.waits{margin-top:9px;display:flex;flex-direction:column;gap:3px}
.wait{font-size:11.5px;color:var(--txt2);cursor:pointer;padding:3px 7px;background:#191d23;
border-left:2px solid var(--running);border-radius:0 4px 4px 0}

/* jauges comparatives : aucun chiffre seul, chacun porte son repere de comparaison, la
   mediane du chantier, pour se juger. */
.gauges{margin-top:11px;display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));
gap:9px 16px}
.g{min-width:0}
.track{position:relative;height:5px;border-radius:3px;background:var(--line2);margin-top:6px;
overflow:hidden}
.fill{position:absolute;left:0;top:0;bottom:0;border-radius:3px}
.tick{position:absolute;top:0;bottom:0;width:1px;background:var(--dim2)}
.gv{font-size:11px;color:var(--txt2);margin-top:5px}
.gv em{font-style:normal;color:var(--dim3)}
.pips{display:flex;gap:2px;height:5px;flex:1 1 auto;max-width:170px}
.pip{flex:1 1 0;min-width:3px;height:5px;border-radius:3px;background:#2b3038}
.pip.on{background:var(--done2)}
.pips.fini .pip.on{background:var(--done)}
.pip.doing{background:var(--running);animation:ordopulse 1.6s ease-in-out infinite}

/* composition des jetons : quatre postes illisibles seuls, une seule barre qui les montre
   d'un coup. Nom "jetons" et non "tok" : .tok désigne déjà le contexte du dernier tour
   dans l'en-tête de la case, un autre chiffre que la somme des quatre postes ici. */
.jetons{margin-top:12px}
.jhead{display:flex;align-items:baseline;gap:8px}
.jhead .tot{margin-left:auto;font-size:11px;color:var(--txt2)}
.jhead .tot em{font-style:normal;color:var(--dim3)}
.jbar{display:flex;gap:1px;height:9px;margin-top:6px;border-radius:3px;overflow:hidden;
background:var(--line2)}
.jbar span{min-width:2px}
.jlegend{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:6px;font-size:10px;
color:var(--dim2)}
.jlegend .item{display:flex;align-items:center;gap:5px}
.jlegend .sw{width:7px;height:7px;border-radius:2px}
.jlegend b{font-weight:400;color:var(--txt2)}
.jlegend i{font-style:normal;color:var(--dim3)}

/* frise : la tâche située dans la fenêtre du chantier */
.tl{margin-top:12px}
.tlbar{position:relative;height:14px;border-radius:4px;background:#101317;
border:1px solid #1e232a;overflow:hidden}
.tlspan{position:absolute;top:0;bottom:0;min-width:3px;border-radius:3px;opacity:.85}
.tlspan.open{animation:ordopulse 2.4s ease-in-out infinite}
.tlfoot{display:flex;align-items:baseline;gap:8px;margin-top:4px;font-size:9.5px;
color:var(--dim3)}
.tlfoot .lab{letter-spacing:.09em;text-transform:uppercase;color:#4c5561}
.tlfoot .rule{flex:1 1 auto;height:1px;background:#1e232a}

.zones{display:flex;flex-wrap:wrap;gap:4px;margin-top:11px}
.zone{font-size:10px;color:#9aa4b1;background:var(--badge);border:1px solid var(--line);
border-radius:4px;padding:1px 6px}

.checks{margin-top:12px}
.chead{display:flex;align-items:center;gap:8px}
.chead .cnt{font-size:10.5px;color:var(--dim2);margin-left:auto}
.check{display:flex;flex-direction:column;gap:3px;font-size:11.5px;line-height:1.4;
margin-top:4px}
.ctop{display:flex;gap:7px;align-items:flex-start}
.box{flex:none;width:13px;height:13px;border-radius:3px;border:1px solid #2b3038;
color:var(--done);font-size:9px;line-height:12px;text-align:center;margin-top:2px}
.box.on{border-color:#2c4d35;background:#122116}
.box.doing{border-color:#63541f;background:#241a0c;color:var(--running);
animation:ordopulse 1.8s ease-in-out infinite}
.check.on .ctext{color:#7d8794}
.check.doing .ctext{color:var(--txt)}
.ctext{flex:1 1 auto;min-width:0}
.cdur{flex:none;margin-left:auto;padding-left:8px;font-size:10.5px;color:var(--dim3);
white-space:nowrap}
/* Attributs de nature (brief t-39), en clair et non plus en initiale (retouche t-41) :
   sous le libellé et jamais à côté (voir .ctop ci-dessus) -- mesuré sur les 201 critères
   qualifiés de camcast, leurs cinq valeurs concaténées font jusqu'à 47 caractères et le
   libellé qui les accompagne jusqu'à 41, ce qu'aucune colonne de mur (340px, sa plus
   étroite mesurée) ne tient côte à côte sans tronquer ou repousser le libellé. flex-wrap
   plutôt qu'une troncature : une valeur qui ne tient pas passe à la ligne suivante,
   jamais coupée. padding-left aligne la première valeur sous le début du libellé (largeur
   de .box + gap de .ctop), pas sous la coche. Ton discret (var(--dim3), même famille que
   .cdur ci-dessus) pour ne pas rivaliser avec le critère en cours. */
.cattrs{display:flex;flex-wrap:wrap;gap:3px;padding-left:20px}
/* Retouche demandée par l'humain, même passe que t-44 : le marqueur écrit sa clé ET sa
   valeur ("dependance : aucune"), plus seulement la valeur -- lisible sans déjà connaître
   les cinq clés par cœur. Les chips passent désormais sur deux lignes sous le libellé
   (voir .cattrs ci-dessus), la place existe pour les deux ; s'il en manque quand même,
   c'est la clé qui cède : .cattrk peut se réduire et se couper à l'ellipse, .cattrv (le
   séparateur et la valeur) reste toujours entier, jamais tronqué. */
.cattr{display:flex;min-width:0;max-width:100%;font-size:10px;font-weight:600;
line-height:13px;color:var(--dim3);border:1px solid var(--line2);border-radius:3px;
padding:0 4px}
.cattrk{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cattrv{flex:none;white-space:nowrap}
.doc{margin-top:10px}
.doc button{width:100%;text-align:left;background:#191d23;border:1px solid #262c34;
border-radius:6px;color:#9aa4b1;font:inherit;font-size:11px;padding:5px 9px;cursor:pointer;
display:flex;gap:8px;align-items:center}
.doc .size{margin-left:auto;font-size:10px;opacity:.7}
.doc .chev2{display:inline-block;transition:transform .12s;color:var(--dim3)}
.doc.open .chev2{transform:rotate(90deg)}
.doc pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);
border:1px solid var(--line);border-top:none;border-radius:0 0 6px 6px;padding:9px;
margin:-2px 0 0;font-size:11px;line-height:1.55;color:#b8c1cb;max-height:340px;overflow:auto}
.doc:not(.open) pre{display:none}

/* Le calque des questions, et depuis t-48 celui des alertes (#warn) : même mécanisme,
   jamais deux. Une orchestratrice qui s'arrête pour demander un arbitrage le fait dans
   son terminal ; l'humain, lui, regarde cette page. Sans ce calque la campagne attend
   sans que rien ne le dise à l'endroit qu'il a sous les yeux. Reprend la teinte des
   avertissements, la seule de cette palette qui serve déjà à appeler le regard. */
#askchip{border-color:#63541f;color:#e3b341;cursor:pointer;
animation:ordopulse 2.2s ease-in-out infinite}
#ask,#warn{position:fixed;inset:0;z-index:40;background:rgba(6,8,11,.62);display:flex;
align-items:center;justify-content:center;padding:18px}
#ask[hidden],#warn[hidden]{display:none}
.askbox{width:100%;max-width:430px;background:#191408;border:1px solid #63541f;
border-radius:9px;padding:13px 14px;box-shadow:0 14px 40px rgba(0,0,0,.55)}
.warnlist{margin-top:8px;max-height:50vh;overflow:auto;color:#e3b341;font-size:11.5px}
.warnlist div+div{margin-top:6px}
.askhead{display:flex;align-items:center;gap:8px;font-size:10px;letter-spacing:.11em;
text-transform:uppercase;color:#e3b341;font-weight:600}
.askmore{margin-left:auto;font-size:10px;color:var(--dim2);letter-spacing:0;
text-transform:none}
.askx{background:transparent;border:1px solid #3a3524;border-radius:5px;color:var(--dim2);
font:inherit;font-size:12px;line-height:1;padding:2px 6px;cursor:pointer}
.askx:hover{color:var(--txt)}
.askwho{margin-top:8px;font-size:10.5px;color:var(--dim)}
.askwho.link{cursor:pointer;text-decoration:underline;text-decoration-color:#3a3524}
.asktext{margin-top:5px;font-size:13px;line-height:1.45;color:var(--txt);text-wrap:pretty}
.askopts{margin-top:8px;display:flex;flex-wrap:wrap;gap:5px}
.askopt{font-size:11px;color:var(--txt2);border:1px solid #3a3524;border-radius:5px;
padding:2px 7px}
.askcmd{margin-top:10px;display:flex;align-items:center;gap:6px}
.askcmd code{flex:1 1 auto;min-width:0;overflow:auto;white-space:nowrap;background:var(--bg);
border:1px solid var(--line);border-radius:5px;padding:5px 7px;font-size:10.5px;
color:#b8c1cb}
.askcmd button{flex:none;background:#241a0c;border:1px solid #63541f;border-radius:5px;
color:#e3b341;font:inherit;font-size:10.5px;padding:5px 8px;cursor:pointer}
.asktip{margin-top:7px;font-size:10.5px;color:var(--dim2);line-height:1.45;
text-wrap:pretty}

#legend{position:fixed;left:0;right:0;bottom:0;z-index:30;background:var(--bg);
border-top:1px solid var(--line);padding:6px 14px;display:flex;gap:12px;align-items:center;
font-size:10.5px;color:var(--dim2);flex-wrap:wrap}
#legend .sw{width:7px;height:7px;border-radius:2px;display:inline-block}
#legend span.item{display:flex;align-items:center;gap:5px}
/* Sépare la famille des états de celle des modèles (brief t-46) : une simple ligne
   verticale, assez pour que l'œil ne les lise pas comme une seule série de sept teintes. */
#legend .lsep{width:1px;align-self:stretch;background:var(--line);flex:none}
#legend .sw.rond{border-radius:50%}
#empty{padding:40px;color:var(--dim2)}
""".strip()


# Transposition vanilla du composant Claude Design. Le design est ecrit pour React et son
# runtime DCLogic ; une carte Ordo est un fichier unique sans reseau, donc rien de tout ca
# ne peut etre embarque. Ce qui est porte, c'est le COMPORTEMENT : etat local, rendu
# derive, arcs traces depuis la position reelle des lignes, survol qui remonte et descend
# la chaine transitive.
#
# Le contenu du chantier n'entre JAMAIS dans le DOM par du balisage : il arrive en JSON et
# repart en textContent. C'est ce qui rend inoffensif un titre de tache qui contiendrait du
# HTML, et un modele en ecrit tot ou tard.
# Une seule légende pour les DEUX rendus, html() et panneau(). Elle vivait en dur dans
# html() seulement : le mur, servi par panneau(), affichait un pied vide, si bien que le
# violet de rédaction et les trois teintes de modèle n'ont jamais atteint l'écran que
# l'humain regarde vraiment. C'est le défaut que t-46 venait de corriger DANS la légende
# (deux tables de couleurs qui divergent) reparu un cran plus haut, entre deux rendus.
# Les couleurs restent prises dans les variables de thème, jamais recopiées en hexadécimal.
_LEGENDE = """<div id="legend">
  <span class="item"><span class="sw" style="background:var(--done)"></span>fait</span>
  <span class="item"><span class="sw" style="background:var(--running)"></span>en cours</span>
  <span class="item"><span class="sw" style="background:var(--finishing)"></span>rédaction du rapport</span>
  <span class="item"><span class="sw" style="background:var(--ready)"></span>lançable</span>
  <span class="item"><span class="sw" style="background:var(--queued)"></span>en attente</span>
  <span class="lsep"></span>
  <span class="item"><span class="sw rond" style="background:var(--m-haiku)"></span>haiku</span>
  <span class="item"><span class="sw rond" style="background:var(--m-sonnet)"></span>sonnet</span>
  <span class="item"><span class="sw rond" style="background:var(--m-opus)"></span>opus</span>
  <span style="margin-left:auto" id="foot"></span>
</div>"""

_JS = r"""
(function(){
// Détection d'un chargement en colonne du mur (t-49) : posée tout de suite, avant la
// première donnée, pour qu'aucun flash du doublon h1 ne précède son masquage (voir la
// règle html.embarque dans _CSS). En page de fichier ou en panneau visité seul, parent
// vaut window et cette classe n'est jamais posée.
if(window.parent!==window)document.documentElement.classList.add("embarque");
var D=null, S={sel:null,hover:null,closed:{},docs:{},repliOuvert:{}};
var byId={};
// Les repères du chantier pour les jauges du détail (médiane, plafond) : calculés une
// seule fois par jeu de données, pas à chaque case ouverte. Voir ordoContexte().
var CTX=null;

// Cle de stockage propre a la colonne. sessionStorage est commun a TOUTE l'origine, et le
// mur ouvre plusieurs colonnes dans cette meme origine : sans ce prefixe, ouvrir une tache
// dans une colonne l'ouvrirait dans les autres, et la derniere chargee ecraserait le
// defilement des precedentes. En page de fichier, ORDO_NS n'existe pas et les cles
// retrouvent exactement leur nom d'avant.
function K(n){return "ordo-"+n+(window.ORDO_NS||"")}

// Point d'entree unique du rendu, appele au chargement en mode fichier et a chaque
// battement en mode serveur. L'ETAT DE LECTURE NE BOUGE PAS : ni le defilement, ni la
// tache ouverte, ni la recherche, ni les phases repliees, parce que tout cela vit dans S
// et que S survit au remplacement des donnees. C'est toute la difference avec un
// rechargement de page, qui ramenait le lecteur en haut a chaque cycle.
window.ordoSetData=function(data){
  D=data;byId={};D.tasks.forEach(function(t){byId[t.id]=t});
  CTX=ordoContexte(D.tasks);
  if(S.sel&&!byId[S.sel])S.sel=null;
  if(S.hover&&!byId[S.hover])S.hover=null;
  var y=window.scrollY;
  render();
  if(window.scrollY!==y)window.scrollTo(0,y);
};

// Le defilement ne se restaure qu'APRES un rendu : avant, la page n'a pas encore sa
// hauteur et scrollTo ne va nulle part. En mode fichier ce rendu a lieu au chargement ; en
// colonne, il a lieu a la premiere reponse du serveur, que ce module ne voit pas. D'ou la
// fonction exposee plutot qu'un appel en dur ici.
window.ordoRestoreScroll=function(){
  try{var y=sessionStorage.getItem(K("scroll"));if(y)window.scrollTo(0,+y)}catch(e){}
};

function el(tag,cls,txt){var e=document.createElement(tag);if(cls)e.className=cls;
  if(txt!=null)e.textContent=txt;return e}
// running se scinde en deux au dernier critère coché : la tâche tourne encore, mais il
// n'y a plus rien à faire avancer, seulement le rapport à écrire. L'état se DÉDUIT de
// checkDone/checkTotal, jamais déclaré en plus par l'exécutante -- une checklist sans
// aucun critère (checkTotal 0) ne peut jamais être "à 100%" de rien.
function finReport(t){return t.checkTotal>0&&t.checkDone===t.checkTotal}
function kind(t){
  if(t.status==="done")return "done";
  if(t.status==="running")return finReport(t)?"finishing":"running";
  if(t.status==="cancelled")return "cancelled";
  if(t.status==="blocked"||t.status==="failed")return "blocked";
  return t.ready?"ready":"queued";
}
var COL={done:"#46a35a",running:"#d3a03a",finishing:"#8b5cf6",ready:"#5aa2f0",
         queued:"#4a5361",blocked:"#e05252",cancelled:"#333941"};
var LAB={done:"fait",running:"en cours",finishing:"termine",ready:"lançable",
         queued:"en attente",blocked:"bloquée",cancelled:"annulée"};
function settled(t){var k=kind(t);return k==="done"||k==="cancelled"}
// Une tâche jamais commencée (brief t-58, point 4) : aucun critère coché ET jamais
// démarrée -- les deux ensemble, jamais l'un sans l'autre. "jamais commencée" n'est PAS
// "sans critère coché" : une tâche relancée après un échec a pu tourner sans rien
// cocher (checkDone à 0) mais garde l'elapsedS posé par son premier passage -- elle
// n'est pas vierge, son temps écoulé reste une information et sa case garde son
// affichage complet (voir jamaisLancee dans rowNode()).
function jamaisCommencee(t){return t.checkDone===0&&t.elapsedS==null}
// Repli de tâche (t-44), un cran plus bas que le repli de phase (voir `fini` dans
// render()) : une tâche réglée (finie ou annulée) que plus personne n'attend. DIRECTE,
// pas transitive -- dès qu'une dépendante immédiate est elle-même réglée, le lien qui
// rendait cette tâche visible est consommé, ce qui se passe deux crans plus loin ne la
// concerne plus. Une tâche réglée sans aucune dépendante se replie aussi : personne ne
// l'attendait par définition. Mais une SEULE dépendante directe encore vivante (queued,
// ready, blocked ou running) suffit à garder la case : c'est le repère qui montre d'où
// vient ce qui tourne en ce moment, il ne doit jamais disparaître derrière une ligne.
function repliable(t){
  return settled(t)&&t.dependants.every(function(id){
    var d=byId[id];return !d||settled(d);
  });
}
// Condensation de la liste repliée (t-56) : une phase ancienne pouvait replier vingt
// tâches (t-44), une ligne chacune, qui mangeaient la moitié de l'écran et repoussaient
// hors de vue la tâche en cours plus bas dans la phase suivante. Au-delà de ce seuil,
// la liste elle-même se condense en une seule ligne portant leur nombre -- cinq ou
// moins, rien ne change, le seuil n'existe que pour les phases lourdes.
var SEUIL_REPLI_TACHES=5;
// rowNode et surBascule reçus en paramètres plutôt que lus sur S ou p : cette fonction
// ne construit qu'un DOM à partir de ce qu'on lui donne, sans connaître ni la clé de
// phase ni l'état global, ce qui la rend exécutable seule sous Node (voir
// TestCondensationDeLaListeRepliee) exactement comme peindreProgres() pour le mur.
// Le même bouton ouvre et referme (même écouteur, condition inversée) : il n'existe
// qu'au-delà du seuil, cinq ou moins ne le construit jamais et la liste retrouve
// exactement le rendu d'avant t-56. Point 4 du brief : ne reçoit jamais que `repliees`,
// jamais les tâches non repliables -- leur case reste dans `rangs`, hors de ce compte.
function construireListeRepli(repliees,ouvert,rowNode,surBascule){
  var listeRepli=el("div","rlist repli");
  var condenseRepli=repliees.length>SEUIL_REPLI_TACHES;
  var ouvertRepli=!condenseRepli||!!ouvert;
  if(condenseRepli){
    var btRepli=document.createElement("button");
    btRepli.type="button";
    btRepli.className="rrepli"+(ouvertRepli?" open":"");
    btRepli.appendChild(el("span","chev2","▸"));
    btRepli.appendChild(el("span",null,repliees.length+" tâches réglées"));
    btRepli.addEventListener("click",function(){surBascule(!ouvertRepli)});
    listeRepli.appendChild(btRepli);
  }
  if(ouvertRepli){
    repliees.forEach(function(t){listeRepli.appendChild(rowNode(t))});
  }
  return listeRepli;
}
function focusId(){return S.hover||S.sel}
// Chaine TRANSITIVE, pas seulement les voisins directs : ce qu'on veut savoir en survolant
// une tache, c'est tout ce qui la precede et tout ce qui tombe si elle tombe.
function related(id){
  var up={},dn={};
  (function walk(x){(byId[x]?byId[x].deps:[]).forEach(function(p){
    if(!up[p]){up[p]=1;walk(p)}})})(id);
  (function walk(x){(byId[x]?byId[x].dependants:[]).forEach(function(c){
    if(!dn[c]){dn[c]=1;walk(c)}})})(id);
  return {up:up,dn:dn};
}

function chip(id,parent){
  var t=byId[id];
  var c=el("div","chip");
  var d=el("span","dot");d.style.background=t?COL[kind(t)]:"#333941";c.appendChild(d);
  c.appendChild(el("span","cid2 m",id));
  c.appendChild(el("span","ctitle",t?t.title.replace(/^[\d.]+[a-z]? /,""):""));
  c.addEventListener("click",function(e){e.stopPropagation();select(id)});
  parent.appendChild(c);
}

function select(id){
  S.sel=(S.sel===id)?null:id;
  try{sessionStorage.setItem(K("sel"),S.sel||"")}catch(e){}
  render();
  if(!S.sel)return;
  var r=document.querySelector('[data-row="'+id+'"]');
  if(r){var b=r.getBoundingClientRect();
    if(b.top<90||b.top>window.innerHeight-140)window.scrollBy({top:b.top-140,behavior:"smooth"})}
}

function rowNode(t){
  var k=kind(t), sel=S.sel===t.id;
  // Une case finie ou annulée ET fermée n'a plus de lecteur pour son contexte de jetons,
  // sa progression de checklist ni son critère en cours : la session qui les a produits
  // est morte, le chiffre y est figé pour toujours. Repliée, elle tombe à l'en-tête et au
  // titre, comme une phase repliée (voir .condensed) un cran plus haut. Ouverte (sel),
  // rien ne change : c'est la même règle qu'à la condensation des phases dans render().
  var condense=settled(t)&&!sel;
  // Une tâche jamais commencée (brief t-58, point 4), fermée : elle n'a rien à dire
  // qu'un titre -- voir jamaisCommencee() et son emploi plus bas, pour la ligne de
  // progression comme pour la mise en page de l'en-tête. !settled(t) exclut une tâche
  // ANNULÉE avant même d'avoir démarré (jamaisCommencee peut valoir vrai pour elle aussi)
  // : le repli de tâche (t-44, `repliable()`) et sa mise en page à part la prennent déjà
  // en charge -- jamaisLancee et condense restent ainsi mutuellement exclusifs, jamais
  // les deux mises en page en concurrence sur la même case.
  var jamaisLancee=jamaisCommencee(t)&&!sel&&!settled(t);
  var cls="row "+k;
  if(sel)cls+=" sel";
  if(settled(t))cls+=" settled";
  if(jamaisLancee)cls+=" vierge";
  var row=el("div",cls);
  row.setAttribute("data-row",t.id);
  // La barre de couleur PORTE l'etat : la repeter en toutes lettres sur la case coutait
  // la moitie de la largeur d'une colonne et faisait deborder le reste. Le mot part en
  // infobulle, ou il reste joignable sans rien occuper.
  var sb=el("span","sb");sb.style.background=COL[k];sb.title=LAB[k];row.appendChild(sb);

  var head=el("div","rhead");
  // Le nom du modèle ne s'écrit plus sur la case compacte (brief t-46), et depuis ne
  // teint plus non plus le TEXTE de l'identifiant (brief t-58, point 2, retouche de
  // l'humain) : ça abîmait la lisibilité de l'identifiant, ce qu'on cherche en premier,
  // pour porter une information secondaire. À la place, une pastille ronde (.mdot,
  // partagée avec le détail plus bas) posée AVANT l'identifiant, qui reste un texte
  // comme un autre (voir .rid). La classe ne se prend QUE dans cette table de modèles
  // connus : la poser telle quelle laisserait écrire n'importe quel nom de classe de la
  // feuille de style depuis un --model tapé à la main. Hérité, défaut ou l'absence de
  // modèle ne matchent rien : AUCUNE pastille ne se construit alors, jamais une pastille
  // grise par défaut -- le point de prudence du brief. L'infobulle qui donnait le modèle
  // en clair suit la teinte : sur la pastille quand elle existe, retombe sur
  // l'identifiant sinon, pour ne jamais perdre l'information d'un modèle non reconnu.
  var mconnu={haiku:1,sonnet:1,opus:1}[t.model]?" "+t.model:"";
  var rid=el("span","rid",t.id);
  if(t.model){
    var titreModele=t.model+" — "+(t.modelPredit?"prévu : ":"")+(t.modelWhy||"");
    if(mconnu){
      var mdot=el("span","mdot"+mconnu);
      mdot.title=titreModele;
      head.appendChild(mdot);
    }else{
      rid.title=titreModele;
    }
  }
  head.appendChild(rid);
  var links=el("span","rlinks m");
  // Durée et contexte AVANT les liens, et toujours visibles : ce sont les deux chiffres
  // qu'on lit pendant qu'une tâche tourne, c'est-à-dire au seul moment où on la regarde.
  // Ils vivaient dans la ligne de meta, que la vue graphe n'affiche pas.
  //
  // Tant que la tâche n'est pas terminée, le total estimé de la checklist rejoint la
  // durée écoulée ici même, sous la forme "écoulé / total" (retouche demandée pendant
  // t-41) : c'est ce qui vivait auparavant dans .rprog sous forme de restant, et qui y
  // mangeait la place du libellé du critère en cours. Jamais l'un des deux nombres
  // fabriqué quand il manque -- t.duree seul reste l'affichage d'une tâche sans
  // estimation, exactement comme avant cette retouche.
  var durTxt=t.duree;
  if(durTxt&&t.status!=="done"&&t.totalEstime)durTxt+=" / "+t.totalEstime;
  if(durTxt)links.appendChild(el("span","dur",durTxt));
  // Le dépassement (passé au-delà du total estimé des critères, brief t-27) : à côté de
  // la durée, jamais soumis à `condense` -- une tâche finie en dépassement le reste,
  // c'est une donnée acquise pour affiner la prochaine estimation, pas un état transitoire
  // qui s'éteint avec la case.
  if(t.depassement){
    var over=el("span","over",t.depassement);
    over.title="dépassement : le temps passé dépasse le total estimé des critères";
    links.appendChild(over);
  }
  // Écart estimé/réel (brief t-33) : seulement sur une tâche terminée, dans les deux sens
  // -- distinct du dépassement ci-dessus, qui ne dit que la moitié de l'histoire. Le
  // rappel de l'estimé ("estimé 1h50") a disparu de la case (retouche demandée pendant
  // t-41) : juste la différence signée, à côté de la durée, colorée par classeEcart() --
  // "23m -1h27" tient sur une seule ligne avec le nom de tâche et le modèle, là où
  // "23m estimé 1h50 -1h27" ne tenait pas.
  if(t.ecartValeur){
    var cl=classeEcart(t.ecartValeur);
    var ev=el("span","ecartv"+(cl?" "+cl:""),t.ecartValeur);
    ev.title="écart : différence signée entre estimé et réel — négatif est un gain";
    links.appendChild(ev);
  }
  // t.tokens porte le contexte du DERNIER tour (voir vue() en Python), pas la sortie
  // cumulée de la session : c'est ce chiffre-là, pas l'autre, qui prédit ce qu'un tour de
  // plus va coûter à relire. Il s'allume au même seuil que celui qui déclenche la
  // compaction (usage.SEUIL_CONTEXTE), jamais un seuil posé ici. Le nombre de tours a
  // quitté l'en-tête : il reste lisible en détail, dans les faits de la tâche.
  if(t.tokens&&!condense){
    var tk=el("span","tok"+(t.contexteLourd?" lourd":""),t.tokens);
    tk.title=t.contexteLourd
      ? "contexte lourd : au-delà du seuil de compaction"
      : "contexte du dernier tour";
    links.appendChild(tk);
  }
  // Le nombre de dépendances et de dépendantes (brief t-58, point 1) : personne ne les
  // lit sans ouvrir la tâche, et ils occupaient le coin haut droit au moment où la place
  // manque le plus. Retirés de la case fermée -- ils restent lisibles au détail, dans la
  // chaîne attend/débloque (voir detailNode() plus bas, jamais touchée par ce retrait).
  //
  // jamaisLancee (point 4) : ces quatre champs sont TOUJOURS vides pour une tâche jamais
  // commencée (elapsedS/status l'excluent), donc `links` resterait un span sans aucun
  // enfant -- jamais attaché, pour ne pas casser en flux normal le flux du titre qui
  // rejoint l'identifiant juste en dessous (voir .row.vierge .rhead, display:flex sur
  // .rlinks romprait la ligne même vide).
  if(!jamaisLancee)head.appendChild(links);
  row.appendChild(head);
  if(jamaisLancee){
    head.appendChild(el("span","rtitle",t.title));
  }else{
    row.appendChild(el("div","rtitle",t.title));
  }

  // La progression de checklist et le critère en cours, visibles SANS ouvrir la tâche et
  // dans les deux vues : sur une case fermée en vue graphe, rmeta (ci-dessous) ne s'affiche
  // jamais, c'était la case n°1 pour laquelle une checklist entièrement cochée restait
  // invisible tant qu'on n'ouvrait pas la tâche. Le compteur ne prend la couleur du fini
  // qu'à l'état "done" (voir .row.done .cnt) : une checklist cochée à 100% sur une tâche
  // encore en cours reste un compteur neutre, jamais un mensonge de succès anticipé.
  // condense enveloppe ce bloc entier sans toucher à sa condition interne (t.checkTotal),
  // ni à son indentation d'origine : la case rouverte (sel) doit retrouver l'exacte même
  // construction qu'avant cette tâche, jamais une variante, et les tests qui découpent ce
  // bloc au caractère près (voir TestEcritureDuRapportEnCours) restent valables tels quels.
  // jamaisLancee (brief t-58, point 4) rejoint ce même garde-fou : un "0/9" sur une tâche
  // jamais commencée n'apprend rien que le titre ne dise déjà.
  if(!condense&&!jamaisLancee){
  if(t.checkTotal){
    var prog=el("div","rprog");
    prog.appendChild(el("span","cnt",t.checkDone+"/"+t.checkTotal));
    // Le restant vivait ici (brief t-27) ; il mangeait la place du libellé du critère en
    // cours juste après lui (retouche demandée pendant t-41). Il est monté dans l'en-tête,
    // à côté de la durée écoulée -- voir "totalEstime" dans rowNode() ci-dessus -- et le
    // libellé du critère en cours récupère désormais toute la largeur de cette ligne.
    // Le libellé du critère en cours et la mention de fin de rapport se disputent la
    // MÊME place : jamais les deux à la fois, jamais hors d'une tâche en cours. k vaut
    // déjà "finishing" (voir kind()) quand tout est coché sur une tâche running -- c'est
    // le seul signal qui compte, jamais currentItem, qui peut rester périmé après un
    // passage à "done" et raconterait alors un critère en cours qui n'existe plus.
    if(t.status==="running"){
      if(k==="finishing"){
        prog.appendChild(el("span","doing rapport","Écriture du rapport en cours"));
      }else if(t.doing){
        var doing=el("span","doing",t.doing);
        doing.title=t.doing;
        prog.appendChild(doing);
      }
    }
    row.appendChild(prog);
  }
  }

  if(sel){
    var meta=el("div","rmeta");
    meta.appendChild(el("span","m",t.meta||""));
    meta.appendChild(el("span","zones",t.facts.zones||""));
    row.appendChild(meta);
  }
  if(sel)row.appendChild(detailNode(t));

  // Le survol ne reconstruit PAS le plateau : il repeint les classes des lignes deja
  // posees. Mesure sur ce chantier, soixante taches : reconstruire a chaque mouvement de
  // souris detruisait la ligne sous le curseur, et le clic qui suivait partait dans le
  // vide, sur un noeud detache. React reconciliait, du HTML nu ne reconcilie rien.
  row.addEventListener("mouseenter",function(){S.hover=t.id;paintFocus()});
  row.addEventListener("mouseleave",function(){S.hover=null;paintFocus()});
  row.addEventListener("click",function(e){e.stopPropagation();select(t.id)});
  return row;
}

// Les quatre postes de jetons, dans l'ordre où ils se lisent : ce qui est entré, ce qui
// est sorti, puis le cache -- écrit une fois, relu à chaque tour. Les couleurs viennent
// des variables déjà posées sur les états de case : entrée/sortie partagent leur teinte
// avec les tuiles "lancable"/"fait", le cache écrit avec "en cours".
var POSTES=[
  {k:"jetons entres",lab:"entrés",c:"var(--ready)"},
  {k:"jetons sortis",lab:"sortis",c:"var(--done)"},
  {k:"cache cree",lab:"cache écrit",c:"var(--running)"},
  {k:"cache relu",lab:"cache relu",c:"var(--relu)"}
];

// "7m", "1h50" -> minutes. Une chaîne qui ne matche rien (le tiret d'une tâche jamais
// commencée) rend null, jamais zéro : une jauge à zéro se lirait comme une tâche
// instantanée.
function minutes(s){
  s=(s||"").trim();
  var h=/^(\d+)h(\d*)$/.exec(s), m=/^(\d+)m$/.exec(s), sec=/^(\d+)s$/.exec(s);
  return h?+h[1]*60+(+h[2]||0):m?+m[1]:sec?0:null;
}
function nb(v){
  if(v==null)return null;
  var n=+String(v).replace(/[^\d.-]/g,"");
  return isNaN(n)?null:n;
}
function fmt(n){
  if(n==null)return "—";
  if(n>=1e6)return (n/1e6).toFixed(2).replace(".",",")+" M";
  if(n>=1e4)return Math.round(n/1000)+" k";
  if(n>=1000)return (n/1000).toFixed(1).replace(".",",")+" k";
  return String(n);
}
function med(vals){
  var v=vals.filter(function(x){return x!=null}).sort(function(a,b){return a-b});
  return v.length?v[Math.floor(v.length/2)]:0;
}
function maxDe(vals){
  var v=vals.filter(function(x){return x!=null});
  return v.length?Math.max.apply(null,v):1;
}
function jetonsDe(facts){
  var s=0, vu=false;
  POSTES.forEach(function(p){var v=nb(facts[p.k]);if(v!=null){vu=true;s+=v}});
  return vu?s:null;
}
function hhmm(ms){
  var x=new Date(ms), p=function(n){return (n<10?"0":"")+n};
  return p(x.getDate())+"/"+p(x.getMonth()+1)+" "+p(x.getHours())+":"+p(x.getMinutes());
}

// Les repères du chantier pour les jauges du détail : médiane et plafond de durée, de
// tours, de jetons, et le poids aval (tout ce qu'une tâche débloque, transitivement) qui
// dit si elle ouvre un boulevard ou une impasse. Calculé une fois par jeu de données, pas
// à chaque case ouverte (voir ordoSetData).
function ordoContexte(tasks){
  var poids={}, mins={}, jet={}, tours={}, stamps=[];
  function aval(t,acc){
    (t.dependants||[]).forEach(function(id){
      if(!acc[id]){acc[id]=1;var dt=byId[id];if(dt)aval(dt,acc)}
    });
    return acc;
  }
  tasks.forEach(function(t){
    var f=t.facts||{};
    poids[t.id]=Object.keys(aval(t,{})).length;
    mins[t.id]=minutes(f.duree);
    jet[t.id]=jetonsDe(f);
    tours[t.id]=nb(f.tours);
    var a=Date.parse(f.demarree||""), b=Date.parse(f.finie||"");
    if(!isNaN(a))stamps.push(a);
    if(!isNaN(b))stamps.push(b);
    // Une tâche encore en cours n'a pas de fin : la fenêtre du chantier s'étire jusqu'à
    // maintenant, sinon sa barre sortirait du cadre par la droite.
    if(t.status==="running"&&isNaN(b))stamps.push(Date.now());
  });
  var ids=tasks.map(function(t){return t.id});
  return {poids:poids,mins:mins,jet:jet,tours:tours,
    med:med(ids.map(function(i){return mins[i]})),
    maxMin:maxDe(ids.map(function(i){return mins[i]})),
    medJet:med(ids.map(function(i){return jet[i]})),
    maxJet:maxDe(ids.map(function(i){return jet[i]})),
    medTours:med(ids.map(function(i){return tours[i]})),
    maxTours:maxDe(ids.map(function(i){return tours[i]})),
    maxW:maxDe(ids.map(function(i){return poids[i]})),
    t0:stamps.length?Math.min.apply(null,stamps):0,
    t1:stamps.length?Math.max.apply(null,stamps):1};
}

// Une jauge = un libellé, une barre bornée par le chantier, un chiffre et son repère (la
// médiane, en trait). txt et repère sont deux nœuds de texte séparés, jamais une chaîne
// avec du balisage dedans : voir la règle d'échappement en tête de fichier.
function jauge(lab,val,ref,plafond,txt,repere,coul,tip){
  var g=el("div","g");
  g.appendChild(el("div","k",lab));
  var track=el("div","track");
  if(tip)track.title=tip;
  var w=val!=null&&plafond?Math.max(2,val/plafond*100):0;
  var fill=el("span","fill");fill.style.width=w+"%";fill.style.background=coul;
  track.appendChild(fill);
  if(ref&&plafond){
    var tick=el("span","tick");tick.style.left=(ref/plafond*100)+"%";
    track.appendChild(tick);
  }
  g.appendChild(track);
  var gv=el("div","gv m",txt);
  if(repere)gv.appendChild(el("em",null," · "+repere));
  g.appendChild(gv);
  return g;
}

// n pastilles ; "essais" teinte toutes sauf la dernière en échec (rouge), le dernier
// passage restant à désigner par la couleur de la case elle-même. Sans passage, une seule
// pastille neutre plutôt qu'une absence, pour ne pas se lire comme un défaut d'affichage.
function pips(n,essais){
  var wrap=el("span","tries");
  if(!n){wrap.appendChild(el("span","try off"));return wrap}
  for(var i=0;i<n;i++)wrap.appendChild(el("span","try"+(essais&&i<n-1?" ko":"")));
  return wrap;
}

function detailNode(t){
  var d=el("div","detail");
  var ctx=CTX||ordoContexte(D?D.tasks:[t]);
  var k=kind(t), f=t.facts||{}, enCours=t.status==="running";
  var cl=t.checklist||[];
  var faits=cl.filter(function(c){return c.done}).length;
  var doingIdx=enCours?cl.map(function(c){return c.done}).indexOf(false):-1;
  var w=ctx.poids[t.id]||0;
  var mn=ctx.mins[t.id], tours=nb(f.tours), jt=jetonsDe(f);
  var essais=nb(f.tentatives)||0, comp=nb(f.compactions), niveau=f.niveau;
  var a=Date.parse(f.demarree||""), bf=Date.parse(f.finie||"");
  var b=isNaN(bf)?(enCours?Date.now():a):bf;
  var span=Math.max(1,ctx.t1-ctx.t0);

  /* 1. qui exécute, où, combien de fois déjà, à quelle profondeur du graphe */
  var pane=(f.pane||"").trim();
  if(t.model||(pane&&pane!=="-")||essais||comp!=null||niveau!=null){
    var strip=el("div","strip");
    if(t.model){
      // La case compacte ne montre que la teinte sur l'identifiant (voir .rid ci-dessus) :
      // chaque pixel y compte. Ici, la tâche est dépliée -- il y a la place, et c'est le
      // moment où on cherche justement le détail -- donc le nom s'écrit en entier, à côté
      // de sa pastille (retouche de l'humain). Même garde-fou de classe qu'au .rid : la
      // table est la seule source de la teinte, jamais un nom de classe pris tel quel
      // depuis un --model tapé à la main.
      var connu={haiku:1,sonnet:1,opus:1}[t.model]?" "+t.model:"";
      var md=el("span","mdl m"+(t.modelPredit?" predit":""));
      md.appendChild(el("span","mdot"+connu));
      md.appendChild(document.createTextNode(t.model));
      md.title=t.model+" — "+(t.modelPredit?"prévu : ":"")+(t.modelWhy||"");
      strip.appendChild(md);
    }
    if(pane&&pane!=="-"){
      var vivant=/vivant/i.test(pane), mort=/MORT/.test(pane);
      var tg=el("span","dtag"+(vivant?" vivant":mort?" mort":""));
      tg.title="pane tmux de l'exécutante";
      tg.appendChild(el("span","dot"+(vivant||mort?"":" off")));
      tg.appendChild(document.createTextNode(pane));
      strip.appendChild(tg);
    }
    if(essais){
      var te=el("span","dtag");
      te.title=essais+" passage(s), le dernier en date à droite";
      te.appendChild(pips(essais,true));
      te.appendChild(document.createTextNode(essais>1?essais+" passages":"1 passage"));
      strip.appendChild(te);
    }
    if(comp!=null){
      var tc=el("span","dtag");
      tc.title="compactions de contexte subies par la session";
      tc.appendChild(pips(comp,false));
      tc.appendChild(document.createTextNode(
        comp?comp+" compaction"+(comp>1?"s":""):"aucune compaction"));
      strip.appendChild(tc);
    }
    if(niveau!=null){
      var tn=el("span","mdl m","niveau "+niveau);
      tn.title="profondeur dans le graphe de dépendances";
      strip.appendChild(tn);
    }
    d.appendChild(strip);
  }

  /* 2. la chaîne : le maillon central serait la carte elle-même, il n'est pas redessiné */
  var chain=el("div","chain");
  var left=el("div","side");
  left.appendChild(el("div","k up","attend ("+t.deps.length+")"));
  var lc=el("div","col");
  if(t.deps.length)t.deps.forEach(function(x){chip(x,lc)});
  else lc.appendChild(el("div","none","rien — elle peut partir seule"));
  left.appendChild(lc);chain.appendChild(left);
  chain.appendChild(el("div","sep"));
  var right=el("div","side");
  var kd=el("div","k down","débloque ("+t.dependants.length+")");
  if(w>t.dependants.length){
    var av=el("span","aval"," "+w+" en aval");
    av.title="tout l'aval transitif, maximum du chantier "+ctx.maxW;
    kd.appendChild(av);
  }
  right.appendChild(kd);
  var rc=el("div","col");
  if(t.dependants.length)t.dependants.forEach(function(x){chip(x,rc)});
  else rc.appendChild(el("div","none","rien n'attend après elle"));
  right.appendChild(rc);chain.appendChild(right);
  d.appendChild(chain);

  /* 3. le pourquoi, puis ce qui la retient */
  d.appendChild(el("div","why"+(t.whyAbsent?" absent":""),
    t.whyAbsent?"aucune explication enregistrée pour cette tâche":t.why));
  if(t.waits.length){
    var wa=el("div","waits");
    t.waits.forEach(function(x){
      var wb=el("div","wait",x.text);
      wb.addEventListener("click",function(e){e.stopPropagation();select(x.id)});
      wa.appendChild(wb);
    });
    d.appendChild(wa);
  }

  /* 4. les jauges comparatives */
  var gs=el("div","gauges");
  gs.appendChild(jauge("durée",mn,ctx.med,ctx.maxMin,
    mn!=null?f.duree:"—", mn!=null?"med "+ctx.med+"m":null,
    mn!=null&&mn>ctx.med?"var(--running)":"var(--done2)",
    mn!=null?mn+" min — médiane du chantier "+ctx.med+" min, plus longue "+ctx.maxMin+" min"
      :"pas de durée enregistrée"));
  if(tours!=null)gs.appendChild(jauge("tours",tours,ctx.medTours,ctx.maxTours,
    String(tours),"med "+ctx.medTours,
    tours>ctx.medTours?"var(--running)":"var(--lien)",
    tours+" tours de boucle — médiane "+ctx.medTours+", plus bavarde "+ctx.maxTours));
  if(jt!=null)gs.appendChild(jauge("jetons",jt,ctx.medJet,ctx.maxJet,
    fmt(jt),"med "+fmt(ctx.medJet),
    jt>ctx.medJet?"var(--running)":"var(--lien)",
    "total des quatre postes — médiane du chantier "+fmt(ctx.medJet)
      +", plus lourde "+fmt(ctx.maxJet)));
  if(cl.length){
    var gcrit=el("div","g");
    gcrit.appendChild(el("div","k","critères"));
    var pp=el("div","pips"+(k==="done"?" fini":""));
    pp.style.marginTop="6px";pp.style.maxWidth="none";
    cl.forEach(function(c,i){
      pp.appendChild(el("span","pip"+(c.done?" on":i===doingIdx?" doing":"")));
    });
    gcrit.appendChild(pp);
    var gv=el("div","gv m",faits+"/"+cl.length);
    if(doingIdx>-1)gv.appendChild(el("em",null," · 1 en cours"));
    gcrit.appendChild(gv);
    gs.appendChild(gcrit);
  }
  d.appendChild(gs);

  /* 5. la composition des jetons : une barre, quatre postes, le cache visible d'un coup */
  if(jt!=null){
    var parts=POSTES.map(function(p){
      var v=nb(f[p.k])||0;
      return {lab:p.lab,c:p.c,v:v,pc:jt?v/jt*100:0};
    });
    var jw=el("div","jetons");
    var jh=el("div","jhead");
    jh.appendChild(el("span","k","composition des jetons"));
    var tot=el("span","tot m",fmt(jt));
    if(tours)tot.appendChild(el("em",null," · "+fmt(Math.round(jt/tours))+"/tour"));
    jh.appendChild(tot);
    jw.appendChild(jh);
    var bar=el("div","jbar");
    parts.forEach(function(p){
      var seg=el("span",null);
      seg.style.flex=p.v+" 0 auto";seg.style.background=p.c;
      seg.title=p.lab+" : "+p.v.toLocaleString("fr-FR");
      bar.appendChild(seg);
    });
    jw.appendChild(bar);
    var leg=el("div","jlegend m");
    parts.forEach(function(p){
      var item=el("span","item");
      var sw=el("span","sw");sw.style.background=p.c;item.appendChild(sw);
      item.appendChild(document.createTextNode(p.lab+" "));
      item.appendChild(el("b",null,fmt(p.v)));
      item.appendChild(document.createTextNode(" "));
      item.appendChild(el("i",null,Math.round(p.pc)+"%"));
      leg.appendChild(item);
    });
    jw.appendChild(leg);
    d.appendChild(jw);
  }

  /* 6. la frise : la tâche située dans la fenêtre du chantier */
  if(!isNaN(a)){
    var tl=el("div","tl");
    var tb=el("div","tlbar");
    var tsp=el("span","tlspan"+(isNaN(bf)&&enCours?" open":""));
    tsp.style.left=((a-ctx.t0)/span*100)+"%";
    tsp.style.width=Math.max(1.2,(b-a)/span*100)+"%";
    tsp.style.background=COL[k];
    if(k==="cancelled")tsp.style.opacity=".35";
    tb.appendChild(tsp);tl.appendChild(tb);
    var tf=el("div","tlfoot m");
    tf.appendChild(el("span","lab","fenêtre"));
    tf.appendChild(el("span",null,hhmm(a)));
    tf.appendChild(el("span","rule"));
    tf.appendChild(el("span",null,isNaN(bf)?(enCours?"en cours":"—"):hhmm(bf)));
    tl.appendChild(tf);
    d.appendChild(tl);
  }

  /* 7. zones touchées */
  var zones=(f.zones||"").split(",").map(function(z){return z.trim()})
    .filter(function(z){return z&&z!=="-"});
  if(zones.length){
    var zo=el("div","zones");
    zones.forEach(function(z){zo.appendChild(el("span","zone m",z))});
    d.appendChild(zo);
  }

  /* 8. les critères, avec celui en cours désigné */
  if(cl.length){
    var ck=el("div","checks");
    var ch=el("div","chead");
    ch.appendChild(el("span","k",
      k==="finishing"?"rédaction du rapport":doingIdx>-1?"critère en cours et suite":"critères"));
    // Même nombre que la case fermée (span .rest, brief t-27) : t.restant vient déjà de
    // _restant_min côté Python, jamais resommé ici en JS à partir des durées de critère --
    // deux calculs finiraient par diverger sans que rien ne le signale (t-31).
    if(t.restant)ch.appendChild(el("span","cnt",t.restant));
    ck.appendChild(ch);
    cl.forEach(function(c,i){
      var st=c.done?" on":i===doingIdx?" doing":"";
      var line=el("div","check"+st);
      var top=el("div","ctop");
      top.appendChild(el("span","box"+st,c.done?"✓":i===doingIdx?"·":""));
      top.appendChild(el("span","ctext",c.text));
      // Chaîne vide et jamais un tiret ni un zéro quand le critère n'a pas encore
      // d'estimation : ce cas est le cas MAJORITAIRE aujourd'hui, pas le cas limite.
      if(c.duree)top.appendChild(el("span","cdur",c.duree));
      line.appendChild(top);
      // Attributs de nature (brief t-39), en clair (retouche t-41) puis clé ET valeur
      // (retouche demandée par l'humain, même passe que t-44) : "M" puis la valeur seule
      // n'identifiaient leur clé qu'au survol -- faux repos pour qui connaît déjà les
      // cinq clés par cœur, mais c'est qui les découvre qui compte. La clé traverse
      // maintenant à l'écran, jamais seulement dans title/aria-label ci-dessous.
      //
      // SOUS le libellé, jamais à côté de lui (ligne .ctop ci-dessus) : mesuré sur les
      // 201 critères qualifiés de camcast, un libellé fait jusqu'à 41 caractères, ce
      // qu'aucune colonne de mur (340px, sa plus étroite) ne tient à côté de rien
      // d'autre -- l'invariant du brief interdit d'y toucher. Les marqueurs se replient
      // donc sur deux lignes sous lui plutôt qu'à côté (flex-wrap sur .cattrs) ; à
      // l'intérieur d'un même marqueur, c'est la clé qui cède si la paire ne tient
      // toujours pas (.cattrk se coupe à l'ellipse), jamais la valeur (.cattrv, entière).
      //
      // title reste le minimum demandé par le brief ; aria-label en plus, pour qui
      // navigue au clavier ou au lecteur d'écran, jamais forcé à bouger une souris pour
      // retrouver la clé -- y compris quand elle s'est coupée à l'écran.
      if(c.attrs&&c.attrs.length){
        var ca=el("span","cattrs");
        c.attrs.forEach(function(a){
          var mk=el("span","cattr");
          var titre=a.cle+" : "+a.valeur;
          mk.appendChild(el("span","cattrk m",a.cle));
          mk.appendChild(el("span","cattrv m"," : "+a.valeur));
          mk.title=titre;
          mk.setAttribute("aria-label",titre);
          ca.appendChild(mk);
        });
        line.appendChild(ca);
      }
      ck.appendChild(line);
    });
    d.appendChild(ck);
  }

  /* 9. pièces jointes */
  t.docs.forEach(function(doc,i){
    var key=t.id+":"+i, open=!!S.docs[key];
    var box2=el("div","doc"+(open?" open":""));
    var bt=document.createElement("button");
    bt.appendChild(el("span","chev2","▸"));
    bt.appendChild(el("span",null,doc.k));
    bt.appendChild(el("span","size m",(Math.round(doc.text.length/100)/10)+"k"));
    bt.addEventListener("click",function(e){
      e.stopPropagation();S.docs[key]=!S.docs[key];render();
    });
    box2.appendChild(bt);
    var pre=document.createElement("pre");pre.className="m";pre.textContent=doc.text;
    box2.appendChild(pre);
    d.appendChild(box2);
  });
  return d;
}

// Une case sans rien à sa droite reprend la largeur libre (brief t-58, point 7) : le
// placement par rang et niveau ne change pas (voir render() ci-dessous, qui construit
// `rangs` avant d'appeler cette fonction) -- seule la LARGEUR d'un rang qui n'a
// personne à côté de lui sur sa ligne change. Le flex-wrap qui pose ces rangs côte à
// côte ne dit lui-même jamais où une ligne casse : il faut lire les positions RÉELLES
// une fois posées dans le DOM pour le savoir, d'où une mesure plutôt qu'un calcul a
// priori. Le dernier rang de chaque ligne (seul ou non) qui laisse de la place à sa
// droite reçoit la classe .etire, qui lève son plafond de largeur -- flex-grow, déjà
// posé sur .rang, fait alors tout le travail de redistribution ; un rang qui partage sa
// ligne à plein (aucune place restante) ne reçoit jamais cette classe, donc ne bouge
// pas (précaution 2 du brief).
function elargirRangsSansVoisin(rangsEl){
  var rangs=rangsEl.children;
  if(!rangs.length)return;
  var cadre=rangsEl.getBoundingClientRect();
  var i=0;
  while(i<rangs.length){
    var r=rangs[i].getBoundingClientRect(), top=r.top, j=i, droite=r.right;
    while(j+1<rangs.length){
      var rj=rangs[j+1].getBoundingClientRect();
      if(Math.round(rj.top)!==Math.round(top))break;
      j++;droite=rj.right;
    }
    // Seuil de 2px : couvre l'arrondi des sous-pixels d'un rang déjà pile à son plafond,
    // jamais une vraie place libre.
    if(cadre.right-droite>2)rangs[j].classList.add("etire");
    i=j+1;
  }
}

function render(){
  var board=document.getElementById("board");
  board.className="view-graphe";
  var wires=document.getElementById("wires");
  board.innerHTML="";
  if(wires)board.appendChild(wires);

  var vues=0, condense=null;
  D.phases.forEach(function(p){
    var all=p.order.map(function(i){return byId[i]}).filter(Boolean);
    var shown=all;
    // Compté ici, avant le repli condensé ci-dessous : une phase compacte ne construit
    // plus ses lignes, et ses tâches ne doivent pas faire croire à un plateau vide.
    vues+=shown.length;
    var faits=all.filter(function(t){return kind(t)==="done"}).length;
    var fini=all.length>0&&all.every(settled);
    var forced=S.closed[p.key];
    // Nouvelle règle par défaut, au PREMIER rendu, avant tout choix de l'humain (brief
    // t-58, point 6) : une phase ENTIÈREMENT finie ou annulée part fermée, toute autre
    // part OUVERTE -- y compris celle dont aucune tâche n'a encore démarré, qui dit
    // justement ce qui vient. `fini` sert déjà à la condensation ci-dessous, jamais
    // recalculé deux fois. S.closed[p.key] reste le seul forçage manuel : quand il est
    // défini, il décide SEUL (jamais `defautOuvert`), dans les deux sens — une phase
    // ouverte à la main ne se referme pas parce qu'elle finit, une phase fermée à la
    // main ne se rouvre pas parce qu'une tâche y démarre.
    var defautOuvert=!fini;
    var open=shown.length>0&&(forced===undefined?defautOuvert:!forced);

    // Une phase finie ET repliée n'a plus rien à dire qu'un numéro, un libellé et un
    // compte : plusieurs à la suite mangeaient toute la hauteur du mur en restant chacune
    // sur sa ligne. Elles se regroupent donc dans une même bande qui passe à la ligne
    // toute seule. Le clic la rouvre exactement comme le ferait son en-tête normal — même
    // ligne S.closed[p.key]=open — donc rouverte, sa présentation redevient celle du jour,
    // sans rien de différent.
    if(fini&&!open){
      if(!condense){condense=el("div","condensed");board.appendChild(condense)}
      var chip=el("div","pchip"+(all.length&&faits===all.length?" full":""));
      chip.appendChild(el("span","pkey m",p.key||"-"));
      chip.appendChild(el("span","cname",p.name));
      chip.appendChild(el("span","pcount m",faits+"/"+all.length));
      chip.addEventListener("click",function(){S.closed[p.key]=open;render()});
      condense.appendChild(chip);
      return;
    }
    condense=null;

    var sec=el("section","phase"+(open?"":" closed")+(all.length&&faits===all.length?" full":""));
    var head=el("div","phead");
    head.appendChild(el("span","pkey m",p.key||"-"));
    head.appendChild(el("span","pname",p.name));
    var right=el("span","pright");
    var track=el("span","ptrack"),fill=el("span","pfill");
    fill.style.width=(all.length?faits/all.length*100:0)+"%";
    fill.style.background=(all.length&&faits===all.length)?"#46a35a":"#3c7a4a";
    track.appendChild(fill);right.appendChild(track);
    right.appendChild(el("span","pcount m",
      p.planned?"annoncée":faits+"/"+all.length));
    right.appendChild(el("span","chev","▾"));
    head.appendChild(right);
    head.addEventListener("click",function(){S.closed[p.key]=open;render()});
    sec.appendChild(head);
    sec.appendChild(el("div","pwhy"+(p.why?"":" absent"),
      p.why||'sans explication : ordo group '+D.campaign.id+' '+(p.key||"")+' "'+p.name+'" --why "..."'));

    // Repli de tâche (t-44) : une phase encore ouverte (donc pas déjà condensée ci-dessus
    // par `fini&&!open`) sort ses tâches repliables de la grille de cases pour une liste
    // compacte en tête de phase -- le reste garde ses cases et ses liens (les arcs se
    // dessinent par data-row, où qu'il soit dans le DOM, voir draw()).
    var repliees=shown.filter(repliable);
    var reste=shown.filter(function(t){return !repliable(t)});
    if(repliees.length){
      // rowNode(t) telle quelle, sans paramètre ni branche ajoutés : une case réglée et
      // fermée y construit déjà en-tête et titre sans rien de plus (condition de t-24
      // inchangée). .repli (CSS) tasse ces deux blocs sur une seule ligne au lieu de deux
      // ; le titre, seul champ de longueur imprévisible, se coupe à l'ellipse plutôt que
      // de revenir à la ligne. Une case sélectionnée n'y reste que par sa position :
      // .repli ne stylant que .row:not(.sel), elle retrouve alors sa présentation entière.
      // Au-delà de cinq (t-56), construireListeRepli condense cette liste en une seule
      // ligne : le choix de l'humain se garde dans S -- même mécanisme que S.closed
      // pour le repli de phase (t-13), un objet qui survit aux rendus tant que la page
      // ne recharge pas, jamais réinitialisé par un changement d'état de tâche ni par
      // un battement du serveur.
      var listeRepli=construireListeRepli(repliees,S.repliOuvert[p.key],rowNode,
        function(v){S.repliOuvert[p.key]=v;render()});
      sec.appendChild(listeRepli);
    }

    var rangs=el("div","rangs");
    var parNiveau={};
    reste.forEach(function(t){(parNiveau[t.level]=parNiveau[t.level]||[]).push(t)});
    Object.keys(parNiveau).map(Number).sort(function(a,b){return a-b}).forEach(function(lv){
      var ts=parNiveau[lv];
      var tient=ts.some(function(t){return t.id===S.sel});
      var rang=el("div","rang"+(tient?" wide":""));
      var liste=el("div","rlist");
      ts.forEach(function(t){liste.appendChild(rowNode(t))});
      rang.appendChild(liste);rangs.appendChild(rang);
    });
    sec.appendChild(rangs);
    board.appendChild(sec);
    // Une case sans rien à sa droite reprend la largeur libre (brief t-58, point 7) :
    // mesuré seulement une fois `sec` attachée au document (rangs a sa géométrie réelle),
    // et seulement si la phase est ouverte -- une phase fermée (display:none) n'a pas de
    // géométrie exploitable, et rien n'y est visible de toute façon.
    if(open)elargirRangsSansVoisin(rangs);
  });
  if(!vues)board.appendChild(el("div","empty","aucune tâche dans ce chantier."));
  paintTop();
  paintFocus();
}

// Repeint le focus sur les lignes en place : la structure ne bouge pas, seules les classes
// et les arcs changent. C'est ce qui rend le survol gratuit et le clic fiable.
function paintFocus(){
  var focus=focusId(), rel=focus?related(focus):null;
  var lignes=document.querySelectorAll(".row");
  for(var i=0;i<lignes.length;i++){
    var r=lignes[i], id=r.getAttribute("data-row");
    r.classList.remove("focus","up","down","dim");
    if(!focus)continue;
    if(id===focus)r.classList.add("focus");
    else if(rel.up[id])r.classList.add("up");
    else if(rel.dn[id])r.classList.add("down");
    else r.classList.add("dim");
  }
  draw();
}

// Formate des minutes-Claude en "9h54"/"45m" (brief t-38), MÊME RÈGLE que _duree_min
// en Python : côté serveur pour chaque case, ici pour l'en-tête, parce que l'heure de
// fin qui l'accompagne dépend de "maintenant" et doit se calculer au rendu, côté
// navigateur (voir paintHeure() ci-dessous) -- il faut donc le restant BRUT jusqu'ici,
// pas seulement son texte déjà formé.
//
// --- t-38 : à partir d'ici, fonctions PURES (aucun accès au DOM, aucun Date.now()
// implicite), extraites telles quelles et exécutées sous Node par
// TestFinDeChantierSousNode -- une assertion de chaîne ne peut pas prouver un calcul de
// date, seule son exécution le peut. ---
function dureeMin(m){
  if(m<60)return m+"m";
  return Math.floor(m/60)+"h"+String(m%60).padStart(2,"0");
}

// Heure de fin et exposant de jour (brief t-38) : "now" est un PARAMÈTRE, jamais lu ici
// via `new Date()` -- c'est paintHeure() qui le fournit, au moment du rendu, ce qui rend
// cette fonction testable avec un "maintenant" figé. L'exposant compte les MINUITS
// franchis entre "now" et l'heure de fin, pas les tranches de 24h : 23h50 plus 20
// minutes tombe le lendemain (+1) après dix minutes seulement, pas après 24h.
function finChantier(now,restantMin){
  var fin=new Date(now.getTime()+restantMin*60000);
  var jourNow=new Date(now.getFullYear(),now.getMonth(),now.getDate());
  var jourFin=new Date(fin.getFullYear(),fin.getMonth(),fin.getDate());
  return {
    heure:fin.getHours()+"h"+String(fin.getMinutes()).padStart(2,"0"),
    jours:Math.round((jourFin-jourNow)/86400000),
  };
}

// Couleur de l'écart signé (retouche t-38) : le signe du TEXTE déjà formaté par
// _ecart_valeur_texte en Python porte le sens, jamais un second champ numérique --
// "-1h27" commence par "-", "+20m" par "+", "0m" par aucun des deux.
function classeEcart(valeur){
  if(valeur.charAt(0)==="-")return "gain";
  if(valeur.charAt(0)==="+")return "depasse";
  return "";
}

// Repeint le restant du chantier et son heure de fin (brief t-38), à GAUCHE du
// pourcentage. Recalculé à CHAQUE appel, jamais mis en cache : l'heure de fin dépend de
// "maintenant", qui avance même quand rien d'autre ne bouge dans le chantier -- une
// colonne ouverte depuis trois heures doit voir sa fin reculer de trois heures, pas la
// garder figée au dernier battement qui a changé quelque chose. D'où l'appel depuis
// paintTop() (à chaque redessin réel) ET depuis un minuteur dédié (voir bootstrap en bas
// de ce fichier), qui rafraîchit même quand le battement réseau ne change rien.
function paintHeure(){
  var rEl=document.getElementById("hrest"), fEl=document.getElementById("hfin");
  if(!rEl||!fEl)return;
  var min=D&&D.restantChantierMin;
  var connu=min!=null;
  rEl.hidden=!connu;fEl.hidden=!connu;
  var r=null;
  if(connu){
    rEl.textContent=dureeMin(min);
    r=finChantier(new Date(),min);
    fEl.textContent="";
    fEl.appendChild(document.createTextNode(r.heure));
    if(r.jours>0)fEl.appendChild(el("sup",null,"+"+r.jours));
  }
  annoncerOnglet(connu,connu?rEl.textContent:null,r);
}

// Résumé de la progression par phase (t-57) : même décompte que la boucle qui peint #segs
// plus bas, mis en fonction pour être transmis à l'onglet du mur sans dupliquer le calcul.
// Pure : ne lit que D/byId/kind, jamais le DOM.
function resumePhases(){
  return (D.phases||[]).map(function(p){
    var ts=p.order.map(function(i){return byId[i]}).filter(Boolean);
    var dn=ts.filter(function(t){return kind(t)==="done"}).length;
    return {key:p.key||"-", nom:p.name, dn:dn, total:ts.length};
  });
}

// Fait remonter à l'onglet du mur (t-49, complété t-57 de la progression et des alertes)
// les valeurs déjà peintes ci-dessus et par paintTop() : jamais recalculées côté mur,
// seulement transmises telles quelles. N'agit que depuis une colonne du mur (window.parent
// différent de window, voir le classList posé en tête de ce fichier) ; en page de fichier
// ou en panneau visité seul, l'appel ne fait rien.
function annoncerOnglet(connu,restant,r){
  if(window.parent===window)return;
  var pct=document.getElementById("pct");
  try{
    window.parent.postMessage({
      ordoOnglet: true,
      id: D&&D.campaign&&D.campaign.id,
      pct: pct?pct.textContent:"",
      connu: connu,
      restant: connu?restant:null,
      heure: connu&&r?r.heure:null,
      jours: connu&&r?r.jours:null,
      phases: resumePhases(),
      warn: (D&&D.warnings&&D.warnings.length)||0,
      ask: (D&&D.questions&&D.questions.length)||0,
    }, window.location.origin);
  }catch(e){}
}

function paintTop(){
  var vivantes=D.tasks.filter(function(t){return kind(t)!=="cancelled"}).length;
  var faits=D.tasks.filter(function(t){return kind(t)==="done"}).length;
  document.getElementById("pct").textContent=
    (vivantes?Math.round(faits/vivantes*100):0)+"%";
  paintHeure();
  var segs=document.getElementById("segs");segs.innerHTML="";
  resumePhases().forEach(function(p){
    var s=el("div","seg");
    s.style.flex=(p.total||1)+" 1 0";
    s.title=p.key+" "+p.nom+" — "+p.dn+"/"+p.total;
    var tr=el("div","track"),f=el("span","fill");
    f.style.width=(p.total?p.dn/p.total*100:0)+"%";
    f.style.background=(p.total&&p.dn===p.total)?"#46a35a":"#3c7a4a";
    tr.appendChild(f);s.appendChild(tr);
    s.appendChild(el("div","lab m",p.key));
    segs.appendChild(s);
  });
  var c=D.campaign;
  document.getElementById("cid").textContent=c.id;
  document.getElementById("ctx").textContent=
    [c.slug,c.state,c.tmuxSession].filter(Boolean).join(" · ");
  var wb=document.getElementById("warn"), wc=document.getElementById("warnchip"),
      nw=document.getElementById("n-warn");
  if(wb&&wc){
    wc.hidden=!D.warnings.length;
    if(D.warnings.length){
      if(nw)nw.textContent=D.warnings.length;
      wc.title=D.warnings.length+" alerte"+(D.warnings.length>1?"s":"")+", cliquer pour la liste";
      // Boîte identique à celle du calque des questions (askbox/askhead/askx) : même
      // mécanisme réutilisé, jamais un second calque écrit pour la même idée.
      wb.innerHTML="";
      var boiteW=el("div","askbox");
      var teteW=el("div","askhead");
      teteW.appendChild(el("span",null,"alertes"));
      teteW.appendChild(el("span","askmore",""+D.warnings.length));
      var xw=el("button","askx","×");
      xw.title="fermer";
      xw.addEventListener("click",function(){
        wb.hidden=true;try{sessionStorage.setItem(K("warn"),"1")}catch(e){}
      });
      teteW.appendChild(xw);
      boiteW.appendChild(teteW);
      var listeW=el("div","warnlist");
      D.warnings.forEach(function(w){listeW.appendChild(el("div",null,w.detail))});
      boiteW.appendChild(listeW);
      wb.appendChild(boiteW);
    }
    // Fermé par défaut, contrairement à l'ancien bandeau : un calque plein écran qui
    // s'ouvrirait tout seul au chargement couvrirait la page avant même qu'on ait pu la
    // lire. Il ne s'ouvre qu'au clic sur la pastille (voir plus bas) -- seule une
    // fermeture déjà faite par ce clic est mémorisée, jamais une ouverture.
    var cache=true;
    try{
      var m=sessionStorage.getItem(K("warn"));
      if(m!==null)cache=m==="1";
    }catch(e){}
    wb.hidden=!D.warnings.length||cache;
  }
  paintAsk();
  // Les trois raccourcis ne sont plus rappelés : ils se répétaient au bas de CHAQUE colonne
  // du mur, soit jusqu'à six fois à l'écran, pour une phrase qu'on lit une fois et qu'on
  // n'oublie plus. Le pied ne garde que ce qui varie d'un chantier à l'autre, et reste vide
  // quand il n'y a rien à signaler.
  var bouts=[];
  if(D.missingWhy.length)bouts.unshift(D.missingWhy.length+" sans explication");
  var annoncees=D.phases.filter(function(p){return p.planned&&p.key})
    .map(function(p){return p.key});
  if(annoncees.length)bouts.unshift("phases annoncées non découpées : "+annoncees.join(", "));
  document.getElementById("foot").textContent=bouts.join(" · ");
}

// Ce qui attend un choix de l'humain. Le calque se reconstruit SEULEMENT quand la question
// affichee change : le battement tombe toutes les trois secondes, et refaire les noeuds
// sous les doigts detacherait le bouton qu'on est en train de cliquer.
function paintAsk(){
  var box=document.getElementById("ask"), chip=document.getElementById("askchip");
  if(!box||!chip)return;
  var qs=(D&&D.questions)||[];
  chip.hidden=!qs.length;
  if(!qs.length){box.hidden=true;box.removeAttribute("data-q");return}
  var q=qs[0];
  chip.textContent=qs.length>1?qs.length+" choix à faire":"choix à faire";
  // Le masquage ne vaut que pour LA question masquee. Sans cette egalite, un seul clic de
  // fermeture rendrait sourd pour tout le reste du chantier, y compris aux questions
  // suivantes, qui sont justement celles qu'on n'a pas encore vues.
  var masque=null;
  try{masque=sessionStorage.getItem(K("ask"))}catch(e){}
  box.hidden=(masque===q.id);
  if(box.getAttribute("data-q")===q.id)return;
  box.setAttribute("data-q",q.id);
  box.innerHTML="";

  var carte2=el("div","askbox");
  var head=el("div","askhead");
  head.appendChild(el("span",null,"choix à faire"));
  head.appendChild(el("span","askmore",qs.length>1?"+"+(qs.length-1)+" autre"+
    (qs.length>2?"s":""):""));
  var x=el("button","askx","×");
  x.title="masquer jusqu'à la prochaine question";
  x.addEventListener("click",function(){
    box.hidden=true;
    try{sessionStorage.setItem(K("ask"),q.id)}catch(e){}
  });
  head.appendChild(x);
  carte2.appendChild(head);

  var qui=el("div","askwho"+(q.task?" link":""),
    q.id+(q.task?" · "+q.task+" "+q.taskTitle:" · toute la campagne"));
  if(q.task)qui.addEventListener("click",function(){box.hidden=true;select(q.task)});
  carte2.appendChild(qui);
  carte2.appendChild(el("div","asktext",q.text));

  if(q.options&&q.options.length){
    var opts=el("div","askopts");
    q.options.forEach(function(o){opts.appendChild(el("span","askopt",o))});
    carte2.appendChild(opts);
  }

  var cmd=el("div","askcmd");
  var code=document.createElement("code");code.className="m";code.textContent=q.answerCmd||"";
  cmd.appendChild(code);
  var cp=el("button",null,"copier");
  cp.addEventListener("click",function(){
    var fait=function(){cp.textContent="copié";setTimeout(function(){cp.textContent="copier"},1400)};
    // 127.0.0.1 est un contexte sûr, donc le presse-papier est joignable ; en file:// il
    // ne l'est pas toujours, d'où la sélection en repli plutôt qu'un bouton mort.
    if(navigator.clipboard&&navigator.clipboard.writeText)
      navigator.clipboard.writeText(code.textContent).then(fait,function(){selectionner(code)});
    else selectionner(code);
  });
  cmd.appendChild(cp);
  carte2.appendChild(cmd);
  carte2.appendChild(el("div","asktip",
    "la session orchestratrice attend votre réponse dans son terminal. "+
    "Cette commande referme la question ; elle ne la débloque pas à sa place."));
  box.appendChild(carte2);
}

function selectionner(noeud){
  try{
    var r=document.createRange();r.selectNodeContents(noeud);
    var s=window.getSelection();s.removeAllRanges();s.addRange(r);
  }catch(e){}
}

// Le graphe est en couches : un prerequis est toujours a un rang plus petit que sa
// dependante, donc chaque fleche part d'elle et sa pointe designe qui attend qui. Les
// coordonnees viennent du DOM parce que les rangs passent a la ligne selon la largeur.
function draw(){
  var svg=document.getElementById("wires"), board=document.getElementById("board");
  if(!svg||!board)return;
  var id=focusId();
  var base=board.getBoundingClientRect();
  // Une ligne dans une phase repliee existe toujours dans le DOM, mais son conteneur est
  // en display:none et son rectangle vaut zero partout. Sans ce controle, chaque arc qui
  // la vise part vers le coin haut-gauche de la page : verifie sur ce chantier, la carte
  // etait barree de diagonales qui ne voulaient rien dire.
  function rect(x){
    var e=board.querySelector('[data-row="'+x+'"]');
    if(!e)return null;
    var r=e.getBoundingClientRect();
    return (r.width||r.height)?r:null;
  }
  function arc(from,to,col,mk,w,op){
    var a=rect(from),b=rect(to);if(!a||!b)return "";
    var ax,ay,bx,by,c1x,c1y,c2x,c2y;
    if(b.left-a.right>-6){
      ax=a.right-base.left;ay=a.top-base.top+Math.min(20,a.height/2);
      bx=b.left-base.left-1;by=b.top-base.top+Math.min(20,b.height/2);
      var g=Math.max(12,(bx-ax)/2);c1x=ax+g;c1y=ay;c2x=bx-g;c2y=by;
    }else if(b.top-a.bottom>-4){
      ax=a.left-base.left+a.width/2;ay=a.bottom-base.top;
      bx=b.left-base.left+b.width/2;by=b.top-base.top-1;
      var g2=Math.max(10,(by-ay)/2);c1x=ax;c1y=ay+g2;c2x=bx;c2y=by-g2;
    }else{
      var droite=b.left>=a.left;
      ax=(droite?a.right-12:a.left+12)-base.left;
      bx=(droite?b.left+12:b.right-12)-base.left;
      ay=a.bottom-base.top;by=b.bottom-base.top+1;
      var v=Math.min(20,9+Math.abs(bx-ax)/12);
      c1x=ax+(droite?v:-v);c1y=ay+v;c2x=bx-(droite?v:-v);c2y=by+v;
    }
    return '<path d="M'+ax+','+ay+' C'+c1x+','+c1y+' '+c2x+','+c2y+' '+bx+','+by+
      '" fill="none" stroke="'+col+'" stroke-width="'+w+'" marker-end="url(#'+mk+')" opacity="'+op+'"/>';
  }
  var out='<defs>'+
    ['g:#5fa96f','b:#5aa2f0','d:#55606f'].map(function(x){
      var p=x.split(":");
      return '<marker id="ah-'+p[0]+'" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5"'+
        ' markerHeight="5" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="'+p[1]+'"/></marker>';
    }).join("")+'</defs>';
  // Une tâche repliée (t-44) n'a plus de case : un arc qui la vise partirait vers une
  // ligne de la liste compacte en tête de phase, jamais vers le même repère visuel que
  // les autres cases -- ce trait ne dit plus rien, il brouille juste le dessin. Aucun
  // arc ne se trace vers elle ni depuis elle, qu'il vienne du maillage ambiant ou du
  // focus d'une tâche voisine.
  function enCase(t){return !repliable(t)}
  D.tasks.forEach(function(t){
    if(!enCase(t))return;
    t.deps.forEach(function(p){
      if(id&&(p===id||t.id===id))return;
      var dp=byId[p];
      if(dp&&!enCase(dp))return;
      out+=arc(p,t.id,"#55606f","ah-d",1.2,id?0.12:0.8);
    });
  });
  var t=id?byId[id]:null;
  if(t&&enCase(t)){
    t.deps.forEach(function(p){
      var dp=byId[p];
      if(!dp||enCase(dp))out+=arc(p,id,"#5fa96f","ah-g",1.8,1);
    });
    t.dependants.forEach(function(c){
      var dc=byId[c];
      if(!dc||enCase(dc))out+=arc(id,c,"#5aa2f0","ah-b",1.8,1);
    });
  }
  svg.innerHTML=out;
  svg.setAttribute("height",board.scrollHeight);
}

// Bascule des calques #warn/#ask, factorisée (t-57) pour être appelée aussi bien par un
// clic local (page seule ou panneau visité hors mur) que par le message envoyé depuis
// l'onglet du mur, qui porte maintenant la pastille (voir plus bas et _MUR_JS) -- même
// calque, ouvert par le même geste, où qu'il soit déclenché.
var wb=document.getElementById("warn"),wc=document.getElementById("warnchip");
function basculerWarn(){
  if(!wb)return;
  wb.hidden=!wb.hidden;
  // Les deux calques occupent le même plein écran (voir #ask,#warn en CSS) : n'en montrer
  // qu'un à la fois, sinon celui posé en dernier dans le DOM (#ask) cacherait l'autre sans
  // que rien ne le dise.
  if(!wb.hidden){var askEl=document.getElementById("ask");if(askEl)askEl.hidden=true}
  try{sessionStorage.setItem(K("warn"),wb.hidden?"1":"0")}catch(e){}
}
if(wc)wc.addEventListener("click",basculerWarn);
if(wb){try{if(sessionStorage.getItem(K("warn"))==="1")wb.hidden=true}catch(e){}}

var ac=document.getElementById("askchip");
function basculerAsk(){
  var box=document.getElementById("ask");
  if(!box)return;
  box.hidden=!box.hidden;
  if(!box.hidden&&wb)wb.hidden=true;
  // Rouvrir efface le masquage, sinon le prochain battement le refermerait aussitot.
  try{
    if(box.hidden)sessionStorage.setItem(K("ask"),box.getAttribute("data-q")||"");
    else sessionStorage.removeItem(K("ask"));
  }catch(e){}
}
if(ac)ac.addEventListener("click",basculerAsk);

// Reçoit le clic porté par l'onglet du mur (t-57) sur sa pastille d'alerte : le calque vit
// dans CETTE fenêtre (le panneau), jamais dans le mur, qui ne peut donc que demander de
// l'ouvrir -- jamais le construire lui-même ni le dupliquer.
window.addEventListener("message",function(e){
  if(e.origin!==window.location.origin)return;
  var msg=e.data;
  if(!msg)return;
  if(msg.ordoOuvrir==="warn")basculerWarn();
  else if(msg.ordoOuvrir==="ask")basculerAsk();
});
window.addEventListener("keydown",function(e){if(e.key==="Escape"&&S.sel){S.sel=null;render()}});
window.addEventListener("resize",draw);
try{var last=sessionStorage.getItem(K("sel"));if(last)S.sel=last}catch(e){}
window.addEventListener("scroll",function(){
  try{sessionStorage.setItem(K("scroll"),window.scrollY)}catch(e){}});
// L'heure de fin (brief t-38) dépend de "maintenant" : sur une colonne du mur, le
// battement réseau ne redessine QUE si le chantier a changé (voir _PANNEAU_JS), donc
// sans ce minuteur dédié une colonne inactive pendant des heures garderait l'heure de
// fin figée à son dernier vrai changement. Trente secondes : largement assez pour un
// affichage qui ne porte que la minute, jamais la seconde.
setInterval(paintHeure,30000);
if(window.ORDO){
  window.ordoSetData(window.ORDO);
  window.ordoRestoreScroll();
}
})();
""".strip()


def _json(data: object) -> str:
    """JSON sûr a poser dans un <script> : "<" encode, donc "</script>" impossible.

    Le piege vaut d'etre nomme. Un prompt de tache contient du texte qu'un modele a ecrit ;
    qu'il y apparaisse "</script>" n'a rien d'improbable, et il suffirait a fermer la balise
    qui le porte, casser la page, et laisser tout ce qui suit etre interprete comme du
    balisage. json.dumps n'echappe pas "<" de lui-meme, d'ou la substitution explicite.
    """
    brut = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return brut.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def html(m: dict, interval: int = 0) -> str:
    """Page autonome : un seul fichier, aucune ressource externe, aucun serveur.

    Le rafraichissement passe par un meta refresh plutot que par du fetch : la page est
    ouverte en file://, ou un fetch de son propre voisin est refuse par le navigateur. La
    position de defilement et la tache selectionnee survivent au rechargement via
    sessionStorage, sinon chaque cycle ramenerait le lecteur en haut de page.

    Tout le contenu du chantier voyage en JSON et n'atteint le DOM que par textContent.
    Aucun titre, aucun prompt, aucune note de rapport n'est concatene dans du balisage : un
    modele ecrit ce qu'il veut dans ces champs, y compris du HTML.
    """
    c = m["campaign"]
    refresh = (
        f'<meta http-equiv="refresh" content="{int(interval)}">'
        if interval and interval > 0 else ""
    )

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}
<title>ordo {_e(c["id"])} {_e(c["slug"] or "")}</title>
<style>{_CSS}</style></head>
<body>
<div id="top">
  <h1><span class="cid m" id="cid"></span><span id="ctx"></span>
      <span id="hend"><span class="m" id="hrest" hidden></span><span class="m" id="hfin" hidden></span><span class="pct m" id="pct"></span></span></h1>
  <div id="segs"></div>
  <div id="bar">
    <span class="pill" id="warnchip" hidden><svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg><b class="m" id="n-warn"></b></span>
    <span class="pill" id="askchip" hidden></span>
  </div>
</div>
<div id="warn" hidden></div>
<div id="ask" hidden></div>
<div id="board"><svg id="wires"></svg></div>
{_LEGENDE}
<script>window.ORDO={_json(vue(m))};</script>
<script>{_JS}</script>
</body></html>
"""


# Intervalle de battement des pages servies, en secondes. Trois secondes est l'ordre de
# grandeur d'un changement d'etat reel ; plus court ferait travailler le serveur pour rien,
# puisque le redessin n'a lieu que si l'empreinte a bouge.
POLL_S = 3


# ---------------------------------------------------------------------------
# Le mur : une colonne par chantier, cote a cote, sur un seul ecran
# ---------------------------------------------------------------------------
#
# La page servie montrait UN chantier avec un menu pour en changer. Suivre deux projets
# demandait alors deux onglets et un aller-retour a chaque fois, ce qui est exactement le
# geste qu'un tableau de bord existe pour supprimer.
#
# Le mur est donc un CADRE, et rien d'autre : il ne porte aucune donnee de chantier. Chaque
# colonne est une page a part entiere, dans une iframe, qui va chercher son etat elle-meme
# et se redessine sur son propre rythme. Ce choix a une consequence qui vaut a lui seul le
# procede : une colonne qui se rafraichit ne touche pas les autres, donc la tache ouverte,
# le defilement et la recherche des colonnes voisines survivent.
#
# L'autre solution -- instancier N fois le rendu dans une seule page -- demandait de
# reecrire tout _JS, qui tient son etat en variables de module et vise le DOM par
# identifiants uniques. Beaucoup de code de dessin deja eprouve, casse pour une mise en
# page. La frontiere de document que donne l'iframe rend ce travail inutile.


_PANNEAU_CSS = """
/* Une colonne fait quelques centaines de pixels : tout ce qui est marge se resserre, et
   la barre du bas se coupe au lieu de passer sur trois lignes. */
#top{padding:9px 11px 8px}
#board{padding:11px 11px 70vh}
body{padding-bottom:30px}
#legend{padding:5px 11px;flex-wrap:nowrap;white-space:nowrap;overflow:hidden}
#foot{overflow:hidden;text-overflow:ellipsis}
#dead{border-color:#5a2f2f;color:#e05252}
#dead[hidden]{display:none}
#wait{padding:26px 12px;color:var(--dim2);font-size:11.5px}
""".strip()


_PANNEAU_JS = r"""
(function(){
var POLL=__POLL__, C=window.ORDO_CIBLE, empreinte=null, timer=null, premier=true;
// Deux pannes, deux messages. Le serveur qui ne repond plus et le chantier qu'il refuse
// de servir se ressemblent a l'ecran et ne se reparent pas pareil : dire "serveur muet"
// quand le serveur repond parfaitement envoie chercher la panne au mauvais endroit.
function muet(texte){
  var d=document.getElementById("dead");
  if(!d)return;
  d.hidden=!texte;
  if(texte)d.textContent=texte;
}
function charger(){
  fetch("/api/map?home="+encodeURIComponent(C.home)+
        "&campaign="+encodeURIComponent(C.campaign))
    .then(function(r){
      if(r.status===403||r.status===404)throw new Error("introuvable");
      if(!r.ok)throw new Error(r.status);
      return r.json();
    })
    .then(function(vue){
      muet("");
      // Redessine seulement si le contenu a change. L'empreinte ignore l'horodatage, sans
      // quoi chaque battement paraitrait different du precedent et la colonne se
      // reconstruirait toutes les trois secondes pour rien.
      if(vue.fingerprint===empreinte)return;
      empreinte=vue.fingerprint;
      var w=document.getElementById("wait");
      if(w&&w.parentNode)w.parentNode.removeChild(w);
      window.ordoSetData(vue);
      // Une seule fois, au premier rendu. Le mur qu'on rouvre doit se retrouver ou on
      // l'avait laisse ; restaurer a chaque battement, en revanche, remonterait la colonne
      // sous les doigts de qui est en train de la lire.
      if(premier){premier=false;window.ordoRestoreScroll()}
    })
    .catch(function(e){
      muet(String(e&&e.message)==="introuvable"
        ? "chantier introuvable" : "serveur muet");
    });
}
charger();
timer=setInterval(charger,POLL*1000);
// Une colonne cachee n'a personne pour la lire. La visibilite d'une iframe suit celle de
// l'onglet qui la porte : replier le mur arrete donc toutes les colonnes d'un coup.
document.addEventListener("visibilitychange",function(){
  if(document.hidden){clearInterval(timer);timer=null}
  else if(!timer){charger();timer=setInterval(charger,POLL*1000)}
});
})();
""".strip()


def panneau(home: str, campaign: str, poll: int = POLL_S) -> str:
    """Une colonne du mur : un chantier, servi par le serveur local.

    Ne porte aucune donnee : elle recoit une CIBLE et va lire /api/map elle-meme, en
    boucle. C'est ce qui permet au mur de la poser sans rien savoir du chantier, et a la
    colonne de se mettre a jour sans que le mur se recharge.

    Le selecteur de chantier n'est pas ici mais dans le mur, qui possede la mise en page :
    deux selecteurs pour une meme colonne se contrediraient des le premier changement.
    """
    js = _PANNEAU_JS.replace("__POLL__", str(max(1, int(poll))))
    # L'espace de nommage cloisonne sessionStorage, commun a toute l'origine donc a toutes
    # les colonnes. Sans lui, ouvrir une tache dans une colonne l'ouvrirait dans les
    # autres, et la derniere chargee ecraserait le defilement des precedentes.
    cible = _json({"home": home, "campaign": campaign})
    espace = _json(f"|{home}|{campaign}")
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ordo {_e(campaign)}</title>
<style>{_CSS}
{_PANNEAU_CSS}</style></head>
<body>
<div id="top">
  <h1><span class="cid m" id="cid"></span><span id="ctx"></span>
      <span id="hend"><span class="m" id="hrest" hidden></span><span class="m" id="hfin" hidden></span><span class="pct m" id="pct"></span></span></h1>
  <div id="segs"></div>
  <div id="bar">
    <span class="pill" id="warnchip" hidden><svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg><b class="m" id="n-warn"></b></span>
    <span class="pill" id="askchip" hidden></span>
    <span class="pill" id="dead" hidden></span>
  </div>
</div>
<div id="warn" hidden></div>
<div id="ask" hidden></div>
<div id="board"><svg id="wires"></svg><div id="wait">lecture du chantier...</div></div>
<div id="legend"><span id="foot"></span></div>
<script>window.ORDO_CIBLE={cible};window.ORDO_NS={espace};</script>
<script>{_JS}</script>
<script>{js}</script>
</body></html>
"""


_MUR_CSS = """
html,body{height:100%;overflow:hidden}
body{display:flex;flex-direction:column}
#wtop{flex:none;display:flex;align-items:center;gap:9px;height:34px;padding:0 10px;
background:var(--bg);border-bottom:1px solid var(--line)}
#mark{font-size:11px;font-weight:600;letter-spacing:.12em;color:var(--dim)}
#pulse{width:6px;height:6px;border-radius:50%;background:var(--done);flex:none;
transition:opacity .3s}
#pulse.stale{background:var(--blocked)}
#wtop .tag{font-size:10.5px;color:var(--dim2);border:1px solid var(--line);
border-radius:20px;padding:2px 8px;white-space:nowrap}
#wtop .tag.err{border-color:#5a2f2f;color:#e05252}
#wtop .tag.ask{border-color:#63541f;color:#e3b341;
animation:ordopulse 2.2s ease-in-out infinite}
#quota{display:flex;align-items:center;gap:12px;font-size:10.5px;color:var(--dim2)}
#quota[hidden]{display:none}
#quota .qwin{display:flex;align-items:center;gap:5px;white-space:nowrap}
#quota .qwin.estompe{opacity:.45}
#quota .qkey{font-weight:600;color:var(--dim)}
.col.ask{border-right-color:#63541f}
.col.ask .chead{background:#241a0c;border-bottom-color:#63541f}
/* Attend son orchestratrice : rien ne tourne mais quelque chose peut partir. C'est le
   signal le plus coûteux à manquer (vingt minutes perdues sur loko), donc le plus voyant :
   même famille que #dead (rouge d'alerte), pas celle de .ask (choix humain, pas relance). */
.col.wait{border-right-color:#5a2f2f}
.col.wait .chead{background:#241010;border-bottom-color:#5a2f2f}
/* Finie ou bloquée : rien ne tourne, rien n'est lançable. Une décision (fermer, ou nommer
   le blocage), pas une urgence : ton neutre, distinct du rouge de .wait et de l'ambre
   de .ask pour ne jamais se confondre avec eux. */
.col.stall{border-right-color:#3a4048}
.col.stall .chead{background:#1a1d22;border-bottom-color:#3a4048}
#lgd{display:flex;gap:10px;margin-left:auto;font-size:10px;color:var(--dim2)}
#lgd span.item{display:flex;align-items:center;gap:4px;white-space:nowrap}
#lgd .sw{width:7px;height:7px;border-radius:2px;display:inline-block}
/* Rond pour un modele, carre pour un etat : deux familles dans la meme legende, et
   la forme les separe sans ajouter un mot. Meme convention que la legende de la page
   autonome (#legend .sw.rond), pour qu'un aller-retour entre les deux ne surprenne pas. */
#lgd .sw.rond{border-radius:50%}
#lgd .lsep{width:1px;align-self:stretch;background:var(--line);flex:none}
#wtop button{flex:none}
#wtop button:disabled{opacity:.35;cursor:default}
/* Boutons d'action sans libellé (t-48) : le texte tenait large sur une barre qui doit
   rester sur une seule ligne à largeur d'écran normale. Le title porte le sens pour la
   souris et les lecteurs d'écran ; aria-label le répète, une icône seule n'étant pas un
   texte accessible par défaut. */
.icon-btn{border:1px solid var(--line);background:var(--panel);color:#9aa4b1;
border-radius:7px;display:inline-flex;align-items:center;justify-content:center;
padding:6px;cursor:pointer}
.icon-btn:hover{color:var(--txt);border-color:#3f4753}

#cols{flex:1 1 auto;min-height:0;display:flex;align-items:stretch;overflow-x:auto}
.col{display:flex;flex-direction:column;flex:1 1 0;min-width:340px;
border-right:1px solid var(--line)}
.chead{flex:none;display:flex;flex-direction:column;
background:var(--panel);border-bottom:1px solid var(--line2)}
/* align-self:stretch, même raison que .trow2 juste dessous : .chead est un flex
   column, où un enfant prend la largeur de son CONTENU. Mesuré : .trow occupait
   251px d'une colonne de 533, et l'onglet 211 de ces 251 -- il remplissait
   correctement un parent qui, lui, ne remplissait rien. */
.trow{display:flex;align-items:center;gap:5px;padding:5px 6px 1px;
align-self:stretch;width:100%}
.chead button{flex:none;background:transparent;border:1px solid var(--line);border-radius:6px;
color:var(--dim2);font:inherit;font-size:12px;line-height:1;padding:4px 7px;cursor:pointer}
.chead button:hover{color:var(--txt);border-color:#3f4753}
/* L'onglet (t-49) fusionne le select et la ligne identifiant/projet/état/session que
   portait le panneau juste en dessous : deux classes, donc plus spécifique que
   ".chead button" ci-dessus, qui l'emporterait sinon sur flex/background/padding sans
   égard à l'ordre des règles. */
.chead .tab{flex:1 1 auto;min-width:0;width:100%;display:flex;flex-direction:column;
align-items:stretch;gap:3px;cursor:pointer;
background:var(--row);border:1px solid var(--line);border-radius:6px;color:var(--txt);
font:inherit;font-size:11.5px;padding:3px 8px;cursor:pointer;text-align:left}
.chead .tab:hover{border-color:#3f4753}
.chead .tab:focus-visible{outline:2px solid #3f7fc4;outline-offset:1px;border-color:#3f7fc4}
.chead .tab[aria-expanded="true"]{border-color:#3f7fc4}
.tdot{width:7px;height:7px;border-radius:50%;flex:none}
/* Trois mots courts, jamais que la teinte (t-49, c3) : le mot voyage dans le title du
   bouton, lu au survol comme au clavier. Couleurs reprises à la palette existante, aucune
   inventée : done (vivant) pour ouvert, queued (déjà "en attente" dans la légende) pour en
   sommeil, cancelled pour fermé. */
.tdot.ouvert{background:var(--done)}
.tdot.sommeil{background:var(--queued)}
.tdot.ferme{background:var(--cancelled)}
.tnom{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
min-width:0;flex:1 1 auto}
/* Même partition qu'en tête de panneau (#hend, brief t-38, "en-tête étroit") : le nom cède
   la place en premier, le bloc pourcentage/restant/fin ne raccourcit jamais -- un chiffre
   tronqué dirait autre chose que ce qu'il mesure. C'est ce qui tient l'onglet sur une
   seule ligne même à 260px (c8). */
.tfin{margin-left:auto;flex:none;display:flex;align-items:baseline;gap:6px;min-width:0}
.trest{font-size:10.5px;color:var(--dim2)}
.teta{font-size:10.5px;font-weight:600;color:var(--txt2)}
.teta sup{font-size:8.5px;font-weight:600;color:var(--dim2);margin-left:1px}
.tpct{font-size:11.5px;font-weight:600;color:var(--txt)}

/* Seconde ligne de l'onglet (t-49, complétée t-57) : la progression par phase et les
   alertes qui vivaient dans #top du panneau, cerné de marges et sur sa propre ligne
   (voir annoncerOnglet() dans _JS). Reprend les teintes et les bords du panneau -- même
   vert de #segs .fill, même ambre que #warnchip/#askchip -- avec des tailles resserrées :
   une ligne fine, pas la maquette du panneau où la place ne manque pas. hidden porte sur
   la ligne entière : une colonne sans donnée (pas encore de message, ou chantier sans la
   moindre phase ni alerte) ne réserve aucune hauteur (voir peindreProgres()). */
/* align-self:stretch : .chead est un flex column, et un enfant y prend la largeur de
   son CONTENU, pas celle offerte. Mesuré sur une colonne de 533px : .trow2 n'en
   occupait que 102, la barre de progression 58, soit un cinquième de la place
   disponible pour la seule chose que cette ligne a à montrer. */
/* .tline porte l'ancienne disposition horizontale de l'onglet (pastille, nom, chiffres),
   qui vivait sur .tab avant que celui-ci ne devienne une colonne de deux lignes. */
.tline{display:flex;align-items:center;gap:6px;min-width:0;width:100%}
.trow2{display:flex;align-items:center;gap:6px;padding:0;min-width:0;
align-self:stretch;width:100%}
.trow2[hidden]{display:none}
.tsegs{display:flex;gap:4px;flex:1 1 auto;min-width:0;align-items:center}
/* gap 2px et min-width 3px, mesurés : un chantier à 12 phases saturait la barre
   (12 x 8px + 11 x 3px = 129px = toute la largeur), si bien qu'aucun segment ne
   pouvait grandir et que le flex-grow proportionnel au nombre de tâches ne servait
   à rien. Le minimum garde une phase d'une tâche visible, il ne doit pas décider de
   la largeur de toutes les autres. */
.tsegs .seg{min-width:3px;flex:1 1 0}
/* display:block sur .track, et .seg qui n'est plus un simple flex-item : les deux sont des
   <span>, donc inline par défaut, et un élément inline IGNORE height -- la barre existait,
   large de 129px, haute de 0. Elle se voyait dans le DOM et les tests, jamais à l'écran.
   #segs (le même dessin dans le panneau, avant t-57) n'avait pas le problème parce que son
   parent lui donnait sa hauteur ; la structure a été reprise dans l'onglet sans cette
   condition-là. Mesuré au navigateur : 0px avant, 4px après. */
.tsegs .seg{display:block}
/* Le numéro DANS la barre, pas dessous : sous la barre il coûtait une ligne de hauteur
   pour une information qui tient dans un creux qu'on épaissit. La barre passe donc de 4 à
   13px et devient le support du numéro, la couleur restant portée par le remplissage.
   #07090c au lieu de var(--bg) #0b0d10 : sur le fond de bandeau (--panel #12151a) un creux
   à --bg se lisait encore comme un relief, pas comme un vide. */
.tsegs .track{position:relative;display:block;height:13px;border-radius:4px;
background:#07090c;overflow:hidden;box-shadow:inset 0 0 0 1px #191e25}
/* --txt2 et non --dim2 : le numéro se lit par-dessus DEUX fonds, le creux sombre et le
   remplissage vert, et un gris moyen disparaissait sur le vert. L'ombre portée le décolle
   des deux sans l'alourdir. */
.tsegs .lab{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
font-size:9px;font-weight:600;line-height:1;color:var(--txt2);white-space:nowrap;
pointer-events:none;text-shadow:0 1px 2px rgba(0,0,0,.75)}
.tsegs .fill{display:block;height:100%;border-radius:4px}
/* .chead .tchip (deux classes) l'emporte sur ".chead button" (classe+élément) ci-dessus --
   même mécanique de spécificité que .chead .tab plus haut, sans !important. */
.chead .tchip{display:inline-flex;align-items:center;border:1px solid #63541f;
background:var(--panel);color:#e3b341;border-radius:20px;font:inherit;font-size:9.5px;
line-height:1.3;padding:1px 6px;cursor:pointer;flex:none}
.chead .tchip:hover{border-color:#7a6526}
.chead .tchip[hidden]{display:none}

/* Popover du sélecteur : posé dans <body> (voir creer()), jamais dans .col, pour échapper
   au clip vertical que #cols impose à tout ce qu'il contient. */
.tablist{position:fixed;z-index:50;background:var(--panel);border:1px solid var(--line);
border-radius:8px;box-shadow:0 14px 40px rgba(0,0,0,.55);padding:4px;margin:0;
list-style:none;max-height:60vh;overflow:auto}
.tablist .opt-group{padding:6px 8px 3px;font-size:9.5px;letter-spacing:.08em;
text-transform:uppercase;color:var(--dim3)}
/* Option du popover (t-56) : même structure que l'onglet fermé qu'elle produira une
   fois choisie -- dot, nom, pourcentage, restant, ETA et la seconde ligne de
   progression (voir peindreOption() dans _MUR_JS). Colonne, jamais plus une seule
   ligne de texte tronquée : deux rangées empilées, une par ligne de l'onglet. */
.tablist .opt{padding:5px 8px;border-radius:5px;font-size:11.5px;color:var(--txt2);
cursor:pointer;display:flex;flex-direction:column;gap:2px;min-width:0}
.tablist .opt .orow1{display:flex;align-items:center;gap:6px;min-width:0}
.tablist .opt .trow2{padding:0;min-width:0}
/* .tchip n'a de règle visible que scopée à .chead (voir plus haut) : reprise ici sans
   le curseur -- cliquer une option choisit le chantier entier, jamais sa seule alerte
   -- avec son propre [hidden], le même piège déjà évité pour #warnchip et .chead
   .tchip : un attribut hidden posé par peindreProgres() ne masquerait rien sans lui,
   la règle plus spécifique (deux classes) l'emportant sur celle du navigateur. */
.tablist .opt .tchip{display:inline-flex;align-items:center;border:1px solid #63541f;
background:var(--panel);color:#e3b341;border-radius:20px;font:inherit;font-size:9.5px;
line-height:1.3;padding:1px 6px;flex:none}
.tablist .opt .tchip[hidden]{display:none}
.tablist .opt[aria-selected="true"]{color:var(--txt);font-weight:600}
.tablist .opt.hl{background:var(--rowsel)}
.tablist .opt-sep{height:1px;background:var(--line);margin:4px 2px}
.col iframe{flex:1 1 auto;width:100%;border:0;background:var(--bg);display:block}
#vide{margin:auto;padding:40px;color:var(--dim2);font-size:12px;text-align:center;
text-wrap:pretty}
#vide[hidden]{display:none}
""".strip()


_MUR_JS = r"""
(function(){
var POLL=__POLL__, CLE="ordo-mur", MAX=8;
var cols=[], seq=0, campagnes=[], monte=false, timer=null;

function pulse(vivant){
  var p=document.getElementById("pulse");
  p.classList.toggle("stale",!vivant);
  p.style.opacity=.35;setTimeout(function(){p.style.opacity=1},260);
}
function cle(c){return c.home+" "+c.campaign}
function cleDe(c){return c.home+" "+c.id}
function trouver(k){
  for(var i=0;i<campagnes.length;i++)if(cleDe(campagnes[i])===k)return campagnes[i];
  return null;
}
// Trois situations, jamais confondues. Une exécutante vit => aucun signal (le compteur
// "en cours" suffit déjà). Aucune exécutante mais quelque chose est lançable => la
// campagne attend son orchestratrice, c'est le cas qui a coûté vingt minutes à l'humain,
// il doit se voir le plus. Aucune exécutante et rien de lançable => la campagne est finie
// ou bloquée, elle mérite d'être fermée ou son blocage nommé. Un "asking" en cours
// explique déjà l'immobilité par une autre voie (une réponse humaine pendante) : les deux
// signaux ne se cumulent jamais, sinon la colonne mentirait par excès. Une campagne sans
// aucune tâche (total=0, juste créée) n'a encore rien à signaler. "ouvert" est l'état
// francophone qu'une version antérieure écrivait encore (voir _CHANTIER_OUVERT côté
// Python) : les deux campagnes réelles citées par le mandat (lavuln c-03, loko c-02) le
// portent encore, un signal qui ne le reconnaîtrait pas resterait aveugle exactement là où
// on lui demande de voir.
function ouvert(c){return c.state==="open"||c.state==="ouvert"}
function signal(c){
  if(!ouvert(c)||c.asking||c.running||!c.total)return null;
  return c.ready?"wait":"stall";
}
function libelle(c){
  var sig=signal(c);
  return (c.slug||c.id)+" · "+c.done+"/"+c.total+
    (c.asking?" · CHOIX A FAIRE":"")+
    (c.running?" · "+c.running+" en cours":"")+
    (sig==="wait"?" · ATTEND ORCHESTRATRICE":"")+
    (sig==="stall"?" · TERMINEE OU BLOQUEE":"")+
    (c.state==="open"?"":" · "+c.state);
}
// Deux groupes pour le sélecteur de l'onglet (t-49, c3), jamais mélangés : ce qui a
// quelque chose EN COURS (une exécutante vivante ou une tâche lançable) d'abord, le reste
// -- terminé, en sommeil ou fermé -- ensuite. Une campagne fermée n'a jamais running ni
// ready non nuls (ses tâches ne tournent ni ne se lancent plus), donc ouvert(c) est une
// garde et non une redondance.
function enCours(c){return ouvert(c)&&!!((c.running||0)+(c.ready||0))}
function grouper(liste){
  return {
    enCours: liste.filter(enCours),
    autres: liste.filter(function(c){return !enCours(c)}),
  };
}
// La disposition survit au rechargement : un mur qu'il faut remonter à chaque ouverture
// n'est pas un écran dédié, c'est un formulaire.
function lire(){
  try{
    var d=JSON.parse(localStorage.getItem(CLE));
    if(d&&d.cols&&d.cols.length)
      return d.cols.filter(function(c){return c&&c.home&&c.campaign});
  }catch(e){}
  return null;
}
function ecrire(){
  try{localStorage.setItem(CLE,JSON.stringify({v:1,cols:cols.map(function(c){
    return {home:c.home,campaign:c.campaign}})}))}catch(e){}
}

// L'onglet fusionne (t-49) ce que deux bandeaux répétaient : le sélecteur de session et la
// ligne identifiant/projet/état/session du panneau. Il ne porte plus que le nom du projet
// (une fois), une pastille d'état et les trois valeurs du panneau (pourcentage, restant,
// heure de fin) reçues par message -- jamais recalculées ici. Un seul col a son popover
// ouvert à la fois (listeActive).
var listeActive=null;

// Repeint l'étiquette : nom, pastille d'état, et le dernier message reçu du panneau (voir
// annoncerOnglet() dans _JS). Toujours sûr d'être appelé, y compris popover ouvert : ces
// nœuds sont hors du <ul>, les reconstruire ne touche jamais le clavier qui y navigue.
function peindreTab(col){
  var etat=trouver(cle(col));
  col.nom.textContent=etat?(etat.slug||etat.id):(col.campaign+" — introuvable");
  var mot="ouvert",cls="ouvert";
  if(etat){
    if(!ouvert(etat)){mot="fermé";cls="ferme"}
    else if(signal(etat)==="stall"){mot="en sommeil";cls="sommeil"}
  }else{
    mot="introuvable";cls="ferme";
  }
  col.dot.className="tdot "+cls;
  col.tab.title=etat?mot+(etat.tmuxSession?" · "+etat.tmuxSession:""):"chantier introuvable";
  var td=col.tabData;
  col.tpct.textContent=td?td.pct:"";
  var connu=!!(td&&td.connu);
  col.trest.hidden=!connu;col.teta.hidden=!connu;
  col.trest.textContent=connu?td.restant:"";
  col.teta.textContent="";
  if(connu){
    col.teta.appendChild(document.createTextNode("ETA "+td.heure));
    if(td.jours>0){
      var sup=document.createElement("sup");sup.textContent="+"+td.jours;
      col.teta.appendChild(sup);
    }
  }
  peindreProgres(col,td);
}

// Seconde ligne de l'onglet (t-57) : progression par phase + alertes, reçues du panneau
// (annoncerOnglet() dans _JS), jamais recalculées ici -- même principe que pct/restant/eta
// ci-dessus. Une colonne sans donnée (pas encore de message, ou chantier sans la moindre
// phase ni alerte) ne réserve aucune hauteur pour cette ligne : le hidden porte sur
// trow2 lui-même, jamais un contenu vide affiché pour rien.
function peindreProgres(col,td){
  var phases=(td&&td.phases)||[];
  var warn=(td&&td.warn)||0, ask=(td&&td.ask)||0;
  col.trow2.hidden=!(phases.length||warn||ask);
  while(col.tsegs.firstChild)col.tsegs.removeChild(col.tsegs.firstChild);
  phases.forEach(function(p){
    var s=document.createElement("span");s.className="seg";
    s.style.flex=(p.total||1)+" 1 0";
    s.title=p.key+" "+(p.nom||"")+" — "+p.dn+"/"+p.total;
    var tr=document.createElement("span");tr.className="track";
    var f=document.createElement("span");f.className="fill";
    f.style.width=(p.total?p.dn/p.total*100:0)+"%";
    f.style.background=(p.total&&p.dn===p.total)?"#46a35a":"#3c7a4a";
    // Le numéro DANS le creux, superposé au remplissage : sans lui, douze barres se
    // ressemblent toutes et le survol reste le seul moyen de savoir laquelle avance.
    var lb=document.createElement("span");lb.className="lab";lb.textContent=p.key;
    tr.appendChild(f);tr.appendChild(lb);s.appendChild(tr);
    col.tsegs.appendChild(s);
  });
  col.twarn.hidden=!warn;
  if(warn){
    col.twarn.textContent=warn;
    col.twarn.title=warn+" alerte"+(warn>1?"s":"")+", cliquer pour la liste";
  }
  col.tdemande.hidden=!ask;
  if(ask){
    col.tdemande.textContent=ask>1?ask+" choix":"choix à faire";
    col.tdemande.title=ask>1?ask+" choix à faire":"choix à faire";
  }
}

// Le tabData d'une campagne du popover (t-56) : jamais recalculé ici, seulement
// retrouvé si une colonne DÉJÀ OUVERTE sur ce mur affiche justement ce chantier -- sa
// propre iframe l'a alors déjà transmis par annoncerOnglet() (voir peindreProgres()).
// Un chantier que ce mur n'a ouvert nulle part n'a "rien d'estimé" (DEUX LIMITES du
// brief) : ni pct, ni restant, ni ETA, ni phases -- peindreOption() ci-dessous ne
// promet jamais plus que ce que trouve cette recherche.
function tabDataDe(c){
  for(var i=0;i<cols.length;i++)if(cle(cols[i])===cleDe(c))return cols[i].tabData||null;
  return null;
}

// Peint une option du popover avec la structure de l'onglet fermé qu'elle produira une
// fois choisie (t-56) -- dot, nom, pourcentage, restant, ETA, et la seconde ligne de
// progression par phase + alertes (t-57). Les classes terminales (tdot, tnom, tfin,
// trest, teta, tpct, tsegs) ne sont PAS scopées à .chead : les reprendre ici donne
// exactement les mêmes teintes et les mêmes espacements sans dupliquer une seule règle
// CSS (voir .tablist .opt dans _CSS). Le bloc pct/restant/ETA reprend telle quelle la
// condition de peindreTab() -- td absent ou non connu masque restant/ETA, jamais un
// nouveau calcul qui pourrait diverger de ce que l'onglet montrera une fois ouvert. La
// seconde ligne réutilise peindreProgres() sans y toucher : mêmes segments, mêmes
// pastilles, sur un sac de nœuds indépendant de toute colonne réelle. Jamais
// interactive au-delà du clic global posé sur <li> par bloc() ci-dessous : les
// pastilles y sont des <span>, pas des <button> -- choisir une option choisit le
// chantier entier, jamais la seule alerte d'un autre chantier que celui-ci.
function peindreOption(li,c){
  var row1=document.createElement("div");row1.className="orow1";
  var dot=document.createElement("span");
  dot.className="tdot "+(!ouvert(c)?"ferme":(signal(c)==="stall"?"sommeil":"ouvert"));
  var nom=document.createElement("span");nom.className="tnom";nom.textContent=c.slug||c.id;
  var fin=document.createElement("span");fin.className="tfin";
  var trest=document.createElement("span");trest.className="trest";trest.hidden=true;
  var teta=document.createElement("span");teta.className="teta";teta.hidden=true;
  var tpct=document.createElement("span");tpct.className="tpct";
  fin.appendChild(trest);fin.appendChild(teta);fin.appendChild(tpct);
  row1.appendChild(dot);row1.appendChild(nom);row1.appendChild(fin);
  li.appendChild(row1);

  var td=tabDataDe(c);
  tpct.textContent=td?td.pct:"";
  var connu=!!(td&&td.connu);
  trest.hidden=!connu;teta.hidden=!connu;
  trest.textContent=connu?td.restant:"";
  if(connu){
    teta.appendChild(document.createTextNode("ETA "+td.heure));
    // el() n'existe pas dans ce script (_MUR_JS) : jamais importé ici, c'est un
    // helper du panneau (_JS). Même construction manuelle que peindreTab() juste
    // au-dessus pour ce même sup, sans quoi cette ligne lève une ReferenceError dès
    // qu'un chantier estimé dépasse minuit -- trouvé en écrivant le test Node de
    // TestOptionsDuPopoverAuStyleDeLOnglet, jamais exécuté avant lui.
    // jours > 0, comme partout ailleurs (peindreTab et le panneau) : sans cette garde
    // l'option affichait "+0" sur tout chantier qui finit dans la journée, c'est-à-dire
    // le cas courant, pour une mention qui n'existe que quand l'ETA franchit minuit.
    if(td.jours>0){
      var sup=document.createElement("sup");sup.textContent="+"+td.jours;
      teta.appendChild(sup);
    }
  }

  var trow2=document.createElement("div");trow2.className="trow2";
  var tsegs=document.createElement("span");tsegs.className="tsegs";
  var twarn=document.createElement("span");twarn.className="tchip";twarn.hidden=true;
  var tdemande=document.createElement("span");tdemande.className="tchip";tdemande.hidden=true;
  trow2.appendChild(tsegs);trow2.appendChild(twarn);trow2.appendChild(tdemande);
  li.appendChild(trow2);
  peindreProgres({trow2:trow2,tsegs:tsegs,twarn:twarn,tdemande:tdemande},td);
}

// Construit les options du popover : deux groupes (voir grouper()), toujours dans cet
// ordre, séparés par une ligne. Ne reconstruit jamais pendant que le clavier y navigue
// (col.listeOuverte) -- même piège que l'ancien select reconstruit sous le focus. La
// signature (t-56) porte aussi le tabData des colonnes ouvertes : sans lui, une option
// qui reprend le pct/restant/ETA d'une autre colonne resterait figée à leur valeur du
// jour de l'ouverture du popover, jusqu'au prochain changement de libellé -- jamais
// jusqu'au prochain battement qui fait pourtant progresser ce même chiffre à l'onglet.
function optionsListe(col){
  if(col.listeOuverte)return;
  var sig=campagnes.map(function(c){return cleDe(c)+"="+libelle(c)}).join("|")+"#"+cle(col)
    +"~"+cols.map(function(c){return cle(c)+":"+(c.tabData?JSON.stringify(c.tabData):"")})
      .join(",");
  if(col.optSig===sig)return;
  col.optSig=sig;
  var ul=col.liste;
  while(ul.firstChild)ul.removeChild(ul.firstChild);
  col.optionsId=[];col.optionsMap={};
  function bloc(titre,liste){
    var lbl=document.createElement("li");
    lbl.className="opt-group";lbl.setAttribute("role","presentation");
    lbl.setAttribute("aria-hidden","true");lbl.textContent=titre;
    ul.appendChild(lbl);
    liste.forEach(function(c){
      var li=document.createElement("li");
      li.id="opt-"+col.uid+"-"+col.optionsId.length;
      li.setAttribute("role","option");li.className="opt";
      li.setAttribute("aria-label",libelle(c));
      li.setAttribute("aria-selected",cle(col)===cleDe(c)?"true":"false");
      peindreOption(li,c);
      li.addEventListener("click",function(){choisir(col,c)});
      li.addEventListener("mouseenter",function(){surligner(col,li.id)});
      ul.appendChild(li);
      col.optionsId.push(li.id);col.optionsMap[li.id]=c;
    });
  }
  var g=grouper(campagnes);
  bloc("en cours",g.enCours);
  var sep=document.createElement("li");
  sep.setAttribute("role","separator");sep.setAttribute("aria-hidden","true");
  sep.className="opt-sep";
  ul.appendChild(sep);
  bloc("autres",g.autres);
}

function surligner(col,id){
  var prev=col.liste.querySelector(".opt.hl");
  if(prev)prev.classList.remove("hl");
  var next=document.getElementById(id);
  if(!next)return;
  next.classList.add("hl");
  col.tab.setAttribute("aria-activedescendant",id);
  next.scrollIntoView({block:"nearest"});
}

function deplacer(col,delta){
  var ids=col.optionsId;
  if(!ids||!ids.length)return;
  var i=ids.indexOf(col.tab.getAttribute("aria-activedescendant"));
  i=i<0?0:Math.max(0,Math.min(ids.length-1,i+delta));
  surligner(col,ids[i]);
}

function fermerListe(col){
  if(!col.listeOuverte)return;
  col.listeOuverte=false;col.liste.hidden=true;
  col.tab.setAttribute("aria-expanded","false");
  col.tab.removeAttribute("aria-activedescendant");
  if(listeActive===col)listeActive=null;
}

function ouvrirListe(col){
  if(listeActive&&listeActive!==col)fermerListe(listeActive);
  optionsListe(col);
  if(!col.optionsId.length)return;
  col.listeOuverte=true;listeActive=col;
  var r=col.tab.getBoundingClientRect();
  col.liste.style.left=r.left+"px";
  col.liste.style.top=(r.bottom+4)+"px";
  col.liste.style.minWidth=r.width+"px";
  col.liste.hidden=false;
  col.tab.setAttribute("aria-expanded","true");
  // Reprend la navigation sur l'option déjà choisie, jamais en la renvoyant tout en haut.
  var courant=col.liste.querySelector('[aria-selected="true"]')||col.liste.querySelector(".opt");
  if(courant)surligner(col,courant.id);
}

function choisir(col,c){
  col.home=c.home;col.campaign=c.id;
  ecrire();
  fermerListe(col);
  dessiner();
  col.tab.focus();
}

// Ouverture au clavier ET au doigt (t-49, c7) : Entrée/Espace/flèches ouvrent, les flèches
// déplacent l'option surlignée, Entrée/Espace choisit, Échap referme -- le focus ne quitte
// jamais le bouton, aria-activedescendant porte seul le déplacement dans la liste.
function onTabKeydown(e,col){
  var k=e.key;
  if(!col.listeOuverte){
    if(k==="ArrowDown"||k==="ArrowUp"||k==="Enter"||k===" "){
      e.preventDefault();ouvrirListe(col);
    }
    return;
  }
  if(k==="ArrowDown"){e.preventDefault();deplacer(col,1)}
  else if(k==="ArrowUp"){e.preventDefault();deplacer(col,-1)}
  else if(k==="Escape"){e.preventDefault();fermerListe(col)}
  else if(k==="Enter"||k===" "){
    e.preventDefault();
    var id=col.tab.getAttribute("aria-activedescendant");
    var c=id&&col.optionsMap[id];
    if(c)choisir(col,c);
  }
}

function creer(col){
  var d=document.createElement("div");
  d.className="col";d.setAttribute("data-uid",col.uid);
  var h=document.createElement("div");h.className="chead";
  var row1=document.createElement("div");row1.className="trow";

  // role=button plutôt que <button> : cet onglet porte désormais les chips d'alerte, qui
  // sont eux-mêmes des boutons, et le HTML interdit un bouton dans un bouton -- le clic sur
  // l'enfant ne partirait jamais. Le clavier est rendu à la main (Enter/Espace ci-dessous),
  // ce qu'un <button> offrait gratuitement : c'est le prix de l'imbrication.
  var tab=document.createElement("div");
  tab.className="tab";tab.tabIndex=0;
  tab.setAttribute("role","button");
  tab.setAttribute("aria-haspopup","listbox");
  tab.setAttribute("aria-expanded","false");
  tab.setAttribute("aria-controls","tablist-"+col.uid);
  var dot=document.createElement("span");dot.className="tdot";
  var nom=document.createElement("span");nom.className="tnom";
  var fin=document.createElement("span");fin.className="tfin";
  var trest=document.createElement("span");trest.className="trest";trest.hidden=true;
  var teta=document.createElement("span");teta.className="teta";teta.hidden=true;
  var tpct=document.createElement("span");tpct.className="tpct";
  fin.appendChild(trest);fin.appendChild(teta);fin.appendChild(tpct);
  // Ligne 1 dans un porteur propre : l'onglet devient une colonne de deux lignes, et sans
  // ce porteur dot/nom/fin s'empileraient verticalement avec la progression.
  var tline=document.createElement("span");tline.className="tline";
  tline.appendChild(dot);tline.appendChild(nom);tline.appendChild(fin);
  tab.appendChild(tline);
  tab.addEventListener("click",function(e){
    // Un clic parti d'un chip d'alerte ouvre son calque, jamais la liste des chantiers.
    if(e.target.closest(".tchip"))return;
    if(col.listeOuverte)fermerListe(col);else ouvrirListe(col);
  });
  tab.addEventListener("keydown",function(e){
    if(e.key===" "||e.key==="Enter"){
      if(e.target.closest(".tchip"))return;
      e.preventDefault();
      if(col.listeOuverte)fermerListe(col);else ouvrirListe(col);
    }
  });
  tab.addEventListener("keydown",function(e){onTabKeydown(e,col)});

  var x=document.createElement("button");
  x.type="button";x.textContent="×";x.title="fermer la colonne";
  x.setAttribute("aria-label","fermer la colonne");
  x.addEventListener("click",function(e){
    e.stopPropagation();
    fermerListe(col);
    if(col.liste&&col.liste.parentNode)col.liste.parentNode.removeChild(col.liste);
    cols=cols.filter(function(c){return c!==col});ecrire();dessiner();
  });
  row1.appendChild(tab);row1.appendChild(x);

  // Seconde ligne (t-57) : progression par phase + alertes, reçues du panneau par message
  // (voir peindreProgres()). Masquée par défaut -- une colonne fraîchement créée n'a encore
  // rien reçu -- et reconstruite à chaque message, jamais construite ici une fois pour
  // toutes : le nombre de phases varie d'un chantier à l'autre.
  var row2=document.createElement("div");row2.className="trow2";row2.hidden=true;
  var tsegs=document.createElement("span");tsegs.className="tsegs";
  var twarn=document.createElement("button");
  twarn.type="button";twarn.className="tchip";twarn.hidden=true;
  twarn.addEventListener("click",function(){envoyerAuPanneau(col,"warn")});
  var tdemande=document.createElement("button");
  tdemande.type="button";tdemande.className="tchip";tdemande.hidden=true;
  tdemande.addEventListener("click",function(){envoyerAuPanneau(col,"ask")});
  row2.appendChild(tsegs);row2.appendChild(twarn);row2.appendChild(tdemande);

  // row2 DANS l'onglet, plus sous lui : le bandeau ne montre plus deux blocs separes
  // (selecteur puis progression) mais un seul objet qui porte tout l'etat du chantier.
  tab.appendChild(row2);
  h.appendChild(row1);

  // Le popover vit hors de la colonne, dans <body> : #cols défile horizontalement
  // (overflow-x:auto), qui force aussi le clip vertical -- une liste posée dedans se
  // couperait dès qu'elle dépasse la hauteur visible de la colonne.
  var ul=document.createElement("ul");
  ul.id="tablist-"+col.uid;ul.className="tablist";ul.hidden=true;
  ul.setAttribute("role","listbox");
  ul.setAttribute("aria-label","chantier de la colonne");
  document.body.appendChild(ul);

  var f=document.createElement("iframe");
  f.setAttribute("title","carte du chantier");
  d.appendChild(h);d.appendChild(f);
  col.node=d;col.tab=tab;col.dot=dot;col.nom=nom;col.trest=trest;col.teta=teta;col.tpct=tpct;
  col.trow2=row2;col.tsegs=tsegs;col.twarn=twarn;col.tdemande=tdemande;
  col.liste=ul;col.frame=f;col.listeOuverte=false;col.optionsId=[];col.optionsMap={};
  return d;
}

// Relaie vers le panneau (l'iframe) le clic sur une pastille d'alerte de l'onglet : le
// calque #warn/#ask vit dans CETTE fenêtre-là, jamais dans le mur (voir _JS), donc le mur
// ne fait que demander de l'ouvrir plutôt que de le dupliquer.
function envoyerAuPanneau(col,quoi){
  if(!col.frame||!col.frame.contentWindow)return;
  try{col.frame.contentWindow.postMessage({ordoOuvrir:quoi},window.location.origin)}catch(e){}
}

function dessiner(){
  var hote=document.getElementById("cols"), vivants={};
  cols.forEach(function(col){
    // Les colonnes ne sont jamais REORDONNEES dans le DOM : deplacer une iframe la
    // recharge, et une colonne rechargee perd tout ce que le mur sert a garder sous les
    // yeux. Une nouvelle colonne s'ajoute a la fin, point.
    if(!col.node)hote.appendChild(creer(col));
    vivants[col.uid]=1;
    peindreTab(col);optionsListe(col);
    // La colonne entiere se teinte quand son chantier attend un arbitrage. Le calque, lui,
    // vit DANS la colonne : sur un mur de six colonnes en plein ecran, il faut d'abord
    // savoir laquelle regarder.
    var etat=trouver(cle(col)), sig=etat?signal(etat):null;
    col.node.classList.toggle("ask",!!(etat&&etat.asking));
    col.node.classList.toggle("wait",sig==="wait");
    col.node.classList.toggle("stall",sig==="stall");
    var url="/panel?home="+encodeURIComponent(col.home)+
            "&campaign="+encodeURIComponent(col.campaign);
    // Meme raison : la source ne se reecrit que si la cible a vraiment change, sinon
    // chaque battement rechargerait toutes les colonnes.
    if(col.frame.getAttribute("data-cible")!==url){
      col.frame.setAttribute("data-cible",url);col.frame.src=url;
    }
  });
  Array.prototype.slice.call(hote.children).forEach(function(n){
    // Le message de mur vide n'a pas d'uid et n'est pas une colonne : sans ce garde-fou,
    // le premier passage l'emporterait et il ne reviendrait jamais.
    if(n.hasAttribute("data-uid")&&!vivants[n.getAttribute("data-uid")])hote.removeChild(n);
  });
  document.getElementById("vide").hidden=cols.length>0;
  document.getElementById("plus").disabled=!campagnes.length||cols.length>=MAX;
}

function ajouter(){
  if(!campagnes.length||cols.length>=MAX)return;
  var pris={};cols.forEach(function(c){pris[cle(c)]=1});
  var libre=null;
  for(var i=0;i<campagnes.length;i++){
    if(!pris[cleDe(campagnes[i])]){libre=campagnes[i];break}
  }
  var c=libre||campagnes[0];
  cols.push({uid:String(++seq),home:c.home,campaign:c.id});
  ecrire();dessiner();
}

function premier(){
  var sauve=lire();
  if(sauve){
    cols=sauve.map(function(c){
      return {uid:String(++seq),home:c.home,campaign:c.campaign}});
    return;
  }
  // Premiere ouverture : les chantiers ouverts, plafonnes a trois. Ouvrir douze colonnes
  // d'un coup sur une machine qui suit douze projets ne montrerait rien de lisible.
  campagnes.filter(function(c){return c.state==="open"}).slice(0,3).forEach(function(c){
    cols.push({uid:String(++seq),home:c.home,campaign:c.id})});
  ecrire();
}

// Âge au-delà duquel une lecture de quota est jugée trop vieille pour être montrée
// à jour. Le fichier n'est réécrit que par une session Claude Code active : quinze
// minutes de silence disent qu'aucune session ne tourne plus pour la rafraîchir.
var QUOTA_AGE_MAX=15*60;

function remplirQuota(q){
  var conteneur=document.getElementById("quota");
  var fenetres=q&&q.fenetres||[];
  // Sans fenêtre lisible, le conteneur reste caché : une jauge vide se lirait
  // comme "zéro consommé", ce que ce module ne sait justement pas dire.
  if(!fenetres.length){conteneur.hidden=true;return}
  conteneur.hidden=false;
  // Vidage nœud par nœud, jamais par une affectation globale : la reconstruction
  // qui suit n'utilise que createElement et textContent, une donnée du fichier de
  // quota ne doit jamais se retrouver interprétée comme du balisage.
  while(conteneur.firstChild)conteneur.removeChild(conteneur.firstChild);
  var vieux=q.ageSecondes>QUOTA_AGE_MAX;
  fenetres.forEach(function(f){
    var perime=f.perime||vieux;
    var item=document.createElement("span");
    item.className="qwin"+(perime?" estompe":"");
    if(perime)item.title=f.perime
      ? "cette fenêtre a déjà été réinitialisée : ce pourcentage date d'avant"
      : "lecture vieille de plus de quinze minutes : aucune session ne la rafraîchit";
    var cle=document.createElement("span");
    cle.className="qkey";cle.textContent=f.cle;
    item.appendChild(cle);
    var piste=document.createElement("span");piste.className="ptrack";
    var barre=document.createElement("span");barre.className="pfill";
    barre.style.width=Math.max(0,Math.min(100,f.pourcent))+"%";
    barre.style.background=f.couleur;
    piste.appendChild(barre);
    item.appendChild(piste);
    var texte=document.createElement("span");
    texte.textContent=f.pourcent+"% · reset "+f.resetTexte;
    item.appendChild(texte);
    conteneur.appendChild(item);
  });
}

function battement(){
  fetch("/api/state").then(function(r){return r.json()}).then(function(etat){
    campagnes=etat.campaigns||[];
    var pb=document.getElementById("pbs");
    pb.hidden=!etat.problems.length;
    if(etat.problems.length)pb.textContent=etat.problems.length+" home illisible";
    var choix=campagnes.reduce(function(n,c){return n+(c.asking||0)},0);
    var ak=document.getElementById("asks");
    ak.hidden=!choix;
    ak.textContent=choix+" choix à faire";
    remplirQuota(etat.quota);
    pulse(true);
    if(!monte){monte=true;premier()}
    dessiner();
  }).catch(function(){pulse(false)});
}

// Reçoit ce que le panneau vient d'afficher (voir annoncerOnglet() dans _JS) : jamais
// recalculé côté mur, juste reporté sur l'onglet. e.source identifie la colonne sans
// ambiguïté, même si deux colonnes montrent par erreur le même chantier.
window.addEventListener("message",function(e){
  if(e.origin!==window.location.origin)return;
  var msg=e.data;
  if(!msg||!msg.ordoOnglet)return;
  for(var i=0;i<cols.length;i++){
    if(cols[i].frame&&cols[i].frame.contentWindow===e.source){
      cols[i].tabData=msg;peindreTab(cols[i]);break;
    }
  }
});
// Un clic hors de l'onglet ou de sa liste referme le popover ouvert -- comportement
// attendu de tout menu déroulant, y compris natif.
document.addEventListener("click",function(e){
  if(!listeActive)return;
  if(listeActive.tab.contains(e.target)||listeActive.liste.contains(e.target))return;
  fermerListe(listeActive);
});

document.getElementById("plus").addEventListener("click",ajouter);
document.getElementById("full").addEventListener("click",function(){
  if(document.fullscreenElement)document.exitFullscreen();
  else document.documentElement.requestFullscreen();
});
battement();
timer=setInterval(battement,POLL*1000);
document.addEventListener("visibilitychange",function(){
  if(document.hidden){clearInterval(timer);timer=null}
  else if(!timer){battement();timer=setInterval(battement,POLL*1000)}
});
})();
""".strip()


def page(poll: int = POLL_S) -> str:
    """Le mur, servi a la racine : une colonne par chantier, cote a cote.

    C'est l'adresse qu'on met en favori, donc celle qui doit ouvrir sur TOUT ce qui tourne,
    pas sur un chantier avec un menu pour changer. Elle ne porte aucune donnee : la
    disposition vit dans le navigateur (localStorage), l'etat de chaque chantier dans sa
    colonne. Un mur qui embarquerait l'etat serait fige des sa livraison, et il faudrait le
    recharger en entier pour voir bouger une seule colonne.
    """
    js = _MUR_JS.replace("__POLL__", str(max(1, int(poll))))
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ordo</title>
<style>{_CSS}
{_MUR_CSS}</style></head>
<body>
<div id="wtop">
  <span id="mark" class="m">ORDO</span>
  <span id="pulse" title="battement du serveur"></span>
  <span id="quota" hidden></span>
  <span class="tag err" id="pbs" hidden></span>
  <span class="tag ask" id="asks" hidden></span>
  <span id="lgd">
    <span class="item"><span class="sw" style="background:var(--done)"></span>fait</span>
    <span class="item"><span class="sw" style="background:var(--running)"></span>en cours</span>
    <span class="item"><span class="sw" style="background:var(--finishing)"></span>rédaction</span>
    <span class="item"><span class="sw" style="background:var(--ready)"></span>lançable</span>
    <span class="item"><span class="sw" style="background:var(--queued)"></span>en attente</span>
    <span class="lsep"></span>
    <span class="item"><span class="sw rond" style="background:var(--m-haiku)"></span>haiku</span>
    <span class="item"><span class="sw rond" style="background:var(--m-sonnet)"></span>sonnet</span>
    <span class="item"><span class="sw rond" style="background:var(--m-opus)"></span>opus</span>
  </span>
  <button class="icon-btn" id="plus" title="ajouter une colonne" aria-label="ajouter une colonne">
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
  </button>
  <button class="icon-btn" id="full" title="plein écran" aria-label="plein écran">
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>
  </button>
</div>
<div id="cols">
  <div id="vide" hidden>aucune colonne. « + colonne » en ouvre une.</div>
</div>
<script>{js}</script>
</body></html>
"""
