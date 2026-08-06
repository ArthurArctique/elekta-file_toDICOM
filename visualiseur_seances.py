"""Explore les séances d'une archive SDD dans le navigateur.

    from visualiseur_seances import Visualiseur
    Visualiseur().lancer()                       # puis http://127.0.0.1:8052

L'archive se choisit **dans la page** : le bouton « Localiser l'archive… »
ouvre la boîte de dialogue du système. Une SDD pesant plusieurs gigaoctets,
elle n'est pas téléversée par le navigateur — l'application tourne sur le poste
et lit le fichier là où il est. Le champ de texte reste disponible pour un
copier-coller, et un second bouton accepte un dossier de `.trf`.

Le découpage en séances est celui d'`archive_trf.ArchiveTrf` — la classe est appelée,
pas réimplémentée. Cette page n'est qu'une interface par-dessus.

Le cache
--------
Décoder 400 TRF prend du temps. Au premier passage, l'archive est parcourue une
fois puis un dossier `seances/` est écrit :

    seances/index.json          le survol de chaque séance, pour le menu
    seances/s0001/*.trf         les TRF de la séance, déjà extraits

Aux lancements suivants, le menu se remplit depuis `index.json` sans rien
décoder, et une séance choisie n'est décodée que quand on la demande — en
relisant son dossier, sans rouvrir l'archive. Le cache est invalidé si le zip
change de taille ou de date.

⚠️ `seances/` contient des copies de données patient : à protéger comme les
originaux. Le `.gitignore` du dépôt l'exclut déjà.
"""

import datetime
import json
import pathlib
import subprocess
import sys
import zipfile

import numpy as np
import plotly.graph_objects as go
from dash import (Dash, Input, Output, State, callback_context,
                  dash_table, dcc, html)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from archive_trf import ArchiveTrf  # noqa: E402
from conventions import COL_ETAT, COL_MU, PAS_S  # noqa: E402

TABLEAU = {
    "style_cell": {"fontFamily": "ui-monospace, SFMono-Regular, monospace",
                   "fontSize": "12px", "textAlign": "left", "padding": "5px 8px"},
    "style_header": {"fontWeight": "600", "textTransform": "uppercase",
                     "fontSize": "10px", "letterSpacing": ".05em"},
    "style_table": {"overflowX": "auto"},
}
JEUX = {
    "essentiel": ("Control point/Actual Value (None)", "Linac State/Actual Value (None)",
                  "Step Dose/Actual Value (Mu)", "Actual Dose Rate/Actual Value (Mu/min)",
                  "Step Gantry/Scaled Actual (deg)", "Step Collimator/Scaled Actual (deg)",
                  "X1 Diaphragm/Scaled Actual (mm)", "X2 Diaphragm/Scaled Actual (mm)"),
}


class CacheSeances:
    """Le dossier `seances/` : découpe une fois, relit ensuite.

    L'unique appel coûteux — `ArchiveTrf(zip)` — n'a lieu qu'à la construction
    du cache. Ensuite chaque séance est un simple dossier de `.trf`, que
    `ArchiveTrf` sait relire aussi.
    """

    def __init__(self, archive, dossier="seances"):
        self.archive = pathlib.Path(archive)
        self.dossier = pathlib.Path(dossier)
        self.index = self._charger() or self._construire()

    def _signature(self):
        etat = self.archive.stat()
        return {"archive": str(self.archive.resolve()),
                "octets": etat.st_size, "modifie": int(etat.st_mtime)}

    def _charger(self):
        fichier = self.dossier / "index.json"
        if not fichier.exists():
            return None
        contenu = json.loads(fichier.read_text(encoding="utf-8"))
        if contenu.get("signature") != self._signature():
            print("  cache périmé (l'archive a changé) : reconstruction")
            return None
        print(f"  cache lu : {len(contenu['seances'])} séance(s), aucun décodage")
        return contenu

    def _octets(self):
        """Le contenu de chaque .trf, que la source soit un zip ou un dossier."""
        if self.archive.is_dir():
            return {str(p.relative_to(self.archive)): p.read_bytes()
                    for p in self.archive.rglob("*.trf")}
        with zipfile.ZipFile(self.archive) as zf:
            return {nom: zf.read(nom) for nom in zf.namelist()
                    if nom.lower().endswith(".trf")}

    def _construire(self):
        print(f"  découpage de {self.archive.name} — long au premier passage…")
        lecture = ArchiveTrf(self.archive)
        seances = lecture.seances()
        if lecture.doublons:
            print(f"  {len(lecture.doublons)} fichier(s) en double écarté(s)")
        self.dossier.mkdir(parents=True, exist_ok=True)
        # Un cache précédent peut compter plus de séances : ses dossiers
        # restants seraient relus comme des séances fantômes.
        for ancien in self.dossier.glob("s[0-9]*"):
            if ancien.is_dir():
                for f in ancien.glob("*.trf"):
                    f.unlink()
                ancien.rmdir()
        octets = self._octets()

        resumes = []
        for rang, s in enumerate(seances, start=1):
            sous = self.dossier / f"s{rang:04d}"
            sous.mkdir(exist_ok=True)
            noms = []
            for f in s["fichiers"]:
                cible = sous / pathlib.PurePath(f["nom"]).name
                if f["nom"] in octets:
                    cible.write_bytes(octets[f["nom"]])
                noms.append(cible.name)
            resumes.append({
                "rang": rang, "dossier": sous.name,
                "machine": s["machine"], "champ": s["champ"],
                "debut": s["debut"].isoformat(sep=" ", timespec="seconds"),
                "fin": s["fin"].isoformat(sep=" ", timespec="seconds"),
                "mu": round(s["mu"], 1), "etat_final": s["etat_final"],
                "nb_fichiers": len(s["fichiers"]), "fichiers": noms,
            })

        contenu = {"signature": self._signature(), "seances": resumes}
        (self.dossier / "index.json").write_text(
            json.dumps(contenu, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {len(resumes)} séance(s) écrite(s) dans {self.dossier}/")
        print("  ⚠ copies de données patient : à protéger comme les originaux.")
        return contenu

    def seances(self):
        return self.index["seances"]

    def tables(self, rang):
        """Les tables décodées d'une séance, à la demande.

        Relit le dossier de la séance avec la même classe que l'archive : c'est
        le décodage d'`archive_trf.ArchiveTrf`, jamais une seconde implémentation.
        """
        resume = self.seances()[rang]
        lecture = ArchiveTrf(self.dossier / resume["dossier"])
        return sorted(lecture._fichiers, key=lambda f: f["debut"])


FILTRES = {
    "zip": [("Archives SDD", "*.zip"), ("Tous les fichiers", "*.*")],
    "dcm": [("RT Plan DICOM", "*.dcm"), ("Tous les fichiers", "*.*")],
}


def demander_chemin(dossier=False, genre="zip", titre=None):
    """Ouvre la boîte de dialogue du système et rend le chemin choisi.

    `genre` choisit le filtre d'extensions — `"zip"` pour une archive SDD,
    `"dcm"` pour un RT Plan. Sans ce paramètre, tous les appelants héritaient du
    filtre `*.zip` et un sélecteur de DICOM ne montrait que des archives.

    L'application tourne sur le poste de l'utilisateur : le dialogue s'ouvre
    donc là où il regarde. C'est ce qui permet d'éviter `dcc.Upload`, qui ferait
    transiter plusieurs gigaoctets par le navigateur pour rien.

    Lancé dans un **sous-processus** : sur macOS, Tk refuse de s'ouvrir depuis
    un fil d'exécution secondaire, or les callbacks Dash n'en sont jamais le
    principal. Le sous-processus a le sien.

    Rend une chaîne vide si l'utilisateur annule ou si Tk est absent.
    """
    if dossier:
        intitule = titre or "Choisir un dossier"
        ouvrir = f"askdirectory(title={intitule!r})"
    else:
        intitule = titre or ("Localiser l'archive SDD" if genre == "zip"
                             else "Choisir un RT Plan DICOM")
        ouvrir = (f"askopenfilename(title={intitule!r}, "
                  f"filetypes={FILTRES.get(genre, FILTRES['zip'])!r})")
    code = ("import tkinter as tk\n"
            "from tkinter.filedialog import askopenfilename, askdirectory\n"
            "racine = tk.Tk(); racine.withdraw()\n"
            "racine.attributes('-topmost', True)\n"
            f"print(({ouvrir}) or '')\n")
    try:
        issue = subprocess.run([sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=600)
    except Exception as erreur:
        print(f"  boîte de dialogue indisponible : {erreur}", file=sys.stderr)
        return ""
    return issue.stdout.strip()


def etiquette(s):
    """Une ligne du menu déroulant : date · champ · MU."""
    return (f"{s['debut'][:16]}  ·  {s['champ'][:22]:<22}  ·  {s['mu']:>8.1f} MU"
            + ("" if s["nb_fichiers"] == 1 else f"  ·  {s['nb_fichiers']} fichiers"))


def carte(titre, valeur, note=""):
    return html.Div([
        html.Div(titre, style={"fontSize": "11px", "textTransform": "uppercase",
                               "letterSpacing": ".06em", "opacity": .6}),
        html.Div(valeur, style={"fontSize": "22px", "fontWeight": 600, "margin": "2px 0"}),
        html.Div(note, style={"fontSize": "11px", "opacity": .55}),
    ], style={"padding": "10px 14px", "border": "1px solid rgba(128,128,128,.35)",
              "borderRadius": "8px", "minWidth": "140px", "flex": "1"})


class Visualiseur:
    """La page : un menu de séances, les infos de la séance, les TRF en détail."""

    def __init__(self, archive="", dossier="seances", port=8052):
        # L'archive n'est plus obligatoire : elle se choisit dans la page. Le
        # paramètre ne sert qu'à pré-remplir le champ.
        self.defaut = str(archive)
        self.dossier = dossier
        self.cache = None
        self.port = port
        self.app = Dash(__name__, title="Séances")
        self.app.layout = self._mise_en_page()
        self._callbacks()

    def _mise_en_page(self):
        return html.Div([
            html.H2("Séances de l'archive", style={"marginBottom": "2px"}),
            html.Div("Une archive SDD pèse plusieurs gigaoctets : on donne son "
                     "chemin, on ne la téléverse pas. Tout reste sur le poste.",
                     style={"fontSize": "12px", "opacity": .6}),

            html.Div([
                html.Button("📂  Localiser l'archive…", id="parcourir", n_clicks=0,
                            style={"padding": "8px 16px", "fontSize": "13px",
                                   "cursor": "pointer", "borderRadius": "6px",
                                   "border": "1px solid rgba(128,128,128,.45)",
                                   "background": "#f4f4f5", "whiteSpace": "nowrap"}),
                html.Button("dossier…", id="parcourir_dossier", n_clicks=0,
                            title="choisir un dossier de .trf au lieu d'un zip",
                            style={"padding": "8px 12px", "fontSize": "13px",
                                   "cursor": "pointer", "borderRadius": "6px",
                                   "border": "1px solid rgba(128,128,128,.45)",
                                   "background": "#fafafa", "whiteSpace": "nowrap"}),
                dcc.Input(id="chemin", type="text", value=self.defaut,
                          placeholder="…ou coller ici le chemin du zip",
                          debounce=True,
                          style={"flex": "1", "padding": "8px 10px", "fontSize": "13px",
                                 "fontFamily": "ui-monospace, monospace",
                                 "border": "1px solid rgba(128,128,128,.45)",
                                 "borderRadius": "6px"}),
                html.Button("Charger", id="charger", n_clicks=0,
                            style={"padding": "8px 18px", "fontSize": "13px",
                                   "cursor": "pointer", "borderRadius": "6px",
                                   "border": "1px solid rgba(128,128,128,.45)",
                                   "background": "#e8eefc", "fontWeight": 600}),
            ], style={"display": "flex", "gap": "8px", "margin": "16px 0 6px",
                      "alignItems": "center"}),

            dcc.Loading(html.Div(id="etat", style={"fontSize": "12px",
                                                   "minHeight": "18px"})),

            html.Label("séance", style={"fontSize": "12px", "opacity": .7,
                                        "marginTop": "14px", "display": "block"}),
            dcc.Dropdown(id="seance", options=[], value=None, clearable=False,
                         placeholder="charger une archive d'abord",
                         style={"fontFamily": "ui-monospace, monospace"}),

            html.Div(id="contenu", style={"display": "none"}, children=[
            html.H4("La séance", style={"marginTop": "24px"}),
            html.Div(id="cartes", style={"display": "flex", "gap": "10px"}),
            html.Div(id="details", style={"marginTop": "14px"}),
            dcc.Graph(id="dose"),

            html.H4("Les fichiers", style={"marginTop": "24px"}),
            html.Div("Un onglet par TRF de la séance. Le tableau montre les "
                     "colonnes telles que la machine les a écrites.",
                     style={"fontSize": "12px", "opacity": .65}),
            dcc.Tabs(id="onglets", value="0"),
            html.Div([
                dcc.RadioItems(id="jeu", value="essentiel", inline=True,
                               options=[{"label": "  colonnes essentielles", "value": "essentiel"},
                                        {"label": "  lames Y1", "value": "y1"},
                                        {"label": "  lames Y2", "value": "y2"},
                                        {"label": "  erreurs de position", "value": "err"},
                                        {"label": "  tout", "value": "tout"}],
                               style={"fontSize": "12px"}),
            ], style={"margin": "12px 0 6px"}),
            html.Div(id="entete_fichier",
                     style={"fontSize": "12px", "opacity": .7, "margin": "6px 0"}),
            dash_table.DataTable(id="table", page_size=20, **TABLEAU),
            ]),

            html.P("Lu en local, rien n'est transmis. Le dossier de cache "
                   "contient des copies de données patient.",
                   style={"fontSize": "11px", "opacity": .55, "marginTop": "26px"}),
        ], style={"maxWidth": "1150px", "margin": "0 auto", "padding": "24px",
                  "fontFamily": "system-ui, -apple-system, sans-serif",
                  "background": "#fff", "color": "#1a1a1a", "minHeight": "100vh"})

    def _callbacks(self):

        @self.app.callback(
            Output("chemin", "value"),
            Input("parcourir", "n_clicks"), Input("parcourir_dossier", "n_clicks"),
            State("chemin", "value"), prevent_initial_call=True)
        def _parcourir(_zip, _dossier, actuel):
            """Ouvre le dialogue système et remplit le champ.

            Annuler laisse le champ tel quel plutôt que de l'effacer.
            """
            declencheur = callback_context.triggered_id
            choisi = demander_chemin(dossier=declencheur == "parcourir_dossier")
            return choisi or actuel or ""

        @self.app.callback(
            Output("seance", "options"), Output("seance", "value"),
            Output("etat", "children"), Output("contenu", "style"),
            Input("charger", "n_clicks"), Input("chemin", "value"),
            prevent_initial_call=True)
        def _charger(_clics, chemin):
            """Construit ou relit le cache de l'archive demandée.

            Le premier passage sur une vraie archive prend des minutes : le
            navigateur attend, d'où le `dcc.Loading` autour de cet état. Les
            suivants sont immédiats, le cache évitant tout décodage.
            """
            cache_vide = {"display": "none"}
            if not chemin:
                return [], None, "Localiser une archive SDD, ou coller son chemin.", cache_vide
            source = pathlib.Path(chemin.strip().strip('"').strip("'"))
            if not source.exists():
                return [], None, f"❌ introuvable : {source}", cache_vide
            try:
                self.cache = CacheSeances(source, self.dossier)
            except Exception as erreur:
                self.cache = None
                return [], None, f"❌ {type(erreur).__name__} : {erreur}", cache_vide

            seances = self.cache.seances()
            options = [{"label": etiquette(s), "value": i}
                       for i, s in enumerate(seances)]
            machines = sorted({s["machine"] for s in seances})
            return options, (0 if options else None), (
                f"✅ {source.name} · {len(seances)} séance(s) · "
                f"machine(s) {', '.join(machines)} · cache dans {self.dossier}/"), (
                {"display": "block"} if options else cache_vide)

        @self.app.callback(
            Output("cartes", "children"), Output("details", "children"),
            Output("dose", "figure"), Output("onglets", "children"),
            Output("onglets", "value"),
            Input("seance", "value"))
        def _seance(index):
            if index is None or self.cache is None:
                return [], "", go.Figure(), [], "0"
            cache = self.cache
            s = cache.seances()[index]
            tables = cache.tables(index)
            debut = datetime.datetime.fromisoformat(s["debut"])
            fin = datetime.datetime.fromisoformat(s["fin"])
            echantillons = sum(len(f["table"]) for f in tables)

            cartes = [
                carte("MU délivrées", f"{s['mu']:.1f}"),
                carte("fichiers", str(s["nb_fichiers"])),
                carte("échantillons", f"{echantillons}",
                      f"{echantillons * PAS_S:.0f} s enregistrées"),
                carte("durée", f"{(fin - debut).total_seconds():.0f} s",
                      "de bout en bout"),
                carte("état final", s["etat_final"]),
            ]

            lignes = [
                ("machine", s["machine"]),
                ("champ", s["champ"]),
                ("début (UTC)", s["debut"]),
                ("fin (UTC)", s["fin"]),
                ("MU par fichier", " + ".join(f"{f['mu']:.1f}" for f in tables)),
                ("états finaux", " → ".join(f["etat_final"] for f in tables)),
                ("dossier de cache", f"{cache.dossier}/{s['dossier']}"),
            ]
            # Un creux entre deux fichiers, c'est le temps d'arrêt de la séance.
            if len(tables) > 1:
                creux = [(tables[i + 1]["debut"] - tables[i]["fin"]).total_seconds()
                         for i in range(len(tables) - 1)]
                lignes.append(("interruptions",
                               " · ".join(f"{c:.0f} s" for c in creux)))
            details = dash_table.DataTable(
                data=[{"champ": a, "valeur": str(b)} for a, b in lignes],
                columns=[{"name": c, "id": c} for c in ("champ", "valeur")],
                **TABLEAU)

            figure = go.Figure()
            decalage = 0.0
            for f in tables:
                mu = f["table"][COL_MU].values
                d = np.diff(mu, prepend=0.0)
                d[d < 0] = 0
                continu = np.cumsum(d) + decalage
                figure.add_trace(go.Scatter(
                    x=np.arange(len(continu)) * PAS_S, y=continu,
                    mode="lines", name=f["nom"][-22:]))
                decalage = float(continu[-1])
            figure.update_layout(
                xaxis_title="temps enregistré (s)", yaxis_title="MU cumulées",
                height=280, template="plotly_white",
                margin={"l": 60, "r": 20, "t": 20, "b": 45},
                legend={"orientation": "h", "y": -0.3})

            onglets = [dcc.Tab(label=f["nom"][-24:], value=str(i))
                       for i, f in enumerate(tables)]
            return cartes, details, figure, onglets, "0"

        @self.app.callback(
            Output("table", "data"), Output("table", "columns"),
            Output("entete_fichier", "children"),
            Input("seance", "value"), Input("onglets", "value"), Input("jeu", "value"))
        def _fichier(index, onglet, jeu):
            if index is None or self.cache is None:
                return [], [], ""
            tables = self.cache.tables(index)
            f = tables[min(int(onglet or 0), len(tables) - 1)]
            table = f["table"]

            if jeu == "essentiel":
                colonnes = [c for c in JEUX["essentiel"] if c in table.columns]
            elif jeu == "y1":
                colonnes = [c for c in table.columns if c.startswith("Y1 Leaf")
                            and "Scaled Actual" in c]
            elif jeu == "y2":
                colonnes = [c for c in table.columns if c.startswith("Y2 Leaf")
                            and "Scaled Actual" in c]
            elif jeu == "err":
                colonnes = [c for c in table.columns if "Positional Error" in c]
            else:
                colonnes = list(table.columns)

            extrait = table[colonnes].head(400).round(2)
            data = [{"t (s)": round(i * PAS_S, 2), **ligne}
                    for i, ligne in zip(extrait.index, extrait.to_dict("records"))]
            entetes = [{"name": "t (s)", "id": "t (s)"}] + \
                      [{"name": c.replace("/", " / "), "id": c} for c in colonnes]
            resume = (f"{f['nom']} · {len(table)} échantillons · "
                      f"{len(table.columns)} colonnes · {f['mu']:.1f} MU · "
                      f"fin {f['fin']:%Y-%m-%d %H:%M:%S} UTC · "
                      f"état final « {f['etat_final']} »"
                      + (f" · {len(colonnes)} colonne(s) affichée(s), "
                         "400 premières lignes" if len(table) > 400 else ""))
            return data, entetes, resume

    def lancer(self, debug=False):
        print(f"  http://127.0.0.1:{self.port}")
        self.app.run(debug=debug, port=self.port)
