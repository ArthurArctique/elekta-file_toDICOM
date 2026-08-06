"""Une seule page pour toute la chaîne : archive → séances → plans délivrés.

    from interface import Interface
    Interface().lancer()                        # puis http://127.0.0.1:8050

L'archive SDD se choisit **une fois**, en haut : elle est l'état commun aux
trois onglets.

    Séances     parcourir les séances de l'archive, TRF par TRF
    Plan        confronter un RT Plan à l'archive, choisir les séances à
                exporter en DICOM délivré, et sauter à leur détail
    Comparer    le plan d'origine face aux plans délivrés d'un dossier

Rien n'est réimplémenté : les séances viennent de `main.ArchiveTrf`, les plans
de `main.LecteurRtplan`, la substitution de `main.Chaine`, et l'affichage des
séances comme des écarts est repris tel quel de `visualiseur_seances` et
`comparateur_dicom`, qui restent lançables séparément.
"""

import datetime
import pathlib
import sys

import numpy as np
import plotly.graph_objects as go
from dash import (Dash, Input, Output, State, callback_context,
                  dash_table, dcc, html)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from comparateur_dicom import (bilan, comparables, figure_bras,  # noqa: E402
                               figure_ecart, figure_superposition, profil)
from main import (SONDAGES, ArchiveTrf, Chaine,  # noqa: E402
                  EcrivainDicom, LecteurRtplan)
from visualiseur_seances import (JEUX, TABLEAU, CacheSeances,  # noqa: E402
                                 carte, demander_chemin, etiquette)

PAS_S = 0.04
VIDE = "—"
TOLERANCE_MU = 0.01     # écart de MU toléré, en fraction
SEUIL_DESSIN = 3.0      # mm de médiane tolérés sur le dessin du champ
BOUTON = {"padding": "8px 16px", "fontSize": "13px", "cursor": "pointer",
          "borderRadius": "6px", "border": "1px solid rgba(128,128,128,.45)",
          "background": "#f4f4f5", "whiteSpace": "nowrap"}
PRINCIPAL = {**BOUTON, "background": "#e8eefc", "fontWeight": 600}
CHAMP = {"flex": "1", "padding": "8px 10px", "fontSize": "13px",
         "fontFamily": "ui-monospace, monospace", "borderRadius": "6px",
         "border": "1px solid rgba(128,128,128,.45)"}


def ligne_chemin(bouton, texte, champ, valeur, invite, largeur="230px"):
    """Un bouton de dialogue et son champ de texte, côte à côte."""
    return html.Div([
        html.Button(texte, id=bouton, n_clicks=0,
                    style={**BOUTON, "minWidth": largeur}),
        dcc.Input(id=champ, type="text", value=valeur, placeholder=invite,
                  debounce=True, style=CHAMP),
    ], style={"display": "flex", "gap": "8px", "marginBottom": "8px",
              "alignItems": "center"})


class Interface:
    """Les trois onglets, autour d'une archive choisie une seule fois."""

    def __init__(self, archive="", dossier_cache="seances", port=8050):
        self.defaut_archive = str(archive)
        self.dossier_cache = dossier_cache
        self.cache = None            # CacheSeances, partagé par les onglets
        self.appariees = []          # séances retenues pour le plan courant
        self.chaine = None           # main.Chaine, pour l'export
        self.profils, self.ordre = {}, []
        self.port = port
        self.app = Dash(__name__, title="Elekta — logs et plans",
                        suppress_callback_exceptions=True)
        self.app.layout = self._mise_en_page()
        self._archive()
        self._seances()
        self._plan()
        self._comparaison()

    # ------------------------------------------------------------------ vue

    def _mise_en_page(self):
        return html.Div([
            html.H2("Logs machine et plans de traitement",
                    style={"marginBottom": "2px"}),
            html.Div("Une archive SDD pèse plusieurs gigaoctets : on la localise, "
                     "elle n'est pas téléversée. Tout reste sur le poste.",
                     style={"fontSize": "12px", "opacity": .6, "marginBottom": "14px"}),

            ligne_chemin("choisir_archive", "📂  Archive SDD…", "chemin_archive",
                         self.defaut_archive, "…ou coller le chemin du zip"),
            html.Button("Charger l'archive", id="charger_archive", n_clicks=0,
                        style=PRINCIPAL),
            dcc.Loading(html.Div(id="etat_archive",
                                 style={"fontSize": "12px", "minHeight": "18px",
                                        "margin": "10px 0 4px"})),

            dcc.Tabs(id="onglets", value="seances", children=[
                dcc.Tab(label="Séances", value="seances"),
                dcc.Tab(label="Plan → export", value="plan"),
                dcc.Tab(label="Comparer", value="comparer"),
            ]),
            html.Div(id="panneau_seances", children=self._vue_seances()),
            html.Div(id="panneau_plan", children=self._vue_plan()),
            html.Div(id="panneau_comparer", children=self._vue_comparaison()),

            html.P("Lu en local, rien n'est transmis. Le cache des séances "
                   "contient des copies de données patient.",
                   style={"fontSize": "11px", "opacity": .55, "marginTop": "26px"}),
        ], style={"maxWidth": "1150px", "margin": "0 auto", "padding": "24px",
                  "fontFamily": "system-ui, -apple-system, sans-serif",
                  "background": "#fff", "color": "#1a1a1a", "minHeight": "100vh"})

    def _vue_seances(self):
        return html.Div([
            html.Label("séance", style={"fontSize": "12px", "opacity": .7,
                                        "marginTop": "16px", "display": "block"}),
            dcc.Dropdown(id="seance", options=[], value=None, clearable=False,
                         placeholder="charger une archive d'abord",
                         style={"fontFamily": "ui-monospace, monospace"}),
            html.Div(id="sea_contenu", style={"display": "none"}, children=[
                html.H4("La séance", style={"marginTop": "20px"}),
                html.Div(id="sea_cartes", style={"display": "flex", "gap": "10px"}),
                html.Div(id="sea_details", style={"marginTop": "14px"}),
                dcc.Graph(id="sea_dose"),
                html.H4("Les fichiers", style={"marginTop": "20px"}),
                dcc.Tabs(id="sea_onglets", value="0"),
                dcc.RadioItems(id="sea_jeu", value="essentiel", inline=True,
                               options=[{"label": "  essentielles", "value": "essentiel"},
                                        {"label": "  lames Y1", "value": "y1"},
                                        {"label": "  lames Y2", "value": "y2"},
                                        {"label": "  erreurs", "value": "err"},
                                        {"label": "  tout", "value": "tout"}],
                               style={"fontSize": "12px", "margin": "12px 0 6px"}),
                html.Div(id="sea_entete",
                         style={"fontSize": "12px", "opacity": .7, "margin": "6px 0"}),
                dash_table.DataTable(id="sea_table", page_size=20, **TABLEAU),
            ]),
        ])

    def _vue_plan(self):
        return html.Div([
            html.Div("Confronte un RT Plan à l'archive chargée : MU totales à 1 % "
                     "près et dessin du champ sous 3 mm. Les séances retenues "
                     "peuvent être exportées en RT Plan délivré.",
                     style={"fontSize": "12px", "opacity": .65, "margin": "16px 0 10px"}),
            ligne_chemin("choisir_plan", "📄  RT Plan…", "chemin_plan", "",
                         "…ou coller le chemin du .dcm"),
            html.Button("Chercher les séances", id="chercher", n_clicks=0,
                        style=PRINCIPAL),
            dcc.Loading(html.Div(id="etat_plan",
                                 style={"fontSize": "12px", "minHeight": "18px",
                                        "margin": "10px 0"})),
            html.Div(id="plan_contenu", style={"display": "none"}, children=[
                dash_table.DataTable(
                    id="trouvees", row_selectable="multi", selected_rows=[],
                    **TABLEAU,
                    columns=[{"name": c, "id": c} for c in
                             ("séance", "début", "champ", "fichiers", "MU",
                              "écart de MU", "dessin")]),
                html.Div([
                    html.Button("👁  Voir la séance", id="voir",
                                n_clicks=0, disabled=True, style=BOUTON),
                    html.Button("⚖  Comparer la sélection", id="comparer_selection",
                                n_clicks=0, disabled=True, style=BOUTON),
                    html.Div(id="aide_boutons",
                             style={"fontSize": "12px", "opacity": .65,
                                    "alignSelf": "center"}),
                ], style={"display": "flex", "gap": "8px", "margin": "12px 0",
                          "alignItems": "center"}),
                html.H4("Exporter", style={"marginTop": "18px"}),
                ligne_chemin("choisir_sortie", "📂  Dossier de sortie…",
                             "chemin_sortie", "", "…ou coller le chemin du dossier"),
                html.Button("Exporter la sélection", id="exporter", n_clicks=0,
                            style=PRINCIPAL),
                dcc.Loading(html.Div(id="etat_export",
                                     style={"fontSize": "12px", "minHeight": "18px",
                                            "margin": "10px 0"})),
            ]),
        ])

    def _vue_comparaison(self):
        return html.Div([
            html.Div("Le plan d'origine face aux plans délivrés d'un dossier, et "
                     "les délivrances entre elles. Changer la référence bascule "
                     "d'une question à l'autre.",
                     style={"fontSize": "12px", "opacity": .65, "margin": "16px 0 10px"}),
            ligne_chemin("cmp_choisir_ref", "📄  Plan de référence…",
                         "cmp_chemin_ref", "", "…ou coller le chemin du .dcm"),
            ligne_chemin("cmp_choisir_dossier", "📂  Dossier à comparer…",
                         "cmp_chemin_dossier", "", "…ou coller le chemin du dossier"),
            html.Button("Charger", id="cmp_charger", n_clicks=0, style=PRINCIPAL),
            dcc.Loading(html.Div(id="cmp_etat",
                                 style={"fontSize": "12px", "minHeight": "18px",
                                        "margin": "10px 0"})),
            html.Div(id="cmp_contenu", style={"display": "none"}, children=[
                dash_table.DataTable(id="cmp_inventaire", **TABLEAU,
                                     columns=[{"name": c, "id": c} for c in
                                              ("fichier", "rôle", "faisceaux",
                                               "points de contrôle", "MU", "lecture")]),
                html.Div([
                    html.Div([html.Label("référence", style={"fontSize": "12px",
                                                             "opacity": .7}),
                              dcc.Dropdown(id="cmp_reference", clearable=False)],
                             style={"flex": "1"}),
                    html.Div([html.Label("comparés", style={"fontSize": "12px",
                                                            "opacity": .7}),
                              dcc.Dropdown(id="cmp_compares", multi=True)],
                             style={"flex": "3"}),
                ], style={"display": "flex", "gap": "14px", "margin": "20px 0 6px"}),
                html.Div(id="cmp_alertes",
                         style={"fontSize": "12px", "color": "#b45309"}),
                html.H4("Écart des lames", style={"marginTop": "16px"}),
                dcc.Graph(id="cmp_ecart"),
                html.H4("Écart d'angle de bras", style={"marginTop": "14px"}),
                dcc.Graph(id="cmp_bras"),
                html.H4("Superposition des ouvertures", style={"marginTop": "18px"}),
                html.Div(id="cmp_legende", style={"fontSize": "12px", "opacity": .7}),
                dcc.Slider(id="cmp_point", min=0, max=1, step=1, value=0,
                           tooltip={"placement": "bottom", "always_visible": False}),
                dcc.Graph(id="cmp_superposition"),
                html.H4("Bilan", style={"marginTop": "18px"}),
                dash_table.DataTable(id="cmp_bilan", sort_action="native", **TABLEAU,
                                     columns=[{"name": c, "id": c} for c in
                                              ("fichier", "rôle", "MU", "médiane",
                                               "p95", "max", "écart de MU")]),
            ]),
        ])

    # ------------------------------------------------- onglets, affichage

    def _archive(self):
        app = self.app

        @app.callback(
            Output("panneau_seances", "style"), Output("panneau_plan", "style"),
            Output("panneau_comparer", "style"), Input("onglets", "value"))
        def _basculer(onglet):
            return tuple({"display": "block" if onglet == cle else "none"}
                         for cle in ("seances", "plan", "comparer"))

        @app.callback(
            Output("chemin_archive", "value"),
            Input("choisir_archive", "n_clicks"),
            State("chemin_archive", "value"), prevent_initial_call=True)
        def _localiser(_c, actuel):
            return demander_chemin(genre="zip") or actuel or ""

        @app.callback(
            Output("seance", "options"), Output("seance", "value"),
            Output("etat_archive", "children"),
            Input("charger_archive", "n_clicks"),
            State("chemin_archive", "value"), prevent_initial_call=True)
        def _charger(_c, chemin):
            if not chemin or not chemin.strip():
                return [], None, "Localiser une archive SDD, ou coller son chemin."
            source = pathlib.Path(chemin.strip().strip('"').strip("'"))
            if not source.exists():
                return [], None, f"❌ introuvable : {source}"
            try:
                self.cache = CacheSeances(source, self.dossier_cache)
            except Exception as erreur:
                self.cache = None
                return [], None, f"❌ {type(erreur).__name__} : {erreur}"
            seances = self.cache.seances()
            machines = sorted({s["machine"] for s in seances})
            return ([{"label": etiquette(s), "value": i}
                     for i, s in enumerate(seances)],
                    0 if seances else None,
                    f"✅ {source.name} · {len(seances)} séance(s) · "
                    f"machine(s) {', '.join(machines)}")

    def _seances(self):
        app = self.app

        @app.callback(
            Output("sea_cartes", "children"), Output("sea_details", "children"),
            Output("sea_dose", "figure"), Output("sea_onglets", "children"),
            Output("sea_onglets", "value"), Output("sea_contenu", "style"),
            Input("seance", "value"))
        def _voir(index):
            if index is None or self.cache is None:
                return [], "", go.Figure(), [], "0", {"display": "none"}
            s = self.cache.seances()[index]
            tables = self.cache.tables(index)
            echantillons = sum(len(f["table"]) for f in tables)
            duree = (np.datetime64(s["fin"]) - np.datetime64(s["debut"])) / np.timedelta64(1, "s")

            cartes = [
                carte("MU délivrées", f"{s['mu']:.1f}"),
                carte("fichiers", str(s["nb_fichiers"])),
                carte("échantillons", str(echantillons),
                      f"{echantillons * PAS_S:.0f} s enregistrées"),
                carte("durée", f"{duree:.0f} s", "de bout en bout"),
                carte("état final", s["etat_final"]),
            ]
            lignes = [("machine", s["machine"]), ("champ", s["champ"]),
                      ("début (UTC)", s["debut"]), ("fin (UTC)", s["fin"]),
                      ("MU par fichier", " + ".join(f"{f['mu']:.1f}" for f in tables)),
                      ("états finaux", " → ".join(f["etat_final"] for f in tables)),
                      ("dossier", f"{self.cache.dossier}/{s['dossier']}")]
            if len(tables) > 1:
                creux = [(tables[i + 1]["debut"] - tables[i]["fin"]).total_seconds()
                         for i in range(len(tables) - 1)]
                lignes.append(("interruptions", " · ".join(f"{c:.0f} s" for c in creux)))
            details = dash_table.DataTable(
                data=[{"champ": a, "valeur": str(b)} for a, b in lignes],
                columns=[{"name": c, "id": c} for c in ("champ", "valeur")], **TABLEAU)

            figure, decalage = go.Figure(), 0.0
            for f in tables:
                mu = f["table"]["Step Dose/Actual Value (Mu)"].values
                d = np.diff(mu, prepend=0.0)
                d[d < 0] = 0
                continu = np.cumsum(d) + decalage
                figure.add_trace(go.Scatter(x=np.arange(len(continu)) * PAS_S,
                                            y=continu, mode="lines",
                                            name=f["nom"][-22:]))
                decalage = float(continu[-1])
            figure.update_layout(xaxis_title="temps enregistré (s)",
                                 yaxis_title="MU cumulées", height=280,
                                 template="plotly_white",
                                 margin={"l": 60, "r": 20, "t": 20, "b": 45},
                                 legend={"orientation": "h", "y": -0.3})
            onglets = [dcc.Tab(label=f["nom"][-24:], value=str(i))
                       for i, f in enumerate(tables)]
            return cartes, details, figure, onglets, "0", {"display": "block"}

        @app.callback(
            Output("sea_table", "data"), Output("sea_table", "columns"),
            Output("sea_entete", "children"),
            Input("seance", "value"), Input("sea_onglets", "value"),
            Input("sea_jeu", "value"))
        def _fichier(index, onglet, jeu):
            if index is None or self.cache is None:
                return [], [], ""
            tables = self.cache.tables(index)
            f = tables[min(int(onglet or 0), len(tables) - 1)]
            table = f["table"]
            if jeu == "essentiel":
                colonnes = [c for c in JEUX["essentiel"] if c in table.columns]
            elif jeu in ("y1", "y2"):
                colonnes = [c for c in table.columns
                            if c.startswith(jeu.upper() + " Leaf") and "Scaled Actual" in c]
            elif jeu == "err":
                colonnes = [c for c in table.columns if "Positional Error" in c]
            else:
                colonnes = list(table.columns)
            extrait = table[colonnes].head(400).round(2)
            data = [{"t (s)": round(i * PAS_S, 2), **ligne}
                    for i, ligne in zip(extrait.index, extrait.to_dict("records"))]
            entetes = [{"name": "t (s)", "id": "t (s)"}] + \
                      [{"name": c.replace("/", " / "), "id": c} for c in colonnes]
            return data, entetes, (
                f"{f['nom']} · {len(table)} échantillons · {len(table.columns)} "
                f"colonnes · {f['mu']:.1f} MU · état final « {f['etat_final']} »"
                + (" · 400 premières lignes" if len(table) > 400 else ""))

    # ---------------------------------------------- plan, appariement, export

    def _plan(self):
        app = self.app

        @app.callback(
            Output("chemin_plan", "value"),
            Input("choisir_plan", "n_clicks"),
            State("chemin_plan", "value"), prevent_initial_call=True)
        def _localiser(_c, actuel):
            return demander_chemin(genre="dcm") or actuel or ""

        @app.callback(
            Output("chemin_sortie", "value"),
            Input("choisir_sortie", "n_clicks"),
            State("chemin_sortie", "value"), prevent_initial_call=True)
        def _sortie(_c, actuel):
            return demander_chemin(dossier=True,
                                   titre="Dossier de sortie") or actuel or ""

        @app.callback(
            Output("trouvees", "data"), Output("trouvees", "selected_rows"),
            Output("etat_plan", "children"), Output("plan_contenu", "style"),
            Input("chercher", "n_clicks"),
            State("chemin_plan", "value"), prevent_initial_call=True)
        def _chercher(_c, chemin):
            masque = {"display": "none"}
            if self.cache is None:
                return [], [], "Charger une archive d'abord.", masque
            if not chemin or not chemin.strip():
                return [], [], "Localiser un RT Plan.", masque
            plan = pathlib.Path(chemin.strip().strip('"').strip("'"))
            if not plan.exists():
                return [], [], f"❌ introuvable : {plan}", masque
            try:
                # Le coût est dans le décodage des TRF : on ne décode que les
                # séances dont les MU collent déjà, connues du cache sans lire
                # un seul octet de log. Sur une semaine, cela ramène quelques
                # centaines de fichiers à une poignée.
                self.chaine = Chaine(plan)          # sans archive : on ne substitue
                mu = self.chaine.plan.mu_total(self.chaine.fraction)
                empreinte = self.chaine.plan.empreinte(SONDAGES, self.chaine.fraction)
                candidates = [i for i, s in enumerate(self.cache.seances())
                              if mu and abs(s["mu"] - mu) / mu <= TOLERANCE_MU]

                self.appariees = []
                for i in candidates:
                    resume = self.cache.seances()[i]
                    seance = {
                        "machine": resume["machine"], "champ": resume["champ"],
                        "debut": datetime.datetime.fromisoformat(resume["debut"]),
                        "mu": resume["mu"], "fichiers": self.cache.tables(i),
                        "index_cache": i,
                    }
                    dessin = float(np.median(np.abs(
                        ArchiveTrf._empreinte(seance, SONDAGES) - empreinte)))
                    if dessin <= SEUIL_DESSIN:
                        seance["dessin"] = dessin
                        self.appariees.append(seance)
            except Exception as erreur:
                self.appariees = []
                return [], [], f"❌ {type(erreur).__name__} : {erreur}", masque

            lignes = [{
                "séance": rang, "début": s["debut"].strftime("%Y-%m-%d %H:%M"),
                "champ": s["champ"], "fichiers": len(s["fichiers"]),
                "MU": f"{s['mu']:.1f}",
                "écart de MU": f"{100 * (s['mu'] - mu) / mu:+.2f} %",
                "dessin": f"{s['dessin']:.2f} mm",
            } for rang, s in enumerate(self.appariees, start=1)]
            if not lignes:
                return [], [], (f"Aucune séance ne correspond à {plan.name} "
                                f"({mu:.1f} MU) dans cette archive."), masque
            return (lignes, list(range(len(lignes))),
                    f"✅ {plan.name} · {mu:.1f} MU · {len(lignes)} séance(s) "
                    "correspondante(s), toutes sélectionnées",
                    {"display": "block"})

        @app.callback(
            Output("voir", "disabled"), Output("comparer_selection", "disabled"),
            Output("aide_boutons", "children"),
            Input("trouvees", "selected_rows"))
        def _activer(choisies):
            """Chaque bouton n'a de sens que pour un nombre donné de séances.

            Le visualiseur n'en montre qu'une ; la comparaison en demande au
            moins deux. Les désactiver vaut mieux qu'un message après coup.
            """
            n = len(choisies or [])
            if n == 0:
                return True, True, "cocher une séance pour la voir, deux ou plus pour comparer"
            if n == 1:
                return False, True, "une séance : visible, mais rien à comparer"
            return True, False, f"{n} séances : comparables entre elles"

        def exporter(choisies, sortie):
            """Écrit les séances choisies. Rend (dossier, noms, message d'erreur)."""
            if not self.appariees or self.chaine is None:
                return None, [], "Chercher des séances d'abord."
            if not choisies:
                return None, [], "Aucune séance sélectionnée."
            if not sortie or not sortie.strip():
                return None, [], "Choisir un dossier de sortie."
            dossier = pathlib.Path(sortie.strip().strip('"').strip("'"))
            try:
                dossier.mkdir(parents=True, exist_ok=True)
            except Exception as erreur:
                return None, [], f"❌ dossier inutilisable : {erreur}"

            ecrivain, ecrits = EcrivainDicom(), []
            souche = self.chaine.plan.chemin.stem
            for rang in sorted(choisies):
                seance = self.appariees[rang]
                try:
                    delivre = self.chaine._substituer(seance)
                    chemin = dossier / (f"{souche}_delivre_"
                                        f"{seance['debut']:%Y%m%d_%H%M%S}_"
                                        f"s{rang + 1:04d}.dcm")
                    ecrivain.ecrire(delivre, chemin,
                                    f"Derive de {len(seance['fichiers'])} log(s) "
                                    "machine. Analyse uniquement.")
                    ecrits.append(chemin.name)
                except Exception as erreur:
                    return None, [], (f"❌ séance {rang + 1} : "
                                      f"{type(erreur).__name__} : {erreur}")
            return dossier, ecrits, None

        @app.callback(
            Output("etat_export", "children"),
            Input("exporter", "n_clicks"),
            State("trouvees", "selected_rows"), State("chemin_sortie", "value"),
            prevent_initial_call=True)
        def _exporter(_c, choisies, sortie):
            dossier, ecrits, erreur = exporter(choisies, sortie)
            if erreur:
                return erreur
            return html.Div([
                html.Div(f"✅ {len(ecrits)} fichier(s) écrit(s) dans {dossier}/"),
                html.Div(", ".join(ecrits), style={"opacity": .7}),
                html.Div("UID neufs · UNAPPROVED · plans dérivés pour analyse, "
                         "à tenir hors de toute route DICOM clinique",
                         style={"opacity": .7}),
            ])

        @app.callback(
            Output("onglets", "value"), Output("seance", "value", allow_duplicate=True),
            Input("voir", "n_clicks"), State("trouvees", "selected_rows"),
            prevent_initial_call=True)
        def _voir_seance(_c, choisies):
            """Bascule sur l'onglet Séances, sur la séance retenue.

            Chaque séance appariée garde l'index du cache dont elle vient : pas
            de recherche, donc pas d'ambiguïté si deux séances se ressemblent.
            """
            if not choisies or len(choisies) != 1 or self.cache is None:
                return "plan", None
            return "seances", self.appariees[choisies[0]]["index_cache"]

        @app.callback(
            Output("onglets", "value", allow_duplicate=True),
            Output("cmp_chemin_ref", "value", allow_duplicate=True),
            Output("cmp_chemin_dossier", "value", allow_duplicate=True),
            Output("cmp_charger", "n_clicks", allow_duplicate=True),
            Output("etat_export", "children", allow_duplicate=True),
            Input("comparer_selection", "n_clicks"),
            State("trouvees", "selected_rows"), State("chemin_sortie", "value"),
            State("cmp_charger", "n_clicks"), prevent_initial_call=True)
        def _comparer_selection(_c, choisies, sortie, clics):
            """Exporte la sélection, puis bascule sur l'onglet Comparer.

            Comparer suppose des DICOM : on les écrit d'abord, dans le même
            dossier de sortie que le bouton d'export, avec les mêmes noms — un
            second passage réécrit les mêmes fichiers plutôt que d'en empiler.
            Le compteur de clics de « Charger » est incrémenté pour déclencher
            le chargement de l'onglet sans que l'utilisateur ait à y toucher.
            """
            dossier, ecrits, erreur = exporter(choisies, sortie)
            if erreur:
                return "plan", "", "", clics or 0, erreur
            return ("comparer", str(self.chaine.plan.chemin), str(dossier),
                    (clics or 0) + 1,
                    html.Div(f"✅ {len(ecrits)} fichier(s) écrit(s), "
                             "comparaison chargée dans l'onglet Comparer."))

    # ------------------------------------------------------------ comparaison

    def _comparaison(self):
        app = self.app

        @app.callback(
            Output("cmp_chemin_ref", "value", allow_duplicate=True),
            Output("cmp_chemin_dossier", "value", allow_duplicate=True),
            Input("cmp_choisir_ref", "n_clicks"),
            Input("cmp_choisir_dossier", "n_clicks"),
            State("cmp_chemin_ref", "value"), State("cmp_chemin_dossier", "value"),
            prevent_initial_call=True)
        def _localiser(_a, _b, ref, dossier):
            if callback_context.triggered_id == "cmp_choisir_ref":
                return demander_chemin(genre="dcm") or ref or "", dossier or ""
            return ref or "", demander_chemin(
                dossier=True, titre="Dossier des plans délivrés") or dossier or ""

        @app.callback(
            Output("cmp_inventaire", "data"),
            Output("cmp_reference", "options"), Output("cmp_reference", "value"),
            Output("cmp_compares", "options"), Output("cmp_compares", "value"),
            Output("cmp_etat", "children"), Output("cmp_contenu", "style"),
            Input("cmp_charger", "n_clicks"),
            State("cmp_chemin_ref", "value"), State("cmp_chemin_dossier", "value"),
            prevent_initial_call=True)
        def _charger(_c, chemin_ref, chemin_dossier):
            masque = {"display": "none"}
            chemins = []
            if chemin_ref and chemin_ref.strip():
                ref = pathlib.Path(chemin_ref.strip().strip('"').strip("'"))
                if not ref.exists():
                    return [], [], None, [], [], f"❌ référence introuvable : {ref}", masque
                chemins.append(ref)
            if chemin_dossier and chemin_dossier.strip():
                dossier = pathlib.Path(chemin_dossier.strip().strip('"').strip("'"))
                if not dossier.exists():
                    return [], [], None, [], [], f"❌ dossier introuvable : {dossier}", masque
                deja = {c.resolve() for c in chemins}
                chemins += [p for p in sorted(dossier.glob("*.dcm"))
                            if p.resolve() not in deja]
            if len(chemins) < 2:
                return [], [], None, [], [], ("Il en faut au moins deux : un plan "
                                              "de référence et un dossier."), masque

            self.profils, self.ordre = {}, []
            for chemin in chemins:
                p = profil(chemin)
                nom = p["nom"]
                while nom in self.profils:
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
                return (inventaire, [], None, [], [],
                        "❌ moins de deux fichiers exploitables.", masque)
            defaut = next((n for n in lisibles
                           if self.profils[n]["role"] == "plan"), lisibles[0])
            return (inventaire, self.ordre, defaut, self.ordre,
                    [n for n in lisibles if n != defaut],
                    f"✅ {len(lisibles)} plan(s) lu(s) · référence {defaut}",
                    {"display": "block"})

        @app.callback(
            Output("cmp_ecart", "figure"), Output("cmp_bras", "figure"),
            Output("cmp_bilan", "data"), Output("cmp_alertes", "children"),
            Output("cmp_point", "max"),
            Input("cmp_reference", "value"), Input("cmp_compares", "value"))
        def _comparer(reference, choisis):
            if not reference or reference not in self.profils:
                return go.Figure(), go.Figure(), [], "", 1
            choisis = choisis or []
            etat = comparables(self.profils, reference)
            alertes = [f"{nom} : {raison}" for nom, raison in etat.items()
                       if raison and nom in choisis]
            return (figure_ecart(self.profils, reference, choisis, etat),
                    figure_bras(self.profils, reference, choisis, etat),
                    bilan(self.profils, reference, choisis, etat),
                    " · ".join(alertes),
                    max(1, len(self.profils[reference]["lames"]) - 1))

        @app.callback(
            Output("cmp_superposition", "figure"), Output("cmp_legende", "children"),
            Input("cmp_reference", "value"), Input("cmp_compares", "value"),
            Input("cmp_point", "value"))
        def _superposer(reference, choisis, index):
            if not reference or reference not in self.profils:
                return go.Figure(), ""
            ref = self.profils[reference]
            index = min(int(index or 0), len(ref["lames"]) - 1)
            etat = comparables(self.profils, reference)
            return (figure_superposition(self.profils, reference, choisis or [],
                                         etat, index),
                    f"point {index}/{len(ref['lames']) - 1} · "
                    f"{ref['mu'][index]:.1f} MU cumulées · "
                    f"bras {ref['bras'][index]:.1f}°")

    def lancer(self, debug=False):
        print(f"  http://127.0.0.1:{self.port}")
        self.app.run(debug=debug, port=self.port)
