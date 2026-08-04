# La chaîne, en une lecture

Ce que fait l'outil, étape par étape, et les seules valeurs qui décident.
Pour le détail, [MEMOIRE.md](MEMOIRE.md) ; pour les formats,
[COMPRENDRE_LES_FICHIERS.md](COMPRENDRE_LES_FICHIERS.md).

---

## Les cinq étapes

```
archive SDD  ──1──▶  séances  ──2──▶  les fractions de CE plan  ──3──▶  DICOM délivrés  ──4──▶  écarts
   420 .trf          356           celles qui correspondent          un par fraction
```

| | Étape | Outil |
|---|---|---|
| 1 | Découper l'archive en séances | `organiser_trf.py` |
| 2 | Retrouver les séances d'un plan | `chercher_seances.py` (RTP) · intégré à l'étape 3 (DICOM) |
| 3 | Écrire un RT Plan « délivré » par fraction | `seance_vers_dicom.py` |
| 4 | Confronter plan et fractions | `comparer_dicom.py` |
| — | Inspecter un plan quelconque | `visualiser_rtplan.py` |

L'étape 3 fait seule les étapes 2 et 3 quand on part d'un DICOM :

```bash
python3 exploration/seance_vers_dicom.py plan.dcm "SDD+xxx.zip" --sortie delivres/
python3 exploration/comparer_dicom.py plan.dcm delivres/
```

---

## Le lien entre le plan et le log

**Le seul axe commun est la dose cumulée.** Le plan ignore le temps, le log
ignore les points de contrôle du plan. Tout passe par les MU.

| Grandeur | RT Plan DICOM | TRF | Piège |
|---|---|---|---|
| **Dose cumulée** | `CumulativeMetersetWeight` (300A,0134), de 0 à 1 | `Step Dose/Actual Value (Mu)` | les MU absolues ne sont **pas** dans le faisceau : `BeamMeterset` (300A,0086) est dans `FractionGroupSequence` |
| **Lames** | `LeafJawPositions` (300A,011C), type `MLCX` — 160 valeurs à plat, banc 1 puis banc 2 | `Y1 Leaf n/Scaled Actual (mm)` + `Y2 Leaf n` | rien dans le tag ne dit où passe la frontière entre les bancs |
| **Mâchoires** | même séquence, type `ASYMY` — 2 valeurs | `Dlg Y1` / `Dlg Y2` | |
| **Bras** | `GantryAngle` (300A,011E) | `Step Gantry/Scaled Actual (deg)` | dérouler avant d'interpoler, sinon 360°→0° fait une chute |
| **Collimateur** | `BeamLimitingDeviceAngle` (300A,0120) | `Step Collimator/Scaled Actual (deg)` | |
| **Point de contrôle** | `ControlPointIndex` (300A,0112), **démarre à 0** | `Control point/Actual Value (None)`, **démarre à 1** | décalage de 1 |
| **Consigne du servo** | — le plan *est* la consigne | `.../Positional Error (mm)` | Y1 : `actual + erreur` · Y2 : `actual − erreur` |

Pour convertir un poids en MU :
`MU = BeamMeterset × CumulativeMetersetWeight / FinalCumulativeMetersetWeight`.

### Quatre pièges qui ne font pas planter

1. **Le compteur de MU du TRF repart de zéro** à chaque faisceau, et à chaque
   reprise après interruption. Il faut le rendre continu avant tout usage.
2. **Un point de contrôle DICOM n'écrit que ce qui change.** Le premier porte
   tout, les suivants font l'appoint : sans report de la dernière valeur connue,
   la moitié des angles d'un arc semblent absents.
3. **Unités.** DICOM toujours en mm et degrés. TRF en dixièmes (÷ 10). RTP
   Connect en centimètres.
4. **`Y2` est nié par pymedphys, mais pas ses erreurs.** D'où le signe opposé
   pour reconstituer la consigne. Se tromper donne 46 mm d'écart au lieu de 0,1.

### Apparier un point de contrôle à un échantillon

Deux méthodes, mesurées sur le plan VMAT public :

| Méthode | Écart médian au plan |
|---|---|
| **Interpolation sur les MU cumulées** ← retenue | **0,45 mm** |
| Premier échantillon où le compteur machine vaut `k+1` | 0,60 mm |
| Dernier échantillon où il vaut `k+1` | 5,00 mm |

---

## Ce qui existe déjà, ce qu'il a fallu écrire

### Repris tel quel

| Brique | Ce qu'elle apporte |
|---|---|
| **pymedphys** | Décodage TRF : noms des 350 colonnes, table des 4 versions d'encodage, `Delivery`. Et les conversions de repère `mlc_dd2dcm` / `jaw_dd2dcm` — les réécrire à la main donnait 48 mm d'erreur |
| **pydicom** | Lecture et écriture DICOM |
| **numpy**, **dash**, **plotly** | Calcul et affichage |

### Écrit ici, parce que ça n'existait pas

| Besoin | Pourquoi |
|---|---|
| **Reconstituer les séances** depuis une archive SDD | pymedphys ne fait rien de tel : il lit un TRF, isolément |
| **Apparier un plan à ses séances** | idem |
| **TRF → DICOM en VMAT** | `Delivery.to_dicom` refuse les arcs (*« Only a single gantry angle per beam is currently supported »*). D'où la **substitution** : garder la grille du plan et y injecter le mesuré, au lieu de reconstruire |
| **Lire un RTP Connect** | pas dans pymedphys |
| **Tout le visuel** | — |

Un point d'attention : pymedphys 0.41.0 utilise `np.array(x, copy=False)`, que
NumPy 2 refuse. Corrigé en mémoire par `corriger_numpy2()`, la vraie solution
étant d'épingler `numpy<2`.

---

## Les valeurs qui décident

### Regrouper les fichiers en séances

**La règle est l'état final que la machine inscrit elle-même**, pas un seuil de
durée. Mesuré : le plus petit intervalle *entre* deux séances (94 s) était plus
court que le plus grand intervalle *à l'intérieur* d'une séance (162 s). Un
seuil de temps ne pouvait donc pas trancher.

| Code | État | Effet |
|---|---|---|
| 46 | `Terminated Ok` | clôt la séance |
| 43, 44, 47 | `Interupted`, `Interupted Ready`, `Terminated Fault` | la séance continue dans le fichier suivant |

Filets de sécurité quand l'état manque : `--ecart-max` 1800 s, et cumul de MU
atteignant `--seuil-complet` 0,97 du total attendu.

### Reconnaître les séances d'un plan

Deux critères, tous deux décisifs.

| Critère | Seuil | Mesuré |
|---|---|---|
| **MU totales** | ≤ 1 % | vrai appariement : **0,03 à 0,07 %** |
| **Dessin du champ** | ≤ 3 mm | même plan : **0,40 – 0,49 mm** · autre traitement : **10,3 – 12,8 mm** |

Le dessin du champ se prend en **cinq sondages** à 15, 35, 55, 75 et 92 % des MU
cumulées, sur les 160 lames, en essayant les **quatre conventions** d'ordre des
bancs et de numérotation et en gardant la meilleure.

**Ne sont pas comparés** : le nom de la machine — le log la désigne par son
numéro de série, le plan par le nom du TPS — ni le nom de champ, que deux
systèmes ne nomment pas pareil.

### Lire les écarts obtenus

| Comparaison | Sur les données publiques |
|---|---|
| Plan contre chaque fraction | 0,20 – 0,22 mm médian |
| **Fractions entre elles** | **0,07 – 0,10 mm** |

Les fractions se ressemblent deux fois plus entre elles qu'elles ne ressemblent
au plan : l'essentiel de l'écart au plan est **systématique**, pas accidentel.
C'est pourquoi la seconde ligne est le meilleur garde-fou.

Pour un pic isolé, deux questions suffisent :

- **revient-il à chaque fraction ?** sinon, il s'est passé quelque chose ce jour-là ;
- **combien de MU pour combien de course de lames ?** au-delà de ~10 mm/MU pour
  ~1 MU, le recalage sur l'axe des MU est mal conditionné : l'écart y est
  surtout méthodologique, et de faible poids dosimétrique puisque le segment ne
  délivre presque rien.

---

## La réserve de fond

Tout ceci relève de la **cohérence interne** : les méthodes confrontées lisent
le même capteur. Si l'encodeur de la machine dérive, elles dérivent ensemble
sans que rien ne le signale. Le contrôle par mesure indépendante garde donc sa
raison d'être.
