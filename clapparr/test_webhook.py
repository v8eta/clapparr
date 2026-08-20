"""Tests for the post-write webhook.

Two things here must not be wrong. Credentials placed in the URL have to be
moved into a header and stripped BEFORE the request is built, or a urllib
exception -- which quotes the URL back -- leaks them into the log. And the path
has to be rewritten into whatever the notified service calls it, because
Dispatcharr's container path is not what a media server sees.

No network access: urlopen is replaced with a recorder.
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

PASS = FAIL = 0
captured = {}


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return 200


def _recorder(req, timeout=None):
    captured.clear()
    captured["url"] = req.full_url
    captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
    captured["method"] = req.get_method()
    captured["body"] = req.data
    return _Resp()


def _boom(req, timeout=None):
    raise urllib.error.HTTPError(req.full_url, 500, "boom", None, None)


urllib.request.urlopen = _recorder


class _Log:
    def __init__(self):
        self.lines = []

    def _rec(self, fmt, *a):
        self.lines.append(fmt % a if a else fmt)

    info = warning = debug = _rec


def cfg(**kw):
    base = {
        "webhook_url": "",
        "webhook_method": "GET",
        "webhook_from": "",
        "webhook_to": "",
        "webhook_header": "",
    }
    base.update(kw)
    return base


PLAN = {
    "mkv": "/data/recordings/TV_Shows/Seven News/Seven News - S26E231 - 2026-08-19.mkv",
    "show": "Seven News",
}


def check(label, cond, detail=""):
    global PASS, FAIL
    print("  %-4s %-54s %s" % ("ok" if cond else "FAIL", label, detail[:44]))
    if cond:
        PASS += 1
    else:
        FAIL += 1


p = mod.Plugin()

# --- path rewriting --------------------------------------------------------
print("path rewrite:")
check("prefix replaced",
      mod._rewrite_path("/data/recordings/TV_Shows/A/b.mkv",
                        "/data/recordings/TV_Shows", "/mnt/unionfs/dvr")
      == "/mnt/unionfs/dvr/A/b.mkv")
check("non-matching prefix left alone",
      mod._rewrite_path("/elsewhere/A/b.mkv", "/data/recordings", "/mnt") ==
      "/elsewhere/A/b.mkv")
check("blank rewrite is a no-op",
      mod._rewrite_path("/data/x.mkv", "", "") == "/data/x.mkv")

# --- credentials -----------------------------------------------------------
print("credentials:")
log = _Log()
ok = p._fire_webhook(PLAN, cfg(
    webhook_url="http://touki:s3cr3t@autopulse:2875/triggers/manual?path={path}",
    webhook_from="/data/recordings/TV_Shows", webhook_to="/mnt/unionfs/dvr",
), log)
check("request was sent", ok)
check("userinfo stripped from URL", "s3cr3t" not in captured["url"], captured["url"][:40])
check("Authorization header set", captured["headers"].get("authorization", "").startswith("Basic "))
check("host and port preserved", "autopulse:2875" in captured["url"])
check("rewritten path in query", "/mnt/unionfs/dvr/Seven%20News/" in captured["url"]
      or "/mnt/unionfs/dvr/Seven News/" in captured["url"], captured["url"][-46:])
check("no credential in any log line", all("s3cr3t" not in ln for ln in log.lines))

log = _Log()
p._fire_webhook(PLAN, cfg(webhook_url="http://autopulse:2875/t?path={path}"), log)
check("no credentials -> no Authorization header",
      "authorization" not in captured["headers"])

# a failing request must not put the URL's credentials in the log either
urllib.request.urlopen = _boom
log = _Log()
ok = p._fire_webhook(PLAN, cfg(
    webhook_url="http://touki:s3cr3t@autopulse:2875/triggers/manual?path={path}"), log)
check("failure returns False", ok is False)
check("failure log carries no credential", all("s3cr3t" not in ln for ln in log.lines),
      "; ".join(log.lines)[:44])
urllib.request.urlopen = _recorder

# --- method, headers, templating ------------------------------------------
print("request shape:")
log = _Log()
p._fire_webhook(PLAN, cfg(webhook_url="http://x/y?p={path}",
                          webhook_header="X-Api-Key: abc123"), log)
check("extra header applied", captured["headers"].get("x-api-key") == "abc123")

log = _Log()
p._fire_webhook(PLAN, cfg(webhook_url="http://x/y", webhook_method="POST"), log)
check("POST sends JSON", captured["method"] == "POST"
      and json.loads(captured["body"])["show"] == "Seven News")

log = _Log()
p._fire_webhook(PLAN, cfg(webhook_url="http://x/y?d={dir}&f={file}&s={show}"), log)
check("dir/file/show placeholders resolve",
      "Seven%20News" in captured["url"] and "S26E231" in captured["url"])

captured.clear()
log = _Log()
ok = p._fire_webhook(PLAN, cfg(webhook_url="http://x/y?p={nonexistent}"), log)
check("unknown placeholder refuses to send", ok is False and not captured)

log = _Log()
ok = p._fire_webhook(PLAN, cfg(webhook_url=""), log)
check("blank URL does nothing", ok is False)

print("\n  %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
