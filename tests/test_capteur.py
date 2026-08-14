"""Tests unitaires de ordo/capteur.py.

Isoles par ORDO_HOME, comme test_store.py et test_chantier.py. Les capteurs de test
sont de vrais petits scripts ecrits sur disque dans le repertoire temporaire, avec un
shebang reel : c'est la seule facon de prouver que le lanceur respecte le shebang
plutot que de supposer sh (defaut trouve en revue dans le projet d'origine).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import capteur, chantier, store


def _write_script(path: Path, shebang: str, body: str) -> Path:
    path.write_text(f"{shebang}\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class CapteurTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-capteur-test-")
        self._prev_home = os.environ.get("ORDO_HOME")
        os.environ["ORDO_HOME"] = self._tmp
        self._scripts = Path(tempfile.mkdtemp(prefix="ordo-capteur-scripts-"))
        # Le repertoire doit exister : start() refuse un projet fantome depuis le
        # 9 aout 2026, et cette fixture s'appuyait justement sur un dossier jamais cree.
        projet = Path(self._tmp) / "projet"
        projet.mkdir(exist_ok=True)
        self.chantier_id = chantier.start(str(projet), "obj")["id"]

    def tearDown(self) -> None:
        if self._prev_home is None:
            os.environ.pop("ORDO_HOME", None)
        else:
            os.environ["ORDO_HOME"] = self._prev_home
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._scripts, ignore_errors=True)

    # -- fabriques de capteurs de test --------------------------------------

    def _python_ok(self, name: str = "sensor.py", extra: str = "") -> Path:
        # N'utilise QUE de la syntaxe Python (f-string, import) : si un lanceur
        # l'executait via sh malgre le shebang, sh echouerait a l'analyser et ne
        # produirait aucun JSON valide sur stdout.
        body = (
            "import json\n"
            f"{extra}\n"
            'payload = {"at": "2026-08-09T10:00:00Z", "ok": True, '
            '"measured": [{"name": "n", "value": 1}], "declared": [], '
            '"drift": [], "unknown": []}\n'
            "print(json.dumps(payload))\n"
        )
        return _write_script(self._scripts / name, "#!/usr/bin/env python3", body)

    def _shell_ok(self, name: str = "sensor.sh") -> Path:
        body = (
            "echo '{\"at\": \"2026-08-09T10:00:00Z\", \"ok\": true, "
            '"measured": [], "declared": [], "drift": [], "unknown": []}\''
        )
        return _write_script(self._scripts / name, "#!/bin/sh", body)

    def _python_sleeper(self, seconds: float, name: str = "sleeper.py") -> Path:
        body = f"import time\ntime.sleep({seconds})\nprint('{{}}')\n"
        return _write_script(self._scripts / name, "#!/usr/bin/env python3", body)

    def _python_flood(self, name: str = "flood.py") -> Path:
        body = "import sys\nsys.stdout.write('x' * (400 * 1024))\n"
        return _write_script(self._scripts / name, "#!/usr/bin/env python3", body)

    def _python_missing_key(self, name: str = "missing.py") -> Path:
        body = (
            "import json\n"
            'payload = {"at": "2026-08-09T10:00:00Z", "ok": True, '
            '"measured": [], "declared": [], "drift": []}\n'
            "print(json.dumps(payload))\n"
        )
        return _write_script(self._scripts / name, "#!/usr/bin/env python3", body)

    def _python_counter(self, name: str = "counter.py") -> Path:
        # Mesure une valeur qui change a chaque appel (via un fichier compteur a
        # cote du script), pour distinguer un capteur SAIN (forme stable, valeurs
        # qui bougent) d'un capteur FIGE (valeurs identiques a chaque cycle).
        counter_file = self._scripts / f"{name}.count"
        counter_file.write_text("0", encoding="utf-8")
        body = (
            "import json\n"
            f"counter_path = {str(counter_file)!r}\n"
            "n = int(open(counter_path).read().strip()) + 1\n"
            "open(counter_path, 'w').write(str(n))\n"
            'payload = {"at": "2026-08-09T10:00:00Z", "ok": True, '
            '"measured": [{"name": "n", "value": n}], "declared": [], '
            '"drift": [], "unknown": []}\n'
            "print(json.dumps(payload))\n"
        )
        return _write_script(self._scripts / name, "#!/usr/bin/env python3", body)

    def _python_bad_exit(self, name: str = "bad.py") -> Path:
        body = "import sys\nsys.exit(1)\n"
        return _write_script(self._scripts / name, "#!/usr/bin/env python3", body)

    def _python_measured_declared(
        self, measured_value, declared_value, name: str = "md.py"
    ) -> Path:
        body = (
            "import json\n"
            "payload = {\n"
            '  "at": "2026-08-09T10:00:00Z", "ok": True,\n'
            f'  "measured": [{{"name": "m", "value": {measured_value!r}}}],\n'
            f'  "declared": [{{"name": "d", "value": {declared_value!r}, "source": "x"}}],\n'
            '  "drift": [], "unknown": [],\n'
            "}\n"
            "print(json.dumps(payload))\n"
        )
        return _write_script(self._scripts / name, "#!/usr/bin/env python3", body)


class TestInstall(CapteurTestCase):
    def test_install_copies_script_and_makes_it_executable(self):
        src = self._python_ok()
        c = capteur.install(self.chantier_id, src)
        dest = Path(c["capteur"]["path"])
        self.assertTrue(dest.exists())
        self.assertTrue(dest.is_relative_to(store.home() / "sensors"))
        mode = dest.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_install_resets_previous_capteur_state(self):
        src = self._python_ok()
        capteur.install(self.chantier_id, src)
        capteur.run(self.chantier_id)
        capteur.run(self.chantier_id)
        capteur.run(self.chantier_id)
        capteur.adopt(self.chantier_id)
        # un nouveau script efface la confiance accordee au precedent
        c = capteur.install(self.chantier_id, self._python_ok(name="sensor2.py"))
        self.assertFalse(c["capteur"]["adopted"])
        self.assertEqual(c["capteur"]["runs"], [])

    def test_install_unknown_chantier_raises(self):
        with self.assertRaises(capteur.CapteurError):
            capteur.install("c-99", self._python_ok())

    def test_install_missing_script_raises(self):
        with self.assertRaises(capteur.CapteurError):
            capteur.install(self.chantier_id, self._scripts / "absent.py")

    def test_install_hors_du_repo_audite(self):
        # emplacement impose : ORDO_HOME/sensors/, jamais dans le repo du projet
        src = self._python_ok()
        c = capteur.install(self.chantier_id, src)
        self.assertNotIn(str(self._scripts), c["capteur"]["path"])


class TestRunRespectsShebang(CapteurTestCase):
    def test_run_executes_python_script_as_python(self):
        capteur.install(self.chantier_id, self._python_ok())
        record = capteur.run(self.chantier_id)
        self.assertTrue(record["ok"], record.get("error"))
        self.assertEqual(record["output"]["measured"][0]["name"], "n")

    def test_run_executes_shell_script_as_shell(self):
        capteur.install(self.chantier_id, self._shell_ok())
        record = capteur.run(self.chantier_id)
        self.assertTrue(record["ok"], record.get("error"))

    def test_run_without_installed_capteur_raises(self):
        with self.assertRaises(capteur.CapteurError):
            capteur.run(self.chantier_id)


class TestRunTimeoutAndSize(CapteurTestCase):
    def test_run_hard_timeout_never_blocks_the_cycle(self):
        capteur.install(self.chantier_id, self._python_sleeper(5))
        started = time.monotonic()
        record = capteur.run(self.chantier_id, timeout=0.4)
        elapsed = time.monotonic() - started
        self.assertFalse(record["ok"])
        self.assertTrue(record["timedOut"])
        self.assertLess(elapsed, 3.0, "le timeout doit couper bien avant la fin du sleep")

    def test_run_output_bounded_to_256_kio(self):
        capteur.install(self.chantier_id, self._python_flood())
        started = time.monotonic()
        record = capteur.run(self.chantier_id, timeout=10)
        elapsed = time.monotonic() - started
        self.assertFalse(record["ok"])
        self.assertTrue(record["truncated"])
        self.assertLess(elapsed, 5.0, "un capteur qui inonde stdout doit etre coupe vite")

    def test_run_bad_exit_code_is_a_failure(self):
        capteur.install(self.chantier_id, self._python_bad_exit())
        record = capteur.run(self.chantier_id)
        self.assertFalse(record["ok"])
        self.assertIsNotNone(record["error"])


class TestI10AucuneValeurParDefaut(CapteurTestCase):
    def test_i10_cle_manquante_est_signalee_pas_comblee(self):
        # I10 : ce qui n'a pas pu etre mesure va dans unknown avec sa raison. Si le
        # script omet une cle entiere (ici "unknown"), Ordo ne la comble jamais
        # silencieusement par une liste vide : il signale l'echec.
        capteur.install(self.chantier_id, self._python_missing_key())
        record = capteur.run(self.chantier_id)
        self.assertFalse(record["ok"])
        self.assertIn("unknown", record["error"])
        self.assertIsNone(record["output"])


class TestI9MeasuredDeclaredJamaisMelanges(CapteurTestCase):
    def test_i9_measured_et_declared_restent_separes_apres_run(self):
        capteur.install(
            self.chantier_id, self._python_measured_declared("12", "42/379")
        )
        record = capteur.run(self.chantier_id)
        self.assertTrue(record["ok"], record.get("error"))
        measured_names = {m["name"] for m in record["output"]["measured"]}
        declared_names = {d["name"] for d in record["output"]["declared"]}
        self.assertEqual(measured_names, {"m"})
        self.assertEqual(declared_names, {"d"})
        # aucune contamination croisee : le nom declare n'apparait jamais cote mesure
        self.assertNotIn("d", measured_names)
        self.assertNotIn("m", declared_names)

    def test_i9_status_garde_measured_et_declared_separes(self):
        script = self._python_measured_declared("12", "42/379")
        capteur.install(self.chantier_id, script)
        for _ in range(3):
            capteur.run(self.chantier_id)
        capteur.adopt(self.chantier_id)
        s = capteur.status(self.chantier_id)
        self.assertEqual({m["name"] for m in s["measured"]}, {"m"})
        self.assertEqual({d["name"] for d in s["declared"]}, {"d"})


class TestAdoption(CapteurTestCase):
    def test_trois_executions_concordantes_permettent_adopt(self):
        capteur.install(self.chantier_id, self._python_ok())
        for _ in range(3):
            capteur.run(self.chantier_id)
        c = capteur.adopt(self.chantier_id)
        self.assertTrue(c["capteur"]["adopted"])
        self.assertIsNotNone(c["capteur"]["adoptedAt"])

    def test_adopt_refuse_avant_trois_executions(self):
        capteur.install(self.chantier_id, self._python_ok())
        capteur.run(self.chantier_id)
        capteur.run(self.chantier_id)
        with self.assertRaises(capteur.CapteurError):
            capteur.adopt(self.chantier_id)

    def test_adopt_refuse_si_une_execution_a_echoue(self):
        # trois executions dans l'historique mais l'une d'elles a echoue : pas
        # concordant, meme si les deux autres sont identiques et reussies.
        capteur.install(self.chantier_id, self._python_ok())
        capteur.run(self.chantier_id)
        with store.locked() as state:
            state["chantiers"][self.chantier_id]["capteur"]["runs"].append(
                {"at": store.now(), "ok": False, "error": "echec simule",
                 "timedOut": False, "truncated": False, "output": None}
            )
        capteur.run(self.chantier_id)
        with self.assertRaises(capteur.CapteurError):
            capteur.adopt(self.chantier_id)

    def test_adopt_refuse_si_forme_de_sortie_change_entre_executions(self):
        # deux runs mesurent "n", le troisieme mesure un nom different : pas concordant
        capteur.install(self.chantier_id, self._python_ok())
        capteur.run(self.chantier_id)
        capteur.run(self.chantier_id)
        capteur.install(
            self.chantier_id,
            self._python_measured_declared("1", "x"),
        )
        capteur.run(self.chantier_id)
        with self.assertRaises(capteur.CapteurError):
            capteur.adopt(self.chantier_id)


class TestI12SignalFiltre(CapteurTestCase):
    def test_i12_status_avant_adoption_ne_sert_aucune_mesure(self):
        # I12 : tant que le capteur n'est pas adopte, status() rend un signal
        # "unknown" et ne sert AUCUNE mesure, meme si des runs ont deja reussi.
        capteur.install(self.chantier_id, self._python_ok())
        capteur.run(self.chantier_id)
        capteur.run(self.chantier_id)
        s = capteur.status(self.chantier_id)
        self.assertEqual(s, {"adopted": False, "signal": "unknown"})

    def test_i12_status_apres_adoption_sert_le_dernier_succes(self):
        # valeurs qui changent a chaque cycle (compteur) : ne doit pas se lire
        # comme fige, seulement comme "ok".
        capteur.install(self.chantier_id, self._python_counter())
        for _ in range(3):
            capteur.run(self.chantier_id)
        capteur.adopt(self.chantier_id)
        s = capteur.status(self.chantier_id)
        self.assertTrue(s["adopted"])
        self.assertEqual(s["signal"], "ok")
        self.assertEqual(s["measured"][0]["name"], "n")


class TestEchecsEtReveil(CapteurTestCase):
    def test_deux_echecs_consecutifs_declenchent_reveil(self):
        capteur.install(self.chantier_id, self._python_ok())
        for _ in range(3):
            capteur.run(self.chantier_id)
        capteur.adopt(self.chantier_id)
        capteur.install(self.chantier_id, self._python_bad_exit())
        # reinstaller reinitialise l'adoption ; on la force a la main pour isoler
        # le comportement de status() sur les echecs sans redemander 3 succes
        with store.locked() as state:
            state["chantiers"][self.chantier_id]["capteur"]["adopted"] = True
        capteur.run(self.chantier_id)
        capteur.run(self.chantier_id)
        s = capteur.status(self.chantier_id)
        self.assertEqual(s["signal"], "waking")

    def test_un_seul_echec_ne_declenche_pas_encore_reveil(self):
        capteur.install(self.chantier_id, self._python_ok())
        for _ in range(3):
            capteur.run(self.chantier_id)
        capteur.adopt(self.chantier_id)
        capteur.install(self.chantier_id, self._python_bad_exit())
        with store.locked() as state:
            state["chantiers"][self.chantier_id]["capteur"]["adopted"] = True
        capteur.run(self.chantier_id)
        s = capteur.status(self.chantier_id)
        self.assertEqual(s["signal"], "failed")
        self.assertNotEqual(s["signal"], "waking")


class TestCapteurFige(CapteurTestCase):
    def test_sortie_identique_sur_plusieurs_cycles_est_figee_pas_bloquee(self):
        # une sortie identique vaut CAPTEUR FIGE, jamais tache bloquee : les deux se
        # distinguent dans ce que rend status().
        capteur.install(self.chantier_id, self._python_ok())
        for _ in range(4):
            capteur.run(self.chantier_id)
        capteur.adopt(self.chantier_id)
        s = capteur.status(self.chantier_id)
        self.assertEqual(s["signal"], "frozen")
        self.assertNotEqual(s["signal"], "failed")
        self.assertNotEqual(s["signal"], "waking")

    def test_fige_et_reveil_sont_des_signaux_distincts(self):
        # ne jamais confondre : capteur fige = corriger le script, ce n'est pas une
        # tache bloquee, et ce n'est pas non plus un echec d'execution.
        capteur.install(self.chantier_id, self._python_ok())
        for _ in range(4):
            capteur.run(self.chantier_id)
        capteur.adopt(self.chantier_id)
        s_fige = capteur.status(self.chantier_id)
        capteur.install(self.chantier_id, self._python_bad_exit())
        with store.locked() as state:
            state["chantiers"][self.chantier_id]["capteur"]["adopted"] = True
        capteur.run(self.chantier_id)
        capteur.run(self.chantier_id)
        s_reveil = capteur.status(self.chantier_id)
        self.assertNotEqual(s_fige["signal"], s_reveil["signal"])


class TestDue(CapteurTestCase):
    def test_due_true_when_never_run(self):
        capteur.install(self.chantier_id, self._python_ok())
        self.assertTrue(capteur.due(self.chantier_id, every=120))

    def test_due_false_without_installed_capteur(self):
        self.assertFalse(capteur.due(self.chantier_id, every=120))

    def test_due_false_right_after_a_run(self):
        capteur.install(self.chantier_id, self._python_ok())
        capteur.run(self.chantier_id)
        self.assertFalse(capteur.due(self.chantier_id, every=120))

    def test_due_true_after_interval_elapsed(self):
        capteur.install(self.chantier_id, self._python_ok())
        capteur.run(self.chantier_id)
        with store.locked() as state:
            runs = state["chantiers"][self.chantier_id]["capteur"]["runs"]
            runs[-1]["at"] = "2000-01-01T00:00:00Z"
        self.assertTrue(capteur.due(self.chantier_id, every=120))


class TestCleDeStockageParId(CapteurTestCase):
    def test_deux_chantiers_meme_projet_ont_des_capteurs_independants(self):
        # cle de stockage : chantier["id"], jamais un chemin de projet. Deux
        # chantiers sur le MEME projet ne doivent jamais partager leur capteur.
        projet = Path(self._tmp) / "meme-projet"
        projet.mkdir()
        # home_partage : la fixture a deja ouvert un chantier sur un AUTRE projet dans ce
        # meme ORDO_HOME. Le point D refuse ce melange par defaut ; ici il est voulu, le
        # test porte sur la cle de stockage du capteur, pas sur l'isolation par home.
        c1 = chantier.start(str(projet), "premier", home_partage=True)["id"]
        c2 = chantier.start(str(projet), "second", home_partage=True)["id"]
        capteur.install(c1, self._python_ok(name="a.py"))
        capteur.run(c1)
        capteur.run(c1)
        capteur.run(c1)
        capteur.adopt(c1)
        self.assertFalse(capteur.status(c2)["adopted"])
        with self.assertRaises(capteur.CapteurError):
            capteur.run(c2)


if __name__ == "__main__":
    unittest.main()
