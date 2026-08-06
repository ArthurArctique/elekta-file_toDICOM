"""Les conventions du format, en un seul endroit.

Noms de colonnes du TRF, géométrie du MLC visé, seuils de découpage, et les deux
conversions du repère `Delivery` de pymedphys vers celui de DICOM.

Ces valeurs sont partagées par tous les modules : les dupliquer serait le moyen
le plus sûr de les voir diverger.
"""

import numpy as np

COL_MU = "Step Dose/Actual Value (Mu)"
COL_ETAT = "Linac State/Actual Value (None)"
COL_BRAS = "Step Gantry/Scaled Actual (deg)"
COL_COLLIMATEUR = "Step Collimator/Scaled Actual (deg)"
COLS_MACHOIRES = ("X1 Diaphragm/Scaled Actual (mm)", "X2 Diaphragm/Scaled Actual (mm)")
PAIRES = 80
SONDAGES = (0.15, 0.35, 0.55, 0.75, 0.92)  # où sonder le dessin du champ, en fraction de MU
ECART_MAX_S = 1800          # au-delà, deux fichiers sont deux séances
PAS_S = 0.04                # 25 Hz


# La SOP Class d'un RT Plan. Ici plutôt que dans une classe : le lecteur la
# vérifie à l'entrée, l'écrivain la réinscrit à la sortie.
CLASSE_RT_PLAN = "1.2.840.10008.5.1.4.1.1.481.5"

# --- conversions Delivery -> DICOM, reprises de pymedphys ---
# Réécrites pour deux défauts de la 0.41.0 : np.array(copy=False) refusé par
# NumPy 2, et str(float64) qui dépasse les 16 caractères de la VR DS.

def mlc_vers_dicom(lames):
    """(n_cp, 80, 2) convention Delivery -> listes MLCX (banc négatif puis positif)."""
    return [[f"{v:.4f}" for v in np.hstack([-cp[::-1, 1], cp[::-1, 0]])]
            for cp in np.asarray(lames)]


def machoires_vers_dicom(machoires):
    """(n_cp, 2) convention Delivery -> listes ASYMY."""
    return [[f"{-cote[1]:.4f}", f"{cote[0]:.4f}"] for cote in np.asarray(machoires)]
