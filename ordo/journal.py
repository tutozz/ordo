"""Journal du chantier et brief regenere.

Le journal est la memoire d'un chantier qui survit a un redemarrage. Trois auteurs, et
trois seulement (I11) : ORDO ecrit les faits observables, gratuitement ; ORCH (l'orchestra-
trice) ecrit le pourquoi, une ligne, seulement quand elle tranche quelque chose de non
evident ; USER porte les messages de l'humain. Le pourquoi est exactement ce qu'un
redemarrage perd si on ne l'ecrit pas ici.

brief() compose ce qu'une orchestratrice relit si elle repart a froid. Il doit rester
dense : il remplace six heures de transcript, il ne les recopie pas.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import chantier, store

# Les trois seuls auteurs valides (I11). Un quatrieme n'existe pas : cf. write().
_AUTEURS = ("ORDO", "ORCH", "USER")

# Bornes de densite du brief. Le chantier peut durer six heures ; l'historique complet
# vit deja dans state.json et sur disque (journal, rapports), le brief n'a besoin que du
# plus recent pour reancrer une orchestratrice qui repart a froid.
_CAPTEUR_CYCLES_DANS_LE_BRIEF = 3
_MESSAGES_HUMAIN_DANS_LE_BRIEF = 10

# Etats de rapport qui comptent comme "termines" pour la section "notes des rapports
# termines" du brief. "progress" et "asking" sont des signaux intermediaires : la tache
# n'a pas conclu, sa note n'a rien a faire dans un resume qui doit rester dense.
_ETATS_RAPPORT_TERMINES = ("done", "blocked")


def _journal_path(chantier_id: str) -> Path:
    return store.home() / "journal" / f"{chantier_id}.md"


def _get_chantier(state: dict, chantier_id: str) -> dict:
    ch = state["chantiers"].get(chantier_id)
    if ch is None:
        raise chantier.ChantierError(f"campaign not found: {chantier_id}")
    return ch


def _format_line(heure: str, auteur: str, texte: str) -> str:
    # Le format sur disque impose UNE ligne par fait. Un texte multiligne romprait ce
    # contrat et le reparsing avec lui ; on echappe donc les retours a la ligne du texte
    # avant ecriture et on les restaure a la lecture (_parse_line). Limite documentee : un
    # texte contenant deja litteralement les deux caracteres "\n" serait mal reparse ; ce
    # cas n'est pas traite, la probabilite qu'un auteur tape ces deux caracteres a la
    # suite est jugee negligeable face au risque reel (un journal de decisions qui casse).
    echappe = texte.replace("\r\n", "\n").replace("\n", "\\n")
    return f"{heure}  {auteur:<4}  {echappe}"


def _parse_line(line: str) -> dict | None:
    # Format ecrit par _format_line : heure(5) + "  " + auteur(4) + "  " + texte. On
    # decoupe par position plutot que par split() : un texte qui commencerait par des
    # espaces ne doit jamais deplacer la frontiere entre les champs.
    if len(line) < 13:
        return None
    heure = line[0:5]
    auteur = line[7:11].rstrip()
    texte = line[13:].replace("\\n", "\n")
    return {"heure": heure, "auteur": auteur, "texte": texte}


def write(chantier_id: str, auteur: str, texte: str) -> None:
    """Ajoute un fait au journal du chantier. auteur doit etre ORDO, ORCH ou USER (I11).

    Refuser tout le reste n'est pas de la purete stylistique : dans le projet d'origine,
    le verbe qui laissait l'orchestratrice ecrire un POURQUOI journalisait sous USER. Ses
    arbitrages quittaient alors la section "journal des decisions" pour "derniers messages
    humains", et a la relance suivante elle relisait ses propres decisions comme des
    ordres de l'utilisateur. Le refus est explicite et nomme les trois auteurs valides.
    """
    if auteur not in _AUTEURS:
        raise chantier.ChantierError(
            f"invalid journal author: {auteur!r}, expected ORDO, ORCH or USER"
        )
    state = store.load()
    _get_chantier(state, chantier_id)
    heure = time.strftime("%H:%M")
    ligne = _format_line(heure, auteur, texte)
    with _journal_path(chantier_id).open("a", encoding="utf-8") as f:
        f.write(ligne + "\n")


def read(chantier_id: str, limit: int | None = None) -> list[dict]:
    """Reparse le journal du chantier en dicts {heure, auteur, texte}.

    Sans fichier, un chantier tout juste ouvert n'a pas encore ecrit son premier fait :
    liste vide, jamais une erreur. limit garde les entrees les PLUS RECENTES (fin de
    fichier), l'ordre d'ecriture faisant deja foi de la chronologie.
    """
    path = _journal_path(chantier_id)
    if not path.exists():
        return []
    entries: list[dict] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line:
            continue
        entry = _parse_line(raw_line)
        if entry is not None:
            entries.append(entry)
    if limit is not None:
        entries = entries[-limit:]
    return entries


def _section_objectif(ch: dict) -> str:
    lignes = [
        "## Goal and scope",
        f"Goal: {ch['objectif']}",
        f"Scope: {ch['perimetre'] or 'not specified'}",
        f"Out of scope: {ch['horsScope'] or 'not specified'}",
    ]
    return "\n".join(lignes)


def _section_graphe(chantier_id: str) -> str:
    return "## Graph\n" + chantier.graph_ascii(chantier_id)


def _section_rapports(state: dict, chantier_id: str) -> str:
    taches = sorted(
        (t for t in state["taches"].values() if t["chantier"] == chantier_id),
        key=lambda t: t["id"],
    )
    lignes = []
    for t in taches:
        report = t.get("report")
        if not isinstance(report, dict) or report.get("state") not in _ETATS_RAPPORT_TERMINES:
            continue
        note = report.get("note", "")
        lignes.append(f"- {t['id']} {t['titre']} [{report.get('state')}]: {note}")
    if not lignes:
        lignes = ["(no completed report)"]
    return "## Notes from completed reports\n" + "\n".join(lignes)


def _section_capteur(ch: dict) -> str:
    capteur = ch.get("capteur") or {}
    # I9/I12 : un capteur non adopte ne sert AUCUNE mesure, meme si des runs existent deja
    # (phase des 3 executions concordantes avant validation humaine). Servir un chiffre ici
    # fabriquerait exactement la fausse completion que le capteur existe pour attraper.
    if not capteur.get("adopted"):
        return "## Sensor\nunknown (sensor not adopted or absent)"
    runs = [r for r in (capteur.get("runs") or []) if isinstance(r, dict)]
    recentes = runs[-_CAPTEUR_CYCLES_DANS_LE_BRIEF:]
    if not recentes:
        return "## Sensor\nadopted, no cycle recorded yet"
    lignes = [f"adopted on {capteur.get('adoptedAt')}"]
    for run in recentes:
        at = run.get("at", "?")
        # capteur.run() range la sortie du script sous run["output"], pas a la racine du
        # run. Lire run["measured"] rendait la section "(aucune mesure)" en permanence,
        # meme apres trois cycles reussis et une adoption : un brief regenere qui affirme
        # silencieusement qu'on ne mesure rien est pire qu'une section absente.
        sortie = run.get("output") or {}
        mesures = ", ".join(
            f"{m.get('name')}={m.get('value')}"
            for m in sortie.get("measured", []) or []
            if isinstance(m, dict)
        )
        lignes.append(f"- {at}: {mesures or '(no measurement)'}")
        for d in sortie.get("drift", []) or []:
            if isinstance(d, dict):
                lignes.append(f"  drift: {d.get('detail', d)}")
    return "## Sensor\n" + "\n".join(lignes)


def _section_decisions(chantier_id: str) -> str:
    decisions = [e for e in read(chantier_id) if e["auteur"] == "ORCH"]
    if not decisions:
        return "## Decision journal\n(no decision logged)"
    lignes = [f"{e['heure']}  {e['texte']}" for e in decisions]
    return "## Decision journal\n" + "\n".join(lignes)


def _section_messages_humain(chantier_id: str) -> str:
    messages = [e for e in read(chantier_id) if e["auteur"] == "USER"]
    messages = messages[-_MESSAGES_HUMAIN_DANS_LE_BRIEF:]
    if not messages:
        return "## Latest human messages\n(no message)"
    lignes = [f"{e['heure']}  {e['texte']}" for e in messages]
    return "## Latest human messages\n" + "\n".join(lignes)


def brief(chantier_id: str) -> str:
    """Compose le brief regenere du chantier, dans l'ordre impose par la spec.

    Ordre : objectif et perimetre, graphe courant, notes des rapports termines, sorties
    capteur des derniers cycles, journal des decisions, derniers messages de l'humain.
    C'est ce que relit une orchestratrice qui repart a froid ; ce qui n'y est pas est perdu.
    """
    state = store.load()
    ch = _get_chantier(state, chantier_id)
    sections = [
        _section_objectif(ch),
        _section_graphe(chantier_id),
        _section_rapports(state, chantier_id),
        _section_capteur(ch),
        _section_decisions(chantier_id),
        _section_messages_humain(chantier_id),
    ]
    return "\n\n".join(sections) + "\n"
