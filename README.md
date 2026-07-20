# Cover Review

Cover Review est une interface Web locale destinée à repérer les pochettes d’albums trop petites, rechercher des images de remplacement et les valider visuellement avant écriture.

## Version 1.3.0

Cette version améliore la couverture des recherches :

- ajout de fanart.tv, avec clés configurées localement ;
- recherche MusicBrainz sur des variantes prudentes des titres, par exemple sans suffixe « Deluxe Edition » ou « Remastered » ;
- choix des sources activables dans les réglages ;
- invalidation automatique des anciens résultats lorsque les sources ou les clés changent ;
- aucune clé API ni donnée propre à une machine n’est incluse dans le projet.

L’intégration optionnelle des pochettes dans les fichiers audio reste disponible :

- conservation de la grande image sélectionnée dans `cover.jpg` ;
- création d’une version JPEG séparée pour les tags ;
- taille maximale intégrée configurable, 1000 px par défaut ;
- qualité JPEG configurable, 88 par défaut ;
- remplacement de la pochette avant uniquement lorsque le format le permet ;
- sauvegarde des anciennes pochettes externes et intégrées ;
- restauration via l’action d’annulation ;
- aucun chemin propre à une machine ou à un utilisateur n’est inclus dans le projet.

La recherche et la validation en lot restent disponibles :

- recherches en arrière-plan ;
- affichage uniquement des albums ayant au moins un candidat conforme ;
- premier candidat sélectionné par défaut ;
- validation groupée des albums cochés ;
- réutilisation des résultats préparés dans la vue individuelle.

## Formats pris en charge pour l’intégration

- MP3 : cadre ID3 `APIC` de type pochette avant ;
- FLAC : bloc `PICTURE` de type pochette avant ;
- Ogg Vorbis et Opus : champ `METADATA_BLOCK_PICTURE` ;
- M4A et MP4 : champ `covr`.

Pour les fichiers M4A et MP4, le format ne distingue pas toujours plusieurs rôles de pochettes de la même façon que FLAC ou ID3. Le champ `covr` existant est donc remplacé par la nouvelle image.

Les autres formats audio restent utilisables dans la bibliothèque, mais leur pochette n’est pas intégrée dans les tags. Le fichier `cover.jpg` peut néanmoins être créé normalement.

## Sécurité et sauvegardes

- aucune image n’est appliquée sans validation individuelle ou en lot ;
- l’intégration dans les tags est désactivée par défaut ;
- les autres métadonnées audio sont conservées ;
- les anciennes pochettes sont sauvegardées dans `.cover-review-backups` ;
- une erreur pendant l’écriture déclenche une tentative de restauration ;
- le serveur écoute uniquement sur `127.0.0.1`.

Il reste conseillé de disposer d’une sauvegarde normale de la bibliothèque avant toute modification massive de fichiers audio.

## Installation

```bash
cd cover-review
./install.sh
./run.sh
```

Ouvrir ensuite : `http://127.0.0.1:5000`

Au premier lancement, renseigner le chemin de la bibliothèque musicale et la résolution minimale, puis lancer le scan.

## Mise à jour

Décompresser la nouvelle archive par-dessus le dossier existant, puis relancer :

```bash
unzip -o cover-review.zip
cd cover-review
./run.sh
```

La base et les réglages sont conservés dans le dossier de données standard de l’utilisateur :

```text
~/.local/share/cover-review/
```

Ce chemin peut être remplacé avec la variable d’environnement `COVER_REVIEW_DATA_DIR`.

## Réglages d’écriture

Les réglages permettent de choisir indépendamment :

- la création de `cover.jpg` ;
- l’intégration dans les fichiers audio ;
- la taille maximale de l’image intégrée ;
- la qualité JPEG de l’image intégrée.

Le réglage conseillé est :

```text
cover.jpg : résolution du candidat sélectionné
pochette intégrée : 1000 px maximum, JPEG qualité 88
```

L’image intégrée n’est jamais agrandie si le candidat est plus petit que la taille maximale configurée.

## Validation en lot

1. Ouvrir l’onglet **Validation en lot**.
2. Cliquer sur **Rechercher en arrière-plan**.
3. Contrôler les candidats qui apparaissent progressivement.
4. Décocher les résultats douteux ou choisir une autre proposition.
5. Cliquer sur **Valider les pochettes cochées**.

La validation peut prendre davantage de temps lorsque l’intégration dans les tags est activée, car chaque piste de chaque album doit être réécrite.

## Sources de pochettes

La recherche peut utiliser :

- MusicBrainz pour identifier les albums et leurs éditions ;
- Cover Art Archive pour les pochettes associées aux éditions MusicBrainz ;
- TheAudioDB comme source complémentaire ;
- fanart.tv pour les pochettes liées à un identifiant MusicBrainz Release Group.

fanart.tv nécessite une clé API de projet. Une clé personnelle `client_key` peut être ajoutée facultativement. Les clés sont enregistrées uniquement dans la base locale de l’utilisateur et ne doivent jamais être commitées dans le dépôt.

Une image locale ou une URL directe peut également être utilisée.

## Données créées

Les données de l’application sont stockées dans `~/.local/share/cover-review/` :

- `cover-review.sqlite3` : albums, résultats de recherche et état de validation ;
- `cache/` : pochettes intégrées extraites et données temporaires.

Les sauvegardes sont placées dans les dossiers d’albums, par exemple :

```text
.cover-review-backups/20260720-143000-cover.jpg
.cover-review-backups/20260720-143000-000000-embedded/
```
