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

Trier les fichiers par heure de début, puis pour chacun regarder le **dernier
code d'état** :

- **46 `Terminated Ok`** → la délivrance est allée à son terme, la séance se clôt.
- **43, 44, 47** → interrompue, la séance continue dans le fichier suivant.

Filets quand l'état manque : un écart de plus de 1800 s ouvre une nouvelle
séance, ou un cumul de MU atteignant 97 % du total attendu la clôt.

Les enregistrements sans dose (< 1 MU) sont isolés : ce ne sont pas des
traitements.

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
