# Diagnostic : pourquoi une réponse d'orchestratrice n'arrive pas dans la session

Tâche t-01 de la campagne c-01. Diagnostic seul, aucune correction. Aucun fichier de
`ordo/` n'a été modifié.

## Verdict en une phrase

Les deux symptômes ont bien une cause commune, mais **ce n'est pas celle qui était
supposée** : le canal d'envoi ne perd rien, c'est le **rapport `asking` qui n'est jamais
consommé**, et cette seule omission produit à la fois les questions dupliquées et la
réponse jamais injectée.

## L'hypothèse de départ, vérifiée puis infirmée

> « Rien ne relit l'état réel de la session après avoir écrit dedans. »

Le constat est exact — `panes.send()` n'a aucun accusé de réception — mais il n'explique
aucun des deux symptômes. Dix mesures sur de vrais panes Claude Code, dans les trois états
du brief et au-delà, n'ont produit **aucune perte** :

| Cas | Condition d'envoi | Message arrivé | Délai avant exécution |
|---|---|---|---|
| A | pane au repos | oui | 3,73 s |
| B | pane occupé, outil de 70 s en cours | oui | 71,9 s |
| C | envoi à l'instant où `busy()` retombe | oui | 2,13 s |
| D0 | occupé, 0 ms entre le texte et l'Entrée | oui | 39,28 s |
| D1 | occupé, 50 ms | oui | 40,56 s |
| D2 | occupé, 250 ms | oui | 40,32 s |
| D3 | occupé, 1 000 ms | oui | 40,30 s |
| E | occupé, texte de 484 caractères | oui | 44,53 s |
| F | occupé, texte de 3 lignes | oui | 44,94 s |
| G | occupé, 3 envois consécutifs | 3 sur 3 | 46,64 s |

Protocole : la preuve n'est pas ce que montre l'écran. Chaque message demande à la cible de
créer un fichier témoin ; un `C-m` avalé laisse le témoin absent. Le premier critère essayé
— chercher le marqueur à l'écran — était un décor : le texte envoyé contenait déjà son
propre marqueur, il passait donc même si rien n'était soumis. Il a été remplacé avant toute
conclusion.

**Les deux appels `send-keys` ne sont pas en cause.** Faire varier le délai entre le texte
et la validation de 0 à 1 000 ms ne change rien (cas D). Le collage entre crochets n'avale
pas la touche : un texte de trois lignes arrive en un seul message, sauts de ligne
préservés, sans soumission prématurée (cas F).

**Ce que fait réellement le TUI quand il est occupé** (cas B, mesure directe) : le message
est accepté et mis en file. La ligne de saisie n'affiche pas le texte mais
`Press up to edit queued messages`, et le message est traité à la fin de **l'appel d'outil
en cours**, pas à la fin du tour. Le cas 2 a été confirmé une seconde fois en direct :
l'`ordo say` envoyé pendant que j'écrivais ce diagnostic est arrivé **pendant** mon tour,
accolé au résultat de l'outil suivant, parce que mes appels d'outils sont courts. La cible
du cas B, occupée par un unique outil de 70 s, l'a reçu à 71,9 s. Même mécanique, latence
différente.

Conséquence directe : un message injecté dans une session occupée **n'est pas perdu, il est
différé**, d'une durée qui est celle de l'appel d'outil en cours. Un observateur qui
regarde 30 s et ne voit rien conclut à tort à une perte.

## La cause commune, mesurée

Un rapport `asking` est appliqué mais **jamais consommé**. `report.clear()` existe et n'est
appelé qu'au lancement d'une tâche (`ordo/cli.py:368`), jamais après lecture. Le fichier
reste sur disque et redevient un signal neuf à chaque fois que la tâche repasse
`running`. La boucle :

| Étape | Ce qui se passe | Où |
|---|---|---|
| 1 | l'exécutante écrit un rapport `asking`, une seule fois | disque |
| 2 | tick étape 2 applique le rapport, tâche → `waiting`, crée `q-01` | `controle.py:490-518` |
| 3 | `ordo answer q-01` écrit la réponse et **n'envoie rien** ; imprime `answered` | `cli.py:1553-1566` |
| 4 | tick étape 4 injecte, tâche → `running`. Le rapport `asking` est toujours là | `controle.py:556-565` |
| 5 | tick suivant : la tâche est `running`, l'étape 2 **relit le même rapport**, crée `q-02` | `controle.py:494-518` |
| 6 | l'étape 4 ne regarde que `_latest_question` : `q-02`, sans réponse → **elle n'injecte rien** | `controle.py:557-559` |

L'étape 5 est le bug des questions dupliquées. L'étape 6 est le bug de la réponse perdue.
C'est la même omission qui produit les deux.

**Reproduction 1, les questions dupliquées.** Un seul rapport `asking`, six ticks :

```
tick 1: état=waiting  questions=['q-01']                 injections=0
tick 2: état=running  questions=['q-01']                 injections=1
tick 3: état=waiting  questions=['q-01','q-02']          injections=1
tick 4: état=running  questions=['q-01','q-02']          injections=2
tick 5: état=waiting  questions=['q-01','q-02','q-03']   injections=2
tick 6: état=running  questions=['q-01','q-02','q-03']   injections=3
```

Un rapport, trois questions, textes identiques. C'est exactement `q-01, q-02, q-03` sur
t-43, et chacune des trois réponses a bien réinjecté du texte dans une session qui
travaillait déjà.

La colonne `injections` compte les messages réellement arrivés dans le pane. Le compteur
brut du script comptait les occurrences du mot `Answer:` à l'écran, soit le double, chaque
injection laissant la ligne tapée puis l'écho du shell ; les trois injections distinctes
ont été recomptées une à une (`tour 1`, `tour 3`, `tour 5`) avant d'écrire ce tableau.

**Reproduction 2, la réponse perdue.** Même montage, la réponse arrive pendant que la tâche
est `running` :

```
tick 1 : q-01 créée, état=waiting
la tâche est repassée running, rapport asking toujours sur disque
`ordo answer q-01 "..."` -> imprime 'q-01  answered'
tick 2 : événements = ['t-01 waiting for an answer: ...', 'q-02 created for t-01']
tick 2 : injections de la réponse humaine = 0
VERDICT : réponse perdue = True, état affiché par poll = waiting
```

`answered` imprimé, zéro injection, `waiting` affiché. Le symptôme de t-55, à l'identique.

**Pourquoi c'est intermittent.** Tout dépend de la seconde où le tick tombe. `store.now()`
est horodaté à la seconde (`store.py:66`) et `_latest_question` départage par `max()` sur
`askedAt` : deux questions nées dans la même seconde ne sont plus départagées du tout,
`max()` rend la première du dictionnaire. Mesuré : avec trois questions au même horodatage,
`_latest_question` a rendu `q-02` alors que la plus récente était `q-91`. Quand le hasard
rend l'ancienne question, elle porte la réponse et l'injection a lieu — « resumes, answer
injected », t-43 et t-54. Quand il rend la neuve, la réponse est ignorée sans un mot —
t-55. La même exécution donne les deux résultats selon la seconde où elle tombe.

## Le troisième silence : le garde-fou est désarmé par le bug

Le seul motif de réveil temporel est `control-round`, qui se déclenche après
`WAKE_IDLE_AFTER_S = 900 s` sans événement journalisé (`controle.py:387-396`). Sa référence
est `chantier["lastEvent"]`, que `_tick_one` rafraîchit dès qu'il produit **au moins un**
événement (`controle.py:623-627`). Or la boucle produit `q-NN created for t-NN` à chaque
tick. Le journal n'est donc jamais silencieux, le compteur de 900 s ne repart jamais, et le
filet ne se déclenche pas — précisément dans la situation qu'il existe pour rattraper.

Aucun des sept motifs de réveil ne couvre « une réponse a été donnée et n'a pas été
injectée ». `pane-mort` ne se déclenche pas : le pane est vivant. C'est le cumul décrit
dans le brief : `answered` qui ment, `waiting` indistinguable d'une attente normale, et un
garde-fou neutralisé par le défaut lui-même.

## Un mode de perte réel du canal, trouvé en cherchant

Le canal ne perd rien dans les trois états du brief, mais il perd **tout** dans un
quatrième, non prévu : après une commande slash qui ouvre un panneau plein écran.

Mesuré sur `/cost` (choisi parce que local et gratuit) : le panneau masque la boîte de
saisie, et à partir de là plus aucun message ne passe. État final du pane, vérifié :

| Observable | Valeur |
|---|---|
| pane vivant (`pane_dead`) | 0, vivant |
| `panes.busy()` | **False**, donc « au repos » pour Ordo |
| ligne de saisie | absente de l'écran |
| témoin du message envoyé après | jamais créé |
| témoin après une Entrée de récupération | jamais créé |

Un pane vivant, déclaré au repos, dont tous les messages tombent dans le vide sans laisser
de trace : c'est le profil de `%131`. Ordo envoie lui-même une commande slash par ce canal,
`/compact` à l'étape 5.9 de `_tick_one` (`controle.py:590`, `_compacter`).

Une seconde perte a été observée par accident, au premier montage du banc : sur un dossier
jamais approuvé, le dialogue de confiance était ouvert ; le texte envoyé a été **jeté** et
le `C-m` a validé « Yes, I trust this folder ». Ordo se protège de ce cas dans `launch`
(`wait_ready()` refuse d'envoyer), mais `say`, `answer` et `_compacter` appellent
`panes.send()` sans jamais revérifier l'état du pane.

## Ce qui est mesuré, ce qui reste supposé

| Affirmation | Niveau | Preuve | Non vérifié |
|---|---|---|---|
| `panes.send()` n'a pas perdu de message dans les 3 états du brief | ÉPROUVÉ | 10 essais, cas A à G, témoins sur disque | testé sur Haiku 4.5, tmux 3.7b, un seul poste |
| Faire varier le délai texte → Entrée ne change rien | ÉPROUVÉ | cas D, 4 valeurs de 0 à 1 000 ms | au-delà d'1 s non testé |
| Un message envoyé pendant un tour est différé, pas perdu | VÉRIFIÉ | cas B, 71,9 s pour un outil de 70 s ; `Press up to edit queued messages` | comportement propre à cette version du TUI |
| Un seul rapport `asking` produit N questions identiques | VÉRIFIÉ | reproduction 1, 3 questions en 6 ticks | |
| Une réponse peut être ignorée en silence, `poll` affichant `waiting` | VÉRIFIÉ | reproduction 2, 0 injection, `answered` imprimé | |
| `_latest_question` ne départage pas deux questions de la même seconde | VÉRIFIÉ | `store.py:66` ; `_latest_question` a rendu `q-02` au lieu de `q-91` | |
| `lastEvent` rafraîchi par les questions dupliquées désarme `control-round` | ÉCRIT | lecture de `controle.py:387-396` et `623-627` | **jamais exécuté** : aucune campagne n'a été laissée tourner 900 s pour l'observer |
| Une commande slash à panneau tue le canal sans trace | VÉRIFIÉ | cas H puis cas I, témoins jamais créés, `busy()` False | mesuré sur `/cost` ; **`/compact`, ce qu'Ordo envoie vraiment, n'a pas été mesuré** |
| Le texte resté 20 min dans la ligne de saisie de `%131` | **SUPPOSÉ** | — | **non reproduit.** Dans mes dix mesures la file affiche `Press up to edit queued messages`, jamais le texte brut. Le lien avec le panneau slash est une hypothèse |

Deux limites de validité à connaître avant de s'appuyer sur ce document :

- Les mesures côté `controle.py` ont tourné sur le working tree du 16 août 2026 vers 02:45
  UTC, pas sur `3ac962c`. **Seize fichiers étaient modifiés et non commités** par une
  session concurrente pendant mes mesures ; un premier run a échoué sur un `NameError`
  transitoire (`COMPACT_TOURS`) dû à une écriture en cours sur `controle.py`. Les deux
  reproductions ont été refaites après stabilisation et sont cohérentes entre elles, mais
  elles doivent être rejouées sur un arbre propre avant de servir de test de non-régression.
- Le banc utilise Haiku 4.5. Le TUI est le même, la latence ne l'est pas.

## Découpage proposé de la correction

Quatre tâches disjointes par fichier de production. Les trois premières se suffisent à
elles-mêmes ; la quatrième est le durcissement.

| Tâche | Fichier de production | Objet | Dépend de |
|---|---|---|---|
| A | `ordo/report.py` | consommer le rapport après application, pour qu'un `asking` déjà traité ne redevienne jamais un signal neuf | — |
| B | `ordo/controle.py` | l'étape 4 cible la question **répondue et non injectée**, pas la plus récente ; départage par identifiant, pas par `askedAt` à la seconde ; ne pas laisser un événement purement répétitif rafraîchir `lastEvent` | A |
| C | `ordo/cli.py` | `answer` cesse d'imprimer `answered` pour une réponse simplement écrite : soit elle est acheminée, soit la sortie dit qu'elle attend un tick | A |
| D | `ordo/panes.py` | `send()` relit le pane après écriture et échoue bruyamment quand la boîte de saisie n'est pas là (panneau slash, dialogue) au lieu d'écrire dans le vide | — |

A est la racine : B et C traitent chacun un symptôme, mais laissés seuls ils masquent la
cause sans la retirer. D est indépendant et peut être mené en parallèle.

Le test tmux réel que réclame l'objectif de campagne a sa forme dans le cas B ci-dessus :
occuper une vraie session, envoyer, exiger un **témoin sur disque**. Il rougit si le message
n'est pas soumis. Il ne rougirait pas s'il se contentait de chercher le texte à l'écran :
ce piège a été rencontré et écarté au premier essai de ce diagnostic.

## Reproduire

Scripts de mesure, hors dépôt :
`/private/tmp/claude-501/-Users-luisparra-Documents-ordo-public/e85f344a-65fb-4d21-9e29-dd18fb3d423b/scratchpad/`
(`banc.py`, `mesure.py`, `mesure2.py`, `mesure3.py`, `repro_questions.py`, `repro_perte.py`).
Traces, captures de panes et relevés horodatés : `/private/tmp/diag-envoi/`.
