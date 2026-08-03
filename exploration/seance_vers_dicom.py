"""Produit un RT Plan DICOM « délivré » à partir d'un plan et d'une séance.

    python3 exploration/seance_vers_dicom.py plan.dcm seance/ --sortie delivre.dcm
    python3 exploration/seance_vers_dicom.py plan.dcm "SDD+xxx.zip" --filtre 21_53
    python3 exploration/seance_vers_dicom.py plan.dcm seance/ --consigne

Le fichier produit a la structure exacte du plan d'origine — mêmes faisceaux,
mêmes points de contrôle — mais les positions de lames, de mâchoires, les angles
de bras et de collimateur, et les MU, sont **ceux que la machine a relevés**.

⚠️ Ce fichier n'est pas traitable et ne doit jamais repartir vers un R&V ou un
accélérateur. Il porte un SOP Instance UID neuf (il n'écrasera donc jamais le
plan d'origine), `ApprovalStatus = UNAPPROVED`, et un libellé suffixé. C'est un
document d'analyse.

Substituer plutôt que reconstruire
----------------------------------
Repartir des logs pour fabriquer un plan de zéro échoue en VMAT : `to_dicom` de
pymedphys segmente par angle de bras et refuse un arc, et on obtiendrait de
toute façon vingt fois trop de points de contrôle (2 717 échantillons pour 111
points). On garde donc la grille du plan et on y injecte le mesuré, interpolé
sur l'axe des MU cumulées. C'est aussi l'architecture du précédent publié
(PMC10018669).

L'axe des MU est le seul commun aux deux fichiers : le plan ignore le temps, le
log ignore les points de contrôle du plan. Tout passe par là.

Ce que le script suppose
------------------------
Que la séance a délivré les faisceaux du plan, dans l'ordre du plan. Il le
vérifie sur le total de MU et sur le nombre de faisceaux, et le dit s'il y a
désaccord — mais il ne peut pas le prouver. Passer d'abord par
`chercher_seances.py` pour s'assurer qu'on tient la bonne séance.
"""

import argparse
import copy
import pathlib
import sys
import tempfile

import numpy as np
import pydicom
import pymedphys
from pydicom.uid import generate_uid
from pymedphys import Delivery

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from organiser_trf import TrfIllisible, parcourir  # noqa: E402
from organiser_trf import resumer as resumer_trf  # noqa: E402
from trf_vers_dicom_vmat import corriger_numpy2  # noqa: E402

PAIRES = 80


def consigne_du_servo(table):
    """Reconstitue ce que la machine *demandait* aux lames, à chaque instant.

    Le TRF porte sa propre mesure d'erreur : consigne − réalisé, établie par la
    machine au même instant. La consigne se retrouve donc en la rajoutant.

    Attention au signe : pymedphys renvoie les positions du banc Y2 (il les
    multiplie par −1) mais **pas** ses erreurs. Pour Y2 il faut donc soustraire
    là où pour Y1 il faut ajouter. Vérifié sur imrt.trf : pendant les
    déplacements, la bonne combinaison donne une consigne stable à 0,1 mm près,
    la mauvaise dérive de 46 mm.
    """
    reconstituee = table.copy()
    for i in range(1, PAIRES + 1):
        for banc, signe in (("Y1", +1), ("Y2", -1)):
            position = f"{banc} Leaf {i}/Scaled Actual (mm)"
            erreur = f"{banc} Leaf {i}/Positional Error (mm)"
            if position in table and erreur in table:
                reconstituee[position] = table[position] + signe * table[erreur]
    return reconstituee


def lire_seance(sources, filtre=None, consigne=False):
    """Concatène les fichiers d'une séance en une délivrance continue.

    Une séance interrompue s'écrit en plusieurs fichiers dont le compteur de MU
    repart de zéro ; à l'intérieur d'un fichier, il repart aussi à chaque
    faisceau. `Delivery.from_trf` neutralise déjà les remises à zéro internes
    (il cumule les écarts positifs) ; il reste à décaler chaque fichier du total
    des précédents pour obtenir un axe de MU continu sur toute la séance.
    """
    trouves = []
    for nom, _, octets in parcourir(sources):
        if filtre and filtre not in nom:
            continue
        try:
            trouves.append((resumer_trf(octets, nom)["debut_utc"], nom, octets))
        except (TrfIllisible, Exception) as erreur:  # noqa: B014
            print(f"  ⚠ {nom} ignoré : {erreur}", file=sys.stderr)
    if not trouves:
        raise SystemExit("Aucun fichier TRF exploitable.")
    trouves.sort()

    morceaux, decalage = [], 0.0
    with tempfile.TemporaryDirectory() as dossier:
        for rang, (_, nom, octets) in enumerate(trouves):
            chemin = pathlib.Path(dossier) / f"{rang:03d}.trf"
            chemin.write_bytes(octets)
            _, table = pymedphys.trf.read(str(chemin))
            livraison = Delivery._from_pandas(
                consigne_du_servo(table) if consigne else table
            )
            mu = np.asarray(livraison.monitor_units, dtype=float)
            morceaux.append({
                "nom": nom,
                "mu": mu + decalage,
                "mlc": np.asarray(livraison.mlc, dtype=float),
                "jaw": np.asarray(livraison.jaw, dtype=float),
                "bras": np.asarray(livraison.gantry, dtype=float),
                "collimateur": np.asarray(livraison.collimator, dtype=float),
                # une remise à zéro du compteur brut = un faisceau de plus
                "faisceaux": 1 + int((np.diff(
                    table["Step Dose/Actual Value (Mu)"].values) < 0).sum()),
            })
            decalage += float(mu.max())

    for morceau in morceaux:
        print(f"  {morceau['nom'][-46:]:<46} {morceau['mu'].max() - morceau['mu'].min():7.1f} MU"
              f" · {morceau['faisceaux']} faisceau(x)")

    return {
        "mu": np.concatenate([m["mu"] for m in morceaux]),
        "mlc": np.concatenate([m["mlc"] for m in morceaux]),
        "jaw": np.concatenate([m["jaw"] for m in morceaux]),
        "bras": np.concatenate([m["bras"] for m in morceaux]),
        "collimateur": np.concatenate([m["collimateur"] for m in morceaux]),
        "fichiers": len(morceaux),
        "faisceaux": sum(m["faisceaux"] for m in morceaux),
    }


def grille_du_plan(plan, fraction):
    """Les MU cumulées de chaque point de contrôle, sur un axe continu.

    Les faisceaux sont mis bout à bout comme la machine les délivre : c'est le
    même axe que celui reconstitué du côté du log.
    """
    metersets = {}
    for groupe in plan.FractionGroupSequence:
        if int(getattr(groupe, "FractionGroupNumber", 1)) != fraction:
            continue
        for reference in groupe.ReferencedBeamSequence:
            if "BeamMeterset" in reference:
                metersets[int(reference.ReferencedBeamNumber)] = \
                    float(reference.BeamMeterset)
    if not metersets:
        raise SystemExit(f"Aucun BeamMeterset pour le groupe de fractions {fraction}.")

    faisceaux, decalage = [], 0.0
    for faisceau in plan.BeamSequence:
        numero = int(faisceau.BeamNumber)
        if numero not in metersets:
            continue
        mu = metersets[numero]
        finale = float(faisceau.FinalCumulativeMetersetWeight)
        poids = np.array([float(cp.CumulativeMetersetWeight)
                          for cp in faisceau.ControlPointSequence])
        faisceaux.append({
            "faisceau": faisceau,
            "numero": numero,
            "mu": mu,
            "cibles": decalage + mu * poids / finale,
        })
        decalage += mu
    return faisceaux, decalage


def echantillonner(seance, cibles):
    """Relève le mesuré aux MU demandées.

    Les angles sont déroulés avant interpolation : sans quoi le passage 360°→0°
    d'un arc produit une moyenne aberrante au milieu de la plage.
    """
    mu = seance["mu"]
    ordre = np.argsort(mu, kind="stable")
    mu = mu[ordre]

    def serie(nom):
        return seance[nom][ordre]

    mlc, jaw = serie("mlc"), serie("jaw")
    lames = np.stack([
        [np.interp(cibles, mu, mlc[:, lame, banc]) for banc in range(2)]
        for lame in range(mlc.shape[1])
    ], axis=1)                                    # (2, n_lames, n_cibles)
    lames = np.transpose(lames, (2, 1, 0))        # (n_cibles, n_lames, 2)

    machoires = np.stack(
        [np.interp(cibles, mu, jaw[:, cote]) for cote in range(2)], axis=1)

    def angle(nom):
        deroule = np.degrees(np.unwrap(np.radians(serie(nom))))
        return np.mod(np.interp(cibles, mu, deroule), 360.0)

    return lames, machoires, angle("bras"), angle("collimateur")


def paires_ouvertes(dicom):
    """Masque des lames qui forment réellement le champ.

    `LeafJawPositions` range les 2 × N lames bout à bout : les N premières pour
    le banc négatif, les N suivantes pour le banc positif. L'ouverture d'une
    paire est donc leur **différence** — à ne pas confondre avec la convention
    `Delivery` de pymedphys, où les deux bancs sont comptés dans le même sens et
    où l'ouverture est leur somme. S'être trompé de convention donnait 27 % de
    lames retenues au lieu de 52 %.

    Sans ce tri, la médiane est dominée par les lames garées, immobiles et donc
    parfaitement conformes : elle mesure surtout combien de lames ne servent pas.
    """
    n = dicom.shape[1] // 2
    ecartees = (dicom[:, n:] - dicom[:, :n]) > 5.0     # (n_points, n_paires)
    return np.hstack([ecartees, ecartees])             # (n_points, 2 n_paires)


def main():
    analyseur = argparse.ArgumentParser(
        description="Produit un RT Plan DICOM « délivré » depuis un plan et une séance.",
        epilog="Le fichier produit est un document d'analyse : il n'est pas "
               "traitable et ne doit jamais repartir vers un R&V.")
    analyseur.add_argument("plan", help="RT Plan DICOM d'origine")
    analyseur.add_argument("sources", nargs="+",
                           help="fichiers .trf de la séance, dossier, ou archive SDD")
    analyseur.add_argument("--sortie", help="DICOM produit (défaut : <plan>_delivre.dcm)")
    analyseur.add_argument("--fraction", type=int, default=1,
                           help="groupe de fractions à utiliser (défaut : 1)")
    analyseur.add_argument("--filtre", help="ne retenir que les .trf dont le nom contient ceci")
    analyseur.add_argument("--consigne", action="store_true",
                           help="écrire la consigne du servomoteur (position + erreur) "
                                "au lieu de la position réellement atteinte")
    args = analyseur.parse_args()

    utilitaires = corriger_numpy2()
    plan = pydicom.dcmread(args.plan, force=True)
    if "BeamSequence" not in plan:
        raise SystemExit(f"{args.plan} n'est pas un RT Plan.")

    print(f"Plan    {pathlib.Path(args.plan).name}")
    faisceaux, mu_plan = grille_du_plan(plan, args.fraction)
    points = sum(len(f["cibles"]) for f in faisceaux)
    print(f"        {len(faisceaux)} faisceau(x) · {points} points de contrôle "
          f"· {mu_plan:.1f} MU\n")

    print("Séance")
    seance = lire_seance(args.sources, args.filtre, args.consigne)
    mu_log = float(seance["mu"].max())
    print(f"        {seance['fichiers']} fichier(s) · {len(seance['mu'])} échantillons "
          f"· {seance['faisceaux']} faisceau(x) · {mu_log:.1f} MU")

    ecart = 100 * (mu_log - mu_plan) / mu_plan if mu_plan else 0.0
    print(f"\nMU log / plan : {mu_log:.1f} contre {mu_plan:.1f} ({ecart:+.2f} %)")
    if abs(ecart) > 1.0:
        print("  ⚠ plus de 1 % d'écart : cette séance n'est probablement pas ce plan,")
        print("    ou elle est incomplète. Vérifier avec chercher_seances.py.")
    if seance["faisceaux"] != len(faisceaux):
        print(f"  ⚠ {seance['faisceaux']} faisceau(x) dans le log pour "
              f"{len(faisceaux)} dans le plan — une séance interrompue en compte "
              f"davantage, c'est attendu ; sinon l'appariement est douteux.")

    # Le plan est rejoué sur l'axe réellement parcouru : si la délivrance s'est
    # arrêtée avant la fin, interpoler aux MU absolues du plan prolongerait la
    # dernière valeur mesurée sans le dire. On met les deux axes à la même
    # échelle et on le signale plutôt.
    facteur = mu_log / mu_plan if mu_plan else 1.0

    delivre = copy.deepcopy(plan)
    par_numero = {int(f.BeamNumber): f for f in delivre.BeamSequence}
    mu_delivres, ecarts, ecarts_ouverts = {}, [], []

    for bloc in faisceaux:
        lames, machoires, bras, collimateur = echantillonner(
            seance, bloc["cibles"] * facteur)
        lames_dicom = utilitaires.mlc_dd2dcm(lames)
        machoires_dicom = utilitaires.jaw_dd2dcm(machoires)

        cible = par_numero[bloc["numero"]]
        avant = []
        for index, cp in enumerate(cible.ControlPointSequence):
            for position in getattr(cp, "BeamLimitingDevicePositionSequence", []):
                if position.RTBeamLimitingDeviceType == "MLCX":
                    avant.append(np.array(position.LeafJawPositions, dtype=float))
                    position.LeafJawPositions = lames_dicom[index]
                elif position.RTBeamLimitingDeviceType in ("ASYMY", "Y"):
                    position.LeafJawPositions = machoires_dicom[index]
            if "GantryAngle" in cp:
                cp.GantryAngle = f"{bras[index]:.4f}"
            if "BeamLimitingDeviceAngle" in cp:
                cp.BeamLimitingDeviceAngle = f"{collimateur[index]:.4f}"

        mu_delivres[bloc["numero"]] = bloc["mu"] * facteur
        if avant:
            apres = np.array([np.array(v, dtype=float) for v in lames_dicom[:len(avant)]])
            avant = np.array(avant)
            ecart_lames = np.abs(apres - avant)
            ecarts.append(ecart_lames)
            ecarts_ouverts.append(ecart_lames[paires_ouvertes(avant)])

    # Les MU réellement délivrées, là où elles vivent réellement.
    for groupe in delivre.FractionGroupSequence:
        if int(getattr(groupe, "FractionGroupNumber", 1)) != args.fraction:
            continue
        for reference in groupe.ReferencedBeamSequence:
            numero = int(reference.ReferencedBeamNumber)
            if numero in mu_delivres:
                reference.BeamMeterset = f"{mu_delivres[numero]:.4f}"

    # Un identifiant neuf : ce fichier ne doit jamais pouvoir écraser le plan.
    delivre.SOPInstanceUID = generate_uid()
    delivre.SeriesInstanceUID = generate_uid()
    if "RTPlanLabel" in delivre:
        delivre.RTPlanLabel = (str(delivre.RTPlanLabel) or "")[:10] + "_DEL"
    if "RTPlanName" in delivre:
        delivre.RTPlanName = (str(delivre.RTPlanName) or "")[:54] + " (delivre)"
    delivre.ApprovalStatus = "UNAPPROVED"
    delivre.RTPlanDescription = (
        f"Reconstitue depuis {seance['fichiers']} log(s) machine par "
        f"{'consigne servo' if args.consigne else 'position mesuree'}. "
        f"Document d'analyse, non traitable.")[:64]

    sortie = args.sortie or str(
        pathlib.Path(args.plan).with_suffix("")) + "_delivre.dcm"
    delivre.save_as(sortie, enforce_file_format=False)
    print(f"\nÉcrit : {sortie}")
    print(f"  SOP Instance UID neuf · ApprovalStatus UNAPPROVED")

    relu = pydicom.dcmread(sortie, force=True)
    boucle = Delivery.from_dicom(relu, args.fraction)
    print(f"  relecture : {len(relu.BeamSequence)} faisceau(x), "
          f"Delivery.from_dicom ✅ {float(boucle.monitor_units[-1]):.1f} MU")

    if ecarts:
        tous = np.concatenate([e.ravel() for e in ecarts])
        ouverts = np.concatenate([e.ravel() for e in ecarts_ouverts])
        source = "consigne servo" if args.consigne else "position mesurée"
        print(f"\nÉcart prévu / {source}, sur la grille du plan :")
        print(f"  lames, toutes        : médiane {np.median(tous):.2f} mm · "
              f"p95 {np.percentile(tous, 95):.2f} mm · max {tous.max():.2f} mm")
        print(f"  lames dans le champ  : médiane {np.median(ouverts):.2f} mm · "
              f"p95 {np.percentile(ouverts, 95):.2f} mm · "
              f"{100 * len(ouverts) / len(tous):.0f} % des lames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
