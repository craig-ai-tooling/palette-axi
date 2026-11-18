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

**Reads and writes (since 0.6.0).** Write verbs cover the profile and cluster
flows agents were still hand-rolling with `curl`/`urllib` (see
[Write verbs](#write-verbs-and-the-safety-model)). Every write supports
`--dry-run`, needs an explicit `--project`, and refuses to touch anything
outside that project.

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
palette-axi cluster rpi-inference --project SA-Craig-Smith --profiles  # + attached profiles/pack tags
palette-axi profiles --project SA-Craig-Smith
palette-axi profile craig-nvidia --project SA-Craig-Smith
palette-axi profile craig-nvidia --project SA-Craig-Smith --variables  # + declared variables
palette-axi pack-values csi-portworx-generic 3.7.0 --project SA-Craig-Smith          # summary + presets
palette-axi pack-values csi-portworx-generic 3.7.0 --project SA-Craig-Smith --registry 64eaff45  # disambiguate a pack in >1 registry
palette-axi pack-values csi-portworx-generic 3.7.0 --project SA-Craig-Smith --values > values.yaml  # raw default values only
palette-axi pack-values csi-portworx-generic 3.7.0 --project SA-Craig-Smith --preset px-normal-mode  # one preset's add/remove
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

# 0.6.0 reads
palette-axi cluster kubeconfig rpi-inference --project SA-Craig-Smith           # ./rpi-inference.kubeconfig, mode 600
palette-axi cluster kubeconfig rpi-inference --project SA-Craig-Smith --admin --out ~/.kube/rpi.yaml
palette-axi cluster variables dane-first-cluster --project SA-Craig-Smith      # current values per profile
palette-axi cluster rbac rpi-inference --project SA-Craig-Smith
palette-axi cluster events rpi-inference --project SA-Craig-Smith               # same as `events`
palette-axi pcgs                                                                 # PCGs: state, health, version, heartbeat
palette-axi pcg custeng-maas-sbx-pcg-1
palette-axi cloudaccount smc-maas-01 --project SA-Craig-Smith                   # credentials masked
palette-axi clustergroups --project SA-Craig-Smith ; palette-axi clustergroup <name|uid> --project ...
palette-axi vclusters --project SA-Craig-Smith
palette-axi profile export craig-nvidia --version 25.3.0 --project SA-Craig-Smith --out craig-nvidia.json
```

### Write verbs and the safety model

```
palette-axi profile create --file body.json --project P [--publish]
palette-axi profile version <ref>|--from <uid> --version 1.2.1 --project P [--set-pack-values edge-k8s=values.yaml ...] [--no-publish]
palette-axi profile variables set <ref> --project P (--file vars.json | --set name=default ...)
palette-axi profile publish <ref> [--version X] --project P
palette-axi profile delete <ref> [--version X] --project P --yes
palette-axi cluster create edge-native|maas|virtual --file body.json --project P
palette-axi cluster profiles attach <cluster> --profile <ref> [--version X] [--var n=v ...] --project P
palette-axi cluster profiles swap <cluster> --from <uid> --to <ref> [--to-version X] [--var n=v ...] --project P
palette-axi cluster rbac set <cluster> --file rbacs.json --project P
palette-axi cluster delete <cluster> --project P --yes [--force]
palette-axi cloudaccount create maas --project P --name N --endpoint URL --api-key-env VAR [--pcg <ref>]
palette-axi edgehost token create <name> [--out FILE | --stdout]     # tenant-scoped
```

The two-word forms (`profile create`, `cluster profiles swap`, ...) are rewritten
to hyphenated verbs (`profile-create`, `cluster-profiles-swap`) before parsing,
so `profile <ref>` and `cluster <ref>` behave exactly as before. A profile or
cluster literally named `create`/`delete`/... has to be addressed by uid.

Safety rules, enforced in code and covered by `tests/test_write_verbs.py`:

| Rule | How |
|---|---|
| Project is explicit | Every write needs `--project <name|uid>` on the command line. `$PALETTE_PROJECT` is **not** honored for writes, so a stale env var can't pick where a create or delete lands. Edge host tokens are the one tenant-scoped write (the API rejects a `ProjectUid` header there). |
| `--dry-run` | Runs the reads needed to build the request, then prints every write it would send (method, path with query, ProjectUid, JSON body) and sends none. Secret-shaped keys (`apiKey`, `password`, `token`, ...) print as `********`. |
| Never outside the project | Before any edit or delete, the target's `metadata.annotations` must say `scope=project` and `projectUid=<--project>`; otherwise exit 4. Tenant-scoped objects are visible from a project and are never changed from one. |
| Deletes need `--yes` | Without it, exit 4 naming exactly what would be deleted. `profile delete` also refuses a version any cluster uses. |
| `cluster delete --force` | Only when the cluster is already `Deleting`: Palette refuses `forceDelete=true` otherwise (and for ~15 min after an aborted delete). |
| Profiles get new versions | `profile version` clones server-side; the source version is never edited. `profile variables set` refuses an in-use version (Palette would answer `VariableUpdateFailure`) and points at `profile version`. |
| Swaps keep variables | `cluster profiles swap` reads current values from `GET /v1/spectroclusters/{uid}/variables` and re-supplies them for the new uid, plus `--var` overrides, and keeps every other attached profile. Masked sensitive values can't be carried: it refuses unless each is given with `--var` or `--allow-sensitive-reset` is passed. |
| Known silent failures refused | `cluster create edge-native` refuses `cloudConfig.vip` (dropped silently by the API; use `controlPlaneEndpoint {host, type: VIP}`). |
| Credentials never on argv or stdout | MAAS API key comes from `--api-key-env`/`--api-key-file`. Kubeconfigs and edge host tokens go to mode-600 files unless `--stdout`. `cloudaccount` masks every secret-shaped field, `--json` included. |

## Verbs and the evidence behind each

| Verb | Endpoint(s) actually hit | Why it's here |
|---|---|---|
| `projects` | `GET /v1/projects` | Step 1 of every real session (47 combined refs); every other verb needs a ProjectUid resolved from a name. |
| `clusters` | `POST /v1/dashboard/spectroclusters/search` | Plain `GET /v1/spectroclusters` list carries **no `status.health` at all** (confirmed live) — every session that wanted health went to this search endpoint instead. It's a POST because it takes a filter body, not because it mutates anything. |
| `cluster` | `GET /v1/spectroclusters/{uid}` + `GET /v1/dashboard/spectroclusters/{uid}/overview` | Same health gap as above at the single-resource level; conditions come from the overview surface per the `spectrocloud-troubleshooting` skill's own debugging workflow. `--profiles` adds a `profiles` and a `packs` table from `spec.clusterProfileTemplates[]` — already in the plain GET, so no extra call. Default output (no `--profiles`) is unchanged. See [Real API behavior](#real-api-behavior-discovered-while-building-this-not-documented-anywhere) below. |
| `profiles` | `GET /v1/clusterprofiles` | 111+ direct refs; profile discovery precedes almost every profile-editing session. |
| `profile` | `GET /v1/clusterprofiles/{uid}` | Layer/pack shape (`spec.published.packs[].tag`) confirmed against a real transcript that was diffing two profile versions. `--variables` adds one more call, `GET /v1/clusterprofiles/{uid}/variables`, for the profile's declared variables — a sensitive one's default always renders masked. Default output (no `--variables`) is unchanged. See [Real API behavior](#real-api-behavior-discovered-while-building-this-not-documented-anywhere) below. |
| `pack-values` | `GET /v1/packs/{packUid}?includePackValues=true` | A live 9/21/26 session needed a pack's default values YAML and a named preset's add/remove pair to build a storage layer, and had to drop to a raw urllib GET for both. Resolves the pack uid through the same `/v1/packs` search `packs` already uses; the same name+version can exist in more than one registry, so an ambiguous match dies `E_USAGE` listing every candidate `registryUid` — `--registry <uid-prefix>` disambiguates. `--values` prints only the raw default values YAML (redirectable); `--preset <name>` prints that preset's `add` YAML plus a `# remove:` comment block. See [Real API behavior](#real-api-behavior-discovered-while-building-this-not-documented-anywhere) below. |
| `edgehosts` | `GET /v1/edgehosts` | 40+ refs; the field shapes match all three skills' documented `jq` filters verbatim. `--wide` adds hardware columns (cores, memGB, disks, sanDisks, ip, secureBoot) from `spec.device`; `--min-cores`/`--label` filter client-side. Default output (no `--wide`) is unchanged. |
| `edgehost` | `GET /v1/edgehosts/{uid}` | An SE with a UID from a ticket or a name from a customer often doesn't have the project. Resolves the ref (uid/name/substring) against the same edge host search `edgehosts` uses, scoped to `--project` if given, else walks every project and reports which one matched. Prints the full hardware inventory: cpu/memory, OS, disks + partitions, NICs (virtual/container interfaces like `lxc*`/`cilium*`/`veth*`/`flannel*`/`cni*`/`docker*`/`kube-ipvs*`/`vxlan*`/`genev*`/`tunl*`/`lo` hidden by default, `--all-nics` to show them), gpu count. See [Real API behavior](#real-api-behavior-discovered-while-building-this-not-documented-anywhere) below. |
| `cloudaccounts` | `GET /v1/cloudaccounts/summary` | Lists every cloud type (aws, azure, gcp, vsphere, maas, openstack, ...) in one call. Never hits a per-cloud endpoint — see [Real API behavior](#real-api-behavior-discovered-while-building-this-not-documented-anywhere) below for why. |
| `cloudconfig` | `GET /v1/spectroclusters/{uid}` + `GET /v1/cloudconfigs/{kind}/{uid}` | 23 combined transcript refs, 13.0% failing, zero prior coverage. A cluster's `spec.cloudConfigRef` names the exact `{kind, uid}` to GET next — there is no tenant-wide cloudconfig list, so the cluster is the index. Read-only: the write calls this scout doc also saw (`PUT clusterConfig`, `PUT machinePools/...`) are deliberately out of scope. |
| `events` | `GET /v1/events/components/spectrocluster/{uid}` | **Not** `/v1/spectroclusters/{uid}/events` or `.../status/events` — both of those 404 or 422 in practice. A real session in the transcripts probed six candidate endpoints live and found this one; that probe's exact result is what this verb uses. |
| `packs` | `GET /v1/packs?filters=metadata.name=...` (fully paginated) | 127 direct refs, called out as "MANDATORY"/"CRITICAL" pagination in three separate skills because the endpoint silently caps at 50 results per page. This verb pages it exhaustively and returns versions newest-first with the true latest marked, instead of every session re-deriving the same offset-loop-plus-sort `jq` pipeline by hand. |
| `registries` | `GET /v1/registries/metadata` (list) + `GET /v1/registries/{pack,helm,oci}/{uid}` (describe) | 43 combined transcript refs, 30.2% failing — the worst fail rate of any bucket with double-digit volume, and zero prior coverage. `metadata` is the one endpoint that actually lists all three kinds; `GET /v1/registries/oci` itself 405s (`Allow: DELETE`). See [Real API behavior](#real-api-behavior-discovered-while-building-this-not-documented-anywhere) below. |
| `doctor` | Same read the `projects` verb uses, `limit=1` | Not a Palette data verb — checks whether 1Password and the Palette API are actually usable before you run one of the above. See [Configure](#configure). |

0.6.0 verbs, from an 11/18/26 transcript pass over 30 days of sessions (553
hand-rolled Palette calls; the ranked table is in the PR that added them):

| Verb | Endpoint(s) |
|---|---|
| `cluster kubeconfig` | `GET /v1/spectroclusters/{uid}/assets/{kubeconfig,adminKubeconfig,kubeconfigclient}` (Accept: octet-stream) |
| `cluster variables` | `GET /v1/spectroclusters/{uid}/variables` |
| `cluster rbac` / `rbac set` | `GET` / `PUT /v1/spectroclusters/{uid}/config/rbacs` |
| `cluster create` | `POST /v1/spectroclusters/{edge-native,maas,virtual}` |
| `cluster profiles attach` | `PATCH /v1/spectroclusters/{uid}/profiles?resolveNotification=false` |
| `cluster profiles swap` | `GET .../variables`, `PUT /v1/spectroclusters/{uid}/profiles` |
| `cluster delete` | `DELETE /v1/spectroclusters/{uid}[?forceDelete=true]` |
| `profile create` | `POST /v1/clusterprofiles`, `PUT .../variables`, `PATCH .../publish` |
| `profile version` | `POST /v1/clusterprofiles/{uid}/clone`, `PUT /v1/clusterprofiles/{new}/packs/{name}`, `PATCH .../publish` |
| `profile variables set` | `PUT /v1/clusterprofiles/{uid}/variables` |
| `profile publish` / `delete` | `PATCH .../publish` / `DELETE /v1/clusterprofiles/{uid}` |
| `profile export` | `GET /v1/clusterprofiles/{uid}/export` (Accept: octet-stream) |
| `pcgs` / `pcg` | `GET /v1/overlords[/{uid}]` |
| `cloudaccount` / `create maas` | `GET /v1/cloudaccounts/{kind}/{uid}` (masked) / `POST /v1/cloudaccounts/maas?privateGateway=` |
| `clustergroups` / `clustergroup` | `GET /v1/clustergroups/hostCluster/metadata` / `GET /v1/clustergroups/{uid}` |
| `vclusters` | the `clusters` search, filtered to cloudType nested/virtual |
| `edgehost token create` | `POST /v1/edgehosts/tokens` (no ProjectUid) |

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

- **(9/21/26) `GET /v1/spectroclusters/{uid}` already carries `spec.
  clusterProfileTemplates[]`** — the plain single-resource GET `cluster`
  already made, no separate call needed. Each entry is
  `{name, uid, type, profileVersion, packs[]{name, layer, tag}}`; a live
  session found `profileVersion` populated on every attached profile, so
  the `version` fallback `cluster --profiles` also checks (for symmetry
  with `profile`'s own `spec.version`) was not itself exercised live.
  Confirmed live against a cluster with 7 attached profiles (1 primary
  `cluster`-type infra profile plus 6 `add-on`-type profiles) — output
  matched a second, independently-verified direct GET on a different
  cluster in the same fleet.
- **(9/21/26) `GET /v1/packs/{packUid}?includePackValues=true` (with
  `ProjectUid`) returns `packValues[0]`** — `{values` (the default values
  YAML as a single string), `presets[]{name, group, label, add` (a second
  YAML string), `remove` (a list of dotted paths to strip)`, readme,
  schema}`. Confirmed live: a pack with 21 presets and a 182-line default
  values document round-tripped exactly through `pack-values`'s summary,
  `--values`, and `--preset` output. **The same pack name+version can
  exist in more than one registry at once** (confirmed live on
  `csi-portworx-generic 3.7.0` — two distinct `registryUid`s, same
  version) — `pack-values` dies `E_USAGE` listing every candidate
  registry uid rather than silently picking one.
- **(9/21/26) `GET /v1/clusterprofiles/{uid}/variables` returns
  `{variables:[{name, displayName, defaultValue, format, required,
  immutable, hidden, isSensitive}]}`** — confirmed live against a profile
  with 3 declared variables (two CIDRs, one enum-shaped string), none of
  which happened to be `isSensitive`. `profile --variables` still masks
  any `isSensitive` variable's `defaultValue` as `********`
  unconditionally (not live-exercised on an actual sensitive variable this
  session) — this tool never prints a value it can't confirm is safe to
  print, the same rule `cloudaccounts` and `registries` already follow for
  their own secret-shaped fields.
- **(9/21/26) `list_clusters()`'s pagination cap (`max_pages=10` ×
  `page_limit=50` = 500 rows) undercounts a project with more than 500
  clusters** — confirmed live in a 505-cluster project: `clusters`
  correctly printed the truncation `note:` (500 or fewer of 505, varying
  run to run — the search endpoint's own pagination ordering isn't
  stable), but `cluster <name>` resolving against that same incomplete
  list means a cluster that lands outside the fetched page range is
  reported as "no cluster matching" even though it exists. This predates
  and is unrelated to `cluster --profiles` — the same failure reproduces
  on plain `cluster <name>` with no flags. Not fixed here: raising
  `max_pages` (or teaching `list_clusters` a server-side name filter) is a
  behavior change to a verb this task didn't touch, out of scope for a
  no-mistakes-mode change without its own dedicated evidence and review.

- **(11/18/26) `GET /v1/clusterprofiles` now answers `405: method GET is not
  allowed, but [POST] are`**, which broke `profiles` and `profile`.
  `POST /v1/dashboard/clusterprofiles` (filter body) serves the list but
  returns one row per profile *name*, with every version under
  `specSummary.versions[]`. `profiles` expands them back to one row per
  version, newest first. `profile <uid>` now GETs the uid directly.
- **(11/18/26) Octet-stream endpoints fake success under `Accept:
  application/json`.** `assets/kubeconfig`, `assets/adminKubeconfig` and
  `clusterprofiles/{uid}/export` answer 200 with the 14-byte body
  `{"Reader":{}}`. `api_raw()` sends `Accept: application/octet-stream`, and
  `cluster kubeconfig` refuses to write anything that isn't a kubeconfig.
- **(11/18/26) `POST /v1/clusterprofiles/{uid}/clone` `{metadata:{name,
  version}}` returns an already-published copy**, and
  `PUT /v1/clusterprofiles/{uid}/packs/{name}` edits that copy's *draft*: the
  published values don't change until `PATCH .../publish`. Verified with a
  throwaway `paxi-selftest-111826` profile (created, cloned, published,
  exported, deleted).
- **(11/18/26) `GET /v1/spectroclusters/{uid}/variables` returns a bare list**
  `[{profileUid, variables:[{name, value, defaultValue, isSensitive, ...}]}]`.
- **(11/18/26) `429 TooManyRequests`** showed up during a burst of probes.
  `api()` retries a 429 up to 3 times (Retry-After honored, capped at 15s); a
  429 means the request wasn't processed, so this is safe for writes too.
- **(11/18/26) `GET /v1/clustergroups` is POST-only (create)** and
  `/v1/dashboard/clustergroups` 404s. `clustergroups` uses
  `GET /v1/clustergroups/hostCluster/metadata`. Not verified against a
  populated project; SA-Craig-Smith had no groups left. `vclusters`' cloudType
  filter (`nested`/`virtual`) is likewise unverified live.

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
| 4 | `E_REFUSED` | a write refused on policy: outside `--project`, delete without `--yes`, `--force` on a non-Deleting cluster, in-use profile, masked sensitive variable on a swap, a known-silent-failure body |

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

- PCG registration/pairing token helpers (`palette pcg install` covers it today).
- Edge host token list/delete, cluster group create, machine pool edits
  (`PATCH /v1/spectroclusters/{uid}/machinePools/...`), cloudconfig PUTs.
- Server-side validation (`?validateOnly`) before `cluster create`.

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
