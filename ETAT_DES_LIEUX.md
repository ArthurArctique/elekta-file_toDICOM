# Où on en est : séparer les séances, lier les points de contrôle

Synthèse destinée à être critiquée. Chaque affirmation porte son niveau de
preuve. Le détail des mesures est dans [RESULTATS_RECHERCHE.md](RESULTATS_RECHERCHE.md),
le format des fichiers dans [COMPRENDRE_LES_FICHIERS.md](COMPRENDRE_LES_FICHIERS.md).

---

## 1. Séparer les séances

### Le problème

Une séance interrompue puis reprise ne s'écrit **pas** comme un fichier avec un
trou : la machine produit **plusieurs fichiers distincts**, chacun avec son
compteur de MU repartant de zéro. Rien dans l'en-tête ne dit qu'ils vont
ensemble.

### Pourquoi le temps ne suffit pas

Mesuré sur la séance de référence :

- plus petit intervalle **entre** deux séances : **94 s**
- plus grand intervalle **à l'intérieur** d'une séance : **162 s**

Les deux plages se chevauchent. Aucun seuil de durée ne peut les séparer.

### La règle retenue

Sur une semaine, un même champ est délivré plusieurs fois. Le **total de MU le
plus élevé observé** pour ce champ sert de référence. Une séance reste ouverte
tant que son cumul ne l'atteint pas.

Une nouvelle séance s'ouvre si la machine change, si le champ change, si
l'intervalle dépasse 30 min, ou si la séance précédente avait atteint sa
référence.

### Ce que ça vaut

✅ Validé sur la seule vérité terrain disponible : **3 séances reconstituées
correctement**, dont une interrompue trois fois et répartie sur 4 fichiers.

⚠️ **C'est une validation mince.** Un site, une machine, six fichiers, un seul
cas d'interruption. Rien ne garantit la tenue sur un vrai lot hebdomadaire.

⚠️ **La référence ne vaut que ce que vaut le lot.** Un champ qui n'apparaît
qu'en fragments — traitement commencé en fin de semaine — donnera une référence
sous-estimée et un découpage faux. La colonne `completude` sert à repérer ces cas.

### Ce qui rendrait la chose solide

Le `BeamMeterset` du RT Plan donne le total attendu **sans estimation**. Le jour
où les plans seront disponibles, cette heuristique devient inutile. C'est la
principale raison d'accélérer leur obtention.

---

## 2. Lier les points de contrôle aux logs

### Deux fichiers, deux axes incompatibles

| | Plan DICOM | Log TRF |
|---|---|---|
| Axe | poids relatif 0 → 1 | temps, 25 Hz |
| Granularité | ~111 points de contrôle | des milliers d'échantillons |
| Notion de temps | **aucune** | horodatage à la milliseconde |

Le seul axe commun est la **dose cumulée**. Le plan l'exprime en poids relatif,
le log en MU absolues.

### La conversion

```
MU(cp) = BeamMeterset × CumulativeMetersetWeight(cp) / FinalCumulativeMetersetWeight
```

Pour chaque point de contrôle du plan, on calcule sa MU cible, puis on lit le log
à cette valeur en interpolant linéairement entre les deux échantillons qui
l'encadrent.

### 🔴 Le piège qui fait tout basculer

Le TRF contient une colonne `Control point` — la machine écrit elle-même à quel
point de contrôle elle se trouve. Mais **son compteur démarre à 1, celui du plan
à 0**.

| Décalage appliqué | Écart des lames |
|---|---|
| **−1 · le bon** | **0,08 mm** |
| 0 · l'erreur naturelle | **2,59 mm** |
| +1 | 4,89 mm |

Se tromper d'un cran multiplie l'erreur par trente. Le fichier produit reste
valide, les chiffres restent plausibles, **rien ne signale la faute**. À vérifier
une fois sur vos données, puis à figer.

### Comment on sait que le lien est bon

On répond deux fois à la même question, par deux chemins qui ne partagent aucune
hypothèse :

- **A** — interpolation sur les MU, depuis le plan
- **B** — le premier échantillon que **la machine** attribue à ce point

Leur désaccord borne l'incertitude réelle :

| | Valeur |
|---|---|
| Médiane | **0,076 mm** |
| p95 | 1,02 mm |
| Part sous la résolution du format (0,1 mm) | 60 % |

La médiane tombe **sous ce que le fichier sait exprimer**. Pour la majorité des
lames, la valeur au point de contrôle est aussi bonne que possible.

### La queue, et sa cause

Le compteur de MU est gradué au dixième, la machine délivre ~2,5 MU/s : **une
graduation vaut 40 ms**, soit exactement un échantillon. L'axe des MU n'apporte
aucune précision de plus que le temps. Une lame à 27 mm/s parcourt 1,1 mm pendant
cette graduation — et c'est très exactement le p95 mesuré.

**L'incertitude d'une valeur vaut la vitesse de la lame multipliée par la
granularité de l'axe.** Elle se calcule depuis le log : chaque valeur peut donc
porter sa propre barre d'erreur.

---

## 3. Ce qu'on mesure vraiment

Un écart entre plan et délivrance est **normal**. L'enjeu est de savoir ce qui,
dedans, vient de la machine et ce qui vient de la façon de mesurer.

Le TRF contient sa **propre** mesure d'erreur (consigne − réalisé), établie par
la machine, sans rien devoir à notre chaîne. Mesuré aux instants que la machine
attribue elle-même aux points de contrôle, **sans aucune interpolation** :

| Composante | Médiane | p95 | Max |
|---|---|---|---|
| **Physique** — retard du servomoteur | 0,300 mm | 1,20 mm | **2,70 mm** |
| **Total** — écart au plan | 0,300 mm | 1,20 mm | **9,90 mm** |

**Médiane et p95 identiques.** Sur l'essentiel de la distribution, l'écart
plan/délivrance **est** le retard servo, et rien d'autre. Le retard explique
**95,9 %** de l'écart cumulé.

Et ce retard suit une loi propre :

| Vitesse de la lame | Retard médian |
|---|---|
| 0 – 2 mm/s | 0,10 mm *(le plancher — donc nul)* |
| 10 – 20 | 0,50 mm |
| 30 – 60 | 1,10 mm |

Strictement monotone, proportionnel à la vitesse : c'est un asservissement, avec
une constante de temps d'environ **24 ms**. C'est une **caractéristique de la
machine**, mesurable et surveillable dans le temps — probablement l'indicateur le
plus intéressant que le log puisse fournir.

### Le budget méthodologique

| Source | Ordre de grandeur | Réductible ? |
|---|---|---|
| Quantification du format | 0,1 mm | ❌ prix du fichier |
| Granularité de l'axe des MU | jusqu'à 1 mm sur lame rapide | ⚠️ −15 % via l'horodatage |
| Interpolation entre échantillons 40 ms | second ordre | ❌ prix du fichier |
| **Décalage d'indice ±1** | **2,6 mm** | ✅ à vérifier une fois |
| Lames fermées incluses ou non | 0,18 → 0,45 mm | ✅ convention à fixer |
| Recollement d'une séance interrompue | 0,40 → 0,20 mm | ✅ remise à l'échelle |

Les trois dernières lignes sont des **décisions**, pas des fatalités.

---

## 3 bis. Premier lot réel : ce qu'il a appris

420 fichiers, une semaine, une machine, **0 illisible**. Le format d'en-tête de
votre parc est donc compatible : le problème d'Integrity 4.1.0.0
([pymedphys#1890](https://github.com/pymedphys/pymedphys/issues/1890)) ne vous
concerne pas.

**Encodage : v3 sur la totalité du lot.** C'est celui des données de référence
utilisées dans tout ce document. Rien n'est extrapolé hors du domaine vérifié, et
la branche v4 — jamais testable faute de fichier — ne vous concerne pas non plus.

### Une anomalie non élucidée, sans conséquence pratique

**62 fichiers portent un en-tête dont le total de MU vaut zéro**, dont **57
délivrent pourtant une dose bien réelle**. Répartition de leur état final :
`State Code Unknown` (44), `Terminated Ok` (13), `Terminated Fault` (5).

Hypothèses testées et **toutes écartées** :

| Piste | Verdict |
|---|---|
| Enregistrements sans traitement (imagerie, mise en place) | ❌ ils portent une dose |
| Machine ou firmware particulier | ❌ une seule machine dans le lot |
| Version d'encodage différente | ❌ v3 comme tout le reste |
| Fichier tronqué à la source | ❌ la date d'en-tête, qui marque la fin de l'écriture, est valide : l'en-tête a bien été finalisé |
| Total écrit seulement quand la délivrance conclut | ❌ 39 des 57 sont des séances complètes en un seul fichier |

Le seul lien qui subsiste est statistique : 44 de ces 62 fichiers se terminent sur
le code d'état **34**, que pymedphys nomme lui-même « State Code Unknown ». Ce que
vaut ce code demanderait la documentation d'Elekta.

**Sans conséquence pratique** : le total de MU est recalculé depuis le corps du
fichier, qui est correct, et le découpage en séances s'appuie sur l'état machine.
Ces fichiers sont exploitables comme les autres. La colonne `entete_sans_mu` de
`fichiers.csv` permet de les isoler si besoin.

### Ce qui a été corrigé au passage

Le découpage initial signalait **130 séances sur 356**. Il reposait sur une
estimation du total attendu par champ, or le nom de champ n'est pas unique par
patient : la référence était le maximum sur tous les patients confondus, donc
presque tout paraissait incomplet.

La machine écrit son propre verdict dans le **dernier code d'état** du fichier :
`Terminated Ok` clôt la séance, `Terminated Fault` ou `Interupted` annoncent que
la suite est dans le fichier suivant. Aucune estimation n'est nécessaire. Les
séances signalées sont tombées à **41**, et ce sont désormais de vrais signaux —
traitements abandonnés ou états inhabituels.

---

## 4. Ce qu'on ne sait pas

À opposer à tout ce qui précède.

**L'assiette de validation reste étroite.** Les mesures de qualité des §2 et §3
viennent d'un seul site, trois séances, des données de 2019-2020. Le lot réel de
420 fichiers a confirmé le décodage et le découpage, mais **aucune mesure d'écart
au plan n'a encore été faite sur vos données** — faute de RT Plan.

**Ce n'est que de la cohérence interne.** Les deux méthodes confrontées lisent le
même capteur. Si l'encodeur de la machine dérive, elles dérivent ensemble sans
que rien ne le signale. C'est exactement l'avertissement de la documentation
pymedphys, et la raison pour laquelle le contrôle par mesure indépendante garde
sa raison d'être.

**Le format n'est pas documenté.** Tout le décodage vient de la rétro-ingénierie.
Une mise à jour du logiciel machine peut le casser — ou, pire, le décaler
silencieusement.

**0,16 % des lames résistent.** Quinze lames sur 9 120 ont un résidu inexpliqué
supérieur à 3 mm, concentrées sur cinq points de contrôle (69, 70, 79, 80, 81).
Structurel, donc élucidable, mais non élucidé.

**La séparation des séances repose sur une validation à un seul cas.** Voir §1.

---

## 5. Ce qui manque pour avancer

| Il faut | Pour |
|---|---|
| **Un RT Plan** apparié à une des séances déjà découpées | Le seul verrou restant. Il permet de vérifier le décalage d'indice sur vos données et de produire la première comparaison réelle |
| Fixer les **seuils d'acceptation** | Définir ce qui est conforme (repère externe : ±0,2 mm dans la littérature) |
| *(sans objet)* Licence TRF, version d'encodage 4 | Réglés par le premier lot : les fichiers arrivent, et ils sont en v3 |

Le reste est de l'ingénierie dont le principe est validé.
