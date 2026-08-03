"""Produit un RT Plan DICOM « délivré » pour chaque séance d'un plan.

    python3 exploration/seance_vers_dicom.py plan.dcm "SDD+xxx.zip"
    python3 exploration/seance_vers_dicom.py plan.dcm seance/ --consigne
    python3 exploration/seance_vers_dicom.py plan.dcm "SDD+xxx.zip" --sortie delivres/

On lui donne un plan et une source de logs — une archive SDD, un dossier, des
`.trf` en vrac. Il découpe la source en séances, retient celles qui
correspondent au plan, et écrit **un DICOM par séance retenue**. Un plan délivré
en cinq fractions donne cinq fichiers, un par fraction.

Chaque fichier a la structure exacte du plan d'origine — mêmes faisceaux, mêmes
points de contrôle — mais les positions de lames, de mâchoires, les angles de
bras et de collimateur, et les MU, sont **ceux que la machine a relevés**.

⚠️ Ces fichiers ne sont pas traitables et ne doivent jamais repartir vers un R&V
ou un accélérateur. Chacun porte un SOP Instance UID neuf (il n'écrasera donc
jamais le plan d'origine), `ApprovalStatus = UNAPPROVED`, et un libellé suffixé.
Ce sont des documents d'analyse.

Comment une séance est retenue
------------------------------
Deux critères, tous deux décisifs, repris de `comparer_rtp_seance.py` :

  MU totales       à 1 % près. Mesuré : un vrai appariement tombe à 0,1 %.
  Dessin du champ  les 160 lames confrontées en cinq points de la délivrance,
                   sur l'axe des MU cumulées. Mesuré sur les données publiques :
                   0,4 mm entre un plan et sa séance, 12,8 mm face à un autre
                   traitement. Le seuil est à 3 mm, largement entre les deux.

Le nom de champ n'est pas comparé : le TPS et le log ne le nomment pas
forcément pareil. La machine non plus : le log la désigne par son numéro de
série, le plan par le nom du TPS.

Substituer plutôt que reconstruire
----------------------------------
Repartir des logs pour fabriquer un plan de zéro échoue en VMAT : `to_dicom` de
pymedphys segmente par angle de bras et refuse un arc, et on obtiendrait de
toute façon vingt fois trop de points de contrôle (2 717 échantillons pour 111
points). On garde donc la grille du plan et on y injecte le mesuré, interpolé
sur l'axe des MU cumulées — le seul axe commun aux deux fichiers, le plan
ignorant le temps et le log ignorant les points de contrôle du plan. C'est
aussi l'architecture du précédent publié (PMC10018669).
"""

import argparse
import copy
import datetime
import pathlib
import sys
import tempfile

import numpy as np
import pydicom
import pymedphys
from pydicom.uid import generate_uid
from pymedphys import Delivery

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from comparer_rtp_seance import (SONDAGES, accorder_lames,  # noqa: E402
                                 lames_du_log, lames_du_plan)
from organiser_trf import TrfIllisible, parcourir, regrouper  # noqa: E402
from organiser_trf import resumer as resumer_trf  # noqa: E402
from trf_vers_dicom_vmat import corriger_numpy2  # noqa: E402

PAIRES = 80
SEUIL_MU = 1.0        # % d'écart toléré sur le total de MU
SEUIL_DESSIN = 3.0    # mm d'écart médian toléré sur le dessin du champ


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


def plan_depuis_dicom(plan, fraction):
    """Met le plan DICOM dans la forme qu'attend l'empreinte de champ.

    `comparer_rtp_seance` a été écrit pour des plans RTP ; ses fonctions ne
    demandent en réalité que des MU cumulées et deux bancs de lames par point de
    contrôle. On les lui fournit depuis le DICOM plutôt que d'écrire un second
    comparateur qu'il faudrait valider à son tour.

    Les positions n'y sont écrites que quand elles changent : on reporte donc la
    dernière connue, sinon la moitié des points de contrôle d'un arc n'auraient
    pas de lames.
    """
    metersets = {}
    for groupe in plan.FractionGroupSequence:
        if int(getattr(groupe, "FractionGroupNumber", 1)) != fraction:
            continue
        for reference in groupe.ReferencedBeamSequence:
            if "BeamMeterset" in reference:
                metersets[int(reference.ReferencedBeamNumber)] = \
                    float(reference.BeamMeterset)

    faisceaux = []
    for faisceau in plan.BeamSequence:
        numero = int(faisceau.BeamNumber)
        if numero not in metersets:
            continue
        total, finale = metersets[numero], float(faisceau.FinalCumulativeMetersetWeight)
        courant, points = None, []
        for cp in faisceau.ControlPointSequence:
            for item in getattr(cp, "BeamLimitingDevicePositionSequence", []):
                if item.RTBeamLimitingDeviceType == "MLCX":
                    courant = [float(v) for v in item.LeafJawPositions]
            if courant is None:
                continue
            points.append({
                "mu": total * float(cp.CumulativeMetersetWeight) / finale,
                "lames_a": courant[:PAIRES],
                "lames_b": courant[PAIRES:],
            })
        faisceaux.append({"mu": total, "points_de_controle": points})
    return {"faisceaux": faisceaux}


def decouper(sources, filtre, ecart_max, seuil_complet):
    """Découpe la source en séances, avec toute la logique d'`organiser_trf`.

    Le découpage s'appuie sur l'état final que la machine inscrit elle-même dans
    chaque fichier, pas sur un simple seuil de durée : sur les données de
    référence, le plus petit intervalle entre deux séances était plus court que
    le plus grand intervalle à l'intérieur d'une séance.
    """
    resumes, contenus = [], {}
    for nom, _, octets in parcourir(sources):
        if filtre and filtre not in nom:
            continue
        try:
            resumes.append(resumer_trf(octets, nom))
            contenus[nom] = octets
        except TrfIllisible as erreur:
            print(f"  ⚠ {nom} illisible : {erreur}", file=sys.stderr)
        except Exception as erreur:  # un fichier abîmé n'arrête pas le reste
            print(f"  ⚠ {nom} : {type(erreur).__name__}: {erreur}", file=sys.stderr)
    if not resumes:
        raise SystemExit("Aucun fichier TRF exploitable.")

    seances = []
    for s in regrouper(resumes, ecart_max, seuil_complet):
        if s.get("issue") == "sans_dose":
            continue
        fichiers = [(nom, contenus[nom]) for nom in s["fichiers"].split(" | ")
                    if nom in contenus]
        if fichiers:
            seances.append((s, fichiers))
    return seances


def lire_seance(fichiers, consigne=False):
    """Concatène les fichiers d'une séance en une délivrance continue.

    Une séance interrompue s'écrit en plusieurs fichiers dont le compteur de MU
    repart de zéro ; à l'intérieur d'un fichier, il repart aussi à chaque
    faisceau. `Delivery.from_trf` neutralise déjà les remises à zéro internes
    (il cumule les écarts positifs) ; il reste à décaler chaque fichier du total
    des précédents pour obtenir un axe de MU continu sur toute la séance.
    """
    morceaux, decalage = [], 0.0
    with tempfile.TemporaryDirectory() as dossier:
        for rang, (nom, octets) in enumerate(fichiers):
            chemin = pathlib.Path(dossier) / f"{rang:03d}.trf"
            chemin.write_bytes(octets)
            _, table = pymedphys.trf.read(str(chemin))
            livraison = Delivery._from_pandas(
                consigne_du_servo(table) if consigne else table)
            mu = np.asarray(livraison.monitor_units, dtype=float)
            morceaux.append({
                "mu": mu + decalage,
                "mlc": np.asarray(livraison.mlc, dtype=float),
                "jaw": np.asarray(livraison.jaw, dtype=float),
                "bras": np.asarray(livraison.gantry, dtype=float),
                "collimateur": np.asarray(livraison.collimator, dtype=float),
                "faisceaux": 1 + int((np.diff(
                    table["Step Dose/Actual Value (Mu)"].values) < 0).sum()),
            })
            decalage += float(mu.max())

    return {
        "mu": np.concatenate([m["mu"] for m in morceaux]),
        "mlc": np.concatenate([m["mlc"] for m in morceaux]),
        "jaw": np.concatenate([m["jaw"] for m in morceaux]),
        "bras": np.concatenate([m["bras"] for m in morceaux]),
        "collimateur": np.concatenate([m["collimateur"] for m in morceaux]),
        "octets": [o for _, o in fichiers],
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
            "faisceau": faisceau, "numero": numero, "mu": mu,
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
    ecartees = (dicom[:, n:] - dicom[:, :n]) > 5.0
    return np.hstack([ecartees, ecartees])


def confronter(empreinte_plan, seance, mu_plan):
    """Écart de MU et écart de dessin entre le plan et une séance."""
    mu_log = float(seance["mu"].max())
    ecart_mu = 100 * (mu_log - mu_plan) / mu_plan if mu_plan else 0.0

    empreinte_log = lames_du_log(seance["octets"], SONDAGES)
    if empreinte_log is None or empreinte_plan is None:
        return mu_log, ecart_mu, None, ""
    dessin, convention = accorder_lames(empreinte_plan, empreinte_log)
    return mu_log, ecart_mu, dessin, convention


def fabriquer(plan, faisceaux, mu_plan, seance, chemin, fraction,
              consigne, utilitaires):
    """Écrit un DICOM délivré pour une séance. Rend l'écart aux lames."""
    mu_log = float(seance["mu"].max())
    # Le plan est rejoué sur l'axe réellement parcouru : si la délivrance s'est
    # arrêtée avant la fin, interpoler aux MU absolues du plan prolongerait la
    # dernière valeur mesurée sans le dire.
    facteur = mu_log / mu_plan if mu_plan else 1.0

    delivre = copy.deepcopy(plan)
    par_numero = {int(f.BeamNumber): f for f in delivre.BeamSequence}
    mu_delivres, ecarts, ouverts = {}, [], []

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
            avant = np.array(avant)
            apres = np.array([np.array(v, dtype=float)
                              for v in lames_dicom[:len(avant)]])
            ecart = np.abs(apres - avant)
            ecarts.append(ecart)
            ouverts.append(ecart[paires_ouvertes(avant)])

    # Les MU réellement délivrées, là où elles vivent réellement.
    for groupe in delivre.FractionGroupSequence:
        if int(getattr(groupe, "FractionGroupNumber", 1)) != fraction:
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
        f"Reconstitue depuis {seance['fichiers']} log(s) par "
        f"{'consigne servo' if consigne else 'position mesuree'}. "
        f"Analyse, non traitable.")[:64]

    delivre.save_as(chemin, enforce_file_format=False)

    relu = pydicom.dcmread(chemin, force=True)
    boucle = Delivery.from_dicom(relu, fraction)
    tous = np.concatenate([e.ravel() for e in ecarts]) if ecarts else np.array([0.])
    dans_champ = np.concatenate([e.ravel() for e in ouverts]) if ouverts else tous
    return {
        "mu_relu": float(boucle.monitor_units[-1]),
        "median": float(np.median(dans_champ)),
        "p95": float(np.percentile(dans_champ, 95)),
        "median_tous": float(np.median(tous)),
        "part": 100 * len(dans_champ) / len(tous),
    }


def main():
    analyseur = argparse.ArgumentParser(
        description="Produit un RT Plan « délivré » par séance correspondant au plan.",
        epilog="Les fichiers produits sont des documents d'analyse : ils ne sont "
               "pas traitables et ne doivent jamais repartir vers un R&V.")
    analyseur.add_argument("plan", help="RT Plan DICOM d'origine")
    analyseur.add_argument("sources", nargs="+",
                           help="archive SDD, dossier, ou fichiers .trf")
    analyseur.add_argument("--sortie", metavar="DOSSIER",
                           help="où écrire (défaut : à côté du plan)")
    analyseur.add_argument("--fraction", type=int, default=1,
                           help="groupe de fractions à utiliser (défaut : 1)")
    analyseur.add_argument("--filtre", help="ne garder que les .trf dont le nom contient ceci")
    analyseur.add_argument("--consigne", action="store_true",
                           help="écrire la consigne du servomoteur (position + erreur) "
                                "au lieu de la position réellement atteinte")
    analyseur.add_argument("--tout", action="store_true",
                           help="écrire aussi les séances écartées")
    analyseur.add_argument("--seuil-dessin", type=float, default=SEUIL_DESSIN,
                           help=f"mm d'écart toléré sur le dessin (défaut : {SEUIL_DESSIN})")
    analyseur.add_argument("--ecart-max", type=float, default=1800,
                           help="pour le découpage en séances (défaut : 1800 s)")
    analyseur.add_argument("--seuil-complet", type=float, default=0.97,
                           help="pour le découpage en séances (défaut : 0.97)")
    args = analyseur.parse_args()

    utilitaires = corriger_numpy2()
    plan = pydicom.dcmread(args.plan, force=True)
    if "BeamSequence" not in plan:
        raise SystemExit(f"{args.plan} n'est pas un RT Plan.")

    faisceaux, mu_plan = grille_du_plan(plan, args.fraction)
    points = sum(len(f["cibles"]) for f in faisceaux)
    print(f"Plan    {pathlib.Path(args.plan).name}")
    print(f"        {len(faisceaux)} faisceau(x) · {points} points de contrôle "
          f"· {mu_plan:.1f} MU")

    empreinte_plan = lames_du_plan(plan_depuis_dicom(plan, args.fraction), SONDAGES)

    seances = decouper(args.sources, args.filtre, args.ecart_max, args.seuil_complet)
    print(f"\n{len(seances)} séance(s) trouvée(s) dans la source\n")

    base = pathlib.Path(args.plan)
    dossier = pathlib.Path(args.sortie) if args.sortie else base.parent
    dossier.mkdir(parents=True, exist_ok=True)

    retenues, ecartees = [], []
    for resume, fichiers in seances:
        try:
            seance = lire_seance(fichiers, args.consigne)
        except Exception as erreur:
            print(f"  séance {resume['seance']:>4} · illisible : {erreur}")
            continue

        mu_log, ecart_mu, dessin, convention = confronter(
            empreinte_plan, seance, mu_plan)
        accord = (abs(ecart_mu) <= SEUIL_MU
                  and dessin is not None and dessin <= args.seuil_dessin)
        debut = resume.get("debut_local") or resume["debut_utc"]
        etiquette = (f"  séance {resume['seance']:>4} · {debut[:16]} · "
                     f"{resume['nb_fichiers']} fichier(s) · {mu_log:7.1f} MU "
                     f"({ecart_mu:+6.2f} %) · dessin "
                     + (f"{dessin:6.2f} mm" if dessin is not None else "   —   "))

        if not accord and not args.tout:
            print(etiquette + "  ✗ écartée")
            ecartees.append(resume)
            continue

        # Le numéro de séance fait partie du nom : deux séances peuvent démarrer
        # dans la même seconde — sur deux machines, ou si un même fichier figure
        # deux fois dans l'archive — et l'une écraserait l'autre sans rien dire.
        horodatage = debut.replace(":", "").replace("-", "").replace(" ", "_")[:15]
        chemin = dossier / f"{base.stem}_delivre_{horodatage}_s{resume['seance']:04d}.dcm"
        stats = fabriquer(plan, faisceaux, mu_plan, seance, str(chemin),
                          args.fraction, args.consigne, utilitaires)
        marque = "✅" if accord else "⚠ "
        print(etiquette + f"  {marque} {chemin.name}")
        print(f"           lames dans le champ : médiane {stats['median']:.2f} mm · "
              f"p95 {stats['p95']:.2f} mm · {stats['part']:.0f} % des lames · "
              f"relu {stats['mu_relu']:.1f} MU")
        retenues.append(chemin)

    print(f"\n{len(retenues)} fichier(s) écrit(s) dans {dossier}/")
    if retenues:
        print("  SOP Instance UID neuf · ApprovalStatus UNAPPROVED · non traitables")
    if ecartees and not args.tout:
        print(f"  {len(ecartees)} séance(s) écartée(s) — « --tout » les écrit aussi")
    if not retenues:
        print("  Aucune séance ne correspond au plan. Vérifier que l'archive couvre")
        print("  bien la période du traitement, ou relâcher --seuil-dessin.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
