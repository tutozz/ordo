"""Boucle de reconciliation : derive de scope, fausse completion, reveil, tick().

Ce module coud ensemble tous les autres : store (etat, verrou), chantier (graphe,
propagation d'echec), report (canal de rapport), panes (tmux), capteur (mesure), plan
(propositions), journal (memoire persistante). Il ne detient aucune donnee propre : tout
ce qu'il lit ou ecrit vit dans state.json ou dans les fichiers deja definis par ces
modules.

Invariant central, I2 : pour une tache "running", le RAPPORT est lu en premier. S'il est
present et terminal, on agit sur son contenu sans jamais regarder l'etat du pane. Un pane
mort ne l'emporte jamais sur un rapport "done" deja arrive ; c'est exactement l'inverse
qui a laisse une session terminee passer inapercue pendant quatorze jours dans le projet
d'origine.

Defaut a ne jamais reproduire : une exception levee en traitant UN chantier ne doit
jamais interrompre tick() pour les autres. tick() isole chaque chantier dans son propre
bloc protege ; voir _tick_one() et son appelant.
"""

from __future__ import annotations

import os
from contextlib import suppress
from datetime import datetime, timezone

from . import capteur, chantier, journal, panes, plan, report, store

# Delai de grace entre la constatation "pane mort, aucun rapport" et le blocage effectif
# de la tache. Un pane peut mourir juste apres avoir ecrit son rapport, dont l'ecriture
# sur disque n'est pas forcement encore visible au tick qui observe la mort du process ;
# ce delai laisse le rapport une chance d'arriver avant de declarer la tache bloquee.
# Nomme et patchable : les tests n'attendent jamais ce delai en temps reel.
PANE_DEAD_GRACE_S = 30.0

# Prefixe fixe de task["error"] quand tick() bloque une tache pour pane mort sans
# rapport. wake_reasons() s'en sert pour distinguer ce cas de "rapport terminal" (les
# deux se traduisent par task["state"] == "blocked", mais leur cause differe). Ce
# prefixe est un contrat interne a ce module : lui seul l'ecrit, lui seul le relit.
PANE_MORT_RAISON = "dead pane, no report received"

# Prefixe exact ecrit par chantier.propagate_failures() (voir ordo/chantier.py :
# task["error"] = f"dependency {dep_id} dead ({dep['state']})"). Sert a exclure les
# blocages en cascade de wake_reasons() : la tache dont la mort a declenche la cascade a
# deja son propre motif de reveil, et un doublon ("terminal-report" pour une tache qui
# n'a jamais recu de rapport) induirait l'orchestratrice en erreur sur la cause reelle.
_DEPENDANCE_MORTE_PREFIX = "dependency "

# Duree sans aucun evenement journalise au-dela de laquelle un simple tour de controle
# devient lui-meme une raison de reveil : le silence total sur un chantier ouvert n'est
# jamais un etat de repos legitime, il faut verifier que quelque chose avance encore.
# Nomme et patchable, comme PANE_DEAD_GRACE_S.
WAKE_IDLE_AFTER_S = 900.0


class ControleError(Exception):
    """Refus explicite d'une operation de controle (meme esprit que I8 : toute
    operation refusee dit laquelle et pourquoi, jamais un echec muet)."""


def _get_chantier(state: dict, chantier_id: str) -> dict:
    ch = state["chantiers"].get(chantier_id)
    if ch is None:
        raise ControleError(f"campaign not found: {chantier_id}")
    return ch


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _elapsed_seconds(iso_ts: str | None) -> float | None:
    """Secondes ecoulees depuis iso_ts (format store.now()), ou None si iso_ts est
    absent ou illisible. None est traite comme "on ne sait pas" par les appelants,
    jamais comme zero : une tache sans startedAt ne doit pas paraitre fraiche a tort."""
    if not iso_ts:
        return None
    dt = _parse_iso(iso_ts)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _sorted_tasks(state: dict, chantier_id: str) -> list[dict]:
    return sorted(
        (t for t in state["taches"].values() if t["chantier"] == chantier_id),
        key=lambda t: t["id"],
    )


# ===========================================================================
# scope_drift
# ===========================================================================


def _covered(touched: str, declared: list[str]) -> bool:
    """Regle de correspondance entre un fichier reellement touche et une zone
    declaree, l'une comme l'autre une chaine libre (une zone n'est pas forcement un
    chemin : "db-test", "staging" sont des zones valides, cf. plan.py).

    Une zone COUVRE un chemin touche si l'une des deux chaines est une sous-chaine de
    l'autre. Cela couvre a la fois :
      - le prefixe de chemin classique ("app/src/api" couvre "app/src/api/routes.py",
        car la zone est une sous-chaine du chemin) ;
      - la zone opaque qui n'est pas un chemin ("db-test" couvre "db-test", ou un
        chemin qui la contient litteralement, ex. "migrations/db-test/x.sql").

    Ce que cette regle NE detecte PAS :
      - une re-nomination semantique : la zone "app/api" ne couvre pas le chemin
        "backend/api" meme si les deux designent la meme chose pour un humain (aucune
        sous-chaine commune) ; c'est en general le comportement souhaite (dérive
        reelle), mais un renommage de dossier legitime en cours de tache produira le
        meme faux positif.
      - une casse differente : "DB" et "db/schema.sql" ne se couvrent pas (comparaison
        sensible a la casse), ce qui peut produire un FAUX POSITIF (derive signalee a
        tort) si l'executante ou l'orchestratrice ne sont pas coherentes sur la casse.
      - un chevauchement fortuit : la zone "api" couvre a tort le chemin
        "src/apiExperimentale.js" (sous-chaine coincidente), ce qui peut au contraire
        MASQUER une vraie derive (faux negatif).
    """
    return any(zone in touched or touched in zone for zone in declared)


def scope_drift(chantier_id: str) -> list[dict]:
    """Compare les zones declarees (task["touches"]) aux fichiers reellement ecrits,
    rapportes par l'executante dans task["report"]["touched"].

    Une tache sans "touches" declare ne produit jamais de derive : on ne peut pas
    deriver d'un perimetre qu'on n'a pas declare (le silence de l'orchestratrice n'est
    pas une autorisation illimitee, mais ce module n'a rien a comparer). Une tache sans
    rapport n'a rien touche de connu, donc rien a comparer non plus.

    Voir _covered() pour la regle de correspondance retenue et ce qu'elle ne detecte
    pas.
    """
    state = store.load()
    _get_chantier(state, chantier_id)
    drifts: list[dict] = []
    for task in _sorted_tasks(state, chantier_id):
        declared = task.get("touches") or []
        if not declared:
            continue
        rep = task.get("report")
        if not isinstance(rep, dict):
            continue
        for touched in rep.get("touched") or []:
            if not _covered(touched, declared):
                drifts.append(
                    {
                        "task": task["id"],
                        "touched": touched,
                        "declared": list(declared),
                        "detail": (
                            f"{touched} touched outside declared zones "
                            f"({', '.join(declared)})"
                        ),
                    }
                )
    return drifts


# ===========================================================================
# fausse_completion
# ===========================================================================


def fausse_completion(chantier_id: str) -> list[dict]:
    """Croise ce que le capteur DECLARE (valeurs lues dans des documents) avec ce qu'il
    MESURE reellement (I9 : les deux natures ne se melangent jamais, on ne les compare
    qu'a distance, jamais en les fusionnant sous un meme nom).

    Invariant I12 : tant que le capteur n'est pas adopte, capteur.status() ne sert
    aucune mesure, meme si des executions ont deja reussi. Dans ce cas, cette fonction
    ne rend PAS une liste vide (qui se lirait comme "aucune fausse completion, tout va
    bien") : elle rend une entree explicite disant qu'aucune mesure n'est disponible et
    que la fausse completion est donc indetectable pour l'instant.

    Une fois le capteur adopte, une entree "declared" sans entree "measured" du meme
    nom est un candidat de fausse completion : quelque chose est affirme (typiquement
    dans un document) sans qu'aucune mesure independante ne le confirme.
    """
    state = store.load()
    _get_chantier(state, chantier_id)
    status = capteur.status(chantier_id)
    if not status["adopted"]:
        return [
            {
                "kind": "unavailable",
                "detail": (
                    "sensor not adopted: no measurement available, false "
                    "completion undetectable (I12)"
                ),
            }
        ]

    measured_names = {m.get("name") for m in status["measured"]}
    results: list[dict] = []
    for d in status["declared"]:
        name = d.get("name")
        if name in measured_names:
            continue
        results.append(
            {
                "kind": "declared-unmeasured",
                "name": name,
                "value": d.get("value"),
                "source": d.get("source"),
                "detail": (
                    f"'{name}' = {d.get('value')!r} declared (source: "
                    f"{d.get('source')}) with no matching measurement"
                ),
            }
        )
    return results


# ===========================================================================
# wake_reasons
# ===========================================================================


def _is_propagated_block(task: dict) -> bool:
    return str(task.get("error") or "").startswith(_DEPENDANCE_MORTE_PREFIX)


def _is_pane_mort_block(task: dict) -> bool:
    return str(task.get("error") or "").startswith(PANE_MORT_RAISON)


def _is_confiance_block(task: dict) -> bool:
    """True for a task cli.py's launch blocked on the tmux trust dialog.

    Written by _do_launch() in ordo/cli.py, never by this module: see
    panes.TRUST_DIALOG_BLOCK_RAISON for why the exact prefix string lives in
    panes.py rather than being duplicated here (same reasoning already
    applied to _DEPENDANCE_MORTE_PREFIX above, just resolved by importing
    the constant instead of copying it, since panes.py is already a
    dependency of this module).
    """
    return str(task.get("error") or "").startswith(panes.TRUST_DIALOG_BLOCK_RAISON)


def _attach_command(session: str) -> str:
    """Commande d'attache exacte pour `session`, prefixee env -u TMUX si necessaire.

    tmux refuse de nester un attach tant que la variable TMUX de l'appelant est
    definie (ce qui est le cas de l'orchestratrice elle-meme, qui tourne dans son
    propre pane) : sans le prefixe, la commande copiee-collee telle quelle
    echouerait silencieusement a s'attacher a la bonne session.
    """
    prefixe = "env -u TMUX " if os.environ.get("TMUX") else ""
    return f"{prefixe}tmux attach -t {session}"


def _pane_attach_hint(task: dict | None, ch: dict) -> str:
    """Suffixe " (pane <id>, <commande d'attache>)", ou chaine vide.

    Point 2 du contrat : un motif qui concerne une tache vivante (elle porte un
    pane_id) doit donner le moyen d'aller voir. Une tache sans pane_id, ou l'absence
    de tache (motif qui ne concerne aucune tache precise, ex. tour-de-controle), ne
    porte evidemment rien : il n'y a nul endroit ou aller voir.
    """
    if task is None:
        return ""
    pane_id = task.get("paneId")
    if not pane_id:
        return ""
    return f" (pane {pane_id}, {_attach_command(ch['tmuxSession'])})"


def wake_reasons(chantier_id: str) -> list[dict]:
    """Ce qui doit reveiller l'orchestratrice pour ce chantier, a l'instant present.

    Fonction pure, purement une lecture de l'etat courant (aucune trace de ce qui a
    "deja ete vu" n'existe dans le schema figé) : elle recalcule les raisons a chaque
    appel plutot que de suivre un delta depuis le dernier reveil.

    Sept familles, dans cet ordre :
      1. rapport-terminal : une tache done/blocked dont le blocage vient reellement
         d'un rapport (ou de son absence de sens), pas d'un pane mort, d'un dialogue
         de confiance ni d'une propagation de dependance (voir _is_pane_mort_block,
         _is_confiance_block, _is_propagated_block).
      2. question-humain : une question non repondue explicitement marquee pour
         l'humain (pourHumain True). Une question issue d'un rapport "asking" arrive
         toujours chez l'orchestratrice d'abord (pourHumain False par defaut, voir
         tick()) ; elle n'apparait ici que si quelque chose l'a explicitement
         escaladee.
      3. derive-scope : chaque entree de scope_drift().
      4. capteur-double-echec : signal "waking" de capteur.status(), donc uniquement
         visible apres adoption (I12 masque tout avant, y compris ce signal ; un
         capteur qui echoue systematiquement avant meme d'etre adoptable ne remonte
         donc pas ici, limite documentee du contrat de capteur.status()).
      5. pane-mort : chaque tache bloquee par _is_pane_mort_block.
      6. confiance-attendue : chaque tache bloquee par _is_confiance_block, c'est-a-dire
         par ordo/cli.py's launch face au dialogue de confiance tmux (voir
         panes.TRUST_DIALOG_BLOCK_RAISON). Distincte de pane-mort (le pane est vivant,
         il attend une decision) et de derive-scope (aucune ecriture n'a eu lieu).
      7. tour-de-controle : rien n'a ete journalise depuis WAKE_IDLE_AFTER_S secondes.

    Un chantier qui n'est pas "open" ne produit jamais aucun motif (voir plus bas) :
    un chantier clos n'a plus vocation a reveiller personne, meme s'il porte encore
    des taches "done"/"blocked" ou une derive de scope jamais vue.

    rapport-terminal, pane-mort et derive-scope portent chacun, en plus de leur texte
    propre, le pane_id de la tache concernee et la commande d'attache tmux exacte
    quand cette tache en a un (voir _pane_attach_hint) : confiance-attendue est seul
    a deja porter cette information nativement (ecrite par cli.py, voir
    panes.TRUST_DIALOG_BLOCK_RAISON), et question-humain/capteur-double-echec/
    tour-de-controle ne concernent aucune tache precise, donc aucun pane a montrer.
    """
    state = store.load()
    ch = _get_chantier(state, chantier_id)
    # Point 4 du contrat : un chantier clos n'emet plus aucun motif de reveil. Sans
    # ce filtre, cli.py's cmd_tick (qui boucle sur TOUS les chantiers, sans
    # condition d'etat, contrairement a tick(None) qui se limite aux "open")
    # continuait d'imprimer des lignes REVEIL pour un chantier deja ferme.
    if ch["state"] != "open":
        return []
    reasons: list[dict] = []

    for task in _sorted_tasks(state, chantier_id):
        if task["state"] not in ("done", "blocked"):
            continue
        if _is_propagated_block(task):
            continue
        if _is_pane_mort_block(task):
            reasons.append(
                {
                    "kind": "pane-dead",
                    "task": task["id"],
                    "detail": task["error"] + _pane_attach_hint(task, ch),
                }
            )
        elif _is_confiance_block(task):
            reasons.append(
                {"kind": "trust-expected", "task": task["id"], "detail": task["error"]}
            )
        else:
            note = ""
            rep = task.get("report")
            if isinstance(rep, dict):
                note = rep.get("note") or ""
            detail = task.get("error") or note or task["state"]
            reasons.append(
                {
                    "kind": "terminal-report",
                    "task": task["id"],
                    "detail": detail + _pane_attach_hint(task, ch),
                }
            )

    for q in sorted(state["questions"].values(), key=lambda q: q["id"]):
        if q["chantier"] != chantier_id:
            continue
        if q.get("pourHumain") and q.get("answer") is None:
            reasons.append(
                {"kind": "human-question", "task": q.get("tache"), "detail": q.get("question")}
            )

    for drift in scope_drift(chantier_id):
        drift_task = state["taches"].get(drift["task"])
        reasons.append(
            {
                "kind": "scope-drift",
                "task": drift["task"],
                "detail": drift["detail"] + _pane_attach_hint(drift_task, ch),
            }
        )

    status = capteur.status(chantier_id)
    if status.get("adopted") and status.get("signal") == "waking":
        reasons.append(
            {
                "kind": "sensor-double-failure",
                "task": None,
                "detail": f"{status['consecutiveFailures']} consecutive failures",
            }
        )

    baseline = ch.get("lastEvent") or ch.get("createdAt")
    elapsed = _elapsed_seconds(baseline)
    if elapsed is not None and elapsed >= WAKE_IDLE_AFTER_S:
        reasons.append(
            {
                "kind": "control-round",
                "task": None,
                "detail": f"nothing reported since {WAKE_IDLE_AFTER_S:.0f} s",
            }
        )

    return reasons


# Un motif deja servi n'est pas re-servi. Borne du souvenir : au-dela, les plus anciens
# sont oublies, ce qui les ferait au pire ressortir une fois. Perdre un souvenir est
# benin, faire grossir state.json sans limite ne l'est pas.
WAKE_SEEN_MAX = 300

# Un motif purement temporel se redeclenche par le temps, jamais par un fait nouveau :
# le marquer comme vu l'eteindrait definitivement des le premier tour.
WAKE_KINDS_JAMAIS_MARQUES = ("control-round",)


def _wake_key(motif: dict) -> str:
    return f"{motif.get('kind')}|{motif.get('task')}|{motif.get('detail')}"


def wake_new(chantier_id: str) -> list[dict]:
    """Motifs de reveil pas encore servis pour ce chantier, et les marque comme servis.

    wake_reasons() est pure : elle recalcule tout l'etat courant a chaque appel. Servie
    telle quelle par tick(), elle recrachait indefiniment les memes motifs, un par tache
    terminee ; sur un chantier de vingt taches, chaque tour de controle en rendait vingt,
    et le seul motif reellement nouveau se noyait dedans. Trouve en parcours reel.

    L'appelant qui veut l'etat complet, sans filtrage ni effet de bord, appelle
    wake_reasons() directement.
    """
    motifs = wake_reasons(chantier_id)
    with store.locked() as state:
        chantier = _get_chantier(state, chantier_id)
        vus = chantier.setdefault("wakeSeen", [])
        deja = set(vus)
        neufs = [m for m in motifs if _wake_key(m) not in deja]
        for m in neufs:
            if m.get("kind") in WAKE_KINDS_JAMAIS_MARQUES:
                continue
            vus.append(_wake_key(m))
        if len(vus) > WAKE_SEEN_MAX:
            del vus[: len(vus) - WAKE_SEEN_MAX]
    return neufs


# ===========================================================================
# tick()
# ===========================================================================


def _latest_question(state: dict, task_id: str) -> dict | None:
    candidates = [q for q in state["questions"].values() if q["tache"] == task_id]
    if not candidates:
        return None
    return max(candidates, key=lambda q: q["askedAt"])


def _tick_one(chantier_id: str) -> list[str]:
    """Etapes 2 a 8 de tick() pour UN chantier. Leve sur toute anomalie inattendue ;
    l'appelant (tick()) capture cette levee sans laisser un chantier casse empecher la
    reconciliation des autres (voir le docstring du module).

    Journalise ses propres evenements au fil de l'eau plutot qu'a la toute fin : si une
    etape plus loin dans la liste leve, ce qui a deja ete accompli reste visible au
    journal au lieu d'etre perdu avec l'exception.
    """
    state = store.load()
    if chantier_id not in state["chantiers"]:
        raise ControleError(f"campaign not found: {chantier_id}")

    events: list[str] = []

    # -- collecte hors verrou : lecture de fichiers et appels tmux ne doivent jamais
    # avoir lieu pendant que state.json.lock est detenu, meme raisonnement que
    # capteur.run() qui relache le verrou autour de l'execution du script (voir
    # capteur.py). --------------------------------------------------------------
    running = [t for t in _sorted_tasks(state, chantier_id) if t["state"] == "running"]
    report_raw: dict[str, str] = {}
    pane_alive: dict[str, bool] = {}
    for t in running:
        rp = report.path(t["id"], t["chantier"])
        if rp.exists():
            report_raw[t["id"]] = rp.read_text(encoding="utf-8")
        pane_id = t.get("paneId")
        if pane_id:
            pane_alive[t["id"]] = panes.alive(pane_id)

    to_send: list[tuple[str, str]] = []

    with store.locked() as state:
        if chantier_id not in state["chantiers"]:
            raise ControleError(f"campaign not found: {chantier_id}")

        # Etape 2 (I2) : le rapport est lu EN PREMIER. S'il est present, il tranche
        # seul ; l'etape 3 ci-dessous ne regarde jamais le pane d'une tache deja
        # traitee ici.
        for t in running:
            task = state["taches"].get(t["id"])
            if task is None or task["state"] != "running":
                continue
            raw = report_raw.get(t["id"])
            if raw is None:
                continue
            events.extend(report.apply(state, t["id"], raw))
            fresh = state["taches"][t["id"]]
            if fresh["state"] == "waiting":
                # Piege connu : report.apply() fait passer la tache en "waiting" mais
                # ne cree AUCUNE entree dans state["questions"]. C'est le travail de
                # tick(). pourHumain vaut toujours False ici : une question issue d'un
                # rapport d'executante arrive chez l'orchestratrice d'abord, jamais
                # directement chez l'humain (SKILL.md, section "Les questions").
                question_text = (fresh.get("report") or {}).get("question")
                qid = store.next_id(state, "question")
                state["questions"][qid] = {
                    "id": qid,
                    "chantier": chantier_id,
                    "tache": t["id"],
                    "question": question_text,
                    "options": [],
                    "pourHumain": False,
                    "answer": None,
                    "askedAt": store.now(),
                    "answeredAt": None,
                }
                events.append(f"{qid} created for {t['id']}")

        # Etape 3 : pane mort au-dela du delai SANS rapport -> blocked. Un pane vivant
        # mais inactif n'est pas un echec, c'est un tour fini sans signal ; on ne fait
        # rien dans ce cas, distinct du cas mort.
        for t in running:
            task = state["taches"].get(t["id"])
            if task is None or task["state"] != "running":
                continue
            if t["id"] in report_raw:
                continue
            if pane_alive.get(t["id"], True):
                continue
            elapsed = _elapsed_seconds(task.get("startedAt"))
            if elapsed is not None and elapsed >= PANE_DEAD_GRACE_S:
                # Point 3 du contrat : ne dit pas seulement qu'un pane est mort, mais
                # LEQUEL, et ou chercher la trace ecrite : le brief envoye et le
                # rapport attendu vivent tous deux sur disque, a des chemins fixes
                # (memes conventions que prompt.brief_executante() et report.path()).
                pane_id = t.get("paneId")
                brief_path = store.home() / "briefs" / t["chantier"] / f"{t['id']}.md"
                rapport_path = report.path(t["id"], t["chantier"])
                raison = (
                    f"{PANE_MORT_RAISON} ({elapsed:.0f} s after launch), "
                    f"pane {pane_id}: brief {brief_path}, report expected {rapport_path}"
                )
                task["state"] = "blocked"
                task["error"] = raison
                # Jamais une propagation : ce blocage est pour raison propre a la tache.
                # Ecrit explicitement, sans se fier a une valeur par defaut ailleurs, pour
                # que chantier.unblock_propagated() ne le releve jamais (voir son
                # discriminant blockedCause).
                task["blockedCause"] = None
                events.append(f"{t['id']} blocked: {raison}")

        # Etape 4 : tache waiting dont la question est repondue -> reponse injectee
        # dans le pane (envoi effectif fait hors verrou, ci-dessous), tache reprise.
        waiting = [tt for tt in _sorted_tasks(state, chantier_id) if tt["state"] == "waiting"]
        for task in waiting:
            q = _latest_question(state, task["id"])
            if q is None or q["answer"] is None:
                continue
            pane_id = task.get("paneId")
            if not pane_id:
                continue
            to_send.append((pane_id, f"Answer: {q['answer']}"))
            task["state"] = "running"
            events.append(f"{task['id']} resumes, answer injected")

        # Etape 5. propagate_failures() confine desormais sa cascade au seul chantier
        # vise (point F du contrat, decision cote chantier.py) : call site mis a jour
        # en consequence, chantier_id est deja le parametre de _tick_one().
        blocked = chantier.propagate_failures(state, chantier_id)
        events.extend(f"{tid} blocked by dependency" for tid in blocked)

        # Etape 5.5 : leve ce que l'etape 5 (ou un tour precedent) a pose, une fois la
        # cause redevenue saine. Bug reel corrige ici : une tache bloquee en cascade
        # restait bloquee pour toujours meme apres que sa dependance, relancee, ait
        # reellement reussi (done + checklist cochee) ; le blocage pose par propagation
        # n'etait jamais leve. Ne touche jamais une tache bloquee pour sa propre raison
        # (pane mort, rapport illisible, dialogue de confiance) : voir le discriminant
        # blockedCause dans chantier.unblock_propagated().
        unblocked = chantier.unblock_propagated(state)
        events.extend(f"{tid} unblocked: dependency(ies) healthy again" for tid in unblocked)

    # Envois reels hors verrou (I6 : deux appels tmux separes, geres par panes.send()).
    for pane_id, texte in to_send:
        panes.send(pane_id, texte)

    # Etape 6 : capteur, entierement hors verrou (capteur.run() gere le sien, et ne
    # doit jamais etre appele depuis l'interieur d'un store.locked() deja detenu, sous
    # peine de LockTimeoutError puisque le verrou n'est pas reentrant).
    if capteur.due(chantier_id):
        try:
            capteur.run(chantier_id)
        except capteur.CapteurError as exc:
            events.append(f"sensor: execution failed ({exc})")
        status = capteur.status(chantier_id)
        if status["signal"] == "waking":
            events.append(
                f"sensor waking: {status['consecutiveFailures']} consecutive failures"
            )
        elif status["signal"] == "frozen":
            events.append(f"sensor frozen: {status['identicalCycles']} identical cycles")

    # Etape 7 : lu apres la sauvegarde du bloc verrouille ci-dessus, pour voir les
    # rapports fraichement appliques a l'etape 2 (scope_drift/fausse_completion font
    # chacune leur propre store.load(), qui ne verrait pas des mutations encore en
    # memoire dans un state non sauvegarde).
    for drift in scope_drift(chantier_id):
        events.append(f"scope drift {drift['task']}: {drift['detail']}")
    for fc in fausse_completion(chantier_id):
        if fc["kind"] != "unavailable":
            events.append(f"false completion: {fc['detail']}")

    # Etape 8 : tous les faits observables partent au journal sous ORDO, jamais ORCH
    # ni USER (I11) : ce sont des constats, pas des arbitrages.
    for texte in events:
        journal.write(chantier_id, "ORDO", texte)

    if events:
        with store.locked() as state:
            ch = state["chantiers"].get(chantier_id)
            if ch is not None:
                ch["lastEvent"] = store.now()

    return events


def _expire_propositions() -> dict[str, list[str]]:
    """Etape 1, globale : toute proposition "pending" dont les 45 s sont passees,
    quel que soit son chantier. Renvoie les evenements groupes par chantier pour que
    l'appelant puisse les rattacher au bon journal et au bon resultat par chantier.

    Volontairement global meme quand tick() ne reconcilie qu'un seul chantier : la
    deadline d'une proposition est un fait horaire independant de ce que l'appelant a
    demande de reconcilier ce tour-ci, et la retarder pour les autres chantiers
    n'apporterait rien.
    """
    by_chantier: dict[str, list[str]] = {}
    with store.locked() as state:
        expired = plan.expire_due(state)
        for pid in expired:
            prop = state["propositions"][pid]
            if prop["state"] == "accepted":
                texte = f"proposal {pid} auto-accepted after deadline expiration"
            else:
                texte = f"proposal {pid} auto-rejected after deadline expiration: {prop.get('refus')}"
            by_chantier.setdefault(prop["chantier"], []).append(texte)
    return by_chantier


def tick(chantier_id: str | None = None) -> dict:
    """Reconciliation complete. Sans argument, reconcilie tous les chantiers "open" ;
    avec un chantier_id, reconcilie seulement celui-la (l'etape 1 reste globale, voir
    _expire_propositions()).

    Renvoie {"at": horodatage, "chantiers": {id: {"events": [...], "error": str|None}}}.
    Une entree d'erreur signifie que ce chantier a leve pendant sa reconciliation ; les
    autres chantiers du meme appel ne sont jamais affectes (voir le docstring du
    module) : chacun est traite dans son propre bloc protege, l'erreur est capturee et
    journalisee sous ORDO au lieu de remonter et d'interrompre la boucle.
    """
    result: dict = {"at": store.now(), "chantiers": {}}

    try:
        expired_by_chantier = _expire_propositions()
    except Exception as exc:  # une proposition corrompue ne doit pas bloquer le tick
        expired_by_chantier = {}
        result["expireError"] = str(exc)

    for cid, textes in expired_by_chantier.items():
        for texte in textes:
            with suppress(Exception):
                journal.write(cid, "ORDO", texte)

    if chantier_id is not None:
        targets = [chantier_id]
    else:
        state = store.load()
        targets = sorted(
            cid for cid, ch in state["chantiers"].items() if ch["state"] == "open"
        )

    for cid in targets:
        try:
            events = list(expired_by_chantier.get(cid, []))
            events += _tick_one(cid)
            result["chantiers"][cid] = {"events": events, "error": None}
        except Exception as exc:
            # LE defaut a ne jamais reproduire : cette exception s'arrete ICI, elle ne
            # remonte jamais au-dela de ce chantier. Les chantiers suivants de la
            # boucle sont traites normalement.
            with suppress(Exception):
                journal.write(cid, "ORDO", f"tick failed: {exc}")
            result["chantiers"][cid] = {"events": [], "error": str(exc)}

    return result
