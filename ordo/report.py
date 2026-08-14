"""Canal de rapport par fichier, parseur indulgent, application a l'etat.

Une executante n'a pas d'AskUserQuestion et son environnement ne se propage pas : le
seul canal fiable pour qu'elle communique son resultat est un fichier dont le chemin
lui est donne dans son brief (voir ordo/prompt.py). Ce module lit ce fichier, en
tolerant la prose et les deformations qu'un modele produit reellement, et applique le
resultat a l'etat d'Ordo.

Invariant I2, le plus important du module : un rapport ILLISIBLE bloque la tache, il
ne la laisse jamais passer pour reussie. parse() leve sur ce qu'il ne comprend pas ;
apply() encaisse cette levee et traduit en etat "blocked" avec la raison ecrite dans
la tache. Un parse() indulgent au point d'inventer un state: done par defaut serait
exactement le defaut que ce module existe pour empecher.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import store

# Etats qu'un rapport peut declarer. Toute autre valeur, ou son absence, est une
# raison de lever (I2) : ce module ne devine jamais un etat.
_VALID_STATES = frozenset({"done", "blocked", "asking", "progress"})

# Guillemets typographiques qu'un modele substitue parfois aux guillemets droits
# attendus par JSON. Remplacement global avant l'extraction : un guillemet courbe a
# l'interieur d'un texte francais (une apostrophe) redevient un guillemet droit valide
# de toute facon, donc ce remplacement ne peut pas corrompre un rapport par ailleurs
# correct.
_SMART_QUOTES = str.maketrans(
    {"“": '"', "”": '"', "‘": "'", "’": "'"}
)

# Une virgule finale avant une accolade ou un crochet fermant : invalide en JSON
# strict, frequent dans une sortie de modele.
_TRAILING_COMMA = re.compile(r",\s*([\]}])")

# Un bloc de code Markdown, avec ou sans l'etiquette de langage "json".
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class RapportError(Exception):
    """Leve quand parse() ne peut pas donner de sens au contenu brut d'un rapport.

    Ne doit jamais etre avalee en silence : apply() la capture uniquement pour la
    traduire en tache "blocked" (I2), jamais pour laisser passer un rapport qu'elle
    n'a pas compris.
    """


def path(task_id: str, chantier_id: str) -> Path:
    """Chemin du fichier de rapport d'une tache, sous reports/<chantier>/.

    Le chantier prefixe le chemin, il n'est pas decoratif : les identifiants de tache sont
    uniques par ORDO_HOME, mais un home partage entre plusieurs projets melangeait leurs
    rapports a plat et rien dans reports/t-01.json ne disait de quel projet il venait. Un
    ORDO_HOME reconstruit, ou un --shared-home, suffisait alors a faire lire a Ordo le
    rapport d'un autre chantier comme s'il etait le sien.

    Cree le sous-repertoire du chantier : une executante ecrit son rapport elle-meme, a un
    chemin qu'on lui a donne dans son brief, et n'a aucune raison de creer un repertoire
    d'Ordo. Meme parti pris que store.home(), qui cree eagerly son arbre de donnees.
    """
    dossier = store.home() / "reports" / chantier_id
    dossier.mkdir(parents=True, exist_ok=True)
    return dossier / f"{task_id}.json"


def read(task_id: str, chantier_id: str) -> dict | None:
    """Lit et parse le rapport d'une tache. None si le fichier n'existe pas encore.

    Si le fichier existe mais que son contenu est illisible, l'exception de parse()
    remonte telle quelle : read() ne la convertit pas en None, ce qui confondrait
    "pas encore ecrit" et "illisible", exactement la distinction dont apply() a
    besoin pour appliquer I2.
    """
    p = path(task_id, chantier_id)
    if not p.exists():
        return None
    return parse(p.read_text(encoding="utf-8"))


def clear(task_id: str, chantier_id: str) -> None:
    """Supprime le rapport d'une tache, sans erreur s'il n'existe pas.

    Utilise avant une relance sur le meme identifiant de tache : un rapport perime
    d'une tentative precedente ne doit jamais etre pris pour le signal du nouveau
    tour.
    """
    path(task_id, chantier_id).unlink(missing_ok=True)


def _extract_json_block(text: str) -> str:
    """Isole le premier objet JSON balance dans un texte qui peut contenir de la
    prose avant et apres, et un bloc Markdown avec ou sans etiquette de langage.

    Le parcours respecte les chaines JSON (une accolade a l'interieur d'une valeur
    ne compte pas) pour trouver l'accolade fermante qui correspond reellement a la
    premiere accolade ouvrante, plutot que de couper au premier "}" venu.
    """
    fence = _FENCE.search(text)
    if fence:
        text = fence.group(1).strip()

    start = text.find("{")
    if start == -1:
        raise RapportError("no JSON object found in the report")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    raise RapportError("unterminated JSON object: missing closing brace")


def _as_str_list(value: object, field: str) -> list[str]:
    """Normalise checked/touched : une chaine seule devient une liste a un element.

    Un modele qui ecrit "checked": "c1" au lieu de ["c1"] est une deformation
    plausible et anodine ; un type totalement inattendu (nombre, objet) ne l'est
    pas et doit lever plutot qu'etre devine.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise RapportError(f"field {field}: list of strings expected, got {value!r}")


def _as_text(value: object, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise RapportError(f"field {field}: string expected, got {value!r}")


def parse(raw: str) -> dict:
    """Parseur indulgent : encaisse la prose, la casse des cles, les virgules
    finales et les guillemets typographiques, mais ne comble jamais un champ state
    absent ou invalide (I2). Renvoie un dict a cles canoniques et minuscules :
    task, state, note, checked, question, touched.
    """
    if not raw or not raw.strip():
        raise RapportError("empty report")

    text = raw.translate(_SMART_QUOTES)
    block = _extract_json_block(text)
    block = _TRAILING_COMMA.sub(r"\1", block)

    try:
        data = json.loads(block)
    except json.JSONDecodeError as exc:
        raise RapportError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise RapportError(
            f"the report must be a JSON object, got {type(data).__name__}"
        )

    normalized = {str(k).strip().lower(): v for k, v in data.items()}

    state_raw = normalized.get("state")
    if not isinstance(state_raw, str) or state_raw.strip().lower() not in _VALID_STATES:
        raise RapportError(
            f"field state missing or invalid: {state_raw!r} "
            f"(expected one of {sorted(_VALID_STATES)})"
        )

    return {
        "task": normalized.get("task"),
        "state": state_raw.strip().lower(),
        "note": _as_text(normalized.get("note"), "note"),
        "checked": _as_str_list(normalized.get("checked"), "checked"),
        "question": normalized.get("question"),
        "touched": _as_str_list(normalized.get("touched"), "touched"),
    }


def _tick_checklist(task: dict, checked: list[str]) -> list[str]:
    """Coche les items listes ; un identifiant inconnu est ignore, jamais fatal.

    Une executante qui se trompe d'identifiant de case ne doit pas faire echouer
    tout le reste de l'application du rapport (I2 concerne la lisibilite du JSON,
    pas la validite de chaque reference interne).
    """
    ids = {item["id"] for item in task["checklist"]}
    applied = []
    for item_id in checked:
        if item_id not in ids:
            continue
        for item in task["checklist"]:
            if item["id"] == item_id:
                item["done"] = True
                applied.append(item_id)
    return applied


def apply(state: dict, task_id: str, raw: str) -> list[str]:
    """Applique le contenu brut d'un rapport a la tache task_id, mute state en place.

    Ne verrouille jamais l'etat lui-meme (meme motif que chantier.propagate_failures) :
    l'appelant est cense deja detenir store.locked() pour regrouper plusieurs effets
    dans une seule ecriture atomique, typiquement le tick() decrit dans SPEC.md.
    Reappeler store.locked() ici interbloquerait ce cas d'usage.

    parse() est invoquee ICI, jamais avant : c'est ce qui garantit I2 sans dependre
    d'un appelant externe pour traduire la levee. Un rapport illisible bloque
    directement la tache, avec la raison ecrite dans task["error"].
    """
    task = state["taches"][task_id]

    try:
        report = parse(raw)
    except RapportError as exc:
        task["state"] = "blocked"
        task["error"] = f"unreadable report: {exc}"
        task["lastReportAt"] = store.now()
        return [f"{task_id} blocked: unreadable report ({exc})"]

    events: list[str] = []
    task["lastReportAt"] = store.now()
    task["report"] = report
    task["notes"].append(
        {"at": task["lastReportAt"], "state": report["state"], "note": report["note"]}
    )

    applied = _tick_checklist(task, report["checked"])
    for item_id in applied:
        events.append(f"{task_id}: item {item_id} checked")
    ignored = set(report["checked"]) - set(applied)
    for item_id in ignored:
        events.append(f"{task_id}: item {item_id} unknown, ignored")

    if report["state"] == "done":
        task["state"] = "done"
        task["finishedAt"] = task["lastReportAt"]
        task["error"] = None
        events.append(f"{task_id} done")
    elif report["state"] == "blocked":
        task["state"] = "blocked"
        task["error"] = report["note"]
        events.append(f"{task_id} blocked: {report['note'] or 'no note provided'}")
    elif report["state"] == "asking":
        task["state"] = "waiting"
        events.append(f"{task_id} waiting for an answer: {report['question']}")
    else:  # "progress" : simple avancement, aucune transition d'etat
        events.append(f"{task_id} progressing: {report['note'] or 'no note'}")

    return events
