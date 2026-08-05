"""La chaîne complète en un fichier : zip de TRF → séances → RT Plan « délivré ».

    from main import Chaine
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

Quatre classes, une responsabilité chacune :

    ArchiveTrf     lit les TRF d'un zip et rend les séances correspondant aux
                   critères qu'on lui DONNE — elle ne lit jamais le plan
    LecteurRtplan  lit un RT Plan DICOM, rend les tags demandés et le ds brut
    EcrivainDicom  écrit un ds en sécurité (UID neufs, UNAPPROVED)
    Chaine         orchestre les trois

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
import datetime
import pathlib
import sys
import tempfile
import warnings
import zipfile

import numpy as np
import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.uid import (ExplicitVRBigEndian, ExplicitVRLittleEndian,
                         ImplicitVRLittleEndian, generate_uid)

# Filtre restreint à l'import de pymedphys, qui est bavard au chargement. Un
# filtre global masquerait aussi les avertissements de pydicom sur les valeurs
# non conformes — VR DS trop longue notamment —, c'est-à-dire exactement ceux
# qu'on veut voir.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
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
        self._seances = None

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
        if self._seances is not None:
            return self._seances
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
        self._seances = seances
        return seances

    # ---- appariement ----

    @staticmethod
    def _delivrance(seance):
        """L'axe de MU continu et les lames Delivery (n, 80, 2) d'une séance.

        Les remises à zéro internes sont neutralisées (cumul des écarts
        positifs) et chaque fragment est décalé du total des précédents.

        Mémorisé sur la séance : l'appariement le calcule pour toutes les
        séances, la substitution le redemande pour celles qui sont retenues.
        """
        if "_recollee" in seance:
            return seance["_recollee"]
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
        seance["_recollee"] = (np.concatenate(mus), np.concatenate(lames))
        return seance["_recollee"]

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
        # Lecture normale d'abord. `force=True` n'est employé qu'en repli, et
        # signalé : il accepte un fichier sans préambule ni marqueur « DICM »,
        # ce qui est le cas des plans de référence publics mais ne doit pas
        # passer inaperçu sur un export clinique.
        try:
            self.ds = pydicom.dcmread(chemin)
        except pydicom.errors.InvalidDicomError:
            self.ds = pydicom.dcmread(chemin, force=True)
            print(f"  ⚠ {pathlib.Path(chemin).name} : lecture forcée, "
                  "préambule ou méta-en-tête absent", file=sys.stderr)
        classe = getattr(self.ds, "SOPClassUID", None)
        if classe is not None and classe != EcrivainDicom.CLASSE_RT_PLAN:
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

    def _lames_depliees(self, fraction=1):
        """(mu_cumulées, lames Delivery) de chaque point de contrôle du plan.

        Un point de contrôle n'écrit que ce qui change : la dernière valeur
        MLCX connue est reportée. Conversion DICOM -> Delivery vérifiée :
        banc 0 = LeafJawPositions[80:] renversé, banc 1 = -[:80] renversé.
        """
        # Appariement par BeamNumber, jamais par position : `grille()` ne rend
        # que les faisceaux du groupe de fractions demandé, alors que
        # `BeamSequence` les porte tous. Un zip décalerait tout dès qu'un
        # faisceau du plan n'appartient pas au groupe — cas réel du plan à deux
        # groupes, où le groupe 2 ne référence que les faisceaux 4, 5 et 6.
        par_numero = {int(f.BeamNumber): f for f in self.ds.BeamSequence}
        mus, lames = [], []
        for bloc in self.grille(fraction):
            faisceau = par_numero[bloc["numero"]]
            # Vrai par construction — `cibles` vient de cette ControlPointSequence
            # — mais posé explicitement : c'est le dernier appariement positionnel
            # du fichier, et une évolution de `grille()` le romprait en silence.
            if len(faisceau.ControlPointSequence) != len(bloc["cibles"]):
                raise SystemExit(
                    f"Faisceau {bloc['numero']} : {len(faisceau.ControlPointSequence)} "
                    f"points de contrôle pour {len(bloc['cibles'])} cibles de MU.")
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
    """Écrit un ds avec une identité neuve, pour qu'il n'écrase jamais le plan.

    ⚠️ Ce marquage n'est **pas** une protection technique. Le fichier reste un
    RT Plan avec sa SOP Class d'origine : un R&V peut l'importer, l'afficher et
    le transmettre. `UNAPPROVED` est un statut, pas un verrou. La vraie barrière
    est l'environnement — répertoire isolé, aucune route DICOM vers le réseau
    clinique.
    """

    CLASSE_RT_PLAN = "1.2.840.10008.5.1.4.1.1.481.5"

    # Politique d'identité, explicite parce qu'elle engage :
    #   SOPInstanceUID              remplacé   — c'est un autre document
    #   SeriesInstanceUID           remplacé   — il n'appartient pas à la série du plan
    #   MediaStorage*               synchronisés sur les deux ci-dessus
    #   StudyInstanceUID            CONSERVÉ   — le dérivé reste dans l'étude du
    #                                            patient, ce qui le rend traçable
    #                                            mais aussi associable au dossier
    #   SOPClassUID                 conservé   — ça reste un RT Plan
    #   FrameOfReferenceUID         conservé   — même repère géométrique

    def ecrire(self, ds, chemin, description=""):
        nouvel_uid = generate_uid()
        ds.SOPInstanceUID = nouvel_uid
        ds.SeriesInstanceUID = generate_uid()

        # Le méta-en-tête porte une seconde copie de l'UID et de la SOP Class.
        # Sans ces lignes, le fichier sort avec un UID neuf dans le dataset et
        # **celui du plan d'origine** dans le méta : l'identité du document
        # devient incohérente. Invisible sur les plans publics, qui n'ont aucun
        # méta-en-tête du tout.
        if getattr(ds, "file_meta", None) is None:
            ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPInstanceUID = nouvel_uid
        ds.file_meta.MediaStorageSOPClassUID = getattr(
            ds, "SOPClassUID", self.CLASSE_RT_PLAN)
        if "TransferSyntaxUID" not in ds.file_meta:
            # Aucun méta d'origine : on déclare l'encodage que pydicom a
            # effectivement employé à la lecture plutôt qu'une valeur arbitraire.
            # `original_encoding` vaut (implicit_VR, little_endian).
            implicite, petit_boutiste = getattr(
                ds, "original_encoding", (True, True))
            if implicite and petit_boutiste:
                ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
            elif petit_boutiste:
                ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            else:
                ds.file_meta.TransferSyntaxUID = ExplicitVRBigEndian

        if "RTPlanLabel" in ds:
            ds.RTPlanLabel = (str(ds.RTPlanLabel) or "")[:10] + "_DEL"
        ds.ApprovalStatus = "UNAPPROVED"
        if description:
            ds.RTPlanDescription = description[:64]

        # `enforce_file_format=True` écrit le préambule de 128 octets et le
        # marqueur « DICM ». Sans lui le fichier n'est pas conforme Part 10 et
        # ne se relit qu'avec `force=True` — y compris par les visionneuses.
        ds.save_as(str(chemin), enforce_file_format=True)
        self._controler(chemin, nouvel_uid)
        return chemin

    @staticmethod
    def _controler(chemin, uid_attendu):
        """Relit le fichier écrit et vérifie son identité.

        Contrôler l'objet en mémoire ne prouve rien sur le fichier : c'est
        précisément ainsi qu'un premier correctif est passé pour bon alors que
        le préambule manquait encore. La relecture se fait donc **sans**
        `force=True`, ce qui vérifie du même coup la conformité Part 10.
        """
        relu = pydicom.dcmread(str(chemin))
        meta = relu.file_meta
        if relu.SOPInstanceUID != uid_attendu:
            raise SystemExit(f"{chemin} : SOPInstanceUID écrit incohérent.")
        if meta.MediaStorageSOPInstanceUID != relu.SOPInstanceUID:
            raise SystemExit(f"{chemin} : méta-en-tête et dataset en désaccord "
                             "sur le SOP Instance UID.")
        if meta.MediaStorageSOPClassUID != relu.SOPClassUID:
            raise SystemExit(f"{chemin} : méta-en-tête et dataset en désaccord "
                             "sur la SOP Class.")
        syntaxe = meta.TransferSyntaxUID
        if relu.original_encoding != (syntaxe.is_implicit_VR, syntaxe.is_little_endian):
            raise SystemExit(f"{chemin} : TransferSyntaxUID déclaré "
                             f"({syntaxe.name}) ≠ encodage réellement écrit.")


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
                            f"Derive de {len(seance['fichiers'])} log(s) machine. "
                            "Analyse uniquement.")
            ecart = 100 * (seance["mu"] - mu_plan) / mu_plan
            print(f"  {seance['debut']:%Y-%m-%d %H:%M} · "
                  f"{len(seance['fichiers'])} fichier(s) · {seance['mu']:7.1f} MU "
                  f"({ecart:+.2f} %) · dessin {seance['dessin']:.2f} mm "
                  f"-> {chemin.name}")
            ecrits.append(chemin)

        print(f"\n{len(ecrits)} fichier(s) écrit(s) dans {self.sortie}/")
        if ecrits:
            print("  UID neufs · UNAPPROVED · plans dérivés pour analyse, à tenir "
                  "hors de toute route DICOM clinique")
        return ecrits
