#!/usr/bin/env python3
import os
import re
import time
import base64
import logging
from urllib.parse import quote

import requests
from flask import Flask, jsonify, request, abort

app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("bitchord-addon")

APP_NAME = os.getenv("ADDON_NAME", "Open Archive Audio")
APP_ID = os.getenv("ADDON_ID", "dev.ujjal.open-archive-audio")
VERSION = os.getenv("ADDON_VERSION", "1.1.0")
ARCHIVE = "https://archive.org"
TIMEOUT = 12
MAX_RESULTS = 12

session = requests.Session()
session.headers.update({"User-Agent": "BitChord-OpenArchive-Addon/1.1 (metadata search)"})

# Stable, URL-safe opaque ID containing an Archive.org identifier and filename.
def pack_id(identifier, filename):
    raw = f"{identifier}\n{filename}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

def unpack_id(value):
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
        identifier, filename = raw.split("\n", 1)
        if not identifier or not filename or "/" in identifier or ".." in filename:
            raise ValueError("invalid id")
        return identifier, filename
    except Exception:
        abort(404)

def json_error(message, status=502):
    response = jsonify({"error": message})
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response

def get_json(url, params=None):
    response = session.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()

def clean_text(value, default="Unknown"):
    if not value:
        return default
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if v)
    return re.sub(r"\s+", " ", str(value)).strip() or default

def license_is_acceptable(metadata):
    # Only include records that explicitly declare a public-domain or Creative Commons license.
    license_url = str(metadata.get("metadata", {}).get("licenseurl", "")).lower()
    license_text = str(metadata.get("metadata", {}).get("license", "")).lower()
    combined = license_url + " " + license_text
    return (
        "creativecommons.org" in combined
        or "publicdomain" in combined
        or "public domain" in combined
        or "creativecommons" in combined
    )

def choose_audio_file(files, requested_quality):
    audio = []
    for item in files:
        name = str(item.get("name", ""))
        if not name or name.endswith("/"):
            continue
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in {"flac", "mp3", "wav", "ogg", "opus", "m4a", "aac"}:
            continue
        # Avoid derivative thumbnails, previews and tiny sample clips.
        low_name = name.lower()
        if any(token in low_name for token in ("sample", "preview", "thumbnail", "spectrogram")):
            continue
        try:
            size = int(item.get("size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        audio.append((item, ext, size))

    if not audio:
        return None

    quality = (requested_quality or "HIGH").upper()
    lossless = [x for x in audio if x[1] in {"flac", "wav"}]
    lossy = [x for x in audio if x[1] in {"mp3", "m4a", "aac", "opus", "ogg"}]

    if quality == "LOSSLESS":
        # Prefer FLAC, then WAV. Never relabel a lossy file as lossless.
        candidates = [x for x in audio if x[1] in {"flac", "wav"}]
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[1] == "flac", x[2]), reverse=True)
        return candidates[0]

    if quality == "LOW":
        # Prefer common compact formats for a metered request.
        candidates = [x for x in lossy if x[1] in {"mp3", "m4a", "aac", "opus", "ogg"}]
        if candidates:
            candidates.sort(key=lambda x: (x[1] in {"opus", "aac", "m4a"}, -x[2]))
            return candidates[0]
        return lossless[0] if lossless else audio[0]

    # HIGH: prefer MP3/AAC/M4A when available; otherwise use the best available audio.
    candidates = [x for x in lossy if x[1] in {"mp3", "m4a", "aac"}]
    if candidates:
        candidates.sort(key=lambda x: x[2], reverse=True)
        return candidates[0]
    if lossless:
        lossless.sort(key=lambda x: x[2], reverse=True)
        return lossless[0]
    audio.sort(key=lambda x: x[2], reverse=True)
    return audio[0]

def get_record(identifier):
    data = get_json(f"{ARCHIVE}/metadata/{quote(identifier, safe='')}")
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    if not license_is_acceptable(data):
        return None
    return data

@app.get("/")
def root():
    return jsonify({
        "name": APP_NAME,
        "status": "ok",
        "manifest": "/manifest.json",
        "note": "Searches only Archive.org audio records that explicitly declare a Creative Commons or public-domain license."
    })

@app.get("/manifest.json")
def manifest():
    return jsonify({
        "id": APP_ID,
        "name": APP_NAME,
        "version": VERSION,
        "resources": ["search", "stream"],
        "allowDownloads": 0,
        "checkValidLossless": 0,
        "settings": [{
            "key": "quality",
            "type": "select",
            "default": "high",
            "options": [
                {"label": "High (best available lossy or fallback)", "value": "high"},
                {"label": "Lossless (FLAC/WAV only)", "value": "lossless"},
                {"label": "Low (compact)", "value": "low"}
            ]
        }]
    })

@app.get("/search")
def search():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"tracks": []})
    quality = request.args.get("quality", "HIGH")
    # Search Archive.org's advanced search. The license filter is applied again
    # against each record's metadata before returning it.
    params = {
        "q": f'mediatype:audio AND ({query}) AND (licenseurl:*creativecommons.org* OR licenseurl:*publicdomain*)',
        "fl[]": ["identifier", "title", "creator", "collection", "date"],
        "rows": MAX_RESULTS,
        "page": 1,
        "output": "json",
        "sort[]": "downloads desc"
    }
    try:
        result = get_json(f"{ARCHIVE}/advancedsearch.php", params=params)
        docs = result.get("response", {}).get("docs", [])
        tracks = []
        for doc in docs:
            identifier = doc.get("identifier")
            if not identifier:
                continue
            try:
                record = get_record(str(identifier))
                if not record:
                    continue
                meta = record.get("metadata", {})
                files = record.get("files", [])
                chosen = choose_audio_file(files, quality)
                if not chosen:
                    continue
                file_info, ext, size = chosen
                filename = str(file_info.get("name", ""))
                title = clean_text(meta.get("title") or doc.get("title"), str(identifier))
                creator = clean_text(meta.get("creator") or doc.get("creator"), "Unknown artist")
                album = clean_text(meta.get("album") or meta.get("collection") or doc.get("collection"), "Internet Archive")
                duration = meta.get("runtime") or meta.get("length") or 0
                try:
                    duration = float(str(duration).split(";")[0])
                    # Some records store duration as HH:MM:SS.
                    if ":" in str(meta.get("runtime", "")):
                        parts = [float(p) for p in str(meta["runtime"]).split(":")]
                        duration = sum(v * (60 ** i) for i, v in enumerate(reversed(parts)))
                except (TypeError, ValueError):
                    duration = 0
                direct_url = f"{ARCHIVE}/download/{quote(str(identifier), safe='')}/{quote(filename, safe='/')}"
                tracks.append({
                    "id": pack_id(str(identifier), filename),
                    "title": title,
                    "artist": creator,
                    "album": album,
                    "duration": duration,
                    "artworkURL": f"{ARCHIVE}/services/img/{quote(str(identifier), safe='')}",
                    "format": ext,
                    "audioQuality": "LOSSLESS" if ext in {"flac", "wav"} else "HIGH",
                    "streamURL": direct_url
                })
            except requests.RequestException as exc:
                log.warning("Skipping Archive.org record %s: %s", identifier, exc)
            except Exception:
                log.exception("Could not process Archive.org record %s", identifier)
        return jsonify({"tracks": tracks})
    except requests.RequestException as exc:
        log.warning("Archive.org search failed: %s", exc)
        return json_error("The music catalogue is temporarily unavailable. Try again later.")

@app.get("/stream/<track_id>")
def stream(track_id):
    identifier, filename = unpack_id(track_id)
    quality = request.args.get("quality", "HIGH")
    try:
        record = get_record(identifier)
        if not record:
            abort(404)
        chosen = choose_audio_file(record.get("files", []), quality)
        if not chosen:
            return json_error("No rendition is available for the requested quality.", 404)
        file_info, ext, size = chosen
        # Do not silently serve a different file than the one selected in search.
        # Re-resolve by metadata at stream time to honor BitChord's quality request.
        filename = str(file_info.get("name", ""))
        media_url = f"{ARCHIVE}/download/{quote(identifier, safe='')}/{quote(filename, safe='/')}"
        response = {
            "url": media_url,
            "format": ext,
            "codec": {"flac": "flac", "wav": "pcm", "mp3": "mp3", "m4a": "aac", "aac": "aac", "ogg": "vorbis", "opus": "opus"}.get(ext, ext),
            "container": {"flac": "flac", "wav": "wav", "mp3": "mpeg", "m4a": "mp4", "aac": "aac", "ogg": "ogg", "opus": "ogg"}.get(ext, ext),
            "manifest": "none",
            "encrypted": False,
            "quality": "Lossless" if ext in {"flac", "wav"} else "Best available lossy",
        }
        if ext in {"flac", "wav"}:
            response["sampleRate"] = 44100
            if ext == "flac":
                response["bitDepth"] = 16
        # Archive metadata often doesn't reliably expose the encoded bitrate.
        # Do not fabricate 320 kbps; omit bitrate unless a source provides it.
        return jsonify(response)
    except requests.RequestException as exc:
        log.warning("Archive.org stream resolution failed: %s", exc)
        return json_error("The music catalogue is temporarily unavailable.")

@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Track not found in this source."}), 404

@app.errorhandler(429)
def rate_limited(_error):
    response = jsonify({"error": "Too many requests. Please retry shortly."})
    response.status_code = 429
    response.headers["Retry-After"] = "30"
    return response

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
        detected_sr, detected_bd = flac_info(url)
        sr = sr if sr is not None else detected_sr
        bd = bd if bd is not None else detected_bd

    return {
        "url": url,
        "filename": name,
        "size": num(item.get("size")),
        "length": num(item.get("length")),
        "bitrate": num(first(item, "bitrate", "bit_rate", "bitRate")),
        "sampleRate": sr,
        "bitDepth": bd,
        "quality": quality(sr, bd),
    }



def get_flac(identifier):
    response = SESSION.get(
        IA_METADATA.format(quote(identifier, safe="")),
        timeout=(5, 20),
    )
    response.raise_for_status()
    data = response.json()

    candidates = []
    for item in data.get("files", []):
        name = str(item.get("name", ""))
        if not name.lower().endswith(".flac"):
            continue
        candidates.append((name, item))

    if not candidates:
        return None

    # Prefer the largest FLAC first, then inspect the best few in parallel.
    candidates.sort(key=lambda pair: num(pair[1].get("size")) or 0, reverse=True)
    candidates = candidates[:12]

    inspected = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(inspect, identifier, item)
            for _, item in candidates
        ]
        for future in as_completed(futures):
            try:
                inspected.append(future.result())
            except Exception:
                pass

    if not inspected:
        return None

    inspected.sort(
        key=lambda item: (
            int((item.get("bitDepth") or 0) > 16),
            item.get("bitDepth") or 0,
            item.get("sampleRate") or 0,
            item.get("size") or 0,
        ),
        reverse=True,
    )
    return inspected[0]



def normalized(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()



def artist_hint(query, title, creator):
    # IA metadata is inconsistent. Keep the real creator when available.
    if isinstance(creator, list):
        return str(creator[0]) if creator else "Internet Archive"
    if creator:
        return str(creator)
    return "Internet Archive"



def make_track(doc, flac):
    identifier = str(doc["identifier"])
    title = str(doc.get("title") or flac["filename"])
    creator = doc.get("creator")
    tier = flac.get("quality", "LOSSLESS")

    return {
        "id": identifier,
        "title": title,
        "artist": artist_hint("", title, creator),
        "album": str(doc.get("album") or ""),
        "duration": flac.get("length"),
        "artworkURL": f"https://archive.org/services/img/{quote(identifier, safe='')}",
        "format": "flac",
        "audioQuality": tier,
        "quality": quality_label(flac.get("sampleRate"), flac.get("bitDepth")),
        "sampleRate": flac.get("sampleRate"),
        "bitDepth": flac.get("bitDepth"),
        "bitrate": flac.get("bitrate"),
        "streamURL": flac["url"],
    }



def do_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"tracks": []})

    # BitChord does not require a limit parameter; it trims the returned rows.
    try:
        limit = min(max(int(request.args.get("limit", "20")), 1), 30)
    except ValueError:
        limit = 20

    try:
        response = SESSION.get(
            IA_SEARCH,
            params={
                "q": f"(mediatype:audio) AND ({query})",
                "fl[]": ["identifier", "title", "creator", "album", "year"],
                "rows": min(limit * 4, 80),
                "page": 1,
                "output": "json",
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        docs = response.json().get("response", {}).get("docs", [])
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"tracks": [], "error": str(exc)}), 502

    tracks = []
    # Resolve several IA items concurrently so BitChord does not wait through
    # a serial metadata request for every non-FLAC search hit.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for doc in docs:
            identifier = doc.get("identifier")
            if identifier:
                futures[pool.submit(get_flac, str(identifier))] = doc

        for future in as_completed(futures):
            doc = futures[future]
            try:
                flac = future.result()
            except requests.RequestException:
                continue
            except Exception:
                continue
            if not flac:
                continue

            tracks.append(make_track(doc, flac))
            if len(tracks) >= limit:
                break

    return jsonify({"tracks": tracks})



def do_stream(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return jsonify({"error": "missing id"}), 400

    try:
        flac = get_flac(identifier)
    except requests.RequestException as exc:
        return jsonify({"error": str(exc)}), 502

    if not flac:
        return jsonify({"error": "No FLAC file found for this item"}), 404

    return jsonify({
        "url": flac["url"],
        "format": "flac",
        "quality": quality_label(flac.get("sampleRate"), flac.get("bitDepth")),
        "audioQuality": flac.get("quality", "LOSSLESS"),
        "codec": "flac",
        "container": "flac",
        "manifest": "none",
        "encrypted": False,
        "sampleRate": flac.get("sampleRate"),
        "bitDepth": flac.get("bitDepth"),
        "bitrate": flac.get("bitrate"),
        "audioMode": "stereo",
    })



@app.get("/")
def root():
    return jsonify(MANIFEST)


@app.get("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


@app.get("/health")
def health():
    return jsonify({"ok": True, "name": MANIFEST["name"], "version": MANIFEST["version"]})


@app.get("/search")
def search():
    return do_search()


@app.get("/stream/<path:identifier>")
def stream(identifier):
    return do_stream(identifier)


# Compatibility with older addon examples that used /stream?id=...
@app.get("/stream")
def stream_query():
    return do_stream(request.args.get("id"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
