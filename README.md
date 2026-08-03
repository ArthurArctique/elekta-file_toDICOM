# elekta-file_toDICOM

Exploiter les logs machine d'un accélérateur Elekta (`.trf`) pour reconstituer ce
qui a été **réellement délivré**, et le confronter au **plan de traitement**, au
niveau des points de contrôle.

Parc visé : **Versa HD**, tête Agility, encodage TRF **v3**.

---

## Bilan

### Ce qui fonctionne, sur données réelles

| | |
|---|---|
| **Décoder un TRF** | 420 fichiers d'une semaine lus, **0 illisible**. Toutes les colonnes, tous les octets attribués |
| **Reconstituer les séances** | Découpage par l'état final de la machine, y compris les séances interrompues réparties sur plusieurs fichiers |
| **Retrouver les fractions d'un plan** | Un plan RTP confronté à une semaine de logs rend l'historique de ses délivrances |
| **Lire un plan RTP Connect** | Export Mosaiq décodé, conventions détectées et non supposées |

### Ce qui est validé sur données de référence

| | |
|---|---|
| **Comparer les points de contrôle** | Écart plan/délivré mesuré : **0,18 mm** médian sur les lames exposées |
| **Produire un RT Plan « délivré »** | En VMAT, par substitution dans la grille du plan — aller-retour exact |
| **Séparer le physique du méthodologique** | Le retard du servomoteur explique **95,9 %** de l'écart observé |

### Ce qui bloque

**Une chaîne reproductible d'obtention des plans.** Un `.rtp` exporté à la main
valide la méthode, il n'industrialise rien. Les deux voies — export DICOM depuis
Mosaiq, ou accès SQL à sa base — sont aujourd'hui fermées.

C'est le seul verrou. Le reste est de l'ingénierie dont le principe est établi.

---

## Démarrage

Aucune dépendance obligatoire hors `numpy`. `pymedphys` est utilisé s'il est
présent, uniquement pour nommer les colonnes.

```bash
# 1. inventorier une semaine de logs, sans rien déplacer
python3 exploration/organiser_trf.py "SDD+xxxx.zip" --sortie rapport/

# 2. retrouver les séances correspondant à un plan
python3 exploration/chercher_seances.py plan.rtp "SDD+xxxx.zip" --detail
```

Le premier produit `fichiers.csv` et `seances.csv`. Le second rend la liste des
fractions, avec pour chacune l'écart de MU et l'écart des positions de lames.

⚠️ Les CSV portent des identifiants de champ de traitement, ré-identifiants via
le R&V. Le `.gitignore` les exclut, ainsi que tout `.trf`, `.dcm` et `.rtp`.

---

## Les outils

Dans `exploration/`, chacun documenté en tête de fichier.

### Chaîne principale

| Script | Rôle |
|---|---|
| **`organiser_trf.py`** | Inventorie une archive SDD, reconstitue les séances. `--extraire` crée un dossier par séance, `--diagnostic` détaille les fichiers atypiques, `--filtre` cible un fichier |
| **`lire_rtp.py`** | Décode un plan RTP Connect exporté par Mosaiq |
| **`chercher_seances.py`** | Retrouve les séances d'un plan dans une archive. `--methode` explique les critères, `--detail` les déroule |
| **`comparer_rtp_seance.py`** | Confronte un plan à une séance précise |
| **`visualiser_rtplan.py`** | Explore un RT Plan DICOM dans le navigateur : faisceaux, points de contrôle, ouverture du collimateur, tags bruts. Champs identifiants masqués par défaut. Demande `dash` et `plotly` |
| **`comparer_dicom.py`** | Confronte plusieurs RT Plan dans le navigateur : le plan face à ses fractions délivrées, et les fractions entre elles. Écart des lames le long de la délivrance, superposition des ouvertures |
| **`seance_vers_dicom.py`** | Retrouve seul les séances d'un plan dans une archive et écrit **un RT Plan « délivré » par fraction** : positions, angles et MU relevés par la machine, substitués dans la grille du plan. `--consigne` écrit la consigne du servo au lieu de la position atteinte |

### Démonstration et vérification

| Script | Rôle |
|---|---|
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
| **[MEMOIRE.md](MEMOIRE.md)** | **À lire en premier.** Les pièges qui produisent un résultat plausible et faux, les chiffres mesurés, et les erreurs commises pendant le projet |
| [COMPRENDRE_LES_FICHIERS.md](COMPRENDRE_LES_FICHIERS.md) | Les deux formats expliqués sans prérequis de physique médicale, sur de vrais octets |
| [ETAT_DES_LIEUX.md](ETAT_DES_LIEUX.md) | La synthèse critiquable : chaque affirmation avec son niveau de preuve |
| [RESULTATS_RECHERCHE.md](RESULTATS_RECHERCHE.md) | Le détail des mesures, corrections comprises |
| [data/README.md](data/README.md) | Provenance des jeux de données publics et leurs limites |
| `archives/` | Documents dépassés, conservés pour l'historique |

---

## Le principe, en trois points

**Le seul axe commun est la dose cumulée.** Le plan l'exprime en poids relatif de
0 à 1, le log en MU absolues. Le plan n'a pas de notion de temps, le log en a une
à la milliseconde. Toute mise en correspondance passe par les MU.

**On ne reconstruit pas le plan, on y substitue.** Produire un RT Plan à partir
du log en repartant de zéro échoue en VMAT et donne vingt fois trop de points de
contrôle. Garder la grille du plan et y injecter les valeurs mesurées, interpolées
sur l'axe des MU, contourne le problème par construction — c'est aussi ce que
fait le précédent publié le plus proche.

**Aucune convention n'est supposée.** Ordre des bancs de lames, sens de
numérotation, unités, nature des MU : tout est détecté depuis les données et
affiché. Une convention mal devinée ne fait pas planter, elle donne des chiffres
faux et vraisemblables — c'est arrivé, à 48 mm près.

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
