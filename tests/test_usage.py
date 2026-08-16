"""Tests unitaires de ordo/usage.py, la lecture des jetons consommes par une executante.

Aucun test ne touche les vrais transcripts de la machine : ORDO_TRANSCRIPTS pointe sur un
repertoire temporaire. Lire les vrais rendrait ces tests dependants de ce que l'utilisateur
a fait ce jour-la.
"""

from __future__ import annotations

import importlib
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

    def _tour(self, out: int, cache_read: int = 0, cache_creation: int = 0, entree: int = 0,
              mid: str | None = None):
        return {
            "type": "assistant",
            "message": {
                "id": mid,
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


class TestTourEclate(UsageTestCase):
    """Un tour d'assistant est ecrit sur PLUSIEURS lignes, une par bloc de contenu, et
    chacune repete le meme `usage`. Additionner les lignes compte donc le meme tour
    plusieurs fois.

    Mesure sur soixante transcripts reels de ce depot : la sortie etait comptee DEUX fois
    (+102%), le cache relu +59%, et le nombre de tours +68%. Un compteur de jetons qui se
    trompe d'un facteur deux ne sert a rien -- il sert meme contre, puisqu'il donne
    confiance en un chiffre faux.
    """

    def test_les_lignes_d_un_meme_tour_ne_comptent_qu_une_fois(self):
        self._transcript("s1", [
            self._tour(100, cache_read=5000, mid="msg_a"),
            self._tour(100, cache_read=5000, mid="msg_a"),
            self._tour(100, cache_read=5000, mid="msg_a"),
        ])
        u = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(u["output"], 100)
        self.assertEqual(u["cacheRead"], 5000)
        self.assertEqual(u["turns"], 1)

    def test_deux_tours_distincts_comptent_deux_fois(self):
        # Le garde-fou du garde-fou : dedoublonner ne doit pas avaler un vrai tour.
        self._transcript("s1", [
            self._tour(100, mid="msg_a"),
            self._tour(70, mid="msg_b"),
        ])
        u = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(u["output"], 170)
        self.assertEqual(u["turns"], 2)

    def test_le_dedoublonnage_survit_a_la_lecture_incrementale(self):
        # Le cas qui casse une solution naive : les lignes d'un meme tour tombent de part
        # et d'autre de la frontiere de lecture. Le serveur relit toutes les trois
        # secondes, donc cette frontiere tombe n'importe ou.
        self._transcript("s1", [self._tour(100, cache_read=5000, mid="msg_a")])
        premier = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(premier["output"], 100)
        self._transcript("s1", [self._tour(100, cache_read=5000, mid="msg_a")])
        second = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(second["output"], 100)
        self.assertEqual(second["turns"], 1)

    def test_un_tour_sans_identifiant_compte_quand_meme(self):
        # Un transcript ecrit par une version qui ne pose pas d'id ne doit pas disparaitre
        # des compteurs : absent d'identifiant ne veut pas dire doublon.
        self._transcript("s1", [self._tour(10), self._tour(20)])
        u = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(u["output"], 30)
        self.assertEqual(u["turns"], 2)


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


class TestContextePorte(UsageTestCase):
    """Le contexte du DERNIER tour, ce que la compaction lit désormais pour décider s'il
    faut remettre une session à plat -- voir controle._compacter() et usage.SEUIL_CONTEXTE.
    À la différence des autres champs de pour(), qui cumulent toute la session, celui-ci
    écrase : une session peut porter un gros contexte en peu de tours, et une somme sur
    toute la session le cacherait derrière des tours anciens déjà compactés."""

    def test_le_contexte_du_dernier_tour_est_expose(self):
        self._transcript("s1", [self._tour(50, entree=100, cache_read=2000, cache_creation=300)])
        u = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(u["dernierContexte"], 100 + 2000 + 300)

    def test_la_sortie_ne_compte_pas_dans_le_contexte(self):
        # La sortie d'un tour n'est jamais relue par ce même tour ; elle n'entre dans un
        # contexte que via le cache_read du tour suivant, déjà compté séparément.
        self._transcript("s1", [self._tour(999999, entree=10, cache_read=20)])
        u = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(u["dernierContexte"], 30)

    def test_le_contexte_porte_n_est_pas_cumule_comme_les_totaux(self):
        # À la différence de input/cacheRead, qui s'additionnent sur toute la session,
        # dernierContexte ne vaut que pour le dernier tour lu.
        self._transcript("s1", [
            self._tour(10, entree=100, cache_read=1000),
            self._tour(10, entree=50, cache_read=9000),
        ])
        u = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(u["dernierContexte"], 50 + 9000)
        self.assertEqual(u["cacheRead"], 10000)  # le total cumulé, lui, reste une somme

    def test_les_lignes_d_un_meme_tour_ne_faussent_pas_le_contexte(self):
        self._transcript("s1", [
            self._tour(10, entree=100, cache_read=1000, mid="msg_a"),
            self._tour(10, entree=100, cache_read=1000, mid="msg_a"),
        ])
        u = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(u["dernierContexte"], 1100)

    def test_le_contexte_porte_survit_a_une_lecture_sans_nouveau_tour(self):
        self._transcript("s1", [self._tour(10, entree=100, cache_read=1000)])
        premier = usage.pour({"id": "t-01", "claudeSessionId": "s1"})
        self.assertEqual(premier["dernierContexte"], 1100)
        second = usage.pour({"id": "t-01", "claudeSessionId": "s1"})  # rien de nouveau à lire
        self.assertEqual(second["dernierContexte"], 1100)


class TestSeuils(unittest.TestCase):
    """SEUIL_TOURS et SEUIL_CONTEXTE sont lues à l'import du module : chaque test change
    l'environnement puis recharge le module, et remet les deux dans leur état par défaut en
    sortant pour ne pas polluer les tests suivants (test_controle.py notamment fige son
    propre alias sur usage.SEUIL_CONTEXTE à SON import, mais tout code qui lit
    usage.SEUIL_CONTEXTE après un reload verrait la valeur laissée en place)."""

    VARS = ("ORDO_COMPACT_EVERY", "ORDO_COMPACT_EVERY_CONTEXTE")

    def setUp(self) -> None:
        self._avant = {v: os.environ.get(v) for v in self.VARS}

    def tearDown(self) -> None:
        for v, valeur in self._avant.items():
            if valeur is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = valeur
        importlib.reload(usage)

    def _recharger(self, **env: str) -> None:
        for v in self.VARS:
            os.environ.pop(v, None)
        os.environ.update(env)
        importlib.reload(usage)

    def test_les_deux_seuils_ont_leur_valeur_par_defaut(self):
        self._recharger()
        self.assertEqual(usage.SEUIL_TOURS, 75)
        self.assertEqual(usage.SEUIL_CONTEXTE, 150000)

    def test_seuil_tours_lit_sa_propre_variable(self):
        self._recharger(ORDO_COMPACT_EVERY="50")
        self.assertEqual(usage.SEUIL_TOURS, 50)
        # Le bug corrigé : poser la variable de cadence en tours ne doit plus toucher au
        # seuil de contexte, qui reste sur sa valeur par défaut.
        self.assertEqual(usage.SEUIL_CONTEXTE, 150000)

    def test_seuil_contexte_lit_sa_propre_variable(self):
        self._recharger(ORDO_COMPACT_EVERY_CONTEXTE="99000")
        self.assertEqual(usage.SEUIL_CONTEXTE, 99000)
        self.assertEqual(usage.SEUIL_TOURS, 75)

    def test_les_deux_variables_reglees_ensemble_ne_se_marchent_pas_dessus(self):
        self._recharger(ORDO_COMPACT_EVERY="50", ORDO_COMPACT_EVERY_CONTEXTE="200000")
        self.assertEqual(usage.SEUIL_TOURS, 50)
        self.assertEqual(usage.SEUIL_CONTEXTE, 200000)

    def test_zero_desactive_chaque_seuil_independamment(self):
        self._recharger(ORDO_COMPACT_EVERY="0")
        self.assertEqual(usage.SEUIL_TOURS, 0)
        self.assertEqual(usage.SEUIL_CONTEXTE, 150000)
        self._recharger(ORDO_COMPACT_EVERY_CONTEXTE="0")
        self.assertEqual(usage.SEUIL_CONTEXTE, 0)
        self.assertEqual(usage.SEUIL_TOURS, 75)


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
