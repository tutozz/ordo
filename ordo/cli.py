"""Dispatch des verbes du CLI ordo : argparse, sortie humaine et JSON, erreurs lisibles.

Sans ce module tous les autres sont inertes : une orchestratrice est une session Claude
Code, et sans commandes elle n'a aucun moyen d'agir. Ce fichier ne porte aucune regle
metier propre ; il compose les fonctions deja ecrites dans store, chantier, panes, report,
capteur, plan, journal et prompt, et gere trois choses qui n'appartiennent a aucun d'eux :

- la resolution tache -> chantier partagee par les verbes d'execution et de signal ;
- le registre des questions (state["questions"]), que SPEC.md decrit dans le schema mais
  qu'aucun module ordo/*.py existant ne cree ni ne lit (voir CliError plus bas) ;
- I12 : chaque verbe de lecture sert un signal filtre, jamais l'etat brut. En particulier
  la sortie d'un chantier ne reexpose jamais son champ "capteur" tel quel ; elle passe
  systematiquement par capteur.status(), la seule fonction qui applique la regle
  d'adoption avant de laisser fuiter une mesure.

ordo/controle.py est ecrit en parallele par un autre agent et peut etre absent au moment ou
ce fichier est charge : seul cmd_tick() l'importe, et seulement au moment de l'appel, pour
que tout le reste du CLI reste utilisable meme sans lui. panes.py est ecrit en parallele lui
aussi : ce fichier code contre les signatures annoncees par le contrat de publication
(ensure_session() -> (session, window_id), kill_session(), set_verbose()) meme quand elles
ne sont pas encore sur le disque ; les appels vers ce qui n'existe pas encore sont proteges
quand une degradation silencieuse est preferable a un crash de tout le CLI (voir
_apply_verbose), jamais quand l'appel est au coeur du geste demande (ensure_session,
kill_session : leur absence doit faire echouer bruyamment l'appelant, pas se taire).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

from . import __version__, capteur, chantier, journal, panes, plan, prompt, report, store


class CliError(Exception):
    """Refus explicite pris par le dispatch CLI lui-meme, jamais par un module metier.

    Reservee a ce qu'aucun module de la spec ne porte : resoudre une tache vers son
    chantier, decider si une tache est eligible au lancement, gerer le registre des
    questions. Tout le reste leve deja sa propre exception (ChantierError, CapteurError,
    PlanError, RapportError) : la reutiliser ici brouillerait quel module a refuse quoi.
    """


# ---------------------------------------------------------------------------
# Aides partagees
# ---------------------------------------------------------------------------


# Point G du contrat de publication : "skip" reproduit le comportement historique
# (--dangerously-skip-permissions ajoute sans option, sans consentement) ; "normal"
# n'ajoute rien. Nomme skip/normal plutot que True/False : un booleen ne dit pas dans
# quel sens il va, ces deux chaines si.
PERMISSIONS_SKIP = "skip"
PERMISSIONS_NORMAL = "normal"
PERMISSIONS_CHOICES = (PERMISSIONS_SKIP, PERMISSIONS_NORMAL)
DEFAULT_PERMISSIONS = PERMISSIONS_SKIP

# Texte affiche en clair a chaque lancement (decision G : "jamais tue"), cle par la
# valeur retenue plutot que reconstruit a la volee, pour que le mot exact qui apparait
# dans la commande shell (--dangerously-skip-permissions) ne puisse jamais diverger du
# mot affiche a l'humain.
_PERMISSIONS_LABEL = {
    PERMISSIONS_SKIP: "skip (--dangerously-skip-permissions)",
    PERMISSIONS_NORMAL: "normal (no option added)",
}


def _print_json(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _sans_capteur(ch: dict) -> dict:
    """Copie d'un chantier sans son champ "capteur" brut.

    Sert a chaque verbe qui affiche un chantier entier : le champ brut porte
    lastSuccess avant meme l'adoption humaine, exactement ce que I12 interdit de
    servir. L'appelant qui veut un signal capteur passe par capteur.status().
    """
    return {k: v for k, v in ch.items() if k != "capteur"}


def _resolve_task(state: dict, task_id: str) -> tuple[dict, dict]:
    task = state["taches"].get(task_id)
    if task is None:
        raise CliError(f"task not found: {task_id}")
    ch = state["chantiers"].get(task["chantier"])
    if ch is None:
        raise CliError(f"campaign not found for task {task_id}: {task['chantier']}")
    return task, ch


def _derniere_ligne_utile(pane_id: str) -> str:
    texte = panes.capture(pane_id, lines=10)
    for ligne in reversed(texte.splitlines()):
        if ligne.strip():
            return ligne.strip()
    return ""


def _pane_title(task: dict) -> str:
    """Titre lisible affiche en permanence sur la bordure du pane (pane-border-format).

    Porte l'identifiant ET le titre de la tache (ex. "t-03 migration du schema"), jamais
    l'un sans l'autre : l'identifiant seul oblige a traduire mentalement, le titre seul ne
    dit plus quelle tache regarder si deux portent un nom proche. C'est un texte affiche
    par tmux, jamais un identifiant de ciblage (I5) : aucune commande de ce fichier ne
    prend un pane pour cible par ce texte, uniquement par son pane_id.
    """
    return f"{task['id']} {task['titre']}"


def _attach_command(session: str) -> str:
    """Commande exacte a taper pour s'attacher a la session d'un chantier (decisions I/K).

    Prefixee de "env -u TMUX " quand la variable d'environnement TMUX est deja definie :
    tmux refuse par defaut une attache imbriquee ("sessions should be nested with care"),
    et toute pane, meme dans une session detachee, porte deja TMUX dans son environnement
    (skill/references/tmux.md section 11). Sans ce prefixe la commande affichee a un
    humain attache depuis une exécutante echouerait a la lettre.
    """
    base = f"tmux attach -t {session}"
    if os.environ.get("TMUX"):
        return f"env -u TMUX {base}"
    return base


def _avertissement_multi_projet(state: dict) -> str | None:
    """Alerte quand ce ORDO_HOME porte des chantiers OUVERTS sur des projets differents.

    Point I4 du contrat : n'est jamais un refus, seulement une annonce en tete de sortie
    de tick/poll/doctor. La vraie parade reste l'isolation par ORDO_HOME (un home par
    projet, decision D) ; cette fonction ne change aucun comportement au-dela d'afficher
    la ligne quand la situation se produit.
    """
    ouverts = [c for c in state["chantiers"].values() if c["state"] == "open"]
    projets = sorted({c["project"] for c in ouverts})
    if len(projets) <= 1:
        return None
    return (
        "WARNING: this ORDO_HOME carries open campaigns on different projects "
        f"({', '.join(projets)}); recommended convention: one ORDO_HOME per "
        "project (export ORDO_HOME=$PWD/.ordo)"
    )


def _trace_tmux(command: list[str]) -> None:
    """Callback installe sur panes.TRACE par --verbose (point L).

    panes.py appelle ce callback depuis son unique choke-point interne (_tmux()) avec la
    commande complete passee a subprocess (ex. ["tmux", "attach", "-t", "session"]) :
    c'est panes.py qui expose le mecanisme (panes.TRACE, None par defaut, voir son
    commentaire au-dessus de la constante), cli.py se contente de le brancher.
    """
    print("tmux> " + " ".join(command), file=sys.stderr)


def _apply_verbose(verbose: bool) -> None:
    """Branche --verbose sur panes.TRACE (point L), jamais l'inverse.

    Sans le drapeau, cette fonction ne touche a rien : panes.TRACE reste a sa valeur par
    defaut (None) et tout se comporte "exactement comme si cette constante n'existait
    pas" (commentaire de panes.py). getattr() protege l'appel au cas ou panes.py, ecrit
    en parallele, n'exposerait pas encore TRACE au moment ou ce fichier est charge : le
    reste du CLI doit rester utilisable meme sans lui, meme raisonnement que cmd_tick
    pour ordo/controle.py.
    """
    if not verbose:
        return
    if hasattr(panes, "TRACE"):
        panes.TRACE = _trace_tmux


def _build_launch_cmd(
    cwd: str, model: str | None, permissions: str, session_id: str | None = None
) -> str:
    """Commande d'executante, gabarit impose par SPEC.md section 5 (ordo/panes.py).

    Le brief part par fichier, jamais par stdin ni en positionnel (I4) : cette commande
    ne porte aucun prompt, seulement le lancement interactif de claude. send() injecte
    la consigne de lecture du brief une fois le pane pret, dans _do_launch ci-dessous.

    permissions vaut "skip" (comportement historique, ajoute
    --dangerously-skip-permissions) ou "normal" (n'ajoute rien) -- decision G du contrat
    de publication : le flag n'est plus code en dur, sans option ni consentement.

    Verifie la presence du binaire claude AVANT de construire quoi que ce soit
    (shutil.which) : appelee par _do_launch avant tout appel a panes.ensure_session(),
    pour qu'un claude absent ne laisse jamais trainer une session tmux vide creee pour
    rien (voir _do_launch).
    """
    if shutil.which("claude") is None:
        raise CliError(
            "claude binary not found in PATH; install Claude Code "
            "(https://claude.com/claude-code) before launching an executor"
        )
    cmd = f"cd {shlex.quote(cwd)} && claude"
    # L'identifiant de session est IMPOSE, jamais devine apres coup. Le deduire en
    # cherchant le transcript le plus recent du repertoire serait ambigu des que deux
    # executantes du meme chantier tournent dans le meme projet, ce qui est le cas
    # nominal. Verifie a la main le 11 aout 2026 : claude --session-id <uuid> ecrit bien
    # ~/.claude/projects/<cwd encode>/<uuid>.jsonl, et claude --resume <uuid> retrouve
    # ensuite le contexte de ce tour.
    if session_id:
        cmd += f" --session-id {shlex.quote(session_id)}"
    if permissions == PERMISSIONS_SKIP:
        cmd += " --dangerously-skip-permissions"
    if model:
        cmd += f" --model {shlex.quote(model)}"
    cmd += " < /dev/null"
    return cmd


def _build_resume_cmd(cwd: str, session_id: str, permissions: str) -> str:
    """Commande de REPRISE d'une executante deja moissonnee.

    Reprendre coute cher : la session recharge tout son contexte, et c'est exactement ce
    qu'il faut eviter quand une tache neuve suffirait. Ordo ne le fait donc jamais tout
    seul, seulement sur le verbe resume, qui l'annonce.
    """
    if shutil.which("claude") is None:
        raise CliError(
            "claude binary not found in PATH; install Claude Code "
            "(https://claude.com/claude-code) before resuming an executor"
        )
    cmd = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}"
    if permissions == PERMISSIONS_SKIP:
        cmd += " --dangerously-skip-permissions"
    cmd += " < /dev/null"
    return cmd


def _record_window(chantier_id: str, window_id: str) -> None:
    """Persiste chantier["tmuxWindow"] la premiere fois qu'ensure_session() le revele.

    chantier.py n'importe jamais panes.py (decision E) : chantier.start() ne peut pas
    connaitre ce window_id des la creation, un vrai appel tmux est necessaire pour
    l'obtenir. cli.py, seul module a toucher panes.py, le reporte donc sur le chantier au
    premier lancement qui cree reellement la fenetre -- decision B, cible stable pour
    tout ce qui agit sur la fenetre plutot que la session (qui suit l'onglet courant).
    """
    with store.locked() as locked_state:
        ch = locked_state["chantiers"].get(chantier_id)
        if ch is not None and ch.get("tmuxWindow") != window_id:
            ch["tmuxWindow"] = window_id


def _record_pane_outcome(
    task_id: str,
    pane_id: str,
    cwd: str,
    model: str | None,
    state: str,
    error: str | None,
    session_id: str | None = None,
) -> None:
    """Commit the outcome of one spawn attempt to state.json.

    Shared by both endings of _do_launch below: the TUI came up and the
    brief was sent ("running", error=None), or the trust dialog appeared
    and nothing was sent ("blocked", error=raison). Both really spawned a
    process and own a real pane_id, so both record the same fields; only
    the resulting state and the reason differ.

    blockedCause is always reset to None here, even on the "blocked" ending: a launch or
    relaunch attempt is a fresh tour, never a propagation from another task's failure. A
    relaunch is explicitly allowed on a task blocked by chantier.propagate_failures() (see
    eligible_states in cmd_relaunch); without this reset, its stale blockedCause would
    survive the attempt, and a later own-reason block (e.g. pane mort) would be wrongly
    read as a propagation by chantier.unblock_propagated() on the next tick.
    """
    with store.locked() as locked_state:
        locked_task, _ = _resolve_task(locked_state, task_id)
        locked_task["state"] = state
        locked_task["paneId"] = pane_id
        locked_task["cwd"] = cwd
        locked_task["model"] = model
        locked_task["startedAt"] = store.now()
        locked_task["finishedAt"] = None
        locked_task["error"] = error
        locked_task["blockedCause"] = None
        # Ecrase l'identifiant du tour precedent : une relance cree une session claude
        # NEUVE, et garder l'ancien ferait reprendre un contexte qui n'est plus celui du
        # pane vivant. Le transcript de l'ancienne reste sur disque, rien n'est perdu.
        locked_task["claudeSessionId"] = session_id
        locked_task["reaped"] = False
        locked_task["attempts"] += 1


def _do_launch(
    task_id: str,
    model: str | None,
    permissions: str | None,
    eligible_states: tuple[str, ...],
    verb_label: str,
    force: bool,
    require_ready: bool,
) -> dict:
    """Coeur partage de launch et relaunch : brief, session, pane, injection.

    Renvoie desormais un dict {"paneId", "brief", "session", "window", "attach",
    "permissions", "title"} (decision I) plutot qu'un tuple. Les six premieres cles sont
    exactement celles annoncees par le contrat de publication et servies telles quelles
    en --json par cmd_launch/cmd_relaunch ; "title" est un extra interne (jamais servi en
    JSON), uniquement pour afficher le titre du pane a cote de son pane_id en sortie
    humaine (voir _print_launch_result).

    permissions surcharge, pour cette seule tentative, la valeur stockee sur le chantier
    (chantier["permissions"], decision G) ; None laisse le defaut du chantier decider.

    require_ready n'existe que pour launch : une tache relancee a deja ete lancee une
    premiere fois, donc ses dependances etaient deja satisfaites a ce moment-la : les
    revalider ici ferait dependre une relance de l'etat *actuel* du graphe, qui a pu
    changer entre-temps sans rapport avec la raison du blocage.

    _build_launch_cmd() est appelee AVANT panes.ensure_session() : elle verifie la
    presence du binaire claude, et un claude absent ne doit jamais laisser trainer une
    session tmux vide creee pour rien avant que l'echec ne soit connu.

    Entre le spawn et l'injection, panes.wait_ready() attend que le TUI de claude soit
    reellement pret a recevoir du texte (le premier defaut observe : la consigne partait
    avant que le TUI existe et se perdait). Si le pane affiche a la place le dialogue de
    confiance tmux, AUCUNE touche n'est envoyee (ni Entree, ni Escape) : Ordo n'approuve
    jamais un dossier automatiquement, c'est une decision produit explicite. La tache est
    marquee bloquee avec une raison lisible par un humain, et cette fonction leve pour
    sortir en code non nul ; controle.wake_reasons() la remonte comme motif de reveil
    distinct d'un pane mort (voir _is_confiance_block dans controle.py).
    """
    state = store.load()
    task, ch = _resolve_task(state, task_id)
    if task["state"] not in eligible_states and not force:
        raise CliError(
            f"{verb_label} refused: {task_id} is in state {task['state']}, "
            f"expected one of {', '.join(eligible_states)} (--force to override)"
        )
    if require_ready and not force:
        prets = {t["id"] for t in chantier.ready(task["chantier"])}
        if task_id not in prets:
            raise CliError(
                f"launch refused: {task_id} is not ready (unmet dependencies "
                f"or incomplete checklist); see "
                f"'ordo ready {task['chantier']} --why', or --force to override"
            )

    # Une relance sur le meme identifiant de tache ne doit jamais lire le rapport
    # perime d'une tentative precedente comme s'il etait le signal du nouveau tour.
    report.clear(task_id, task["chantier"])

    brief_path = prompt.brief_executante(task_id)
    resolved_model = model or task.get("model")
    resolved_permissions = permissions or ch.get("permissions") or DEFAULT_PERMISSIONS
    cwd = task.get("cwd") or ch["project"]

    session_id = str(uuid.uuid4())
    cmd = _build_launch_cmd(cwd, resolved_model, resolved_permissions, session_id)

    # label renomme la fenetre au nom du chantier (son slug) plutot que de laisser tmux
    # lui donner le nom par defaut d'un shell ; voir ensure_session() pour pourquoi
    # c'est idempotent de le reappliquer a chaque lancement.
    session, window_id = panes.ensure_session(ch["tmuxSession"], label=ch["slug"])
    _record_window(ch["id"], window_id)
    # title est reaffecte a CHAQUE appel, que ce spawn() reutilise le pane germe ou en
    # splitte un nouveau : c'est ce qui empeche un pane recycle de garder le nom de
    # l'ancienne tache (voir spawn() dans panes.py).
    title = _pane_title(task)
    pane_id = panes.spawn(session, cwd, cmd, title=title)
    panes.relayout(session)

    etat_pane = panes.wait_ready(pane_id)

    if etat_pane == panes.PANE_ETAT_CONFIANCE:
        raison = (
            f"{panes.TRUST_DIALOG_BLOCK_RAISON} in pane {pane_id} (session "
            f"{session}): a human must attach with \"{_attach_command(session)}\" "
            "and choose \"Yes, I trust this folder\" or \"No, exit\" themselves; Ordo "
            "never approves a directory automatically"
        )
        _record_pane_outcome(
            task_id, pane_id, cwd, resolved_model, "blocked", raison, session_id
        )
        journal.write(
            ch["id"],
            "ORDO",
            f"{verb_label} {task_id} pane={pane_id} blocked: tmux trust dialog",
        )
        raise CliError(
            # Le message COMPLET part sur stderr, pas une version courte. La version
            # courte laissait l'humain devant un echec sans rien pour aller regarder : la
            # session et la commande d'attache n'existaient que dans state.json et ne
            # ressortaient qu'au tick suivant. C'est l'instant precis ou l'information est
            # utile ; la differer, c'est la perdre.
            f"unable to {verb_label} {task_id}: {raison}"
        )

    _record_pane_outcome(
        task_id, pane_id, cwd, resolved_model, "running", None, session_id
    )
    panes.send(pane_id, f"Read {brief_path} and apply it.")
    journal.write(ch["id"], "ORDO", f"{verb_label} {task_id} pane={pane_id}")

    return {
        "paneId": pane_id,
        "brief": brief_path,
        "session": session,
        "window": window_id,
        "attach": _attach_command(session),
        "permissions": resolved_permissions,
        "title": title,
    }


def _print_launch_result(task_id: str, participe: str, result: dict, as_json: bool) -> int:
    """Sortie humaine et JSON commune a launch/relaunch, forme imposee par decision I.

    JSON ne sert QUE les six cles annoncees par le contrat (plus "tache") : "title" reste
    un detail d'affichage humain, jamais expose tel quel en JSON.
    """
    if as_json:
        _print_json(
            {
                "task": task_id,
                "paneId": result["paneId"],
                "brief": result["brief"],
                "session": result["session"],
                "window": result["window"],
                "attach": result["attach"],
                "permissions": result["permissions"],
            }
        )
        return 0
    print(f"{task_id}  {participe}, real Claude Code session in a tmux pane")

    def ligne(label: str, valeur: str) -> None:
        print(f"  {label:<12}{valeur}")

    ligne("session", result["session"])
    ligne("watch", result["attach"])
    ligne("pane", f"{result['paneId']}   (title: {result['title']})")
    ligne("brief", result["brief"])
    ligne("permissions", _PERMISSIONS_LABEL.get(result["permissions"], result["permissions"]))
    return 0


# ---------------------------------------------------------------------------
# Chantier
# ---------------------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    ch = chantier.start(
        args.project,
        args.goal,
        perimetre=args.scope,
        hors_scope=args.out_of_scope,
        permissions=args.permissions,
        home_partage=args.shared_home,
        keep_panes=args.keep_panes,
    )
    if args.json:
        _print_json(ch)
        return 0
    print(f"{ch['id']}  {ch['slug']}  {ch['project']}")
    print(f"{'goal':<13}: {ch['objectif']}")
    print(f"{'scope':<13}: {ch['perimetre'] or '(not specified)'}")
    print(f"{'out of scope':<13}: {ch['horsScope'] or '(not specified)'}")
    print(f"{'tmux session':<13}: {ch['tmuxSession']}")
    print(f"{'permissions':<13}: {ch.get('permissions', args.permissions)}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    state = store.load()
    chantiers = sorted(state["chantiers"].values(), key=lambda c: c["id"])
    if args.json:
        _print_json([_sans_capteur(c) for c in chantiers])
        return 0
    if not chantiers:
        print("(no campaigns)")
        return 0
    for c in chantiers:
        print(f"{c['id']:<6} {c['slug']:<20} {c['state']:<6} {c['project']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    state = store.load()
    ch = state["chantiers"].get(args.campaign)
    if ch is None:
        raise CliError(f"campaign not found: {args.campaign}")
    cap_status = capteur.status(args.campaign)
    taches = [t for t in state["taches"].values() if t["chantier"] == args.campaign]
    compte: dict[str, int] = {}
    for t in taches:
        compte[t["state"]] = compte.get(t["state"], 0) + 1
    if args.json:
        _print_json({**_sans_capteur(ch), "sensor": cap_status, "tasks": compte})
        return 0
    print(f"{ch['id']}  {ch['slug']}  [{ch['state']}]")
    print(f"{'project':<13}: {ch['project']}")
    print(f"{'goal':<13}: {ch['objectif']}")
    print(f"{'scope':<13}: {ch['perimetre'] or '(not specified)'}")
    print(f"{'out of scope':<13}: {ch['horsScope'] or '(not specified)'}")
    print(f"{'sensor':<13}: signal={cap_status['signal']}  adopted={cap_status['adopted']}")
    resume = ", ".join(f"{k}={v}" for k, v in sorted(compte.items())) or "(none)"
    print(f"{'tasks':<13}: {resume}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    ch, resultat = chantier.close(args.campaign, force=args.force, alive_check=panes.alive)

    # Le geste tmux reste ici, jamais dans chantier.py (decision E) : avec --force, on
    # tue chaque pane_id que close() a rendu, un par un, jamais tmux kill-server ni
    # pkill tmux. La session n'est tuee que si panes.panes() la rend vide APRES ce
    # tri : d'autres panes que ceux de cette tache pourraient encore y vivre.
    # panes_tues ne porte que ce qui a REELLEMENT ete detruit. Les pane_id stockes dans
    # l'etat designent souvent des panes morts depuis longtemps ; les compter comme tues
    # ferait annoncer a l'humain des process arretes qui ne tournaient plus.
    panes_tues: list[str] = []
    panes_deja_morts: list[str] = []
    if args.force:
        for pane_id in resultat.get("panes", []):
            if panes.kill(pane_id):
                panes_tues.append(pane_id)
            else:
                panes_deja_morts.append(pane_id)
        session = resultat.get("session")
        if session and not panes.panes(session):
            # kill_session est idempotente : la session peut avoir deja disparu avec son
            # dernier pane, et c'est le resultat voulu, pas un echec de fermeture.
            panes.kill_session(session)

    archives = resultat.get("archives", [])
    journal.write(
        ch["id"],
        "ORDO",
        f"close {ch['id']}: {len(panes_tues)} pane(s) killed, {len(archives)} archive(s)",
    )

    if args.json:
        _print_json(
            {
                **_sans_capteur(ch),
                "panesKilled": panes_tues,
                "panesAlreadyDead": panes_deja_morts,
                "archives": archives,
            }
        )
        return 0
    print(f"{ch['id']} closed")
    if panes_tues:
        print(f"  panes killed: {', '.join(panes_tues)}")
    else:
        print("  no pane killed")
    if panes_deja_morts:
        print(f"  already dead: {', '.join(panes_deja_morts)}")
    if archives:
        print(f"  archived: {', '.join(archives)}")
    else:
        print("  nothing archived")
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    texte = journal.brief(args.campaign)
    if args.json:
        _print_json({"campaign": args.campaign, "brief": texte})
        return 0
    print(texte, end="")
    return 0


# ---------------------------------------------------------------------------
# Graphe
# ---------------------------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    t = chantier.add_task(
        args.campaign,
        args.title,
        args.prompt,
        depends_on=args.depends or [],
        touches=args.touches or [],
        checklist=args.check or [],
    )
    if args.json:
        _print_json(t)
        return 0
    print(f"{t['id']}  {t['titre']}  (depends on: {', '.join(t['dependsOn']) or '-'})")
    return 0


def cmd_dep(args: argparse.Namespace) -> int:
    t = chantier.depend(args.task, args.on)
    if args.json:
        _print_json(t)
        return 0
    print(f"{t['id']} now depends on {', '.join(t['dependsOn'])}")
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    if not args.json:
        print(chantier.graph_ascii(args.campaign))
        return 0
    state = store.load()
    if args.campaign not in state["chantiers"]:
        raise CliError(f"campaign not found: {args.campaign}")
    taches = sorted(
        (t for t in state["taches"].values() if t["chantier"] == args.campaign),
        key=lambda t: t["id"],
    )
    _print_json(
        [
            {
                "id": t["id"],
                "titre": t["titre"],
                "state": t["state"],
                "dependsOn": t["dependsOn"],
                "touches": t["touches"],
                "checklist": t["checklist"],
            }
            for t in taches
        ]
    )
    return 0


def _raisons_non_pretes(state: dict, chantier_id: str, prets_ids: set[str]) -> list[dict]:
    """Pour chaque tache en file non prete, la ou les dependances qui la bloquent.

    Raffinement demande en plus du contrat de base : une dependance qui pointe vers un
    identifiant inexistant rend aujourd'hui une tache non prete pour toujours, sans que
    rien ne le dise. Une orchestratrice attendrait indefiniment sans diagnostic.
    """
    tasks = state["taches"]
    resultat = []
    for t in sorted(tasks.values(), key=lambda x: x["id"]):
        if t["chantier"] != chantier_id or t["state"] != "queued" or t["id"] in prets_ids:
            continue
        raisons = []
        for dep_id in t["dependsOn"]:
            dep = tasks.get(dep_id)
            if dep is None:
                raisons.append(f"{dep_id} not found (nonexistent id)")
            elif dep["state"] != "done":
                raisons.append(f"{dep_id} is in state {dep['state']} (not done yet)")
            elif not all(item["done"] for item in dep["checklist"]):
                manquantes = ", ".join(i["id"] for i in dep["checklist"] if not i["done"])
                raisons.append(f"{dep_id} finished but checklist incomplete ({manquantes})")
        resultat.append({"id": t["id"], "titre": t["titre"], "reasons": raisons})
    return resultat


def cmd_ready(args: argparse.Namespace) -> int:
    prets = chantier.ready(args.campaign)
    if not args.why:
        if args.json:
            _print_json(prets)
            return 0
        if not prets:
            print("(no task ready)")
            return 0
        for t in prets:
            print(f"{t['id']}  {t['titre']}")
        return 0

    state = store.load()
    prets_ids = {t["id"] for t in prets}
    non_pretes = _raisons_non_pretes(state, args.campaign, prets_ids)
    if args.json:
        _print_json({"ready": prets, "notReady": non_pretes})
        return 0
    for t in prets:
        print(f"{t['id']}  {t['titre']}  [ready]")
    for t in non_pretes:
        raison = " ; ".join(t["reasons"])
        print(f"{t['id']}  {t['titre']}  [waiting]  {raison}")
    if not prets and not non_pretes:
        print("(no task queued)")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    t = chantier.cancel(args.task)
    if args.json:
        _print_json(t)
        return 0
    print(f"{t['id']} cancelled in the graph")
    # Vocabulaire honnete (decision J) : annuler une tache ne touche a rien du cote tmux.
    # Si un pane existe, le dire explicitement plutot que de laisser croire que "annulee"
    # a coupe quoi que ce soit de reel.
    pane_id = t.get("paneId")
    if pane_id:
        print(f"  pane {pane_id} and the claude process inside it are still running")
        print(f"  to really stop it: ordo kill {t['id']}")
    return 0


def cmd_priority(args: argparse.Namespace) -> int:
    t = chantier.prioritize(args.task, args.n)
    if args.json:
        _print_json(t)
        return 0
    print(f"{t['id']} priority={t['priority']}")
    return 0


def cmd_amend(args: argparse.Namespace) -> int:
    t = chantier.amend(args.task, args.prompt)
    if args.json:
        _print_json(t)
        return 0
    print(f"{t['id']} prompt updated")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    t = chantier.check(args.task, args.item, done=not args.undo)
    if args.json:
        _print_json(t)
        return 0
    item = next(i for i in t["checklist"] if i["id"] == args.item)
    etat = "checked" if item["done"] else "unchecked"
    print(f"{t['id']}: {item['id']} {etat}")
    return 0


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    pave = sys.stdin.read()
    if not pave.strip():
        raise CliError("empty raw plan on standard input; the planner needs text")
    prop = plan.propose(args.campaign, pave, model=args.model)
    if args.json:
        _print_json(prop)
        return 0
    print(f"{prop['id']}  {len(prop['taches'])} task(s) proposed")
    print(f"automatic acceptance at {prop['deadline']} absent a decision")
    for t in prop["taches"]:
        print(f"  {t['ref']}  {t['titre']}")
    return 0


def cmd_propositions(args: argparse.Namespace) -> int:
    state = store.load()
    props = list(state["propositions"].values())
    if args.campaign:
        props = [p for p in props if p["chantier"] == args.campaign]
    if not args.all:
        props = [p for p in props if p["state"] == "pending"]
    props.sort(key=lambda p: p["id"])
    if args.json:
        _print_json(props)
        return 0
    if not props:
        print("(no proposal)")
        return 0
    for p in props:
        print(f"{p['id']}  {p['chantier']}  [{p['state']}]  {len(p['taches'])} task(s)")
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    prop = plan.accept(args.proposal)
    if args.json:
        _print_json(prop)
        return 0
    print(f"{prop['id']} accepted, {len(prop['taches'])} task(s) materialized")
    return 0


def cmd_refuse(args: argparse.Namespace) -> int:
    prop = plan.refuse(args.proposal, args.reason)
    if args.json:
        _print_json(prop)
        return 0
    print(f"{prop['id']} rejected: {prop['refus']}")
    return 0


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def cmd_launch(args: argparse.Namespace) -> int:
    result = _do_launch(
        args.task,
        args.model,
        args.permissions,
        eligible_states=("queued",),
        verb_label="launch",
        force=args.force,
        require_ready=True,
    )
    return _print_launch_result(args.task, "launched", result, args.json)


def cmd_relaunch(args: argparse.Namespace) -> int:
    result = _do_launch(
        args.task,
        args.model,
        args.permissions,
        eligible_states=("blocked", "failed", "waiting"),
        verb_label="relaunch",
        force=args.force,
        require_ready=False,
    )
    return _print_launch_result(args.task, "relaunched", result, args.json)


def cmd_say(args: argparse.Namespace) -> int:
    state = store.load()
    task, _ = _resolve_task(state, args.task)
    pane_id = task.get("paneId")
    if not pane_id:
        raise CliError(f"{args.task} has no active pane (never launched?)")
    if not panes.alive(pane_id):
        raise CliError(f"{args.task}: pane {pane_id} is no longer alive")
    panes.send(pane_id, args.text)
    if args.json:
        _print_json({"task": args.task, "paneId": pane_id, "sent": args.text})
        return 0
    print(f"{args.task}  sent to {pane_id}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    state = store.load()
    task, ch = _resolve_task(state, args.task)
    pane_id = task.get("paneId")
    if not pane_id:
        # Une tache moissonnee n'a plus de pane mais garde la queue d'ecran de son dernier
        # tour : la servir vaut mieux que refuser, sinon fermer les panes termines
        # reviendrait a effacer la seule trace lisible de ce qui s'y est passe.
        garde = task.get("lastCapture")
        if garde:
            if args.json:
                _print_json(
                    {
                        "task": args.task,
                        "paneId": None,
                        "session": ch["tmuxSession"],
                        "attach": None,
                        "capture": garde,
                        "reaped": True,
                    }
                )
                return 0
            print(f"task    {args.task}  (pane closed when the task finished)")
            print(f"session {ch['tmuxSession']}")
            print("source  last capture kept at reaping time, not a live pane")
            print()
            print(garde, end="" if garde.endswith("\n") else "\n")
            return 0
        raise CliError(f"{args.task} has no pane (never launched?)")
    # warn_floor=True : capture est le verbe humain, pas la boucle interne de busy() /
    # wait_ready() ; une lecture faite pendant qu'un client tmux est attache depuis un
    # petit terminal peut etre tronquee sans que rien ne le dise autrement.
    texte = panes.capture(pane_id, lines=args.lines, warn_floor=True)
    attach_cmd = _attach_command(ch["tmuxSession"])
    if args.json:
        _print_json(
            {
                "task": args.task,
                "paneId": pane_id,
                "session": ch["tmuxSession"],
                "attach": attach_cmd,
                "capture": texte,
            }
        )
        return 0
    # En-tete (decision J) : dit quelle tache, quel pane, quelle session, et comment s'y
    # attacher, avant le contenu brut du pane -- jamais melange dans "capture" du JSON,
    # qui reste le texte du pane, rien d'autre.
    print(f"task {args.task}  pane {pane_id}  session {ch['tmuxSession']}")
    print(f"to attach: {attach_cmd}")
    print("-" * 40)
    print(texte)
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    state = store.load()
    avertissement = _avertissement_multi_projet(state)
    lignes = []
    for t in sorted(state["taches"].values(), key=lambda x: x["id"]):
        if args.campaign and t["chantier"] != args.campaign:
            continue
        pane_id = t.get("paneId")
        if not pane_id or not panes.alive(pane_id):
            continue
        lignes.append(
            {
                "task": t["id"],
                "title": t["titre"],
                "campaign": t["chantier"],
                "paneId": pane_id,
                "state": t["state"],
                "busy": panes.busy(pane_id),
                "report": report.path(t["id"], t["chantier"]).exists(),
                # Signal visible et structure (I12-like : jamais une capture tronquee
                # servie comme si elle etait fiable) plutot que de forcer l'appelant a
                # deviner depuis lastLine : voir is_degraded() dans panes.py.
                "belowFloor": panes.is_degraded(pane_id),
                "lastLine": _derniere_ligne_utile(pane_id),
            }
        )
    if args.json:
        _print_json(lignes)
        return 0
    if avertissement:
        print(avertissement)
    if not lignes:
        print("(no live executor)")
        return 0
    for l in lignes:
        # Vocabulaire honnete (decision J) : "idle" ne dit pas si le tour s'est termine
        # normalement (rapport recu) ou si la tache est silencieuse depuis longtemps.
        if l["busy"]:
            activite = "busy"
        elif l["report"]:
            activite = "turn-done"
        else:
            activite = "silence"
        rapport = "yes" if l["report"] else "no"
        plancher = "  BELOW FLOOR" if l["belowFloor"] else ""
        print(
            f"{l['task']:<6} {l['campaign']:<6} {l['paneId']:<6} {l['state']:<8} "
            f"{activite:<9} report={rapport:<3}  {l['title']}  {l['lastLine']}{plancher}"
        )
    print(
        "legend: busy = turn in progress; turn-done = last turn finished, report "
        "received; silence = no report for a while"
    )
    return 0


def _etat_du_rapport(chemin) -> str:
    """Champ "state" du rapport, ou "unreadable" si le fichier ne se lit pas.

    Tolerant par construction : un rapport a moitie ecrit, mal forme ou tronque reste un
    FAIT NOUVEAU qu'il faut signaler, pas une erreur qui doit tuer la veille. C'est meme
    le cas le plus urgent a reveiller (I2 : un rapport illisible bloque la tache).
    """
    try:
        with chemin.open("r", encoding="utf-8") as f:
            return str(json.load(f).get("state") or "unreadable")
    except Exception:
        return "unreadable"


def cmd_watch(args: argparse.Namespace) -> int:
    """Flux d'evenements en LECTURE SEULE, une ligne par fait nouveau.

    Existe pour fermer la boucle qui faisait caler un chantier : une orchestratrice qui
    lance une executante puis termine son tour n'a rien qui la ramene. La derive n'etait
    vue qu'au rapport final, et la tache suivante attendait qu'un humain y pense. Ce verbe
    ne diagnostique rien, il sonne : chaque ligne reveille l'orchestratrice, qui commence
    alors par un vrai `tick`.

    N'ECRIT JAMAIS. Pas de store.locked(), pas de controle.tick(), pas de wake_new(). Une
    veille qui consommerait les motifs de reveil affamerait exactement la reconciliation
    qu'elle declenche : l'orchestratrice, reveillee, ne trouverait plus rien a traiter.

    L'etat de deduplication vit en memoire de ce process uniquement. Un fait n'est emis
    qu'une fois ; rien n'en est persiste, donc deux veilles concurrentes ne se genent pas.
    """
    state = store.load()
    if args.campaign not in state["chantiers"]:
        raise CliError(f"unknown campaign: {args.campaign}")

    vus: dict[str, dict] = {}
    questions_vues: set[str] = set()
    passe = 0

    def emettre(ligne: str) -> None:
        # flush immediat : chaque ligne est un evenement pour le surveillant qui nous
        # lit, pas une trace differee. Bufferisee, elle arriverait par paquets et le
        # reveil perdrait tout son interet.
        print(ligne, flush=True)

    while True:
        state = store.load()
        taches = [t for t in state["taches"].values() if t["chantier"] == args.campaign]
        un_pane_vivant = False

        for t in sorted(taches, key=lambda x: x["id"]):
            tid = t["id"]
            s = vus.setdefault(
                tid,
                {"mtime": None, "alive": None, "busy": None, "silence_dite": False,
                 "depuis": time.monotonic()},
            )

            chemin = report.path(tid, t["chantier"])
            mtime = chemin.stat().st_mtime if chemin.exists() else None
            if mtime is not None and mtime != s["mtime"]:
                s["mtime"] = mtime
                s["depuis"] = time.monotonic()
                s["silence_dite"] = False
                emettre(f"report {tid} {_etat_du_rapport(chemin)}")

            pane_id = t.get("paneId")
            if pane_id:
                vivant = panes.alive(pane_id)
                if s["alive"] and not vivant:
                    s["depuis"] = time.monotonic()
                    s["silence_dite"] = False
                    emettre(f"pane-dead {tid}")
                if vivant:
                    un_pane_vivant = True
                    occupe = panes.busy(pane_id)
                    # busy -> idle sans rapport : le tour est fini et rien n'a ete ecrit.
                    # C'est le cas que l'invariant I2 rend dangereux, celui ou un pane
                    # silencieux ressemble a une reussite.
                    if s["busy"] and not occupe and mtime is None:
                        emettre(f"turn-done {tid} no report")
                    if occupe != s["busy"]:
                        s["depuis"] = time.monotonic()
                        s["silence_dite"] = False
                    s["busy"] = occupe
                    ecoule = time.monotonic() - s["depuis"]
                    if not s["silence_dite"] and ecoule >= args.silence:
                        s["silence_dite"] = True
                        emettre(f"silent {tid} {int(ecoule)}s")
                s["alive"] = vivant

        for q in sorted(state["questions"].values(), key=lambda x: x["id"]):
            if q.get("chantier") != args.campaign or q.get("answer") is not None:
                continue
            if q["id"] in questions_vues:
                continue
            questions_vues.add(q["id"])
            emettre(f"question {q['id']} {q.get('tache')}")

        if not un_pane_vivant:
            # Fin naturelle : plus rien de ce chantier ne vit, donc plus rien ne peut
            # produire de fait nouveau. Sortir permet au surveillant de se desarmer au
            # lieu de rester braque sur un flux mort.
            emettre(f"idle {args.campaign}")
            return 0
        passe += 1
        if args.passes and passe >= args.passes:
            return 0
        time.sleep(args.interval)


def cmd_resume(args: argparse.Namespace) -> int:
    """Rouvre un pane sur la session claude d'une tache moissonnee, avec son contexte.

    A EVITER. Reprendre une session lui fait recharger tout son contexte : c'est le geste
    le plus cher du systeme, et une tache neuve avec un brief propre coute presque toujours
    moins. Ordo ne le fait donc jamais tout seul ; il faut ce verbe, explicitement, et il
    imprime ce que ca coute.

    Verifie a la main le 11 aout 2026 : claude --resume <uuid> retrouve bien le contenu du
    tour precedent de la meme session.
    """
    state = store.load()
    task, ch = _resolve_task(state, args.task)
    session_id = task.get("claudeSessionId")
    if not session_id:
        raise CliError(
            f"{args.task} has no recorded claude session id; it predates session pinning "
            "or was never launched. Relaunch it instead: ordo relaunch " + args.task
        )
    if task.get("paneId") and panes.alive(task["paneId"]):
        raise CliError(
            f"{args.task} still has a live pane ({task['paneId']}); resuming would start a "
            "second session on the same conversation. Read it with: ordo capture "
            + args.task
        )

    cwd = task.get("cwd") or ch["project"]
    permissions = task.get("permissions") or ch.get("permissions", PERMISSIONS_SKIP)
    cmd = _build_resume_cmd(cwd, session_id, permissions)
    session, window_id = panes.ensure_session(ch["tmuxSession"], label=ch["slug"])
    _record_window(ch["id"], window_id)
    title = _pane_title(task)
    pane_id = panes.spawn(window_id, cwd, cmd, title=title)
    panes.relayout(window_id)

    with store.locked() as locked:
        lt = locked["taches"][args.task]
        lt["paneId"] = pane_id
        lt["reaped"] = False
    journal.write(ch["id"], "ORDO", f"{args.task} resumed in pane {pane_id} (session {session_id})")

    attach_cmd = _attach_command(session)
    if args.json:
        _print_json(
            {
                "task": args.task,
                "paneId": pane_id,
                "session": session,
                "window": window_id,
                "attach": attach_cmd,
                "claudeSessionId": session_id,
                "permissions": permissions,
            }
        )
        return 0
    print(f"{args.task}  resumed, its previous claude session reloaded its whole context")
    print("  this is the most expensive move available; a fresh task is usually cheaper")
    ligne = lambda k, v: print(f"  {k:<11} {v}")  # noqa: E731
    ligne("session", session)
    ligne("watch", attach_cmd)
    ligne("pane", f"{pane_id}   (title: {title})")
    ligne("claude id", session_id)
    ligne("permissions", permissions)
    return 0


def cmd_kill(args: argparse.Namespace) -> int:
    state = store.load()
    task, ch = _resolve_task(state, args.task)
    pane_id = task.get("paneId")
    if not pane_id:
        raise CliError(f"{args.task} has no pane to kill")
    # tue vaut False quand le pane etait deja mort. Point J : ne jamais annoncer un
    # process detruit qui ne l'a pas ete, le pane stocke dans l'etat peut dater.
    tue = panes.kill(pane_id)
    journal.write(
        ch["id"],
        "ORDO",
        f"kill {args.task} pane={pane_id}" if tue else f"kill {args.task} pane={pane_id} already dead",
    )
    if args.json:
        _print_json({"task": args.task, "paneId": pane_id, "killed": tue})
        return 0
    if tue:
        print(f"{args.task}  pane {pane_id} destroyed, along with the claude process it contained")
    else:
        print(f"{args.task}  pane {pane_id} was already dead, nothing to destroy")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    """Point K : imprime la commande d'attache exacte, jamais ne l'execute.

    Executer l'attache depuis ici detournerait le terminal de l'orchestratrice elle-meme
    -- exactement le geste qu'un humain doit faire lui-meme, dans un autre terminal.
    """
    state = store.load()
    ch = state["chantiers"].get(args.target)
    if ch is None:
        task = state["taches"].get(args.target)
        if task is None:
            raise CliError(f"campaign or task not found: {args.target}")
        ch = state["chantiers"].get(task["chantier"])
        if ch is None:
            raise CliError(
                f"campaign not found for task {args.target}: {task['chantier']}"
            )
    session = ch["tmuxSession"]
    window = ch.get("tmuxWindow")
    attach_cmd = _attach_command(session)
    rows = panes.panes(session)
    if args.json:
        _print_json(
            {
                "campaign": ch["id"],
                "session": session,
                "window": window,
                "attach": attach_cmd,
                "panes": [{"paneId": r["pane_id"], "title": r["titre"]} for r in rows],
            }
        )
        return 0
    print(f"{'campaign':<13}{ch['id']}")
    print(f"{'session':<13}{session}")
    print(f"{'window':<13}{window or '(unknown, never launched)'}")
    print(f"{'attach':<13}{attach_cmd}")
    if not rows:
        print("(no pane yet)")
        return 0
    for r in rows:
        print(f"  pane {r['pane_id']:<6} {r['titre']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Point M : verifie l'environnement, jamais l'etat d'un chantier precis.

    Sort en code 1 si un element indispensable manque (tmux ou claude absents du PATH).
    Le partage de home entre plusieurs projets n'est qu'un avertissement (voir
    _avertissement_multi_projet) : il ne change jamais le code de sortie.
    """
    python_version = sys.version.split()[0]

    tmux_path = shutil.which("tmux")
    tmux_version = None
    if tmux_path:
        result = subprocess.run(["tmux", "-V"], capture_output=True, text=True)
        tmux_version = (result.stdout or result.stderr).strip() or "unknown version"

    claude_path = shutil.which("claude")

    probleme = tmux_path is None or claude_path is None

    home = store.home()
    state = store.load()
    ouverts = sorted(
        (c for c in state["chantiers"].values() if c["state"] == "open"),
        key=lambda c: c["id"],
    )
    avertissement = _avertissement_multi_projet(state)

    if args.json:
        _print_json(
            {
                "python": python_version,
                "tmux": {"present": tmux_path is not None, "version": tmux_version},
                "claude": {"present": claude_path is not None, "path": claude_path},
                "ordoHome": str(home),
                "openCampaigns": len(ouverts),
                "campaigns": [{"id": c["id"], "project": c["project"]} for c in ouverts],
                "projectWarning": avertissement,
            }
        )
        return 1 if probleme else 0

    print(f"{'python':<20}{python_version}   [OK]")
    if tmux_path:
        print(f"{'tmux':<20}{tmux_version}   [OK]")
    else:
        print(
            "tmux                not found in PATH, install via `brew install tmux`   "
            "[MISSING]"
        )
    if claude_path:
        print(f"{'claude':<20}{claude_path}   [OK]")
    else:
        print(
            "claude              not found in PATH, install Claude Code "
            "(https://claude.com/claude-code)   [MISSING]"
        )
    print(f"{'ORDO_HOME':<20}{home}")
    print(f"{'open campaigns':<20}{len(ouverts)}")
    for c in ouverts:
        print(f"  {c['id']:<6} {c['project']}")
    if avertissement:
        print(avertissement)
    return 1 if probleme else 0


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------



# Nombre de lignes de pane conservees avant de fermer une executante terminee. Assez pour
# relire son dernier tour sans stocker tout son ecran dans state.json.
REAP_CAPTURE_LINES = 60


def _moissonner_les_finies(campaign: str | None) -> list[str]:
    """Ferme le pane des taches passees a done, et rend la liste des taches moissonnees.

    Pourquoi seulement done. Une tache blocked ou failed est exactement celle qu'un humain
    veut aller regarder : son pane reste ouvert. Une tache done n'a plus rien a dire, et
    son pane laisse croire, dans un tmux attach, qu'il se passe encore quelque chose la.

    La queue d'ecran part dans task["lastCapture"] AVANT la fermeture. Sans elle, moissonner
    detruirait la seule trace lisible du dernier tour, et capture n'aurait plus rien a
    servir : ce serait echanger du bruit contre de l'amnesie.

    Le chantier peut refuser la moisson avec keepPanes, pose au start.
    """
    state = store.load()
    a_fermer: list[tuple[str, str, str]] = []
    for t in sorted(state["taches"].values(), key=lambda x: x["id"]):
        if campaign and t["chantier"] != campaign:
            continue
        if t["state"] != "done" or not t.get("paneId"):
            continue
        ch = state["chantiers"].get(t["chantier"]) or {}
        if ch.get("keepPanes"):
            continue
        a_fermer.append((t["id"], t["paneId"], t["chantier"]))

    moissonnees: list[str] = []
    for task_id, pane_id, chantier_id in a_fermer:
        try:
            queue = panes.capture(pane_id, lines=REAP_CAPTURE_LINES)
        except Exception:
            # Un pane deja mort n'a plus de queue a rendre ; ce n'est pas une raison de
            # laisser l'etat croire qu'il vit encore.
            queue = ""
        panes.kill(pane_id)
        with store.locked() as locked:
            lt = locked["taches"].get(task_id)
            if lt is None:
                continue
            lt["lastCapture"] = queue
            lt["paneId"] = None
            lt["reaped"] = True
        moissonnees.append(task_id)
        journal.write(chantier_id, "ORDO", f"{task_id} done, pane {pane_id} closed")

    # Re-tuiler une seule fois par fenetre, apres toutes les fermetures : le faire a chaque
    # pane ferait danser la geometrie sous les yeux d'un humain attache.
    if moissonnees:
        state = store.load()
        for cid in {c for _, _, c in a_fermer}:
            fenetre = (state["chantiers"].get(cid) or {}).get("tmuxWindow")
            if fenetre:
                with suppress(Exception):
                    panes.relayout(fenetre)
    return moissonnees


def cmd_tick(args: argparse.Namespace) -> int:
    try:
        from . import controle
    except ImportError as exc:
        raise CliError(
            "ordo/controle.py unavailable (written in parallel by another agent, "
            f"not yet on disk or failing to import): {exc}"
        ) from exc
    resultat = controle.tick(args.campaign)

    # Moisson : une tache terminee n'a plus rien a montrer, son pane encombre la fenetre
    # et fait passer pour vivant ce qui ne l'est plus. On le ferme, apres avoir garde sa
    # queue d'ecran dans l'etat pour que capture reste possible ensuite.
    resultat["reaped"] = _moissonner_les_finies(args.campaign)

    # Les motifs de reveil sont joints a la reconciliation, jamais laisses dans un verbe
    # separe. Une orchestratrice appelle tick() et rien d'autre en reprenant la main : un
    # motif qu'elle doit escalader mais que tick() ne sert pas est invisible. Le depot
    # d'origine a deja perdu une regle entiere ainsi, un predicat correct que personne
    # n'appelait.
    state_pour_cibles = store.load()
    cibles = [args.campaign] if args.campaign else sorted(state_pour_cibles["chantiers"])
    # Par defaut on ne sert que les motifs NEUFS : wake_reasons() recalcule tout l'etat
    # courant, donc servie telle quelle elle noierait le seul motif nouveau sous tous les
    # anciens. --all-wakeups rend l'etat complet, sans marquer quoi que ce soit.
    lecteur = controle.wake_reasons if getattr(args, "all_wakeups", False) else controle.wake_new
    reveils: dict[str, list[dict]] = {}
    for cid in cibles:
        try:
            motifs = lecteur(cid)
        except Exception as exc:  # un chantier casse ne prive pas les autres de leurs motifs
            motifs = [{"kind": "wake-error", "task": None, "detail": str(exc)}]
        if motifs:
            reveils[cid] = motifs
    resultat["wakeups"] = reveils

    # Point I4 : convention d'un ORDO_HOME par projet. Sans --campaign, tick reconcilie
    # deja tous les chantiers ouverts et marque leurs motifs comme servis, quel que soit
    # leur projet -- comportement inchange, seule l'annonce est nouvelle.
    avertissement = _avertissement_multi_projet(state_pour_cibles)
    resultat["projectWarning"] = avertissement

    if args.json:
        _print_json(resultat)
        return 0

    if avertissement:
        print(avertissement)

    # Forme documentee et verifiee de controle.tick() :
    # {"at": ..., "chantiers": {id: {"events": [...], "error": str|None}}}. Repli sur
    # un dump JSON brut si jamais cette forme change un jour : aucune information n'est
    # cachee derriere une cle devinee.
    chantiers = resultat.get("chantiers") if isinstance(resultat, dict) else None
    if not isinstance(chantiers, dict):
        _print_json(resultat)
        return 0
    if not chantiers and not reveils:
        print("(nothing to reconcile)")
        return 0
    for cid, detail in sorted(chantiers.items()):
        events = detail.get("events") or []
        erreur = detail.get("error")
        if erreur:
            print(f"{cid}  ERROR: {erreur}")
        for e in events:
            print(f"{cid}  {e}")
        if not events and not erreur:
            print(f"{cid}  (nothing to report)")
    for cid, motifs in sorted(reveils.items()):
        for m in motifs:
            cible = f" {m['task']}" if m.get("task") else ""
            print(f"{cid}  WAKEUP {m.get('kind', '?')}{cible}: {m.get('detail', '')}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    state = store.load()
    task, _ = _resolve_task(state, args.task)
    live: dict | None = None
    live_error: str | None = None
    try:
        live = report.read(args.task, task["chantier"])
    except report.RapportError as exc:
        live_error = str(exc)
    if args.json:
        _print_json(
            {
                "task": args.task,
                "taskState": task["state"],
                "rawReportPresent": report.path(args.task, task["chantier"]).exists(),
                "rawReport": live,
                "rawReportError": live_error,
                "appliedReport": task.get("report"),
            }
        )
        return 0
    print(f"{args.task}  state={task['state']}")
    if live_error:
        print(f"unreadable raw report: {live_error}")
    elif live is None:
        print("no report written yet")
    else:
        print(f"{'raw report':<16}: state={live['state']}  note={live['note']}")
    applied = task.get("report")
    if applied:
        print(f"{'applied report':<16}: state={applied['state']}  note={applied['note']}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    # Champs "chantier"/"tache"/"pourHumain" delibirement laisses en francais : ce dict
    # est le schema partage de state["questions"], que ordo/controle.py (autre agent,
    # hors perimetre) lit et ecrit lui aussi avec ces memes noms de cles (wake_reasons(),
    # _latest_question()) ; les renommer ici casserait ce contrat croise en silence.
    with store.locked() as state:
        task, ch = _resolve_task(state, args.task)
        qid = store.next_id(state, "question")
        q = {
            "id": qid,
            "chantier": ch["id"],
            "tache": task["id"],
            "question": args.text,
            "options": args.option or [],
            "pourHumain": args.for_human,
            "answer": None,
            "askedAt": store.now(),
            "answeredAt": None,
        }
        state["questions"][qid] = q
    if args.json:
        _print_json(q)
        return 0
    marque = "  [for human]" if q["pourHumain"] else ""
    print(f"{q['id']}  {q['tache']}  {q['question']}{marque}")
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    with store.locked() as state:
        q = state["questions"].get(args.question)
        if q is None:
            raise CliError(f"question not found: {args.question}")
        if q["answer"] is not None:
            raise CliError(f"answer refused: {args.question} already has an answer")
        q["answer"] = args.text
        q["answeredAt"] = store.now()
    if args.json:
        _print_json(q)
        return 0
    print(f"{q['id']}  answered")
    return 0


def cmd_questions(args: argparse.Namespace) -> int:
    state = store.load()
    qs = list(state["questions"].values())
    if args.campaign:
        qs = [q for q in qs if q["chantier"] == args.campaign]
    if args.task:
        qs = [q for q in qs if q["tache"] == args.task]
    if not args.all:
        qs = [q for q in qs if q["answer"] is None]
    qs.sort(key=lambda q: q["id"])
    if args.json:
        _print_json(qs)
        return 0
    if not qs:
        print("(no question)")
        return 0
    for q in qs:
        marque = "[for human] " if q["pourHumain"] else ""
        print(f"{q['id']}  {q['tache']}  {marque}{q['question']}")
    return 0


# ---------------------------------------------------------------------------
# Capteur
# ---------------------------------------------------------------------------


def cmd_capteur_install(args: argparse.Namespace) -> int:
    ch = capteur.install(args.campaign, args.script)
    payload = {
        "campaign": args.campaign,
        "path": ch["capteur"]["path"],
        "status": capteur.status(args.campaign),
    }
    if args.json:
        _print_json(payload)
        return 0
    print(f"{args.campaign}  sensor installed: {payload['path']}")
    return 0


def cmd_capteur_run(args: argparse.Namespace) -> int:
    record = capteur.run(args.campaign, timeout=args.timeout)
    if args.json:
        _print_json(record)
        return 0
    etat = "ok" if record["ok"] else f"failed ({record['error']})"
    print(f"{args.campaign}  {etat}  at={record['at']}")
    if record["ok"] and record["output"]:
        for m in record["output"]["measured"]:
            unite = f" {m['unit']}" if m.get("unit") else ""
            print(f"  measured  {m.get('name')} = {m.get('value')}{unite}")
    if not capteur.status(args.campaign)["adopted"]:
        print(
            "(sensor not adopted: this run alone is not a signal, "
            "3 concordant runs then 'sensor adopt')"
        )
    return 0


def cmd_capteur_status(args: argparse.Namespace) -> int:
    st = capteur.status(args.campaign)
    if args.json:
        _print_json(st)
        return 0
    if not st["adopted"]:
        print(f"{args.campaign}  signal=inconnu (sensor not adopted)")
        return 0
    print(f"{args.campaign}  signal={st['signal']}  consecutive failures={st['consecutiveFailures']}")
    for m in st["measured"]:
        unite = f" {m['unit']}" if m.get("unit") else ""
        print(f"  measured  {m.get('name')} = {m.get('value')}{unite}")
    for d in st["declared"]:
        print(f"  declared {d.get('name')} = {d.get('value')} (source: {d.get('source')})")
    for drift in st["drift"]:
        print(f"  drift   {drift.get('detail', drift)}")
    return 0


def cmd_capteur_adopt(args: argparse.Namespace) -> int:
    capteur.adopt(args.campaign)
    st = capteur.status(args.campaign)
    if args.json:
        _print_json({"campaign": args.campaign, "status": st})
        return 0
    print(f"{args.campaign}  sensor adopted")
    return 0


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


def cmd_journal_add(args: argparse.Namespace) -> int:
    journal.write(args.campaign, args.author, args.note)
    if args.json:
        _print_json({"campaign": args.campaign, "author": args.author, "note": args.note})
        return 0
    print(f"{args.campaign}  [{args.author}]  {args.note}")
    return 0


def cmd_journal_show(args: argparse.Namespace) -> int:
    entries = journal.read(args.campaign, limit=args.limit)
    if args.json:
        _print_json(entries)
        return 0
    if not entries:
        print("(empty journal)")
        return 0
    for e in entries:
        print(f"{e['heure']}  {e['auteur']:<4}  {e['texte']}")
    return 0


# ---------------------------------------------------------------------------
# Construction du parseur
# ---------------------------------------------------------------------------


def _build_parser() -> dict[str, argparse.ArgumentParser]:
    """Construit l'arbre argparse complet et renvoie les parseurs nommes utiles a main().

    Le dict renvoye contient toujours "main", et en plus "capteur" / "journal" : les deux
    seuls groupes reellement imbriques (les cinq autres groupes du tableau de SPEC.md sont
    une classification documentaire, pas une hierarchie de commandes ; chaque verbe qu'ils
    listent est un sous-parseur direct de "main"). main() s'en sert pour afficher l'aide du
    bon niveau quand un groupe est nomme sans verbe.
    """
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument("--json", action="store_true", help="stable JSON output (I12)")

    parser = argparse.ArgumentParser(
        prog="ordo",
        description=(
            "Command post of an orchestrator: campaigns, task graph, tmux executors, "
            "sensor, journal. --debug/--verbose must precede the verb."
        ),
    )
    parser.add_argument(
        "--debug", action="store_true", help="show the full Python traceback on error"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help='trace every tmux command executed on stderr, prefixed "tmux> "',
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ordo {__version__}",
        help="show ordo's version and exit",
    )
    verbs = parser.add_subparsers(dest="verb", metavar="verb", required=False)

    # Chantier
    p = verbs.add_parser("start", parents=[json_parent], help="open a campaign")
    p.add_argument("project")
    p.add_argument("--goal", required=True)
    p.add_argument("--scope", default="")
    p.add_argument("--out-of-scope", default="")
    p.add_argument(
        "--permissions",
        choices=PERMISSIONS_CHOICES,
        default=DEFAULT_PERMISSIONS,
        help="permissions for this campaign's executors (default skip)",
    )
    p.add_argument(
        "--keep-panes",
        action="store_true",
        dest="keep_panes",
        help=(
            "do not close an executor's pane when its task finishes; keeps every pane "
            "open for inspection, at the cost of a window full of dead sessions"
        ),
    )
    p.add_argument(
        "--shared-home",
        action="store_true",
        dest="shared_home",
        help=(
            "allows this ORDO_HOME to carry several different projects (decision D); "
            "without it, a second different project in an already open home is refused"
        ),
    )
    p.set_defaults(func=cmd_start)

    p = verbs.add_parser("list", parents=[json_parent], help="list campaigns")
    p.set_defaults(func=cmd_list)

    p = verbs.add_parser("show", parents=[json_parent], help="campaign detail")
    p.add_argument("campaign")
    p.set_defaults(func=cmd_show)

    p = verbs.add_parser(
        "close",
        parents=[json_parent],
        help="close a campaign; --force kills live panes and claude processes, otherwise refuses",
    )
    p.add_argument("campaign")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_close)

    p = verbs.add_parser("brief", parents=[json_parent], help="regenerated campaign brief")
    p.add_argument("campaign")
    p.set_defaults(func=cmd_brief)

    # Graphe
    p = verbs.add_parser("add", parents=[json_parent], help="add a task")
    p.add_argument("campaign")
    p.add_argument("--title", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--depends", action="append", metavar="TASK")
    p.add_argument("--touches", action="append", metavar="ZONE")
    p.add_argument("--check", action="append", metavar="LABEL")
    p.set_defaults(func=cmd_add)

    p = verbs.add_parser("dep", parents=[json_parent], help="add a dependency")
    p.add_argument("task")
    p.add_argument("on")
    p.set_defaults(func=cmd_dep)

    p = verbs.add_parser("graph", parents=[json_parent], help="render the task graph")
    p.add_argument("campaign")
    p.set_defaults(func=cmd_graph)

    p = verbs.add_parser("ready", parents=[json_parent], help="tasks launchable now")
    p.add_argument("campaign")
    p.add_argument("--why", action="store_true", help="explain what blocks the ones not ready")
    p.set_defaults(func=cmd_ready)

    p = verbs.add_parser(
        "cancel",
        parents=[json_parent],
        help="cancel a task in the graph (an existing pane keeps running, see 'kill')",
    )
    p.add_argument("task")
    p.set_defaults(func=cmd_cancel)

    p = verbs.add_parser("priority", parents=[json_parent], help="set a task's priority")
    p.add_argument("task")
    p.add_argument("n", type=int)
    p.set_defaults(func=cmd_priority)

    p = verbs.add_parser("amend", parents=[json_parent], help="amend a task's prompt")
    p.add_argument("task")
    p.add_argument("--prompt", required=True)
    p.set_defaults(func=cmd_amend)

    p = verbs.add_parser("check", parents=[json_parent], help="check a checklist item")
    p.add_argument("task")
    p.add_argument("item")
    p.add_argument("--undo", action="store_true", help="uncheck instead of check")
    p.set_defaults(func=cmd_check)

    # Plan
    p = verbs.add_parser("plan", parents=[json_parent], help="propose a graph from stdin")
    p.add_argument("campaign")
    p.add_argument("--model", default="sonnet")
    p.set_defaults(func=cmd_plan)

    p = verbs.add_parser("proposals", parents=[json_parent], help="list proposals")
    p.add_argument("--campaign")
    p.add_argument("--all", action="store_true", help="include proposals already decided")
    p.set_defaults(func=cmd_propositions)

    p = verbs.add_parser("accept", parents=[json_parent], help="accept a proposal")
    p.add_argument("proposal")
    p.set_defaults(func=cmd_accept)

    p = verbs.add_parser("reject", parents=[json_parent], help="reject a proposal")
    p.add_argument("proposal")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_refuse)

    # Execution
    p = verbs.add_parser(
        "launch",
        parents=[json_parent],
        help="launch a real claude session in a tmux pane (see 'ordo attach' next)",
    )
    p.add_argument("task")
    p.add_argument("--model", default=None)
    p.add_argument(
        "--permissions",
        choices=PERMISSIONS_CHOICES,
        default=None,
        help="override permissions for this task (default: the campaign's)",
    )
    p.add_argument("--force", action="store_true", help="override launch eligibility")
    p.set_defaults(func=cmd_launch)

    p = verbs.add_parser("say", parents=[json_parent], help="send an instruction into the pane")
    p.add_argument("task")
    p.add_argument("text")
    p.set_defaults(func=cmd_say)

    p = verbs.add_parser(
        "capture",
        parents=[json_parent],
        help="last lines of a task's pane, with task/pane/session/attach in a header",
    )
    p.add_argument("task")
    p.add_argument("--lines", type=int, default=80)
    p.set_defaults(func=cmd_capture)

    p = verbs.add_parser(
        "poll",
        parents=[json_parent],
        help="state of every live executor (pane, activity, report)",
    )
    p.add_argument("--campaign")
    p.set_defaults(func=cmd_poll)

    # Pas de parents=[json_parent] : la sortie de watch EST un flux de lignes, une par
    # fait nouveau, destinee a un surveillant. Un --json n'aurait rien a envelopper.
    p = verbs.add_parser(
        "watch",
        help="read-only event stream for a campaign, one line per new fact",
    )
    p.add_argument("campaign")
    p.add_argument(
        "--interval", type=int, default=10, help="seconds between two scans (default 10)"
    )
    p.add_argument(
        "--silence",
        type=int,
        default=900,
        help="seconds of a live but quiet pane before it becomes an event (default 900)",
    )
    # --passes plutot que --once : les faits les plus utiles sont des TRANSITIONS (un pane
    # qui meurt, un tour qui se termine sans rapport). Une passe unique n'a rien a quoi
    # comparer et ne peut, par construction, en observer aucune. Le borner en nombre de
    # passes rend la veille testable sans jamais boucler.
    p.add_argument(
        "--passes",
        type=int,
        default=0,
        help="stop after N scans; 0 (default) watches until nothing is alive",
    )
    p.set_defaults(func=cmd_watch)

    p = verbs.add_parser(
        "kill",
        parents=[json_parent],
        help="destroy a task's pane and the claude process it contains",
    )
    p.add_argument("task")
    p.set_defaults(func=cmd_kill)

    p = verbs.add_parser("relaunch", parents=[json_parent], help="relaunch a blocked task")
    p.add_argument("task")
    p.add_argument("--model", default=None)
    p.add_argument(
        "--permissions",
        choices=PERMISSIONS_CHOICES,
        default=None,
        help="override permissions for this task (default: the campaign's)",
    )
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_relaunch)

    p = verbs.add_parser(
        "resume",
        parents=[json_parent],
        help="reopen a reaped task's claude session with its context (expensive, avoid)",
    )
    p.add_argument("task")
    p.set_defaults(func=cmd_resume)

    p = verbs.add_parser(
        "attach",
        parents=[json_parent],
        help="attach command for a campaign or a task, printed but never executed",
    )
    p.add_argument("target", help="campaign or task id")
    p.set_defaults(func=cmd_attach)

    p = verbs.add_parser(
        "doctor",
        parents=[json_parent],
        help="check python, tmux, claude and the active ORDO_HOME",
    )
    p.set_defaults(func=cmd_doctor)

    # Signal
    p = verbs.add_parser("tick", parents=[json_parent], help="full reconciliation")
    p.add_argument("--campaign")
    p.add_argument(
        "--all-wakeups",
        action="store_true",
        dest="all_wakeups",
        help="return all wake-up reasons, including those already served, without marking anything",
    )
    p.set_defaults(func=cmd_tick)

    p = verbs.add_parser("report", parents=[json_parent], help="inspect a task's report")
    p.add_argument("task")
    p.set_defaults(func=cmd_report)

    p = verbs.add_parser("ask", parents=[json_parent], help="formalize a question")
    p.add_argument("task")
    p.add_argument("text")
    p.add_argument("--option", action="append", metavar="TEXT")
    p.add_argument("--for-human", action="store_true", dest="for_human")
    p.set_defaults(func=cmd_ask)

    p = verbs.add_parser("answer", parents=[json_parent], help="answer a question")
    p.add_argument("question")
    p.add_argument("text")
    p.set_defaults(func=cmd_answer)

    p = verbs.add_parser("questions", parents=[json_parent], help="list questions")
    p.add_argument("--campaign")
    p.add_argument("--task")
    p.add_argument("--all", action="store_true", help="include questions already answered")
    p.set_defaults(func=cmd_questions)

    # Capteur (seul vrai sous-groupe avec Journal)
    capteur_parser = verbs.add_parser("sensor", help="a campaign's sensor contract")
    capteur_sub = capteur_parser.add_subparsers(dest="action", metavar="action", required=False)

    p = capteur_sub.add_parser("install", parents=[json_parent], help="install a script")
    p.add_argument("campaign")
    p.add_argument("script")
    p.set_defaults(func=cmd_capteur_install)

    p = capteur_sub.add_parser("run", parents=[json_parent], help="run once")
    p.add_argument("campaign")
    p.add_argument("--timeout", type=float, default=capteur.DEFAULT_TIMEOUT)
    p.set_defaults(func=cmd_capteur_run)

    p = capteur_sub.add_parser("status", parents=[json_parent], help="filtered signal (I12)")
    p.add_argument("campaign")
    p.set_defaults(func=cmd_capteur_status)

    p = capteur_sub.add_parser("adopt", parents=[json_parent], help="validate after 3 concordant runs")
    p.add_argument("campaign")
    p.set_defaults(func=cmd_capteur_adopt)

    # Journal
    journal_parser = verbs.add_parser("journal", help="campaign journal")
    journal_sub = journal_parser.add_subparsers(dest="action", metavar="action", required=False)

    p = journal_sub.add_parser("add", parents=[json_parent], help="add a fact")
    p.add_argument("campaign")
    p.add_argument("--author", required=True, choices=("ORDO", "ORCH", "USER"))
    p.add_argument("--note", required=True)
    p.set_defaults(func=cmd_journal_add)

    p = journal_sub.add_parser("show", parents=[json_parent], help="reread the journal")
    p.add_argument("campaign")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_journal_show)

    return {"main": parser, "sensor": capteur_parser, "journal": journal_parser}


# ---------------------------------------------------------------------------
# Point d'entree
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parsers = _build_parser()
    args = parsers["main"].parse_args(argv)
    _apply_verbose(getattr(args, "verbose", False))

    if args.verb is None:
        parsers["main"].print_help()
        return 0

    func = getattr(args, "func", None)
    if func is None:
        # Un groupe a ete nomme ("capteur" ou "journal") sans action : montrer l'aide de
        # ce groupe precisement, pas l'aide generale qui ne repeterait pas ses actions.
        parsers.get(args.verb, parsers["main"]).print_help()
        return 0

    try:
        return func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - frontiere volontaire, voir docstring module
        if getattr(args, "debug", False):
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
