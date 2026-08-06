"""Les TRF d'une archive SDD, regroupés en séances.

    from archive_trf import ArchiveTrf
    archive = ArchiveTrf("SDD+xxx.zip")
    seances = archive.seances()
    retenues = archive.correspondantes(mu_total, empreinte_plan)

La classe ne lit jamais un plan : les critères d'appariement lui sont **donnés**.
"""

import datetime
import pathlib
import sys
import tempfile
import warnings
import zipfile

import numpy as np

# Filtre restreint à l'import de pymedphys, qui est bavard au chargement. Un
# filtre global masquerait aussi les avertissements de pydicom sur les valeurs
# non conformes — VR DS trop longue notamment —, c'est-à-dire exactement ceux
# qu'on veut voir.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import pymedphys  # noqa: E402

from conventions import (COL_ETAT, COL_MU, ECART_MAX_S, PAIRES, PAS_S,
                         SONDAGES)

class ArchiveTrf:
    """Les TRF d'un zip, regroupés en séances, filtrés sur des critères reçus."""

    def __init__(self, source):
        self.source = pathlib.Path(source)
        self.doublons = []
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
        """Décode chaque fichier par pymedphys (qui exige un chemin sur disque).

        Les doublons sont écartés. Une archive SDD contient couramment le même
        enregistrement à deux endroits ; sans ce tri, chaque séance concernée
        est comptée **deux fois** — la seconde copie ouvrant une séance de plus,
        puisque la précédente s'est close sur « Terminated Ok ».

        La clé est sémantique : une même machine ne peut pas finir deux
        délivrances différentes du même champ à la même seconde, avec les mêmes
        MU et le même nombre d'échantillons.
        """
        vus = set()
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
                cle = (str(entete["machine"].iloc[0]), champ, fin,
                       round(total, 1), len(table))
                if cle in vus:
                    self.doublons.append(nom)
                    continue
                vus.add(cle)

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

    @staticmethod
    def _empreinte(seance, fractions):
        """Les lames de la séance aux fractions de MU demandées."""
        mu, lames = ArchiveTrf._delivrance(seance)
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
