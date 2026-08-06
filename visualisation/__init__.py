"""Les pages Dash, posées sur le paquet `noyau`.

    from visualisation import Interface
    Interface().lancer()          # les trois onglets, http://127.0.0.1:8050

Les deux vues qui la composent restent utilisables seules :

    from visualisation import Visualiseur, Comparateur

Aucune logique métier ici : le découpage en séances, la lecture des plans et la
substitution viennent tous de `noyau`.
"""

from .comparateur_dicom import Comparateur
from .interface import Interface
from .visualiseur_seances import Visualiseur

__all__ = ["Interface", "Visualiseur", "Comparateur"]
