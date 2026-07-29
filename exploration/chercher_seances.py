"""Retrouve, dans une arborescence de séances, toutes celles qui correspondent
à un plan RTP.

    python3 exploration/chercher_seances.py plan.rtp seances/

L'arborescence attendue est celle que produit `organiser_trf.py --extraire` :
un dossier par séance, contenant ses fichiers `.trf`. N'importe quel dossier
contenant des `.trf` fait l'affaire — le regroupement se fait par répertoire.

Pourquoi chercher plutôt que comparer
-------------------------------------
Un plan n'est pas délivré une fois : il l'est à chaque fraction. Chercher les
séances qui lui correspondent donne donc l'historique des délivrances, pas une
réponse binaire. C'est aussi le seul moyen de repérer qu'une fraction manque.

Le classement s'appuie sur l'empreinte décrite dans `comparer_rtp_seance.py` :
machine et total de MU sont décisifs, le nombre de points de contrôle est fort,
le nom de champ n'est qu'indicatif — deux systèmes ne nomment pas forcément un
champ de la même façon.
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from comparer_rtp_seance import NON, OK, comparer, lire_seance  # noqa: E402
from lire_rtp import extraire as lire_rtp  # noqa: E402


def rassembler(racine):
    """Groupe les .trf par dossier : un dossier, une séance."""
    racine = Path(racine)
    if racine.is_file():
        return {racine.parent: [racine]}

    par_dossier = defaultdict(list)
    for chemin in sorted(racine.rglob("*.trf")):
        par_dossier[chemin.parent].append(chemin)
    return dict(par_dossier)


def juger(criteres):
    """Réduit les critères à un verdict et un score de classement."""
    decisifs_ko = [c for c in criteres if c.poids == "decisif" and c.verdict != OK]
    portants = [c for c in criteres if c.poids != "indicatif"]
    portants_ok = [c for c in portants if c.verdict == OK]

    if decisifs_ko:
        verdict = "non"
    elif len(portants_ok) == len(portants):
        verdict = "correspond"
    else:
        verdict = "doute"

    ecart_mu = 1e9
    for c in criteres:
        if c.nom == "MU totales":
            try:
                ecart_mu = abs(float(c.log) - float(c.plan))
            except (TypeError, ValueError):
                pass
    return verdict, len(portants_ok), ecart_mu


def main():
    analyseur = argparse.ArgumentParser(
        description="Retrouve les séances correspondant à un plan RTP.",
    )
    analyseur.add_argument("rtp", help="plan exporté depuis Mosaiq (.rtp)")
    analyseur.add_argument("racine", help="dossier des séances (ou un .trf isolé)")
    analyseur.add_argument("--tout", action="store_true",
                           help="afficher aussi les séances écartées")
    args = analyseur.parse_args()

    plan = lire_rtp(args.rtp)
    mu_plan = sum(f.get("mu") or 0 for f in plan["faisceaux"])
    cp_plan = sum(len(f["points_de_controle"]) for f in plan["faisceaux"])
    noms = sorted({f.get("nom", "") for f in plan["faisceaux"] if f.get("nom")})
    sites = sorted({f.get("site", "") for f in plan["faisceaux"] if f.get("site")})

    print(f"\n  PLAN  {Path(args.rtp).name}")
    print(f"        {len(plan['faisceaux'])} faisceau(x) · {mu_plan:.1f} MU · "
          f"{cp_plan} points de contrôle")
    if noms:
        print(f"        champ(s) : {', '.join(noms)}")
    if sites:
        print(f"        site     : {', '.join(sites)}")

    dossiers = rassembler(args.racine)
    if not dossiers:
        raise SystemExit(f"Aucun fichier .trf sous {args.racine}")
    print(f"\n  {len(dossiers)} séance(s) à examiner sous {args.racine}\n")

    resultats = []
    for dossier, fichiers in dossiers.items():
        try:
            seance = lire_seance([str(f) for f in fichiers])
        except SystemExit:
            continue
        criteres = comparer(plan, seance)
        verdict, score, ecart = juger(criteres)
        resultats.append({
            "dossier": dossier, "seance": seance, "criteres": criteres,
            "verdict": verdict, "score": score, "ecart": ecart,
        })

    retenus = [r for r in resultats if r["verdict"] != "non"]
    retenus.sort(key=lambda r: r["seance"]["debut_local"])

    if not retenus:
        print(f"  {NON} Aucune séance ne correspond à ce plan.")
        proches = sorted(resultats, key=lambda r: r["ecart"])[:3]
        if proches:
            print("\n  Les plus proches en MU, pour situer :")
            for r in proches:
                s_ = r["seance"]
                print(f"    {s_['debut_local'][:16]}  {s_['champ_nom'][:22]:<24} "
                      f"{s_['mu']:>8.1f} MU  (plan : {mu_plan:.1f})")
    else:
        print(f"  {OK} {len(retenus)} séance(s) correspondent — "
              f"vraisemblablement autant de fractions.\n")
        print(f"    {'date (locale)':<18} {'champ':<20} {'fich.':>5} {'MU':>9} "
              f"{'écart':>8}  ")
        print(f"    {'-' * 68}")
        for r in retenus:
            s_ = r["seance"]
            marque = OK if r["verdict"] == "correspond" else "⚠️ "
            print(f"    {s_['debut_local'][:16]:<18} {s_['champ_nom'][:19]:<20} "
                  f"{len(s_['fichiers']):>5} {s_['mu']:>9.1f} "
                  f"{s_['mu'] - mu_plan:>+8.1f}  {marque}")

        dates = sorted({r["seance"]["debut_local"][:10] for r in retenus})
        print(f"\n    du {dates[0]} au {dates[-1]} · {len(dates)} jour(s) distinct(s)")
        multi = [r for r in retenus if len(r["seance"]["fichiers"]) > 1]
        if multi:
            print(f"    dont {len(multi)} séance(s) interrompue(s) puis reprise(s)")
        doutes = [r for r in retenus if r["verdict"] == "doute"]
        if doutes:
            print(f"    {len(doutes)} avec une réserve — relancer "
                  f"comparer_rtp_seance.py dessus pour le détail")

    if args.tout:
        ecartes = [r for r in resultats if r["verdict"] == "non"]
        if ecartes:
            print(f"\n  Séances écartées ({len(ecartes)}) :")
            for r in sorted(ecartes, key=lambda r: r["ecart"])[:20]:
                s_ = r["seance"]
                motifs = ", ".join(c.nom.lower() for c in r["criteres"]
                                   if c.poids == "decisif" and c.verdict != OK)
                print(f"    {s_['debut_local'][:16]}  {s_['champ_nom'][:20]:<22} "
                      f"{s_['mu']:>8.1f} MU  → {motifs}")
            if len(ecartes) > 20:
                print(f"    … et {len(ecartes) - 20} autres")

    print("\n  Cette recherche établit une identité de traitement, pas une "
          "conformité de\n  délivrance : aucune position de lame n'est comparée.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
