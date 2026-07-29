-- =====================================================================
-- Relier une séance du log machine à son dossier Mosaiq
-- =====================================================================
--
-- Requête en LECTURE SEULE. Elle ne modifie rien.
--
-- Le TRF ne contient aucun nom de plan ni identifiant patient : seulement
-- un numéro de machine, un horodatage, et deux étiquettes de champ. Le lien
-- vers le dossier se fait donc dans Mosaiq, pas dans un fichier DICOM.
--
-- Structure d'après l'implémentation de pymedphys
-- (`_mosaiq/delivery.py` et `_mosaiq/helpers.py`), qui interroge ces mêmes
-- tables pour identifier les logs.
--
--
-- LES TROIS PARAMÈTRES
-- --------------------
--   @machine       le numéro de série écrit dans l'en-tête du TRF.
--                  Dans Mosaiq, une machine est un enregistrement de la
--                  table Staff : on compare à Staff.Last_Name, espaces
--                  retirés.
--
--   @debut_local   colonne `debut_local` de seances.csv
--   @fin_local     colonne `fin_local` de seances.csv
--
--                  ⚠ EN HEURE LOCALE. Les horodatages du TRF sont en UTC —
--                  le « Z » final de la date le signale — alors que Mosaiq
--                  travaille en heure locale. Sans conversion, aucune séance
--                  n'est retrouvée : une délivrance de 8 h apparaît à 6 h et
--                  peut même tomber la veille.
--
--                  Rappel : la date de l'en-tête marque la FIN de la
--                  délivrance, pas son début. Le script organiser_trf.py
--                  fournit les deux bornes.
--
-- =====================================================================

DECLARE @machine      VARCHAR(50) = '____';                 -- ex. '2619'
DECLARE @debut_local  DATETIME    = '2026-07-28 08:00:00';
DECLARE @fin_local    DATETIME    = '2026-07-28 08:10:00';

SELECT
    -- Identité du dossier
    Ident.IDA                        AS patient_id,
    Patient.Last_Name,
    Patient.First_Name,

    -- Ce que vous cherchiez : le nom du site de traitement
    Site.Site_Name                   AS nom_du_site,

    -- Le champ délivré. Field_Label et Field_Name sont exactement les deux
    -- étiquettes présentes dans l'en-tête du TRF — dans Monaco, ce sont
    -- respectivement le « Field ID » et la « Description ».
    TxField.Field_Label,
    TxField.Field_Name,
    TxField.FLD_ID,
    TxField.Meterset                 AS mu_prevues,
    TxField.Type_Enum                AS type_de_champ,
    TxField.Version                  AS version_du_champ,

    -- La délivrance elle-même
    TrackTreatment.Create_DtTm       AS debut_mosaiq,
    TrackTreatment.Edit_DtTm         AS fin_mosaiq,
    TrackTreatment.WasBeamComplete   AS faisceau_termine,
    TrackTreatment.WasQAMode         AS mode_qa,

    -- La machine, telle que Mosaiq la nomme
    REPLACE(Staff.Last_Name, ' ', '') AS machine

FROM TrackTreatment
    INNER JOIN Ident   ON TrackTreatment.Pat_ID1 = Ident.Pat_ID1
    INNER JOIN Patient ON Patient.Pat_ID1        = Ident.Pat_ID1
    INNER JOIN TxField ON TrackTreatment.FLD_ID  = TxField.FLD_ID
    INNER JOIN Staff   ON Staff.Staff_ID         = TrackTreatment.Machine_ID_Staff_ID
    -- LEFT JOIN : un champ sans site rattaché ne doit pas disparaître
    LEFT  JOIN Site    ON TxField.SIT_Set_ID     = Site.SIT_Set_ID

WHERE
    REPLACE(Staff.Last_Name, ' ', '') = @machine
    -- Recouvrement de deux intervalles : la délivrance Mosaiq et la séance
    -- du log se chevauchent. Plus robuste qu'une égalité d'horodatage, les
    -- deux horloges n'étant pas nécessairement synchronisées.
    AND TrackTreatment.Create_DtTm <= @fin_local
    AND TrackTreatment.Edit_DtTm   >= @debut_local

ORDER BY TrackTreatment.Create_DtTm;


-- =====================================================================
-- CE QU'IL FAUT VÉRIFIER SUR LES PREMIERS RÉSULTATS
-- =====================================================================
--
-- 1. Les deux horloges concordent-elles ?
--    Comparer `debut_mosaiq` / `fin_mosaiq` aux bornes du log. Un décalage
--    systématique de quelques secondes est normal — la machine et le serveur
--    Mosaiq ne sont pas synchronisés à la seconde près. Un décalage
--    important signalerait autre chose, et il faudra l'élargir dans la
--    clause WHERE.
--
-- 2. Field_Label et Field_Name correspondent-ils au TRF ?
--    Si oui, l'appariement peut se faire sur ces étiquettes en plus de
--    l'horodatage, ce qui lève toute ambiguïté quand plusieurs délivrances
--    se chevauchent.
--
--    ⚠ Réserve mesurée : sur les fichiers de référence, `field_label` valait
--    '1-2' dans deux fichiers de contenus différents. Il n'est donc pas
--    fiable seul.
--
-- 3. `WasBeamComplete` recoupe-t-il l'état final du log ?
--    C'est le contrôle croisé qui manquait au découpage en séances : le log
--    dit « Terminated Ok » ou « Terminated Fault », Mosaiq dit vrai ou faux.
--    Les deux sources sont indépendantes. Si elles s'accordent, le découpage
--    est validé par autre chose que lui-même.
--
-- 4. `Meterset` correspond-il aux MU du log ?
--    C'est le total prévu pour ce champ — l'équivalent du `BeamMeterset`
--    d'un RT Plan DICOM. Il remplace avantageusement l'estimation par
--    champ que faisait organiser_trf.py.
--
-- =====================================================================
