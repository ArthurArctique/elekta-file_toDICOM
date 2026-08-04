"""Examine la colonne `exces` de `seances.csv` pour savoir si le chaînage tient.

    python3 exploration/diagnostic_cumuls.py rapport/seances.csv
    python3 exploration/diagnostic_cumuls.py rapport/ --marge 0.05

`organiser_trf.py` signale les séances dont le cumul de MU dépasse le total
habituel du champ : c'est la signature d'une délivrance partielle chaînée à une
reprise **complète**. Ce script regarde la distribution de ces écarts, parce
qu'un signalement isolé et un signalement absent ne veulent pas dire la même
chose.

Le piège que ce script sert à détecter
--------------------------------------
La référence est la **médiane** des cumuls de séance du champ. Si les chaînages
abusifs sont fréquents *pour un même champ*, la médiane monte avec eux et
l'excès s'annule : le contrôle devient aveugle exactement là où il y aurait le
plus à voir. Dans ce cas ce sont les **bonnes** séances qui ressortent, en excès
négatif. Un champ dont les excès se répartissent en deux paquets — un vers 0 et
un vers −40 % — est bien plus inquiétant qu'un champ à +40 % isolé.

Aucune donnée patient n'est lue : seuls le nom de champ et des nombres.
"""

import argparse
import collections
import csv
import pathlib
import sys

import numpy as np

TRANCHES = ((-1.00, -0.20, "≤ −20 %"), (-0.20, -0.05, "−20 à −5 %"),
            (-0.05, 0.05, "−5 à +5 %"), (0.05, 0.20, "+5 à +20 %"),
            (0.20, 1e9, "> +20 %"))


def lire(chemin):
    chemin = pathlib.Path(chemin)
    if chemin.is_dir():
        chemin = chemin / "seances.csv"
    if not chemin.exists():
        raise SystemExit(f"Introuvable : {chemin}")
    with open(chemin, encoding="utf-8-sig", newline="") as fichier:
        lignes = list(csv.DictReader(fichier, delimiter=";"))
    if lignes and "exces" not in lignes[0]:
        raise SystemExit(
            f"{chemin} ne contient pas la colonne « exces ». Ce rapport a été "
            "produit par une version antérieure : relancer organiser_trf.py.")
    return chemin, lignes


def main():
    analyseur = argparse.ArgumentParser(
        description="Examine la distribution des excès de cumul de MU.")
    analyseur.add_argument("source", help="seances.csv, ou le dossier qui le contient")
    analyseur.add_argument("--marge", type=float, default=0.05,
                           help="seuil de signalement employé (défaut : 0.05)")
    analyseur.add_argument("--combien", type=int, default=15,
                           help="nombre de champs détaillés (défaut : 15)")
    args = analyseur.parse_args()

    chemin, lignes = lire(args.source)
    avec_doute = [l for l in lignes if l.get("doute")]
    par_champ = collections.defaultdict(list)
    tous = []
    for ligne in lignes:
        if not ligne["exces"]:
            continue
        valeur = float(ligne["exces"])
        tous.append(valeur)
        par_champ[ligne["champ_nom"]].append(valeur)

    print(f"\n  {chemin}")
    print(f"  {len(lignes)} séance(s) · {len(avec_doute)} avec doute · "
          f"{len(tous)} avec un excès calculé "
          f"({len(lignes) - len(tous)} sans, champ vu moins de 3 fois)")
    if not tous:
        print("\n  Aucun excès calculable : trop peu de séances par champ.")
        return 0

    print("\n  RÉPARTITION DES EXCÈS")
    for bas, haut, libelle in TRANCHES:
        n = sum(1 for v in tous if bas <= v < haut)
        barre = "█" * round(40 * n / len(tous))
        print(f"    {libelle:>12}  {n:>5}  {barre}")

    print(f"\n  CHAMPS LES PLUS ATYPIQUES (sur {len(par_champ)})")
    print(f"    {'champ':<26} {'n':>3} {'min':>8} {'médiane':>9} {'max':>8}")
    classement = sorted(par_champ.items(),
                        key=lambda x: -max(abs(min(x[1])), abs(max(x[1]))))
    for champ, valeurs in classement[: args.combien]:
        print(f"    {champ[:26]:<26} {len(valeurs):>3} {min(valeurs):>+8.2f} "
              f"{np.median(valeurs):>+9.2f} {max(valeurs):>+8.2f}")

    # --- lecture ---
    excessifs = [v for v in tous if v > args.marge]
    creux = [v for v in tous if v < -0.20]
    suspects = [c for c, v in par_champ.items()
                if len(v) >= 3 and min(v) < -0.20 and np.median(v) > -0.05]

    print("\n  LECTURE")
    if not excessifs and not creux:
        print("    Tous les excès tiennent dans la marge : le chaînage est propre.")
    if excessifs:
        print(f"    {len(excessifs)} séance(s) au-dessus de {args.marge:+.0%} — "
              "délivrance partielle chaînée à une reprise complète.")
        print("      Les vérifier dans seances.csv, colonne « fichiers ».")
    if suspects:
        print(f"    ⚠ {len(suspects)} champ(s) où la médiane est probablement "
              "contaminée :")
        for champ in suspects[:8]:
            valeurs = par_champ[champ]
            print(f"        {champ[:26]:<26} {len(valeurs)} séances, "
                  f"min {min(valeurs):+.2f} pour une médiane à "
                  f"{np.median(valeurs):+.2f}")
        print("      Les séances en creux sont alors les BONNES, et la référence")
        print("      devrait être le minimum des cumuls plutôt que leur médiane.")
    elif creux:
        print(f"    {len(creux)} séance(s) sous −20 % : délivrances incomplètes,")
        print("      normal si elles portent déjà un doute d'interruption.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
