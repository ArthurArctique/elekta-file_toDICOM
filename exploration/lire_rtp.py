"""Lit un fichier RTP Connect exporté par Mosaiq et en extrait ce qu'il faut
pour comparer au log machine : MU par faisceau, et par point de contrôle les
MU, l'angle de bras, les mâchoires et les positions de lames.

    python3 exploration/lire_rtp.py plan.rtp
    python3 exploration/lire_rtp.py plan.rtp --json sortie.json

Le RTP n'est pas du DICOM : c'est du texte, une ligne par enregistrement,
champs séparés par des virgules et guillemetés, avec une somme de contrôle en
fin de ligne.

Disposition des enregistrements d'après l'implémentation de référence
(github.com/dicom/rtp-connect). Le nombre de champs dépend de la version de
Mosaiq : un `CONTROL_PT_DEF` compte 232 champs jusqu'à la 2.62 et 235 à partir
de la 2.64, qui ajoute `iso_pos_x/y/z`. Comme les 200 positions de lames
viennent *après* ces champs, se tromper de version décale tout le tableau —
d'où la détection automatique plutôt qu'une valeur en dur.

Rien n'est supposé des conventions : `mu_convention` et `scale_convention` sont
relevés et affichés, et la nature des MU (cumulées, relatives ou par segment)
est déduite des valeurs elles-mêmes.
"""

import argparse
import csv
import json
import sys
from collections import defaultdict

# Positions dans un CONTROL_PT_DEF, avant les tableaux de lames.
CP = {
    "field_id": 1, "mlc_type": 2, "mlc_leaves": 3, "total_control_points": 4,
    "control_pt_number": 5, "mu_convention": 6, "monitor_units": 7,
    "energy": 9, "doserate": 10, "ssd": 11, "scale_convention": 12,
    "gantry_angle": 13, "gantry_dir": 14, "collimator_angle": 15,
    "collimator_x1": 19, "collimator_x2": 20,
    "collimator_y1": 23, "collimator_y2": 24,
}
NB_SCALAIRES_2_64 = 35   # keyword … iso_pos_z
NB_SCALAIRES_2_62 = 32   # idem sans les trois iso_pos
LAMES_PAR_BANC = 100     # le format en réserve 100, même si le MLC en a moins

# Le RTP exprime traditionnellement les longueurs en centimètres, là où DICOM
# les donne en millimètres. Rien ne le garantissant, l'unité est déduite de
# l'amplitude des positions plutôt que supposée.

FD = {
    "rx_site_name": 1, "field_name": 2, "field_id": 3, "field_dose": 5,
    "field_monitor_units": 6, "treatment_machine": 8, "modality": 10,
    "energy": 11, "arc_direction": 32, "arc_start_angle": 33,
    "arc_stop_angle": 34,
}


def nombre(valeur):
    try:
        return float(str(valeur).strip().strip('"'))
    except (TypeError, ValueError):
        return None


def texte(valeur):
    return str(valeur).strip().strip('"')


def lire(chemin):
    """Rend les enregistrements groupés par mot-clé."""
    groupes = defaultdict(list)
    with open(chemin, encoding="latin-1", newline="") as fichier:
        for ligne in csv.reader(fichier):
            if ligne:
                groupes[texte(ligne[0])].append(ligne)
    return groupes


def disposition_lames(nb_champs):
    """Déduit de la largeur d'un enregistrement où commencent les lames."""
    for scalaires, version in ((NB_SCALAIRES_2_64, "2.64+"), (NB_SCALAIRES_2_62, "≤ 2.62")):
        if nb_champs in (scalaires + 2 * LAMES_PAR_BANC, scalaires + 2 * LAMES_PAR_BANC + 1):
            return scalaires, version
    return None, None


def nature_des_mu(valeurs, total_faisceau):
    """Les MU d'un point de contrôle : cumulées, relatives, ou par segment ?"""
    if not valeurs:
        return "indéterminée", None
    croissant = all(b >= a for a, b in zip(valeurs, valeurs[1:]))
    dernier, somme = valeurs[-1], sum(valeurs)
    if croissant and abs(dernier - 1.0) < 0.01:
        return "poids cumulé (0 → 1)", lambda v: v * total_faisceau
    if croissant and total_faisceau and abs(dernier - total_faisceau) < 0.05 * total_faisceau:
        return "MU cumulées", lambda v: v
    if total_faisceau and abs(somme - total_faisceau) < 0.05 * total_faisceau:
        return "MU par segment", None
    return "indéterminée", None


def extraire(chemin):
    groupes = lire(chemin)
    if "CONTROL_PT_DEF" not in groupes:
        raise SystemExit("Aucun point de contrôle dans ce fichier.")

    largeur = len(groupes["CONTROL_PT_DEF"][0])
    debut_lames, version = disposition_lames(largeur)
    if debut_lames is None:
        raise SystemExit(
            f"Largeur d'enregistrement inattendue : {largeur} champs. "
            f"Attendu 232/233 (Mosaiq ≤ 2.62) ou 235/236 (2.64+)."
        )

    plan = {
        "fichier": chemin,
        "version_estimee": version,
        "champs_par_point": largeur,
        "debut_des_lames": debut_lames,
        "faisceaux": [],
    }

    premier = groupes["CONTROL_PT_DEF"][0]
    plan["mlc_type"] = texte(premier[CP["mlc_type"]])
    plan["mlc_leaves"] = nombre(premier[CP["mlc_leaves"]])
    plan["mu_convention"] = texte(premier[CP["mu_convention"]])
    plan["scale_convention"] = texte(premier[CP["scale_convention"]])

    par_faisceau = defaultdict(list)
    for ligne in groupes["CONTROL_PT_DEF"]:
        par_faisceau[texte(ligne[CP["field_id"]])].append(ligne)

    totaux = {}
    for ligne in groupes.get("FIELD_DEF", []):
        totaux[texte(ligne[FD["field_id"]])] = {
            "nom": texte(ligne[FD["field_name"]]),
            "site": texte(ligne[FD["rx_site_name"]]),
            "machine": texte(ligne[FD["treatment_machine"]]),
            "modalite": texte(ligne[FD["modality"]]),
            "energie": texte(ligne[FD["energy"]]),
            "mu": nombre(ligne[FD["field_monitor_units"]]),
            "dose": nombre(ligne[FD["field_dose"]]),
            "arc": texte(ligne[FD["arc_direction"]]),
        }

    for identifiant, lignes in par_faisceau.items():
        lignes.sort(key=lambda l: nombre(l[CP["control_pt_number"]]) or 0)
        entete = totaux.get(identifiant, {})
        total = entete.get("mu")

        brutes = [nombre(l[CP["monitor_units"]]) for l in lignes]
        brutes = [v for v in brutes if v is not None]
        libelle, convertir = nature_des_mu(brutes, total)

        points = []
        for ligne in lignes:
            brut = nombre(ligne[CP["monitor_units"]])
            a = [nombre(v) for v in ligne[debut_lames:debut_lames + LAMES_PAR_BANC]]
            b = [nombre(v) for v in
                 ligne[debut_lames + LAMES_PAR_BANC:debut_lames + 2 * LAMES_PAR_BANC]]
            points.append({
                "numero": nombre(ligne[CP["control_pt_number"]]),
                "mu_brut": brut,
                "mu": convertir(brut) if (convertir and brut is not None) else brut,
                "gantry": nombre(ligne[CP["gantry_angle"]]),
                "gantry_sens": texte(ligne[CP["gantry_dir"]]),
                "collimateur": nombre(ligne[CP["collimator_angle"]]),
                "machoires_x": [nombre(ligne[CP["collimator_x1"]]),
                                nombre(ligne[CP["collimator_x2"]])],
                "machoires_y": [nombre(ligne[CP["collimator_y1"]]),
                                nombre(ligne[CP["collimator_y2"]])],
                "lames_a": a,
                "lames_b": b,
            })

        plan["faisceaux"].append({
            "id": identifiant, "nature_mu": libelle,
            "points_de_controle": points, **entete,
        })

    return plan


def resumer(plan):
    print(f"=== {plan['fichier']} ===")
    print(f"  version Mosaiq estimée : {plan['version_estimee']} "
          f"({plan['champs_par_point']} champs par point de contrôle)")
    print(f"  MLC : type {plan['mlc_type']}, {plan['mlc_leaves']:.0f} lames déclarées "
          f"(le format en réserve {LAMES_PAR_BANC} par banc)")
    print(f"  conventions : MU = {plan['mu_convention']}, échelle = {plan['scale_convention']}")
    print(f"  {len(plan['faisceaux'])} faisceau(x)\n")

    for faisceau in plan["faisceaux"]:
        points = faisceau["points_de_controle"]
        gantries = [p["gantry"] for p in points if p["gantry"] is not None]
        arc = len({round(g) for g in gantries}) > 5
        print(f"  faisceau {faisceau['id']} · « {faisceau.get('nom', '?')} » · "
              f"machine {faisceau.get('machine', '?')}")
        print(f"    {len(points)} points de contrôle · {faisceau.get('mu')} MU · "
              f"{faisceau.get('energie', '?')} · {'ARC' if arc else 'gantry fixe'}")
        print(f"    MU par point : {faisceau['nature_mu']}")
        if gantries:
            print(f"    gantry {min(gantries):.1f}° → {max(gantries):.1f}° "
                  f"({len({round(g) for g in gantries})} angles distincts)")

        actives = [i for i in range(LAMES_PAR_BANC)
                   if any(p["lames_a"][i] is not None for p in points)]
        if actives:
            toutes = [v for p in points for i in actives
                      for v in (p["lames_a"][i], p["lames_b"][i]) if v is not None]
            ampleur = max(abs(min(toutes)), abs(max(toutes)))
            # Un champ de traitement dépasse rarement 20 cm de demi-ouverture :
            # au-delà d'une centaine, les positions sont forcément en millimètres.
            unite = ("centimètres" if ampleur < 30 else
                     "millimètres" if ampleur > 60 else "à vérifier")
            print(f"    lames renseignées : {len(actives)} paires sur {LAMES_PAR_BANC} "
                  f"· positions de {min(toutes):.1f} à {max(toutes):.1f} "
                  f"→ unité probable : {unite}")
            if unite == "à vérifier":
                print("      ⚠ amplitude ambiguë : confronter à l'ouverture réelle du champ")
        # contrôle de cohérence : les MU reconstruites retombent-elles sur le total ?
        mus = [p["mu"] for p in points if p["mu"] is not None]
        if mus and faisceau.get("mu"):
            ecart = mus[-1] - faisceau["mu"]
            marque = "✅" if abs(ecart) < max(0.5, 0.01 * faisceau["mu"]) else "⚠"
            print(f"    {marque} dernier point à {mus[-1]:.1f} MU pour un total "
                  f"annoncé de {faisceau['mu']:.1f} (écart {ecart:+.1f})")
        print()


def main():
    analyseur = argparse.ArgumentParser(
        description="Lit un RTP Connect exporté par Mosaiq.",
        epilog="Le résumé ne contient pas de nom de patient, mais les noms de "
               "champ et de site sont ré-identifiants : à traiter comme tels.",
    )
    analyseur.add_argument("fichier")
    analyseur.add_argument("--json", metavar="SORTIE",
                           help="écrit le plan décodé au format JSON")
    args = analyseur.parse_args()

    plan = extraire(args.fichier)
    resumer(plan)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as sortie:
            json.dump(plan, sortie, ensure_ascii=False, indent=1)
        print(f"Écrit : {args.json}")
        print("  ⚠ contient les noms de champ et de site du plan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
