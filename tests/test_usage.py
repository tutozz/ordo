"""Tests unitaires de ordo/usage.py, la lecture des jetons consommes par une executante.

Aucun test ne touche les vrais transcripts de la machine : ORDO_TRANSCRIPTS pointe sur un
repertoire temporaire. Lire les vrais rendrait ces tests dependants de ce que l'utilisateur
a fait ce jour-la.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import usage


class UsageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="ordo-usage-test-")
        self._prev = os.environ.get("ORDO_TRANSCRIPTS")
        os.environ["ORDO_TRANSCRIPTS"] = self._tmp
        usage.forget()

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("ORDO_TRANSCRIPTS", None)
        else:
            os.environ["ORDO_TRANSCRIPTS"] = self._prev
        usage.forget()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _transcript(self, sid: str, tours: list[dict], projet: str = "-Users-moi-projet") -> Path:
        dossier = Path(self._tmp) / projet
        dossier.mkdir(parents=True, exist_ok=True)
        chemin = dossier / f"{sid}.jsonl"
        with chemin.open("a", encoding="utf-8") as f:
            for tour in tours:
                f.write(json.dumps(tour) + "\n")
        return chemin

    def _tour(self, out: int, cache_read: int = 0, cache_creation: int = 0, entree: int = 0):
        return {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": entree,
                    "output_tokens": out,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                }
            },
        }


class TestLecture(UsageTestCase):
    def test_sans_identifiant_de_session_il_n_y_a_rien_a_lire(self):
        self.assertIsNone(usage.pour({"id": "t-01"}))

    def test_un_transcript_absent_rend_rien_plutot_qu_un_zero(self):
        # Zero se lirait comme "cette tache n'a rien consomme", ce qui est faux : la verite
        # est qu'on ne sait pas, et une carte qui affiche 0 jeton sur une executante qui
        # tourne depuis vingt minutes ment.
        self.assertIsNone(usage.pour({"id": "t-01", "claudeSessionId": "absent"}))

    def test_les_usages_de_tous_les_tours_s_additionnent(self):
        self._transcript("s1", [self._tour(100, cache_read=5000), self._tour(50, entree=7)])
        u = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(u["output"], 150)
        self.assertEqual(u["cacheRead"], 5000)
        self.assertEqual(u["input"], 7)
        self.assertEqual(u["turns"], 2)

    def test_une_ligne_illisible_est_sautee_sans_tout_perdre(self):
        chemin = self._transcript("s1", [self._tour(100)])
        with chemin.open("a", encoding="utf-8") as f:
            f.write("{ ceci n est pas du json\n")
        self._transcript("s1", [self._tour(20)])
        u = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(u["output"], 120)

    def test_une_ligne_sans_usage_ne_compte_pas_pour_un_tour(self):
        self._transcript("s1", [{"type": "user", "message": {"content": "salut"}},
                                self._tour(10)])
        self.assertEqual(usage.pour({"id": "t-01", "claudeSessionId": "s1"})["turns"], 1)

    def test_le_transcript_se_trouve_quel_que_soit_le_dossier_de_projet(self):
        # Le nom du dossier est un encodage du chemin du projet que ce module n'a aucune
        # raison de savoir reproduire : un identifiant de session est unique, le chercher
        # est plus sur que le deduire.
        self._transcript("s1", [self._tour(42)], projet="-un-chemin-totalement-different")
        self.assertEqual(usage.pour({"id": "t-01", "claudeSessionId": "s1"})["output"], 42)


class TestLectureIncrementale(UsageTestCase):
    def test_les_lignes_ajoutees_s_ajoutent_au_total(self):
        self._transcript("s1", [self._tour(100)])
        self.assertEqual(usage.pour({"id": "t", "claudeSessionId": "s1"})["output"], 100)
        self._transcript("s1", [self._tour(30)])
        self.assertEqual(usage.pour({"id": "t", "claudeSessionId": "s1"})["output"], 130)

    def test_seules_les_lignes_nouvelles_sont_relues(self):
        # Un transcript d'executante fait plusieurs mega-octets et la page interroge toutes
        # les trois secondes : tout relire a chaque fois ferait payer le fichier entier,
        # pour soixante taches, en boucle.
        self._transcript("s1", [self._tour(100)])
        usage.pour({"id": "t", "claudeSessionId": "s1"})
        avant = usage.octets_lus()
        self._transcript("s1", [self._tour(30)])
        usage.pour({"id": "t", "claudeSessionId": "s1"})
        ajout = usage.octets_lus() - avant
        self.assertGreater(ajout, 0)
        self.assertLess(ajout, avant)

    def test_un_fichier_qui_retrecit_est_relu_en_entier(self):
        # Un transcript remplace sous nos pieds laisserait un decalage de lecture pointant
        # au milieu d'une ligne, et le total serait faux pour toujours.
        chemin = self._transcript("s1", [self._tour(100), self._tour(100)])
        self.assertEqual(usage.pour({"id": "t", "claudeSessionId": "s1"})["output"], 200)
        chemin.write_text(json.dumps(self._tour(7)) + "\n", encoding="utf-8")
        self.assertEqual(usage.pour({"id": "t", "claudeSessionId": "s1"})["output"], 7)

    def test_forget_efface_le_cache(self):
        self._transcript("s1", [self._tour(100)])
        usage.pour({"id": "t", "claudeSessionId": "s1"})
        usage.forget()
        self.assertEqual(usage.octets_lus(), 0)
        self.assertEqual(usage.pour({"id": "t", "claudeSessionId": "s1"})["output"], 100)


class TestFormat(UsageTestCase):
    def test_un_total_court_se_lit_tel_quel(self):
        self.assertEqual(usage.court(0), "0")
        self.assertEqual(usage.court(999), "999")

    def test_les_milliers_et_les_millions_sont_abreges(self):
        self.assertEqual(usage.court(1500), "1.5k")
        self.assertEqual(usage.court(112221), "112k")
        self.assertEqual(usage.court(25480808), "25.5M")


if __name__ == "__main__":
    unittest.main()
