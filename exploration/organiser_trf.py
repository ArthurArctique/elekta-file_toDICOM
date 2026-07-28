"""Inventorie et regroupe en séances les fichiers TRF d'une sauvegarde SDD.

Ne lit que les métadonnées d'en-tête et quelques colonnes du corps : aucune
donnée patient n'est extraite, aucun fichier n'est déplacé ni modifié.

    python3 exploration/organiser_trf.py SDD+2020-04-28.zip
    python3 exploration/organiser_trf.py /chemin/vers/dossier --sortie rapport/
    python3 exploration/organiser_trf.py *.zip --ecart-max 1800

Produit deux tableaux CSV :
  fichiers.csv  une ligne par TRF, avec ses métadonnées
  seances.csv   une ligne par séance reconstituée

Pourquoi ce n'est pas trivial
-----------------------------
Une séance interrompue puis reprise s'écrit en **plusieurs fichiers**, chacun
avec son compteur de MU repartant de zéro. Un simple seuil de durée ne suffit
pas à les distinguer de deux séances distinctes : sur les données de référence,
le plus petit intervalle entre deux séances (94 s) était plus court que le plus
grand intervalle à l'intérieur d'une séance (162 s).

La règle retenue s'appuie donc sur le cumul de MU. Sur une semaine, un même
champ est délivré plusieurs fois ; le total le plus élevé observé sert de
référence. Une séance reste ouverte tant que son cumul n'atteint pas ce total.

Limite à connaître : cette référence ne vaut que ce que vaut le lot. Si un champ
n'apparaît qu'en fragments — un traitement commencé en fin de semaine, par
exemple — la référence sera sous-estimée et les séances mal découpées. La colonne
`completude` de `seances.csv` sert à repérer ces cas. Le jour où les RT Plan
seront disponibles, leur `BeamMeterset` remplacera avantageusement cette
estimation.

Deux détails du format, établis par la mesure et non par la documentation :
  - la date de l'en-tête marque la **fin** de l'enregistrement, pas le début ;
  - le champ `mu` de l'en-tête est en **dixièmes** de MU.
"""

import argparse
import csv
import datetime
import json
import pathlib
import re
import sys
import zipfile

import numpy as np

# Barème d'encodage : octets par valeur, préfixe de ligne, facteur d'échelle.
VERSIONS = {
    1: {"taille": 2, "prefixe": 0, "echelle": 1},
    2: {"taille": 2, "prefixe": 8, "echelle": 1},
    3: {"taille": 2, "prefixe": 8, "echelle": 1},
    4: {"taille": 4, "prefixe": 8, "echelle": 2},
}

MOTIF_DATE = re.compile(rb"^\d\d[/-]\d\d[/-]\d\d \d\d:\d\d:\d\d Z$")

COL_MU = "Step Dose/Actual Value (Mu)"
COL_GANTRY = "Step Gantry/Scaled Actual (deg)"
COL_ETAT = "Linac State/Actual Value (None)"
COL_CP = "Control point/Actual Value (None)"

PAS = 0.04  # 25 Hz


def charger_noms_colonnes():
    """Dictionnaire code → nom de colonne, emprunté à pymedphys s'il est là."""
    try:
        import pymedphys

        chemin = (
            pathlib.Path(pymedphys.__file__).parent / "_trf" / "decode" / "config.json"
        )
        return json.loads(chemin.read_text())["item_part_names"]
    except Exception:
        return {}


NOMS = charger_noms_colonnes()


class TrfIllisible(Exception):
    pass


def lire_chaine(octets, position):
    longueur = octets[position]
    texte = octets[position + 1 : position + 1 + longueur].decode("ascii", "replace")
    return texte, position + 1 + longueur


def lire_entete(octets):
    """Décode l'en-tête. Lève TrfIllisible si ça n'a pas la forme attendue."""
    if len(octets) < 64:
        raise TrfIllisible("fichier trop court")

    position = 0
    date, position = lire_chaine(octets, position)
    fuseau, position = lire_chaine(octets, position)
    champ, position = lire_chaine(octets, position)
    machine, position = lire_chaine(octets, position)

    if not MOTIF_DATE.match(date.encode("ascii", "replace")):
        raise TrfIllisible(f"date d'en-tête inattendue : {date[:30]!r}")

    mu_dixiemes = np.frombuffer(octets, np.float64, 1, position)[0]
    version = int(np.frombuffer(octets, np.int32, 1, position + 8)[0])
    nb_colonnes = int(np.frombuffer(octets, np.int32, 1, position + 12)[0])

    if version not in VERSIONS:
        raise TrfIllisible(f"version d'encodage {version} inconnue")
    if not 0 < nb_colonnes < 5000:
        raise TrfIllisible(f"nombre de colonnes aberrant : {nb_colonnes}")

    schema = np.frombuffer(octets, np.int16, nb_colonnes * 2, position + 16)
    colonnes = [
        NOMS.get(f"{schema[i]}_{schema[i + 1]}", f"{schema[i]}_{schema[i + 1]}")
        for i in range(0, len(schema), 2)
    ]

    etiquette, _, nom_champ = champ.partition("/")
    if not nom_champ:
        etiquette, nom_champ = "", champ

    return {
        "fin_entete": position + 16 + nb_colonnes * 4,
        "date": date,
        "fuseau": fuseau,
        "champ_etiquette": etiquette,
        "champ_nom": nom_champ,
        "machine": machine,
        "mu_entete": mu_dixiemes / 10.0,
        "version": version,
        "nb_colonnes": nb_colonnes,
        "colonnes": colonnes,
    }


def extraire_colonne(corps, nb_lignes, taille_ligne, prefixe, taille, indice):
    """Extrait une colonne sans décoder tout le tableau."""
    grille = np.frombuffer(corps, np.uint8, nb_lignes * taille_ligne).reshape(
        nb_lignes, taille_ligne
    )
    debut = prefixe + indice * taille
    tranche = np.ascontiguousarray(grille[:, debut : debut + taille])
    return tranche.view(np.int16 if taille == 2 else np.int32).ravel().astype(float)


def resumer(octets, nom_fichier):
    """Métadonnées d'un TRF, sans décoder les 350 colonnes."""
    entete = lire_entete(octets)
    v = VERSIONS[entete["version"]]
    taille_ligne = v["echelle"] * entete["nb_colonnes"] * 2 + v["prefixe"]
    corps = octets[entete["fin_entete"] :]
    nb_lignes = len(corps) // taille_ligne
    if nb_lignes < 1:
        raise TrfIllisible("aucune ligne de données")

    resume = {
        "fichier": nom_fichier,
        "machine": entete["machine"],
        "version": entete["version"],
        "champ_etiquette": entete["champ_etiquette"],
        "champ_nom": entete["champ_nom"],
        "fuseau": entete["fuseau"],
        "echantillons": nb_lignes,
        "duree_s": round(nb_lignes * PAS, 2),
        "octets_par_ligne": taille_ligne,
        "reste_octets": len(corps) - nb_lignes * taille_ligne,
        "mu_entete": round(entete["mu_entete"], 1),
    }

    index = {nom: i for i, nom in enumerate(entete["colonnes"])}
    resume["colonnes_inconnues"] = [c for c in entete["colonnes"] if re.fullmatch(r"-?\d+_-?\d+", c)]

    def colonne(nom):
        if nom not in index:
            return None
        return extraire_colonne(
            corps, nb_lignes, taille_ligne, v["prefixe"], v["echelle"] * 2, index[nom]
        )

    # Le compteur de MU repart à zéro à chaque faisceau : un fichier qui en
    # contient plusieurs doit être sommé faisceau par faisceau, sinon on ne
    # relève que le plus gros d'entre eux.
    mu = colonne(COL_MU)
    if mu is not None:
        mu = mu / 10.0
        ruptures = np.where(np.diff(mu) < 0)[0]
        segments = [float(mu[i]) for i in ruptures] + [float(mu[-1])]
        resume["mu"] = round(sum(segments), 1)
        resume["mu_max_faisceau"] = round(max(segments), 1)
        resume["faisceaux"] = len(segments)
    else:
        resume["mu"] = resume["mu_max_faisceau"] = resume["faisceaux"] = None

    # L'état machine sert à ne juger la géométrie que pendant l'irradiation :
    # entre deux faisceaux, le bras tourne et fausserait la détection d'arc.
    etat = colonne(COL_ETAT)
    irradie = etat == 42 if etat is not None else None  # 42 = « Radiation On »
    if irradie is not None:
        resume["part_irradiation"] = round(float(irradie.mean()), 3)

    gantry = colonne(COL_GANTRY)
    if gantry is not None:
        gantry = gantry / 10.0
        resume["gantry_debut"] = round(float(gantry[0]), 1)
        resume["gantry_fin"] = round(float(gantry[-1]), 1)
        resume["gantry_min"] = round(float(gantry.min()), 1)
        resume["gantry_max"] = round(float(gantry.max()), 1)
        utile = gantry[irradie] if irradie is not None and irradie.any() else gantry
        angles = np.unique(np.round(utile))
        resume["angles_irradies"] = len(angles)
        resume["arc"] = len(angles) > 10

    cp = colonne(COL_CP)
    if cp is not None:
        resume["cp_min"] = int(cp.min())
        resume["cp_max"] = int(cp.max())

    # L'horodatage machine (versions ≥ 2) donne la vraie durée, au cas où
    # des échantillons manqueraient.
    if v["prefixe"] >= 8 and nb_lignes > 1:
        grille = np.frombuffer(corps, np.uint8, nb_lignes * taille_ligne).reshape(
            nb_lignes, taille_ligne
        )
        ms = np.ascontiguousarray(grille[:, :8]).view(np.uint64).ravel().astype(float)
        resume["duree_s"] = round((ms[-1] - ms[0]) / 1000.0, 2)
        pas = np.diff(ms)
        resume["coupures"] = int((pas > 60).sum())

    # Contrôle d'intégrité gratuit : le total de MU annoncé par l'en-tête doit
    # retomber sur celui recalculé depuis le corps du fichier.
    if resume["mu"] is not None:
        resume["ecart_mu_entete"] = round(resume["mu"] - resume["mu_entete"], 1)

    fin = datetime.datetime.strptime(entete["date"], "%y/%m/%d %H:%M:%S Z")
    debut = fin - datetime.timedelta(seconds=resume["duree_s"])
    resume["debut_utc"] = debut.isoformat(sep=" ")
    resume["fin_utc"] = fin.isoformat(sep=" ")

    return resume


def parcourir(chemins):
    """Rend (nom, octets) pour chaque .trf trouvé, dans les zip comme sur disque."""
    for chemin in chemins:
        p = pathlib.Path(chemin)
        if p.is_dir():
            for f in sorted(p.rglob("*.trf")):
                yield str(f.relative_to(p)), f.read_bytes()
        elif p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as archive:
                for nom in sorted(archive.namelist()):
                    if nom.lower().endswith(".trf"):
                        yield f"{p.name}::{nom}", archive.read(nom)
        elif p.suffix.lower() == ".trf":
            yield p.name, p.read_bytes()
        else:
            print(f"  ignoré (ni zip, ni dossier, ni .trf) : {p}", file=sys.stderr)


def regrouper(resumes, ecart_max_s, seuil_complet):
    """Chaîne les fichiers en séances.

    Une séance reste ouverte tant que son cumul de MU n'atteint pas le total de
    référence du champ — le plus élevé observé sur l'ensemble du lot.
    """
    reference = {}
    for r in resumes:
        if r.get("mu") is None:
            continue
        cle = (r["machine"], r["champ_nom"])
        reference[cle] = max(reference.get(cle, 0.0), r["mu"])

    ordonnes = sorted(resumes, key=lambda r: (r["machine"], r["debut_utc"]))
    seances, courante = [], None

    for r in ordonnes:
        cle = (r["machine"], r["champ_nom"])
        total_attendu = reference.get(cle, 0.0)
        raison = None

        if courante is None:
            raison = "premier fichier"
        elif courante["machine"] != r["machine"]:
            raison = "machine différente"
        elif courante["champ_nom"] != r["champ_nom"]:
            raison = "champ différent"
        else:
            ecart = (
                datetime.datetime.fromisoformat(r["debut_utc"])
                - datetime.datetime.fromisoformat(courante["fin_utc"])
            ).total_seconds()
            if ecart > ecart_max_s:
                raison = f"écart de {ecart / 60:.0f} min"
            elif total_attendu and courante["mu_cumul"] >= seuil_complet * total_attendu:
                raison = "la séance précédente était complète"

        if raison:
            courante = {
                "seance": len(seances) + 1,
                "machine": r["machine"],
                "champ_nom": r["champ_nom"],
                "champ_etiquette": r["champ_etiquette"],
                "debut_utc": r["debut_utc"],
                "fin_utc": r["fin_utc"],
                "fichiers": [r["fichier"]],
                "nb_fichiers": 1,
                "mu_cumul": r.get("mu") or 0.0,
                "mu_reference": round(total_attendu, 1),
                "ouverture": raison,
            }
            seances.append(courante)
        else:
            courante["fichiers"].append(r["fichier"])
            courante["nb_fichiers"] += 1
            courante["mu_cumul"] += r.get("mu") or 0.0
            courante["fin_utc"] = r["fin_utc"]

        r["seance"] = courante["seance"]

    for s in seances:
        s["mu_cumul"] = round(s["mu_cumul"], 1)
        ref = s["mu_reference"]
        s["completude"] = round(s["mu_cumul"] / ref, 3) if ref else None
        s["fichiers"] = " | ".join(s["fichiers"])
        s["doute"] = ""
        if s["nb_fichiers"] > 1:
            s["doute"] = "reconstituée à partir de plusieurs fichiers"
        if ref and s["mu_cumul"] < seuil_complet * ref:
            s["doute"] = (
                f"cumul incomplet ({s['completude']:.0%} de la référence) — "
                "séance tronquée, ou référence mal estimée"
            )

    return seances


def ecrire_csv(chemin, lignes, colonnes):
    with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
        ecrivain = csv.DictWriter(f, fieldnames=colonnes, delimiter=";", extrasaction="ignore")
        ecrivain.writeheader()
        for ligne in lignes:
            ecrivain.writerow(ligne)


def main():
    analyseur = argparse.ArgumentParser(
        description="Inventorie et regroupe en séances les TRF d'une sauvegarde SDD.",
        epilog="Aucune donnée patient n'est extraite. Aucun fichier n'est modifié.",
    )
    analyseur.add_argument("sources", nargs="+", help="zip SDD, dossier, ou .trf")
    analyseur.add_argument("--sortie", default=".", help="dossier des CSV (défaut : .)")
    analyseur.add_argument(
        "--ecart-max", type=float, default=1800,
        help="au-delà de cet écart en secondes, on ouvre une nouvelle séance (défaut : 1800)",
    )
    analyseur.add_argument(
        "--seuil-complet", type=float, default=0.97,
        help="fraction du total de référence à partir de laquelle une séance est "
             "réputée complète (défaut : 0.97)",
    )
    args = analyseur.parse_args()

    resumes, illisibles = [], []
    for n, (nom, octets) in enumerate(parcourir(args.sources), 1):
        if n % 250 == 0:
            print(f"  … {n} fichiers lus", file=sys.stderr, flush=True)
        try:
            resumes.append(resumer(octets, nom))
        except TrfIllisible as e:
            illisibles.append((nom, str(e)))
        except Exception as e:  # un fichier abîmé ne doit pas tout arrêter
            illisibles.append((nom, f"{type(e).__name__}: {e}"))

    if not resumes:
        print("Aucun fichier TRF exploitable trouvé.", file=sys.stderr)
        return 1

    seances = regrouper(resumes, args.ecart_max, args.seuil_complet)

    sortie = pathlib.Path(args.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    cols_fichiers = [
        "seance", "fichier", "machine", "version", "champ_etiquette", "champ_nom",
        "debut_utc", "fin_utc", "duree_s", "echantillons", "mu", "mu_entete",
        "ecart_mu_entete", "faisceaux", "mu_max_faisceau", "part_irradiation",
        "gantry_debut", "gantry_fin", "gantry_min", "gantry_max",
        "angles_irradies", "arc", "cp_min", "cp_max", "coupures",
        "octets_par_ligne", "reste_octets", "fuseau",
    ]
    cols_seances = [
        "seance", "machine", "champ_etiquette", "champ_nom", "debut_utc", "fin_utc",
        "nb_fichiers", "mu_cumul", "mu_reference", "completude", "ouverture",
        "doute", "fichiers",
    ]
    ecrire_csv(sortie / "fichiers.csv", sorted(resumes, key=lambda r: r["debut_utc"]), cols_fichiers)
    ecrire_csv(sortie / "seances.csv", seances, cols_seances)

    machines = sorted({r["machine"] for r in resumes})
    dates = sorted(r["debut_utc"][:10] for r in resumes)
    multi = [s for s in seances if s["nb_fichiers"] > 1]
    doutes = [s for s in seances if s["doute"]]

    print(f"\n{len(resumes)} fichiers TRF lus, {len(illisibles)} illisibles")
    print(f"{len(seances)} séances reconstituées")
    print(f"  machines : {', '.join(machines)}")
    print(f"  période  : {dates[0]} → {dates[-1]}")
    print(f"  séances en plusieurs fichiers : {len(multi)}")
    print(f"  séances à vérifier           : {len(doutes)}")

    versions = {}
    for r in resumes:
        versions[r["version"]] = versions.get(r["version"], 0) + 1
    print(f"  versions d'encodage : "
          + ", ".join(f"v{v} ({n})" for v, n in sorted(versions.items())))

    tronques = [r for r in resumes if r.get("reste_octets")]
    if tronques:
        print(f"  ⚠ {len(tronques)} fichier(s) avec des octets en trop en fin de fichier")
    coupes = [r for r in resumes if r.get("coupures")]
    if coupes:
        print(f"  ⚠ {len(coupes)} fichier(s) avec une coupure d'échantillonnage")
    discordants = [r for r in resumes if abs(r.get("ecart_mu_entete") or 0) > 1.0]
    if discordants:
        print(f"  ⚠ {len(discordants)} fichier(s) où les MU du corps et de l'en-tête "
              f"divergent de plus de 1 MU — décodage à vérifier")
    inconnues = sorted({c for r in resumes for c in r.get("colonnes_inconnues", [])})
    if inconnues:
        print(f"  ⚠ {len(inconnues)} code(s) de colonne absent(s) du dictionnaire : "
              + ", ".join(inconnues[:6]))

    if illisibles:
        print("\nFichiers non décodés :")
        for nom, motif in illisibles[:10]:
            print(f"  {nom} — {motif}")
        if len(illisibles) > 10:
            print(f"  … et {len(illisibles) - 10} autres")

    if doutes:
        print("\nSéances à vérifier :")
        for s in doutes[:10]:
            print(f"  séance {s['seance']:>4} · {s['machine']} · {s['champ_nom'][:24]:<24} "
                  f"· {s['nb_fichiers']} fichier(s) · {s['doute']}")
        if len(doutes) > 10:
            print(f"  … et {len(doutes) - 10} autres")

    print(f"\nÉcrit : {sortie / 'fichiers.csv'}  et  {sortie / 'seances.csv'}")
    print("Ces tableaux contiennent des identifiants de champ de traitement : "
          "à traiter comme des données sensibles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
