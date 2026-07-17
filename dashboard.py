#!/usr/bin/env python3
"""GHGA service-landscape dashboard — backend.

Serves ``dashboard.html`` and a ``/api/data`` JSON endpoint that aggregates,
for every configured ``ghga-de`` repository:

  * latest release        — version + date
  * latest pre-release     — version + date
  * open pull requests     — count (+ list)
  * main vs latest release — are there unreleased commits on the default branch?

For the ``file-services-backend`` monorepo it additionally reports each
service's version (from ``services/<svc>/pyproject.toml``) at the latest
release tag and on ``main``, so you can see which service drifted.

Run
---
    export GITHUB_TOKEN=ghp_xxxxxxxx       # PAT with read access to the repos
    python3 dashboard.py                   # then open http://127.0.0.1:8888
    python3 dashboard.py build data.json   # build the static payload and exit
                                           # (used by the GitHub Pages workflow)

A token is effectively required: GitHub allows only 60 unauthenticated
requests/hour, and one refresh makes ~80 calls. For public repos a token with
no scopes is enough; private repos need read (classic: ``repo``) access.

Environment knobs: GITHUB_TOKEN / GH_TOKEN, PORT (8888), HOST (127.0.0.1),
CACHE_TTL (seconds, 60), MONOREPO_DETAIL (1 to fetch per-service versions),
HTTP_RETRIES (attempts per request on transient errors, 3), LOG_LEVEL (INFO).
"""

__version__ = "1.0.0"

import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

OWNER = "ghga-de"

# Standalone repositories — from latest_releases.sh (de-duplicated).
STANDALONE_REPOS = [
    "dataset-information-service",
    "datahub-file-service",
    "ghga-connector",
    "ghga-registry-service",
    "auth-service",
    "data-portal",
    "work-package-service",
    "access-request-service",
    "mass",
    "metldata",
    "well-known-value-service",
    "dlq-service",
    "notification-service",
    "notification-orchestration-service",
    "reverse-transpiler-service",
    "state-management-service",
]

# Monorepo, several services, each with its
# own version in services/<svc>/pyproject.toml.
MONOREPO = "file-services-backend"
FSB_SERVICES = {
    "dcs": "ghga/download-controller-service",
    "ekss": "ghga/encryption-key-store-service",
    "ucs": "ghga/upload-controller-service",
    "ifrs": "ghga/internal-file-registry-service",
    "fis": "ghga/file-ingest-service",
    "pcs": "ghga/purge-controller-service",
}

# Integrating test bed: a (private) repo whose devcontainer docker-compose pins
# the exact image version of each service currently under testing.
# Pulled once per build; each ghga/<name>:<version> image is matched to a tracked
# repo (ghga/<repo>) or a monorepo service (an FSB_SERVICES image).
TESTBED_REPO = os.environ.get("TESTBED_REPO", "archive-test-bed")
TESTBED_REF = os.environ.get("TESTBED_REF", "main")
TESTBED_COMPOSE_PATH = os.environ.get(
    "TESTBED_COMPOSE_PATH", ".devcontainer/docker-compose.yml"
)

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
PORT = int(os.environ.get("PORT", "8888"))
HOST = os.environ.get("HOST", "127.0.0.1")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))
MONOREPO_DETAIL = os.environ.get("MONOREPO_DETAIL", "1") not in ("0", "false", "no")
HTTP_RETRIES = max(1, int(os.environ.get("HTTP_RETRIES", "3")))
# Minimum gap between outgoing request starts (seconds), enforced across all
# worker threads. Spacing requests out avoids the DNS-resolver burst and
# GitHub's secondary-rate-limit (abuse) detector.
REQUEST_SPACING = float(os.environ.get("REQUEST_SPACING", "0.05"))

API = "https://api.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "index.html")
VERSION_RE = re.compile(r'(?m)^version\s*=\s*"([^"]+)"')
LAST_PAGE_RE = re.compile(r'<[^>]*[?&]page=(\d+)[^>]*>;\s*rel="last"')
# Matches a docker-compose image line like:  image: ghga/<name>:<version>
COMPOSE_IMAGE_RE = re.compile(
    r'^\s*image:\s*["\']?ghga/([A-Za-z0-9._-]+):([A-Za-z0-9._+-]+)["\']?\s*$'
)

log = logging.getLogger("dashboard")


# --------------------------------------------------------------------------- #
# GitHub HTTP helpers
# --------------------------------------------------------------------------- #


def _short(url):
    """Trim the API prefix so log lines stay readable."""
    return url.replace(API, "") or url


def _backoff(attempt):
    """Exponential backoff with jitter: ~0.25s, 0.5s, 1s … capped at 2s."""
    return min(0.25 * (2 ** (attempt - 1)), 2.0) + random.random() * 0.2


def _retry_after(exc):
    """Seconds to wait for a secondary-rate-limit response, or None.

    Only honoured when GitHub sends a short Retry-After (abuse detection);
    primary rate-limit exhaustion has no Retry-After and is not retried.
    """
    ra = (exc.headers.get("Retry-After") or "").strip()
    if ra.isdigit():
        return min(int(ra), 8)
    return None


_throttle_lock = threading.Lock()
_last_request_at = [0.0]


def _throttle():
    """Block until at least REQUEST_SPACING has elapsed since the last request
    start. The lock is held across the sleep so concurrent threads queue and
    their request starts come out evenly spaced rather than in a burst."""
    if REQUEST_SPACING <= 0:
        return
    with _throttle_lock:
        now = time.monotonic()
        wait = _last_request_at[0] + REQUEST_SPACING - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_request_at[0] = now


def _request_once(url, accept, raw):
    _throttle()
    headers = {
        "Accept": accept,
        "User-Agent": "ghga-dashboard",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = resp.read()
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
    if raw:
        return body, hdrs
    return (json.loads(body.decode("utf-8")) if body else None), hdrs


def _request(url, accept="application/vnd.github+json", raw=False):
    """Perform a GitHub request, retrying transient failures. Returns (payload, headers).

    Retried: DNS / connection failures (incl. the macOS getaddrinfo "Errno 8"
    hiccup seen under concurrent load), timeouts, HTTP 5xx, and secondary
    rate-limit responses carrying a short Retry-After. NOT retried: deterministic
    errors (404) or primary rate-limit exhaustion — those fail fast with a clear
    message. Raises urllib HTTPError on the final non-2xx so callers can still
    branch on status codes (404, 403, ...).
    """
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            return _request_once(url, accept, raw)
        except urllib.error.HTTPError as exc:
            wait = None
            if exc.code in (500, 502, 503, 504):
                wait = _backoff(attempt)
            elif exc.code in (403, 429):
                wait = _retry_after(exc)  # only set for a short secondary limit
            if wait is None or attempt == HTTP_RETRIES:
                raise
            log.warning(
                "GitHub %d on %s (attempt %d/%d) — retrying in %.1fs",
                exc.code, _short(url), attempt, HTTP_RETRIES, wait,
            )
            time.sleep(wait)
        except (urllib.error.URLError, OSError) as exc:
            # URLError wraps DNS/connection errors; raw OSError covers socket
            # timeouts and resets. Both are usually transient.
            if attempt == HTTP_RETRIES:
                raise
            reason = getattr(exc, "reason", None) or exc
            wait = _backoff(attempt)
            log.warning(
                "network error on %s (attempt %d/%d): %s — retrying in %.1fs",
                _short(url), attempt, HTTP_RETRIES, reason, wait,
            )
            time.sleep(wait)
    # Unreachable: the loop returns on success or raises on the final attempt.
    raise RuntimeError(f"request to {_short(url)} exhausted retries")


def gh(path, params=None, accept="application/vnd.github+json", raw=False):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _request(url, accept=accept, raw=raw)


# --------------------------------------------------------------------------- #
# Per-aspect collectors
# --------------------------------------------------------------------------- #


def _fmt_release(rel):
    if not rel:
        return None
    return {
        "tag": rel.get("tag_name"),
        "name": rel.get("name") or rel.get("tag_name"),
        "published_at": rel.get("published_at") or rel.get("created_at"),
        "html_url": rel.get("html_url"),
    }


def release_info(repo):
    """Latest release (per GitHub's own "Latest" marking) and latest pre-release.

    GitHub lets a maintainer choose which release is "Latest" (the make_latest
    flag / green badge). That is NOT necessarily the newest by date or the top
    of the list — e.g. a backport patch published after a newer major is not
    marked latest. The /releases/latest endpoint returns exactly the release
    GitHub considers latest, so we trust it rather than scanning the list.

    The releases list is still fetched, but only to find the newest pre-release
    (which /releases/latest never returns) and a total count.
    """
    # Authoritative latest full release — honours the "Latest" marking.
    latest = None
    try:
        latest, _ = gh(f"/repos/{OWNER}/{repo}/releases/latest")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:  # 404 => repo has no published full release at all
            raise

    # Newest pre-release (and a count) from the list, which is newest-first.
    data, _ = gh(f"/repos/{OWNER}/{repo}/releases", {"per_page": "100"})
    prerelease = None
    for rel in data or []:
        if rel.get("draft") or not rel.get("prerelease"):
            continue
        prerelease = rel
        break

    return {
        "latest": _fmt_release(latest),
        "prerelease": _fmt_release(prerelease),
        "count": len(data or []),
    }


def open_pr_info(repo):
    """Open PRs: exact count where possible, plus a capped list for display."""
    data, _ = gh(
        f"/repos/{OWNER}/{repo}/pulls",
        {"state": "open", "per_page": "100", "sort": "updated", "direction": "desc"},
    )
    items = [
        {
            "number": p["number"],
            "title": p["title"],
            "html_url": p["html_url"],
            "draft": p.get("draft", False),
            "user": (p.get("user") or {}).get("login"),
            "updated_at": p.get("updated_at"),
        }
        for p in (data or [])
    ]
    return {"count": len(items), "capped": len(items) == 100, "items": items[:50]}


def _compare(repo, base, head):
    base_q = urllib.parse.quote(base, safe="")
    head_q = urllib.parse.quote(head, safe="")
    data, _ = gh(f"/repos/{OWNER}/{repo}/compare/{base_q}...{head_q}")
    return {
        "status": data.get("status"),  # ahead / behind / identical / diverged
        "ahead_by": data.get("ahead_by", 0),  # commits on the branch not yet released
        "behind_by": data.get("behind_by", 0),
        "total_commits": data.get("total_commits", 0),
        "branch": head,
        "base": base,
        "html_url": data.get("html_url"),
    }


def main_status(repo, base_tag):
    """Compare latest release tag against the default branch.

    ahead_by > 0  =>  there are commits on the branch not in the release.
    Assumes 'main'; falls back to the repo's real default branch on 404.
    """
    if not base_tag:
        return None
    try:
        return _compare(repo, base_tag, "main")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    # 'main' didn't resolve, so look up the real default branch and retry once.
    try:
        meta, _ = gh(f"/repos/{OWNER}/{repo}")
        branch = meta.get("default_branch") or "master"
        if branch != "main":
            return _compare(repo, base_tag, branch)
    except urllib.error.HTTPError:
        pass
    return {"error": "could not compare branch to release tag"}


# --------------------------------------------------------------------------- #
# Monorepo per-service versions
# --------------------------------------------------------------------------- #


def _service_version(svc, ref):
    """Read services/<svc>/pyproject.toml at a ref and extract its version.

    Best-effort: any failure (missing file, network, rate limit) returns None so
    a single service can't fail the whole monorepo row — it just shows "—".
    """
    if not ref:
        return None
    path = f"/repos/{OWNER}/{MONOREPO}/contents/services/{svc}/pyproject.toml"
    try:
        body, _ = gh(path, {"ref": ref}, accept="application/vnd.github.raw", raw=True)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        log.warning("monorepo service %s@%s: %s — version unavailable", svc, ref, reason)
        return None
    m = VERSION_RE.search(body.decode("utf-8", "replace"))
    return m.group(1) if m else None


def fsb_services(tag, compose=None):
    """Per-service version at the latest release tag, on main, and in the test bed."""

    def one(svc):
        released = _service_version(svc, tag)
        on_main = _service_version(svc, "main")
        image = FSB_SERVICES[svc]
        return {
            "service": svc,
            "image": image,
            "released": released,
            "main": on_main,
            "changed": bool(released and on_main and released != on_main),
            # test-bed version compared against this service's released version
            "testbed": _testbed_entry(compose, image, released),
        }

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(one, FSB_SERVICES.keys()))


# --------------------------------------------------------------------------- #
# Integration test bed (archive-test-bed docker-compose)
# --------------------------------------------------------------------------- #


def _parse_compose(text):
    """Map every ``ghga/<name>`` image in the compose to its version(s).

    Multiple instances of a service (rest, consumer, init, …) reuse the same
    image, so versions are collected into a set and collapsed to the unique
    value. A 'conflict' flag is set in the rare case instances disagree.
    """
    found = {}  # image -> set of versions seen
    for line in text.splitlines():
        m = COMPOSE_IMAGE_RE.match(line)
        if m:
            found.setdefault("ghga/" + m.group(1), set()).add(m.group(2))
    result = {}
    for image, versions in found.items():
        ordered = sorted(versions)
        result[image] = {
            "version": ordered[0] if len(ordered) == 1 else " / ".join(ordered),
            "versions": ordered,
            "conflict": len(ordered) > 1,
        }
    return result


def fetch_testbed_versions(ref=None):
    """Pull the test-bed docker-compose for a ref and return the parsed image map.

    ref defaults to TESTBED_REF ('main'); pass a branch name to read that branch.
    """
    ref = ref or TESTBED_REF
    path = f"/repos/{OWNER}/{TESTBED_REPO}/contents/{TESTBED_COMPOSE_PATH}"
    meta = {"repo": TESTBED_REPO, "ref": ref, "path": TESTBED_COMPOSE_PATH}
    try:
        body, _ = gh(path, {"ref": ref}, accept="application/vnd.github.raw", raw=True)
    except urllib.error.HTTPError as exc:
        meta["error"] = (
            f"branch '{ref}' or compose not found" if exc.code == 404 else _http_error_text(exc)
        )
        log.warning("test-bed compose @%s unavailable: %s", ref, meta["error"])
        return {"versions": {}, **meta}
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        meta["error"] = f"network error: {reason}"
        log.warning("test-bed compose @%s fetch failed: %s", ref, reason)
        return {"versions": {}, **meta}

    versions = _parse_compose((body or b"").decode("utf-8", "replace"))
    tracked = {f"ghga/{r}" for r in STANDALONE_REPOS} | set(FSB_SERVICES.values())
    untracked = sorted(img for img in versions if img not in tracked)
    log.info(
        "test-bed %s@%s: %d ghga images (%d tracked, %d untracked)",
        TESTBED_REPO, ref, len(versions), len(versions) - len(untracked), len(untracked),
    )
    return {"versions": versions, "untracked": untracked, "count": len(versions), **meta}


def _semver(v):
    """Parse 'X.Y.Z…' into a tuple of ints (ignoring any -pre/+build), or None."""
    if not v:
        return None
    core = re.split(r"[-+]", v, maxsplit=1)[0]
    try:
        return tuple(int(p) for p in core.split("."))
    except ValueError:
        return None


def _cmp_versions(testbed_v, latest_v):
    """How the test-bed version relates to the latest released version:
    match / behind / ahead / differs / unknown."""
    if not testbed_v or not latest_v:
        return "unknown"
    if testbed_v == latest_v:
        return "match"
    a, b = _semver(testbed_v), _semver(latest_v)
    if a is None or b is None:
        return "differs"
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return "behind" if a < b else ("ahead" if a > b else "match")


def _testbed_entry(compose, image, latest_version):
    """Test-bed pin for one image, compared to its latest released version."""
    entry = (compose or {}).get(image)
    if not entry:
        return None
    return {
        "version": entry["version"],
        "image": image,
        "conflict": entry.get("conflict", False),
        "vs_latest": "conflict" if entry.get("conflict") else _cmp_versions(entry["version"], latest_version),
    }


# --------------------------------------------------------------------------- #
# Per-repo assembly
# --------------------------------------------------------------------------- #


def build_repo(repo, monorepo=False, compose=None):
    out = {
        "repo": repo,
        "url": f"https://github.com/{OWNER}/{repo}",
        "monorepo": monorepo,
    }
    try:
        rel = release_info(repo)
        out["latest_release"] = rel["latest"]
        out["latest_prerelease"] = rel["prerelease"]
        out["release_count"] = rel["count"]

        out["open_prs"] = open_pr_info(repo)

        base = None
        if rel["latest"]:
            base = rel["latest"]["tag"]
        elif rel["prerelease"]:
            base = rel["prerelease"]["tag"]
        out["main"] = main_status(repo, base)

        if monorepo and MONOREPO_DETAIL:
            # the monorepo has no single image; test-bed versions live per service
            out["services"] = fsb_services(base, compose)
        elif not monorepo:
            latest_tag = out["latest_release"]["tag"] if out.get("latest_release") else None
            out["testbed"] = _testbed_entry(compose, f"ghga/{repo}", latest_tag)
    except urllib.error.HTTPError as exc:
        out["error"] = _http_error_text(exc)
        log.warning("repo %s failed: %s (HTTP %s)", repo, out["error"], exc.code)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        out["error"] = f"network error: {reason}"
        log.warning("repo %s failed after %d attempts: network error: %s",
                    repo, HTTP_RETRIES, reason)
    except Exception as exc:  # noqa: BLE001 - surface anything else in the UI
        out["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("repo %s failed with unexpected error", repo)
    return out


def _http_error_text(exc):
    if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
        reset = exc.headers.get("X-RateLimit-Reset")
        when = ""
        if reset:
            try:
                when = " — resets " + time.strftime(
                    "%H:%M:%S UTC", time.gmtime(int(reset))
                )
            except ValueError:
                pass
        return "GitHub rate limit exceeded" + when
    if exc.code == 404:
        return "not found (renamed, or token lacks access)"
    return f"HTTP {exc.code} {exc.reason}"


def rate_limit():
    try:
        data, _ = gh("/rate_limit")
        core = (data or {}).get("resources", {}).get("core", {})
        return {
            "remaining": core.get("remaining"),
            "limit": core.get("limit"),
            "reset": core.get("reset"),
        }
    except Exception:  # noqa: BLE001
        return None


def build_payload():
    started = time.monotonic()
    # /rate_limit is free (unmetered), so reading it before and after lets us
    # report exactly how many quota units THIS refresh actually consumed.
    rate_before = rate_limit()
    log.info("building dashboard for %d repositories…", len(STANDALONE_REPOS) + 1)

    # Pull the integration test-bed compose once; every repo/service reuses it.
    testbed = fetch_testbed_versions()
    compose = testbed.get("versions", {})

    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(build_repo, r, False, compose): r for r in STANDALONE_REPOS}
        mono_future = pool.submit(build_repo, MONOREPO, True, compose)
        for fut, repo in futures.items():
            results[repo] = fut.result()
        results[MONOREPO] = mono_future.result()

    ordered = [results[MONOREPO]] + [results[r] for r in STANDALONE_REPOS]
    rate = rate_limit()
    errored = [r["repo"] for r in ordered if r.get("error")]
    elapsed = time.monotonic() - started

    # Actual calls spent on this build = drop in 'remaining' (the two free
    # /rate_limit reads don't decrement it, so they don't skew the delta).
    used = None
    if rate_before and rate and rate_before.get("remaining") is not None and rate.get("remaining") is not None:
        used = rate_before["remaining"] - rate["remaining"]
        rate = dict(rate, used_last_build=used)
    cost = f"{used} calls" if used is not None else "calls: n/a"

    if errored:
        log.warning("built %d repos in %.1fs — %d with errors: %s; this refresh spent %s; API remaining %s/%s",
                    len(ordered), elapsed, len(errored), ", ".join(errored), cost,
                    (rate or {}).get("remaining"), (rate or {}).get("limit"))
    else:
        log.info("built %d repos in %.1fs — all OK; this refresh spent %s; API remaining %s/%s",
                 len(ordered), elapsed, cost,
                 (rate or {}).get("remaining"), (rate or {}).get("limit"))
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "owner": OWNER,
        "authenticated": bool(TOKEN),
        "rate": rate,
        # test-bed meta for the UI (drop the large per-image map; repos carry their own)
        "testbed": {k: v for k, v in testbed.items() if k != "versions"},
        "repos": ordered,
    }


# --------------------------------------------------------------------------- #
# Cache (so rapid page refreshes don't re-hammer GitHub)
# --------------------------------------------------------------------------- #

_cache = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()


def get_data(force=False):
    with _cache_lock:
        fresh = (
            _cache["data"] is not None
            and (time.time() - _cache["at"]) < CACHE_TTL
            and not force
        )
        if fresh:
            return _cache["data"], True
        data = build_payload()
        _cache["data"] = data
        _cache["at"] = time.time()
        return data, False


# Per-branch test-bed override: recompute only the Test-bed column for a chosen
# branch by re-reading just that branch's compose (1 GitHub call) and reusing the
# already-cached release data, instead of rebuilding the whole dashboard.
_tb_cache = {}  # ref -> (timestamp, result)
_tb_lock = threading.Lock()


def _compute_testbed_overrides(ref):
    data, _ = get_data()  # current dashboard payload (cached when warm)
    tb = fetch_testbed_versions(ref)
    compose = tb.get("versions", {})
    repos_over, services_over = {}, {}
    for r in data.get("repos", []):
        if r.get("monorepo"):
            for s in r.get("services", []):
                services_over[s["service"]] = _testbed_entry(
                    compose, s.get("image"), s.get("released")
                )
        else:
            latest_tag = (r.get("latest_release") or {}).get("tag")
            repos_over[r["repo"]] = _testbed_entry(compose, f"ghga/{r['repo']}", latest_tag)
    return {
        "repo": tb.get("repo"),
        "ref": tb.get("ref", ref),
        "count": tb.get("count", 0),
        "untracked": tb.get("untracked", []),
        "error": tb.get("error"),
        "repos": repos_over,
        "services": services_over,
    }


def get_testbed_overrides(ref, force=False):
    with _tb_lock:
        hit = _tb_cache.get(ref)
        if hit and not force and (time.time() - hit[0]) < CACHE_TTL:
            return hit[1]
    result = _compute_testbed_overrides(ref)
    if not result.get("error"):  # don't cache failures — let a retry try again
        with _tb_lock:
            _tb_cache[ref] = (time.time(), result)
    return result


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #


class Handler(BaseHTTPRequestHandler):
    server_version = "ghga-dashboard/" + __version__

    def log_message(self, fmt, *args):  # route access logs through the logger
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html", "/dashboard.html"):
            try:
                with open(HTML_PATH, "rb") as fh:
                    body = fh.read()
            except OSError:
                self._send(500, b"index.html not found next to dashboard.py", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return

        if path == "/api/data":
            qs = urllib.parse.parse_qs(parsed.query)
            force = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
            try:
                data, cached = get_data(force=force)
                payload = dict(data, cached=cached)
                self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            except Exception as exc:  # noqa: BLE001
                err = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
                self._send(500, err, "application/json")
            return

        if path == "/api/testbed":
            qs = urllib.parse.parse_qs(parsed.query)
            ref = (qs.get("ref", [""])[0] or TESTBED_REF).strip()[:200]
            force = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
            try:
                self._send(200, json.dumps(get_testbed_overrides(ref, force=force)).encode("utf-8"),
                           "application/json")
            except Exception as exc:  # noqa: BLE001
                err = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
                self._send(500, err, "application/json")
            return

        if path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")

    do_HEAD = do_GET


def _setup_logging():
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def build_static(out_path="data.json"):
    """Build the dashboard payload once and write it to a JSON file.

    This is what the GitHub Pages build (a scheduled Action) runs so the
    published page can read a prebuilt data.json instead of ever handling a
    GitHub token in the browser. The token is read from the environment exactly
    as the server does. The volatile 'rate' block is dropped (it reflects the
    build's own API quota, meaningless to a viewer) and 'mode' is stamped so the
    page can tell it is serving prebuilt data.
    """
    if not TOKEN:
        log.warning(
            "no GITHUB_TOKEN/GH_TOKEN set — the build will hit GitHub's 60/hour "
            "unauthenticated limit and most rows will show errors"
        )
    data = build_payload()
    static = {k: v for k, v in data.items() if k != "rate"}
    static["mode"] = "static"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(static, fh, separators=(",", ":"))
    log.info("wrote %s — %d repos, generated_at %s",
             out_path, len(static.get("repos", [])), static.get("generated_at"))
    return static


def main():
    _setup_logging()
    if not TOKEN:
        log.warning(
            "no GITHUB_TOKEN/GH_TOKEN set — GitHub permits only 60 unauthenticated "
            "requests/hour and one refresh needs ~80. Set a token "
            "(public repos: any PAT; private: read access): export GITHUB_TOKEN=ghp_xxxxxxxx"
        )
    else:
        log.info("using GITHUB_TOKEN (%d chars)", len(TOKEN))

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("ghga-dashboard v%s serving at http://%s:%d - refresh the page to update (Ctrl-C to stop)",
             __version__, HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    # `python3 dashboard.py build [out.json]` builds the static payload and exits
    # (used by the Pages workflow); with no args it runs the live server.
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        _setup_logging()
        build_static(sys.argv[2] if len(sys.argv) > 2 else "data.json")
    else:
        main()
