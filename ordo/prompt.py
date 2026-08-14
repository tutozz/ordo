"""Composition des briefs d'executante et rappel de role de l'orchestratrice.

Le partage est net. L'ORCHESTRATRICE donne le fond : l'objectif, le perimetre et les
pieges vivent deja dans le chantier et la tache, ecrits avant de deleguer. ORDO assemble le
reste : le protocole de rapport, le chemin exact du fichier de rapport, la checklist a
cocher, les zones declarees et les interdits.

Ce module n'importe ni report.py ni capteur.py (developpes en parallele, hors perimetre) :
le chemin du fichier de rapport est derive directement de store.home(), la convention
`reports/<chantier>/<tache>.json` etant fixee par SPEC.md section 2, pas par
l'implementation interne de report.py.
"""

from __future__ import annotations

from pathlib import Path

from . import chantier, store


def _resolve(state: dict, task_id: str) -> tuple[dict, dict]:
    task = state["taches"].get(task_id)
    if task is None:
        raise chantier.ChantierError(f"task not found: {task_id}")
    ch = state["chantiers"].get(task["chantier"])
    if ch is None:
        raise chantier.ChantierError(
            f"campaign not found for task {task_id}: {task['chantier']}"
        )
    return task, ch


def _format_note(n: object) -> str:
    """Une ligne lisible par note du schema {"at", "state", "note"} (SPEC.md section 3).

    Tolerant au format : une note qui ne serait qu'une simple chaine (jamais produite
    par report.apply(), mais rien n'interdit a un autre appelant d'en ecrire une un
    jour) s'affiche telle quelle plutot que de faire planter le brief.
    """
    if isinstance(n, dict):
        at = n.get("at", "?")
        etat = n.get("state", "?")
        texte = n.get("note", "")
        return f"- {at}  [{etat}]  {texte}"
    return f"- {n}"


def _section_entete(task_id: str, task: dict, ch: dict, report_path: Path) -> str:
    """En-tete de situation (point H du contrat), avant meme la section objectif.

    Les briefs sont ranges par chantier (briefs/<chantier>/<tache>.md), mais le chemin
    seul ne suit pas le fichier quand une executante le lit : cet en-tete nomme le
    chantier, le projet, le repertoire de travail reel, la session tmux et le chemin
    absolu du rapport attendu.

    task["cwd"] peut valoir None tant que la tache n'a pas ete lancee : dans ce cas,
    le repertoire affiche est celui du chantier, la valeur effectivement utilisee au
    lancement (voir cli.py, _do_launch : cwd = task.get("cwd") or ch["project"]).
    """
    cwd = task.get("cwd") or ch["project"]
    return (
        f"# Executor {task_id}, campaign {ch['id']}\n\n"
        "You are a real Claude Code session, launched in a tmux pane by Ordo. You are not a\n"
        "sub-agent: you have your own context, your own cost, and you talk to no one\n"
        "but your orchestrator, through the report file described below.\n\n"
        "| | |\n"
        "|---|---|\n"
        f"| Project | {ch['project']} |\n"
        f"| Working directory | {cwd} |\n"
        f"| tmux session | {ch['tmuxSession']} |\n"
        f"| Your report | {report_path} |"
    )


def _section_fond(task: dict, ch: dict) -> str:
    # Le fond est ce que l'orchestratrice a deja ecrit en demarrant le chantier et en
    # ajoutant la tache : objectif, perimetre, ce qui est explicitement hors scope (les
    # pieges qu'elle a identifies a l'avance), et le prompt propre a cette tache.
    lignes = [
        "## Campaign goal",
        ch["objectif"],
        "",
        "## Scope",
        ch["perimetre"] or "not specified",
        "",
        "## Out of scope, known pitfalls",
        ch["horsScope"] or "not specified",
        "",
        "## Your task",
        task["prompt"],
    ]
    if task.get("attempts", 0) > 0:
        lignes += [
            "",
            f"Warning: {task['attempts']} previous attempt(s) on this task "
            "failed or were blocked. Read the notes below before starting over.",
        ]
    if task.get("notes"):
        lignes += ["", "## Previous notes"]
        lignes += [_format_note(n) for n in task["notes"]]
    return "\n".join(lignes)


def _section_zones(task: dict) -> str:
    touches = task.get("touches") or []
    if not touches:
        contenu = "(no declared zone, stay within the campaign's scope)"
    else:
        contenu = "\n".join(f"- {z}" for z in touches)
    return "## Declared zones\nYou touch only this:\n" + contenu


def _section_checklist(task: dict) -> str:
    checklist = task.get("checklist") or []
    if not checklist:
        return "## Checklist\n(no item to check for this task)"
    lignes = [f"- {item['id']}: {item['label']}" for item in checklist]
    return (
        "## Checklist\n"
        "In your report, field \"checked\", tick the identifiers you actually satisfy "
        "(a wrongly ticked item blocks the tasks that depend on it):\n"
        + "\n".join(lignes)
    )


def _section_protocole_rapport(task_id: str, report_path: Path) -> str:
    return (
        "## Report protocol\n"
        "At the end of your turn, or as soon as you are blocked or need to ask "
        "something, WRITE this file, absolute path, whether it already exists or "
        "not:\n"
        f"{report_path}\n\n"
        "Exact JSON format, exactly once, nothing else around it:\n"
        "{\n"
        f'  "task": "{task_id}",\n'
        '  "state": "done",\n'
        '  "note": "one factual sentence about what you did",\n'
        '  "checked": ["c1"],\n'
        '  "question": null,\n'
        '  "touched": ["path/actually/modified"]\n'
        "}\n\n"
        '"state" is exactly one of done, blocked, asking or progress. Nothing else.'
    )


def _section_interdits() -> str:
    return (
        "## Prohibitions\n"
        "1. AskUserQuestion does not exist in your session: the tool is absent "
        "outside interactive mode, you look for it, you do not find it, you give up "
        "without delivering anything. If you need to ask something, write "
        "\"state\": \"asking\" in your report, a clear question in \"question\", and "
        "END YOUR TURN. Never look for AskUserQuestion.\n"
        "2. You never self-validate. You never declare your own work compliant with "
        "the intent: you report verifiable facts and you tick only what you have "
        "actually verified.\n"
        "3. Nothing irreversible or external without asking through the report: no "
        "remote push, no deployment, no spending money, no secret handled. This is "
        "the last barrier: an undecided graph moves on automatically after 45 "
        "seconds, that is not implicit authorization."
    )


def brief_executante(task_id: str) -> str:
    """Compose et ecrit le brief d'une executante dans briefs/<chantier>/<tache>.md.

    Retourne le chemin ABSOLU du fichier ecrit : une executante tourne dans le repertoire
    du projet pilote, pas dans le dossier d'Ordo, et un chemin relatif ne resout nulle
    part. C'est le defaut exact qu'une vraie session du projet d'origine a signale
    ("je ne peux executer aucune commande Ordo").
    """
    state = store.load()
    task, ch = _resolve(state, task_id)

    report_path = Path(store.canon(store.home() / "reports" / ch["id"] / f"{task_id}.json"))
    brief_path = Path(store.canon(store.home() / "briefs" / ch["id"] / f"{task_id}.md"))

    sections = [
        _section_entete(task_id, task, ch, report_path),
        _section_fond(task, ch),
        _section_zones(task),
        _section_checklist(task),
        _section_protocole_rapport(task_id, report_path),
        _section_interdits(),
    ]
    contenu = "\n\n".join(sections) + "\n"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    # Le repertoire de rapport est cree ici, avant que le brief ne parte : l'executante
    # ecrit ce fichier elle-meme et n'a aucune raison de creer un repertoire d'Ordo.
    report_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(contenu, encoding="utf-8")
    return str(brief_path)


def contrat_role() -> str:
    """Rappel du role de l'orchestratrice, injecte au demarrage d'un chantier.

    Fonction statique, sans effet de bord : elle ne lit ni n'ecrit rien sur disque.
    """
    return (
        "## Your role in this campaign\n"
        "You are the orchestrator. You hold the goal, the scope, the task graph "
        "and the alignment. You do not read code and you produce nothing: you "
        "delegate to real executors in tmux panes, you read them, you correct "
        "their course.\n\n"
        "Three invariants:\n"
        "1. Alignment is captured before delegating.\n"
        "2. It is maintained throughout: you re-read every report, you do not "
        "discover drift at the end.\n"
        "3. Proof outranks declaration: an executor that says it is done is not "
        "done.\n\n"
        "You never self-validate: you do not validate your own graph, an "
        "alignment guarantor that self-validates guarantees nothing anymore. "
        "Validation belongs to the human, or to auto-acceptance after 45 "
        "seconds.\n\n"
        "You escalate to the human only what belongs to the human: architecture "
        "decisions, business calls, money, irreversible or external action, "
        "scope drift, information only the human holds. Everything else, you "
        "decide."
    )
