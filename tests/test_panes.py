"""Tests for ordo/panes.py against real tmux panes running bash.

No mocking of tmux: every test drives an actual tmux server, sauf la poignee de
tests dedies au point M (binaire/version) qui simulent un tmux absent ou trop
ancien via unittest.mock, sans jamais toucher au vrai serveur tmux. Sessions
sont nommees avec un prefixe reconnaissable et toujours tuees en tearDown, y
compris en cas d'echec, pour qu'aucun test ne laisse une session vivre et que
les autres sessions de la machine, quelles qu'elles soient,
ne soient jamais touchees.

Run: python3 -m unittest tests.test_panes
"""

import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import panes  # noqa: E402  (path setup must run first)
from tests import tmux_isolation  # noqa: E402

SESSION_PREFIX = "ordo-test-panes-"
HOME = os.path.expanduser("~")
# A plain, deterministic shell: --noprofile --norc skips the user's rc
# files, so tests are not at the mercy of a custom prompt (starship, a
# themed PS1, aliases) turning a predictable "bash-3.2$ " prompt into
# something a substring check cannot anticipate.
TEST_SHELL = "bash --noprofile --norc"


def _unique_session() -> str:
    return f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"


def _kill_session_quietly(session: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", f"={session}"], capture_output=True)


def _wait_for(predicate, timeout=3.0, interval=0.1):
    """Poll predicate until truthy or timeout elapses; returns the last value."""
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


def _line_present(captured: str, marker: str) -> bool:
    """True if some line of captured is exactly marker.

    Proof that a command actually ran and produced output, as opposed to
    merely being typed at the prompt: a swallowed enter leaves the marker
    sitting inside the still-uncommitted command line, never on a line of
    its own.
    """
    return any(line.strip() == marker for line in captured.split("\n"))


# Deliberately starts with SESSION_PREFIX: a client session left alive by a failing
# test still gets swept by tearDownModule()'s prefix scan below, the same safety net
# that already covers every chantier session in this file.
CLIENT_SESSION_PREFIX = f"{SESSION_PREFIX}client-"


def _attach_simulated_client(target_session: str, cols: int, rows: int) -> str:
    """Attach a real tmux client of `cols`x`rows` to target_session; return the name of
    the wrapper session hosting that client, to be torn down with
    _detach_simulated_client().

    This is the exact technique SPEC.md section 7 says window-size manual was verified
    with: a detached wrapper session whose sole pane runs `tmux attach` against the real
    target. `env -u TMUX` is required and was confirmed necessary by hand on 2026-08-09
    before writing this test: every pane, even inside a detached session, already carries
    TMUX in its environment (that is how a process knows which session it lives in), so a
    plain `tmux attach` from inside one refuses with "sessions should be nested with
    care, unset $TMUX to force" and no client ever actually attaches -- verified by the
    fact that neither hook fired and the target window never changed size until the
    unset was added.
    """
    client_session = f"{CLIENT_SESSION_PREFIX}{uuid.uuid4().hex[:8]}"
    inner = f"env -u TMUX tmux attach -t {target_session}; sleep 30"
    subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", client_session,
            "-x", str(cols), "-y", str(rows),
            "bash", "--noprofile", "--norc", "-c", inner,
        ],
        capture_output=True,
    )
    return client_session


def _detach_simulated_client(client_session: str) -> None:
    """Kill the simulated client's wrapper session: the tmux-level equivalent of the
    human closing their terminal, which is what fires the target's client-detached hook.
    """
    _kill_session_quietly(client_session)


def _fenetre(session: str) -> tuple[int, int]:
    """(width, height) of session's sole window, read straight from tmux."""
    out = subprocess.run(
        ["tmux", "list-windows", "-t", f"={session}", "-F", "#{window_width} #{window_height}"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    w, h = out.split(" ")
    return int(w), int(h)


def _fenetre_par_id(window_id: str) -> tuple[int, int]:
    """(width, height) of the window designated by window_id, read straight from tmux.

    list-windows only takes a target-SESSION (verified against the man page and by
    hand 2026-08-09), so a window_id is resolved through display-message instead,
    which accepts a target-pane and happily takes a bare window_id, defaulting to
    that window's active pane -- the pane's owning window is what #{window_width}/
    #{window_height} report, independent of which pane inside it is active.
    """
    out = subprocess.run(
        ["tmux", "display-message", "-p", "-t", window_id, "-F", "#{window_width} #{window_height}"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    w, h = out.split(" ")
    return int(w), int(h)


class PanesTestCase(unittest.TestCase):
    """Base case: one fresh, uniquely named session per test, always killed."""

    def setUp(self):
        self.session = _unique_session()

    def tearDown(self):
        _kill_session_quietly(self.session)


_PRIVATE_TMUX: tuple[str, str | None, str] = ("", None, "")


def setUpModule():
    """Route every tmux call in this process to a private server (tmux_isolation.py).

    These tests create dozens of real panes. Sharing the machine's tmux server with
    the user's own sessions made send-keys fail roughly one full-suite run in three
    with "no current client", while the same test passed 12 times out of 12 on its
    own. A flaky test proves nothing.
    """
    global _PRIVATE_TMUX
    _PRIVATE_TMUX = tmux_isolation.start_private_server()


def tearDownModule():
    """Safety net: sweep any ordo-test-panes- session a crashed test left behind."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    )
    for name in result.stdout.splitlines():
        if name.startswith(SESSION_PREFIX):
            _kill_session_quietly(name)
    tmux_isolation.stop_private_server(*_PRIVATE_TMUX)


class EnsureSessionTests(PanesTestCase):
    def test_creates_session_with_manual_window_size(self):
        session, window = panes.ensure_session(self.session)
        has = subprocess.run(
            ["tmux", "has-session", "-t", f"={self.session}"], capture_output=True
        )
        self.assertEqual(has.returncode, 0)
        size = subprocess.run(
            ["tmux", "show-window-options", "-t", window, "window-size"],
            capture_output=True,
            text=True,
        )
        self.assertIn("manual", size.stdout)

    def test_returns_session_and_a_stable_window_id(self):
        """Defaut B : ensure_session() rend desormais (session, window_id), avec
        window_id de la forme '@12', l'identifiant tmux stable de la fenetre."""
        result = panes.ensure_session(self.session)
        self.assertEqual(len(result), 2, result)
        session, window = result
        self.assertEqual(session, self.session)
        self.assertRegex(window, r"^@\d+$", window)

    def test_idempotent_no_duplicate_session(self):
        panes.ensure_session(self.session)
        panes.ensure_session(self.session)
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.splitlines().count(self.session), 1)

    def test_idempotent_returns_the_same_window_id(self):
        _, first_window = panes.ensure_session(self.session)
        _, second_window = panes.ensure_session(self.session)
        self.assertEqual(first_window, second_window)

    def test_label_renomme_la_fenetre_au_nom_du_chantier(self):
        panes.ensure_session(self.session, label="mon-chantier")
        out = subprocess.run(
            ["tmux", "list-windows", "-t", f"={self.session}", "-F", "#{window_name}"],
            capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(out, "mon-chantier")

    def test_pane_border_status_et_format_poses_sur_la_fenetre(self):
        _, window = panes.ensure_session(self.session)
        status = subprocess.run(
            ["tmux", "show-window-options", "-t", window, "pane-border-status"],
            capture_output=True, text=True,
        ).stdout
        fmt = subprocess.run(
            ["tmux", "show-window-options", "-t", window, "pane-border-format"],
            capture_output=True, text=True,
        ).stdout
        self.assertIn("top", status)
        self.assertIn("pane_title", fmt)

    def test_hooks_installes_sont_scopes_a_la_session_jamais_globaux(self):
        """Le hook doit apparaitre sur CETTE session ; jamais via -g, sinon il
        deborderait sur les sessions tmux de l'utilisateur qui partagent le meme
        serveur (interdiction absolue rappelee dans la consigne de ce chantier)."""
        panes.ensure_session(self.session)
        # show-hooks -t est un target-pane sur cette version de tmux (comme set-hook,
        # voir _install_attach_hooks() dans panes.py) : le ':' final est necessaire
        # pour que '=session' soit resolu en correspondance exacte plutot que de
        # planter avec "can't find pane".
        local = subprocess.run(
            ["tmux", "show-hooks", "-t", f"={self.session}:"],
            capture_output=True, text=True,
        ).stdout
        self.assertIn("client-attached", local)
        self.assertIn("client-detached", local)
        glob = subprocess.run(
            ["tmux", "show-hooks", "-g"], capture_output=True, text=True,
        ).stdout
        self.assertNotIn(self.session, glob)


class SessionExactMatchTests(PanesTestCase):
    """Defaut C : toute commande visant une session par son NOM doit faire une
    correspondance EXACTE, jamais par prefixe. Reproduit le cas reel du contrat :
    'ordo-api' ne doit jamais matcher 'ordo-api-docs'."""

    def setUp(self):
        super().setUp()
        self.longer = f"{self.session}-docs"

    def tearDown(self):
        _kill_session_quietly(self.longer)
        super().tearDown()

    def test_session_exists_does_not_match_by_prefix(self):
        panes.ensure_session(self.longer)  # self.session est un prefixe strict de self.longer
        self.assertFalse(
            panes._session_exists(self.session),
            "le nom court ne doit jamais matcher la session plus longue par prefixe",
        )

    def test_ensure_session_creates_a_distinct_session_despite_the_prefix_trap(self):
        panes.ensure_session(self.longer)
        session, window = panes.ensure_session(self.session)
        names = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertIn(self.session, names)
        self.assertIn(self.longer, names)
        _, other_window = panes.ensure_session(self.longer)
        self.assertNotEqual(
            window, other_window,
            "deux sessions distinctes doivent produire deux fenetres distinctes",
        )

    def test_kill_session_on_short_name_never_touches_the_longer_one(self):
        """Point 5, garde-fou direct : kill_session('X') ne doit jamais tuer 'X-docs'."""
        panes.ensure_session(self.longer)
        # self.session n'existe pas : sans correspondance exacte, tmux tuerait
        # self.longer par prefixe. L'appel est un no-op depuis que kill_session est
        # idempotente ; ce que ce test protege n'est pas la levee d'erreur, c'est la
        # survie de l'autre session, et cela ne se relache pas.
        panes.kill_session(self.session)
        self.assertEqual(
            subprocess.run(
                ["tmux", "has-session", "-t", f"={self.longer}"], capture_output=True
            ).returncode,
            0,
            "la session plus longue ne doit jamais etre tuee par accident",
        )


class KillSessionTests(PanesTestCase):
    """Point 5 : kill_session(session) detruit UNE session nommee, rien d'autre.
    Jamais tmux kill-server, jamais pkill tmux (interdiction absolue du contrat)."""

    def test_kill_session_destroys_the_named_session(self):
        panes.ensure_session(self.session)
        panes.kill_session(self.session)
        has = subprocess.run(
            ["tmux", "has-session", "-t", f"={self.session}"], capture_output=True
        )
        self.assertNotEqual(has.returncode, 0)

    def test_kill_session_leaves_other_sessions_untouched(self):
        other = _unique_session()
        panes.ensure_session(self.session)
        panes.ensure_session(other)
        try:
            panes.kill_session(self.session)
            has_other = subprocess.run(
                ["tmux", "has-session", "-t", f"={other}"], capture_output=True
            )
            self.assertEqual(has_other.returncode, 0, "une session non visee ne doit jamais etre tuee")
        finally:
            _kill_session_quietly(other)

    def test_kill_pane_already_dead_is_a_no_op_and_says_so(self):
        """Meme defaut que kill_session, meme cause : close --force parcourt des paneId
        stockes dans l'etat, dont beaucoup designent des panes morts depuis longtemps.

        kill() rend True quand elle a reellement detruit un pane, False quand il n'y avait
        deja plus rien. L'appelant a besoin de la difference pour ne pas annoncer un
        process tue qui ne l'a pas ete (point J, vocabulaire honnete).
        """
        sess, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, str(Path.cwd()), "sleep 30")
        self.assertTrue(panes.kill(pane_id))
        self.assertFalse(panes.kill(pane_id))

    def test_kill_session_on_nonexistent_session_is_a_no_op(self):
        """Une session deja absente est le resultat voulu, pas une erreur.

        Trouve par une fumee reelle : un chantier dont la session tmux etait deja morte
        faisait planter `close --force` APRES avoir archive, la fermeture sortant en code
        non nul alors qu'elle avait reussi. Verifier l'existence avant de tuer ne suffit
        pas non plus, la session peut mourir entre le controle et l'appel. Toute autre
        erreur tmux continue d'echouer bruyamment, elle.
        """
        panes.kill_session(self.session)  # jamais creee
        panes.ensure_session(self.session)
        panes.kill_session(self.session)
        panes.kill_session(self.session)  # deuxieme fois, idempotent

    def test_kill_session_still_raises_on_a_real_tmux_error(self):
        with mock.patch.object(
            panes,
            "_tmux",
            return_value=subprocess.CompletedProcess([], 1, "", "server exited unexpectedly"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                panes.kill_session(self.session)
        self.assertIn("kill-session", str(ctx.exception))


class AttachDetachHooksTests(PanesTestCase):
    """Demande d'ergonomie 1, verifiee contre un vrai client tmux simule (voir
    _attach_simulated_client) : la fenetre epouse le terminal de l'humain qui s'attache,
    et retrouve sa geometrie large des que plus personne ne regarde."""

    def test_attach_retrecit_la_fenetre_a_la_taille_du_client_puis_detach_restaure(self):
        _, window = panes.ensure_session(self.session)
        panes.spawn(window, HOME, TEST_SHELL)
        panes.relayout(window)
        large = _fenetre(self.session)
        # A 1 seul pane, 130x40 (WINDOW_BASE_COLS x WINDOW_BASE_ROWS) tient deja les
        # deux planchers : relayout() n'a pas eu besoin de grandir, la mesure "large"
        # est donc deterministe pour ce test, pas un artefact d'une machine lente.
        self.assertGreaterEqual(large[0], panes.PANE_MIN_USABLE_COLS)
        self.assertGreaterEqual(large[1], panes.PANE_MIN_USABLE_ROWS)

        # 23 lignes et non 24 : avec `status on`, tmux reserve une ligne au bandeau de
        # statut, donc un client de 24 lignes n'en AFFICHE que 23. Dimensionner la
        # fenetre a la hauteur brute du client laisserait une ligne hors ecran, c'est-a-
        # dire exactement le scroll que ce hook existe pour supprimer. `resize-window -A`
        # vise la zone reellement affichable ; mesure du 9 aout 2026, client 80x24 :
        # -A donne 80x23, forcer -y 24 donne 80x24 et cache une ligne.
        client = _attach_simulated_client(self.session, 80, 24)
        attendu = (80, 23)
        try:
            _wait_for(lambda: _fenetre(self.session) == attendu, timeout=5.0)
            self.assertEqual(
                _fenetre(self.session), attendu,
                "la fenetre doit suivre la zone affichable du client attache",
            )
        finally:
            _detach_simulated_client(client)

        _wait_for(lambda: _fenetre(self.session) == large, timeout=5.0)
        self.assertEqual(
            _fenetre(self.session), large,
            "la fenetre doit retrouver sa geometrie large au detachement",
        )

    def test_detach_ne_restaure_pas_tant_quun_autre_client_reste_attache(self):
        _, window = panes.ensure_session(self.session)
        panes.spawn(window, HOME, TEST_SHELL)
        panes.relayout(window)
        large = _fenetre(self.session)

        # Hauteurs attendues a une ligne pres de la taille brute du client : le bandeau
        # de statut de tmux en consomme une (voir le test precedent).
        client_a = _attach_simulated_client(self.session, 80, 24)
        client_b = _attach_simulated_client(self.session, 100, 30)
        try:
            _wait_for(lambda: _fenetre(self.session) == (100, 29), timeout=5.0)
            self.assertEqual(_fenetre(self.session), (100, 29))

            _detach_simulated_client(client_b)
            # client_a est toujours attache : le detachement de client_b ne doit PAS
            # rendre la geometrie large pendant que client_a regarde, mais il doit
            # redimensionner a client_a. Sans cette seconde partie, mesuree le 9 aout
            # 2026, la fenetre restait a la taille du client parti et le survivant
            # scrollait une fenetre dimensionnee pour quelqu'un qui n'est plus la.
            _wait_for(lambda: _fenetre(self.session) == (80, 23), timeout=5.0)
            self.assertNotEqual(
                _fenetre(self.session), large,
                "le detachement d'un second client ne doit pas restaurer la geometrie "
                "large tant qu'un premier client reste attache",
            )
            self.assertEqual(
                _fenetre(self.session), (80, 23),
                "la fenetre doit se recaler sur le client qui reste, pas garder la "
                "taille de celui qui vient de partir",
            )
        finally:
            _detach_simulated_client(client_a)

        _wait_for(lambda: _fenetre(self.session) == large, timeout=5.0)
        self.assertEqual(_fenetre(self.session), large)


class SpawnTests(PanesTestCase):
    def test_spawn_returns_pane_id_format(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        self.assertRegex(pane_id, r"^%\d+$")

    def test_spawn_sets_title(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL, title="t-01")
        rows = panes.panes(window)
        row = next(r for r in rows if r["pane_id"] == pane_id)
        self.assertEqual(row["titre"], "t-01")

    def test_spawn_on_missing_cwd_falls_back_instead_of_raising(self):
        # Verified 2026-08-09: split-window -c on a nonexistent directory
        # does not fail (tmux falls back to its own default cwd), so
        # spawn() legitimately succeeds here; there is nothing to raise on.
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(
            window, "/chemin/qui/nexiste/pas/vraiment", TEST_SHELL
        )
        self.assertRegex(pane_id, r"^%\d+$")

    def test_spawn_accepts_a_legacy_session_name_for_backward_compat(self):
        """Point B : le chemin nominal est window_id, mais un appelant qui passe
        encore un nom de session doit continuer a fonctionner raisonnablement."""
        panes.ensure_session(self.session)
        pane_id = panes.spawn(self.session, HOME, TEST_SHELL)
        self.assertRegex(pane_id, r"^%\d+$")


class WindowTargetingTests(PanesTestCase):
    """Defaut B, le coeur du chantier. Preuve directe et reproductible : quand la
    session porte une SECONDE fenetre devenue courante (un humain attache qui ouvre
    un nouvel onglet), spawn()/relayout()/panes() doivent quand meme atteindre la
    fenetre d'ORIGINE via son window_id, jamais la fenetre courante de la session.

    Verifie a la main le 2026-08-09 avant d'ecrire ce test : `split-window -t
    <session>` (l'ancien code) atterrit dans la fenetre courante nouvellement creee ;
    `split-window -t <window_id>` atterrit dans la bonne fenetre quoi qu'il arrive.
    """

    def _ouvre_une_seconde_fenetre_qui_devient_courante(self) -> None:
        subprocess.run(["tmux", "new-window", "-t", self.session], capture_output=True)

    def test_spawn_targets_the_original_window_even_when_another_is_current(self):
        session, window = panes.ensure_session(self.session)
        self._ouvre_une_seconde_fenetre_qui_devient_courante()

        pane_id = panes.spawn(window, HOME, TEST_SHELL)

        listing = subprocess.run(
            ["tmux", "list-panes", "-t", window, "-F", "#{pane_id}"],
            capture_output=True, text=True,
        ).stdout.splitlines()
        self.assertIn(
            pane_id, listing,
            "spawn() a cree le pane dans la mauvaise fenetre : la fenetre courante "
            "de la session au lieu de la fenetre d'origine visee par window_id",
        )

    def test_panes_lists_only_the_targeted_window_not_the_current_one(self):
        session, window = panes.ensure_session(self.session)
        panes.spawn(window, HOME, TEST_SHELL)
        self._ouvre_une_seconde_fenetre_qui_devient_courante()

        rows = panes.panes(window)

        self.assertEqual(len(rows), 1, rows)  # la fenetre d'origine n'a qu'un seul pane

    def test_relayout_targets_the_original_window_even_when_another_is_current(self):
        session, window = panes.ensure_session(self.session)
        panes.spawn(window, HOME, TEST_SHELL)
        self._ouvre_une_seconde_fenetre_qui_devient_courante()

        panes.relayout(window)

        geo = _fenetre_par_id(window)
        self.assertGreaterEqual(geo[0], panes.PANE_MIN_USABLE_COLS)
        self.assertGreaterEqual(geo[1], panes.PANE_MIN_USABLE_ROWS)
        rows = panes.panes(window)
        self.assertEqual(len(rows), 1, rows)
        self.assertTrue(rows[0]["vivant"])

    def test_relayout_accepts_a_legacy_session_name_for_backward_compat(self):
        panes.ensure_session(self.session)
        panes.spawn(self.session, HOME, TEST_SHELL)
        panes.relayout(self.session)  # ne doit pas lever


class SpawnReuseTests(PanesTestCase):
    """Defaut 3 : le premier spawn() d'un chantier reutilise le pane initial
    de ensure_session() au lieu d'en creer un second a cote."""

    def test_first_spawn_reuses_the_seed_pane_no_extra_pane_created(self):
        _, window = panes.ensure_session(self.session)
        seed = panes.panes(window)
        self.assertEqual(len(seed), 1, seed)
        seed_id = seed[0]["pane_id"]

        pane_id = panes.spawn(window, HOME, TEST_SHELL)

        self.assertEqual(pane_id, seed_id, "le spawn aurait du reutiliser le pane seed")
        rows = panes.panes(window)
        self.assertEqual(len(rows), 1, rows)

    def test_reused_pane_id_is_valid_for_send_and_capture_like_any_other(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        marker = f"REUSE_MARK_{uuid.uuid4().hex[:6]}"
        panes.send(pane_id, f"echo {marker}")
        self.assertTrue(
            _wait_for(lambda: _line_present(panes.capture(pane_id), marker), timeout=3.0)
        )
        self.assertTrue(panes.alive(pane_id))

    def test_second_spawn_splits_a_new_pane_instead_of_reusing(self):
        _, window = panes.ensure_session(self.session)
        first = panes.spawn(window, HOME, TEST_SHELL)
        panes.relayout(window)

        second = panes.spawn(window, HOME, TEST_SHELL)

        self.assertNotEqual(first, second)
        rows = panes.panes(window)
        self.assertEqual(len(rows), 2, rows)
        ids = {r["pane_id"] for r in rows}
        self.assertEqual(ids, {first, second})
        # ni l'un ni l'autre pane n'est un pane vide : les deux ont ete
        # explicitement lances par spawn(), aucun shell idle residuel.
        for pid in (first, second):
            self.assertTrue(panes.alive(pid))

    def test_a_lone_already_claimed_pane_is_never_silently_reused(self):
        # Une seule tache pour tout le chantier, tuee puis relancee : au
        # moment du second spawn() la session ne contient a nouveau qu'un
        # seul pane vivant, mais ce n'est PAS le seed pane vierge ;
        # _reusable_seed_pane doit le refuser, spawn() doit donc splitter.
        _, window = panes.ensure_session(self.session)
        first = panes.spawn(window, HOME, TEST_SHELL)
        panes.relayout(window)
        second = panes.spawn(window, HOME, TEST_SHELL)
        panes.relayout(window)
        panes.kill(first)

        rows = panes.panes(window)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["pane_id"], second)

        third = panes.spawn(window, HOME, TEST_SHELL)

        self.assertNotEqual(
            third, second, "un pane deja claim ne doit jamais etre reutilise"
        )
        self.assertTrue(panes.alive(second))
        self.assertTrue(panes.alive(third))


class RelayoutAndDimensionsTests(PanesTestCase):
    def test_dimensions_formula_matches_spec(self):
        # rows now scales by the SAME `side` multiplier as cols, not
        # ceil(n/side): see the WINDOW_BASE_ROWS module comment in panes.py
        # for the real-tmux measurement (n=2 tiled as two full-width panes
        # stacked at 17/18 rows, not side by side at full height) that the
        # old ceil(n/side) formula got wrong. This is a starting guess only;
        # relayout() grows past it if real panes still come back too short
        # (see test_relayout_every_pane_clears_both_floors_n_1_to_6 below).
        self.assertEqual(panes._dimensions(1), (130, 40))
        self.assertEqual(panes._dimensions(2), (260, 80))
        self.assertEqual(panes._dimensions(3), (260, 80))
        self.assertEqual(panes._dimensions(4), (260, 80))
        self.assertEqual(panes._dimensions(5), (390, 120))
        self.assertEqual(panes._dimensions(9), (390, 120))

    def test_dimensions_floor_is_130x40(self):
        self.assertEqual(panes._dimensions(0), (130, 40))

    def test_relayout_keeps_every_pane_above_120_columns_and_balanced(self):
        _, window = panes.ensure_session(self.session)
        # The first spawn() reuses ensure_session()'s seed pane (see
        # SpawnReuseTests below), so 4 spawns here means 4 panes total, not
        # 5. n=4 is a perfect square (side=2): tmux's tiled layout was
        # measured 2026-08-09 to balance that case into an even 2x2 grid,
        # unlike n=3 which legitimately tiles as two even panes on one row
        # plus one full-width pane on the other (see
        # test_relayout_every_pane_clears_both_floors_n_1_to_6 below, which
        # covers those uneven counts without asserting balance).
        for _ in range(4):
            panes.spawn(window, HOME, TEST_SHELL)
            panes.relayout(window)
        rows = panes.panes(window)
        widths = [p["largeur"] for p in rows]
        self.assertEqual(len(widths), 4, widths)
        self.assertTrue(all(w >= 120 for w in widths), widths)
        self.assertLessEqual(max(widths) - min(widths), 2, widths)

    def test_relayout_on_empty_session_does_nothing(self):
        _, window = panes.ensure_session(self.session)
        panes.relayout(window)  # must not raise on zero panes

    def test_relayout_every_pane_clears_both_floors_n_1_to_6(self):
        """Defaut 4, verifie sur de vrais panes : jamais moins de
        PANE_MIN_USABLE_COLS colonnes ni PANE_MIN_USABLE_ROWS lignes,
        pour n allant de 1 a 6, comme demande explicitement pour ce defaut.
        """
        _, window = panes.ensure_session(self.session)
        for n in range(1, 7):
            panes.spawn(window, HOME, TEST_SHELL)
            panes.relayout(window)
            rows = panes.panes(window)
            self.assertEqual(len(rows), n, rows)
            widths = [p["largeur"] for p in rows]
            heights = [p["hauteur"] for p in rows]
            self.assertTrue(
                all(w >= panes.PANE_MIN_USABLE_COLS for w in widths),
                f"n={n} widths={widths}",
            )
            self.assertTrue(
                all(h >= panes.PANE_MIN_USABLE_ROWS for h in heights),
                f"n={n} heights={heights}",
            )


class CaptureTests(PanesTestCase):
    def test_capture_strips_trailing_blank_lines(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        panes.send(pane_id, "echo CAPTURE_MARKER")
        _wait_for(lambda: _line_present(panes.capture(pane_id), "CAPTURE_MARKER"))
        text = panes.capture(pane_id, lines=80)
        lines = text.split("\n")
        self.assertNotEqual(lines[-1].strip(), "", text)

    def test_capture_joins_wrapped_lines(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        # A narrow pane on purpose: a 300 character line wraps across
        # several physical rows well under 300 columns wide, exactly the
        # condition -J exists to undo.
        subprocess.run(
            ["tmux", "resize-window", "-t", self.session, "-x", "100", "-y", "30"],
            capture_output=True,
        )
        marker = "Y" * 300
        panes.send(pane_id, f"printf '{marker}\\n'")
        self.assertTrue(_wait_for(lambda: marker in panes.capture(pane_id, join=True)))
        joined = panes.capture(pane_id, join=True)
        line_with_marker = next(l for l in joined.split("\n") if marker in l)
        self.assertGreaterEqual(len(line_with_marker), 300)

    def test_capture_on_gone_pane_raises_explicit_error(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        panes.kill(pane_id)
        with self.assertRaises(RuntimeError) as ctx:
            panes.capture(pane_id)
        self.assertIn("capture-pane", str(ctx.exception))


class FloorWarningTests(PanesTestCase):
    """Garde-fou demande explicitement : une lecture faite pendant qu'un pane est sous
    le plancher (120 colonnes x 30 lignes) doit se signaler, jamais se rendre comme un
    contenu fiable. is_degraded() est le signal structure ; capture(warn_floor=True) est
    la mention en sortie texte, toutes deux dependent du meme plancher mesure."""

    def _retrecir_sous_le_plancher(self, pane_id: str) -> None:
        subprocess.run(
            ["tmux", "resize-window", "-t", self.session, "-x", "100", "-y", "20"],
            capture_output=True,
        )

    def test_is_degraded_false_a_la_geometrie_par_defaut(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        self.assertFalse(panes.is_degraded(pane_id))

    def test_is_degraded_true_sous_le_plancher(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        self._retrecir_sous_le_plancher(pane_id)
        self.assertTrue(panes.is_degraded(pane_id))

    def test_is_degraded_false_pour_un_pane_introuvable(self):
        # Un pane absent est couvert par alive()/capture() qui rendent/levent
        # explicitement "mort" ; is_degraded() n'invente pas un second signal pour le
        # meme fait, elle rend simplement False (ni degrade ni fiable a discuter ici).
        self.assertFalse(panes.is_degraded("%9999999"))

    def test_capture_sans_warn_floor_ne_mentionne_jamais_lavertissement(self):
        """Mutation-provable : par defaut (busy()/wait_ready()), aucune requete
        supplementaire, donc aucun avertissement, meme sous le plancher."""
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        self._retrecir_sous_le_plancher(pane_id)
        texte = panes.capture(pane_id)
        self.assertNotIn(panes.FLOOR_WARNING_PREFIX, texte)

    def test_capture_warn_floor_signale_le_pane_sous_le_plancher(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        self._retrecir_sous_le_plancher(pane_id)
        texte = panes.capture(pane_id, warn_floor=True)
        self.assertTrue(texte.startswith(panes.FLOOR_WARNING_PREFIX), texte)
        self.assertIn(pane_id, texte)
        self.assertIn(f"{panes.PANE_MIN_USABLE_COLS}x{panes.PANE_MIN_USABLE_ROWS}", texte)

    def test_capture_warn_floor_silencieux_a_la_geometrie_par_defaut(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        texte = panes.capture(pane_id, warn_floor=True)
        self.assertNotIn(panes.FLOOR_WARNING_PREFIX, texte)

    def test_capture_warn_floor_ne_perd_aucun_contenu_reel(self):
        # L'avertissement s'ajoute, il ne remplace rien : le contenu reellement ecrit
        # dans le pane reste present et complet dans le texte rendu.
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        marker = f"FLOOR_MARK_{uuid.uuid4().hex[:6]}"
        panes.send(pane_id, f"echo {marker}")
        self._retrecir_sous_le_plancher(pane_id)
        self.assertTrue(_wait_for(lambda: _line_present(panes.capture(pane_id), marker)))
        texte = panes.capture(pane_id, warn_floor=True)
        self.assertTrue(_line_present(texte, marker), texte)


class WaitReadyTests(PanesTestCase):
    """Defauts 1 et 2. Un vrai `claude` n'est jamais relance ici (cf. consigne du
    chantier) ; le contenu du TUI mesure reellement (voir les constantes
    READY_MARKERS / TRUST_DIALOG_MARKERS dans panes.py) est simule dans un pane bash
    avec un delai, pour prouver que wait_ready() attend reellement et ne se contente
    pas d'un coup d'oeil immediat."""

    def test_wait_ready_blocks_then_returns_pret_once_marker_appears(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        # Le marqueur n'existe pas encore quand wait_ready() est appele : s'il
        # rendait "ready" ici, ce serait la preuve qu'il ne poll pas reellement.
        panes.send(pane_id, "sleep 0.6; echo 'Claude Code v2.1.226'")
        etat = panes.wait_ready(pane_id, timeout=5.0)
        self.assertEqual(etat, panes.PANE_ETAT_PRET)

    def test_wait_ready_returns_confiance_when_trust_dialog_appears(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        panes.send(
            pane_id,
            "sleep 0.4; echo 'Quick safety check: Is this a project you trust?'",
        )
        etat = panes.wait_ready(pane_id, timeout=5.0)
        self.assertEqual(etat, panes.PANE_ETAT_CONFIANCE)

    def test_wait_ready_prefers_confiance_when_both_markers_present(self):
        # Decision produit : Ordo n'approuve jamais un dossier automatiquement.
        # Si le pane porte les deux motifs a la fois (ecran de transition,
        # ambiguite quelconque), le dialogue de confiance doit toujours gagner.
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        panes.send(
            pane_id,
            "echo 'Claude Code v2.1.226'; echo 'Quick safety check: ...'",
        )
        etat = panes.wait_ready(pane_id, timeout=5.0)
        self.assertEqual(etat, panes.PANE_ETAT_CONFIANCE)

    def test_wait_ready_finds_marker_pushed_to_the_top_of_a_tall_pane(self):
        # Reproduces a real, measured failure mode: relayout() now produces
        # panes well over 40 rows tall for any multi-task chantier, and
        # claude's real TUI renders its ready banner pinned to the TOP with
        # blank rows filling the gap down to the input box at the bottom
        # (measured 2026-08-09 on a real 260x80 claude pane: banner at lines
        # 2-4, box/status at line 80). capture() only strips TRAILING blank
        # lines, so that gap survives; a fixed "last 40 lines" slice, tried
        # first here, missed the banner outright against that real capture.
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        subprocess.run(
            ["tmux", "resize-window", "-t", self.session, "-x", "130", "-y", "90"],
            capture_output=True,
        )
        marker = "Claude Code v9.9.9"
        # Marqueur d'abord, puis un grand vide, puis un prompt non vide en
        # bas : la meme forme banniere-en-haut / boite-en-bas que la vraie
        # mesure, dans un pane bien plus haut que 40 lignes.
        panes.send(pane_id, f"echo '{marker}'; for i in $(seq 1 70); do echo; done")
        self.assertTrue(
            _wait_for(lambda: marker in panes.capture(pane_id, lines=0), timeout=3.0),
            "le marqueur devrait etre visible en capture complete avant meme d'appeler wait_ready",
        )
        etat = panes.wait_ready(pane_id, timeout=5.0)
        self.assertEqual(etat, panes.PANE_ETAT_PRET)

    def test_wait_ready_raises_french_error_naming_the_pane_on_timeout(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)  # reste idle, aucun marqueur
        with self.assertRaises(RuntimeError) as ctx:
            panes.wait_ready(pane_id, timeout=0.6)
        message = str(ctx.exception)
        self.assertIn(pane_id, message)
        self.assertIn("ready", message)

    def test_wait_ready_refuses_to_target_callers_own_pane(self):
        fake_pane = "%999999"
        old = os.environ.get("TMUX_PANE")
        os.environ["TMUX_PANE"] = fake_pane
        try:
            with self.assertRaises(ValueError):
                panes.wait_ready(fake_pane, timeout=0.1)
        finally:
            if old is None:
                os.environ.pop("TMUX_PANE", None)
            else:
                os.environ["TMUX_PANE"] = old


class SendTests(PanesTestCase):
    def test_send_refuses_raw_escape_byte(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        with self.assertRaises(ValueError):
            panes.send(pane_id, "\x1b")

    def test_send_refuses_escape_keyword(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        with self.assertRaises(ValueError):
            panes.send(pane_id, "Escape")

    def test_send_allows_text_merely_mentioning_escape(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        panes.send(pane_id, "echo 'press Escape to cancel'")
        self.assertTrue(
            _wait_for(lambda: "press Escape to cancel" in panes.capture(pane_id))
        )

    def test_invariant_i6_send_keys_two_separate_calls(self):
        """I6: the literal text (-l) and the enter (C-m) are two tmux calls.

        A single combined call turns C-m into literal characters instead of
        a real keypress, so the typed command never submits: no bare
        output line ever appears, only the still-uncommitted "echo
        MARKERC-m" typed line. Mutated to one call, this test goes red
        (confirmed 2026-08-09: merging the two send-keys calls left the
        prompt holding "echo <marker>C-m" unexecuted, no bare output line).
        """
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        marker = f"I6_MARK_{uuid.uuid4().hex[:6]}"
        panes.send(pane_id, f"echo {marker}")

        ran = _wait_for(lambda: _line_present(panes.capture(pane_id), marker), timeout=3.0)
        self.assertTrue(ran, panes.capture(pane_id))


class SelfTargetTests(PanesTestCase):
    def test_refuses_to_target_callers_own_pane(self):
        fake_pane = "%999999"
        old = os.environ.get("TMUX_PANE")
        os.environ["TMUX_PANE"] = fake_pane
        try:
            with self.assertRaises(ValueError):
                panes.send(fake_pane, "hello")
            with self.assertRaises(ValueError):
                panes.kill(fake_pane)
            with self.assertRaises(ValueError):
                panes.alive(fake_pane)
            with self.assertRaises(ValueError):
                panes.busy(fake_pane)
            with self.assertRaises(ValueError):
                panes.capture(fake_pane)
        finally:
            if old is None:
                os.environ.pop("TMUX_PANE", None)
            else:
                os.environ["TMUX_PANE"] = old


class AliveKillTests(PanesTestCase):
    def test_alive_true_then_false_after_kill(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        self.assertTrue(panes.alive(pane_id))
        panes.kill(pane_id)
        self.assertFalse(panes.alive(pane_id))

    def test_kill_on_nonexistent_pane_reports_that_nothing_was_killed(self):
        # Un pane inexistant n'est plus une erreur, mais il ne doit jamais se faire
        # passer pour un pane detruit : c'est le False qui porte cette garantie.
        self.assertFalse(panes.kill("%9999999"))

    def test_kill_still_raises_on_a_real_tmux_error(self):
        with mock.patch.object(
            panes,
            "_tmux",
            return_value=subprocess.CompletedProcess([], 1, "", "server exited unexpectedly"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                panes.kill("%42")
        self.assertIn("kill-pane", str(ctx.exception))

    def test_alive_false_for_bogus_pane_when_server_has_sessions(self):
        _, window = panes.ensure_session(self.session)
        panes.spawn(window, HOME, TEST_SHELL)
        self.assertFalse(panes.alive("%9999999"))


class BusyTests(PanesTestCase):
    def test_idle_bash_is_not_busy(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        _wait_for(lambda: panes.capture(pane_id).strip() != "", timeout=2.0)
        self.assertFalse(panes.busy(pane_id))

    def test_marker_in_output_flips_busy(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        panes.send(pane_id, "echo 'esc to interrupt'")
        self.assertTrue(_wait_for(lambda: panes.busy(pane_id), timeout=3.0))

    def test_marker_scrolled_out_of_last_20_lines_goes_idle_again(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        panes.send(pane_id, "echo 'Synthesizing'")
        self.assertTrue(_wait_for(lambda: panes.busy(pane_id), timeout=3.0))
        panes.send(pane_id, "for i in $(seq 1 40); do echo line-$i; done")
        self.assertTrue(
            _wait_for(lambda: "line-40" in panes.capture(pane_id), timeout=3.0)
        )
        self.assertFalse(panes.busy(pane_id))


class PanesListTests(PanesTestCase):
    def test_lists_metadata_for_every_pane(self):
        _, window = panes.ensure_session(self.session)
        id_a = panes.spawn(window, HOME, TEST_SHELL, title="t-01")
        id_b = panes.spawn(window, HOME, TEST_SHELL, title="t-02")
        panes.relayout(window)
        rows = panes.panes(window)
        ids = {r["pane_id"] for r in rows}
        self.assertIn(id_a, ids)
        self.assertIn(id_b, ids)
        for row in rows:
            self.assertEqual(
                set(row.keys()),
                {"pane_id", "titre", "largeur", "hauteur", "vivant", "actif", "sousPlancher"},
            )
        self.assertEqual(sum(1 for r in rows if r["actif"]), 1)

    def test_missing_session_returns_empty_list(self):
        self.assertEqual(panes.panes(self.session), [])

    def test_missing_window_id_returns_empty_list(self):
        self.assertEqual(panes.panes("@9999999"), [])

    def test_sous_plancher_false_at_default_geometry(self):
        # ensure_session() cree une fenetre a WINDOW_BASE_COLS x WINDOW_BASE_ROWS
        # (130x40), au-dessus des deux planchers (120x30) : un seul pane a la geometrie
        # par defaut ne doit jamais se dire sous le plancher.
        _, window = panes.ensure_session(self.session)
        panes.spawn(window, HOME, TEST_SHELL)
        rows = panes.panes(window)
        self.assertEqual(len(rows), 1, rows)
        self.assertFalse(rows[0]["sousPlancher"], rows)

    def test_sous_plancher_true_quand_la_fenetre_retrecit_sous_le_plancher(self):
        _, window = panes.ensure_session(self.session)
        pane_id = panes.spawn(window, HOME, TEST_SHELL)
        subprocess.run(
            ["tmux", "resize-window", "-t", self.session, "-x", "100", "-y", "20"],
            capture_output=True,
        )
        rows = panes.panes(window)
        row = next(r for r in rows if r["pane_id"] == pane_id)
        self.assertTrue(row["sousPlancher"], row)


class InvariantI5Tests(PanesTestCase):
    def test_invariant_i5_pane_id_survives_close_of_middle_pane(self):
        """I5: a pane is targeted by its pane_id, never by session:window.index.

        Three panes are created. The middle one, by current index, is
        closed. A send aimed at that pane's now-stale index lands on
        whichever pane slid into the freed slot instead: the exact
        wrong-target failure pane_id targeting exists to rule out. The two
        survivors are then proven to still receive the right message when
        addressed by the pane_id captured before the close, unaffected by
        the index reshuffle.

        Mutated (2026-08-09): changing spawn()'s -F format from
        "#{pane_id}" to an index-based "#{session_name}:#{window_index}.
        #{pane_index}" string makes the later sends target the reshuffled
        slot instead of the intended pane, and this test goes red.
        """
        _, window = panes.ensure_session(self.session)
        id_a = panes.spawn(window, HOME, TEST_SHELL)
        id_b = panes.spawn(window, HOME, TEST_SHELL)
        id_c = panes.spawn(window, HOME, TEST_SHELL)
        for pid in (id_a, id_b, id_c):
            self.assertRegex(pid, r"^%\d+$")

        listing = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-t",
                self.session,
                "-F",
                "#{pane_id} #{window_index} #{pane_index}",
            ],
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        index_of = {}
        window_index = None
        for line in listing:
            pid, win, idx = line.split(" ")
            index_of[pid] = int(idx)
            window_index = win

        low_id, mid_id, high_id = sorted((id_a, id_b, id_c), key=index_of.get)
        mid_index = index_of[mid_id]

        panes.kill(mid_id)
        self.assertFalse(panes.alive(mid_id))

        # Simulate a stale index-based sender: the slot mid_id used to
        # occupy now belongs to whichever pane shifted into it (high_id).
        stale_target = f"{self.session}:{window_index}.{mid_index}"
        wrong_marker = f"WRONG_{uuid.uuid4().hex[:6]}"
        subprocess.run(
            ["tmux", "send-keys", "-t", stale_target, "-l", f"echo {wrong_marker}"],
            capture_output=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", stale_target, "C-m"], capture_output=True
        )
        landed_on_high = _wait_for(
            lambda: _line_present(panes.capture(high_id), wrong_marker), timeout=3.0
        )
        self.assertTrue(
            landed_on_high,
            "l'envoi par index perime n'a pas atteint le pane qui a herite du slot",
        )
        self.assertFalse(_line_present(panes.capture(low_id), wrong_marker))

        # The real API: pane_id keeps working correctly for both survivors,
        # unaffected by the close and the index reshuffle.
        marker_high = f"RIGHT_HIGH_{uuid.uuid4().hex[:6]}"
        marker_low = f"RIGHT_LOW_{uuid.uuid4().hex[:6]}"
        panes.send(high_id, f"echo {marker_high}")
        panes.send(low_id, f"echo {marker_low}")
        self.assertTrue(
            _wait_for(lambda: _line_present(panes.capture(high_id), marker_high), timeout=3.0)
        )
        self.assertTrue(
            _wait_for(lambda: _line_present(panes.capture(low_id), marker_low), timeout=3.0)
        )
        self.assertFalse(_line_present(panes.capture(low_id), marker_high))
        self.assertFalse(_line_present(panes.capture(high_id), marker_low))


class TraceTests(PanesTestCase):
    """Point L : panes.TRACE, quand pose par l'appelant (le CLI, --verbose), voit
    passer la commande tmux complete avant execution. Sans activation (defaut None),
    le comportement ne change pas d'un octet."""

    def tearDown(self):
        panes.TRACE = None
        super().tearDown()

    def test_trace_none_by_default(self):
        self.assertIsNone(panes.TRACE)

    def test_trace_receives_the_full_command_before_execution(self):
        seen = []
        panes.TRACE = seen.append

        panes.ensure_session(self.session)

        self.assertTrue(seen, "TRACE aurait du recevoir au moins une commande")
        self.assertTrue(all(cmd[0] == "tmux" for cmd in seen), seen)
        self.assertTrue(
            any("has-session" in cmd for cmd in seen),
            "la toute premiere commande d'ensure_session (has-session) doit etre tracee",
        )

    def test_trace_none_does_not_change_behaviour(self):
        self.assertIsNone(panes.TRACE)
        session, window = panes.ensure_session(self.session)  # ne doit pas lever
        self.assertRegex(window, r"^@\d+$")


class TmuxAvailabilityTests(unittest.TestCase):
    """Point M : le binaire et la version de tmux sont verifies une seule fois par
    processus, jamais a chaque commande, avec un message explicite qui dit quoi
    faire. Rien ici ne touche au vrai serveur tmux : seuls shutil.which et
    subprocess.run sont simules, dans des blocs `with` etroitement scopes."""

    def setUp(self):
        self._saved_verified = panes._tmux_verified
        panes._tmux_verified = False

    def tearDown(self):
        panes._tmux_verified = self._saved_verified

    def test_raises_explicit_install_message_when_tmux_binary_missing(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                panes._ensure_tmux_ready()
        message = str(ctx.exception).lower()
        self.assertIn("tmux", message)
        self.assertIn("install", message)
        self.assertFalse(panes._tmux_verified, "un echec ne doit jamais se marquer verifie")

    def test_raises_explicit_error_when_version_below_minimum(self):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="tmux 2.3\n", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(RuntimeError) as ctx:
                panes._ensure_tmux_ready()
        message = str(ctx.exception)
        self.assertIn("2.3", message)
        self.assertIn("2.9", message)
        self.assertFalse(panes._tmux_verified)

    def test_accepts_a_version_at_exactly_the_minimum(self):
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="tmux 2.9\n", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            panes._ensure_tmux_ready()  # ne doit pas lever
        self.assertTrue(panes._tmux_verified)

    def test_check_runs_tmux_version_at_most_once_per_process(self):
        calls = []

        def counting_run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="tmux 3.7b\n", stderr="")

        with mock.patch("subprocess.run", side_effect=counting_run):
            panes._ensure_tmux_ready()
            panes._ensure_tmux_ready()
            panes._ensure_tmux_ready()

        self.assertEqual(len(calls), 1, calls)

    def test_real_tmux_on_this_machine_is_accepted(self):
        panes._ensure_tmux_ready()  # tmux reel, installe et recent : ne doit pas lever
        self.assertTrue(panes._tmux_verified)


if __name__ == "__main__":
    unittest.main()
