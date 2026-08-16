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

import shlex
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


def _ligne_duree(task_id: str, item: dict, home: str) -> str:
    """Demande la révision de l'estimation, item par item, plutôt que de seulement la
    permettre (brief t-27, c9).

    Postée juste au-dessus de la commande --doing déjà obligatoire de CE critère, jamais
    dans un paragraphe à part : trois exécutantes sur trois ont ignoré un geste facultatif
    documenté item par item quand il vivait loin de l'action qu'elles devaient déjà faire
    (voir --doing avant que ce brief ne l'explique ici même). Une estimation posée avant
    de lire le code est fausse ; celle posée après l'avoir lu vaut quelque chose.
    """
    duree = item.get("dureeMin")
    commande = f"ORDO_HOME={home} ordo checklist duree {task_id} {item['id']} <minutes>"
    if duree is None:
        return f"no duration estimate yet — set one now that you've read the code: {commande}"
    return f"estimate: {duree} Claude-minute(s) — correct it now if wrong, having read the code: {commande}"


def _section_checklist(task_id: str, task: dict) -> str:
    """Dit de cocher tout de suite, commande prête à copier, avant de rappeler le filet.

    L'ancien texte ne mentionnait la checklist qu'au moment du rapport final : rien ne
    bougeait avant, personne ne voyait l'avancement pendant que la tâche tournait.
    ORDO_HOME devant la commande, shlex.quote sur le home, même raison qu'à
    carte.py:731-734 : un home peut contenir une espace, et une commande affichée puis
    copiée telle quelle doit marcher au premier essai.

    `--doing` est expliqué AVANT la liste des items à cocher, pas après : sans lui, une
    exécutante n'utilise jamais un verbe qu'on ne lui a pas donné (elle ne le devine pas),
    et l'humain qui regarde la carte ne voit qu'un compteur ("checks 2/5") qui n'avance
    qu'à la toute fin de chaque item, sans jamais dire sur quoi la session travaille entre
    deux coches ni si elle avance ou si elle patine.
    """
    checklist = task.get("checklist") or []
    if not checklist:
        return "## Checklist\n(no item to check for this task)"
    home = shlex.quote(str(store.home()))
    lignes = [
        f"- {item['id']}: {item['label']}\n"
        f"  {_ligne_duree(task_id, item, home)}\n"
        f"  when you start it: ORDO_HOME={home} ordo check {task_id} {item['id']} --doing\n"
        f"  when it is true:   ORDO_HOME={home} ordo check {task_id} {item['id']}"
        for item in checklist
    ]
    return (
        "## Checklist\n"
        "Two commands per item, both written out below: one when you START it, one when "
        "it is TRUE. Run the first one before touching an item - it is not optional and it "
        "is not a report. The human watches a card, and between two ticks that card can "
        "either name what you are doing right now or say nothing at all; without --doing it "
        "says nothing, and twenty silent minutes look exactly like a session that died.\n\n"
        "Tick each item AS SOON AS you have actually verified it, immediately, do not "
        "wait for the end of the task (a wrongly ticked item blocks the tasks that depend "
        "on it):\n"
        + "\n".join(lignes)
        + "\n\nThe report's \"checked\" field, described below, still stands at the end "
        "as a safety net: ticking the same item twice has no effect.\n\n"
        + _section_checklist_evolution(task_id, home)
    )


def _section_checklist_evolution(task_id: str, home: str) -> str:
    """La part du brief qui ouvre a l'executante trois gestes sur SA propre checklist,
    et lui dit sans ambiguite ce qu'aucun des trois ne fait (brief t-22).

    Un verbe qu'on ne donne pas a l'executante n'est jamais utilise, elle ne le devine
    pas -- c'est exactement ce qui est arrive au verbe check lui-meme avant que ce brief
    ne l'explique. Le refus de suppression est dit ICI, au moment ou l'idee de retirer un
    critere genant pourrait naitre, pas seulement laisse a l'absence d'un verbe dans
    l'aide : le contrat interdit formellement de se declarer finie en retirant un critere.
    """
    return (
        "It is working that reveals a criterion was really two, or that a whole piece of "
        "work was never planned for. You can grow this checklist yourself:\n"
        f'- a piece of work was not planned for: `ordo checklist add {task_id} "<label>"`\n'
        f'- a criterion turns out to be two: `ordo checklist split {task_id} <item-id> '
        '"<label one>" "<label two>"` (the original id keeps the first label, the second '
        "gets a fresh id)\n"
        f'- a label turns out wrong or too broad: `ordo checklist reword {task_id} '
        '<item-id> "<new label>"` (same id, same checked state)\n'
        f'- an estimate is missing or turns out wrong, once you have read the code: '
        f'`ordo checklist duree {task_id} <item-id> <minutes>` (Claude-minutes, same id, '
        'same label, same checked state)\n\n'
        "What you CANNOT do, on purpose: remove a criterion. There is no verb for it, and "
        "there never will be for you -- an executor able to drop a criterion could declare "
        "itself done by dropping the one in its way, and the contract formally forbids "
        "self-validation. If a criterion turns out impossible or off-topic, say so in your "
        "report (\"blocked\" or \"asking\"), never make it disappear; the orchestrator is "
        "the one who decides."
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
        "3. NEVER run `git stash`, `git checkout -- <path>`, `git reset`, `git clean` "
        "or `git add -A`. You are not alone in this repository: other Claude Code "
        "sessions are writing to the same working tree at the same time, and none of you "
        "is isolated in a git worktree. Those commands act on the WHOLE tree, so they "
        "silently take away work that other sessions have written and not yet committed. "
        "This has already happened here: one `git stash` removed the delivered work of "
        "four finished tasks and three other sessions from the tree, and nothing "
        "signalled it -- the code still imported, the suite still passed. A module "
        "restored to HEAD looks exactly like a module that never changed. If you need a "
        "clean state to test something, copy the file aside; never rewind the tree. "
        "`git add <specific paths>` is fine, `git add -A` is not.\n"
        "4. NEVER run `tmux kill-server`, `tmux kill-session`, or any tmux command "
        "without an explicit target. You are yourself running inside a tmux pane, and so "
        "is every other live executor, across every campaign on this machine - they all "
        "share one tmux server. `kill-server` therefore destroys every session at once, "
        "including your own, mid-sentence, with no report written by anyone. This has "
        "already happened here: one such command killed three executors of this campaign "
        "plus the live sessions of two other campaigns, and the only trace was three "
        "panes going dead at the same second. If you need a pane to experiment on, create "
        "it yourself and kill it BY ITS OWN ID (`tmux kill-pane -t %<id>`), never by "
        "server, never by session.\n"
        "5. Nothing irreversible or external without asking through the report: no "
        "remote push, no deployment, no spending money, no secret handled. This is "
        "the last barrier: an undecided graph moves on automatically after 45 "
        "seconds, that is not implicit authorization."
    )


def _section_discipline_contexte() -> str:
    """La section qui empeche une executante de noyer sa propre conversation.

    Chiffree, parce qu'une consigne sans sa raison se contourne des que l'executante
    croit bien faire. Mesure sur soixante transcripts reels de ce depot : Bash apporte
    47 % de ce qui entre dans le contexte d'une session, Read 32 %, et chaque jeton entre
    est ensuite RELU CENT FOIS -- une sortie de test deversee en entier n'est pas payee
    une fois, elle est payee a chaque tour restant de la session.
    """
    return (
        "## Context discipline\n"
        "Every token you put in this conversation is re-read on every later turn of "
        "this session - measured at about 100 times over a real session. A test log "
        "pasted in full is not paid once, it is paid on every turn that follows. This "
        "is not about tidiness, it is the single biggest cost in your session.\n\n"
        "1. Long command output goes to a file, not into the conversation. Run "
        "`npm test > /tmp/out.log 2>&1; tail -40 /tmp/out.log`, then grep that file for "
        "what you actually need. Never let a build, a test suite, an install or a "
        "`git log` print hundreds of lines here.\n"
        "2. Read files in targeted slices, with `offset` and `limit`. Read a whole file "
        "only when you genuinely need the whole file, and never twice: if you already "
        "read it in this session, it is still in your context.\n"
        "3. Search before you read. `grep -n` a pattern and read around the hits, "
        "rather than loading a large file to find one function.\n"
        "4. When a command fails, print the error, not the whole run."
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
        _section_checklist(task_id, task),
        _section_protocole_rapport(task_id, report_path),
        _section_discipline_contexte(),
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
