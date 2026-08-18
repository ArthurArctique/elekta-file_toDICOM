"""La chaîne : archive de logs machine → RT Plan dérivé, séance par séance.

    from noyau import ArchiveTrf, Chaine, EcrivainDicom, LecteurRtplan

Une responsabilité par module :

    conventions      noms de colonnes, géométrie visée, seuils, conversions
    archive_trf      les TRF d'un zip, regroupés en séances
    lecteur_rtplan   un RT Plan : ses tags, sa trajectoire, son `ds` brut
    ecrivain_dicom   l'identité neuve d'un dérivé, écrite ou non
    chaine           l'orchestration des trois
    tableaux         le plan et le log mis à plat, pour l'export CSV
"""

from .archive_trf import ArchiveTrf
from .chaine import Chaine
from .conventions import (CLASSE_RT_PLAN, ECART_MAX_S, PAIRES, PAS_S, SONDAGES,
                          machoires_vers_dicom, mlc_vers_dicom)
from .ecrivain_dicom import EcrivainDicom
from .lecteur_rtplan import LecteurRtplan
from .tableaux import (COLONNES_LOG, COLONNES_PLAN, ecrire_csv, table_log,
                       table_plan)

__all__ = ["ArchiveTrf", "Chaine", "EcrivainDicom", "LecteurRtplan",
           "CLASSE_RT_PLAN", "ECART_MAX_S", "PAIRES", "PAS_S", "SONDAGES",
           "machoires_vers_dicom", "mlc_vers_dicom",
           "COLONNES_LOG", "COLONNES_PLAN", "ecrire_csv", "table_log",
           "table_plan"]
