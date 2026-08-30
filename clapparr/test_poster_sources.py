#!/usr/bin/env python3
"""Unit tests for poster source selection, grading and the RPDB URL.

No API key, no network, no Dispatcharr. Run: python3 test_poster_sources.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plugin as P  # noqa: E402

fails = []


def check(name, got, want=True):
    ok = (got == want)
    print(("  ok   " if ok else "  FAIL ") + name
          + ("" if ok else "   got %r want %r" % (got, want)))
    if not ok:
        fails.append(name)


# ---- _tvdb_id: one derivation, shared by the NFO and the RPDB URL ----------
check("tvdb id strips the 'series/' prefix",
      P._tvdb_id({"thetvdb.com_id": "series/260092"}), "260092")
check("a bare id passes through", P._tvdb_id({"thetvdb.com_id": "74768"}), "74768")
check("missing key yields empty", P._tvdb_id({}), "")
check("None props yield empty", P._tvdb_id(None), "")
check("whitespace-only yields empty", P._tvdb_id({"thetvdb.com_id": "   "}), "")


# ---- _image_dims ----------------------------------------------------------
def _png(w, h):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + w.to_bytes(4, "big") + h.to_bytes(4, "big"))


def _jpeg(w, h):
    return (b"\xff\xd8\xff\xc0\x00\x11\x08"
            + h.to_bytes(2, "big") + w.to_bytes(2, "big"))


tmpd = tempfile.mkdtemp(prefix="posterprobe-")


def _write(name, data):
    p = os.path.join(tmpd, name)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


check("PNG dimensions read from IHDR",
      P._image_dims(_write("a.png", _png(680, 1000))), (680, 1000))
check("JPEG dimensions read from SOF0",
      P._image_dims(_write("a.jpg", _jpeg(1920, 2880))), (1920, 2880))
check("a non-image is not a size",
      P._image_dims(_write("a.txt", b"not an image at all")), None)
check("a missing file is not a size",
      P._image_dims(os.path.join(tmpd, "nope.jpg")), None)
check("a truncated JPEG does not hang or raise",
      P._image_dims(_write("t.jpg", b"\xff\xd8\xff")), None)

# ⚠️ A header parser validated only against bytes this test wrote itself proves
# only that it agrees with its author. Check it against a REAL encoder, and
# against that encoder's own probe, whenever one is present.
try:
    real = os.path.join(tmpd, "real.jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=blue:s=734x1080", "-frames:v", "1", real],
                   check=True, timeout=60)
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", real],
        check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    w, h = (int(x) for x in probed.split(",")[:2])
    check("agrees with ffprobe on a real ffmpeg JPEG", P._image_dims(real), (w, h))
except (OSError, subprocess.SubprocessError) as e:
    print("  skip  real-encoder cross-check (ffmpeg unavailable: %s)" % e)


# ---- _poster_grade --------------------------------------------------------
check("a portrait poster is accepted",
      P._poster_grade(_write("p1.jpg", _jpeg(680, 1000)))[0], True)
check("a square image is accepted",
      P._poster_grade(_write("p2.jpg", _jpeg(600, 600)))[0], True)
check("a LANDSCAPE image is refused (it is a banner, not a poster)",
      P._poster_grade(_write("p3.jpg", _jpeg(1022, 576)))[0], False)
check("the refusal says why it was landscape",
      "landscape" in P._poster_grade(_write("p4.jpg", _jpeg(1022, 576)))[2], True)
check("a tiny poster is refused",
      P._poster_grade(_write("p5.jpg", _jpeg(120, 180)))[0], False)
check("grade reports the width it judged on",
      P._poster_grade(_write("p6.jpg", _jpeg(416, 624)))[1], 416)
check("a 416px portrait is valid but below the good bar",
      (P._poster_grade(_write("p7.jpg", _jpeg(416, 624)))[0],
       416 >= P._POSTER_GOOD_W), (True, False))


# ---- _rpdb_url ------------------------------------------------------------
class _Log:
    def info(self, *a, **k):
        pass

    debug = warning = error = info


class _Stub:
    """Only what _rpdb_url touches, with TVmaze stubbed rather than called."""

    def __init__(self, ids=None):
        self._ids = ids or {"tvdb": "", "imdb": ""}
        self.ids_calls = 0

    def _tvmaze_ids(self, show_title, cfg, log):
        self.ids_calls += 1
        return self._ids


LOG = _Log()


def _url(stub, plan, cfg):
    return P.Plugin._rpdb_url(stub, plan, cfg, LOG)


OFF = {"rpdb_key": "", "rpdb_type": "poster-default", "fuzzy": True}
ON = {"rpdb_key": "t3-abc123", "rpdb_type": "poster-default", "fuzzy": True}
NOFUZZ = {"rpdb_key": "t3-abc123", "rpdb_type": "poster-default", "fuzzy": False}

check("no API key means no RPDB request at all",
      _url(_Stub(), {"tvdb": "74768"}, OFF), None)

# 1. the EPG id, which costs nothing
s = _Stub({"tvdb": "999", "imdb": "tt999"})
url = _url(s, {"tvdb": "74768", "show": "X"}, ON)
check("the EPG tvdb id is preferred",
      url, "https://api.ratingposterdb.com/t3-abc123/tvdb/poster-default/"
           "series-74768.jpg?fallback=true")
check("and TVmaze is not consulted when the EPG already had an id",
      s.ids_calls, 0)
check("fallback=true is always present -- without it RPDB errors on any show "
      "it holds no ratings for", "fallback=true" in url, True)

# 2. TVmaze tvdb id when the EPG has none
s = _Stub({"tvdb": "74768", "imdb": "tt0418372"})
check("falls back to the TVmaze tvdb id",
      "tvdb/poster-default/series-74768.jpg" in _url(s, {"show": "The Block"}, ON),
      True)

# 3. IMDB id when TVmaze has no tvdb id -- used BARE, no series- prefix
s = _Stub({"tvdb": "", "imdb": "tt0418372"})
u3 = _url(s, {"show": "The Block"}, ON)
check("falls back to the imdb id", "/imdb/poster-default/tt0418372.jpg" in u3, True)
check("an imdb id is NOT series-prefixed", "series-tt" in u3, False)

check("no id anywhere means no request",
      _url(_Stub({"tvdb": "", "imdb": ""}), {"show": "Local News"}, ON), None)

s = _Stub({"tvdb": "74768", "imdb": ""})
check("fuzzy disabled means TVmaze is never consulted for an id",
      _url(s, {"show": "The Block"}, NOFUZZ), None)
check("and no lookup was attempted", s.ids_calls, 0)

odd = _url(_Stub(), {"tvdb": "a/b c"},
           {"rpdb_key": "t3-x/y", "rpdb_type": "textless-certs", "fuzzy": True})
check("key and id are URL-encoded, so neither can inject a path segment",
      ("t3-x%2Fy" in odd and "series-a%2Fb%20c" in odd), True)
check("the chosen poster style appears in the path",
      "/textless-certs/" in odd, True)


print()
if fails:
    print("FAILED: " + ", ".join(fails))
    sys.exit(1)
print("all poster-source tests passed")
