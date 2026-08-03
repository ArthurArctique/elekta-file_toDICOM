"""Explore un RT Plan DICOM dans le navigateur : plan, faisceaux, points de
contrôle, ouverture du collimateur, et les tags bruts derrière chaque écran.

    python3 exploration/visualiser_rtplan.py plan.dcm
    python3 exploration/visualiser_rtplan.py plan.dcm --port 8060

Tout se passe en local : le fichier n'est lu que sur ce poste, aucune requête ne
sort, rien n'est écrit sur disque. Les champs identifiants (nom, ID, dates de
naissance, médecin, établissement) sont **masqués par défaut** ; une case à
cocher les révèle, à n'utiliser que si l'écran n'est vu par personne d'autre.

Pourquoi cet outil
------------------
Avant de confronter un plan aux logs machine, il faut savoir ce que le plan
contient vraiment : combien de MU par faisceau, combien de points de contrôle,
si le bras tourne, comment les lames sont numérotées. Ces réponses sont dans le
DICOM mais éparpillées entre trois séquences imbriquées, et certaines valeurs
ne sont écrites qu'une fois puis sous-entendues.

Trois pièges que cette page rend visibles
-----------------------------------------
1. **Les MU ne sont pas dans le faisceau.** `BeamSequence` ne porte qu'un poids
   relatif (`CumulativeMetersetWeight`, de 0 à `FinalCumulativeMetersetWeight`).
   Les MU absolues sont ailleurs, dans `FractionGroupSequence` →
   `ReferencedBeamSequence` → `BeamMeterset` (300A,0086).

2. **Un point de contrôle n'écrit que ce qui change.** Le premier porte tout,
   les suivants seulement les valeurs modifiées. Lire `cp.GantryAngle` sans
   propager la dernière valeur connue donne des trous — ici tout est déplié.

3. **Les positions de lames sont un seul tableau à plat.** `LeafJawPositions`
   (300A,011C) contient les 2 × N lames bout à bout : les N premières pour le
   banc 1, les N suivantes pour le banc 2. Rien dans le tag ne le dit.
"""

import argparse
import sys

import numpy as np
import plotly.graph_objects as go
import pydicom
from dash import Dash, Input, Output, dash_table, dcc, html

# Champs ré-identifiants : masqués tant que la case n'est pas cochée.
IDENTIFIANTS = {
    "PatientName", "PatientID", "PatientBirthDate", "PatientSex",
    "PatientAge", "OtherPatientIDs", "OtherPatientNames",
    "ReferringPhysicianName", "PhysiciansOfRecord", "OperatorsName",
    "InstitutionName", "InstitutionAddress", "StationName",
    "StudyID", "AccessionNumber", "RTPlanName", "RTPlanLabel",
}

# Séquences trop volumineuses pour un tableau de tags : montrées à part.
SEQUENCES_LOURDES = {"BeamSequence", "ControlPointSequence",
                     "FractionGroupSequence", "PatientSetupSequence"}

VIDE = "—"


def texte(valeur):
    if valeur is None:
        return VIDE
    return str(valeur)


def deplier(faisceau):
    """Rend les points de contrôle complets, valeur par valeur.

    Le DICOM n'écrit dans un point de contrôle que ce qui diffère du précédent.
    On reporte donc la dernière valeur connue, sans quoi la moitié des angles
    de bras d'un arc apparaissent comme absents.
    """
    courant, points = {}, []
    scalaires = ("GantryAngle", "GantryRotationDirection", "BeamLimitingDeviceAngle",
                 "PatientSupportAngle", "TableTopEccentricAngle", "DoseRateSet",
                 "NominalBeamEnergy", "SourceToSurfaceDistance")

    for numero, cp in enumerate(faisceau.ControlPointSequence):
        for cle in scalaires:
            if cle in cp:
                courant[cle] = cp[cle].value
        for item in getattr(cp, "BeamLimitingDevicePositionSequence", []):
            courant[item.RTBeamLimitingDeviceType] = \
                [float(v) for v in item.LeafJawPositions]

        points.append({"numero": numero,
                       "poids": float(cp.CumulativeMetersetWeight),
                       **{cle: valeur for cle, valeur in courant.items()}})
    return points


def metersets(plan):
    """MU absolues par numéro de faisceau, prises là où elles sont réellement."""
    table = {}
    for groupe in getattr(plan, "FractionGroupSequence", []):
        for reference in getattr(groupe, "ReferencedBeamSequence", []):
            if "BeamMeterset" in reference:
                table[int(reference.ReferencedBeamNumber)] = \
                    float(reference.BeamMeterset)
    return table


def groupes_de_fractions(plan):
    """Un plan peut découper le traitement en plusieurs groupes de fractions.

    Chacun a son propre nombre de fractions et ses propres faisceaux — un boost
    de 4 séances après 16 séances de traitement, par exemple. Sommer toutes les
    MU puis multiplier par le nombre de fractions du premier groupe donnerait un
    total faux : le cas se présente dans les données de référence.
    """
    groupes = []
    for groupe in getattr(plan, "FractionGroupSequence", []):
        mu = sum(float(r.BeamMeterset)
                 for r in getattr(groupe, "ReferencedBeamSequence", [])
                 if "BeamMeterset" in r)
        fractions = getattr(groupe, "NumberOfFractionsPlanned", None)
        groupes.append({
            "numero": int(getattr(groupe, "FractionGroupNumber", len(groupes) + 1)),
            "fractions": int(fractions) if fractions is not None else None,
            "mu_par_fraction": mu,
            "faisceaux": [int(r.ReferencedBeamNumber)
                          for r in getattr(groupe, "ReferencedBeamSequence", [])],
        })
    return groupes


def geometrie_mlc(faisceau):
    """Bornes des lames et nombre de paires, depuis le descriptif du MLC."""
    for appareil in getattr(faisceau, "BeamLimitingDeviceSequence", []):
        if appareil.RTBeamLimitingDeviceType.startswith("MLC"):
            bornes = [float(v) for v in appareil.LeafPositionBoundaries]
            return bornes, int(appareil.NumberOfLeafJawPairs)
    return None, 0


def resume_faisceau(faisceau, mu):
    """Une ligne de tableau par faisceau."""
    points = deplier(faisceau)
    angles = [p.get("GantryAngle") for p in points if p.get("GantryAngle") is not None]
    angles = [float(a) for a in angles]
    etendue = max(angles) - min(angles) if angles else 0.0
    # Un arc se reconnaît à ce que le bras bouge, pas au nom du faisceau.
    rotatif = len({round(a) for a in angles}) > 5
    _, paires = geometrie_mlc(faisceau)

    return {
        "n°": int(faisceau.BeamNumber),
        "nom": texte(getattr(faisceau, "BeamName", None)),
        "type": texte(getattr(faisceau, "BeamType", None)),
        "technique": "arc" if rotatif else "bras fixe",
        "MU": round(mu, 1) if mu is not None else VIDE,
        "points de contrôle": len(points),
        "énergie": texte(points[0].get("NominalBeamEnergy") if points else None),
        "bras": f"{min(angles):.1f}° → {max(angles):.1f}°" if angles else VIDE,
        "étendue": f"{etendue:.0f}°" if angles else VIDE,
        "collimateur": texte(points[0].get("BeamLimitingDeviceAngle") if points else None),
        "paires de lames": paires or VIDE,
    }


def carte(titre, valeur, note=""):
    return html.Div([
        html.Div(titre, style={"fontSize": "11px", "textTransform": "uppercase",
                               "letterSpacing": ".06em", "opacity": .6}),
        html.Div(valeur, style={"fontSize": "22px", "fontWeight": 600,
                                "margin": "2px 0"}),
        html.Div(note, style={"fontSize": "11px", "opacity": .55}),
    ], style={"padding": "10px 14px", "border": "1px solid rgba(128,128,128,.35)",
              "borderRadius": "8px", "minWidth": "150px", "flex": "1"})


def figure_trajectoire(points, mu_total):
    """Ce que le plan demande, le long de l'axe des MU cumulées.

    L'axe des MU est le seul commun au plan et au log machine : c'est sur lui
    que se fera plus tard toute mise en correspondance.
    """
    mus = [p["poids"] * mu_total for p in points] if mu_total else \
          [p["poids"] for p in points]
    angles = [p.get("GantryAngle") for p in points]
    figure = go.Figure()

    if any(a is not None for a in angles):
        brut = np.array([float(a) if a is not None else np.nan for a in angles])
        # Un arc qui franchit 0° retomberait de 359° à 1° : déroulé, sinon la
        # courbe montre une chute verticale là où le bras n'a bougé que d'un
        # degré. C'est le même piège qu'à l'interpolation des logs.
        continu = np.degrees(np.unwrap(np.radians(brut)))
        figure.add_trace(go.Scatter(
            x=mus, y=continu, mode="lines+markers", marker={"size": 4},
            customdata=np.mod(continu, 360.0),
            hovertemplate="%{x:.1f} MU · %{customdata:.1f}°<extra></extra>"))

    figure.update_layout(
        xaxis_title="MU cumulées" if mu_total else "poids cumulé",
        yaxis_title="angle de bras, déroulé (°)", height=300,
        margin={"l": 60, "r": 20, "t": 30, "b": 45},
        template="plotly_white", showlegend=False)
    return figure


def figure_mu(points, mu_total):
    """MU délivrées entre deux points de contrôle : régulières ou non ?"""
    mus = np.array([p["poids"] for p in points]) * (mu_total or 1.0)
    pas = np.diff(mus, prepend=0.0)
    figure = go.Figure(go.Bar(x=[p["numero"] for p in points], y=pas))
    figure.update_layout(
        xaxis_title="point de contrôle",
        yaxis_title="MU du segment" if mu_total else "poids du segment",
        height=250, margin={"l": 60, "r": 20, "t": 30, "b": 45},
        template="plotly_white")
    return figure


def figure_ouverture(point, bornes, paires):
    """L'ouverture vue depuis la source, lame par lame.

    Les lames sont dessinées à leur vraie hauteur : c'est ce qui rend visible
    que `LeafJawPositions` est un seul tableau où le banc 1 précède le banc 2.
    """
    figure = go.Figure()
    lames = point.get("MLCX") or point.get("MLCY")
    machoires_y = point.get("ASYMY") or point.get("Y")
    machoires_x = point.get("ASYMX") or point.get("X")

    if lames and bornes and paires:
        banc1, banc2 = lames[:paires], lames[paires:]
        for i in range(paires):
            bas, haut = bornes[i], bornes[i + 1]
            for depart, arrivee, couleur in (
                    (-200.0, banc1[i], "rgba(90,120,200,.55)"),
                    (banc2[i], 200.0, "rgba(200,110,90,.55)")):
                figure.add_shape(type="rect", x0=depart, x1=arrivee,
                                 y0=bas, y1=haut, line={"width": .4,
                                                        "color": "rgba(60,60,60,.5)"},
                                 fillcolor=couleur, layer="below")

    for machoires, axe in ((machoires_x, "x"), (machoires_y, "y")):
        if machoires and len(machoires) == 2:
            for position in machoires:
                figure.add_shape(
                    type="line", line={"width": 2, "dash": "dot", "color": "#444"},
                    **({"x0": position, "x1": position, "y0": -220, "y1": 220}
                       if axe == "x" else
                       {"y0": position, "y1": position, "x0": -220, "x1": 220}))

    figure.update_layout(
        height=520, template="plotly_white",
        margin={"l": 55, "r": 20, "t": 30, "b": 45},
        xaxis={"title": "x (mm)", "range": [-210, 210], "constrain": "domain"},
        yaxis={"title": "y (mm)", "range": [-210, 210],
               "scaleanchor": "x", "scaleratio": 1})
    return figure


def tags(dataset, montrer_identite, sauf=SEQUENCES_LOURDES):
    """Les tags d'un niveau, en clair, sans les grosses séquences."""
    lignes = []
    for element in dataset:
        if element.keyword in sauf:
            valeur = f"séquence de {len(element.value)} élément(s)"
        elif element.keyword in IDENTIFIANTS and not montrer_identite:
            valeur = "•••••  (masqué)"
        elif element.VR == "SQ":
            valeur = f"séquence de {len(element.value)} élément(s)"
        else:
            valeur = str(element.value)
        lignes.append({
            "tag": str(element.tag), "nom": element.keyword or element.name,
            "VR": element.VR, "valeur": valeur[:180],
        })
    return lignes


TABLEAU = {
    "style_cell": {"fontFamily": "ui-monospace, SFMono-Regular, monospace",
                   "fontSize": "12px", "textAlign": "left", "padding": "5px 8px"},
    "style_header": {"fontWeight": "600", "textTransform": "uppercase",
                     "fontSize": "10px", "letterSpacing": ".05em"},
    "style_table": {"overflowX": "auto"},
}


def construire(plan, chemin):
    mu_par_faisceau = metersets(plan)
    faisceaux = list(plan.BeamSequence)
    resumes = [resume_faisceau(f, mu_par_faisceau.get(int(f.BeamNumber)))
               for f in faisceaux]
    groupes = groupes_de_fractions(plan)
    total_mu = sum(mu_par_faisceau.values())
    # Chaque groupe apporte ses MU autant de fois qu'il a de fractions.
    total_traitement = sum(g["mu_par_fraction"] * g["fractions"]
                           for g in groupes if g["fractions"])
    if len(groupes) == 1:
        libelle_fractions = texte(groupes[0]["fractions"])
        note_fractions = ""
    else:
        libelle_fractions = " + ".join(texte(g["fractions"]) for g in groupes)
        note_fractions = f"{len(groupes)} groupes de fractions"

    application = Dash(__name__, title="RT Plan")
    application.layout = html.Div([
        html.H2("RT Plan", style={"marginBottom": "2px"}),
        html.Div(chemin, style={"fontSize": "12px", "opacity": .6,
                                "fontFamily": "monospace"}),

        html.Div([
            carte("faisceaux", str(len(faisceaux))),
            carte("MU, tous faisceaux", f"{total_mu:.1f}" if total_mu else VIDE,
                  "300A,0086 · hors du faisceau"),
            carte("fractions", libelle_fractions, note_fractions),
            carte("MU du traitement",
                  f"{total_traitement:.0f}" if total_traitement else VIDE),
            carte("points de contrôle",
                  str(sum(r["points de contrôle"] for r in resumes))),
        ], style={"display": "flex", "gap": "10px", "margin": "16px 0"}),

        html.Div([
            html.Div(f"groupe {g['numero']} · {texte(g['fractions'])} fractions · "
                     f"{g['mu_par_fraction']:.1f} MU · "
                     f"faisceaux {', '.join(str(n) for n in g['faisceaux'])}",
                     style={"fontSize": "12px", "opacity": .7})
            for g in groupes
        ] if len(groupes) > 1 else [], style={"margin": "-6px 0 14px"}),

        dcc.Checklist(
            id="identite", options=[{"label": "  afficher les champs identifiants",
                                     "value": "oui"}], value=[],
            style={"fontSize": "12px", "margin": "0 0 14px"}),

        html.H4("Faisceaux"),
        dash_table.DataTable(
            data=resumes, id="table-faisceaux",
            columns=[{"name": c, "id": c} for c in resumes[0]],
            row_selectable="single", selected_rows=[0], **TABLEAU),

        html.H4("Points de contrôle du faisceau retenu",
                style={"marginTop": "26px"}),
        dcc.Graph(id="trajectoire"),
        dcc.Graph(id="mu"),

        html.H4("Ouverture du collimateur", style={"marginTop": "26px"}),
        html.Div(id="legende-cp", style={"fontSize": "12px", "opacity": .7}),
        dcc.Slider(id="cp", min=0, max=1, step=1, value=0,
                   tooltip={"placement": "bottom", "always_visible": False}),
        dcc.Graph(id="ouverture"),

        html.H4("Tags DICOM", style={"marginTop": "26px"}),
        dcc.Tabs(id="niveau", value="plan", children=[
            dcc.Tab(label="plan", value="plan"),
            dcc.Tab(label="faisceau retenu", value="faisceau"),
            dcc.Tab(label="point de contrôle retenu", value="cp"),
        ]),
        dash_table.DataTable(id="tags", page_size=25,
                             columns=[{"name": c, "id": c}
                                      for c in ("tag", "nom", "VR", "valeur")],
                             **TABLEAU),

        html.P("Lu en local, rien n'est transmis ni écrit. Les noms de champ et "
               "de site restent ré-identifiants via le R&V.",
               style={"fontSize": "11px", "opacity": .55, "marginTop": "28px"}),
    ], style={"maxWidth": "1100px", "margin": "0 auto", "padding": "24px",
              "fontFamily": "system-ui, -apple-system, sans-serif",
              # Les graphiques sont en thème clair : la page l'est aussi, sinon
              # elle devient illisible dans un navigateur réglé en sombre.
              "background": "#fff", "color": "#1a1a1a", "minHeight": "100vh"})

    def choisi(lignes):
        return faisceaux[lignes[0] if lignes else 0]

    @application.callback(
        Output("trajectoire", "figure"), Output("mu", "figure"),
        Output("cp", "max"), Output("cp", "marks"),
        Input("table-faisceaux", "selected_rows"))
    def _trajectoire(lignes):
        faisceau = choisi(lignes)
        points = deplier(faisceau)
        mu = mu_par_faisceau.get(int(faisceau.BeamNumber))
        dernier = len(points) - 1
        pas = max(1, dernier // 8)
        marques = {i: str(i) for i in range(0, dernier + 1, pas)}
        return (figure_trajectoire(points, mu), figure_mu(points, mu),
                dernier, marques)

    @application.callback(
        Output("ouverture", "figure"), Output("legende-cp", "children"),
        Input("table-faisceaux", "selected_rows"), Input("cp", "value"))
    def _ouverture(lignes, index):
        faisceau = choisi(lignes)
        points = deplier(faisceau)
        index = min(int(index or 0), len(points) - 1)
        point = points[index]
        bornes, paires = geometrie_mlc(faisceau)
        mu = mu_par_faisceau.get(int(faisceau.BeamNumber))
        cumul = point["poids"] * mu if mu else point["poids"]
        legende = (f"point {index}/{len(points) - 1} · "
                   f"{'MU cumulées' if mu else 'poids cumulé'} {cumul:.1f} · "
                   f"bras {texte(point.get('GantryAngle'))}° · "
                   f"collimateur {texte(point.get('BeamLimitingDeviceAngle'))}°")
        return figure_ouverture(point, bornes, paires), legende

    @application.callback(
        Output("tags", "data"),
        Input("niveau", "value"), Input("table-faisceaux", "selected_rows"),
        Input("cp", "value"), Input("identite", "value"))
    def _tags(niveau, lignes, index, identite):
        montrer = "oui" in (identite or [])
        if niveau == "plan":
            return tags(plan, montrer)
        faisceau = choisi(lignes)
        if niveau == "faisceau":
            return tags(faisceau, montrer)
        sequence = faisceau.ControlPointSequence
        return tags(sequence[min(int(index or 0), len(sequence) - 1)], montrer,
                    sauf=set())

    return application


def main():
    analyseur = argparse.ArgumentParser(
        description="Explore un RT Plan DICOM dans le navigateur.",
        epilog="Le fichier est lu en local ; aucune donnée ne sort du poste.")
    analyseur.add_argument("plan", help="RT Plan DICOM (.dcm)")
    analyseur.add_argument("--port", type=int, default=8050)
    args = analyseur.parse_args()

    plan = pydicom.dcmread(args.plan, force=True)
    if "BeamSequence" not in plan:
        raise SystemExit(f"{args.plan} ne contient pas de BeamSequence : "
                         "ce n'est pas un RT Plan.")

    print(f"  {len(plan.BeamSequence)} faisceau(x) · "
          f"http://127.0.0.1:{args.port}")
    construire(plan, args.plan).run(debug=False, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
