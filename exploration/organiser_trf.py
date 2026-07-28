"""Inventorie et regroupe en séances les fichiers TRF d'une sauvegarde SDD.

Ne lit que les métadonnées d'en-tête et quelques colonnes du corps : aucune
donnée patient n'est extraite, aucun fichier n'est déplacé ni modifié.

    python3 exploration/organiser_trf.py SDD+2020-04-28.zip
    python3 exploration/organiser_trf.py /chemin/vers/dossier --sortie rapport/
    python3 exploration/organiser_trf.py *.zip --ecart-max 1800

Produit deux ou trois tableaux CSV :
  fichiers.csv    une ligne par TRF décodé, avec ses métadonnées
  seances.csv     une ligne par séance reconstituée
  illisibles.csv  une ligne par TRF rejeté, avec son motif et ses premiers
                  octets — écrit seulement s'il y en a

Par défaut rien n'est déplacé ni copié : les séances n'existent que comme lignes
de `seances.csv`, dont la colonne `fichiers` liste leur composition. L'option
`--extraire` crée en plus un dossier par séance.

Pourquoi ce n'est pas trivial
-----------------------------
Une séance interrompue puis reprise s'écrit en **plusieurs fichiers**, chacun
avec son compteur de MU repartant de zéro. Un simple seuil de durée ne suffit
pas à les distinguer de deux séances distinctes : sur les données de référence,
le plus petit intervalle entre deux séances (94 s) était plus court que le plus
grand intervalle à l'intérieur d'une séance (162 s).

La règle s'appuie donc sur ce que la machine dit elle-même : le dernier code
d'état du fichier. « Terminated Ok » signifie que la délivrance est allée à son
terme et clôt la séance ; « Terminated Fault » ou « Interupted » signifient que
la suite est dans le fichier suivant. Aucune estimation n'est nécessaire.

Le cumul de MU ne sert plus que de repli, pour les fichiers où la colonne d'état
serait absente, et de contrôle a posteriori via la colonne `completude`.

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

# Codes d'état de fin de délivrance. La machine dit elle-même si elle est allée
# au bout : c'est un signal bien plus sûr qu'une estimation sur les MU.
ETAT_TERMINE_OK = 46      # « Terminated Ok »       -> délivrance menée à son terme
ETATS_INTERROMPU = {43, 44, 47}  # Interupted, Interupted Ready, Terminated Fault
NOMS_ETATS = {16: "Closed", 34: "State Code Unknown", 39: "Move Only", 40: "Pause",
              41: "Intersegment", 42: "Radiation On", 43: "Interupted",
              44: "Interupted Ready", 45: "Terminated Checking",
              46: "Terminated Ok", 47: "Terminated Fault"}

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


def resumer(octets, nom_fichier, origine=("fichier", "", "")):
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
        "origine": origine,
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
        # Un compteur remis à zéro retombe exactement sur 0 : c'est cette
        # signature qu'on cherche, et non l'ampleur de la chute. Un seuil sur
        # l'ampleur raterait les petits faisceaux ; se contenter d'une
        # différence négative prendrait le moindre bruit pour une frontière.
        plancher = max(0.5, 0.01 * float(mu.max()))
        ruptures = np.where((np.diff(mu) < 0) & (mu[1:] <= plancher))[0]
        segments = [float(mu[i]) for i in ruptures] + [float(mu[-1])]
        resume["mu_corps"] = round(sum(segments), 1)
        resume["mu_max_faisceau"] = round(max(segments), 1)
        resume["faisceaux"] = len(segments)
        resume["mu_brut_min"] = int(mu.min() * 10)
        resume["mu_brut_max"] = int(mu.max() * 10)
    else:
        resume["mu_corps"] = resume["mu_max_faisceau"] = None
        resume["faisceaux"] = resume["mu_brut_min"] = resume["mu_brut_max"] = None

    # Le total retenu vient du corps du fichier : il est recalculé depuis les
    # données réellement enregistrées. L'en-tête porte bien un total, mais il
    # vaut 0 sur une partie des fichiers — il ne sert donc que de recoupement,
    # quand il est renseigné.
    resume["mu"] = resume["mu_corps"] if resume["mu_corps"] is not None else resume["mu_entete"]
    resume["entete_sans_mu"] = resume["mu_entete"] == 0
    # Un enregistrement peut ne porter aucune dose : imagerie, mise en place,
    # séance avortée avant le premier rayonnement. Ce n'est pas un traitement.
    resume["delivrance"] = (resume["mu"] or 0) >= 1.0

    # L'état machine sert à ne juger la géométrie que pendant l'irradiation :
    # entre deux faisceaux, le bras tourne et fausserait la détection d'arc.
    etat = colonne(COL_ETAT)
    irradie = etat == 42 if etat is not None else None  # 42 = « Radiation On »
    if irradie is not None:
        resume["part_irradiation"] = round(float(irradie.mean()), 3)
        code_final = int(etat[-1])
        resume["etat_final"] = NOMS_ETATS.get(code_final, str(code_final))
        if code_final == ETAT_TERMINE_OK:
            resume["issue"] = "terminee"
        elif code_final in ETATS_INTERROMPU:
            resume["issue"] = "interrompue"
        else:
            resume["issue"] = "indeterminee"
    else:
        resume["etat_final"] = resume["issue"] = None

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
    # Chaque faisceau perd jusqu'à un pas de quantification (0,1 MU) au moment
    # où son compteur est remis à zéro : l'écart attendu croît avec leur nombre.
    # Le recoupement n'a de sens que si l'en-tête annonce quelque chose.
    if resume["mu_corps"] is not None and not resume["entete_sans_mu"]:
        resume["ecart_mu_entete"] = round(resume["mu_corps"] - resume["mu_entete"], 1)
        tolerance = 0.5 + 0.15 * (resume["faisceaux"] or 1)
        resume["mu_incoherent"] = abs(resume["ecart_mu_entete"]) > tolerance
    else:
        resume["ecart_mu_entete"] = None
        resume["mu_incoherent"] = False

    fin = datetime.datetime.strptime(entete["date"], "%y/%m/%d %H:%M:%S Z")
    debut = fin - datetime.timedelta(seconds=resume["duree_s"])
    resume["debut_utc"] = debut.replace(microsecond=0).isoformat(sep=" ")
    resume["fin_utc"] = fin.isoformat(sep=" ")

    return resume


def diagnostiquer(octets, nom_fichier):
    """Détaille la structure du compteur de MU d'un seul fichier.

    Sert à comprendre une divergence entre le total de l'en-tête et celui
    recalculé depuis le corps. N'affiche que des chiffres : ni positions de
    lames, ni identifiant patient.
    """
    entete = lire_entete(octets)
    v = VERSIONS[entete["version"]]
    taille_ligne = v["echelle"] * entete["nb_colonnes"] * 2 + v["prefixe"]
    corps = octets[entete["fin_entete"]:]
    nb_lignes = len(corps) // taille_ligne
    index = {nom: i for i, nom in enumerate(entete["colonnes"])}

    print(f"\n=== {nom_fichier} ===")
    print(f"  encodage v{entete['version']} · {entete['nb_colonnes']} colonnes · "
          f"{taille_ligne} o/ligne · {nb_lignes} lignes")
    muet = entete["mu_entete"] == 0
    print(f"  total annoncé par l'en-tête : {entete['mu_entete']:.1f} MU"
          + ("   ⚠ EN-TÊTE MUET" if muet else ""))

    if COL_MU not in index:
        print("  ⚠ colonne des MU absente du schéma")
        return
    brut = extraire_colonne(corps, nb_lignes, taille_ligne, v["prefixe"],
                            v["echelle"] * 2, index[COL_MU])
    mu = brut / 10.0
    print(f"  colonne des MU, valeurs brutes : min={int(brut.min())} max={int(brut.max())}")
    if brut.min() < -30000 or brut.max() > 30000:
        print("    ⚠ on frôle les bornes d'un entier 16 bits (±32767) : "
              "débordement probable")
    negatifs = int((brut < 0).sum())
    if negatifs:
        print(f"    ⚠ {negatifs} valeur(s) négative(s) — un compteur de dose ne "
              "devrait jamais l'être")

    d = np.diff(mu)
    plancher = max(0.5, 0.01 * float(mu.max()))
    chutes = np.where(d < 0)[0]
    vers_zero = np.where((d < 0) & (mu[1:] <= plancher))[0]
    print(f"  chutes du compteur : {len(chutes)} au total, dont {len(vers_zero)} "
          f"retombant sous {plancher:.1f} MU")
    if len(chutes):
        arrivees = mu[chutes + 1]
        print(f"    valeurs d'arrivée : min={arrivees.min():.1f} "
              f"médiane={np.median(arrivees):.1f} max={arrivees.max():.1f}")
        amplitudes = -d[chutes]
        print(f"    amplitudes        : min={amplitudes.min():.1f} "
              f"médiane={np.median(amplitudes):.1f} max={amplitudes.max():.1f}")

    segments = [float(mu[i]) for i in vers_zero] + [float(mu[-1])]
    print(f"  segments retenus : {len(segments)} -> total {sum(segments):.1f} MU "
          f"(écart {sum(segments) - entete['mu_entete']:+.1f})")
    apercu = ", ".join(f"{x:.1f}" for x in segments[:12])
    print(f"    {apercu}{' …' if len(segments) > 12 else ''}")

    if COL_ETAT in index:
        etat = extraire_colonne(corps, nb_lignes, taille_ligne, v["prefixe"],
                                v["echelle"] * 2, index[COL_ETAT])
        comptes = {}
        for code in etat.astype(int):
            comptes[code] = comptes.get(code, 0) + 1
        libelles = ", ".join(f"{NOMS_ETATS.get(c, c)}={n}"
                             for c, n in sorted(comptes.items(), key=lambda x: -x[1]))
        print(f"  états machine : {libelles}")


def parcourir(chemins):
    """Rend (nom, origine, octets) pour chaque .trf, dans les zip comme sur disque.

    `origine` permet de relire le fichier plus tard sans tout garder en mémoire.
    """
    for chemin in chemins:
        p = pathlib.Path(chemin)
        if p.is_dir():
            for f in sorted(p.rglob("*.trf")):
                yield str(f.relative_to(p)), ("fichier", str(f), ""), f.read_bytes()
        elif p.suffix.lower() == ".zip":
            with zipfile.ZipFile(p) as archive:
                for nom in sorted(archive.namelist()):
                    if nom.lower().endswith(".trf"):
                        yield f"{p.name}::{nom}", ("zip", str(p), nom), archive.read(nom)
        elif p.suffix.lower() == ".trf":
            yield p.name, ("fichier", str(p), ""), p.read_bytes()
        else:
            print(f"  ignoré (ni zip, ni dossier, ni .trf) : {p}", file=sys.stderr)


def relire(origine):
    genre, chemin, interne = origine
    if genre == "zip":
        with zipfile.ZipFile(chemin) as archive:
            return archive.read(interne)
    return pathlib.Path(chemin).read_bytes()


def assainir(texte, defaut="sans-nom"):
    """Rend un fragment de nom de dossier sûr sur tous les systèmes."""
    propre = re.sub(r"[^A-Za-z0-9._-]+", "-", texte).strip("-")
    return propre[:40] or defaut


def extraire_seances(seances, resumes, destination):
    """Copie les TRF dans un dossier par séance. Ne supprime jamais la source."""
    par_fichier = {r["fichier"]: r for r in resumes}
    destination = pathlib.Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    total_octets, total_fichiers = 0, 0
    for s in seances:
        debut = datetime.datetime.fromisoformat(s["debut_utc"])
        nom = (
            f"{debut:%Y-%m-%d_%H%M%S}_{assainir(s['machine'], 'machine')}"
            f"_s{s['seance']:04d}_{assainir(s['champ_nom'])}"
        )
        dossier = destination / nom
        dossier.mkdir(exist_ok=True)

        for chemin_affiche in s["fichiers"].split(" | "):
            r = par_fichier.get(chemin_affiche)
            if r is None:
                continue
            octets = relire(r["origine"])
            cible = dossier / pathlib.PurePath(chemin_affiche.split("::")[-1]).name
            if cible.exists() and cible.read_bytes() == octets:
                continue
            cible.write_bytes(octets)
            total_octets += len(octets)
            total_fichiers += 1

        recap = [
            f"séance {s['seance']}",
            f"machine        {s['machine']}",
            f"champ          {s['champ_etiquette']}/{s['champ_nom']}",
            f"début (UTC)    {s['debut_utc']}",
            f"fin (UTC)      {s['fin_utc']}",
            f"fichiers       {s['nb_fichiers']}",
            f"MU cumulées    {s['mu_cumul']} (référence du champ : {s['mu_reference']})",
            f"complétude     {s['completude']}",
            f"ouverture      {s['ouverture']}",
        ]
        if s["doute"]:
            recap.append(f"À VÉRIFIER     {s['doute']}")
        (dossier / "seance.txt").write_text("\n".join(recap) + "\n", encoding="utf-8")

    return total_fichiers, total_octets


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

        if not r.get("delivrance", True):
            # Entrée isolée, mais transparente pour le chaînage : un cliché
            # d'imagerie glissé entre deux fragments ne doit pas couper la
            # séance en cours.
            isole = {
                "seance": len(seances) + 1, "machine": r["machine"],
                "champ_nom": r["champ_nom"], "champ_etiquette": r["champ_etiquette"],
                "debut_utc": r["debut_utc"], "fin_utc": r["fin_utc"],
                "fichiers": [r["fichier"]], "nb_fichiers": 1,
                "mu_cumul": r.get("mu") or 0.0, "mu_reference": round(total_attendu, 1),
                "issue": "sans_dose", "etat_final": r.get("etat_final"),
                "ouverture": "enregistrement sans dose",
            }
            seances.append(isole)
            r["seance"] = isole["seance"]
            continue                       # `courante` reste intacte
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
            elif courante["issue"] == "terminee":
                # La machine a écrit « Terminated Ok » : la séance est close.
                raison = "délivrance précédente menée à son terme"
            elif courante["issue"] is None and total_attendu and (
                courante["mu_cumul"] >= seuil_complet * total_attendu
            ):
                # Repli quand l'état machine est absent du fichier.
                raison = "cumul de MU atteint (état machine indisponible)"

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
                "issue": r.get("issue"),
                "etat_final": r.get("etat_final"),
                "ouverture": raison,
            }
            seances.append(courante)
            active = courante
        else:
            active = courante
            courante["fichiers"].append(r["fichier"])
            courante["nb_fichiers"] += 1
            courante["mu_cumul"] += r.get("mu") or 0.0
            courante["fin_utc"] = r["fin_utc"]
            courante["issue"] = r.get("issue")
            courante["etat_final"] = r.get("etat_final")

        r["seance"] = active["seance"]

    for s in seances:
        s["mu_cumul"] = round(s["mu_cumul"], 1)
        ref = s["mu_reference"]
        s["completude"] = round(s["mu_cumul"] / ref, 3) if ref else None
        s["fichiers"] = " | ".join(s["fichiers"])
        s["doute"] = ""
        if s["issue"] == "sans_dose":
            s["doute"] = "aucune dose enregistrée : ce n'est pas un traitement"
        elif s["issue"] == "interrompue":
            s["doute"] = (
                f"se termine sur « {s['etat_final']} » : la suite manque, "
                "ou la séance a été abandonnée"
            )
        elif s["issue"] == "indeterminee":
            s["doute"] = f"état final inhabituel : « {s['etat_final']} »"

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
        "--diagnostic", nargs="?", type=int, const=3, metavar="N",
        help="après l'inventaire, détaille la structure du compteur de MU des N "
             "fichiers dont le total diverge le plus de l'en-tête (3 par défaut). "
             "Inutile de désigner un fichier : il les trouve lui-même.",
    )
    analyseur.add_argument(
        "--filtre", metavar="MOTIF",
        help="ne traiter que les fichiers dont le chemin contient ce motif. "
             "Utile pour cibler un fichier précis à l'intérieur d'un zip.",
    )
    analyseur.add_argument(
        "--extraire", metavar="DOSSIER",
        help="copie en plus les TRF dans un dossier par séance. Duplique des "
             "données patient sur le disque : à n'utiliser qu'en connaissance de cause.",
    )
    analyseur.add_argument(
        "--seuil-complet", type=float, default=0.97,
        help="fraction du total de référence à partir de laquelle une séance est "
             "réputée complète (défaut : 0.97)",
    )
    args = analyseur.parse_args()

    resumes, illisibles = [], []
    for n, (nom, origine, octets) in enumerate(parcourir(args.sources), 1):
        if args.filtre and args.filtre not in nom:
            continue
        if n % 250 == 0:
            print(f"  … {n} fichiers lus", file=sys.stderr, flush=True)
        try:
            resumes.append(resumer(octets, nom, origine))
        except TrfIllisible as e:
            illisibles.append({"fichier": nom, "motif": str(e),
                               "octets": len(octets),
                               "premiers_octets": octets[:24].hex(" ")})
        except Exception as e:  # un fichier abîmé ne doit pas tout arrêter
            illisibles.append({"fichier": nom, "motif": f"{type(e).__name__}: {e}",
                               "octets": len(octets),
                               "premiers_octets": octets[:24].hex(" ")})

    if not resumes:
        print("Aucun fichier TRF exploitable trouvé.", file=sys.stderr)
        return 1

    seances = regrouper(resumes, args.ecart_max, args.seuil_complet)

    sortie = pathlib.Path(args.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    cols_fichiers = [
        "seance", "fichier", "machine", "version", "champ_etiquette", "champ_nom",
        "debut_utc", "fin_utc", "duree_s", "echantillons", "mu", "mu_entete",
        "delivrance", "mu_corps", "entete_sans_mu", "ecart_mu_entete",
        "mu_incoherent", "faisceaux",
        "mu_max_faisceau", "mu_brut_min", "mu_brut_max", "part_irradiation",
        "etat_final", "issue",
        "gantry_debut", "gantry_fin", "gantry_min", "gantry_max",
        "angles_irradies", "arc", "cp_min", "cp_max", "coupures",
        "octets_par_ligne", "reste_octets", "fuseau",
    ]
    cols_seances = [
        "seance", "machine", "champ_etiquette", "champ_nom", "debut_utc", "fin_utc",
        "nb_fichiers", "mu_cumul", "mu_reference", "completude", "etat_final",
        "issue", "ouverture", "doute", "fichiers",
    ]
    ecrire_csv(sortie / "fichiers.csv", sorted(resumes, key=lambda r: r["debut_utc"]), cols_fichiers)
    ecrire_csv(sortie / "seances.csv", seances, cols_seances)
    if illisibles:
        ecrire_csv(sortie / "illisibles.csv", illisibles,
                   ["fichier", "motif", "octets", "premiers_octets"])

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
    sans_dose = [r for r in resumes if not r.get("delivrance", True)]
    if sans_dose:
        print(f"  {len(sans_dose)} fichier(s) sans dose enregistrée (< 1 MU) — "
              f"imagerie, mise en place ou séance avortée")
        etats = {}
        for r in sans_dose:
            etats[r.get("etat_final") or "inconnu"] = etats.get(r.get("etat_final") or "inconnu", 0) + 1
        print("      états finaux : "
              + ", ".join(f"{k} ({n})" for k, n in sorted(etats.items(), key=lambda x: -x[1])))

    muets = [r for r in resumes if r.get("entete_sans_mu")]
    if muets:
        avec_dose = [r for r in muets if r.get("delivrance")]
        print(f"  {len(muets)} fichier(s) dont l'en-tête n'annonce aucune MU, "
              f"dont {len(avec_dose)} portant tout de même une dose")
        etats = {}
        for r in muets:
            cle = r.get("etat_final") or "inconnu"
            etats[cle] = etats.get(cle, 0) + 1
        print("      états finaux : "
              + ", ".join(f"{k} ({n})" for k, n in sorted(etats.items(), key=lambda x: -x[1])))
        if avec_dose:
            doses = sorted(r["mu"] for r in avec_dose)
            print(f"      doses relevées dans le corps : min={doses[0]:.1f} "
                  f"médiane={doses[len(doses) // 2]:.1f} max={doses[-1]:.1f} MU")

    discordants = [r for r in resumes if r.get("mu_incoherent")]
    if discordants:
        pires = sorted(discordants, key=lambda r: -abs(r["ecart_mu_entete"]))[:3]
        print(f"  ⚠ {len(discordants)} fichier(s) où les MU du corps et de l'en-tête "
              f"divergent au-delà de la tolérance")
        for r in pires:
            print(f"      {r['fichier'][-40:]} : {r['ecart_mu_entete']:+.1f} MU "
                  f"sur {r['faisceaux']} faisceau(x)")
    inconnues = sorted({c for r in resumes for c in r.get("colonnes_inconnues", [])})
    if inconnues:
        print(f"  ⚠ {len(inconnues)} code(s) de colonne absent(s) du dictionnaire : "
              + ", ".join(inconnues[:6]))

    if illisibles:
        motifs = {}
        for i in illisibles:
            cle = i["motif"].split(":")[0].strip()
            motifs[cle] = motifs.get(cle, 0) + 1
        print(f"\nFichiers non décodés ({len(illisibles)}), par motif :")
        for motif, n in sorted(motifs.items(), key=lambda x: -x[1]):
            print(f"  {n:>6}  {motif}")
        if any("date d'en-tête" in m for m in motifs):
            print("\n  ⚠ « date d'en-tête inattendue » en nombre est la signature connue")
            print("    d'un changement de format d'en-tête (cf. pymedphys#1890, Integrity")
            print("    4.1.0.0). La liste complète, avec les premiers octets de chaque")
            print("    fichier, est dans illisibles.csv — de quoi reprendre le parsing.")

    if doutes:
        print("\nSéances à vérifier :")
        for s in doutes[:10]:
            print(f"  séance {s['seance']:>4} · {s['machine']} · {s['champ_nom'][:24]:<24} "
                  f"· {s['nb_fichiers']} fichier(s) · {s['doute']}")
        if len(doutes) > 10:
            print(f"  … et {len(doutes) - 10} autres")

    if args.extraire:
        nb, octets = extraire_seances(seances, resumes, args.extraire)
        print(f"\n{nb} fichier(s) copiés ({octets / 1e6:.0f} Mo) dans "
              f"{len(seances)} dossiers sous {args.extraire}")
        print("  ⚠ ce sont des copies de données patient : à protéger comme les originaux.")

    if args.diagnostic:
        # Les anomalies, de la plus parlante à la moins : en-tête muet,
        # divergence de total, puis les plus gros fichiers à défaut d'anomalie.
        def rang(r):
            if r.get("entete_sans_mu"):
                return (0, -(r.get("mu") or 0))
            if r.get("mu_incoherent"):
                return (1, -abs(r.get("ecart_mu_entete") or 0))
            return (2, -(r.get("echantillons") or 0))

        candidats = sorted(resumes, key=rang)[: args.diagnostic]
        if not candidats:
            print("\nRien à diagnostiquer.")
        else:
            print(f"\n{'=' * 62}")
            print(f"DIAGNOSTIC des {len(candidats)} fichiers les plus atypiques")
            print(f"{'=' * 62}")
            for r in candidats:
                try:
                    diagnostiquer(relire(r["origine"]), r["fichier"])
                except Exception as e:
                    print(f"\n=== {r['fichier']} ===\n  illisible : {e}")

    produits = ["fichiers.csv", "seances.csv"] + (["illisibles.csv"] if illisibles else [])
    print(f"\nÉcrit dans {sortie} : " + ", ".join(produits))
    print("Ces tableaux contiennent des identifiants de champ de traitement : "
          "à traiter comme des données sensibles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
