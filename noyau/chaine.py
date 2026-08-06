"""L'orchestration : archive + plan → un RT Plan dérivé par séance.

    from chaine import Chaine
    Chaine("plan.dcm", "SDD+xxx.zip", sortie="delivres/").executer()

Pour chaque séance de l'archive qui correspond au plan, écrit un RT Plan
**dérivé** : la grille du plan est conservée — mêmes faisceaux, mêmes points de
contrôle — et les trajectoires relevées par la machine y sont rééchantillonnées.
Les MU totales viennent du log ; elles sont ensuite réparties entre les
faisceaux **au prorata du plan**, pas relevées faisceau par faisceau (limite 1).

⚠️ Les fichiers produits sont des RT Plan dérivés, destinés à l'analyse. UID
neufs et ApprovalStatus UNAPPROVED les distinguent du plan d'origine, mais ce
marquage ne garantit pas leur rejet par un système clinique : ils gardent leur
SOP Class et restent importables. La barrière doit être l'environnement —
répertoire isolé, aucune route DICOM vers le réseau clinique.

Dépendances : numpy, pydicom, pymedphys (décodage TRF). Les colonnes
`unknown1..4` de pymedphys — l'horodatage machine découpé en quatre pour la
compatibilité de ses tests — sont recomposées ici en une colonne `ms`.

Limites connues, non corrigées
------------------------------
Elles sont réelles et documentées plutôt que masquées.

1. **Les MU par faisceau sont réparties au prorata du plan.** Le facteur
   `MU délivrées / MU prévues` est global : si un seul faisceau a été écourté,
   l'écart est étalé sur tous. Plan 100 + 200 MU délivré 90 + 200 donnerait
   96,7 + 193,3, total juste et répartition fausse. `BeamMeterset` ne doit donc
   pas être lu comme « les MU relevées pour ce faisceau ». Retrouver la vraie
   répartition demanderait d'apparier les faisceaux du log à ceux du plan et
   d'interpoler faisceau par faisceau — ce qui lèverait aussi la limite 2.

2. **L'axe des MU comporte des plateaux.** Mesuré sur l'IMRT à neuf faisceaux,
   54 % des échantillons partagent une MU déjà vue, et le plus long plateau
   dure 457 échantillons pendant lesquels les lames parcourent 108 mm : c'est
   le repositionnement entre segments, faisceau éteint. `np.interp` y retient
   la **dernière** valeur, donc la géométrie d'arrivée. C'est défendable mais
   subi, pas choisi.

3. **Seuls les tags déjà présents dans un point de contrôle sont réécrits.**
   Un plan qui omet `GantryAngle` sur un point de contrôle inchangé ne peut pas
   recevoir l'angle mesuré à cet endroit. La structure du plan est préservée au
   prix de la fidélité.

4. **Les sens de rotation ne sont pas reconstruits.** `GantryRotationDirection`
   reste celui du plan alors que les angles, eux, sont mesurés.

5. **L'appariement repose sur deux critères** — MU totales et médiane du dessin
   du champ. Mesuré : 0,4 mm face au bon plan contre 12,8 mm face à un autre
   traitement. Une médiane peut néanmoins diluer quelques lames très fausses.
"""

import copy
import pathlib

import numpy as np

from .archive_trf import ArchiveTrf
from .conventions import (COL_BRAS, COL_COLLIMATEUR, COLS_MACHOIRES,
                          PAIRES, SONDAGES, machoires_vers_dicom,
                          mlc_vers_dicom)
from .ecrivain_dicom import EcrivainDicom
from .lecteur_rtplan import LecteurRtplan

class Chaine:
    """Plan + archive -> un DICOM « délivré » par séance correspondante."""

    def __init__(self, chemin_plan, source_trf=None, sortie=None, fraction=1):
        # `source_trf` peut être omis quand on ne veut que substituer dans des
        # séances déjà en main : lire l'archive est de loin le plus coûteux.
        self.plan = LecteurRtplan(chemin_plan)
        self.archive = ArchiveTrf(source_trf) if source_trf is not None else None
        self.sortie = pathlib.Path(sortie) if sortie else self.plan.chemin.parent
        self.fraction = fraction

    def _substituer(self, seance):
        """Injecte le mesuré de la séance dans la grille du plan."""
        mu_log, lames_log = ArchiveTrf._delivrance(seance)
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
            # Heure locale : c'est celle sous laquelle on cherchera la
            # séance dans Mosaiq.
            horodatage = seance.get("debut_local", seance["debut"]).strftime(
                "%Y%m%d_%H%M%S")
            chemin = self.sortie / (f"{self.plan.chemin.stem}_delivre_"
                                    f"{horodatage}_s{rang:04d}.dcm")
            ecrivain.ecrire(delivre, chemin,
                            f"Derive de {len(seance['fichiers'])} log(s) machine. "
                            "Analyse uniquement.")
            ecart = 100 * (seance["mu"] - mu_plan) / mu_plan
            print(f"  {seance.get('debut_local', seance['debut']):%Y-%m-%d %H:%M} · "
                  f"{len(seance['fichiers'])} fichier(s) · {seance['mu']:7.1f} MU "
                  f"({ecart:+.2f} %) · dessin {seance['dessin']:.2f} mm "
                  f"-> {chemin.name}")
            ecrits.append(chemin)

        print(f"\n{len(ecrits)} fichier(s) écrit(s) dans {self.sortie}/")
        if ecrits:
            print("  UID neufs · UNAPPROVED · plans dérivés pour analyse, à tenir "
                  "hors de toute route DICOM clinique")
        return ecrits
