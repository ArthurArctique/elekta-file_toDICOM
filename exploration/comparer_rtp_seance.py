"""Confronte un plan RTP à une séance du log machine, pour vérifier qu'on
travaille bien sur le même traitement avant toute comparaison géométrique.

    python3 exploration/comparer_rtp_seance.py plan.rtp seance/*.trf

Les fichiers d'une séance se lisent dans la colonne `fichiers` de
`seances.csv`, ou se trouvent déjà regroupés si `organiser_trf.py` a été
lancé avec `--extraire`.

Ce que ce script fait, et ne fait pas
-------------------------------------
Il vérifie une **identité**, pas une conformité. Il répond à « ce plan et ce
log parlent-ils du même traitement ? », pas à « la délivrance est-elle
conforme ». Aucune position de lame n'est comparée ici.

Le raisonnement s'appuie sur une empreinte : la machine, le nom de champ, le
nombre de faisceaux, le total de MU, le nombre de points de contrôle et
l'étendue de l'arc. Pris isolément aucun de ces critères ne prouve grand-chose,
mais leur conjonction est très discriminante — deux traitements différents
partagent rarement le même total de MU à la dizaine près *et* le même nombre
de points de contrôle.

Le total de MU est le critère le plus fort : mesuré sur les données de
référence, log et plan concordaient à 0,1 MU près.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from lire_rtp import LAMES_PAR_BANC  # noqa: E402
from lire_rtp import extraire as lire_rtp  # noqa: E402
from organiser_trf import (COL_MU, VERSIONS, TrfIllisible,  # noqa: E402
                           extraire_colonne, lire_entete)
from organiser_trf import resumer as resumer_trf  # noqa: E402

NB_PAIRES = 80          # un Agility : 80 paires de lames
SONDAGES = (0.15, 0.35, 0.55, 0.75, 0.92)   # où sonder la délivrance, en fraction de MU

OK, DOUTE, NON = "✅", "⚠️ ", "❌"


class Critere:
    """Un point de comparaison, avec son poids dans le verdict.

    Tous ne se valent pas : deux systèmes peuvent parfaitement nommer un champ
    différemment sans que ce soit un traitement différent, alors qu'un total de
    MU qui ne tombe pas est rédhibitoire.
    """

    def __init__(self, nom, plan, log, verdict, note="", poids="fort"):
        self.nom, self.plan, self.log = nom, plan, log
        self.verdict, self.note, self.poids = verdict, note, poids


def _lames_brutes(chemin):
    """MU cumulées et positions des 160 lames d'un fichier."""
    octets = Path(chemin).read_bytes()
    entete = lire_entete(octets)
    v = VERSIONS[entete["version"]]
    taille = v["echelle"] * entete["nb_colonnes"] * 2 + v["prefixe"]
    corps = octets[entete["fin_entete"]:]
    lignes = len(corps) // taille
    index = {nom: i for i, nom in enumerate(entete["colonnes"])}

    def colonne(nom):
        if nom not in index:
            return None
        return extraire_colonne(corps, lignes, taille, v["prefixe"],
                                v["echelle"] * 2, index[nom]) / 10.0

    mu = colonne(COL_MU)
    if mu is None or mu.max() <= 0:
        return None

    bancs = []
    for prefixe in ("Y1", "Y2"):
        banc = []
        for i in range(1, NB_PAIRES + 1):
            col = colonne(f"{prefixe} Leaf {i}/Scaled Actual (mm)")
            if col is None:
                return None
            banc.append(col)
        bancs.append(np.array(banc))          # (80, n_lignes)

    return mu, np.array(bancs)                # (2, 80, n_lignes)


def lames_du_log(chemins, fractions):
    """Positions des 160 lames du log, aux fractions de MU demandées.

    Le compteur de MU sert d'axe : c'est le seul commun au plan et au log.
    Une séance interrompue s'étale sur plusieurs fichiers dont les compteurs
    repartent de zéro : il faut les recoller avant de sonder, sans quoi on
    échantillonne le début de l'arc en croyant en parcourir la totalité.
    """
    morceaux, decalage = [], 0.0
    for chemin in sorted(chemins):
        lu = _lames_brutes(chemin)
        if lu is None:
            continue
        mu, bancs = lu
        morceaux.append((mu + decalage, bancs))
        decalage += float(mu.max())
    if not morceaux:
        return None

    mu = np.concatenate([m for m, _ in morceaux])
    bancs = np.concatenate([b for _, b in morceaux], axis=2)
    ordre = np.argsort(mu, kind="stable")
    mu, bancs = mu[ordre], bancs[:, :, ordre]

    cibles = [f * mu.max() for f in fractions]
    return np.array([
        [np.interp(cibles, mu, bancs[banc, i]) for i in range(NB_PAIRES)]
        for banc in range(2)
    ])                                        # (2, 80, n_sondages)


def lames_du_plan(plan, fractions):
    """Positions de lames du plan, aux mêmes fractions de MU cumulées."""
    faisceau = max(plan["faisceaux"], key=lambda f: f.get("mu") or 0)
    points = faisceau["points_de_controle"]
    mus = np.array([p["mu"] if p["mu"] is not None else 0.0 for p in points])
    if mus.max() <= 0:
        return None

    a = np.array([[v if v is not None else np.nan for v in p["lames_a"][:NB_PAIRES]]
                  for p in points])           # (n_cp, 80)
    b = np.array([[v if v is not None else np.nan for v in p["lames_b"][:NB_PAIRES]]
                  for p in points])
    if np.isnan(a).all() or np.isnan(b).all():
        return None

    # Le RTP travaille en centimètres, le log en millimètres.
    ampleur = np.nanmax(np.abs(np.concatenate([a, b])))
    facteur = 10.0 if ampleur < 30 else 1.0

    cibles = [f * mus.max() for f in fractions]
    return np.array([
        [np.interp(cibles, mus, np.nan_to_num(banc[:, i])) * facteur
         for i in range(NB_PAIRES)]
        for banc in (a, b)
    ])


def accorder_lames(plan_lames, log_lames):
    """Confronte les deux dessins de champ, sans présumer des conventions.

    L'ordre des bancs et le sens de numérotation des lames diffèrent d'un
    système à l'autre — s'être trompé là-dessus donnait 48 mm d'écart au lieu
    de 0,2. On essaie donc les quatre combinaisons et on retient la meilleure.
    """
    meilleur, description = None, ""
    for bancs_inverses in (False, True):
        for lames_inversees in (False, True):
            candidat = log_lames[::-1] if bancs_inverses else log_lames
            if lames_inversees:
                candidat = candidat[:, ::-1, :]
            ecart = float(np.median(np.abs(candidat - plan_lames)))
            if meilleur is None or ecart < meilleur:
                meilleur = ecart
                description = ("bancs échangés · " if bancs_inverses else "") + \
                              ("lames inversées" if lames_inversees else "ordre direct")
    return meilleur, description


def lire_seance(chemins):
    """Résume les fichiers d'une séance, comme le fait l'inventaire."""
    fichiers = []
    for chemin in chemins:
        octets = Path(chemin).read_bytes()
        try:
            fichiers.append(resumer_trf(octets, Path(chemin).name))
        except TrfIllisible as erreur:
            print(f"  ⚠ {Path(chemin).name} illisible : {erreur}", file=sys.stderr)
    if not fichiers:
        raise SystemExit("Aucun fichier de log exploitable.")

    fichiers.sort(key=lambda f: f["debut_utc"])
    gantry = [f[cle] for f in fichiers for cle in ("gantry_min", "gantry_max")
              if f.get(cle) is not None]
    # Le compteur de points de controle repart a 1 dans chaque fichier, comme
    # celui des MU : sur une seance reconstituee, il faut sommer les fragments
    # et non prendre le maximum.
    cps = [f["cp_max"] for f in fichiers if f.get("cp_max") is not None]

    return {
        "fichiers": fichiers,
        "machine": fichiers[0]["machine"],
        "champ_nom": fichiers[0]["champ_nom"],
        "champ_etiquette": fichiers[0]["champ_etiquette"],
        "mu": round(sum(f.get("mu") or 0 for f in fichiers), 1),
        "faisceaux": max((f.get("faisceaux") or 0) for f in fichiers),
        "gantry_min": min(gantry) if gantry else None,
        "gantry_max": max(gantry) if gantry else None,
        "cp_total": sum(cps) if cps else None,
        "debut_local": fichiers[0]["debut_local"],
        "fin_local": fichiers[-1]["fin_local"],
        "interrompue": len(fichiers) > 1,
        "chemins": [str(c) for c in chemins],
    }


def comparer(plan, seance):
    criteres = []

    # --- machine ---
    machines = {f.get("machine", "") for f in plan["faisceaux"] if f.get("machine")}
    machine_plan = ", ".join(sorted(machines)) or "—"
    concorde = seance["machine"] in machines
    criteres.append(Critere(
        "Machine", machine_plan, seance["machine"],
        OK if concorde else (DOUTE if not machines else NON),
        "" if concorde else "le plan ne désigne pas cette machine",
        poids="decisif",
    ))

    # --- nom de champ ---
    noms = {f.get("nom", "") for f in plan["faisceaux"] if f.get("nom")}
    nom_log = seance["champ_nom"]
    exact = nom_log in noms
    approche = any(nom_log.lower() in n.lower() or n.lower() in nom_log.lower()
                   for n in noms if n)
    criteres.append(Critere(
        "Nom de champ", ", ".join(sorted(noms)) or "—", nom_log,
        OK if exact else DOUTE,
        "" if exact else (
            "correspondance partielle" if approche else
            "noms différents — Monaco et le log ne suivent pas forcément la "
            "même convention, ce seul écart ne conclut rien"),
        poids="indicatif",
    ))

    # --- nombre de faisceaux ---
    n_plan = len(plan["faisceaux"])
    n_log = seance["faisceaux"]
    criteres.append(Critere(
        "Faisceaux", n_plan, n_log,
        OK if n_plan == n_log else DOUTE,
        "" if n_plan == n_log else
        "une séance interrompue répartit un faisceau sur plusieurs fichiers"
        if seance["interrompue"] else "comptes différents",
        poids="moyen",
    ))

    # --- total de MU : le critère le plus discriminant ---
    mu_plan = sum(f.get("mu") or 0 for f in plan["faisceaux"])
    mu_log = seance["mu"]
    ecart = mu_log - mu_plan
    relatif = abs(ecart) / mu_plan if mu_plan else 1.0
    criteres.append(Critere(
        "MU totales", f"{mu_plan:.1f}", f"{mu_log:.1f}",
        OK if relatif < 0.01 else (DOUTE if relatif < 0.05 else NON),
        f"écart {ecart:+.1f} MU ({relatif:.1%})",
        poids="decisif",
    ))

    # --- points de contrôle ---
    cp_plan = sum(len(f["points_de_controle"]) for f in plan["faisceaux"])
    cp_log = seance["cp_total"]
    if cp_log is not None:
        ecart_cp = cp_log - cp_plan
        proche = abs(ecart_cp) <= max(2, 0.05 * cp_plan)
        if proche and ecart_cp:
            note = ("le compteur machine est 1-based, le plan 0-based" if not
                    seance["interrompue"] else
                    "les fragments se recouvrent légèrement aux reprises")
        elif proche:
            note = ""
        else:
            note = "structures différentes"
        criteres.append(Critere(
            "Points de contrôle", cp_plan, f"{cp_log} (écart {ecart_cp:+d})",
            OK if proche else DOUTE, note, poids="fort",
        ))

    # --- étendue de l'arc ---
    angles = [p["gantry"] for f in plan["faisceaux"]
              for p in f["points_de_controle"] if p["gantry"] is not None]
    if angles and seance["gantry_min"] is not None:
        arc_plan = max(angles) - min(angles)
        arc_log = seance["gantry_max"] - seance["gantry_min"]
        proche = abs(arc_plan - arc_log) < 15
        criteres.append(Critere(
            "Étendue du bras",
            f"{min(angles):.0f}° → {max(angles):.0f}° ({arc_plan:.0f}°)",
            f"{seance['gantry_min']:.0f}° → {seance['gantry_max']:.0f}° ({arc_log:.0f}°)",
            OK if proche else DOUTE,
            "" if proche else "les conventions d'origine des angles peuvent différer",
            poids="moyen",
        ))

    # --- angle de collimateur : souvent propre au plan, et gratuit à comparer ---
    cols_plan = {round(p["collimateur"]) for f in plan["faisceaux"]
                 for p in f["points_de_controle"] if p["collimateur"] is not None}
    if cols_plan and seance.get("collimateur") is not None:
        col_log = round(seance["collimateur"])
        proche = any(abs(c - col_log) <= 2 or abs(abs(c - col_log) - 360) <= 2
                     for c in cols_plan)
        criteres.append(Critere(
            "Collimateur", "°, ".join(str(c) for c in sorted(cols_plan)) + "°",
            f"{col_log}°", OK if proche else DOUTE,
            "" if proche else "angles différents",
            poids="moyen",
        ))

    # --- le dessin du champ : l'empreinte la plus discriminante ---
    if seance.get("lames_ecart") is not None:
        ecart = seance["lames_ecart"]
        criteres.append(Critere(
            "Dessin du champ", "160 lames", f"écart médian {ecart:.1f} mm",
            # Mesuré : 0,4 mm entre un plan et sa propre séance, 13,8 mm
            # face à un autre traitement. La frontière est large.
            OK if ecart < 3 else (DOUTE if ecart < 10 else NON),
            seance.get("lames_note", ""),
            poids="decisif",
        ))

    return criteres


def enrichir(plan, seance):
    """Ajoute à la séance ce qui demande de relire les colonnes de lames."""
    try:
        log = lames_du_log(seance["chemins"], SONDAGES)
    except Exception:
        log = None
    attendu = lames_du_plan(plan, SONDAGES)
    if log is None or attendu is None:
        return
    ecart, description = accorder_lames(attendu, log)
    seance["lames_ecart"] = ecart
    seance["lames_note"] = f"conventions retenues : {description}"


def main():
    analyseur = argparse.ArgumentParser(
        description="Vérifie qu'un plan RTP et une séance du log désignent bien "
                    "le même traitement.",
    )
    analyseur.add_argument("rtp", help="plan exporté depuis Mosaiq (.rtp)")
    analyseur.add_argument("trf", nargs="+", help="fichier(s) de log de la séance")
    args = analyseur.parse_args()

    plan = lire_rtp(args.rtp)
    seance = lire_seance(args.trf)
    enrichir(plan, seance)

    print(f"\n  PLAN   {Path(args.rtp).name}")
    print(f"         {len(plan['faisceaux'])} faisceau(x) · "
          f"{sum(f.get('mu') or 0 for f in plan['faisceaux']):.1f} MU")
    sites = {f.get("site", "") for f in plan["faisceaux"] if f.get("site")}
    if sites:
        print(f"         site : {', '.join(sorted(sites))}")

    print(f"\n  SÉANCE {len(seance['fichiers'])} fichier(s) · machine "
          f"{seance['machine']} · {seance['mu']:.1f} MU")
    print(f"         {seance['debut_local']} → {seance['fin_local'][11:]} (heure locale)")
    if seance["interrompue"]:
        print(f"         séance reconstituée à partir de plusieurs fichiers")

    criteres = comparer(plan, seance)
    largeur = max(len(c.nom) for c in criteres)

    print(f"\n  {'':<{largeur}}   {'plan':<28} {'log':<24}")
    print(f"  {'-' * (largeur + 58)}")
    for c in criteres:
        print(f"  {c.nom:<{largeur}}   {str(c.plan)[:27]:<28} {str(c.log)[:23]:<24} {c.verdict}")
        if c.note:
            print(f"  {'':<{largeur}}   → {c.note}")

    decisifs_ko = [c for c in criteres if c.poids == "decisif" and c.verdict != OK]
    portants = [c for c in criteres if c.poids != "indicatif"]
    portants_ok = [c for c in portants if c.verdict == OK]
    print()
    if decisifs_ko:
        motifs = ", ".join(c.nom.lower() for c in decisifs_ko)
        print(f"  {NON} Contradiction sur un critère décisif ({motifs}) — ce plan "
              f"ne correspond pas à cette séance.")
    elif len(portants_ok) == len(portants):
        indicatifs_ko = [c for c in criteres
                         if c.poids == "indicatif" and c.verdict != OK]
        print(f"  {OK} Tous les critères déterminants concordent : même "
              f"traitement, selon toute vraisemblance.")
        if indicatifs_ko:
            print(f"     ({', '.join(c.nom.lower() for c in indicatifs_ko)} "
                  f"diffère, mais ce critère ne tranche rien)")
    else:
        print(f"  {DOUTE}{len(portants_ok)}/{len(portants)} critères déterminants "
              f"concordent. Regarder les réserves avant de conclure.")

    print("\n  Rappel : cette vérification porte sur l'identité du traitement, "
          "pas sur\n  sa conformité. Les lames sont confrontées en cinq points "
          "de la délivrance,\n  ce qui suffit à reconnaître un champ — pas à "
          "juger de sa précision.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
