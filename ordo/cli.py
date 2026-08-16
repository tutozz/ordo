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
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

from . import (
    __version__,
    capteur,
    chantier,
    journal,
    panes,
    plan,
    prompt,
    report,
    routage,
    situation,
    store,
)


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


def _avertissement_checklist(hors_convention: list[dict]) -> str | None:
    """Alerte quand un ou plusieurs labels de checklist dépassent la convention de
    40 caractères (chantier.CHECKLIST_LABEL_MAX).

    Jamais un refus : la tâche existe déjà quand cette fonction est appelée, le label
    n'est ni tronqué ni corrigé, seulement signalé à qui l'a rédigé trop long.
    """
    if not hors_convention:
        return None
    detail = ", ".join(f"{item['id']} ({item['length']} chars)" for item in hors_convention)
    return (
        f"WARNING: checklist label(s) exceed the {chantier.CHECKLIST_LABEL_MAX}-char "
        f"convention: {detail}"
    )


# "label" ou "label:minutes" : le suffixe reste collé à SON item, jamais apparié par
# position avec une option séparée qui se désynchroniserait au moindre réordonnancement de
# plusieurs --check sur une même commande (brief t-27). \d+ seulement : un suffixe non
# numérique (ex. "chapitre:trois") n'est pas une durée, il reste dans le libellé tel quel.
_CHECK_DUREE_RE = re.compile(r"^(.*):(\d+)$")


def _parse_check(brut: str) -> dict:
    """Un `--check` accepte "libellé" ou "libellé:minutes" (brief t-27, minutes-Claude)."""
    trouve = _CHECK_DUREE_RE.match(brut)
    if not trouve:
        return {"label": brut, "dureeMin": None}
    return {"label": trouve.group(1), "dureeMin": int(trouve.group(2))}


# "item:clé=valeur", ex. "c1:geste=lire" (brief t-36). Volontairement une syntaxe séparée
# de --check plutôt qu'un troisième segment collé au suffixe ":minutes" : le suffixe de
# --check est ancré sur \d+ SEUL (voir _CHECK_DUREE_RE juste au-dessus), lui ajouter une
# grammaire d'attributs casserait ce contrat pour tout libellé qui contient déjà un ":".
_ATTRIBUT_RE = re.compile(r"^([^:=]+):([^:=]+)=([^:=]+)$")


def _parse_attribut(brut: str) -> tuple[str, str, str]:
    """Un `--attribut` accepte "item:clé=valeur", ex. "c1:geste=lire" (brief t-36).

    Erreur de syntaxe pure, jamais métier : chantier.set_checklist_attribut() est celui
    qui refuse une clé ou une valeur inconnue (ChantierError), cette fonction ne fait que
    reconnaître la forme de la chaîne.
    """
    trouve = _ATTRIBUT_RE.match(brut)
    if not trouve:
        raise CliError(
            f'malformed --attribut {brut!r}: expected "item:key=value", e.g. "c1:geste=lire"'
        )
    return trouve.group(1), trouve.group(2), trouve.group(3)


def _avertissement_checklist_sans_duree(sans_duree: list[dict]) -> str | None:
    """Alerte quand un ou plusieurs critères créés n'ont reçu aucune estimation
    (chantier.checklist_sans_duree), même régime que _avertissement_checklist ci-dessus :
    jamais un refus, seulement un signal à qui vient de rédiger la checklist.

    Scopée à la création (`ordo add`) seulement, pas à `checklist add`/`split` : ce sont
    justement les critères ajoutés en cours de route, par une exécutante qui vient de lire
    le code, qui ont le moins besoin qu'on le lui rappelle -- la révision volontaire est
    déjà couverte par `ordo checklist duree`.
    """
    if not sans_duree:
        return None
    detail = ", ".join(item["id"] for item in sans_duree)
    return f"WARNING: checklist item(s) without a duration estimate: {detail}"


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
    # Sans --model, le modele se deduit de la tache plutot que d'etre laisse au defaut de
    # Claude Code, qui est le plus cher pour tout le monde y compris pour appliquer un
    # brief deja arbitre. routage.pour_lancement respecte toute decision deja prise, et
    # `--model herite` restaure le comportement d'avant.
    resolved_model, model_reason = routage.pour_lancement(model, task)
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
    # window_id et non session (Point B, voir panes._resolve_target) : `-t <session>` vise
    # la fenetre COURANTE de la session, celle qui derive des qu'une seconde fenetre existe
    # et qu'un humain attache bascule dessus. Tant que la session d'un chantier n'avait
    # qu'une fenetre, la distinction etait invisible ; `ordo map --pane` en ouvre une
    # seconde, et alors une executante pouvait naitre dans la fenetre de la carte, y
    # recycler la pane du rafraichisseur, et relayout redimensionner la mauvaise fenetre
    # pendant que les executantes gardaient une geometrie perimee. cmd_resume() passait
    # deja window_id ; ces deux lignes s'alignent dessus.
    pane_id = panes.spawn(window_id, cwd, cmd, title=title)
    panes.relayout(window_id)

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
        # Extras internes, jamais servis en --json : le contrat de publication fige les
        # six cles ci-dessus, et l'elargir serait un changement de contrat.
        "model": resolved_model,
        "modelReason": model_reason,
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
    # Le modele sort TOUJOURS avec son motif. Un choix automatique qu'on ne voit pas
    # passer est un choix qu'on ne peut pas contester, et celui-ci se conteste en une
    # commande : relancer avec --model.
    ligne("model", f"{result['model'] or 'defaut de claude'}   ({result['modelReason']})")
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


def cmd_digest(args: argparse.Namespace) -> int:
    """Remise en contexte d'un chantier, destinee a etre recopiee dans un message humain.

    Verbe de lecture pure. Son interet n'est pas d'ajouter de l'information mais d'en
    interdire l'omission : situation.render() ne sort jamais un identifiant de tache sans
    son titre, ce qu'une orchestratrice dont le contexte a ete compacte ne peut plus
    garantir de memoire.
    """
    m = situation.model(args.campaign)
    if args.json:
        _print_json(m)
        return 0
    print(situation.render(m))
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
        checklist=[_parse_check(c) for c in (args.check or [])],
        why=args.why or "",
    )
    # Attributs posés à la création (c3, brief t-36) : une seule syntaxe séparée de
    # --check (voir _parse_attribut), appliquée après coup sur la tâche fraîchement créée
    # -- même appel `ordo add`, donc toujours "à la création" du point de vue de qui tape
    # la commande, sans fragiliser le contrat déjà en place de --check.
    for brut in args.attribut or []:
        item_id, cle, valeur = _parse_attribut(brut)
        t = chantier.set_checklist_attribut(t["id"], item_id, cle, valeur)
    if args.json:
        _print_json(t)
        return 0
    avertissement = _avertissement_checklist(chantier.checklist_hors_convention(t["checklist"]))
    if avertissement:
        print(avertissement, file=sys.stderr)
    avertissement_duree = _avertissement_checklist_sans_duree(
        chantier.checklist_sans_duree(t["checklist"])
    )
    if avertissement_duree:
        print(avertissement_duree, file=sys.stderr)
    print(f"{t['id']}  {t['titre']}  (depends on: {', '.join(t['dependsOn']) or '-'})")
    if not t["why"]:
        # Dit une fois, au moment ou la reponse est encore fraiche dans la tete de qui
        # decoupe. Personne ne remonte expliquer un decoupage trois heures plus tard.
        print(f"           no why: ordo why {t['id']} \"...\" says why this task exists")
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
    # Lu AVANT la mutation, seulement pour savoir si CE check est une vraie transition
    # (brief t-36) : chantier.check() est idempotent, recocher un item déjà coché n'a
    # "aucun effet" sur son état (voir le filet de sécurité du protocole de rapport), mais
    # journaliser quand même un second "checklist-coche" fausserait la mesure -- il
    # écraserait la durée déjà mesurée par une valeur quasi nulle (l'écart entre les deux
    # coches), sans que rien ne signale la perte.
    deja_coche = False
    if not args.doing and not args.undo:
        etat_avant = store.load()
        tache_avant = etat_avant["taches"].get(args.task)
        item_avant = next(
            (i for i in (tache_avant or {}).get("checklist", []) if i["id"] == args.item),
            None,
        )
        deja_coche = bool(item_avant and item_avant.get("done"))
    t = chantier.check(args.task, args.item, done=not args.undo, doing=args.doing)
    # Journal machine (t-34) : datation réelle de CE check, en direct, brief t-36 -- seule
    # source de vérité pour mesurer un jour la durée individuelle de chaque critère plutôt
    # que de diviser la durée totale de la tâche par leur nombre (voir
    # chantier.duree_mesuree_par_critere). --undo ne journalise rien : décocher n'est
    # jamais une fin de critère, ce serait fausser toute mesure ultérieure sur cet item.
    if args.doing:
        journal.enregistrer_evenement(
            t["chantier"], "checklist-doing", tache=args.task, item=args.item,
        )
    elif not args.undo and not deja_coche:
        journal.enregistrer_evenement(
            t["chantier"], "checklist-coche", tache=args.task, item=args.item,
        )
    # Déduction du critère en cours : seulement après une coche réelle (ni --doing, qui
    # déclare déjà explicitement le sien, ni --undo, qui décoche et n'est jamais une
    # "attaque" d'un nouveau critère). Import local et défensif, même raison qu'ailleurs
    # dans ce fichier (voir cmd_tick) : ordo/controle.py est écrit en parallèle par une
    # autre exécutante et peut être absent ou cassé, ce qui ne doit jamais empêcher un
    # `ordo check` de fonctionner -- la déduction est un confort, pas le cœur du verbe.
    if not args.doing and not args.undo:
        try:
            from . import controle
        except ImportError:
            controle = None
        if controle is not None:
            suivant = controle.deduce_current_item(t)
            if suivant:
                t = chantier.check(args.task, suivant, doing=True)
                journal.enregistrer_evenement(
                    t["chantier"], "checklist-doing", tache=args.task, item=suivant,
                )
    if args.json:
        _print_json(t)
        return 0
    item = next(i for i in t["checklist"] if i["id"] == args.item)
    if args.doing:
        print(f"{t['id']}: {item['id']} in progress")
        # Rappel déductible, pas volontaire (brief t-27, c4/c9) : accroché à --doing, déjà
        # obligatoire pour toucher cet item, jamais un paragraphe à part qu'on peut sauter
        # -- exactement ce qui est arrivé trois fois de suite à --doing lui-même avant que
        # le brief ne l'explique ici même.
        duree = item.get("dureeMin")
        commande = f"ordo checklist duree {t['id']} {item['id']} <minutes>"
        if duree is None:
            print(f"  no duration estimate on {item['id']}; set one now: {commande}", file=sys.stderr)
        else:
            print(
                f"  {item['id']} estimated at {duree}m; correct it now if wrong: {commande}",
                file=sys.stderr,
            )
        return 0
    etat = "checked" if item["done"] else "unchecked"
    print(f"{t['id']}: {item['id']} {etat}")
    return 0


# ---------------------------------------------------------------------------
# Checklist : ajouter, decouper, reformuler -- jamais supprimer (brief t-22)
# ---------------------------------------------------------------------------


def cmd_checklist_add(args: argparse.Namespace) -> int:
    state = store.load()
    _, ch = _resolve_task(state, args.task)
    t = chantier.add_checklist_item(args.task, args.label)
    nouveau = t["checklist"][-1]
    journal.write(
        ch["id"], args.author, f'checklist {args.task}: added {nouveau["id"]} "{args.label}"'
    )
    if args.json:
        _print_json(t)
        return 0
    avertissement = _avertissement_checklist(chantier.checklist_hors_convention(t["checklist"]))
    if avertissement:
        print(avertissement, file=sys.stderr)
    print(f"{args.task}: {nouveau['id']} added - {args.label}")
    return 0


def cmd_checklist_split(args: argparse.Namespace) -> int:
    state = store.load()
    _, ch = _resolve_task(state, args.task)
    t = chantier.split_checklist_item(args.task, args.item, args.first, args.second)
    nouveau = t["checklist"][-1]
    journal.write(
        ch["id"],
        args.author,
        f"checklist {args.task}: split {args.item} into {args.item} + {nouveau['id']}",
    )
    if args.json:
        _print_json(t)
        return 0
    avertissement = _avertissement_checklist(chantier.checklist_hors_convention(t["checklist"]))
    if avertissement:
        print(avertissement, file=sys.stderr)
    print(f"{args.task}: {args.item} split into {args.item} + {nouveau['id']}")
    return 0


def cmd_checklist_reword(args: argparse.Namespace) -> int:
    t = chantier.reword_checklist_item(args.task, args.item, args.label)
    if args.json:
        _print_json(t)
        return 0
    avertissement = _avertissement_checklist(chantier.checklist_hors_convention(t["checklist"]))
    if avertissement:
        print(avertissement, file=sys.stderr)
    print(f"{args.task}: {args.item} reworded")
    return 0


def cmd_checklist_duree(args: argparse.Namespace) -> int:
    t = chantier.set_checklist_duree(args.task, args.item, args.minutes)
    if args.json:
        _print_json(t)
        return 0
    item = next(i for i in t["checklist"] if i["id"] == args.item)
    print(f"{args.task}: {item['id']} estimated at {item['dureeMin']}m")
    return 0


def cmd_checklist_attribut(args: argparse.Namespace) -> int:
    t = chantier.set_checklist_attribut(args.task, args.item, args.cle, args.valeur)
    if args.json:
        _print_json(t)
        return 0
    item = next(i for i in t["checklist"] if i["id"] == args.item)
    print(f"{args.task}: {item['id']} {args.cle}={item['attributs'][args.cle]}")
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
    # plan.py reste pur (aucun print) : prop["taches"] porte encore le checklist brut du
    # modèle (liste de chaînes), _materialize() ne le normalise que dans state["taches"],
    # jamais dans la proposition elle-même. On refait ici le même calcul d'id (f"c{i}")
    # que chantier._normalize_checklist et _materialize, préfixé du ref pour distinguer
    # les tâches entre elles : deux tâches partagent sinon le même id "c1".
    hors_convention = []
    for tache in prop["taches"]:
        checklist = [
            {"id": f"{tache['ref']}:c{i}", "label": label}
            for i, label in enumerate(tache.get("checklist") or [], start=1)
        ]
        hors_convention.extend(chantier.checklist_hors_convention(checklist))
    avertissement = _avertissement_checklist(hors_convention)
    if avertissement:
        print(avertissement, file=sys.stderr)
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

    # C'est ici que le tableau de bord s'allume, et nulle part ailleurs. Une veille est
    # armee au premier lancement de chaque chantier et vit aussi longtemps que lui : c'est
    # le seul point du systeme dont la duree coincide avec celle d'un chantier en cours.
    # Le premier l'allume, les suivants le trouvent debout et s'inscrivent seulement ;
    # s'il est tombe, la veille suivante le rallume. Un echec ici ne fait jamais echouer la
    # veille : un tableau de bord absent est un desagrement, une veille morte est un
    # chantier a l'arret.
    if not getattr(args, "no_serve", False):
        try:
            from . import serveur

            port = args.port or serveur.PORT
            etat = serveur.ensure(port)
            # Sur stderr, jamais sur stdout : la sortie de watch est un CANAL MACHINE, une
            # ligne par fait nouveau, que le surveillant de l'orchestratrice lit pour la
            # reveiller. Y glisser une ligne de confort la reveillerait pour un fait qui
            # n'en est pas un, au tout premier tour de chaque chantier.
            print(f"map http://127.0.0.1:{port}/ ({etat})", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 - voir le commentaire ci-dessus
            print(f"map unavailable: {exc}", file=sys.stderr, flush=True)

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
                    # occupe gagne toujours : un pane qui réfléchit longtemps (effort
                    # xhigh, dix à quinze minutes) porte son indicateur d'activité en
                    # continu sans jamais retoucher "depuis", donc "ecoule" grossit
                    # pendant qu'il travaille -- capteur.pane_silencieux() refuse
                    # d'émettre tant que occupe est vrai, seule façon de ne pas confondre
                    # une session qui pense avec une session morte (voir son docstring).
                    if not s["silence_dite"] and capteur.pane_silencieux(
                        occupe, ecoule, args.silence
                    ):
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
        # La cible est une tâche OU un chantier. Une orchestratrice escalade le plus
        # souvent une question qui n'appartient à aucune tâche -- lancer la suite en
        # parallèle ou en série, découper maintenant ou plus tard -- et exiger une tâche
        # la forçait à en désigner une au hasard, ce qui rendait ensuite la question
        # illisible pour qui la lisait attachée à cette tâche-là.
        ch = state["chantiers"].get(args.task)
        if ch is not None:
            task = None
        else:
            task, ch = _resolve_task(state, args.task)
        qid = store.next_id(state, "question")
        q = {
            "id": qid,
            "chantier": ch["id"],
            "tache": task["id"] if task else None,
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
    # La cible, jamais "None" : une question de chantier n'a pas de tâche, et afficher
    # le mot None à la place d'un identifiant se lit comme un bug de la commande.
    print(f"{q['id']}  {q['tache'] or q['chantier']}  {q['question']}{marque}")
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    """Enregistre la réponse ET la pousse dans le pane de l'exécutante qui a demandé,
    comme le ferait un ordo say (voir panes.send, chemin unique réutilisé ici).

    L'écriture de l'état (réponse + refus de double réponse) et l'envoi tmux sont deux
    verrous distincts, jamais un seul : un appel tmux peut prendre plusieurs secondes
    (panes.send() dort, capture, retente) et ne doit jamais avoir lieu sous
    state.json.lock, même raisonnement que controle.py::_tick_one (étape 4). La réponse
    est donc TOUJOURS enregistrée en premier, avant même de savoir si la livraison va
    réussir : un envoi qui échoue ensuite ne doit ni perdre la réponse de l'humain, ni
    la rendre répondable une seconde fois (refus de double réponse inchangé), seulement
    signaler bruyamment que personne ne l'a reçue.

    Une question de chantier (q["tache"] est None) n'a pas d'exécutante à qui parler :
    rien à livrer, comportement inchangé depuis avant cette fonction.
    """
    with store.locked() as state:
        q = state["questions"].get(args.question)
        if q is None:
            raise CliError(f"question not found: {args.question}")
        if q["answer"] is not None:
            raise CliError(f"answer refused: {args.question} already has an answer")
        q["answer"] = args.text
        q["answeredAt"] = store.now()
        task_id = q["tache"]
        task = state["taches"].get(task_id) if task_id else None
        pane_id = task.get("paneId") if task else None

    if task_id is not None:
        # Pane mort, jamais lancé, ou tâche disparue : rien à envoyer. Ce n'est pas une
        # erreur de l'humain qui répond, c'est l'exécutante qui est indisponible -- la
        # réponse ci-dessus reste enregistrée, seule la livraison échoue (point 3).
        if task is None or not pane_id or not panes.alive(pane_id):
            raise CliError(
                f"{q['id']}: answer recorded but {task_id} has no live pane -- "
                f"nobody received it, {task_id} must be relaunched"
            )
        try:
            panes.send(pane_id, f"Answer: {args.text}")
        except RuntimeError as exc:
            raise CliError(
                f"{q['id']}: answer recorded but delivery to {task_id} failed: {exc}"
            ) from exc

        with store.locked() as state:
            fresh_q = state["questions"].get(args.question)
            if fresh_q is not None:
                fresh_q["injectedAt"] = store.now()
                q["injectedAt"] = fresh_q["injectedAt"]
            fresh_task = state["taches"].get(task_id)
            if fresh_task is not None and fresh_task["state"] == "waiting":
                fresh_task["state"] = "running"

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
        print(f"{q['id']}  {q['tache'] or q['chantier']}  {marque}{q['question']}")
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
# Carte
# ---------------------------------------------------------------------------


# Intervalle par defaut du rafraichissement de la carte, en secondes. Cinq secondes est
# l'ordre de grandeur d'un tick : plus court ne montrerait rien de nouveau et ferait juste
# clignoter la page sous les yeux de celui qui la lit.
MAP_INTERVAL_S = 5


def _map_path(args: argparse.Namespace) -> Path:
    """Chemin absolu de la page. Absolu et pas relatif : --pane relance cette commande dans
    un process tmux dont le repertoire courant n'est pas garanti etre le notre, et deux cwd
    differents ecriraient deux fichiers differents en croyant en tenir un seul.
    """
    if getattr(args, "out", None):
        return Path(args.out).expanduser().resolve()
    dossier = store.home() / "map"
    dossier.mkdir(exist_ok=True)
    return dossier / f"{args.campaign}.html"


def _map_write(campaign: str, chemin: Path, interval: int) -> dict:
    """Recalcule le modele et reecrit la page. Rend le modele, pour l'affichage.

    La vivacite des panes est lue en UN appel tmux pour toute la carte (panes.live_ids),
    pas un par tache : cette fonction est rappelee en boucle par --watch.

    tmux absent ou trop ancien ne fait pas echouer la carte : la vivacite devient inconnue
    et la carte le dit, au lieu de refuser de dessiner un graphe qui n'a pourtant besoin
    d'aucun serveur pour etre lu. _ensure_tmux_ready() LEVE dans ce cas, avant meme de
    regarder le code de retour, d'ou ce filet ici plutot que dans live_ids(), dont tous les
    autres appelants, eux, ont bel et bien besoin de tmux.

    carte.py est importe ICI et pas en tete de fichier, pour la meme raison que controle.py
    (voir le docstring du module) : un defaut dans le module de dessin ne doit pas rendre
    inutilisable un CLI dont depend une orchestration en cours. Dessiner la carte est un
    confort, lancer et reconcilier ne l'est pas.
    """
    from . import carte, usage

    try:
        vivants = panes.live_ids()
        verificateur = lambda pane_id: pane_id in vivants  # noqa: E731
    except RuntimeError:
        verificateur = None
    model = carte.model(campaign, alive=verificateur, usage_de=usage.pour)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(carte.html(model, interval=interval), encoding="utf-8")
    return model


def _map_open(chemin: Path) -> None:
    """Ouvre la page dans le navigateur du systeme, sans jamais faire echouer la commande.

    Un poste sans "open" ni "xdg-open" reste un poste ou la carte a bien ete ecrite ; le
    chemin est deja affiche, l'ouverture n'est qu'un confort.
    """
    for lanceur in ("open", "xdg-open"):
        if shutil.which(lanceur):
            subprocess.run([lanceur, str(chemin)], capture_output=True)
            return
    print(f"note: neither open nor xdg-open found, open {chemin} by hand", file=sys.stderr)


def cmd_map(args: argparse.Namespace) -> int:
    """Ecrit la carte HTML du chantier, une fois ou en boucle.

    LECTURE SEULE sur l'etat du chantier : cette commande n'ecrit que son propre fichier
    HTML. Elle peut donc tourner en permanence a cote d'un chantier en cours sans jamais
    consommer un motif de reveil ni prendre le verrou de state.json en ecriture.
    """
    interval = max(0, int(args.interval))
    chemin = _map_path(args)

    if args.pane:
        state = store.load()
        ch = state["chantiers"].get(args.campaign)
        if ch is None:
            raise CliError(f"campaign not found: {args.campaign}")
        session = ch.get("tmuxSession")
        if not session:
            raise CliError(f"{args.campaign} has no tmux session yet")
        # ORDO_HOME est repasse explicitement. tmux ne transmet PAS l'environnement du
        # client a new-window : le process fils herite de l'environnement global du
        # serveur tmux, fige au demarrage de celui-ci, plus celui de la session. Un
        # serveur tmux plus vieux que le premier `ordo start` -- le cas normal des qu'un
        # humain a deja ses propres sessions -- ferait donc resoudre ORDO_HOME vers
        # ~/.claude/ordo, et la boucle mourrait sur "campaign not found" a la premiere
        # iteration. Le pire est le silence : la page a deja ete ecrite ici, avec le bon
        # ORDO_HOME, et rien a l'ecran ne dirait que la fenetre est morte. `env` en tete
        # plutot que new-window -e, pour valoir aussi sur la branche respawn-window.
        commande = shlex.join([
            "env", f"ORDO_HOME={store.home()}",
            str(Path(sys.argv[0]).resolve()), "map", args.campaign,
            "--watch", "--interval", str(interval or MAP_INTERVAL_S),
            "--out", str(chemin),
        ])
        window_id = panes.side_window(session, panes.MAP_WINDOW_NAME, commande)
        model = _map_write(args.campaign, chemin, interval or MAP_INTERVAL_S)
        if args.open:
            _map_open(chemin)
        if args.json:
            _print_json({"path": str(chemin), "window": window_id, "session": session})
            return 0
        print(f"map        {chemin}")
        print(f"window     {window_id} ({panes.MAP_WINDOW_NAME}) in session {session}")
        print(f"refresh    every {interval or MAP_INTERVAL_S}s")
        print(f"tasks      {model['counts']['total']}")
        print(f"attach     {_attach_command(session)}")
        return 0

    if args.json and args.watch:
        # Refus explicite plutot que --json ignore en silence (I8) : une boucle qui rend du
        # texte humain la ou l'appelant attend du JSON casse son parseur sans un mot.
        raise CliError("--json and --watch cannot be combined; use one or the other")

    if args.json:
        _print_json(_map_write(args.campaign, chemin, interval))
        return 0

    if not args.watch:
        model = _map_write(args.campaign, chemin, interval)
        if args.open:
            _map_open(chemin)
        counts = model["counts"]
        resume = "  ".join(
            f"{etat} {n}" for etat, n in sorted(counts.items()) if etat != "total"
        )
        print(f"{chemin}")
        print(f"{counts['total']} tasks   {resume}")
        for w in model["warnings"]:
            print(f"warning    {w['detail']}")
        return 0

    if interval == 0:
        raise CliError("--watch needs a non-zero --interval")
    if args.open:
        _map_write(args.campaign, chemin, interval)
        _map_open(chemin)
    passe = 0
    while True:
        model = _map_write(args.campaign, chemin, interval)
        counts = model["counts"]
        print(
            f"{store.now()}  {chemin}  {counts.get('done', 0)}/{counts['total']} done  "
            f"{counts.get('running', 0)} running  {len(model['warnings'])} warnings",
            flush=True,
        )
        passe += 1
        if args.passes and passe >= args.passes:
            return 0
        time.sleep(interval)


def cmd_group(args: argparse.Namespace) -> int:
    ch = chantier.set_group(args.campaign, args.key, args.label, why=args.why or "")
    if args.json:
        _print_json({"campaign": ch["id"], "groups": ch.get("groupes", {})})
        return 0
    print(f"{ch['id']}  phase {args.key} named {args.label!r}")
    pose = (ch.get("groupes") or {}).get(args.key) or {}
    raison = pose.get("why") if isinstance(pose, dict) else ""
    if raison:
        print(f"           why: {raison}")
    else:
        # Le libelle seul ne dit toujours pas a quoi la phase sert : le taire ferait croire
        # que le decoupage est explique alors qu'il ne l'est pas.
        print("           no why yet: rerun with --why to say what this phase serves")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Demarre, arrete ou interroge le serveur de cartes local.

    serveur.py est importe ICI, pas en tete de fichier, pour la meme raison que carte.py et
    controle.py : un defaut dans le tableau de bord ne doit pas rendre inutilisable le CLI
    dont depend une orchestration en cours.
    """
    from . import serveur

    args.port = args.port or serveur.PORT
    if args.stop:
        arrete = serveur.stop(args.port)
        print(f"server on {args.port}: " + ("stopped" if arrete else "was not running"))
        return 0
    if args.foreground:
        serveur.register(store.home())
        serveur.serve(args.port)
        return 0

    etat = serveur.ensure(args.port)
    url = f"http://127.0.0.1:{args.port}/"
    if args.json:
        _print_json({"port": args.port, "url": url, "state": etat,
                     "homes": serveur.homes()})
        return 0
    print(f"server     {url}  ({etat})")
    for home in serveur.homes():
        print(f"home       {home}")
    if args.open:
        _map_open(url)
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    t = chantier.explain(args.task, args.why)
    if args.json:
        _print_json({"id": t["id"], "why": t["why"]})
        return 0
    print(f"{t['id']}  why: {t['why']}")
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

    p = verbs.add_parser(
        "digest",
        parents=[json_parent],
        help="where the campaign stands, in words a human can read cold",
    )
    p.add_argument("campaign")
    p.set_defaults(func=cmd_digest)

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
    p.add_argument(
        "--check",
        action="append",
        metavar="LABEL[:MINUTES]",
        help='checklist item; optional Claude-minutes estimate as "label:20"',
    )
    p.add_argument(
        "--attribut",
        action="append",
        metavar="ITEM:KEY=VALUE",
        help='set a criterion attribute at creation, e.g. "c1:geste=lire" '
        f"(keys: {', '.join(chantier.ATTRIBUTS_VALEURS)})",
    )
    p.add_argument(
        "--why",
        default="",
        help="why this task exists and why here in the split, not what it does",
    )
    p.set_defaults(func=cmd_add)

    p = verbs.add_parser("dep", parents=[json_parent], help="add a dependency")
    p.add_argument("task")
    p.add_argument("on")
    p.set_defaults(func=cmd_dep)

    p = verbs.add_parser("graph", parents=[json_parent], help="render the task graph")
    p.add_argument("campaign")
    p.set_defaults(func=cmd_graph)

    p = verbs.add_parser(
        "map",
        parents=[json_parent],
        help="write a standalone HTML map of the graph (read only)",
    )
    p.add_argument("campaign")
    p.add_argument("--out", default=None, help="target file, default ORDO_HOME/map/<campaign>.html")
    p.add_argument(
        "--interval",
        type=int,
        default=MAP_INTERVAL_S,
        help=f"seconds between refreshes, 0 for a static page (default {MAP_INTERVAL_S})",
    )
    p.add_argument("--watch", action="store_true", help="rewrite the page in a loop")
    p.add_argument(
        "--pane",
        action="store_true",
        help="run the loop in a dedicated tmux window of the campaign session",
    )
    p.add_argument("--open", action="store_true", help="open the page in the browser")
    p.add_argument("--passes", type=int, default=0, help="stop after N refreshes (tests)")
    p.set_defaults(func=cmd_map)

    p = verbs.add_parser("group", parents=[json_parent], help="name a phase of the graph")
    p.add_argument("campaign")
    p.add_argument("key", help='phase prefix read from task titles, e.g. "0"')
    p.add_argument("label", help="the phase's name, in plain words")
    p.add_argument(
        "--why",
        default="",
        help="what this phase serves and why it comes here; kept if omitted on a rename",
    )
    p.set_defaults(func=cmd_group)

    p = verbs.add_parser(
        "serve", parents=[json_parent], help="local map server for every live campaign"
    )
    p.add_argument("--port", type=int, default=None, help="default 9123")
    p.add_argument("--stop", action="store_true", help="stop whatever listens on the port")
    p.add_argument(
        "--foreground",
        action="store_true",
        help="run the loop here instead of detaching it (used by the auto-start)",
    )
    p.add_argument("--open", action="store_true", help="open the page in the browser")
    p.set_defaults(func=cmd_serve)

    p = verbs.add_parser(
        "why", parents=[json_parent], help="say why a task exists, after the fact"
    )
    p.add_argument("task")
    p.add_argument("why", help="why this task exists and why here, not what it does")
    p.set_defaults(func=cmd_why)

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

    p = verbs.add_parser(
        "check", parents=[json_parent], help="check a checklist item, or declare it in progress"
    )
    p.add_argument("task")
    p.add_argument("item")
    p.add_argument("--undo", action="store_true", help="uncheck instead of check")
    p.add_argument(
        "--doing",
        action="store_true",
        help="declare the item as the one currently being worked on, without checking it",
    )
    p.set_defaults(func=cmd_check)

    # Checklist : ajouter, decouper, reformuler -- jamais supprimer (brief t-22)
    checklist_parser = verbs.add_parser(
        "checklist",
        help="grow a task's checklist: add a criterion, split one, reword one (never delete)",
    )
    checklist_sub = checklist_parser.add_subparsers(
        dest="action", metavar="action", required=False
    )

    p = checklist_sub.add_parser(
        "add", parents=[json_parent], help="append a new criterion to a task"
    )
    p.add_argument("task")
    p.add_argument("label")
    p.add_argument("--author", default="ORDO", choices=("ORDO", "ORCH", "USER"))
    p.set_defaults(func=cmd_checklist_add)

    p = checklist_sub.add_parser(
        "split", parents=[json_parent], help="split one existing criterion into two"
    )
    p.add_argument("task")
    p.add_argument("item")
    p.add_argument("first", help="new label for the item that keeps its id")
    p.add_argument("second", help="label for the freshly created item")
    p.add_argument("--author", default="ORDO", choices=("ORDO", "ORCH", "USER"))
    p.set_defaults(func=cmd_checklist_split)

    p = checklist_sub.add_parser(
        "reword", parents=[json_parent], help="fix a criterion's label, same id, same state"
    )
    p.add_argument("task")
    p.add_argument("item")
    p.add_argument("label")
    p.set_defaults(func=cmd_checklist_reword)

    p = checklist_sub.add_parser(
        "duree",
        parents=[json_parent],
        help="set or correct a criterion's estimate, in Claude-minutes",
    )
    p.add_argument("task")
    p.add_argument("item")
    p.add_argument("minutes", type=int)
    p.set_defaults(func=cmd_checklist_duree)

    p = checklist_sub.add_parser(
        "attribut",
        parents=[json_parent],
        help="set or correct one attribute of a criterion "
        f"({', '.join(chantier.ATTRIBUTS_VALEURS)})",
    )
    p.add_argument("task")
    p.add_argument("item")
    p.add_argument("cle", metavar="key")
    p.add_argument("valeur", metavar="value")
    p.set_defaults(func=cmd_checklist_attribut)

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
    p.add_argument(
        "--model",
        default=None,
        help="modele de l'executante ; par defaut deduit de la tache "
        "(haiku/sonnet/opus), 'herite' pour n'en imposer aucun",
    )
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
    p.add_argument(
        "--no-serve",
        action="store_true",
        help="do not start the local map server (it starts by default with the watch)",
    )
    p.add_argument("--port", type=int, default=None, help="map server port, default 9123")
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
    p.add_argument(
        "--model",
        default=None,
        help="modele de l'executante ; par defaut deduit de la tache "
        "(haiku/sonnet/opus), 'herite' pour n'en imposer aucun",
    )
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

    return {
        "main": parser,
        "sensor": capteur_parser,
        "journal": journal_parser,
        "checklist": checklist_parser,
    }


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
