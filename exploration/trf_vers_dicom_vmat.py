"""Preuve de concept : TRF → RT Plan DICOM en VMAT, par substitution.

`pymedphys.Delivery.to_dicom` échoue en VMAT ("Only a single gantry angle per
beam is currently supported") parce qu'il segmente les faisceaux par angle de
gantry. On contourne en gardant la grille de points de contrôle du plan et en y
substituant les valeurs mesurées, interpolées sur l'axe des MU cumulées.

C'est l'architecture décrite dans RESULTATS_RECHERCHE.md §11 ter, et celle du
précédent publié (PMC10018669).

Les conversions de repère réutilisent les fonctions de pymedphys plutôt que d'en
réécrire : c'est le seul moyen de garantir que ce qu'on écrit est bien ce que
`Delivery.from_dicom` relira.

    python3 exploration/trf_vers_dicom_vmat.py
"""

import copy
import glob

import numpy as np
import pydicom
import pymedphys
from pymedphys import Delivery

PLAN = "data/vmat_pymedphys/979797_VMAT.dcm"
SORTIE = "data/vmat_pymedphys/979797_VMAT_delivre.dcm"


def corriger_numpy2():
    """Corrige deux défauts de pymedphys 0.41.0 dans les conversions de repère.

    1. `np.array(x, copy=False)` est refusé par NumPy 2.
    2. `.astype(str)` sur des float64 produit des chaînes de 17 à 21 caractères
       (« -49.599999999999994 »), alors que la VR ``DS`` du standard DICOM en
       autorise 16 au maximum. Le fichier produit est non conforme. On arrondit
       donc à 4 décimales, largement au-delà de la résolution machine.
    """
    import pymedphys._dicom.delivery.utilities as utilities

    def formater(valeurs):
        return [f"{v:.4f}" for v in valeurs]

    def mlc_dd2dcm(mlc):
        mlc = np.asarray(mlc)
        return [
            formater(np.hstack([-cp[-1::-1, 1], cp[-1::-1, 0]])) for cp in mlc
        ]

    def jaw_dd2dcm(jaw):
        jaw = np.asarray(jaw)
        return [formater([-cote[1], cote[0]]) for cote in jaw]

    utilities.mlc_dd2dcm = mlc_dd2dcm
    utilities.jaw_dd2dcm = jaw_dd2dcm

    return utilities


def trf_apparie(plan_meterset):
    """Retient le TRF dont les MU totales collent au meterset du plan."""
    candidats = []
    for chemin in sorted(glob.glob("data/vmat_pymedphys/trf/*1-1_VMAT.trf")):
        _, table = pymedphys.trf.read(chemin)
        mu = table["Step Dose/Actual Value (Mu)"].max()
        candidats.append((abs(mu - plan_meterset), chemin))

    ecart, chemin = min(candidats)
    print(f"  TRF retenu : {chemin.split('/')[-1]}  (écart {ecart:.2f} MU)")

    return chemin


def mu_cibles_du_plan(beam, meterset):
    """MU cumulées de chaque point de contrôle du plan."""
    final = float(beam.FinalCumulativeMetersetWeight)
    poids = np.array(
        [float(cp.CumulativeMetersetWeight) for cp in beam.ControlPointSequence]
    )

    return meterset * poids / final


def interpoler_sur_mu(delivery, mu_cibles):
    """Ramène un Delivery mesuré sur la grille de MU du plan."""
    mu_mesure = np.asarray(delivery.monitor_units)
    mlc = np.asarray(delivery.mlc)
    jaw = np.asarray(delivery.jaw)

    n_cp, n_lames = len(mu_cibles), mlc.shape[1]

    mlc_interpole = np.empty((n_cp, n_lames, 2))
    for lame in range(n_lames):
        for banc in range(2):
            mlc_interpole[:, lame, banc] = np.interp(
                mu_cibles, mu_mesure, mlc[:, lame, banc]
            )

    jaw_interpole = np.empty((n_cp, 2))
    for cote in range(2):
        jaw_interpole[:, cote] = np.interp(mu_cibles, mu_mesure, jaw[:, cote])

    # l'angle est déroulé avant interpolation, sinon le passage ±180° casse tout
    gantry_deroule = np.degrees(np.unwrap(np.radians(np.asarray(delivery.gantry))))
    gantry_interpole = np.mod(
        np.interp(mu_cibles, mu_mesure, gantry_deroule), 360.0
    )

    return mlc_interpole, jaw_interpole, gantry_interpole


def main():
    utilities = corriger_numpy2()

    plan = pydicom.dcmread(PLAN, force=True)
    beam = plan.BeamSequence[0]
    meterset = float(
        plan.FractionGroupSequence[0].ReferencedBeamSequence[0].BeamMeterset
    )
    print(f"Plan  : {beam.NumberOfControlPoints} points de contrôle, "
          f"{meterset:.1f} MU")

    delivery = Delivery.from_trf(trf_apparie(meterset))
    mu_cibles = mu_cibles_du_plan(beam, meterset)
    print(f"  TRF   : {len(delivery.monitor_units)} échantillons à 25 Hz")
    print(f"  cible : {len(mu_cibles)} points de contrôle\n")

    mlc, jaw, gantry = interpoler_sur_mu(delivery, mu_cibles)

    # conversions de repère : celles de pymedphys, pas les nôtres
    mlc_dicom = utilities.mlc_dd2dcm(mlc)
    jaw_dicom = utilities.jaw_dd2dcm(jaw)

    delivre = copy.deepcopy(plan)
    for index, cp in enumerate(delivre.BeamSequence[0].ControlPointSequence):
        for position in cp.BeamLimitingDevicePositionSequence:
            if position.RTBeamLimitingDeviceType == "MLCX":
                position.LeafJawPositions = mlc_dicom[index]
            elif position.RTBeamLimitingDeviceType == "ASYMY":
                position.LeafJawPositions = jaw_dicom[index]

        if hasattr(cp, "GantryAngle"):
            cp.GantryAngle = f"{gantry[index]:.4f}"

    delivre.RTPlanLabel = (plan.RTPlanLabel or "")[:10] + "_DEL"
    delivre.save_as(SORTIE, enforce_file_format=False)
    print(f"Écrit : {SORTIE}")

    # --- vérification par relecture ---
    relu = pydicom.dcmread(SORTIE, force=True)
    depuis_dicom = Delivery.from_dicom(relu, 1)
    origine = Delivery.from_dicom(plan, 1)

    print(f"\nRelecture : {relu.BeamSequence[0].NumberOfControlPoints} CP, "
          f"Delivery.from_dicom ✅ {depuis_dicom.monitor_units[-1]:.1f} MU")

    # aller-retour : ce qu'on a écrit est-il ce qu'on relit ?
    boucle = np.abs(np.asarray(depuis_dicom.mlc) - mlc)
    print(f"  aller-retour des lames : max {boucle.max():.4f} mm "
          f"({'✅ exact' if boucle.max() < 0.01 else '❌ dérive'})")

    # --- écart prévu / délivré ---
    mlc_origine = np.asarray(origine.mlc)
    ecart = np.abs(mlc - mlc_origine)

    # L'ouverture d'une paire est la SOMME des deux bancs, pas leur différence :
    # dans la convention `Delivery`, banc 0 vaut X2 et banc 1 vaut -X1 (vérifié
    # au millième contre le DICOM brut). Une paire garée est à environ 3,6 mm.
    ouvert = (mlc_origine[:, :, 0] + mlc_origine[:, :, 1]) > 5.0
    ecart_gantry = np.abs(
        np.asarray(depuis_dicom.gantry) - np.asarray(origine.gantry)
    )

    print("\nÉcart prévu / délivré, sur la grille du plan :")
    print(f"  lames, toutes        : médiane {np.median(ecart):.2f} mm, "
          f"p95 {np.percentile(ecart, 95):.2f} mm, max {ecart.max():.2f} mm")
    print(f"  lames, dans le champ : médiane {np.median(ecart[ouvert]):.2f} mm, "
          f"p95 {np.percentile(ecart[ouvert], 95):.2f} mm")
    print(f"  gantry               : médiane {np.median(ecart_gantry):.2f}°, "
          f"max {ecart_gantry.max():.2f}°")


if __name__ == "__main__":
    main()
