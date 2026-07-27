"""Extrait de `data/vmat_pymedphys/` les données réelles affichées par
`exploration/visualiseur.html`.

Le visualiseur embarque ce JSON en dur pour être autonome. Relancer ce script
puis réinjecter le contenu de `donnees_visu.json` dans la balise
`<script id="donnees">` du HTML si les fichiers sources changent.

    python3 exploration/extraire_donnees_visu.py
"""

import json
import os
import warnings

import numpy as np
import pydicom
import pymedphys
from pymedphys import Delivery

warnings.filterwarnings("ignore")

PLAN = "data/vmat_pymedphys/979797_VMAT.dcm"
TRF = "data/vmat_pymedphys/trf/20_04_28 21_53_39 Z 1-1_VMAT.trf"
SORTIE = "exploration/donnees_visu.json"

NUM_LEAF_PAIRS = 80
PAS = 3  # sous-échantillonnage des séries temporelles, pour alléger la page
LAMES_SUIVIES = [30, 40, 50]


def arrondi(valeurs, decimales=1):
    return np.round(np.asarray(valeurs, dtype=float), decimales).tolist()


def interpoler_mlc(mu_cible, mu_mesure, mlc):
    resultat = np.empty((len(mu_cible), NUM_LEAF_PAIRS, 2))
    for lame in range(NUM_LEAF_PAIRS):
        for banc in range(2):
            resultat[:, lame, banc] = np.interp(
                mu_cible, mu_mesure, mlc[:, lame, banc]
            )

    return resultat


def main():
    plan = pydicom.dcmread(PLAN, force=True)
    beam = plan.BeamSequence[0]
    meterset = float(
        plan.FractionGroupSequence[0].ReferencedBeamSequence[0].BeamMeterset
    )
    final = float(beam.FinalCumulativeMetersetWeight)

    header, table = pymedphys.trf.read(TRF)
    mesure = Delivery.from_trf(TRF)
    attendu = Delivery.from_dicom(plan, 1)

    mu_mesure = np.asarray(mesure.monitor_units)
    mlc_mesure = np.asarray(mesure.mlc)
    jaw_mesure = np.asarray(mesure.jaw)
    gantry_mesure = np.asarray(mesure.gantry)
    temps = np.asarray(table.index)

    poids = np.array(
        [float(cp.CumulativeMetersetWeight) for cp in beam.ControlPointSequence]
    )
    mu_cible = meterset * poids / final

    # la « traduction » : le log ramené sur la grille de points de contrôle
    mlc_traduit = interpoler_mlc(mu_cible, mu_mesure, mlc_mesure)
    jaw_traduit = np.stack(
        [np.interp(mu_cible, mu_mesure, jaw_mesure[:, i]) for i in range(2)], axis=1
    )
    gantry_deroule = np.degrees(np.unwrap(np.radians(gantry_mesure)))
    gantry_traduit = ((np.interp(mu_cible, mu_mesure, gantry_deroule) + 180) % 360) - 180

    # combien d'échantillons du log tombent dans chaque point de contrôle
    bornes = np.searchsorted(mu_mesure, mu_cible)
    echantillons_par_cp = np.diff(np.concatenate([[0], bornes]))

    colonnes_erreur = [
        c for c in table.columns if c.endswith("Positional Error (mm)") and "Leaf" in c
    ]
    erreur_max = np.abs(table[colonnes_erreur].values).max(axis=1)

    # Ce que la traduction perd vraiment : on ne garde que les 111 valeurs
    # retenues, on tente de reconstituer le signal complet à partir d'elles,
    # et on mesure l'écart au signal d'origine. C'est l'erreur que la méthode
    # s'inflige à elle-même — à comparer à l'écart prévu/délivré qu'elle mesure.
    residus = []
    for lame in range(NUM_LEAF_PAIRS):
        for banc in range(2):
            retenu = np.interp(mu_cible, mu_mesure, mlc_mesure[:, lame, banc])
            reconstruit = np.interp(mu_mesure, mu_cible, retenu)
            residus.append(np.abs(reconstruit - mlc_mesure[:, lame, banc]))
    residus = np.concatenate(residus)

    instants_cp = np.interp(mu_cible, mu_mesure, temps)
    intervalles = np.diff(instants_cp)

    # Fiabilité de la valeur AU point de contrôle : on confronte deux méthodes
    # qui ne partagent aucune hypothèse.
    #   A — interpolation sur les MU cumulées, depuis le plan
    #   B — le premier échantillon que la machine attribue elle-même au point,
    #       via sa colonne `Control point` (compteur 1-based côté machine,
    #       0-based côté plan : d'où le k-1)
    compteur = table["Control point/Actual Value (None)"].values
    desaccords, vitesses = [], []
    for k in range(1, len(mu_cible) + 1):
        ou = np.where(compteur == k)[0]
        if len(ou) == 0 or k - 1 >= len(mu_cible):
            continue
        premier = ou[0]
        for lame in range(NUM_LEAF_PAIRS):
            for banc in range(2):
                a = np.interp(mu_cible[k - 1], mu_mesure, mlc_mesure[:, lame, banc])
                b = mlc_mesure[premier, lame, banc]
                voisin = min(premier + 1, len(mu_mesure) - 1)
                vitesse = abs(
                    mlc_mesure[voisin, lame, banc] - mlc_mesure[premier, lame, banc]
                ) / 0.04
                desaccords.append(abs(a - b))
                vitesses.append(vitesse)
    desaccords = np.array(desaccords)
    vitesses = np.array(vitesses)

    # le désaccord suit-il la vitesse de la lame ? on agrège par tranche
    seuils = [0, 2, 5, 10, 20, 30, 45, 200]
    tranches = []
    for i in range(len(seuils) - 1):
        dans = (vitesses >= seuils[i]) & (vitesses < seuils[i + 1])
        if dans.sum() < 20:
            continue
        tranches.append({
            "v0": seuils[i], "v1": seuils[i + 1], "n": int(dans.sum()),
            "med": round(float(np.median(desaccords[dans])), 3),
            "p95": round(float(np.percentile(desaccords[dans], 95)), 2),
        })

    mlc_attendu = np.asarray(attendu.mlc)
    jaw_attendu = np.asarray(attendu.jaw)

    donnees = {
        "meta": {
            "meterset": round(meterset, 1),
            "n_cp": len(mu_cible),
            "n_ech": len(table),
            "duree": round(float(temps[-1]), 1),
            "machine": str(header["machine"].iloc[0]),
            "date": str(header["date"].iloc[0]),
            "version": int(header["version"].iloc[0]),
            "n_col": len(table.columns),
            "pas": PAS,
            "perte_med": round(float(np.median(residus)), 3),
            "perte_p95": round(float(np.percentile(residus, 95)), 2),
            "perte_max": round(float(residus.max()), 2),
            "trou_med": round(float(np.median(intervalles)), 2),
            "trou_max": round(float(intervalles.max()), 2),
            "ponct_med": round(float(np.median(desaccords)), 3),
            "ponct_p95": round(float(np.percentile(desaccords, 95)), 2),
            "ponct_max": round(float(desaccords.max()), 2),
            "ponct_sous_res": round(float(100*(desaccords <= 0.1).mean())),
            "v_p95": round(float(np.percentile(vitesses, 95)), 1),
            "v_max": round(float(vitesses.max()), 1),
        },
        "tranches": tranches,
        "trf": {
            "t": arrondi(temps[::PAS]),
            "mu": arrondi(mu_mesure[::PAS]),
            "gantry": arrondi(gantry_mesure[::PAS]),
            "cp": [int(x) for x in table["Control point/Actual Value (None)"].values[::PAS]],
            "etat": [str(x) for x in table["Linac State/Actual Value (None)"].values[::PAS]],
            "err": arrondi(erreur_max[::PAS]),
        },
        "plan": {
            "mu": arrondi(mu_cible),
            "poids": arrondi(poids, 4),
            "gantry": arrondi(np.asarray(attendu.gantry)),
            "jaw": [arrondi(jaw_attendu[:, 0]), arrondi(jaw_attendu[:, 1])],
        },
        "trad": {
            "gantry": arrondi(gantry_traduit),
            "jaw": [arrondi(jaw_traduit[:, 0]), arrondi(jaw_traduit[:, 1])],
            "ech_par_cp": [int(x) for x in echantillons_par_cp],
        },
        "mlc": {
            "plan": [
                [arrondi(mlc_attendu[i, :, 0]), arrondi(mlc_attendu[i, :, 1])]
                for i in range(len(mu_cible))
            ],
            "trad": [
                [arrondi(mlc_traduit[i, :, 0]), arrondi(mlc_traduit[i, :, 1])]
                for i in range(len(mu_cible))
            ],
        },
        "traj": {
            str(lame): {
                "trf": [
                    arrondi(mlc_mesure[::PAS, lame, 0]),
                    arrondi(mlc_mesure[::PAS, lame, 1]),
                ],
                "trad": [
                    arrondi(mlc_traduit[:, lame, 0]),
                    arrondi(mlc_traduit[:, lame, 1]),
                ],
            }
            for lame in LAMES_SUIVIES
        },
    }

    with open(SORTIE, "w") as fichier:
        json.dump(donnees, fichier, separators=(",", ":"))

    print(f"{SORTIE} : {os.path.getsize(SORTIE) / 1024:.0f} Ko")
    print(f"  {len(mu_cible)} points de contrôle, {len(table)} échantillons")
    print(f"  échantillons par CP : min={echantillons_par_cp.min()} "
          f"médiane={int(np.median(echantillons_par_cp))} "
          f"max={echantillons_par_cp.max()}")
    print(f"  CP sans aucun échantillon : {(echantillons_par_cp == 0).sum()}")


if __name__ == "__main__":
    main()
