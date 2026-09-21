#!/usr/bin/env python3
"""palette-axi — agent-ergonomic CLI over the Spectro Cloud Palette API.

Built on the same AXI (Agent eXperience Interface) principles as opp-axi:
TOON output, minimal default schemas, truncation with --full escape hatches,
pre-computed aggregates, definitive empty states, structured exit codes,
content-first output, and next-step disclosure.

READ-ONLY. No verb in this tool creates, updates, deletes, or deploys
anything against Palette. See README.md "Future work" for the write case
that was deliberately left out. `doctor` is the one exception to "no verb
prints anything but data" in spirit only — it still never mutates Palette;
it just probes whether the 1Password and Palette API connectors are usable.

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

# Exit codes — mirrors opp-axi's contract so both tools compose in the same
# pipeline/agent loop without the caller needing two different tables.
#   0 E_OK        success
#   1 E_ERR       API/network/op error, or any failure not covered below
#   2 E_USAGE     bad args, or an ambiguous name that needs disambiguating
#   3 E_NOTFOUND  no such project/cluster/profile/edge host/pack
#   4 E_REFUSED   refused on policy grounds (reserved — v1 has no write path
#                 to refuse, kept for symmetry and for any future verb that
#                 needs to say no)
E_OK, E_ERR, E_USAGE, E_NOTFOUND, E_REFUSED = 0, 1, 2, 3, 4


# ── output (copied verbatim from opp-axi — both tools must agree) ──────────
def _tv(v):
    """TOON scalar: quote only when it would break the row.

    None and False are DIFFERENT facts and must not share a cell. Collapsing
    False into "" made "POV not required" read identically to "nobody knows",
    which is exactly the kind of silent wrong answer this tool exists to stop.
    Empty means unknown; false means known-false."""
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    s = str(v).replace("\r", "")
    if any(c in s for c in ',"\n'):
        return '"' + s.replace('"', '""').replace("\n", "\\n") + '"'
    return s


def toon(name, fields, rows, indent="  "):
    """TOON block. rows==[] yields a definitive `name[0]{...}: (none)` — never ambiguous."""
    head = f"{name}[{len(rows)}]{{{','.join(fields)}}}:"
    if not rows:
        return head + " (none)"
    return "\n".join([head] + [indent + ",".join(_tv(r.get(f)) for f in fields) for r in rows])


def emit(*parts):
    print("\n".join(p for p in parts if p))


def nxt(*suggestions):
    """Contextual disclosure — what to run next, not a wall of help text."""
    return "\nnext: " + " | ".join(suggestions) if suggestions else ""


def die(msg, code=E_ERR):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def size_hint(full, shown):
    extra = len(full) - len(shown)
    return f"  [+{extra}B truncated, --full]" if extra > 0 else ""


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
def api(method, path, api_key, project=None, params=None, json_body=None, timeout=30):
    url = f"{API_BASE}{path}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    headers = {"ApiKey": api_key, "Accept": "application/json"}
    if project:
        headers["ProjectUid"] = project
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read()
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


def list_all(path, api_key, project=None, filters=None, page_limit=50, max_pages=10,
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


def list_clusters(api_key, project, page_limit=50, max_pages=10):
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

    conds = ost.get("conditions") or []
    show = conds if a.full else [c2 for c2 in conds if c2.get("status") != "True"]
    crows = [{"type": c2.get("type"), "status": c2.get("status"),
              "reason": c2.get("reason"), "message": trunc(c2.get("message"), 100)} for c2 in show]
    emit(toon("conditions", ["type", "status", "reason", "message"], crows))
    emit(nxt(f"palette-axi events {m.get('name')} --project {proj}",
             f"palette-axi cluster {m.get('name')} --project {proj} --full") if not a.full
         else nxt(f"palette-axi events {m.get('name')} --project {proj}"))


def cmd_profiles(a):
    key = get_api_key(a.tenant)
    proj = resolve_project(a.project, key)
    items = list_all("/v1/clusterprofiles", key, proj)
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
    items = list_all("/v1/clusterprofiles", key, proj)
    hit = resolve_by_name(a.ref, items, "profile")
    uid = hit["metadata"]["uid"]
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

    def vkey(p):
        parts = re.split(r"[.\-]", dget(p, "spec").get("version") or "0")
        return tuple(int(x) if x.isdigit() else 0 for x in parts)

    active.sort(key=vkey, reverse=True)
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


# ── cli ────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(prog="palette-axi", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default=DEFAULT_TENANT,
                    help=f"1Password 'Palette API Key (<tenant>)' item to use (default: {DEFAULT_TENANT})")
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
    s.set_defaults(fn=cmd_cluster)

    s = sub.add_parser("profiles", help="list cluster profiles in a project")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.set_defaults(fn=cmd_profiles)

    s = sub.add_parser("profile", help="describe one cluster profile: layers/packs")
    s.add_argument("ref", help="profile name or uid")
    s.add_argument("--project", help="project name or uid (or set PALETTE_PROJECT)")
    s.add_argument("--full", action="store_true", help="untruncated registry uids")
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

    s = sub.add_parser("doctor", help="check whether 1Password and the Palette API are usable")
    s.add_argument("--json", action="store_true", help="emit JSON instead of TOON")
    s.set_defaults(fn=cmd_doctor)

    a = p.parse_args()
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
