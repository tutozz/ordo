"""Tests unitaires de ordo/store.py.

Isoles par ORDO_HOME : chaque test pointe sur un repertoire temporaire propre. Aucun
test de ce fichier ne doit toucher le vrai ~/.claude/ordo.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import store


class StoreTestCase(unittest.TestCase):
    """Cree un ORDO_HOME temporaire par test et restaure l'environnement apres coup."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-store-test-")
        self._prev_home = os.environ.get("ORDO_HOME")
        os.environ["ORDO_HOME"] = self._tmp

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("ORDO_HOME", None)
        else:
            os.environ["ORDO_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestHome(StoreTestCase):
    def test_home_creates_data_tree(self):
        base = store.home()
        self.assertEqual(base, Path(self._tmp))
        for sub in ("reports", "sensors", "journal", "briefs", "archives"):
            self.assertTrue((base / sub).is_dir(), f"{sub} devrait exister sous home()")

    def test_home_is_idempotent(self):
        first = store.home()
        second = store.home()
        self.assertEqual(first, second)

    def test_home_defaults_to_dot_claude_ordo_when_unset(self):
        os.environ.pop("ORDO_HOME", None)
        fake_home = Path(self._tmp) / "fake-home"
        with mock.patch.object(Path, "home", return_value=fake_home):
            base = store.home()
        self.assertEqual(base, fake_home / ".claude" / "ordo")


class TestCanon(StoreTestCase):
    def test_canon_empty_string_stays_empty(self):
        # os.path.realpath("") resout au repertoire courant : un chemin de projet vide
        # deviendrait "le dossier ou tourne Ordo". Defaut deja trouve en revue.
        self.assertEqual(store.canon(""), "")

    def test_canon_expands_tilde(self):
        self.assertEqual(store.canon("~"), os.path.realpath(os.path.expanduser("~")))

    def test_canon_resolves_dot_dot_segments(self):
        target = Path(self._tmp) / "projet"
        target.mkdir()
        self.assertEqual(store.canon(f"{target}/../projet"), str(target.resolve()))

    def test_i3_canon_resout_les_liens_symboliques(self):
        # I3 : tout chemin passe par canon() avant d'etre stocke ou compare. macOS resout
        # /var en /private/var ; sans canon(), deux chemins de la meme ressource comparent
        # comme differents, en silence, et une comparaison brute casse tout.
        reel = Path(self._tmp) / "reel"
        reel.mkdir()
        lien = Path(self._tmp) / "lien"
        lien.symlink_to(reel)
        self.assertNotEqual(str(lien), str(reel))
        self.assertEqual(store.canon(lien), store.canon(reel))


class TestNow(StoreTestCase):
    def test_now_matches_iso8601_utc_with_z(self):
        self.assertRegex(store.now(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestLoadSave(StoreTestCase):
    def test_load_creates_skeleton_when_absent(self):
        state = store.load()
        self.assertEqual(state["version"], 1)
        self.assertEqual(
            state["seq"], {"chantier": 0, "tache": 0, "question": 0, "proposition": 0}
        )
        self.assertEqual(state["chantiers"], {})
        self.assertEqual(state["taches"], {})
        self.assertEqual(state["questions"], {})
        self.assertEqual(state["propositions"], {})
        self.assertTrue((store.home() / "state.json").exists())

    def test_save_load_roundtrip(self):
        state = store.load()
        state["chantiers"]["c-01"] = {"id": "c-01", "slug": "test"}
        store.save(state)
        reloaded = store.load()
        self.assertEqual(reloaded["chantiers"]["c-01"]["slug"], "test")

    def test_save_leaves_no_stray_tmp_files(self):
        store.save(store.load())
        self.assertEqual(list(store.home().glob(".state-*.tmp")), [])

    def test_save_failure_cleans_tmp_and_preserves_previous_state(self):
        store.save(store.load())
        before = (store.home() / "state.json").read_text(encoding="utf-8")
        with mock.patch("ordo.store.json.dump", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                store.save({"broken": True})
        after = (store.home() / "state.json").read_text(encoding="utf-8")
        self.assertEqual(before, after)
        self.assertEqual(list(store.home().glob(".state-*.tmp")), [])


class TestNextId(StoreTestCase):
    def test_next_id_increments_with_prefix(self):
        state = store.load()
        self.assertEqual(store.next_id(state, "tache"), "t-01")
        self.assertEqual(store.next_id(state, "tache"), "t-02")
        self.assertEqual(store.next_id(state, "chantier"), "c-01")
        self.assertEqual(store.next_id(state, "question"), "q-01")
        self.assertEqual(store.next_id(state, "proposition"), "p-01")

    def test_next_id_unknown_kind_raises(self):
        state = store.load()
        with self.assertRaises(ValueError):
            store.next_id(state, "unknown")


class TestLocked(StoreTestCase):
    def test_locked_yields_state_and_saves_on_normal_exit(self):
        with store.locked() as state:
            state["chantiers"]["c-99"] = {"id": "c-99"}
        reloaded = store.load()
        self.assertIn("c-99", reloaded["chantiers"])

    def test_locked_does_not_save_on_exception(self):
        store.save(store.load())
        with self.assertRaises(ValueError):
            with store.locked() as state:
                state["chantiers"]["c-88"] = {"id": "c-88"}
                raise ValueError("echec volontaire")
        reloaded = store.load()
        self.assertNotIn("c-88", reloaded["chantiers"])

    def test_locked_releases_lock_after_use(self):
        with store.locked():
            pass
        self.assertFalse((store.home() / "state.json.lock").exists())

    def test_locked_relit_etat_dans_le_verrou_pas_une_copie_anterieure(self):
        store.save(store.load())
        with store.locked() as state:
            state["seq"]["tache"] = 5
        # un autre writer modifie l'etat pendant qu'on ne detient pas le verrou
        external = store.load()
        external["seq"]["tache"] = 99
        store.save(external)
        with store.locked() as state:
            # doit voir 99, pas 5 : locked() doit relire l'etat A L'INTERIEUR du verrou,
            # jamais utiliser une copie chargee avant l'acquisition. Course deja trouvee
            # en revue.
            self.assertEqual(state["seq"]["tache"], 99)

    def test_lock_serializes_concurrent_threads(self):
        store.save(store.load())
        events: list[tuple[int, str]] = []

        def worker(marker: int) -> None:
            with store.locked() as state:
                events.append((marker, "enter"))
                time.sleep(0.05)
                events.append((marker, "exit"))
                state.setdefault("chantiers", {})[f"c-{marker}"] = {"id": f"c-{marker}"}

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        depth = 0
        for _, kind in events:
            depth += 1 if kind == "enter" else -1
            self.assertLessEqual(depth, 1, "deux sections verrouillees actives a la fois")

        final = store.load()
        self.assertEqual(len(final["chantiers"]), 3)

    def test_lock_vole_si_pid_mort_et_verrou_perime(self):
        lock_path = store.home() / "state.json.lock"
        dead_pid = 2**30  # tres improbable que ce pid existe reellement
        lock_path.write_text(str(dead_pid), encoding="utf-8")
        stale = time.time() - store.LOCK_STALE_AFTER - 1
        os.utime(lock_path, (stale, stale))

        started = time.monotonic()
        with store.locked() as state:
            state["chantiers"]["c-vole"] = {"id": "c-vole"}
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0, "le verrou perime aurait du etre vole immediatement")
        self.assertIn("c-vole", store.load()["chantiers"])

    def test_lock_pas_vole_si_verrou_recent_et_pid_mort(self):
        lock_path = store.home() / "state.json.lock"
        dead_pid = 2**30
        lock_path.write_text(str(dead_pid), encoding="utf-8")  # mtime = maintenant
        with mock.patch.object(store, "LOCK_WAIT_TIMEOUT", 0.3), mock.patch.object(
            store, "LOCK_POLL_INTERVAL", 0.02
        ):
            with self.assertRaises(store.LockTimeoutError):
                with store.locked():
                    pass
        lock_path.unlink()

    def test_lock_timeout_explicite_si_pid_vivant(self):
        lock_path = store.home() / "state.json.lock"
        lock_path.write_text(str(os.getpid()), encoding="utf-8")  # nous-memes : vivant
        with mock.patch.object(store, "LOCK_WAIT_TIMEOUT", 0.3), mock.patch.object(
            store, "LOCK_POLL_INTERVAL", 0.02
        ):
            with self.assertRaises(store.LockTimeoutError):
                with store.locked():
                    pass
        lock_path.unlink()

    def test_lock_message_pid_vivant_dit_une_autre_session_le_detient(self):
        # Le message doit distinguer le cas ou une session travaille reellement de celui
        # ou le verrou est simplement abandonne : un pid vivant doit dire "detient" et
        # inviter a reessayer, jamais suggerer d'effacer le fichier a la main.
        lock_path = store.home() / "state.json.lock"
        lock_path.write_text(str(os.getpid()), encoding="utf-8")  # nous-memes : vivant
        with mock.patch.object(store, "LOCK_WAIT_TIMEOUT", 0.3), mock.patch.object(
            store, "LOCK_POLL_INTERVAL", 0.02
        ):
            with self.assertRaises(store.LockTimeoutError) as ctx:
                with store.locked():
                    pass
        message = str(ctx.exception)
        self.assertIn("holds", message)
        self.assertIn(str(os.getpid()), message)
        self.assertNotIn("erase", message)
        lock_path.unlink()

    def test_lock_message_pid_mort_dit_verrou_abandonne_et_le_chemin(self):
        # Pid mort mais verrou trop recent pour etre vole (LOCK_STALE_AFTER > timeout de
        # cette attente) : le message doit dire que le verrou est abandonne, nommer le
        # pid mort et donner le chemin du fichier, pas laisser croire qu'une session
        # travaille reellement.
        lock_path = store.home() / "state.json.lock"
        dead_pid = 2**30
        lock_path.write_text(str(dead_pid), encoding="utf-8")  # mtime = maintenant
        with mock.patch.object(store, "LOCK_WAIT_TIMEOUT", 0.3), mock.patch.object(
            store, "LOCK_POLL_INTERVAL", 0.02
        ):
            with self.assertRaises(store.LockTimeoutError) as ctx:
                with store.locked():
                    pass
        message = str(ctx.exception)
        self.assertIn("abandoned", message)
        self.assertIn(str(dead_pid), message)
        self.assertIn(str(lock_path), message)
        self.assertNotIn("holds", message)
        lock_path.unlink()

    def test_lock_message_contenu_illisible_le_dit(self):
        # Contenu du verrou non entier : ni pid vivant ni pid mort identifiable, le
        # message doit le dire explicitement plutot que de pretendre a une session.
        lock_path = store.home() / "state.json.lock"
        lock_path.write_text("pas-un-pid", encoding="utf-8")
        with mock.patch.object(store, "LOCK_WAIT_TIMEOUT", 0.3), mock.patch.object(
            store, "LOCK_POLL_INTERVAL", 0.02
        ):
            with self.assertRaises(store.LockTimeoutError) as ctx:
                with store.locked():
                    pass
        message = str(ctx.exception)
        self.assertIn("unreadable", message)
        self.assertNotIn("holds", message)
        lock_path.unlink()


if __name__ == "__main__":
    unittest.main()
