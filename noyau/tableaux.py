"""Le plan et le log mis à plat, en tables exportables.

    from noyau import table_plan, table_log, ecrire_csv
    ecrire_csv("plan.csv", table_plan(LecteurRtplan("plan.dcm")))
    ecrire_csv("log.csv", table_log(seance))

Deux tables, une par source, pour sortir la trajectoire et l'ouvrir ailleurs —
tableur, R, ce qu'on veut. Elles portent **les mêmes noms de colonnes** afin de
se tracer sur les mêmes axes.

Ce que « CumulativeMetersetWeight » veut dire de chaque côté
-----------------------------------------------------------
Côté plan, c'est le tag DICOM (300A,0134) : un poids sans unité qui va de 0 à
`FinalCumulativeMetersetWeight` (souvent 1,0). Il ne dit pas des MU, il dit une
**proportion** de la dose du faisceau.

Côté log, ce tag n'existe pas — la machine n'enregistre pas de poids, elle
enregistre des MU. La colonne porte donc les MU cumulées, et
`final_cumulative_meterset_weight` le total de la séance. Les deux fichiers ne
sont donc pas comparables colonne à colonne sur cette valeur brute : c'est
`fraction_delivree`, le rapport des deux, qui l'est. Elle vaut 0 au départ et 1
à l'arrivée des deux côtés, et c'est sur elle que les trajectoires se
superposent.

`mu_cumulees` fait le pont dans l'autre sens : côté plan, il est reconstitué en
multipliant le poids par le `BeamMeterset` de la fraction, ce que fait déjà
`LecteurRtplan.grille`.
"""

import csv

import numpy as np

from .archive_trf import ArchiveTrf
from .conventions import COL_BRAS, COL_CP, PAS_S

COLONNES_PLAN = ("faisceau", "point_de_controle", "angle_bras_deg",
                 "cumulative_meterset_weight", "final_cumulative_meterset_weight",
                 "fraction_delivree", "mu_cumulees")
COLONNES_LOG = ("temps_s", "fichier", "cp_machine", "angle_bras_deg",
                "cumulative_meterset_weight", "final_cumulative_meterset_weight",
                "fraction_delivree")


def table_plan(plan, fraction=1):
    """Un point de contrôle par ligne, tel que le plan les écrit.

    L'angle de bras est **reporté** d'un point au suivant : un point de
    contrôle n'écrit que ce qui change, et un plan qui omet `GantryAngle` sur
    un point inchangé laisserait sinon un trou là où l'angle est simplement
    resté le même.
    """
    par_numero = {int(f.BeamNumber): f for f in plan.ds.BeamSequence}
    lignes = []
    for bloc in plan.grille(fraction):
        faisceau = par_numero[bloc["numero"]]
        finale = float(faisceau.FinalCumulativeMetersetWeight)
        angle = None
        for index, cp in enumerate(faisceau.ControlPointSequence):
            if "GantryAngle" in cp:
                angle = float(cp.GantryAngle)
            poids = float(cp.CumulativeMetersetWeight)
            lignes.append({
                "faisceau": bloc["numero"],
                # Le numéro que porte le point, pas son rang dans la séquence :
                # ils coïncident ici mais rien ne l'impose.
                "point_de_controle": int(getattr(cp, "ControlPointIndex", index)),
                "angle_bras_deg": "" if angle is None else round(angle, 4),
                "cumulative_meterset_weight": round(poids, 6),
                "final_cumulative_meterset_weight": round(finale, 6),
                "fraction_delivree": round(poids / finale, 6) if finale else "",
                "mu_cumulees": round(float(bloc["cibles"][index]), 4),
            })
    return lignes


def table_log(seance):
    """Un échantillon de log par ligne, dans l'ordre où la machine l'a écrit.

    Les MU viennent de l'axe recollé d'`ArchiveTrf._delivrance` : remises à
    zéro entre faisceaux neutralisées, fragments décalés du total des
    précédents. C'est exactement l'axe sur lequel la substitution interpole,
    pas un second calcul.

    Le temps court depuis le début de la séance, interruptions comprises :
    chaque fichier est replacé par son propre horodatage, sans quoi une séance
    reprise après vingt minutes paraîtrait continue.

    Cette origine est prise sur les fichiers eux-mêmes, jamais sur
    `seance["debut"]` : selon l'appelant ce champ porte l'UTC ou l'heure
    locale, alors que `f["debut"]` est toujours en UTC. Les mélanger décalait
    tout le temps du fuseau de la machine — dix heures sur les données
    publiques, et des temps négatifs en tête de fichier.

    L'angle de bras est ramené dans [0, 360[ comme le fait déjà la
    substitution. Sans cela le log annonce −180° là où le plan écrit 180° : le
    même angle, et un écart apparent de 360° au tracé.
    """
    mu, _ = ArchiveTrf._delivrance(seance)
    total = float(mu[-1])
    depart_seance = min(f["debut"] for f in seance["fichiers"])

    lignes, rang = [], 0
    for f in seance["fichiers"]:
        table = f["table"]
        bras = np.mod(table[COL_BRAS].values.astype(float), 360.0)
        cps = table[COL_CP].values if COL_CP in table.columns else None
        decalage = (f["debut"] - depart_seance).total_seconds()
        for i in range(len(table)):
            cumul = float(mu[rang])
            lignes.append({
                "temps_s": round(decalage + i * PAS_S, 3),
                "fichier": f["nom"],
                "cp_machine": "" if cps is None else int(cps[i]),
                "angle_bras_deg": round(float(bras[i]), 4),
                "cumulative_meterset_weight": round(cumul, 4),
                "final_cumulative_meterset_weight": round(total, 4),
                "fraction_delivree": round(cumul / total, 6) if total else "",
            })
            rang += 1
    return lignes


def ecrire_csv(chemin, lignes, colonnes=None):
    """Écrit la table. Point-virgule et BOM : Excel francophone ouvre alors seul.

    Sans le BOM, Excel lit l'UTF-8 comme du latin-1 et abîme les accents ; avec
    la virgule pour séparateur, il met toute la ligne dans une seule colonne.
    """
    if not lignes and colonnes is None:
        raise ValueError("table vide et colonnes non précisées")
    colonnes = list(colonnes or lignes[0].keys())
    with open(chemin, "w", encoding="utf-8-sig", newline="") as fichier:
        greffier = csv.DictWriter(fichier, fieldnames=colonnes, delimiter=";")
        greffier.writeheader()
        greffier.writerows(lignes)
    return chemin
