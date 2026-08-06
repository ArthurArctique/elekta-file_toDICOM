"""Lecture d'un RT Plan DICOM : les tags demandés, et le ds brut.

    from lecteur_rtplan import LecteurRtplan
    plan = LecteurRtplan("plan.dcm")
    plan.mu_total()            # MU de la fraction
    plan.trajectoire()         # mu, lames, bras, frontières de faisceaux
    plan.ds                    # le dataset pydicom, intact
"""

import pathlib
import sys

import numpy as np
import pydicom

from conventions import CLASSE_RT_PLAN, PAIRES

class LecteurRtplan:
    """Un RT Plan DICOM : les tags demandés, et le ds brut."""

    def __init__(self, chemin):
        # Un dataset déjà en mémoire est accepté tel quel : un plan dérivé tout
        # juste substitué n'a pas à faire un aller-retour par le disque pour
        # être relu.
        if isinstance(chemin, pydicom.dataset.Dataset):
            self.ds = chemin
            chemin = getattr(chemin, "RTPlanLabel", None) or "dataset"
        else:
            # Lecture normale d'abord. `force=True` n'est employé qu'en repli,
            # et signalé : il accepte un fichier sans préambule ni marqueur
            # « DICM », ce qui est le cas des plans de référence publics mais ne
            # doit pas passer inaperçu sur un export clinique.
            try:
                self.ds = pydicom.dcmread(chemin)
            except pydicom.errors.InvalidDicomError:
                self.ds = pydicom.dcmread(chemin, force=True)
                print(f"  ⚠ {pathlib.Path(chemin).name} : lecture forcée, "
                      "préambule ou méta-en-tête absent", file=sys.stderr)
        classe = getattr(self.ds, "SOPClassUID", None)
        if classe is not None and classe != CLASSE_RT_PLAN:
            raise SystemExit(f"{chemin} : SOP Class {classe}, "
                             "attendu RT Plan Storage.")
        for requis in ("BeamSequence", "FractionGroupSequence"):
            if requis not in self.ds:
                raise SystemExit(f"{chemin} : {requis} absente, "
                                 "ce n'est pas un RT Plan exploitable.")
        self.chemin = pathlib.Path(chemin)
        self._grilles = {}
        self._verifier_geometrie()

    def _verifier_geometrie(self):
        """Ce fichier vise un MLC à 80 paires (Agility). Le dire tôt et net.

        Sans ce contrôle, une autre géométrie ne lèverait rien : elle donnerait
        des positions découpées au mauvais endroit, plausibles et fausses.
        """
        for faisceau in self.ds.BeamSequence:
            for cp in faisceau.ControlPointSequence:
                for item in getattr(cp, "BeamLimitingDevicePositionSequence", []):
                    if item.RTBeamLimitingDeviceType == "MLCX":
                        n = len(item.LeafJawPositions)
                        if n != 2 * PAIRES:
                            raise SystemExit(
                                f"{self.chemin.name} : MLCX à {n // 2} paires de "
                                f"lames, cet outil en attend {PAIRES} (Agility).")
                        return
        raise SystemExit(f"{self.chemin.name} : aucune position MLCX trouvée.")

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
        if fraction in self._grilles:
            return self._grilles[fraction]
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
        self._grilles[fraction] = faisceaux
        return faisceaux

    def trajectoire(self, fraction=1):
        """Ce que le plan demande, point de contrôle par point de contrôle.

        Rend un dictionnaire : `mu` cumulées, `lames` en convention Delivery
        (n, 80, 2), `bras` en degrés, et `decoupe` — les frontières de faisceaux
        sur l'axe, utiles pour les tracer.

        Un point de contrôle n'écrit que ce qui change : la dernière valeur
        connue est reportée, sans quoi la moitié des angles d'un arc manquent.
        Conversion DICOM -> Delivery vérifiée : banc 0 = LeafJawPositions[80:]
        renversé, banc 1 = -[:80] renversé.
        """
        mus, lames, bras, decoupe = self._deplier(fraction)
        return {"mu": mus, "lames": lames, "bras": bras, "decoupe": decoupe}

    def _lames_depliees(self, fraction=1):
        """(mu_cumulées, lames Delivery) — ce dont l'empreinte a besoin."""
        mus, lames, _, _ = self._deplier(fraction)
        return mus, lames

    def _deplier(self, fraction=1):
        """Le dépliage lui-même, en un seul endroit."""
        # Appariement par BeamNumber, jamais par position : `grille()` ne rend
        # que les faisceaux du groupe de fractions demandé, alors que
        # `BeamSequence` les porte tous. Un zip décalerait tout dès qu'un
        # faisceau du plan n'appartient pas au groupe — cas réel du plan à deux
        # groupes, où le groupe 2 ne référence que les faisceaux 4, 5 et 6.
        par_numero = {int(f.BeamNumber): f for f in self.ds.BeamSequence}
        mus, lames, bras, decoupe = [], [], [], []
        for bloc in self.grille(fraction):
            faisceau = par_numero[bloc["numero"]]
            debut = len(mus)
            # Vrai par construction — `cibles` vient de cette ControlPointSequence
            # — mais posé explicitement : c'est le dernier appariement positionnel
            # du fichier, et une évolution de `grille()` le romprait en silence.
            if len(faisceau.ControlPointSequence) != len(bloc["cibles"]):
                raise SystemExit(
                    f"Faisceau {bloc['numero']} : {len(faisceau.ControlPointSequence)} "
                    f"points de contrôle pour {len(bloc['cibles'])} cibles de MU.")
            courant, angle = None, 0.0
            for cp, cible in zip(faisceau.ControlPointSequence, bloc["cibles"]):
                for item in getattr(cp, "BeamLimitingDevicePositionSequence", []):
                    if item.RTBeamLimitingDeviceType == "MLCX":
                        courant = np.array(item.LeafJawPositions, dtype=float)
                if "GantryAngle" in cp:
                    angle = float(cp.GantryAngle)
                if courant is None:
                    continue
                mus.append(cible)
                bras.append(angle)
                lames.append(np.stack([courant[PAIRES:][::-1],
                                       -courant[:PAIRES][::-1]], axis=1))
            decoupe.append((bloc["numero"], debut, len(mus)))
        return np.array(mus), np.array(lames), np.array(bras), decoupe

    def empreinte(self, fractions, fraction=1):
        """Les lames du plan aux fractions de MU demandées — même format que le log."""
        mu, lames = self._lames_depliees(fraction)
        cibles = np.array(fractions) * mu[-1]
        return np.stack([
            [np.interp(cibles, mu, lames[:, i, banc]) for i in range(PAIRES)]
            for banc in range(2)
        ])
