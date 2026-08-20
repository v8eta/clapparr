"""Tests for sidecar ownership and the Plex artwork refresh.

Ownership matters because Dispatcharr's container runs as root: sidecars land
root-owned beside media owned by the media user, everything still reads, and
the problem only shows up later when tooling running as that user cannot move
its own sidecars.

The Plex half exists because a library scan re-reads episode NFOs but ignores a
show's poster.jpg, so a changed poster needs a forced per-show refresh.

No network access: urlopen is replaced with a recorder.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("nfoplugin", os.path.join(HERE, "plugin.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    print("  %-4s %-52s %s" % ("ok" if cond else "FAIL", label, str(detail)[:44]))
    if cond:
        PASS += 1
    else:
        FAIL += 1


class _Log:
    def __init__(self):
        self.lines = []

    def _rec(self, fmt, *a):
        self.lines.append(fmt % a if a else fmt)

    info = warning = debug = _rec


# --- ownership -------------------------------------------------------------
print("ownership:")
tmp = tempfile.mkdtemp(prefix="nfo-own-")
try:
    target = os.path.join(tmp, "episode.nfo")
    mod._write(target, "<episodedetails/>", overwrite=True)
    d = os.stat(tmp)
    f = os.stat(target)
    check("written file matches its directory's owner",
          (f.st_uid, f.st_gid) == (d.st_uid, d.st_gid),
          "%d:%d vs %d:%d" % (f.st_uid, f.st_gid, d.st_uid, d.st_gid))
    check("already-matching file reports no change",
          mod._match_dir_owner(target) is False)
    check("missing file does not raise",
          mod._match_dir_owner(os.path.join(tmp, "nope.nfo")) is False)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# --- Plex lookup and refresh ----------------------------------------------
print("plex:")
SECTIONS = b"""<MediaContainer>
  <Directory key="1" type="movie" title="Movies"/>
  <Directory key="6" type="show" title="DVR"/>
  <Directory key="2" type="show" title="TV Shows"/>
</MediaContainer>"""
SEC6 = b"""<MediaContainer>
  <Directory ratingKey="27520" title="Highway Patrol Special"/>
  <Directory ratingKey="29033" title="The Block"/>
</MediaContainer>"""
SEC2 = b"""<MediaContainer>
  <Directory ratingKey="900" title="Some Other Show"/>
</MediaContainer>"""

calls = []


class _Resp:
    def __init__(self, body):
        self._b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _fake(req, timeout=None):
    calls.append((req.get_method(), req.full_url, dict(req.headers)))
    u = req.full_url
    if u.endswith("/library/sections"):
        return _Resp(SECTIONS)
    if "/sections/6/all" in u:
        return _Resp(SEC6)
    if "/sections/2/all" in u:
        return _Resp(SEC2)
    return _Resp(b"<MediaContainer/>")


urllib.request.urlopen = _fake

cfg = {"plex_url": "http://plex:32400", "plex_token": "SECRET-TOKEN"}
p = mod.Plugin()
log = _Log()

ok = p._plex_refresh_show("Highway Patrol Special", cfg, log)
check("refresh reported success", ok)
put = [c for c in calls if c[0] == "PUT"]
check("issued a forced PUT on the right ratingKey",
      len(put) == 1 and "/library/metadata/27520/refresh?force=1" in put[0][1],
      put[0][1] if put else "no PUT")
check("token sent as a header, not in the URL",
      all("SECRET-TOKEN" not in c[1] for c in calls)
      and put[0][2].get("X-plex-token") == "SECRET-TOKEN")
check("token never written to the log",
      all("SECRET-TOKEN" not in ln for ln in log.lines))

before = len(calls)
p._plex_refresh_show("Highway Patrol Special", cfg, log)
check("ratingKey cached (only the PUT repeats)", len(calls) - before == 1)

log = _Log()
ok = p._plex_refresh_show("A Show Plex Has Never Heard Of", cfg, log)
check("unknown show is skipped, not guessed", ok is False)
check("skip is explained in the log",
      any("no Plex show matching" in ln for ln in log.lines))

log = _Log()
ok = p._plex_refresh_show("The Block", {"plex_url": "", "plex_token": ""}, log)
check("unconfigured Plex does nothing", ok is False and not log.lines)


def _boom(req, timeout=None):
    raise OSError("connection refused")


urllib.request.urlopen = _boom
p2 = mod.Plugin()
log = _Log()
ok = p2._plex_refresh_show("The Block", cfg, log)
check("unreachable Plex fails soft", ok is False)

print("\n  %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
