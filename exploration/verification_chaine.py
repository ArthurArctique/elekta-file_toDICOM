"""Reproduit les vérifications de RESULTATS_RECHERCHE.md sur le jeu de test
public de pymedphys (téléchargé depuis Zenodo au premier lancement).

Aucune donnée patient n'est nécessaire.

    python3 exploration/verification_chaine.py

Si le téléchargement échoue en SSL sur macOS, lancer une fois
`/Applications/Python 3.11/Install Certificates.command`, ou préfixer :

    SSL_CERT_FILE=$(python3 -m certifi) python3 exploration/verification_chaine.py
"""

import warnings

import numpy as np
import pydicom
import pymedphys
from pymedphys import Delivery

warnings.filterwarnings("ignore")

NUM_LEAF_PAIRS = 80


def patch_numpy2():
    """pymedphys 0.41.0 utilise `np.array(x, copy=False)`, que NumPy 2 refuse.

    Corrige les deux fonctions qui bloquent `to_dicom`. La vraie solution est
    d'épingler numpy<2 ; ce correctif sert à démontrer que c'est le seul blocage.
    """
    import pymedphys._dicom.delivery.utilities as utilities

    def mlc_dd2dcm(mlc):
        mlc = np.asarray(mlc)
        return [
            np.hstack([-cp[-1::-1, 1], cp[-1::-1, 0]]).astype(str).tolist()
            for cp in mlc
        ]

    def angle_dd2dcm(angle):
        diff = np.append(np.diff(angle), 0)
        movement = np.empty_like(angle).astype(str)
        movement[diff > 0] = "CW"
        movement[diff < 0] = "CC"
        movement[diff == 0] = "NONE"

        converted = np.asarray(angle).astype(float).copy()
        converted[converted < 0] += 360

        return converted.astype(str).tolist(), movement

    utilities.mlc_dd2dcm = mlc_dd2dcm
    utilities.angle_dd2dcm = angle_dd2dcm


def test_data(directory, filename):
    paths = pymedphys.zip_data_paths("delivery_test_data.zip")
    matched = [
        p for p in paths if p.parent.name == directory and p.name == filename
    ]
    assert len(matched) == 1
    return str(matched[0])


def plan_mlc_for_control_point(control_point):
    """Positions MLC d'un point de contrôle, dans la convention `Delivery`."""
    positions = [
        item
        for item in control_point.BeamLimitingDevicePositionSequence
        if item.RTBeamLimitingDeviceType == "MLCX"
    ]
    if not positions:
        return None

    leaf_jaw = np.array(positions[0].LeafJawPositions, dtype=float)
    bank_a = -leaf_jaw[0:NUM_LEAF_PAIRS][::-1]
    bank_b = leaf_jaw[NUM_LEAF_PAIRS:][::-1]

    return np.array([bank_b, bank_a]).T


def split_into_beams(monitor_units):
    """Le compteur de MU repart à zéro à chaque faisceau : on découpe dessus."""
    resets = np.where(np.diff(monitor_units) < 0)[0]
    return [0] + list(resets + 1) + [len(monitor_units)]


def main():
    trf_path = test_data("original", "imrt.trf")
    plan_path = test_data("original", "rtplan.dcm")

    header, table = pymedphys.trf.read(trf_path)
    plan = pydicom.dcmread(plan_path, force=True)

    print("=== 1. Contenu du TRF ===")
    print(f"    machine {header['machine'].iloc[0]}, "
          f"champ '{header['field_name'].iloc[0]}', "
          f"{len(table)} échantillons, {len(table.columns)} colonnes")
    print(f"    pas d'échantillonnage : {np.diff(table.index[:2])[0]:.2f} s")

    print("\n=== 2. L'index de point de contrôle est natif ===")
    monitor_units = table["Step Dose/Actual Value (Mu)"].values
    control_point = table["Control point/Actual Value (None)"].values
    bounds = split_into_beams(monitor_units)

    for index, beam in enumerate(plan.BeamSequence):
        segment = control_point[bounds[index] : bounds[index + 1]]
        print(f"    champ {index + 1}: plan={beam.NumberOfControlPoints:>3} CP, "
              f"TRF={segment.min():>3}→{segment.max():>3}")

    print("\n=== 3. Attendu = Actual + Positional Error ===")
    actual = table["Y1 Leaf 40/Scaled Actual (mm)"]
    error = table["Y1 Leaf 40/Positional Error (mm)"]
    moving = table["Linac State/Actual Value (None)"] == "Move Only"
    segments = (moving != moving.shift()).cumsum()[moving]
    spreads = [
        (actual + error)[segments[segments == s].index].std()
        for s in segments.unique()[:8]
    ]
    print(f"    écart-type de (actual+error) pendant les déplacements : "
          f"{np.round(spreads, 3)}")

    print("\n=== 4. Conversion TRF → DICOM ===")
    patch_numpy2()
    delivery = Delivery.from_trf(trf_path)._filter_cps()
    created = delivery.to_dicom(plan, 1)
    print(f"    {len(created.BeamSequence)} faisceaux produits")
    for beam, original in zip(created.BeamSequence, plan.BeamSequence):
        print(f"      {beam.BeamName}: {beam.NumberOfControlPoints:>4} CP "
              f"(plan d'origine : {original.NumberOfControlPoints})")

    print("\n=== 5. Aller-retour ===")
    back = Delivery.from_dicom(created, 1)
    for name in ("monitor_units", "gantry", "collimator", "mlc", "jaw"):
        before = np.asarray(getattr(delivery, name), dtype=float)
        after = np.asarray(getattr(back, name), dtype=float)
        identical = np.all(np.around(before, 2) == np.around(after, 2))
        print(f"    {name:<14} identique : {identical}")

    print("\n=== 6. Écart au plan, point de contrôle par point de contrôle ===")
    mlc_trf = np.asarray(Delivery.from_trf(trf_path).mlc)
    metersets = [
        float(ref.BeamMeterset)
        for ref in plan.FractionGroupSequence[0].ReferencedBeamSequence
    ]

    deviations = []
    for index, beam in enumerate(plan.BeamSequence):
        start, end = bounds[index], bounds[index + 1]
        beam_mu = monitor_units[start:end]
        beam_mlc = mlc_trf[start:end]
        final_weight = float(beam.FinalCumulativeMetersetWeight)

        for control_point_item in beam.ControlPointSequence:
            expected = plan_mlc_for_control_point(control_point_item)
            if expected is None:
                continue

            weight = float(control_point_item.CumulativeMetersetWeight)
            target_mu = metersets[index] * weight / final_weight

            measured = np.empty((NUM_LEAF_PAIRS, 2))
            for leaf in range(NUM_LEAF_PAIRS):
                for bank in range(2):
                    measured[leaf, bank] = np.interp(
                        target_mu, beam_mu, beam_mlc[:, leaf, bank]
                    )

            deviations.append(np.abs(measured - expected))

    deviations = np.array(deviations)
    print(f"    {len(deviations)} points de contrôle comparés")
    print(f"    écart médian par lame : {np.median(deviations):.2f} mm")
    print(f"    p95                   : {np.percentile(deviations, 95):.2f} mm")


if __name__ == "__main__":
    main()
