# Résultats de recherche — TRF Elekta → DICOM RT Plan

Réponses au [plan de recherche](PLAN_RECHERCHE.md). Chaque réponse porte un
**niveau de certitude** et sa **source**.

> 📘 Si la structure interne des deux formats n'est pas familière, commencer par
> **[COMPRENDRE_LES_FICHIERS.md](COMPRENDRE_LES_FICHIERS.md)** — explication sans
> prérequis de physique médicale, sur les vrais fichiers.

## Méthode

La recherche n'est pas restée documentaire. J'ai récupéré le jeu de test public
de pymedphys (Zenodo, 4 fichiers : deux couples `imrt.trf` + `rtplan.dcm`
appariés, machine Elekta n° 2619, Agility) et **exécuté la chaîne complète**.
La majorité des réponses ci-dessous sont donc mesurées, pas déduites.

Convention de certitude :

- **100 %** — reproduit ici, trace à l'appui
- **80–95 %** — mesuré sur les 2 jeux de test, mais un seul modèle de machine
- **50–75 %** — source unique, ou déduit du code sans avoir pu l'exécuter
- **INTERNE** — aucune source externe ne peut répondre, il faut demander au service

---

## 1. 🔴 Bloquant immédiat : NumPy 2 casse `to_dicom`

**Certitude : 100 %** — reproduit, trace complète.

```
ValueError: Unable to avoid copy while creating an array as requested.
  pymedphys/_dicom/delivery/utilities.py:32  →  mlc = np.array(mlc, copy=False)
```

NumPy 2.0 a changé la sémantique de `copy=False` : au lieu de copier quand c'est
nécessaire, il lève une exception. L'environnement a **numpy 2.4.2**, donc
`Delivery.to_dicom` — le cœur de l'objectif du dépôt — **ne fonctionne pas du
tout**.

**Aggravant : pip ne vous protégera pas.** pymedphys 0.41.0 déclare
`numpy>=1.26` **sans borne supérieure** (vérifié dans ses métadonnées). Une
installation propre aujourd'hui tire donc numpy 2.x et casse silencieusement —
`from_trf` et `from_dicom` continuent de marcher, seul `to_dicom` échoue.

**17 sites concernés** au total (`grep -rn "copy=False"`), dont
`_dicom/delivery/utilities.py` (2), `_metersetmap/metersetmap.py` (4),
`_base/delivery.py` (2), `_dicom/dose.py` (5).

Deux corrections possibles :

| Option | Effort | Remarque |
|---|---|---|
| **Épingler `numpy<2`** | trivial | Recommandé pour démarrer |
| Patcher `np.array(x, copy=False)` → `np.asarray(x)` | 17 remplacements mécaniques | Bon candidat à une PR amont |

✅ **Vérifié : c'est le seul blocage.** Avec les deux fonctions de
`utilities.py` corrigées en mémoire, la chaîne complète passe (voir §3).

Note : `metersetmap` fonctionne malgré ses 4 occurrences (ses entrées sont déjà
des ndarray). Seul `to_dicom` est réellement cassé.

---

## 2. État de l'amont

| Question | Réponse | Certitude |
|---|---|---|
| Version la plus récente de pymedphys ? | **0.41.0**, publiée le **30/01/2025**. C'est celle installée. | 100 % (PyPI) |
| La limitation VMAT est-elle corrigée en amont ? | **Non.** 0.41.0 est la dernière version. | 100 % |
| Python 3.11.9 est-il supporté ? | Oui (`>=3.10,<3.13`). | 100 % |
| pydicom 3.0.2 est-il compatible ? | **Oui**, toute la chaîne passe. pymedphys demande `>=2.0.0`. | 95 % |

**Historique utile trouvé au CHANGELOG** : la version 0.32.0 corrigeait un bug où
*« leaf pairs 77, 78, 79, and 80 on the Y2 bank were decoded into having the
wrong sign »*. Un décodage TRF peut donc être faux **silencieusement** — argument
pour tester les lames de bord explicitement. (Certitude 100 %, CHANGELOG.)

---

## 3. La chaîne TRF → DICOM fonctionne (une fois numpy réglé)

**Certitude : 100 %** — exécuté sur le couple `original`.

```
Delivery.from_trf      →  8947 points (25 Hz, 357,8 s)
_filter_cps()          →  4254 points
to_dicom(template, 1)  →  9 faisceaux, RT Plan écrit sur disque
from_dicom(résultat)   →  aller-retour EXACT sur les 5 grandeurs
                          (monitor_units, gantry, collimator, mlc, jaw)
```

**⚠️ Mais le plan produit n'a pas la structure de l'original :**

| Faisceau | CP dans le plan d'origine | CP dans le plan reconstruit |
|---|---|---|
| 1-1 | 25 | **538** |
| 1-5 | 22 | **575** |
| … | ~20 | ~400–570 |

`to_dicom` transforme **chaque échantillon TRF retenu en point de contrôle**. Le
fichier produit est un RT Plan valide, mais c'est un enregistrement haute
résolution du déroulé — pas un plan comparable point à point avec l'original.
**Le passage par DICOM ne résout donc pas le problème d'alignement, il le
déplace.** Ce qui suit le résout.

---

## 4. ⭐ Le TRF contient l'index des points de contrôle du plan

**Certitude : 98 %** — c'est le résultat le plus important de cette recherche.

La colonne **`Control point/Actual Value (None)`** du TRF est un **index global
et croissant sur toute la séance**, qui pointe directement dans les points de
contrôle du plan. Vérifié champ par champ :

| Champ | CP dans le plan | Plage dans le TRF | |
|---|---|---|---|
| 1 | 25 | 1 → 24 | ✓ |
| 2 | 21 | 25 → 45 | ✓ exact |
| 3 | 20 | 46 → 65 | ✓ exact |
| 4 | 21 | 66 → 86 | ✓ exact |
| 5 | 22 | 87 → 108 | ✓ exact |
| 6 | 22 | 109 → 130 | ✓ exact |
| 7 | 20 | 131 → 150 | ✓ exact |
| 8 | 20 | 151 → 170 | ✓ exact |
| 9 | 24 | 171 → 194 | ✓ exact |

Les frontières tombent **exactement** sur les cumuls du plan
(25+21+20+21+22+22+20+20+24 = 195).

> 🔴 **Correction majeure — décalage d'un indice.** J'écrivais ici que « l'index
> TRF correspond à l'index global 0-based ». **C'est faux.** Mesuré sur l'arc
> VMAT : le compteur de la machine est **1-based**, le `ControlPointIndex` du
> plan est **0-based**. Autrement dit **`Control point` = k ↔ point de contrôle
> k−1 du plan**.
>
> | Décalage testé | Écart en MU | Écart-type | Écart lames |
> |---|---|---|---|
> | **−1** | **−0,10 MU** | **0,10** | **0,08 mm** |
> | 0 (ce que j'affirmais) | −3,28 MU | 2,66 | **2,59 mm** |
> | +1 | −6,46 MU | 5,06 | 4,89 mm |
>
> Avec le bon décalage, l'accord est **au niveau de la résolution du format**
> (0,1 MU, 0,1 mm). Avec le mauvais, l'erreur médiane est de **2,59 mm** — soit
> quatorze fois l'écart qu'on cherche à mesurer, et parfaitement invisible :
> le fichier produit reste valide et les chiffres restent plausibles.
>
> Vérifié sur les deux TRF complets et indépendants de la même délivrance.
> Le plan a 111 points (0→110), le TRF compte de 1 à 110.
>
> **À revérifier sur vos propres données** : rien ne garantit que cette
> convention soit la même sur toutes les versions d'encodage ou toutes les
> configurations.
>
> ✏️ **Précision apportée par les données réelles** : ce compteur est global
> **à l'intérieur d'un fichier** — il court sur tous les faisceaux d'une même
> séance — mais il **repart à 1 dans chaque fichier**, exactement comme celui
> des MU. Sur une séance interrompue, il faut donc sommer les fragments et non
> prendre le maximum. Mesuré : 11 + 11 + 78 + 13 = 113 pour un plan de 111
> points, l'excédent venant du recouvrement aux reprises.

**pymedphys ignore complètement cette colonne** — elle est décodée dans la table
mais `_from_pandas` ne la lit jamais.

C'est l'appariement natif que je pensais absent dans la v1 du plan. **Il n'y a
pas besoin de deviner la correspondance : la machine l'écrit.**

---

## 5. ⭐ Le TRF contient sa propre référence attendue

**Certitude : 95 %** — mesuré. **Correction d'une erreur de la v1 du plan.**

Il n'existe **aucune colonne `Scaled Expected`** — j'avais tort. Le TRF contient
à la place, pour chaque axe, une colonne **`Positional Error`** :

```
164 colonnes  «… /Scaled Actual (mm)»
164 colonnes  «… /Positional Error (mm)»
  6 + 6       idem en (deg)
```

**La relation est : `Attendu = Actual + Positional Error`.** Vérifiée en suivant
une lame pendant son déplacement : `actual` passe de −61,8 à −57,0 mm, `error`
décroît de 12,2 à 7,5 mm, et **leur somme reste à −49,6 mm** (écart-type mesuré
sur 8 plages de déplacement : 0,05 à 0,51 mm).

C'est mieux qu'une colonne « expected » : l'écart prévu/réalisé est donné
**directement, à chaque échantillon de 40 ms, sans aucun ré-échantillonnage**.

⚠️ **Réserve importante** : cet « attendu » est la consigne du système de
contrôle de la machine, pas le RT Plan du TPS. Il valide l'exécution machine, pas
la fidélité au plan. Les deux comparaisons restent complémentaires.

---

## 6. Comparaison des points de contrôle : résultats mesurés

J'ai implémenté les deux méthodes d'appariement et les ai comparées au plan réel.

| Méthode | Écart médian *par CP* † | Écart absmax médian par CP |
|---|---|---|
| **A** — index `Control point` natif, dernier échantillon | 1,15 mm | 8,3 mm |
| **B** — interpolation linéaire en MU cumulé | **0,39 mm** | 9,0 mm |

† médiane, sur les points de contrôle, de l'écart médian par lame. Agrégé
autrement — médiane sur l'ensemble des couples (CP, lame, banc) — la méthode B
donne **0,85 mm** et un p95 de **14,3 mm** (chiffres produits par
[`exploration/verification_chaine.py`](exploration/verification_chaine.py)). Les
deux agrégations sont légitimes ; il faudra en choisir une et s'y tenir.

**Certitude : 100 %** sur les chiffres, **75 %** sur leur interprétation.

Lectures :

- **L'alignement est correct** : le gantry concorde à **0,2° près au maximum**,
  les mâchoires à 0,8 mm médian. Si l'appariement était faux, ces valeurs
  exploseraient.
- **B est nettement meilleur en médian** (0,39 mm) — la delivery est dynamique
  (`BeamType = DYNAMIC`, sliding window), donc les lames bougent en continu et
  l'interpolation en MU est plus juste que « le dernier échantillon du CP ».
- **Mais les deux gardent des valeurs extrêmes élevées** (jusqu'à 76 mm). Ces
  écarts se concentrent sur les **paires de lames fermées** : restreindre aux
  lames dont l'ouverture au plan dépasse 2 mm fait tomber la médiane de 1,5 à
  0,6 mm. Les lames fermées sont garées à des positions machine qui ne
  correspondent pas au nominal du plan, sans conséquence dosimétrique.

**Recommandation** : méthode B (interpolation en MU cumulé), en utilisant
l'index `Control point` pour découper les champs, et en excluant les paires
fermées des statistiques.

**Conventions MLC : aucune conversion à faire.** J'ai testé les 5 transformations
plausibles entre `from_trf` et `from_dicom` :

| Transformation | Écart médian |
|---|---|
| **identité** | **1,50 mm** ✅ |
| lames inversées (1↔80) | 9,10 mm |
| bancs échangés | 21,60 mm |
| signe opposé | 22,20 mm |

L'identité gagne d'un facteur 6 à 15. Les deux entrées produisent déjà la même
convention. **Certitude : 95 %.** Le risque signalé en v1 du plan est levé côté
pymedphys — il resterait à vérifier si vous relisez le DICOM vous-même.

---

## 7. Structure et contenu réels d'un TRF

Tout mesuré sur les deux fichiers de test. **Certitude 100 %** sauf indication.

| Élément | Valeur |
|---|---|
| Échantillonnage | **0,04 s exactement (25 Hz)** |
| Colonnes | **350** |
| Durée du fichier `original` | 357,8 s / 8947 échantillons |
| États machine rencontrés | `Radiation On` (6174), `Move Only` (2283), `Intersegment` (264), `Terminated Checking` (216), `Terminated Ok` (10) |
| Axes journalisés | 160 lames (Y1/Y2 × 80), 2 diaphragmes X, **2 guides de lames (`Dlg Y1/Y2`)**, gantry, collimateur, 4 axes de table |

**Header** : `machine` (n° de série, ici `2619`), `date` UTC + `timezone`,
`field_label`, `field_name`, `mu`, `version` d'encodage.

**Le champ `mu` du header est en dixièmes de MU** — certitude 90 % :
header 9946,0 pour 994,4 MU mesurées ; header 4756,0 pour 475,5 MU. Facteur 10
sur les deux fichiers.

### Deux points de vigilance découverts

**a) Les guides de lames dynamiques (`Dlg Y1`/`Dlg Y2`) bougent — et pymedphys
les ignore.** Amplitude mesurée : 49,8 → 100,0 mm et 49,7 → 103,5 mm. Le
mécanisme Agility déplace les guides pour étendre la course des lames. pymedphys
ne lit que les positions de lames et jamais les guides. Les positions de lames
semblent bien absolues (l'aller-retour DICOM est exact, et l'accord au plan est
sub-millimétrique), donc ce n'est probablement pas un problème — mais **c'est à
confirmer explicitement** sur un champ très latéralisé, là où le guide est en
butée. **Certitude que c'est sans conséquence : 70 %.**

**b) Bug de libellé d'unités** : `Table Longitudinal`, `Table Lateral` et
`Table Height` sont annotées `(deg)` dans pymedphys alors que ce sont des
distances (valeurs mesurées : 649,5 et 179,4 — des mm). Cosmétique, mais piégeux
si vous exploitez la table. **Certitude 95 %.**

---

## 8. Appariement TRF ↔ RT Plan

**Le discriminant fort, c'est les MU.** Mesuré sur `original` :

| | MU par faisceau |
|---|---|
| TRF | 136,4 · 110,3 · 80,6 · 102,5 · 156,8 · 109,5 · 94,6 · 81,6 · 122,1 |
| Plan (`BeamMeterset`) | 136,3 · 110,5 · 80,6 · 102,4 · 156,9 · 109,5 · 94,7 · 81,6 · 122,1 |

Accord **à 0,2 MU près**. Sur le second couple, la signature MU identifie sans
ambiguïté le **groupe de fractions 2** (et pas le 1). **Certitude 100 %.**

**En revanche `field_label` n'est pas fiable** pour identifier les faisceaux :
il vaut `'1-2'` dans les **deux** fichiers, alors que l'un contient 9 champs et
l'autre 3, et que les faisceaux du plan s'appellent `1-1` … `1-9`. Soit le header
ne mémorise qu'un seul champ, soit ces fichiers de test sont des agrégats.
**À vérifier sur vos données. Certitude sur l'anomalie : 90 % ; sur son
explication : 30 %.**

→ Utilisez **(n° de série machine, horodatage UTC, MU par faisceau)** comme clé.

---

## 9. Granularité : 1 TRF = 1 séance complète

**Certitude : 85 %** — vérifié sur les 2 fichiers, mais un seul site/machine.

Le compteur de MU **repart à zéro à chaque faisceau** : 8 remises à zéro pour
9 champs (fichier `9FLD IMRT`), 2 pour 3 champs (`3 FIELD IMRT`). C'est d'ailleurs
la raison du `diff[diff < 0] = 0` suivi d'un `cumsum` dans `_from_pandas` :
pymedphys recolle les champs bout à bout.

Un fichier TRF couvre donc **tous les faisceaux d'une séance**, transitions de
gantry comprises. À confirmer sur vos Versa HD : c'est peut-être un choix de
configuration.

---

## 10. Et le VMAT ?

> ✅ **RÉSOLU — testé sur données VMAT réelles.** Un couple VMAT apparié a été
> trouvé et installé (§10 bis). Les certitudes passent de 75–85 % à **100 %**.

| Question | Réponse | Certitude |
|---|---|---|
| `from_dicom` gère-t-il le VMAT ? | **Oui, parfaitement.** Testé sur un arc Monaco de 111 CP : trajectoire complète récupérée, −180° → 180°, 111 angles distincts, 426,7 MU. | **100 %** — exécuté |
| `to_dicom` casse-t-il en VMAT ? | **Oui.** `ValueError: Only a single gantry angle per beam is currently supported`, exactement comme prévu par la lecture de code. | **100 %** — exécuté |
| Peut-on contourner ? | **Oui, démontré de bout en bout** (§10 bis) | **100 %** |

## 10 bis. ⭐ Pipeline VMAT complète, démontrée

**Certitude : 100 %** — [`exploration/trf_vers_dicom_vmat.py`](exploration/trf_vers_dicom_vmat.py)
tourne de bout en bout.

L'architecture B (§11 ter) contourne le blocage : au lieu de laisser pymedphys
reconstruire la séquence de points de contrôle — ce qui l'oblige à segmenter par
angle de gantry — on **garde la grille du plan et on y substitue** les valeurs
mesurées, interpolées sur l'axe des MU cumulées.

```
Plan VMAT (111 CP)  ─┐
                     ├─→  RT Plan « délivré » (111 CP)  →  from_dicom ✅
TRF (2717 éch.) ─────┘
```

Résultat sur le couple `979797_VMAT` :

| | |
|---|---|
| CP produits | **111 — identique au plan** (contre ~2700 avec `to_dicom`) |
| Aller-retour des lames | **exact (0,0000 mm)** — ce qu'on écrit est ce que `from_dicom` relit |
| Écart lames **dans le champ** | **médiane 0,18 mm**, p95 1,03 mm |
| Écart lames, toutes | médiane 0,45 mm, p95 6,90 mm, max 11,75 mm |
| Écart gantry | médiane 0,20°, max 1,14° |

> ✏️ **Correction.** Une première version de ces chiffres annonçait 0,20 mm et
> p95 1,36 mm pour les lames « dans le champ ». Le filtre était faux : j'avais
> écrit `banc1 − banc0 > 2`, alors que dans la convention `Delivery`
> **l'ouverture d'une paire est la *somme* des deux bancs** — le banc 0 vaut
> `X2` et le banc 1 vaut `−X1` (vérifié au millième contre le DICOM brut).
> L'ancien filtre retenait 27 % des lames, le bon en retient 52 %. La conclusion
> ne bouge pas, mais elle portait sur la mauvaise population.
>
> Une paire garée est à ~3,6 mm d'ouverture (`X1 = −1,8`, `X2 = +1,8`), d'où le
> seuil à 5 mm. Et ce sont les **mâchoires Y** qui déterminent quelles paires
> sont réellement exposées.

La médiane de 0,20 mm dans le champ tombe pile sur le seuil de sensibilité
(± 0,2 mm) du précédent publié — c'est cohérent avec une délivrance normale.
Les valeurs extrêmes restent portées par les paires de lames fermées (§6).

**Ce que ça valide :**

- Le VMAT n'est plus un risque — la pipeline complète existe et fonctionne.
- Les conventions de repère sont bonnes (aller-retour exact), à condition de
  **réutiliser `mlc_dd2dcm` / `jaw_dd2dcm` de pymedphys** plutôt que de les
  réécrire. J'ai fait l'erreur une première fois : des bancs de lames inversés
  donnaient 48 mm d'écart médian au lieu de 0,20 mm — une erreur **silencieuse**,
  qui produit un fichier parfaitement valide et des chiffres absurdes.
- Le déroulement de l'angle (`np.unwrap`) avant interpolation est indispensable :
  sans lui, le passage ±180° de l'arc produit des angles aberrants.

### Nouveau défaut trouvé dans pymedphys

`mlc_dd2dcm` et `jaw_dd2dcm` convertissent les positions par `.astype(str)` sur
des `float64`. Cela produit des chaînes de **17 à 21 caractères**
(`-49.599999999999994`), alors que la VR `DS` du standard DICOM en autorise
**16 au maximum** — pydicom émet un `UserWarning` et **le fichier produit est non
conforme**. Corrigé ici par un formatage à 4 décimales. Concerne aussi la sortie
de `to_dicom`. **Certitude : 95 %.**

---

## 11. Configuration côté linac *(section demandée)*

C'est la partie la moins bien documentée publiquement, et la plus dépendante
d'Elekta.

### 11.1 🔴 Il faut une licence Elekta

**Certitude : 85 %** — source unique mais explicite (liste pymedphys, réponse
d'un utilisateur : *« Check with your Elekta Sales or Engineers whether you have
the Treatment Record File (TRF) licences »*).

**La génération des TRF est une fonctionnalité licenciée.** Ce n'est pas une
case à cocher : si la licence n'est pas au contrat, aucun `.trf` n'est produit,
et aucun développement ne rattrapera ça.

👉 **C'est la toute première question à poser à Elekta.** Tout le projet en
dépend.

### 11.2 Modèle de machine — ce n'est pas le badge qui compte

> ✏️ **Révisé à la baisse (85 % → 50 %)** après la recherche d'antériorité
> (§11 bis).

Un ingénieur Elekta cité sur la liste pymedphys affirme que *« trf files are not
generated on Synergy's »*, les modèles antérieurs ne produisant que des « IMRT
Logs » bien plus pauvres sur l'activité des lames.

**Mais un article publié contredit cette généralisation** : le précédent le plus
proche du projet ([PMC10018669](https://pmc.ncbi.nlm.nih.gov/articles/PMC10018669/))
exploite des logs à 40 ms produits par… un **Synergy équipé d'un Agility et du
contrôle Integrity R4.0**.

**Le facteur déterminant est donc vraisemblablement le couple
« tête Agility + système de contrôle Integrity », pas le modèle commercial.**
Le témoignage négatif portait probablement sur un Synergy à tête MLCi2 et
contrôle antérieur.

Bonne nouvelle pour vous dans les deux lectures : un **Versa HD est Agility +
Integrity par construction**. Le doute porte sur la *version* d'Integrity et,
surtout, sur la licence (§11.1).

### 11.3 Accès réseau au NSS

**Certitude : 90 %** (doc amont + code).

- Partage : `\\<IP_du_NSS>\Backup\TCS\SDD+*.zip`
- Il faut une **IP DNS hospitalière attribuée au NSS** du linac
- Il faut un **compte SAMBA sur le NSS, fourni par un ingénieur Elekta**
- Le code amont note qu'il faut s'être connecté une fois au partage en cochant
  « mémoriser les identifiants »
- Identifiant de machine recommandé : **le numéro de série** (stable, et c'est
  ce que le header TRF inscrit)

### 11.4 Fréquence des sauvegardes de diagnostic

**Certitude : 60 %.** La doc amont recommande de programmer les backups de
diagnostic *« at their highest available frequency »*, mais **je n'ai trouvé
aucune source publique décrivant où ce réglage se fait**. C'est un paramètre
côté TCS/NSS, vraisemblablement accessible uniquement en mode service.

👉 À faire régler par Elekta lors de la même intervention que l'accès NSS.

### 11.5 Rétention

**Certitude : 75 %.** La doc amont indique 8 jours (*« for the previous 8
days »*). Source unique, non vérifiée sur matériel, et probablement dépendante de
la fréquence des dumps et de l'espace disque. **À faire confirmer par Elekta** —
c'est la contrainte qui dicte le calendrier du projet.

### 11.6 Alternative : iCom

**Certitude : 80 %.** Si la licence TRF n'est pas disponible, ou pour éviter la
contrainte de rétention, **iCom** est la porte de sortie : un flux temps réel
émis par le linac, capté par un « listener » tournant en service Windows sur un
serveur du réseau clinique. pymedphys expose `Delivery.from_icom`, et sa
documentation *Adding a Linac* décrit la configuration (`config.toml`,
répertoires d'échange, service).

Compromis :

| | TRF | iCom |
|---|---|---|
| Résolution | 25 Hz | ~5× moins de points (source : abstract AAPM 2020) |
| Rétention | ~8 jours, rétrospectif | Illimitée, mais **uniquement à partir du jour où on écoute** |
| Prérequis | Licence Elekta + accès NSS | Serveur + service Windows + réseau stable |
| Contenu lames | Riche | Plus pauvre |

**Certitude sur le facteur 5 : 60 %** (source secondaire, non vérifiée).

---

## 10 ter. ⭐ Fiabilité de la valeur **à** un point de contrôle

**Certitude : 95 %** — deux méthodes indépendantes confrontées.

Question distincte de celle du §6 (« l'écart au plan ») et de celle de la perte
inter-CP : **si l'on se contente d'un couple « une valeur du plan, une valeur du
log » par point de contrôle, cette valeur est-elle sûre ?**

Test : comparer deux façons totalement indépendantes de répondre à « où étaient
les lames au point de contrôle k ».

- **A** — interpolation sur les MU cumulées, depuis le plan (notre traduction)
- **B** — le premier échantillon que **la machine elle-même** attribue à ce point,
  via sa colonne `Control point`

Elles ne partagent aucune hypothèse. Leur désaccord borne l'incertitude réelle.

| | Écart entre A et B |
|---|---|
| Médiane | **0,076 mm** |
| p95 | 1,02 mm |
| p99 | 2,04 mm |
| Maximum | 4,76 mm |
| Part ≤ 0,1 mm (résolution du format) | **60 %** |
| Part ≤ 0,3 mm | 79 % |

**Verdict : oui, c'est fiable — mais pas uniformément.** La médiane est *sous* la
résolution du format : pour la majorité des lames, la valeur au point de contrôle
est aussi bonne que ce que le fichier peut exprimer. Il reste une queue.

### D'où vient la queue — mécanisme identifié

Le compteur de MU est quantifié à **0,1 MU**, et le débit médian est de
**2,5 MU/s** : une graduation vaut donc **40 ms**, exactement un échantillon.
L'axe des MU n'a aucune résolution de plus que l'axe du temps.

Or les lames vont jusqu'à **57,5 mm/s** (p95 : 27,5 mm/s). En 40 ms, une lame
rapide parcourt **1,10 mm**.

> Prédiction : 1,10 mm · Mesure du p95 de désaccord : **1,02 mm**.

Le mécanisme est donc établi : **l'incertitude sur une lame vaut sa vitesse
multipliée par la granularité de l'axe des MU.** Une lame lente ou à l'arrêt est
connue à 0,1 mm ; une lame rapide à environ 1 mm.

**C'est exploitable** : la vitesse de chaque lame se calcule depuis le log. On
peut donc attacher une **incertitude à chaque valeur** plutôt que de les traiter
toutes comme équivalentes.

### L'horodatage machine améliore l'axe — sur la queue, pas sur la médiane

**Certitude : 95 %** — mesuré sur les trois séances.

L'axe des MU est un escalier : 27,6 % des échantillons n'incrémentent pas le
compteur. Le temps, lui, avance toujours. On peut donc **dégraduer l'escalier**
en interpolant les MU entre les marches, sur l'horodatage réel du préfixe de
ligne (§ ci-dessous). Cela fait tomber le nombre de paliers de **749 à 204** —
les 204 restants étant les vraies plages sans dose, qu'il faut laisser plates.

Effet sur le désaccord entre les deux méthodes indépendantes :

| Axe des MU | Médiane | p95 | ≤ 0,1 mm |
|---|---|---|---|
| compteur brut | 0,076 mm | 1,02 mm | 60 % |
| **dégradué par l'horodatage** | **0,064 mm** | **0,93 mm** | 61 % |
| intégration du débit de dose | 0,535 mm | 6,04 mm | 25 % |

Et sur l'écart au plan, la mesure qui compte :

| Séance | Axe brut | Axe dégradué |
|---|---|---|
| complète n° 1 | 0,175 / **1,03** | 0,173 / **0,88** |
| complète n° 2 | 0,193 / **1,16** | 0,192 / **0,95** |
| interrompue, recollée | 0,200 / **1,44** | 0,195 / **1,27** |

*(médiane / p95, en mm, lames exposées)*

**La médiane ne bouge pas ; le p95 gagne 12 à 18 %.** C'est cohérent avec le
mécanisme : la médiane est portée par les lames lentes, que la quantification des
MU n'affecte pas ; la queue est portée par les lames rapides, pour lesquelles
0,1 MU vaut plus d'un millimètre. Le gain est donc exactement là où il manquait.

⚠️ **L'intégration du débit de dose est une fausse bonne idée** : la colonne
`Actual Dose Rate` est trop grossière, et l'axe reconstruit ainsi est **sept fois
pire** que le compteur brut.

Amélioration non implémentée dans les scripts du dépôt — elle demande de lire le
préfixe de ligne en octets bruts, que pymedphys n'expose pas.

### Deux réserves supplémentaires

- **27,6 % des échantillons n'incrémentent pas le compteur de MU**, et il existe
  une plage de **204 échantillons consécutifs** (8,2 s) totalement figée.
  Sur un plateau, l'interpolation en MU est **indéterminée** : plusieurs positions
  de lames différentes partagent la même abscisse. 23 des 111 MU cibles tombent
  sur une valeur répétée.
- Le désaccord A/B ne mesure que la cohérence interne. Il ne dit **rien** sur la
  justesse absolue : si l'encodeur de la machine se trompe, les deux méthodes se
  trompent ensemble.

---

## 10 quater. ⭐ Séparer le physique du méthodologique

**Certitude : 95 %.** Un écart entre le plan et la délivrance est **normal** —
l'enjeu n'est pas de le réduire mais de savoir ce qui, dedans, vient de la
machine et ce qui vient de la façon de mesurer.

Le TRF permet cette séparation, parce qu'il contient **sa propre mesure d'erreur**
(consigne − réalisé), établie par la machine au même instant, sans rien devoir à
notre chaîne de traitement.

### La mesure, sans aucune interpolation

Faite à l'échantillon que **la machine elle-même** attribue au point de contrôle.
Aucun choix méthodologique n'intervient.

| Composante | Médiane | p95 | Max |
|---|---|---|---|
| **Physique** — retard du servomoteur, mesuré par la machine | 0,300 mm | 1,20 mm | **2,70 mm** |
| **Total** — écart au plan | 0,300 mm | 1,20 mm | **9,90 mm** |

**La médiane et le p95 sont identiques.** Sur l'essentiel de la distribution,
l'écart plan/délivrance **est** le retard servo, et rien d'autre. Le résidu a une
médiane de 0,000 mm et un p95 de 0,00 mm.

**Le retard servo explique 95,9 % de l'écart cumulé.**

### Le retard servo est une loi propre, pas du bruit

| Vitesse de la lame | Retard médian | p95 |
|---|---|---|
| 0 – 2 mm/s | 0,10 mm *(le plancher, donc nul)* | 0,60 |
| 2 – 5 | 0,20 mm | 0,60 |
| 5 – 10 | 0,30 mm | 0,70 |
| 10 – 20 | 0,50 mm | 1,00 |
| 20 – 30 | 0,80 mm | 1,33 |
| 30 – 60 | 1,10 mm | 1,84 |

Strictement monotone, et proportionnel à la vitesse : c'est le comportement
attendu d'un asservissement, la lame traînant derrière sa consigne d'une constante
de temps. La pente donne environ **24 ms**. Le retard dépasse 1 mm sur 8 % des
lames, 2 mm sur 0,34 %, et ne dépasse jamais 2,70 mm.

C'est **une caractéristique de la machine** — mesurable, reproductible,
surveillable dans le temps. C'est probablement l'indicateur le plus intéressant
que le log puisse fournir.

### Ce qui reste et n'est pas physique

Seules **15 lames sur 9 120** (0,16 %) ont un résidu supérieur à 3 mm. Elles ne
sont ni rapides ni dispersées : elles se concentrent sur **cinq points de contrôle**
(69, 70, 79, 80, 81). C'est structurel, pas statistique — à élucider, mais
marginal.

### Le budget méthodologique, récapitulé

| Source | Ordre de grandeur | Réductible ? |
|---|---|---|
| Quantification du format | 0,1 mm | ❌ limite du fichier |
| Granularité de l'axe des MU | jusqu'à 1 mm sur lame rapide | ⚠️ −15 % via l'horodatage |
| Interpolation entre échantillons 40 ms | second ordre | ❌ limite du fichier |
| **Décalage d'indice ±1** | **2,6 mm** | ✅ à vérifier une fois |
| Choix des lames (fermées incluses ?) | 0,18 → 0,45 mm | ✅ convention à fixer |
| Recollement d'une séance interrompue | 0,40 → 0,20 mm | ✅ remise à l'échelle |

Les trois dernières lignes sont des **décisions**, pas des fatalités. Les deux
premières sont le prix du format.

> 👉 **Conclusion pour le cadrage du projet.** La chaîne est essentiellement
> transparente : sur 95 % des lames elle n'ajoute rien de mesurable au signal
> physique. Ce qu'on mesure alors, c'est la performance de l'asservissement des
> lames — pas un artefact de traitement. Reste que ce chiffre ne dit rien de la
> **justesse absolue** : si l'encodeur de la machine dérive, le log et son erreur
> dérivent ensemble, sans que rien ne le signale. Le contrôle par mesure
> indépendante garde donc sa raison d'être.

---

## 11 bis. Antériorité : qui a déjà fait ça ?

Recherche menée après coup, sur demande. **Réponse courte : beaucoup de monde.**
Ce n'est pas un sujet neuf — c'est une pratique établie, avec des produits
commerciaux. Ça ne disqualifie pas le projet, mais ça change les arguments à
avancer pour le justifier.

### A. Le précédent le plus proche — académique

**Le plus important de cette section.** *A method for patient-specific DVH
verification using a high-sampling-rate log file in an Elekta linac*
([PMC10018669](https://pmc.ncbi.nlm.nih.gov/articles/PMC10018669/)).

| | |
|---|---|
| Machine | Elekta Synergy + **Agility**, contrôle **Integrity R4.0** |
| Log | **40 ms** (le TRF haute résolution) |
| Technique | **VMAT**, 10 prostates |
| TPS | **Monaco 5.11.02** (Monte Carlo XVMC) |
| Outil | **Développement interne JAVA + Python** |

Leur méthode, citée : *« An in-house software was developed using JAVA and Python
to obtain the leaf positions at the control points from the HLF and to create
DICOM-RT files in which the leaf positions of the original plans were replaced
with those obtained from the HLF. **Linear interpolation was further required to
obtain values at each control point.** »*

**C'est mot pour mot l'architecture que j'ai validée en §3 et §6** — remplacement
dans le plan d'origine servant de template, et interpolation linéaire pour
ramener le log sur les points de contrôle (ma « méthode B »). Une équipe
indépendante a convergé sur la même solution, et l'a fait tourner **en VMAT**.

Résultats obtenus : écarts sur le D₉₅ du PTV ≤ 0,08 Gy (0,11 %), accord au
centre de 0,21 % ± 0,67 % contre mesure, sensibilité à des erreurs systématiques
de lames de **± 0,2 mm**.

👉 Deux enseignements : l'approche est validée par un tiers, et **le VMAT est
faisable** — ce qui déplace le risque du §10 « est-ce possible » vers « pymedphys
sait-il le faire ».

### B. Commercial — le sujet est industrialisé

**LINACwatch** ([Qualiformed](https://www.qualiformed.com/linacwatch), France) —
le plus directement concurrent. Il *« exporte un fichier DICOM RTplan intégrant
la "réalité" de l'irradiation telle que documentée par les log files »*, à
recharger dans le TPS pour recalculer la dose réellement délivrée. Il analyse
lames, mâchoires, gantry, collimateur, MU, fluence intégrale (gamma/chi) et
retards d'extinction du faisceau, en moins de 4 secondes après la séance.

⚠️ **Mais il lit l'Elekta à 4 Hz — c'est-à-dire l'iCom, pas le TRF à 25 Hz.**
C'est la différence exploitable : notre route offrirait **6× plus de résolution
temporelle** que le produit commercial. Il note aussi que les RT Plans générés
ont *« autant de points de contrôle que dans le log file »* — exactement le
comportement de `to_dicom` observé en §3 (538 CP pour 25). Ce n'est donc pas une
anomalie de pymedphys, c'est la norme du domaine.

Autres produits croisés : **Elekta Log File Convertor R3.2** (outil constructeur,
paramètres toutes les 40 ms) et le *log-based fingerprinting* de **Mobius3D**
([évalué sur Elekta](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8833274/)).

### C. Open source

**pymedphys est essentiellement seul.** Aucune alternative sérieuse trouvée pour
le décodage TRF. À signaler :

- `trf2dcm.py` dans le dépôt pymedphys, et un **convertisseur en ligne** sur
  `app.pymedphys.com`
- Un support **expérimental du TRF Elekta Unity** dans les versions récentes
- `pylinac` traite très bien le Varian mais pas le TRF nativement

### D. Autres travaux utiles

| Travail | Intérêt |
|---|---|
| [Kabat et al. 2019, *Med Phys*](https://aapm.onlinelibrary.wiley.com/doi/10.1002/mp.13374) | Performance du MLC Agility par log files **sur deux Versa HD** — la correspondance matérielle la plus proche de votre parc |
| [4 Hz + Monte Carlo SciMoCa, VMAT prostate](https://pmc.ncbi.nlm.nih.gov/articles/PMC8292700/) | Conclut que le log peut *remplacer* le contrôle par fantôme. Taux gamma 93,5–99,8 % en 2 %/1 mm |
| [Impact of log file source and data frequency on accuracy of log-based PSQA](https://www.researchgate.net/publication/371864234) | Traite directement l'arbitrage **TRF 25 Hz vs iCom 4 Hz** — à lire avant de choisir la source |

### E. Ce que ça change pour le projet

- ✅ **L'approche est validée par des tiers**, y compris en VMAT et sur Agility.
- ✅ **Notre différenciateur est la résolution** : 25 Hz (TRF) contre 4 Hz pour
  LINACwatch. À condition que la licence TRF soit là (§11.1).
- ⚠️ **La question « construire ou acheter » se pose sérieusement.** Si
  LINACwatch est déjà déployé ou accessible dans l'établissement, il faut un
  argument explicite pour développer : résolution temporelle, coût, maîtrise,
  souplesse de recherche, ou indépendance vis-à-vis d'un fournisseur.
- 📌 **Cible de sensibilité connue** : le précédent académique détecte des
  erreurs systématiques de lames de ± 0,2 mm. C'est un ordre de grandeur à viser,
  et un élément de réponse à la question ouverte « quels seuils d'acceptation ? ».

---

## 11 ter. Ce qui manque exactement à pymedphys

Découpage de la pipeline complète, étape par étape, avec ce que pymedphys couvre
réellement.

| # | Étape | pymedphys | Verdict |
|---|---|---|---|
| 1 | Récupérer les TRF du linac | `trf orchestrate` : rapatrie les zip SDD, extrait les `.trf`, indexe | ⚠️ **existe, couplé à Mosaiq** |
| 2 | Décoder le TRF | `pymedphys.trf.read()` → header + 350 colonnes | ✅ **complet et robuste** |
| 3 | Segmenter en faisceaux | *implicite et fragile* — voir ci-dessous | ❌ **point de rupture** |
| 4 | Apparier le log au bon plan | via **Mosaiq SQL uniquement** | ❌ **manquant hors Mosaiq** |
| 5 | Ramener le log sur les CP du plan | **rien** — fait l'inverse | ❌ **manquant, c'est le cœur** |
| 6 | Écrire le RT Plan DICOM | `to_dicom` | ⚠️ **cassé + inadapté** |
| 7 | Comparer | `metersetmap` + `gamma` (niveau fluence) | ❌ **rien au niveau CP** |

### Étape 3 — le vrai point de rupture

`_from_pandas` applique `diff[diff < 0] = 0` puis un `cumsum` : il **recolle les
faisceaux bout à bout** au lieu de les séparer. La séparation réelle n'intervient
qu'ensuite, dans `to_dicom`, via `_mask_by_gantry` — qui regroupe les échantillons
**par angle de gantry lu dans le template**.

👉 **C'est précisément pour ça que le VMAT casse.** En arc, un faisceau n'a pas
« un » angle de gantry, donc `get_gantry_angles_from_dicom` lève son exception.
Le problème n'est pas le VMAT en soi : c'est que pymedphys utilise le gantry
comme clé de segmentation.

Or **le TRF fournit une bien meilleure clé, que pymedphys ignore** : la colonne
`Control point` (§4), et les remises à zéro du compteur de MU (§9). Les deux
segmentent les faisceaux sans jamais regarder le gantry.

### Étape 5 — pymedphys fait l'inverse de ce qu'il faut

`to_dicom` produit **un point de contrôle par échantillon TRF retenu** : 538 CP
là où le plan en a 25 (§3). Pour recalculer une dose c'est acceptable — c'est
d'ailleurs ce que fait LINACwatch. **Pour comparer des points de contrôle,
c'est inexploitable** : il n'y a plus de correspondance 1-pour-1 avec le plan.

### Deux architectures cibles possibles

| | **A — Haute résolution** | **B — Substitution** |
|---|---|---|
| Principe | 1 CP par échantillon du log | On garde la grille de CP du plan, on y **substitue** les valeurs interpolées |
| Qui le fait | `to_dicom`, LINACwatch | **PMC10018669** |
| Nb de CP produits | ~20× le plan | **identique au plan** |
| Comparaison CP à CP | ❌ impossible | ✅ immédiate |
| Recalcul de dose au TPS | ✅ | ✅ (et plus rapide) |
| Bute sur le VMAT ? | ✅ oui, via `_mask_by_gantry` | ❌ **non — on ne reconstruit pas la structure** |

**Recommandation : architecture B.** Elle répond à l'objectif (produire un DICOM),
rend la comparaison des points de contrôle triviale, et **contourne
structurellement le blocage VMAT** — puisqu'on ne demande jamais à pymedphys de
reconstruire la séquence de points de contrôle.

C'est exactement ce que fait le précédent publié : *« créer des fichiers DICOM-RT
dans lesquels les **positions de lames** du plan d'origine sont remplacées par
celles obtenues du log »*. Ils ne touchent **que les lames** — le gantry, les MU
et la structure du plan restent ceux de l'original.

### Récapitulatif de ce qu'il reste à écrire

| Brique | Effort estimé | Dépend de |
|---|---|---|
| Correctif numpy (ou `numpy<2`) | trivial | — |
| Segmentation en faisceaux par `Control point` / resets MU | faible | §4, §9 |
| Interpolation en MU cumulé sur les CP du plan | **moyen** — le cœur | §6 |
| Substitution dans le template pydicom (architecture B) | moyen | pydicom seul, sans `to_dicom` |
| Appariement TRF ↔ plan sans Mosaiq | faible | §8 (clé : MU + horodatage) |
| Statistiques d'écart par lame et par CP | moyen | choix des métriques |

**Ce qu'on garde de pymedphys : les étapes 1 et 2** — le décodage TRF, qui est
le morceau le plus difficile et le plus fiable de la bibliothèque. Le reste de
la pipeline est à écrire, mais aucune brique n'est de la recherche : ce sont des
briques d'ingénierie dont le précédent publié démontre qu'elles fonctionnent.

---

## 12. Questions qui ne peuvent trouver réponse qu'en interne

Aucune recherche externe ne les résoudra.

| # | Question | Pourquoi c'est bloquant |
|---|---|---|
| 1 | **Avez-vous la licence TRF Elekta ?** | Sans elle, pas de données du tout (§11.1) |
| 2 | Quelle fréquence de dumps SDD est configurée aujourd'hui ? | Détermine ce qui est déjà perdu |
| 3 | IP DNS du NSS et numéros de série de chaque Versa HD | Nécessaires à la collecte |
| 4 | Un Mosaiq (ou autre R&V) est-il interrogeable en SQL ? | Automatisation de l'appariement (§8) |
| 5 | Quel TPS produit les RT Plans ? | Monaco → `from_monaco` offrirait un 3ᵉ point de comparaison |
| 6 | Proportion VMAT / IMRT à gantry fixe | Détermine si §10 est bloquant ou marginal |
| 7 | Prototype de recherche ou outil clinique ? | Change les exigences de validation |
| 8 | Seuils d'acceptation visés (mm sur les lames, MU) ? | Définit le critère de réussite. Repère externe : ± 0,2 mm de détection systématique dans le précédent académique (§11 bis) |
| 8bis | **LINACwatch est-il déjà déployé, ou envisagé, dans l'établissement ?** | Détermine l'argumentaire « construire ou acheter » (§11 bis E) |
| 9 | Position du DPO sur le statut des TRF | `field_label` + horodatage sont ré-identifiants |

---

## 13. Nouvelles questions nées de cette recherche

1. **Les positions de lames du TRF sont-elles absolues ou relatives au guide
   dynamique ?** (§7a) Les indices convergent vers « absolues », mais un champ
   très latéralisé le prouverait.
2. **Pourquoi `field_label` vaut-il `'1-2'` sur deux fichiers de contenus
   différents ?** (§8) Bug de décodage, ou sémantique mal comprise.
3. **Comment traiter les paires de lames fermées dans les statistiques ?** (§6)
   Elles dominent les valeurs extrêmes sans porter de dose. Un seuil d'ouverture
   ? Une pondération par la dose ?
4. **Quelle métrique retenir ?** Les écarts instantanés en delivery dynamique
   sont dominés par les retards transitoires aux transitions de CP. Une
   **erreur pondérée par les MU délivrées** serait physiquement plus juste qu'un
   maximum brut. À creuser dans la littérature (Kabat et al. 2019, *Med Phys* —
   étude menée sur deux Versa HD, la référence la plus proche de votre cas).
5. **Que faire du CP 0 ?** Le premier point de contrôle de la séance n'est jamais
   journalisé.
6. **Le nombre de CP journalisés est parfois inférieur au plan** (179 sur 195,
   soit 19–20 par champ contre 20–25 au plan). Certains CP passent trop vite pour
   être échantillonnés à 25 Hz. Comment les signaler ?
7. **Faut-il remonter le correctif numpy en amont ?** (§1) 17 remplacements
   mécaniques, projet actif — bonne première contribution.

---

## 14. Ce qui a changé dans le plan

| v1 du plan | Après recherche |
|---|---|
| « Le problème d'alignement est le cœur du projet, pymedphys ne le résout pas » | ⚠️ **À moitié faux** — le TRF porte un index natif de CP (§4). Reste l'interpolation intra-CP en delivery dynamique. |
| « Le TRF a des colonnes `Scaled Expected` inexploitées » | ❌ **Faux** — ce sont des colonnes `Positional Error`, et c'est mieux (§5) |
| « Vérifier les conventions de signe, risque de comparaison fausse » | ✅ **Levé** côté pymedphys, testé sur 5 variantes (§6) |
| « Installer pydicom, c'est bloquant » | ✅ Fait — mais le vrai blocage était **numpy 2** (§1) |
| « L'accès Zenodo fonctionne-t-il ? » | ⚠️ Échec SSL sur ce poste — contourné via `certifi`. Correctif permanent : lancer `/Applications/Python 3.11/Install Certificates.command` |
| Rien sur la licence TRF | 🔴 **Ajouté** — prérequis absolu (§11.1) |

---

Sources externes : [pymedphys#1046](https://github.com/pymedphys/pymedphys/issues/1046),
[pymedphys#429](https://github.com/pymedphys/pymedphys/issues/429),
[Elekta Logfile Decoding and Indexing](https://docs.pymedphys.com/en/latest/users/background/elekta-logfiles.html),
[Adding a Linac](https://docs.pymedphys.com/lib/howto/add-a-linac.html),
[liste pymedphys — Logfile query](https://groups.google.com/g/pymedphys/c/FVjvbIw0e3I),
[CHANGELOG pymedphys](https://github.com/pymedphys/pymedphys/blob/main/CHANGELOG.md),
[Kabat et al. 2019, Med Phys](https://aapm.onlinelibrary.wiley.com/doi/abs/10.1002/mp.13374).
Le reste est mesuré localement sur le jeu de test Zenodo de pymedphys.
