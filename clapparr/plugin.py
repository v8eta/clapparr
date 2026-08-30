"""Clapparr - the metadata slate for Dispatcharr DVR recordings.

Writes Kodi/Plex sidecar metadata (NFOs, posters, thumbnails) so recordings
present with real titles, summaries and artwork. Named for the clapperboard:
the slate that identifies footage is exactly what this writes for recordings.

Dispatcharr records the file; media servers then have to guess what it is. Plex's
TV scanner takes the episode index from the FILENAME and its NFO agent skips any
episode it could not index, so a recording with no SxxExx in its name shows up as
"Episode 08-18" with no title, no summary and no artwork -- even though
Dispatcharr already holds the title, sub-title, description, season, episode and
a poster URL on the Recording row.

This plugin writes that metadata back out beside the file as Kodi-style sidecars:

    <show>/tvshow.nfo                  <tvshow>    - once per show
    <show>/poster.jpg                  poster      - from the EPG poster_url
    <show>/<recording>.nfo             <episodedetails>
    <show>/<recording>-thumb.jpg       episode still

Consumed by Plex (agent "Plex TV Series Agent (NFO)"), Emby, Jellyfin and Kodi.

TIMING -- why this defers to a thread
-------------------------------------
The `recording_end` event fires from inside `run_recording`, BEFORE that same
task remuxes the .ts into its final .mkv and writes `file_path` onto the
Recording. Blocking the handler until the file appears would therefore deadlock
the very task that produces it.

Dispatcharr's own plugin documentation confirms the mechanism: actions with an
`events` list are dispatched from `log_system_event()` "on a separate gevent
when uWSGI has an active hub (otherwise synchronously, e.g. Celery)". Recording
runs on a Celery worker, so this handler is called SYNCHRONOUSLY, in-line with
the recording task -- the deadlock is real, not theoretical.

The handler therefore hands off to a daemon thread which waits for
`status == completed` and then writes the sidecars, returning immediately. That
thread calls `close_old_connections()` in its own `finally` block, as the same
documentation requires of any thread a plugin spawns that touches the ORM.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.dom import minidom

PLUGIN_KEY = "clapparr"

# How often the waiter re-checks for the finished file.
_POLL_SECONDS = 10

# Frames sampled by ffmpeg's `thumbnail` filter when picking a still. The filter
# scores each frame in the batch against the batch average and returns the most
# representative one, which is what keeps us off blank fades and hard cuts.
_THUMB_BATCH = 100

# --- artwork fallback ------------------------------------------------------
# Dispatcharr resolves posters with TVmaze `singlesearch` on the EXACT EPG
# title, so a variant title returns nothing at all and the recording is stored
# with no poster_url -- "Highway Patrol Special" finds nothing because TVmaze
# carries the series as "Highway Patrol".
#
# Re-querying the fuzzy `search` endpoint fixes that, but loose title matching
# is exactly how the wrong show's artwork gets attached to a recording. A
# candidate is therefore accepted only if it clears ALL of these:
#
#   * its name is a contiguous WORD-PREFIX of the EPG title -- "highway patrol"
#     of "highway patrol special" -- never a loose character overlap
#   * that name is at least 2 words and 8 characters, so a bare base like
#     "News" can never swallow "News Special"
#   * those words cover at least `fuzzy_min_coverage` of the EPG title
#   * it actually carries an image
#   * exactly one candidate survives. TVmaze lists both an AU and a US
#     "Highway Patrol", identically named, so a tie is genuinely ambiguous and
#     is refused unless a country preference separates them.
#
# Every decision is logged with its reason, so a wrong poster is traceable to
# the rule that let it through rather than being silently attached.
_TVMAZE_SEARCH = "https://api.tvmaze.com/search/shows?q="
_FUZZY_MIN_WORDS = 2
_FUZZY_MIN_CHARS = 8
# TVmaze returns nothing at all for a variant title rather than degrading to the
# base series, so the query is trimmed a word at a time. Capped so an unmatched
# show costs a bounded number of calls.
_FUZZY_MAX_QUERIES = 3


def _norm_words(name):
    """Lowercase word list with punctuation dropped: 'The Block (AU)' -> [the, block, au]."""
    return [w for w in re.split(r"[^0-9a-z]+", (name or "").lower()) if w]


def _rewrite_path(path, frm, to):
    """Translate a container path into whatever the notified service sees.

    Dispatcharr writes to /data/recordings/... while the media server may know
    the same file as /mnt/unionfs/dvr/..., so a scan request carrying the raw
    container path finds nothing.
    """
    if frm and path.startswith(frm):
        return to + path[len(frm):]
    return path


def _show_country(show):
    """Two-letter country for a TVmaze show, via its network or web channel."""
    for key in ("network", "webChannel"):
        node = show.get(key) or {}
        code = ((node.get("country") or {}).get("code") or "").upper()
        if code:
            return code
    return ""


def _fuzzy_show_match(title, results, min_coverage, country):
    """Choose at most one TVmaze show for an EPG title.

    Returns (show_or_None, reason). The reason is always populated so the
    caller can log why a poster was or was not attached.
    """
    want = _norm_words(title)
    if not want:
        return None, "empty title"
    if not isinstance(results, list):
        return None, "unexpected search response"

    viable = []
    for entry in results:
        show = (entry or {}).get("show") or {}
        if not show.get("image"):
            continue
        got = _norm_words(show.get("name"))
        if len(got) < _FUZZY_MIN_WORDS or len("".join(got)) < _FUZZY_MIN_CHARS:
            continue
        if want[:len(got)] != got:
            continue
        coverage = len(got) / float(len(want))
        if coverage < min_coverage:
            continue
        viable.append((coverage, show))

    if not viable:
        return None, "no candidate cleared the guards"

    if country:
        preferred = [v for v in viable if _show_country(v[1]) == country]
        if preferred:
            viable = preferred

    viable.sort(key=lambda v: -v[0])
    if len(viable) > 1 and viable[0][0] == viable[1][0]:
        return None, "ambiguous - %s match equally well" % ", ".join(
            "%s (%s)" % (v[1].get("name"), _show_country(v[1]) or "??")
            for v in viable[:3]
        )

    best_coverage, best = viable[0]
    return best, "matched %r (%s), coverage %.2f" % (
        best.get("name"), _show_country(best) or "??", best_coverage,
    )


# --- RPDB (Rating Poster Database) -----------------------------------------
# OPTIONAL, and entirely off unless an API key is set. https://rpdb.apidoc.io
#
# The API is keyed on an IMDB / TMDB / TVDB id and returns the show poster with
# the rating rendered onto it. `?fallback=true` is not optional in practice:
# without it the API returns an ERROR for any title it holds no ratings for,
# which for regional and news programming is most of them. With it, such a title
# still yields a plain poster, so enabling RPDB can never leave a show with less
# artwork than it had.
_RPDB_BASE = "https://api.ratingposterdb.com"
_RPDB_TYPES = ("poster-default", "poster-certs", "poster-mc", "poster-rt",
               "textless-default", "textless-certs", "textless-mc",
               "textless-rt")

# A poster is portrait. Anything wider than it is tall is a banner or an episode
# still that a provider filed under the poster key, and Plex will letterbox it
# into a poster slot. Below _MIN it is a thumbnail and not worth writing; at or
# above _GOOD it is good enough to stop looking rather than spend another fetch.
_POSTER_MIN_W = 200
_POSTER_GOOD_W = 600


def _rm(path):
    """Delete if present. Cleanup must never mask the error that caused it."""
    try:
        os.remove(path)
    except OSError:
        pass


def _tvdb_id(custom_props):
    """TVDB series id from an EPG row, or "".

    ⭐ ONE derivation, two callers -- the NFO `uniqueid` element and the RPDB
    URL. The EPG stores it as 'series/260092' and both callers need the bare
    number. Deriving it separately in each place is exactly how two routes to
    one value drift apart later.
    """
    raw = str((custom_props or {}).get("thetvdb.com_id") or "").strip()
    return raw.rsplit("/", 1)[-1] if raw else ""


def _image_dims(path):
    """(width, height) of a JPEG or PNG, or None if it is neither/unreadable.

    Header only, stdlib only. Deliberately NOT Pillow: this plugin has no image
    dependency today and should not acquire one to read two integers. Reads a
    few KB at most and never decodes pixels.
    """
    try:
        with open(path, "rb") as fh:
            if fh.read(2) == b"\xff\xd8":                     # JPEG
                while True:
                    b = fh.read(1)
                    while b and b != b"\xff":
                        b = fh.read(1)
                    if not b:
                        return None
                    marker = fh.read(1)
                    while marker == b"\xff":                   # fill bytes
                        marker = fh.read(1)
                    if not marker:
                        return None
                    m = marker[0]
                    if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:  # no length field
                        continue
                    # SOF0..SOF15 carry the dimensions; DHT/JPG/DAC do not.
                    if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                        fh.read(3)                             # length, precision
                        h = int.from_bytes(fh.read(2), "big")
                        w = int.from_bytes(fh.read(2), "big")
                        return (w, h) if w and h else None
                    seg = int.from_bytes(fh.read(2), "big")
                    if seg < 2:
                        return None
                    fh.seek(seg - 2, 1)
            fh.seek(0)
            if fh.read(8) == b"\x89PNG\r\n\x1a\n":             # PNG IHDR
                fh.seek(16)
                w = int.from_bytes(fh.read(4), "big")
                h = int.from_bytes(fh.read(4), "big")
                return (w, h) if w and h else None
    except Exception:
        return None
    return None


def _poster_grade(path):
    """(ok, width, why) for a downloaded poster candidate.

    Portrait-or-square and wide enough. A landscape image is rejected outright
    rather than written and left for a human to notice in Plex months later.
    """
    dims = _image_dims(path)
    if not dims:
        return False, 0, "not a readable JPEG/PNG"
    w, h = dims
    if w > h:
        return False, w, "landscape %dx%d (a poster is portrait)" % (w, h)
    if w < _POSTER_MIN_W:
        return False, w, "only %dpx wide" % w
    return True, w, "%dx%d" % (w, h)


def _text(parent, tag, value):
    """Append <tag>value</tag> when value is meaningful. Skips None/blank."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    el = ET.SubElement(parent, tag)
    el.text = value
    return el


def _pretty(root):
    raw = ET.tostring(root, encoding="utf-8")
    dom = minidom.parseString(raw)
    body = dom.documentElement.toprettyxml(indent="  ")
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n' + body


def _match_dir_owner(path):
    """Give a newly written file the same ownership as the directory holding it.

    Dispatcharr's container runs as root, so sidecars land root-owned beside
    media owned by the media user. Everything still *reads*, which is why this
    goes unnoticed, but any later tooling running as that user cannot move or
    delete the sidecars it is supposed to manage.

    Best effort: silently does nothing where it cannot (unprivileged container,
    non-POSIX filesystem, squashed NFS root).
    """
    try:
        want = os.stat(os.path.dirname(path))
        have = os.stat(path)
        if (have.st_uid, have.st_gid) == (want.st_uid, want.st_gid):
            return False
        os.chown(path, want.st_uid, want.st_gid)
        return True
    except (OSError, AttributeError):
        return False


def _write(path, content, overwrite):
    if os.path.exists(path) and not overwrite:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)
    _match_dir_owner(path)
    return True


def _run(cmd, timeout=120):
    """Run a subprocess, returning (ok, stdout). Never raises."""
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False,
        )
        return p.returncode == 0, (p.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return False, ""


def _duration(path):
    ok, out = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", path,
    ], timeout=60)
    if not ok:
        return None
    try:
        return float(out.strip())
    except (TypeError, ValueError):
        return None


class Plugin:
    name = "Clapparr"
    # ⚠️ Must match plugin.json's "version". Dispatcharr reads THIS constant for
    # what it displays, while the catalog reads plugin.json -- so a drift shows
    # users one version and tells the installer another. It drifted silently for
    # two releases (v1.5.0 and v1.6.0 both still shipped "1.4.1", including
    # inside their zips) because nothing compared the two. test_version_sync.py
    # now does; do not bump one without the other.
    version = "1.6.0"
    description = (
        "The metadata slate for your DVR: writes Kodi/Plex NFO sidecars, "
        "posters and episode thumbnails so recordings present with real "
        "titles, summaries and artwork instead of 'Episode 08-18'."
    )
    author = "v8eta"
    help_url = "https://github.com/v8eta/clapparr#readme"

    fields = [
        {
            "id": "_sec_what",
            "label": "What to write",
            "type": "info",
            "description": (
                "Sidecars are written next to the recording. Plex needs the "
                "library's agent set to 'Plex TV Series Agent (NFO)' to read them."
            ),
        },
        {
            "id": "auto_on_recording_end",
            "label": "Generate automatically after each recording",
            "type": "boolean",
            "default": True,
            "description": (
                "Runs when a recording finishes. Waits for the remux to complete "
                "first, in the background."
            ),
        },
        {
            "id": "write_tvshow_nfo",
            "label": "Write tvshow.nfo per show folder",
            "type": "boolean",
            "default": True,
        },
        {
            "id": "write_poster",
            "label": "Download show poster.jpg",
            "type": "boolean",
            "default": True,
            "description": "Uses the poster URL Dispatcharr already stored on the recording.",
        },
        {
            "id": "artwork_fuzzy_fallback",
            "label": "Look up artwork on TVmaze when the recording has none",
            "type": "boolean",
            "default": True,
            "description": (
                "Dispatcharr matches TVmaze on the exact EPG title, so variant "
                "titles ('Highway Patrol Special' vs 'Highway Patrol') get no "
                "poster at all. Only a candidate whose name is a word-prefix of "
                "the title is accepted, and an ambiguous tie is refused."
            ),
        },
        {
            "id": "fuzzy_min_coverage",
            "label": "Minimum title coverage for a fuzzy match",
            "type": "number",
            "default": 0.6,
            "description": (
                "How much of the EPG title the matched show name must account "
                "for. 'Highway Patrol' covers 0.67 of 'Highway Patrol Special'. "
                "Raise toward 1.0 to accept only near-exact names."
            ),
        },
        {
            "id": "tvmaze_country",
            "label": "Prefer shows from this country (2-letter code)",
            "type": "string",
            "default": "",
            "description": (
                "Breaks ties between identically named shows — TVmaze lists an "
                "AU and a US 'Highway Patrol'. Left blank, such a tie is "
                "rejected as ambiguous rather than guessed."
            ),
        },
        {
            "id": "rpdb_api_key",
            "label": "RPDB API key",
            "type": "string",
            "default": "",
            "input_type": "password",
            "description": (
                "Optional, and off while blank. With a key, the show poster is "
                "fetched from the Rating Poster Database "
                "(https://ratingposterdb.com) with the rating rendered onto it. "
                "Needs a paid RPDB subscription; keys look like 't1-…'. One "
                "request per show, and the poster is written once and reused, "
                "so a whole library costs very few requests. If RPDB has no "
                "artwork for a show, the normal poster sources are used instead."
            ),
        },
        {
            "id": "rpdb_poster_type",
            "label": "RPDB poster style",
            "type": "string",
            "default": "poster-default",
            "description": (
                "poster-default, poster-certs, poster-mc, poster-rt, or any of "
                "those with a 'textless-' prefix, which prefers artwork without "
                "the show title burned in. The lowest RPDB tier supports only "
                "poster-default; higher tiers support all eight. An unrecognised "
                "value falls back to poster-default. Ignored with no API key."
            ),
        },
        {
            "id": "_sec_thumb",
            "label": "Episode thumbnails",
            "type": "info",
            "description": (
                "A still saved as <recording>-thumb.jpg. 'Representative' lets "
                "ffmpeg pick the most representative frame from a sampled batch, "
                "which avoids fades and blank frames."
            ),
        },
        {
            "id": "thumbnail_mode",
            "label": "Thumbnail",
            "type": "select",
            "default": "representative",
            "options": [
                {"value": "off", "label": "Do not generate"},
                {"value": "fixed", "label": "Fixed position"},
                {"value": "representative", "label": "Representative frame (recommended)"},
            ],
        },
        {
            "id": "thumbnail_position",
            "label": "Position through the recording (%)",
            "type": "number",
            "default": 40,
            "description": "Where to sample. Kept clear of the opening and closing minutes.",
        },
        {
            "id": "thumbnail_skip_intro",
            "label": "Skip the first N seconds",
            "type": "number",
            "default": 150,
            "description": "Avoids pre-roll, idents and opening titles.",
        },
        {
            "id": "thumbnail_width",
            "label": "Thumbnail width (px)",
            "type": "number",
            "default": 640,
        },
        {
            "id": "_sec_titles",
            "label": "Titles",
            "type": "info",
            "description": (
                "Daily programmes (news, current affairs) often carry no episode "
                "sub-title in the EPG. This decides what to call those episodes."
            ),
        },
        {
            "id": "title_fallback",
            "label": "When the EPG has no episode title",
            "type": "select",
            "default": "airdate_long",
            "options": [
                {"value": "airdate_long", "label": "Air date, e.g. 'Wednesday 20 August 2026'"},
                {"value": "airdate_short", "label": "Air date, e.g. '2026-08-20'"},
                {"value": "episode_number", "label": "Episode number, e.g. 'Episode 238'"},
                {"value": "show_name", "label": "The show name"},
            ],
        },
        {
            "id": "_sec_webhook",
            "label": "Notify another service after writing",
            "type": "info",
            "description": (
                "Optional. Calls a URL once per recording, after its sidecars "
                "are written — for a scan relay such as autopulse, or anything "
                "else that wants to know a recording is ready. Placeholders: "
                "{path} {dir} {file} {show}."
            ),
        },
        {
            "id": "webhook_url",
            "label": "Webhook URL",
            "type": "string",
            "default": "",
            "description": (
                "Left blank, nothing is called. Example for autopulse: "
                "http://user:pass@autopulse:2875/triggers/manual?path={path} — "
                "credentials in the URL are converted to an auth header and are "
                "never written to the log. Stored in plain text in the plugin "
                "config, so prefer a scoped credential."
            ),
        },
        {
            "id": "webhook_method",
            "label": "Method",
            "type": "select",
            "default": "GET",
            "options": [
                {"value": "GET", "label": "GET (autopulse and most relays)"},
                {"value": "POST", "label": "POST (sends the fields as JSON)"},
            ],
        },
        {
            "id": "webhook_path_from",
            "label": "Rewrite path prefix — from",
            "type": "string",
            "default": "",
            "description": (
                "Dispatcharr's own path, e.g. /data/recordings/TV_Shows. Leave "
                "both blank if the other service sees the same paths."
            ),
        },
        {
            "id": "webhook_path_to",
            "label": "Rewrite path prefix — to",
            "type": "string",
            "default": "",
            "description": "What the notified service calls it, e.g. /mnt/unionfs/dvr.",
        },
        {
            "id": "webhook_header",
            "label": "Extra header (optional)",
            "type": "string",
            "input_type": "password",
            "default": "",
            "description": "One header as 'Name: value', e.g. 'X-Api-Key: abc123'.",
        },
        {
            "id": "_sec_plex",
            "label": "Plex artwork refresh",
            "type": "info",
            "description": (
                "Optional, Plex-specific. A library scan re-reads episode NFOs "
                "but IGNORES a show's poster.jpg, so when the poster changes "
                "Plex keeps serving the old one until that show's metadata is "
                "refreshed. Fill these in and the plugin does it for you."
            ),
        },
        {
            "id": "plex_url",
            "label": "Plex base URL",
            "type": "string",
            "default": "",
            "description": "e.g. http://plex:32400 — leave blank to disable.",
        },
        {
            "id": "plex_token",
            "label": "Plex token",
            "type": "string",
            "input_type": "password",
            "default": "",
            "description": "X-Plex-Token. Sent as a header, never in a URL.",
        },
        {
            "id": "_sec_behaviour",
            "label": "Behaviour",
            "type": "info",
            "description": "",
        },
        {
            "id": "overwrite",
            "label": "Overwrite sidecars that already exist",
            "type": "boolean",
            "default": False,
            "description": "Off by default so hand-edited metadata is never clobbered.",
        },
        {
            "id": "wait_minutes",
            "label": "Wait up to N minutes for the remux",
            "type": "number",
            "default": 30,
            "description": (
                "A recording is not on disk at its final path until the remux "
                "finishes. Long recordings take longer."
            ),
        },
        {
            "id": "recordings_root",
            "label": "Recordings root (inside the container)",
            "type": "string",
            "default": "/data/recordings",
            "description": "Only used to sanity-check paths before writing.",
        },
    ]

    actions = [
        {
            "id": "on_recording_start",
            "label": "Handle recording_start",
            "description": (
                "Internal. Fires automatically when a recording begins, and "
                "captures the episode name from the EPG into the recording "
                "while that row is certainly still present."
            ),
            "events": ["recording_start"],
        },
        {
            "id": "on_recording_end",
            "label": "Handle recording_end",
            "description": "Internal. Fires automatically when a recording finishes.",
            "events": ["recording_end"],
        },
        {
            "id": "generate_missing",
            "label": "Generate missing sidecars",
            "description": "Scan every completed recording and write anything absent.",
            "button_label": "Generate",
        },
        {
            "id": "regenerate_all",
            "label": "Regenerate all sidecars",
            "description": "Rewrite every sidecar, replacing existing files.",
            "button_label": "Regenerate",
            "button_variant": "outline",
            "confirm": {
                "title": "Regenerate all sidecars?",
                "message": "Existing .nfo, poster and thumbnail files will be overwritten.",
            },
        },
        {
            "id": "preview",
            "label": "Preview (no files written)",
            "description": "Show what would be written for each recording.",
            "button_label": "Preview",
            "button_variant": "outline",
        },
        {
            "id": "refresh_plex_artwork",
            "label": "Refresh show artwork in Plex",
            "description": (
                "Force a metadata refresh of every show that has a poster, so "
                "Plex re-reads poster.jpg. A library scan does not do this."
            ),
            "button_label": "Refresh artwork",
            "button_variant": "outline",
        },
        {
            "id": "test_webhook",
            "label": "Test the webhook",
            "description": (
                "Fires the configured webhook against your most recent "
                "recording, so the URL and any path rewrite can be checked "
                "without waiting for one to finish."
            ),
            "button_label": "Send test",
            "button_variant": "outline",
        },
    ]

    def __init__(self):
        # One TVmaze answer per show name per process, negatives included, so a
        # bulk regenerate over 30 recordings of the same show is a single call.
        # title -> matched TVmaze show dict, or None if nothing matched. One
        # cache, because artwork and external ids come from the SAME match:
        # resolving them separately could pair one show's poster with another
        # show's ratings.
        self._tvmaze_cache = {}
        # Same idea for Plex show ratingKeys.
        self._plex_key_cache = {}
        # A bulk regenerate touches every episode of a show, but the show only
        # needs refreshing once for its poster.
        self._refreshed_shows = set()

    # ---------------------------------------------------------------- helpers

    def _settings(self, context):
        s = dict(context.get("settings") or {})

        def num(key, default):
            try:
                return float(s.get(key, default))
            except (TypeError, ValueError):
                return default

        return {
            "auto": bool(s.get("auto_on_recording_end", True)),
            "tvshow": bool(s.get("write_tvshow_nfo", True)),
            "poster": bool(s.get("write_poster", True)),
            "thumb_mode": s.get("thumbnail_mode", "representative"),
            "thumb_pos": max(1.0, min(99.0, num("thumbnail_position", 40))),
            "thumb_skip": max(0.0, num("thumbnail_skip_intro", 150)),
            "thumb_width": int(max(160, num("thumbnail_width", 640))),
            "title_fallback": s.get("title_fallback", "airdate_long"),
            "overwrite": bool(s.get("overwrite", False)),
            "wait_s": max(0.0, num("wait_minutes", 30)) * 60.0,
            "root": (s.get("recordings_root") or "/data/recordings").rstrip("/"),
            "fuzzy": bool(s.get("artwork_fuzzy_fallback", True)),
            "fuzzy_coverage": max(0.0, min(1.0, num("fuzzy_min_coverage", 0.6))),
            "country": (s.get("tvmaze_country") or "").strip().upper(),
            "webhook_url": (s.get("webhook_url") or "").strip(),
            "webhook_method": (s.get("webhook_method") or "GET").strip().upper(),
            "webhook_from": (s.get("webhook_path_from") or "").rstrip("/"),
            "webhook_to": (s.get("webhook_path_to") or "").rstrip("/"),
            "webhook_header": (s.get("webhook_header") or "").strip(),
            "plex_url": (s.get("plex_url") or "").strip().rstrip("/"),
            "plex_token": (s.get("plex_token") or "").strip(),
            # Blank key = RPDB disabled entirely; no request is ever made.
            "rpdb_key": (s.get("rpdb_api_key") or "").strip(),
            # An unrecognised style degrades to the one every tier supports,
            # rather than 404ing on every show until someone reads a log.
            "rpdb_type": (
                (s.get("rpdb_poster_type") or "").strip()
                if (s.get("rpdb_poster_type") or "").strip() in _RPDB_TYPES
                else "poster-default"),
        }

    def _fresh_sub_title(self, prog, log=None):
        """Episode NAME the EPG holds NOW for this airing, or None.

        The exact counterpart of _fresh_plot, and it exists for the exact same
        reason its docstring gives: the snapshot on the Recording was taken when
        the recording was SCHEDULED, and a provider publishes the episode name
        as late as it publishes the synopsis.

        ⛔ Without this the two halves of the same EPG row were treated
        differently: the plot was re-read and correct, while the title fell back
        to a date. Real NFOs carried
            title "Wednesday 26 August 2026"
            plot  "The teams are having problems with their trades..."
        while the EPG held sub_title "Main Ensuite Week" for that very slot.

        Same matching rules as _fresh_plot, deliberately -- tvg_id AND title AND
        a tight window. Title is not optional: matching on tvg_id plus a window
        selects the NEIGHBOURING programme, which is how a 10 News+ recording
        was once handed The Bold and the Beautiful's synopsis.
        """
        try:
            from datetime import timedelta

            from django.utils.dateparse import parse_datetime

            from apps.epg.models import ProgramData

            tvg = (prog.get("tvg_id") or "").strip()
            title = (prog.get("title") or "").strip()
            start = prog.get("start_time")
            if not (tvg and title and start):
                return None
            st = parse_datetime(str(start)) if not hasattr(start, "year") else start
            if st is None:
                return None

            win = timedelta(minutes=self.EPG_WINDOW_MIN)
            best = None
            for r in ProgramData.objects.filter(
                    tvg_id=tvg, title=title,
                    start_time__gte=st - win, start_time__lte=st + win):
                sub = (getattr(r, "sub_title", "") or "").strip()
                if not sub:
                    # ⭐ The thin feed leaves sub_title EMPTY and puts the name on
                    # the FIRST LINE of description instead:
                    #     'Main Ensuite Week\nThe teams are picking tiles...'
                    # Reading only sub_title misses those rows entirely -- measured
                    # on The Block, 11 of 23 rows carry sub_title and the rest use
                    # this shape.
                    #
                    # ⚠️ Only when there IS a second line. A single-line
                    # description is a synopsis, not a name, and taking it would
                    # put the whole plot in the title field.
                    desc = (getattr(r, "description", "") or "").strip()
                    head, sep, body = desc.partition(chr(10))
                    if sep and body.strip() and 0 < len(head.strip()) <= 80:
                        sub = head.strip()
                if not sub or sub.lower() == "unknown":
                    continue
                # Longest wins, as with the plot: several feeds carry the same
                # slot and only the richer one names the episode. Measured on
                # The Block: 11 of 23 rows carry sub_title, the rest are blank.
                if best is None or len(sub) > len(best):
                    best = sub
            return best
        except Exception as e:  # noqa: BLE001
            # Never let a metadata nicety break the NFO write.
            if log:
                log("clapparr: fresh sub_title lookup failed: %r" % (e,))
            return None

    # Show-level blurbs seen presented AS episode synopses. Compared
    # case-insensitively on a prefix, because feeds truncate them at different
    # lengths. Kept short and specific on purpose -- a broad rule here would
    # start discarding real synopses, which is a worse failure than keeping a
    # generic one.
    _GENERIC_PLOT_HINTS = (
        "is australia's leading current affairs program",
        "what drives people in europe",
    )

    def _is_generic_plot(self, plot, show_plot=""):
        """Is this the SHOW's blurb rather than the EPISODE's synopsis?

        Two independent tests, because neither is sufficient alone:

        1. It equals the series-level description we already hold (tvshow.nfo).
           Exact, and catches any show without needing a hint for it.
        2. It contains a known show-blurb phrase. Catches the case where the
           series description was never captured, or differs in length.

        ⚠️ Deliberately CONSERVATIVE. A false positive discards a real synopsis
        and leaves an episode with no description at all; a false negative just
        leaves today's behaviour. When in doubt this returns False.
        """
        p = (plot or "").strip().lower()
        if not p:
            return False
        sp = (show_plot or "").strip().lower()
        if sp and len(p) > 40 and (p == sp or p.startswith(sp[:120]) or sp.startswith(p[:120])):
            return True
        return any(h in p for h in self._GENERIC_PLOT_HINTS)

    def _episode_title(self, prog, aired, season, episode, show, mode):
        # Snapshot first: if the name was already known at scheduling time it is
        # correct and costs no query.
        sub = (prog.get("sub_title") or "").strip()
        if sub and sub.lower() != "unknown":
            return sub
        # ⭐ Otherwise re-read the EPG, exactly as the plot does. This is the
        # whole fix: the snapshot is usually taken before the provider names the
        # episode, so a blank here does NOT mean the name is unavailable -- only
        # that it was unavailable THEN. Verified on live data: four Block
        # recordings whose snapshot had no title had one in the EPG, including
        # "Main Ensuite Week" and "Living Dining and Alfresco Week".
        #
        # ⚠️ Only useful while the EPG row still exists. Retention is about two
        # days of history, and clapparr runs when a recording FINISHES, so the
        # row is minutes old at write time. The bulk-backfill action runs long
        # after and will still fall through to the date -- correctly, because by
        # then the answer is genuinely gone.
        fresh = self._fresh_sub_title(prog)
        if fresh:
            return fresh
        if mode == "show_name":
            return show
        if mode == "episode_number" and episode:
            return f"Episode {episode}"
        if aired:
            try:
                d = datetime.strptime(aired, "%Y-%m-%d")
                if mode == "airdate_short":
                    return aired
                return d.strftime("%A %d %B %Y").replace(" 0", " ")
            except ValueError:
                pass
        return aired or show

    def _recording_rows(self):
        """Completed recordings that have a file on disk, newest first."""
        from apps.channels.models import Recording

        rows = []
        for rec in Recording.objects.all().order_by("-id"):
            cp = rec.custom_properties or {}
            path = cp.get("file_path")
            if not path:
                continue
            rows.append((rec, cp, path))
        return rows

    # Deliberately tight. The snapshot's start_time came from the very EPG row
    # being re-read, so real drift is a minute or two; the window exists only to
    # absorb that. It was 30 and is now 10 because a half-hour show that repeats
    # back-to-back (DW Focus On Europe) can put the NEXT episode of the same
    # title inside a 30-minute window, and a confidently-wrong synopsis is worse
    # than a generic one.
    EPG_WINDOW_MIN = 10

    def _fresh_plot(self, prog, log=None):
        """Best description the EPG holds NOW for this airing, or None.

        The snapshot on the Recording was taken when it was scheduled, which for
        a series is usually before the provider published the episode synopsis.
        Re-reading at write time is the whole point of this function.
        """
        try:
            from datetime import timedelta

            from django.utils.dateparse import parse_datetime

            from apps.epg.models import ProgramData

            tvg = (prog.get("tvg_id") or "").strip()
            title = (prog.get("title") or "").strip()
            start = prog.get("start_time")
            if not (tvg and title and start):
                return None
            st = parse_datetime(str(start)) if not hasattr(start, "year") else start
            if st is None:
                return None

            # ⛔ TITLE IS NOT OPTIONAL. Matching only tvg_id + a time window
            #    selects the NEIGHBOURING programme on that channel: tested
            #    without it and a 10 News+ recording was handed the synopsis for
            #    The Bold and the Beautiful, and another got Gogglebox. A wrong
            #    synopsis stated confidently is far worse than a generic one, so
            #    the title must pin the programme and the window only resolves
            #    the small drift between the scheduled start and the EPG row.
            win = timedelta(minutes=self.EPG_WINDOW_MIN)
            rows = ProgramData.objects.filter(
                tvg_id=tvg, title=title,
                start_time__gte=st - win, start_time__lte=st + win,
            )
            # Two tiers, not one. "Longest wins" alone hands back a SHOW blurb
            # whenever the feed carries one, because a marketing paragraph is
            # usually longer than a real episode synopsis -- and a generic blurb
            # presented as an episode plot is a quiet lie: it looks like real
            # metadata and Plex displays it forever.
            #
            # ⚠️ Non-destructive by construction. Generic candidates are only
            # DEPRIORITISED, never discarded: if every row is generic we still
            # return one, so this can improve the answer and never empty it.
            best = None          # best non-generic
            fallback = None      # best generic, used only if nothing else
            for r in rows:
                desc = (r.description or "").strip()
                if not desc:
                    continue
                # Longest wins within a tier: several feeds can carry the same
                # slot, and a thinner source may supply only "E27 Sunday 23
                # August 2026" where a richer one has the actual synopsis.
                if self._is_generic_plot(desc):
                    if fallback is None or len(desc) > len(fallback):
                        fallback = desc
                elif best is None or len(desc) > len(best):
                    best = desc
            if best is None:
                best = fallback
            # Only worth returning if it actually beats what we already have.
            snap = (prog.get("description") or "").strip()
            if best and best == snap:
                return None
            if best and log:
                log.info("clapparr: EPG re-read gave a fresher plot for %s", title)
            return best
        except Exception as exc:  # never let metadata enrichment break the NFO
            if log:
                log.debug("clapparr: EPG re-read skipped (%s)", exc)
            return None

    def _plan(self, cp, path, cfg):
        """Work out every sidecar for one recording. Touches no disk.

        No longer strictly pure: it reads the EPG to find a fresher episode
        synopsis than the scheduling-time snapshot holds. That lookup is
        wrapped so any failure falls back to the snapshot.
        """
        prog = cp.get("program") or {}
        show = (prog.get("title") or "").strip() or os.path.basename(os.path.dirname(path))

        season = cp.get("season") or prog.get("season")
        episode = cp.get("episode") or prog.get("episode")
        try:
            season = int(season) if season not in (None, "") else None
        except (TypeError, ValueError):
            season = None
        try:
            episode = int(episode) if episode not in (None, "") else None
        except (TypeError, ValueError):
            episode = None

        aired = None
        start = prog.get("start_time") or cp.get("started_at")
        if start:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", str(start))
            if m:
                aired = m.group(1)

        title = self._episode_title(prog, aired, season, episode, show, cfg["title_fallback"])

        return {
            "show": show,
            "season": season,
            "episode": episode,
            "aired": aired,
            "title": title,
            # EPG first, snapshot second. The snapshot is what was known when
            # the recording was SCHEDULED; the EPG is what is known now.
            "plot": (self._fresh_plot(prog)
                     or (prog.get("description") or "").strip()),
            "rating": cp.get("rating"),
            "poster_url": cp.get("poster_url"),
            # Same derivation the NFO uniqueid uses; RPDB is keyed on it.
            "tvdb": _tvdb_id(cp),
            # ⭐ Everything below was already sitting in the row we had fetched.
            # Reading it costs one dict lookup each -- the query is the expensive
            # part and it had already happened. The EPG was carrying 21 keys that
            # never reached an NFO, several of which map straight onto standard
            # Kodi/Plex elements.
            "extra": self._extra_fields(prog),
            "dir": os.path.dirname(path),
            "nfo": os.path.splitext(path)[0] + ".nfo",
            "thumb": os.path.splitext(path)[0] + "-thumb.jpg",
            "mkv": path,
        }

    def _extra_fields(self, prog):
        """The EPG detail that standard NFO elements can carry.

        Purely a read of data already in hand -- the EPG row was fetched to get
        the plot, so every key here is free.

        ⚠️ Everything is OPTIONAL and individually guarded. A feed that omits a
        key, or supplies it in an unexpected shape, must produce an NFO missing
        one element rather than no NFO at all: this runs on every recording and
        a malformed sidecar is worse than a sparse one. Hence the per-field
        try/except rather than one around the lot, which would drop the whole
        set on a single bad value.

        ⛔ Deliberately NOT carried: `video`, `audio` and `subtitles`. Plex and
        Kodi read those from the file itself, where they are authoritative; an
        EPG's claim about aspect ratio or audio channels is a prediction, and a
        wrong one would override the truth.
        """
        out = {"genres": [], "credits": {}, "tags": []}
        if not isinstance(prog, dict):
            return out

        # ⛔⛔ THE SNAPSHOT DOES NOT CARRY THESE FIELDS. The Recording's
        # `program` dict holds only description/episode/season/start_time/
        # sub_title/title/tvg_id/... -- verified against live rows. Reading
        # `prog` directly here would find nothing, write nothing, and look
        # exactly like a working feature: the silent-disarm shape this codebase
        # keeps hitting. The detail lives on the EPG row's custom_properties, so
        # it has to be fetched the same way the plot is.
        cp = {}
        try:
            from datetime import timedelta

            from django.utils.dateparse import parse_datetime

            from apps.epg.models import ProgramData

            tvg = (prog.get("tvg_id") or "").strip()
            title = (prog.get("title") or "").strip()
            start = prog.get("start_time")
            if tvg and title and start:
                st = parse_datetime(str(start)) if not hasattr(start, "year") else start
                if st is not None:
                    win = timedelta(minutes=self.EPG_WINDOW_MIN)
                    # Richest row wins: feeds differ, and the one with the most
                    # keys is the one worth reading. Same tvg_id AND title AND
                    # window as every other lookup here -- see _fresh_plot for
                    # why the title is not optional.
                    for r in ProgramData.objects.filter(
                            tvg_id=tvg, title=title,
                            start_time__gte=st - win, start_time__lte=st + win):
                        rc = r.custom_properties
                        if isinstance(rc, dict) and len(rc) > len(cp):
                            cp = rc
        except Exception:      # noqa: BLE001 - metadata is never worth an error
            cp = {}
        if not cp:
            return out

        def _try(fn):
            try:
                fn()
            except Exception:   # noqa: BLE001 - one bad field must not cost the rest
                pass

        def _genres():
            cats = cp.get("categories")
            if isinstance(cats, (list, tuple)):
                out["genres"] = [str(c).strip() for c in cats if str(c).strip()]
        _try(_genres)

        def _year():
            d = str(cp.get("date") or "").strip()
            m = re.search(r"(19|20)\d{2}", d)
            if m:
                out["year"] = m.group(0)
        _try(_year)

        def _country():
            c = cp.get("country")
            if isinstance(c, str) and c.strip():
                out["country"] = c.strip()
        _try(_country)

        def _rating():
            # star_ratings: [{'value': '8/10', 'system': 'themoviedb.org'}]
            srs = cp.get("star_ratings")
            if not isinstance(srs, (list, tuple)):
                return
            for sr in srs:
                if not isinstance(sr, dict):
                    continue
                val = str(sr.get("value") or "").strip()
                m = re.match(r"^\s*([\d.]+)\s*/\s*([\d.]+)\s*$", val)
                if not m:
                    continue
                num, den = float(m.group(1)), float(m.group(2))
                if den > 0:
                    # Normalise to /10, which is what Kodi expects.
                    out["rating_value"] = round(num * 10.0 / den, 1)
                    out["rating_system"] = str(sr.get("system") or "").strip()
                    return
        _try(_rating)

        def _credits():
            # credits: {'actor': [{'name': 'Mike Greenberg'}], 'director': [...]}
            cr = cp.get("credits")
            if not isinstance(cr, dict):
                return
            for role in ("actor", "director", "writer", "presenter", "guest"):
                people = cr.get(role)
                if not isinstance(people, (list, tuple)):
                    continue
                names = []
                for person in people:
                    if isinstance(person, dict):
                        n = str(person.get("name") or "").strip()
                    else:
                        n = str(person).strip()
                    if n:
                        names.append(n)
                if names:
                    out["credits"][role] = names
        _try(_credits)

        def _uniqueid():
            t = _tvdb_id(cp)          # 'series/260092' -> '260092'
            if t:
                out["tvdb"] = t
        _try(_uniqueid)

        def _flags():
            # Not standard NFO elements, so they land as <tag>. They are the
            # first-run/repeat signal, which is exactly what distinguishes an
            # airing with a real synopsis from one carrying the show blurb.
            for key, tag in (("new", "New"), ("premiere", "Premiere"),
                            ("live", "Live"), ("previously_shown", "Repeat")):
                if cp.get(key):
                    out["tags"].append(tag)
            kws = cp.get("keywords")
            if isinstance(kws, (list, tuple)):
                out["tags"].extend(str(k).strip() for k in kws if str(k).strip())
        _try(_flags)

        def _icon():
            ic = cp.get("icon")
            if isinstance(ic, str) and ic.startswith("http"):
                out["icon"] = ic
        _try(_icon)

        return out

    def _episode_nfo(self, plan):
        root = ET.Element("episodedetails")
        _text(root, "title", plan["title"])
        _text(root, "showtitle", plan["show"])
        if plan["season"] is not None:
            _text(root, "season", plan["season"])
        if plan["episode"] is not None:
            _text(root, "episode", plan["episode"])
        _text(root, "plot", plan["plot"])
        _text(root, "aired", plan["aired"])
        _text(root, "mpaa", plan["rating"])

        # --- EPG detail that was previously discarded -----------------------
        x = plan.get("extra") or {}
        for g in x.get("genres", []):
            _text(root, "genre", g)
        if x.get("year"):
            _text(root, "year", x["year"])
        if x.get("country"):
            _text(root, "country", x["country"])
        if x.get("rating_value") is not None:
            # <ratings><rating name=..><value/></rating></ratings> is the modern
            # Kodi shape; the flat <rating> is kept too because Plex reads it.
            ratings = ET.SubElement(root, "ratings")
            r = ET.SubElement(ratings, "rating",
                              {"name": x.get("rating_system") or "epg",
                               "max": "10"})
            _text(r, "value", x["rating_value"])
            _text(root, "rating", x["rating_value"])
        for role, names in (x.get("credits") or {}).items():
            # Kodi: <actor><name/></actor>; everything else is a flat element.
            for n in names:
                if role == "actor":
                    a = ET.SubElement(root, "actor")
                    _text(a, "name", n)
                else:
                    _text(root, role, n)
        if x.get("tvdb"):
            ET.SubElement(root, "uniqueid", {"type": "tvdb"}).text = str(x["tvdb"])
        for t in dict.fromkeys(x.get("tags", [])):     # de-duped, order kept
            _text(root, "tag", t)

        # A LOCAL thumbnail always wins: it is a frame of this actual recording,
        # whereas the EPG icon is promotional art for the programme.
        if os.path.exists(plan["thumb"]):
            _text(root, "thumb", os.path.basename(plan["thumb"]))
        elif x.get("icon"):
            _text(root, "thumb", x["icon"])
        return _pretty(root)

    def _tvshow_nfo(self, plan):
        root = ET.Element("tvshow")
        _text(root, "title", plan["show"])
        _text(root, "showtitle", plan["show"])
        _text(root, "mpaa", plan["rating"])
        return _pretty(root)

    def _make_thumb(self, plan, cfg, log):
        mode = cfg["thumb_mode"]
        if mode == "off":
            return False
        mkv = plan["mkv"]
        if not os.path.exists(mkv):
            return False
        dur = _duration(mkv)
        if not dur or dur <= 0:
            return False

        # Stay clear of the opening minutes and the final tenth of the file.
        lo = min(cfg["thumb_skip"], dur * 0.25)
        hi = dur * 0.90
        if hi <= lo:
            lo, hi = dur * 0.20, dur * 0.80
        start = lo + (hi - lo) * (cfg["thumb_pos"] / 100.0)
        start = max(0.0, min(start, max(0.0, dur - 5.0)))

        scale = f"scale={cfg['thumb_width']}:-2"
        if mode == "representative":
            vf = f"thumbnail={_THUMB_BATCH},{scale}"
            # The thumbnail filter needs a run of frames to choose between.
            window = ["-t", "60"]
        else:
            vf = scale
            window = []

        tmp = plan["thumb"] + ".tmp.jpg"
        ok, _ = _run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{start:.2f}"]
            + window
            + ["-i", mkv, "-vf", vf, "-frames:v", "1", "-q:v", "3", tmp],
            timeout=300,
        )
        if ok and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, plan["thumb"])
            _match_dir_owner(plan["thumb"])
            return True
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        log.debug("[%s] thumbnail failed for %s", PLUGIN_KEY, mkv)
        return False

    def _tvmaze_show(self, show_title, cfg, log):
        """Fuzzy TVmaze lookup for a show Dispatcharr could not resolve exactly.

        Returns the matched show dict (or None). The caller picks what it needs
        from it: `image` for artwork, `externals` for IMDB/TVDB ids. One lookup
        serves both because they must describe the same show -- and TVmaze
        returns the ids in the response the artwork already required, so the
        ids cost nothing extra.

        TVmaze's search does not degrade gracefully on a variant title: querying
        "Highway Patrol Special" returns ZERO results, while "Highway Patrol"
        returns the series. So the query itself is trimmed a word at a time
        until something comes back.

        Only the QUERY is relaxed. Every candidate is still judged against the
        full original title, so a shorter query cannot widen what is accepted --
        it only changes where we look.
        """
        if show_title in self._tvmaze_cache:
            return self._tvmaze_cache[show_title]

        import urllib.parse
        import urllib.request

        words = _norm_words(show_title)
        queries = [
            " ".join(words[:n])
            for n in range(len(words), _FUZZY_MIN_WORDS - 1, -1)
        ][:_FUZZY_MAX_QUERIES]

        match = None
        reason = "no query returned a usable candidate"
        for i, q in enumerate(queries):
            try:
                if i:
                    time.sleep(0.5)  # TVmaze asks for light use; we are not in a hurry
                req = urllib.request.Request(
                    _TVMAZE_SEARCH + urllib.parse.quote(q),
                    headers={"User-Agent": "Dispatcharr-NFO"},
                )
                with urllib.request.urlopen(req, timeout=20) as r:
                    results = json.load(r)
            except Exception as e:
                log.debug("[%s] TVmaze query %r failed: %s", PLUGIN_KEY, q, e)
                continue

            if not results:
                continue
            show, why = _fuzzy_show_match(
                show_title, results, cfg["fuzzy_coverage"], cfg["country"]
            )
            if show:
                match = show
                reason = "query %r %s" % (q, why)
                break
            reason = "query %r %s" % (q, why)

        log.info("[%s] artwork fallback for %r: %s", PLUGIN_KEY, show_title, reason)
        self._tvmaze_cache[show_title] = match
        return match

    def _tvmaze_poster(self, show_title, cfg, log):
        """Poster URL from the fuzzy TVmaze match, or None.

        `original` before `medium`: medium is roughly 210px wide, which is a
        thumbnail, and a poster written once and kept for years should not be
        the small one when the large one costs the same request.
        """
        image = (self._tvmaze_show(show_title, cfg, log) or {}).get("image") or {}
        return image.get("original") or image.get("medium")

    def _tvmaze_ids(self, show_title, cfg, log):
        """{"tvdb": str, "imdb": str} from the fuzzy TVmaze match; values may be "".

        The same guarded match the artwork uses -- coverage threshold, country
        tie-break, ambiguity refused -- so an id can only come from a show this
        plugin was already willing to take a poster from. That matters: a wrong
        id here would fetch a confidently wrong poster for the wrong series.
        """
        ext = (self._tvmaze_show(show_title, cfg, log) or {}).get("externals") or {}
        return {"tvdb": str(ext.get("thetvdb") or "").strip(),
                "imdb": str(ext.get("imdb") or "").strip()}

    def _rpdb_url(self, plan, cfg, log):
        """RPDB poster URL for this show, or None when no id can be resolved.

        Id resolution, cheapest first:

          1. the TVDB id the EPG already carries      -- free, no request
          2. TVDB id from the fuzzy TVmaze match      -- cached, and the same
             request the artwork fallback already makes
          3. IMDB id from that same match             -- RPDB takes `tt…` bare

        ⭐ TMDB is deliberately NOT consulted. Its /find endpoint resolves the
        same shows by external id, but it needs its own API key, and TVmaze
        already returns both ids in the response the artwork lookup fetches. A
        second credential for overlapping coverage is a poor trade in a plugin
        whose whole point is that it works with what Dispatcharr already has.

        The ids come from the SAME guarded match the artwork uses -- coverage
        threshold, country tie-break, ambiguity refused -- so RPDB can only be
        asked about a show this plugin already trusts. A wrong id here would
        return a confidently wrong poster for a different series, which is worse
        than no poster.
        """
        if not cfg["rpdb_key"]:
            return None

        id_type = media_id = ""
        if plan.get("tvdb"):
            id_type, media_id = "tvdb", "series-%s" % plan["tvdb"]
        elif cfg["fuzzy"]:
            ids = self._tvmaze_ids(plan["show"], cfg, log)
            if ids["tvdb"]:
                id_type, media_id = "tvdb", "series-%s" % ids["tvdb"]
            elif ids["imdb"]:
                # An IMDB id is used bare; the movie-/series- prefix is a
                # TMDB/TVDB requirement only.
                id_type, media_id = "imdb", ids["imdb"]
        if not id_type:
            return None

        import urllib.parse

        return "%s/%s/%s/%s/%s.jpg?fallback=true" % (
            _RPDB_BASE,
            urllib.parse.quote(cfg["rpdb_key"], safe=""),
            id_type,
            cfg["rpdb_type"],
            urllib.parse.quote(media_id, safe=""),
        )

    def _download(self, url, tmp):
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "Dispatcharr-NFO"})
        with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as fh:
            shutil.copyfileobj(r, fh)
        return os.path.getsize(tmp) > 0

    def _fetch_poster(self, plan, cfg, log):
        """Write <show>/poster.jpg from the best source that yields a real poster.

        Sources are tried in preference order and each is GRADED after download.
        A landscape image is refused outright: providers do file banners and
        episode stills under the poster key, and Plex will letterbox one into a
        poster slot where it looks broken for as long as nobody notices.

        The first candidate at or above _POSTER_GOOD_W wins immediately;
        otherwise the widest valid candidate is kept. That is the difference
        between artwork chosen on quality and artwork chosen on precedence -- a
        416px provider thumbnail should not beat a 680px TVmaze original just
        because it was consulted first.

        Candidate URLs resolve lazily, so a good first source still costs
        exactly one request and TVmaze is not consulted at all.
        """
        dest = os.path.join(plan["dir"], "poster.jpg")
        if os.path.exists(dest) and not cfg["overwrite"]:
            return False

        cands = []
        if cfg["rpdb_key"]:
            cands.append(("rpdb", lambda: self._rpdb_url(plan, cfg, log)))
        if plan.get("poster_url"):
            cands.append(("epg", lambda: plan["poster_url"]))
        if cfg["fuzzy"]:
            cands.append(("tvmaze",
                          lambda: self._tvmaze_poster(plan["show"], cfg, log)))
        if not cands:
            return False

        tmp, best = dest + ".tmp", None
        try:
            for source, resolve in cands:
                try:
                    url = resolve()
                    if not url or not self._download(url, tmp):
                        continue
                except Exception as e:
                    # ⚠️ NEVER log the url for the rpdb source -- it embeds the
                    # API key. The source label is enough to diagnose with.
                    log.debug("[%s] poster fetch failed (%s): %s",
                              PLUGIN_KEY, source, e)
                    continue
                ok, width, why = _poster_grade(tmp)
                if not ok:
                    log.info("[%s] %s poster refused for %r: %s",
                             PLUGIN_KEY, source, plan["show"], why)
                    continue
                if width >= _POSTER_GOOD_W:
                    os.replace(tmp, dest)
                    _match_dir_owner(dest)
                    log.info("[%s] poster from %s for %r (%s)",
                             PLUGIN_KEY, source, plan["show"], why)
                    return True
                if best is None or width > best[0]:
                    if best:
                        _rm(best[1])
                    keep = dest + ".cand"
                    os.replace(tmp, keep)
                    best = (width, keep, source)
            if best:
                os.replace(best[1], dest)
                _match_dir_owner(dest)
                log.info("[%s] poster from %s for %r (%dpx; no wider source "
                         "available)", PLUGIN_KEY, best[2], plan["show"], best[0])
                return True
        finally:
            _rm(tmp)
            if best:
                _rm(best[1])
        return False

    def _plex(self, cfg, path, method="GET"):
        """One Plex API call. Token travels as a header, never in the URL."""
        import urllib.request

        req = urllib.request.Request(
            cfg["plex_url"] + path,
            headers={"X-Plex-Token": cfg["plex_token"], "Accept": "application/xml"},
            method=method,
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()

    def _plex_show_key(self, show, cfg, log):
        """ratingKey of a show, matched by title across every TV section.

        Only show-level keys are needed: episode NFOs are picked up by an
        ordinary scan, and it is the show poster that a scan ignores.
        """
        if show in self._plex_key_cache:
            return self._plex_key_cache[show]

        key = None
        try:
            sections = ET.fromstring(self._plex(cfg, "/library/sections"))
            for section in sections.iter("Directory"):
                if section.get("type") != "show":
                    continue
                listing = ET.fromstring(
                    self._plex(cfg, "/library/sections/%s/all?type=2" % section.get("key"))
                )
                for entry in listing.iter("Directory"):
                    if entry.get("title") == show:
                        key = entry.get("ratingKey")
                        break
                if key:
                    break
        except Exception as e:
            log.debug("[%s] Plex show lookup failed for %r: %s", PLUGIN_KEY, show, e)

        self._plex_key_cache[show] = key
        return key

    def _plex_refresh_show(self, show, cfg, log):
        """Force a metadata refresh so Plex re-reads a changed poster."""
        if not (cfg["plex_url"] and cfg["plex_token"]):
            return False
        key = self._plex_show_key(show, cfg, log)
        if not key:
            log.info("[%s] no Plex show matching %r; artwork refresh skipped",
                     PLUGIN_KEY, show)
            return False
        try:
            self._plex(cfg, "/library/metadata/%s/refresh?force=1" % key, method="PUT")
            log.info("[%s] forced Plex artwork refresh for %r (ratingKey %s)",
                     PLUGIN_KEY, show, key)
            return True
        except Exception as e:
            log.warning("[%s] Plex refresh failed for %r: %s", PLUGIN_KEY, show, e)
            return False

    def _fire_webhook(self, plan, cfg, log):
        """Notify another service that one recording is ready.

        Any credentials in the URL are moved into an Authorization header and
        stripped from the URL BEFORE the request is made, so neither a success
        line nor a urllib exception (which quotes the URL back) can leak them.
        """
        template = cfg["webhook_url"]
        if not template:
            return False

        import base64
        import urllib.parse
        import urllib.request

        media = _rewrite_path(plan["mkv"], cfg["webhook_from"], cfg["webhook_to"])
        values = {
            "path": media,
            "dir": os.path.dirname(media),
            "file": os.path.basename(media),
            "show": plan["show"],
        }
        try:
            url = template.format(
                **{k: urllib.parse.quote(v, safe="/") for k, v in values.items()}
            )
        except (KeyError, IndexError, ValueError) as e:
            log.warning("[%s] webhook URL template is not usable: %s", PLUGIN_KEY, e)
            return False

        headers = {"User-Agent": "Dispatcharr-NFO"}
        parts = urllib.parse.urlsplit(url)
        if parts.username or parts.password:
            token = "%s:%s" % (parts.username or "", parts.password or "")
            headers["Authorization"] = "Basic " + base64.b64encode(
                token.encode("utf-8")
            ).decode("ascii")
            netloc = parts.hostname or ""
            if parts.port:
                netloc = "%s:%d" % (netloc, parts.port)
            url = urllib.parse.urlunsplit(
                (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
            )

        if cfg["webhook_header"] and ":" in cfg["webhook_header"]:
            name, value = cfg["webhook_header"].split(":", 1)
            if name.strip():
                headers[name.strip()] = value.strip()

        method = "POST" if cfg["webhook_method"] == "POST" else "GET"
        body = None
        if method == "POST":
            body = json.dumps(values).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=15) as r:
                status = getattr(r, "status", None) or r.getcode()
            log.info("[%s] webhook %s -> HTTP %s for %s",
                     PLUGIN_KEY, method, status, os.path.basename(media))
            return True
        except Exception as e:
            log.warning("[%s] webhook %s failed for %s: %s",
                        PLUGIN_KEY, method, os.path.basename(media), e)
            return False

    def _emit(self, cp, path, cfg, log, dry_run=False):
        """Write every sidecar for one recording. Returns a per-file summary."""
        plan = self._plan(cp, path, cfg)
        wrote = {"nfo": False, "tvshow": False, "poster": False, "thumb": False,
                 "webhook": False, "plex": False}

        if dry_run:
            return plan, wrote

        if not os.path.isdir(plan["dir"]):
            return plan, wrote

        # Thumbnail first: the episode NFO references it only if it exists.
        if cfg["thumb_mode"] != "off":
            # ⛔ Defer to an EXTERNAL OWNER that has already chosen this
            # thumbnail. clapparr fires at recording_end, before any ad-break
            # analysis exists, so its own pick is a position heuristic that can
            # land inside a commercial -- correct as a placeholder, wrong as a
            # replacement for something better chosen later. `overwrite`
            # deliberately does NOT override this.
            #
            # ⭐ Written generically -- any "<base>-thumb.<owner>.json" claims
            # it -- rather than naming breakarr. This is a reasonable upstream
            # feature that carries no estate-specific knowledge, which matters
            # because a clapparr update would otherwise silently revert a local
            # patch and the protection would vanish with no signal.
            #
            # ⚠️ Derived from plan["mkv"] (the original recording path) by the
            # SAME route _plan() used to build plan["thumb"] itself, not by
            # string surgery on plan["thumb"]. Two derivations of one path is
            # how they drift. (This plugin's plan dict has no "path" key --
            # the recording path is stored under "mkv".)
            _stem = os.path.splitext(plan["mkv"])[0]
            # ⚠️ glob.escape: a show title containing [ ] is read as a character
            # class, the pattern then matches NOTHING, and the ownership check
            # silently reports "unowned" -- so the cutter's thumbnail gets
            # overwritten by exactly the protection meant to prevent it. Failing
            # open is the wrong direction for a guard. ("Doctor Who [2005]"
            # reproduced this; a literal ? or * happens to still self-match.)
            _owned = glob.glob(glob.escape(_stem) + "-thumb.*.json")
            if not _owned and (cfg["overwrite"]
                               or not os.path.exists(plan["thumb"])):
                wrote["thumb"] = self._make_thumb(plan, cfg, log)

        wrote["nfo"] = _write(plan["nfo"], self._episode_nfo(plan), cfg["overwrite"])

        if cfg["tvshow"]:
            wrote["tvshow"] = _write(
                os.path.join(plan["dir"], "tvshow.nfo"),
                self._tvshow_nfo(plan),
                cfg["overwrite"],
            )
        if cfg["poster"]:
            wrote["poster"] = self._fetch_poster(plan, cfg, log)

        # A scan re-reads episode NFOs but ignores a show's poster.jpg, so only
        # a new poster needs the forced refresh -- and only once per show.
        if wrote["poster"] and plan["show"] not in self._refreshed_shows:
            self._refreshed_shows.add(plan["show"])
            wrote["plex"] = self._plex_refresh_show(plan["show"], cfg, log)

        # Only notify when something actually changed, so a no-op sweep over a
        # library that is already complete does not spray a scan relay.
        if any(wrote.values()):
            wrote["webhook"] = self._fire_webhook(plan, cfg, log)

        return plan, wrote

    # ------------------------------------------------------- deferred waiter

    def _capture_epg_at_start(self, recording_id, log):
        """Write the EPG's episode name into the recording, at start.

        The counterpart to _fresh_sub_title, one step earlier: rather than
        re-reading the EPG when the NFO is written and hoping the row survives,
        this pins the answer into the recording while the programme is ON AIR
        and the row cannot have aged out.

        Deliberately narrow:
        - Only fills sub_title, and only when it is ABSENT. A name that was
          already known at scheduling time is correct and is left alone; this
          must never overwrite good data with a window match.
        - Never raises. A metadata nicety must not affect a recording, so every
          failure is logged and swallowed.
        - close_old_connections() in `finally`, the pattern the end-handler
          established in this same container -- a daemon thread that keeps a
          connection open outlives the request that would have closed it.
        """
        from django.db import close_old_connections, transaction

        from apps.channels.models import Recording

        try:
            # --- read phase: decide whether there is anything to do, and do the
            # EPG lookup, OUTSIDE any lock. The query is the slow part and it
            # touches a different table; holding a row lock across it would put
            # this thread in the way of the recording task for no reason.
            rec = Recording.objects.filter(id=recording_id).first()
            if rec is None:
                log.info("[%s] recording %s vanished before EPG capture", PLUGIN_KEY, recording_id)
                return
            prog = (rec.custom_properties or {}).get("program") or {}
            existing = (prog.get("sub_title") or "").strip()
            if existing and existing.lower() != "unknown":
                return                      # already named; nothing to do

            fresh = self._fresh_sub_title(prog, log)
            if not fresh:
                log.info("[%s] recording %s: EPG has no episode name at start",
                         PLUGIN_KEY, recording_id)
                return

            # --- write phase: re-read UNDER A ROW LOCK and merge into whatever
            # custom_properties holds NOW.
            #
            # ⛔ custom_properties is a single JSON blob that the recording task
            # is actively writing during start-up (status, file_path). A plain
            # read-modify-write from this thread would save a dict captured
            # BEFORE those updates and silently revert them -- and the loss would
            # be invisible, because the field would still look well-formed.
            # Re-reading inside the lock means we only ever add one key to the
            # current value.
            with transaction.atomic():
                locked = Recording.objects.select_for_update().filter(id=recording_id).first()
                if locked is None:
                    return
                cp = locked.custom_properties or {}
                prog_now = cp.get("program") or {}
                # Re-check under the lock: another writer may have supplied the
                # name while the EPG query was running.
                again = (prog_now.get("sub_title") or "").strip()
                if again and again.lower() != "unknown":
                    return
                prog_now["sub_title"] = fresh
                cp["program"] = prog_now
                # Provenance, so a later reader can tell a captured name from one
                # that was in the original snapshot.
                cp["clapparr_sub_title_source"] = "epg_at_start"
                locked.custom_properties = cp
                locked.save(update_fields=["custom_properties"])
            log.info("[%s] recording %s: captured episode name %r from the EPG",
                     PLUGIN_KEY, recording_id, fresh)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] EPG capture failed for recording %s: %r",
                        PLUGIN_KEY, recording_id, e)
        finally:
            close_old_connections()

    def _wait_and_emit(self, recording_id, cfg, log):
        """Wait for the remux, then write sidecars. Runs on its own thread.

        `recording_end` is emitted from inside run_recording BEFORE that task
        remuxes the file and stores file_path, so this cannot run inline.
        """
        from django.db import close_old_connections

        deadline = time.time() + cfg["wait_s"]
        try:
            while True:
                close_old_connections()
                from apps.channels.models import Recording

                rec = Recording.objects.filter(id=recording_id).first()
                if rec is None:
                    log.info("[%s] recording %s vanished (cancelled?)", PLUGIN_KEY, recording_id)
                    return
                cp = rec.custom_properties or {}
                path = cp.get("file_path")
                if path and os.path.exists(path) and cp.get("status") == "completed":
                    plan, wrote = self._emit(cp, path, cfg, log)
                    log.info(
                        "[%s] recording %s -> %s | nfo=%s tvshow=%s poster=%s thumb=%s",
                        PLUGIN_KEY, recording_id, os.path.basename(path),
                        wrote["nfo"], wrote["tvshow"], wrote["poster"], wrote["thumb"],
                    )
                    return
                if time.time() >= deadline:
                    log.warning(
                        "[%s] gave up waiting for recording %s to finalise (status=%s path=%s)",
                        PLUGIN_KEY, recording_id, cp.get("status"), path,
                    )
                    return
                time.sleep(_POLL_SECONDS)
        except Exception:
            log.exception("[%s] deferred sidecar write failed for %s", PLUGIN_KEY, recording_id)
        finally:
            close_old_connections()

    # -------------------------------------------------------------- entry pt

    def run(self, action, params, context):
        log = context.get("logger")
        cfg = self._settings(context)
        params = params or {}

        if action == "on_recording_start":
            # ⭐ WHY AT START, not only at the end.
            # custom_properties["program"] is snapshotted when the recording is
            # SCHEDULED -- often days before broadcast, before the provider has
            # named the episode. That stale snapshot is why _episode_title fell
            # back to a date ("Wednesday 26 August 2026") while the plot, which
            # IS re-read, came out correct. Capturing here fixes the snapshot at
            # source rather than compensating for it later.
            if not cfg["auto"]:
                return {"status": "skipped", "message": "Automatic generation is disabled"}
            payload = params.get("payload") or {}
            rid = payload.get("recording_id") or payload.get("recordingId")
            if not rid:
                return {"status": "skipped", "message": "Event carried no recording_id"}
            # ⛔ Same hard rule as on_recording_end: dispatcharr runs this handler
            # SYNCHRONOUSLY, inline with the recording task, so it must not block.
            # The work is two queries, but it takes the thread anyway -- an
            # inline DB write on the task that is opening the stream is not worth
            # the risk, and the symmetry keeps one pattern rather than two.
            threading.Thread(
                target=self._capture_epg_at_start, args=(rid, log),
                name=f"{PLUGIN_KEY}-start-{rid}", daemon=True,
            ).start()
            return {"status": "ok", "message": f"Queued EPG capture for recording {rid}"}

        if action == "on_recording_end":
            if not cfg["auto"]:
                return {"status": "skipped", "message": "Automatic generation is disabled"}
            payload = params.get("payload") or {}
            rid = payload.get("recording_id") or payload.get("recordingId")
            if not rid:
                return {"status": "skipped", "message": "Event carried no recording_id"}
            if payload.get("interrupted"):
                log.info("[%s] recording %s was interrupted; still writing sidecars", PLUGIN_KEY, rid)
            # Hand off: this event fires inside the task that performs the remux,
            # so blocking here would deadlock the file we are waiting for.
            threading.Thread(
                target=self._wait_and_emit, args=(rid, cfg, log),
                name=f"{PLUGIN_KEY}-{rid}", daemon=True,
            ).start()
            return {"status": "ok", "message": f"Queued sidecar write for recording {rid}"}

        if action == "refresh_plex_artwork":
            if not (cfg["plex_url"] and cfg["plex_token"]):
                return {"status": "error",
                        "message": "Set the Plex base URL and token first"}
            shows, done, missed = set(), 0, []
            for _rec, cp, path in self._recording_rows():
                if not os.path.exists(path):
                    continue
                show = self._plan(cp, path, cfg)["show"]
                if show in shows:
                    continue
                shows.add(show)
                if self._plex_refresh_show(show, cfg, log):
                    done += 1
                else:
                    missed.append(show)
            return {
                "status": "ok",
                "message": "Refreshed %d of %d show(s) in Plex.%s" % (
                    done, len(shows),
                    (" Not matched: " + ", ".join(missed[:5])) if missed else "",
                ),
            }

        if action == "test_webhook":
            if not cfg["webhook_url"]:
                return {"status": "error", "message": "No webhook URL is configured"}
            rows = self._recording_rows()
            rows = [r for r in rows if os.path.exists(r[2])]
            if not rows:
                return {"status": "error",
                        "message": "No recording with a file on disk to test against"}
            _rec, cp, path = rows[0]
            plan = self._plan(cp, path, cfg)
            sent = self._fire_webhook(plan, cfg, log)
            target = _rewrite_path(plan["mkv"], cfg["webhook_from"], cfg["webhook_to"])
            return {
                "status": "ok" if sent else "error",
                "message": (
                    "%s the webhook with path %s"
                    % ("Sent" if sent else "Failed to send", target)
                ),
            }

        if action in ("generate_missing", "regenerate_all", "preview"):
            if action == "regenerate_all":
                cfg["overwrite"] = True
            dry = action == "preview"

            rows = self._recording_rows()
            done = 0
            missing_file = 0
            lines = []
            totals = {"nfo": 0, "tvshow": 0, "poster": 0, "thumb": 0,
                      "webhook": 0, "plex": 0}

            for _rec, cp, path in rows:
                if not os.path.exists(path):
                    missing_file += 1
                    continue
                plan, wrote = self._emit(cp, path, cfg, log, dry_run=dry)
                done += 1
                for k in totals:
                    if wrote[k]:
                        totals[k] += 1
                if dry and len(lines) < 40:
                    se = (
                        f"S{plan['season']:02d}E{plan['episode']:02d}"
                        if plan["season"] is not None and plan["episode"] is not None
                        else "no index"
                    )
                    lines.append(
                        f"{plan['show']} [{se}] {plan['title']!r} "
                        f"plot={'yes' if plan['plot'] else 'NONE'}"
                    )

            msg = (
                f"{'Previewed' if dry else 'Processed'} {done} recording(s); "
                f"{missing_file} had no file on disk."
            )
            if not dry:
                msg += (
                    f" Wrote nfo={totals['nfo']} tvshow={totals['tvshow']} "
                    f"poster={totals['poster']} thumb={totals['thumb']}."
                )
                if cfg["webhook_url"]:
                    msg += f" Notified {totals['webhook']}."
                if totals["plex"]:
                    msg += f" Refreshed {totals['plex']} show(s) in Plex."
            return {"status": "ok", "message": msg, "results": lines}

        return {"status": "error", "message": f"Unknown action '{action}'"}
