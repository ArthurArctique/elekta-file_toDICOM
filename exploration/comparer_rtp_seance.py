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

from lire_rtp import extraire as lire_rtp  # noqa: E402
from organiser_trf import TrfIllisible, resumer as resumer_trf  # noqa: E402

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

    return criteres


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
          "pas sur\n  la conformité de la délivrance. Aucune position de lame "
          "n'est comparée ici.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
