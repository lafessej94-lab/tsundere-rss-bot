#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import html
import time
import sqlite3
import asyncio
import logging
import subprocess
from pathlib import Path

from transferit import Transferit

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


# Intervalle entre deux vérifications RSS
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

# Nombre d'épisodes récents retraités au démarrage (0 = aucun)
STARTUP_COUNT = int(os.getenv("STARTUP_COUNT", "5"))

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "TON_TOKEN_ICI")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1000000000000")
CREATOR_ID = int(os.getenv("CREATOR_ID", "0"))

RSS_URL = os.getenv("RSS_URL", "")

DB_FILE = os.getenv("DB_FILE", "rss_bot.db")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))

# Marge volontaire.
MAX_FILE_SIZE = 1_950 * 1024 * 1024


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("tsundere_rss_bot")


# ============================================================
# BASE DE DONNÉES
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT UNIQUE,
            title TEXT,
            url TEXT,
            created_at INTEGER
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS permanent_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            link_key TEXT UNIQUE NOT NULL,
            transfer_url TEXT NOT NULL,
            title TEXT,
            created_at INTEGER
        )
        """
    )

    conn.commit()
    conn.close()


def already_processed(guid: str) -> bool:
    conn = sqlite3.connect(DB_FILE)

    row = conn.execute(
        "SELECT 1 FROM processed WHERE guid = ? LIMIT 1",
        (guid,),
    ).fetchone()

    conn.close()

    return row is not None


def mark_processed(guid: str, title: str, url: str):
    conn = sqlite3.connect(DB_FILE)

    conn.execute(
        """
        INSERT OR IGNORE INTO processed
        (guid, title, url, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (guid, title, url, int(time.time())),
    )

    conn.commit()
    conn.close()


# ============================================================
# UTILITAIRES
# ============================================================

def clean_html(text: str) -> str:
    if not text:
        return ""

    text = html.unescape(text)

    text = re.sub(
        r"<br\s*/?>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def extract_urls(text: str):
    if not text:
        return []

    text = html.unescape(text)

    urls = re.findall(
        r'https?://[^\s<>"\']+',
        text,
    )

    result = []

    for url in urls:
        url = url.rstrip(".,);]}>")

        if url not in result:
            result.append(url)

    return result


def is_video_url(url: str) -> bool:
    if not url:
        return False

    lower = url.lower()

    video_extensions = (
        ".mp4",
        ".mkv",
        ".webm",
        ".avi",
        ".mov",
        ".m4v",
        ".ts",
        ".m3u8",
        ".mpd",
    )

    if lower.split("?")[0].endswith(video_extensions):
        return True

    video_keywords = (
        ".m3u8?",
        "/video/",
        "/stream/",
        "/download/",
        "master.m3u8",
        "playlist.m3u8",
    )

    return any(x in lower for x in video_keywords)


# ============================================================
# EXTRACTION DE LA VIDÉO
# ============================================================

def extract_video_url(entry):
    """
    Détecte UNIQUEMENT Transfer.it.
    Aucune autre source n'est acceptée.
    """

    import html
    import re

    urls = []

    def add(value):
        if not value:
            return

        value = html.unescape(str(value)).strip()

        # Cherche directement un lien Transfer.it dans le texte
        found = re.findall(
            r'https?://(?:www\.)?transfer\.it/t/[A-Za-z0-9_-]+',
            value,
            re.IGNORECASE,
        )

        for url in found:
            if url not in urls:
                urls.append(url)

    def field(obj, name):
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    # link
    add(field(entry, "link"))

    # guid
    add(field(entry, "guid"))

    # id
    add(field(entry, "id"))

    # enclosures
    for enclosure in field(entry, "enclosures") or []:
        if isinstance(enclosure, dict):
            add(enclosure.get("href"))
            add(enclosure.get("url"))
        else:
            add(getattr(enclosure, "href", None))
            add(getattr(enclosure, "url", None))

    # media_content
    for media in field(entry, "media_content") or []:
        if isinstance(media, dict):
            add(media.get("url"))
            add(media.get("href"))
        else:
            add(getattr(media, "url", None))
            add(getattr(media, "href", None))

    # description / summary
    add(field(entry, "description"))
    add(field(entry, "summary"))

    # content
    for content in field(entry, "content") or []:
        if isinstance(content, dict):
            add(content.get("value"))

    if urls:
        logger.info(
            "🎯 Transfer.it détecté : %s",
            urls[0],
        )
        return urls[0]

    logger.info(
        "⏭️ Aucun Transfer.it trouvé."
    )

    return None



def is_hardsub_entry(entry):
    """
    Utilise le champ officiel Tsundere :
    tsundere_hardsub = true / false
    """

    value = None

    if isinstance(entry, dict):
        value = entry.get("tsundere_hardsub")
    else:
        value = getattr(entry, "tsundere_hardsub", None)

    return str(value).strip().lower() == "true"



def get_title(entry):
    title = entry.get("title", "").strip()

    if title:
        return clean_html(title)

    return "Anime sans titre"


# ============================================================
# CLÉ ÉPISODE
# ============================================================

def episode_key(title: str) -> str:

    value = str(title or "").lower()

    value = re.sub(
        r"\bhardsub\b",
        "",
        value,
    )

    value = re.sub(
        r"\b(720p|1080p|480p|2160p|4k)\b",
        "",
        value,
    )

    value = re.sub(
        r"\b(cr|web-dl|webrip|bluray|bdrip)\b",
        "",
        value,
    )

    value = re.sub(
        r"\baac\d?(?:\.\d+)?\b",
        "",
        value,
    )

    value = re.sub(
        r"\bx264\b",
        "",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value


# ============================================================
# PRIORITÉ SOURCE
# ============================================================

def source_priority(url: str) -> int:

    url = str(url or "").lower()

    if "transfer.it/t/" in url:
        return 1

    if "mega.nz/" in url or "mega.co.nz/" in url:
        return 2

    if "1fichier.com" in url:
        return 9

    if "nyaa.si/" in url:
        return 10

    if "nekobt.to/" in url:
        return 11

    return 5


# ============================================================
# TÉLÉCHARGEMENT
# ============================================================


# ============================================================
# TRANSFER.IT → SERVEURS MEGA → SERVEUR 200
# ============================================================

def _get_transferit_mega_candidates(transfer_url: str, attempts: int = 8):
    tx = Transferit()

    xh = tx._api.parse_xh(transfer_url)
    node_dicts, pw_token = tx._api.fetch_transfer(xh)

    logger.info(
        "📦 Transfer.it : %d élément(s) trouvé(s)",
        len(node_dicts),
    )

    file_node = next(
        (n for n in node_dicts if n.get("t") == 0),
        None,
    )

    if not file_node:
        raise RuntimeError(
            "❌ Aucun fichier trouvé dans Transfer.it"
        )

    filename = file_node.get("name") or "video.mkv"

    logger.info(
        "📄 Fichier : %s",
        filename,
    )

    candidates = []

    for i in range(attempts):
        try:
            dl = tx._api.get_download_url(
                xh,
                file_node["h"],
                pw_token=pw_token,
            )

            mega_url = dl.get("g")

            if mega_url and mega_url not in candidates:
                candidates.append(mega_url)

                logger.info(
                    "🌐 Serveur Mega %d trouvé",
                    len(candidates),
                )

        except Exception as e:
            logger.warning(
                "⚠️ Erreur serveur Mega %d : %s",
                i + 1,
                e,
            )

    return candidates, filename


def _test_mega_server(mega_url: str):
    try:
        result = subprocess.run(
            [
                "curl",
                "-L",
                "--fail",
                "--silent",
                "--show-error",
                "--range",
                "0-1048575",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code} %{size_download}",
                "--connect-timeout",
                "20",
                "--max-time",
                "60",
                mega_url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=70,
        )

        parts = result.stdout.strip().split()

        if len(parts) < 2:
            return False

        code = parts[0]

        try:
            received = int(float(parts[1]))
        except Exception:
            received = 0

        logger.info(
            "🧪 Serveur Mega | HTTP=%s | reçu=%d octets",
            code,
            received,
        )

        return code in ("200", "206") and received > 0

    except Exception as e:
        logger.warning(
            "❌ Test Mega échoué : %s",
            e,
        )
        return False


def resolve_working_mega(transfer_url: str, attempts: int = 8):

    candidates, filename = _get_transferit_mega_candidates(
        transfer_url,
        attempts,
    )

    if not candidates:
        raise RuntimeError(
            "❌ Aucun serveur Mega fourni par Transfer.it"
        )

    logger.info(
        "🔎 %d serveur(s) Mega à tester",
        len(candidates),
    )

    for index, mega_url in enumerate(candidates, 1):

        logger.info(
            "🧪 Test serveur Mega %d/%d",
            index,
            len(candidates),
        )

        if _test_mega_server(mega_url):

            logger.info(
                "✅ SERVEUR MEGA VALIDE : HTTP 200/206"
            )

            return mega_url, filename

    raise RuntimeError(
        "❌ Aucun serveur Mega ne répond correctement"
    )


def download_mega_server(mega_url: str, filename: str):

    DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = Path(filename).name

    if not filename:
        filename = "video.mkv"

    output = DOWNLOAD_DIR / filename
    part = DOWNLOAD_DIR / (filename + ".part")

    if part.exists():
        part.unlink()

    logger.info(
        "📥 Téléchargement depuis le serveur Mega validé..."
    )

    result = subprocess.run(
        [
            "curl",
            "-L",
            "--fail",
            "--retry",
            "3",
            "--retry-delay",
            "2",
            "--connect-timeout",
            "30",
            "--max-time",
            "7200",
            "-o",
            str(part),
            mega_url,
        ],
        timeout=7300,
    )

    if result.returncode != 0:
        if part.exists():
            part.unlink()

        raise RuntimeError(
            f"❌ Téléchargement Mega échoué : curl={result.returncode}"
        )

    if not part.exists() or part.stat().st_size <= 0:
        if part.exists():
            part.unlink()

        raise RuntimeError(
            "❌ Fichier Mega vide"
        )

    if output.exists():
        output.unlink()

    part.rename(output)

    logger.info(
        "✅ Téléchargement terminé : %.2f Mo",
        output.stat().st_size / (1024 * 1024),
    )

    return output



# ============================================================
# TRANSFER.IT → PLUSIEURS SERVEURS MEGA
# ============================================================


def resolve_transferit_final_url(transfer_url):
    """
    Résout un lien Transfer.it jusqu'à l'URL CDN finale
    sans télécharger le fichier.
    """
    import httpx
    from transferit import Transferit, TransferNode

    transfer_url = str(transfer_url).strip()

    logger.info("🔗 Résolution Transfer.it : %s", transfer_url)

    tx = Transferit()
    xh = tx._api.parse_xh(transfer_url)

    nodes_dict, pw_token = tx._api.fetch_transfer(xh)

    nodes = [TransferNode.from_dict(n) for n in nodes_dict]
    files = [n for n in nodes if n.is_file]

    if not files:
        raise RuntimeError("Aucun fichier trouvé dans Transfer.it.")

    node = files[0]

    filename = getattr(node, "name", None) or "video.mp4"
    handle = getattr(node, "handle", None)

    if not handle:
        raise RuntimeError("Handle Transfer.it/Mega introuvable.")

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": transfer_url,
        "Origin": "https://transfer.it",
    }

    params = {
        "x": xh,
        "n": handle,
        "fn": filename,
    }

    api_url = "https://bt7.api.mega.co.nz/cs/g"

    logger.info("🔎 Recherche de l'URL CDN finale...")

    # stream() permet d'obtenir les redirections sans lire/télécharger
    # le contenu du fichier.
    with httpx.stream(
        "GET",
        api_url,
        params=params,
        headers=headers,
        follow_redirects=True,
        timeout=30.0,
    ) as response:

        final_url = str(response.url)

    if not final_url:
        raise RuntimeError("URL finale vide.")

    logger.info("🎯 URL finale obtenue : %s", final_url)

    if "userstorage.mega.co.nz/" not in final_url.lower():
        raise RuntimeError(
            f"URL finale inattendue : {final_url}"
        )

    return final_url


def download_transferit(transfer_url):
    """
    Téléchargement Transfer.it avec tentative de 4 connexions
    HTTP Range simultanées.

    Si le serveur ne supporte pas Range/206, retour automatique
    au téléchargement classique à une seule connexion.
    """

    import math
    import shutil
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path

    import httpx
    from transferit import Transferit
    from transferit._models import TransferNode

    output_dir = (
        Path(__file__).resolve().parent
        / "downloads"
    )
    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    tx = Transferit()

    logger.info(
        "🔗 Analyse Transfer.it : %s",
        transfer_url,
    )

    xh = tx._api.parse_xh(
        transfer_url
    )

    logger.info(
        "🔑 XH Transfer.it : %s",
        xh,
    )

    nodes_dict, pw_token = tx._api.fetch_transfer(
        xh
    )

    nodes = [
        TransferNode.from_dict(n)
        for n in nodes_dict
    ]

    files = [
        n
        for n in nodes
        if n.is_file
    ]

    if not files:
        raise RuntimeError(
            "❌ Aucun fichier trouvé dans Transfer.it."
        )

    for node in files:

        filename = (
            node.name
            or node.handle
            or "video.mkv"
        )

        output_path = output_dir / filename
        size = int(node.size or 0)

        logger.info(
            "📥 Fichier Transfer.it : %s",
            filename,
        )

        logger.info(
            "📦 Taille attendue : %.2f Mo",
            size / 1024 / 1024,
        )

        if output_path.exists():
            try:
                output_path.unlink()
            except Exception:
                pass

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": str(transfer_url),
            "Origin": "https://transfer.it",
        }

        params = {
            "x": xh,
            "n": node.handle,
            "fn": filename,
        }

        # =====================================================
        # TEST DU SUPPORT HTTP RANGE
        # =====================================================

        range_supported = False

        if size > 1:

            try:

                test_headers = dict(headers)
                test_headers["Range"] = "bytes=0-0"

                logger.info(
                    "🧪 Test HTTP Range Transfer.it..."
                )

                with httpx.stream(
                    "GET",
                    "https://bt7.api.mega.co.nz/cs/g",
                    params=params,
                    headers=test_headers,
                    follow_redirects=True,
                    timeout=httpx.Timeout(
                        connect=30.0,
                        read=30.0,
                        write=30.0,
                        pool=30.0,
                    ),
                ) as resp:

                    logger.info(
                        "📡 Test Range : HTTP %s",
                        resp.status_code,
                    )

                    if resp.status_code == 206:

                        range_supported = True

                        logger.info(
                            "✅ HTTP Range supporté : "
                            "activation de 8 connexions."
                        )

                    else:

                        logger.warning(
                            "⚠️ HTTP Range non supporté "
                            "(HTTP %s).",
                            resp.status_code,
                        )

            except Exception as e:

                logger.warning(
                    "⚠️ Test HTTP Range échoué : %s",
                    e,
                )

        # =====================================================
        # TÉLÉCHARGEMENT PAR 8 MORCEAUX
        # =====================================================

        if range_supported:

            parts_dir = (
                output_dir
                / ".transferit_parts"
            )

            parts_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            part_paths = []

            progress_lock = threading.Lock()
            downloaded_total = 0
            last_bucket = -1

            part_count = 8
            chunk_size = math.ceil(
                size / part_count
            )

            ranges = []

            for index in range(part_count):

                start_byte = (
                    index * chunk_size
                )

                end_byte = min(
                    size - 1,
                    (
                        start_byte
                        + chunk_size
                        - 1
                    ),
                )

                if start_byte > end_byte:
                    continue

                ranges.append(
                    (
                        index,
                        start_byte,
                        end_byte,
                    )
                )

            logger.info(
                "🚀 Téléchargement parallèle : %d connexions",
                len(ranges),
            )

            def download_part(item):

                nonlocal downloaded_total
                nonlocal last_bucket

                index, start_byte, end_byte = item

                part_path = (
                    parts_dir
                    / (
                        f"{filename}.part"
                        f"{index}"
                    )
                )

                expected = (
                    end_byte
                    - start_byte
                    + 1
                )

                for attempt in range(1, 4):

                    try:

                        if transfer_cancel_event.is_set():

                            raise RuntimeError(
                                "DOWNLOAD_CANCELLED"
                            )

                        if part_path.exists():

                            try:
                                part_path.unlink()
                            except Exception:
                                pass

                        part_headers = dict(
                            headers
                        )

                        part_headers["Range"] = (
                            f"bytes={start_byte}-{end_byte}"
                        )

                        logger.info(
                            "🧩 Partie %d : "
                            "%d-%d | tentative %d/3",
                            index + 1,
                            start_byte,
                            end_byte,
                            attempt,
                        )

                        received = 0

                        with httpx.stream(
                            "GET",
                            "https://bt7.api.mega.co.nz/cs/g",
                            params=params,
                            headers=part_headers,
                            follow_redirects=True,
                            timeout=httpx.Timeout(
                                connect=30.0,
                                read=60.0,
                                write=60.0,
                                pool=60.0,
                            ),
                        ) as resp:

                            if resp.status_code != 206:

                                raise RuntimeError(
                                    "Serveur Range inattendu : "
                                    f"HTTP {resp.status_code}"
                                )

                            with part_path.open(
                                "wb"
                            ) as fh:

                                for chunk in resp.iter_bytes(
                                    1024 * 1024
                                ):

                                    if transfer_cancel_event.is_set():

                                        raise RuntimeError(
                                            "DOWNLOAD_CANCELLED"
                                        )

                                    if not chunk:
                                        continue

                                    fh.write(chunk)

                                    received += len(
                                        chunk
                                    )

                                    with progress_lock:

                                        downloaded_total += len(
                                            chunk
                                        )

                                        percent = (
                                            downloaded_total
                                            * 100
                                            / size
                                        )

                                        bucket = int(
                                            percent / 5
                                        )

                                        if bucket != last_bucket:

                                            last_bucket = bucket

                                            logger.info(
                                                "📥 %s : "
                                                "%.1f%% | "
                                                "%.2f / %.2f Mo",
                                                filename,
                                                percent,
                                                downloaded_total
                                                / 1024
                                                / 1024,
                                                size
                                                / 1024
                                                / 1024,
                                            )

                        if received != expected:

                            raise RuntimeError(
                                "Taille de partie incorrecte : "
                                f"{received} / {expected} octets"
                            )

                        logger.info(
                            "✅ Partie %d terminée : %.2f Mo",
                            index + 1,
                            received / 1024 / 1024,
                        )

                        return (
                            index,
                            part_path,
                        )

                    except Exception as e:

                        logger.warning(
                            "⚠️ Partie %d échouée "
                            "(tentative %d/3) : %s",
                            index + 1,
                            attempt,
                            e,
                        )

                        if (
                            str(e)
                            == "DOWNLOAD_CANCELLED"
                        ):
                            raise

                        try:
                            if part_path.exists():
                                part_path.unlink()
                        except Exception:
                            pass

                raise RuntimeError(
                    f"❌ Partie {index + 1} "
                    "impossible à télécharger."
                )

            try:

                completed_parts = {}

                with ThreadPoolExecutor(
                    max_workers=len(ranges)
                ) as executor:

                    futures = [
                        executor.submit(
                            download_part,
                            item,
                        )
                        for item in ranges
                    ]

                    for future in as_completed(
                        futures
                    ):

                        index, part_path = (
                            future.result()
                        )

                        completed_parts[
                            index
                        ] = part_path

                if transfer_cancel_event.is_set():

                    raise RuntimeError(
                        "DOWNLOAD_CANCELLED"
                    )

                # =============================================
                # ASSEMBLAGE DES 8 PARTIES
                # =============================================

                logger.info(
                    "🧩 Assemblage des %d parties...",
                    len(completed_parts),
                )

                with output_path.open(
                    "wb"
                ) as final_file:

                    for index, _, _ in ranges:

                        part_path = (
                            completed_parts[index]
                        )

                        with part_path.open(
                            "rb"
                        ) as part_file:

                            shutil.copyfileobj(
                                part_file,
                                final_file,
                                length=8 * 1024 * 1024,
                            )

                received_size = (
                    output_path.stat().st_size
                )

                if received_size != size:

                    raise RuntimeError(
                        "Taille finale incorrecte : "
                        f"{received_size} / {size} octets"
                    )

                logger.info(
                    "✅ Transfer.it 8 connexions terminé : "
                    "%s | %.2f Mo",
                    output_path,
                    received_size / 1024 / 1024,
                )

                # Nettoyage
                for part_path in completed_parts.values():

                    try:
                        part_path.unlink()
                    except Exception:
                        pass

                return output_path

            except Exception as e:

                logger.warning(
                    "⚠️ Téléchargement parallèle échoué : %s",
                    e,
                )

                try:
                    if output_path.exists():
                        output_path.unlink()
                except Exception:
                    pass

                try:
                    for part_path in parts_dir.glob(
                        f"{filename}.part*"
                    ):
                        part_path.unlink()
                except Exception:
                    pass

                if str(e) == "DOWNLOAD_CANCELLED":
                    raise

                logger.warning(
                    "🔄 Retour au téléchargement classique..."
                )

        # =====================================================
        # TÉLÉCHARGEMENT CLASSIQUE / FALLBACK
        # =====================================================

        for attempt in range(1, 9):

            logger.info(
                "🌐 Serveur Transfer.it %d/8...",
                attempt,
            )

            try:

                if output_path.exists():

                    try:
                        output_path.unlink()
                    except Exception:
                        pass

                logger.info(
                    "🔗 Appel direct Transfer.it /cs/g..."
                )

                with httpx.stream(
                    "GET",
                    "https://bt7.api.mega.co.nz/cs/g",
                    params=params,
                    headers=headers,
                    follow_redirects=True,
                    timeout=httpx.Timeout(
                        connect=30.0,
                        read=60.0,
                        write=60.0,
                        pool=60.0,
                    ),
                ) as resp:

                    logger.info(
                        "📡 HTTP %s",
                        resp.status_code,
                    )

                    logger.info(
                        "🌍 Serveur final : %s",
                        resp.url.host,
                    )

                    resp.raise_for_status()

                    content_length = (
                        resp.headers.get(
                            "content-length"
                        )
                    )

                    logger.info(
                        "📦 Taille HTTP : %s",
                        content_length or "inconnue",
                    )

                    written = 0
                    last_bucket = -1

                    with output_path.open(
                        "wb"
                    ) as fh:

                        for chunk in resp.iter_bytes(
                            8 * 1024 * 1024
                        ):

                            if transfer_cancel_event.is_set():

                                logger.warning(
                                    "🛑 Téléchargement "
                                    "Transfer.it annulé par /cancel."
                                )

                                raise RuntimeError(
                                    "DOWNLOAD_CANCELLED"
                                )

                            if not chunk:
                                continue

                            fh.write(chunk)
                            written += len(chunk)

                            if size:

                                percent = (
                                    written
                                    * 100
                                    / size
                                )

                                bucket = int(
                                    percent / 5
                                )

                                if bucket != last_bucket:

                                    last_bucket = bucket

                                    logger.info(
                                        "📥 %s : %.1f%% | "
                                        "%.2f / %.2f Mo",
                                        filename,
                                        percent,
                                        written
                                        / 1024
                                        / 1024,
                                        size
                                        / 1024
                                        / 1024,
                                    )

                    received_size = (
                        output_path.stat().st_size
                    )

                    if size and received_size != size:

                        raise RuntimeError(
                            "Taille reçue incorrecte : "
                            f"{received_size} / "
                            f"{size} octets."
                        )

                    logger.info(
                        "✅ Transfer.it téléchargé : %s | %.2f Mo",
                        output_path,
                        received_size / 1024 / 1024,
                    )

                    return output_path

            except Exception as e:

                if str(e) == "DOWNLOAD_CANCELLED":
                    raise

                logger.warning(
                    "⚠️ Transfer.it %d/8 échoué : %s",
                    attempt,
                    e,
                )

                try:
                    if output_path.exists():
                        output_path.unlink()
                except Exception:
                    pass

        raise RuntimeError(
            "❌ Échec du téléchargement Transfer.it "
            "après 8 tentatives."
        )


def download_video(video_url):
    """Télécharge une vidéo via aria2 RPC et valide réellement le fichier."""

    import os
    import time
    import subprocess
    from pathlib import Path
    from urllib.parse import urlparse
    from aria2p import API, Client

    workdir = (
        Path(os.path.dirname(os.path.abspath(__file__)))
        / "downloads"
    )
    workdir.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(video_url)

    name = Path(parsed.path).name or "video.mkv"
    name = name.split("?")[0].strip() or "video.mkv"

    output = workdir / name

    logger.info("📥 Téléchargement aria2 : %s", video_url)
    logger.info("📄 Destination : %s", output)

    # Nettoyage d'un ancien fichier portant le même nom
    for old_file in (
        output,
        Path(str(output) + ".aria2"),
        Path(str(output) + ".part"),
    ):
        try:
            if old_file.exists():
                old_file.unlink()
                logger.info(
                    "🧹 Ancien fichier supprimé : %s",
                    old_file,
                )
        except Exception as e:
            logger.warning(
                "⚠️ Impossible de supprimer %s : %s",
                old_file,
                e,
            )

    # Connexion aria2
    try:
        client = Client(
            host="http://localhost",
            port=6800,
            secret=os.getenv("ARIA2_SECRET", "rssaria2"),
        )

        api = API(client)

        version = client.get_version()["version"]

        logger.info(
            "🔌 aria2 RPC connecté : %s",
            version,
        )

    except Exception as e:
        raise RuntimeError(
            f"❌ Impossible de contacter aria2 RPC : {e}"
        )

    options = {
        "dir": str(workdir),
        "out": name,

        "file-allocation": "none",
        "continue": "true",
        "always-resume": "true",
        "allow-overwrite": "true",
        "auto-file-renaming": "false",

        "max-tries": "3",
        "retry-wait": "2",

        "timeout": "30",
        "connect-timeout": "15",

        "max-connection-per-server": "4",
        "split": "4",
        "min-split-size": "1M",

        "header": "User-Agent: Mozilla/5.0",
    }

    try:
        download = api.add_uris(
            [video_url],
            options,
        )

    except Exception as e:
        raise RuntimeError(
            f"❌ Impossible d'ajouter le téléchargement à aria2 : {e}"
        )

    if not download:
        raise RuntimeError(
            "❌ aria2 n'a retourné aucun téléchargement."
        )

    gid = download.gid

    logger.info(
        "🆔 GID aria2 : %s",
        gid,
    )

    start_time = time.time()
    last_log = 0

    while True:

        try:
            download = api.get_download(gid)

        except Exception as e:
            raise RuntimeError(
                f"❌ Erreur lors du suivi aria2 : {e}"
            )

        status = download.status
        now = time.time()

        if now - last_log >= 2:

            try:
                logger.info(
                    "📥 Téléchargement | %s | %s / %s | ⚡ %s | ⏱️ %s",
                    download.progress_string(),
                    download.completed_length_string(),
                    download.total_length_string(),
                    download.download_speed_string(),
                    download.eta_string(),
                )

            except Exception as e:
                logger.warning(
                    "⚠️ Progression aria2 indisponible : %s",
                    e,
                )

            last_log = now

        if status == "complete":
            break

        if status == "error":

            error_message = getattr(
                download,
                "error_message",
                None,
            )

            # Nettoyage du faux téléchargement
            for bad_file in (
                output,
                Path(str(output) + ".aria2"),
            ):
                try:
                    if bad_file.exists():
                        bad_file.unlink()
                        logger.info(
                            "🧹 Fichier aria2 supprimé après erreur : %s",
                            bad_file,
                        )
                except Exception:
                    pass

            raise RuntimeError(
                "❌ aria2 a échoué : "
                f"{error_message or 'erreur inconnue'}"
            )

        if status == "removed":

            for bad_file in (
                output,
                Path(str(output) + ".aria2"),
            ):
                try:
                    if bad_file.exists():
                        bad_file.unlink()
                except Exception:
                    pass

            raise RuntimeError(
                "❌ Téléchargement aria2 supprimé."
            )

        time.sleep(1)

    # ------------------------------------------------------------------
    # Vérification physique du fichier
    # ------------------------------------------------------------------

    if not output.exists():
        raise FileNotFoundError(
            f"❌ Fichier téléchargé introuvable : {output}"
        )

    size = output.stat().st_size

    logger.info(
        "📦 Fichier reçu : %.2f Mo",
        size / 1024 / 1024,
    )

    # Une vraie vidéo de cet usage ne fera évidemment pas 900 octets.
    if size < 1_000_000:

        logger.error(
            "❌ Fichier beaucoup trop petit : %d octets",
            size,
        )

        try:
            output.unlink()
        except Exception:
            pass

        aria2_file = Path(str(output) + ".aria2")

        try:
            if aria2_file.exists():
                aria2_file.unlink()
        except Exception:
            pass

        raise RuntimeError(
            "❌ Le serveur a renvoyé un fichier trop petit "
            "au lieu de la vidéo."
        )

    # ------------------------------------------------------------------
    # Vérification FFprobe
    # ------------------------------------------------------------------

    logger.info(
        "🔍 Vérification FFprobe du fichier téléchargé..."
    )

    try:

        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration,size",
                "-of",
                "default=noprint_wrappers=1",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

    except subprocess.TimeoutExpired:

        logger.error(
            "❌ FFprobe a dépassé 60 secondes."
        )

        try:
            output.unlink()
        except Exception:
            pass

        raise RuntimeError(
            "❌ Vérification vidéo expirée."
        )

    if probe.returncode != 0:

        logger.error(
            "❌ Fichier téléchargé invalide :\n%s",
            probe.stderr[-3000:] if probe.stderr else "aucune erreur",
        )

        # Affichage utile pour identifier HTML/JSON/etc.
        try:
            with open(
                output,
                "rb",
            ) as f:
                first_bytes = f.read(500)

            logger.error(
                "🔎 Début du fichier invalide : %r",
                first_bytes,
            )

        except Exception:
            pass

        try:
            output.unlink()
        except Exception:
            pass

        aria2_file = Path(str(output) + ".aria2")

        try:
            if aria2_file.exists():
                aria2_file.unlink()
        except Exception:
            pass

        raise RuntimeError(
            "❌ Le serveur a renvoyé un fichier qui n'est pas une vidéo."
        )

    # ------------------------------------------------------------------
    # Nettoyage aria2
    # ------------------------------------------------------------------

    aria2_file = Path(str(output) + ".aria2")

    try:
        if aria2_file.exists():
            aria2_file.unlink()
    except Exception:
        pass

    logger.info(
        "✅ Fichier vidéo valide :\n%s",
        probe.stdout.strip(),
    )

    logger.info(
        "✅ Téléchargement terminé : %s | %.2f Mo | %.0f s",
        output.name,
        size / 1024 / 1024,
        time.time() - start_time,
    )

    return output

def download_mega_with_retry(transfer_url, attempts=8, progress_callback=None):
    """Transfer.it -> Mega avec rotation des URLs et reprise automatique."""

    import os
    import time
    import subprocess
    from pathlib import Path
    from urllib.parse import urlparse
    from transferit import Transferit

    workdir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "mega-download",
    )
    os.makedirs(workdir, exist_ok=True)

    tx = Transferit()

    xh = tx._api.parse_xh(transfer_url)
    nodes, pw_token = tx._api.fetch_transfer(xh)

    file_node = next(
        (n for n in nodes if n.get("t") == 0),
        None,
    )

    if not file_node:
        raise RuntimeError("❌ Aucun fichier trouvé dans Transfer.it.")

    node_handle = file_node["h"]

    filename = os.path.basename(
        file_node.get("name") or "download"
    )

    output_path = os.path.join(workdir, filename)

    logger.info("📄 Fichier : %s", filename)
    logger.info("🔑 Node : %s", node_handle)

    last_error = None

    for retry in range(1, attempts + 1):

        log_file = os.path.join(
            workdir,
            f".aria2_{os.getpid()}_{retry}.log",
        )

        try:
            logger.info(
                "🔄 Tentative %s/%s : génération d'une nouvelle URL Mega",
                retry,
                attempts,
            )

            result = tx._api.get_download_url(
                xh,
                node_handle,
                pw_token=pw_token,
            )

            mega_url = (
                result.get("g")
                if isinstance(result, dict)
                else None
            )

            if not mega_url:
                raise RuntimeError("URL Mega invalide.")

            logger.info(
                "🌐 Serveur Mega : %s",
                urlparse(mega_url).netloc,
            )

            if os.path.exists(output_path):
                partial = os.path.getsize(output_path)
            else:
                partial = 0

            if partial:
                logger.info(
                    "♻️ Reprise du téléchargement à %.2f Mo",
                    partial / 1024 / 1024,
                )
            else:
                logger.info("📥 Nouveau téléchargement")

            cmd = [
                "aria2c",
                "-x4",
                "-s4",
                "-k1M",

                "--file-allocation=none",

                "--console-log-level=notice",
                "--summary-interval=5",

                "--max-tries=1",
                "--retry-wait=0",

                "--connect-timeout=30",
                "--timeout=60",

                "--allow-overwrite=true",
                "--auto-file-renaming=false",

                "--continue=true",
                "--always-resume=true",

                "--dir",
                workdir,

                "--out",
                filename,

                "--header=User-Agent: Mozilla/5.0",
                "--header=Referer: " + transfer_url,

                mega_url,
            ]

            logger.info("🚀 Démarrage aria2c")

            with open(
                log_file,
                "w",
                encoding="utf-8",
            ) as log:

                process = subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

                # IMPORTANT :
                # Pas de timeout global de 60 secondes.
                # On laisse Mega télécharger aussi longtemps que nécessaire.
                code = process.wait()

            try:
                output = Path(log_file).read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except Exception:
                output = ""

            logger.info(
                "🏁 aria2c terminé | code=%s",
                code,
            )

            for line in output.splitlines()[-20:]:
                logger.info("ARIA2C: %s", line)

            # Téléchargement terminé
            if (
                code == 0
                and os.path.isfile(output_path)
                and os.path.getsize(output_path) > 0
            ):
                size = os.path.getsize(output_path)

                logger.info(
                    "✅ Mega terminé : %.2f Mo",
                    size / 1024 / 1024,
                )

                try:
                    os.remove(log_file)
                except Exception:
                    pass

                return output_path

            # Recherche d'une vraie erreur HTTP / réseau
            lower = output.lower()

            if "509" in lower:
                last_error = RuntimeError("Mega HTTP 509")
                logger.warning("⚠️ Mega HTTP 509")
                logger.warning("🔄 Nouvelle URL Mega...")

            elif "403" in lower:
                last_error = RuntimeError("Mega HTTP 403")
                logger.warning("⚠️ Mega HTTP 403")

            elif "404" in lower:
                last_error = RuntimeError("Mega HTTP 404")
                logger.warning("⚠️ Mega HTTP 404")

            elif "416" in lower:
                last_error = RuntimeError("Mega HTTP 416")
                logger.warning("⚠️ Range HTTP 416")
                logger.warning("🔄 Vérification de la reprise...")

            elif code != 0:
                last_error = RuntimeError(
                    f"Échec Mega code={code}\n{output[-2000:]}"
                )

            else:
                last_error = RuntimeError(
                    "Mega terminé sans fichier valide."
                )

            try:
                os.remove(log_file)
            except Exception:
                pass

            time.sleep(2)

        except Exception as e:
            last_error = e

            logger.exception(
                "❌ Erreur tentative Mega %s/%s",
                retry,
                attempts,
            )

            try:
                if "process" in locals() and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            except Exception:
                pass

            time.sleep(2)

    raise RuntimeError(
        f"❌ Mega impossible après {attempts} tentatives : "
        f"{last_error}"
    )




def check_file_size(file_path):
    """
    Vérifie qu'un fichier téléchargé existe et possède une taille valide.
    Aucun plafond artificiel n'est imposé ici : le Bot API local permet
    d'envoyer les gros fichiers.
    """
    try:
        file_path = Path(file_path)

        if not file_path.exists():
            logger.error(
                "❌ Fichier introuvable : %s",
                file_path,
            )
            return False

        if not file_path.is_file():
            logger.error(
                "❌ Ce n'est pas un fichier : %s",
                file_path,
            )
            return False

        size = file_path.stat().st_size

        if size <= 0:
            logger.error(
                "❌ Fichier vide : %s",
                file_path,
            )
            return False

        logger.info(
            "📦 Taille du fichier : %.2f MiB",
            size / (1024 * 1024),
        )

        return True

    except Exception:
        logger.exception(
            "❌ Impossible de vérifier la taille du fichier"
        )
        return False


# ============================================================
# TRAITEMENT D'UNE PUBLICATION
# ============================================================


async def fetch_feed():
    """
    Récupère le flux RSS Tsundere.

    Si le XML contient une erreur de syntaxe, on tente une réparation
    minimale avant de le donner à feedparser.
    """

    import asyncio
    import re
    import feedparser
    import httpx

    rss_url = RSS_URL

    logger.info(
        "📡 Lecture du flux RSS : %s",
        rss_url,
    )

    def parse_feed():
        try:
            # =====================================================
            # 1. RÉCUPÉRATION DU XML BRUT
            # =====================================================

            response = httpx.get(
                rss_url,
                timeout=60,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/rss+xml, application/xml, text/xml, */*",
                },
            )

            response.raise_for_status()

            raw = response.content

            logger.info(
                "📥 XML RSS reçu : %.2f Ko",
                len(raw) / 1024,
            )

            # =====================================================
            # 2. PREMIER PARSING NORMAL
            # =====================================================

            feed = feedparser.parse(raw)

            entries = getattr(feed, "entries", [])

            if entries:
                logger.info(
                    "📡 %d publication(s) trouvée(s)",
                    len(entries),
                )
                return feed

            # =====================================================
            # 3. XML MAL FORMÉ → RÉPARATION
            # =====================================================

            logger.warning(
                "⚠️ RSS mal formé : tentative de réparation du XML..."
            )

            text = raw.decode(
                "utf-8",
                errors="replace",
            )

            # Suppression des caractères de contrôle interdits
            # par XML 1.0.
            text = re.sub(
                r"[\x00-\x08\x0B\x0C\x0E-\x1F]",
                "",
                text,
            )

            # Répare les '&' isolés qui ne commencent pas
            # une vraie entité XML/HTML.
            text = re.sub(
                r"&(?!#\d+;|#x[0-9A-Fa-f]+;|amp;|lt;|gt;|quot;|apos;)",
                "&amp;",
                text,
            )

            repaired = text.encode(
                "utf-8",
                errors="replace",
            )

            feed = feedparser.parse(repaired)

            entries = getattr(feed, "entries", [])

            if entries:
                logger.info(
                    "✅ XML réparé : %d publication(s) trouvée(s)",
                    len(entries),
                )
            else:
                logger.error(
                    "❌ Impossible de récupérer les publications même après réparation."
                )

                if getattr(feed, "bozo", False):
                    logger.error(
                        "❌ Erreur RSS après réparation : %s",
                        getattr(
                            feed,
                            "bozo_exception",
                            "inconnue",
                        ),
                    )

            return feed

        except Exception as e:
            logger.exception(
                "❌ Erreur lors de la récupération du RSS : %s",
                e,
            )
            return None

    return await asyncio.to_thread(parse_feed)

async def find_alternative_source(application, title, guid):
    """
    Recherche UNIQUEMENT une autre source Transfer.it
    pour le même épisode.
    """

    logger.info(
        "🔄 Recherche d'une source Transfer.it pour : %s",
        title,
    )

    try:
        feed = await fetch_feed()
    except Exception as e:
        logger.exception(
            "❌ Impossible de récupérer le RSS : %s",
            e,
        )
        return False

    if not feed:
        logger.error("❌ Flux RSS vide ou inaccessible.")
        return False

    title_lower = str(title).lower().strip()

    def episode_key(text):
        if not text:
            return ""

        text = str(text).lower()

        m = re.search(
            r"\bs(\d{1,3})\s*e(\d{1,4})\b",
            text,
        )

        if m:
            return (
                f"s{int(m.group(1)):02d}"
                f"e{int(m.group(2)):02d}"
            )

        m = re.search(
            r"\b(\d{1,3})x(\d{1,4})\b",
            text,
        )

        if m:
            return (
                f"s{int(m.group(1)):02d}"
                f"e{int(m.group(2)):02d}"
            )

        return ""

    wanted_episode = episode_key(title)
    candidates = []

    for item in feed.entries:
        item_title = get_title(item)

        if not item_title:
            continue

        item_guid = str(
            item.get("id")
            or item.get("guid")
            or item.get("link")
            or item_title
        ).strip()

        if item_guid == str(guid).strip():
            continue

        # Vérification de l'épisode
        item_episode = episode_key(item_title)

        if wanted_episode and item_episode != wanted_episode:
            continue

        # Vérification du titre
        item_title_lower = item_title.lower()

        words = [
            word
            for word in re.findall(
                r"[a-z0-9àâçéèêëîïôûùüÿœ'-]+",
                title_lower,
            )
            if len(word) >= 3
        ]

        if words:
            matches = sum(
                1
                for word in words
                if word in item_title_lower
            )

            required = min(
                3,
                max(1, len(words) // 3),
            )

            if matches < required:
                continue

        urls = []

        def add_url(value):
            if not value:
                return

            value = str(value).strip()

            if (
                value.startswith("http://")
                or value.startswith("https://")
            ):
                if value not in urls:
                    urls.append(value)

        # Enclosures
        for enclosure in item.get("enclosures", []) or []:
            if isinstance(enclosure, dict):
                add_url(enclosure.get("href"))
                add_url(enclosure.get("url"))

        # Media
        for media in item.get("media_content", []) or []:
            if isinstance(media, dict):
                add_url(media.get("url"))
                add_url(media.get("href"))

        # Champs classiques
        add_url(item.get("link"))
        add_url(item.get("guid"))

        # Description / résumé
        for field in ("description", "summary"):
            value = item.get(field)

            if not value:
                continue

            found_urls = re.findall(
                r'https?://[^\s<>"\']+',
                str(value),
            )

            for found_url in found_urls:
                add_url(found_url)

        # SEUL TRANSFER.IT EST ACCEPTÉ
        for url in urls:
            if "transfer.it/t/" not in url.lower():
                continue

            candidates.append(
                (
                    0,
                    "Transfer.it",
                    url,
                    item,
                    item_guid,
                    item_title,
                )
            )

    if not candidates:
        logger.error(
            "❌ Aucun lien Transfer.it trouvé pour : %s",
            title,
        )
        return False

    # Le premier Transfer.it trouvé
    (
        priority,
        source,
        url,
        item,
        item_guid,
        item_title,
    ) = candidates[0]

    logger.info(
        "🎯 Source alternative sélectionnée : Transfer.it | %s",
        url,
    )

    selected_entry = (
        item.copy()
        if hasattr(item, "copy")
        else item
    )

    selected_entry["link"] = url
    selected_entry["guid"] = item_guid

    selected_entry["enclosures"] = [
        {
            "href": url,
            "url": url,
            "type": "application/octet-stream",
        }
    ]

    selected_entry["media_content"] = [
        {
            "url": url,
        }
    ]

    logger.info(
        "🔧 URL Transfer.it injectée : %s",
        url,
    )

    try:
        await process_entry(
            application,
            selected_entry,
            forced_transfer_url=url,
        )
    except Exception as e:
        logger.exception(
            "❌ Erreur lors du traitement Transfer.it : %s",
            e,
        )
        return False

    return True


import threading

# Verrou global : bloque la surveillance RSS pendant l'upload Telegram.
upload_in_progress = threading.Event()

processing_guids = set()



async def tgup_upload_file(file_path, target, caption=""):
    import asyncio
    import os
    import subprocess
    import re

    tgup_bin = os.path.expanduser("~/tgup-test/tgup")
    session = os.path.expanduser("~/tgup-test/session")
    state = os.path.expanduser("~/tgup-test/rss-upload.sqlite")

    # Récupérer exactement les mêmes identifiants que lors du test manuel
    try:
        ps = subprocess.check_output(
            ["sh", "-c", "ps -ef | grep '[t]elegram-bot-api'"],
            text=True,
            stderr=subprocess.DEVNULL,
        )

        m_id = re.search(r"--api-id=(\d+)", ps)
        m_hash = re.search(r"--api-hash=([^\s]+)", ps)

        if not m_id or not m_hash:
            raise RuntimeError("API ID/API hash introuvables")

        env = os.environ.copy()
        env["TGUP_API_ID"] = m_id.group(1)
        env["TGUP_API_HASH"] = m_hash.group(1)

    except Exception as e:
        raise RuntimeError(
            f"Impossible de préparer l'environnement TGUP: {e}"
        )

    cmd = [
        tgup_bin,
        "run",
        "--force-multi-command",
        "--state", state,
        "--session", session,
        "--src", str(file_path),
        "--target", str(target),
        "--threads", "8",
        "--pool-size", "8",
    ]

    logger.info("🚀 TGUP/MTProto : %s", file_path)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    output = []

    while True:
        line = await proc.stdout.readline()

        if not line:
            break

        text = line.decode(errors="replace").rstrip()
        output.append(text)
        logger.info("TGUP | %s", text)

    rc = await proc.wait()

    if rc != 0:
        raise RuntimeError(
            "TGUP upload échoué (code %s):\n%s"
            % (rc, "\n".join(output[-30:]))
        )

    logger.info("✅ TGUP upload terminé : %s", file_path)
    return True

async def send_file(application, file_path, title, entry):
    # 🚀 Upload principal via TGUP/MTProto
    try:
        _tgup_target = os.getenv("TGUP_TARGET_CHANNEL", CHANNEL_ID)
        await tgup_upload_file(file_path, _tgup_target, title or "")
        return
    except Exception:
        logger.exception("❌ TGUP échoué, retour au système d'upload actuel")
    """
    Upload vidéo optimisé vers le Bot API local.

    - Envoi multipart streaming
    - Aucun chargement complet du fichier en RAM
    - Confirmation réelle du Bot API attendue
    - Surveillance RSS bloquée pendant tout l'upload
    """

    import asyncio
    import time
    import uuid
    from pathlib import Path
    import httpx

    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"Fichier introuvable : {file_path}"
        )

    file_size = file_path.stat().st_size

    if file_size <= 0:
        raise RuntimeError("Fichier vidéo vide.")

    size_mb = file_size / (1024 * 1024)

    upload_url = (
        f"http://127.0.0.1:8081/bot{BOT_TOKEN}/sendVideo"
    )

    boundary = (
        "----TsundereRaws"
        + uuid.uuid4().hex
    )

    filename = file_path.name

    prefix = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
        f"{CHANNEL_ID}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="video"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: video/mp4\r\n"
        f"\r\n"
    ).encode()

    suffix = (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n'
        f"{title}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="supports_streaming"\r\n\r\n'
        f"true\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    content_length = (
        len(prefix)
        + file_size
        + len(suffix)
    )

    sent_bytes = 0
    start_time = time.monotonic()
    last_log_time = start_time
    last_percent = -1

    logger.info(
        "📤 Préparation upload Telegram : %.2f Mo",
        size_mb,
    )

    def body_generator():
        nonlocal sent_bytes
        nonlocal last_log_time
        nonlocal last_percent

        yield prefix

        with open(file_path, "rb", buffering=1024 * 1024) as f:
            while True:
                chunk = f.read(4 * 1024 * 1024)

                if not chunk:
                    break

                sent_bytes += len(chunk)

                yield chunk

                percent = int(
                    sent_bytes * 100 / file_size
                )

                now = time.monotonic()

                if (
                    percent >= last_percent + 5
                    or now - last_log_time >= 10
                    or sent_bytes >= file_size
                ):
                    elapsed = max(
                        now - start_time,
                        0.001,
                    )

                    speed_mbit = (
                        sent_bytes * 8
                        / elapsed
                        / 1_000_000
                    )

                    remaining = max(
                        file_size - sent_bytes,
                        0,
                    )

                    speed_bytes = sent_bytes / elapsed

                    remaining_seconds = (
                        remaining / speed_bytes
                        if speed_bytes > 0
                        else 0
                    )

                    logger.info(
                        "📤 Upload Telegram : "
                        "%3d%% | %.2f / %.2f Mo | "
                        "⚡ %.2f Mbit/s | "
                        "⏳ reste ~%ds",
                        percent,
                        sent_bytes / 1024 / 1024,
                        size_mb,
                        speed_mbit,
                        int(remaining_seconds),
                    )

                    last_percent = percent
                    last_log_time = now

        yield suffix

    logger.info(
        "🚀 Envoi vers le Bot API local..."
    )

    upload_start = time.monotonic()

    upload_in_progress.set()

    logger.info(
        "🔒 Upload Telegram actif : surveillance RSS en pause."
    )

    def perform_upload():
        with httpx.Client(
            timeout=httpx.Timeout(
                connect=120,
                read=3600,
                write=3600,
                pool=120,
            ),
            limits=httpx.Limits(
                max_connections=1,
                max_keepalive_connections=1,
            ),
        ) as client:

            return client.post(
                upload_url,
                headers={
                    "Content-Type": (
                        "multipart/form-data; "
                        f"boundary={boundary}"
                    ),
                    "Content-Length": str(
                        content_length
                    ),
                },
                content=body_generator(),
            )

    try:
        response = await asyncio.to_thread(
            perform_upload
        )

    finally:
        upload_in_progress.clear()

        logger.info(
            "🔓 Upload Telegram terminé : surveillance RSS autorisée."
        )

    total_duration = (
        time.monotonic() - upload_start
    )

    logger.info(
        "📨 Réponse Bot API reçue après %.1f s",
        total_duration,
    )

    if response.status_code != 200:
        logger.error(
            "❌ Bot API HTTP %s",
            response.status_code,
        )
        logger.error(
            "❌ Réponse : %s",
            response.text[:3000],
        )

        raise RuntimeError(
            f"Bot API HTTP {response.status_code}: "
            f"{response.text[:1000]}"
        )

    try:
        result = response.json()

    except Exception as e:
        logger.error(
            "❌ Réponse Bot API non JSON : %s",
            response.text[:3000],
        )

        raise RuntimeError(
            "Réponse Bot API invalide."
        ) from e

    if not result.get("ok"):
        description = result.get(
            "description",
            "Erreur Telegram inconnue",
        )

        logger.error(
            "❌ Telegram a refusé la vidéo : %s",
            description,
        )

        raise RuntimeError(
            f"Telegram a refusé la vidéo : "
            f"{description}"
        )

    logger.info(
        "✅ TELEGRAM A CONFIRMÉ L'ENVOI"
    )

    logger.info(
        "🎬 Vidéo : %s",
        title,
    )

    logger.info(
        "📦 Taille : %.2f Mo",
        size_mb,
    )

    logger.info(
        "⏱️ Temps total sendVideo : %.1f s",
        total_duration,
    )

    logger.info(
        "📨 Message Telegram confirmé."
    )

    return result

async def process_entry(
    application,
    entry,
    forced_transfer_url=None,
    bypass_duplicate=False,
):
    """Wrapper anti-doublon autour du vrai traitement."""

    title = (
        entry.get("title")
        or entry.get("name")
        or "Vidéo inconnue"
    )

    guid = str(
        entry.get("guid")
        or entry.get("id")
        or entry.get("link")
        or title
    ).strip()

    if not guid:
        guid = title.strip().lower()

    # Même épisode déjà en cours
    if guid in processing_guids:
        logger.warning(
            "⏭️ DOUBLON IGNORÉ — épisode déjà en cours : %s",
            title,
        )
        return

    # Même épisode déjà terminé
    if not bypass_duplicate:
        try:
            if already_processed(guid):
                logger.info(
                    "⏭️ DOUBLON IGNORÉ — épisode déjà traité : %s",
                    title,
                )
                return

        except Exception as e:
            logger.warning(
                "⚠️ Vérification anti-doublon impossible pour %s : %s",
                title,
                e,
            )

    processing_guids.add(guid)

    logger.info(
        "🔒 Verrou anti-doublon activé : %s",
        title,
    )

    try:
        return await _process_entry_impl(
            application,
            entry,
            forced_transfer_url=forced_transfer_url,
        )

    finally:
        processing_guids.discard(guid)

        logger.info(
            "🔓 Verrou anti-doublon libéré : %s",
            title,
        )


async def _process_entry_impl(application, entry, forced_transfer_url=None):
    title = (
        entry.get("title")
        or entry.get("name")
        or "Vidéo inconnue"
    )

    guid = (
        entry.get("guid")
        or entry.get("id")
        or entry.get("link")
        or title
    )

    guid = str(guid).strip()

    # --------------------------------------------------------
    # UNIQUEMENT HARDSUB
    # --------------------------------------------------------
    if not is_hardsub_entry(entry):
        logger.info(
            "⏭️ Ignoré — pas HARDSUB : %s",
            title,
        )
        return

    # --------------------------------------------------------
    # Récupération du lien Transfer.it
    # --------------------------------------------------------
    if forced_transfer_url:
        transfer_url = str(forced_transfer_url).strip()
    else:
        transfer_url = extract_video_url(entry)

    logger.info(
        "🔎 URL extraite pour %s : %r",
        title,
        transfer_url,
    )

    if not transfer_url:
        logger.warning(
            "⚠️ Aucun lien pour : %s",
            title,
        )
        return

    # --------------------------------------------------------
    # UNIQUEMENT TRANSFER.IT
    # --------------------------------------------------------
    if "transfer.it/t/" not in transfer_url.lower():
        logger.info(
            "⏭️ Ignoré — pas Transfer.it : %s",
            title,
        )
        return

    logger.info(
        "🎯 Transfer.it détecté : %s",
        transfer_url,
    )

    # --------------------------------------------------------
    # Résolution Transfer.it -> Mega CDN
    # SANS téléchargement
    # --------------------------------------------------------
    try:
        final_url = await asyncio.to_thread(
            resolve_transferit_final_url,
            transfer_url,
        )
    except Exception as e:
        if "blocked" in str(e).lower():
            logger.warning(
                "🚫 Transfer.it répond « blocked » (lien bloqué OU IP Colab "
                "bloquée par Mega), épisode ignoré : %s | détail : %s",
                title,
                e,
            )
            # Mémorise le lien mort : il ne sera plus retenté
            # à chaque redémarrage du bot.
            try:
                mark_processed(guid, title, transfer_url)
            except Exception:
                logger.exception("⚠️ Impossible de mémoriser le lien mort")
        else:
            logger.exception(
                "❌ Impossible de résoudre Transfer.it pour %s : %s",
                title,
                e,
            )
        return

    # --------------------------------------------------------
    # CRÉATION DU LIEN PERMANENT
    # --------------------------------------------------------

    try:
        import re

        clean_title = str(title).strip()

        # Cherche SxxEyy dans le titre RSS
        match = re.search(
            r'\bS(\d{1,2})E(\d{1,3})\b',
            clean_title,
            re.IGNORECASE,
        )

        if match:
            season_episode = (
                f"S{int(match.group(1)):02d}"
                f"E{int(match.group(2)):02d}"
            )

            # Le nom est tout ce qui précède SxxEyy
            name_part = clean_title[:match.start()].strip()

            if not name_part:
                name_part = clean_title

        else:
            season_episode = ""
            name_part = clean_title

        # Enregistre UNIQUEMENT le Transfer.it original.
        # L'URL Mega temporaire n'est pas enregistrée.
        link_key = create_permanent_link(
            transfer_url,
            clean_title,
        )

        # Récupère automatiquement le username du bot.
        bot_info = await application.bot.get_me()
        bot_username = bot_info.username

        if not bot_username:
            raise RuntimeError(
                "Impossible de récupérer le username du bot."
            )

        permanent_url = (
            f"https://t.me/{bot_username}"
            f"?start={link_key}"
        )

        if season_episode:
            message_text = (
                f"{name_part} "
                f"{season_episode} "
                f"HARDSUB={permanent_url}"
            )
        else:
            message_text = (
                f"{name_part} "
                f"HARDSUB={permanent_url}"
            )

        await application.bot.send_message(
            chat_id=CHANNEL_ID,
            text=message_text,
            disable_web_page_preview=True,
        )

        logger.info(
            "📨 Lien PERMANENT envoyé : %s",
            permanent_url,
        )

        logger.info(
            "🔐 Transfer.it conservé pour régénération : %s",
            transfer_url,
        )

    except Exception:
        logger.exception(
            "❌ Échec de création/envoi du lien permanent : %s",
            title,
        )
        raise

    # --------------------------------------------------------
    # MARQUÉ COMME TRAITÉ APRÈS SUCCÈS
    # On garde le lien Transfer.it comme source enregistrée.
    # --------------------------------------------------------
    try:
        mark_processed(
            guid,
            title,
            transfer_url,
        )
    except Exception:
        logger.exception(
            "⚠️ URL envoyée mais impossible de marquer comme traité : %s",
            title,
        )
        raise

    logger.info(
        "✅ Épisode traité — URL finale envoyée : %s",
        title,
    )

async def rss_loop(application):

    logger.info(
        "📡 Surveillance RSS démarrée"
    )

    logger.info(
        "🔗 %s",
        RSS_URL,
    )

    first_run = True
    known_guids = set()

    try:

        conn = sqlite3.connect(DB_FILE)

        rows = conn.execute(
            "SELECT guid FROM processed"
        ).fetchall()

        conn.close()

        known_guids = {
            str(row[0]).strip()
            for row in rows
            if row and row[0]
        }

        logger.info(
            "🗃️ %d publication(s) déjà connues",
            len(known_guids),
        )

    except Exception:

        logger.exception(
            "❌ Impossible de charger l'historique"
        )

    while True:
        while upload_in_progress.is_set():
            logger.info(
                "⏸️ Vérification RSS en attente : "
                "upload Telegram encore en cours..."
            )
            await asyncio.sleep(1)

        logger.info("🔄 Vérification RSS démarrée")

        try:

            feed = await fetch_feed()

            if feed is None:
                logger.warning(
                    "⏭️ RSS indisponible : vérification ignorée. État actuel conservé."
                )
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            entries = list(
                getattr(
                    feed,
                    "entries",
                    [],
                )
            )

            logger.info(
                "📡 %d publication(s) trouvée(s)",
                len(entries),
            )

            # =================================================
            # PREMIER PASSAGE :
            # 5 DERNIÈRES VIDÉOS HARDSUB + TRANSFER.IT
            # =================================================
            if first_run:

                logger.info(
                    "🚀 Premier démarrage : recherche des 5 dernières vidéos HARDSUB + Transfer.it"
                )

                import calendar

                candidates = []

                for entry in entries:

                    # HARDSUB officiel Tsundere
                    if not is_hardsub_entry(entry):
                        continue

                    # Transfer.it obligatoire
                    transfer_url = extract_video_url(entry)

                    if not (
                        transfer_url
                        and "transfer.it/t/" in transfer_url.lower()
                    ):
                        continue

                    # Date réelle de publication RSS
                    published = entry.get("published_parsed")

                    if published:
                        try:
                            timestamp = calendar.timegm(published)
                        except Exception:
                            timestamp = 0
                    else:
                        timestamp = 0

                    candidates.append(
                        (
                            timestamp,
                            entry,
                        )
                    )

                # Plus récent -> plus ancien
                candidates.sort(
                    key=lambda item: item[0],
                    reverse=True,
                )

                # ====================================================
                # 🔒 ANTI-DOUBLON DU DÉMARRAGE
                # ====================================================

                startup_entries = []
                startup_episode_keys = set()

                for _, entry in candidates:

                    title = get_title(entry)

                    # episode_key() permet de reconnaître
                    # le même anime + saison + épisode.
                    try:
                        episode_id = episode_key(title)
                    except Exception:
                        episode_id = title.strip().lower()

                    if episode_id in startup_episode_keys:
                        logger.info(
                            "⏭️ Doublon startup ignoré : %s",
                            title,
                        )
                        continue

                    startup_episode_keys.add(episode_id)
                    startup_entries.append(entry)

                    if len(startup_entries) >= max(STARTUP_COUNT, 1):
                        break

                startup_entries = startup_entries[:max(STARTUP_COUNT, 0)]

                logger.info(
                    "🎞️ %d vidéo(s) HARDSUB + Transfer.it trouvée(s)",
                    len(candidates),
                )

                logger.info(
                    "🎬 %d vidéo(s) sélectionnée(s) pour le démarrage",
                    len(startup_entries),
                )

                # Afficher précisément les 5 choisies
                for position, entry in enumerate(
                    startup_entries,
                    1,
                ):

                    title = get_title(entry)

                    published = entry.get(
                        "published",
                        "date inconnue",
                    )

                    transfer_url = extract_video_url(
                        entry
                    )

                    logger.info(
                        "🎯 [%d/5] %s | %s | %s",
                        position,
                        published,
                        title,
                        transfer_url,
                    )

                # Ne jamais retraiter ces 5 pendant
                # la surveillance suivante.
                selected_guids = set()

                for entry in startup_entries:

                    guid = str(
                        entry.get("id")
                        or entry.get("guid")
                        or entry.get("link")
                        or ""
                    ).strip()

                    if guid:
                        selected_guids.add(guid)

                # Toutes les publications qui ne font pas
                # partie des 5 sélectionnées sont considérées
                # comme anciennes au démarrage.
                for entry in entries:

                    guid = str(
                        entry.get("id")
                        or entry.get("guid")
                        or entry.get("link")
                        or ""
                    ).strip()

                    if guid and guid not in selected_guids:
                        known_guids.add(guid)

                # Traitement séquentiel des 5
                for position, entry in enumerate(
                    startup_entries,
                    1,
                ):

                    guid = str(
                        entry.get("id")
                        or entry.get("guid")
                        or entry.get("link")
                        or ""
                    ).strip()

                    if not guid:
                        continue

                    title = get_title(entry)

                    transfer_url = extract_video_url(
                        entry
                    )

                    logger.info(
                        "🚀 [%d/5] Traitement : %s",
                        position,
                        title,
                    )

                    logger.info(
                        "🎯 [%d/5] Transfer.it : %s",
                        position,
                        transfer_url,
                    )

                    try:

                        await process_entry(
                            application,
                            entry,
                        )

                        known_guids.add(guid)

                        logger.info(
                            "✅ [%d/5] Terminé : %s",
                            position,
                            title,
                        )

                    except Exception:

                        logger.exception(
                            "❌ [%d/5] Erreur : %s",
                            position,
                            title,
                        )

                first_run = False

                logger.info(
                    "✅ Traitement initial terminé."
                )

                logger.info(
                    "👀 Surveillance RSS activée."
                )

                logger.info(
                    "⏳ Prochaine vérification RSS dans %ss",
                    CHECK_INTERVAL,
                )

                await asyncio.sleep(
                    CHECK_INTERVAL
                )

                continue

            # NOUVELLES ENTRÉES
            # =================================================

            new_entries = []

            for entry in entries:

                # 🔒 HARDSUB officiel uniquement
                if not is_hardsub_entry(entry):
                    continue

                # 🔒 Transfer.it uniquement
                transfer_check = extract_video_url(entry)

                if not (
                    transfer_check
                    and "transfer.it/t/" in transfer_check.lower()
                ):
                    continue

                guid = str(
                    entry.get("id")
                    or entry.get("guid")
                    or entry.get("link")
                    or ""
                ).strip()

                if not guid:
                    continue

                if guid in known_guids:
                    continue

                if already_processed(guid):
                    known_guids.add(guid)
                    continue

                new_entries.append((guid, entry))


            if not new_entries:

                logger.info("⏳ Prochaine vérification RSS dans %ss", CHECK_INTERVAL)
                await asyncio.sleep(
                    CHECK_INTERVAL
                )

                continue

            # =================================================
            # GROUPEMENT PAR ÉPISODE
            # =================================================

            groups = {}

            for guid, entry in new_entries:

                title = get_title(entry)

                key = episode_key(title)

                groups.setdefault(
                    key,
                    [],
                ).append(
                    (
                        guid,
                        entry,
                    )
                )

            # =================================================
            # TRAITEMENT
            # =================================================

            for key, group in groups.items():

                def group_priority(item):

                    entry = item[1]

                    url = extract_video_url(entry)

                    if not url:
                        return 999

                    return source_priority(url)

                group.sort(
                    key=group_priority
                )

                guid, selected_entry = group[0]

                title = get_title(
                    selected_entry
                )

                # =================================================
                # TRANSFER.IT PRIORITAIRE
                # =================================================
                # Dans un groupe, on cherche d'abord l'entrée qui
                # contient réellement le lien Transfer.it.
                transfer_entry = None

                for group_guid, group_entry in group:
                    group_url = extract_video_url(group_entry)

                    if group_url and "transfer.it/t/" in group_url.lower():
                        transfer_entry = group_entry
                        logger.info(
                            "🎯 Transfer.it sélectionné pour %s : %s",
                            title,
                            group_url,
                        )
                        break

                if transfer_entry is not None:
                    selected_entry = transfer_entry

                    # Le GUID doit correspondre à l'entrée réellement
                    # sélectionnée et téléchargée.
                    guid = str(
                        selected_entry.get("id")
                        or selected_entry.get("guid")
                        or selected_entry.get("link")
                        or ""
                    ).strip()

                url = extract_video_url(
                    selected_entry
                )

                logger.info(
                    "🎯 Source choisie pour %s : %s",
                    title,
                    url,
                )

                try:
                    await process_entry(
                        application,
                        selected_entry,
                    )

                    if already_processed(guid):
                        # Le GUID réellement traité est mémorisé.
                        known_guids.add(guid)

                        # Les autres entrées du même épisode sont aussi
                        # ignorées afin d'éviter un téléchargement doublon.
                        for group_guid, group_entry in group:
                            known_guids.add(group_guid)

                except Exception:
                    logger.exception(
                        "❌ Erreur pendant la vérification RSS"
                    )


        except Exception:
            logger.exception(
                "❌ Erreur pendant la vérification RSS"
            )

        logger.info(
            "⏳ Prochaine vérification RSS dans %ss",
            CHECK_INTERVAL,
        )
        await asyncio.sleep(
            CHECK_INTERVAL
        )

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    # Aucun message en PV
    return


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    # /status uniquement en PV du créateur
    if not update.effective_chat:
        return

    if update.effective_chat.type != "private":
        return

    if not update.effective_user:
        return

    if update.effective_user.id != CREATOR_ID:
        return

    try:
        conn = sqlite3.connect(DB_FILE)

        count = conn.execute(
            "SELECT COUNT(*) FROM processed"
        ).fetchone()[0]

        conn.close()

        await update.effective_message.reply_text(
            (
                "📊 STATUS TSUNDERE RAWS\n\n"
                f"📚 Publications traitées : {count}\n"
                f"⏱️ Vérification RSS : toutes les {CHECK_INTERVAL}s\n"
                f"📢 Canal : {CHANNEL_ID}\n"
                "🔗 Transfer.it → Mega : actif\n"
                "📤 Publication automatique : active"
            )
        )

    except Exception as e:
        logger.exception("❌ Erreur /status")

        if update.effective_message:
            await update.effective_message.reply_text(
                f"❌ Erreur /status : {e}"
            )


# ============================================================
# TEST RSS DANS LE CANAL
# ============================================================

def resolve_transferit_mega_sync(url: str):
    """
    Résout Transfer.it vers son URL Mega directe
    sans télécharger le fichier.
    """

    tx = Transferit()

    xh = tx._api.parse_xh(url)

    node_dicts, pw_token = tx._api.fetch_transfer(xh)

    logger.info(
        "📦 Transfer.it : %d élément(s)",
        len(node_dicts),
    )

    file_node = next(
        (
            node
            for node in node_dicts
            if node.get("t") == 0
        ),
        None,
    )

    if not file_node:
        raise RuntimeError(
            "Aucun fichier trouvé dans Transfer.it."
        )

    logger.info(
        "📄 Fichier : %s",
        file_node.get("name", "inconnu"),
    )

    dl = tx._api.get_download_url(
        xh,
        file_node["h"],
        pw_token=pw_token,
    )

    mega_url = dl.get("g")

    if not mega_url:
        raise RuntimeError(
            "Transfer.it n'a pas retourné de lien Mega."
        )

    return mega_url



async def safe_edit_message(message, text, **kwargs):
    """Évite qu'un timeout Telegram fasse planter le traitement."""
    for attempt in range(3):
        try:
            return await message.edit_text(
                text,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=30,
                **kwargs,
            )
        except Exception as e:
            logger.warning(
                "⚠️ Échec édition Telegram (%s/3): %s",
                attempt + 1,
                e,
            )
            if attempt < 2:
                await asyncio.sleep(2)

    logger.warning("⚠️ Impossible de modifier le message Telegram.")
    return None


async def test_rss(update, context):
    if not update.effective_chat:
        return

    chat_id = update.effective_chat.id
    release_channel = -1004442860376

    message = await context.application.bot.send_message(
        chat_id=chat_id,
        text=(
            "🧪 <b>/test lancé...</b>\n\n"
            "🔎 Recherche d'un épisode dans le RSS..."
        ),
        parse_mode="HTML",
    )

    test_file = None

    try:
        feed = await fetch_feed()

        if not feed.entries:
            await safe_edit_message(message,"❌ Aucun élément trouvé dans le RSS.")
            return

        # Recherche automatique du premier Transfer.it
        entry = None
        transfer_url = None

        for candidate in feed.entries:
            candidate_url = extract_video_url(candidate)

            if (
                candidate_url
                and "transfer.it/t/" in candidate_url.lower()
            ):
                entry = candidate
                transfer_url = candidate_url
                break

        if entry is None or not transfer_url:
            await safe_edit_message(
                message,
                "❌ Aucun lien Transfer.it trouvé dans le RSS.",
                parse_mode="HTML",
            )
            return

        title = get_title(entry)

        logger.info(
            "🎯 /test : Transfer.it trouvé automatiquement : %s",
            transfer_url,
        )
        title = get_title(entry)
        transfer_url = extract_video_url(entry)

        if not transfer_url:
            await safe_edit_message(message,
                f"❌ Aucune URL trouvée pour :\n<b>{title}</b>",
                parse_mode="HTML",
            )
            return

        if "transfer.it/t/" not in transfer_url.lower():
            await safe_edit_message(message,
                "❌ Le premier élément du RSS n'est pas un lien Transfer.it."
            )
            return

        await safe_edit_message(message,
            f"📺 <b>{title}</b>\n\n"
            "📥 Téléchargement depuis Mega...",
            parse_mode="HTML",
        )

        # Transfer.it → Mega → téléchargement réel
        loop = asyncio.get_running_loop()

        def update_download_progress(text):
            try:
                asyncio.run_coroutine_threadsafe(
                    safe_edit_message(
                        message,
                        text,
                        parse_mode="HTML",
                    ),
                    loop,
                )
            except Exception as e:
                logger.debug(
                    "⚠️ Mise à jour progression ignorée: %s",
                    e,
                )

        test_file = await asyncio.to_thread(
            download_mega_with_retry,
            transfer_url,
            8,
            update_download_progress,
        )

        if not test_file:
            raise RuntimeError("Le téléchargement Mega a échoué.")

        test_file = Path(test_file)

        if not test_file.exists():
            raise RuntimeError("Le fichier téléchargé est introuvable.")

        size_mb = test_file.stat().st_size / (1024 * 1024)

        if size_mb <= 0:
            raise RuntimeError("Le fichier téléchargé est vide.")

        await safe_edit_message(message,
            f"✅ <b>Téléchargement terminé</b>\n\n"
            f"📦 Taille : {size_mb:.2f} MiB\n"
            f"📤 Envoi vers <b>Release</b>...",
            parse_mode="HTML",
        )

        # 📤 Envoi du vrai fichier dans Release
        send_ok = False
        last_send_error = None

        logger.info(
            "📤 Début upload Telegram | %.2f MiB | canal=%s",
            size_mb,
            release_channel,
        )

        for send_attempt in range(1, 4):
            try:
                logger.info(
                    "📤 Upload Telegram | tentative %d/3",
                    send_attempt,
                )

                with test_file.open("rb") as document:
                    result = await context.application.bot.send_document(
                        chat_id=release_channel,
                        document=document,
                        caption=f"🎬 {title}",
                        read_timeout=1800,
                        write_timeout=1800,
                        connect_timeout=180,
                        pool_timeout=180,
                    )

                if result and result.document:
                    send_ok = True
                    logger.info(
                        "✅ Upload Telegram réussi | message_id=%s",
                        result.message_id,
                    )
                    break

                raise RuntimeError(
                    "Telegram n'a pas retourné de document après l'upload."
                )

            except Exception as e:
                last_send_error = e

                logger.exception(
                    "❌ Upload Telegram échoué | tentative %d/3",
                    send_attempt,
                )

                if send_attempt < 3:
                    logger.info(
                        "⏳ Nouvelle tentative dans 15 secondes..."
                    )
                    await asyncio.sleep(15)

        if not send_ok:
            raise RuntimeError(
                f"Upload Telegram impossible après 3 tentatives : "
                f"{type(last_send_error).__name__}: {last_send_error}"
            )

    except Exception as e:
        logger.exception("❌ Erreur /test")

        await safe_edit_message(message,
            f"❌ <b>Erreur pendant /test</b>\n\n"
            f"<code>{str(e)[:1500]}</code>",
            parse_mode="HTML",
        )

    finally:
        # 🛡️ Suppression uniquement après un upload Telegram réussi.
        # En cas d'échec, le fichier est conservé pour permettre une nouvelle tentative.
        try:
            if "test_file" in locals() and test_file and Path(test_file).exists():
                if "send_ok" in locals() and send_ok:
                    Path(test_file).unlink()
                    logger.info(
                        "🗑️ Fichier supprimé après upload réussi : %s",
                        test_file,
                    )
                else:
                    logger.warning(
                        "⚠️ Upload échoué : fichier conservé pour nouvelle tentative : %s",
                        test_file,
                    )

        except Exception:
            logger.exception(
                "❌ Impossible de gérer le fichier temporaire : %s",
                test_file if "test_file" in locals() else "inconnu",
            )
        if test_file:
            try:
                path = Path(test_file)
                if path.exists():
                    path.unlink()
                    logger.info("🗑️ Fichier de test supprimé : %s", path)
            except Exception:
                logger.exception("⚠️ Impossible de supprimer le fichier de test")
async def post_init(
    application,
):

    asyncio.create_task(
        rss_loop(application)
    )

    logger.info(
        "🚀 Boucle RSS lancée"
    )


import threading

transfer_cancel_event = threading.Event()


# ============================================================
# /cancel — annuler le téléchargement Transfer.it
# ============================================================

async def cancel_download(update, context):
    if not update.effective_user or update.effective_user.id != CREATOR_ID:
        return

    transfer_cancel_event.set()

    if update.effective_chat:
        await update.effective_chat.send_message(
            "🛑 Annulation du téléchargement demandée..."
        )

    logger.info(
        "🛑 /cancel reçu : annulation Transfer.it demandée."
    )


# ============================================================
# LIENS PERMANENTS
# ============================================================

def create_permanent_link(transfer_url: str, title: str) -> str:
    """
    Enregistre le lien Transfer.it original et crée une clé courte.
    Le lien Mega temporaire n'est jamais stocké.
    """
    import secrets

    transfer_url = str(transfer_url).strip()
    title = str(title).strip()

    conn = sqlite3.connect(DB_FILE)

    try:
        for _ in range(10):
            link_key = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]

            try:
                conn.execute(
                    """
                    INSERT INTO permanent_links
                    (link_key, transfer_url, title, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (link_key, transfer_url, title, int(time.time())),
                )
                conn.commit()
                return link_key

            except sqlite3.IntegrityError:
                continue

        raise RuntimeError("Impossible de créer une clé permanente.")

    finally:
        conn.close()


async def permanent_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start <clé>

    Retrouve le Transfer.it original puis génère une nouvelle URL Mega.
    """
    if not update.effective_message:
        return

    if not context.args:
        return

    link_key = str(context.args[0]).strip()

    if not link_key:
        return

    conn = sqlite3.connect(DB_FILE)

    row = conn.execute(
        """
        SELECT transfer_url, title
        FROM permanent_links
        WHERE link_key = ?
        LIMIT 1
        """,
        (link_key,),
    ).fetchone()

    conn.close()

    if not row:
        await update.effective_message.reply_text(
            "❌ Ce lien n'existe plus ou est invalide."
        )
        return

    transfer_url, title = row

    try:
        logger.info(
            "🔄 Régénération du lien Mega : %s",
            title,
        )

        final_url = await asyncio.to_thread(
            resolve_transferit_final_url,
            transfer_url,
        )

        await update.effective_message.reply_text(
            f"🎬 {title}\n\n"
            f"🔗 {final_url}",
            disable_web_page_preview=True,
        )

        logger.info(
            "✅ Nouveau lien Mega généré : %s",
            title,
        )

    except Exception as e:
        logger.exception(
            "❌ Impossible de régénérer le lien : %s",
            title,
        )

        await update.effective_message.reply_text(
            "❌ Impossible de régénérer le lien pour le moment.\n"
            "Le lien Transfer.it peut être temporairement indisponible."
        )


# ============================================================
# MAIN
# ============================================================

async def rssdebug_command(update, context):
    """
    /rssdebug : affiche, pour les 3 premières entrées HARDSUB du flux,
    tous les liens trouvés champ par champ et celui que le bot choisit.
    Réservé au créateur.
    """
    if not update.effective_user or update.effective_user.id != CREATOR_ID:
        return

    if not update.message:
        return

    try:
        feed = await fetch_feed()
    except Exception as e:
        await update.message.reply_text(f"❌ Lecture RSS impossible : {e}")
        return

    entries = getattr(feed, "entries", []) or []
    shown = 0

    for entry in entries:
        if not is_hardsub_entry(entry):
            continue

        lines = [f"TITRE : {entry.get('title', '?')}"]

        for key in ("published", "id", "guid", "link"):
            if entry.get(key):
                lines.append(f"{key} : {entry.get(key)}")

        for key in entry.keys():
            if "tsundere" in str(key).lower():
                lines.append(f"{key} : {entry.get(key)}")

        for enc in entry.get("enclosures", []) or []:
            lines.append(
                f"enclosure : {enc.get('href') or enc.get('url')}"
            )

        for media in entry.get("media_content", []) or []:
            lines.append(
                f"media : {media.get('url') or media.get('href')}"
            )

        for field in ("description", "summary"):
            for url in extract_urls(str(entry.get(field) or "")):
                lines.append(f"{field} : {url}")

        for content in entry.get("content", []) or []:
            for url in extract_urls(str(content.get("value") or "")):
                lines.append(f"content : {url}")

        lines.append(f"==> CHOISI : {extract_video_url(entry)}")

        await update.message.reply_text(
            "\n".join(lines)[:3800],
            disable_web_page_preview=True,
        )

        shown += 1
        if shown >= 3:
            break

    if shown == 0:
        await update.message.reply_text(
            "Aucune entrée HARDSUB trouvée dans le flux."
        )


async def testlink_command(update, context):
    """
    /testlink <lien transfer.it> [titre]
    Crée et publie un lien permanent dans le canal pour UN lien
    Transfer.it donné, sans passer par le flux RSS. Réservé au créateur.
    """
    if not update.effective_user or update.effective_user.id != CREATOR_ID:
        return

    if not update.message:
        return

    args = context.args or []

    if not args or "transfer.it/t/" not in args[0].lower():
        await update.message.reply_text(
            "Usage : /testlink <lien transfer.it> [titre]\n\n"
            "Exemple :\n"
            "/testlink https://transfer.it/t/xxxxxxxx Mon Anime S01E01"
        )
        return

    transfer_url = args[0].strip()
    title = " ".join(args[1:]).strip() or "Test Lien S01E01"

    entry = {
        "title": title,
        "guid": f"testlink-{int(time.time())}",
        "tsundere_hardsub": "true",
    }

    await update.message.reply_text(
        f"🧪 Test du lien : {title}\n⏳ Création du lien permanent..."
    )

    try:
        await process_entry(
            context.application,
            entry,
            forced_transfer_url=transfer_url,
            bypass_duplicate=True,
        )
    except Exception as e:
        logger.exception("❌ Échec /testlink : %s", e)
        await update.message.reply_text(f"❌ Échec : {e}")
        return

    await update.message.reply_text(
        "✅ Terminé. Vérifie le canal : le message avec le lien "
        "permanent doit y apparaître. Sinon, regarde les logs Colab "
        "(ligne « 📨 Lien PERMANENT envoyé » ou erreur)."
    )


async def test_upload_command(update, context):
    if not update.effective_user or update.effective_user.id != CREATOR_ID:
        return

    """
    /test = teste les 3 dernières publications RSS compatibles :
    HARDSUB + Transfer.it + 720p.

    Le test force volontairement le traitement même si les épisodes
    sont déjà présents dans la base anti-doublon.
    """
    if not update.message:
        return

    await update.message.reply_text(
        "🧪 <b>TEST RSS RÉEL</b>\n\n"
        "📡 Lecture du RSS...\n"
        "🔎 Recherche des 3 derniers : HARDSUB + Transfer.it + 720p",
        parse_mode="HTML",
    )

    try:
        feed = await fetch_feed()

        if feed is None:
            await update.message.reply_text(
                "❌ Impossible de récupérer le RSS."
            )
            return

        entries = getattr(feed, "entries", [])

        if not entries:
            await update.message.reply_text(
                "❌ Aucune publication trouvée dans le RSS."
            )
            return

        candidates = []

        for entry in entries:

            # HARDSUB obligatoire
            if not is_hardsub_entry(entry):
                continue

            # Transfer.it obligatoire
            transfer_url = extract_video_url(entry)

            if not transfer_url:
                continue

            if "transfer.it/t/" not in transfer_url.lower():
                continue

            title = (
                entry.get("title")
                or entry.get("name")
                or "Vidéo inconnue"
            )

            # 720p obligatoire
            if "720p" not in title.lower():
                continue

            candidates.append(
                (
                    entry.get("published_parsed"),
                    entry,
                    transfer_url,
                )
            )

        if not candidates:
            await update.message.reply_text(
                "❌ Aucune vidéo compatible trouvée.\n\n"
                "Conditions :\n"
                "• HARDSUB = true\n"
                "• Transfer.it\n"
                "• 720p"
            )
            return

        # Plus récente en premier
        import calendar

        def timestamp(item):
            published = item[0]

            if not published:
                return 0

            try:
                return calendar.timegm(published)
            except Exception:
                return 0

        candidates.sort(
            key=timestamp,
            reverse=True,
        )

        # Les 3 plus récentes
        selected_candidates = candidates[:3]

        await update.message.reply_text(
            "✅ <b>3 DERNIERS ANIMÉS TROUVÉS</b>\n\n"
            f"📺 {len(selected_candidates)} vidéo(s)\n"
            "🎞 HARDSUB : oui\n"
            "📦 Source : Transfer.it\n"
            "🔗 Liens permanents activés.",
            parse_mode="HTML",
        )

        success_count = 0

        for position, (_, entry, transfer_url) in enumerate(
            selected_candidates,
            start=1,
        ):
            title = (
                entry.get("title")
                or entry.get("name")
                or "Vidéo inconnue"
            )

            await update.message.reply_text(
                "🧪 <b>TEST RSS</b>\n\n"
                f"🎬 {title}\n\n"
                f"📌 {position}/{len(selected_candidates)}\n"
                "📺 Qualité : 720p\n"
                "🎞 HARDSUB : oui\n"
                "📦 Source : Transfer.it\n\n"
                "🔗 Création du lien permanent...",
                parse_mode="HTML",
            )

            logger.info(
                "🧪 /test [%d/%d] : %s",
                position,
                len(selected_candidates),
                title,
            )

            logger.info(
                "🧪 Transfer.it sélectionné : %s",
                transfer_url,
            )

            try:
                await process_entry(
                    context.application,
                    entry,
                    forced_transfer_url=transfer_url,
                    bypass_duplicate=True,
                )

                success_count += 1

            except Exception as item_error:
                logger.exception(
                    "❌ Échec /test [%d/%d] : %s",
                    position,
                    len(selected_candidates),
                    item_error,
                )

                await update.message.reply_text(
                    "❌ <b>ÉCHEC</b>\n\n"
                    f"🎬 {title}\n"
                    f"<code>{str(item_error)[:1000]}</code>",
                    parse_mode="HTML",
                )

        await update.message.reply_text(
            "✅ <b>TEST RSS TERMINÉ</b>\n\n"
            f"🎬 {success_count}/{len(selected_candidates)} "
            "traité(s).",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.exception(
            "❌ Erreur /test RSS réel : %s",
            e,
        )

        await update.message.reply_text(
            "❌ <b>ÉCHEC DU TEST RSS</b>\n\n"
            f"<code>{str(e)[:1500]}</code>",
            parse_mode="HTML",
        )


def main():

    if BOT_TOKEN == "TON_TOKEN_ICI":

        raise RuntimeError(
            "❌ Configure BOT_TOKEN avant de lancer le bot."
        )

    if CHANNEL_ID == "-1000000000000":

        raise RuntimeError(
            "❌ Configure CHANNEL_ID avant de lancer le bot."
        )

    if not RSS_URL:
        raise RuntimeError("❌ Configure RSS_URL avant de lancer le bot.")

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url("http://127.0.0.1:8081/bot")
        .base_file_url("http://127.0.0.1:8081/file/bot")
        .local_mode(True)
        .post_init(post_init)
        .build()
    )

    # ========================================================
    # COMMANDES PV
    # ========================================================

    # /start <clé> : lien permanent
    # Groupe -1 pour passer avant le /start normal.
    application.add_handler(
        CommandHandler(
            "start",
            permanent_link_start,
        ),
        group=-1,
    )

    # /start normal
    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # /status : uniquement le créateur en PV
    application.add_handler(
        CommandHandler(
            "status",
            status,
        )
    )

    # /cancel : annuler le téléchargement Transfer.it
    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_download,
        )
    )


    logger.info(
        "🤖 Bot Telegram démarré"
    )

    application.add_handler(
        CommandHandler(
            "test",
            test_upload_command,
        ),
        group=-1,
    )

    application.add_handler(
        CommandHandler(
            "testlink",
            testlink_command,
        ),
        group=-1,
    )

    application.add_handler(
        CommandHandler(
            "rssdebug",
            rssdebug_command,
        ),
        group=-1,
    )

    application.run_polling(
        drop_pending_updates=True
    )



# ============================================================
# LANCEMENT
# ============================================================

if __name__ == "__main__":
    main()
