"""Écriture d'un RT Plan dérivé, avec une identité qui lui est propre.

    from ecrivain_dicom import EcrivainDicom
    EcrivainDicom().preparer(ds, "description")      # identité neuve, rien d'écrit
    EcrivainDicom().ecrire(ds, "sortie.dcm", "…")    # écrit puis contrôle le fichier
"""

import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.uid import (ExplicitVRBigEndian, ExplicitVRLittleEndian,
                         ImplicitVRLittleEndian, generate_uid)

from .conventions import CLASSE_RT_PLAN

class EcrivainDicom:
    """Écrit un ds avec une identité neuve, pour qu'il n'écrase jamais le plan.

    ⚠️ Ce marquage n'est **pas** une protection technique. Le fichier reste un
    RT Plan avec sa SOP Class d'origine : un R&V peut l'importer, l'afficher et
    le transmettre. `UNAPPROVED` est un statut, pas un verrou. La vraie barrière
    est l'environnement — répertoire isolé, aucune route DICOM vers le réseau
    clinique.
    """

    # Politique d'identité, explicite parce qu'elle engage :
    #   SOPInstanceUID              remplacé   — c'est un autre document
    #   SeriesInstanceUID           remplacé   — il n'appartient pas à la série du plan
    #   MediaStorage*               synchronisés sur les deux ci-dessus
    #   StudyInstanceUID            CONSERVÉ   — le dérivé reste dans l'étude du
    #                                            patient, ce qui le rend traçable
    #                                            mais aussi associable au dossier
    #   SOPClassUID                 conservé   — ça reste un RT Plan
    #   FrameOfReferenceUID         conservé   — même repère géométrique

    def ecrire(self, ds, chemin, description=""):
        """Prépare l'identité du dérivé, l'écrit, et contrôle le fichier écrit."""
        self.preparer(ds, description)
        ds.save_as(str(chemin), enforce_file_format=True)
        self._controler(chemin, ds.SOPInstanceUID)
        return chemin

    def preparer(self, ds, description=""):
        """Donne au dérivé son identité neuve, **sans l'écrire**.

        Séparé de `ecrire` parce que tout n'a pas vocation à passer par le
        disque : comparer deux délivrances ne demande que des datasets en
        mémoire, et les écrire pour les relire aussitôt serait un détour.
        """
        nouvel_uid = generate_uid()
        ds.SOPInstanceUID = nouvel_uid
        ds.SeriesInstanceUID = generate_uid()

        # Le méta-en-tête porte une seconde copie de l'UID et de la SOP Class.
        # Sans ces lignes, le fichier sort avec un UID neuf dans le dataset et
        # **celui du plan d'origine** dans le méta : l'identité du document
        # devient incohérente. Invisible sur les plans publics, qui n'ont aucun
        # méta-en-tête du tout.
        if getattr(ds, "file_meta", None) is None:
            ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPInstanceUID = nouvel_uid
        ds.file_meta.MediaStorageSOPClassUID = getattr(
            ds, "SOPClassUID", CLASSE_RT_PLAN)
        if "TransferSyntaxUID" not in ds.file_meta:
            # Aucun méta d'origine : on déclare l'encodage que pydicom a
            # effectivement employé à la lecture plutôt qu'une valeur arbitraire.
            # `original_encoding` vaut (implicit_VR, little_endian).
            implicite, petit_boutiste = getattr(
                ds, "original_encoding", (True, True))
            if implicite and petit_boutiste:
                ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
            elif petit_boutiste:
                ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
            else:
                ds.file_meta.TransferSyntaxUID = ExplicitVRBigEndian

        if "RTPlanLabel" in ds:
            ds.RTPlanLabel = (str(ds.RTPlanLabel) or "")[:10] + "_DEL"
        ds.ApprovalStatus = "UNAPPROVED"
        if description:
            ds.RTPlanDescription = description[:64]
        return ds

    @staticmethod
    def _controler(chemin, uid_attendu):
        """Relit le fichier écrit et vérifie son identité.

        Contrôler l'objet en mémoire ne prouve rien sur le fichier : c'est
        précisément ainsi qu'un premier correctif est passé pour bon alors que
        le préambule manquait encore. La relecture se fait donc **sans**
        `force=True`, ce qui vérifie du même coup la conformité Part 10.
        """
        relu = pydicom.dcmread(str(chemin))
        meta = relu.file_meta
        if relu.SOPInstanceUID != uid_attendu:
            raise SystemExit(f"{chemin} : SOPInstanceUID écrit incohérent.")
        if meta.MediaStorageSOPInstanceUID != relu.SOPInstanceUID:
            raise SystemExit(f"{chemin} : méta-en-tête et dataset en désaccord "
                             "sur le SOP Instance UID.")
        if meta.MediaStorageSOPClassUID != relu.SOPClassUID:
            raise SystemExit(f"{chemin} : méta-en-tête et dataset en désaccord "
                             "sur la SOP Class.")
        syntaxe = meta.TransferSyntaxUID
        if relu.original_encoding != (syntaxe.is_implicit_VR, syntaxe.is_little_endian):
            raise SystemExit(f"{chemin} : TransferSyntaxUID déclaré "
                             f"({syntaxe.name}) ≠ encodage réellement écrit.")
