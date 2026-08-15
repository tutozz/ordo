"""Ou en est le chantier, en clair, pour l'humain qui revient apres deux heures.

Ce module n'apporte aucune information que carte.model() n'ait deja. Ce qu'il apporte est
une CONTRAINTE : il refuse d'imprimer un identifiant de tache sans son titre, et il refuse
de taire l'objet d'une tache vivante. C'est le seul defaut de lecture d'Ordo qui compte
vraiment.

Le probleme qu'il resout, tel qu'il se produit : l'orchestratrice parle a l'humain en
`t-33`, `q-03`, `c-01`. Ces identifiants lui suffisent, elle les resout dans son propre
contexte. L'humain, lui, revient d'une reunion et lit "t-33 travaille" sans avoir la
moindre idee de ce qu'est t-33. Pire, apres une compaction de son contexte,
l'orchestratrice elle-meme n'a plus les titres et continue pourtant a citer les
identifiants : le message devient illisible pour tout le monde, sans que rien ne le
signale.

D'ou le parti pris : la remise en contexte est CALCULEE depuis l'etat, jamais rappelee de
memoire. `ordo digest` rend un bloc pret a recopier, dont l'orchestratrice traduit les
etiquettes dans la langue de l'humain sans en toucher le contenu.

Lecture seule, comme carte : ce module n'ecrit rien et ne parle jamais a tmux.
"""

from __future__ import annotations

from . import carte, store

# Une tache "vivante" est une tache dont l'humain doit savoir qu'elle consomme des jetons
# en ce moment. "waiting" en fait partie : son pane existe toujours et son executante
# reprendra des qu'une reponse arrive.
_VIVANTS = ("running", "waiting")

# Ce qui, dans l'etat, exige un geste de l'humain plutot que de l'orchestratrice. Une
# question non marquee pourHumain n'y figure pas : elle appartient a l'orchestratrice, et
# la remonter ici ferait sonner l'humain pour une decision qui n'est pas la sienne.
_APPEL_HUMAIN = ("blocked", "failed")


def model(chantier_id: str) -> dict:
    """Projection de remise en contexte d'un chantier. Leve si le chantier n'existe pas.

    `alive` n'est pas un parametre, contrairement a carte.model() : la vivacite d'un pane
    ne change rien a ce qu'un humain a besoin de lire pour se resituer, et la demander
    ferait payer un aller-retour tmux a chaque digest.
    """
    m = carte.model(chantier_id)
    nodes = m["nodes"]
    state = store.load()

    # nodes est ordonne par identifiant numerique croissant (carte.model le construit
    # depuis `ids` deja trie) : parcourir le dict suffit, aucun retri necessaire ici.
    vivantes = [tid for tid in nodes if nodes[tid]["state"] in _VIVANTS]
    phases_vivantes = {nodes[tid]["group"] for tid in vivantes}

    phases = [
        {
            "key": g["key"],
            "label": g["label"],
            "why": g["why"],
            "done": g["done"],
            "total": len(g["tasks"]),
            "planned": g["planned"],
            "live": g["key"] in phases_vivantes,
        }
        for g in m["groups"]
    ]

    running = [_vue_tache(nodes[tid], m) for tid in vivantes]
    suivantes = [_vue_tache(nodes[tid], m) for tid in nodes if nodes[tid]["ready"]]

    return {
        "campaign": m["campaign"],
        "progress": {
            "done": m["counts"].get("done", 0),
            "total": m["counts"].get("total", 0),
        },
        "phases": phases,
        "running": running,
        "forHuman": _pour_humain(chantier_id, nodes, state),
        "next": suivantes,
    }


def _vue_tache(node: dict, m: dict) -> dict:
    """Une tache reduite a ce qui la rend comprehensible : son titre, son objet, sa phase.

    `whyMissing` n'est pas un detail de presentation. Une tache vivante sans `why` est une
    tache dont personne ne peut dire pourquoi elle tourne ; la taire reviendrait a
    fabriquer un digest qui a l'air complet et ne l'est pas.
    """
    phase = next((g for g in m["groups"] if g["key"] == node["group"]), None)
    return {
        "id": node["id"],
        "titre": node["titre"],
        "state": node["state"],
        "why": node["why"],
        "whyMissing": not node["why"],
        "phase": node["group"],
        "phaseLabel": (phase or {}).get("label", ""),
        "elapsedS": node["elapsedS"],
        "checkDone": node["checkDone"],
        "checkTotal": node["checkTotal"],
    }


def _pour_humain(chantier_id: str, nodes: dict, state: dict) -> list[dict]:
    """Ce qui attend un geste de l'humain, et strictement rien d'autre.

    L'ordre est intentionnel : les questions d'abord, parce qu'une executante y est
    reellement arretee ; les taches bloquees ensuite, qui attendent un diagnostic.
    """
    entrees: list[dict] = []

    for qid in sorted(state["questions"], key=_num_id):
        q = state["questions"][qid]
        if q["chantier"] != chantier_id or not q.get("pourHumain"):
            continue
        if q.get("answer") is not None:
            continue
        tid = q.get("tache")
        entrees.append({
            "kind": "question",
            "id": qid,
            "task": tid,
            "taskTitle": nodes.get(tid, {}).get("titre", ""),
            "detail": q.get("question") or "",
        })

    for tid in sorted(nodes, key=_num_id):
        node = nodes[tid]
        if node["state"] not in _APPEL_HUMAIN:
            continue
        entrees.append({
            "kind": node["state"],
            "id": tid,
            "task": tid,
            "taskTitle": node["titre"],
            "detail": node["error"] or node["reportNote"] or "no reason recorded",
        })

    return entrees


def _num_id(identifiant: str) -> tuple[int, str]:
    _, _, chiffres = identifiant.partition("-")
    return (int(chiffres), identifiant) if chiffres.isdigit() else (10**9, identifiant)


# ---------------------------------------------------------------------------
# Rendu texte
# ---------------------------------------------------------------------------

_ETIQUETTE = 8


def render(m: dict) -> str:
    """Le bloc que l'orchestratrice recopie dans son message a l'humain.

    Invariant unique et non negociable : aucune ligne ne cite un identifiant de tache sans
    son titre sur la meme ligne. C'est ce que verifie
    tests/test_situation.py::RenderTest::test_render_ne_cite_aucun_identifiant_nu, et c'est
    la seule raison pour laquelle ce rendu existe plutot qu'un `ordo show` un peu enrichi.
    """
    lignes: list[str] = []

    ch = m["campaign"]
    entete = f"{ch['id']}  {ch.get('slug') or ''}".rstrip()
    lignes.append(f"{entete}  ·  {m['progress']['done']}/{m['progress']['total']} tasks done")

    vivantes = [p for p in m["phases"] if p["live"]]
    if vivantes:
        for p in vivantes:
            lignes.append(_champ("phase", f"{_phase(p)} — {p['done']}/{p['total']} done"))
    else:
        lignes.append(_champ("phase", "no phase in progress"))

    if m["running"]:
        for i, t in enumerate(m["running"]):
            lignes.append(_champ("running" if i == 0 else "", _ligne_tache(t)))
            if t["why"]:
                lignes.append(_champ("", f"  why: {t['why']}"))
    else:
        lignes.append(_champ("running", "nothing alive"))

    if m["forHuman"]:
        for i, e in enumerate(m["forHuman"]):
            lignes.append(_champ("for you" if i == 0 else "", _ligne_appel(e)))
    else:
        lignes.append(_champ("for you", "nothing"))

    if m["next"]:
        for i, t in enumerate(m["next"]):
            objet = f" — {t['why']}" if t["why"] else ""
            lignes.append(
                _champ("next" if i == 0 else "", f"{_nomme(t['id'], t['titre'])}{objet}")
            )
    else:
        lignes.append(_champ("next", "nothing launchable right now"))

    return "\n".join(lignes)


def _champ(etiquette: str, valeur: str) -> str:
    return f"{etiquette:<{_ETIQUETTE}}: {valeur}" if etiquette else f"{' ' * _ETIQUETTE}  {valeur}"


def _phase(p: dict) -> str:
    return f"{p['key']} « {p['label']} »" if p["key"] else p["label"]


def _nomme(identifiant: str, titre: str) -> str:
    """Un identifiant ne sort jamais seul de ce module. C'est tout le contrat.

    Le repli sur "no title" n'est pas de la coquetterie : chantier.add_task() accepte un
    titre vide, et sans ce repli l'invariant tombait sur `t-01 «  »`, un identifiant nu
    entre deux guillemets. Le garde-fou de la suite ne pouvait meme pas le voir, puisque la
    chaine vide est contenue dans n'importe quelle ligne.
    """
    return f"{identifiant} « {titre or 'no title'} »"


def _duree(secondes: int | None) -> str:
    """Copie assumee de carte._duree : un helper de six lignes ne justifie pas de dependre
    d'un nom prive d'un autre module, qui casserait en silence a son prochain renommage."""
    if secondes is None:
        return "-"
    if secondes < 60:
        return f"{secondes}s"
    if secondes < 3600:
        return f"{secondes // 60}m"
    return f"{secondes // 3600}h{(secondes % 3600) // 60:02d}"


def _ligne_tache(t: dict) -> str:
    morceaux = [_nomme(t["id"], t["titre"]), _duree(t["elapsedS"])]
    if t["checkTotal"]:
        morceaux.append(f"checks {t['checkDone']}/{t['checkTotal']}")
    if t["state"] == "waiting":
        morceaux.append("waiting for an answer")
    ligne = "  ".join(morceaux)
    if t["whyMissing"]:
        # Le rappel de commande porte l'identifiant ET le titre parce qu'il est sur la
        # meme ligne : un objet manquant se repare, il ne se signale pas sur une ligne
        # orpheline que personne ne relie a sa tache.
        ligne += f'  — why NOT WRITTEN, run: ordo why {t["id"]} "..."'
    return ligne


def _ligne_appel(e: dict) -> str:
    cible = _nomme(e["task"], e["taskTitle"]) if e.get("task") else "campaign"
    if e["kind"] == "question":
        return f"{e['id']} on {cible} — {e['detail']}"
    return f"{cible} {e['kind']}: {e['detail']}"
