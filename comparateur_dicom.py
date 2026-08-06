"""Confronte un RT Plan de référence aux plans délivrés d'un dossier.

    from comparateur_dicom import Comparateur
    Comparateur().lancer()                       # puis http://127.0.0.1:8053

Les deux chemins se choisissent **dans la page** : un bouton pour le plan de
référence, un autre pour le dossier des plans à comparer — typiquement celui
qu'a rempli `main.Chaine`. Tout est lu localement, rien n'est téléversé.

Les plans sont lus par `main.LecteurRtplan`, la trajectoire vient de sa méthode
`trajectoire()` : même dépliage des points de contrôle et mêmes conventions de
bancs que la chaîne qui a produit ces fichiers. Aucune seconde implémentation.

Deux questions, une seule page
------------------------------
En prenant le **plan** pour référence : la délivrance a-t-elle suivi le plan ?
En prenant une **fraction** pour référence : la machine est-elle constante ?

La seconde est souvent la plus parlante. Un écart au plan peut être une
propriété normale de la machine — le retard du servomoteur en est une —, alors
qu'une fraction qui s'écarte des autres signale quelque chose de ce jour-là.
Mesuré sur les données publiques : 0,20–0,22 mm au plan, 0,07–0,10 mm entre
fractions.
"""

import pathlib
import sys

import numpy as np
import plotly.graph_objects as go
from dash import (Dash, Input, Output, State, callback_context,
                  dash_table, dcc, html)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from main import PAIRES, LecteurRtplan  # noqa: E402
from visualiseur_seances import TABLEAU, carte, demander_chemin  # noqa: E402

COULEURS = ("#3b6fd4", "#d4663b", "#3ba55c", "#a53b96", "#c9a227", "#00838f")
GRIS = "#9aa0a6"
VIDE = "—"


def profil(chemin):
    """Un plan, aplati en séries comparables. `None` si illisible."""
    try:
        plan = LecteurRtplan(chemin)
        t = plan.trajectoire()
    except SystemExit as erreur:
        return {"nom": pathlib.Path(chemin).name, "erreur": str(erreur)}
    except Exception as erreur:
        return {"nom": pathlib.Path(chemin).name,
                "erreur": f"{type(erreur).__name__} : {erreur}"}

    ds = plan.ds
    derive = (str(getattr(ds, "ApprovalStatus", "")) == "UNAPPROVED"
              and "erive" in str(getattr(ds, "RTPlanDescription", "")))
    return {
        "nom": pathlib.Path(chemin).name, "chemin": str(chemin), "ds": ds,
        "role": "délivré" if derive else "plan",
        "mu": t["mu"], "lames": t["lames"], "bras": t["bras"],
        "decoupe": t["decoupe"], "mu_total": plan.mu_total(),
        "bornes": bornes_des_lames(ds), "erreur": None,
    }


def bornes_des_lames(ds):
    """Hauteurs des lames, pour les dessiner à l'échelle."""
    for faisceau in ds.BeamSequence:
        for appareil in getattr(faisceau, "BeamLimitingDeviceSequence", []):
            if appareil.RTBeamLimitingDeviceType == "MLCX":
                return [float(v) for v in appareil.LeafPositionBoundaries]
    return None


def paires_ouvertes(lames):
    """Les lames qui forment le champ, en convention Delivery.

    L'ouverture d'une paire y est la **somme** des deux bancs, comptés dans le
    même sens — à ne pas confondre avec le repère DICOM où c'est leur
    différence. Les lames garées ne bougent pas : les garder ferait surtout
    mesurer combien de lames ne servent pas.
    """
    ouvert = (lames[:, :, 0] + lames[:, :, 1]) > 5.0
    return np.stack([ouvert, ouvert], axis=2)


def comparables(profils, reference):
    """Ce qui se compare, et pourquoi le reste ne se compare pas."""
    forme = profils[reference]["lames"].shape
    etat = {}
    for nom, p in profils.items():
        if p.get("erreur"):
            etat[nom] = p["erreur"]
        elif p["lames"].shape != forme:
            etat[nom] = (f"grille différente : {p['lames'].shape[0]} points de "
                         f"{p['lames'].shape[1]} paires contre "
                         f"{forme[0]} de {forme[1]}")
        else:
            etat[nom] = None
    return etat


def ecarts(reference, autre):
    """Écart des lames point par point, restreint aux lames dans le champ."""
    masque = paires_ouvertes(reference["lames"])
    ecart = np.abs(autre["lames"] - reference["lames"])
    medianes, p95 = [], []
    for i in range(len(ecart)):
        retenues = ecart[i][masque[i]]
        if retenues.size == 0:
            retenues = ecart[i]
        medianes.append(np.median(retenues))
        p95.append(np.percentile(retenues, 95))
    return np.array(medianes), np.array(p95), ecart[masque]


def escalier(positions, bornes):
    """Le bord d'un banc, en marches — une par lame.

    Superposer deux escaliers se lit d'un coup d'œil, là où superposer deux fois
    cent-soixante rectangles ne donne rien.
    """
    xs, ys = [], []
    for i in range(min(len(positions), len(bornes) - 1)):
        xs += [positions[i], positions[i]]
        ys += [bornes[i], bornes[i + 1]]
    return xs, ys


def figure_ecart(profils, reference, choisis, etat):
    figure = go.Figure()
    ref = profils[reference]
    for rang, nom in enumerate(choisis):
        if nom == reference or etat.get(nom):
            continue
        medianes, _, _ = ecarts(ref, profils[nom])
        figure.add_trace(go.Scatter(
            x=ref["mu"], y=medianes, mode="lines", name=nom,
            line={"color": COULEURS[rang % len(COULEURS)], "width": 1.8},
            hovertemplate="%{x:.0f} MU · %{y:.2f} mm<extra>" + nom + "</extra>"))
    for _, _, fin in ref["decoupe"][:-1]:
        figure.add_vline(x=ref["mu"][fin - 1],
                         line={"width": 1, "dash": "dot", "color": GRIS})
    figure.update_layout(
        xaxis_title="MU cumulées (référence)",
        yaxis_title="écart des lames, médiane (mm)",
        height=320, template="plotly_white",
        margin={"l": 60, "r": 20, "t": 20, "b": 45},
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
        figure.add_trace(go.Scatter(
            x=ref["mu"], y=(brut + 180.0) % 360.0 - 180.0, mode="lines",
            name=nom, line={"color": COULEURS[rang % len(COULEURS)], "width": 1.5}))
    figure.update_layout(
        xaxis_title="MU cumulées (référence)", yaxis_title="écart de bras (°)",
        height=250, template="plotly_white", showlegend=False,
        margin={"l": 60, "r": 20, "t": 20, "b": 45})
    return figure

def figure_superposition(profils, reference, choisis, etat, index):
    figure = go.Figure()
    ref = profils[reference]
    bornes = ref["bornes"]
    index = min(index, len(ref["lames"]) - 1)

    if bornes:
        # En repère Delivery le banc 1 est compté à l'envers du DICOM : on
        # le renvoie pour redessiner les deux bords dans le même repère.
        for banc, signe in ((0, 1.0), (1, -1.0)):
            xs, ys = escalier(signe * ref["lames"][index][:, banc], bornes)
            figure.add_trace(go.Scatter(
                x=xs, y=ys, mode="lines", name=f"{reference} (réf.)",
                line={"color": GRIS, "width": 2.5},
                showlegend=banc == 0, legendgroup="ref"))
        for rang, nom in enumerate(choisis):
            if nom == reference or etat.get(nom):
                continue
            couleur = COULEURS[rang % len(COULEURS)]
            for banc, signe in ((0, 1.0), (1, -1.0)):
                xs, ys = escalier(signe * profils[nom]["lames"][index][:, banc],
                                  bornes)
                figure.add_trace(go.Scatter(
                    x=xs, y=ys, mode="lines", name=nom,
                    line={"color": couleur, "width": 1.4},
                    showlegend=banc == 0, legendgroup=nom))

    figure.update_layout(
        height=540, template="plotly_white",
        margin={"l": 55, "r": 20, "t": 20, "b": 45},
        legend={"orientation": "h", "y": -0.14},
        xaxis={"title": "x (mm)", "range": [-210, 210], "constrain": "domain"},
        yaxis={"title": "y (mm)", "range": [-210, 210],
               "scaleanchor": "x", "scaleratio": 1})
    return figure

def bilan(profils, reference, choisis, etat):
    ref = profils[reference]
    lignes = []
    for nom in choisis:
        if nom == reference:
            continue
        p = profils[nom]
        if etat.get(nom):
            lignes.append({"fichier": nom, "rôle": p.get("role", VIDE),
                           "MU": VIDE, "médiane": VIDE, "p95": VIDE,
                           "max": VIDE, "écart de MU": etat[nom]})
            continue
        _, _, toutes = ecarts(ref, p)
        lignes.append({
            "fichier": nom, "rôle": p["role"], "MU": f"{p['mu_total']:.1f}",
            "médiane": f"{np.median(toutes):.2f} mm",
            "p95": f"{np.percentile(toutes, 95):.2f} mm",
            "max": f"{toutes.max():.2f} mm",
            "écart de MU": f"{100 * (p['mu_total'] - ref['mu_total']) / ref['mu_total']:+.2f} %",
        })
    return lignes


class Comparateur:
    """La page : deux chemins à choisir, puis les écarts sous trois angles."""

    def __init__(self, reference="", dossier="", port=8053):
        self.defaut_reference = str(reference)
        self.defaut_dossier = str(dossier)
        self.profils, self.ordre = {}, []
        self.port = port
        self.app = Dash(__name__, title="Comparer des RT Plan")
        self.app.layout = self._mise_en_page()
        self._callbacks()

    # ---- présentation ----

    def _ligne(self, bouton, texte_bouton, champ, valeur, invite):
        return html.Div([
            html.Button(texte_bouton, id=bouton, n_clicks=0,
                        style={"padding": "8px 16px", "fontSize": "13px",
                               "cursor": "pointer", "borderRadius": "6px",
                               "border": "1px solid rgba(128,128,128,.45)",
                               "background": "#f4f4f5", "whiteSpace": "nowrap",
                               "minWidth": "230px"}),
            dcc.Input(id=champ, type="text", value=valeur, placeholder=invite,
                      debounce=True,
                      style={"flex": "1", "padding": "8px 10px", "fontSize": "13px",
                             "fontFamily": "ui-monospace, monospace",
                             "border": "1px solid rgba(128,128,128,.45)",
                             "borderRadius": "6px"}),
        ], style={"display": "flex", "gap": "8px", "marginBottom": "8px",
                  "alignItems": "center"})

    def _mise_en_page(self):
        return html.Div([
            html.H2("Comparer des RT Plan", style={"marginBottom": "2px"}),
            html.Div("Le plan d'origine face aux plans délivrés d'un dossier, "
                     "et les délivrances entre elles.",
                     style={"fontSize": "12px", "opacity": .6, "marginBottom": "16px"}),

            self._ligne("choisir_ref", "📄  Plan de référence…", "chemin_ref",
                        self.defaut_reference, "…ou coller le chemin du .dcm"),
            self._ligne("choisir_dossier", "📂  Dossier à comparer…", "chemin_dossier",
                        self.defaut_dossier, "…ou coller le chemin du dossier"),
            html.Button("Charger", id="charger", n_clicks=0,
                        style={"padding": "8px 22px", "fontSize": "13px",
                               "cursor": "pointer", "borderRadius": "6px",
                               "border": "1px solid rgba(128,128,128,.45)",
                               "background": "#e8eefc", "fontWeight": 600}),
            dcc.Loading(html.Div(id="etat", style={"fontSize": "12px",
                                                   "minHeight": "18px",
                                                   "marginTop": "10px"})),

            html.Div(id="contenu", style={"display": "none"}, children=[
                html.Div(id="cartes",
                         style={"display": "flex", "gap": "10px", "margin": "16px 0"}),
                dash_table.DataTable(id="inventaire", **TABLEAU,
                                     columns=[{"name": c, "id": c} for c in
                                              ("fichier", "rôle", "faisceaux",
                                               "points de contrôle", "MU", "lecture")]),

                html.Div([
                    html.Div([
                        html.Label("référence", style={"fontSize": "12px", "opacity": .7}),
                        dcc.Dropdown(id="reference", clearable=False),
                    ], style={"flex": "1"}),
                    html.Div([
                        html.Label("comparés", style={"fontSize": "12px", "opacity": .7}),
                        dcc.Dropdown(id="compares", multi=True),
                    ], style={"flex": "3"}),
                ], style={"display": "flex", "gap": "14px", "margin": "22px 0 6px"}),
                html.Div(id="alertes", style={"fontSize": "12px", "color": "#b45309"}),

                html.H4("Écart des lames le long de la délivrance",
                        style={"marginTop": "18px"}),
                html.Div("Lames dans le champ seulement. Les pointillés verticaux "
                         "séparent les faisceaux.",
                         style={"fontSize": "12px", "opacity": .65}),
                dcc.Graph(id="ecart"),

                html.H4("Écart d'angle de bras", style={"marginTop": "16px"}),
                dcc.Graph(id="bras"),

                html.H4("Superposition des ouvertures", style={"marginTop": "20px"}),
                html.Div(id="legende", style={"fontSize": "12px", "opacity": .7}),
                dcc.Slider(id="point", min=0, max=1, step=1, value=0,
                           tooltip={"placement": "bottom", "always_visible": False}),
                dcc.Graph(id="superposition"),

                html.H4("Bilan", style={"marginTop": "20px"}),
                dash_table.DataTable(id="bilan", sort_action="native", **TABLEAU,
                                     columns=[{"name": c, "id": c} for c in
                                              ("fichier", "rôle", "MU", "médiane",
                                               "p95", "max", "écart de MU")]),
            ]),

            html.P("Lu en local, rien n'est transmis ni écrit.",
                   style={"fontSize": "11px", "opacity": .55, "marginTop": "26px"}),
        ], style={"maxWidth": "1150px", "margin": "0 auto", "padding": "24px",
                  "fontFamily": "system-ui, -apple-system, sans-serif",
                  "background": "#fff", "color": "#1a1a1a", "minHeight": "100vh"})

    # ---- figures : les fonctions de module font le travail ----

    def _figure_ecart(self, reference, choisis, etat):
        return figure_ecart(self.profils, reference, choisis, etat)

    def _figure_bras(self, reference, choisis, etat):
        return figure_bras(self.profils, reference, choisis, etat)

    def _figure_superposition(self, reference, choisis, etat, index):
        return figure_superposition(self.profils, reference, choisis, etat, index)

    def _bilan(self, reference, choisis, etat):
        return bilan(self.profils, reference, choisis, etat)

    # ---- callbacks ----

    def _callbacks(self):

        @self.app.callback(
            Output("chemin_ref", "value"), Output("chemin_dossier", "value"),
            Input("choisir_ref", "n_clicks"), Input("choisir_dossier", "n_clicks"),
            State("chemin_ref", "value"), State("chemin_dossier", "value"),
            prevent_initial_call=True)
        def _parcourir(_a, _b, ref, dossier):
            if callback_context.triggered_id == "choisir_ref":
                return demander_chemin(genre="dcm") or ref or "", dossier or ""
            return ref or "", demander_chemin(
                dossier=True, titre="Dossier des plans délivrés") or dossier or ""

        @self.app.callback(
            Output("inventaire", "data"), Output("cartes", "children"),
            Output("reference", "options"), Output("reference", "value"),
            Output("compares", "options"), Output("compares", "value"),
            Output("etat", "children"), Output("contenu", "style"),
            Input("charger", "n_clicks"),
            State("chemin_ref", "value"), State("chemin_dossier", "value"),
            prevent_initial_call=True)
        def _charger(_clics, chemin_ref, chemin_dossier):
            masque = {"display": "none"}
            vide = ([], [], [], None, [], [], "", masque)

            chemins = []
            if chemin_ref and chemin_ref.strip():
                ref = pathlib.Path(chemin_ref.strip().strip('"').strip("'"))
                if not ref.exists():
                    return vide[:6] + (f"❌ référence introuvable : {ref}", masque)
                chemins.append(ref)
            if chemin_dossier and chemin_dossier.strip():
                dossier = pathlib.Path(chemin_dossier.strip().strip('"').strip("'"))
                if not dossier.exists():
                    return vide[:6] + (f"❌ dossier introuvable : {dossier}", masque)
                chemins += [p for p in sorted(dossier.glob("*.dcm"))
                            if p.resolve() not in {c.resolve() for c in chemins}]
            if len(chemins) < 2:
                return vide[:6] + ("Il en faut au moins deux à comparer : un plan "
                                   "de référence et un dossier.", masque)

            self.profils, self.ordre = {}, []
            for chemin in chemins:
                p = profil(chemin)
                nom = p["nom"]
                while nom in self.profils:          # deux dossiers, même nom
                    nom += "'"
                p["nom"] = nom
                self.profils[nom] = p
                self.ordre.append(nom)

            inventaire = [{
                "fichier": p["nom"], "rôle": p.get("role", VIDE),
                "faisceaux": len(p["decoupe"]) if not p["erreur"] else VIDE,
                "points de contrôle": len(p["lames"]) if not p["erreur"] else VIDE,
                "MU": f"{p['mu_total']:.1f}" if not p["erreur"] else VIDE,
                "lecture": p["erreur"] or "ok",
            } for p in (self.profils[n] for n in self.ordre)]

            lisibles = [n for n in self.ordre if not self.profils[n]["erreur"]]
            if len(lisibles) < 2:
                return (inventaire, [], [], None, [], [],
                        "❌ moins de deux fichiers exploitables.", masque)

            defaut = next((n for n in lisibles
                           if self.profils[n]["role"] == "plan"), lisibles[0])
            cartes = [
                carte("fichiers", str(len(lisibles))),
                carte("référence", defaut, "les écarts partent d'elle"),
                carte("points de contrôle", str(len(self.profils[defaut]["lames"]))),
                carte("MU de la référence", f"{self.profils[defaut]['mu_total']:.1f}"),
            ]
            autres = [n for n in lisibles if n != defaut]
            return (inventaire, cartes, self.ordre, defaut, self.ordre, autres,
                    f"✅ {len(lisibles)} plan(s) lu(s) · référence {defaut}",
                    {"display": "block"})

        @self.app.callback(
            Output("ecart", "figure"), Output("bras", "figure"),
            Output("bilan", "data"), Output("alertes", "children"),
            Output("point", "max"),
            Input("reference", "value"), Input("compares", "value"))
        def _comparer(reference, choisis):
            if not reference or reference not in self.profils:
                return go.Figure(), go.Figure(), [], "", 1
            choisis = choisis or []
            etat = comparables(self.profils, reference)
            alertes = [f"{nom} : {raison}" for nom, raison in etat.items()
                       if raison and nom in choisis]
            return (self._figure_ecart(reference, choisis, etat),
                    self._figure_bras(reference, choisis, etat),
                    self._bilan(reference, choisis, etat),
                    " · ".join(alertes),
                    max(1, len(self.profils[reference]["lames"]) - 1))

        @self.app.callback(
            Output("superposition", "figure"), Output("legende", "children"),
            Input("reference", "value"), Input("compares", "value"),
            Input("point", "value"))
        def _superposer(reference, choisis, index):
            if not reference or reference not in self.profils:
                return go.Figure(), ""
            ref = self.profils[reference]
            index = min(int(index or 0), len(ref["lames"]) - 1)
            etat = comparables(self.profils, reference)
            legende = (f"point {index}/{len(ref['lames']) - 1} · "
                       f"{ref['mu'][index]:.1f} MU cumulées · "
                       f"bras {ref['bras'][index]:.1f}°")
            return (self._figure_superposition(reference, choisis or [], etat, index),
                    legende)

    def lancer(self, debug=False):
        print(f"  http://127.0.0.1:{self.port}")
        self.app.run(debug=debug, port=self.port)
