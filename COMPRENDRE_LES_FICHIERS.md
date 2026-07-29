# Comprendre les deux fichiers : RT Plan DICOM et log TRF

Écrit pour quelqu'un qui n'est pas physicien médical. Tous les exemples viennent
des vrais fichiers de `data/`, décodés pour l'occasion — rien n'est inventé.

---

## Partie 0 — Le minimum de contexte physique

### La machine

Un accélérateur linéaire produit un faisceau de rayons X. La tête qui l'émet est
montée sur un **bras (« gantry ») qui tourne autour du patient** allongé au
centre. Le point autour duquel tout tourne s'appelle l'**isocentre**, et on le
place dans la tumeur.

Entre la source et le patient, deux dispositifs découpent la forme du faisceau :

- Le **collimateur multilames (MLC)** — sur un Elekta Agility, **160 lames** de
  tungstène organisées en **2 bancs de 80 lames qui se font face**. Chaque paire
  peut s'ouvrir plus ou moins. En les bougeant, on dessine n'importe quelle forme,
  et on la change en continu pendant l'irradiation.
- Les **diaphragmes (« mâchoires »)** — deux gros blocs qui font une découpe
  grossière, en renfort.

### L'unité qui compte : la MU

Le temps n'est **pas** la grandeur de référence. Ce qui compte, c'est l'**unité
moniteur (MU)** : une chambre d'ionisation dans la tête de la machine mesure le
rayonnement réellement émis, et l'incrémente. La MU est proportionnelle à la dose
délivrée.

> ⚠️ **C'est le point le plus important de tout ce document.** Une séance n'est
> pas décrite « en secondes » mais « en MU ». Si le débit de la machine varie,
> la séance dure plus ou moins longtemps, mais la dose reste la même. C'est
> pourquoi tout notre travail d'alignement se fait sur l'axe des **MU cumulées**,
> jamais sur le temps.

### Ce que la machine compte vraiment

Dans la tête de l'accélérateur, après la cible, le faisceau traverse une
**chambre d'ionisation de transmission** — deux électrodes entre lesquelles le
rayonnement arrache des charges. Le courant recueilli est proportionnel au
rayonnement émis, et la machine l'intègre. C'est ce compteur qu'on appelle
l'unité moniteur.

Deux conséquences :

**La MU n'est pas une unité physique universelle**, c'est une grandeur
d'étalonnage. Chaque accélérateur est réglé pour qu'une MU corresponde à une
dose donnée dans des conditions de référence — typiquement 1 cGy, la référence
exacte variant selon les conventions du service. Vérifier cet étalonnage est
d'ailleurs un contrôle qualité en soi.

**La machine s'arrête sur ce compteur, pas sur un chronomètre.** Elle délivre
jusqu'à atteindre le nombre de MU demandé. Si le débit faiblit, la séance dure
plus longtemps ; la dose, elle, ne change pas. C'est aussi pour ça qu'une
délivrance interrompue reprend là où le compteur s'était arrêté.

### Le point de contrôle

Un plan de traitement est une liste d'**étapes intermédiaires**, appelées points
de contrôle. Chacun dit en substance :

> « Quand le compteur aura atteint 41 % du total, le bras devra être à 213°,
> et les 160 lames à ces positions-là. »

Entre deux points de contrôle, la machine interpole : elle bouge continûment.

### Nos deux fichiers

| | Le **RT Plan** (DICOM) | Le **log** (TRF) |
|---|---|---|
| C'est quoi | Ce qu'il **faut** faire | Ce qui **s'est** passé |
| Produit par | Le logiciel de planification (ici Monaco) | L'accélérateur lui-même |
| Rythme | ~111 points de contrôle | **25 mesures par seconde** |
| Analogie | L'itinéraire | La trace GPS |

Notre projet consiste à superposer les deux.

---

## Partie 1 — Le RT Plan DICOM

### Ce qu'est DICOM

DICOM est le format universel de l'imagerie médicale. Un fichier DICOM est
essentiellement un **dictionnaire clé → valeur**, où chaque clé est un couple de
nombres hexadécimaux appelé **tag**, de la forme `(groupe, élément)`.

Par exemple `(300A,0086)` désigne toujours et partout « BeamMeterset ». Les tags
du groupe `300A` sont ceux de la radiothérapie.

Certaines valeurs sont des listes d'autres dictionnaires — on les appelle des
**séquences** (`...Sequence`). C'est ce qui donne au fichier sa structure en
arbre.

### L'arbre, sur notre vrai plan VMAT

```
RT PLAN  ·  data/vmat_pymedphys/979797_VMAT.dcm
│
├── (300A,0002) RTPlanLabel ............... 'AVMAT'
├── (0008,1090) ManufacturerModelName ..... 'Monaco'
│
├── (300A,0070) FractionGroupSequence ..... « la prescription »
│   └── groupe n° 1
│       ├── (300A,0078) NumberOfFractionsPlanned .. 2
│       └── (300C,0004) ReferencedBeamSequence
│           └── ├── (300C,0006) ReferencedBeamNumber .. 1   ← clé de jointure
│               └── (300A,0086) BeamMeterset ......... 426.710052   ← LES MU
│
└── (300A,00B0) BeamSequence ............... « les faisceaux »
    └── faisceau n° 1
        ├── (300A,00C0) BeamNumber ................... 1   ← clé de jointure
        ├── (300A,00C4) BeamType .................... 'DYNAMIC'
        ├── (300A,00B2) TreatmentMachineName ........ '2619'
        ├── (300A,0110) NumberOfControlPoints ....... 111
        ├── (300A,010E) FinalCumulativeMetersetWeight  1.0
        │
        ├── (300A,00B6) BeamLimitingDeviceSequence ... « la description du MLC »
        │   ├── ASYMY : 1 paire            ← les mâchoires
        │   └── MLCX  : 80 paires, bornes -200..200 mm   ← les 160 lames
        │
        └── (300A,0111) ControlPointSequence ......... « les 111 étapes »
            ├── CP 0
            │   ├── (300A,0134) CumulativeMetersetWeight .. 0.000000
            │   ├── (300A,011E) GantryAngle ............... 180.0
            │   ├── (300A,011F) GantryRotationDirection ... 'CW'
            │   └── (300A,011A) BeamLimitingDevicePositionSequence
            │       ├── ASYMY → (300A,011C) LeafJawPositions = [-85.0, 100.0]
            │       └── MLCX  → (300A,011C) LeafJawPositions = 160 valeurs
            ├── CP 1   ... poids 0.007733, gantry 181.9
            └── ... jusqu'à CP 110, poids 1.000000
```

### Trois subtilités qui piègent

**1. Les MU ne sont écrites nulle part au niveau du point de contrôle.**

Chaque CP porte un **poids relatif** entre 0 et 1. Il faut le multiplier par le
total du faisceau :

```
MU(cp) = BeamMeterset × CumulativeMetersetWeight(cp) / FinalCumulativeMetersetWeight
```

Sur notre plan :

| CP | poids | MU réelles |
|---|---|---|
| 0 | 0.000000 | 0,00 |
| 1 | 0.007733 | 3,30 |
| 55 | 0.410383 | 175,11 |
| 110 | 1.000000 | 426,71 |

Conséquence pratique déjà rencontrée : si le plan a été exporté **sans calcul de
dose**, ce champ peut être absent, et tout s'effondre.

**2. Les valeurs peuvent être héritées du point de contrôle précédent.**

Pour ne pas répéter 111 fois la même chose, un CP peut **omettre** un champ : il
faut alors reprendre la dernière valeur connue. Une lecture naïve qui ferait
`cp.GantryAngle` plantera sur un `AttributeError` au milieu du fichier. C'est la
raison d'être de la fonction `get_cp_attribute_leaning_on_prior` de pymedphys.

**3. `LeafJawPositions` est une liste plate de 160 nombres.**

Pas une liste de paires. Ce sont les **80 positions du premier banc**, puis les
**80 du second**. À vous de la découper en deux, et de savoir dans quel ordre —
c'est exactement là que je me suis trompé une première fois, en inversant les
bancs : le fichier produit restait parfaitement valide, mais les écarts calculés
passaient de 0,20 mm à 48 mm. **Une erreur silencieuse.**

---

## Partie 2 — Le log TRF

### Point de départ : ce format n'est pas documenté

Elekta ne publie pas la spécification du TRF. Tout ce qui suit vient du
**reverse engineering** fait par le projet pymedphys, qui l'écrit noir sur blanc
dans son code : *« Determined through brute force reverse engineering only. Not
based upon official documentation. »*

C'est un risque à assumer : une mise à jour du firmware peut changer le format
sans préavis. Il existe d'ailleurs déjà **4 versions d'encodage** connues.

### Un fichier en deux morceaux

```
┌─────────────────────────────────────────┐
│  EN-TÊTE  (1457 octets dans notre cas)  │  ← qui, quand, et surtout :
│                                          │     la LISTE DES COLONNES
├─────────────────────────────────────────┤
│  TABLEAU  (1 923 636 octets)            │  ← 2717 lignes de 708 octets
│                                          │     une ligne = 40 ms
└─────────────────────────────────────────┘
```

### L'en-tête, octet par octet

Voici les 48 premiers octets réels de
`data/vmat_pymedphys/trf/20_04_28 21_53_39 Z 1-1_VMAT.trf` :

```
0000  13 32 30 2f 30 34 2f 32 38 20 32 31 3a 35 33 3a   |.20/04/28 21:53:|
0010  33 39 20 5a 06 2b 31 30 3a 30 30 08 31 2d 31 2f   |39 Z.+10:00.1-1/|
0020  56 4d 41 54 04 32 36 31 39 00 00 00 00 00 ab b0   |VMAT.2619.......|
0030  40 03 00 00 00 5e 01 00 00 c0 08 6f 00 81 08 64   |@....^.....o...d|
```

La première partie est du **texte préfixé par sa longueur** : un octet donne le
nombre de caractères, puis viennent les caractères.

| Offset | Octet de longueur | Contenu | Signification |
|---|---|---|---|
| 0 | `13` = 19 | `20/04/28 21:53:39 Z` | date **UTC** — c'est la **fin** de l'enregistrement (voir plus bas) |
| 20 | `06` = 6 | `+10:00` | décalage horaire local |
| 27 | `08` = 8 | `1-1/VMAT` | `label/nom` du champ |
| 36 | `04` = 4 | `2619` | **numéro de série de la machine** |

À partir de l'offset 41, on passe en binaire pur :

| Offset | Type | Valeur lue | Signification |
|---|---|---|---|
| 41 | `float64` | `4267.0` | MU totales — **en dixièmes**, soit 426,7 MU |
| 49 | `int32` | `3` | **version d'encodage** |
| 53 | `int32` | `350` | **nombre de colonnes** |
| 57 | 350 × 2 `int16` | … | **le schéma** (voir ci-dessous) |

L'en-tête fait donc 57 + 4 × 350 = **1457 octets**. Il est de longueur variable,
puisqu'il dépend des noms et du nombre de colonnes.

> ⚠️ **La date de l'en-tête marque la FIN de l'enregistrement, pas son début.**
> Vérifié en confrontant les écarts de date à l'horodatage machine (§ ci-dessous)
> sur six fichiers : l'hypothèse « fin » colle à **0,72 s** près en médiane,
> l'hypothèse « début » se trompe de **60 s**. Le fichier
> `20_04_28 21_53_39 Z` couvre donc 21:51:50 → 21:53:39, et non l'inverse.
> C'est contre-intuitif, et ça compte dès qu'on veut apparier un log à un
> horodatage de traitement.

### Le préfixe de ligne : un horodatage que pymedphys n'interprète pas

À partir de la version d'encodage 2, chaque ligne commence par **8 octets** que
pymedphys conserve tels quels sous les noms `unknown1` à `unknown4`. Lus comme un
seul entier 64 bits, ils forment un **compteur en millisecondes** :

- strictement croissant, pas médian de **40** — soit l'intervalle d'échantillonnage
- la durée qu'il donne (108,64 s) coïncide **au millième** avec le comptage de lignes
- il est **continu d'un fichier à l'autre** sur toute la machine

pymedphys ne s'en sert pas : il reconstruit le temps par `numéro de ligne × 40 ms`.
Or 11 à 42 % des intervalles réels ne font pas exactement 40 ms (ils vont de 37 à
41). L'écart reste faible — **4 ms au maximum**, sans dérive — mais une vraie
coupure d'échantillonnage serait, elle, silencieusement écrasée en un intervalle
régulier. Le [lecteur TRF](exploration/lecteur_trf.html) de ce dépôt décode ce
compteur et affiche l'écart.

### Le schéma : le fichier décrit lui-même son contenu

C'est la partie élégante. Les 700 entiers de la fin de l'en-tête forment
**350 paires de codes**. Chaque paire nomme une colonne :

| Paire | Codes lus | Clé | Colonne |
|---|---|---|---|
| 0 | (2240, 111) | `2240_111` | Control point/Actual Value |
| 1 | (2177, 100) | `2177_100` | Energy Cal Block/Set |
| 2 | (2543, 111) | `2543_111` | Linac State/Actual Value |
| 3 | (2542, 111) | `2542_111` | Actual Dose Rate (Mu/min) |
| 4 | (2238, 111) | `2238_111` | **Step Dose/Actual Value (Mu)** |
| 5 | (2162, 101) | `2162_101` | Dose/Raw value (1/64th Mu) |

Le fichier ne stocke que les **codes**. La correspondance code → nom lisible
vit dans un dictionnaire de pymedphys (`_trf/decode/config.json`), lui aussi
reconstitué par reverse engineering.

👉 **Conséquence directe pour le projet** : si vos machines journalisent un
capteur dont le code n'est pas dans ce dictionnaire, la colonne apparaîtra sous
son code brut (`1234_56`) au lieu de son nom. Ce n'est pas une erreur — c'est
juste une lacune du dictionnaire, et c'est réparable.

### Le tableau

Après l'en-tête, tout le reste est une suite de lignes de **taille fixe**, une
toutes les 40 ms (25 Hz) :

```
1 923 636 octets ÷ 2717 lignes = 708 octets par ligne

708 = 8 octets d'en-tête de ligne  +  350 colonnes × 2 octets
```

Chaque valeur est un **entier signé sur 16 bits**. Pour obtenir la grandeur
physique, on **divise par 10** :

| Colonne | Entier brut | Valeur décodée |
|---|---|---|
| Control point/Actual Value | `19` | 19 *(un index, pas de division)* |
| Linac State/Actual Value | `42` | `"Radiation On"` *(code → texte)* |
| Step Dose (Mu) | `651` | **65,1 MU** |
| Step Gantry (deg) | `-1172` | **−117,2°** |
| Y1 Leaf 40 (mm) | `569` | **56,9 mm** |
| Y2 Leaf 40 (mm) | `158` | **−15,8 mm** *(banc Y2 : ×−1 en plus)* |
| X1 Diaphragm (mm) | `1051` | **105,1 mm** |

*(ligne 500 du fichier, soit t = 20,0 s)*

La résolution est donc de **0,1 mm et 0,1°**. Le format n'en permet pas plus.

Les quatre versions d'encodage diffèrent sur ces détails : la **version 4** passe
en `int32` avec une échelle différente. D'où l'importance de connaître la version
de vos propres fichiers.

### Ce que contiennent les 350 colonnes

| Combien | Quoi |
|---|---|
| 160 | position réelle de chaque lame (`Y1 Leaf 1..80`, `Y2 Leaf 1..80`) |
| 160 | **erreur de position** de chaque lame |
| 2 + 2 | diaphragmes `X1`/`X2`, position et erreur |
| 2 + 2 | `Dlg Y1`/`Dlg Y2` — les **guides de lames**, qui coulissent pour étendre la course des lames |
| 6 + 6 | gantry, collimateur, et 4 axes de la table |
| ~10 | index de point de contrôle, état machine, MU, débit de dose, coin filtre… |

**Les colonnes « erreur de position » sont un cadeau.** Le log ne stocke pas la
consigne, mais l'**écart à la consigne**. On retrouve donc la consigne par
addition :

```
position attendue  =  position réelle  +  erreur de position
```

Vérifié sur nos données : quand une lame se déplace de −61,8 vers −49,6 mm,
l'erreur décroît de 12,2 à 7,5 mm, et **la somme reste constante à −49,6**.

Cela donne, gratuitement et sans le moindre calage temporel, une comparaison
« prévu / réalisé » toutes les 40 ms. Avec une réserve : cette consigne est celle
du **système de contrôle de la machine**, pas celle du logiciel de planification.
Elle vérifie que la machine a fait ce qu'on lui a demandé, pas que ce qu'on lui
a demandé correspondait au plan.

---

## Partie 2 bis — Les MU du plan et celles du log ne coïncident jamais tout à fait

Le plan annonce ce qu'il faut délivrer, le log enregistre ce qui l'a été. Quatre
causes les séparent, d'ampleur très différente. Mesures faites sur le couple
VMAT de référence, dont le plan annonce **426,710052 MU**.

### 1. La résolution du format — 0,1 MU

Le compteur du TRF est un entier divisé par dix. Son plus petit incrément non
nul mesuré est de **0,10 MU** : il ne peut pas faire mieux. Une séance complète
relève 426,6 contre 426,710052 au plan, soit **−0,1 MU**. C'est le plancher, et
c'est irréductible.

### 2. Le cumul sur plusieurs faisceaux — 0,1 MU par frontière

Le compteur repart à zéro à chaque faisceau, et la dernière incrémentation n'est
pas enregistrée avant la remise à zéro. Un fichier de 9 faisceaux perd donc
**−0,2 MU** au total. L'écart attendu croît avec le nombre de faisceaux, ce qui
interdit d'appliquer une tolérance fixe.

### 3. Les reprises après interruption — 0,1 MU par raccord

La séance interrompue trois fois totalise 426,4 MU, soit **−0,3 MU**. Chaque
raccord coûte environ un pas de quantification.

### 4. Une délivrance réellement incomplète — de quelques MU à tout

C'est le seul écart qui ait un sens clinique. Un fragment isolé de la séance
ci-dessus affiche 26,5 MU contre 426,7 au plan : **−400 MU**. Ce n'est pas une
erreur de mesure, c'est un traitement qui s'est arrêté.

| Cause | Ampleur mesurée | Sens |
|---|---|---|
| Résolution du format | −0,1 MU | artefact |
| Frontière de faisceau | −0,1 MU par faisceau | artefact |
| Raccord de reprise | −0,1 MU par raccord | artefact |
| Délivrance incomplète | jusqu'à tout | **réel** |

**Le log lit donc systématiquement un peu bas**, jamais haut. Un écart positif
serait suspect.

### Et « les MU du plan » sont elles-mêmes ambiguës

Un plan peut porter plusieurs prescriptions. Sur notre second jeu de test :

| Groupe de fractions | Total |
|---|---|
| 1 | 436,7 MU |
| 2 | 475,6 MU |

Le log correspondait au **groupe 2**. Comparer sans préciser le groupe n'a donc
pas de sens — c'est exactement ce que fait le paramètre `fraction_group_number`.

---

## Partie 3 — Les deux fichiers face à face

| | RT Plan DICOM | Log TRF |
|---|---|---|
| Format | Standard public, documenté | **Propriétaire, non documenté** |
| Structure | Arbre de dictionnaires | En-tête + tableau plat |
| Lisibilité | Auto-descriptif (tags normalisés) | Codes numériques + dictionnaire externe |
| Axe | **Poids de MU relatif** (0 → 1) | **Temps** (25 Hz) — mais contient aussi les MU |
| Granularité | 111 points de contrôle | 2717 échantillons |
| Précision | décimale libre | **0,1 mm / 0,1°** |
| Identité | `PatientID`, `BeamNumber`… | n° de série machine, horodatage, label de champ |

### Le pont entre les deux

C'est tout le projet, et il repose sur deux découvertes :

1. **Le TRF contient une colonne `Control point`** qui pointe directement dans
   les points de contrôle du plan. La machine écrit elle-même la correspondance.
2. **Les deux fichiers partagent l'axe des MU cumulées.** C'est le seul axe
   commun : le plan n'a pas de temps, le log n'a pas de poids relatif.

D'où la méthode retenue : pour chaque point de contrôle du plan, on calcule sa
MU cible, on interpole le log à cette MU, et on compare.

---

Pour aller plus loin : [RESULTATS_RECHERCHE.md](RESULTATS_RECHERCHE.md) (les
mesures et les pièges), [exploration/verification_chaine.py](exploration/verification_chaine.py)
et [exploration/trf_vers_dicom_vmat.py](exploration/trf_vers_dicom_vmat.py)
(le code qui produit tous les chiffres cités ici).
