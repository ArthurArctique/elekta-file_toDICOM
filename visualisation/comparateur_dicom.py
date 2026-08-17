"""Confronte un RT Plan de référence aux plans délivrés d'un dossier.

    from comparateur_dicom import Comparateur
    Comparateur().lancer()                       # puis http://127.0.0.1:8053

Les deux chemins se choisissent **dans la page** : un bouton pour le plan de
référence, un autre pour le dossier des plans à comparer — typiquement celui
qu'a rempli `chaine.Chaine`. Tout est lu localement, rien n'est téléversé.

Les plans sont lus par `lecteur_rtplan.LecteurRtplan`, la trajectoire vient de
sa méthode `trajectoire()` : même dépliage des points de contrôle et mêmes
conventions de bancs que la chaîne. Aucune seconde implémentation.

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
                  dash_table, dcc, html, no_update)

# La racine du dépôt sur le chemin : le paquet `noyau` s'importe alors
# quel que soit le dossier depuis lequel on lance.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from noyau.archive_trf import ArchiveTrf  # noqa: E402
from noyau.conventions import COL_ETAT, PAS_S  # noqa: E402
from noyau.lecteur_rtplan import LecteurRtplan  # noqa: E402

from .visualiseur_seances import TABLEAU, carte, demander_chemin  # noqa: E402

COULEURS = ("#3b6fd4", "#d4663b", "#3ba55c", "#a53b96", "#c9a227", "#00838f")
GRIS = "#9aa0a6"
VIDE = "—"

# Le compteur de points de contrôle que la machine tient elle-même. Il ne sert
# pas au calcul — l'appariement se fait sur les MU — mais il permet de le
# recouper : voir `origine_du_point`.
COL_CP_MACHINE = "Control point/Actual Value (None)"

# Au-delà de ce nombre de lignes à MU constante, le plateau n'est plus la
# résolution du format (0,1 MU) mais un vrai arrêt de la dose. Les plateaux
# pathologiques mesurés sur l'IMRT à neuf faisceaux se comptent en centaines.
PLATEAU_LONG = 10


def profil(source, nom=None, seance=None):
    """Un plan, aplati en séries comparables.

    `source` est un chemin **ou** un dataset pydicom déjà en mémoire : comparer
    des délivrances tout juste reconstituées n'a pas à passer par le disque.

    `seance` est la séance de logs dont ce plan est issu, quand il en vient
    d'une. Elle n'entre dans aucun calcul : elle permet seulement de remonter
    d'un point de contrôle aux lignes de TRF qui l'ont produit.
    """
    etiquette = nom or (source.name if isinstance(source, pathlib.Path)
                        else pathlib.Path(str(source)).name)
    try:
        plan = LecteurRtplan(source)
        t = plan.trajectoire()
    except SystemExit as erreur:
        return {"nom": etiquette, "erreur": str(erreur)}
    except Exception as erreur:
        return {"nom": etiquette, "erreur": f"{type(erreur).__name__} : {erreur}"}

    ds = plan.ds
    derive = (str(getattr(ds, "ApprovalStatus", "")) == "UNAPPROVED"
              and "erive" in str(getattr(ds, "RTPlanDescription", "")))
    return {
        "nom": etiquette, "chemin": str(getattr(plan, "chemin", etiquette)), "ds": ds,
        "role": "délivré" if derive else "plan",
        "mu": t["mu"], "lames": t["lames"], "bras": t["bras"],
        "decoupe": t["decoupe"], "mu_total": plan.mu_total(),
        "bornes": bornes_des_lames(ds), "seance": seance, "erreur": None,
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


# --------------------------------------------------------------------------
# Du point de contrôle aux lignes de log
#
# La chaîne rejoue la grille du plan sur l'axe des MU réellement parcourues :
# chaque point de contrôle vise une MU cumulée, et `np.interp` va chercher
# la géométrie entre les deux lignes de log qui l'encadrent. Ce qui suit
# refait ce chemin **en sens inverse**, pour montrer d'où sort la valeur
# affichée plutôt que de la donner à croire.
# --------------------------------------------------------------------------

def situer_ligne(seance, rang):
    """(fichier, ligne dans ce fichier) pour un rang de l'axe recollé."""
    depart = 0
    for f in seance["fichiers"]:
        n = len(f["table"])
        if rang < depart + n:
            return f, rang - depart
        depart += n
    return seance["fichiers"][-1], len(seance["fichiers"][-1]["table"]) - 1


def origine_du_point(seance, mu_cible):
    """Les lignes de log derrière la géométrie interpolée à `mu_cible`.

    Reproduit exactement l'encadrement de `np.interp` employé par
    `Chaine._substituer` : borne droite par recherche binaire, si bien que sur
    un plateau — plusieurs lignes à la même MU — c'est la **dernière** qui est
    retenue. Vérifié : la valeur reconstruite ici et celle de `np.interp`
    coïncident à 7·10⁻¹⁵ mm près sur les 111 points du plan VMAT public.

    Rend aussi la taille du plateau, parce que c'est là que se loge la limite 2
    de `chaine.py` : un long plateau signifie que la machine se repositionne
    faisceau éteint, et que la géométrie retenue est celle de l'arrivée.
    """
    mu, _ = ArchiveTrf._delivrance(seance)
    ordre = np.argsort(mu, kind="stable")
    mu_trie = mu[ordre]

    j = int(np.searchsorted(mu_trie, mu_cible, side="right"))
    j = min(max(j, 1), len(mu_trie) - 1)
    gauche, droite = j - 1, j
    largeur = float(mu_trie[droite] - mu_trie[gauche])
    poids = 0.0 if largeur <= 0 else (mu_cible - float(mu_trie[gauche])) / largeur

    valeur = mu_trie[gauche]
    debut = int(np.searchsorted(mu_trie, valeur, side="left"))
    fin = int(np.searchsorted(mu_trie, valeur, side="right"))

    rang = int(ordre[gauche])
    fichier, ligne = situer_ligne(seance, rang)
    table = fichier["table"]
    cp_machine = (int(table[COL_CP_MACHINE].values[ligne])
                  if COL_CP_MACHINE in table.columns else None)
    return {
        "rang_gauche": rang, "rang_droite": int(ordre[droite]),
        "mu_gauche": float(mu_trie[gauche]), "mu_droite": float(mu_trie[droite]),
        "poids": poids, "plateau": fin - debut,
        "fichier": fichier, "ligne": ligne, "cp_machine": cp_machine,
        "etat": (str(table[COL_ETAT].values[ligne])
                 if COL_ETAT in table.columns else VIDE),
    }


def erreurs_au_point(seance, mu_cible, limite=20):
    """Les écarts de position que la machine a elle-même relevés à cet instant.

    Le TRF porte, pour chaque axe mécanique, un « Positional Error » à côté de
    la position atteinte : c'est l'écart entre la consigne et le relevé, tel
    que la machine le juge — pas un calcul de notre part.
    """
    o = origine_du_point(seance, mu_cible)
    table = o["fichier"]["table"]
    colonnes = [c for c in table.columns if "Positional Error" in c]
    if not colonnes:
        return o, [], 0

    releve = table.iloc[o["ligne"]]
    lignes = []
    for c in colonnes:
        erreur = float(releve[c])
        axe = c.split("/")[0]
        position = c.replace("Positional Error", "Scaled Actual")
        lignes.append({
            "axe": axe,
            "position": (f"{float(releve[position]):.1f}"
                         if position in table.columns else VIDE),
            "écart": f"{erreur:+.2f}",
            "unité": "deg" if c.endswith("(deg)") else "mm",
            "_tri": abs(erreur),
        })
    non_nuls = sum(1 for l in lignes if l["_tri"] > 1e-9)
    lignes.sort(key=lambda l: -l["_tri"])
    for l in lignes:
        del l["_tri"]
    return o, lignes[:limite], non_nuls


def detail_du_point(profil_delivre, index):
    """D'où sort la géométrie affichée pour ce point de contrôle.

    Rendu sous forme de lignes (intitulé, valeur) : le but est qu'on puisse
    refaire le calcul à la main, pas qu'on fasse confiance au graphique.
    """
    mu = profil_delivre["mu"]
    index = int(min(max(index, 0), len(mu) - 1))
    cible = float(mu[index])

    faisceau, rang_local = VIDE, VIDE
    for numero, debut, fin in profil_delivre["decoupe"]:
        if debut <= index < fin:
            faisceau, rang_local = str(numero), f"{index - debut} / {fin - debut - 1}"
            break

    lignes = [
        ("point de contrôle", f"{index} sur {len(mu) - 1}"),
        ("faisceau", f"n° {faisceau}  ·  point {rang_local} du faisceau"),
        ("MU cumulées visées", f"{cible:.2f} MU"),
        ("angle de bras", f"{float(profil_delivre['bras'][index]):.1f}°"),
    ]

    seance = profil_delivre.get("seance")
    if seance is None:
        lignes.append(("origine", "plan lu depuis un DICOM : les lignes de log "
                                  "n'accompagnent pas le fichier"))
        return lignes

    o = origine_du_point(seance, cible)
    largeur = o["mu_droite"] - o["mu_gauche"]
    lignes += [
        ("fichier de log", o["fichier"]["nom"]),
        ("ligne retenue", f"{o['ligne']}  ·  t ≈ {o['ligne'] * PAS_S:.2f} s "
                          "après le début du fichier"),
        ("encadrement", f"{o['mu_gauche']:.2f} → {o['mu_droite']:.2f} MU "
                        f"(largeur {largeur:.2f} MU)"),
        ("interpolation", f"valeur = gauche + {o['poids']:.3f} × (droite − gauche)"
                          if largeur > 0 else
                          "les deux bornes portent la même MU : valeur reprise telle quelle"),
        ("état machine", o["etat"]),
    ]
    if o["cp_machine"] is not None:
        # La machine numérote le SEGMENT qu'elle parcourt : son CP k va du
        # point k−1 au point k du plan (mesuré : 0,06 MU d'écart médian sur
        # l'arc VMAT public). Un point du plan tombe donc sur la frontière
        # entre deux segments, et lire k ou k+1 est normal.
        lignes.append(("CP compté par la machine",
                       f"{o['cp_machine']}  ·  recoupement indépendant du "
                       f"calcul. La machine numérote le segment parcouru, du "
                       f"point {o['cp_machine'] - 1} au point {o['cp_machine']} "
                       f"du plan : lire {index} ou {index + 1} est attendu ici."))
    if o["plateau"] > 1:
        duree = o["plateau"] * PAS_S
        texte = (f"{o['plateau']} lignes de log portent cette même MU "
                 f"({duree:.1f} s) ; c'est celle de la DERNIÈRE qui est retenue.")
        if o["plateau"] < PLATEAU_LONG:
            texte += (" Court : les MU sont enregistrées par pas de 0,1, "
                      "deux ou trois lignes identiques sont la résolution du "
                      "format, pas un arrêt.")
        else:
            texte += (" Long : la dose n'avance plus, mais les lames, elles, "
                      "continuent de bouger — repositionnement entre segments. "
                      "La géométrie retenue est celle de l'arrivée, pas du "
                      "parcours (limite 2 de chaine.py).")
        lignes.append(("plateau" if o["plateau"] < PLATEAU_LONG else "⚠ plateau",
                       texte))
    return lignes


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


def axe_des_cp(figure, mu, combien=9):
    """Un second axe, en haut : les points de contrôle.

    Les MU et les points de contrôle ne sont pas proportionnels — un point qui
    n'irradie pas n'avance pas les MU — donc l'axe du haut ne peut pas être une
    règle régulière. Chaque graduation est posée à la MU **réelle** de son point
    de contrôle : l'espacement irrégulier qu'on y voit est l'information.

    `matches` verrouille les deux axes, sinon un zoom sur l'un décalerait
    silencieusement les graduations de l'autre.
    """
    n = len(mu)
    if n < 2:
        return
    pas = max(1, round(n / combien))
    reperes = list(range(0, n, pas))
    # Le dernier point mérite sa graduation, mais le dernier repère régulier
    # tombe souvent juste avant : les deux étiquettes se chevaucheraient. On
    # remplace alors plutôt que d'ajouter.
    if reperes[-1] != n - 1:
        if n - 1 - reperes[-1] < pas / 2:
            reperes[-1] = n - 1
        else:
            reperes.append(n - 1)
    # Une trace transparente ancre l'axe : Plotly ne dessine pas un axe
    # superposé auquel aucune trace n'est rattachée.
    figure.add_trace(go.Scatter(
        x=[float(mu[0]), float(mu[-1])], y=[None, None], xaxis="x2",
        mode="lines", showlegend=False, hoverinfo="skip"))
    figure.update_layout(xaxis2={
        "overlaying": "x", "side": "top", "matches": "x",
        "tickmode": "array",
        "tickvals": [float(mu[i]) for i in reperes],
        "ticktext": [str(i) for i in reperes],
        "title": {"text": "point de contrôle", "font": {"size": 11}},
        # Sans angle imposé, Plotly incline les étiquettes dès qu'il les croit
        # à l'étroit : elles deviennent illisibles, ce qui ôte tout intérêt à
        # ce second axe.
        "tickangle": 0, "tickfont": {"size": 10},
        "showgrid": False, "zeroline": False,
    })


def survol(nom, mu, unite):
    """Le survol porte les deux repères, comme les deux axes."""
    return {
        "customdata": np.arange(len(mu)),
        "hovertemplate": ("CP %{customdata} · %{x:.1f} MU · %{y:.2f} "
                          + unite + "<extra>" + nom + "</extra>"),
    }


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
            **survol(nom, ref["mu"], "mm")))
    for _, _, fin in ref["decoupe"][:-1]:
        figure.add_vline(x=ref["mu"][fin - 1],
                         line={"width": 1, "dash": "dot", "color": GRIS})
    axe_des_cp(figure, ref["mu"])
    figure.update_layout(
        xaxis_title="MU cumulées (référence)",
        yaxis_title="écart des lames, médiane (mm)",
        height=340, template="plotly_white", clickmode="event",
        margin={"l": 60, "r": 20, "t": 42, "b": 45},
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
            name=nom, line={"color": COULEURS[rang % len(COULEURS)], "width": 1.5},
            **survol(nom, ref["mu"], "°")))
    axe_des_cp(figure, ref["mu"])
    figure.update_layout(
        xaxis_title="MU cumulées (référence)", yaxis_title="écart de bras (°)",
        height=270, template="plotly_white", showlegend=False, clickmode="event",
        margin={"l": 60, "r": 20, "t": 42, "b": 45})
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


# --------------------------------------------------------------------------
# Les commandes du point examiné, communes aux deux pages
#
# `Comparateur` et `Interface` montrent la même chose : les identifiants ne
# diffèrent que par un préfixe. Écrire ces blocs une fois évite que les deux
# pages se mettent à diverger l'une de l'autre.
# --------------------------------------------------------------------------

COLONNES_ERREURS = ("axe", "position", "écart", "unité")


def index_pour_mu(mu, valeur):
    """Le point de contrôle dont la MU cumulée est la plus proche."""
    return int(np.argmin(np.abs(np.asarray(mu, dtype=float) - float(valeur))))


def index_du_clic(clic):
    """Le point de contrôle cliqué, ou None.

    `pointIndex` est le rang du point dans sa courbe, donc directement le
    numéro du point de contrôle — les courbes sont tracées sur la grille du
    plan, un point par CP.

    `customdata` est essayé d'abord mais ne suffit pas : **Dash ne le transmet
    pas** dans `clickData` (relevé sur la requête réelle — elle ne porte que
    `curveNumber`, `pointNumber`, `pointIndex`, `x`, `y`, `bbox`), alors même
    que Plotly s'en sert côté navigateur pour le survol. S'appuyer dessus seul
    rendait le clic sans effet.

    La trace transparente qui ancre l'axe des CP porte `hoverinfo="skip"` : un
    clic ne peut pas la désigner, et il n'y a donc pas de rang parasite à
    écarter ici.
    """
    points = (clic or {}).get("points") or []
    if not points:
        return None
    for cle in ("customdata", "pointIndex", "pointNumber"):
        valeur = points[0].get(cle)
        if valeur is not None:
            return int(valeur)
    return None


def bloc_du_point(prefixe=""):
    """Choisir le point examiné : par son numéro, ou par les MU cumulées."""
    petit = {"fontSize": "12px", "opacity": .7, "display": "block",
             "marginBottom": "2px"}
    return html.Div([
        html.Div([
            html.Label("point de contrôle", style=petit),
            dcc.Slider(id=f"{prefixe}point", min=0, max=1, step=1, value=0,
                       tooltip={"placement": "bottom", "always_visible": False}),
        ], style={"flex": "1"}),
        html.Div([
            html.Label("aller à — MU cumulées", style=petit),
            # Pas de `debounce` : une valeur tapée doit partir tout de suite,
            # comme partout ailleurs dans ces pages. Ce champ commande le
            # curseur mais n'est pas recopié depuis lui : Dash refuse un cycle
            # entre deux callbacks. La MU courante se lit sous le curseur.
            dcc.Input(id=f"{prefixe}mu", type="number", step=0.1, min=0,
                      placeholder="ex. 175.1",
                      style={"width": "110px", "padding": "6px 8px",
                             "fontSize": "13px", "borderRadius": "6px",
                             "fontFamily": "ui-monospace, monospace",
                             "border": "1px solid rgba(128,128,128,.45)"}),
        ], style={"width": "120px"}),
    ], style={"display": "flex", "gap": "20px", "alignItems": "flex-end",
              "margin": "4px 0 2px"})


def bloc_origine(prefixe=""):
    """Sous le dessin des lames : d'où sort la valeur, et ce que la machine a relevé."""
    return html.Div([
        html.H4("D'où vient cette position", style={"marginTop": "18px",
                                                    "marginBottom": "4px"}),
        html.Div("Le chemin exact entre le plan et le log, pour ce point.",
                 style={"fontSize": "12px", "opacity": .65}),
        html.Div(id=f"{prefixe}detail", style={"margin": "10px 0 4px"}),
        html.H4("Écarts relevés par la machine", style={"marginTop": "16px",
                                                        "marginBottom": "4px"}),
        html.Div(id=f"{prefixe}erreurs_entete",
                 style={"fontSize": "12px", "opacity": .65, "marginBottom": "6px"}),
        dash_table.DataTable(id=f"{prefixe}erreurs", sort_action="native",
                             page_size=10, **TABLEAU,
                             columns=[{"name": c, "id": c} for c in COLONNES_ERREURS]),
    ])


def rendu_detail(lignes):
    """Les couples (intitulé, valeur) en lignes lisibles."""
    return html.Div([
        html.Div([
            html.Div(intitule, style={"minWidth": "210px", "opacity": .6,
                                      "fontSize": "11px", "textTransform": "uppercase",
                                      "letterSpacing": ".05em", "paddingTop": "2px"}),
            html.Div(str(valeur), style={"flex": "1", "fontSize": "13px",
                                         "fontFamily": "ui-monospace, monospace"}),
        ], style={"display": "flex", "gap": "12px", "padding": "3px 0",
                  "borderBottom": "1px solid rgba(128,128,128,.14)"})
        for intitule, valeur in lignes
    ])


def origine_affichee(profils, reference, choisis, index):
    """Le détail et le tableau d'écarts pour le point examiné.

    Rend (detail, entête du tableau, lignes du tableau). La séance examinée est
    la première des sélectionnées qui porte ses logs : le plan de référence, lu
    depuis un DICOM, n'en a pas — et c'est dit plutôt que masqué.
    """
    avec_logs = [n for n in (choisis or [])
                 if n in profils and profils[n].get("seance") is not None]
    examine = avec_logs[0] if avec_logs else reference
    p = profils[examine]
    detail = rendu_detail([("plan examiné", examine)] + detail_du_point(p, index))

    seance = p.get("seance")
    if seance is None:
        return detail, ("Les écarts de position sont relevés dans le TRF. Ils "
                        "n'apparaissent que pour une séance passée par l'onglet "
                        "« Plan → export », qui garde le lien vers ses logs."), []

    o, lignes, non_nuls = erreurs_au_point(seance, float(p["mu"][index]))
    if not lignes:
        return detail, "Ce log ne porte aucune colonne « Positional Error ».", []
    entete = (f"{non_nuls} axe(s) en écart à cet instant · ligne {o['ligne']} de "
              f"{o['fichier']['nom']}"
              + (f" · CP machine {o['cp_machine']}" if o["cp_machine"] else "")
              + f" · {len(lignes)} plus grands écarts affichés")
    return detail, entete, lignes


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
                html.Div("Cliquer une courbe des deux graphiques ci-dessus amène "
                         "ici le point de contrôle correspondant.",
                         style={"fontSize": "12px", "opacity": .65}),
                bloc_du_point(),
                html.Div(id="legende", style={"fontSize": "12px", "opacity": .7,
                                              "margin": "2px 0 4px"}),
                dcc.Graph(id="superposition"),
                bloc_origine(),

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
            Output("point", "value"),
            Input("mu", "value"),
            Input("ecart", "clickData"), Input("bras", "clickData"),
            State("reference", "value"), prevent_initial_call=True)
        def _pointer(mu_voulue, clic_ecart, clic_bras, reference):
            """Les trois façons de désigner un point aboutissent au curseur.

            `reference` est en State, pas en Input : le curseur ne doit pas se
            déplacer parce qu'on a changé de référence.
            """
            if not reference or reference not in self.profils:
                return no_update
            mu = self.profils[reference]["mu"]
            declencheur = callback_context.triggered_id
            if declencheur == "mu":
                return no_update if mu_voulue is None else index_pour_mu(mu, mu_voulue)
            index = index_du_clic(clic_ecart if declencheur == "ecart" else clic_bras)
            return no_update if index is None else min(index, len(mu) - 1)

        @self.app.callback(
            Output("superposition", "figure"), Output("legende", "children"),
            Output("detail", "children"), Output("erreurs_entete", "children"),
            Output("erreurs", "data"),
            Input("reference", "value"), Input("compares", "value"),
            Input("point", "value"))
        def _superposer(reference, choisis, index):
            if not reference or reference not in self.profils:
                return go.Figure(), "", "", "", []
            ref = self.profils[reference]
            index = min(int(index or 0), len(ref["lames"]) - 1)
            etat = comparables(self.profils, reference)
            legende = (f"point {index}/{len(ref['lames']) - 1} · "
                       f"{ref['mu'][index]:.1f} MU cumulées · "
                       f"bras {ref['bras'][index]:.1f}° (référence)")
            detail, entete, erreurs = origine_affichee(
                self.profils, reference, choisis or [], index)
            return (self._figure_superposition(reference, choisis or [], etat, index),
                    legende, detail, entete, erreurs)

    def lancer(self, debug=False):
        print(f"  http://127.0.0.1:{self.port}")
        self.app.run(debug=debug, port=self.port)
