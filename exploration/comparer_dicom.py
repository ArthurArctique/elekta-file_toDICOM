"""Confronte plusieurs RT Plan DICOM dans le navigateur.

    python3 exploration/comparer_dicom.py plan.dcm delivres/
    python3 exploration/comparer_dicom.py plan.dcm f1.dcm f2.dcm --port 8060

Pensé pour l'usage qui suit `seance_vers_dicom.py` : mettre le plan d'origine
face aux fractions réellement délivrées, et les fractions entre elles. Répond à
deux questions différentes —

  « la délivrance a-t-elle suivi le plan ? »    plan contre chaque fraction
  « la machine est-elle constante ? »           fractions entre elles

La seconde est souvent la plus parlante : un écart au plan peut être une
propriété normale de la machine (le retard du servomoteur en est une), alors
qu'une fraction qui s'écarte des autres signale quelque chose de ce jour-là.

Ce que la comparaison suppose
-----------------------------
Que les fichiers partagent la **même grille de points de contrôle** — même
nombre de faisceaux, même nombre de points par faisceau. C'est le cas par
construction pour les fichiers produits par `seance_vers_dicom.py`, qui
substituent dans la grille du plan. Les fichiers qui ne collent pas sont
affichés mais exclus des comparaisons, avec la raison.

Tout se passe en local : rien n'est transmis, rien n'est écrit. Les champs
identifiants sont masqués par défaut.
"""

import argparse
import pathlib
import sys

import numpy as np
import plotly.graph_objects as go
import pydicom
from dash import Dash, Input, Output, dash_table, dcc, html

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from seance_vers_dicom import paires_ouvertes  # noqa: E402
from visualiser_rtplan import (TABLEAU, VIDE, carte,  # noqa: E402
                               deplier, geometrie_mlc, metersets, texte)

COULEURS = ("#3b6fd4", "#d4663b", "#3ba55c", "#a53b96", "#c9a227", "#00838f")
GRIS = "#9aa0a6"


def profil(chemin, fraction):
    """Aplatit un plan en séries comparables : un point par point de contrôle.

    Les faisceaux sont mis bout à bout sur un axe de MU cumulé continu, comme
    la machine les délivre — c'est le même axe que partout ailleurs dans le
    projet, et le seul sur lequel deux fichiers se superposent.
    """
    dataset = pydicom.dcmread(chemin, force=True)
    if "BeamSequence" not in dataset:
        return {"nom": pathlib.Path(chemin).name, "erreur": "pas un RT Plan"}

    mu_faisceau = metersets(dataset)
    lames, mus, bras, collimateur, decoupe = [], [], [], [], []
    decalage = 0.0

    for faisceau in dataset.BeamSequence:
        numero = int(faisceau.BeamNumber)
        if numero not in mu_faisceau:
            continue
        total = mu_faisceau[numero]
        finale = float(faisceau.FinalCumulativeMetersetWeight)
        points = deplier(faisceau)
        debut = len(mus)
        for point in points:
            positions = point.get("MLCX")
            if positions is None:
                continue
            lames.append(np.array(positions, dtype=float))
            mus.append(decalage + total * point["poids"] / finale)
            bras.append(float(point.get("GantryAngle") or 0.0))
            collimateur.append(float(point.get("BeamLimitingDeviceAngle") or 0.0))
        decoupe.append((numero, debut, len(mus)))
        decalage += total

    if not lames:
        return {"nom": pathlib.Path(chemin).name, "erreur": "aucune position MLCX"}

    delivre = (str(getattr(dataset, "ApprovalStatus", "")) == "UNAPPROVED"
               and "econstitue" in str(getattr(dataset, "RTPlanDescription", "")))
    bornes, paires = geometrie_mlc(dataset.BeamSequence[0])

    return {
        "nom": pathlib.Path(chemin).name,
        "chemin": str(chemin),
        "dataset": dataset,
        "role": "délivré" if delivre else "plan",
        "lames": np.array(lames),                  # (n_points, 2 × paires)
        "mu": np.array(mus),
        "bras": np.array(bras),
        "collimateur": np.array(collimateur),
        "decoupe": decoupe,
        "bornes": bornes,
        "paires": paires,
        "mu_total": decalage,
        "faisceaux": len(decoupe),
        "erreur": None,
    }


def comparables(profils, reference):
    """Sépare ce qui se compare de ce qui ne se compare pas, et dit pourquoi."""
    forme = profils[reference]["lames"].shape
    etat = {}
    for nom, p in profils.items():
        if p.get("erreur"):
            etat[nom] = p["erreur"]
        elif p["lames"].shape != forme:
            etat[nom] = (f"grille différente : {p['lames'].shape[0]} points de "
                         f"{p['lames'].shape[1] // 2} paires contre "
                         f"{forme[0]} de {forme[1] // 2}")
        else:
            etat[nom] = None
    return etat


def ecarts_par_point(reference, autre):
    """Écart de lames point par point, restreint aux lames dans le champ.

    Les lames garées ne bougent pas et sont donc parfaitement conformes : les
    inclure ferait surtout mesurer combien de lames ne servent pas.
    """
    masque = paires_ouvertes(reference["lames"])
    ecart = np.abs(autre["lames"] - reference["lames"])
    medianes, p95 = [], []
    for i in range(len(ecart)):
        retenues = ecart[i][masque[i]]
        if len(retenues) == 0:
            retenues = ecart[i]
        medianes.append(np.median(retenues))
        p95.append(np.percentile(retenues, 95))
    return np.array(medianes), np.array(p95), ecart[masque]


def escalier(positions, bornes, paires):
    """Le bord d'un banc de lames, en marches — une marche par lame.

    Superposer deux escaliers se lit d'un coup d'œil, là où superposer deux fois
    cent-soixante rectangles ne donne rien.
    """
    xs, ys = [], []
    for i in range(paires):
        xs += [positions[i], positions[i]]
        ys += [bornes[i], bornes[i + 1]]
    return xs, ys


def figure_ecart(profils, reference, choisis, etat):
    figure = go.Figure()
    ref = profils[reference]
    for rang, nom in enumerate(choisis):
        if nom == reference or etat.get(nom):
            continue
        medianes, p95, _ = ecarts_par_point(ref, profils[nom])
        couleur = COULEURS[rang % len(COULEURS)]
        figure.add_trace(go.Scatter(x=ref["mu"], y=p95, mode="lines",
                                    line={"width": 0}, showlegend=False,
                                    hoverinfo="skip"))
        figure.add_trace(go.Scatter(
            x=ref["mu"], y=medianes, mode="lines", name=nom,
            line={"color": couleur, "width": 1.8},
            hovertemplate="%{x:.0f} MU · %{y:.2f} mm<extra>" + nom + "</extra>"))
    for _, _, fin in ref["decoupe"][:-1]:
        figure.add_vline(x=ref["mu"][fin - 1], line={"width": 1, "dash": "dot",
                                                     "color": GRIS})
    figure.update_layout(
        xaxis_title="MU cumulées (référence)",
        yaxis_title="écart des lames, médiane (mm)",
        height=320, template="plotly_white",
        margin={"l": 60, "r": 20, "t": 30, "b": 45},
        legend={"orientation": "h", "y": -0.25})
    return figure


def figure_bras(profils, reference, choisis, etat):
    figure = go.Figure()
    ref = profils[reference]
    for rang, nom in enumerate(choisis):
        if nom == reference or etat.get(nom):
            continue
        # écart angulaire signé, ramené dans ]-180, 180]
        brut = profils[nom]["bras"] - ref["bras"]
        ecart = (brut + 180.0) % 360.0 - 180.0
        figure.add_trace(go.Scatter(
            x=ref["mu"], y=ecart, mode="lines", name=nom,
            line={"color": COULEURS[rang % len(COULEURS)], "width": 1.5}))
    figure.update_layout(
        xaxis_title="MU cumulées (référence)", yaxis_title="écart de bras (°)",
        height=250, template="plotly_white", showlegend=False,
        margin={"l": 60, "r": 20, "t": 30, "b": 45})
    return figure


def figure_superposition(profils, reference, choisis, etat, index):
    figure = go.Figure()
    ref = profils[reference]
    bornes, paires = ref["bornes"], ref["paires"]
    index = min(index, len(ref["lames"]) - 1)

    if bornes:
        for banc in (0, 1):
            debut = banc * paires
            xs, ys = escalier(ref["lames"][index][debut:debut + paires],
                              bornes, paires)
            figure.add_trace(go.Scatter(
                x=xs, y=ys, mode="lines", name=f"{reference} (réf.)",
                line={"color": GRIS, "width": 2.5}, showlegend=banc == 0,
                legendgroup="ref"))

        for rang, nom in enumerate(choisis):
            if nom == reference or etat.get(nom):
                continue
            couleur = COULEURS[rang % len(COULEURS)]
            for banc in (0, 1):
                debut = banc * paires
                xs, ys = escalier(profils[nom]["lames"][index][debut:debut + paires],
                                  bornes, paires)
                figure.add_trace(go.Scatter(
                    x=xs, y=ys, mode="lines", name=nom,
                    line={"color": couleur, "width": 1.4},
                    showlegend=banc == 0, legendgroup=nom))

    figure.update_layout(
        height=560, template="plotly_white",
        margin={"l": 55, "r": 20, "t": 30, "b": 45},
        legend={"orientation": "h", "y": -0.14},
        xaxis={"title": "x (mm)", "range": [-210, 210], "constrain": "domain"},
        yaxis={"title": "y (mm)", "range": [-210, 210],
               "scaleanchor": "x", "scaleratio": 1})
    return figure


def pics(profils, reference, choisis, etat, combien=10):
    """Les points de contrôle les plus déviants, et de quoi les interpréter.

    Un pic isolé ne dit pas grand-chose ; deux questions le qualifient.

    **Revient-il à chaque fraction ?** Si oui il est structurel — il tient au
    plan ou à la méthode, pas à la journée. Sinon il s'est passé quelque chose
    ce jour-là, et c'est bien plus intéressant.

    **Combien de dose ce segment délivre-t-il, pour combien de mouvement ?**
    Les MU sont le seul axe dont on dispose pour recaler le log sur le plan. Un
    point de contrôle où les lames parcourent 20 mm pour 1 MU y est presque
    dégénéré : une incertitude minime sur les MU s'y traduit par un grand écart
    de position. Mesuré sur les données de référence : les points déviants
    délivrent 1,0 MU pour 14 à 22 mm de course, contre 5,0 MU pour 1,1 mm sur
    les points calmes — corrélation de 0,64 entre l'écart et les mm par MU.
    Leur poids dosimétrique est faible par construction, puisqu'ils ne
    délivrent presque rien.
    """
    ref = profils[reference]
    retenus = [n for n in choisis if n != reference and not etat.get(n)]
    if not retenus:
        return []

    masque = paires_ouvertes(ref["lames"])
    dmu = np.diff(ref["mu"], prepend=0.0)
    course = np.abs(np.diff(ref["lames"], axis=0, prepend=ref["lames"][:1]))
    vitesse = np.array([np.median(course[i][masque[i]]) / max(dmu[i], 1e-6)
                        for i in range(len(ref["mu"]))])

    par_fraction = {}
    for nom in retenus:
        _, p95, _ = ecarts_par_point(ref, profils[nom])
        par_fraction[nom] = p95

    pire = np.max(np.array(list(par_fraction.values())), axis=0)
    classement = np.argsort(-pire)[:combien]

    lignes = []
    for i in sorted(classement):
        # « commun » : déviant dans toutes les fractions, pas seulement la pire
        seuil = max(1.0, 0.5 * float(pire[i]))
        partout = all(p95[i] >= seuil for p95 in par_fraction.values())
        lignes.append({
            "point": int(i),
            "MU du segment": f"{dmu[i]:.2f}",
            "course des lames": f"{vitesse[i]:.1f} mm/MU",
            "écart p95 max": f"{pire[i]:.1f} mm",
            "par fraction": " · ".join(f"{p95[i]:.1f}" for p95 in par_fraction.values()),
            "lecture": ("structurel — revient à chaque fraction"
                        if partout else "varie d'une fraction à l'autre"),
        })
    return lignes


def bilan(profils, reference, choisis, etat):
    ref = profils[reference]
    lignes = []
    for nom in choisis:
        if nom == reference:
            continue
        if etat.get(nom):
            lignes.append({"fichier": nom, "rôle": profils[nom].get("role", VIDE),
                           "MU": VIDE, "médiane": VIDE, "p95": VIDE, "max": VIDE,
                           "remarque": etat[nom]})
            continue
        _, _, toutes = ecarts_par_point(ref, profils[nom])
        mu = profils[nom]["mu_total"]
        lignes.append({
            "fichier": nom, "rôle": profils[nom]["role"],
            "MU": f"{mu:.1f}",
            "médiane": f"{np.median(toutes):.2f} mm",
            "p95": f"{np.percentile(toutes, 95):.2f} mm",
            "max": f"{toutes.max():.2f} mm",
            "remarque": f"{100 * (mu - ref['mu_total']) / ref['mu_total']:+.2f} % de MU",
        })
    return lignes


def construire(profils, ordre, defaut):
    inventaire = [{
        "fichier": p["nom"], "rôle": p.get("role", VIDE),
        "faisceaux": p.get("faisceaux", VIDE),
        "points de contrôle": len(p["lames"]) if p.get("erreur") is None else VIDE,
        "MU": f"{p['mu_total']:.1f}" if p.get("erreur") is None else VIDE,
        "statut": texte(getattr(p.get("dataset"), "ApprovalStatus", None))
                  if p.get("dataset") is not None else VIDE,
        "lecture": p.get("erreur") or "ok",
    } for p in (profils[n] for n in ordre)]

    application = Dash(__name__, title="Comparer des RT Plan")
    application.layout = html.Div([
        html.H2("Comparer des RT Plan", style={"marginBottom": "2px"}),
        html.Div(f"{len(ordre)} fichier(s)",
                 style={"fontSize": "12px", "opacity": .6}),

        html.Div([
            carte("fichiers", str(len(ordre))),
            carte("référence", defaut, "les écarts sont comptés depuis elle"),
            carte("points de contrôle", str(len(profils[defaut]["lames"]))),
            carte("MU de la référence", f"{profils[defaut]['mu_total']:.1f}"),
        ], style={"display": "flex", "gap": "10px", "margin": "16px 0"}),

        dash_table.DataTable(data=inventaire, id="inventaire",
                             columns=[{"name": c, "id": c} for c in inventaire[0]],
                             **TABLEAU),

        html.Div([
            html.Div([
                html.Label("référence", style={"fontSize": "12px", "opacity": .7}),
                dcc.Dropdown(id="reference", options=ordre, value=defaut,
                             clearable=False),
            ], style={"flex": "1"}),
            html.Div([
                html.Label("comparés", style={"fontSize": "12px", "opacity": .7}),
                dcc.Dropdown(id="compares", options=ordre,
                             value=[n for n in ordre if n != defaut],
                             multi=True),
            ], style={"flex": "3"}),
        ], style={"display": "flex", "gap": "14px", "margin": "22px 0 6px"}),

        html.Div(id="avertissements",
                 style={"fontSize": "12px", "color": "#b45309", "margin": "6px 0"}),

        html.H4("Écart des lames le long de la délivrance",
                style={"marginTop": "20px"}),
        html.Div("Lames dans le champ seulement — les lames garées ne bougent pas "
                 "et seraient parfaitement conformes. Les pointillés verticaux "
                 "séparent les faisceaux.",
                 style={"fontSize": "12px", "opacity": .65}),
        dcc.Graph(id="ecart"),

        html.H4("Écart d'angle de bras", style={"marginTop": "18px"}),
        dcc.Graph(id="bras"),

        html.H4("Les points de contrôle les plus déviants",
                style={"marginTop": "22px"}),
        html.Div("Un pic qui revient à chaque fraction tient au plan ou à la "
                 "méthode. Un pic propre à une fraction s'est passé ce jour-là. "
                 "Une forte course de lames pour peu de MU rend le recalage sur "
                 "l'axe des MU mal conditionné : l'écart y est largement "
                 "méthodologique, et de faible poids dosimétrique.",
                 style={"fontSize": "12px", "opacity": .65}),
        dash_table.DataTable(id="pics", **TABLEAU,
                             columns=[{"name": c, "id": c} for c in
                                      ("point", "MU du segment", "course des lames",
                                       "écart p95 max", "par fraction", "lecture")]),

        html.H4("Superposition des ouvertures", style={"marginTop": "22px"}),
        html.Div(id="legende", style={"fontSize": "12px", "opacity": .7}),
        dcc.Slider(id="point", min=0, max=len(profils[defaut]["lames"]) - 1,
                   step=1, value=0,
                   tooltip={"placement": "bottom", "always_visible": False}),
        dcc.Graph(id="superposition"),

        html.H4("Bilan", style={"marginTop": "22px"}),
        dash_table.DataTable(id="bilan",
                             columns=[{"name": c, "id": c} for c in
                                      ("fichier", "rôle", "MU", "médiane",
                                       "p95", "max", "remarque")],
                             **TABLEAU),

        html.P("Lu en local, rien n'est transmis ni écrit.",
               style={"fontSize": "11px", "opacity": .55, "marginTop": "26px"}),
    ], style={"maxWidth": "1150px", "margin": "0 auto", "padding": "24px",
              "fontFamily": "system-ui, -apple-system, sans-serif",
              "background": "#fff", "color": "#1a1a1a", "minHeight": "100vh"})

    @application.callback(
        Output("ecart", "figure"), Output("bras", "figure"),
        Output("bilan", "data"), Output("pics", "data"),
        Output("avertissements", "children"), Output("point", "max"),
        Input("reference", "value"), Input("compares", "value"))
    def _comparer(reference, choisis):
        choisis = choisis or []
        etat = comparables(profils, reference)
        alertes = [f"{nom} : {raison}" for nom, raison in etat.items()
                   if raison and nom in choisis]
        return (figure_ecart(profils, reference, choisis, etat),
                figure_bras(profils, reference, choisis, etat),
                bilan(profils, reference, choisis, etat),
                pics(profils, reference, choisis, etat),
                " · ".join(alertes),
                len(profils[reference]["lames"]) - 1)

    @application.callback(
        Output("superposition", "figure"), Output("legende", "children"),
        Input("reference", "value"), Input("compares", "value"),
        Input("point", "value"))
    def _superposer(reference, choisis, index):
        choisis = choisis or []
        etat = comparables(profils, reference)
        ref = profils[reference]
        index = min(int(index or 0), len(ref["lames"]) - 1)
        legende = (f"point {index}/{len(ref['lames']) - 1} · "
                   f"{ref['mu'][index]:.1f} MU cumulées · "
                   f"bras {ref['bras'][index]:.1f}°")
        return (figure_superposition(profils, reference, choisis, etat, index),
                legende)

    return application


def rassembler(entrees):
    """Développe les dossiers en fichiers .dcm, en gardant l'ordre donné."""
    fichiers = []
    for entree in entrees:
        chemin = pathlib.Path(entree)
        if chemin.is_dir():
            fichiers += sorted(chemin.glob("*.dcm"))
        else:
            fichiers.append(chemin)
    vus, uniques = set(), []
    for f in fichiers:
        if str(f) not in vus:
            vus.add(str(f))
            uniques.append(f)
    return uniques


def main():
    analyseur = argparse.ArgumentParser(
        description="Confronte plusieurs RT Plan DICOM dans le navigateur.",
        epilog="Les fichiers sont lus en local ; aucune donnée ne sort du poste.")
    analyseur.add_argument("fichiers", nargs="+",
                           help="fichiers .dcm et/ou dossiers en contenant")
    analyseur.add_argument("--fraction", type=int, default=1,
                           help="groupe de fractions à lire (défaut : 1)")
    analyseur.add_argument("--port", type=int, default=8051)
    args = analyseur.parse_args()

    chemins = rassembler(args.fichiers)
    if len(chemins) < 2:
        raise SystemExit("Il en faut au moins deux à comparer.")

    profils, ordre = {}, []
    for chemin in chemins:
        p = profil(chemin, args.fraction)
        nom = p["nom"]
        while nom in profils:                    # deux dossiers, même nom
            nom += "'"
        p["nom"] = nom
        profils[nom], _ = p, ordre.append(nom)
        etat = p["erreur"] or (f"{len(p['lames'])} points · {p['mu_total']:.1f} MU")
        print(f"  {p.get('role', '?'):<8} {nom:<48} {etat}")

    lisibles = [n for n in ordre if profils[n]["erreur"] is None]
    if len(lisibles) < 2:
        raise SystemExit("Moins de deux fichiers exploitables.")
    # Le plan d'origine fait la référence par défaut ; à défaut, le premier lu.
    defaut = next((n for n in lisibles if profils[n]["role"] == "plan"), lisibles[0])

    print(f"\n  référence : {defaut}\n  http://127.0.0.1:{args.port}")
    construire(profils, ordre, defaut).run(debug=False, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
