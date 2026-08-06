"""Démonstration : comment les classes s'utilisent, et dans quel ordre.

    python3 main.py

Ce fichier ne contient aucune logique — il montre. Le code vit dans cinq
modules, un par responsabilité :

    conventions.py      noms de colonnes, géométrie visée, seuils, et les
                        conversions du repère Delivery vers celui de DICOM
    archive_trf.py      ArchiveTrf     — les TRF d'un zip, regroupés en séances
    lecteur_rtplan.py   LecteurRtplan  — un RT Plan : ses tags et son ds brut
    ecrivain_dicom.py   EcrivainDicom  — donne au dérivé une identité neuve,
                                         avec ou sans écriture
    chaine.py           Chaine         — orchestre les trois

Les interfaces (`interface.py`, `visualiseur_seances.py`, `comparateur_dicom.py`)
importent ces modules directement.

⚠️ Les plans produits sont des documents d'analyse : UID neufs et
`ApprovalStatus = UNAPPROVED` les distinguent de l'original, mais ce marquage ne
garantit pas leur rejet par un système clinique. La barrière doit être
l'environnement — répertoire isolé, aucune route DICOM vers le réseau clinique.
"""

import pathlib

PLAN = "data/vmat_pymedphys/979797_VMAT.dcm"
ARCHIVE = "data/vmat_pymedphys/trf"          # un dossier de .trf ou un zip SDD
SORTIE = "delivres"


def tout_en_un():
    """Le chemin le plus court : `Chaine` fait les trois étapes elle-même."""
    from chaine import Chaine

    print("=" * 68, "\n1. LA CHAÎNE COMPLÈTE\n" + "=" * 68)
    Chaine(PLAN, ARCHIVE, sortie=SORTIE).executer()


def etape_par_etape():
    """Les mêmes étapes, à la main, pour voir ce que chaque classe apporte."""
    from archive_trf import ArchiveTrf
    from chaine import Chaine
    from conventions import SONDAGES
    from ecrivain_dicom import EcrivainDicom
    from lecteur_rtplan import LecteurRtplan

    print("\n" + "=" * 68, "\n2. ÉTAPE PAR ÉTAPE\n" + "=" * 68)

    # --- le plan : ce qu'on cherche à retrouver dans les logs ---
    plan = LecteurRtplan(PLAN)
    mu = plan.mu_total()
    print(f"\nPlan   {plan.chemin.name}")
    print(f"       {len(plan.grille())} faisceau(x) · {mu:.1f} MU")
    print(f"       MU par faisceau : {plan.mu_par_faisceau()}")
    trajet = plan.trajectoire()
    print(f"       trajectoire : {len(trajet['mu'])} points · "
          f"bras {trajet['bras'].min():.1f}° → {trajet['bras'].max():.1f}°")

    # --- l'archive : elle ne connaît pas le plan, on lui donne les critères ---
    archive = ArchiveTrf(ARCHIVE)
    print(f"\nArchive {ARCHIVE}")
    print(f"        {len(archive._fichiers)} fichier(s) · "
          f"{len(archive.doublons)} doublon(s) écarté(s)")
    for s in archive.seances():
        print(f"        {s['debut']:%Y-%m-%d %H:%M} · {s['champ']:<10} · "
              f"{s['mu']:>7.1f} MU · {len(s['fichiers'])} fichier(s)")

    retenues = archive.correspondantes(mu, plan.empreinte(SONDAGES))
    print(f"\n        {len(retenues)} séance(s) correspondent à ce plan :")
    for s in retenues:
        print(f"        {s['debut']:%Y-%m-%d %H:%M} · dessin {s['dessin']:.2f} mm")

    # --- la substitution : un dataset en mémoire, écrit seulement si on veut ---
    chaine = Chaine(PLAN)                    # sans archive : on substitue seulement
    ecrivain = EcrivainDicom()
    for s in retenues[:1]:
        delivre = ecrivain.preparer(chaine._substituer(s), "Demonstration.")
        print(f"\nDérivé  en mémoire · {delivre.RTPlanLabel} · {delivre.ApprovalStatus}")
        print(f"        UID {delivre.SOPInstanceUID[-24:]}")
        print(f"        (rien n'a été écrit — `ecrire()` le ferait)")


def main():
    if not pathlib.Path(PLAN).exists():
        raise SystemExit(f"{PLAN} est absent : lancer d'abord "
                         "exploration/verification_chaine.py, qui télécharge "
                         "le jeu de test public.")
    tout_en_un()
    etape_par_etape()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
