"""Découpe les séances sur l'horloge machine, pas sur les fichiers.

    python3 exploration/seances_par_horloge.py "SDD+xxx.zip" --sortie horloge/
    python3 exploration/seances_par_horloge.py "SDD+xxx.zip" --comparer

Méthode alternative à `organiser_trf.py`, volontairement indépendante de lui.

L'idée
------
Les versions d'encodage ≥ 2 portent, en tête de chaque ligne, un compteur en
millisecondes propre à la machine. Vérifié sur les données publiques : il est
**strictement croissant et continu d'un fichier à l'autre**, sans recouvrement,
et concorde avec les dates d'en-tête à ±1,4 s.

On peut donc oublier les fichiers : recoller **tous les échantillons d'une
machine** en une seule ligne de temps, puis la découper là où la machine écrit
« Terminated Ok ». Un fichier n'est plus qu'un contenant arbitraire.

Ce que ça change par rapport à `organiser_trf.py`
------------------------------------------------
En mieux : l'axe temporel est **mesuré** et non reconstruit. `organiser_trf`
calcule `début = date d'en-tête − nombre de lignes × 40 ms`, ce qui suppose un
échantillonnage parfaitement régulier — faux sur 11 % des pas — et dépend de la
date d'en-tête. Ici le temps vient du compteur lui-même.

À l'identique : le critère de coupure. Mesuré sur les 9 fichiers publics,
« Terminated Ok » est **toujours le dernier état du fichier et rien ne suit**.
Découper à l'échantillon donne donc les mêmes frontières que découper au
fichier.

En moins bien : les fichiers de **version 1 n'ont pas de compteur** et sont
ignorés ici, alors qu'`organiser_trf` les traite.

Les garde-fous
--------------
Couper aux seuls « Terminated Ok » ne suffit pas : quand une délivrance se
termine sans — faute, abandon, machine éteinte —, le patient suivant est
recollé au précédent. Deux garde-fous ferment donc aussi une séance, le
**changement de nom de champ** et l'**écart de temps**.

Comparer les MU d'un fichier au suivant ne marcherait pas : sur une séance
interrompue légitime, les fragments consécutifs font 26,5 puis 41,7 puis 314,4
puis 43,8 MU. Un test sur l'écart de MU la couperait en quatre.

`--brut` retire les garde-fous et applique l'idée telle quelle, pour voir ce
qu'elle donne seule.
"""

import argparse
import collections
import csv
import datetime
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# Seul le décodage du format est emprunté : il est vérifié colonne par colonne
# contre pymedphys. Toute la logique de regroupement est écrite ici, pour que la
# comparaison entre les deux méthodes ne soit pas circulaire.
from organiser_trf import (COL_ETAT, COL_MU, NOMS_ETATS,  # noqa: E402
                           VERSIONS, TrfIllisible, extraire_colonne,
                           lire_entete, parcourir)

ETAT_TERMINE_OK = 46
PAS_MS = 40


def echantillons(sources, filtre=None):
    """Rend, par machine, tous les échantillons rangés sur l'horloge machine.

    Chaque échantillon porte son instant, son état, ses MU et le fichier d'où il
    vient. Le fichier ne sert plus qu'à la traçabilité.
    """
    par_machine = collections.defaultdict(
        lambda: {"ms": [], "etat": [], "mu": [], "source": [], "fichiers": []})
    sans_horloge, illisibles = [], []

    for nom, _, octets in parcourir(sources):
        if filtre and filtre not in nom:
            continue
        try:
            entete = lire_entete(octets)
        except TrfIllisible as erreur:
            illisibles.append((nom, str(erreur)))
            continue
        except Exception as erreur:
            illisibles.append((nom, f"{type(erreur).__name__}: {erreur}"))
            continue

        v = VERSIONS[entete["version"]]
        if v["prefixe"] < 8:
            sans_horloge.append(nom)          # version 1 : pas de compteur
            continue

        taille = v["echelle"] * entete["nb_colonnes"] * 2 + v["prefixe"]
        corps = octets[entete["fin_entete"]:]
        n = len(corps) // taille
        if n < 1:
            illisibles.append((nom, "aucune ligne de données"))
            continue

        grille = np.frombuffer(corps, np.uint8, n * taille).reshape(n, taille)
        ms = np.ascontiguousarray(grille[:, :8]).view(np.int64).ravel()

        index = {c: i for i, c in enumerate(entete["colonnes"])}
        if COL_ETAT not in index or COL_MU not in index:
            illisibles.append((nom, "colonne d'état ou de dose absente"))
            continue

        def colonne(cle):
            return extraire_colonne(corps, n, taille, v["prefixe"],
                                    v["echelle"] * 2, index[cle])

        bloc = par_machine[entete["machine"]]
        rang = len(bloc["fichiers"])
        bloc["fichiers"].append({
            "nom": nom, "champ": entete["champ_nom"],
            "fuseau": entete["fuseau"], "date_fin": entete["date"],
            "ms_fin": int(ms[-1]),
        })
        bloc["ms"].append(ms)
        bloc["etat"].append(colonne(COL_ETAT).astype(np.int16))
        bloc["mu"].append(colonne(COL_MU) / 10.0)
        bloc["source"].append(np.full(n, rang, dtype=np.int32))

    for machine, bloc in par_machine.items():
        for cle in ("ms", "etat", "mu", "source"):
            bloc[cle] = np.concatenate(bloc[cle])
        ordre = np.argsort(bloc["ms"], kind="stable")
        for cle in ("ms", "etat", "mu", "source"):
            bloc[cle] = bloc[cle][ordre]

    return par_machine, sans_horloge, illisibles


def origine_horloge(bloc):
    """Rattache le compteur à l'heure réelle.

    Le compteur n'a pas d'origine connue — lu comme un epoch Unix il donnerait
    2004 pour un fichier de 2020. Mais chaque fichier fournit un point
    d'ancrage : sa date d'en-tête est l'instant de son **dernier** échantillon.
    On prend la médiane des ancrages, et on rend leur dispersion : si les
    fichiers ne s'accordent pas, c'est que le compteur n'est pas ce qu'on croit.
    """
    ancrages = []
    for f in bloc["fichiers"]:
        try:
            fin = datetime.datetime.strptime(f["date_fin"], "%y/%m/%d %H:%M:%S Z")
        except ValueError:
            continue
        ancrages.append(fin.timestamp() - f["ms_fin"] / 1000.0)
    if not ancrages:
        return None, None
    return float(np.median(ancrages)), float(np.std(ancrages))


def decouper(bloc, ecart_max_ms=1_800_000, brut=False):
    """Coupe la ligne de temps, et dit pourquoi à chaque fois.

    Coupure principale : après chaque salve de « Terminated Ok ». Elle tombe au
    **dernier** échantillon de la salve, la machine écrivant cet état sur une
    dizaine d'échantillons consécutifs en fin de délivrance.

    Deux garde-fous, sauf en mode `brut` :

    **Changement de nom de champ.** Quand une délivrance se termine sans
    « Terminated Ok » — faute, abandon, machine éteinte —, la coupure n'a pas
    lieu et le patient suivant est recollé au précédent. Le nom de champ est le
    seul signal direct de ce recollement.

    Comparer les MU d'un fichier au suivant, en revanche, **ne marche pas** :
    mesuré sur une séance interrompue légitime, les fragments consécutifs font
    26,5 puis 41,7 puis 314,4 puis 43,8 MU. Un test sur l'écart de MU couperait
    cette séance en quatre.

    **Écart de temps.** Deux délivrances du même champ à des heures éloignées
    sont deux séances, même si la première n'a pas conclu. Ici l'écart est
    *mesuré* sur le compteur, pas reconstruit.
    """
    etat, ms, source = bloc["etat"], bloc["ms"], bloc["source"]
    ok = etat == ETAT_TERMINE_OK
    coupures = {}

    for i in np.where(ok & ~np.append(ok[1:], False))[0]:
        coupures[int(i)] = "Terminated Ok"

    if not brut:
        rang_du_champ = {}
        codes = np.array([rang_du_champ.setdefault(f["champ"], len(rang_du_champ))
                          for f in bloc["fichiers"]], dtype=np.int32)
        par_echantillon = codes[source]
        for i in np.where(par_echantillon[:-1] != par_echantillon[1:])[0]:
            coupures.setdefault(int(i), "champ différent")
        for i in np.where(np.diff(ms) > ecart_max_ms)[0]:
            coupures.setdefault(int(i), f"écart > {ecart_max_ms / 60000:.0f} min")

    segments, debut = [], 0
    for i in sorted(coupures):
        segments.append((debut, i + 1, coupures[i]))
        debut = i + 1
    if debut < len(etat):
        segments.append((debut, len(etat), "fin de l'archive"))
    return segments


def mu_du_segment(mu):
    """Somme les faisceaux : le compteur repart de zéro à chacun."""
    if len(mu) == 0:
        return 0.0
    plancher = max(0.5, 0.01 * float(mu.max()))
    ruptures = np.where((np.diff(mu) < 0) & (mu[1:] <= plancher))[0]
    return float(sum(mu[i] for i in ruptures) + mu[-1])


def decrire(bloc, machine, debut, fin, origine, numero, cloture=""):
    """Résume une tranche de la ligne de temps."""
    ms, etat, mu, source = (bloc[c][debut:fin] for c in ("ms", "etat", "mu", "source"))
    rangs = sorted(set(int(r) for r in source))
    fichiers = [bloc["fichiers"][r] for r in rangs]
    champs = sorted({f["champ"] for f in fichiers})

    def horloge(valeur_ms):
        if origine is None:
            return ""
        t = datetime.datetime.fromtimestamp(origine + valeur_ms / 1000.0)
        return t.replace(microsecond=0).isoformat(sep=" ")

    trous = np.where(np.diff(ms) > 5000)[0]      # 5 s sans le moindre échantillon
    doutes = []
    if len(champs) > 1:
        doutes.append(f"{len(champs)} champs différents dans la même séance : "
                      + ", ".join(champs[:4]))
    if int(etat[-1]) != ETAT_TERMINE_OK:
        doutes.append(f"ne se termine pas sur « Terminated Ok » mais sur "
                      f"« {NOMS_ETATS.get(int(etat[-1]), int(etat[-1]))} »")
    if len(trous):
        pires = sorted((np.diff(ms)[t] / 1000.0 for t in trous), reverse=True)[:3]
        doutes.append("interruption(s) d'enregistrement de "
                      + ", ".join(f"{p:.0f} s" for p in pires))

    return {
        "seance": numero,
        "cloture": cloture,
        "machine": machine,
        "champ": " | ".join(champs),
        "debut": horloge(int(ms[0])),
        "fin": horloge(int(ms[-1])),
        "duree_s": round((int(ms[-1]) - int(ms[0])) / 1000.0, 2),
        "echantillons": len(ms),
        "nb_fichiers": len(fichiers),
        "fichiers": " | ".join(f["nom"] for f in fichiers),
        "mu": round(mu_du_segment(mu), 1),
        "etat_final": NOMS_ETATS.get(int(etat[-1]), str(int(etat[-1]))),
        "doute": " · ".join(doutes),
    }


def comparer(seances, sources, filtre, ecart_max, seuil_complet):
    """Confronte le découpage obtenu à celui d'`organiser_trf.py`."""
    from organiser_trf import parcourir as parcourir2
    from organiser_trf import regrouper
    from organiser_trf import resumer as resumer_trf

    resumes = []
    for nom, _, octets in parcourir2(sources):
        if filtre and filtre not in nom:
            continue
        try:
            resumes.append(resumer_trf(octets, nom))
        except Exception:
            continue
    reference = [s for s in regrouper(resumes, ecart_max, seuil_complet)
                 if s.get("issue") != "sans_dose"]

    # La comparaison n'a de sens que sur les fichiers vus par les DEUX méthodes :
    # l'horloge ignore les encodages v1, qui paraîtraient sinon « perdus ».
    vus_ici = {f for s in seances for f in s["fichiers"].split(" | ")}
    vus_la = {f for s in reference for f in s["fichiers"].split(" | ")}
    communs = vus_ici & vus_la

    ici = [frozenset(s["fichiers"].split(" | ")) & communs for s in seances]
    la = [frozenset(s["fichiers"].split(" | ")) & communs for s in reference]
    ici = [c for c in ici if c]
    la = [c for c in la if c]

    print(f"\n{'=' * 66}\nCOMPARAISON AVEC organiser_trf.py\n{'=' * 66}")
    print(f"  fichiers vus par l'horloge : {len(vus_ici)} · par organiser_trf : "
          f"{len(vus_la)} · communs : {len(communs)}")
    if vus_la - vus_ici:
        print(f"    {len(vus_la - vus_ici)} fichier(s) hors comparaison "
              "(sans horloge, encodage v1)")
    print(f"  séances sur les fichiers communs : {len(ici)} par l'horloge, "
          f"{len(la)} par organiser_trf")

    comptes, exemples = qualifier(ici, la)

    print("\n  NATURE DES DÉSACCORDS")
    for genre, n in comptes.most_common():
        print(f"    {n:>5}  {genre}")
        if genre != "identiques":
            for fichiers in exemples[genre]:
                print("             "
                      + ", ".join(f.split("::")[-1][-24:] for f in fichiers[:4])
                      + (" …" if len(fichiers) > 4 else ""))


def qualifier(ici, la):
    """Classe les désaccords entre deux découpages du même lot de fichiers.

    Chaque découpage est une liste d'ensembles de fichiers. On construit les
    composantes connexes du graphe qui relie une séance d'un côté à celles de
    l'autre avec lesquelles elle partage au moins un fichier : une composante
    est un groupe de fichiers que les deux méthodes se disputent. Sa forme dit
    la nature du désaccord — 3 contre 1 signifie que la première découpe ce que
    la seconde garde entier.
    """
    appartenance = {}
    for rang, composition in enumerate(la):
        for f in composition:
            appartenance[f] = rang
    lien = collections.defaultdict(set)
    for rang, composition in enumerate(ici):
        for f in composition:
            if f in appartenance:
                lien[rang].add(appartenance[f])
    inverse = collections.defaultdict(set)
    for a, bs in lien.items():
        for b in bs:
            inverse[b].add(a)

    vus, familles = set(), []
    for depart in range(len(ici)):
        if depart in vus:
            continue
        pile, cote_ici, cote_la = [("ici", depart)], set(), set()
        while pile:
            cote, rang = pile.pop()
            if cote == "ici":
                if rang in cote_ici:
                    continue
                cote_ici.add(rang)
                pile += [("la", r) for r in lien.get(rang, ())]
            else:
                if rang in cote_la:
                    continue
                cote_la.add(rang)
                pile += [("ici", r) for r in inverse.get(rang, ())]
        vus |= cote_ici
        familles.append((cote_ici, cote_la))

    comptes = collections.Counter()
    exemples = collections.defaultdict(list)
    for cote_ici, cote_la in familles:
        n, m = len(cote_ici), len(cote_la)
        if n == 1 and m == 1:
            genre = ("identiques" if ici[next(iter(cote_ici))] == la[next(iter(cote_la))]
                     else "même groupe, composition différente")
        elif m == 1:
            genre = f"l'horloge DÉCOUPE ce qu'organiser_trf garde entier ({n} contre 1)"
        elif n == 1:
            genre = f"l'horloge FUSIONNE ce qu'organiser_trf sépare (1 contre {m})"
        else:
            genre = f"redécoupage croisé ({n} contre {m})"
        comptes[genre] += 1
        if len(exemples[genre]) < 3:
            fichiers = sorted(set().union(*[ici[r] for r in cote_ici]))
            exemples[genre].append(fichiers)

    return comptes, exemples


def main():
    analyseur = argparse.ArgumentParser(
        description="Découpe les séances sur l'horloge machine plutôt que sur "
                    "les fichiers.",
        epilog="Méthode alternative à organiser_trf.py, qui n'est pas modifié.")
    analyseur.add_argument("sources", nargs="+", help="archive SDD, dossier, ou .trf")
    analyseur.add_argument("--sortie", metavar="DOSSIER",
                           help="écrit seances_horloge.csv")
    analyseur.add_argument("--filtre", help="ne garder que les .trf dont le nom contient ceci")
    analyseur.add_argument("--brut", action="store_true",
                           help="applique l'idée sans garde-fou : coupe uniquement "
                                "aux « Terminated Ok »")
    analyseur.add_argument("--comparer", action="store_true",
                           help="confronte le résultat à celui d'organiser_trf.py")
    analyseur.add_argument("--ecart-max", type=float, default=1800,
                           help="écart au-delà duquel une séance se ferme (défaut : 1800 s)")
    analyseur.add_argument("--seuil-complet", type=float, default=0.97,
                           help="pour la comparaison seulement (défaut : 0.97)")
    args = analyseur.parse_args()

    par_machine, sans_horloge, illisibles = echantillons(args.sources, args.filtre)
    if not par_machine:
        raise SystemExit("Aucun fichier exploitable avec une horloge machine "
                         "(versions d'encodage ≥ 2 seulement).")

    seances, numero = [], 0
    for machine in sorted(par_machine):
        bloc = par_machine[machine]
        origine, dispersion = origine_horloge(bloc)
        print(f"\nMachine {machine}")
        print(f"  {len(bloc['fichiers'])} fichier(s) · {len(bloc['ms'])} échantillons "
              f"· {(bloc['ms'][-1] - bloc['ms'][0]) / 3600000:.1f} h de compteur")
        if origine is not None:
            print(f"  ancrage de l'horloge : dispersion {dispersion:.2f} s entre fichiers"
                  + ("  ⚠ incohérent" if dispersion > 5 else ""))

        recul = np.where(np.diff(bloc["ms"]) < 0)[0]
        if len(recul):
            print(f"  ⚠ {len(recul)} recul(s) du compteur — l'horloge n'est pas "
                  "monotone, le postulat de la méthode ne tient pas")

        for debut, fin, cloture in decouper(bloc, args.ecart_max * 1000, args.brut):
            if fin - debut < 2:
                continue
            numero += 1
            seances.append(decrire(bloc, machine, debut, fin, origine, numero, cloture))

    doutes = [s for s in seances if s["doute"]]
    print(f"\n{len(seances)} séance(s) · {len(doutes)} avec doute")
    if sans_horloge:
        print(f"  {len(sans_horloge)} fichier(s) ignoré(s), encodage v1 sans horloge")
    if illisibles:
        print(f"  {len(illisibles)} fichier(s) illisible(s)")

    for s in seances[:20]:
        print(f"  séance {s['seance']:>4} · {s['debut'][:16]} · {s['duree_s']:>7.1f} s "
              f"· {s['nb_fichiers']} fich. · {s['mu']:>7.1f} MU · {s['champ'][:18]:<18} "
              f"· clôt sur {s['cloture']}")
        if s["doute"]:
            print(f"        ⚠ {s['doute']}")
    if len(seances) > 20:
        print(f"  … et {len(seances) - 20} autres")

    if args.sortie:
        dossier = pathlib.Path(args.sortie)
        dossier.mkdir(parents=True, exist_ok=True)
        chemin = dossier / "seances_horloge.csv"
        with open(chemin, "w", newline="", encoding="utf-8-sig") as fichier:
            ecrivain = csv.DictWriter(fichier, fieldnames=list(seances[0]),
                                      delimiter=";", extrasaction="ignore")
            ecrivain.writeheader()
            ecrivain.writerows(seances)
        print(f"\nÉcrit : {chemin}")
        print("  Contient des identifiants de champ de traitement : "
              "à traiter comme des données sensibles.")

    if args.comparer:
        comparer(seances, args.sources, args.filtre,
                 args.ecart_max, args.seuil_complet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
