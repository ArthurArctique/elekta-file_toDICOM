> ⚠️ **DOCUMENT ARCHIVÉ — conservé pour l'historique, ne pas s'y fier.**
>
> C'est le plan de recherche initial, rédigé avant toute mesure. Une bonne part
> de ses hypothèses se sont révélées fausses et sont annotées comme telles au fil
> du texte. Il montre d'où l'on est parti, pas où l'on en est.
>
> Pour l'état actuel : [README](../README.md) · [MEMOIRE](../MEMOIRE.md)

---

# Plan de recherche — TRF Elekta (Versa HD) → DICOM RT Plan → comparaison des points de contrôle

Branche : `exploration`. Document de cadrage, pas de code.
Toutes les affirmations « code » ci-dessous viennent de la lecture de
pymedphys 0.41.0 installé localement, pas de la documentation en ligne.

> 📌 **Ce plan a été exécuté.** Les réponses, avec niveau de certitude et
> sources, sont dans **[RESULTATS_RECHERCHE.md](../RESULTATS_RECHERCHE.md)**.
> Plusieurs hypothèses de ce document s'y trouvent corrigées — les passages
> concernés sont signalés ci-dessous.

## Cadrage retenu

| | |
|---|---|
| Machines | **Elekta Versa HD** |
| Objet de la comparaison | **Les points de contrôle** (gantry, MLC, mâchoires, MU) |
| Données TRF | **Aucune pour l'instant** — les trouver et les récupérer fait partie du travail |
| RT Plan | Non fournis pour l'instant — les tags nécessaires sont dérivés en §5 |

---

## 1. Verdict Versa HD : configuration nominale supportée

Le Versa HD embarque un MLC **Agility** : 160 lames / **80 paires**, plus des
diaphragmes de secours. Or :

- `from_trf` lit exactement **80 paires** (`Y1 Leaf 1..80/Scaled Actual (mm)` et
  `Y2 Leaf 1..80/…`) → correspondance directe.
- `from_dicom` n'accepte que la combinaison `{"MLCX", "ASYMY"}` et lève une
  `ValueError` sinon → c'est précisément la signature DICOM d'un Agility.

**Le Versa HD est donc la configuration nominale de pymedphys.** Le risque n°3
de la version précédente de ce document tombe.

⚠️ **Piège de nommage à vérifier sur données réelles.** Les conventions Elekta et
DICOM ne coïncident pas. Ce que le code implémente :

| Grandeur | Nom TRF (Elekta) | Type DICOM | Attribut `Delivery` |
|---|---|---|---|
| Lames | `Y1 Leaf N` / `Y2 Leaf N` | `MLCX` | `mlc` |
| Diaphragmes | `X1 Diaphragm` / `X2 Diaphragm` | `ASYMY` | `jaw` |

Les axes portent des noms **croisés** entre les deux mondes. De plus,
`_from_dicom_beam` applique des inversions de signe et des permutations de
colonnes côté mâchoires (`jaw[:,1] = -jaw[:,1]`, échange des colonnes 0 et 1) et
retourne les bancs de lames. Ces conventions **s'annulent en aller-retour** — le
test de non-régression amont ne les couvre donc pas — mais elles **fausseraient
une comparaison directe TRF ↔ plan**. À valider explicitement sur un champ
asymétrique connu (typiquement un champ où X1 ≠ X2).

---

## 2. `to_dicom` est sur le chemin critique — et c'est le maillon faible

> ✏️ **Révisé.** La v1 de ce document recommandait de contourner `to_dicom` et de
> renommer le dépôt. L'objectif étant bien de **produire un DICOM**, cette
> recommandation est retirée : `to_dicom` reste sur le chemin critique. Ce qui
> suit décrit donc des **problèmes à résoudre**, pas des raisons de contourner.
> Voir [RESULTATS_RECHERCHE.md §1, §3 et §10](../RESULTATS_RECHERCHE.md).

`Delivery` est le pivot de pymedphys : un conteneur de 5 séries
(`monitor_units`, `gantry`, `collimator`, `mlc`, `jaw`) alimentable depuis un TRF
**comme** depuis un RT Plan. Les deux entrées produisent la même structure.

```
   TRF  ──from_trf──►  Delivery ◄──from_dicom──  RT Plan
                          │
                    comparaison ici
```

Passer par `to_dicom` pour ensuite comparer deux fichiers DICOM serait un détour
**inutile et fragile**, pour trois raisons établies :

1. **`to_dicom` casse en VMAT.** Il appelle `get_gantry_angles_from_dicom`, qui
   lève `"Only a single gantry angle per beam is currently supported"` dès qu'un
   faisceau contient plusieurs angles de gantry — c'est-à-dire tout arc. Sur un
   Versa HD, majoritairement VMAT, c'est rédhibitoire.
2. **`to_dicom` n'écrit rien de neuf.** Il exige un `dicom_template` et n'y
   remplace que les `ControlPointSequence`, `BeamMeterset` et les angles. Le
   « plan délivré » serait donc dérivé du plan prévu : tout ce qui n'est pas
   point de contrôle serait identique par construction. Comparer les deux
   fichiers ne pourrait rien révéler de plus que comparer les `Delivery`.
3. **Son auteur amont le qualifie lui-même de « giant ball of sticky tape »**,
   en opposant explicitement le chemin TRF → `Delivery`, décrit comme
   *« quite robust »*
   ([pymedphys#1046](https://github.com/pymedphys/pymedphys/issues/1046)).

**Bonne nouvelle : `from_dicom` gère le VMAT.** Vérifié dans le code —
`_from_dicom_beam` lit les angles **point de contrôle par point de contrôle**
(`get_cp_attribute_leaning_on_prior(control_points, "GantryAngle")`) et ne passe
jamais par la fonction restrictive. La trajectoire complète du gantry est donc
récupérée.

> ⚠️ Une réserve pratique : il faut **passer `fraction_group_number`
> explicitement** à `from_dicom`. Si on le laisse à `None` avec plusieurs groupes
> de fractions, pymedphys tente une auto-détection qui, elle, appelle la fonction
> restrictive et re-casse en VMAT.

---

## 3. Aligner deux échantillonnages incompatibles

> ✏️ **Partiellement corrigé.** Le TRF porte une colonne `Control point` qui
> indexe **directement** les points de contrôle du plan — l'appariement est donc
> natif et exact, contrairement à ce qui est écrit ci-dessous. Reste
> l'interpolation *à l'intérieur* d'un point de contrôle en delivery dynamique.
> Voir [RESULTATS_RECHERCHE.md §4 et §6](../RESULTATS_RECHERCHE.md).

| | TRF | RT Plan |
|---|---|---|
| Nature de l'axe | **Temps**, 25 Hz (40 ms) | **Index de point de contrôle** |
| Volume | Des milliers d'échantillons | Quelques dizaines à ~180 CP |
| Origine | Mesure machine | Prescription du TPS |

Les deux `Delivery` ne sont donc **pas comparables terme à terme**. Il faut les
projeter sur un axe commun, et l'axe naturel est le **MU cumulé** — pas le temps
(le TPS n'en a pas), pas l'index de CP (le TRF n'en a pas au même sens).

Ce que pymedphys fournit et ce qu'il ne fournit pas :

- ✅ `_filter_cps()` — audité : il ne repose **pas** sur les codes d'état machine
  (`Radiation On`, `Pause`, `Interupted`…) comme je le supposais, mais uniquement
  sur les MU. `find_relevant_control_points` ne garde que les échantillons ayant
  une variation de MU d'au moins un côté, éliminant les plages « faisceau coupé /
  déplacement seul ». Utile, mais purement du dégrossissage.
- ❌ **Aucun ré-échantillonnage sur la grille du plan.** À écrire.

**C'est le cœur du projet.** Sous-questions :

- Interpoler le TRF aux valeurs de MU cumulé des CP du plan (linéaire ? au plus
  proche voisin ?), ou l'inverse ?
- Que faire des segments à MU constant mais mouvement non nul (inter-segments) ?
- Comment gérer une délivrance interrompue puis reprise (MU non monotone) ?
  `_from_pandas` écrête déjà les diffs négatives (`diff[diff < 0] = 0`) puis
  refait un `cumsum` — à comprendre avant de s'y fier.
- Que compare-t-on par lame : position de chaque lame, ou ouverture par paire ?
  Métriques : écart max, RMS, percentile 95, par lame et par CP ?

**Piste alternative à évaluer** : le TRF contient sa propre référence attendue,
déjà alignée temporellement — donc **sans aucun problème de ré-échantillonnage**.
Cela ne remplace pas la comparaison au plan du TPS (l'attendu du TRF vient du
système de contrôle, pas du TPS), mais c'est une comparaison gratuite, immédiate,
et un excellent premier livrable.

> ✏️ **Corrigé** : il ne s'agit pas de colonnes `Scaled Expected` (elles
> n'existent pas) mais de colonnes **`Positional Error`**, avec la relation
> `Attendu = Actual + Positional Error`. Voir
> [RESULTATS_RECHERCHE.md §5](../RESULTATS_RECHERCHE.md).

---

## 4. Où sont les TRF et comment les récupérer

pymedphys automatise déjà cette récupération : `_trf/manage/diagnostics_zips.py`
et `orchestration.py` sont la documentation la plus concrète disponible.

**Chemin d'accès :**

```
\\<IP_du_linac>\Backup\TCS\SDD+*.zip
```

Les `.trf` sont **à l'intérieur** de ces archives de diagnostic
(`SDD` = System Diagnostic Dump). `extract_diagnostic_zips_and_archive` les en
extrait en filtrant sur l'extension `.trf`.

### 4.1 Ce qu'il faut configurer sur le linac pour que les TRF existent

Point ajouté après la v1 : **le partage ne contiendra des `.trf` que si la
machine en produit**, ce qui n'a rien d'automatique. Par ordre de blocage :

| # | Prérequis | Nature | Qui |
|---|---|---|---|
| 1 | **Licence « Treatment Record File »** | Contractuelle | Elekta Sales |
| 2 | Modèle générant des TRF (Versa HD/Integrity : oui ; Synergy : **non**) | Matérielle | — |
| 3 | Génération des TRF activée dans le TCS | Mode service | Ingénieur Elekta |
| 4 | Fréquence des dumps SDD réglée au maximum | Mode service | Ingénieur Elekta |
| 5 | Partage `\\NSS\Backup\TCS` accessible + compte SAMBA | Réseau | Elekta + informatique |
| 6 | IP DNS hospitalière attribuée au NSS | Réseau | Informatique |

**Le point 1 est le plus important et le moins évident** : la production de TRF
est une **fonctionnalité licenciée** chez Elekta. Si elle n'est pas au contrat,
aucun développement ne compensera — il n'y aura simplement pas de fichiers.
C'est la première question à poser.

**Questions à instruire pour cette section :**

- Avons-nous la licence TRF sur chacun des Versa HD ?
- La génération est-elle activée, et depuis quand ?
- Où se règle la fréquence des SDD, et à quelle valeur est-elle aujourd'hui ?
- Quelle est la rétention réelle configurée sur nos machines ?
- Si la licence manque : bascule-t-on sur **iCom** (flux temps réel, pas de
  licence, mais serveur d'écoute à déployer et résolution moindre) ?

Détail des réponses trouvées et de leur fiabilité :
[RESULTATS_RECHERCHE.md §11](../RESULTATS_RECHERCHE.md).

### 4.2 Contraintes opérationnelles

**Trois contraintes, par ordre de gravité :**

1. 🔴 **Rétention ≈ 8 jours seulement.** La doc amont est explicite : les backups
   de diagnostic contiennent les délivrances *« for the previous 8 days »*.
   **Il est donc impossible d'aller chercher des données rétrospectives.** Il faut
   mettre en place la collecte automatique *avant* de pouvoir constituer un jeu
   de données. Cela remonte la récupération en tête de priorité — c'est le seul
   axe où le retard est irrécupérable.
2. 🟠 **Accès réseau à négocier.** Le partage exige un compte du **NSS** du linac,
   *« supplied by an Elekta engineer »*. C'est une démarche auprès d'Elekta et du
   service biomédical/informatique, pas une tâche technique — donc à lancer tout
   de suite, le délai est administratif.
3. 🟠 **Fréquence des dumps à régler.** Les SDD ne sont produits qu'à intervalle
   configuré ; il faut demander la fréquence maximale disponible, sinon des
   délivrances seront simplement absentes.

**Questions à instruire :**

- Quelle est la fréquence actuelle de génération des SDD sur vos Versa HD ?
- Le partage `\\IP\Backup\TCS\` est-il ouvert par défaut sur votre configuration
  (la doc amont indique que le NSS partage son répertoire de backup par défaut) ?
- Sur quel poste faire tourner la collecte quotidienne (Windows sur le réseau
  clinique) ? Quelle politique de stockage ?
- Quelles IP / quels numéros de série pour chaque machine ? (le TRF identifie la
  machine par son numéro de série, ex. `2619`)
- Les TRF contiennent-ils des données patient au sens RGPD ? Le header porte
  `field_label` + `field_name` — pas de nom patient directement, mais un
  identifiant de champ ré-identifiant via le R&V. À trancher avec le DPO.

**Risque de fond à documenter** : le format TRF n'est **pas documenté par
Elekta**. Le décodeur pymedphys est explicitement du reverse engineering
(*« Determined through brute force reverse engineering only. Not based upon
official documentation »*). Une mise à jour firmware du Versa HD peut donc casser
le décodage sans préavis. `pymedphys trf detect` sert à diagnostiquer la version
d'encodage (1 à 4 supportées).

---

## 5. Tags DICOM nécessaires, dérivés

Liste établie en remontant le code de `_dicom/rtplan/core.py`,
`_dicom/delivery/core.py` et `_dicom/rtplan/build.py` — c'est-à-dire strictement
ce que pymedphys lit pour construire un `Delivery`, donc le minimum requis pour
la comparaison des points de contrôle.

### 5.1 Strictement indispensables

| Tag | Nom | Rôle |
|---|---|---|
| (300A,0070) | `FractionGroupSequence` | Sélection de la prescription |
| (300A,0071) | ↳ `FractionGroupNumber` | À passer explicitement (cf. §2) |
| (300C,0004) | ↳ `ReferencedBeamSequence` | Lien groupe → faisceaux |
| (300C,0006) | ↳↳ `ReferencedBeamNumber` | Clé de jointure vers `BeamSequence` |
| **(300A,0086)** | ↳↳ **`BeamMeterset`** | **MU totales du faisceau** |
| (300A,00B0) | `BeamSequence` | Les faisceaux |
| (300A,00C0) | ↳ `BeamNumber` | Clé de jointure |
| (300A,00B6) | ↳ `BeamLimitingDeviceSequence` | Description du collimateur |
| (300A,00B8) | ↳↳ `RTBeamLimitingDeviceType` | Doit valoir `MLCX` **et** `ASYMY` |
| (300A,00BC) | ↳↳ `NumberOfLeafJawPairs` | 80 attendu (Agility) |
| (300A,00BE) | ↳↳ `LeafPositionBoundaries` | Largeurs de lames (par `np.diff`) |
| **(300A,010E)** | ↳ **`FinalCumulativeMetersetWeight`** | Dénominateur de conversion MU |
| (300A,0110) | ↳ `NumberOfControlPoints` | Contrôle de cohérence |
| (300A,0111) | ↳ `ControlPointSequence` | **Les points de contrôle** |
| (300A,0112) | ↳↳ `ControlPointIndex` | Index |
| **(300A,0134)** | ↳↳ **`CumulativeMetersetWeight`** | **L'axe d'alignement (§3)** |
| (300A,011E) | ↳↳ `GantryAngle` | Angle de bras |
| (300A,011F) | ↳↳ `GantryRotationDirection` | Sens (`CW`/`CC`/`NONE`) |
| (300A,0120) | ↳↳ `BeamLimitingDeviceAngle` | Angle de collimateur |
| (300A,0121) | ↳↳ `BeamLimitingDeviceRotationDirection` | Sens |
| (300A,011A) | ↳↳ `BeamLimitingDevicePositionSequence` | Positions |
| (300A,011C) | ↳↳↳ `LeafJawPositions` | **Lames et mâchoires** |

**Conversion MU** (implémentée aux lignes 309–312 de `_dicom/delivery/core.py`) :

```
MU(cp) = BeamMeterset × CumulativeMetersetWeight(cp) / FinalCumulativeMetersetWeight
```

**Deux pièges sur ces tags :**

- `CumulativeMetersetWeight` est **absent si le plan a été exporté sans calcul de
  dose** — pymedphys lève alors une erreur explicite. À vérifier dès le premier
  export du TPS.
- `GantryAngle` et `BeamLimitingDeviceAngle` peuvent n'être présents que sur le
  **premier** point de contrôle, les suivants héritant implicitement de la valeur
  précédente. D'où `get_cp_attribute_leaning_on_prior`, qui propage la dernière
  valeur connue. **Toute lecture naïve avec pydicom se plantera ici.**

### 5.2 Nécessaires à l'appariement TRF ↔ plan

Le header TRF expose (`_trf/decode/header.py`) : `machine` (n° de série),
`date` (UTC) + `timezone`, `field_label` (ex. `'1-1'`), `field_name`
(ex. `'AP G0'`), `mu` (MU totales). Le champ complet est stocké au format
`label/name`, et `field_label` est **vide pour les faisceaux en mode service**.

Hypothèse d'appariement **sans Mosaiq**, à tester :

| Tag | Nom | Correspondance TRF supposée |
|---|---|---|
| (300A,00C2) | `BeamName` | ↔ `field_name` |
| (300A,00C3) | `BeamDescription` | ↔ `field_name` |
| (300A,0002) | `RTPlanLabel` | ↔ `field_label` (?) |
| (300A,0003) | `RTPlanName` | contexte |
| (0010,0020) | `PatientID` | à croiser hors DICOM |
| (300A,0086) | `BeamMeterset` | ↔ header `mu` — **discriminant fort** |

pymedphys, lui, apparie via **Mosaiq SQL** (`_trf/manage/identify.py`) en croisant
horodatage + `field_label` + `field_name`. Question ouverte : avez-vous un Mosaiq
interrogeable, ou faut-il apparier à la main ? Sans R&V, le triplet
(machine, horodatage, MU totales) est probablement suffisant pour un jeu de test
restreint.

### 5.3 Non nécessaires ici

`SourceAxisDistance` (300A,00B4), `SourceToSurfaceDistance` (300A,0130),
`IsocenterPosition` (300A,012C), `SurfaceEntryPoint` (300A,012E) : utilisés
ailleurs dans pymedphys (reconstruction de dose), **pas** pour la comparaison des
points de contrôle. Idem pour tout le CIOD structures/dose.

---

## 6. Ordre d'exécution proposé

Réordonné : la contrainte des 8 jours domine tout le reste.

> ✏️ **Réordonné après recherche.** Version à jour ci-dessous.

| # | Action | Pourquoi maintenant | État |
|---|---|---|---|
| 1 | **Vérifier la licence TRF auprès d'Elekta** (§4.1) | Sans elle, le projet n'a pas de données. Rien d'autre n'a d'importance tant que ce n'est pas tranché | ⬜ interne |
| 2 | **Lancer la demande d'accès NSS + réglage SDD** | Délai administratif, chemin critique | ⬜ interne |
| 3 | **Épingler `numpy<2`** | `to_dicom` est totalement cassé sinon | ⬜ 5 min |
| 4 | Installer `pydicom` | Prérequis DICOM | ✅ fait (3.0.2) |
| 5 | Valider la chaîne sur les données Zenodo | Dérisque sans données patient | ✅ fait |
| 6 | **Obtenir un plan VMAT anonymisé** | Seul trou de connaissance majeur restant | ⬜ 10 min de test |
| 7 | Mettre en place la collecte quotidienne des SDD | Démarre l'accumulation (rétention ~8 j) | ⬜ |
| 8 | Décoder un premier TRF réel (`trf detect` + colonnes) | Profil de données | ⬜ |
| 9 | Prototyper la comparaison via `Positional Error` | Livrable rapide, zéro dépendance au TPS | ⬜ |
| 10 | Écrire la comparaison au plan (index CP + interpolation MU) | Le cœur du projet | ⬜ |
| 11 | Valider le guide de lames dynamique sur un champ latéralisé | Seul doute résiduel sur les positions | ⬜ |

Les étapes 1–2 ne demandent pas de code et bloquent tout le reste : à lancer
avant d'écrire quoi que ce soit. L'étape 3 débloque le développement immédiat.

---

## 7. Questions restantes

1. **VMAT ou step-and-shoot, dans quelles proportions ?** Le chemin retenu (§2)
   gère les deux, mais l'étape 8 (alignement) est nettement plus délicate en arc.
2. **Y a-t-il un Mosaiq (ou autre R&V) interrogeable ?** Détermine si
   l'appariement TRF ↔ plan est automatisable ou manuel (§5.2).
3. **Granularité d'un TRF** : un fichier par faisceau, par fraction, par séance ?
   À constater sur les premières données réelles.
4. **Quel TPS** produit les RT Plans ? (Monaco → `Delivery.from_monaco` existe et
   pourrait servir de troisième point de comparaison)
5. **Statut du projet** : prototype de recherche, ou outil destiné à un usage
   clinique routinier ? Change radicalement les exigences de validation.
6. **Critères d'acceptation** : quels seuils sur les écarts de position de lames
   et de MU font qu'une délivrance est jugée conforme ?

### Une réserve à garder en tête

La documentation amont est explicite et mérite d'être citée telle quelle :
*« It is not the intent of this project to replace patient specific QA
measurements »*. Deux raisons y sont données : les positions de lames et
mâchoires rapportées **ne sont pas indépendantes de la machine** (une panne de
positionnement peut ne pas apparaître dans le log), et les modèles de faisceau
des TPS ne modélisent pas toujours fidèlement les MLC. À intégrer au cadrage si
l'outil vise un usage clinique.

---

Sources externes : [pymedphys#1046](https://github.com/pymedphys/pymedphys/issues/1046),
[Elekta Logfile Decoding and Indexing](https://docs.pymedphys.com/en/latest/users/background/elekta-logfiles.html).
Le reste provient de la lecture du code source de pymedphys 0.41.0 installé.
