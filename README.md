# palette-axi

Agent-ergonomic CLI over the Spectro Cloud Palette API. Sibling to
[`opp-axi`](../customer-opportunities/automation/opp-axi) — same AXI (Agent
eXperience Interface) conventions: TOON output, truncation with `--full`
escape hatches, pre-computed aggregates, definitive empty states, structured
exit codes, content-first output, next-step disclosure.

**Why this exists**: a transcript-mining pass found 599 Bash calls across 7
sessions hand-rolling `curl` + `jq` against `api.spectrocloud.com` (811
endpoint references), plus 532 `op read`/`op item` calls with inconsistent
vault casing. The auth flow, ProjectUid resolution, and pagination gotchas
were documented in prose across the `spectrocloud-*` skills but weren't
executable. This makes them executable.

**READ-ONLY.** No verb creates, updates, deletes, or deploys anything. See
[Future work](#future-work-not-implemented) for the write case that was
deliberately left out.

## Install

The release artifact is a single-file zipapp. It needs `python3 >= 3.10` on the
target; it is not a static binary.

```sh
curl -fsSL https://raw.githubusercontent.com/craig-ai-tooling/palette-axi/main/scripts/install.sh | bash
```

or by hand:

```sh
curl -fsSL https://github.com/craig-ai-tooling/palette-axi/releases/latest/download/palette-axi.pyz \
  -o ~/.local/bin/palette-axi
chmod +x ~/.local/bin/palette-axi
```

The bundled installer honours `$BIN` for the target path.

Then: `palette-axi doctor` — it says exactly what still needs configuring.

`palette-axi.pyz` is a zipapp — it needs a Python 3.10+ interpreter on the target (every
lab box has one), not a compiled binary.

### From source

```sh
git clone <this repo>
python3 -m palette_axi --help     # run straight from a checkout, or:
make build                        # writes dist/palette-axi.pyz
```

## Configure

1. `PALETTE_API_KEY` env var wins if set — skips 1Password entirely.
2. Otherwise the key comes from 1Password, vault `Lobster` (or
   `$PALETTE_AXI_VAULT`), tenant `custeng-prod` by default (or `--tenant NAME`
   / `$PALETTE_AXI_TENANT`).

| Env | Default | Required? |
|---|---|---|
| `PALETTE_API_KEY` | (none) | No — skips 1Password entirely when set |
| `PALETTE_AXI_TENANT` | `custeng-prod` | No |
| `PALETTE_AXI_VAULT` | `Lobster` | No |
| `PALETTE_AXI_OP_ITEM` | (none) | No — skips tenant→item-id lookup when set |
| `PALETTE_AXI_KEY_TTL` | `900` | No — seconds the resolved key is cached; `0` disables caching |
| `PALETTE_PROJECT` | (none) | No — most verbs also accept `--project` |

`palette-axi doctor` tells you what is still missing.

### Key cache

Every verb used to cost **two** 1Password calls (`op item list` to resolve
the tenant's item id, then `op item get` for the secret) even though the
mapping and the key rarely change between calls. On 9/21/26 the shared
1Password service account started rate-limiting reads
(`Too many requests. Your client has been rate-limited.`), and an agent
running a few dozen verbs in a row burned through the limit fast.

`palette-axi` now caches both under `$XDG_RUNTIME_DIR/palette-axi/`
(tmpfs, per-user, gone at logout — never a persistent path like `~/.cache`):

- The resolved **API key**, for `$PALETTE_AXI_KEY_TTL` seconds (default 900;
  `0` disables caching entirely). Cache key is the tenant name plus
  `$PALETTE_AXI_OP_ITEM` when set, so different overrides never collide.
- The **tenant → 1Password item id** mapping (not a secret), for 24h. This
  means even a key-cache miss costs one `op` call, not two.

If `$XDG_RUNTIME_DIR` is unset or not a writable directory, caching is
disabled outright — `palette-axi` never falls back to a persistent location
for a secret. If the Palette API answers `401` to a request made with a
cached key, that cache file is deleted before the command exits, so the next
run re-reads 1Password instead of retrying a stale key forever.

`palette-axi doctor` shows the cache state for the active tenant (`hit (age
Ns)`, `miss`, or `disabled (<why>)`) in its config table — it never prints
the key itself.

### The op item-ID gotcha

**1Password items titled `Palette API Key (<tenant>)` contain literal
parentheses in the title.** `op://Lobster/Palette API Key (acme)/password`
breaks — `op` does not parse parenthesized titles reliably in an `op://`
reference. `palette-axi` never builds one. Instead it:

1. Runs `op item list --vault Lobster --format json` and matches the title.
2. Takes that item's **ID** (a flat alphanumeric string, e.g.
   `abcdefghijklmnopqrstuvwxyz`) and calls `op item get <ID> --vault Lobster`.

A service account token also requires an **explicit `--vault`** — every `op`
call in this tool passes one; there is no bare `op read`/`op item get` call
anywhere in the source.

If you already know the item ID, skip the lookup call entirely:
`PALETTE_AXI_OP_ITEM=<id> palette-axi projects`.

The secret field on these items is inconsistently labeled `password` or
`credential` across tenants (hand-created by different people over time) —
`palette-axi` checks both rather than assuming one. **No verb ever prints a
key value**; on 1Password failure the error names the item ID and vault, not
the secret.

## Usage

Every project-scoped verb takes `--project <name-or-uid>` (or
`$PALETTE_PROJECT`). A name is resolved via `GET /v1/projects`
(case-insensitive substring match). Ambiguous or missing matches are a hard
error listing every candidate — this tool never guesses which project you
meant, matching the `spectrocloud-common` skill's "ALWAYS ASK" rule, just
enforced as a CLI contract instead of a reminder to the agent.

```
palette-axi projects
palette-axi clusters --project SA-Craig-Smith
palette-axi clusters --project SA-Craig-Smith --edge      # edge-native only
palette-axi cluster rpi-inference --project SA-Craig-Smith
palette-axi cluster rpi-inference --project SA-Craig-Smith --full   # all conditions, not just non-True
palette-axi profiles --project SA-Craig-Smith
palette-axi profile craig-nvidia --project SA-Craig-Smith
palette-axi edgehosts --project SA-Craig-Smith
palette-axi edgehosts --project SA-Craig-Smith --wide       # + cores, memGB, disks, sanDisks, ip, secureBoot
palette-axi edgehosts --project SA-Craig-Smith --min-cores 64
palette-axi edgehosts --project SA-Craig-Smith --label site=dal-dc1
palette-axi edgehost ucs-blade-01 --project SA-Craig-Smith  # one host's hardware inventory
palette-axi edgehost 693064bc882df8800821e001               # no --project: searches every project
palette-axi edgehost ucs-blade-01 --all-nics                # include lxc*/cilium*/veth*/... noise
palette-axi cloudaccounts --project SA-Craig-Smith --cloud aws  # cloud accounts visible to that project
palette-axi cloudconfig rpi-inference --project SA-Craig-Smith # cluster's cloud config + machine pools
palette-axi events rpi-inference --project SA-Craig-Smith --limit 100
palette-axi packs edge-k3s                                # tenant-wide, no --project needed
palette-axi packs cni-calico --full                        # every version, not just newest 12
palette-axi registries                                    # all pack/helm/oci registries, tenant-wide
palette-axi registries --kind oci                          # one kind only
palette-axi registries "Public Repo"                       # describe one by name or uid
```

## Verbs and the evidence behind each

| Verb | Endpoint(s) actually hit | Why it's here |
|---|---|---|
| `projects` | `GET /v1/projects` | Step 1 of every real session (47 combined refs); every other verb needs a ProjectUid resolved from a name. |
| `clusters` | `POST /v1/dashboard/spectroclusters/search` | Plain `GET /v1/spectroclusters` list carries **no `status.health` at all** (confirmed live) — every session that wanted health went to this search endpoint instead. It's a POST because it takes a filter body, not because it mutates anything. |
| `cluster` | `GET /v1/spectroclusters/{uid}` + `GET /v1/dashboard/spectroclusters/{uid}/overview` | Same health gap as above at the single-resource level; conditions come from the overview surface per the `spectrocloud-troubleshooting` skill's own debugging workflow. |
| `profiles` | `GET /v1/clusterprofiles` | 111+ direct refs; profile discovery precedes almost every profile-editing session. |
| `profile` | `GET /v1/clusterprofiles/{uid}` | Layer/pack shape (`spec.published.packs[].tag`) confirmed against a real transcript that was diffing two profile versions. |
| `edgehosts` | `GET /v1/edgehosts` | 40+ refs; the field shapes match all three skills' documented `jq` filters verbatim. `--wide` adds hardware columns (cores, memGB, disks, sanDisks, ip, secureBoot) from `spec.device`; `--min-cores`/`--label` filter client-side. Default output (no `--wide`) is unchanged. |
| `edgehost` | `GET /v1/edgehosts/{uid}` | An SE with a UID from a ticket or a name from a customer often doesn't have the project. Resolves the ref (uid/name/substring) against the same edge host search `edgehosts` uses, scoped to `--project` if given, else walks every project and reports which one matched. Prints the full hardware inventory: cpu/memory, OS, disks + partitions, NICs (virtual/container interfaces like `lxc*`/`cilium*`/`veth*`/`flannel*`/`cni*`/`docker*`/`kube-ipvs*`/`vxlan*`/`genev*`/`tunl*`/`lo` hidden by default, `--all-nics` to show them), gpu count. See [Real API behavior](#real-api-behavior-discovered-while-building-this-not-documented-anywhere) below. |
| `cloudaccounts` | `GET /v1/cloudaccounts/summary` | Lists every cloud type (aws, azure, gcp, vsphere, maas, openstack, ...) in one call. Never hits a per-cloud endpoint — see [Real API behavior](#real-api-behavior-discovered-while-building-this-not-documented-anywhere) below for why. |
| `cloudconfig` | `GET /v1/spectroclusters/{uid}` + `GET /v1/cloudconfigs/{kind}/{uid}` | 23 combined transcript refs, 13.0% failing, zero prior coverage. A cluster's `spec.cloudConfigRef` names the exact `{kind, uid}` to GET next — there is no tenant-wide cloudconfig list, so the cluster is the index. Read-only: the write calls this scout doc also saw (`PUT clusterConfig`, `PUT machinePools/...`) are deliberately out of scope. |
| `events` | `GET /v1/events/components/spectrocluster/{uid}` | **Not** `/v1/spectroclusters/{uid}/events` or `.../status/events` — both of those 404 or 422 in practice. A real session in the transcripts probed six candidate endpoints live and found this one; that probe's exact result is what this verb uses. |
| `packs` | `GET /v1/packs?filters=metadata.name=...` (fully paginated) | 127 direct refs, called out as "MANDATORY"/"CRITICAL" pagination in three separate skills because the endpoint silently caps at 50 results per page. This verb pages it exhaustively and returns versions newest-first with the true latest marked, instead of every session re-deriving the same offset-loop-plus-sort `jq` pipeline by hand. |
| `registries` | `GET /v1/registries/metadata` (list) + `GET /v1/registries/{pack,helm,oci}/{uid}` (describe) | 43 combined transcript refs, 30.2% failing — the worst fail rate of any bucket with double-digit volume, and zero prior coverage. `metadata` is the one endpoint that actually lists all three kinds; `GET /v1/registries/oci` itself 405s (`Allow: DELETE`). See [Real API behavior](#real-api-behavior-discovered-while-building-this-not-documented-anywhere) below. |
| `doctor` | Same read the `projects` verb uses, `limit=1` | Not a Palette data verb — checks whether 1Password and the Palette API are actually usable before you run one of the above. See [Configure](#configure). |

Full profile/cluster/cloudconfig **create-or-update** flows were left out —
the task scope is the verbs actually used for **inspection**; write flows
(build a profile, then create a cluster, then tune its cloud config) are
explicitly out of scope for v1.

## Real API behavior discovered while building this (not documented anywhere)

These came from running the tool live against `custeng-prod`, not from the
skills:

- **`GET /v1/projects` ignores `limit` and `offset` entirely.** It always
  returns the same first 50 rows no matter what you pass, even though
  `listmeta.count` correctly reports a larger total (65 in the tenant tested).
  There is no working pagination path for this endpoint today.
- **`GET /v1/edgehosts` returns no `listmeta` at all.**
- **Pack objects carry `"status": null`**, not an omitted key — code that
  does `pack.get("status", {})` still crashes on `None.get(...)`.
  `palette-axi` uses a `dget()` helper everywhere instead of raw `.get(key, {})`.
- **`GET /v1/clusterprofiles` pages correctly via `listmeta.continue`**, but
  still lost 2 of 146 rows in a live paginate-to-exhaustion test — the
  endpoint's own pagination isn't fully reliable, independent of how the
  client walks it.
- **Cluster health uses `"Healthy"`/`"Unhealthy"`; edge host health uses
  lowercase `"healthy"`/`"unhealthy"`** — same concept, different casing
  convention on two sibling endpoints.
- **Edge host cluster attachment lives in `status.inUseClusters[]`**, not a
  `clusterUid` field (that field does not exist on this resource).
- **(9/14/26) `GET /v1/cloudaccounts/summary` returns every cloud type in one
  call** (`kind`: `aws`, `azure`, `gcp`, `vsphere`, `maas`, `openstack`, ...)
  and its `specSummary` is **empty — no credentials**. The per-cloud
  endpoints (`GET /v1/cloudaccounts/{aws,azure,gcp,vsphere,maas}`) return
  `spec.secretKey` / `spec.secretToken` / `spec.sts.externalId` — real
  credentials — so `cloudaccounts` never calls one of those.
- **(9/14/26) `GET /v1/cloudaccounts/openstack` 404s**, even though the
  summary endpoint lists openstack accounts fine — there is no working
  per-cloud endpoint for every kind summary reports, another reason to stay
  on summary-only.
- **(9/14/26) Summary ignores query-param filtering entirely.**
  `cloudType=aws` is silently ignored (still returns all 20 rows in a
  20-account tenant) and `filters=spec.cloudType=aws` returns **zero** rows.
  `cloudaccounts --cloud` filters client-side on `kind` instead of trusting
  either query param.
- **(9/14/26) `ProjectUid` changes scope, not row shape.** Without it,
  `/v1/cloudaccounts/summary` returns only tenant-owned accounts (20 in
  `custeng-prod`); with a `ProjectUid` header it returns that project's own
  account(s) plus the tenant accounts shared into it (14 rows for one
  project tested) — some tenant accounts (`scopeVisibility "4"`) never show
  up in project scope at all.
- **(9/15/26) `GET /v1/registries/oci` 405s** (`Allow: DELETE`) — there is no
  working GET list endpoint for oci registries at all, unlike pack and helm.
  `GET /v1/registries/metadata` is the one endpoint that lists all three
  kinds (105 rows in `custeng-prod`: 7 pack, 75 helm, 23 oci) in a single
  unpaginated call, and it silently ignores both `kind=` and `limit=` query
  params — confirmed by requesting `limit=5` and `kind=oci` and getting all
  105 rows back either way. Its per-kind subtotal matches `list_all`'s own
  `listmeta.count` exactly for pack and helm, the two kinds with an
  independent paginated endpoint to check it against.
- **(9/15/26) `GET /v1/registries/oci/{uid}` returns a FLAT spec-only
  object** — no `kind`/`metadata`/`status` wrapper, unlike `pack`/`helm`
  describe which return the full resource. `registries` merges in
  name/uid/isDefault/isPrivate from the `metadata` list for oci rows since
  the describe call itself doesn't carry them.
- **(9/15/26) `auth.password` / `auth.token` come back as the literal string
  `"********"`** on every registry kind (pack, helm, oci) — masked
  server-side. Unlike `cloudaccounts`, there is no per-kind endpoint this
  verb has to avoid for secrecy.
- **(9/15/26) A cluster's `spec.cloudConfigRef`** (`GET
  /v1/spectroclusters/{uid}`) is `{"kind": ..., "name": ..., "uid": ...}` —
  the cloudconfig uid is different from the cluster uid, and its `kind`
  names the exact `/v1/cloudconfigs/{kind}/{uid}` collection to call next.
  Confirmed for `edge-native` (rpi-inference, project SA-Craig-Smith) and
  `eks` (hf-connect-eks-demo, same project) — this field is not
  edge-native/maas-specific, every cloud type appears to carry one.
  `maas` itself was not independently exercised live (no maas cluster was
  reachable in the ~9 projects checked this session); `cloudconfig`'s code
  path is generic on `cloudConfigRef.kind`, so it calls
  `GET /v1/cloudconfigs/maas/{uid}` by construction, not a maas-specific
  branch, but that exact path has not itself returned a live response yet.
- **(9/15/26) `GET /v1/cloudconfigs/{kind}/{uid}` returns the FULL document
  in one call** — `spec.clusterConfig` (control-plane endpoint, NTP servers,
  overlay CIDR) and `spec.machinePoolConfig` (every pool, with its host
  list) both come back inline. No separate machine-pools GET is needed to
  see pool/host data; the `machinePools/{pool}` and
  `machinePools/{uid}/machines` paths the curl-gaps scout saw were `PUT`
  targets for partial updates, which `cloudconfig` never calls (read-only).

- **`GET /v1/edgehosts/{uid}` carries the device inventory at `spec.device`** —
  `archType`, `cpu.cores`, `memory.sizeInMB`, `secureBoot`, `hostType`,
  `hostState`, `os{family,version,kernelVersion}`, `disks[]{controller,size,
  vendor,partitions[]}`, `nics[]{nicName,macAddr,ip,subnet,gateway,dns[],
  isDefault}`, `gpus[]`. `spec.host{hostAddress,macAddress,hostUid}` is a
  sibling of `spec.device`, not nested under it. This is the shape the
  `edgehosts` list has never surfaced (name/uid/state/health/cluster only);
  `edgehost` and `edgehosts --wide` are the verbs that read it. A real NIC
  list carries container/CNI plumbing alongside the physical/bond/vlan
  interfaces (one live host: 28 NICs, ~4 physical) — `edgehost` hides
  `lxc*`/`cilium*`/`veth*`/`flannel*`/`cni*`/`docker*`/`kube-ipvs*`/`vxlan*`/
  `genev*`/`tunl*`/`lo` by default. `edgehosts --wide`'s `sanDisks` is a
  disk-vendor heuristic (`PURE`/`NETAPP`/`EMC`/`HITACHI`/`IBM`), not a real
  SAN/LUN protocol check. **Not independently probed live this session** —
  the 1Password service account's read limit was exhausted (see the Key
  cache section above); the shape above comes from a previously saved real
  response, and this build's own request/response handling for `edgehost`
  and `edgehosts --wide` is offline-tested only. Whether the edgehosts
  *search* response (`EDGEHOSTS_PATH`, what `edgehosts` and `edgehost`'s
  resolution step both call) already carries `spec.device` inline, or only
  the metadata/status shape `edgehosts` has always rendered, is also
  unverified live — `--wide`'s per-host fallback GET exists for the case
  where it doesn't; see `_wide_fields()`'s docstring in `cli.py`.

Every list verb that can detect this (via `listmeta.count`) prints a `note:`
line naming exactly how many rows it got vs. how many the API claims exist,
rather than silently showing a partial list as if it were complete.

## Exit codes

Matches `opp-axi`'s contract so both tools compose in the same agent loop:

| Code | Name | Meaning |
|---|---|---|
| 0 | `E_OK` | success |
| 1 | `E_ERR` | API/network/op error, or anything not covered below |
| 2 | `E_USAGE` | bad args, or a name that's ambiguous and needs disambiguating |
| 3 | `E_NOTFOUND` | no such project/cluster/profile/edge host/pack/tenant/registry/cloudconfig |
| 4 | `E_REFUSED` | reserved for policy refusals — v1 has no write path to refuse, kept for symmetry with `opp-axi` and for any future verb that needs to say no |

## Env overrides

| Var | Default | Purpose |
|---|---|---|
| `PALETTE_API_KEY` | (none) | Skip 1Password entirely |
| `PALETTE_PROJECT` | (none) | Default `--project` |
| `PALETTE_AXI_TENANT` | `custeng-prod` | Default `--tenant` |
| `PALETTE_AXI_VAULT` | `Lobster` | 1Password vault |
| `PALETTE_AXI_OP_ITEM` | (none) | Skip tenant→item-id lookup, use this op item id directly |
| `PALETTE_AXI_KEY_TTL` | `900` | Seconds the resolved API key is cached in `$XDG_RUNTIME_DIR/palette-axi`; `0` disables caching |

## Future work (not implemented)

**Write verbs** — create/update a cluster profile, create a cluster, delete a
cluster, create an edge host registration token. All heavily represented in
the transcripts (profile/cluster create flows are most of the write-side
599 calls this tool's evidence came from) but explicitly out of scope for v1
per the build brief: this tool is read-only. If it's built later, it should
keep the same disambiguation-before-action contract as `resolve_project` /
`resolve_by_name` here (never act on a name match without confirming it's
unique), and should very likely require a `--yes`/confirmation flag before
any DELETE, matching the caution the `spectrocloud-clusters` skill already
uses for teardown flows.

## Develop

```sh
python3 -m unittest discover -s tests   # tests (offline; network is stubbed)
ruff check .                            # lint
python3 -m compileall palette_axi       # compile check
make build                              # dist/palette-axi.pyz
```

A `v*` tag builds the `.pyz` and publishes it to a GitHub Release via
`.github/workflows/release.yml` — that release is what [Install](#install) pulls.

See [AGENTS.md](AGENTS.md) for the full contract, including the **no-mistakes**
mode this repo runs in.
