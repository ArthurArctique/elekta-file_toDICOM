"""Les TRF d'une séance mis à plat, en tables exportables.

    from noyau import table_brute, table_geometrie, ecrire_csv
    ecrire_csv("brut.csv", table_brute(seance))
    ecrire_csv("geometrie.csv", table_geometrie(seance))

Deux tables, du même matériau :

`table_brute` rend **tout** ce que les TRF de la séance contiennent — les 350 et
quelques colonnes, toutes les lignes, les fichiers recollés bout à bout. C'est
la page « Séances » sans sa troncature à 400 lignes ni son choix de colonnes.

`table_geometrie` n'en garde que de quoi rejouer le mouvement : l'horodatage
machine, l'angle de bras, les mâchoires, les 160 lames, et les MU cumulées.

Les MU cumulées ne sont pas la colonne brute
--------------------------------------------
`Step Dose/Actual Value (Mu)` **repart de zéro à chaque faisceau**, et repart
aussi à chaque fichier d'une séance interrompue. La colonne `mu_cumulees`
ajoutée ici est l'axe recollé d'`ArchiveTrf._delivrance` : écarts négatifs
neutralisés, chaque fragment décalé du total des précédents. C'est exactement
l'axe sur lequel la substitution interpole — pas un second calcul.

Ce que le TRF ne contient pas, et par quoi on le remplace, est résumé dans
`correspondances_trf_dicom.txt`, à la racine.
"""

import numpy as np
import pandas as pd

from .archive_trf import ArchiveTrf
from .conventions import COL_BRAS, COL_COLLIMATEUR, COLS_MACHOIRES, PAIRES, PAS_S

COL_MS = "ms"


def _colonnes_lames():
    """Les 160 colonnes de position de lames, bancs Y1 puis Y2."""
    return ([f"Y1 Leaf {i}/Scaled Actual (mm)" for i in range(1, PAIRES + 1)]
            + [f"Y2 Leaf {i}/Scaled Actual (mm)" for i in range(1, PAIRES + 1)])


def _temps_et_mu(seance):
    """Par fichier : (table, temps en s depuis le début de la séance).

    L'origine est prise sur les fichiers eux-mêmes, jamais sur
    `seance["debut"]` : selon l'appelant ce champ porte l'UTC ou l'heure locale,
    alors que `f["debut"]` est toujours en UTC. Les mélanger décalait tout le
    temps du fuseau de la machine.

    Le temps inclut les interruptions — chaque fichier est replacé par son
    propre horodatage —, sans quoi une séance reprise après vingt minutes
    paraîtrait continue.
    """
    depart = min(f["debut"] for f in seance["fichiers"])
    for f in seance["fichiers"]:
        decalage = (f["debut"] - depart).total_seconds()
        temps = decalage + np.arange(len(f["table"])) * PAS_S
        yield f, np.round(temps, 3)


def table_brute(seance):
    """Tout le contenu des TRF de la séance, fichiers recollés.

    Les colonnes sont celles que la machine écrit, sous leur nom d'origine :
    c'est la table de référence, on n'y touche pas. Seules trois colonnes sont
    ajoutées en tête pour situer chaque ligne — le fichier dont elle vient, son
    rang dans ce fichier, et le temps écoulé depuis le début de la séance.

    `pandas.concat` aligne les colonnes par leur nom : si deux fichiers d'une
    même séance n'avaient pas exactement le même jeu — un v1 et un v3 mêlés, par
    exemple —, les manquantes seraient vides plutôt que décalées.
    """
    morceaux = []
    for f, temps in _temps_et_mu(seance):
        bloc = f["table"].copy()
        bloc.insert(0, "temps_s", temps)
        bloc.insert(1, "ligne_dans_fichier", np.arange(len(bloc)))
        bloc.insert(2, "fichier", f["nom"])
        morceaux.append(bloc)
    table = pd.concat(morceaux, ignore_index=True, sort=False)

    # Les MU cumulées valent la peine d'être là aussi : la colonne brute
    # repart de zéro, celle-ci non.
    mu, _ = ArchiveTrf._delivrance(seance)
    table["mu_cumulees"] = np.round(mu, 4)
    return table


def table_geometrie(seance):
    """De quoi rejouer le mouvement, et rien d'autre.

    Les positions gardent leur nom TRF plutôt qu'un nom DICOM : elles sont dans
    la convention de la machine, pas celle du plan — bancs et signes diffèrent,
    et « X1/X2 Diaphragm » alimente le `ASYMY` du DICOM, pas son `ASYMX`. Les
    renommer ici laisserait croire à une équivalence qui demande une conversion.
    """
    table = table_brute(seance)
    voulues = (["temps_s", "fichier", "ligne_dans_fichier"]
               + ([COL_MS] if COL_MS in table.columns else [])
               + [COL_BRAS, COL_COLLIMATEUR]
               + [c for c in COLS_MACHOIRES if c in table.columns]
               + [c for c in _colonnes_lames() if c in table.columns]
               + ["mu_cumulees"])
    return table[[c for c in voulues if c in table.columns]]


def ecrire_csv(chemin, table):
    """Écrit la table. Point-virgule et BOM : Excel francophone ouvre alors seul.

    Sans le BOM, Excel lit l'UTF-8 comme du latin-1 et abîme les accents ; avec
    la virgule pour séparateur, il met toute la ligne dans une seule colonne.
    """
    if not isinstance(table, pd.DataFrame):
        table = pd.DataFrame(list(table))
    table.to_csv(chemin, sep=";", index=False, encoding="utf-8-sig")
    return chemin
