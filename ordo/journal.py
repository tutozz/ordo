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

import json
import time
from contextlib import suppress
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


# ===========================================================================
# Journal machine (t-34) : un fait par ligne JSON, à côté du Markdown ci-dessus.
# ===========================================================================
#
# Le Markdown ci-dessus (write/read/brief) sert un lecteur : l'humain qui reprend un
# chantier à froid, et il en dit assez pour ça. Une campagne d'optimisation qui veut
# trier, compter ou croiser des faits (quel modèle tient quelle tâche, combien de
# tentatives avant succès, quelles dérives de périmètre) ne peut rien faire d'un
# paragraphe de prose sans le reparser au jugé. Ce qui suit écrit les MÊMES familles de
# faits que _tick_one() journalise déjà en texte libre (voir ordo/controle.py), mais
# sous une forme qu'une machine relit sans deviner : un objet JSON complet par ligne,
# ajouté au fichier .jsonl du chantier, jamais au .md qui reste inchangé.
#
# Le chantier n'est PAS revalidé à chaque appel (pas de store.load() ici) : les seuls
# appelants de ce module (controle.py) ont déjà résolu le chantier plus haut dans le
# même tick via _get_chantier(). Revalider à chaque événement coûterait une lecture de
# state.json par fait journalisé, pour une garantie que l'appelant a déjà.


def _evenements_path(chantier_id: str) -> Path:
    return store.home() / "journal" / f"{chantier_id}.jsonl"


def enregistrer_evenement(chantier_id: str, type_evenement: str, **champs: object) -> bool:
    """Ajoute un fait structuré, une ligne JSON autonome, au journal machine du chantier.

    Chaque ligne porte tout ce qu'il faut pour se comprendre seule, des mois plus tard,
    hors de son contexte : l'horodatage ("at"), le chantier, le type du fait, et les
    champs propres à ce type (ex. tache="t-34", item="c1" pour un critère coché). Aucune
    ligne ne renvoie à la précédente.

    Ne lève JAMAIS (exigence 2 du brief t-34, invariant le plus important de cette
    fonction) : un disque plein, un chemin verrouillé, illisible ou déjà pris par autre
    chose qu'un fichier ne doit jamais faire tomber une campagne pour la seule raison
    qu'on a voulu journaliser un fait -- ni un champ passé par erreur que JSON ne sait
    pas sérialiser (TypeError, ex. un set au lieu d'une liste). En échec, l'écriture
    échoue en silence pour l'appelant (renvoie False) mais SIGNALE la panne sur le canal
    Markdown déjà fiable, en best-effort ; si ce second canal échoue lui aussi, l'échec
    reste muet plutôt que de lever à son tour.
    """
    ligne = {"at": store.now(), "chantier": chantier_id, "type": type_evenement, **champs}
    try:
        texte = json.dumps(ligne, ensure_ascii=False) + "\n"
        with _evenements_path(chantier_id).open("a", encoding="utf-8") as f:
            f.write(texte)
        return True
    except (OSError, TypeError) as exc:
        with suppress(Exception):
            write(
                chantier_id, "ORDO",
                f"machine journal write failed ({type_evenement}): {exc}",
            )
        return False


def lire_evenements(chantier_id: str, type_evenement: str | None = None) -> list[dict]:
    """Reparse le journal machine du chantier en dicts, dans l'ordre d'écriture.

    Sans fichier, liste vide, jamais une erreur (même parti pris que read()). Une ligne
    corrompue (écriture interrompue par un crash en plein milieu du fichier) est ignorée
    plutôt que de faire échouer la relecture des lignes valides qui l'entourent.
    type_evenement, quand fourni, ne garde que les faits de ce type.

    Ne lève JAMAIS, même invariant que enregistrer_evenement() : un chemin devenu
    illisible (permissions changées, remplacé par un répertoire) rend une liste vide
    plutôt que de faire tomber l'appelant -- trouvé en pratique, un appelant qui relit ce
    fichier pour dédoublonner (voir controle.py, étape 7) le fait à chaque tick, jamais
    seulement à l'écriture.
    """
    path = _evenements_path(chantier_id)
    try:
        if not path.exists():
            return []
        contenu = path.read_text(encoding="utf-8")
    except OSError as exc:
        with suppress(Exception):
            write(chantier_id, "ORDO", f"machine journal read failed: {exc}")
        return []
    evenements: list[dict] = []
    for ligne in contenu.splitlines():
        if not ligne.strip():
            continue
        try:
            evt = json.loads(ligne)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            # JSON valide mais pas un objet (ex. un nombre ou une liste isolée sur sa
            # propre ligne) : ce n'est pas un fait au format attendu, on l'ignore comme
            # une ligne corrompue plutôt que de lever sur evt.get() plus bas.
            continue
        if type_evenement is not None and evt.get("type") != type_evenement:
            continue
        evenements.append(evt)
    return evenements


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
