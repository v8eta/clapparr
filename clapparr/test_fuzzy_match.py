"""Guard tests for the TVmaze fuzzy artwork fallback.

Loose title matching is how the wrong show's poster ends up on a recording, so
every rule that exists to prevent that has a case here -- including the real
ambiguity that motivated the country tie-break (TVmaze lists both an AU and a
US "Highway Patrol").
"""
import importlib.util
import json
import os
import sys
import urllib.request

spec = importlib.util.spec_from_file_location(
    "nfoplugin", os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
match = mod._fuzzy_show_match


def show(name, country=None, image=True):
    node = {"name": name, "image": {"original": "http://x/i.jpg"} if image else None}
    if country:
        node["network"] = {"country": {"code": country}}
    return {"show": node}


PASS = FAIL = 0


def check(label, got_show, got_why, expect_name):
    global PASS, FAIL
    actual = got_show.get("name") if got_show else None
    ok = actual == expect_name
    print("  %-4s %-52s -> %-18s %s" % ("ok" if ok else "FAIL", label,
                                        repr(actual), got_why[:46]))
    if ok:
        PASS += 1
    else:
        FAIL += 1


# --- the real case, against live TVmaze data -------------------------------
with urllib.request.urlopen(
    "https://api.tvmaze.com/search/shows?q=Highway%20Patrol%20Special", timeout=20
) as r:
    live_special = json.load(r)
with urllib.request.urlopen(
    "https://api.tvmaze.com/search/shows?q=Highway%20Patrol", timeout=20
) as r:
    live_hp = json.load(r)

print("live TVmaze:")
s, why = match("Highway Patrol Special", live_hp, 0.6, "AU")
check("real results, country=AU", s, why, "Highway Patrol")

s, why = match("Highway Patrol Special", live_hp, 0.6, "")
check("real results, no country preference -> ambiguous", s, why, None)

s, why = match("Highway Patrol Special", live_special, 0.6, "AU")
check("what Dispatcharr's own exact query returns", s, why, None)

# --- synthetic guards ------------------------------------------------------
print("guards:")
hp = [show("Highway Patrol", "AU"), show("Highway Patrol", "US")]

s, why = match("Highway Patrol Special", hp, 0.6, "AU")
check("country breaks the AU/US tie", s, why, "Highway Patrol")

s, why = match("Highway Patrol Special", hp, 0.6, "GB")
check("country matches nothing -> falls back, still ambiguous", s, why, None)

s, why = match("Highway Patrol Special", [show("Highway Patrol", "AU")], 0.9, "AU")
check("coverage 0.67 below a 0.90 threshold", s, why, None)

s, why = match("News Special", [show("News", "AU")], 0.4, "AU")
check("bare 'News' too short to swallow 'News Special'", s, why, None)

s, why = match("Highway to Heaven", [show("Highway Patrol", "AU")], 0.5, "AU")
check("not a word-prefix -> rejected", s, why, None)

s, why = match("Highway Patrol Special", [show("Highway Patrol", "AU", image=False)],
                0.6, "AU")
check("candidate carries no image", s, why, None)

s, why = match("Highway Patrol Special Investigation Unit Extra",
               [show("Highway Patrol", "AU")], 0.6, "AU")
check("coverage 2/7 too thin", s, why, None)

s, why = match("The Block", [show("The Block", "AU")], 0.6, "AU")
check("exact name still matches", s, why, "The Block")

s, why = match("Highway Patrol Special", [show("Highway Patrolling", "AU")], 0.6, "AU")
check("prefix must be whole WORDS, not characters", s, why, None)

s, why = match("Highway Patrol Special", "not-a-list", 0.6, "AU")
check("malformed response handled", s, why, None)

s, why = match("", hp, 0.6, "AU")
check("empty title", s, why, None)


# --- end-to-end through the real lookup, including query trimming ----------
# TVmaze returns 0 results for "Highway Patrol Special", so this only works if
# the query is trimmed to "highway patrol" while acceptance is still judged
# against the full title.
print("end-to-end (live):")


class _Log:
    def info(self, *a):
        pass

    def debug(self, *a):
        pass


def cfg(country, coverage=0.6):
    return {"fuzzy_coverage": coverage, "country": country}


p = mod.Plugin()
url = p._tvmaze_poster("Highway Patrol Special", cfg("AU"), _Log())
ok = bool(url) and url.startswith("http")
print("  %-4s %-52s -> %s" % ("ok" if ok else "FAIL",
                              "trims query, resolves a poster URL",
                              (url or "None")[:52]))
PASS, FAIL = (PASS + 1, FAIL) if ok else (PASS, FAIL + 1)

p2 = mod.Plugin()
url2 = p2._tvmaze_poster("Highway Patrol Special", cfg(""), _Log())
ok2 = url2 is None
print("  %-4s %-52s -> %s" % ("ok" if ok2 else "FAIL",
                              "no country preference stays ambiguous",
                              url2))
PASS, FAIL = (PASS + 1, FAIL) if ok2 else (PASS, FAIL + 1)

# a title with no plausible base must not resolve to something unrelated
p3 = mod.Plugin()
url3 = p3._tvmaze_poster("Wollongong Council Planning Meeting", cfg("AU"), _Log())
ok3 = url3 is None
print("  %-4s %-52s -> %s" % ("ok" if ok3 else "FAIL",
                              "unknown local programme resolves to nothing",
                              url3))
PASS, FAIL = (PASS + 1, FAIL) if ok3 else (PASS, FAIL + 1)

# the cache must serve the second call without another HTTP round trip
before = dict(p._tvmaze_cache)
p._tvmaze_poster("Highway Patrol Special", cfg("AU"), _Log())
ok4 = p._tvmaze_cache == before
print("  %-4s %-52s -> %s" % ("ok" if ok4 else "FAIL",
                              "second lookup served from cache", ok4))
PASS, FAIL = (PASS + 1, FAIL) if ok4 else (PASS, FAIL + 1)

# The cache holds the matched SHOW, not just its poster URL, so the artwork and
# the external ids used for RPDB can never come from two different matches.
cached = p._tvmaze_cache.get("Highway Patrol Special") or {}
ok5 = bool(cached.get("name")) and "externals" in cached
print("  %-4s %-52s -> %s" % ("ok" if ok5 else "FAIL",
                              "cache holds the show, with externals",
                              cached.get("name")))
PASS, FAIL = (PASS + 1, FAIL) if ok5 else (PASS, FAIL + 1)

ids = p._tvmaze_ids("Highway Patrol Special", cfg("AU"), _Log())
ok6 = isinstance(ids, dict) and set(ids) == {"tvdb", "imdb"}
print("  %-4s %-52s -> %s" % ("ok" if ok6 else "FAIL",
                              "ids come from that same cached match", ids))
PASS, FAIL = (PASS + 1, FAIL) if ok6 else (PASS, FAIL + 1)

print("\n  %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
