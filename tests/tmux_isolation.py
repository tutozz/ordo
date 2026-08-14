"""Serveur tmux prive pour les tests qui pilotent de vrais panes.

Pourquoi. Les tests de panes, de controle et le parcours e2e creent des dizaines de
panes reels. Executes sur le serveur tmux de la machine, ils entrent en concurrence
avec les sessions de l'humain : mesure du 11 aout 2026, le test
BusyTests.test_marker_scrolled_out_of_last_20_lines_goes_idle_again passait 12 fois
sur 12 lance seul, et echouait environ une fois sur trois dans la suite complete, sur
un `send-keys` refuse avec "no current client", alors que 16 panes etrangers vivaient
sur le meme serveur. Un test qui echoue au hasard ne prouve rien et detruit la
confiance dans toute la suite.

Comment. tmux derive le chemin de sa socket de TMUX_TMPDIR. Pointer cette variable
vers un repertoire temporaire fait demarrer un serveur distinct, vide, qui ne voit
aucune session de l'humain et qu'aucune session de l'humain ne ralentit. Aucune ligne
de ordo/ ne change : les tests invoquent le meme binaire, avec un environnement
different.

Ce module ne tue jamais de serveur tmux. Le serveur prive s'arrete de lui-meme quand
sa derniere session meurt, et le balayage par prefixe deja present dans chaque module
de test suffit a l'y amener. `tmux kill-server` reste absent de tout ce depot, tests
compris : une commande qu'on ecrit dans un test finit toujours par etre recopiee
ailleurs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid

_ENV = "TMUX_TMPDIR"

# Session temoin, sans autre role que d'empecher le serveur prive de s'arreter entre
# deux tests. Mesure du 11 aout 2026 : sans elle, le serveur meurt avec la derniere
# session, chaque commande suivante le relance, et la suite de panes passe de 18 s a
# 122 s. Elle ne contient qu'un shell endormi et ne sert jamais de cible a un test.
_HOLDER_PREFIX = "ordo-test-holder-"


def start_private_server() -> tuple[str, str | None, str]:
    """Bascule les appels tmux du process vers un serveur prive, et le maintient en vie.

    Rend (repertoire cree, valeur precedente de TMUX_TMPDIR, nom de la session temoin),
    a repasser tel quel a stop_private_server(). Sans effet si la variable est deja
    posee : un appelant exterieur, une CI par exemple, garde la main sur son isolement.
    """
    previous = os.environ.get(_ENV)
    if previous:
        return "", previous, ""
    tmpdir = tempfile.mkdtemp(prefix="ordo-test-tmux-")
    os.environ[_ENV] = tmpdir
    holder = f"{_HOLDER_PREFIX}{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", holder, "sleep", "100000"],
        capture_output=True,
    )
    return tmpdir, None, holder


def stop_private_server(tmpdir: str, previous: str | None, holder: str) -> None:
    """Tue la session temoin, restaure l'environnement, efface le repertoire de socket.

    Le serveur prive s'arrete de lui-meme une fois sa derniere session partie ; aucun
    kill-server n'est necessaire, et il n'y en a nulle part dans ce depot.
    """
    if holder:
        subprocess.run(["tmux", "kill-session", "-t", f"={holder}"], capture_output=True)
    if previous is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = previous
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
