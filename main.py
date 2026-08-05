"""La chaîne complète en un fichier : zip de TRF → séances → RT Plan « délivré ».

    python3 main.py plan.dcm "SDD+xxx.zip" --sortie delivres/

Pour chaque séance de l'archive qui correspond au plan, écrit un DICOM ayant la
structure exacte du plan — mêmes faisceaux, mêmes points de contrôle — mais les
positions de lames, de mâchoires, les angles et les MU que la machine a relevés.

⚠️ Les fichiers produits sont des documents d'analyse : SOP Instance UID neuf,
ApprovalStatus UNAPPROVED. Ils ne doivent jamais repartir vers un R&V.

Quatre classes, une responsabilité chacune :

    ArchiveTrf     lit les TRF d'un zip et rend les séances correspondant aux
                   critères qu'on lui DONNE — elle ne lit jamais le plan
    LecteurRtplan  lit un RT Plan DICOM, rend les tags demandés et le ds brut
    EcrivainDicom  écrit un ds en sécurité (UID neufs, UNAPPROVED)
    Chaine         orchestre les trois

Dépendances : numpy, pydicom, pymedphys (décodage TRF). Les colonnes
`unknown1..4` de pymedphys — l'horodatage machine découpé en quatre pour la
compatibilité de ses tests — sont recomposées ici en une colonne `ms`.
"""

import argparse
import copy
import datetime
import pathlib
import sys
import tempfile
import warnings
import zipfile

import numpy as np
import pydicom
from pydicom.uid import generate_uid

warnings.filterwarnings("ignore")
import pymedphys  # noqa: E402

COL_MU = "Step Dose/Actual Value (Mu)"
COL_ETAT = "Linac State/Actual Value (None)"
COL_BRAS = "Step Gantry/Scaled Actual (deg)"
COL_COLLIMATEUR = "Step Collimator/Scaled Actual (deg)"
COLS_MACHOIRES = ("X1 Diaphragm/Scaled Actual (mm)", "X2 Diaphragm/Scaled Actual (mm)")
PAIRES = 80
SONDAGES = (0.15, 0.35, 0.55, 0.75, 0.92)  # où sonder le dessin du champ, en fraction de MU
ECART_MAX_S = 1800          # au-delà, deux fichiers sont deux séances
PAS_S = 0.04                # 25 Hz


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


class ArchiveTrf:
    """Les TRF d'un zip, regroupés en séances, filtrés sur des critères reçus."""

    def __init__(self, source):
        self.source = pathlib.Path(source)
        self._fichiers = list(self._lire_tout())

    # ---- lecture ----

    def _entrees(self):
        """(nom, octets) pour chaque .trf, dans le zip comme dans un dossier."""
        if self.source.suffix.lower() == ".zip":
            with zipfile.ZipFile(self.source) as archive:
                for nom in sorted(archive.namelist()):
                    if nom.lower().endswith(".trf"):
                        yield nom, archive.read(nom)
        else:
            for chemin in sorted(self.source.rglob("*.trf")):
                yield str(chemin.relative_to(self.source)), chemin.read_bytes()

    @staticmethod
    def _colonne_ms(table):
        """Recompose l'horodatage machine que pymedphys laisse en unknown1..4."""
        if "unknown1" not in table.columns:
            return table              # encodage v1 : pas d'horodatage
        morceaux = [table[f"unknown{k}"].values.astype(np.uint64) for k in range(1, 5)]
        table["ms"] = (morceaux[0] + (morceaux[1] << np.uint64(16))
                       + (morceaux[2] << np.uint64(32))
                       + (morceaux[3] << np.uint64(48))).astype(np.int64)
        return table.drop(columns=[f"unknown{k}" for k in range(1, 5)])

    def _lire_tout(self):
        """Décode chaque fichier par pymedphys (qui exige un chemin sur disque)."""
        with tempfile.TemporaryDirectory() as dossier:
            for rang, (nom, octets) in enumerate(self._entrees()):
                chemin = pathlib.Path(dossier) / f"{rang:04d}.trf"
                chemin.write_bytes(octets)
                try:
                    entete, table = pymedphys.trf.read(str(chemin))
                except Exception as erreur:
                    print(f"  ⚠ {nom} illisible : {erreur}", file=sys.stderr)
                    continue
                table = self._colonne_ms(table)

                mu = table[COL_MU].values
                # Le compteur repart de zéro à chaque faisceau : le total est la
                # somme des segments, pas le maximum.
                plancher = max(0.5, 0.01 * float(mu.max())) if mu.max() > 0 else 0.5
                ruptures = np.where((np.diff(mu) < 0) & (mu[1:] <= plancher))[0]
                total = float(sum(mu[i] for i in ruptures) + mu[-1])

                # La date d'en-tête est la FIN de l'enregistrement ; la durée
                # vient de l'horloge machine quand elle existe.
                fin = datetime.datetime.strptime(
                    str(entete["date"].iloc[0]), "%y/%m/%d %H:%M:%S Z")
                duree = ((table["ms"].iloc[-1] - table["ms"].iloc[0]) / 1000.0
                         if "ms" in table.columns else len(table) * PAS_S)

                champ = str(entete["field_name"].iloc[0])
                yield {
                    "nom": nom,
                    "machine": str(entete["machine"].iloc[0]),
                    "champ": champ,
                    "debut": fin - datetime.timedelta(seconds=float(duree)),
                    "fin": fin,
                    "mu": total,
                    "etat_final": str(table[COL_ETAT].iloc[-1]),
                    "table": table,
                }

    # ---- regroupement ----

    def seances(self):
        """Chaîne les fichiers en séances, sur l'état final que la machine écrit.

        Une séance interrompue s'étale sur plusieurs fichiers : seule la règle
        « la précédente s'est finie sur Terminated Ok » clôt une séance, les
        autres coupures (machine, champ, écart de temps) séparent des
        traitements distincts.
        """
        seances, courante = [], None
        for f in sorted(self._fichiers, key=lambda x: (x["machine"], x["debut"])):
            if f["mu"] < 1.0:
                continue              # imagerie ou mise en place, transparent
            nouvelle = (
                courante is None
                or f["machine"] != courante["machine"]
                or f["champ"] != courante["champ"]
                or (f["debut"] - courante["fin"]).total_seconds() > ECART_MAX_S
                or courante["etat_final"] == "Terminated Ok"
            )
            if nouvelle:
                courante = {"machine": f["machine"], "champ": f["champ"],
                            "debut": f["debut"], "fin": f["fin"],
                            "mu": f["mu"], "etat_final": f["etat_final"],
                            "fichiers": [f]}
                seances.append(courante)
            else:
                courante["fichiers"].append(f)
                courante["mu"] += f["mu"]
                courante["fin"] = f["fin"]
                courante["etat_final"] = f["etat_final"]
        return seances

    # ---- appariement ----

    @staticmethod
    def _delivrance(seance):
        """L'axe de MU continu et les lames Delivery (n, 80, 2) d'une séance.

        Les remises à zéro internes sont neutralisées (cumul des écarts
        positifs) et chaque fragment est décalé du total des précédents.
        """
        mus, lames, decalage = [], [], 0.0
        for f in seance["fichiers"]:
            table = f["table"]
            d = np.diff(table[COL_MU].values, prepend=0.0)
            d[d < 0] = 0
            continu = np.cumsum(d)
            y1 = np.stack([table[f"Y1 Leaf {i}/Scaled Actual (mm)"].values
                           for i in range(1, PAIRES + 1)], axis=1)
            y2 = np.stack([table[f"Y2 Leaf {i}/Scaled Actual (mm)"].values
                           for i in range(1, PAIRES + 1)], axis=1)
            mus.append(continu + decalage)
            lames.append(np.stack([y1, y2], axis=2))
            decalage += float(continu[-1])
        return np.concatenate(mus), np.concatenate(lames)

    def _empreinte(self, seance, fractions):
        """Les lames de la séance aux fractions de MU demandées."""
        mu, lames = self._delivrance(seance)
        ordre = np.argsort(mu, kind="stable")
        mu, lames = mu[ordre], lames[ordre]
        cibles = np.array(fractions) * mu[-1]
        return np.stack([
            [np.interp(cibles, mu, lames[:, i, banc]) for i in range(PAIRES)]
            for banc in range(2)
        ])                                        # (2, 80, n_sondages)

    def correspondantes(self, mu_total, empreinte_plan,
                        tolerance_mu=0.01, seuil_dessin=3.0):
        """Les séances compatibles avec les critères REÇUS.

        Deux critères, tous deux décisifs : le total de MU à 1 % près — mesuré,
        un vrai appariement tombe à 0,1 % — et le dessin du champ à 3 mm de
        médiane — mesuré : 0,4 mm face au bon plan, 12,8 mm face à un autre.
        """
        retenues = []
        for seance in self.seances():
            ecart_mu = abs(seance["mu"] - mu_total) / mu_total if mu_total else 1.0
            if ecart_mu > tolerance_mu:
                continue
            dessin = float(np.median(np.abs(
                self._empreinte(seance, SONDAGES) - empreinte_plan)))
            if dessin <= seuil_dessin:
                seance["dessin"] = dessin
                retenues.append(seance)
        return retenues


class LecteurRtplan:
    """Un RT Plan DICOM : les tags demandés, et le ds brut."""

    def __init__(self, chemin):
        self.ds = pydicom.dcmread(chemin, force=True)
        if "BeamSequence" not in self.ds:
            raise SystemExit(f"{chemin} n'est pas un RT Plan.")
        self.chemin = pathlib.Path(chemin)

    def valeur(self, mot_cle):
        return getattr(self.ds, mot_cle, None)

    def mu_par_faisceau(self, fraction=1):
        """Les MU absolues sont dans FractionGroupSequence, pas dans le faisceau."""
        table = {}
        for groupe in self.ds.FractionGroupSequence:
            if int(getattr(groupe, "FractionGroupNumber", 1)) != fraction:
                continue
            for ref in groupe.ReferencedBeamSequence:
                if "BeamMeterset" in ref:
                    table[int(ref.ReferencedBeamNumber)] = float(ref.BeamMeterset)
        if not table:
            raise SystemExit(f"Aucun BeamMeterset pour le groupe de fractions {fraction}.")
        return table

    def mu_total(self, fraction=1):
        return sum(self.mu_par_faisceau(fraction).values())

    def grille(self, fraction=1):
        """MU cumulées de chaque point de contrôle, faisceaux bout à bout."""
        metersets = self.mu_par_faisceau(fraction)
        faisceaux, decalage = [], 0.0
        for faisceau in self.ds.BeamSequence:
            numero = int(faisceau.BeamNumber)
            if numero not in metersets:
                continue
            poids = np.array([float(cp.CumulativeMetersetWeight)
                              for cp in faisceau.ControlPointSequence])
            finale = float(faisceau.FinalCumulativeMetersetWeight)
            faisceaux.append({
                "numero": numero,
                "mu": metersets[numero],
                "cibles": decalage + metersets[numero] * poids / finale,
            })
            decalage += metersets[numero]
        return faisceaux

    def _lames_depliees(self, fraction=1):
        """(mu_cumulées, lames Delivery) de chaque point de contrôle du plan.

        Un point de contrôle n'écrit que ce qui change : la dernière valeur
        MLCX connue est reportée. Conversion DICOM -> Delivery vérifiée :
        banc 0 = LeafJawPositions[80:] renversé, banc 1 = -[:80] renversé.
        """
        mus, lames = [], []
        for bloc, faisceau in zip(self.grille(fraction), self.ds.BeamSequence):
            courant = None
            for cp, cible in zip(faisceau.ControlPointSequence, bloc["cibles"]):
                for item in getattr(cp, "BeamLimitingDevicePositionSequence", []):
                    if item.RTBeamLimitingDeviceType == "MLCX":
                        courant = np.array(item.LeafJawPositions, dtype=float)
                if courant is None:
                    continue
                mus.append(cible)
                lames.append(np.stack([courant[PAIRES:][::-1],
                                       -courant[:PAIRES][::-1]], axis=1))
        return np.array(mus), np.array(lames)

    def empreinte(self, fractions, fraction=1):
        """Les lames du plan aux fractions de MU demandées — même format que le log."""
        mu, lames = self._lames_depliees(fraction)
        cibles = np.array(fractions) * mu[-1]
        return np.stack([
            [np.interp(cibles, mu, lames[:, i, banc]) for i in range(PAIRES)]
            for banc in range(2)
        ])


class EcrivainDicom:
    """Écrit un ds en sécurité : le fichier ne doit jamais écraser le plan."""

    def ecrire(self, ds, chemin, description=""):
        ds.SOPInstanceUID = generate_uid()
        ds.SeriesInstanceUID = generate_uid()
        if "RTPlanLabel" in ds:
            ds.RTPlanLabel = (str(ds.RTPlanLabel) or "")[:10] + "_DEL"
        ds.ApprovalStatus = "UNAPPROVED"
        if description:
            ds.RTPlanDescription = description[:64]
        ds.save_as(str(chemin), enforce_file_format=False)
        return chemin


class Chaine:
    """Plan + archive -> un DICOM « délivré » par séance correspondante."""

    def __init__(self, chemin_plan, source_trf, sortie=None, fraction=1):
        self.plan = LecteurRtplan(chemin_plan)
        self.archive = ArchiveTrf(source_trf)
        self.sortie = pathlib.Path(sortie) if sortie else self.plan.chemin.parent
        self.fraction = fraction

    def _substituer(self, seance):
        """Injecte le mesuré de la séance dans la grille du plan."""
        mu_log, lames_log = self.archive._delivrance(seance)
        ordre = np.argsort(mu_log, kind="stable")
        mu_log, lames_log = mu_log[ordre], lames_log[ordre]

        # Séries complémentaires, sur le même axe : mâchoires et angles.
        tables = [f["table"] for f in seance["fichiers"]]
        machoires = np.concatenate([
            np.stack([t[c].values for c in COLS_MACHOIRES], axis=1) for t in tables
        ])[ordre]
        bras = np.concatenate([t[COL_BRAS].values for t in tables])[ordre]
        collimateur = np.concatenate(
            [t[COL_COLLIMATEUR].values for t in tables])[ordre]

        mu_plan = self.plan.mu_total(self.fraction)
        # Le plan est rejoué sur l'axe réellement parcouru : sans ce facteur,
        # une délivrance arrêtée avant la fin prolongerait la dernière valeur.
        facteur = float(mu_log[-1]) / mu_plan if mu_plan else 1.0

        delivre = copy.deepcopy(self.plan.ds)
        par_numero = {int(f.BeamNumber): f for f in delivre.BeamSequence}
        mu_delivres = {}

        for bloc in self.plan.grille(self.fraction):
            cibles = bloc["cibles"] * facteur
            lames = np.stack([
                [np.interp(cibles, mu_log, lames_log[:, i, banc])
                 for i in range(PAIRES)] for banc in range(2)
            ])                                       # (2, 80, n_cp)
            lames = np.transpose(lames, (2, 1, 0))   # (n_cp, 80, 2)
            jaw = np.stack([np.interp(cibles, mu_log, machoires[:, c])
                            for c in range(2)], axis=1)

            def angle(serie):
                deroule = np.degrees(np.unwrap(np.radians(serie)))
                return np.mod(np.interp(cibles, mu_log, deroule), 360.0)

            angles_bras, angles_coll = angle(bras), angle(collimateur)
            lames_dicom = mlc_vers_dicom(lames)
            jaw_dicom = machoires_vers_dicom(jaw)

            cible = par_numero[bloc["numero"]]
            for index, cp in enumerate(cible.ControlPointSequence):
                for position in getattr(cp, "BeamLimitingDevicePositionSequence", []):
                    if position.RTBeamLimitingDeviceType == "MLCX":
                        position.LeafJawPositions = lames_dicom[index]
                    elif position.RTBeamLimitingDeviceType in ("ASYMY", "Y"):
                        position.LeafJawPositions = jaw_dicom[index]
                if "GantryAngle" in cp:
                    cp.GantryAngle = f"{angles_bras[index]:.4f}"
                if "BeamLimitingDeviceAngle" in cp:
                    cp.BeamLimitingDeviceAngle = f"{angles_coll[index]:.4f}"
            mu_delivres[bloc["numero"]] = bloc["mu"] * facteur

        for groupe in delivre.FractionGroupSequence:
            if int(getattr(groupe, "FractionGroupNumber", 1)) != self.fraction:
                continue
            for ref in groupe.ReferencedBeamSequence:
                numero = int(ref.ReferencedBeamNumber)
                if numero in mu_delivres:
                    ref.BeamMeterset = f"{mu_delivres[numero]:.4f}"
        return delivre

    def executer(self):
        mu_plan = self.plan.mu_total(self.fraction)
        empreinte = self.plan.empreinte(SONDAGES, self.fraction)
        print(f"Plan    {self.plan.chemin.name} · {mu_plan:.1f} MU · "
              f"{len(self.plan.grille(self.fraction))} faisceau(x)")

        seances = self.archive.correspondantes(mu_plan, empreinte)
        print(f"        {len(seances)} séance(s) correspondante(s) "
              f"sur {len(self.archive.seances())}\n")

        self.sortie.mkdir(parents=True, exist_ok=True)
        ecrivain, ecrits = EcrivainDicom(), []
        for rang, seance in enumerate(seances, start=1):
            delivre = self._substituer(seance)
            horodatage = seance["debut"].strftime("%Y%m%d_%H%M%S")
            chemin = self.sortie / (f"{self.plan.chemin.stem}_delivre_"
                                    f"{horodatage}_s{rang:04d}.dcm")
            ecrivain.ecrire(delivre, chemin,
                            f"Reconstitue depuis {len(seance['fichiers'])} log(s) "
                            "machine. Analyse, non traitable.")
            ecart = 100 * (seance["mu"] - mu_plan) / mu_plan
            print(f"  {seance['debut']:%Y-%m-%d %H:%M} · "
                  f"{len(seance['fichiers'])} fichier(s) · {seance['mu']:7.1f} MU "
                  f"({ecart:+.2f} %) · dessin {seance['dessin']:.2f} mm "
                  f"-> {chemin.name}")
            ecrits.append(chemin)

        print(f"\n{len(ecrits)} fichier(s) écrit(s) dans {self.sortie}/")
        if ecrits:
            print("  SOP Instance UID neufs · ApprovalStatus UNAPPROVED · non traitables")
        return ecrits


def main():
    analyseur = argparse.ArgumentParser(
        description="Écrit un RT Plan « délivré » par séance d'une archive de TRF.",
        epilog="Les fichiers produits sont des documents d'analyse, non traitables.")
    analyseur.add_argument("plan", help="RT Plan DICOM de référence")
    analyseur.add_argument("source", help="archive SDD (.zip) ou dossier de .trf")
    analyseur.add_argument("--sortie", help="dossier de sortie (défaut : celui du plan)")
    analyseur.add_argument("--fraction", type=int, default=1,
                           help="groupe de fractions du plan (défaut : 1)")
    args = analyseur.parse_args()

    ecrits = Chaine(args.plan, args.source, args.sortie, args.fraction).executer()
    return 0 if ecrits else 1


if __name__ == "__main__":
    sys.exit(main())
