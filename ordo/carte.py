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
comportement, pas le code : phases repliables, recherche, filtres, deux vues, chaine
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
        return impose, "modele impose a son lancement", False
    if task.get("startedAt"):
        return "defaut", "lancee sans modele impose : celui de claude a servi", False
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
    """Faits courts de la ligne de detail. La duree n'y est plus : elle a rejoint l'en-tete
    de la carte, ou elle reste visible quelle que soit la vue."""
    bouts = []
    if node["checkTotal"]:
        bouts.append(f'{node["checkDone"]}/{node["checkTotal"]}')
    if node["paneId"]:
        bouts.append(f'{node["paneId"]} {_VIVACITE[node["paneAlive"]]}')
    return _SEP.join(bouts)


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
        tours = (jetons or {}).get("turns") or 0
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
            "elapsedS": node["elapsedS"],
            # Le modele sort a part de facts pour la meme raison que la duree : il se lit
            # SANS ouvrir la tache. C'est lui qui dit s'il faut relire le brief avant de
            # lancer, et le relire apres coup ne sert plus a rien.
            "model": node["model"],
            "modelWhy": node["modelWhy"],
            "modelPredit": node["modelPredit"],
            # Le pane et les tours sortent a part de facts, comme la duree et le modele :
            # ce sont les chiffres qui disent OU une tache s'execute et depuis combien de
            # temps elle traine son contexte, et le second predit son cout mieux que tout
            # le reste. Les lire ne doit pas demander d'ouvrir la tache.
            "pane": node["paneId"] or "",
            "turns": tours,
            "sessionLongue": bool(tours >= (usage.SEUIL_TOURS or float("inf"))),
            "tokens": usage.court(jetons["output"]) if jetons else "",
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
            "checklist": [
                {"text": c["label"], "done": bool(c["done"])} for c in node["checklist"]
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
--done:#46a35a;--running:#d3a03a;--ready:#5aa2f0;--queued:#4a5361;--blocked:#e05252;
--cancelled:#333941;--up:#5fa96f;--down:#5aa2f0;--accent:#8cc0f7}
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
.cid{font-size:12px;font-weight:600;letter-spacing:.04em;color:var(--txt);
border:1px solid #2b3038;border-radius:5px;padding:1px 6px}
.pct{margin-left:auto;font-size:13px;font-weight:600;color:var(--txt)}
#segs{display:flex;gap:3px;margin-top:9px;align-items:flex-end}
#segs .seg{min-width:12px}
#segs .track{height:6px;border-radius:2px;background:var(--line2);overflow:hidden}
#segs .fill{display:block;height:100%;border-radius:2px}
#segs .lab{font-size:9.5px;color:var(--dim2);margin-top:3px;text-align:center}
#bar{display:flex;gap:6px;margin-top:10px;align-items:center;flex-wrap:wrap}
#q{flex:1 1 150px;min-width:110px;background:var(--panel);border:1px solid var(--line);
border-radius:6px;color:var(--txt);font:inherit;font-size:12px;padding:5px 9px;outline:none}
#q:focus{border-color:#2f6ba8}
.pill{border:1px solid var(--line);background:var(--panel);color:#9aa4b1;border-radius:20px;
font:inherit;font-size:11px;padding:4px 9px;cursor:pointer;flex:none}
.pill.on{border-color:#2f6ba8;background:#132539;color:var(--accent)}
.pill b{opacity:.55;margin-left:5px;font-weight:400}
#views{display:flex;border:1px solid var(--line);border-radius:20px;overflow:hidden;
flex:none;margin-left:auto}
#views button{background:transparent;color:#7d8794;border:none;font:inherit;font-size:11px;
padding:4px 10px;cursor:pointer}
#views button.on{background:#132539;color:var(--accent)}

#warn{border-bottom:1px solid #5a3a1a;background:#241a0c;padding:7px 14px;
max-height:20vh;overflow:auto;color:#e3b341;font-size:11.5px}
#warn div+div{margin-top:3px}
#warn[hidden]{display:none}

#board{position:relative;padding:14px 14px 0}
#wires{position:absolute;left:0;top:0;width:100%;pointer-events:none;z-index:0;
overflow:visible}
section.phase{margin-bottom:14px}
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
.view-liste .rangs{flex-direction:column;gap:9px}
.rang{min-width:0}
.view-graphe .rang{flex:1 1 190px;max-width:240px}
.view-graphe .rang.wide{flex:1 1 100%;max-width:none}
.rlist{display:flex;flex-direction:column;gap:7px}
.view-liste .rlist{gap:5px}

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
.rhead{display:flex;align-items:baseline;gap:7px}
.rid{font-size:10.5px;font-weight:600;color:#9aa4b1;flex:none}
.rst{font-size:10px;font-weight:500}
/* Le modele de l'executante, sur la case. Trait plein quand il a reellement tourne,
   pointille quand ce n'est encore qu'une prevision du routage : la difference entre un
   fait et un pronostic doit se voir sans lire le mot. */
.mdl{font-size:9.5px;font-weight:600;letter-spacing:.03em;border:1px solid var(--line);
border-radius:4px;padding:0 4px;color:var(--dim2);flex:none}
.mdl.haiku{color:#7fb08a;border-color:#2c4d35}
.mdl.sonnet{color:#6fa8dc;border-color:#274a68}
.mdl.opus{color:#b892e0;border-color:#453060}
.mdl.predit{border-style:dashed;opacity:.75}
/* Le pane et les tours d'une session en cours. Les tours virent a l'orange au-dela du
   seuil de compaction : c'est le seul chiffre qui predit ce qu'une session va coûter, et
   il n'apparaissait nulle part. */
.pane{color:var(--dim3)}
.turns{color:var(--dim2)}
.row.running .turns{color:var(--txt2)}
.turns.longue{color:#e3b341;font-weight:600}
.rlinks{margin-left:auto;flex:none;display:flex;gap:7px;font-size:10px}
.row.focus .rlinks{font-size:12px;font-weight:600}
.nup{color:var(--up)}.ndown{color:var(--down)}.nnone{color:#3a4049}
.dur{color:var(--dim2)}
/* Les jetons ne se colorent que sur une tache en cours : c'est la seule ou le chiffre
   bouge encore, donc la seule ou il appelle le regard. */
.tok{color:var(--dim2)}
.row.running .tok{color:var(--running)}
.rtitle{font-size:12.5px;line-height:1.35;color:var(--txt);margin-top:3px;text-wrap:pretty}
.row.cancelled .rtitle{color:var(--dim2);text-decoration:line-through}
.rmeta{display:flex;gap:9px;align-items:baseline;margin-top:3px;font-size:10.5px;
color:var(--dim2)}
.rmeta .zones{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.detail{margin-top:9px;border-top:1px solid #262c34;padding-top:11px}
.chain{display:flex;gap:10px;align-items:stretch;flex-wrap:wrap;margin-bottom:12px}
.chain .side{flex:1 1 190px;min-width:0}
.chain .arrow{flex:none;display:flex;align-items:center;color:#3f4753;font-size:15px}
.chain .self{flex:0 1 150px;min-width:0;display:flex;align-items:center}
.chain .selfbox{width:100%;border:1px solid #3f7fc4;border-radius:6px;background:#101922;
padding:7px 9px}
.k{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--dim2);
margin-bottom:5px}
.k.up{color:var(--up)}.k.down{color:var(--down)}
.col{display:flex;flex-direction:column;gap:4px}
.chip{display:flex;align-items:center;gap:6px;cursor:pointer;background:#191d23;
border:1px solid #262c34;border-radius:5px;padding:4px 7px;min-width:0}
.chip:hover{border-color:#3f4753}
.dot{width:6px;height:6px;border-radius:2px;flex:none;display:inline-block}
.chip .cid2{font-size:10.5px;color:#9aa4b1;flex:none}
.chip .ctitle{font-size:11px;color:var(--txt2);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.none{font-size:11px;color:var(--dim3)}
.why{font-size:11.5px;line-height:1.5;color:var(--txt2);border-left:2px solid #2f6ba8;
padding-left:9px;text-wrap:pretty}
.why.absent{color:#8a7433;border-left-color:#63541f}
.wait{font-size:11.5px;color:var(--txt2);cursor:pointer;padding:3px 7px;background:#191d23;
border-left:2px solid var(--running);border-radius:0 4px 4px 0}
.facts{margin-top:10px;display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
gap:4px 12px}
.fact{display:flex;gap:6px;font-size:11px;border-bottom:1px solid #1a1e24;padding-bottom:3px}
.fact .fk{color:var(--dim2);flex:none}
.fact .fv{color:var(--txt2);margin-left:auto;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.check{display:flex;gap:7px;align-items:flex-start;font-size:11.5px;line-height:1.4}
.box{flex:none;width:13px;height:13px;border-radius:3px;border:1px solid #2b3038;
color:var(--done);font-size:9px;line-height:12px;text-align:center;margin-top:2px}
.box.on{border-color:#2c4d35;background:#122116}
.check.on .ctext{color:#7d8794}
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

/* Le calque des questions. Une orchestratrice qui s'arrête pour demander un arbitrage le
   fait dans son terminal ; l'humain, lui, regarde cette page. Sans ce calque la campagne
   attend sans que rien ne le dise à l'endroit qu'il a sous les yeux. Reprend la teinte des
   avertissements, la seule de cette palette qui serve déjà à appeler le regard. */
#askchip{border-color:#63541f;color:#e3b341;cursor:pointer;
animation:ordopulse 2.2s ease-in-out infinite}
#ask{position:fixed;inset:0;z-index:40;background:rgba(6,8,11,.62);display:flex;
align-items:center;justify-content:center;padding:18px}
#ask[hidden]{display:none}
.askbox{width:100%;max-width:430px;background:#191408;border:1px solid #63541f;
border-radius:9px;padding:13px 14px;box-shadow:0 14px 40px rgba(0,0,0,.55)}
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
_JS = r"""
(function(){
var D=null, S={sel:null,hover:null,q:"",filter:"reste",closed:{},docs:{},view:"graphe"};
var byId={};

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
function kind(t){
  if(t.status==="done")return "done";
  if(t.status==="running")return "running";
  if(t.status==="cancelled")return "cancelled";
  if(t.status==="blocked"||t.status==="failed")return "blocked";
  return t.ready?"ready":"queued";
}
var COL={done:"#46a35a",running:"#d3a03a",ready:"#5aa2f0",queued:"#4a5361",
         blocked:"#e05252",cancelled:"#333941"};
var LAB={done:"fait",running:"en cours",ready:"lançable",queued:"en attente",
         blocked:"bloquée",cancelled:"annulée"};
function settled(t){var k=kind(t);return k==="done"||k==="cancelled"}
function focusId(){return S.hover||S.sel}
function matches(t){
  var q=S.q.trim().toLowerCase();
  if(!q)return true;
  return (t.id+" "+t.title+" "+(t.facts.zones||"")).toLowerCase().indexOf(q)>=0;
}
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
  var cls="row "+k;
  if(sel)cls+=" sel";
  if(S.filter==="reste"&&settled(t))cls+=" settled";
  var row=el("div",cls);
  row.setAttribute("data-row",t.id);
  var sb=el("span","sb");sb.style.background=COL[k];row.appendChild(sb);

  var head=el("div","rhead");
  head.appendChild(el("span","rid m",t.id));
  var st=el("span","rst",LAB[k]);st.style.color=(k==="queued")?"#6d7683":COL[k];
  head.appendChild(st);
  if(t.model){
    // La teinte ne se prend QUE dans cette table. t.model vient de l'etat, donc d'un
    // --model tape a la main : le poser tel quel en classe laisserait ecrire n'importe
    // quel nom de classe de la feuille de style depuis un lancement.
    var connu={haiku:1,sonnet:1,opus:1}[t.model]?" "+t.model:"";
    var md=el("span","mdl m"+connu+(t.modelPredit?" predit":""),t.model);
    // title est une propriete, jamais du balisage : un motif de routage y passe sans
    // risque, et c'est la que se conteste le choix du modele.
    md.title=(t.modelPredit?"prévu : ":"")+(t.modelWhy||"");
    head.appendChild(md);
  }
  var links=el("span","rlinks m");
  // Duree et jetons AVANT les liens, et toujours visibles : ce sont les deux chiffres
  // qu'on lit pendant qu'une tache tourne, c'est-a-dire au seul moment ou on la regarde.
  // Ils vivaient dans la ligne de meta, que la vue graphe n'affiche pas.
  if(t.duree)links.appendChild(el("span","dur",t.duree));
  if(t.tokens)links.appendChild(el("span","tok",t.tokens));
  // Le pane dit OU la tache s'execute, les tours disent depuis combien de temps elle
  // traine son contexte. Une session longue s'allume : au dela du seuil, chaque tour
  // supplementaire relit tout ce qui precede.
  if(t.pane)links.appendChild(el("span","pane",t.pane));
  if(t.turns){
    var tr=el("span","turns"+(t.sessionLongue?" longue":""),t.turns+"t");
    tr.title=t.sessionLongue
      ? "session longue : chaque tour relit tout le contexte accumulé"
      : "tours de la session";
    links.appendChild(tr);
  }
  links.appendChild(el("span",t.deps.length?"nup":"nnone","↑"+t.deps.length));
  links.appendChild(el("span",t.dependants.length?"ndown":"nnone","↓"+t.dependants.length));
  head.appendChild(links);
  row.appendChild(head);
  row.appendChild(el("div","rtitle",t.title));

  if(S.view!=="graphe"||sel){
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

function detailNode(t){
  var d=el("div","detail");

  var chain=el("div","chain");
  var left=el("div","side");
  left.appendChild(el("div","k up","attend ("+t.deps.length+")"));
  var lc=el("div","col");
  if(t.deps.length)t.deps.forEach(function(x){chip(x,lc)});
  else lc.appendChild(el("div","none","rien — elle peut partir seule"));
  left.appendChild(lc);chain.appendChild(left);
  chain.appendChild(el("div","arrow","→"));
  var self=el("div","self"),box=el("div","selfbox");
  box.appendChild(el("div","cid2 m",t.id));
  box.appendChild(el("div","ctitle",t.title.replace(/^[\d.]+[a-z]? /,"")));
  self.appendChild(box);chain.appendChild(self);
  chain.appendChild(el("div","arrow","→"));
  var right=el("div","side");
  right.appendChild(el("div","k down","débloque ("+t.dependants.length+")"));
  var rc=el("div","col");
  if(t.dependants.length)t.dependants.forEach(function(x){chip(x,rc)});
  else rc.appendChild(el("div","none","rien n'attend après elle"));
  right.appendChild(rc);chain.appendChild(right);
  d.appendChild(chain);

  d.appendChild(el("div","why"+(t.whyAbsent?" absent":""),
    t.whyAbsent?"aucune explication enregistrée pour cette tâche":t.why));

  if(t.waits.length){
    var w=el("div");w.style.marginTop="9px";
    w.appendChild(el("div","k","bloquée par"));
    var wc=el("div","col");
    t.waits.forEach(function(x){
      var b=el("div","wait",x.text);
      b.addEventListener("click",function(e){e.stopPropagation();select(x.id)});
      wc.appendChild(b);
    });
    w.appendChild(wc);d.appendChild(w);
  }

  var facts=el("div","facts");
  Object.keys(t.facts).forEach(function(key){
    if(key==="depend de"||key==="debloque")return;
    var f=el("div","fact");
    f.appendChild(el("span","fk",key));
    f.appendChild(el("span","fv m",t.facts[key]));
    facts.appendChild(f);
  });
  d.appendChild(facts);

  if(t.checklist.length){
    var c=el("div");c.style.marginTop="11px";
    var faits=t.checklist.filter(function(x){return x.done}).length;
    c.appendChild(el("div","k","checklist "+faits+"/"+t.checklist.length));
    var cc=el("div","col");
    t.checklist.forEach(function(x){
      var line=el("div","check"+(x.done?" on":""));
      line.appendChild(el("span","box"+(x.done?" on":""),x.done?"✓":""));
      line.appendChild(el("span","ctext",x.text));
      cc.appendChild(line);
    });
    c.appendChild(cc);d.appendChild(c);
  }

  t.docs.forEach(function(doc,i){
    var key=t.id+":"+i, open=!!S.docs[key];
    var box2=el("div","doc"+(open?" open":""));
    var b=document.createElement("button");
    b.appendChild(el("span","chev2","▸"));
    b.appendChild(el("span",null,doc.k));
    b.appendChild(el("span","size m",(Math.round(doc.text.length/100)/10)+"k"));
    b.addEventListener("click",function(e){
      e.stopPropagation();S.docs[key]=!S.docs[key];render();
    });
    box2.appendChild(b);
    var pre=document.createElement("pre");pre.className="m";pre.textContent=doc.text;
    box2.appendChild(pre);
    d.appendChild(box2);
  });
  return d;
}

function render(){
  var board=document.getElementById("board");
  board.className="view-"+S.view;
  var wires=document.getElementById("wires");
  board.innerHTML="";
  if(wires)board.appendChild(wires);

  var vues=0;
  D.phases.forEach(function(p){
    var all=p.order.map(function(i){return byId[i]}).filter(Boolean);
    var shown=all.filter(matches);
    var faits=all.filter(function(t){return kind(t)==="done"}).length;
    var fini=all.length>0&&all.every(settled);
    var forced=S.closed[p.key];
    // "reste" ne cache jamais une tache : il replie les phases finies et estompe le fait.
    // Une tache qui disparait, c'est une position qui bouge, donc un reperage perdu.
    var open=shown.length>0&&(forced===undefined?!(S.filter==="reste"&&fini):!forced);
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

    var rangs=el("div","rangs");
    var parNiveau={};
    shown.forEach(function(t){(parNiveau[t.level]=parNiveau[t.level]||[]).push(t)});
    Object.keys(parNiveau).map(Number).sort(function(a,b){return a-b}).forEach(function(lv){
      var ts=parNiveau[lv];
      var tient=ts.some(function(t){return t.id===S.sel});
      var rang=el("div","rang"+(tient?" wide":""));
      var liste=el("div","rlist");
      ts.forEach(function(t){liste.appendChild(rowNode(t));vues++});
      rang.appendChild(liste);rangs.appendChild(rang);
    });
    sec.appendChild(rangs);
    board.appendChild(sec);
  });
  if(!vues)board.appendChild(el("div","empty","aucune tache ne correspond au filtre."));
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

function paintTop(){
  var vivantes=D.tasks.filter(function(t){return kind(t)!=="cancelled"}).length;
  var faits=D.tasks.filter(function(t){return kind(t)==="done"}).length;
  document.getElementById("pct").textContent=
    (vivantes?Math.round(faits/vivantes*100):0)+"%";
  var segs=document.getElementById("segs");segs.innerHTML="";
  D.phases.forEach(function(p){
    var ts=p.order.map(function(i){return byId[i]}).filter(Boolean);
    var dn=ts.filter(function(t){return kind(t)==="done"}).length;
    var s=el("div","seg");
    s.style.flex=(ts.length||1)+" 1 0";
    s.title=(p.key||"-")+" "+p.name+" — "+dn+"/"+ts.length;
    var tr=el("div","track"),f=el("span","fill");
    f.style.width=(ts.length?dn/ts.length*100:0)+"%";
    f.style.background=(ts.length&&dn===ts.length)?"#46a35a":"#3c7a4a";
    tr.appendChild(f);s.appendChild(tr);
    s.appendChild(el("div","lab m",p.key||"-"));
    segs.appendChild(s);
  });
  var reste=D.tasks.filter(function(t){return !settled(t)}).length;
  document.getElementById("n-reste").textContent=reste;
  document.getElementById("n-tout").textContent=D.tasks.length;
  ["reste","tout"].forEach(function(k){
    document.getElementById("f-"+k).classList.toggle("on",S.filter===k)});
  ["graphe","liste"].forEach(function(v){
    document.getElementById("v-"+v).classList.toggle("on",S.view===v)});

  var c=D.campaign;
  document.getElementById("cid").textContent=c.id;
  document.getElementById("ctx").textContent=
    [c.slug,c.state,c.tmuxSession].filter(Boolean).join(" · ");
  var wb=document.getElementById("warn"), wc=document.getElementById("warnchip");
  if(wb&&wc){
    wb.innerHTML="";
    D.warnings.forEach(function(w){wb.appendChild(el("div",null,w.detail))});
    wc.hidden=!D.warnings.length;
    wc.textContent=D.warnings.length+" alerte"+(D.warnings.length>1?"s":"");
    if(D.warnings.length){wc.style.cursor="pointer";wc.style.borderColor="#63541f";
      wc.style.color="#e3b341"}
    var cache=false;
    try{cache=sessionStorage.getItem(K("warn"))==="1"}catch(e){}
    wb.hidden=!D.warnings.length||cache;
  }
  paintAsk();
  var bouts=["survol = liens · clic = détail · échap = fermer"];
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
  var id=focusId(), graphe=S.view==="graphe";
  if(!graphe&&!id){svg.innerHTML="";svg.setAttribute("height",0);return}
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
  if(graphe)D.tasks.forEach(function(t){t.deps.forEach(function(p){
    if(id&&(p===id||t.id===id))return;
    out+=arc(p,t.id,"#55606f","ah-d",1.2,id?0.12:0.8);
  })});
  var t=id?byId[id]:null;
  if(t){
    t.deps.forEach(function(p){out+=arc(p,id,"#5fa96f","ah-g",1.8,1)});
    t.dependants.forEach(function(c){out+=arc(id,c,"#5aa2f0","ah-b",1.8,1)});
  }
  svg.innerHTML=out;
  svg.setAttribute("height",board.scrollHeight);
}

document.getElementById("q").addEventListener("input",function(e){S.q=e.target.value;render()});
["reste","tout"].forEach(function(k){
  document.getElementById("f-"+k).addEventListener("click",function(){S.filter=k;render()})});
["graphe","liste"].forEach(function(v){
  document.getElementById("v-"+v).addEventListener("click",function(){S.view=v;render()})});
var wb=document.getElementById("warn"),wc=document.getElementById("warnchip");
if(wb&&wc){wc.addEventListener("click",function(){
  wb.hidden=!wb.hidden;try{sessionStorage.setItem(K("warn"),wb.hidden?"1":"0")}catch(e){}});
  try{if(sessionStorage.getItem(K("warn"))==="1")wb.hidden=true}catch(e){}}
var ac=document.getElementById("askchip");
if(ac)ac.addEventListener("click",function(){
  var box=document.getElementById("ask");
  if(!box)return;
  box.hidden=!box.hidden;
  // Rouvrir efface le masquage, sinon le prochain battement le refermerait aussitot.
  try{
    if(box.hidden)sessionStorage.setItem(K("ask"),box.getAttribute("data-q")||"");
    else sessionStorage.removeItem(K("ask"));
  }catch(e){}
});
window.addEventListener("keydown",function(e){if(e.key==="Escape"&&S.sel){S.sel=null;render()}});
window.addEventListener("resize",draw);
try{var last=sessionStorage.getItem(K("sel"));if(last)S.sel=last}catch(e){}
window.addEventListener("scroll",function(){
  try{sessionStorage.setItem(K("scroll"),window.scrollY)}catch(e){}});
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
      <span class="pct m" id="pct"></span></h1>
  <div id="segs"></div>
  <div id="bar">
    <input id="q" placeholder="filtrer : id, titre, zone…" autocomplete="off">
    <button class="pill" id="f-reste">reste<b class="m" id="n-reste"></b></button>
    <button class="pill" id="f-tout">tout<b class="m" id="n-tout"></b></button>
    <span class="pill" id="warnchip" hidden></span>
    <span class="pill" id="askchip" hidden></span>
    <div id="views"><button id="v-graphe">graphe</button><button id="v-liste">liste</button></div>
  </div>
</div>
<div id="warn" hidden></div>
<div id="ask" hidden></div>
<div id="board"><svg id="wires"></svg></div>
<div id="legend">
  <span class="item"><span class="sw" style="background:#46a35a"></span>fait</span>
  <span class="item"><span class="sw" style="background:#d3a03a"></span>en cours</span>
  <span class="item"><span class="sw" style="background:#5aa2f0"></span>lançable</span>
  <span class="item"><span class="sw" style="background:#4a5361"></span>en attente</span>
  <span style="margin-left:auto" id="foot"></span>
</div>
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
#board{padding:11px 11px 0}
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
      <span class="pct m" id="pct"></span></h1>
  <div id="segs"></div>
  <div id="bar">
    <input id="q" placeholder="filtrer : id, titre, zone..." autocomplete="off">
    <button class="pill" id="f-reste">reste<b class="m" id="n-reste"></b></button>
    <button class="pill" id="f-tout">tout<b class="m" id="n-tout"></b></button>
    <span class="pill" id="warnchip" hidden></span>
    <span class="pill" id="askchip" hidden></span>
    <span class="pill" id="dead" hidden></span>
    <div id="views"><button id="v-graphe">graphe</button><button id="v-liste">liste</button></div>
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
#wtop .tag.live{border-color:#63541f;color:#d3a03a}
#wtop .tag.err{border-color:#5a2f2f;color:#e05252}
#wtop .tag.ask{border-color:#63541f;color:#e3b341;
animation:ordopulse 2.2s ease-in-out infinite}
.col.ask{border-right-color:#63541f}
.col.ask .chead{background:#241a0c;border-bottom-color:#63541f}
#lgd{display:flex;gap:10px;margin-left:auto;font-size:10px;color:var(--dim2)}
#lgd span.item{display:flex;align-items:center;gap:4px;white-space:nowrap}
#lgd .sw{width:7px;height:7px;border-radius:2px;display:inline-block}
#wtop button{flex:none}
#wtop button:disabled{opacity:.35;cursor:default}

#cols{flex:1 1 auto;min-height:0;display:flex;align-items:stretch;overflow-x:auto}
.col{display:flex;flex-direction:column;flex:1 1 0;min-width:340px;
border-right:1px solid var(--line)}
.chead{flex:none;display:flex;align-items:center;gap:5px;padding:5px 6px;
background:var(--panel);border-bottom:1px solid var(--line2)}
.chead select{flex:1 1 auto;min-width:0;background:var(--row);border:1px solid var(--line);
border-radius:6px;color:var(--txt);font:inherit;font-size:11.5px;padding:3px 6px;
outline:none}
.chead select:focus{border-color:#2f6ba8}
.chead button{flex:none;background:transparent;border:1px solid var(--line);border-radius:6px;
color:var(--dim2);font:inherit;font-size:12px;line-height:1;padding:4px 7px;cursor:pointer}
.chead button:hover{color:var(--txt);border-color:#3f4753}
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
function libelle(c){
  return (c.slug||c.id)+" · "+c.done+"/"+c.total+
    (c.asking?" · CHOIX A FAIRE":"")+
    (c.running?" · "+c.running+" en cours":"")+(c.state==="open"?"":" · "+c.state);
}
// La disposition survit au rechargement : un mur qu'il faut remonter a chaque ouverture
// n'est pas un ecran dedie, c'est un formulaire.
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

function options(col){
  var sel=col.sel;
  // Deux raisons de ne PAS reconstruire. Un menu deroule a le focus : le refaire sous les
  // doigts le referme, et le battement tombe toutes les trois secondes, donc ca arrive.
  // Et une liste identique n'a rien a gagner a etre refaite.
  if(document.activeElement===sel)return;
  var sig=campagnes.map(function(c){return cleDe(c)+"="+libelle(c)}).join("|")+"#"+cle(col);
  if(col.sig===sig)return;
  col.sig=sig;
  sel.innerHTML="";
  campagnes.forEach(function(c){
    var o=document.createElement("option");
    o.value=cleDe(c);o.textContent=libelle(c);
    sel.appendChild(o);
  });
  sel.value=cle(col);
  // Un chantier sorti du registre ne peut pas etre choisi dans le menu : sans cette
  // ligne, le select retomberait en silence sur son premier element et la colonne
  // afficherait un autre projet que celui qu'on croit lire.
  if(sel.value!==cle(col)){
    var o=document.createElement("option");
    o.value=cle(col);o.textContent=col.campaign+" — introuvable";
    sel.appendChild(o);sel.value=cle(col);
  }
}

function creer(col){
  var d=document.createElement("div");
  d.className="col";d.setAttribute("data-uid",col.uid);
  var h=document.createElement("div");h.className="chead";
  var sel=document.createElement("select");
  sel.setAttribute("aria-label","chantier de la colonne");
  sel.addEventListener("change",function(){
    var p=sel.value.split(" ");
    col.home=p[0];col.campaign=p[1];ecrire();dessiner();
  });
  var x=document.createElement("button");
  x.textContent="×";x.title="fermer la colonne";
  x.addEventListener("click",function(){
    cols=cols.filter(function(c){return c!==col});ecrire();dessiner();
  });
  h.appendChild(sel);h.appendChild(x);
  var f=document.createElement("iframe");
  f.setAttribute("title","carte du chantier");
  d.appendChild(h);d.appendChild(f);
  col.node=d;col.sel=sel;col.frame=f;
  return d;
}

function dessiner(){
  var hote=document.getElementById("cols"), vivants={};
  cols.forEach(function(col){
    // Les colonnes ne sont jamais REORDONNEES dans le DOM : deplacer une iframe la
    // recharge, et une colonne rechargee perd tout ce que le mur sert a garder sous les
    // yeux. Une nouvelle colonne s'ajoute a la fin, point.
    if(!col.node)hote.appendChild(creer(col));
    vivants[col.uid]=1;
    options(col);
    // La colonne entiere se teinte quand son chantier attend un arbitrage. Le calque, lui,
    // vit DANS la colonne : sur un mur de six colonnes en plein ecran, il faut d'abord
    // savoir laquelle regarder.
    var etat=trouver(cle(col));
    col.node.classList.toggle("ask",!!(etat&&etat.asking));
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

function battement(){
  fetch("/api/state").then(function(r){return r.json()}).then(function(etat){
    campagnes=etat.campaigns||[];
    var live=campagnes.filter(function(c){return c.running}).length;
    var t=document.getElementById("live");
    t.textContent=campagnes.length+" chantier"+(campagnes.length>1?"s":"")+" · "+
      (live?live+" avec une exécutante":"aucune exécutante");
    t.className="tag"+(live?" live":"");
    var pb=document.getElementById("pbs");
    pb.hidden=!etat.problems.length;
    if(etat.problems.length)pb.textContent=etat.problems.length+" home illisible";
    var choix=campagnes.reduce(function(n,c){return n+(c.asking||0)},0);
    var ak=document.getElementById("asks");
    ak.hidden=!choix;
    ak.textContent=choix+" choix à faire";
    pulse(true);
    if(!monte){monte=true;premier()}
    dessiner();
  }).catch(function(){pulse(false)});
}

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
  <span class="tag" id="live"></span>
  <span class="tag err" id="pbs" hidden></span>
  <span class="tag ask" id="asks" hidden></span>
  <span id="lgd">
    <span class="item"><span class="sw" style="background:#46a35a"></span>fait</span>
    <span class="item"><span class="sw" style="background:#d3a03a"></span>en cours</span>
    <span class="item"><span class="sw" style="background:#5aa2f0"></span>lancable</span>
    <span class="item"><span class="sw" style="background:#4a5361"></span>en attente</span>
  </span>
  <button class="pill" id="plus" title="ajouter une colonne">+ colonne</button>
  <button class="pill" id="full" title="plein ecran">plein ecran</button>
</div>
<div id="cols">
  <div id="vide" hidden>aucune colonne. « + colonne » en ouvre une.</div>
</div>
<script>{js}</script>
</body></html>
"""
