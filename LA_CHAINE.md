# Tout reproduire

De quoi refaire la chaîne sans les scripts d'ici. Chaque section donne la
technique, puis où elle est implémentée si on veut la lire en entier.

---

## 1. Décoder un TRF

Binaire non documenté, rétro-conçu par pymedphys. Un en-tête, puis un tableau
d'échantillons à **40 ms** (25 Hz).

### En-tête

Quatre chaînes préfixées par leur longueur sur 1 octet, à la suite :

```
[1 octet = n][n octets ASCII]   date       ex. "20/04/28 21:53:39 Z"
                                fuseau     ex. "+10:00"
                                champ      ex. "1-1/VMAT"   (étiquette/nom)
                                machine    ex. "2619"
```

La date est en **UTC** et c'est l'heure de **fin** de l'enregistrement. Le
fuseau donne le décalage pour retomber sur l'heure locale. Le champ se coupe sur
le premier `/`.

Ensuite, à la position `p` qui suit la quatrième chaîne :

| Décalage | Type | Contenu |
|---|---|---|
| `p` | `float64` | MU totales **× 10** |
| `p+8` | `int32` | version d'encodage (1 à 4) |
| `p+12` | `int32` | nombre de colonnes |
| `p+16` | `int16 × 2N` | schéma : deux entiers par colonne |

L'en-tête finit à `p + 16 + 4 × N`. Chaque paire du schéma `(a, b)` se traduit
en nom de colonne via la table `item_part_names` de pymedphys
(`pymedphys/_trf/decode/config.json`) : la clé est `"a_b"`, par exemple
`"2238_111"` → `Step Dose/Actual Value (Mu)`.

Un en-tête qui annonce 0 MU n'est pas anormal — 62 fichiers sur 420 dans une
semaine réelle. **Le corps fait foi.**

Le hexdump annoté des 48 premiers octets réels est dans
[COMPRENDRE_LES_FICHIERS.md](COMPRENDRE_LES_FICHIERS.md#lentête-octet-par-octet).
En code, la lecture tient en quelques lignes :

```python
p = 0
for _ in range(4):                       # date, fuseau, champ, machine
    n = octets[p]
    texte = octets[p+1 : p+1+n].decode("ascii", "replace")
    p += 1 + n

mu_dixiemes = np.frombuffer(octets, np.float64, 1, p)[0]
version     = int(np.frombuffer(octets, np.int32, 1, p + 8)[0])
nb_colonnes = int(np.frombuffer(octets, np.int32, 1, p + 12)[0])
schema      = np.frombuffer(octets, np.int16, nb_colonnes * 2, p + 16)
fin_entete  = p + 16 + nb_colonnes * 4
```

Sur le fichier VMAT public : `p = 41`, version 3, 350 colonnes,
`fin_entete = 1457`.

### Attention : la date est celle de la FIN

Le fichier `20_04_28 21_53_39 Z` couvre **21:51:50 → 21:53:39**, pas l'inverse.
Vérifié en confrontant les écarts de date au compteur machine sur six fichiers :
l'hypothèse « fin » colle à 0,72 s en médiane, l'hypothèse « début » se trompe
de 60 s. C'est contre-intuitif et ça fausse tout appariement horaire.

La date est en UTC ; le fuseau donne le décalage local.

### Valider avant de faire confiance

Une archive réelle contient des fichiers abîmés. Cinq contrôles suffisent, et
chacun doit **rejeter le fichier avec un motif**, jamais le laisser passer :

| Contrôle | Motif |
|---|---|
| `len(octets) >= 64` | fichier trop court |
| la date correspond à `\d\d[/-]\d\d[/-]\d\d \d\d:\d\d:\d\d Z` | en-tête non reconnu |
| `version` est dans la table | version d'encodage inconnue |
| `0 < nb_colonnes < 5000` | nombre de colonnes aberrant |
| au moins une ligne complète dans le corps | aucune ligne de données |

Le contrôle sur la date est le plus utile : c'est la signature d'un changement
de format d'en-tête (Integrity 4.1.0.0, cf. pymedphys#1890), et il se distingue
d'un fichier simplement tronqué.

Prévoir aussi un `except` général autour du décodage : un en-tête complet suivi
d'un corps tronqué fait échouer `np.frombuffer` avant d'atteindre les contrôles.
Sur six fichiers volontairement abîmés (vide, tronqué, date écrasée, en-tête
seul, texte quelconque, queue coupée), les cinq premiers sont rejetés avec un
motif exploitable et le sixième est lu normalement, avec ses octets en trop
signalés.

### Corps

La géométrie dépend de la version :

| Version | Type | Préfixe | Échelle |
|---|---|---|---|
| 1 | `int16` | 0 | 1 |
| 2 | `int16` | 8 | 1 |
| 3 | `int16` | 8 | 1 |
| 4 | `int32` | 8 | 2 |

```
taille_ligne = échelle × nombre_de_colonnes × 2 + préfixe
nombre_de_lignes = len(corps) // taille_ligne
```

Le préfixe de 8 octets des versions ≥ 2 est un compteur en millisecondes
(`uint64` petit-boutiste). pymedphys le garde en quatre `uint16` bruts sans
l'interpréter et reconstruit le temps par « numéro de ligne × 40 ms ».

Extraire une colonne sans décoder le reste : voir le tableau comme une grille
`(lignes, taille_ligne)` d'octets, prendre la tranche
`[préfixe + i × taille : + taille]`, et la relire en `int16`/`int32`.

### Mise à l'échelle

Vérifié colonne par colonne contre pymedphys, sur un v1 et un v3 — 350/350 et
354/354 identiques au bit près :

| Facteur | Colonnes |
|---|---|
| **÷ 10** | 256 — dose, angles, diaphragmes, lames Y1, erreurs de position |
| **÷ 10 puis × −1** | 80 — `Y2 Leaf n/Scaled Actual (mm)` seulement |
| **× 1** | 12 — `Control point`, `Actual Dose Rate`, codes d'état… |

En version 4, la division par 10 s'arrête à la colonne
`Mlc Status/Actual Value (None)` ; au-delà, rien n'est mis à l'échelle.

**Le signe de Y2 ne s'applique qu'aux positions, pas aux erreurs.** D'où, pour
reconstituer la consigne du servomoteur :

```
consigne_Y1 = actual_Y1 + erreur_Y1
consigne_Y2 = actual_Y2 − erreur_Y2
```

Contrôle : pendant un `Move Only`, la consigne doit être stable. La bonne
combinaison donne un écart-type de 0,1 mm, la mauvaise de 46 mm.

### Codes d'état machine

`Linac State/Actual Value (None)` : 16 `Closed`, 34 `State Code Unknown`,
39 `Move Only`, 40 `Pause`, 41 `Intersegment`, 42 `Radiation On`,
43 `Interupted`, 44 `Interupted Ready`, 45 `Terminated Checking`,
46 `Terminated Ok`, 47 `Terminated Fault`.

### Extraire une colonne sans décoder le tableau

Un TRF d'une semaine pèse quelques gigaoctets et on n'a besoin que de trois ou
quatre colonnes sur 350. Voir le corps comme une **grille d'octets**, découper
la tranche voulue, et la relire dans le bon type :

```python
grille = np.frombuffer(corps, np.uint8, nb_lignes * taille_ligne) \
           .reshape(nb_lignes, taille_ligne)
debut   = prefixe + indice_colonne * taille_valeur
tranche = np.ascontiguousarray(grille[:, debut : debut + taille_valeur])
valeurs = tranche.view(np.int16).ravel().astype(float)     # int32 en version 4
```

Le `ascontiguousarray` est obligatoire : sans lui, `view` refuse une tranche
non contiguë.

### Vérifier qu'on a bien décodé

**C'est l'étape à ne pas sauter.** Une erreur de signe ou d'échelle ne fait pas
planter : elle donne des valeurs plausibles et fausses. La seule vérification
qui vaille est de confronter **colonne par colonne** à pymedphys, qui a vu
beaucoup plus de fichiers.

Extrait du contrôle, sur la ligne 500 du fichier VMAT :

| Colonne | Brut | Écart si ÷10 | Écart si ÷10 puis ×−1 |
|---|---|---|---|
| `Step Dose/Actual Value (Mu)` | 651 | **0,0000** | 853,2 |
| `Y1 Leaf 40/Scaled Actual (mm)` | 569 | **0,0000** | 256,4 |
| `Y2 Leaf 40/Scaled Actual (mm)` | 158 | 250,2 | **0,0000** |
| `Control point/Actual Value` | 19 | 99,0 | 121,0 |

La bonne convention se lit sans ambiguïté : l'écart tombe à zéro exactement.
`Control point` n'est bon dans aucune des deux — c'est une des 12 colonnes sans
mise à l'échelle.

Résultat du contrôle complet : **350/350 colonnes identiques au bit près** sur
un fichier v1, **354/354** sur un v3. Les 4 colonnes supplémentaires en v3 sont
les `unknown1..4` du préfixe de ligne, que pymedphys place en tête.

### Le compteur de millisecondes du préfixe

Les 8 octets de préfixe des versions ≥ 2, lus en `uint64` petit-boutiste,
donnent un compteur en millisecondes. Mesuré sur le fichier VMAT : pas médian
**40 ms**, minimum 39, maximum 41, et **11 % des intervalles ne font pas
exactement 40 ms**. La durée qu'il donne (108,64 s) coïncide avec le comptage de
lignes (108,68 s).

pymedphys ne l'utilise pas et reconstruit le temps par « numéro de ligne ×
40 ms ». C'est sans conséquence ici — l'écart ne dérive pas — mais une vraie
coupure d'échantillonnage serait silencieusement écrasée en un intervalle
régulier. Le décoder permet de la détecter.

### Repérer les remises à zéro du compteur de MU

Le compteur repart de zéro **à chaque faisceau**, y compris à l'intérieur d'un
même fichier. Ne pas les repérer revient à ne relever que le plus gros faisceau.

La signature est que la valeur **retombe à zéro**, pas que la chute soit ample :
un seuil sur l'amplitude rate les petits faisceaux.

```python
plancher = max(0.5, 0.01 * mu.max())
ruptures = np.where((np.diff(mu) < 0) & (mu[1:] <= plancher))[0]
total    = sum(mu[i] for i in ruptures) + mu[-1]
```

Sur le fichier VMAT : 0 rupture, 1 faisceau, total 426,6 MU pour 426,7 annoncées.

### Lire l'archive SDD sans rien extraire

Les logs arrivent en **System Diagnostic Dump** : des zips déposés par la
machine dans `\\<IP_NSS>\Backup\TCS\SDD+*.zip`, avec une rétention d'environ
**8 jours**. Une semaine sur un accélérateur, c'est de l'ordre de 400 fichiers
et quelques gigaoctets.

Rien n'a besoin d'être extrait. Les `.trf` sont à une profondeur variable dans
l'archive : filtrer sur le suffixe plutôt que sur un chemin attendu, et lire
chaque entrée en mémoire.

```python
with zipfile.ZipFile(chemin) as archive:
    for nom in sorted(archive.namelist()):
        if nom.lower().endswith(".trf"):
            octets = archive.read(nom)          # décompressé en RAM
```

Traiter de la même façon un zip, un dossier et un `.trf` isolé permet aux
outils d'accepter les trois sans le savoir. Chaque fichier est rendu avec
**trois choses** :

| | |
|---|---|
| `nom` | affichable, préfixé de son archive : `SDD+2020-04-28.zip::TCS/xxx.trf` |
| `origine` | le triplet `(genre, chemin, interne)` |
| `octets` | le contenu |

L'`origine` est ce qui permet de **relire** un fichier plus tard sans garder les
quelques gigaoctets en mémoire — utile quand on ne conserve que les métadonnées
au premier passage et qu'on veut revenir sur les seuls fichiers d'une séance :

```python
def relire(origine):
    genre, chemin, interne = origine
    if genre == "zip":
        with zipfile.ZipFile(chemin) as archive:
            return archive.read(interne)
    return pathlib.Path(chemin).read_bytes()
```

Garder le nom de l'archive dans le nom affiché évite une ambiguïté réelle : deux
SDD successifs contiennent des fichiers de même nom.

Une seule fonction exige un chemin sur disque, `pymedphys.trf.read` — lui passer
par un fichier temporaire :

```python
with tempfile.TemporaryDirectory() as dossier:
    f = pathlib.Path(dossier) / "x.trf"
    f.write_bytes(octets)
    entete, table = pymedphys.trf.read(str(f))
```

→ `exploration/organiser_trf.py` (Python), `exploration/lecteur_trf.html` (JS).

---

## 2. Lire le plan

| Grandeur | Tag | Où |
|---|---|---|
| MU du faisceau | `BeamMeterset` (300A,0086) | `FractionGroupSequence` → `ReferencedBeamSequence`, **pas** dans le faisceau |
| Poids cumulé | `CumulativeMetersetWeight` (300A,0134) | chaque point de contrôle, de 0 à… |
| …son maximum | `FinalCumulativeMetersetWeight` (300A,010E) | le faisceau |
| Lames | `LeafJawPositions` (300A,011C), type `MLCX` | 2N valeurs à plat : banc négatif puis banc positif |
| Mâchoires | même séquence, type `ASYMY` | 2 valeurs |
| Hauteur des lames | `LeafPositionBoundaries` | `BeamLimitingDeviceSequence` — N+1 bornes |
| Bras | `GantryAngle` (300A,011E) | point de contrôle |
| Collimateur | `BeamLimitingDeviceAngle` (300A,0120) | point de contrôle |
| Index | `ControlPointIndex` (300A,0112) | démarre à **0** |

```
MU du point = BeamMeterset × CumulativeMetersetWeight / FinalCumulativeMetersetWeight
```

Deux règles à ne pas rater :

- **Un point de contrôle n'écrit que ce qui change.** Le premier porte tout, les
  suivants font l'appoint. Il faut reporter la dernière valeur connue, sinon la
  moitié des angles d'un arc semblent absents.
- **Un plan peut avoir plusieurs groupes de fractions** (un boost après le
  traitement). Sommer toutes les MU puis multiplier par les fractions du premier
  groupe donne un total faux — 14 596 MU au lieu de 8 889 sur un plan public.
  Sommer groupe par groupe.

Unités : DICOM toujours en **mm et degrés**. TRF en dixièmes. RTP Connect en
centimètres.

→ `exploration/visualiser_rtplan.py`.

---

## 3. Recaler le log sur le plan

**Le seul axe commun est la dose cumulée.** Le plan n'a pas de notion de temps,
le log n'a pas les points de contrôle du plan.

Le compteur de MU du TRF repart de zéro à chaque faisceau et à chaque reprise.
Le rendre continu :

```
d = diff(mu) ; d[d < 0] = 0 ; mu_continu = cumsum(d)
```

Pour une séance en plusieurs fichiers, décaler chaque fichier du total des
précédents, dans l'ordre chronologique.

Côté plan, mettre les faisceaux bout à bout sur le même axe cumulé. Puis
**interpoler linéairement** les valeurs mesurées aux MU de chaque point de
contrôle.

Trois façons d'apparier, mesurées sur le plan VMAT public :

| Méthode | Écart médian au plan |
|---|---|
| **Interpolation sur les MU cumulées** | **0,45 mm** |
| Premier échantillon où `Control point == k+1` | 0,60 mm |
| Dernier échantillon où `Control point == k+1` | 5,00 mm |

Le compteur machine démarre à 1 là où le plan démarre à 0, d'où le `+1`.

**Les angles se déroulent avant d'interpoler** (`unwrap`), sinon le passage
360°→0° d'un arc produit une valeur aberrante au milieu de la plage ; on remet
modulo 360 après.

---

## 4. Regrouper les fichiers en séances

**La règle est l'état final que la machine inscrit elle-même**, pas un seuil de
durée : mesuré, le plus petit intervalle *entre* deux séances (94 s) était plus
court que le plus grand intervalle *à l'intérieur* d'une séance (162 s).

### Ce qu'il faut avoir relevé par fichier

| | Comment |
|---|---|
| `machine`, `champ_nom` | chaînes de l'en-tête ; le champ se coupe sur le `/` |
| `fin_utc` | la date de l'en-tête **telle quelle** — c'est déjà la fin |
| `debut_utc` | `fin_utc − nombre_de_lignes × 0,04 s` |
| `debut_local`, `fin_local` | + le décalage du fuseau, `"+10:00"` → 10 h |
| `mu` | somme des segments entre remises à zéro (§1) |
| `etat_final` | dernière valeur de `Linac State/Actual Value` |
| `issue` | `terminee` si 46, `interrompue` si 43/44/47, sinon `None` |
| `delivrance` | `mu >= 1` — en deçà ce n'est pas un traitement |

La conversion en heure locale n'est pas cosmétique : **Mosaiq affiche l'heure
locale**. Sans elle, aucune séance n'est retrouvable — on cherche des séances à
6 h du matin qui n'existent pas.

### Le total de référence

Avant de chaîner, établir pour chaque couple `(machine, champ_nom)` le **plus
grand cumul de MU observé dans tout le lot**. C'est l'estimation de ce que le
champ délivre quand il va au bout, et elle sert de repli quand l'état machine
manque. La tirer du lot plutôt que d'un plan évite d'avoir besoin du plan à ce
stade.

### Le chaînage

Trier par `(machine, debut_utc)`, puis pour chaque fichier décider s'il **ouvre**
une séance ou **prolonge** la courante. Dans cet ordre :

```
si le fichier ne délivre pas (< 1 MU) :
    l'isoler dans sa propre entrée, et NE PAS toucher à la séance courante
    → un cliché d'imagerie glissé entre deux fragments ne doit pas couper la séance

sinon, on ouvre une nouvelle séance si :
    aucune séance courante                          « premier fichier »
    machine différente                              « machine différente »
    champ différent                                 « champ différent »
    debut_utc − fin_utc(courante) > 1800 s          « écart de N min »
    la courante est `terminee`                      « menée à son terme »
    état absent ET cumul ≥ 0,97 × référence         « cumul de MU atteint »

sinon, prolonger : ajouter le fichier, cumuler les MU, avancer fin_utc,
et reprendre l'état final et l'issue du nouveau fichier
```

Trois points qui comptent :

- **L'ordre des tests.** L'écart de temps est examiné *avant* l'état machine :
  deux délivrances du même champ à trois heures d'intervalle sont deux séances,
  même si la première a été interrompue.
- **Les enregistrements sans dose sont transparents.** Les isoler *et* laisser la
  séance courante intacte est la seule façon de ne pas couper une séance
  interrompue par une imagerie.
- **Le cumul de MU n'est qu'un repli**, employé seulement quand `etat_final` est
  absent. C'est l'état machine qui décide quand il est là.

À la fin, noter la position de chaque fichier dans sa séance — seul, premier,
milieu, dernier : un fichier destiné à être poursuivi ne se comporte pas comme
celui qui conclut, et c'est utile pour diagnostiquer.

### Vérification

Sur les 9 TRF publics : 6 séances, dont une reconstituée à partir de 4
fragments, et 3 séances du même plan VMAT à 426,4–426,6 MU chacune. Sur une
semaine réelle : 420 fichiers, 0 illisible, 356 séances, 41 signalées à
vérifier — contre 130 avec une première version qui estimait le total de
référence par le nom de champ.

→ `exploration/organiser_trf.py`.

---

## 5. Reconnaître les séances d'un plan

Deux critères, tous deux décisifs.

**MU totales**, tolérance 1 %. Un vrai appariement tombe à 0,03–0,07 %.

**Dessin du champ.** Sonder les deux côtés en cinq points de la délivrance —
**15, 35, 55, 75 et 92 % des MU cumulées** — et confronter les 160 lames.
Comme l'ordre des bancs et le sens de numérotation diffèrent d'un système à
l'autre, essayer les **quatre combinaisons** (bancs échangés ou non × lames
inversées ou non) et garder la meilleure. S'être trompé là-dessus donnait 48 mm
au lieu de 0,2.

| | Écart médian |
|---|---|
| Le plan face à sa propre séance | **0,40 – 0,49 mm** |
| Face à un autre traitement | **10,3 – 12,8 mm** |

Seuil à 3 mm, largement entre les deux.

**Ne pas comparer** le nom de la machine — le log la désigne par son numéro de
série, le plan par le nom du TPS — ni le nom de champ, que deux systèmes ne
nomment pas pareil.

→ `exploration/comparer_rtp_seance.py`, `exploration/chercher_seances.py`.

---

## 6. Écrire le plan délivré

**Substituer, pas reconstruire.** Repartir des logs pour fabriquer un plan de
zéro échoue en VMAT : `Delivery.to_dicom` de pymedphys segmente par angle de
bras et refuse un arc (*« Only a single gantry angle per beam is currently
supported »*), et on obtiendrait vingt fois trop de points de contrôle (2 717
échantillons pour 111 points). On garde donc la grille du plan et on y injecte
le mesuré, interpolé sur l'axe des MU.

Les conversions de repère se reprennent de pymedphys plutôt que de se
réécrire — les refaire à la main donnait 48 mm d'erreur :

```python
mlc_dicom = [np.hstack([-cp[-1::-1, 1], cp[-1::-1, 0]]) for cp in mlc]
jaw_dicom = [[-cote[1], cote[0]] for cote in jaw]
```

Deux détails qui produisent un fichier non conforme si on les rate :

- La VR `DS` du standard n'autorise que **16 caractères**. `str(float64)` en
  produit jusqu'à 21 (`-49.599999999999994`) : formater en `%.4f`.
- pymedphys 0.41.0 utilise `np.array(x, copy=False)`, que **NumPy 2 refuse**.
  Épingler `numpy<2`, ou corriger les deux fonctions en mémoire.

Écrire les MU réellement délivrées dans `BeamMeterset`, pas ailleurs.

**Le fichier produit ne doit jamais pouvoir être traité.** Lui donner un
`SOPInstanceUID` et un `SeriesInstanceUID` **neufs** — sans quoi il peut écraser
le plan d'origine dans un PACS ou un R&V —, `ApprovalStatus = UNAPPROVED`, et un
libellé distinct. Garder le `StudyInstanceUID` pour qu'il reste rattaché au bon
patient.

→ `exploration/seance_vers_dicom.py`.

---

## 7. Lire les écarts

| Comparaison | Sur les données publiques |
|---|---|
| Plan contre chaque fraction | 0,20 – 0,22 mm médian |
| **Fractions entre elles** | **0,07 – 0,10 mm** |

Les fractions se ressemblent deux fois plus entre elles qu'elles ne ressemblent
au plan : l'essentiel de l'écart au plan est **systématique**. C'est pourquoi la
comparaison des fractions entre elles est le meilleur garde-fou.

Exclure les lames garées des statistiques : immobiles, elles sont parfaitement
conformes et tirent toutes les médianes vers zéro. Une paire est ouverte si
`banc_positif − banc_négatif > 5 mm` en repère DICOM — attention, dans la
convention `Delivery` de pymedphys les deux bancs comptent dans le même sens et
l'ouverture est leur **somme**.

Pour un pic isolé, deux questions :

- **revient-il à chaque fraction ?** sinon, il s'est passé quelque chose ce jour-là ;
- **combien de MU pour combien de course de lames ?** Au-delà de ~10 mm/MU pour
  ~1 MU, le recalage sur l'axe des MU est mal conditionné et l'écart y est
  surtout méthodologique. Mesuré : corrélation de 0,64 entre l'écart et les
  mm/MU, de −0,44 avec les MU du segment.

→ `exploration/comparer_dicom.py`.

---

## La réserve de fond

Tout ceci relève de la **cohérence interne** : les méthodes confrontées lisent
le même capteur. Si l'encodeur de la machine dérive, elles dérivent ensemble
sans que rien ne le signale. Le contrôle par mesure indépendante garde sa raison
d'être.
