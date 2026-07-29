# Mémoire du projet

Ce qu'il ne faut pas re-découvrir. Écrit pour que quelqu'un — vous dans six mois,
un collègue, ou une IA sans historique — puisse reprendre sans refaire le chemin.

Les autres documents : [ETAT_DES_LIEUX.md](ETAT_DES_LIEUX.md) pour la synthèse
critiquable, [RESULTATS_RECHERCHE.md](RESULTATS_RECHERCHE.md) pour les mesures
avec leur niveau de preuve, [COMPRENDRE_LES_FICHIERS.md](COMPRENDRE_LES_FICHIERS.md)
pour les formats.

---

## 1. L'objectif, en une phrase

Reconstruire depuis les logs machine Elekta (`.trf`) ce qui a réellement été
délivré, et le confronter au plan de traitement — au niveau des points de
contrôle.

État : le décodage, le découpage en séances et l'appariement plan ↔ séance
fonctionnent sur données réelles. La comparaison géométrique est validée sur
données de référence. Il manque une chaîne reproductible d'obtention des plans.

---

## 2. Les pièges qui font une erreur silencieuse

Chacun produit un résultat **plausible et faux**. C'est ce qui les rend
dangereux.

### 🔴 Le décalage d'indice des points de contrôle

Le compteur du TRF est **1-based**, le `ControlPointIndex` du plan est
**0-based**. Donc `Control point = k` ↔ point de contrôle **k−1** du plan.

| Décalage | Écart des lames |
|---|---|
| **−1 · le bon** | **0,08 mm** |
| 0 · l'erreur naturelle | **2,59 mm** |

Se tromper multiplie l'erreur par trente. Le fichier produit reste valide, les
chiffres restent plausibles. **À vérifier une fois sur vos données, puis figer.**

### 🔴 Les horodatages sont en UTC

La date de l'en-tête finit par `Z`. Le décalage local est inscrit séparément.
Mosaiq affiche l'heure locale. Sans conversion, **aucune séance n'est
retrouvable** : une délivrance de 8 h apparaît à 6 h, parfois la veille.

### 🔴 La date marque la FIN, pas le début

Écart médian de **0,72 s** avec cette hypothèse, contre **60,64 s** pour
« début ». C'est la fin de l'enregistrement, soit environ **8 s** après le
dernier rayonnement sur une délivrance normale (1 s si elle est interrompue).

### 🔴 L'ouverture d'une paire de lames est une SOMME

Dans la convention `Delivery` de pymedphys, le banc 0 vaut `X2` et le banc 1
vaut `−X1`. L'ouverture est donc `banc0 + banc1`, **pas** leur différence. Une
paire garée est à ~3,6 mm (`X1 = −1,8`, `X2 = +1,8`).

J'ai filtré « les lames dans le champ » avec une différence : le filtre retenait
27 % des lames au lieu de 52 %.

### 🔴 L'ordre des bancs et la numérotation des lames diffèrent

Entre RTP, DICOM et log, ni l'ordre des deux bancs ni le sens de numérotation ne
sont garantis. **Ne jamais présumer — essayer les quatre combinaisons et retenir
la meilleure.** Une inversion de bancs donnait 48 mm d'écart médian au lieu de
0,20.

### 🔴 Tous les compteurs repartent de zéro

Le compteur de MU **et** celui des points de contrôle repartent :

- à chaque **faisceau**, y compris à l'intérieur d'un même fichier ;
- à chaque **fichier**, sur une séance interrompue.

Il faut les rendre continus avant toute interpolation. Mesuré sur une séance
interrompue : 11 + 11 + 78 + 13 = 113 points pour un plan de 111, l'excédent
venant du recouvrement aux reprises.

---

## 3. Ce que le log contient et qu'on n'attendait pas

### La machine écrit son propre verdict

Le **dernier code d'état** du fichier dit si la délivrance est allée à son
terme :

| État final | Sens |
|---|---|
| `Terminated Ok` | la séance est close |
| `Terminated Fault`, `Interupted` | **la suite est dans le fichier suivant** |

C'est ce qui permet de découper les séances **sans aucune estimation**. Vérifié :
les trois premiers fragments d'une séance interrompue portent `Terminated Fault`,
le quatrième `Terminated Ok`.

### Le log porte sa propre référence

Il n'y a pas de colonne « valeur attendue », mais **160 colonnes
`Positional Error`**. La relation est :

```
attendu = réalisé + erreur        (banc Y1)
attendu = réalisé − erreur        (banc Y2, déjà renversé par pymedphys)
```

Vérifié : pendant qu'une lame parcourt 12 mm, la somme reste constante à
0,05–0,51 mm près.

### Le préfixe de ligne est un horodatage

Les 8 octets en tête de chaque ligne (encodage ≥ 2), que pymedphys nomme
`unknown1..4`, forment un **compteur en millisecondes**, continu sur toute la
machine. pymedphys ne l'interprète pas et reconstruit le temps par
`numéro de ligne × 40 ms` — or 11 à 42 % des intervalles ne font pas 40 ms.
L'écart reste sous 4 ms, mais une vraie coupure d'échantillonnage serait
silencieusement écrasée.

### Le nom de traitement est déjà là

Le nom du fichier TRF est **intégralement reconstruit depuis l'en-tête** :

```
20_04_28 21_53_39 Z 1-1_VMAT.trf
└─ date UTC ──────┘ └label┘└ nom du champ ┘
```

Le nom de champ est la « Description » de Monaco. Quand la convention du service
y met la dénomination du traitement, **le lien est dans le log** — colonne
`champ_nom`. Il ne relie pas à un patient.

---

## 4. Les chiffres à ne pas refaire

### Ce qu'on mesure vraiment

Aux instants que la machine attribue elle-même aux points de contrôle, **sans
interpolation** :

| Composante | Médiane | p95 | Max |
|---|---|---|---|
| **Physique** — retard du servomoteur | 0,300 mm | 1,20 mm | **2,70 mm** |
| **Total** — écart au plan | 0,300 mm | 1,20 mm | **9,90 mm** |

Médiane et p95 **identiques** : l'écart plan/délivrance *est* le retard servo.
Il explique **95,9 %** de l'écart cumulé.

Le retard suit la vitesse de la lame — 0,10 mm à l'arrêt, 1,10 mm à 30–60 mm/s —
soit une constante de temps d'environ **24 ms**. C'est une caractéristique de la
machine, mesurable et surveillable.

### La précision de la méthode

Deux méthodes indépendantes confrontées au même point de contrôle :

| | |
|---|---|
| Médiane du désaccord | **0,076 mm** |
| p95 | 1,02 mm |
| Sous la résolution du format (0,1 mm) | 60 % |

L'incertitude vaut **la vitesse de la lame × la granularité de l'axe des MU**.
Le compteur est gradué au dixième de MU, la machine délivre ~2,5 MU/s : une
graduation vaut 40 ms, pendant lesquelles une lame rapide parcourt 1,1 mm.

### L'identification d'un traitement

Le dessin du champ, sondé en cinq points :

| Cas | Écart médian |
|---|---|
| Plan face à sa propre séance | **0,4 mm** |
| Autre fraction du même plan | **0,5 mm** |
| Traitement différent | **13,8 mm** |

### Le découpage en séances

Le temps seul ne suffit pas : plus petit intervalle **entre** séances **94 s**,
plus grand intervalle **dans** une séance **162 s**. Les plages se chevauchent.

---

## 5. Mes erreurs, et ce qu'elles ont coûté

Elles reviendront si on ne les note pas.

| Erreur | Symptôme | Correction |
|---|---|---|
| Bancs de lames inversés | 48 mm au lieu de 0,20 | essayer les 4 conventions |
| Ouverture prise comme différence | mauvais sous-ensemble de lames | c'est une somme |
| Référence de MU par nom de champ | **130 séances signalées à tort** sur 356 | l'état final de la machine |
| Remise à zéro détectée par amplitude | petits faisceaux ratés, 51 → 61 divergences | la valeur d'arrivée retombe à 0 |
| MU de l'en-tête prises pour vérité | 0 MU retenu sur 57 fichiers | l'en-tête est parfois muet |
| Nom de machine en critère décisif | **toutes les séances rejetées** | ne pas comparer les machines |
| Un seul faisceau du plan comparé | **aucun dessin ne correspond** sur un plan multi-arcs | parcourir tous les faisceaux |
| Premier fragment seul lu | séances interrompues écartées | recoller avant de sonder |

Le fil commun : **presque toutes produisaient un résultat plausible**. Aucune ne
plantait.

---

## 6. L'outillage

Tout est dans `exploration/`, chaque script documenté en tête de fichier.

| Script | Rôle |
|---|---|
| `organiser_trf.py` | Inventorie une archive SDD et reconstitue les séances. `--extraire` crée un dossier par séance, `--diagnostic` détaille les fichiers atypiques |
| `lire_rtp.py` | Décode un plan RTP Connect exporté par Mosaiq |
| `chercher_seances.py` | Retrouve toutes les séances correspondant à un plan. `--methode` explique les critères, `--detail` les déroule |
| `comparer_rtp_seance.py` | Confronte un plan à une séance précise |
| `trf_vers_dicom_vmat.py` | Preuve de concept TRF → DICOM en VMAT, par substitution |
| `verification_chaine.py` | Rejoue les vérifications sur les données publiques |
| `lecteur_trf.html` | Lecteur de TRF autonome, tout dans le navigateur |
| `visualiseur.html` | Explique visuellement la traduction plan ↔ log |
| `mosaiq_lier_seances.sql` | Requête SQL, pour le jour où l'accès à la base existera |

### Les recettes

```bash
# inventorier une semaine de logs
python3 exploration/organiser_trf.py "SDD+xxxx.zip" --sortie rapport/

# retrouver les fractions d'un plan, directement dans l'archive
python3 exploration/chercher_seances.py plan.rtp "SDD+xxxx.zip" --detail
```

---

## 7. L'état de pymedphys

- **0.41.0 est la dernière version** (30/01/2025). La limitation VMAT n'est pas
  corrigée en amont.
- 🔴 **NumPy 2 casse `to_dicom`** : `np.array(x, copy=False)` lève une exception.
  pymedphys déclare `numpy>=1.26` sans borne haute, donc pip ne protège pas.
  Épingler `numpy<2`, ou remplacer 17 occurrences par `np.asarray`.
- `Delivery.from_trf` et `from_dicom` gèrent le VMAT. **`to_dicom` non** : il
  segmente les faisceaux par angle de bras, ce qui n'a pas de sens en arc.
- `to_dicom` produit **un point de contrôle par échantillon** — 538 là où le plan
  en a 25. Inexploitable pour comparer des points de contrôle.
- Ses conversions produisent des chaînes de 17 à 21 caractères là où la VR `DS`
  du standard DICOM en autorise 16 : **le fichier produit n'est pas conforme**.

**L'architecture qui marche** : garder la grille de points de contrôle du plan et
y **substituer** les valeurs mesurées, interpolées sur l'axe des MU. C'est ce que
fait le précédent publié ([PMC10018669](https://pmc.ncbi.nlm.nih.gov/articles/PMC10018669/)),
et ça contourne le blocage VMAT par construction.

---

## 8. Le contexte

### Votre parc

Versa HD, tête Agility (80 paires de lames, 5 mm, ±200 mm), **encodage TRF
v3** sur la totalité du premier lot — le même que les données de référence, donc
rien n'est extrapolé. Le problème d'en-tête d'Integrity 4.1.0.0
([pymedphys#1890](https://github.com/pymedphys/pymedphys/issues/1890)) ne vous
concerne pas : 0 fichier illisible sur 420.

### Le format TRF

Non documenté par Elekta. Tout le décodage vient de la rétro-ingénierie de
pymedphys. Une mise à jour firmware peut le casser — ou le décaler
silencieusement.

Récupération : `\\<IP_du_NSS>\Backup\TCS\SDD+*.zip`, rétention **~8 jours**,
licence Elekta requise, accès SAMBA à demander.

### L'antériorité

Le sujet est établi. **LINACwatch** (Qualiformed) fait tout le workflow
commercialement, mais **à 4 Hz via iCom** — notre route TRF offre 25 Hz.
Le précédent académique le plus proche fait exactement notre architecture, en
VMAT, sur Agility.

---

## 9. Ce qui reste ouvert

| Sujet | État |
|---|---|
| **Chaîne reproductible d'obtention des plans** | Le verrou. Export DICOM depuis Mosaiq (service réseau, pas un bouton) ou accès SQL — les deux sont bloqués aujourd'hui |
| Seuils d'acceptation | À fixer. Repère externe : ±0,2 mm de détection dans la littérature |
| 62 en-têtes sans total de MU | Cinq hypothèses testées et écartées. Sans conséquence pratique |
| Validation externe du découpage | Rien ne l'a confronté à une source indépendante |
| Encodage v4 | Implémenté, jamais vérifié. Ne vous concerne pas |

### La réserve de fond

Tout ce qui est mesuré ici relève de la **cohérence interne**. Les deux méthodes
confrontées lisent le même capteur : si l'encodeur de la machine dérive, elles
dérivent ensemble sans que rien ne le signale. C'est l'avertissement explicite de
la documentation pymedphys, et la raison pour laquelle le contrôle par mesure
indépendante garde sa raison d'être.
