"""Tests unitaires de ordo/quota.py.

Chaque test isole ORDO_QUOTA_FILE dans un repertoire temporaire : jamais le vrai
fichier de l'utilisateur, sans quoi ces tests dependraient de l'etat d'une session
Claude Code reelle sur la machine qui les fait tourner.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import quota


class QuotaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-quota-test-")
        self._prev = os.environ.get("ORDO_QUOTA_FILE")
        self._fichier = Path(self._tmp) / "quota"
        os.environ["ORDO_QUOTA_FILE"] = str(self._fichier)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("ORDO_QUOTA_FILE", None)
        else:
            os.environ["ORDO_QUOTA_FILE"] = self._prev
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _ecrire(self, ligne: str) -> None:
        self._fichier.write_text(ligne, encoding="utf-8")


class TestCheminNominal(QuotaTestCase):
    def test_une_ligne_complete_rend_les_deux_fenetres(self) -> None:
        maintenant = int(time.time())
        self._ecrire(f"34 50 {maintenant} {maintenant + 3600} {maintenant + 3 * 86400}\n")
        resultat = quota.lire()
        self.assertIsNotNone(resultat)
        cles = {f["cle"] for f in resultat["fenetres"]}
        self.assertEqual(cles, {"5h", "7j"})
        cinq = next(f for f in resultat["fenetres"] if f["cle"] == "5h")
        sept = next(f for f in resultat["fenetres"] if f["cle"] == "7j")
        self.assertEqual(cinq["pourcent"], 34)
        self.assertEqual(sept["pourcent"], 50)
        self.assertFalse(cinq["perime"])
        self.assertFalse(sept["perime"])
        self.assertGreaterEqual(resultat["ageSecondes"], 0)
        self.assertLess(resultat["ageSecondes"], 5)


class TestFichierAbsent(QuotaTestCase):
    def test_fichier_absent_rend_none(self) -> None:
        self.assertIsNone(quota.lire())


class TestLigneTronquee(QuotaTestCase):
    def test_moins_de_cinq_champs_rend_none(self) -> None:
        # Une ecriture interrompue au milieu, ou un format qui a change : dans les
        # deux cas la ligne ne se laisse pas interpreter a moitie.
        self._ecrire("34 50 1786848279\n")
        self.assertIsNone(quota.lire())


class TestValeurNonNumerique(QuotaTestCase):
    def test_un_champ_ni_tiret_ni_entier_rend_none(self) -> None:
        self._ecrire("abc 50 1786848279 1786857000 1787299200\n")
        self.assertIsNone(quota.lire())


class TestFenetreATiret(QuotaTestCase):
    def test_une_fenetre_a_tiret_est_absente_pas_a_zero(self) -> None:
        maintenant = int(time.time())
        self._ecrire(f"- 50 {maintenant} - {maintenant + 3600}\n")
        resultat = quota.lire()
        self.assertIsNotNone(resultat)
        cles = {f["cle"] for f in resultat["fenetres"]}
        self.assertEqual(cles, {"7j"})


class TestResetDejaPasse(QuotaTestCase):
    def test_un_reset_dans_le_passe_est_marque_perime(self) -> None:
        maintenant = int(time.time())
        self._ecrire(
            f"90 90 {maintenant} {maintenant - 60} {maintenant + 3 * 86400}\n"
        )
        resultat = quota.lire()
        cinq = next(f for f in resultat["fenetres"] if f["cle"] == "5h")
        sept = next(f for f in resultat["fenetres"] if f["cle"] == "7j")
        self.assertTrue(cinq["perime"])
        self.assertFalse(sept["perime"])


class TestFormatageCourt(QuotaTestCase):
    def test_un_reset_dans_les_24h_est_forme_heure_minute(self) -> None:
        maintenant = int(time.time())
        reset = maintenant + 3600
        self._ecrire(f"34 50 {maintenant} {reset} {maintenant + 3 * 86400}\n")
        resultat = quota.lire()
        cinq = next(f for f in resultat["fenetres"] if f["cle"] == "5h")
        attendu = time.strftime("%H:%M", time.localtime(reset))
        self.assertEqual(cinq["resetTexte"], attendu)


class TestFormatageLong(QuotaTestCase):
    def test_un_reset_au_dela_de_24h_porte_le_jour_abrege(self) -> None:
        maintenant = int(time.time())
        reset = maintenant + 3 * 86400
        self._ecrire(f"34 50 {maintenant} {maintenant + 3600} {reset}\n")
        resultat = quota.lire()
        sept = next(f for f in resultat["fenetres"] if f["cle"] == "7j")
        jours = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
        attendu_jour = jours[time.localtime(reset).tm_wday]
        attendu_heure = time.strftime("%H:%M", time.localtime(reset))
        self.assertEqual(sept["resetTexte"], f"{attendu_jour} {attendu_heure}")


class TestFichierIllisible(QuotaTestCase):
    def test_des_octets_non_utf8_rendent_none_au_lieu_de_lever(self) -> None:
        # Le module promet de ne JAMAIS lever, parce qu'un serveur HTTP l'appelle a
        # chaque battement : une exception ici fait tomber la page entiere pour un
        # fichier de quarante octets. Une écriture interrompue par un disque plein
        # laisse exactement ce genre de fichier, et read_text leve alors une
        # UnicodeDecodeError que le premier filet, pose sur OSError, ne rattrape pas.
        self._fichier.write_bytes(b"\xff\xfe 50 1786848279 1786857000 1787299200")
        self.assertIsNone(quota.lire())


class TestSeuilsDeCouleur(QuotaTestCase):
    def _couleur_a(self, pourcent: int) -> str:
        maintenant = int(time.time())
        self._ecrire(
            f"{pourcent} {pourcent} {maintenant} {maintenant + 3600} {maintenant + 3600}\n"
        )
        resultat = quota.lire()
        return next(f for f in resultat["fenetres"] if f["cle"] == "5h")["couleur"]

    def test_les_quatre_seuils_exactement_a_leurs_bornes(self) -> None:
        self.assertEqual(self._couleur_a(49), "#46a35a")
        self.assertEqual(self._couleur_a(50), "#e3b341")
        self.assertEqual(self._couleur_a(74), "#e3b341")
        self.assertEqual(self._couleur_a(75), "#e0803c")
        self.assertEqual(self._couleur_a(89), "#e0803c")
        self.assertEqual(self._couleur_a(90), "#e05252")


if __name__ == "__main__":
    unittest.main()
