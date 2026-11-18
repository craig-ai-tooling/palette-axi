#!/usr/bin/env python3
"""palette-axi — agent-ergonomic CLI over the Spectro Cloud Palette API.

Built on the same AXI (Agent eXperience Interface) principles as opp-axi:
TOON output, minimal default schemas, truncation with --full escape hatches,
pre-computed aggregates, definitive empty states, structured exit codes,
content-first output, and next-step disclosure.

Reads AND writes since 0.6.0. Every write verb takes --dry-run (print the exact
requests, send nothing), needs an explicit --project (never $PALETTE_PROJECT),
and refuses to change or delete anything outside that project; deletes also
need --yes. See README.md "Write verbs" for the full safety model.

Auth: PALETTE_API_KEY env var wins if set. Otherwise the key is pulled from
1Password (vault "Lobster", or $PALETTE_AXI_VAULT) by resolving the item
titled "Palette API Key (<tenant>)" to its ITEM ID first — op:// references
break on titles containing parentheses, so this tool never builds one.
Default tenant is "custeng-prod"; override with --tenant or $PALETTE_AXI_TENANT.

ProjectUid: most verbs need one. Pass --project by name or UID, or set
$PALETTE_PROJECT. A name is resolved via /v1/dashboard/projects (case-insensitive
substring match); ambiguous or missing matches are a hard error listing the
candidates — this tool never guesses which project you meant.

Env overrides: PALETTE_API_KEY, PALETTE_PROJECT, PALETTE_AXI_TENANT,
PALETTE_AXI_VAULT (default Lobster), PALETTE_AXI_OP_ITEM (skip tenant
resolution and use this 1Password item id directly), PALETTE_AXI_KEY_TTL
(seconds the resolved API key is cached in $XDG_RUNTIME_DIR/palette-axi;
default 900, 0 disables caching).

The resolved API key (and the tenant->1Password-item-id mapping) is cached
under $XDG_RUNTIME_DIR/palette-axi so an agent running many verbs back to
back spends at most one `op` call total, not two per verb -- see
get_api_key()'s docstring for the incident that motivated this.

Run `palette-axi doctor` to check whether those connectors are configured.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__

API_BASE = "https://api.spectrocloud.com"
DEFAULT_TENANT = os.environ.get("PALETTE_AXI_TENANT", "custeng-prod")
OP_VAULT = os.environ.get("PALETTE_AXI_VAULT", "Lobster")
UID_RE = re.compile(r"^[0-9a-f]{24}$")
# 9/21/26 16:53Z: the shared 1Password service account started refusing item
# reads with "Too many requests. Your client has been rate-limited." An agent
# doing an inventory runs dozens of verbs in a few minutes, and every verb
# spent 2 op calls (item list + item get) re-fetching a key it already knew,
# so it burned the limit fast. These two caches cut that to at most one op
# call per TTL window, not per invocation.
KEY_CACHE_TTL_DEFAULT = 900  # seconds; override with $PALETTE_AXI_KEY_TTL (0 disables)
ITEM_CACHE_TTL = 24 * 60 * 60  # tenant -> op item id is not a secret and rarely changes

# Exit codes and TOON output helpers (_tv/toon/emit/nxt/die) live in
# palette_axi/axi.py, vendored from craig-ai-tooling/axi-py. Never redefine
# them here — see that file's header for how to change them.
from .axi import E_ERR, E_NOTFOUND, E_OK, E_USAGE, die, emit, nxt, toon
from .axi import E_REFUSED
from .axi import _tv  # noqa: F401 -- re-exported so tests can exercise TOON scalar quoting directly via cli._tv


def trunc(s, n=90):
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def ts(s):
    """Trim an RFC3339 timestamp to minute precision for table display."""
    return (s or "")[:16].replace("T", " ")


def is_unhealthy(health):
    """Cluster health uses 'Healthy'/'Unhealthy'; edge host health uses lowercase
    'healthy'/'unhealthy' (confirmed live, both APIs) — compare case-insensitively
    rather than trusting either casing."""
    return bool(health) and health.lower() != "healthy"


def dget(d, key):
    """d.get(key, {}) does NOT protect against an explicit JSON null for key —
    confirmed live, pack objects carry "status": null rather than omitting it.
    Use this instead of .get(key, {}) for any nested object field."""
    return (d or {}).get(key) or {}


# ── 1Password (Lobster vault, item ID only — see module docstring) ─────────
def _run(cmd, timeout=30, input_text=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
    except subprocess.TimeoutExpired:
        die(f"timeout after {timeout}s: {' '.join(cmd[:2])}")
    except FileNotFoundError:
        die(f"not found on PATH: {cmd[0]}")


def _op_die(msg, stderr_text, code=E_ERR):
    """Both op call sites below hit the same failure mode when the shared
    service account is rate-limited (confirmed live 9/21/26: every verb died
    on 'Too many requests. Your client has been rate-limited.' with no other
    signal) -- name that cause once here instead of a generic op error that
    reads identically to a bad vault, a typo'd item, or `op` being logged out."""
    if "too many requests" in (stderr_text or "").lower():
        die(f"{msg} -- the shared 1Password service account is rate-limited. "
            "Set PALETTE_API_KEY=<key> to bypass 1Password, or wait and retry.", code)
    die(msg, code)


def _op_item_id_for_tenant(tenant):
    """'Palette API Key (<tenant>)' -> item id, via `op item list` (never op:// —
    titles with parens break that syntax). Service accounts need --vault explicit."""
    p = _run(["op", "item", "list", "--vault", OP_VAULT, "--format", "json"], timeout=20)
    if p.returncode != 0:
        stderr = (p.stderr or "").strip()
        _op_die(f"op item list --vault {OP_VAULT} failed: {stderr[:300]}", stderr)
    try:
        items = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        die("op item list returned non-JSON output")
    want = f"palette api key ({tenant.lower()})"
    exact = [i for i in items if (i.get("title") or "").strip().lower() == want]
    if exact:
        return exact[0]["id"]
    loose = [i for i in items if "palette" in (i.get("title") or "").lower()
             and tenant.lower() in (i.get("title") or "").lower()]
    if not loose:
        titles = sorted(i["title"] for i in items if "palette api key" in (i.get("title") or "").lower())
        die(f"no 1Password item 'Palette API Key ({tenant})' in vault {OP_VAULT}. "
            f"Known tenants: {', '.join(titles) or '(none found)'}", E_NOTFOUND)
    if len(loose) > 1:
        die(f"ambiguous tenant '{tenant}': " + ", ".join(i["title"] for i in loose), E_USAGE)
    return loose[0]["id"]


def _op_secret_value(item_id):
    """Pull the credential field out of an op item without ever printing it.
    Tenant items are hand-created and label their secret 'password' or
    'credential' inconsistently — check both rather than assuming one."""
    p = _run(["op", "item", "get", item_id, "--vault", OP_VAULT, "--format", "json"], timeout=20)
    if p.returncode != 0:
        stderr = (p.stderr or "").strip()
        _op_die(f"op item get {item_id} --vault {OP_VAULT} failed: {stderr[:300]}", stderr)
    try:
        d = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        die(f"op item get {item_id}: non-JSON output")
    for f in d.get("fields", []):
        label = (f.get("label") or "").lower()
        fid = (f.get("id") or "").lower()
        if fid in ("password", "credential") or label in ("password", "credential", "api key", "apikey"):
            v = f.get("value")
            if v:
                return v
    die(f"item {item_id} has no password/credential field with a value", E_ERR)


def _sanitize_cache_component(s):
    """A tenant name (or op item id) about to become a filename -- keep it to
    a safe character set instead of trusting whatever --tenant carried."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s or "") or "_"


def _key_cache_name(tenant):
    name = "key-" + _sanitize_cache_component(tenant)
    op_item = os.environ.get("PALETTE_AXI_OP_ITEM")
    if op_item:
        # A different PALETTE_AXI_OP_ITEM points at a different 1Password item
        # (possibly a different tenant's key) -- it must not collide on cache.
        name += "-" + _sanitize_cache_component(op_item)
    return name + ".json"


def _item_cache_name(tenant):
    return "item-" + _sanitize_cache_component(tenant) + ".json"


def _cache_dir():
    """$XDG_RUNTIME_DIR/palette-axi -- tmpfs, per-user, gone at logout. Never
    falls back to a persistent path like ~/.cache: that would leave a copy of
    the Palette API key on disk after the session ends. No usable
    XDG_RUNTIME_DIR means no caching at all, not a different location."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base or not os.path.isdir(base) or not os.access(base, os.W_OK):
        return None
    d = os.path.join(base, "palette-axi")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)  # makedirs' mode is masked by umask -- enforce it
    except OSError:
        return None
    return d


def _key_ttl():
    raw = os.environ.get("PALETTE_AXI_KEY_TTL")
    if raw is None or raw == "":
        return KEY_CACHE_TTL_DEFAULT
    try:
        return int(raw)
    except ValueError:
        return KEY_CACHE_TTL_DEFAULT


def _cache_read(name, ttl):
    """The cached dict if `name` exists and is within `ttl` seconds old, else
    None. ttl<=0 always misses -- the caller decides whether that means
    "disabled" or merely "expired"."""
    if ttl <= 0:
        return None
    d = _cache_dir()
    if not d:
        return None
    path = os.path.join(d, name)
    try:
        st = os.stat(path)
    except OSError:
        return None
    if time.time() - st.st_mtime > ttl:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(name, value):
    """Write mode 0600 regardless of umask -- os.open's mode argument alone
    is masked by it, a permissive umask would otherwise loosen the file.
    Best-effort: a failed cache write must never break a command that
    already has its key."""
    d = _cache_dir()
    if not d:
        return
    path = os.path.join(d, name)
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(value, f)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _cached_item_id_for_tenant(tenant):
    """Wraps _op_item_id_for_tenant with a 24h cache. The tenant -> item-id
    mapping is not a secret and barely ever changes, so even a key-cache miss
    should cost one `op` call (item get), not two (item list + item get)."""
    name = _item_cache_name(tenant)
    cached = _cache_read(name, ITEM_CACHE_TTL)
    if cached and cached.get("item_id"):
        return cached["item_id"]
    item_id = _op_item_id_for_tenant(tenant)
    _cache_write(name, {"item_id": item_id})
    return item_id


def _key_cache_status(tenant):
    """doctor-only: reports cache state without ever touching 1Password, the
    Palette API, or printing the key's value."""
    if os.environ.get("PALETTE_API_KEY"):
        return "disabled (PALETTE_API_KEY set)"
    ttl = _key_ttl()
    if ttl <= 0:
        return "disabled (PALETTE_AXI_KEY_TTL=0)"
    d = _cache_dir()
    if not d:
        base = os.environ.get("XDG_RUNTIME_DIR")
        why = "XDG_RUNTIME_DIR unset" if not base else f"{base} not a writable directory"
        return f"disabled ({why})"
    path = os.path.join(d, _key_cache_name(tenant))
    try:
        st = os.stat(path)
    except OSError:
        return "miss"
    age = int(time.time() - st.st_mtime)
    if age > ttl:
        return "miss"
    return f"hit (age {age}s)"


# Full path of the cache file the current api_key was served from, if any.
# api() deletes it on a 401 so the next run re-reads 1Password instead of
# retrying a stale/revoked key forever. Set by get_api_key(), read by api().
_LAST_KEY_CACHE_PATH = None


def get_api_key(tenant):
    """PALETTE_API_KEY always wins and never touches the cache. Otherwise: a
    cached key (see module docstring for why this cache exists) if one is
    fresh, else 1Password by way of a possibly-cached tenant->item-id
    mapping -- at most one `op` call either way, not two."""
    global _LAST_KEY_CACHE_PATH
    _LAST_KEY_CACHE_PATH = None
    env_key = os.environ.get("PALETTE_API_KEY")
    if env_key:
        return env_key

    ttl = _key_ttl()
    key_name = _key_cache_name(tenant)
    if ttl > 0:
        cached = _cache_read(key_name, ttl)
        if cached and cached.get("key"):
            d = _cache_dir()
            if d:
                _LAST_KEY_CACHE_PATH = os.path.join(d, key_name)
            return cached["key"]

    item_id = os.environ.get("PALETTE_AXI_OP_ITEM") or _cached_item_id_for_tenant(tenant)
    key = _op_secret_value(item_id)

    if ttl > 0:
        _cache_write(key_name, {"key": key})
    return key


# ── Palette API ──────────────────────────────────────────────────────────
# 11/18/26: a burst of probes got `429 TooManyRequests` from api.spectrocloud.com
# mid-run. A 429 means the request was NOT processed, so retrying is safe even
# for a POST/PUT/DELETE. Bounded: RETRY_429 retries, Retry-After honoured but
# capped, so a throttled agent waits seconds, not forever.
RETRY_429 = 3
RETRY_429_MAX_WAIT = 15


def _retry_after(header, attempt):
    try:
        return min(float(header), RETRY_429_MAX_WAIT)
    except (TypeError, ValueError):
        return min(2 ** (attempt + 1), RETRY_429_MAX_WAIT)


def _http(method, path, api_key, project=None, params=None, json_body=None, timeout=30,
          accept="application/json"):
    url = f"{API_BASE}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    headers = {"ApiKey": api_key, "Accept": accept}
    if project:
        headers["ProjectUid"] = project
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    for attempt in range(RETRY_429 + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 429 and attempt < RETRY_429:
                time.sleep(_retry_after(e.headers.get("Retry-After") if e.headers else None, attempt))
                continue
            try:
                j = json.loads(body)
                msg = j.get("message") or j.get("error") or str(j)[:300]
            except Exception:
                msg = body.decode(errors="replace")[:300] or e.reason
            if e.code == 401 and _LAST_KEY_CACHE_PATH:
                # A cached key the API now rejects is worse than no cache at all —
                # every call would die the same way until someone clears it by
                # hand. Delete it so the next run re-reads 1Password.
                with contextlib.suppress(OSError):
                    os.unlink(_LAST_KEY_CACHE_PATH)
            code = E_NOTFOUND if e.code == 404 else E_ERR
            die(f"{method} {path} -> HTTP {e.code}: {msg}", code)
        except urllib.error.URLError as e:
            die(f"network error calling {path}: {e.reason}")
        except TimeoutError:
            # A read timeout mid-body is a bare TimeoutError, not a URLError.
            # Seen live 9/21/26 on GET /v1/dashboard/projects (loves tenant): it
            # escaped as a raw traceback instead of an error line and exit code.
            die(f"timeout after {timeout}s reading {method} {path} — Palette was slow; retry")


def api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
    body = _http(method, path, api_key, project, params, json_body, timeout)
    return json.loads(body) if body else {}


def api_raw(method, path, api_key, project=None, params=None, timeout=60):
    """For the octet-stream endpoints (kubeconfig, profile export). Confirmed
    live 11/18/26: sent with `Accept: application/json` they answer 200 with the
    14-byte body `{"Reader":{}}` -- a serialized Go io.Reader, not the file. A
    caller that trusts the 200 writes that into a kubeconfig. Ask for
    octet-stream and the real content comes back."""
    return _http(method, path, api_key, project, params, None, timeout, accept="application/octet-stream")


# The page loops below stop on their own: a short page, no continue token, or a
# page with no new uids. MAX_PAGES is only a runaway guard. It was 10 (500 rows)
# until 9/21/26, when a real project passed 500 clusters (506) and `cluster
# <name>` answered "no cluster matching" for a cluster that exists.
MAX_PAGES = 100


def list_all(path, api_key, project=None, filters=None, page_limit=50, max_pages=MAX_PAGES,
             method="GET", json_body=None):
    """Page via listmeta.continue when the endpoint returns one — confirmed live
    that's the stable mechanism (packs, clusterprofiles). Raw offset increments
    are a fallback for endpoints that provide no continue token; some of those
    genuinely don't honor offset at all (the retired /v1/projects always returned
    the same first 50 rows whatever offset was sent) and others (edgehosts) return
    no listmeta whatsoever. Progress is tracked by uid so a page that adds
    nothing new ends the loop instead of spinning for max_pages.

    Even continue-token pagination was observed live to lose 2 of 146 rows on
    /v1/clusterprofiles — listmeta.count itself can run slightly ahead of what
    the API actually hands back page by page. LAST_LIST_TRUNCATED is set so
    callers can say so instead of silently showing a partial list as complete."""
    items, seen, offset, cont = [], set(), 0, None
    total = None
    for _ in range(max_pages):
        params = {"limit": page_limit, "filters": filters}
        params["continue"] = cont if cont else None
        if not cont:
            params["offset"] = offset
        data = api(method, path, api_key, project, params, json_body=json_body)
        if total is None:
            total = dget(data, "listmeta").get("count")
        batch = data.get("items") or []
        new = [b for b in batch if dget(b, "metadata").get("uid") not in seen]
        if not new:
            break
        for b in new:
            seen.add(dget(b, "metadata").get("uid"))
        items.extend(new)
        cont = dget(data, "listmeta").get("continue")
        if cont:
            continue
        if len(batch) < page_limit:
            break
        offset += page_limit
    _set_truncated(len(items), total)
    return items


LAST_LIST_TRUNCATED = False
LAST_LIST_GOT = LAST_LIST_TOTAL = 0


def _set_truncated(got, total):
    global LAST_LIST_TRUNCATED, LAST_LIST_GOT, LAST_LIST_TOTAL
    LAST_LIST_GOT, LAST_LIST_TOTAL = got, total or got
    LAST_LIST_TRUNCATED = bool(total and got < total)


def truncation_note():
    # Confirmed live: /v1/clusterprofiles pages correctly via
    # listmeta.continue but still dropped 2 of 146 rows in the same test run —
    # this endpoint's own pagination is not fully reliable, not a bug in list_all.
    return (f"\nnote: API returned {LAST_LIST_GOT} of {LAST_LIST_TOTAL} "
            "(listmeta.count) — this endpoint's pagination did not hand back the "
            "full list; some rows may be missing." if LAST_LIST_TRUNCATED else "")


def list_clusters(api_key, project, page_limit=50, max_pages=MAX_PAGES):
    """GET /v1/spectroclusters carries no status.health at all — confirmed live.
    The health field only comes back from the dashboard search/overview surface,
    which real sessions reached for exactly this reason. This is a POST (it takes
    a filter body), but it is a search, not a mutation — still read-only."""
    body = {"filter": {"conjunction": "and", "filterGroups": []}, "sort": []}
    items, seen, offset, cont = [], set(), 0, None
    total = None
    for _ in range(max_pages):
        params = {"limit": page_limit}
        params["continue"] = cont if cont else None
        if not cont:
            params["offset"] = offset
        data = api("POST", "/v1/dashboard/spectroclusters/search", api_key, project, params, json_body=body)
        if total is None:
            total = dget(data, "listmeta").get("count")
        batch = data.get("items") or []
        new = [b for b in batch if dget(b, "metadata").get("uid") not in seen]
        if not new:
            break
        for b in new:
            seen.add(dget(b, "metadata").get("uid"))
        items.extend(new)
        cont = dget(data, "listmeta").get("continue")
        if cont:
            continue
        if len(batch) < page_limit:
            break
        offset += page_limit
    _set_truncated(len(items), total)
    return items


# ── project / resource resolution ───────────────────────────────────────
# /v1/projects answered GET until it started returning
# `405: method GET is not allowed, but [POST] are`. /v1/dashboard/projects serves
# the same rows, pages honestly via listmeta.continue, and sees all of them: the
# old path capped at 50 while the tenant holds 65.
PROJECTS_PATH = "/v1/dashboard/projects"

# /v1/edgehosts answers the same 405 as /v1/projects did. The search endpoint is
# POST-only but pages via the same listmeta.continue token, and returns the same
# metadata/spec/status shape, so only the fetch changes.
EDGEHOSTS_PATH = "/v1/dashboard/edgehosts/search"


def _project_cluster_count(p):
    """How many clusters the project actually has.

    `spec.isDefault` was the third column until the endpoint moved. The first cut
    of the replacement summed status.clustersHealth — which is WRONG: its buckets
    overlap, so a project with 4 running and 6 unhealthy has 6 clusters, not 10.
    Measured live 9/8/26: 17 of the first 40 projects disagreed. status.usage.clusters
    is one entry per cluster and matches what `palette-axi clusters` lists."""
    return len(dget(dget(p, "status"), "usage").get("clusters") or [])


def resolve_project(ref, api_key):
    ref = ref or os.environ.get("PALETTE_PROJECT")
    if not ref:
        die("no --project given (or set PALETTE_PROJECT). Run 'palette-axi projects' to list options.", E_USAGE)
    if UID_RE.match(ref):
        return ref
    items = list_all(PROJECTS_PATH, api_key)
    exact = [p for p in items if (dget(p, "metadata").get("name") or "").lower() == ref.lower()]
    if exact:
        return exact[0]["metadata"]["uid"]
    cands = [p for p in items if ref.lower() in (dget(p, "metadata").get("name") or "").lower()]
    if not cands:
        die(f"no project matching '{ref}'", E_NOTFOUND)
    if len(cands) > 1:
        names = ", ".join(sorted(p["metadata"]["name"] for p in cands))
        die(f"ambiguous project '{ref}': {names}", E_USAGE)
    return cands[0]["metadata"]["uid"]


def resolve_by_name(ref, items, kind):
    """uid | exact name | unique substring -> item. Same disambiguation contract
    as resolve_project: never guess, always name the candidates."""
    if UID_RE.match(ref):
        hit = next((i for i in items if dget(i, "metadata").get("uid") == ref), None)
        if not hit:
            die(f"no {kind} with uid {ref}", E_NOTFOUND)
        return hit
    exact = [i for i in items if (dget(i, "metadata").get("name") or "").lower() == ref.lower()]
    if exact:
        return exact[0]
    cands = [i for i in items if ref.lower() in (dget(i, "metadata").get("name") or "").lower()]
    if not cands:
        die(f"no {kind} matching '{ref}'", E_NOTFOUND)
    if len(cands) > 1:
        names = ", ".join(sorted(i["metadata"]["name"] for i in cands))
        die(f"ambiguous {kind} '{ref}': {names}", E_USAGE)
    return cands[0]


# ── commands ───────────────────────────────────────────────────────────
def cmd_projects(a):
    key = get_api_key(a.tenant)
    items = list_all(PROJECTS_PATH, key)
    rows = [{"name": p["metadata"]["name"], "uid": p["metadata"]["uid"],
             "clusters": _project_cluster_count(p)} for p in items]
    emit(f"tenant={a.tenant}",
         toon("projects", ["name", "uid", "clusters"], rows),
         f"\ncount:{len(rows)}" + truncation_note(),
         nxt("palette-axi clusters --project <name>", "palette-axi edgehosts --project <name>"))


def _cloud_type(c):
    return (dget(c, "spec").get("cloudType")
            or dget(dget(c, "specSummary"), "cloudConfig").get("cloudType"))


def cmd_clusters(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    items = list_clusters(key, proj)
    if a.edge:
        items = [c for c in items if _cloud_type(c) == "edge-native"]
    rows = []
    for c in items:
        m, st = dget(c, "metadata"), dget(c, "status")
        rows.append({"name": m.get("name"), "uid": m.get("uid"),
                     "cloudType": _cloud_type(c),
                     "state": st.get("state"), "health": dget(st, "health").get("state")})
    unhealthy = sum(1 for r in rows if is_unhealthy(r["health"]))
    emit(f"tenant={a.tenant} project={proj}",
         toon("clusters", ["name", "uid", "cloudType", "state", "health"], rows),
         f"\ncount:{len(rows)} unhealthy:{unhealthy}" + truncation_note(),
         nxt("palette-axi cluster <name>", "palette-axi events <name>"))


def cmd_cluster(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    items = list_clusters(key, proj)
    hit = resolve_by_name(a.ref, items, "cluster")
    uid = hit["metadata"]["uid"]
    # Plain GET /v1/spectroclusters/{uid} carries no status.health — only the
    # dashboard overview does (confirmed live) — so pull created/pools from the
    # plain resource and state/health/conditions from the overview.
    c = api("GET", f"/v1/spectroclusters/{uid}", key, proj)
    overview = api("GET", f"/v1/dashboard/spectroclusters/{uid}/overview", key, proj)
    m, sp = dget(c, "metadata"), dget(c, "spec")
    ost = dget(overview, "status")
    emit(f"tenant={a.tenant} project={proj}",
         toon("cluster", ["name", "uid", "cloudType", "state", "health", "created"],
              [{"name": m.get("name"), "uid": uid, "cloudType": _cloud_type(c) or _cloud_type(overview),
                "state": ost.get("state"), "health": dget(ost, "health").get("state"),
                "created": ts(m.get("creationTimestamp"))}]))

    pools = dget(sp, "cloudConfig").get("machinePools") or []
    if pools:
        prows = [{"name": p.get("name"), "size": p.get("size"),
                  "controlPlane": p.get("controlPlane", False)} for p in pools]
        emit(toon("pools", ["name", "size", "controlPlane"], prows))

    # --profiles: no extra API call -- spec.clusterProfileTemplates[] is already
    # in `c` (the plain GET /v1/spectroclusters/{uid} above). Evidence: a live
    # session on 9/21/26 needed which profiles/pack tags a
    # cluster actually runs and had to drop to a raw urllib GET for it, because
    # `cluster --full` shows state/conditions but never profile attachment.
    # Each template carries name/uid/type and either profileVersion or version
    # (fall back to the latter); packs[] is {name, layer, tag}. Gated behind
    # the flag so the default `cluster` output stays byte-identical.
    if a.profiles:
        templates = sp.get("clusterProfileTemplates") or []
        trows = [{"name": (t or {}).get("name"), "version": (t or {}).get("profileVersion") or (t or {}).get("version"),
                  "type": (t or {}).get("type"), "uid": (t or {}).get("uid")} for t in templates]
        emit(toon("profiles", ["name", "version", "type", "uid"], trows))
        pkrows = []
        for t in templates:
            t = t or {}
            for pk in (t.get("packs") or []):
                pkrows.append({"profile": t.get("name"), "layer": pk.get("layer"),
                                "name": pk.get("name"), "tag": pk.get("tag")})
        emit(toon("packs", ["profile", "layer", "name", "tag"], pkrows))

    conds = ost.get("conditions") or []
    show = conds if a.full else [c2 for c2 in conds if c2.get("status") != "True"]
    crows = [{"type": c2.get("type"), "status": c2.get("status"),
              "reason": c2.get("reason"), "message": trunc(c2.get("message"), 100)} for c2 in show]
    emit(toon("conditions", ["type", "status", "reason", "message"], crows))
    emit(nxt(f"palette-axi events {m.get('name')} --project {proj}",
             f"palette-axi cluster {m.get('name')} --project {proj} --full") if not a.full
         else nxt(f"palette-axi events {m.get('name')} --project {proj}"))


# 11/18/26, confirmed live (custeng-prod, SA-Craig-Smith): `GET /v1/clusterprofiles`
# now answers `405: method GET is not allowed, but [POST] are` -- the same move
# /v1/projects and /v1/edgehosts made. `POST /v1/dashboard/clusterprofiles` with
# a filter body serves the rows, pages via listmeta.continue, but returns ONE row
# per profile NAME: metadata.uid is the newest version and
# specSummary.versions[] = [{uid, version}] carries every version. The old list
# returned one row per version, so expand versions back out, newest first --
# `profiles` keeps listing every version and `profile <name>` keeps resolving.
PROFILES_PATH = "/v1/dashboard/clusterprofiles"


def _vkey(v):
    parts = re.split(r"[.\-]", v or "0")
    return tuple(int(x) if x.isdigit() else 0 for x in parts)


def list_profiles(key, proj):
    rows = list_all(PROFILES_PATH, key, proj, method="POST", json_body={"filter": {}, "sort": []})
    out = []
    for p in rows:
        m, ss = dget(p, "metadata"), dget(p, "specSummary")
        pub = dget(ss, "published")
        versions = [v for v in (ss.get("versions") or []) if (v or {}).get("uid")]
        if not versions:
            versions = [{"uid": m.get("uid"), "version": ss.get("version")}]
        versions.sort(key=lambda v: _vkey(v.get("version")), reverse=True)
        for v in versions:
            out.append({"metadata": {"name": m.get("name"), "uid": v["uid"],
                                     "annotations": dget(m, "annotations")},
                        "spec": {"version": v.get("version"), "cloudType": pub.get("cloudType"),
                                 "published": {"type": pub.get("type")}}})
    return out


def resolve_profile(ref, key, proj, version=None, strict=False):
    """uid -> that uid (no list call). name -> exact (case-insensitive) or unique
    substring NAME, then its versions. A name carries one row per version, so
    `strict` (every write verb) refuses to pick one when there are several and
    no --version was given; the read verb takes the newest, matching what the
    old per-version list resolved to in practice. Returns (uid, name, version)."""
    if UID_RE.match(ref):
        return ref, None, None
    items = list_profiles(key, proj)
    names = {}
    for i in items:
        names.setdefault(i["metadata"]["name"], []).append(i)
    exact = [n for n in names if (n or "").lower() == ref.lower()]
    cands = exact or [n for n in names if ref.lower() in (n or "").lower()]
    if not cands:
        die(f"no profile matching '{ref}' in project {proj}", E_NOTFOUND)
    if len(cands) > 1:
        die(f"ambiguous profile '{ref}': {', '.join(sorted(cands))}", E_USAGE)
    rows = names[cands[0]]
    if version:
        rows = [r for r in rows if r["spec"]["version"] == version]
        if not rows:
            have = ", ".join(r["spec"]["version"] or "" for r in names[cands[0]])
            die(f"profile '{cands[0]}' has no version {version} (have: {have})", E_NOTFOUND)
    if strict and len(rows) > 1:
        have = ", ".join(f"{r['spec']['version']}={r['metadata']['uid']}" for r in rows)
        die(f"profile '{cands[0]}' has {len(rows)} versions ({have}) -- pass --version or a uid", E_USAGE)
    r = rows[0]
    return r["metadata"]["uid"], r["metadata"]["name"], r["spec"]["version"]


def cmd_profiles(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    items = list_profiles(key, proj)
    rows = []
    for p in items:
        m, sp = dget(p, "metadata"), dget(p, "spec")
        rows.append({"name": m.get("name"), "uid": m.get("uid"),
                     "version": sp.get("version"), "type": dget(sp, "published").get("type"),
                     "cloudType": sp.get("cloudType")})
    emit(f"tenant={a.tenant} project={proj}",
         toon("profiles", ["name", "uid", "version", "type", "cloudType"], rows),
         f"\ncount:{len(rows)}" + truncation_note(),
         nxt("palette-axi profile <name>"))


def cmd_profile(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    uid, _, _ = resolve_profile(a.ref, key, proj)
    p = api("GET", f"/v1/clusterprofiles/{uid}", key, proj)
    m, sp = dget(p, "metadata"), dget(p, "spec")
    pub = dget(sp, "published")
    emit(f"tenant={a.tenant} project={proj}",
         toon("profile", ["name", "uid", "version", "type", "cloudType"],
              [{"name": m.get("name"), "uid": uid, "version": sp.get("version"),
                "type": pub.get("type"), "cloudType": sp.get("cloudType")}]))
    packs = pub.get("packs") or []
    prows = [{"name": pk.get("name"), "layer": pk.get("layer"), "tag": pk.get("tag"),
              "registryUid": pk.get("registryUid") if a.full else trunc(pk.get("registryUid"), 12)}
             for pk in packs]
    emit(toon("layers", ["name", "layer", "tag", "registryUid"], prows),
         f"\nlayerCount:{len(prows)}")

    # --variables: one extra call, GET /v1/clusterprofiles/{uid}/variables ->
    # {"variables":[{name, displayName, defaultValue, format, required,
    # immutable, hidden, isSensitive}]}. Evidence: the same 9/21/26 live
    # session needed a profile's declared variables (K8sPodCIDR etc.) and had
    # to drop to a raw urllib GET for it. A sensitive variable's default is
    # ALWAYS rendered as "********", never the underlying value, regardless of
    # whether one is actually set -- this tool never prints a secret (see the
    # API key and registry-auth handling elsewhere in this file for the same
    # rule). Gated behind the flag so default `profile` output is unchanged.
    if a.variables:
        vdata = api("GET", f"/v1/clusterprofiles/{uid}/variables", key, proj)
        variables = vdata.get("variables") or []
        vrows = []
        for v in variables:
            v = v or {}
            sensitive = bool(v.get("isSensitive"))
            vrows.append({"name": v.get("name"), "default": "********" if sensitive else v.get("defaultValue"),
                          "format": v.get("format"), "required": bool(v.get("required")),
                          "immutable": bool(v.get("immutable")), "hidden": bool(v.get("hidden")),
                          "sensitive": sensitive})
        emit(toon("variables", ["name", "default", "format", "required", "immutable", "hidden", "sensitive"], vrows),
             f"\nvariableCount:{len(vrows)}")


SAN_VENDORS = {"PURE", "NETAPP", "EMC", "HITACHI", "IBM"}

# Hidden-by-default noise on a real edge host's NIC list -- a live 28-nic host
# had ~4 physical/bond/vlan interfaces and the rest were container/CNI
# plumbing. --all-nics (on `edgehost`) shows everything.
_VIRTUAL_NIC_PREFIXES = ("lxc", "cilium", "veth", "flannel", "cni", "docker",
                          "kube-ipvs", "vxlan", "genev", "tunl")


def _is_virtual_nic(name):
    n = (name or "").lower()
    return n == "lo" or n.startswith(_VIRTUAL_NIC_PREFIXES)


def _wide_fields(item, key, proj, uid):
    """cores/memGB/disks/sanDisks/ip/secureBoot all live under spec.device on the
    single-resource GET. UNVERIFIED LIVE: whether the edgehosts SEARCH response
    (the `item` this function receives, from list_all against EDGEHOSTS_PATH)
    already carries spec.device inline, or only the metadata/status shape
    `edgehosts` has always rendered. Try the item first -- costs nothing extra
    if it's there -- and only fall back to one GET /v1/edgehosts/{uid} per host
    (sequential, bounded by the row count) when spec.device is empty.

    sanDisks is a vendor-name heuristic (PURE/NETAPP/EMC/HITACHI/IBM against
    disk.vendor, case-insensitive) -- not a real SAN/LUN protocol check. A disk
    from an unlisted SAN vendor will not be counted; see --wide's help text."""
    dev = dget(dget(item, "spec"), "device")
    host = dget(dget(item, "spec"), "host")
    if not dev:
        full = api("GET", f"/v1/edgehosts/{uid}", key, proj)
        dev = dget(dget(full, "spec"), "device")
        host = dget(dget(full, "spec"), "host")
    cores = dget(dev, "cpu").get("cores")
    mem_mb = dget(dev, "memory").get("sizeInMB")
    mem_gb = round(mem_mb / 1024, 1) if isinstance(mem_mb, (int, float)) else None
    disks = dev.get("disks") or []
    san = sum(1 for d in disks if (d.get("vendor") or "").upper() in SAN_VENDORS)
    return {"cores": cores, "memGB": mem_gb, "disks": len(disks), "sanDisks": san,
            "ip": host.get("hostAddress"), "secureBoot": dev.get("secureBoot")}


def cmd_edgehosts(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    items = list_all(EDGEHOSTS_PATH, key, proj,
                     method="POST", json_body={"filter": {}, "sort": []})
    rows = []
    for h in items:
        m, st = dget(h, "metadata"), dget(h, "status")
        # clusterUid does not exist on this resource (confirmed live) — the
        # attachment lives in inUseClusters, a list because appliance-mode
        # 2-node hosts can each show the same cluster.
        in_use = st.get("inUseClusters") or []
        cluster = ", ".join(c.get("name", "") for c in in_use)
        # _labels/_item are carried for --min-cores/--label filtering and the
        # --wide fetch below; toon() only ever reads the columns in `fields`,
        # so these extra keys never touch the default (no-wide, no-filter)
        # output -- that path must stay byte-identical to before this change.
        rows.append({"name": m.get("name"), "uid": m.get("uid"), "state": st.get("state"),
                     "health": dget(st, "health").get("state"), "cluster": cluster,
                     "_labels": dget(m, "labels"), "_item": h})

    if a.wide or a.min_cores is not None:
        for r in rows:
            r.update(_wide_fields(r["_item"], key, proj, r["uid"]))

    total = len(rows)
    filtered = False
    if a.min_cores is not None:
        filtered = True
        rows = [r for r in rows if isinstance(r.get("cores"), int) and r["cores"] >= a.min_cores]
    if a.label:
        if "=" not in a.label:
            die(f"--label must be key=value, got '{a.label}'", E_USAGE)
        filtered = True
        lk, lv = a.label.split("=", 1)
        rows = [r for r in rows if (r.get("_labels") or {}).get(lk) == lv]

    fields = ["name", "uid", "state", "health", "cluster"]
    if a.wide:
        fields = fields + ["cores", "memGB", "disks", "sanDisks", "ip", "secureBoot"]

    unhealthy = sum(1 for r in rows if is_unhealthy(r["health"]))
    unassigned = sum(1 for r in rows if not r["cluster"])
    filtered_note = f" filtered_from:{total}" if filtered else ""
    emit(f"tenant={a.tenant} project={proj}",
         toon("edgehosts", fields, rows),
         f"\ncount:{len(rows)} unhealthy:{unhealthy} unassigned:{unassigned}{filtered_note}" + truncation_note())


def _match_edgehosts(ref, items):
    """Same match order as resolve_by_name (uid exact, name exact, substring)
    but returns the list of hits instead of dying -- an --all-projects search
    needs to keep looking in the next project on a miss, not exit on the first
    one it tries."""
    if UID_RE.match(ref):
        hit = next((i for i in items if dget(i, "metadata").get("uid") == ref), None)
        return [hit] if hit else []
    exact = [i for i in items if (dget(i, "metadata").get("name") or "").lower() == ref.lower()]
    if exact:
        return exact
    return [i for i in items if ref.lower() in (dget(i, "metadata").get("name") or "").lower()]


# Evidence for the shape below: read from a real GET /v1/edgehosts/{uid}
# response (customer data, never copied into this repo) and reproduced here
# as synthetic fixtures with the same field names/types. spec.device is the
# hardware inventory the `edgehosts` list has never surfaced: archType,
# cpu.cores, memory.sizeInMB, secureBoot, hostType, os{family,version,
# kernelVersion}, disks[]{controller,size,vendor,partitions[]}, nics[]{...},
# gpus[]. spec.host{hostAddress,...} is a sibling of spec.device, not nested
# under it. Fields may be JSON null rather than omitted -- dget() throughout,
# never a bare .get(k, {}).
def cmd_edgehost(a):
    key = get_api_key(a.tenant)
    # --all-projects forces the multi-project search even if --project or
    # $PALETTE_PROJECT is set; with neither given, the same search is also the
    # default -- an SE with just a UID from a ticket usually doesn't know the
    # project either. This mirrors resolve_project's existing contract for
    # every other verb (never guess) without changing resolve_project itself.
    use_all = a.all_projects or not (a.project or os.environ.get("PALETTE_PROJECT"))

    search_note = ""
    if use_all:
        projects = list_all(PROJECTS_PATH, key)
        found = []
        for p in projects:
            puid = dget(p, "metadata").get("uid")
            pname = dget(p, "metadata").get("name")
            if not puid:
                continue
            items = list_all(EDGEHOSTS_PATH, key, puid, method="POST", json_body={"filter": {}, "sort": []})
            matches = _match_edgehosts(a.ref, items)
            if matches:
                found.append((puid, pname, matches))
        if not found:
            die(f"no edge host matching '{a.ref}' in any of {len(projects)} projects "
                "searched -- pass --project to search just one", E_NOTFOUND)
        if len(found) > 1:
            where = ", ".join(f"{pname} ({len(m)})" for _, pname, m in found)
            die(f"'{a.ref}' matches edge hosts in more than one project: {where} "
                "-- pass --project to disambiguate", E_USAGE)
        proj, proj_name, matches = found[0]
        if len(matches) > 1:
            names = ", ".join(sorted((dget(m, "metadata").get("name") or "") for m in matches))
            die(f"ambiguous edge host '{a.ref}' in project {proj_name}: {names}", E_USAGE)
        hit = matches[0]
        search_note = f"note: found via --all-projects search ({len(projects)} projects searched); matched project {proj_name}"
    else:
        proj = resolve_project(a.project, key)
        items = list_all(EDGEHOSTS_PATH, key, proj, method="POST", json_body={"filter": {}, "sort": []})
        hit = resolve_by_name(a.ref, items, "edge host")

    uid = dget(hit, "metadata").get("uid")
    eh = api("GET", f"/v1/edgehosts/{uid}", key, proj)

    if a.json:
        print(json.dumps(eh, indent=2))
        return

    m, sp, st = dget(eh, "metadata"), dget(eh, "spec"), dget(eh, "status")
    dev = dget(sp, "device")
    os_ = dget(dev, "os")
    host = dget(sp, "host")
    health = dget(st, "health")
    in_use = st.get("inUseClusters") or []
    cluster = ", ".join(c.get("name", "") for c in in_use)
    labels = dget(m, "labels")
    labels_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items())) or None

    cores = dget(dev, "cpu").get("cores")
    mem_mb = dget(dev, "memory").get("sizeInMB")
    mem_gb = round(mem_mb / 1024, 1) if isinstance(mem_mb, (int, float)) else None
    os_str = " ".join(x for x in [os_.get("family"), os_.get("version")] if x) or None

    row = {"name": m.get("name"), "uid": uid, "project": proj,
           "state": st.get("state"), "health": health.get("state"),
           "agentVersion": health.get("agentVersion"), "cluster": cluster,
           "archType": dev.get("archType"), "hostType": dev.get("hostType"),
           "secureBoot": dev.get("secureBoot"), "os": os_str,
           "kernel": os_.get("kernelVersion"), "cores": cores, "memGB": mem_gb,
           "hostAddress": host.get("hostAddress"), "labels": labels_str}

    emit(f"tenant={a.tenant} project={proj}",
         toon("edgehost", ["name", "uid", "project", "state", "health", "agentVersion",
                            "cluster", "archType", "hostType", "secureBoot", "os", "kernel",
                            "cores", "memGB", "hostAddress", "labels"], [row]),
         search_note)

    disks = dev.get("disks") or []
    drows = []
    for i, d in enumerate(disks):
        parts = d.get("partitions") or []
        mounts = ", ".join(p.get("mountPoint") for p in parts if p.get("mountPoint"))
        drows.append({"idx": i, "vendor": d.get("vendor"), "controller": d.get("controller"),
                      "sizeGB": d.get("size"), "partitions": len(parts), "mounts": trunc(mounts, 70)})
    emit(toon("disks", ["idx", "vendor", "controller", "sizeGB", "partitions", "mounts"], drows))

    nics = dev.get("nics") or []
    visible = [n for n in nics if a.all_nics or not _is_virtual_nic(n.get("nicName"))]
    hidden = len(nics) - len(visible)
    nrows = [{"name": n.get("nicName"), "mac": n.get("macAddr"), "ip": n.get("ip"),
              "subnet": n.get("subnet"), "gateway": n.get("gateway"),
              "default": bool(n.get("isDefault"))} for n in visible]
    emit(toon("nics", ["name", "mac", "ip", "subnet", "gateway", "default"], nrows),
         f"\nnicCount:{len(visible)}" + (f"  [+{hidden} virtual hidden, --all-nics]" if hidden > 0 else ""))

    gpus = dev.get("gpus") or []
    emit(f"gpuCount:{len(gpus)}",
         nxt(f"palette-axi edgehosts --project {proj} --wide"))


# Evidence probed 9/15/26 against custeng-prod, live cluster rpi-inference
# (project SA-Craig-Smith, cloudType edge-native), why this verb resolves
# through the cluster rather than guessing a cloudconfig path:
#   - GET /v1/spectroclusters/{uid} carries spec.cloudConfigRef =
#     {"kind": "edge-native", "name": "...", "uid": "<cloudconfig-uid>"} --
#     the cloudconfig uid is DIFFERENT from the cluster uid, and its kind
#     names the exact collection to GET next. This is the resolve-by-name-or-
#     uid contract's missing link for cloudconfigs: there is no tenant-wide
#     cloudconfig list to search, so the cluster is the index.
#   - GET /v1/cloudconfigs/{kind}/{uid} (kind taken verbatim from
#     cloudConfigRef.kind) returns the FULL document in one call --
#     spec.clusterConfig (controlPlaneEndpoint, ntpServers, overlay CIDR,
#     sshKeys) AND spec.machinePoolConfig (every pool, with its host list)
#     both inline. No separate machinePools GET was needed to see pool/host
#     data; the machinePools/{pool} and machinePools/{uid}/machines paths this
#     task's scout doc saw were PUT targets for partial updates, out of scope
#     here (this verb is GET-only).
#   - maas was NOT independently confirmed live: no maas cluster was found in
#     the ~9 custeng-prod projects checked (10 maas cloud accounts exist, but
#     none had an attached cluster reachable this session). The code path
#     below is generic on cloudConfigRef.kind -- it does not special-case
#     edge-native -- so a maas cluster hits the same
#     GET /v1/cloudconfigs/maas/{uid} call by construction, but that exact
#     path has not itself been exercised against a live response.
def cmd_cloudconfig(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    clusters = list_clusters(key, proj)

    hit = None
    if UID_RE.match(a.ref):
        hit = next((c for c in clusters if dget(c, "metadata").get("uid") == a.ref), None)
    if not hit:
        exact = [c for c in clusters if (dget(c, "metadata").get("name") or "").lower() == a.ref.lower()]
        cands = exact or [c for c in clusters if a.ref.lower() in (dget(c, "metadata").get("name") or "").lower()]
        if len(cands) > 1:
            names = ", ".join(sorted(dget(c, "metadata").get("name") for c in cands))
            die(f"ambiguous cluster '{a.ref}': {names}", E_USAGE)
        hit = cands[0] if cands else None

    if hit:
        cuid = dget(hit, "metadata").get("uid")
        cname = dget(hit, "metadata").get("name")
        cluster = api("GET", f"/v1/spectroclusters/{cuid}", key, proj)
        ref = dget(dget(cluster, "spec"), "cloudConfigRef")
        if not ref.get("uid"):
            die(f"cluster '{cname}' has no spec.cloudConfigRef -- cloudType "
                f"{dget(dget(cluster, 'spec'), 'cloudType')} may not use a cloudconfig resource", E_NOTFOUND)
        cc_uid, cc_kind, cc_name = ref["uid"], ref.get("kind"), ref.get("name")
    else:
        if not a.kind or not UID_RE.match(a.ref):
            die(f"no cluster matching '{a.ref}' in project {proj} "
                "(pass a bare cloudconfig uid with --kind to skip cluster resolution)", E_NOTFOUND)
        cc_uid, cc_kind, cc_name, cname = a.ref, a.kind, None, None

    cc = api("GET", f"/v1/cloudconfigs/{cc_kind}/{cc_uid}", key, proj)
    m, sp = dget(cc, "metadata"), dget(cc, "spec")
    cfg = dget(sp, "clusterConfig")
    cpe = dget(cfg, "controlPlaneEndpoint")
    overlay = dget(cfg, "overlayNetworkConfiguration")
    ntp = ", ".join(cfg.get("ntpServers") or [])
    emit(f"tenant={a.tenant} project={proj}" + (f" cluster={cname}" if cname else ""),
         toon("cloudconfig", ["name", "uid", "kind", "controlPlaneEndpoint", "overlayCidr", "ntpServers"],
              [{"name": m.get("name") or cc_name, "uid": cc_uid, "kind": cc_kind,
                "controlPlaneEndpoint": f"{cpe['host']} ({cpe.get('type')})" if cpe.get("host") else None,
                "overlayCidr": overlay.get("cidr") if overlay.get("enable") else None,
                "ntpServers": ntp if a.full else trunc(ntp, 60)}]))

    pools = sp.get("machinePoolConfig") or []
    prows = []
    for p in pools:
        hosts = p.get("hosts") or []
        addrs = ", ".join(h.get("hostAddress", "") for h in hosts)
        prows.append({"name": p.get("name"), "size": p.get("size"),
                      "controlPlane": bool(p.get("isControlPlane")), "hosts": len(hosts),
                      "hostAddresses": addrs if a.full else trunc(addrs, 60)})
    emit(toon("machinePools", ["name", "size", "controlPlane", "hosts", "hostAddresses"], prows),
         f"\npoolCount:{len(prows)}")


def _pcg(item):
    """overlordUid is the PCG uid on a private-cloud account and "" on a
    public-cloud one (all 20 custeng-prod accounts carried the key, 9/14/26).
    If a future row omits it, that is unknown, not "no PCG" — per _tv's
    contract None and False are different facts."""
    ann = dget(dget(item, "metadata"), "annotations")
    if "overlordUid" not in ann:
        return None
    return bool(ann.get("overlordUid"))


# Evidence probed 9/14/26 against custeng-prod, why this verb is
# summary-only and filters client-side:
#   - GET /v1/cloudaccounts/summary returns every cloud kind in one call;
#     its specSummary is EMPTY — no credentials in this response.
#   - The per-cloud endpoints (GET /v1/cloudaccounts/{aws,azure,gcp,vsphere,
#     maas}) return spec.secretKey / spec.secretToken / spec.sts.externalId —
#     actual credentials — so this verb never calls a per-cloud endpoint.
#   - GET /v1/cloudaccounts/openstack 404s even though summary lists
#     openstack accounts, so a per-cloud endpoint isn't even a viable
#     alternative for every kind — summary is the only one that works for all.
#   - Summary IGNORES cloudType=<x> (returns all rows) and
#     filters=spec.cloudType=<x> (returns zero rows) — so --cloud filters
#     client-side on `kind` and list_all is called with no `filters` at all.
#   - ProjectUid changes SCOPE, not row shape: without it, only tenant-owned
#     accounts come back; with it, the project's own account(s) plus tenant
#     accounts shared into it — some tenant accounts (scopeVisibility "4")
#     never appear in project scope.
def cmd_cloudaccounts(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key) if a.project or os.environ.get("PALETTE_PROJECT") else None
    items = list_all("/v1/cloudaccounts/summary", key, proj)

    # Unfiltered per-cloud counts, so `--cloud gcp` finding nothing still
    # shows what clouds actually exist instead of a bare "(none)".
    clouds = {}
    for it in items:
        k = it.get("kind") or ""
        clouds[k] = clouds.get(k, 0) + 1
    clouds_summary = " ".join(f"{k}={clouds[k]}" for k in sorted(clouds))

    show = items
    if a.cloud:
        show = [it for it in items if (it.get("kind") or "").lower() == a.cloud.lower()]

    rows = []
    for it in show:
        m = dget(it, "metadata")
        ann = dget(m, "annotations")
        rows.append({"name": m.get("name"), "uid": m.get("uid"), "cloud": it.get("kind"),
                     "scope": ann.get("scope"), "pcg": _pcg(it),
                     "created": ts(m.get("creationTimestamp"))})
    rows.sort(key=lambda r: (r["cloud"] or "", r["name"] or ""))

    project_scoped = sum(1 for r in rows if r["scope"] == "project")
    tenant_scoped = sum(1 for r in rows if r["scope"] == "tenant")

    if proj:
        header = f"tenant={a.tenant} scope=project project={proj}"
        note = ("note: project scope lists this project's own accounts plus "
                 "tenant accounts shared with it")
        next_line = nxt(f"palette-axi clusters --project {proj}")
    else:
        header = f"tenant={a.tenant} scope=tenant"
        note = ("note: tenant scope lists tenant-owned accounts only; "
                 "project-owned accounts appear only with --project")
        next_line = nxt("palette-axi cloudaccounts --project <name>")

    emit(header,
         toon("cloudaccounts", ["name", "uid", "cloud", "scope", "pcg", "created"], rows),
         f"\ncount:{len(rows)} projectScoped:{project_scoped} tenantScoped:{tenant_scoped}\n"
         f"clouds: {clouds_summary}" + truncation_note(),
         note,
         next_line)


# Evidence probed 9/15/26 against custeng-prod, why this verb reads only
# GET /v1/registries/metadata for listing and dispatches to a per-kind GET for
# describe, mirroring cloudaccounts' "one endpoint that actually works for
# every kind" shape rather than three separate list calls:
#   - GET /v1/registries -> 404 (no bare collection).
#   - GET /v1/registries/pack and /helm list fine (listmeta.continue paginated,
#     counts 7 and 75 respectively) but GET /v1/registries/oci -> 405, Allow:
#     DELETE — there is no working GET list endpoint for oci at all.
#   - GET /v1/registries/metadata returns all three kinds in ONE unpaginated
#     call (items only, no listmeta) with a flat {kind,name,uid,isDefault,
#     isPrivate,scope} shape. It silently ignores both `kind=` and `limit=`
#     query params (always returns the full set) — confirmed by requesting
#     limit=5 and kind=oci and getting all 105 rows back either way.
#   - Cross-checked trustworthy: metadata's per-kind subtotal (pack=7, helm=75,
#     oci=23 the day this was probed) matches list_all's own listmeta.count
#     exactly for the two kinds that have an independent paginated endpoint to
#     check it against. There is no listmeta on metadata itself, so a future
#     truncation can't be self-detected the way list_all's callers can.
#   - Describe needs a per-kind call because metadata's rows carry no endpoint/
#     auth/sync info: GET /v1/registries/{pack,helm}/{uid} returns the full
#     kind/metadata/spec/status resource (same shape as a list item), but
#     GET /v1/registries/oci/{uid} returns a FLAT spec-only object with no
#     metadata/status wrapper at all — name/uid/isDefault/isPrivate for an oci
#     registry come from the metadata pool, never from its own describe call.
#   - auth.password / auth.token come back as the literal string "********" on
#     every kind (pack, helm, oci) — server-side masking, confirmed live — so
#     unlike cloudaccounts there is no per-kind endpoint this verb must avoid
#     for secrecy; describe is safe to call.
def _registries_metadata(key, proj):
    data = api("GET", "/v1/registries/metadata", key, proj)
    return data.get("items") or []


def _registry_pool(items):
    """Normalize metadata's flat {kind,name,uid,...} rows into the same
    {"metadata": {"uid","name"}, ...} shape resolve_by_name expects everywhere
    else in this tool, instead of teaching resolve_by_name a second shape."""
    return [{"kind": i.get("kind"), "metadata": {"uid": i.get("uid"), "name": i.get("name")},
             "isDefault": bool(i.get("isDefault")), "isPrivate": bool(i.get("isPrivate")),
             "scope": i.get("scope")} for i in items]


def cmd_registries(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key) if a.project or os.environ.get("PALETTE_PROJECT") else None
    items = _registries_metadata(key, proj)
    if a.kind:
        items = [i for i in items if (i.get("kind") or "") == a.kind]
    pool = _registry_pool(items)

    if a.ref:
        hit = resolve_by_name(a.ref, pool, "registry")
        return _emit_registry_describe(a, key, proj, hit)

    rows = [{"name": p["metadata"]["name"], "uid": p["metadata"]["uid"], "kind": p["kind"],
             "isDefault": p["isDefault"], "isPrivate": p["isPrivate"], "scope": p["scope"]}
            for p in pool]
    rows.sort(key=lambda r: (r["kind"] or "", r["name"] or ""))
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    kinds_summary = " ".join(f"{k}={kinds[k]}" for k in sorted(kinds))
    emit(f"tenant={a.tenant}" + (f" project={proj}" if proj else ""),
         toon("registries", ["name", "uid", "kind", "isDefault", "isPrivate", "scope"], rows),
         f"\ncount:{len(rows)}\nkinds: {kinds_summary}",
         nxt("palette-axi registries <name> --kind pack|helm|oci"))


def _emit_registry_describe(a, key, proj, hit):
    uid, name, kind = hit["metadata"]["uid"], hit["metadata"]["name"], hit["kind"]
    if kind in ("pack", "helm"):
        item = api("GET", f"/v1/registries/{kind}/{uid}", key, proj)
        sp, st = dget(item, "spec"), dget(item, "status")
        sync = dget(st, "packSyncStatus") if kind == "pack" else dget(st, "helmSyncStatus")
        auth = dget(sp, "auth")
        row = {"name": name, "uid": uid, "kind": kind, "endpoint": sp.get("endpoint"),
               "authType": auth.get("type"), "tls": dget(auth, "tls").get("enabled"),
               "isPrivate": hit["isPrivate"], "isDefault": hit["isDefault"],
               "scope": sp.get("scope") or hit["scope"], "syncStatus": sync.get("status"), "ociType": None}
    else:  # oci -- flat spec object, no metadata/status wrapper (confirmed live)
        item = api("GET", f"/v1/registries/oci/{uid}", key, proj)
        auth = dget(item, "auth")
        row = {"name": name, "uid": uid, "kind": kind, "endpoint": item.get("endpoint"),
               "authType": auth.get("type"), "tls": dget(auth, "tls").get("enabled"),
               "isPrivate": hit["isPrivate"], "isDefault": hit["isDefault"],
               "scope": item.get("scope") or hit["scope"], "syncStatus": None, "ociType": item.get("type")}
    emit(f"tenant={a.tenant}" + (f" project={proj}" if proj else ""),
         toon("registry", ["name", "uid", "kind", "endpoint", "authType", "tls", "isPrivate",
                            "isDefault", "scope", "syncStatus", "ociType"], [row]))


def cmd_events(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    items = list_all("/v1/spectroclusters", key, proj)
    hit = resolve_by_name(a.ref, items, "cluster")
    uid = hit["metadata"]["uid"]
    # /v1/spectroclusters/{uid}/events and /status/events both 404 in practice —
    # confirmed by re-running the actual probe transcripts. The endpoint that
    # works is under the events-components collection.
    data = api("GET", f"/v1/events/components/spectrocluster/{uid}", key, proj, {"limit": a.limit})
    items = data.get("items") or []
    rows = []
    for e in items:
        sp, m = e.get("spec", e), dget(e, "metadata")
        rows.append({"time": ts(m.get("creationTimestamp")), "severity": sp.get("severity"),
                     "reason": sp.get("reason"), "message": sp.get("message") if a.full else trunc(sp.get("message"))})
    warn = sum(1 for r in rows if r["severity"] in ("Warning", "Error"))
    emit(f"tenant={a.tenant} project={proj} cluster={hit['metadata']['name']}",
         toon("events", ["time", "severity", "reason", "message"], rows),
         f"\ncount:{len(rows)} warnOrError:{warn}",
         nxt(f"palette-axi events {a.ref} --project {proj} --full --limit 200") if not a.full else "")


def cmd_packs(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key) if a.project or os.environ.get("PALETTE_PROJECT") else None
    items = list_all("/v1/packs", key, proj, filters=f"metadata.name={a.name}", page_limit=50, max_pages=10)
    active = [p for p in items if not dget(p, "status").get("disabled")]

    active.sort(key=lambda p: _vkey(dget(p, "spec").get("version")), reverse=True)
    rows = active if a.full else active[:12]
    trows = []
    for i, p in enumerate(rows):
        sp = dget(p, "spec")
        trows.append({"version": sp.get("version"), "uid": p["metadata"]["uid"],
                       "layer": sp.get("layer"), "registryUid": sp.get("registryUid"),
                       "latest": i == 0})
    hidden = len(active) - len(rows)
    emit(f"tenant={a.tenant} pack={a.name}",
         toon("versions", ["version", "uid", "layer", "registryUid", "latest"], trows),
         f"\ntotalVersions:{len(active)} disabledSkipped:{len(items) - len(active)}"
         + (f"  [+{hidden} more versions, --full]" if hidden > 0 else ""),
         nxt(f"palette-axi packs {a.name} --full") if hidden > 0 else "")


# Evidence probed 9/21/26 live, why this verb resolves the pack uid through
# the same /v1/packs search cmd_packs already uses rather than a name-only
# lookup: the same name+version can exist in more than one registry at once
# (confirmed live: csi-portworx-generic 3.7.0 existed in two registries in
# one project) -- --registry disambiguates by a registryUid PREFIX so an
# agent can paste the short id straight out of
# the ambiguity error. GET /v1/packs/{packUid}?includePackValues=true (with
# ProjectUid) returns packValues[0] = {values (default values YAML string),
# presets[]{name, group, label, add (YAML string), remove (list of dotted
# paths)}, readme, schema}. A live session had to drop to a raw urllib GET for
# this to build a Portworx layer, needing both the full default values and one
# named preset's add/remove pair.
def _resolve_pack_uid(name, version, key, proj, registry_prefix):
    items = list_all("/v1/packs", key, proj, filters=f"metadata.name={name}", page_limit=50, max_pages=10)
    active = [p for p in items if not dget(p, "status").get("disabled")]
    matches = [p for p in active if dget(p, "spec").get("version") == version]
    if not matches:
        die(f"no pack '{name}' version '{version}' found", E_NOTFOUND)
    if registry_prefix:
        matches = [p for p in matches
                   if (dget(p, "spec").get("registryUid") or "").startswith(registry_prefix)]
        if not matches:
            die(f"no pack '{name}' version '{version}' in a registry matching '{registry_prefix}'", E_NOTFOUND)
    if len(matches) > 1:
        regs = sorted({dget(p, "spec").get("registryUid") for p in matches})
        die(f"ambiguous pack '{name}' version '{version}': found in {len(regs)} registries "
            f"({', '.join(regs)}) -- pass --registry <uid-prefix>", E_USAGE)
    return matches[0]


def cmd_pack_values(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    hit = _resolve_pack_uid(a.name, a.version, key, proj, a.registry)
    uid = dget(hit, "metadata").get("uid")
    registry_uid = dget(hit, "spec").get("registryUid")
    data = api("GET", f"/v1/packs/{uid}", key, proj, {"includePackValues": "true"})
    pv_list = data.get("packValues") or []
    pv = pv_list[0] if pv_list else {}
    values_yaml = pv.get("values") or ""
    presets = pv.get("presets") or []

    if a.values:
        # Raw YAML to stdout and NOTHING else, so `> file.yaml` works.
        print(values_yaml.rstrip("\n"))
        return

    if a.preset:
        match = next((p for p in presets if (p or {}).get("name") == a.preset), None)
        if not match:
            names = ", ".join(sorted((p or {}).get("name", "") for p in presets)) or "(none)"
            die(f"no preset '{a.preset}' on pack {a.name} {a.version} -- known presets: {names}", E_NOTFOUND)
        print((match.get("add") or "").rstrip("\n"))
        print("# remove:")
        for r in match.get("remove") or []:
            print(f"#   {r}")
        return

    lines = len(values_yaml.splitlines())
    emit(f"tenant={a.tenant} project={proj}",
         toon("pack", ["name", "version", "uid", "registryUid", "lines", "presetCount"],
              [{"name": a.name, "version": a.version, "uid": uid, "registryUid": registry_uid,
                "lines": lines, "presetCount": len(presets)}]))
    prows = [{"name": (p or {}).get("name"), "group": (p or {}).get("group"),
              "removes": len((p or {}).get("remove") or [])} for p in presets]
    emit(toon("presets", ["name", "group", "removes"], prows),
         f"\npresetCount:{len(prows)}",
         nxt(f"palette-axi pack-values {a.name} {a.version} --project {proj} --values",
             f"palette-axi pack-values {a.name} {a.version} --project {proj} --preset <name>")
         if prows else "")


# ── write support ──────────────────────────────────────────────────────
# Safety model for every verb that changes Palette (README "Write verbs"):
#   * --project is REQUIRED on the command line. $PALETTE_PROJECT is not honored
#     for writes: an env var left over from another task must never pick the
#     project a create or delete lands in. Edge host tokens are the one
#     tenant-scoped write (the API rejects a ProjectUid header there).
#   * --dry-run runs the reads needed to build the request, then prints the exact
#     method, path, ProjectUid and body of every write it WOULD send, and sends none.
#   * Deletes need --yes, and refuse (E_REFUSED) any resource whose
#     metadata.annotations.projectUid is not the --project given, or whose scope is
#     not "project". Tenant-scoped objects are visible from a project; they are
#     never deleted from one.
#   * A profile change is a NEW version by default (`profile version`), never an
#     in-place edit of a version a cluster is running.
#   * Secrets in a printed body (apiKey, password, token, ...) render as ********.
SECRET_KEY_RE = re.compile(r"(apikey|api_key|password|secret|token|credential|privatekey|private_key)", re.I)


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("********" if SECRET_KEY_RE.search(k) and isinstance(v, str) and v else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def write_project(a, key):
    """Resolve --project for a write verb. Explicit flag only, see above."""
    if not getattr(a, "project", None):
        die("write verbs need an explicit --project <name|uid> ($PALETTE_PROJECT is not honored for writes)",
            E_USAGE)
    return resolve_project(a.project, key)


def load_json(path, what="body"):
    try:
        with open(path) as f:
            return json.load(f)
    except OSError as e:
        die(f"cannot read {what} file {path}: {e.strerror}", E_USAGE)
    except json.JSONDecodeError as e:
        die(f"{what} file {path} is not valid JSON: {e}", E_USAGE)


def read_text(path, what="file"):
    try:
        with open(path) as f:
            return f.read()
    except OSError as e:
        die(f"cannot read {what} {path}: {e.strerror}", E_USAGE)


def parse_kv(pairs, flag="--var"):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            die(f"{flag} must be name=value, got '{p}'", E_USAGE)
        k, v = p.split("=", 1)
        out[k] = v
    return out


def emit_plan(steps, proj):
    """steps: [(method, path, params, body)] -- printed, never sent."""
    rows = []
    for method, path, params, _ in steps:
        q = ("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})) if params else ""
        rows.append({"method": method, "path": path + q, "project": proj})
    emit("dry-run: no write request was sent", toon("request", ["method", "path", "project"], rows))
    for i, (method, path, _, body) in enumerate(steps):
        if body is not None:
            print(f"body[{i}] {method} {path}:")
            print(json.dumps(redact(body), indent=2))


def emit_changed(rows, *hints):
    emit(toon("changed", ["action", "kind", "name", "uid", "project"], rows), nxt(*hints) if hints else "")


def assert_in_project(obj, proj, kind, label):
    """The delete/edit guard: the object must belong to this project."""
    ann = dget(dget(obj, "metadata"), "annotations")
    scope, owner = ann.get("scope"), ann.get("projectUid")
    if scope != "project" or owner != proj:
        die(f"refusing: {kind} '{label}' has scope={scope or '?'} projectUid={owner or '?'}, "
            f"not project {proj}. palette-axi never changes or deletes objects outside --project.", E_REFUSED)


def write_file_600(path, data):
    path = os.path.expanduser(path)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(tmp, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    os.replace(tmp, path)
    return os.path.abspath(path)


def _resolve_cluster(ref, key, proj):
    if UID_RE.match(ref):
        c = api("GET", f"/v1/spectroclusters/{ref}", key, proj)
        return ref, dget(c, "metadata").get("name"), c
    hit = resolve_by_name(ref, list_clusters(key, proj), "cluster")
    uid = hit["metadata"]["uid"]
    return uid, hit["metadata"]["name"], api("GET", f"/v1/spectroclusters/{uid}", key, proj)


# ── profile writes ─────────────────────────────────────────────────────
def _profile_vars_from_body(body):
    sp = dget(body, "spec")
    return sp.get("variables") or dget(sp, "template").get("variables") or []


def cmd_profile_create(a):
    """POST /v1/clusterprofiles (draft), PUT .../variables when the body declares
    any, PATCH .../publish with --publish. Transcript evidence (sbux-kaas build,
    11/17/26): a POST body's spec.variables was dropped silently on one profile
    and kept on another, and the working scripts all followed the create with a
    PUT of the variables. PUT replaces the list with the same content, so sending
    it always is idempotent and removes the guesswork."""
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    body = load_json(a.file)
    name = dget(body, "metadata").get("name")
    if not name:
        die(f"{a.file}: metadata.name is required", E_USAGE)
    if not dget(body, "spec").get("version"):
        die(f"{a.file}: spec.version is required (e.g. 1.0.0)", E_USAGE)
    variables = _profile_vars_from_body(body)
    steps = [("POST", "/v1/clusterprofiles", None, body)]
    if variables:
        steps.append(("PUT", "/v1/clusterprofiles/<new-uid>/variables", None, {"variables": variables}))
    if a.publish:
        steps.append(("PATCH", "/v1/clusterprofiles/<new-uid>/publish", None, None))
    if a.dry_run:
        return emit_plan(steps, proj)
    uid = api("POST", "/v1/clusterprofiles", key, proj, json_body=body).get("uid")
    if not uid:
        die("POST /v1/clusterprofiles returned no uid", E_ERR)
    if variables:
        api("PUT", f"/v1/clusterprofiles/{uid}/variables", key, proj, json_body={"variables": variables})
    if a.publish:
        api("PATCH", f"/v1/clusterprofiles/{uid}/publish", key, proj)
    emit_changed([{"action": "created" + ("+published" if a.publish else " (draft)"), "kind": "clusterprofile",
                   "name": f"{name} {dget(body, 'spec').get('version')}", "uid": uid, "project": proj}],
                 f"palette-axi profile {uid} --project {proj} --variables")


def cmd_profile_version(a):
    """New version of an existing profile, never an edit in place (Craig's rule,
    9/16/26: keep every old version for traceability). POST
    /v1/clusterprofiles/{src}/clone {metadata:{name, version}} copies packs,
    values, manifests and variables server-side -- no export/import round trip,
    so masked sensitive values never break it. --set-pack-values then PUTs
    /v1/clusterprofiles/{new}/packs/{name} {pack:{name, uid, tag, type, layer,
    values}} on the NEW uid only.

    Confirmed live 11/18/26 (paxi-selftest): clone returns an already-PUBLISHED
    copy, and the pack PUT lands in its DRAFT -- the published values do not
    change until PATCH .../publish. So with --set-pack-values the new version is
    published by default (it is brand new, no cluster uses it); --no-publish
    leaves the edit in the draft."""
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    src_uid, _, _ = resolve_profile(a.ref, key, proj, version=a.from_version, strict=True)
    src = api("GET", f"/v1/clusterprofiles/{src_uid}", key, proj)
    assert_in_project(src, proj, "profile", src_uid)
    name = dget(src, "metadata").get("name")
    sp = dget(src, "spec")
    existing = {(v or {}).get("version") for v in sp.get("versions") or []} | {sp.get("version")}
    if a.version in existing:
        die(f"profile '{name}' already has version {a.version} -- pick a new one", E_USAGE)
    packs = {p.get("name"): p for p in dget(sp, "published").get("packs") or []}
    steps = [("POST", f"/v1/clusterprofiles/{src_uid}/clone", None,
              {"metadata": {"name": name, "version": a.version}})]
    for pk_name, path in parse_kv(a.set_pack_values, "--set-pack-values").items():
        pk = packs.get(pk_name)
        if not pk:
            die(f"profile '{name}' has no pack '{pk_name}' (packs: {', '.join(sorted(packs))})", E_NOTFOUND)
        steps.append(("PUT", f"/v1/clusterprofiles/<new-uid>/packs/{pk_name}", None,
                      {"pack": {"name": pk_name, "uid": pk.get("packUid"), "tag": pk.get("tag"),
                                "type": pk.get("type"), "layer": pk.get("layer"),
                                "values": read_text(path, "values file")}}))
    publish = len(steps) > 1 and not a.no_publish
    if publish:
        steps.append(("PATCH", "/v1/clusterprofiles/<new-uid>/publish", None, None))
    if a.dry_run:
        return emit_plan(steps, proj)
    out = api("POST", f"/v1/clusterprofiles/{src_uid}/clone", key, proj, json_body=steps[0][3])
    uid = out.get("uid")
    if not uid:
        die(f"clone returned no uid: {str(out)[:200]}", E_ERR)
    for method, path, _, body in steps[1:]:
        api(method, path.replace("<new-uid>", uid), key, proj, json_body=body)
    emit_changed([{"action": "created version" + (" +published" if publish else (" (pack edits in draft)" if len(steps) > 1 else "")), "kind": "clusterprofile",
                   "name": f"{name} {a.version} (from {sp.get('version')})", "uid": uid, "project": proj}],
                 f"palette-axi profile {uid} --project {proj}",
                 f"palette-axi cluster profiles swap <cluster> --from {src_uid} --to {uid} --project {proj}")


def cmd_profile_variables_set(a):
    """PUT /v1/clusterprofiles/{uid}/variables. --file replaces the whole list;
    --set name=default edits defaults of variables that already exist. Palette
    refuses variable edits on a version a cluster runs (VariableUpdateFailure),
    so this refuses up front and points at `profile version`."""
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    uid, _, _ = resolve_profile(a.ref, key, proj, version=a.version, strict=True)
    prof = api("GET", f"/v1/clusterprofiles/{uid}", key, proj)
    assert_in_project(prof, proj, "profile", uid)
    in_use = dget(prof, "status").get("inUseClusterUids") or []
    if in_use and not a.force:
        die(f"profile {uid} is used by {len(in_use)} cluster(s); Palette rejects variable edits on an in-use "
            "version. Create a new one: palette-axi profile version ... (or --force to try anyway)", E_REFUSED)
    if a.file:
        data = load_json(a.file, "variables")
        variables = data.get("variables") if isinstance(data, dict) else data
        if not isinstance(variables, list):
            die(f"{a.file}: expected {{\"variables\": [...]}} or a list", E_USAGE)
    else:
        sets = parse_kv(a.set, "--set")
        if not sets:
            die("give --file vars.json or one or more --set name=default", E_USAGE)
        variables = api("GET", f"/v1/clusterprofiles/{uid}/variables", key, proj).get("variables") or []
        known = {v.get("name") for v in variables}
        missing = sorted(set(sets) - known)
        if missing:
            die(f"no such variable(s) on profile {uid}: {', '.join(missing)} (known: {', '.join(sorted(known))})"
                " -- use --file to add new ones", E_NOTFOUND)
        for v in variables:
            if v.get("name") in sets:
                v["defaultValue"] = sets[v["name"]]
    body = {"variables": variables}
    if a.dry_run:
        return emit_plan([("PUT", f"/v1/clusterprofiles/{uid}/variables", None, body)], proj)
    api("PUT", f"/v1/clusterprofiles/{uid}/variables", key, proj, json_body=body)
    emit_changed([{"action": f"set {len(variables)} variables", "kind": "clusterprofile",
                   "name": dget(prof, "metadata").get("name"), "uid": uid, "project": proj}],
                 f"palette-axi profile {uid} --project {proj} --variables")


def cmd_profile_publish(a):
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    uid, _, _ = resolve_profile(a.ref, key, proj, version=a.version, strict=True)
    prof = api("GET", f"/v1/clusterprofiles/{uid}", key, proj)
    assert_in_project(prof, proj, "profile", uid)
    if a.dry_run:
        return emit_plan([("PATCH", f"/v1/clusterprofiles/{uid}/publish", None, None)], proj)
    api("PATCH", f"/v1/clusterprofiles/{uid}/publish", key, proj)
    emit_changed([{"action": "published", "kind": "clusterprofile", "name": dget(prof, "metadata").get("name"),
                   "uid": uid, "project": proj}])


def cmd_profile_delete(a):
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    uid, _, _ = resolve_profile(a.ref, key, proj, version=a.version, strict=True)
    prof = api("GET", f"/v1/clusterprofiles/{uid}", key, proj)
    label = f"{dget(prof, 'metadata').get('name')} {dget(prof, 'spec').get('version')}"
    assert_in_project(prof, proj, "profile", label)
    in_use = dget(prof, "status").get("inUseClusterUids") or []
    if in_use:
        die(f"refusing: profile {label} ({uid}) is attached to cluster(s) {', '.join(in_use)}", E_REFUSED)
    if a.dry_run:
        return emit_plan([("DELETE", f"/v1/clusterprofiles/{uid}", None, None)], proj)
    if not a.yes:
        die(f"would delete profile {label} ({uid}) in project {proj}; re-run with --yes", E_REFUSED)
    api("DELETE", f"/v1/clusterprofiles/{uid}", key, proj)
    emit_changed([{"action": "deleted", "kind": "clusterprofile", "name": label, "uid": uid, "project": proj}])


def cmd_profile_export(a):
    """GET /v1/clusterprofiles/{uid}/export: name+tag based JSON with manifests
    inline and variables -- the input `profile create --file` and the native
    /import both take. Needs Accept: octet-stream (see api_raw)."""
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    uid, _, _ = resolve_profile(a.ref, key, proj, version=a.version)
    data = api_raw("GET", f"/v1/clusterprofiles/{uid}/export", key, proj)
    try:
        doc = json.loads(data)
    except json.JSONDecodeError:
        die(f"export of {uid} was not JSON ({len(data)} bytes)", E_ERR)
    if set(doc) == {"Reader"}:
        die("export returned the empty {\"Reader\":{}} placeholder -- Accept header problem", E_ERR)
    text = json.dumps(doc, indent=2)
    if a.out:
        path = write_file_600(a.out, text + "\n")
        emit(toon("export", ["name", "version", "uid", "path", "bytes"],
                  [{"name": dget(doc, "metadata").get("name"), "version": dget(doc, "spec").get("version"),
                    "uid": uid, "path": path, "bytes": len(text)}]))
    else:
        print(text)


# ── cluster reads/writes ───────────────────────────────────────────────
KUBECONFIG_KINDS = {"default": "kubeconfig", "admin": "adminKubeconfig", "client": "kubeconfigclient"}


def cmd_cluster_kubeconfig(a):
    """GET /v1/spectroclusters/{uid}/assets/{kubeconfig|adminKubeconfig|kubeconfigclient}.
    Written to a mode-600 file, never stdout unless --stdout: it is a credential.
    The default (`kubeconfig`) is the OIDC/non-admin one when the cluster has
    OIDC configured; --admin is the admin cert kubeconfig."""
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    uid, name, _ = _resolve_cluster(a.ref, key, proj)
    kind = "admin" if a.admin else ("client" if a.client else "default")
    data = api_raw("GET", f"/v1/spectroclusters/{uid}/assets/{KUBECONFIG_KINDS[kind]}", key, proj)
    if b"apiVersion" not in data or b"clusters" not in data:
        die(f"{KUBECONFIG_KINDS[kind]} for {name} is not a kubeconfig ({len(data)} bytes) -- cluster may not "
            "be Running yet", E_ERR)
    if a.stdout:
        sys.stdout.write(data.decode(errors="replace"))
        return
    path = write_file_600(a.out or f"./{name}.kubeconfig", data)
    emit(toon("kubeconfig", ["cluster", "uid", "kind", "path", "mode"],
              [{"cluster": name, "uid": uid, "kind": kind, "path": path, "mode": "600"}]),
         nxt(f"KUBECONFIG={path} kubectl get nodes"))


def _cluster_vars(key, proj, uid):
    """GET /v1/spectroclusters/{uid}/variables -> [{profileUid, variables:[{name,
    value, isSensitive, ...}]}] (confirmed live 11/18/26)."""
    data = api("GET", f"/v1/spectroclusters/{uid}/variables", key, proj)
    return data if isinstance(data, list) else (data.get("items") or [])


def cmd_cluster_variables(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    uid, name, _ = _resolve_cluster(a.ref, key, proj)
    rows = []
    for e in _cluster_vars(key, proj, uid):
        for v in e.get("variables") or []:
            sens = bool(v.get("isSensitive"))
            rows.append({"profileUid": e.get("profileUid"), "name": v.get("name"),
                         "value": "********" if sens else v.get("value"),
                         "default": "********" if sens else v.get("defaultValue"), "sensitive": sens})
    emit(f"tenant={a.tenant} project={proj} cluster={name}",
         toon("variables", ["profileUid", "name", "value", "default", "sensitive"], rows),
         f"\ncount:{len(rows)}")


def cmd_cluster_rbac(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    uid, name, _ = _resolve_cluster(a.ref, key, proj)
    items = api("GET", f"/v1/spectroclusters/{uid}/config/rbacs", key, proj).get("items") or []
    rows = []
    for r in items:
        for b in dget(r, "spec").get("bindings") or []:
            subs = ", ".join(f"{s.get('type')}:{s.get('name')}" for s in b.get("subjects") or [])
            rows.append({"type": b.get("type"), "namespace": b.get("namespace"),
                         "role": dget(b, "role").get("name"), "subjects": subs})
    emit(f"tenant={a.tenant} project={proj} cluster={name}",
         toon("bindings", ["type", "namespace", "role", "subjects"], rows), f"\ncount:{len(rows)}")


def cmd_cluster_rbac_set(a):
    """PUT /v1/spectroclusters/{uid}/config/rbacs with the --file body verbatim
    ({"rbacs":[{"spec":{"bindings":[...]}}]}). It REPLACES the cluster's RBAC set."""
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    uid, name, c = _resolve_cluster(a.ref, key, proj)
    assert_in_project(c, proj, "cluster", name)
    body = load_json(a.file)
    if not isinstance(body, dict) or not isinstance(body.get("rbacs"), list):
        die(f"{a.file}: expected {{\"rbacs\": [...]}}", E_USAGE)
    if a.dry_run:
        return emit_plan([("PUT", f"/v1/spectroclusters/{uid}/config/rbacs", None, body)], proj)
    api("PUT", f"/v1/spectroclusters/{uid}/config/rbacs", key, proj, json_body=body)
    emit_changed([{"action": "rbac replaced", "kind": "cluster", "name": name, "uid": uid, "project": proj}],
                 f"palette-axi cluster rbac {name} --project {proj}")


CLUSTER_CREATE_KINDS = ("edge-native", "maas", "virtual")


def check_cluster_body(kind, body):
    """Refuse bodies with a known silent failure. 11/2026: POST
    /v1/spectroclusters/edge-native returns 201 with cloudConfig.vip and drops
    it -- the cluster never gets a control-plane endpoint and cannot be repaired
    while Provisioning. The field it honours is
    cloudConfig.controlPlaneEndpoint {host, type: VIP}."""
    if not dget(body, "metadata").get("name"):
        die("body: metadata.name is required", E_USAGE)
    cc = dget(dget(body, "spec"), "cloudConfig")
    if kind == "edge-native" and cc.get("vip") and not dget(cc, "controlPlaneEndpoint").get("host"):
        die("refusing: spec.cloudConfig.vip is silently dropped by POST /v1/spectroclusters/edge-native. "
            'Use "controlPlaneEndpoint": {"host": "<ip>", "type": "VIP"} instead.', E_REFUSED)
    if kind == "virtual":
        hcc = dget(dget(dget(body, "spec"), "clusterConfig"), "hostClusterConfig")
        if not dget(hcc, "clusterGroup").get("uid"):
            die("body: spec.clusterConfig.hostClusterConfig.clusterGroup.uid is required for a virtual cluster",
                E_USAGE)


def cmd_cluster_create(a):
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    body = load_json(a.file)
    check_cluster_body(a.kind, body)
    name = body["metadata"]["name"]
    path = f"/v1/spectroclusters/{a.kind}"
    if a.dry_run:
        return emit_plan([("POST", path, None, body)], proj)
    uid = api("POST", path, key, proj, json_body=body).get("uid")
    emit_changed([{"action": "created", "kind": f"cluster/{a.kind}", "name": name, "uid": uid, "project": proj}],
                 f"palette-axi cluster {uid} --project {proj}", f"palette-axi events {uid} --project {proj}")


def _profile_entry(uid, variables):
    e = {"uid": uid}
    if variables:
        e["variables"] = [{"name": k, "value": v} for k, v in variables.items()]
    return e


def cmd_cluster_profiles_attach(a):
    """PATCH /v1/spectroclusters/{uid}/profiles?resolveNotification=false --
    additive: attaches without detaching anything (spectrocloud-clusters skill)."""
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    uid, name, c = _resolve_cluster(a.ref, key, proj)
    assert_in_project(c, proj, "cluster", name)
    puid, _, _ = resolve_profile(a.profile, key, proj, version=a.version, strict=True)
    attached = {t.get("uid") for t in dget(c, "spec").get("clusterProfileTemplates") or []}
    if puid in attached:
        die(f"profile {puid} is already attached to {name}", E_USAGE)
    body = {"profiles": [_profile_entry(puid, parse_kv(a.var))]}
    params = {"resolveNotification": "false"}
    if a.dry_run:
        return emit_plan([("PATCH", f"/v1/spectroclusters/{uid}/profiles", params, body)], proj)
    api("PATCH", f"/v1/spectroclusters/{uid}/profiles", key, proj, params, json_body=body)
    emit_changed([{"action": f"attached profile {puid}", "kind": "cluster", "name": name, "uid": uid,
                   "project": proj}], f"palette-axi cluster {name} --project {proj} --profiles")


def cmd_cluster_profiles_swap(a):
    """Point a cluster at a different profile version. PUT
    /v1/spectroclusters/{uid}/profiles REPLACES the attached set, so the body
    carries every currently attached uid, with --from swapped for --to.

    A new profile version is a new uid, and the cluster's variable overrides were
    keyed to the old one: a bare swap resets them to the new version's defaults
    (tds-cluster incident: MetalLB range/interface reset to placeholders). So the
    current values are read from GET .../variables and re-supplied for the new
    uid, plus --var overrides. Sensitive values come back as ******** and cannot
    be carried; the swap refuses unless each is given with --var or
    --allow-sensitive-reset is passed."""
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    uid, name, c = _resolve_cluster(a.ref, key, proj)
    assert_in_project(c, proj, "cluster", name)
    templates = dget(c, "spec").get("clusterProfileTemplates") or []
    attached = [t.get("uid") for t in templates]
    from_uid, _, _ = resolve_profile(a.from_profile, key, proj, strict=True) if not UID_RE.match(a.from_profile) \
        else (a.from_profile, None, None)
    if from_uid not in attached:
        die(f"profile {from_uid} is not attached to {name} (attached: {', '.join(attached)})", E_NOTFOUND)
    to_uid, _, _ = resolve_profile(a.to_profile, key, proj, version=a.to_version, strict=True)
    if to_uid == from_uid:
        die("--from and --to are the same profile uid", E_USAGE)
    overrides = parse_kv(a.var)
    current = next((e for e in _cluster_vars(key, proj, uid) if e.get("profileUid") == from_uid), {})
    carried, masked = {}, []
    for v in current.get("variables") or []:
        n = v.get("name")
        if n in overrides:
            continue
        if v.get("isSensitive"):
            masked.append(n)
        elif v.get("value") not in (None, ""):
            carried[n] = v.get("value")
    if masked and not a.allow_sensitive_reset:
        die(f"refusing: sensitive variable(s) {', '.join(masked)} come back masked and would reset to the new "
            "version's defaults. Re-supply each with --var name=value, or pass --allow-sensitive-reset.", E_REFUSED)
    new_vars = {**carried, **overrides}
    profiles = []
    for t in attached:
        profiles.append(_profile_entry(to_uid, new_vars) if t == from_uid else {"uid": t})
    body = {"profiles": profiles}
    if a.dry_run:
        emit(f"carried:{len(carried)} overridden:{len(overrides)} sensitiveReset:{len(masked)}")
        return emit_plan([("PUT", f"/v1/spectroclusters/{uid}/profiles", None, body)], proj)
    api("PUT", f"/v1/spectroclusters/{uid}/profiles", key, proj, json_body=body)
    emit_changed([{"action": f"swapped {from_uid} -> {to_uid} ({len(new_vars)} vars re-supplied)", "kind": "cluster",
                   "name": name, "uid": uid, "project": proj}],
                 f"palette-axi cluster variables {name} --project {proj}",
                 f"palette-axi cluster {name} --project {proj} --profiles")


def cmd_cluster_delete(a):
    """DELETE /v1/spectroclusters/{uid}; --force adds ?forceDelete=true, which
    Palette only accepts once the cluster is already Deleting (and, per the
    sbux-kaas build 11/17/26, not until 15 min after an aborted delete). So
    --force refuses unless status.state is Deleting."""
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    uid, name, c = _resolve_cluster(a.ref, key, proj)
    assert_in_project(c, proj, "cluster", name)
    state = dget(c, "status").get("state")
    params = None
    if a.force:
        if state != "Deleting":
            die(f"refusing --force: cluster {name} is '{state}', not Deleting. Run a plain delete first; "
                "Palette only accepts forceDelete on a Deleting cluster.", E_REFUSED)
        params = {"forceDelete": "true"}
    if a.dry_run:
        return emit_plan([("DELETE", f"/v1/spectroclusters/{uid}", params, None)], proj)
    if not a.yes:
        die(f"would delete cluster {name} ({uid}, state {state}) in project {proj}; re-run with --yes", E_REFUSED)
    api("DELETE", f"/v1/spectroclusters/{uid}", key, proj, params)
    emit_changed([{"action": "force-deleted" if a.force else "delete requested", "kind": "cluster", "name": name,
                   "uid": uid, "project": proj}], f"palette-axi cluster {uid} --project {proj}")


# ── PCGs, cloud accounts, cluster groups, virtual clusters, tokens ────
def cmd_pcgs(a):
    """GET /v1/overlords (confirmed live 11/18/26): kind, annotations.overlordVersion,
    status.state, status.health.{state,lastHeartBeatTimestamp}, spec.ipAddress."""
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key) if a.project or os.environ.get("PALETTE_PROJECT") else None
    items = api("GET", "/v1/overlords", key, proj).get("items") or []
    rows = []
    for o in items:
        m, st, sp = dget(o, "metadata"), dget(o, "status"), dget(o, "spec")
        h = dget(st, "health")
        rows.append({"name": m.get("name"), "uid": m.get("uid"), "kind": o.get("kind"),
                     "scope": dget(m, "annotations").get("scope"),
                     "version": dget(m, "annotations").get("overlordVersion"), "state": st.get("state"),
                     "health": h.get("state"), "lastHeartbeat": ts(h.get("lastHeartBeatTimestamp")),
                     "ip": sp.get("ipAddress")})
    rows.sort(key=lambda r: (r["kind"] or "", r["name"] or ""))
    unhealthy = sum(1 for r in rows if is_unhealthy(r["health"]))
    emit(f"tenant={a.tenant}" + (f" project={proj}" if proj else ""),
         toon("pcgs", ["name", "uid", "kind", "scope", "version", "state", "health", "lastHeartbeat", "ip"], rows),
         f"\ncount:{len(rows)} unhealthy:{unhealthy}", nxt("palette-axi pcg <name>"))


def cmd_pcg(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key) if a.project or os.environ.get("PALETTE_PROJECT") else None
    if UID_RE.match(a.ref):
        uid = a.ref
    else:
        uid = resolve_by_name(a.ref, api("GET", "/v1/overlords", key, proj).get("items") or [], "pcg")["metadata"]["uid"]
    o = api("GET", f"/v1/overlords/{uid}", key, proj)
    m, st, sp = dget(o, "metadata"), dget(o, "status"), dget(o, "spec")
    h = dget(st, "health")
    emit(toon("pcg", ["name", "uid", "kind", "version", "state", "health", "agentVersion", "lastHeartbeat",
                      "ip", "cloudAccountUid", "clusterUid", "isReady"],
              [{"name": m.get("name"), "uid": uid, "kind": o.get("kind"),
                "version": dget(m, "annotations").get("overlordVersion"), "state": st.get("state"),
                "health": h.get("state"), "agentVersion": h.get("agentVersion"),
                "lastHeartbeat": ts(h.get("lastHeartBeatTimestamp")), "ip": sp.get("ipAddress"),
                "cloudAccountUid": sp.get("cloudAccountUid"), "clusterUid": sp.get("spectroClusterUid"),
                "isReady": st.get("isReady")}]))
    conds = h.get("conditions") or []
    show = conds if a.full else [x for x in conds if (x or {}).get("status") not in ("True", True)]
    emit(toon("conditions", ["type", "status", "reason", "message"],
              [{"type": x.get("type"), "status": x.get("status"), "reason": x.get("reason"),
                "message": trunc(x.get("message"), 100)} for x in show if x]))


def cmd_cloudaccount(a):
    """Describe one account: GET /v1/cloudaccounts/{kind}/{uid}. That endpoint
    returns live credentials (maas spec.apiKey, aws secretKey, ...) -- every
    secret-shaped key is masked before anything prints, --json included."""
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key) if a.project or os.environ.get("PALETTE_PROJECT") else None
    items = list_all("/v1/cloudaccounts/summary", key, proj)
    if a.cloud:
        items = [i for i in items if (i.get("kind") or "").lower() == a.cloud.lower()]
    hit = resolve_by_name(a.ref, items, "cloud account")
    kind, uid = hit.get("kind"), hit["metadata"]["uid"]
    acct = redact(api("GET", f"/v1/cloudaccounts/{kind}/{uid}", key, proj))
    if a.json:
        print(json.dumps(acct, indent=2))
        return
    m, sp = dget(acct, "metadata"), dget(acct, "spec")
    ann = dget(m, "annotations")
    emit(toon("cloudaccount", ["name", "uid", "kind", "scope", "projectUid", "pcg", "endpoint", "created"],
              [{"name": m.get("name"), "uid": uid, "kind": kind, "scope": ann.get("scope"),
                "projectUid": ann.get("projectUid"), "pcg": ann.get("overlordUid") or None,
                "endpoint": sp.get("apiEndpoint") or sp.get("vcenterServer") or sp.get("identityEndpoint"),
                "created": ts(m.get("creationTimestamp"))}]),
         f"specKeys: {', '.join(sorted(sp))}  (secrets masked; --json for the redacted document)")


def cmd_cloudaccount_create(a):
    """POST /v1/cloudaccounts/maas?privateGateway=<pcg uid> with ProjectUid,
    body {metadata:{name}, spec:{apiEndpoint, apiKey}} (sbux-kaas build
    11/17/26: accepted before the PCG was Running). The MAAS API key comes from
    --api-key-env VAR or --api-key-file, never argv (it would land in shell
    history and `ps`), and prints masked."""
    key = get_api_key(a.tenant)
    proj = write_project(a, key)
    if a.cloud != "maas":
        die("only maas is supported", E_USAGE)
    if a.file:
        body = load_json(a.file)
    else:
        if not (a.name and a.endpoint):
            die("give --file body.json, or --name, --endpoint and --api-key-env/--api-key-file", E_USAGE)
        if a.api_key_env:
            secret = os.environ.get(a.api_key_env)
            if not secret:
                die(f"${a.api_key_env} is unset or empty", E_USAGE)
        elif a.api_key_file:
            secret = read_text(a.api_key_file, "api key file").strip()
        else:
            die("the MAAS API key must come from --api-key-env VAR or --api-key-file PATH", E_USAGE)
        body = {"metadata": {"name": a.name}, "spec": {"apiEndpoint": a.endpoint, "apiKey": secret}}
    params = None
    if a.pcg:
        pcg_uid = a.pcg if UID_RE.match(a.pcg) else resolve_by_name(
            a.pcg, api("GET", "/v1/overlords", key, proj).get("items") or [], "pcg")["metadata"]["uid"]
        params = {"privateGateway": pcg_uid}
    if a.dry_run:
        return emit_plan([("POST", "/v1/cloudaccounts/maas", params, body)], proj)
    uid = api("POST", "/v1/cloudaccounts/maas", key, proj, params, json_body=body).get("uid")
    emit_changed([{"action": "created", "kind": "cloudaccount/maas", "name": dget(body, "metadata").get("name"),
                   "uid": uid, "project": proj}], f"palette-axi cloudaccount {uid} --project {proj}")


def cmd_clustergroups(a):
    """GET /v1/clustergroups/hostCluster/metadata (GET /v1/clustergroups is
    POST-only = create; /v1/dashboard/clustergroups 404s -- probed 11/18/26).
    UNVERIFIED with a populated project: SA-Craig-Smith had no groups left."""
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    items = api("GET", "/v1/clustergroups/hostCluster/metadata", key, proj).get("items") or []
    rows = [{"name": dget(i, "metadata").get("name"), "uid": dget(i, "metadata").get("uid"),
             "scope": dget(dget(i, "metadata"), "annotations").get("scope")} for i in items]
    emit(f"tenant={a.tenant} project={proj}", toon("clustergroups", ["name", "uid", "scope"], rows),
         f"\ncount:{len(rows)}", nxt("palette-axi clustergroup <uid>") if rows else "")


def cmd_clustergroup(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    uid = a.ref
    if not UID_RE.match(uid):
        items = api("GET", "/v1/clustergroups/hostCluster/metadata", key, proj).get("items") or []
        uid = resolve_by_name(a.ref, items, "cluster group")["metadata"]["uid"]
    g = api("GET", f"/v1/clustergroups/{uid}", key, proj)
    m, sp = dget(g, "metadata"), dget(g, "spec")
    cc = dget(sp, "clustersConfig")
    lim = dget(cc, "limitConfig")
    emit(toon("clustergroup", ["name", "uid", "scope", "hosts", "endpointType", "distro", "cpuMilliCore",
                               "memoryMiB", "storageGiB", "overSubscription"],
              [{"name": m.get("name"), "uid": uid, "scope": dget(m, "annotations").get("scope"),
                "hosts": len(sp.get("clusterRefs") or []), "endpointType": cc.get("endpointType"),
                "distro": cc.get("kubernetesDistroType"), "cpuMilliCore": lim.get("cpuMilliCore"),
                "memoryMiB": lim.get("memoryMiB"), "storageGiB": lim.get("storageGiB"),
                "overSubscription": lim.get("overSubscription")}]),
         toon("hosts", ["clusterUid", "name"], [{"clusterUid": r.get("clusterUid"), "name": r.get("clusterName")}
                                                for r in sp.get("clusterRefs") or []]))


def cmd_vclusters(a):
    """Virtual clusters are spectroclusters; list_clusters() already returns them.
    Filtered on cloudType nested/virtual. UNVERIFIED live: no virtual cluster
    existed in SA-Craig-Smith on 11/18/26 to confirm which string Palette uses."""
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    items = [c for c in list_clusters(key, proj) if (_cloud_type(c) or "") in ("nested", "virtual")]
    rows = [{"name": dget(c, "metadata").get("name"), "uid": dget(c, "metadata").get("uid"),
             "state": dget(c, "status").get("state"), "health": dget(dget(c, "status"), "health").get("state")}
            for c in items]
    emit(f"tenant={a.tenant} project={proj}", toon("vclusters", ["name", "uid", "state", "health"], rows),
         f"\ncount:{len(rows)}" + truncation_note())


def cmd_edgehost_token_create(a):
    """POST /v1/edgehosts/tokens -- TENANT-scoped: the API rejects a ProjectUid
    header with ProjectUidIsNotEmpty (spectrocloud-clusters skill). The token is
    at spec.token; it is a registration credential, so it goes to a mode-600
    file unless --stdout."""
    key = get_api_key(a.tenant)
    body = {"metadata": {"name": a.name}}
    if a.expiry:
        body["spec"] = {"expiry": a.expiry}
    if a.dry_run:
        return emit_plan([("POST", "/v1/edgehosts/tokens", None, body)], "(tenant scope, no ProjectUid)")
    out = api("POST", "/v1/edgehosts/tokens", key, None, json_body=body)
    uid = out.get("uid")
    tok = dget(out, "spec").get("token")
    if not tok and uid:
        tok = dget(api("GET", f"/v1/edgehosts/tokens/{uid}", key, None), "spec").get("token")
    if not tok:
        die(f"token {uid} created but no spec.token came back", E_ERR)
    if a.stdout:
        print(tok)
        return
    path = write_file_600(a.out or f"./{a.name}.token", tok + "\n")
    emit_changed([{"action": "created", "kind": "edgehost-token", "name": a.name, "uid": uid,
                   "project": "(tenant)"}], f"token written to {path} (mode 600)")


# ── doctor ─────────────────────────────────────────────────────────────
# One table, in code, of every connector this tool depends on. Each probe
# function returns {"need": "required"|"optional", "status": "ok"|"down"|
# "absent"|"skip", "detail": "..."} and must never raise — cmd_doctor also
# wraps every call so a single broken probe can't take the whole command down.
def _safe_call(fn, *args, **kwargs):
    """Run one of the die()-based helpers above for a doctor probe, without
    letting that helper's sys.exit() end the doctor process. die() prints
    "error: <msg>" to stderr then exits; capture that instead of the exit.
    Returns (result, None) on success or (None, message) on failure."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            return fn(*args, **kwargs), None
    except SystemExit:
        msg = buf.getvalue().strip()
        if msg.startswith("error: "):
            msg = msg[len("error: "):]
        return None, msg or "failed (no error detail captured)"


def _classify_op_failure(msg):
    """'absent' means the dependency/resource simply isn't there (op missing,
    no matching item) — anything else (auth, ambiguity, transport) is 'down'."""
    lower = msg.lower()
    if lower.startswith("not found on path") or "no 1password item" in lower:
        return "absent"
    return "down"


def _probe_onepassword(tenant):
    """Required unless PALETTE_API_KEY is set. Probes that `op` is on PATH
    AND the tenant's item (or PALETTE_AXI_OP_ITEM, if set) actually resolves —
    reuses the exact same helpers the real verbs use, so this can't drift from
    what `get_api_key` will do."""
    if os.environ.get("PALETTE_API_KEY"):
        return {"need": "optional", "status": "skip",
                "detail": "PALETTE_API_KEY is set — 1Password is not used"}

    item_id = os.environ.get("PALETTE_AXI_OP_ITEM")
    if item_id:
        _, err = _safe_call(_op_secret_value, item_id)
        if err:
            return {"need": "required", "status": _classify_op_failure(err),
                    "detail": f"{err} — check PALETTE_AXI_OP_ITEM={item_id}, "
                              "or unset it to resolve by tenant instead"}
        return {"need": "required", "status": "ok",
                "detail": f"op on PATH — PALETTE_AXI_OP_ITEM={item_id} resolves in vault {OP_VAULT}"}

    _, err = _safe_call(_op_item_id_for_tenant, tenant)
    if err:
        return {"need": "required", "status": _classify_op_failure(err),
                "detail": f"{err} — fix: run `op signin`, set PALETTE_AXI_OP_ITEM=<id>, "
                          "or set PALETTE_API_KEY"}
    return {"need": "required", "status": "ok",
            "detail": f"op on PATH — item 'Palette API Key ({tenant})' in vault {OP_VAULT}"}


def _probe_palette_api(tenant):
    """Required. Probe = a cheap authenticated read on the same endpoint the
    `projects` verb already uses (limit=1), via the same api()/get_api_key()
    code path — this can't call a different endpoint than the real verbs do."""
    key, err = _safe_call(get_api_key, tenant)
    if err:
        return {"need": "required", "status": "down",
                "detail": f"no API key available: {err} — set PALETTE_API_KEY, "
                          "or fix 1Password (see the onepassword row)"}
    _, err = _safe_call(api, "GET", PROJECTS_PATH, key, None, {"limit": 1})
    if err:
        return {"need": "required", "status": "down",
                "detail": f"{err} — check PALETTE_API_KEY / the 1Password item value, "
                          f"or network access to {API_BASE}"}
    return {"need": "required", "status": "ok",
            "detail": f"GET {PROJECTS_PATH} succeeded — key accepted"}


# name -> probe(tenant). Add a new connector here and doctor picks it up.
DOCTOR_CONNECTORS = [
    ("onepassword", _probe_onepassword),
    ("palette-api", _probe_palette_api),
]


def _config_rows(tenant):
    """The env vars this tool reads, their effective value, and where that
    value came from — never the API key's actual value."""
    def row(var, value, source):
        return {"var": var, "value": value, "source": source}

    rows = []

    env_key = os.environ.get("PALETTE_API_KEY")
    rows.append(row("PALETTE_API_KEY", "set" if env_key else "unset", "env" if env_key else "unset"))

    proj = os.environ.get("PALETTE_PROJECT")
    rows.append(row("PALETTE_PROJECT", proj or "", "env" if proj else "unset"))

    tenant_flag = any(arg == "--tenant" or arg.startswith("--tenant=") for arg in sys.argv[1:])
    tenant_env = os.environ.get("PALETTE_AXI_TENANT")
    rows.append(row("PALETTE_AXI_TENANT", tenant,
                     "flag" if tenant_flag else ("env" if tenant_env else "default")))

    vault_env = os.environ.get("PALETTE_AXI_VAULT")
    rows.append(row("PALETTE_AXI_VAULT", OP_VAULT, "env" if vault_env else "default"))

    op_item = os.environ.get("PALETTE_AXI_OP_ITEM")
    rows.append(row("PALETTE_AXI_OP_ITEM", op_item or "", "env" if op_item else "unset"))

    ttl_env = os.environ.get("PALETTE_AXI_KEY_TTL")
    rows.append(row("PALETTE_AXI_KEY_TTL", str(_key_ttl()), "env" if ttl_env else "default"))
    rows.append(row("key cache", _key_cache_status(tenant), tenant))

    return rows


def cmd_doctor(a):
    tenant = a.tenant
    connectors = []
    for name, probe in DOCTOR_CONNECTORS:
        try:
            result = probe(tenant)
        except Exception as e:  # a probe must never take the whole command down
            result = {"need": "required", "status": "down", "detail": f"probe crashed: {e}"}
        row = {"name": name}
        row.update(result)
        connectors.append(row)

    config = _config_rows(tenant)
    required_bad = [c for c in connectors if c["need"] == "required" and c["status"] != "ok"]

    if a.json:
        print(json.dumps({"connectors": connectors, "config": config}, indent=2))
    else:
        emit(toon("connectors", ["name", "need", "status", "detail"], connectors),
             toon("config", ["var", "value", "source"], config))

    sys.exit(E_ERR if required_bad else E_OK)


# `palette-axi profile create ...` reads better than `profile-create`, but
# `profile <ref>` and `cluster <ref>` already take a positional. Rather than
# nest subparsers (which would change those verbs), rewrite the two-word forms
# to the hyphenated verbs before argparse sees them. Consequence: a profile or
# cluster literally named one of these action words must be addressed by uid.
VERB_REWRITES = {
    ("profile", "variables", "set"): "profile-variables-set",
    ("profile", "create"): "profile-create", ("profile", "version"): "profile-version",
    ("profile", "publish"): "profile-publish", ("profile", "delete"): "profile-delete",
    ("profile", "export"): "profile-export",
    ("cluster", "profiles", "attach"): "cluster-profiles-attach",
    ("cluster", "profiles", "swap"): "cluster-profiles-swap",
    ("cluster", "rbac", "set"): "cluster-rbac-set", ("cluster", "rbac"): "cluster-rbac",
    ("cluster", "create"): "cluster-create", ("cluster", "delete"): "cluster-delete",
    ("cluster", "kubeconfig"): "cluster-kubeconfig", ("cluster", "variables"): "cluster-variables",
    ("cluster", "events"): "events",
    ("cloudaccount", "create"): "cloudaccount-create",
    ("edgehost", "token", "create"): "edgehost-token-create",
}


def rewrite_argv(argv):
    argv = list(argv)
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] == "--tenant" else 1
    for n in (3, 2):
        key = tuple(argv[i:i + n])
        if key in VERB_REWRITES:
            return argv[:i] + [VERB_REWRITES[key]] + argv[i + n:]
    return argv


# ── cli ────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(prog="palette-axi", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default=DEFAULT_TENANT,
                    help=f"1Password 'Palette API Key (<tenant>)' item to use (default: {DEFAULT_TENANT})")
    p.add_argument("--version", action="version", version=f"palette-axi {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("projects", help="list projects in the tenant")
    s.set_defaults(fn=cmd_projects)

    s = sub.add_parser("clusters", help="list clusters (state, health) in a project")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.add_argument("--edge", action="store_true", help="edge-native clusters only")
    s.set_defaults(fn=cmd_clusters)

    s = sub.add_parser("cluster", help="describe one cluster: state, health, pools, conditions")
    s.add_argument("ref", help="cluster name or uid")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.add_argument("--full", action="store_true", help="show all conditions, not just non-True ones")
    s.add_argument("--profiles", action="store_true",
                   help="add profiles (name, version, type, uid) and packs (profile, layer, name, "
                        "tag) tables from spec.clusterProfileTemplates -- no extra API call")
    s.set_defaults(fn=cmd_cluster)

    s = sub.add_parser("profiles", help="list cluster profiles in a project")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.set_defaults(fn=cmd_profiles)

    s = sub.add_parser("profile", help="describe one cluster profile: layers/packs")
    s.add_argument("ref", help="profile name or uid")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.add_argument("--full", action="store_true", help="untruncated registry uids")
    s.add_argument("--variables", action="store_true",
                   help="add a variables table (name, default, format, required, immutable, "
                        "hidden, sensitive) from GET /v1/clusterprofiles/{uid}/variables; a "
                        "sensitive variable's default always renders as ********")
    s.set_defaults(fn=cmd_profile)

    s = sub.add_parser("edgehosts", help="list edge hosts (state, health, cluster) in a project")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.add_argument("--wide", action="store_true",
                   help="add hardware columns (cores, memGB, disks, sanDisks, ip, secureBoot) -- "
                        "fetches each host individually if the list response doesn't already "
                        "carry spec.device; sanDisks is a vendor-name heuristic "
                        "(PURE/NETAPP/EMC/HITACHI/IBM), not a protocol check")
    s.add_argument("--min-cores", type=int,
                   help="only show hosts with at least this many CPU cores (client-side filter; "
                        "implies the same per-host hardware fetch as --wide)")
    s.add_argument("--label", help="only show hosts with this metadata label, k=v (client-side filter)")
    s.set_defaults(fn=cmd_edgehosts)

    s = sub.add_parser("edgehost", help="describe one edge host: hardware inventory (cpu, memory, disks, nics, gpus)")
    s.add_argument("ref", help="edge host name or uid")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT); "
                                     "omit to search every project (this is the default with neither set)")
    s.add_argument("--all-projects", action="store_true",
                   help="search every project even if --project/$PALETTE_PROJECT is set")
    s.add_argument("--all-nics", action="store_true",
                   help="show virtual/container NICs too (lxc*, cilium*, veth*, flannel*, cni*, "
                        "docker*, kube-ipvs*, vxlan*, genev*, tunl*, lo are hidden by default)")
    s.add_argument("--json", action="store_true", help="dump the raw GET /v1/edgehosts/{uid} response")
    s.set_defaults(fn=cmd_edgehost)

    s = sub.add_parser("cloudaccounts",
                        help="list cloud accounts (aws, azure, vsphere, maas, ...) visible at tenant or project scope")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.add_argument("--cloud", help="filter by cloud kind (aws, azure, gcp, vsphere, maas, openstack, ...), case-insensitive")
    s.set_defaults(fn=cmd_cloudaccounts)

    s = sub.add_parser("cloudconfig",
                        help="describe a cluster's maas/edge-native cloud config and machine pools (read-only)")
    s.add_argument("ref", help="cluster name or uid (resolves its cloudConfigRef); "
                               "a bare cloudconfig uid also works with --kind")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.add_argument("--kind", choices=["maas", "edge-native"],
                   help="only needed when ref is a bare cloudconfig uid, not a cluster ref")
    s.add_argument("--full", action="store_true", help="untruncated ntp server / host address lists")
    s.set_defaults(fn=cmd_cloudconfig)

    s = sub.add_parser("registries",
                        help="list pack/helm/oci registries, or describe one by name/uid")
    s.add_argument("ref", nargs="?", help="registry name or uid to describe (omit to list)")
    s.add_argument("--kind", choices=["pack", "helm", "oci"], help="filter the list, or scope a describe lookup")
    s.add_argument("--project", help="project name or uid (optional; scope did not change results when probed live)")
    s.set_defaults(fn=cmd_registries)

    s = sub.add_parser("events", help="recent events for a cluster (debugging)")
    s.add_argument("ref", help="cluster name or uid")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--full", action="store_true", help="untruncated messages")
    s.set_defaults(fn=cmd_events)

    s = sub.add_parser("packs", help="search a pack by name across ALL versions (paginated)")
    s.add_argument("name", help="pack metadata.name, e.g. edge-k3s, cni-calico")
    s.add_argument("--project", help="project name or uid (optional; not required by /v1/packs)")
    s.add_argument("--full", action="store_true", help="show every version, not just the newest 12")
    s.set_defaults(fn=cmd_packs)

    s = sub.add_parser("pack-values",
                        help="show a pack's default values YAML and presets (GET .../packs/{uid}?includePackValues=true)")
    s.add_argument("name", help="pack metadata.name, e.g. csi-portworx-generic")
    s.add_argument("version", help="exact pack version, e.g. 3.7.0")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.add_argument("--registry", help="registry uid prefix -- disambiguates when the same "
                                       "name+version exists in more than one registry")
    s.add_argument("--values", action="store_true",
                   help="print only the raw default values YAML to stdout (nothing else -- safe to redirect)")
    s.add_argument("--preset", help="print only this preset's add YAML plus a '# remove:' comment block")
    s.set_defaults(fn=cmd_pack_values)

    # ── verbs added in 0.6.0 (reads first, then writes) ──
    def proj_arg(s, write=False):
        s.add_argument("--project", help="project name or uid" + (
            " -- REQUIRED for writes; $PALETTE_PROJECT is not honored" if write else " (or set PALETTE_PROJECT)"))

    def write_args(s, delete=False):
        s.add_argument("--dry-run", action="store_true",
                       help="do the reads, print the exact method/path/ProjectUid/body of each write, send nothing")
        if delete:
            s.add_argument("--yes", action="store_true", help="required to actually delete")

    s = sub.add_parser("cluster-kubeconfig", help="download a cluster kubeconfig to a mode-600 file "
                                                 "(also: cluster kubeconfig <ref>)")
    s.add_argument("ref", help="cluster name or uid")
    proj_arg(s)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--admin", action="store_true", help="admin kubeconfig (assets/adminKubeconfig)")
    g.add_argument("--client", action="store_true", help="client kubeconfig (assets/kubeconfigclient)")
    s.add_argument("--out", help="file to write (default ./<cluster>.kubeconfig), mode 600")
    s.add_argument("--stdout", action="store_true", help="print to stdout instead of a file (it is a credential)")
    s.set_defaults(fn=cmd_cluster_kubeconfig)

    s = sub.add_parser("cluster-variables", help="a cluster's current profile-variable values "
                                                "(also: cluster variables <ref>); sensitive values masked")
    s.add_argument("ref", help="cluster name or uid")
    proj_arg(s)
    s.set_defaults(fn=cmd_cluster_variables)

    s = sub.add_parser("cluster-rbac", help="a cluster's RBAC bindings (also: cluster rbac <ref>)")
    s.add_argument("ref", help="cluster name or uid")
    proj_arg(s)
    s.set_defaults(fn=cmd_cluster_rbac)

    s = sub.add_parser("cluster-rbac-set", help="WRITE: replace a cluster's RBAC bindings from a JSON file "
                                               "(also: cluster rbac set <ref>)")
    s.add_argument("ref", help="cluster name or uid")
    proj_arg(s, write=True)
    s.add_argument("--file", required=True, help='JSON body {"rbacs":[{"spec":{"bindings":[...]}}]}')
    write_args(s)
    s.set_defaults(fn=cmd_cluster_rbac_set)

    s = sub.add_parser("cluster-create", help="WRITE: create a cluster from a JSON body "
                                             "(also: cluster create <kind> --file ...)")
    s.add_argument("kind", choices=CLUSTER_CREATE_KINDS)
    proj_arg(s, write=True)
    s.add_argument("--file", required=True, help="JSON request body (metadata.name required)")
    write_args(s)
    s.set_defaults(fn=cmd_cluster_create)

    s = sub.add_parser("cluster-profiles-attach", help="WRITE: attach one more profile, additive PATCH "
                                                      "(also: cluster profiles attach <ref>)")
    s.add_argument("ref", help="cluster name or uid")
    proj_arg(s, write=True)
    s.add_argument("--profile", required=True, help="profile name or uid to attach")
    s.add_argument("--version", help="profile version, when the name has several")
    s.add_argument("--var", action="append", help="variable override name=value (repeatable)")
    write_args(s)
    s.set_defaults(fn=cmd_cluster_profiles_attach)

    s = sub.add_parser("cluster-profiles-swap", help="WRITE: swap an attached profile for another version, "
                                                    "re-supplying variables (also: cluster profiles swap <ref>)")
    s.add_argument("ref", help="cluster name or uid")
    proj_arg(s, write=True)
    s.add_argument("--from", dest="from_profile", required=True, help="attached profile uid (or unique name)")
    s.add_argument("--to", dest="to_profile", required=True, help="new profile uid or name")
    s.add_argument("--to-version", help="version of --to, when the name has several")
    s.add_argument("--var", action="append", help="variable value name=value for the new profile (repeatable)")
    s.add_argument("--allow-sensitive-reset", action="store_true",
                   help="proceed even though masked sensitive variables will reset to defaults")
    write_args(s)
    s.set_defaults(fn=cmd_cluster_profiles_swap)

    s = sub.add_parser("cluster-delete", help="WRITE: delete a cluster in --project (also: cluster delete <ref>)")
    s.add_argument("ref", help="cluster name or uid")
    proj_arg(s, write=True)
    s.add_argument("--force", action="store_true", help="forceDelete=true; only accepted once the cluster is Deleting")
    write_args(s, delete=True)
    s.set_defaults(fn=cmd_cluster_delete)

    s = sub.add_parser("profile-create", help="WRITE: create a cluster profile from a JSON body "
                                             "(also: profile create --file ...)")
    proj_arg(s, write=True)
    s.add_argument("--file", required=True, help="JSON body: metadata.name, spec.version, spec.template, "
                                                 "optional spec.variables")
    s.add_argument("--publish", action="store_true", help="publish after create (default: leave a draft)")
    write_args(s)
    s.set_defaults(fn=cmd_profile_create)

    s = sub.add_parser("profile-version", help="WRITE: new version of a profile via server-side clone; never "
                                              "edits the source (also: profile version <ref> --version X)")
    s.add_argument("ref", nargs="?", help="source profile name or uid (or --from)")
    s.add_argument("--from", dest="from_ref", help="source profile uid or name")
    s.add_argument("--from-version", help="source version, when the name has several")
    s.add_argument("--version", required=True, help="the new version string, e.g. 1.2.1")
    s.add_argument("--set-pack-values", action="append",
                   help="pack=values.yaml -- replace that pack's values in the NEW version (repeatable; "
                        "use the full values file, partial values fail validation)")
    s.add_argument("--no-publish", action="store_true",
                   help="leave --set-pack-values edits in the draft (default: publish them; a clone itself "
                        "comes back published)")
    proj_arg(s, write=True)
    write_args(s)
    s.set_defaults(fn=cmd_profile_version)

    s = sub.add_parser("profile-variables-set", help="WRITE: change a profile's declared variables "
                                                    "(also: profile variables set <ref>)")
    s.add_argument("ref", help="profile name or uid")
    s.add_argument("--version", help="profile version, when the name has several")
    proj_arg(s, write=True)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--file", help='{"variables":[...]} -- replaces the whole list')
    g.add_argument("--set", action="append", help="name=default for an existing variable (repeatable)")
    s.add_argument("--force", action="store_true", help="try even when a cluster uses this version")
    write_args(s)
    s.set_defaults(fn=cmd_profile_variables_set)

    s = sub.add_parser("profile-publish", help="WRITE: publish a draft profile (also: profile publish <ref>)")
    s.add_argument("ref", help="profile name or uid")
    s.add_argument("--version", help="profile version, when the name has several")
    proj_arg(s, write=True)
    write_args(s)
    s.set_defaults(fn=cmd_profile_publish)

    s = sub.add_parser("profile-delete", help="WRITE: delete one profile version that no cluster uses "
                                             "(also: profile delete <ref>)")
    s.add_argument("ref", help="profile name or uid")
    s.add_argument("--version", help="profile version, when the name has several")
    proj_arg(s, write=True)
    write_args(s, delete=True)
    s.set_defaults(fn=cmd_profile_delete)

    s = sub.add_parser("profile-export", help="export a profile as JSON, manifests inline "
                                             "(also: profile export <ref>)")
    s.add_argument("ref", help="profile name or uid")
    s.add_argument("--version", help="profile version, when the name has several")
    proj_arg(s)
    s.add_argument("--out", help="write to this file instead of stdout")
    s.set_defaults(fn=cmd_profile_export)

    s = sub.add_parser("pcgs", help="list private cloud gateways (overlords): state, health, version, heartbeat")
    proj_arg(s)
    s.set_defaults(fn=cmd_pcgs)

    s = sub.add_parser("pcg", help="describe one PCG, with non-True health conditions")
    s.add_argument("ref", help="PCG name or uid")
    proj_arg(s)
    s.add_argument("--full", action="store_true", help="all conditions")
    s.set_defaults(fn=cmd_pcg)

    s = sub.add_parser("cloudaccount", help="describe one cloud account (credentials masked)")
    s.add_argument("ref", help="account name or uid")
    proj_arg(s)
    s.add_argument("--cloud", help="restrict lookup to one kind (maas, aws, ...)")
    s.add_argument("--json", action="store_true", help="the full document, secrets masked")
    s.set_defaults(fn=cmd_cloudaccount)

    s = sub.add_parser("cloudaccount-create", help="WRITE: create a MAAS cloud account "
                                                  "(also: cloudaccount create maas ...)")
    s.add_argument("cloud", choices=["maas"])
    proj_arg(s, write=True)
    s.add_argument("--file", help="full JSON body instead of the flags below")
    s.add_argument("--name")
    s.add_argument("--endpoint", help="MAAS API endpoint, e.g. http://10.0.0.2:5240/MAAS")
    s.add_argument("--api-key-env", help="env var holding the MAAS API key (never pass it on argv)")
    s.add_argument("--api-key-file", help="file holding the MAAS API key")
    s.add_argument("--pcg", help="PCG name or uid (privateGateway)")
    write_args(s)
    s.set_defaults(fn=cmd_cloudaccount_create)

    s = sub.add_parser("clustergroups", help="list cluster groups in a project")
    proj_arg(s)
    s.set_defaults(fn=cmd_clustergroups)

    s = sub.add_parser("clustergroup", help="describe one cluster group: host clusters, limits")
    s.add_argument("ref", help="cluster group name or uid")
    proj_arg(s)
    s.set_defaults(fn=cmd_clustergroup)

    s = sub.add_parser("vclusters", help="list virtual clusters in a project")
    proj_arg(s)
    s.set_defaults(fn=cmd_vclusters)

    s = sub.add_parser("edgehost-token-create", help="WRITE: create a tenant-scoped edge host registration "
                                                    "token, written to a mode-600 file")
    s.add_argument("name")
    s.add_argument("--expiry", help="ISO-8601 expiry (optional)")
    s.add_argument("--out", help="token file (default ./<name>.token)")
    s.add_argument("--stdout", action="store_true", help="print the token instead of writing a file")
    write_args(s)
    s.set_defaults(fn=cmd_edgehost_token_create)

    s = sub.add_parser("doctor", help="check whether 1Password and the Palette API are usable")
    s.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    s.set_defaults(fn=cmd_doctor)

    a = p.parse_args(rewrite_argv(sys.argv[1:]))
    if getattr(a, "fn", None) is cmd_profile_version:
        a.ref = a.from_ref or a.ref
        if not a.ref:
            p.error("profile version needs a source: <ref> or --from <uid>")
    if not a.cmd:
        p.print_help()
        sys.exit(E_USAGE)
    try:
        a.fn(a)
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
