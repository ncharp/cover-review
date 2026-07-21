# Cover Review

Cover Review est une interface Web locale destinée à repérer les pochettes d’albums trop petites, rechercher des images de remplacement et les valider visuellement avant écriture.

## Version 1.5.4

Cette version permet d’afficher volontairement les candidats rejetés par le filtre de résolution :

- les propositions trop petites ou dont les dimensions ne peuvent pas être vérifiées restent masquées par défaut ;
- un bouton permet de les afficher sans modifier le seuil configuré ;
- chaque carte concernée porte un avertissement visible ;
- une proposition sous le minimum peut être sélectionnée et enregistrée explicitement, sans confirmation supplémentaire ;
- les candidats conformes restent sélectionnés automatiquement en priorité.

Cette version renforce la détection Bandcamp lorsqu’un résultat visible dans le
navigateur n’est pas renvoyé correctement au client HTTP de l’application :

- correction des balises autofermantes comme `<img />`, qui pouvaient terminer une carte trop tôt ;
- lecture des URL éventuellement présentes dans les attributs de la carte de résultat ;
- détection supplémentaire des URL échappées dans le HTML ou dans des blocs de données ;
- séparation correcte des métadonnées Bandcamp du type `Album, by Artist` ;
- recours direct à quelques URL Bandcamp probables lorsque la page de recherche renvoie un défi JavaScript ou un HTML inexploitable ;
- invalidation des anciens résultats vides de la recherche en arrière-plan.

Le recours Bandcamp facultatif reste disponible dans la recherche en arrière-plan :

- Bandcamp n’est interrogé que lorsque les sources régulières n’ont fourni aucun candidat ;
- le résultat n’est conservé que si la recherche retourne exactement une page d’album ;
- l’artiste et le titre sont comparés de façon conservatrice aux métadonnées locales ;
- la pochette reste présélectionnée pour validation humaine, jamais appliquée automatiquement ;
- les requêtes sont séquentielles, espacées et mises en cache avec les autres résultats.

La recherche Bandcamp manuelle reste disponible :

- bouton **Bandcamp** dans la vue individuelle ;
- recherche préremplie avec l’artiste et l’album affichés ;
- résultats présentés comme les autres candidats ;
- premier candidat conforme sélectionné automatiquement ;
- lien vers la recherche Bandcamp dans le navigateur lorsqu’aucun résultat exploitable n’est trouvé ;
- possibilité de coller directement l’URL d’une page d’album ou de morceau Bandcamp ;
- récupération de la version 1200 px ou de l’image originale selon la résolution minimale configurée.

Bandcamp ne fournit pas d’API publique de recherche de catalogue adaptée à cet usage. Cette intégration lit donc de manière limitée les pages publiques de recherche et de sortie. Elle peut cesser de fonctionner si la structure du site change.

Les fonctions ajoutées dans les versions précédentes restent disponibles :

- MusicBrainz et Cover Art Archive ;
- TheAudioDB ;
- fanart.tv avec clé configurée localement ;
- variantes prudentes de recherche ;
- recherche et validation en lot ;
- création de `cover.jpg` ;
- intégration optionnelle dans les tags audio ;
- sauvegarde et annulation.

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

## Installation et lancement

### Exécutable prêt à l'emploi (aucune installation)

Des exécutables Windows, Linux et macOS sont construits automatiquement à chaque release et proposés sur la [page Releases](https://github.com/ncharp/cover-review/releases). Télécharger le fichier correspondant au système puis le lancer : le navigateur s'ouvre automatiquement sur l'interface.

Les binaires ne sont pas signés. Windows SmartScreen ou macOS Gatekeeper peuvent afficher un avertissement au premier lancement.

Les méthodes suivantes demandent Python 3.9 ou plus récent.

### Avec pipx

```bash
pipx install git+https://github.com/ncharp/cover-review.git
cover-review
```

pipx s'installe avec `sudo apt install pipx` sur Debian et Ubuntu, ou `brew install pipx` sur macOS.

### Avec uv, sans installation

```bash
uvx --from git+https://github.com/ncharp/cover-review cover-review
```

### Depuis les sources

```bash
git clone https://github.com/ncharp/cover-review.git
cd cover-review
./run.sh
```

Le premier lancement crée automatiquement l'environnement Python et installe les dépendances. Les lancements suivants démarrent directement. Le module `venv` est requis (paquet `python3-venv` sur Debian et Ubuntu).

### Au lancement

Le navigateur s'ouvre automatiquement sur `http://127.0.0.1:5000`. Deux variables d'environnement permettent d'ajuster ce comportement :

```bash
COVER_REVIEW_PORT=8080 cover-review      # changer le port
COVER_REVIEW_NO_BROWSER=1 cover-review   # ne pas ouvrir le navigateur
```

Au premier lancement, renseigner le chemin de la bibliothèque musicale et la résolution minimale, puis lancer le scan.

## Mise à jour

Avec pipx :

```bash
pipx reinstall cover-review
```

Depuis les sources :

```bash
cd cover-review
git pull
./run.sh
```

Si les dépendances ont changé, elles sont réinstallées automatiquement au lancement.

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
- Bandcamp en recours facultatif, seulement si les autres sources n’ont rien trouvé et si un résultat d’album unique correspond aux métadonnées.

fanart.tv nécessite une clé API de projet. Une clé personnelle `client_key` peut être ajoutée facultativement. Les clés sont enregistrées uniquement dans la base locale de l’utilisateur et ne doivent jamais être commitées dans le dépôt.

Une image locale, une URL directe ou une page de sortie Bandcamp peut également être utilisée.

La recherche Bandcamp manuelle se lance depuis la vue individuelle avec le bouton **Bandcamp**. Le recours automatique peut être activé séparément dans les réglages.

## Données créées

Les données de l’application sont stockées dans `~/.local/share/cover-review/` :

- `cover-review.sqlite3` : albums, résultats de recherche et état de validation ;
- `cache/` : pochettes intégrées extraites et données temporaires.

Les sauvegardes sont placées dans les dossiers d’albums, par exemple :

```text
.cover-review-backups/20260720-143000-cover.jpg
.cover-review-backups/20260720-143000-000000-embedded/
```

## Fournisseurs tiers

Album artwork may be provided by [fanart.tv](https://fanart.tv/).
Users must configure their own fanart.tv API key in the application settings.

## Licence

Cover Review est distribué sous licence MIT. Voir [LICENSE](LICENSE).


## Diagnostic Bandcamp

Cover Review écrit un journal local dans le dossier de données de l’application :

```text
~/.local/share/cover-review/cover-review.log
```

Pour suivre les requêtes Bandcamp en temps réel :

```bash
tail -f ~/.local/share/cover-review/cover-review.log
```

Le journal indique les URL directes essayées, le code HTTP, la présence éventuelle
d’une page de protection JavaScript, les métadonnées extraites et la raison d’un rejet.
Les clés API ne sont pas écrites dans ce journal.
