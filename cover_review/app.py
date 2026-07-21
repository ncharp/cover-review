from __future__ import annotations

import base64
import difflib
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import webbrowser
from dataclasses import dataclass
from html import unescape as html_unescape
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file
from mutagen import File as MutagenFile, MutagenError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image, ImageFile, ImageOps, UnidentifiedImageError

APP_NAME = "Cover Review"
APP_VERSION = "1.5.4"
SEARCH_CACHE_VERSION = 7
BATCH_RESULT_VERSION = 5
DATA_DIR = Path(os.environ.get("COVER_REVIEW_DATA_DIR", str(Path.home() / ".local" / "share" / "cover-review"))).expanduser()
CACHE_DIR = DATA_DIR / "cache"
CURRENT_CACHE_DIR = CACHE_DIR / "current"
DB_PATH = DATA_DIR / "cover-review.sqlite3"
LOG_PATH = DATA_DIR / "cover-review.log"

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".oga", ".opus",
    ".wav", ".wma", ".ape", ".wv", ".aiff", ".aif", ".mka",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
COVER_STEMS = {
    "cover", "folder", "front", "album", "albumart", "album art",
    "folderart", "folder art", "artwork",
}
DISC_DIR_RE = re.compile(r"^(?:cd|disc|disk|disque)[ _.-]*\d+$", re.IGNORECASE)
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
REQUEST_TIMEOUT = (8, 30)

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CURRENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

LOGGER = logging.getLogger("cover-review")
LOGGER.setLevel(logging.INFO)
if not LOGGER.handlers:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(stream_handler)

BANDCAMP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_DOWNLOAD_BYTES

SCAN_STATE: dict[str, Any] = {
    "running": False,
    "folders_seen": 0,
    "albums_found": 0,
    "message": "",
    "error": None,
    "started_at": None,
    "finished_at": None,
}
SCAN_LOCK = threading.Lock()
MB_LOCK = threading.Lock()
LAST_MB_REQUEST = 0.0
TADB_LOCK = threading.Lock()
LAST_TADB_REQUEST = 0.0
FANART_LOCK = threading.Lock()
LAST_FANART_REQUEST = 0.0
BANDCAMP_LOCK = threading.Lock()
LAST_BANDCAMP_REQUEST = 0.0

BACKGROUND_SEARCH_STATE: dict[str, Any] = {
    "running": False,
    "stop_requested": False,
    "total": 0,
    "processed": 0,
    "ready": 0,
    "empty": 0,
    "errors": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
}
BACKGROUND_SEARCH_LOCK = threading.Lock()
BACKGROUND_SEARCH_STOP = threading.Event()

BATCH_APPLY_STATE: dict[str, Any] = {
    "running": False,
    "total": 0,
    "processed": 0,
    "succeeded": 0,
    "failed": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
}
BATCH_APPLY_LOCK = threading.Lock()


@dataclass
class Metadata:
    artist: str
    album: str
    year: str
    mb_release_id: str
    mb_release_group_id: str


@dataclass
class WriteResult:
    width: int
    height: int
    embedded_written: int = 0
    embedded_skipped: int = 0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS albums (
                id TEXT PRIMARY KEY,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                year TEXT NOT NULL DEFAULT '',
                album_root TEXT NOT NULL,
                directories_json TEXT NOT NULL,
                audio_path TEXT NOT NULL,
                current_path TEXT,
                current_source TEXT,
                current_width INTEGER,
                current_height INTEGER,
                mb_release_id TEXT NOT NULL DEFAULT '',
                mb_release_group_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                selected_source TEXT,
                selected_url TEXT,
                last_backup_json TEXT,
                scan_seen INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_albums_status ON albums(status);
            CREATE INDEX IF NOT EXISTS idx_albums_artist_album ON albums(artist, album);

            CREATE TABLE IF NOT EXISTS search_cache (
                cache_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS album_search_results (
                album_id TEXT PRIMARY KEY,
                config_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'not_started',
                query_artist TEXT NOT NULL DEFAULT '',
                query_album TEXT NOT NULL DEFAULT '',
                candidates_json TEXT NOT NULL DEFAULT '[]',
                selected_index INTEGER NOT NULL DEFAULT 0,
                checked INTEGER NOT NULL DEFAULT 1,
                error TEXT,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(album_id) REFERENCES albums(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_album_search_status
                ON album_search_results(status);
            """
        )
        # A process interrupted during a search can safely resume those albums.
        conn.execute(
            "UPDATE album_search_results SET status='not_started', error=NULL "
            "WHERE status IN ('queued', 'searching')"
        )
        defaults = {
            "library_root": "",
            "min_size": "1000",
            "include_missing": "1",
            "max_candidates": "16",
            "batch_candidates": "4",
            "save_external_cover": "1",
            "embed_cover": "0",
            "embed_max_size": "1000",
            "embed_quality": "88",
            "source_musicbrainz": "1",
            "source_theaudiodb": "1",
            "source_fanart": "0",
            "source_bandcamp": "0",
            "fanart_api_key": "",
            "fanart_client_key": "",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )


init_db()


def get_settings() -> dict[str, str]:
    with db_connect() as conn:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def update_settings(values: dict[str, Any]) -> dict[str, str]:
    allowed = {
        "library_root",
        "min_size",
        "include_missing",
        "max_candidates",
        "batch_candidates",
        "save_external_cover",
        "embed_cover",
        "embed_max_size",
        "embed_quality",
        "source_musicbrainz",
        "source_theaudiodb",
        "source_fanart",
        "source_bandcamp",
        "fanart_api_key",
        "fanart_client_key",
    }
    with db_connect() as conn:
        for key, value in values.items():
            if key not in allowed:
                continue
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
    return get_settings()


def first_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return first_text(value[0]) if value else ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def tag_value(tags: Any, names: Iterable[str]) -> str:
    if tags is None:
        return ""
    for name in names:
        try:
            if name in tags:
                return first_text(tags[name])
        except (KeyError, TypeError):
            pass
        try:
            value = tags.get(name)
            if value:
                return first_text(value)
        except (AttributeError, KeyError, TypeError):
            pass
    return ""


def clean_year(value: str) -> str:
    match = re.search(r"\b(18|19|20)\d{2}\b", value or "")
    return match.group(0) if match else ""


def read_metadata(audio_path: Path) -> Metadata:
    artist = ""
    album = ""
    year = ""
    mb_release_id = ""
    mb_release_group_id = ""

    try:
        easy = MutagenFile(audio_path, easy=True)
        if easy is not None:
            artist = tag_value(easy, ["albumartist", "album artist", "artist"])
            album = tag_value(easy, ["album"])
            year = clean_year(tag_value(easy, ["date", "year", "originaldate"]))
            mb_release_id = tag_value(
                easy,
                ["musicbrainz_albumid", "musicbrainz release id", "musicbrainz_album_id"],
            )
            mb_release_group_id = tag_value(
                easy,
                [
                    "musicbrainz_releasegroupid",
                    "musicbrainz release group id",
                    "musicbrainz_release_group_id",
                ],
            )
    except Exception:
        pass

    # Some formats expose MusicBrainz identifiers only in non-easy tags.
    if not mb_release_id or not mb_release_group_id:
        try:
            raw = MutagenFile(audio_path, easy=False)
            tags = getattr(raw, "tags", None)
            mb_release_id = mb_release_id or tag_value(
                tags,
                [
                    "MUSICBRAINZ_ALBUMID",
                    "MusicBrainz Album Id",
                    "TXXX:MusicBrainz Album Id",
                    "----:com.apple.iTunes:MusicBrainz Album Id",
                ],
            )
            mb_release_group_id = mb_release_group_id or tag_value(
                tags,
                [
                    "MUSICBRAINZ_RELEASEGROUPID",
                    "MusicBrainz Release Group Id",
                    "TXXX:MusicBrainz Release Group Id",
                    "----:com.apple.iTunes:MusicBrainz Release Group Id",
                ],
            )
        except Exception:
            pass

    if not album:
        album = audio_path.parent.name
    if not artist:
        artist = audio_path.parent.parent.name if audio_path.parent.parent else "Artiste inconnu"

    return Metadata(
        artist=artist or "Artiste inconnu",
        album=album or "Album inconnu",
        year=year,
        mb_release_id=mb_release_id,
        mb_release_group_id=mb_release_group_id,
    )


def album_root_for(audio_dir: Path) -> Path:
    if DISC_DIR_RE.match(audio_dir.name):
        return audio_dir.parent
    return audio_dir


def recognized_cover_files(directory: Path) -> list[Path]:
    recognized: list[Path] = []
    all_images: list[Path] = []
    try:
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            all_images.append(path)
            stem = re.sub(r"[_-]+", " ", path.stem.lower()).strip()
            if stem in COVER_STEMS or re.fullmatch(r"(?:cover|folder|front)(?: ?(?:art|image))?", stem):
                recognized.append(path)
    except (OSError, PermissionError):
        return []
    if recognized:
        return sorted(recognized, key=cover_priority)
    # A single image in an album folder is usually its cover, even with a custom filename.
    if len(all_images) == 1:
        return all_images
    return []


def cover_priority(path: Path) -> tuple[int, str]:
    stem = path.stem.lower()
    priorities = {
        "cover": 0,
        "folder": 1,
        "front": 2,
        "albumart": 3,
        "album": 4,
    }
    return (priorities.get(stem, 10), path.name.lower())


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    try:
        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except (OSError, UnidentifiedImageError, ValueError):
        return None, None


def extract_embedded_cover(audio_path: Path, album_id: str) -> Path | None:
    data: bytes | None = None
    mime = "image/jpeg"

    try:
        suffix = audio_path.suffix.lower()
        if suffix == ".flac":
            audio = FLAC(audio_path)
            pictures = sorted(audio.pictures, key=lambda pic: 0 if pic.type == 3 else 1)
            if pictures:
                data = pictures[0].data
                mime = pictures[0].mime or mime
        elif suffix == ".mp3":
            tags = ID3(audio_path)
            pictures = [frame for frame in tags.values() if isinstance(frame, APIC)]
            pictures.sort(key=lambda pic: 0 if pic.type == 3 else 1)
            if pictures:
                data = pictures[0].data
                mime = pictures[0].mime or mime
        elif suffix in {".m4a", ".mp4"}:
            audio = MP4(audio_path)
            covers = (audio.tags or {}).get("covr", [])
            if covers:
                cover = covers[0]
                data = bytes(cover)
                if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG:
                    mime = "image/png"
        else:
            audio = MutagenFile(audio_path, easy=False)
            pictures = getattr(audio, "pictures", None)
            if pictures:
                data = pictures[0].data
                mime = getattr(pictures[0], "mime", mime) or mime
    except Exception:
        return None

    if not data:
        return None

    extension = mimetypes.guess_extension(mime) or ".jpg"
    if extension == ".jpe":
        extension = ".jpg"
    output = CURRENT_CACHE_DIR / f"{album_id}{extension}"
    try:
        output.write_bytes(data)
        with Image.open(output) as img:
            img.verify()
        return output
    except Exception:
        output.unlink(missing_ok=True)
        return None


def find_current_cover(album_root: Path, audio_dirs: list[Path], audio_path: Path, album_id: str) -> tuple[Path | None, str, int | None, int | None]:
    search_dirs: list[Path] = []
    for directory in [album_root, *audio_dirs]:
        if directory not in search_dirs:
            search_dirs.append(directory)

    for directory in search_dirs:
        covers = recognized_cover_files(directory)
        if covers:
            width, height = image_dimensions(covers[0])
            return covers[0], "external", width, height

    embedded = extract_embedded_cover(audio_path, album_id)
    if embedded:
        width, height = image_dimensions(embedded)
        return embedded, "embedded", width, height

    return None, "missing", None, None


def should_include(width: int | None, height: int | None, min_size: int, include_missing: bool) -> bool:
    if width is None or height is None:
        return include_missing
    return width < min_size or height < min_size


def album_id_for(album_root: Path) -> str:
    return hashlib.sha1(str(album_root.resolve()).encode("utf-8")).hexdigest()[:20]


def scan_library_worker() -> None:
    global SCAN_STATE
    with SCAN_LOCK:
        if SCAN_STATE["running"]:
            return
        SCAN_STATE = {
            "running": True,
            "folders_seen": 0,
            "albums_found": 0,
            "message": "Préparation du scan",
            "error": None,
            "started_at": utc_now(),
            "finished_at": None,
        }

    try:
        settings = get_settings()
        root_text = settings.get("library_root", "").strip()
        if not root_text:
            raise ValueError("Le chemin de la bibliothèque n'est pas configuré.")
        root = Path(root_text).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Le dossier n'existe pas ou n'est pas accessible : {root}")

        min_size = max(1, int(settings.get("min_size", "1000")))
        include_missing = settings.get("include_missing", "1") == "1"

        groups: dict[Path, dict[str, Any]] = {}
        for current_dir, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name for name in dirnames
                if name not in {".cover-review-backups", ".git", "@eaDir"}
            ]
            current = Path(current_dir)
            audio_files = sorted(
                current / name
                for name in filenames
                if Path(name).suffix.lower() in AUDIO_EXTENSIONS
            )
            with SCAN_LOCK:
                SCAN_STATE["folders_seen"] += 1
                SCAN_STATE["message"] = f"Analyse de {current}"

            if not audio_files:
                continue

            album_root = album_root_for(current).resolve()
            group = groups.setdefault(
                album_root,
                {"audio_dirs": [], "audio_files": []},
            )
            if current.resolve() not in group["audio_dirs"]:
                group["audio_dirs"].append(current.resolve())
            group["audio_files"].extend(audio_files)

        now = utc_now()
        with db_connect() as conn:
            conn.execute("UPDATE albums SET scan_seen=0")

        found = 0
        for album_root, group in sorted(groups.items(), key=lambda item: str(item[0]).lower()):
            audio_files = sorted(group["audio_files"])
            audio_dirs = sorted(set(group["audio_dirs"]))
            first_audio = audio_files[0]
            metadata = read_metadata(first_audio)
            album_id = album_id_for(album_root)
            current_path, current_source, width, height = find_current_cover(
                album_root, audio_dirs, first_audio, album_id
            )

            if not should_include(width, height, min_size, include_missing):
                continue

            found += 1
            with db_connect() as conn:
                existing = conn.execute("SELECT status FROM albums WHERE id=?", (album_id,)).fetchone()
                status = existing["status"] if existing and existing["status"] in {"approved", "skipped"} else "pending"
                # If a previously approved cover is again below the threshold, return it to pending.
                if status == "approved" and should_include(width, height, min_size, include_missing):
                    status = "pending"
                conn.execute(
                    """
                    INSERT INTO albums(
                        id, artist, album, year, album_root, directories_json,
                        audio_path, current_path, current_source, current_width,
                        current_height, mb_release_id, mb_release_group_id,
                        status, scan_seen, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        artist=excluded.artist,
                        album=excluded.album,
                        year=excluded.year,
                        album_root=excluded.album_root,
                        directories_json=excluded.directories_json,
                        audio_path=excluded.audio_path,
                        current_path=excluded.current_path,
                        current_source=excluded.current_source,
                        current_width=excluded.current_width,
                        current_height=excluded.current_height,
                        mb_release_id=excluded.mb_release_id,
                        mb_release_group_id=excluded.mb_release_group_id,
                        status=?,
                        scan_seen=1,
                        updated_at=excluded.updated_at
                    """,
                    (
                        album_id,
                        metadata.artist,
                        metadata.album,
                        metadata.year,
                        str(album_root),
                        json.dumps([str(path) for path in audio_dirs], ensure_ascii=False),
                        str(first_audio),
                        str(current_path) if current_path else None,
                        current_source,
                        width,
                        height,
                        metadata.mb_release_id,
                        metadata.mb_release_group_id,
                        status,
                        now,
                        now,
                        status,
                    ),
                )

            with SCAN_LOCK:
                SCAN_STATE["albums_found"] = found

        with db_connect() as conn:
            conn.execute("DELETE FROM albums WHERE scan_seen=0 AND status='pending'")

        with SCAN_LOCK:
            SCAN_STATE["running"] = False
            SCAN_STATE["message"] = f"Scan terminé : {found} album(s) à vérifier"
            SCAN_STATE["finished_at"] = utc_now()
    except Exception as exc:
        with SCAN_LOCK:
            SCAN_STATE["running"] = False
            SCAN_STATE["error"] = str(exc)
            SCAN_STATE["message"] = "Échec du scan"
            SCAN_STATE["finished_at"] = utc_now()


def row_to_album(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "artist": row["artist"],
        "album": row["album"],
        "year": row["year"],
        "album_root": row["album_root"],
        "directories": json.loads(row["directories_json"]),
        "current_source": row["current_source"],
        "current_width": row["current_width"],
        "current_height": row["current_height"],
        "has_current": bool(row["current_path"] and Path(row["current_path"]).is_file()),
        "status": row["status"],
        "mb_release_id": row["mb_release_id"],
        "mb_release_group_id": row["mb_release_group_id"],
        "selected_source": row["selected_source"],
        "selected_url": row["selected_url"],
        "cover_version": hashlib.sha1(
            f"{row['current_path'] or ''}|{row['updated_at']}".encode("utf-8")
        ).hexdigest()[:12],
        "can_undo": bool(row["last_backup_json"]),
    }


def get_album_or_404(album_id: str) -> sqlite3.Row:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
    if row is None:
        raise KeyError("Album introuvable")
    return row


def lucene_quote(value: str) -> str:
    # Escape Lucene characters inside a quoted value.
    escaped = re.sub(r'([+\-!(){}\[\]^"~*?:\\/])', r"\\\1", value)
    return f'"{escaped}"'


def mb_get(path: str, params: dict[str, Any] | None = None) -> requests.Response:
    global LAST_MB_REQUEST
    with MB_LOCK:
        wait = 1.1 - (time.monotonic() - LAST_MB_REQUEST)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(
            f"https://musicbrainz.org/ws/2/{path.lstrip('/')}",
            params=params,
            headers={
                "User-Agent": f"{APP_NAME.replace(' ', '')}/{APP_VERSION} (local desktop application)",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        LAST_MB_REQUEST = time.monotonic()
    response.raise_for_status()
    return response


def caa_json(
    entity: str,
    mbid: str,
    errors: list[str] | None = None,
    attempts: int = 3,
) -> dict[str, Any] | None:
    """Read CAA metadata, retrying transient TLS/network failures.

    A 404 is a normal "no artwork" result. Other failures are retried and
    reported to the caller so an incomplete search is not cached for a week.
    """
    url = f"https://coverartarchive.org/{entity}/{mbid}/"
    retryable_errors = (
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ReadTimeout,
    )
    last_error: Exception | None = None

    for attempt in range(max(1, attempts)):
        try:
            response = requests.get(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": f"CoverReview/{APP_VERSION}",
                    "Connection": "close",
                },
                timeout=(6, 20),
            )
            if response.status_code == 404:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            response.raise_for_status()
            return response.json()
        except retryable_errors as exc:
            last_error = exc
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            # Invalid JSON and temporary HTTP errors can also succeed on retry.

        if attempt + 1 < attempts:
            time.sleep(0.35 * (2 ** attempt))

    if errors is not None and last_error is not None:
        errors.append(f"Cover Art Archive {entity}/{mbid} : {last_error}")
    return None


def artist_credit_text(item: dict[str, Any]) -> str:
    credits = item.get("artist-credit") or []
    if isinstance(credits, str):
        return credits
    parts: list[str] = []
    for credit in credits:
        if isinstance(credit, str):
            parts.append(credit)
            continue
        parts.append(credit.get("name") or (credit.get("artist") or {}).get("name") or "")
        parts.append(credit.get("joinphrase") or "")
    return "".join(parts).strip()


def candidate_from_caa(
    payload: dict[str, Any] | None,
    *,
    source_type: str,
    mbid: str,
    title: str,
    artist: str,
    date: str = "",
    country: str = "",
    fmt: str = "",
    score: int | None = None,
    min_size: int = 1000,
) -> dict[str, Any] | None:
    if not payload:
        return None
    images = payload.get("images") or []
    if not images:
        return None
    image = next((img for img in images if img.get("front")), images[0])
    thumbs = image.get("thumbnails") or {}
    original = image.get("image")
    preview = thumbs.get("500") or thumbs.get("250") or original
    # La miniature CAA de 1200 px est suffisante jusqu'à un minimum de 1200 px.
    # Au-delà, il faut proposer l'original, dont les dimensions seront contrôlées
    # dans l'interface avant que la carte ne soit affichée.
    if min_size > 1200:
        download = original or thumbs.get("1200") or preview
    else:
        download = thumbs.get("1200") or original or preview
    if not preview or not download:
        return None
    return {
        "id": hashlib.sha1(download.encode("utf-8")).hexdigest()[:16],
        "source": "Cover Art Archive",
        "source_type": source_type,
        "mbid": mbid,
        "title": title,
        "artist": artist,
        "date": date,
        "country": country,
        "format": fmt,
        "score": score,
        "preview_url": preview.replace("http://", "https://"),
        "download_url": download.replace("http://", "https://"),
        "original_url": (original or download).replace("http://", "https://"),
        "musicbrainz_url": f"https://musicbrainz.org/{'release-group' if 'groupe' in source_type else 'release'}/{mbid}",
        "source_url": f"https://musicbrainz.org/{'release-group' if 'groupe' in source_type else 'release'}/{mbid}",
        "source_link_label": "MusicBrainz",
        "comment": image.get("comment") or "",
    }



def normalize_search_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def theaudiodb_candidate(artist: str, album: str, errors: list[str] | None = None) -> dict[str, Any] | None:
    global LAST_TADB_REQUEST
    try:
        with TADB_LOCK:
            # Free tier: 30 requests/minute. Keep a small safety margin.
            wait = 2.1 - (time.monotonic() - LAST_TADB_REQUEST)
            if wait > 0:
                time.sleep(wait)
            response = requests.get(
                "https://www.theaudiodb.com/api/v1/json/123/searchalbum.php",
                params={"s": artist, "a": album},
                headers={"User-Agent": f"CoverReview/{APP_VERSION}", "Accept": "application/json"},
                timeout=(6, 20),
            )
            LAST_TADB_REQUEST = time.monotonic()
        response.raise_for_status()
        albums = response.json().get("album") or []
    except (requests.RequestException, ValueError) as exc:
        if errors is not None:
            errors.append(f"TheAudioDB : {exc}")
        return None

    wanted_artist = normalize_search_text(artist)
    wanted_album = normalize_search_text(album)
    best: dict[str, Any] | None = None
    best_score = -1
    for item in albums:
        item_artist = str(item.get("strArtist") or "")
        item_album = str(item.get("strAlbum") or "")
        artist_match = normalize_search_text(item_artist) == wanted_artist
        album_match = normalize_search_text(item_album) == wanted_album
        score = (55 if album_match else 0) + (40 if artist_match else 0)
        if score > best_score:
            best = item
            best_score = score
    if not best or best_score < 55:
        return None

    original = best.get("strAlbumThumbHQ") or best.get("strAlbumThumb")
    if not original:
        return None
    preview = f"{original}/small"
    album_id = str(best.get("idAlbum") or "")
    source_url = f"https://www.theaudiodb.com/album/{album_id}" if album_id else "https://www.theaudiodb.com/"
    return {
        "id": hashlib.sha1(str(original).encode("utf-8")).hexdigest()[:16],
        "source": "TheAudioDB",
        "source_type": "album",
        "mbid": str(best.get("strMusicBrainzID") or ""),
        "title": str(best.get("strAlbum") or album),
        "artist": str(best.get("strArtist") or artist),
        "date": str(best.get("intYearReleased") or ""),
        "country": str(best.get("strLocation") or ""),
        "format": str(best.get("strReleaseFormat") or ""),
        "score": best_score,
        "preview_url": str(preview).replace("http://", "https://"),
        "download_url": str(original).replace("http://", "https://"),
        "original_url": str(original).replace("http://", "https://"),
        "musicbrainz_url": source_url,
        "source_url": source_url,
        "source_link_label": "TheAudioDB",
        "comment": "",
    }



def compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def search_query_variants(artist: str, album: str) -> list[tuple[str, str]]:
    """Build conservative alternate searches without changing stored metadata."""
    variants: list[tuple[str, str]] = []

    def add(candidate_artist: str, candidate_album: str) -> None:
        pair = (compact_spaces(candidate_artist), compact_spaces(candidate_album))
        if pair[0] and pair[1] and pair not in variants:
            variants.append(pair)

    normalized_artist = artist.replace("’", "'").replace("‘", "'")
    normalized_album = album.replace("’", "'").replace("‘", "'")
    add(normalized_artist, normalized_album)

    clean_artist = re.sub(
        r"\s+(?:feat(?:uring)?\.?|ft\.?)\s+.+$",
        "",
        normalized_artist,
        flags=re.IGNORECASE,
    )
    edition_terms = (
        r"deluxe|expanded|remaster(?:ed)?|anniversary|special|collector(?:'s)?|"
        r"limited|bonus(?: tracks?)?|reissue|édition|edition|version"
    )
    clean_album = re.sub(
        rf"\s*[\[(][^\])]*(?:{edition_terms})[^\])]*[\])]\s*$",
        "",
        normalized_album,
        flags=re.IGNORECASE,
    )
    clean_album = re.sub(
        rf"\s*[-–:]\s*(?:{edition_terms}).*$",
        "",
        clean_album,
        flags=re.IGNORECASE,
    )
    clean_album = re.sub(
        r"\s+(?:CD|Disc|Disk|Disque)\s*\d+\s*$",
        "",
        clean_album,
        flags=re.IGNORECASE,
    )
    add(clean_artist, clean_album)
    return variants



def is_bandcamp_release_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"}:
        return False
    if host != "bandcamp.com" and not host.endswith(".bandcamp.com"):
        return False
    return "/album/" in parsed.path or "/track/" in parsed.path


def canonical_bandcamp_release_url(url: str) -> str:
    """Normalize a Bandcamp release URL and remove search tracking parameters."""
    cleaned = html_unescape((url or "").strip()).replace("\\/", "/")
    parsed = urlparse(cleaned)
    if not is_bandcamp_release_url(cleaned):
        return cleaned
    return parsed._replace(query="", fragment="").geturl()


def bandcamp_art_variant(url: str, size_code: int) -> str:
    """Return another public Bandcamp artwork size when the URL matches bcbits."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if not host.endswith("bcbits.com"):
        return url.replace("http://", "https://")
    base, separator, query = url.partition("?")
    converted = re.sub(
        r"_\d+(?=\.(?:jpe?g|png|webp)$)",
        f"_{int(size_code)}",
        base,
        flags=re.IGNORECASE,
    )
    if converted == base:
        return url.replace("http://", "https://")
    result = converted + (separator + query if separator else "")
    return result.replace("http://", "https://")


def normalized_match_text(value: str) -> str:
    """Normalize human-readable metadata for conservative fuzzy matching."""
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold().replace("&", " and ")
    normalized = re.sub(r"\b(?:feat|featuring|ft)\.?\s+.*$", "", normalized)
    edition_terms = (
        r"deluxe|expanded|remaster(?:ed)?|anniversary|special|collector(?:'s)?|"
        r"limited|bonus(?: tracks?)?|reissue|edition|version"
    )
    normalized = re.sub(
        rf"\s*[\[(][^\])]*(?:{edition_terms})[^\])]*[\])]\s*$",
        "",
        normalized,
    )
    normalized = re.sub(rf"\s*[-–:]\s*(?:{edition_terms}).*$", "", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return compact_spaces(normalized)


def metadata_similarity(left: str, right: str) -> float:
    """Return a forgiving but bounded similarity score between two labels."""
    a = normalized_match_text(left)
    b = normalized_match_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sequence = difflib.SequenceMatcher(None, a, b).ratio()
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    token_score = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    containment = 0.0
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 4 and shorter in longer:
        containment = len(shorter) / len(longer)
    return max(sequence, token_score, containment)


def bandcamp_result_matches(item: dict[str, str], artist: str, album: str) -> bool:
    """Reject a unique search result when it still looks unrelated."""
    title = str(item.get("title") or "")
    result_artist = str(item.get("artist") or "")
    variants = search_query_variants(artist, album)
    title_score = max(
        (metadata_similarity(title, variant_album) for _, variant_album in variants),
        default=0.0,
    )
    artist_score = max(
        (metadata_similarity(result_artist, variant_artist) for variant_artist, _ in variants),
        default=0.0,
    )
    # Artist can occasionally be missing from a search card. In that case the
    # title must be almost exact before the result is accepted for review.
    if not normalized_match_text(result_artist):
        return title_score >= 0.92
    return title_score >= 0.78 and artist_score >= 0.70


def bandcamp_get(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
) -> requests.Response:
    """Fetch a Bandcamp HTML page conservatively and retry transient failures."""
    global LAST_BANDCAMP_REQUEST
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or (host != "bandcamp.com" and not host.endswith(".bandcamp.com")):
        raise ValueError("URL Bandcamp invalide.")

    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            LOGGER.info(
                "Bandcamp GET attempt=%s/%s url=%s params=%s",
                attempt + 1,
                max(1, attempts),
                url,
                params or {},
            )
            with BANDCAMP_LOCK:
                wait = 0.8 - (time.monotonic() - LAST_BANDCAMP_REQUEST)
                if wait > 0:
                    time.sleep(wait)
                response = requests.get(
                    url,
                    params=params,
                    headers={
                        "User-Agent": BANDCAMP_USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                        "Connection": "close",
                    },
                    timeout=(8, 25),
                )
                LAST_BANDCAMP_REQUEST = time.monotonic()
            body_folded = response.text.casefold()
            challenged = (
                "client challenge" in body_folded
                or "javascript is disabled" in body_folded
                or "enable javascript" in body_folded
            )
            LOGGER.info(
                "Bandcamp response status=%s final_url=%s bytes=%s challenged=%s",
                response.status_code,
                response.url,
                len(response.content),
                challenged,
            )
            response.raise_for_status()
            return response
        except (
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ReadTimeout,
            requests.exceptions.HTTPError,
        ) as exc:
            last_error = exc
            LOGGER.warning(
                "Bandcamp request failed attempt=%s/%s url=%s error=%r",
                attempt + 1,
                max(1, attempts),
                url,
                exc,
            )
            if attempt + 1 < attempts:
                time.sleep(0.5 * (2 ** attempt))

    raise requests.RequestException(
        f"Recherche Bandcamp impossible après {attempts} tentatives : {last_error}"
    ) from last_error


class BandcampSearchParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.results: list[dict[str, str]] = []
        self.current: dict[str, Any] | None = None
        self.depth = 0
        self.stack: list[set[str]] = []

    @staticmethod
    def classes(attrs: dict[str, str]) -> set[str]:
        return {value for value in attrs.get("class", "").split() if value}

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        classes = self.classes(attrs)
        is_result = (
            (tag == "li" and "searchresult" in classes)
            or (tag == "article" and "searchresult" in classes)
            or "search-result" in classes
        )
        if self.current is None and is_result:
            self.current = {
                "url": "",
                "title_parts": [],
                "artist_parts": [],
                "location_parts": [],
                "item_type_parts": [],
                "image_url": "",
            }
            self.depth = 1
            self.stack = [set()]

            # Some Bandcamp variants expose useful metadata directly on the
            # result container. Keep this deliberately permissive because the
            # exact attribute names have changed over time.
            for raw_value in attrs.values():
                if not raw_value:
                    continue
                decoded = html_unescape(raw_value).replace("\\/", "/")
                for match in re.finditer(
                    r"https?://[^\s\"'<>]+\.bandcamp\.com/(?:album|track)/[^\s\"'<>]+",
                    decoded,
                    flags=re.IGNORECASE,
                ):
                    absolute = canonical_bandcamp_release_url(match.group(0))
                    if is_bandcamp_release_url(absolute):
                        self.current["url"] = absolute
                        break
            return
        if self.current is None:
            return

        # Void HTML elements such as <img> do not have a closing tag. They
        # must not increment the result-card depth, otherwise a real result
        # card never reaches depth zero and is silently discarded.
        void_tag = tag in {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }
        keys: set[str] = set()
        if "heading" in classes:
            keys.add("title")
        if "subhead" in classes:
            keys.add("artist")
        if "location" in classes:
            keys.add("location")
        if "itemtype" in classes:
            keys.add("item_type")
        if not void_tag:
            self.depth += 1
            self.stack.append(keys)

        href = attrs.get("href", "").strip()
        if tag == "a" and href:
            absolute = urljoin(self.base_url, html_unescape(href))
            if is_bandcamp_release_url(absolute) and not self.current["url"]:
                self.current["url"] = canonical_bandcamp_release_url(absolute)

        if tag == "img" and not self.current["image_url"]:
            source = attrs.get("data-original") or attrs.get("data-src") or attrs.get("src") or ""
            if source:
                self.current["image_url"] = urljoin(
                    self.base_url,
                    html_unescape(source.strip()),
                )

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        """Handle XHTML-style void tags without closing the result card.

        ``HTMLParser`` normally calls ``handle_starttag`` followed by
        ``handle_endtag`` for tags such as ``<img ... />``. Since an image is
        a void element and does not increase our nesting depth, that default
        behavior used to close a result card too early.
        """
        self.handle_starttag(tag, attrs_list)

    def handle_data(self, data: str) -> None:
        if self.current is None or not data.strip():
            return
        active: set[str] = set()
        for keys in self.stack:
            active.update(keys)
        for key in active:
            self.current[f"{key}_parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if tag in {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }:
            return
        self.depth -= 1
        if self.stack:
            self.stack.pop()
        if self.depth > 0:
            return

        item_type = compact_spaces(" ".join(self.current["item_type_parts"])).casefold()
        if item_type and not any(value in item_type for value in ("album", "track", "single")):
            self.current = None
            self.stack = []
            return
        page_url = str(self.current["url"] or "")
        if page_url:
            artist = compact_spaces(" ".join(self.current["artist_parts"]))
            artist = re.sub(r"^by\s+", "", artist, flags=re.IGNORECASE)
            self.results.append({
                "url": page_url,
                "title": compact_spaces(" ".join(self.current["title_parts"])),
                "artist": artist,
                "image_url": str(self.current["image_url"] or ""),
                "location": compact_spaces(" ".join(self.current["location_parts"])),
            })
        self.current = None
        self.stack = []


class BandcampReleaseParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.first_art_url = ""

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key.casefold(): value or "" for key, value in attrs_list}
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").casefold()
            content = attrs.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "img" and not self.first_art_url:
            source = attrs.get("data-original") or attrs.get("data-src") or attrs.get("src") or ""
            if "bcbits.com/img/" in source:
                self.first_art_url = source.strip()


def fallback_bandcamp_release_links(html_text: str, base_url: str) -> list[dict[str, str]]:
    """Find release links if Bandcamp changes its result-card class names."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    normalized_html = (
        html_unescape(html_text)
        .replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u003A", ":")
        .replace("\\u003a", ":")
    )
    patterns = (
        r'href\s*=\s*["\']([^"\']+)["\']',
        r'["\'](?:item_url|itemUrl|url)["\']\s*:\s*["\']([^"\']+)["\']',
        r'(https?://[^\s"\'<>]+\.bandcamp\.com/(?:album|track)/[^\s"\'<>]+)',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, normalized_html, flags=re.IGNORECASE):
            raw_url = html_unescape(match.group(1)).replace("\\/", "/")
            absolute = urljoin(base_url, raw_url)
            if not is_bandcamp_release_url(absolute):
                continue
            page_url = canonical_bandcamp_release_url(absolute)
            if page_url in seen:
                continue
            found.append({
                "url": page_url,
                "title": "",
                "artist": "",
                "image_url": "",
                "location": "",
            })
            seen.add(page_url)
    return found


def parse_bandcamp_search_html(html_text: str, base_url: str) -> list[dict[str, str]]:
    """Parse album and track result cards from Bandcamp's public search page."""
    parser = BandcampSearchParser(base_url)
    parser.feed(html_text)
    parser.close()
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    parsed_items = [*parser.results, *fallback_bandcamp_release_links(html_text, base_url)]
    for item in parsed_items:
        page_url = canonical_bandcamp_release_url(item["url"])
        if page_url in seen:
            continue
        item["url"] = page_url
        results.append(item)
        seen.add(page_url)
    return results


def parse_bandcamp_release_html(html_text: str, page_url: str) -> dict[str, str]:
    """Extract cover metadata from one public Bandcamp release page."""
    parser = BandcampReleaseParser()
    parser.feed(html_text)
    parser.close()
    image_url = (
        parser.meta.get("og:image")
        or parser.meta.get("twitter:image")
        or parser.meta.get("image")
        or parser.first_art_url
    )
    raw_title = compact_spaces(
        parser.meta.get("og:title", "")
        or parser.meta.get("twitter:title", "")
        or parser.meta.get("title", "")
    )
    site_name = compact_spaces(parser.meta.get("og:site_name", ""))
    title = raw_title
    artist = "" if site_name.casefold() == "bandcamp" else site_name

    # Bandcamp release pages commonly expose ``Album, by Artist`` in
    # ``og:title``. Split it so metadata matching compares like with like.
    title_match = re.match(
        r"^(?P<title>.+?)\s*,\s*(?:by|par|de)\s+(?P<artist>.+?)$",
        raw_title,
        flags=re.IGNORECASE,
    )
    if title_match:
        title = compact_spaces(title_match.group("title"))
        if not artist:
            artist = compact_spaces(title_match.group("artist"))

    return {
        "url": page_url,
        "title": title,
        "artist": artist,
        "image_url": urljoin(page_url, image_url) if image_url else "",
        "location": "",
    }


def bandcamp_slug_variants(value: str, *, join_words: bool = False) -> list[str]:
    """Return a few conservative Bandcamp-style slug candidates."""
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold().replace("&", " and ")
    tokens = re.findall(r"[a-z0-9]+", normalized)
    if not tokens:
        return []

    values: list[str] = []
    for candidate in ("-".join(tokens), "".join(tokens) if join_words else ""):
        candidate = candidate.strip("-")
        if candidate and candidate not in values:
            values.append(candidate)
    return values


def bandcamp_direct_release_urls(artist: str, album: str) -> list[str]:
    """Guess common Bandcamp release URLs without relying on search HTML.

    Bandcamp's public search page can occasionally return an anti-bot or
    JavaScript challenge to non-browser clients even though the same query
    works in a normal browser. For straightforward artist and album slugs,
    probing the expected public release URL is both faster and more reliable.
    """
    artist_slugs = bandcamp_slug_variants(artist, join_words=True)
    album_slugs = bandcamp_slug_variants(album, join_words=True)
    artist_slugs.sort(key=lambda value: ("-" in value, len(value)))
    urls: list[str] = []
    for artist_slug in artist_slugs[:2]:
        for album_slug in album_slugs[:2]:
            url = f"https://{artist_slug}.bandcamp.com/album/{album_slug}"
            if url not in urls:
                urls.append(url)
    return urls


def bandcamp_direct_candidate(
    artist: str,
    album: str,
    *,
    min_size: int,
) -> dict[str, Any] | None:
    """Try likely Bandcamp URLs and keep only a matching public release."""
    guessed_urls = bandcamp_direct_release_urls(artist, album)
    LOGGER.info(
        "Bandcamp direct fallback artist=%r album=%r guessed_urls=%s",
        artist,
        album,
        guessed_urls,
    )
    for guessed_url in guessed_urls:
        try:
            response = bandcamp_get(guessed_url, attempts=1)
        except (requests.RequestException, ValueError) as exc:
            LOGGER.info("Bandcamp direct URL rejected url=%s error=%r", guessed_url, exc)
            continue

        item = parse_bandcamp_release_html(response.text, response.url)
        title_score = metadata_similarity(str(item.get("title") or ""), album)
        artist_score = metadata_similarity(str(item.get("artist") or ""), artist)
        LOGGER.info(
            "Bandcamp direct parsed requested_url=%s final_url=%s title=%r artist=%r image=%s title_score=%.3f artist_score=%.3f",
            guessed_url,
            response.url,
            item.get("title"),
            item.get("artist"),
            bool(item.get("image_url")),
            title_score,
            artist_score,
        )
        if not item.get("image_url"):
            continue
        if not bandcamp_result_matches(item, artist, album):
            LOGGER.info("Bandcamp direct metadata mismatch url=%s", response.url)
            continue

        candidate = bandcamp_candidate_from_result(
            item,
            fallback_artist=artist,
            fallback_album=album,
            min_size=min_size,
        )
        if candidate:
            candidate["source_type"] = "URL Bandcamp déduite"
            candidate["comment"] = (
                "Page Bandcamp trouvée directement lorsque la recherche HTML "
                "n'était pas exploitable"
            )
            LOGGER.info(
                "Bandcamp direct candidate accepted page=%s image=%s",
                candidate.get("source_url"),
                candidate.get("download_url"),
            )
            return candidate
    return None


def bandcamp_candidate_from_result(
    item: dict[str, str],
    *,
    fallback_artist: str,
    fallback_album: str,
    min_size: int,
) -> dict[str, Any] | None:
    image_url = str(item.get("image_url") or "").strip()
    if not image_url:
        return None
    preview = image_url.replace("http://", "https://")
    original = bandcamp_art_variant(preview, 0)
    download = original if min_size > 1200 else bandcamp_art_variant(preview, 10)
    page_url = str(item.get("url") or "").strip()
    title = str(item.get("title") or fallback_album).strip()
    artist = str(item.get("artist") or fallback_artist).strip()
    return {
        "id": hashlib.sha1(f"bandcamp|{page_url}|{download}".encode("utf-8")).hexdigest()[:16],
        "source": "Bandcamp",
        "source_type": "résultat Bandcamp",
        "mbid": "",
        "title": title,
        "artist": artist,
        "date": "",
        "country": str(item.get("location") or ""),
        "format": "",
        "score": None,
        "preview_url": preview,
        "download_url": download,
        "original_url": original,
        "musicbrainz_url": page_url,
        "source_url": page_url,
        "source_link_label": "Bandcamp",
        "comment": "Recherche Bandcamp lancée manuellement",
    }


def bandcamp_search_candidates(
    artist: str,
    album: str,
    *,
    min_size: int,
    limit: int = 12,
) -> tuple[list[dict[str, Any]], str]:
    query = compact_spaces(f"{artist} {album}")
    if not query:
        return [], "https://bandcamp.com/search"
    params = {"q": query, "item_type": "a"}
    search_url = requests.Request(
        "GET",
        "https://bandcamp.com/search",
        params=params,
    ).prepare().url or "https://bandcamp.com/search"
    try:
        response = bandcamp_get(
            "https://bandcamp.com/search",
            params=params,
        )
        search_url = response.url
        folded = response.text.casefold()
        challenged = (
            "client challenge" in folded
            or "javascript is disabled" in folded
            or "enable javascript" in folded
        )
        if challenged:
            LOGGER.info(
                "Bandcamp search page is a JavaScript challenge; using direct fallback artist=%r album=%r",
                artist,
                album,
            )
            direct = bandcamp_direct_candidate(artist, album, min_size=min_size)
            return ([direct] if direct else []), search_url
        parsed = parse_bandcamp_search_html(response.text, search_url)
        LOGGER.info(
            "Bandcamp search parsed artist=%r album=%r result_count=%s",
            artist,
            album,
            len(parsed),
        )
    except requests.RequestException:
        direct = bandcamp_direct_candidate(
            artist,
            album,
            min_size=min_size,
        )
        if direct:
            return [direct], search_url
        raise
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parsed[: max(limit * 2, limit)]:
        if not item.get("image_url"):
            try:
                page = bandcamp_get(item["url"], attempts=2)
                item.update(parse_bandcamp_release_html(page.text, item["url"]))
            except (requests.RequestException, ValueError):
                continue
        candidate = bandcamp_candidate_from_result(
            item,
            fallback_artist=artist,
            fallback_album=album,
            min_size=min_size,
        )
        if candidate is None or candidate["download_url"] in seen:
            continue
        candidates.append(candidate)
        seen.add(candidate["download_url"])
        if len(candidates) >= limit:
            break

    if not candidates:
        direct = bandcamp_direct_candidate(
            artist,
            album,
            min_size=min_size,
        )
        if direct:
            candidates.append(direct)
    return candidates, search_url


def bandcamp_unique_fallback_candidate(
    artist: str,
    album: str,
    *,
    min_size: int,
) -> dict[str, Any] | None:
    """Return a Bandcamp candidate only for one unambiguous album result.

    This is intentionally conservative: it is used by the background search
    only when the regular providers returned no candidate. The image is still
    merely preselected for human review and is never written automatically.
    """
    query = compact_spaces(f"{artist} {album}")
    if not query:
        return None
    try:
        response = bandcamp_get(
            "https://bandcamp.com/search",
            params={"q": query, "item_type": "a"},
        )
        folded = response.text.casefold()
        challenged = (
            "client challenge" in folded
            or "javascript is disabled" in folded
            or "enable javascript" in folded
        )
        if challenged:
            LOGGER.info(
                "Bandcamp background search received JavaScript challenge; using direct fallback artist=%r album=%r",
                artist,
                album,
            )
            return bandcamp_direct_candidate(artist, album, min_size=min_size)
        parsed = parse_bandcamp_search_html(response.text, response.url)
    except requests.RequestException:
        return bandcamp_direct_candidate(
            artist,
            album,
            min_size=min_size,
        )
    album_results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in parsed:
        page_url = str(item.get("url") or "")
        if "/album/" not in urlparse(page_url).path or page_url in seen_urls:
            continue
        seen_urls.add(page_url)
        album_results.append(item)

    if len(album_results) != 1:
        return bandcamp_direct_candidate(
            artist,
            album,
            min_size=min_size,
        )

    item = album_results[0]
    if not item.get("image_url") or not item.get("title") or not item.get("artist"):
        page = bandcamp_get(item["url"], attempts=2)
        page_data = parse_bandcamp_release_html(page.text, page.url)
        for key, value in page_data.items():
            if value:
                item[key] = value

    if not bandcamp_result_matches(item, artist, album):
        return None

    candidate = bandcamp_candidate_from_result(
        item,
        fallback_artist=artist,
        fallback_album=album,
        min_size=min_size,
    )
    if candidate:
        candidate["source_type"] = "résultat unique, recours automatique"
        candidate["comment"] = (
            "Unique résultat d'album Bandcamp, présélectionné pour validation humaine"
        )
    return candidate


def resolve_bandcamp_page_candidate(
    url: str,
    *,
    min_size: int,
    fallback_artist: str = "",
    fallback_album: str = "",
) -> dict[str, Any]:
    if not is_bandcamp_release_url(url):
        raise ValueError("Cette URL n'est pas une page d'album ou de morceau Bandcamp.")
    response = bandcamp_get(url)
    item = parse_bandcamp_release_html(response.text, response.url)
    candidate = bandcamp_candidate_from_result(
        item,
        fallback_artist=fallback_artist,
        fallback_album=fallback_album,
        min_size=min_size,
    )
    if candidate is None:
        raise ValueError("Aucune pochette n'a été trouvée sur cette page Bandcamp.")
    return candidate

def fanart_candidates(
    release_group_id: str,
    *,
    api_key: str,
    client_key: str = "",
    title: str,
    artist: str,
    date: str = "",
    fmt: str = "",
    score: int | None = None,
    errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return fanart.tv album covers for one MusicBrainz release-group ID."""
    global LAST_FANART_REQUEST
    if not api_key or not release_group_id:
        return []

    try:
        with FANART_LOCK:
            wait = 0.3 - (time.monotonic() - LAST_FANART_REQUEST)
            if wait > 0:
                time.sleep(wait)
            params = {"api_key": api_key}
            if client_key:
                params["client_key"] = client_key
            response = requests.get(
                f"https://webservice.fanart.tv/v3.2/music/albums/{release_group_id}",
                params=params,
                headers={
                    "User-Agent": f"CoverReview/{APP_VERSION}",
                    "Accept": "application/json",
                    "Connection": "close",
                },
                timeout=(6, 20),
            )
            LAST_FANART_REQUEST = time.monotonic()
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        if errors is not None:
            errors.append(f"fanart.tv {release_group_id} : {exc}")
        return []

    albums = payload.get("albums") or []
    album_data: dict[str, Any] | None = None
    if isinstance(albums, list):
        album_data = next(
            (
                item for item in albums
                if str(item.get("release_group_id") or "") == release_group_id
            ),
            albums[0] if albums else None,
        )
    elif isinstance(albums, dict):
        album_data = albums.get(release_group_id)
        if album_data is None and albums:
            album_data = next(iter(albums.values()))
    if not isinstance(album_data, dict):
        return []

    covers = album_data.get("albumcover") or []
    covers = sorted(
        (item for item in covers if item.get("url")),
        key=lambda item: int(item.get("likes") or 0),
        reverse=True,
    )
    result: list[dict[str, Any]] = []
    for item in covers:
        image_url = str(item.get("url") or "").replace("http://", "https://")
        if not image_url:
            continue
        likes = int(item.get("likes") or 0)
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        result.append({
            "id": hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:16],
            "source": "fanart.tv",
            "source_type": "groupe d'éditions",
            "mbid": release_group_id,
            "title": title,
            "artist": artist,
            "date": date,
            "country": "",
            "format": fmt,
            "score": score,
            "preview_url": image_url,
            "download_url": image_url,
            "original_url": image_url,
            "musicbrainz_url": f"https://musicbrainz.org/release-group/{release_group_id}",
            "source_url": f"https://musicbrainz.org/release-group/{release_group_id}",
            "source_link_label": "MusicBrainz",
            "comment": f"fanart.tv, {likes} vote(s)" + (f", {width} × {height} px" if width and height else ""),
            "width": width or None,
            "height": height or None,
        })
    return result

def find_candidates(
    artist: str,
    album: str,
    exact_release_id: str = "",
    exact_group_id: str = "",
    limit: int = 16,
    min_size: int = 1000,
    settings: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    settings = settings or get_settings()
    use_musicbrainz = settings.get("source_musicbrainz", "1") == "1"
    use_theaudiodb = settings.get("source_theaudiodb", "1") == "1"
    use_fanart = settings.get("source_fanart", "0") == "1"
    use_bandcamp = settings.get("source_bandcamp", "0") == "1"
    fanart_api_key = settings.get("fanart_api_key", "").strip()
    fanart_client_key = settings.get("fanart_client_key", "").strip()

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    network_errors: list[str] = []
    release_groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()

    def add(candidate: dict[str, Any] | None) -> None:
        if not candidate:
            return
        key = candidate["download_url"]
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    def remember_group(
        group_id: str,
        *,
        title: str = album,
        artist_name: str = artist,
        date: str = "",
        fmt: str = "",
        score: int | None = None,
    ) -> None:
        if not group_id or group_id in seen_groups:
            return
        seen_groups.add(group_id)
        release_groups.append({
            "id": group_id,
            "title": title,
            "artist": artist_name,
            "date": date,
            "format": fmt,
            "score": score,
        })

    if exact_group_id:
        remember_group(exact_group_id, score=100)

    if use_musicbrainz:
        if exact_release_id:
            add(candidate_from_caa(
                caa_json("release", exact_release_id, network_errors),
                source_type="édition exacte (tag)",
                mbid=exact_release_id,
                title=album,
                artist=artist,
                score=100,
                min_size=min_size,
            ))

        if exact_group_id:
            add(candidate_from_caa(
                caa_json("release-group", exact_group_id, network_errors),
                source_type="groupe exact (tag)",
                mbid=exact_group_id,
                title=album,
                artist=artist,
                score=100,
                min_size=min_size,
            ))

        groups: list[dict[str, Any]] = []
        seen_group_search: set[str] = set()
        variants = search_query_variants(artist, album)
        for variant_artist, variant_album in variants:
            if len(groups) >= min(12, limit):
                break
            query = f"releasegroup:{lucene_quote(variant_album)} AND artist:{lucene_quote(variant_artist)}"
            try:
                response = mb_get(
                    "release-group/",
                    {"query": query, "fmt": "json", "limit": min(12, limit)},
                )
                found = response.json().get("release-groups") or []
            except (requests.RequestException, ValueError) as exc:
                network_errors.append(f"recherche des groupes : {exc}")
                continue
            for group in found:
                group_id = str(group.get("id") or "")
                if group_id and group_id not in seen_group_search:
                    seen_group_search.add(group_id)
                    groups.append(group)

        for group in groups:
            group_id = str(group.get("id") or "")
            if not group_id:
                continue
            group_title = group.get("title") or album
            group_artist = artist_credit_text(group) or artist
            group_date = group.get("first-release-date") or ""
            group_format = group.get("primary-type") or ""
            group_score = int(group.get("score") or 0)
            remember_group(
                group_id,
                title=group_title,
                artist_name=group_artist,
                date=group_date,
                fmt=group_format,
                score=group_score,
            )
            if len(candidates) < limit:
                add(candidate_from_caa(
                    caa_json("release-group", group_id, network_errors),
                    source_type="groupe d'éditions",
                    mbid=group_id,
                    title=group_title,
                    artist=group_artist,
                    date=group_date,
                    fmt=group_format,
                    score=group_score,
                    min_size=min_size,
                ))

        releases: list[dict[str, Any]] = []
        seen_release_search: set[str] = set()
        for variant_artist, variant_album in variants:
            if releases:
                break
            release_query = f"release:{lucene_quote(variant_album)} AND artist:{lucene_quote(variant_artist)}"
            try:
                response = mb_get(
                    "release/",
                    {"query": release_query, "fmt": "json", "limit": min(60, max(25, limit * 3))},
                )
                found = response.json().get("releases") or []
            except (requests.RequestException, ValueError) as exc:
                network_errors.append(f"recherche des éditions : {exc}")
                continue
            for release in found:
                release_id = str(release.get("id") or "")
                if release_id and release_id not in seen_release_search:
                    seen_release_search.add(release_id)
                    releases.append(release)

        for release in releases:
            release_group = release.get("release-group") or {}
            group_id = str(release_group.get("id") or "")
            if group_id:
                remember_group(
                    group_id,
                    title=release_group.get("title") or release.get("title") or album,
                    artist_name=artist_credit_text(release) or artist,
                    date=release.get("date") or "",
                    fmt=release_group.get("primary-type") or "",
                    score=int(release.get("score") or 0),
                )
            if len(candidates) >= limit:
                continue
            caa_info = release.get("cover-art-archive") or {}
            if caa_info and not caa_info.get("front", False):
                continue
            release_id = str(release.get("id") or "")
            if not release_id:
                continue
            media = release.get("media") or []
            formats = sorted({str(medium.get("format")) for medium in media if medium.get("format")})
            add(candidate_from_caa(
                caa_json("release", release_id, network_errors),
                source_type="édition",
                mbid=release_id,
                title=release.get("title") or album,
                artist=artist_credit_text(release) or artist,
                date=release.get("date") or "",
                country=release.get("country") or "",
                fmt=", ".join(formats),
                score=int(release.get("score") or 0),
                min_size=min_size,
            ))

    if use_fanart and fanart_api_key and release_groups and len(candidates) < limit:
        # Prefer exact and best-scoring groups. Limit remote calls so background
        # searches stay practical on large libraries.
        ranked_groups = sorted(
            release_groups,
            key=lambda item: (item["id"] != exact_group_id, -(item.get("score") or 0)),
        )[:6]
        for group in ranked_groups:
            for candidate in fanart_candidates(
                group["id"],
                api_key=fanart_api_key,
                client_key=fanart_client_key,
                title=group["title"],
                artist=group["artist"],
                date=group["date"],
                fmt=group["format"],
                score=group["score"],
                errors=network_errors,
            ):
                add(candidate)
                if len(candidates) >= limit:
                    break
            if len(candidates) >= limit:
                break

    if use_theaudiodb and len(candidates) < min(3, limit):
        for variant_artist, variant_album in search_query_variants(artist, album):
            candidate = theaudiodb_candidate(variant_artist, variant_album, network_errors)
            add(candidate)
            if candidate:
                break

    # Bandcamp is an intentionally narrow fallback. It is queried only when
    # all regular sources returned no image, and only one matching album result
    # is accepted. The candidate remains subject to dimension checking and
    # explicit human validation in the batch or individual view.
    if use_bandcamp and not candidates:
        try:
            add(bandcamp_unique_fallback_candidate(
                artist,
                album,
                min_size=min_size,
            ))
        except (requests.RequestException, ValueError) as exc:
            network_errors.append(f"Bandcamp : {exc}")

    active_sources = (
        int(use_musicbrainz)
        + int(use_theaudiodb)
        + int(use_fanart and bool(fanart_api_key))
        + int(use_bandcamp)
    )
    if not candidates and network_errors and len(network_errors) >= max(1, active_sources):
        raise RuntimeError("Les sources de pochettes sont inaccessibles. " + " ; ".join(network_errors[:3]))
    return candidates[:limit], not network_errors


def cached_candidates(album_row: sqlite3.Row, artist: str, album: str, refresh: bool = False) -> tuple[list[dict[str, Any]], bool]:
    settings = get_settings()
    limit = max(4, min(30, int(settings.get("max_candidates", "16"))))
    min_size = max(1, int(settings.get("min_size", "1000")))
    cache_key = hashlib.sha1(
        json.dumps(
            {
                "search_cache_version": SEARCH_CACHE_VERSION,
                "artist": artist,
                "album": album,
                "release": album_row["mb_release_id"],
                "group": album_row["mb_release_group_id"],
                "limit": limit,
                "min_size": min_size,
                "source_musicbrainz": settings.get("source_musicbrainz", "1"),
                "source_theaudiodb": settings.get("source_theaudiodb", "1"),
                "source_fanart": settings.get("source_fanart", "0"),
                "source_bandcamp": settings.get("source_bandcamp", "0"),
                "fanart_key": hashlib.sha1(settings.get("fanart_api_key", "").encode("utf-8")).hexdigest()[:12],
                "fanart_client_key": hashlib.sha1(settings.get("fanart_client_key", "").encode("utf-8")).hexdigest()[:12],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    if not refresh:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT payload_json, created_at FROM search_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        if row and time.time() - row["created_at"] < 7 * 24 * 3600:
            return json.loads(row["payload_json"]), False

    result, complete = find_candidates(
        artist,
        album,
        exact_release_id=album_row["mb_release_id"],
        exact_group_id=album_row["mb_release_group_id"],
        limit=limit,
        min_size=min_size,
        settings=settings,
    )
    if complete:
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO search_cache(cache_key, payload_json, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json, created_at=excluded.created_at",
                (cache_key, json.dumps(result, ensure_ascii=False), time.time()),
            )
    return result, not complete


def background_config_key(settings: dict[str, str] | None = None) -> str:
    settings = settings or get_settings()
    payload = {
        "version": BATCH_RESULT_VERSION,
        "min_size": int(settings.get("min_size", "1000")),
        "max_candidates": int(settings.get("max_candidates", "16")),
        "batch_candidates": int(settings.get("batch_candidates", "4")),
        "source_musicbrainz": settings.get("source_musicbrainz", "1"),
        "source_theaudiodb": settings.get("source_theaudiodb", "1"),
        "source_fanart": settings.get("source_fanart", "0"),
        "source_bandcamp": settings.get("source_bandcamp", "0"),
        "fanart_key": hashlib.sha1(settings.get("fanart_api_key", "").encode("utf-8")).hexdigest()[:12],
        "fanart_client_key": hashlib.sha1(settings.get("fanart_client_key", "").encode("utf-8")).hexdigest()[:12],
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def remote_image_dimensions(url: str, attempts: int = 3) -> tuple[int, int]:
    """Read remote image dimensions without downloading the whole file when possible."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL d'image invalide.")

    retryable_errors = (
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ReadTimeout,
    )
    last_error: Exception | None = None

    for attempt in range(max(1, attempts)):
        try:
            with requests.get(
                url,
                stream=True,
                allow_redirects=True,
                headers={
                    "User-Agent": f"CoverReview/{APP_VERSION}",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Connection": "close",
                },
                timeout=(6, 20),
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                if content_type and not content_type.lower().startswith("image/"):
                    raise ValueError(f"Le serveur n'a pas renvoyé une image ({content_type}).")

                parser = ImageFile.Parser()
                total = 0
                for chunk in response.iter_content(chunk_size=32 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("L'image dépasse 30 Mo.")
                    parser.feed(chunk)
                    if parser.image is not None:
                        width, height = parser.image.size
                        if width and height:
                            return int(width), int(height)
                image = parser.close()
                return int(image.width), int(image.height)
        except retryable_errors as exc:
            last_error = exc
        except (requests.RequestException, OSError, ValueError) as exc:
            last_error = exc

        if attempt + 1 < attempts:
            time.sleep(0.4 * (2 ** attempt))

    raise requests.RequestException(
        f"Dimensions impossibles à lire après {attempts} tentatives : {last_error}"
    ) from last_error


def validate_candidates_for_batch(
    candidates: list[dict[str, Any]],
    min_size: int,
    wanted: int,
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for candidate in candidates:
        urls: list[str] = []
        for key in ("download_url", "original_url"):
            value = str(candidate.get(key) or "").strip()
            if value and value not in urls:
                urls.append(value)

        chosen_url = ""
        width = height = 0
        for image_url in urls:
            try:
                candidate_width, candidate_height = remote_image_dimensions(image_url)
            except Exception:
                continue
            if candidate_width >= min_size and candidate_height >= min_size:
                chosen_url = image_url
                width, height = candidate_width, candidate_height
                break

        if not chosen_url:
            continue

        item = dict(candidate)
        item["download_url"] = chosen_url
        item["width"] = width
        item["height"] = height
        item["eligible"] = True
        validated.append(item)
        if len(validated) >= wanted:
            break
    return validated


def search_result_counts() -> dict[str, int]:
    counts = {"not_started": 0, "queued": 0, "searching": 0, "ready": 0, "empty": 0, "error": 0}
    current_key = background_config_key()
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT r.status, r.config_key
            FROM album_search_results r
            JOIN albums a ON a.id=r.album_id
            WHERE a.status='pending'
            """
        ).fetchall()
        pending_total = conn.execute("SELECT COUNT(*) FROM albums WHERE status='pending'").fetchone()[0]
    represented = 0
    for row in rows:
        represented += 1
        if row["config_key"] != current_key:
            counts["not_started"] += 1
        else:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
    counts["not_started"] += max(0, pending_total - represented)
    counts["pending_total"] = pending_total
    return counts


def prepare_background_queue(refresh: bool = False) -> list[str]:
    settings = get_settings()
    config_key = background_config_key(settings)
    now = utc_now()
    with db_connect() as conn:
        albums = conn.execute(
            "SELECT * FROM albums WHERE status='pending' ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE"
        ).fetchall()
        queued: list[str] = []
        for album_row in albums:
            existing = conn.execute(
                "SELECT * FROM album_search_results WHERE album_id=?",
                (album_row["id"],),
            ).fetchone()
            reusable = (
                existing
                and existing["config_key"] == config_key
                and existing["query_artist"] == album_row["artist"]
                and existing["query_album"] == album_row["album"]
                and existing["status"] in {"ready", "empty"}
            )
            if reusable and not refresh:
                continue
            conn.execute(
                """
                INSERT INTO album_search_results(
                    album_id, config_key, status, query_artist, query_album,
                    candidates_json, selected_index, checked, error, started_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, '[]', 0, 1, NULL, NULL, ?)
                ON CONFLICT(album_id) DO UPDATE SET
                    config_key=excluded.config_key,
                    status='queued',
                    query_artist=excluded.query_artist,
                    query_album=excluded.query_album,
                    candidates_json='[]',
                    selected_index=0,
                    checked=1,
                    error=NULL,
                    started_at=NULL,
                    updated_at=excluded.updated_at
                """,
                (album_row["id"], config_key, album_row["artist"], album_row["album"], now),
            )
            queued.append(album_row["id"])
    return queued


def background_search_worker(album_ids: list[str]) -> None:
    settings = get_settings()
    min_size = max(1, int(settings.get("min_size", "1000")))
    wanted = max(1, min(8, int(settings.get("batch_candidates", "4"))))

    try:
        for album_id in album_ids:
            if BACKGROUND_SEARCH_STOP.is_set():
                break
            try:
                album_row = get_album_or_404(album_id)
            except KeyError:
                continue
            label = f"{album_row['artist']} : {album_row['album']}"
            with BACKGROUND_SEARCH_LOCK:
                BACKGROUND_SEARCH_STATE["current"] = label
            with db_connect() as conn:
                conn.execute(
                    "UPDATE album_search_results SET status='searching', started_at=?, updated_at=? WHERE album_id=?",
                    (utc_now(), utc_now(), album_id),
                )

            outcome = "error"
            try:
                raw, _partial = cached_candidates(
                    album_row,
                    album_row["artist"],
                    album_row["album"],
                    refresh=False,
                )
                validated = validate_candidates_for_batch(raw, min_size, wanted)
                outcome = "ready" if validated else "empty"
                with db_connect() as conn:
                    conn.execute(
                        """
                        UPDATE album_search_results SET status=?, candidates_json=?, selected_index=0,
                            checked=?, error=NULL, updated_at=? WHERE album_id=?
                        """,
                        (
                            outcome,
                            json.dumps(validated, ensure_ascii=False),
                            1 if validated else 0,
                            utc_now(),
                            album_id,
                        ),
                    )
            except Exception as exc:
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE album_search_results SET status='error', error=?, checked=0, updated_at=? WHERE album_id=?",
                        (str(exc), utc_now(), album_id),
                    )

            with BACKGROUND_SEARCH_LOCK:
                BACKGROUND_SEARCH_STATE["processed"] += 1
                if outcome == "ready":
                    BACKGROUND_SEARCH_STATE["ready"] += 1
                elif outcome == "empty":
                    BACKGROUND_SEARCH_STATE["empty"] += 1
                else:
                    BACKGROUND_SEARCH_STATE["errors"] += 1
    finally:
        stopped = BACKGROUND_SEARCH_STOP.is_set()
        if stopped:
            with db_connect() as conn:
                conn.execute(
                    "UPDATE album_search_results SET status='not_started', error=NULL, updated_at=? WHERE status='queued'",
                    (utc_now(),),
                )
        with BACKGROUND_SEARCH_LOCK:
            BACKGROUND_SEARCH_STATE["running"] = False
            BACKGROUND_SEARCH_STATE["stop_requested"] = stopped
            BACKGROUND_SEARCH_STATE["current"] = ""
            BACKGROUND_SEARCH_STATE["finished_at"] = utc_now()
        BACKGROUND_SEARCH_STOP.clear()


def start_background_search(refresh: bool = False) -> int:
    with BACKGROUND_SEARCH_LOCK:
        if BACKGROUND_SEARCH_STATE["running"]:
            raise RuntimeError("Une recherche en arrière-plan est déjà en cours.")
    album_ids = prepare_background_queue(refresh=refresh)
    with BACKGROUND_SEARCH_LOCK:
        BACKGROUND_SEARCH_STATE.update({
            "running": bool(album_ids),
            "stop_requested": False,
            "total": len(album_ids),
            "processed": 0,
            "ready": 0,
            "empty": 0,
            "errors": 0,
            "current": "",
            "started_at": utc_now() if album_ids else None,
            "finished_at": None if album_ids else utc_now(),
        })
    if album_ids:
        BACKGROUND_SEARCH_STOP.clear()
        threading.Thread(target=background_search_worker, args=(album_ids,), daemon=True).start()
    return len(album_ids)


def batch_item_from_rows(album_row: sqlite3.Row, result_row: sqlite3.Row) -> dict[str, Any]:
    album = row_to_album(album_row)
    candidates = json.loads(result_row["candidates_json"] or "[]")
    selected_index = int(result_row["selected_index"] or 0)
    if candidates:
        selected_index = max(0, min(selected_index, len(candidates) - 1))
    else:
        selected_index = 0
    return {
        "album": album,
        "candidates": candidates,
        "selected_index": selected_index,
        "checked": bool(result_row["checked"]),
        "error": result_row["error"] or "",
        "updated_at": result_row["updated_at"],
    }


def batch_apply_worker(album_ids: list[str]) -> None:
    try:
        for album_id in album_ids:
            with db_connect() as conn:
                row = conn.execute(
                    """
                    SELECT a.*, r.candidates_json, r.selected_index
                    FROM albums a JOIN album_search_results r ON r.album_id=a.id
                    WHERE a.id=? AND a.status='pending' AND r.status='ready'
                    """,
                    (album_id,),
                ).fetchone()
            if row is None:
                continue
            label = f"{row['artist']} : {row['album']}"
            with BATCH_APPLY_LOCK:
                BATCH_APPLY_STATE["current"] = label

            succeeded = False
            error_text = ""
            try:
                candidates = json.loads(row["candidates_json"] or "[]")
                index = max(0, min(int(row["selected_index"] or 0), len(candidates) - 1))
                candidate = candidates[index]
                urls: list[str] = []
                for key in ("download_url", "original_url"):
                    value = str(candidate.get(key) or "").strip()
                    if value and value not in urls:
                        urls.append(value)
                data = None
                selected_url = ""
                last_error: Exception | None = None
                for image_url in urls:
                    try:
                        data = safe_remote_image(image_url)
                        selected_url = image_url
                        break
                    except Exception as exc:
                        last_error = exc
                if data is None:
                    raise last_error or ValueError("Image indisponible.")
                backup_and_write(
                    row,
                    data,
                    f"{candidate.get('source', 'Source')} : {candidate.get('source_type', 'candidat')}",
                    selected_url,
                    allow_small=False,
                )
                succeeded = True
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE album_search_results SET status='applied', checked=0, error=NULL, updated_at=? WHERE album_id=?",
                        (utc_now(), album_id),
                    )
            except Exception as exc:
                error_text = str(exc)
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE album_search_results SET error=?, updated_at=? WHERE album_id=?",
                        (error_text, utc_now(), album_id),
                    )

            with BATCH_APPLY_LOCK:
                BATCH_APPLY_STATE["processed"] += 1
                if succeeded:
                    BATCH_APPLY_STATE["succeeded"] += 1
                else:
                    BATCH_APPLY_STATE["failed"] += 1
    finally:
        with BATCH_APPLY_LOCK:
            BATCH_APPLY_STATE["running"] = False
            BATCH_APPLY_STATE["current"] = ""
            BATCH_APPLY_STATE["finished_at"] = utc_now()


def start_batch_apply() -> int:
    with BATCH_APPLY_LOCK:
        if BATCH_APPLY_STATE["running"]:
            raise RuntimeError("Une validation en lot est déjà en cours.")
    with db_connect() as conn:
        album_ids = [
            row["album_id"]
            for row in conn.execute(
                """
                SELECT r.album_id
                FROM album_search_results r
                JOIN albums a ON a.id=r.album_id
                WHERE r.status='ready' AND r.checked=1 AND a.status='pending'
                ORDER BY a.artist COLLATE NOCASE, a.album COLLATE NOCASE
                """
            ).fetchall()
        ]
    with BATCH_APPLY_LOCK:
        BATCH_APPLY_STATE.update({
            "running": bool(album_ids),
            "total": len(album_ids),
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "current": "",
            "started_at": utc_now() if album_ids else None,
            "finished_at": None if album_ids else utc_now(),
        })
    if album_ids:
        threading.Thread(target=batch_apply_worker, args=(album_ids,), daemon=True).start()
    return len(album_ids)


def safe_remote_image(url: str, attempts: int = 4) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("L'URL doit commencer par http:// ou https://")

    retryable_errors = (
        requests.exceptions.SSLError,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ReadTimeout,
    )
    last_error: requests.RequestException | None = None

    for attempt in range(attempts):
        try:
            with requests.get(
                url,
                stream=True,
                allow_redirects=True,
                headers={
                    "User-Agent": f"CoverReview/{APP_VERSION}",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    # Cover Art Archive ferme parfois prématurément une connexion
                    # réutilisée. Une connexion neuve rend le téléchargement plus fiable.
                    "Connection": "close",
                },
                timeout=REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                if content_type and not content_type.lower().startswith("image/"):
                    raise ValueError(f"Le serveur n'a pas renvoyé une image ({content_type}).")

                buffer = io.BytesIO()
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("L'image dépasse 30 Mo.")
                    buffer.write(chunk)
                return buffer.getvalue()
        except retryable_errors as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(0.6 * (2 ** attempt))

    raise requests.RequestException(
        f"Téléchargement impossible après {attempts} tentatives : {last_error}"
    ) from last_error


def normalize_image(data: bytes) -> tuple[bytes, int, int]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=95, optimize=True, progressive=True)
            return output.getvalue(), int(width), int(height)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("Le fichier sélectionné n'est pas une image valide.") from exc


def make_embedded_image(data: bytes, max_size: int, quality: int) -> tuple[bytes, int, int]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            width, height = image.size
            output = io.BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            return output.getvalue(), int(width), int(height)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("Impossible de préparer la pochette intégrée.") from exc


def album_audio_files(album_row: sqlite3.Row) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    directories = [Path(path) for path in json.loads(album_row["directories_json"])]
    for directory in directories:
        try:
            candidates = sorted(
                path for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
            )
        except (OSError, PermissionError):
            continue
        for path in candidates:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)
    return files


def picture_to_json(picture: Picture) -> dict[str, Any]:
    return {
        "type": int(picture.type),
        "mime": picture.mime or "image/jpeg",
        "desc": picture.desc or "",
        "width": int(picture.width or 0),
        "height": int(picture.height or 0),
        "depth": int(picture.depth or 0),
        "colors": int(picture.colors or 0),
        "data": base64.b64encode(picture.data).decode("ascii"),
    }


def picture_from_json(payload: dict[str, Any]) -> Picture:
    picture = Picture()
    picture.type = int(payload.get("type", 3))
    picture.mime = str(payload.get("mime") or "image/jpeg")
    picture.desc = str(payload.get("desc") or "")
    picture.width = int(payload.get("width") or 0)
    picture.height = int(payload.get("height") or 0)
    picture.depth = int(payload.get("depth") or 0)
    picture.colors = int(payload.get("colors") or 0)
    picture.data = base64.b64decode(payload.get("data") or "")
    return picture


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def replace_embedded_cover(audio_path: Path, jpeg_data: bytes, width: int, height: int, backup_path: Path) -> bool:
    suffix = audio_path.suffix.lower()

    if suffix == ".mp3":
        try:
            tags = ID3(audio_path)
        except ID3NoHeaderError:
            tags = ID3()
        frames = tags.getall("APIC")
        old_front = [
            {
                "encoding": int(frame.encoding),
                "mime": frame.mime or "image/jpeg",
                "type": int(frame.type),
                "desc": frame.desc or "",
                "data": base64.b64encode(frame.data).decode("ascii"),
            }
            for frame in frames if int(frame.type) == 3
        ]
        write_json_atomic(backup_path, {"format": "mp3", "front": old_front})
        tags.delall("APIC")
        for frame in frames:
            if int(frame.type) != 3:
                tags.add(frame)
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=jpeg_data))
        tags.save(audio_path)
        return True

    if suffix == ".flac":
        audio = FLAC(audio_path)
        old_front = [picture_to_json(pic) for pic in audio.pictures if int(pic.type) == 3]
        write_json_atomic(backup_path, {"format": "flac", "front": old_front})
        non_front = [pic for pic in audio.pictures if int(pic.type) != 3]
        new_picture = Picture()
        new_picture.type = 3
        new_picture.mime = "image/jpeg"
        new_picture.desc = "Cover"
        new_picture.width = width
        new_picture.height = height
        new_picture.depth = 24
        new_picture.colors = 0
        new_picture.data = jpeg_data
        audio.clear_pictures()
        for picture in non_front:
            audio.add_picture(picture)
        audio.add_picture(new_picture)
        audio.save()
        return True

    if suffix in {".m4a", ".mp4"}:
        audio = MP4(audio_path)
        if audio.tags is None:
            audio.add_tags()
        old_covers = []
        for cover in (audio.tags or {}).get("covr", []):
            old_covers.append({
                "format": int(getattr(cover, "imageformat", MP4Cover.FORMAT_JPEG)),
                "data": base64.b64encode(bytes(cover)).decode("ascii"),
            })
        write_json_atomic(backup_path, {"format": "mp4", "covers": old_covers})
        audio.tags["covr"] = [MP4Cover(jpeg_data, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
        return True

    if suffix in {".ogg", ".oga", ".opus"}:
        audio = MutagenFile(audio_path, easy=False)
        if audio is None:
            raise ValueError(f"Format audio illisible : {audio_path.name}")
        if audio.tags is None:
            add_tags = getattr(audio, "add_tags", None)
            if not callable(add_tags):
                raise ValueError(f"Tags non pris en charge : {audio_path.name}")
            add_tags()
        tags = audio.tags
        original: dict[str, list[str]] = {}
        for key in ("metadata_block_picture", "coverart", "coverartmime"):
            values = tags.get(key, [])
            if isinstance(values, str):
                values = [values]
            original[key] = list(values)

        write_json_atomic(backup_path, {"format": "vorbis", "tags": original})
        preserved: list[str] = []
        for encoded in original["metadata_block_picture"]:
            try:
                picture = Picture(base64.b64decode(encoded))
                if int(picture.type) != 3:
                    preserved.append(encoded)
            except Exception:
                preserved.append(encoded)

        new_picture = Picture()
        new_picture.type = 3
        new_picture.mime = "image/jpeg"
        new_picture.desc = "Cover"
        new_picture.width = width
        new_picture.height = height
        new_picture.depth = 24
        new_picture.colors = 0
        new_picture.data = jpeg_data
        tags["metadata_block_picture"] = preserved + [
            base64.b64encode(new_picture.write()).decode("ascii")
        ]
        for key in ("coverart", "coverartmime"):
            try:
                del tags[key]
            except KeyError:
                pass
        audio.save()
        return True

    return False


def restore_embedded_cover(audio_path: Path, backup_path: Path) -> None:
    payload = json.loads(backup_path.read_text(encoding="utf-8"))
    format_name = payload.get("format")

    if format_name == "mp3":
        try:
            tags = ID3(audio_path)
        except ID3NoHeaderError:
            tags = ID3()
        frames = tags.getall("APIC")
        tags.delall("APIC")
        for frame in frames:
            if int(frame.type) != 3:
                tags.add(frame)
        for item in payload.get("front", []):
            tags.add(APIC(
                encoding=int(item.get("encoding", 3)),
                mime=str(item.get("mime") or "image/jpeg"),
                type=int(item.get("type", 3)),
                desc=str(item.get("desc") or ""),
                data=base64.b64decode(item.get("data") or ""),
            ))
        tags.save(audio_path)
        return

    if format_name == "flac":
        audio = FLAC(audio_path)
        non_front = [pic for pic in audio.pictures if int(pic.type) != 3]
        audio.clear_pictures()
        for picture in non_front:
            audio.add_picture(picture)
        for item in payload.get("front", []):
            audio.add_picture(picture_from_json(item))
        audio.save()
        return

    if format_name == "mp4":
        audio = MP4(audio_path)
        if audio.tags is None:
            audio.add_tags()
        covers = [
            MP4Cover(
                base64.b64decode(item.get("data") or ""),
                imageformat=int(item.get("format", MP4Cover.FORMAT_JPEG)),
            )
            for item in payload.get("covers", [])
        ]
        if covers:
            audio.tags["covr"] = covers
        else:
            audio.tags.pop("covr", None)
        audio.save()
        return

    if format_name == "vorbis":
        audio = MutagenFile(audio_path, easy=False)
        if audio is None:
            raise ValueError(f"Format audio illisible : {audio_path.name}")
        if audio.tags is None:
            add_tags = getattr(audio, "add_tags", None)
            if callable(add_tags):
                add_tags()
        tags = audio.tags
        original = payload.get("tags", {})
        for key in ("metadata_block_picture", "coverart", "coverartmime"):
            try:
                del tags[key]
            except KeyError:
                pass
            values = original.get(key, [])
            if values:
                tags[key] = values
        audio.save()
        return

    raise ValueError(f"Sauvegarde de tags inconnue : {format_name}")


def restore_embedded_manifest(entries: list[dict[str, str]], remove_backups: bool = False) -> None:
    for entry in reversed(entries):
        audio_path = Path(entry["audio"])
        backup_path = Path(entry["backup"])
        if audio_path.exists() and backup_path.exists():
            restore_embedded_cover(audio_path, backup_path)
        if remove_backups:
            backup_path.unlink(missing_ok=True)
    if remove_backups and entries:
        parent = Path(entries[0]["backup"]).parent
        try:
            parent.rmdir()
        except OSError:
            pass


def backup_and_write(
    album_row: sqlite3.Row,
    image_data: bytes,
    source: str,
    source_url: str = "",
    allow_small: bool = False,
) -> WriteResult:
    normalized, width, height = normalize_image(image_data)
    settings = get_settings()
    min_size = max(1, int(settings.get("min_size", "1000")))
    save_external = settings.get("save_external_cover", "1") == "1"
    embed_cover = settings.get("embed_cover", "0") == "1"
    embed_max_size = max(300, min(4000, int(settings.get("embed_max_size", "1000"))))
    embed_quality = max(50, min(95, int(settings.get("embed_quality", "88"))))

    if not save_external and not embed_cover:
        raise ValueError("Active au moins l'enregistrement de cover.jpg ou l'intégration dans les tags.")
    if not allow_small and (width < min_size or height < min_size):
        raise ValueError(
            f"Cette image fait {width} × {height} px, moins que le minimum configuré de {min_size} px."
        )

    directories = [Path(path) for path in json.loads(album_row["directories_json"])]
    album_root = Path(album_row["album_root"])
    targets: list[Path] = []
    for directory in [album_root, *directories]:
        resolved = directory.resolve()
        if resolved not in targets:
            targets.append(resolved)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    external_manifest: list[dict[str, Any]] = []
    embedded_manifest: list[dict[str, str]] = []
    created_files: list[Path] = []
    embedded_written = 0
    embedded_skipped = 0

    try:
        if save_external:
            for directory in targets:
                if not directory.is_dir():
                    continue
                backup_dir = directory / ".cover-review-backups"
                recognized = recognized_cover_files(directory)
                entries: list[dict[str, str]] = []
                if recognized:
                    backup_dir.mkdir(exist_ok=True)
                    for old_path in recognized:
                        backup_path = backup_dir / f"{timestamp}-{old_path.name}"
                        counter = 1
                        while backup_path.exists():
                            backup_path = backup_dir / f"{timestamp}-{counter}-{old_path.name}"
                            counter += 1
                        shutil.move(str(old_path), str(backup_path))
                        entries.append({"original": str(old_path), "backup": str(backup_path)})

                destination = directory / "cover.jpg"
                external_manifest.append({
                    "directory": str(directory),
                    "created": str(destination),
                    "backups": entries,
                })
                temp_path = directory / ".cover-review-cover.tmp"
                temp_path.write_bytes(normalized)
                os.replace(temp_path, destination)
                created_files.append(destination)

        if embed_cover:
            embedded_data, embedded_width, embedded_height = make_embedded_image(
                normalized, embed_max_size, embed_quality
            )
            audio_files = album_audio_files(album_row)
            embedded_backup_dir = (
                album_root / ".cover-review-backups" / f"{timestamp}-embedded"
            )
            for audio_path in audio_files:
                digest = hashlib.sha1(str(audio_path).encode("utf-8")).hexdigest()[:16]
                backup_path = embedded_backup_dir / f"{digest}.json"
                try:
                    supported = replace_embedded_cover(
                        audio_path,
                        embedded_data,
                        embedded_width,
                        embedded_height,
                        backup_path,
                    )
                except Exception:
                    if backup_path.exists():
                        try:
                            restore_embedded_cover(audio_path, backup_path)
                            backup_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                    raise
                if supported:
                    embedded_manifest.append({"audio": str(audio_path), "backup": str(backup_path)})
                    embedded_written += 1
                else:
                    embedded_skipped += 1

    except Exception:
        try:
            restore_embedded_manifest(embedded_manifest, remove_backups=True)
        except Exception:
            pass
        for created in created_files:
            created.unlink(missing_ok=True)
        for item in reversed(external_manifest):
            for entry in item["backups"]:
                backup = Path(entry["backup"])
                original = Path(entry["original"])
                if backup.exists():
                    shutil.move(str(backup), str(original))
        raise

    if embed_cover and embedded_written == 0 and embedded_skipped > 0 and not save_external:
        raise ValueError("Aucun format audio de cet album ne prend en charge l'intégration des pochettes.")

    main_cover: Path | None = None
    current_source = "embedded" if embedded_written else "missing"
    current_width = width
    current_height = height
    if save_external:
        candidate = album_root / "cover.jpg"
        if candidate.exists():
            main_cover = candidate
        elif created_files:
            main_cover = created_files[0]
        current_source = "external"
    elif embedded_written:
        cached = extract_embedded_cover(Path(album_row["audio_path"]), album_row["id"])
        main_cover = cached
        current_width, current_height = image_dimensions(cached) if cached else (None, None)

    manifest = {
        "version": 2,
        "external": external_manifest,
        "embedded": embedded_manifest,
    }

    with db_connect() as conn:
        conn.execute(
            """
            UPDATE albums SET
                current_path=?, current_source=?, current_width=?, current_height=?,
                status='approved', selected_source=?, selected_url=?, last_backup_json=?, updated_at=?
            WHERE id=?
            """,
            (
                str(main_cover) if main_cover else None,
                current_source,
                current_width,
                current_height,
                source,
                source_url,
                json.dumps(manifest, ensure_ascii=False),
                utc_now(),
                album_row["id"],
            ),
        )
    return WriteResult(
        width=width,
        height=height,
        embedded_written=embedded_written,
        embedded_skipped=embedded_skipped,
    )


def undo_album(album_row: sqlite3.Row) -> None:
    raw_manifest = album_row["last_backup_json"]
    if not raw_manifest:
        raise ValueError("Aucune validation à annuler pour cet album.")
    manifest = json.loads(raw_manifest)

    # Backward compatibility with manifests created before tag embedding existed.
    if isinstance(manifest, list):
        external_manifest = manifest
        embedded_manifest: list[dict[str, str]] = []
    else:
        external_manifest = manifest.get("external", [])
        embedded_manifest = manifest.get("embedded", [])

    restore_embedded_manifest(embedded_manifest, remove_backups=True)

    for item in reversed(external_manifest):
        created = Path(item["created"])
        created.unlink(missing_ok=True)
        for entry in item["backups"]:
            backup = Path(entry["backup"])
            original = Path(entry["original"])
            if backup.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup), str(original))

    refreshed = get_album_or_404(album_row["id"])
    audio_dirs = [Path(path) for path in json.loads(refreshed["directories_json"])]
    current_path, source, width, height = find_current_cover(
        Path(refreshed["album_root"]), audio_dirs, Path(refreshed["audio_path"]), refreshed["id"]
    )
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE albums SET current_path=?, current_source=?, current_width=?, current_height=?,
                status='pending', selected_source=NULL, selected_url=NULL,
                last_backup_json=NULL, updated_at=? WHERE id=?
            """,
            (
                str(current_path) if current_path else None,
                source,
                width,
                height,
                utc_now(),
                album_row["id"],
            ),
        )


@app.get("/")
def index() -> str:
    return render_template("index.html", app_version=APP_VERSION)


@app.get("/api/settings")
def api_get_settings() -> Response:
    return jsonify(get_settings())


@app.post("/api/settings")
def api_update_settings() -> Response:
    payload = request.get_json(silent=True) or {}
    root = str(payload.get("library_root", "")).strip()
    if root:
        path = Path(root).expanduser()
        if not path.exists():
            return jsonify({"error": "Ce chemin n'existe pas."}), 400
    try:
        min_size = int(payload.get("min_size", 1000))
        if min_size < 100 or min_size > 10000:
            raise ValueError
        max_candidates = int(payload.get("max_candidates", 16))
        if max_candidates < 4 or max_candidates > 30:
            raise ValueError
        batch_candidates = int(payload.get("batch_candidates", 4))
        if batch_candidates < 1 or batch_candidates > 8:
            raise ValueError
        embed_max_size = int(payload.get("embed_max_size", 1000))
        if embed_max_size < 300 or embed_max_size > 4000:
            raise ValueError
        embed_quality = int(payload.get("embed_quality", 88))
        if embed_quality < 50 or embed_quality > 95:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Valeur numérique invalide."}), 400

    values = {
        "library_root": root,
        "min_size": min_size,
        "include_missing": "1" if payload.get("include_missing", True) else "0",
        "max_candidates": max_candidates,
        "batch_candidates": batch_candidates,
        "save_external_cover": "1" if payload.get("save_external_cover", True) else "0",
        "embed_cover": "1" if payload.get("embed_cover", False) else "0",
        "embed_max_size": embed_max_size,
        "embed_quality": embed_quality,
        "source_musicbrainz": "1" if payload.get("source_musicbrainz", True) else "0",
        "source_theaudiodb": "1" if payload.get("source_theaudiodb", True) else "0",
        "source_fanart": "1" if payload.get("source_fanart", False) else "0",
        "source_bandcamp": "1" if payload.get("source_bandcamp", False) else "0",
        "fanart_api_key": str(payload.get("fanart_api_key", "")).strip(),
        "fanart_client_key": str(payload.get("fanart_client_key", "")).strip(),
    }
    if values["source_fanart"] == "1" and not values["fanart_api_key"]:
        return jsonify({"error": "La clé API de projet fanart.tv est requise pour activer cette source."}), 400
    if (
        values["source_musicbrainz"] == "0"
        and values["source_theaudiodb"] == "0"
        and values["source_fanart"] == "0"
        and values["source_bandcamp"] == "0"
    ):
        return jsonify({"error": "Active au moins une source de recherche."}), 400
    if values["save_external_cover"] == "0" and values["embed_cover"] == "0":
        return jsonify({"error": "Active au moins cover.jpg ou l'intégration dans les tags."}), 400
    return jsonify(update_settings(values))


@app.post("/api/scan")
def api_scan() -> Response:
    with SCAN_LOCK:
        if SCAN_STATE["running"]:
            return jsonify(SCAN_STATE), 409
    thread = threading.Thread(target=scan_library_worker, daemon=True)
    thread.start()
    return jsonify({"started": True})


@app.get("/api/scan/status")
def api_scan_status() -> Response:
    with SCAN_LOCK:
        return jsonify(dict(SCAN_STATE))


@app.get("/api/stats")
def api_stats() -> Response:
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM albums GROUP BY status"
        ).fetchall()
    counts = {"pending": 0, "approved": 0, "skipped": 0}
    for row in rows:
        counts[row["status"]] = row["count"]
    counts["total"] = sum(counts.values())
    search_counts = search_result_counts()
    counts["search"] = search_counts
    counts["batch_ready"] = search_counts.get("ready", 0)
    return jsonify(counts)


@app.get("/api/albums")
def api_albums() -> Response:
    status = request.args.get("status", "pending")
    search = request.args.get("q", "").strip()
    limit = min(max(int(request.args.get("limit", "100")), 1), 500)
    offset = max(int(request.args.get("offset", "0")), 0)

    clauses = []
    params: list[Any] = []
    if status != "all":
        clauses.append("status=?")
        params.append(status)
    if search:
        clauses.append("(artist LIKE ? OR album LIKE ? OR album_root LIKE ?)")
        pattern = f"%{search}%"
        params.extend([pattern, pattern, pattern])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    with db_connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM albums{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM albums{where} ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return jsonify({"total": total, "albums": [row_to_album(row) for row in rows]})


@app.get("/api/albums/<album_id>")
def api_album(album_id: str) -> Response:
    try:
        return jsonify(row_to_album(get_album_or_404(album_id)))
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404


@app.get("/api/albums/<album_id>/current")
def api_current_cover(album_id: str):
    try:
        row = get_album_or_404(album_id)
        path = Path(row["current_path"] or "")
        if not path.is_file():
            return Response(status=404)
        return send_file(path, conditional=True, max_age=31536000)
    except KeyError:
        return Response(status=404)


@app.get("/api/albums/<album_id>/candidates")
def api_candidates(album_id: str) -> Response:
    try:
        row = get_album_or_404(album_id)
        artist = request.args.get("artist", row["artist"]).strip()
        album = request.args.get("album", row["album"]).strip()
        refresh = request.args.get("refresh", "0") == "1"

        if not refresh and artist == row["artist"] and album == row["album"]:
            with db_connect() as conn:
                prefetched = conn.execute(
                    "SELECT * FROM album_search_results WHERE album_id=?",
                    (album_id,),
                ).fetchone()
            if (
                prefetched
                and prefetched["config_key"] == background_config_key()
                and prefetched["status"] in {"ready", "empty"}
            ):
                candidates = json.loads(prefetched["candidates_json"] or "[]")
                return jsonify({
                    "artist": artist,
                    "album": album,
                    "candidates": candidates,
                    "partial": False,
                    "prefetched": True,
                })

        candidates, partial = cached_candidates(row, artist, album, refresh=refresh)
        return jsonify({"artist": artist, "album": album, "candidates": candidates, "partial": partial, "prefetched": False})
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except requests.RequestException as exc:
        return jsonify({"error": f"Erreur réseau : {exc}"}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/albums/<album_id>/bandcamp-candidates")
def api_bandcamp_candidates(album_id: str) -> Response:
    try:
        row = get_album_or_404(album_id)
        artist = request.args.get("artist", row["artist"]).strip()
        album = request.args.get("album", row["album"]).strip()
        min_size = max(1, int(get_settings().get("min_size", "1000")))
        candidates, search_url = bandcamp_search_candidates(
            artist,
            album,
            min_size=min_size,
            limit=12,
        )
        return jsonify({
            "artist": artist,
            "album": album,
            "candidates": candidates,
            "search_url": search_url,
        })
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except (ValueError, requests.RequestException) as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/albums/<album_id>/approve")
def api_approve(album_id: str) -> Response:
    try:
        row = get_album_or_404(album_id)
        payload = request.get_json(silent=True) or {}
        url = str(payload.get("url", "")).strip()
        fallback_url = str(payload.get("fallback_url", "")).strip()
        source = str(payload.get("source", "Candidat sélectionné")).strip()
        allow_small = bool(payload.get("allow_small", False))
        if not url:
            raise ValueError("URL manquante.")

        source_url = str(payload.get("source_url", "")).strip()
        settings = get_settings()
        min_size = max(1, int(settings.get("min_size", "1000")))
        if is_bandcamp_release_url(url):
            bandcamp_candidate = resolve_bandcamp_page_candidate(
                url,
                min_size=min_size,
                fallback_artist=row["artist"],
                fallback_album=row["album"],
            )
            urls = [bandcamp_candidate["download_url"]]
            original_url = bandcamp_candidate.get("original_url") or ""
            if original_url and original_url not in urls:
                urls.append(original_url)
            source = "Bandcamp : page de sortie"
            source_url = url
        else:
            urls = [url]
            if fallback_url and fallback_url != url:
                urls.append(fallback_url)

        last_error: Exception | None = None
        data: bytes | None = None
        selected_url = url
        for candidate_url in urls:
            try:
                data = safe_remote_image(candidate_url)
                selected_url = candidate_url
                break
            except (requests.RequestException, ValueError) as exc:
                last_error = exc

        if data is None:
            if last_error is not None:
                raise last_error
            raise ValueError("Impossible de télécharger l'image.")

        result = backup_and_write(
            row, data, source, source_url or selected_url, allow_small=allow_small
        )
        return jsonify({
            "ok": True,
            "width": result.width,
            "height": result.height,
            "embedded_written": result.embedded_written,
            "embedded_skipped": result.embedded_skipped,
        })
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except (ValueError, requests.RequestException, OSError, MutagenError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/background-search/start")
def api_background_search_start() -> Response:
    try:
        payload = request.get_json(silent=True) or {}
        queued = start_background_search(refresh=bool(payload.get("refresh", False)))
        return jsonify({"ok": True, "queued": queued})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@app.post("/api/background-search/stop")
def api_background_search_stop() -> Response:
    with BACKGROUND_SEARCH_LOCK:
        if BACKGROUND_SEARCH_STATE["running"]:
            BACKGROUND_SEARCH_STATE["stop_requested"] = True
            BACKGROUND_SEARCH_STOP.set()
    return jsonify({"ok": True})


@app.get("/api/background-search/status")
def api_background_search_status() -> Response:
    with BACKGROUND_SEARCH_LOCK:
        payload = dict(BACKGROUND_SEARCH_STATE)
    payload["counts"] = search_result_counts()
    return jsonify(payload)


@app.get("/api/batch/items")
def api_batch_items() -> Response:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT a.*, r.config_key AS result_config_key, r.status AS result_status,
                r.candidates_json, r.selected_index, r.checked, r.error AS result_error,
                r.updated_at AS result_updated_at
            FROM albums a
            JOIN album_search_results r ON r.album_id=a.id
            WHERE a.status='pending' AND r.status='ready'
            ORDER BY a.artist COLLATE NOCASE, a.album COLLATE NOCASE
            """
        ).fetchall()
    current_key = background_config_key()
    items = []
    for row in rows:
        if row["result_config_key"] != current_key:
            continue
        result_row = {
            "candidates_json": row["candidates_json"],
            "selected_index": row["selected_index"],
            "checked": row["checked"],
            "error": row["result_error"],
            "updated_at": row["result_updated_at"],
        }
        items.append(batch_item_from_rows(row, result_row))
    return jsonify({"items": items, "total": len(items), "counts": search_result_counts()})


@app.post("/api/batch/items/<album_id>")
def api_batch_update_item(album_id: str) -> Response:
    try:
        get_album_or_404(album_id)
        payload = request.get_json(silent=True) or {}
        with db_connect() as conn:
            result = conn.execute(
                "SELECT candidates_json FROM album_search_results WHERE album_id=? AND status='ready'",
                (album_id,),
            ).fetchone()
            if result is None:
                raise ValueError("Ce résultat n'est plus disponible.")
            candidates = json.loads(result["candidates_json"] or "[]")
            selected_index = int(payload.get("selected_index", 0))
            if not candidates or selected_index < 0 or selected_index >= len(candidates):
                raise ValueError("Candidat invalide.")
            checked = 1 if payload.get("checked", True) else 0
            conn.execute(
                "UPDATE album_search_results SET selected_index=?, checked=?, error=NULL, updated_at=? WHERE album_id=?",
                (selected_index, checked, utc_now(), album_id),
            )
        return jsonify({"ok": True})
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/batch/check-all")
def api_batch_check_all() -> Response:
    payload = request.get_json(silent=True) or {}
    checked = 1 if payload.get("checked", True) else 0
    with db_connect() as conn:
        cursor = conn.execute(
            """
            UPDATE album_search_results SET checked=?, updated_at=?
            WHERE status='ready' AND album_id IN (SELECT id FROM albums WHERE status='pending')
            """,
            (checked, utc_now()),
        )
    return jsonify({"ok": True, "updated": cursor.rowcount})


@app.post("/api/batch/apply/start")
def api_batch_apply_start() -> Response:
    try:
        total = start_batch_apply()
        return jsonify({"ok": True, "total": total})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@app.get("/api/batch/apply/status")
def api_batch_apply_status() -> Response:
    with BATCH_APPLY_LOCK:
        return jsonify(dict(BATCH_APPLY_STATE))


@app.post("/api/albums/<album_id>/upload")
def api_upload(album_id: str) -> Response:
    try:
        row = get_album_or_404(album_id)
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            raise ValueError("Aucun fichier sélectionné.")
        data = uploaded.read(MAX_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ValueError("L'image dépasse 30 Mo.")
        allow_small = request.form.get("allow_small", "0") == "1"
        result = backup_and_write(
            row, data, f"Fichier local : {uploaded.filename}", "", allow_small=allow_small
        )
        return jsonify({
            "ok": True,
            "width": result.width,
            "height": result.height,
            "embedded_written": result.embedded_written,
            "embedded_skipped": result.embedded_skipped,
        })
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except (ValueError, OSError, MutagenError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/albums/<album_id>/skip")
def api_skip(album_id: str) -> Response:
    try:
        get_album_or_404(album_id)
        with db_connect() as conn:
            conn.execute(
                "UPDATE albums SET status='skipped', updated_at=? WHERE id=?",
                (utc_now(), album_id),
            )
        return jsonify({"ok": True})
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404


@app.post("/api/albums/<album_id>/pending")
def api_pending(album_id: str) -> Response:
    try:
        get_album_or_404(album_id)
        with db_connect() as conn:
            conn.execute(
                "UPDATE albums SET status='pending', updated_at=? WHERE id=?",
                (utc_now(), album_id),
            )
        return jsonify({"ok": True})
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404


@app.post("/api/albums/<album_id>/undo")
def api_undo(album_id: str) -> Response:
    try:
        row = get_album_or_404(album_id)
        undo_album(row)
        return jsonify({"ok": True})
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except (ValueError, OSError, MutagenError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "Le fichier dépasse 30 Mo."}), 413


def main() -> None:
    port = int(os.environ.get("COVER_REVIEW_PORT", "5000"))
    url = f"http://127.0.0.1:{port}"
    print(f"{APP_NAME} {APP_VERSION}")
    print(f"Interface : {url}")
    print(f"Journal : {LOG_PATH}")
    LOGGER.info("Starting %s %s", APP_NAME, APP_VERSION)
    if not os.environ.get("COVER_REVIEW_NO_BROWSER"):
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
