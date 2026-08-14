"""Etat atomique d'Ordo : chemin canonique, chargement, ecriture, verrou, identifiants.

Tout module qui touche state.json passe par ce fichier. C'est le seul endroit qui connait
le format sur disque et la strategie de verrouillage.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

# Delai apres lequel un verrou dont le pid n'existe plus est considere abandonne et peut
# etre vole. Une valeur trop courte volerait le verrou d'un process qui vient d'ecrire son
# pid mais n'a pas fini de demarrer.
LOCK_STALE_AFTER = 30.0

# Attente maximale pour obtenir le verrou avant d'abandonner explicitement. Rester bloque
# indefiniment transformerait un verrou coince en silence total pour l'appelant.
LOCK_WAIT_TIMEOUT = 10.0

# Intervalle de scrutation pendant l'attente du verrou.
LOCK_POLL_INTERVAL = 0.05

# Sous-repertoires de donnees crees eagerly par home() ; bin/, ordo/, tests/ sont du code,
# pas de la donnee, et ne sont jamais crees a l'execution.
_DATA_DIRS = ("reports", "sensors", "journal", "briefs", "archives")


class LockTimeoutError(RuntimeError):
    """Leve quand le verrou de state.json n'a pas pu etre obtenu dans le delai imparti."""


def home() -> Path:
    """Racine d'Ordo ; cree l'arbre de donnees s'il manque.

    ORDO_HOME surcharge le defaut ~/.claude/ordo. Tous les tests s'en servent pour ne
    jamais toucher le vrai dossier de l'utilisateur.
    """
    raw = os.environ.get("ORDO_HOME")
    base = Path(raw).expanduser() if raw else Path.home() / ".claude" / "ordo"
    base.mkdir(parents=True, exist_ok=True)
    for sub in _DATA_DIRS:
        (base / sub).mkdir(exist_ok=True)
    return base


def canon(p: str | Path) -> str:
    """Chemin canonique : liens resolus, ~ developpe.

    canon("") doit rendre "", jamais le repertoire courant : os.path.realpath("") resout
    a cwd, ce qui a deja fabrique un chemin de projet errone. Au-dela de ce cas, macOS
    resout /var en /private/var ; comparer des chemins non canonises casse tout, en
    silence (I3).
    """
    s = str(p)
    if s == "":
        return ""
    return os.path.realpath(os.path.expanduser(s))


def now() -> str:
    """Horodatage ISO 8601 UTC, suffixe Z, sans microsecondes."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _state_path() -> Path:
    return home() / "state.json"


def _skeleton() -> dict:
    return {
        "version": 1,
        "seq": {"chantier": 0, "tache": 0, "question": 0, "proposition": 0},
        "chantiers": {},
        "taches": {},
        "questions": {},
        "propositions": {},
    }


def load() -> dict:
    """Charge l'etat, ou cree et persiste le squelette s'il n'existe pas encore."""
    path = _state_path()
    if not path.exists():
        skeleton = _skeleton()
        save(skeleton)
        return skeleton
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save(state: dict) -> None:
    """Ecrit l'etat de facon atomique : tmp sur le meme volume, puis os.replace.

    Un crash pendant l'ecriture ne doit jamais laisser un state.json a moitie ecrit.
    os.replace est atomique sur un meme systeme de fichiers, d'ou le tmp cote a cote plutot
    que dans /tmp (volume potentiellement different).
    """
    path = _state_path()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Le process existe mais appartient a un autre utilisateur : il est vivant.
        return True
    return True


def _lock_path() -> Path:
    return home() / "state.json.lock"


def _try_acquire(lock_path: Path) -> bool:
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def _maybe_steal(lock_path: Path) -> None:
    """Vole un verrou abandonne : pid mort et verrou plus vieux que LOCK_STALE_AFTER.

    Le delai protege un process qui vient d'ecrire son pid mais n'a pas fini de demarrer ;
    sans lui, deux writers pourraient se croire tous deux titulaires du meme verrou.
    """
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
        age = time.time() - lock_path.stat().st_mtime
    except FileNotFoundError:
        return
    if age < LOCK_STALE_AFTER:
        return
    try:
        pid = int(content)
    except ValueError:
        pid = -1
    if pid > 0 and _pid_alive(pid):
        return
    with suppress(FileNotFoundError):
        lock_path.unlink()


def _lock_timeout_message(lock_path: Path) -> str:
    """Message final quand le verrou n'a pas pu etre obtenu dans le delai imparti.

    Distingue trois cas plutot que d'accuser a tort une session vivante : le pid inscrit
    peut etre vivant (une autre session travaille reellement), mort mais le verrou trop
    recent pour etre vole (LOCK_STALE_AFTER = 30 s > LOCK_WAIT_TIMEOUT = 10 s : ce cas est
    possible et n'est pas une anomalie), ou le contenu du fichier illisible. Un message qui
    dirait toujours "une autre session le detient" ferait attendre l'utilisateur devant un
    verrou abandonne alors qu'il suffirait de patienter quelques secondes ou d'effacer le
    fichier a la main.
    """
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return (
            f"lock {lock_path} unavailable after {LOCK_WAIT_TIMEOUT:.0f}s of waiting, "
            "then vanished in the meantime; retry"
        )
    try:
        pid = int(content)
    except ValueError:
        return (
            f"lock {lock_path} unavailable after {LOCK_WAIT_TIMEOUT:.0f}s of waiting; "
            f"lock content unreadable ({content!r}), erase it by hand if no Ordo "
            "session is running"
        )
    if _pid_alive(pid):
        return (
            f"lock {lock_path} unavailable after {LOCK_WAIT_TIMEOUT:.0f}s of waiting; "
            f"another Ordo session (pid {pid}) holds it, retry"
        )
    return (
        f"lock {lock_path} unavailable after {LOCK_WAIT_TIMEOUT:.0f}s of waiting; "
        f"abandoned by process {pid}, already dead, it will be stolen automatically "
        f"within seconds (delay of {LOCK_STALE_AFTER:.0f}s), or erase it by hand: "
        f"{lock_path}"
    )


def _acquire_lock() -> Path:
    lock_path = _lock_path()
    deadline = time.monotonic() + LOCK_WAIT_TIMEOUT
    while True:
        if _try_acquire(lock_path):
            return lock_path
        _maybe_steal(lock_path)
        if _try_acquire(lock_path):
            return lock_path
        if time.monotonic() >= deadline:
            raise LockTimeoutError(_lock_timeout_message(lock_path))
        time.sleep(LOCK_POLL_INTERVAL)


def _release_lock(lock_path: Path) -> None:
    # On ne libere que le verrou qu'on detient reellement. S'il ne porte plus notre pid,
    # il a deja ete vole entre-temps ; l'effacer romprait le nouveau titulaire.
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    if content == str(os.getpid()):
        with suppress(FileNotFoundError):
            lock_path.unlink()


@contextmanager
def locked() -> Iterator[dict]:
    """Verrou exclusif puis lecture, avec sauvegarde automatique a la sortie normale.

    L'etat est relu ICI, a l'interieur du verrou : une copie chargee avant l'acquisition
    peut avoir ete depassee par un autre writer pendant l'attente. Course deja trouvee en
    revue ; l'inverse rendrait le verrou decoratif.
    """
    lock_path = _acquire_lock()
    try:
        state = load()
        yield state
        save(state)
    finally:
        _release_lock(lock_path)


_ID_PREFIXES = {"chantier": "c", "tache": "t", "question": "q", "proposition": "p"}


def next_id(state: dict, kind: str) -> str:
    """Identifiant sequentiel prefixe, ex. t-01. Consomme et incremente state['seq']."""
    if kind not in _ID_PREFIXES:
        raise ValueError(f"unknown identifier type: {kind}")
    state["seq"][kind] += 1
    return f"{_ID_PREFIXES[kind]}-{state['seq'][kind]:02d}"
