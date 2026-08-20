"""DVR NFO Generator - Kodi/Plex sidecar metadata for Dispatcharr recordings.

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
the very task that produces it. The handler instead hands off to a daemon thread
which waits for `status == completed` (or the file to appear on disk) and then
writes the sidecars. The handler itself returns immediately.
"""

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

PLUGIN_KEY = "dvr_nfo_generator"

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


def _write(path, content, overwrite):
    if os.path.exists(path) and not overwrite:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)
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
    name = "DVR NFO Generator"
    version = "1.0.0"
    description = (
        "Writes Kodi/Plex NFO sidecars, posters and episode thumbnails for DVR "
        "recordings so they present with real titles, summaries and artwork "
        "instead of 'Episode 08-18'."
    )
    author = "touki"
    help_url = "https://github.com/Dispatcharr/Plugins"

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
    ]

    def __init__(self):
        # One TVmaze answer per show name per process, negatives included, so a
        # bulk regenerate over 30 recordings of the same show is a single call.
        self._poster_cache = {}

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
        }

    def _episode_title(self, prog, aired, season, episode, show, mode):
        sub = (prog.get("sub_title") or "").strip()
        if sub and sub.lower() != "unknown":
            return sub
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

    def _plan(self, cp, path, cfg):
        """Work out every sidecar for one recording. Pure - touches no disk."""
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
            "plot": (prog.get("description") or "").strip(),
            "rating": cp.get("rating"),
            "poster_url": cp.get("poster_url"),
            "dir": os.path.dirname(path),
            "nfo": os.path.splitext(path)[0] + ".nfo",
            "thumb": os.path.splitext(path)[0] + "-thumb.jpg",
            "mkv": path,
        }

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
        if os.path.exists(plan["thumb"]):
            _text(root, "thumb", os.path.basename(plan["thumb"]))
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
            return True
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        log.debug("[%s] thumbnail failed for %s", PLUGIN_KEY, mkv)
        return False

    def _tvmaze_poster(self, show_title, cfg, log):
        """Fuzzy TVmaze lookup for a show Dispatcharr could not resolve exactly.

        TVmaze's search does not degrade gracefully on a variant title: querying
        "Highway Patrol Special" returns ZERO results, while "Highway Patrol"
        returns the series. So the query itself is trimmed a word at a time
        until something comes back.

        Only the QUERY is relaxed. Every candidate is still judged against the
        full original title, so a shorter query cannot widen what is accepted --
        it only changes where we look.
        """
        if show_title in self._poster_cache:
            return self._poster_cache[show_title]

        import urllib.parse
        import urllib.request

        words = _norm_words(show_title)
        queries = [
            " ".join(words[:n])
            for n in range(len(words), _FUZZY_MIN_WORDS - 1, -1)
        ][:_FUZZY_MAX_QUERIES]

        url = None
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
                image = show.get("image") or {}
                url = image.get("original") or image.get("medium")
                reason = "query %r %s" % (q, why)
                break
            reason = "query %r %s" % (q, why)

        log.info("[%s] artwork fallback for %r: %s", PLUGIN_KEY, show_title, reason)
        self._poster_cache[show_title] = url
        return url

    def _fetch_poster(self, plan, cfg, log):
        dest = os.path.join(plan["dir"], "poster.jpg")
        if os.path.exists(dest) and not cfg["overwrite"]:
            return False
        url = plan.get("poster_url")
        if not url and cfg["fuzzy"]:
            # Dispatcharr found nothing for this title; try the fuzzy endpoint.
            url = self._tvmaze_poster(plan["show"], cfg, log)
        if not url:
            return False
        try:
            import urllib.request

            tmp = dest + ".tmp"
            req = urllib.request.Request(url, headers={"User-Agent": "Dispatcharr-NFO"})
            with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as fh:
                shutil.copyfileobj(r, fh)
            if os.path.getsize(tmp) > 0:
                os.replace(tmp, dest)
                return True
            os.remove(tmp)
        except Exception as e:
            log.debug("[%s] poster fetch failed (%s): %s", PLUGIN_KEY, url, e)
        return False

    def _emit(self, cp, path, cfg, log, dry_run=False):
        """Write every sidecar for one recording. Returns a per-file summary."""
        plan = self._plan(cp, path, cfg)
        wrote = {"nfo": False, "tvshow": False, "poster": False, "thumb": False}

        if dry_run:
            return plan, wrote

        if not os.path.isdir(plan["dir"]):
            return plan, wrote

        # Thumbnail first: the episode NFO references it only if it exists.
        if cfg["thumb_mode"] != "off":
            if cfg["overwrite"] or not os.path.exists(plan["thumb"]):
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

        return plan, wrote

    # ------------------------------------------------------- deferred waiter

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

        if action in ("generate_missing", "regenerate_all", "preview"):
            if action == "regenerate_all":
                cfg["overwrite"] = True
            dry = action == "preview"

            rows = self._recording_rows()
            done = 0
            missing_file = 0
            lines = []
            totals = {"nfo": 0, "tvshow": 0, "poster": 0, "thumb": 0}

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
            return {"status": "ok", "message": msg, "results": lines}

        return {"status": "error", "message": f"Unknown action '{action}'"}
