"""Point d'entrée : les chemins en tête, un appel par usage.

    python3 main.py

Renseigner les trois chemins ci-dessous, puis décommenter en bas l'appel voulu.

Le code vit dans deux paquets :

    noyau/          la chaîne, une classe par module
        conventions      noms de colonnes, géométrie visée, seuils, conversions
        archive_trf      ArchiveTrf     — les TRF d'un zip, en séances
        lecteur_rtplan   LecteurRtplan  — un RT Plan : ses tags, son ds brut
        ecrivain_dicom   EcrivainDicom  — l'identité neuve d'un dérivé
        chaine           Chaine         — orchestre les trois

    visualisation/  les pages Dash, posées sur `noyau`
        interface            les trois onglets réunis
        visualiseur_seances  les séances d'une archive
        comparateur_dicom    le plan face à ses délivrances

⚠️ Les plans produits sont des documents d'analyse : UID neufs et
`ApprovalStatus = UNAPPROVED` les distinguent de l'original, mais ce marquage ne
garantit pas leur rejet par un système clinique. La barrière doit être
l'environnement — répertoire isolé, aucune route DICOM vers le réseau clinique.
"""

import pathlib

# --------------------------------------------------------------- mes chemins
# Une archive SDD (.zip) ou un dossier de .trf.
ARCHIVE = "data/vmat_pymedphys/trf"
# Le RT Plan à retrouver dans cette archive.
PLAN = "data/vmat_pymedphys/979797_VMAT.dcm"
# Où écrire les plans dérivés.
SORTIE = "delivres"


def ouvrir_interface(port=8050):
    """Les trois onglets — séances, appariement et export, comparaison.

    Le chemin de l'archive n'est qu'un pré-remplissage : tout se choisit dans
    la page.
    """
    from visualisation import Interface

    Interface(archive=ARCHIVE, port=port).lancer()


def exporter_les_seances():
    """Retrouve les séances de PLAN dans ARCHIVE et les écrit dans SORTIE."""
    from noyau import Chaine

    return Chaine(PLAN, ARCHIVE, sortie=SORTIE).executer()


def inspecter():
    """Les mêmes étapes à la main, pour voir ce que chaque classe apporte."""
    from noyau import (SONDAGES, ArchiveTrf, Chaine, EcrivainDicom,
                       LecteurRtplan)

    plan = LecteurRtplan(PLAN)
    mu = plan.mu_total()
    trajet = plan.trajectoire()
    print(f"\nPlan   {plan.chemin.name}")
    print(f"       {len(plan.grille())} faisceau(x) · {mu:.1f} MU · "
          f"{len(trajet['mu'])} points de contrôle")
    print(f"       MU par faisceau : {plan.mu_par_faisceau()}")

    archive = ArchiveTrf(ARCHIVE)
    print(f"\nArchive {ARCHIVE}")
    print(f"        {len(archive._fichiers)} fichier(s) · "
          f"{len(archive.doublons)} doublon(s) écarté(s)")
    for s in archive.seances():
        print(f"        {s['debut_local']:%Y-%m-%d %H:%M} · {s['champ']:<12} · "
              f"{s['mu']:>7.1f} MU · {len(s['fichiers'])} fichier(s)")

    retenues = archive.correspondantes(mu, plan.empreinte(SONDAGES))
    print(f"\n        {len(retenues)} séance(s) correspondent à ce plan :")
    for s in retenues:
        print(f"        {s['debut_local']:%Y-%m-%d %H:%M} · "
              f"dessin {s['dessin']:.2f} mm")

    # Un dérivé complet, en mémoire : `ecrire()` seul le poserait sur disque.
    if retenues:
        delivre = EcrivainDicom().preparer(
            Chaine(PLAN)._substituer(retenues[0]), "Inspection.")
        print(f"\nDérivé  {delivre.RTPlanLabel} · {delivre.ApprovalStatus} · "
              f"en mémoire, rien n'a été écrit")
    return retenues


def _verifier_chemins(*chemins):
    manquants = [c for c in chemins if not pathlib.Path(c).exists()]
    if manquants:
        raise SystemExit("Chemin(s) introuvable(s), à corriger en tête de "
                         "main.py :\n  " + "\n  ".join(manquants))


if __name__ == "__main__":
    # --- décommenter l'appel voulu ---

    _verifier_chemins(ARCHIVE)
    ouvrir_interface()

    # _verifier_chemins(ARCHIVE, PLAN)
    # exporter_les_seances()

    # _verifier_chemins(ARCHIVE, PLAN)
    # inspecter()
