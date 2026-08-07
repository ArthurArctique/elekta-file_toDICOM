# elekta-file_toDICOM

Exploiter les **logs machine** d'un accélérateur Elekta (`.trf`) pour reconstituer
ce qui a été réellement délivré, et le confronter au **plan de traitement**, point
de contrôle par point de contrôle.

Parc visé : **Versa HD**, tête Agility (80 paires de lames), encodage TRF v3.

---

## Ce que fait l'outil

```
   archive SDD (.zip)                        RT Plan (.dcm)
          │                                        │
          │  ArchiveTrf                            │  LecteurRtplan
          │  décode, regroupe                      │  MU totales
          ▼                                        ▼  + empreinte du champ
      les séances                            ce qu'on cherche
   (fichiers recollés)                               │
          │                                          │
          └──────────────► correspondantes() ◄───────┘
                                  │
                                  │  MU à 1 % près
                                  │  dessin du champ sous 3 mm
                                  ▼
                    les séances de CE plan (ses fractions)
                                  │
                                  │  Chaine — substitution :
                                  │  la grille du plan est gardée,
                                  │  le mesuré y est injecté
                                  ▼
                     RT Plan dérivé, en mémoire
                                  │
                                  │  EcrivainDicom
                                  │  identité neuve, UNAPPROVED
                                  ▼
                        .dcm sur disque, ou comparé directement
```

Le point clé : **on ne reconstruit pas le plan, on y substitue**. Un TRF contient
2 700 échantillons là où le plan a 111 points de contrôle. Repartir de zéro
donnerait vingt fois trop de points ; garder la grille du plan et y interpoler le
mesuré donne un fichier directement comparable à l'original.

---

## Démarrage

```python
from visualisation import Interface
Interface().lancer()          # puis http://127.0.0.1:8050
```

Tout se fait dans la page : localiser l'archive, parcourir ses séances,
confronter un RT Plan, exporter les fractions retenues, comparer. **Rien n'est
téléversé** — l'application tourne sur le poste et lit les fichiers là où ils
sont, une archive SDD pesant plusieurs gigaoctets.

En script, via `main.py` : renseigner les trois chemins en tête, décommenter
l'appel voulu.

```python
from noyau import Chaine
Chaine("plan.dcm", "SDD+xxxx.zip", sortie="delivres/").executer()
```

**Dépendances** : `numpy`, `pydicom`, `pymedphys` ; `dash` et `plotly` en plus
pour les pages.

---

## Les fichiers, et comment ils s'enchaînent

### `noyau/` — la chaîne

| Module | Ce qu'il fait | Qui l'appelle |
|---|---|---|
| `conventions.py` | Noms de colonnes du TRF, géométrie visée, seuils, conversions de repère | tous les autres |
| `archive_trf.py` | **`ArchiveTrf`** : décode les TRF d'un zip, les regroupe en séances, et rend celles qui correspondent aux critères **qu'on lui donne** — elle ne lit jamais un plan | `Chaine`, les pages |
| `lecteur_rtplan.py` | **`LecteurRtplan`** : un RT Plan — ses MU, sa trajectoire dépliée, son `ds` brut | `Chaine`, le comparateur |
| `ecrivain_dicom.py` | **`EcrivainDicom`** : donne au dérivé une identité neuve. `preparer()` sans écrire, `ecrire()` avec contrôle du fichier écrit | `Chaine`, les pages |
| `chaine.py` | **`Chaine`** : orchestre les trois — trouve les séances du plan, y substitue le mesuré, écrit | `main.py`, l'interface |

La séparation qui compte : **`ArchiveTrf` ignore tout des plans**. Elle reçoit un
total de MU et une empreinte de champ, et rend les séances compatibles. C'est ce
qui permet de la tester seule, et de changer le critère sans y toucher.

### `visualisation/` — les pages

| Module | Ce qu'il fait |
|---|---|
| `interface.py` | **`Interface`** : les trois onglets autour d'une archive chargée une fois — Séances · Plan → export · Comparer |
| `visualiseur_seances.py` | **`Visualiseur`** : les séances d'une archive, TRF par TRF. Met en cache dans `seances/` pour que les lancements suivants soient immédiats |
| `comparateur_dicom.py` | **`Comparateur`** : le plan face à ses délivrances, et les délivrances entre elles |

Ces pages **ne contiennent aucune logique métier** : le découpage en séances, la
lecture des plans et la substitution viennent tous de `noyau`. Les deux dernières
sont aussi lançables seules.

### `main.py`

Le point d'entrée : trois chemins en tête, trois fonctions à décommenter —
ouvrir l'interface, exporter les séances d'un plan, ou dérouler les étapes une à
une pour voir ce que chaque classe apporte.

---

## Ce qui est fiable, et comment on le sait

Toutes les mesures ci-dessous sont reproductibles sur le jeu de test public de
pymedphys.

| | Mesuré |
|---|---|
| **Décodage du TRF** | Confronté colonne par colonne à pymedphys : **350/350** colonnes identiques au bit près sur un fichier v1, **354/354** sur un v3 |
| **Reconnaître les séances d'un plan** | **0,40 – 0,49 mm** d'écart de dessin face au bon plan, **12,8 mm** face à un autre traitement. Le seuil est à 3 mm, largement entre les deux |
| **Recoller une séance interrompue** | Une séance éclatée en 4 fragments rend le même résultat qu'une séance intacte : 0,49 mm contre 0,40 et 0,46 |
| **Écart plan / délivré** | **0,22 mm** médian sur les lames dans le champ, p95 1,2 mm |
| **Écart entre fractions** | **0,07 – 0,10 mm** — les fractions se ressemblent deux fois plus entre elles qu'elles ne ressemblent au plan |
| **Aller-retour DICOM** | Ce qui est écrit est relu à l'identique ; chaque fichier produit est relu sans `force=True` et son identité vérifiée |

Sur une semaine réelle de logs (420 fichiers, une machine) : **0 fichier
illisible**, séances reconstituées y compris les interrompues.

La dernière ligne du tableau mérite un mot : **c'est la comparaison des fractions
entre elles qui est le meilleur indicateur.** Un écart au plan peut être une
propriété normale de la machine — le retard du servomoteur en est une, il explique
l'essentiel de l'écart observé. Une fraction qui s'écarte *des autres* signale au
contraire quelque chose de ce jour-là.

---

## Les limites

Elles sont réelles, documentées en tête de `noyau/chaine.py`, et non corrigées.

**Les MU par faisceau sont réparties au prorata du plan.** Le total vient du log,
mais sa répartition entre faisceaux suit celle du plan. Un plan 100 + 200 MU
délivré 90 + 200 ressortira en 96,7 + 193,3 : total juste, répartition fausse.
`BeamMeterset` ne doit donc pas se lire comme « les MU relevées pour ce faisceau ».

**L'axe des MU comporte des plateaux.** Entre deux segments, les lames se
repositionnent sans que la dose avance : 54 % des échantillons partagent une MU
déjà vue, et le plus long plateau dure 18 s pendant lesquelles une lame parcourt
108 mm. L'interpolation y retient la géométrie d'arrivée — défendable, mais subi
plutôt que choisi.

**Seuls les tags déjà présents dans un point de contrôle sont réécrits.** Un plan
qui omet `GantryAngle` sur un point inchangé ne peut pas recevoir l'angle mesuré à
cet endroit. La structure du plan est préservée au prix de la fidélité.

**Les sens de rotation ne sont pas reconstruits** : `GantryRotationDirection`
reste celui du plan alors que les angles, eux, sont mesurés.

**L'appariement repose sur deux critères** — MU totales et médiane du dessin du
champ. La séparation mesurée est large, mais une médiane peut diluer quelques
lames très fausses.

**Géométrie figée à 80 paires de lames.** Une autre configuration est refusée à
la lecture, avec un message clair, plutôt que silencieusement mal découpée.

---

## Ce qui bloque encore

**Une chaîne reproductible d'obtention des plans.** Un `.rtp` exporté à la main
valide la méthode, il n'industrialise rien. Les deux voies — export DICOM depuis
Mosaiq, ou accès SQL à sa base — sont aujourd'hui fermées.

C'est le seul verrou. Le reste est de l'ingénierie dont le principe est établi.

---

## Les scripts d'exploration

Antérieurs au noyau, conservés parce qu'ils font des choses qu'il ne fait pas —
lire un `.rtp`, diagnostiquer une archive, éprouver un découpage alternatif.
Chacun documenté en tête de fichier, dans `exploration/`.

| Script | Rôle |
|---|---|
| **`organiser_trf.py`** | Inventorie une archive SDD, reconstitue les séances. `--extraire` crée un dossier par séance, `--diagnostic` détaille les fichiers atypiques, `--filtre` cible un fichier |
| **`lire_rtp.py`** | Décode un plan RTP Connect exporté par Mosaiq |
| **`chercher_seances.py`** | Retrouve les séances d'un plan dans une archive. `--methode` explique les critères, `--detail` les déroule |
| **`comparer_rtp_seance.py`** | Confronte un plan à une séance précise |
| **`visualiser_rtplan.py`** | Explore un RT Plan DICOM dans le navigateur : faisceaux, points de contrôle, ouverture du collimateur, tags bruts |
| **`comparer_dicom.py`** | Confronte plusieurs RT Plan dans le navigateur — ancêtre de `visualisation/comparateur_dicom.py` |
| **`seance_vers_dicom.py`** | Retrouve seul les séances d'un plan et écrit un RT Plan délivré par fraction. `--consigne` écrit la consigne du servo au lieu de la position atteinte |

### Démonstration et vérification

| Script | Rôle |
|---|---|
| `diagnostic_cumuls.py` | Examine la colonne `exces` de `seances.csv` : dit si les chaînages abusifs sont isolés, ou si la référence elle-même est contaminée |
| `seances_par_horloge.py` | Découpage alternatif : recolle tous les échantillons d'une machine sur son horloge interne et coupe aux « Terminated Ok ». `--comparer` le confronte à `organiser_trf.py` |
| `trf_vers_dicom_vmat.py` | TRF → RT Plan DICOM en VMAT, par substitution |
| `verification_chaine.py` | Rejoue les vérifications sur les données publiques |
| `extraire_donnees_visu.py` | Alimente le visualiseur |

### Pages autonomes

| Page | Rôle |
|---|---|
| `lecteur_trf.html` | Ouvre un TRF et affiche ses 350 colonnes. **Tout se passe dans le navigateur**, rien n'est transmis |
| `visualiseur.html` | Explique visuellement la traduction plan ↔ log, ses doutes et son coût |

---

## Les documents

| Fichier | Pour quoi faire |
|---|---|
| **[LA_CHAINE.md](LA_CHAINE.md)** | **À lire en premier.** De quoi tout refaire sans les scripts d'ici : structure binaire du TRF, tags du plan, recalage sur les MU, règles de regroupement et d'appariement |
| **[MEMOIRE.md](MEMOIRE.md)** | Les pièges qui produisent un résultat plausible et faux, les chiffres mesurés, et les erreurs commises pendant le projet |
| [COMPRENDRE_LES_FICHIERS.md](COMPRENDRE_LES_FICHIERS.md) | Les deux formats expliqués sans prérequis de physique médicale, sur de vrais octets |
| [ETAT_DES_LIEUX.md](ETAT_DES_LIEUX.md) | La synthèse critiquable : chaque affirmation avec son niveau de preuve |
| [RESULTATS_RECHERCHE.md](RESULTATS_RECHERCHE.md) | Le détail des mesures, corrections comprises |
| [data/README.md](data/README.md) | Provenance des jeux de données publics et leurs limites |
| `archives/` | Documents dépassés, conservés pour l'historique |

---

## Données, confidentialité et rôle de l'IA

Ce projet a été mené avec l'assistance d'une IA. La règle a été simple et tenue
d'un bout à l'autre : **aucune donnée patient ni aucun fichier machine réel n'a
été transmis.**

### Ce à quoi l'IA a eu accès

| Source | Nature |
|---|---|
| Jeu de test public de **pymedphys** (Zenodo) | Données de recherche publiées et anonymisées : deux couples TRF + RT Plan appariés, six TRF d'un arc VMAT. Patient « PHYSICS, TEST » — ce sont des délivrances de **contrôle qualité**, pas des traitements |
| Code source de **pymedphys** installé localement | Pour établir les conventions de décodage plutôt que les supposer |
| Sources publiques | Dépôt GitHub et journal de pymedphys, articles publiés, déclaration de conformité DICOM de Mosaiq, documentation du format RTP Connect |
| Le dépôt lui-même | Scripts et documents |

Tout ce qui est mesuré et chiffré dans ces documents provient de ces données
publiques. Elles sont retéléchargeables par
`exploration/verification_chaine.py`, ce qui rend chaque résultat reproductible
par un tiers.

### Ce à quoi l'IA n'a jamais eu accès

- Les **420 fichiers TRF réels** de la sauvegarde SDD hebdomadaire
- Le **plan RTP** exporté depuis Mosaiq
- Le moindre identifiant de patient, nom de champ réel ou horodatage de séance
- La base Mosaiq

### Comment les outils ont malgré tout été éprouvés sur données réelles

**Par un humain, en local.** Les scripts ont été exécutés sur le poste du
service, sur les vrais fichiers, et seuls des **résultats agrégés** ont été
rapportés : nombres de fichiers et de séances, versions d'encodage, répartition
des états machine, plages de MU, motifs de rejet.

Ce sont ces retours — et eux seuls — qui ont révélé les défauts les plus
coûteux : l'estimation de MU qui signalait 130 séances à tort, le nom de machine
qui rejetait tout, la comparaison limitée à un seul faisceau sur un plan à deux
arcs. Aucun n'aurait été trouvé sur les seules données publiques.

### Ce que cette contrainte a produit

Elle a orienté la conception, pour le mieux :

- **Les scripts tournent en local**, sans réseau, et ne transmettent rien.
- **`lecteur_trf.html` décode entièrement dans le navigateur** — un TRF réel peut
  y être ouvert sans qu'aucun octet ne quitte le poste.
- **Le mode `--diagnostic`** n'affiche que des chiffres et des octets bruts, de
  quoi comprendre un problème sans exposer de contenu.
- **Le `.gitignore`** exclut `.trf`, `.dcm`, `.rtp`, `data/` et les rapports
  produits, dès avant l'arrivée des vraies données.

---

## La réserve de fond

Tout ce qui est mesuré ici relève de la **cohérence interne**. Les méthodes
confrontées lisent le même capteur : si l'encodeur de la machine dérive, elles
dérivent ensemble sans que rien ne le signale.

C'est l'avertissement explicite de la documentation pymedphys — *« it is not the
intent of this project to replace patient specific QA measurements »* — et la
raison pour laquelle le contrôle par mesure indépendante garde sa raison d'être.

Les conventions géométriques — ordre des bancs de lames, signe des mâchoires —
sont validées par aller-retour contre pymedphys, ce qui prouve leur cohérence,
pas leur justesse physique. Seule une mesure indépendante sur un champ
franchement asymétrique le ferait.

---

## Les fichiers produits

⚠️ Les RT Plan dérivés sont des **documents d'analyse**. Ils reçoivent un
`SOPInstanceUID` et un `SeriesInstanceUID` neufs — ils ne peuvent donc pas écraser
le plan d'origine —, `ApprovalStatus = UNAPPROVED` et un libellé suffixé `_DEL`.

Ce marquage **ne garantit pas leur rejet par un système clinique** : ils gardent
leur SOP Class et restent importables. La barrière doit être l'environnement —
répertoire isolé, aucune route DICOM vers le réseau clinique.

Le cache `seances/` contient des copies de TRF réels, et les `.dcm` produits
portent les identifiants du patient d'origine. Le `.gitignore` exclut `*.trf`,
`*.dcm`, `*.zip`, `data/`, `seances/` et `donnee_patient/`.
