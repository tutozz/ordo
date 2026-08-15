"""Tests unitaires de ordo/routage.py.

Aucun etat, aucun tmux, aucun disque : le routage est une fonction pure d'une tache
vers un modele. C'est delibere, et c'est ce qui rend cette suite exhaustive sans etre
lente.

Ce que cette suite protege, et qui est la raison d'etre du module : un modele choisi
tout seul doit pouvoir se contester. Chaque decision porte un motif en clair, et
test_tout_choix_porte_un_motif_non_vide interdit qu'une regle soit ajoutee un jour sans
dire pourquoi elle a tranche.

Le garde-fou le plus important de ce fichier est
TestLeCorpsDuBriefNestPasLaDemande. Il encode un defaut REEL, mesure sur un chantier
de 69 taches : chercher le vocabulaire de conception dans tout le brief classait 24
taches d'execution sur 24 en "conception", parce que les mots y apparaissent dans des
INTERDICTIONS ("ne propose aucune tache de deploiement") et dans la methode ("va voir
le code"). Le critere punissait donc les briefs les plus soignes, exactement l'inverse
du but. Ces tests echouent si quelqu'un elargit a nouveau la fenetre de recherche au
corps du brief.

Le second defaut surveille est l'exces de confiance. Le defaut du routage est OPUS,
pas sonnet : une tache dont on ne sait pas dire qu'elle est definie n'est pas une
tache bon marche, c'est une tache dont on ignore le cout d'une erreur.

Run: python3 -m unittest tests.test_routage
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ordo import routage  # noqa: E402  (path setup must run first)


def tache(**kw) -> dict:
    """Tache minimale, avec les seuls champs que le routage regarde."""
    base = {
        "id": "t-01",
        "titre": "Appliquer la route GET /api/tapes",
        "prompt": "Modifier lib/tapes.js selon docs/15-API.md.",
        "touches": ["lib/tapes.js"],
        "checklist": [],
        "model": None,
    }
    base.update(kw)
    return base


def coche(n: int) -> list[dict]:
    return [{"texte": f"critere {i}", "done": False} for i in range(n)]


class TestLeCorpsDuBriefNestPasLaDemande(unittest.TestCase):
    """Le titre dit ce que la tache fait ; le corps porte du contexte partage.

    Chercher le vocabulaire dans le corps produit des faux positifs proportionnels au
    soin mis a rediger le brief. Ces cas sont repris de briefs reels.
    """

    def test_une_interdiction_dans_le_corps_ne_vaut_pas_demande_de_conception(self):
        m, motif = routage.classer(
            tache(
                titre="CTRL-DOC 03 Les decisions figees",
                prompt="TU NE CONSTRUIS RIEN. NE PROPOSE AUCUNE TACHE DE DEPLOIEMENT, "
                "de migration ni de mise en production. Etablis l ecart entre "
                "docs/03-DECISIONS.md et le code.",
                checklist=coche(4),
                touches=["docs/03-DECISIONS.md"],
            )
        )
        self.assertEqual(m, "sonnet", motif)

    def test_la_methode_dans_le_corps_ne_vaut_pas_incertitude(self):
        m, motif = routage.classer(
            tache(
                titre="CTRL-DOC 06 L architecture serveur",
                prompt="Ne conclus jamais IMPLEMENTE sur la foi d un rapport : "
                "va voir le code. Lis src/serveur.js en entier.",
                checklist=coche(4),
                touches=["src/serveur.js"],
            )
        )
        self.assertEqual(m, "sonnet", motif)

    def test_un_garde_fou_cite_dans_le_corps_ne_vaut_pas_conception(self):
        m, motif = routage.classer(
            tache(
                titre="6.1 Mise a jour de tous les composants",
                prompt="Si un arbitrage manque et qu aucune decision de "
                "docs/01-DECISIONS.md ne tranche, arrete-toi et demande.",
                checklist=coche(4),
                touches=["src/composants/"],
            )
        )
        self.assertEqual(m, "sonnet", motif)

    def test_le_vocabulaire_compte_quand_il_est_dans_le_titre(self):
        m, motif = routage.classer(
            tache(
                titre="SYNTHESE DES CONTROLES: ce qui manque",
                prompt="Relis les douze rapports de controle.",
                checklist=coche(4),
                touches=["docs/STATUS.md"],
            )
        )
        self.assertEqual(m, "opus", motif)


class TestOpusSurIndecision(unittest.TestCase):
    """Ce qui n'est pas tranche dans le brief doit l'etre par le modele le plus fort."""

    def test_marqueur_d_incertitude_dans_le_titre_force_opus(self):
        for marqueur in (
            "Format des exports, a definir",
            "Choisir la strategie de cache",
            "Nommer les surfaces du portail",
            "Proposer un decoupage de la phase 5",
        ):
            with self.subTest(marqueur=marqueur):
                m, motif = routage.classer(
                    tache(titre=marqueur, checklist=coche(4), touches=["a.py"])
                )
                self.assertEqual(m, "opus", motif)

    def test_les_accents_du_titre_sont_reconnus(self):
        """Les briefs de ce depot s'ecrivent sans accents ; les titres, pas toujours."""
        m, _ = routage.classer(
            tache(titre="Le format reste à définir", checklist=coche(4), touches=["a.py"])
        )
        self.assertEqual(m, "opus")

    def test_verbe_de_conception_dans_le_titre_force_opus(self):
        for verbe in (
            "Concevoir le schema de donnees",
            "Arbitrer entre les deux approches",
            "Repenser la navigation",
            "Design the empty state",
        ):
            with self.subTest(verbe=verbe):
                m, motif = routage.classer(
                    tache(titre=verbe, checklist=coche(4), touches=["a.py"])
                )
                self.assertEqual(m, "opus", motif)

    def test_design_system_n_est_pas_le_verbe_concevoir(self):
        """"design" est un nom au moins aussi souvent qu'un verbe : integrer un design
        system est de l'execution."""
        m, motif = routage.classer(
            tache(
                titre="DESIGN 03 Design system transverse: tokens, composants",
                checklist=coche(4),
                touches=["src/tokens.css"],
            )
        )
        self.assertEqual(m, "sonnet", motif)

    def test_un_jugement_terminal_force_opus(self):
        for titre in (
            "RECETTE FINALE contre le devis",
            "8. RE-CONTROLE FINAL: tout ce que le devis promet",
        ):
            with self.subTest(titre=titre):
                m, motif = routage.classer(
                    tache(titre=titre, checklist=coche(4), touches=["a.py"])
                )
                self.assertEqual(m, "opus", motif)

    def test_absence_de_checklist_force_opus(self):
        """Sans critere de fini, personne ne peut dire que la tache est definie."""
        m, motif = routage.classer(
            tache(titre="Corriger la marge du bouton", touches=["a.css"], checklist=[])
        )
        self.assertEqual(m, "opus", motif)

    def test_titre_vide_force_opus(self):
        m, motif = routage.classer(tache(titre="", checklist=coche(4)))
        self.assertEqual(m, "opus", motif)

    def test_defaut_est_opus_quand_rien_n_ancre_la_tache(self):
        m, motif = routage.classer(
            tache(titre="Traiter le sujet", prompt="Fais le necessaire.",
                  checklist=coche(3), touches=[])
        )
        self.assertEqual(m, "opus", motif)


class TestSonnetSurSpecificationVerifiable(unittest.TestCase):
    """Le cas nominal d'Ordo : une tache avec des criteres et un perimetre nomme."""

    def test_checklist_et_perimetre_donnent_sonnet(self):
        m, motif = routage.classer(
            tache(
                titre="CTRL-DOC 03 Les decisions figees, D1 a D97",
                prompt="Verifier que chaque decision est appliquee dans le code. " * 40,
                checklist=coche(4),
                touches=["docs/03-DECISIONS.md", "index.js"],
            )
        )
        self.assertEqual(m, "sonnet", motif)

    def test_un_prompt_long_ne_suffit_pas_a_lui_seul(self):
        """La longueur n'est pas la definition : un pave flou reste flou."""
        m, _ = routage.classer(
            tache(titre="Traiter", prompt="x" * 6000, checklist=[], touches=[])
        )
        self.assertEqual(m, "opus")

    def test_un_fichier_cite_dans_le_prompt_ancre_autant_que_touches(self):
        m, motif = routage.classer(
            tache(
                titre="Appliquer la route GET /api/tapes",
                prompt="Mettre a jour lib/tapes.js selon docs/15-API.md.",
                checklist=coche(3),
                touches=[],
            )
        )
        self.assertEqual(m, "sonnet", motif)


class TestHaikuSurGesteMecanique(unittest.TestCase):
    """Haiku ne se merite pas : il faut tous les signaux a la fois."""

    def test_geste_mecanique_court_et_borne_donne_haiku(self):
        m, motif = routage.classer(
            tache(
                titre="Renommer switch.component.tsx",
                prompt="Renommer switch.component.tsx en toggle.component.tsx "
                "et mettre a jour les deux imports.",
                checklist=coche(2),
                touches=["src/switch.component.tsx"],
            )
        )
        self.assertEqual(m, "haiku", motif)

    def test_geste_mecanique_mais_perimetre_large_repasse_en_sonnet(self):
        m, _ = routage.classer(
            tache(
                titre="Renommer la variable partout",
                prompt="Renommer la variable dans tous les modules.",
                checklist=coche(2),
                touches=[f"f{i}.py" for i in range(9)],
            )
        )
        self.assertEqual(m, "sonnet")

    def test_prompt_long_meme_mecanique_n_est_pas_haiku(self):
        m, _ = routage.classer(
            tache(
                titre="Renommer le fichier",
                prompt="Renommer le fichier a.py. " * 200,
                checklist=coche(2),
                touches=["a.py"],
            )
        )
        self.assertEqual(m, "sonnet")

    def test_checklist_dense_disqualifie_haiku(self):
        m, _ = routage.classer(
            tache(
                titre="Renommer le fichier",
                prompt="Renommer a.py en b.py.",
                checklist=coche(6),
                touches=["a.py"],
            )
        )
        self.assertEqual(m, "sonnet")


class TestContrat(unittest.TestCase):
    """Ce que le reste d'Ordo est en droit d'attendre du module."""

    def test_tout_choix_porte_un_motif_non_vide(self):
        for t in (
            tache(checklist=[]),
            tache(titre="Concevoir le module", checklist=coche(2)),
            tache(titre="Renommer a.py", prompt="Renommer a.py en b.py.",
                  checklist=coche(1), touches=["a.py"]),
            tache(titre="CTRL-DOC 01", checklist=coche(4), touches=["a.py"]),
        ):
            m, motif = routage.classer(t)
            with self.subTest(modele=m):
                self.assertIn(m, routage.MODELES)
                self.assertTrue(motif.strip(), "un choix sans motif ne se conteste pas")
                self.assertLess(len(motif), 120, "le motif tient sur une ligne")

    def test_le_champ_model_est_une_trace_pas_une_consigne(self):
        """tache["model"] n'est ecrit que par _mark_running : c'est le modele du DERNIER
        tour, pas un choix humain. Le relire comme une consigne figerait a jamais le
        premier routage et rendrait toute escalade impossible."""
        m, _ = routage.classer(
            tache(
                model="opus",
                titre="Renommer a.py",
                prompt="Renommer a.py en b.py.",
                checklist=coche(1),
                touches=["a.py"],
            )
        )
        self.assertEqual(m, "haiku")

    def test_une_tache_sans_les_champs_attendus_ne_leve_pas(self):
        """Une tache d'un etat ancien n'a pas forcement touches ni checklist."""
        m, motif = routage.classer({"id": "t-99"})
        self.assertIn(m, routage.MODELES)
        self.assertTrue(motif)

    def test_classer_ne_modifie_pas_la_tache(self):
        t = tache(checklist=coche(1))
        avant = json_copie(t)
        routage.classer(t)
        self.assertEqual(json_copie(t), avant)


class TestPourLancement(unittest.TestCase):
    """Precedence au moment du lancement. Le routage propose, l'humain dispose."""

    def test_un_modele_donne_en_ligne_de_commande_prime_sur_tout(self):
        m, motif = routage.pour_lancement(
            "haiku", tache(titre="Concevoir le module", checklist=coche(2))
        )
        self.assertEqual(m, "haiku")
        self.assertIn("--model", motif)

    def test_sans_modele_donne_le_routage_tranche(self):
        m, motif = routage.pour_lancement(
            None, tache(titre="Concevoir le module", checklist=coche(2))
        )
        self.assertEqual(m, "opus")
        self.assertIn("conception", motif)

    def test_le_mot_herite_restaure_le_comportement_d_avant_le_routage(self):
        """Porte de sortie : aucun --model n'est passe a claude, qui garde son defaut."""
        m, motif = routage.pour_lancement(routage.HERITE, tache(checklist=coche(3)))
        self.assertIsNone(m)
        self.assertTrue(motif)


class TestEscaladeSurRelance(unittest.TestCase):
    """Relancer une tache a l'identique la fait echouer a l'identique.

    Le routage peut se tromper vers le bas : c'est son risque assume. Ce qui ne doit
    jamais arriver, c'est qu'il s'y tienne. Chaque tentative deja faite monte le modele
    d'un cran, si bien qu'une tache mal classee se repare toute seule au premier
    relaunch au lieu de boucler.
    """

    def test_une_relance_monte_d_un_cran(self):
        base = tache(titre="CTRL-DOC 01", checklist=coche(4), touches=["a.py"])
        self.assertEqual(routage.pour_lancement(None, base)[0], "sonnet")
        m, motif = routage.pour_lancement(None, dict(base, attempts=1))
        self.assertEqual(m, "opus", motif)
        self.assertIn("tentative", motif.lower())

    def test_une_tache_haiku_relancee_passe_sonnet(self):
        base = tache(
            titre="Renommer a.py",
            prompt="Renommer a.py en b.py.",
            checklist=coche(1),
            touches=["a.py"],
        )
        self.assertEqual(routage.pour_lancement(None, base)[0], "haiku")
        self.assertEqual(routage.pour_lancement(None, dict(base, attempts=1))[0], "sonnet")

    def test_l_escalade_plafonne_a_opus(self):
        base = tache(titre="Renommer a.py", prompt="Renommer a.py.",
                     checklist=coche(1), touches=["a.py"])
        for essais in (2, 5, 40):
            with self.subTest(attempts=essais):
                m, _ = routage.pour_lancement(None, dict(base, attempts=essais))
                self.assertEqual(m, "opus")

    def test_l_escalade_ne_touche_pas_un_modele_impose(self):
        """Un humain qui tape --model haiku sur une relance sait ce qu'il fait."""
        m, _ = routage.pour_lancement(
            "haiku", tache(titre="CTRL-DOC 01", checklist=coche(4), attempts=3)
        )
        self.assertEqual(m, "haiku")

    def test_un_compteur_absent_ou_bizarre_ne_leve_pas(self):
        for essais in (None, "", "deux", -1):
            with self.subTest(attempts=essais):
                m, _ = routage.pour_lancement(
                    None, tache(titre="CTRL-DOC 01", checklist=coche(4),
                                touches=["a.py"], attempts=essais)
                )
                self.assertIn(m, routage.MODELES)


def json_copie(o):
    import json

    return json.loads(json.dumps(o))


if __name__ == "__main__":
    unittest.main()
