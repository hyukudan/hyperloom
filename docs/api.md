# Hyperloom portal API

The portal (`hyperloom ui`) serves the web UI and a JSON API on `http://127.0.0.1:18700/hyperloom/api/`. It only
listens on loopback. This page documents the API, with the command line that does the same thing next to each call.
The shapes of the examples are exact; their values are illustrative (a test fixture), and long fields are trimmed.

- [Access and authentication](#access-and-authentication)
- [Recipes and profiles](#recipes-and-profiles): list, show, validate, explain, check, diff, select, pick, plan, save,
  delete, layers, receipts, probe, deploy, `up` with a recipe, the running deployment and its route statistics
- [Other endpoints](#other-endpoints): the screen documents, `up`/`down`/`install`/`doctor`/`hosts/pin`/
  `keys/rotate`/`cancel`, jobs
- [Feature routes](#feature-routes): all 22 feature modules, arguments, execution, audit and refusal rules
- [Route documentation convention](#route-documentation-convention): keeping this inventory complete

## Access and authentication

| | rule |
|---|---|
| Listener | `127.0.0.1` only. `Host` must be `127.0.0.1:<port>` or `localhost:<port>`. |
| Reads | `GET` (and `HEAD`). No body, no token. |
| Everything else | `POST` with `Content-Type: application/json`, a JSON object body, `Origin: http://<the Host>`, and the header `X-Hyperloom-CSRF: <token>`. |
| CSRF token | Random per portal start, embedded in the page: `<meta name="hyperloom-csrf" content="...">` of `GET /`. A script reads it from there. |
| Body size | 64 KiB; 1 MiB for `recipes/save`, `recipes/check` and `recipes/save-layer` (they carry recipe text, at most 256 KiB). |
| JSON | Strict: duplicate keys, `NaN`/`Infinity` and non-objects are refused (400). |
| Audit | Every core mutation, recipe write and job is appended to `audit.jsonl` next to the cluster configuration. Recipe and layer text is recorded as its size and sha256, not copied. Feature POST audit policy is listed per route. |

A script, with `curl` (PowerShell users: `curl.exe`):

```sh
PORTAL=http://127.0.0.1:18700
TOKEN=$(curl -s $PORTAL/ | sed -n 's/.*name="hyperloom-csrf" content="\([^"]*\)".*/\1/p')
post() { curl -s -X POST "$PORTAL/hyperloom/api/$1" -H "Origin: $PORTAL" -H "X-Hyperloom-CSRF: $TOKEN" \
              -H 'Content-Type: application/json' -d "$2"; }
post recipes/validate '{"recipe": "qwen3.8-flash-next.lab"}'
```

## Recipes and profiles

### Words

| word | meaning | example |
|---|---|---|
| **recipe** (the UI's "recipe profile") | One `strix-stream/recipe-v1` file. Its **id** is the file name without extension, `<model>.<variant>`. | `qwen3.8-flash-next.lab`, `qwen3.8-flash-next.speed`, `deepseek-v4.1-flash.lab` |
| **deployment profile** | A profile of the cluster configuration: which machines run which engine preset. | `qwen-replicas`, `ds41-rtx` |
| **tuning profile** | The autotuner's `strix-stream/recipe-profile-v1` layer for one recipe. | a file in the layers directory |
| **layer** | A tuning profile or an overrides file (`strix-stream/recipe-overrides-v1`) applied on top of a recipe. | `qwen3.8-flash-next.lab.edits.json` |

Recipe sources, all listed by `GET recipes`:

| source | where | writable |
|---|---|---|
| `builtin` | the vendored strix-stream example recipes, `hyperloom/recipes/examples/` (source commit and hashes in `hyperloom/recipes/builtin.json`) | no |
| `pending` | built-in recipes that wait for something (their `blocker`); they never launch and `up --recipe auto` never picks them | no |
| `configured` | the cluster configuration's `pc.recipe_dirs` (absolute directories) | no |
| `user` | `<config dir>/recipes/` (next to the cluster configuration in use) | yes: the only directory the API and `hyperloom recipe save` write |

Layers are in `<config dir>/recipes/layers/`, disk receipts in `<config dir>/run/receipts/`, saved hardware
observations in `<config dir>/run/facts/`. The API names these files by **name only**; it never takes a path.

Recipe references (`GET recipes/<id>`, `recipe`, `a`, `b`, `base`, and `up`'s `recipe`) are **ids only**:
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}` without a `.toml`/`.json` suffix, plus `auto` where the CLI takes it (`plan`, `up`).
Anything with `/`, `\`, `:`, a drive or a file suffix is refused (400) before anything runs, and snapshot digests
(`inputs`, `recipe_inputs`) must be 64 hex digits. The CLI looks an id up in the catalog only, never as a file of its
working directory; recipe FILE arguments (a `.toml`/`.json` name or a path) are for the command line only.

A recipe id or a recipe name used by two files makes every one of those files an error (`error` in the list); nothing
silently wins, and `pick` and `up --recipe auto` refuse them the same way.

### One code path

Every recipe request **is** a `hyperloom recipe ...` command line. The portal checks the JSON arguments, builds the
command line, runs it (a child process, or the same parser and function in process for the cheap reads), and answers
with what the command printed. So the CLI and the API give the same results and the same refusals, word for word.

Every recipe answer has this shape:

```json
{
  "schema": "hyperloom/recipes-<operation>-v1",
  "server_time": 1792877018.27,
  "operation": "validate",
  "cli": "hyperloom recipe validate qwen3.8-flash-next.lab --config /home/op/.config/hyperloom/cluster.json",
  "exit_code": 0,
  "ok": true,
  "result": { "...": "the command's JSON output" },
  "error": null
}
```

| field | meaning |
|---|---|
| `cli` | The equivalent command line. Field edits (see `edits`) appear as `--recipe-overrides edits.json`, with the file's text in `cli_files`. |
| `exit_code` | The command's: `0` ok; `1` refused (the reason is in `error`, sometimes with a `result`); `2` bad arguments; `3` unknown recipe id; `4` conflict (the file changed since it was read, or the id/name is taken). |
| `result` | The command's standard output (JSON). |
| `error` | The command's message (standard error, without the `hyperloom:` prefix). |
| `stopped` | Present when the portal stopped the command: `deadline`, `output overflow` or `client gone`. |

HTTP status: `200` for exit codes 0 and 1 (a refusal is a valid answer: read `ok`), `400` for 2, `404` for 3, `409`
for 4, `503` when two recipe commands are already running and four are waiting (try again), `504` past the deadline
(30 s for operations that do not touch hardware, 150 s for those that do; the deadline counts from arrival), `502`
when the command printed more than 8 MiB or no JSON. A stopped command is removed with its whole process tree.

Operation success and deployability are separate, as in the CLI: `explain` succeeds (exit 0) while reporting that the
hardware refuses the recipe in `result.hardware`.

### Endpoints

| API | CLI | what |
|---|---|---|
| `GET recipes` | `hyperloom recipe list [--model M]` | the catalog, models and the deployment profiles serving each, layers, receipts, saved facts |
| `GET recipes/<id>` | `hyperloom recipe show ID` | one recipe: text, structure, every settable field with its type, range and default |
| `POST recipes/ready` | `hyperloom recipe ready ID [P] [--facts F]` | what is still missing before `up` can run the recipe on the profile, step by step, each with the next command (offline) |
| `POST recipes/export` | `hyperloom recipe export ID [--layer NAME]...` | the recipe and those user layers as one data-only bundle |
| `POST recipes/import` | `hyperloom recipe import - [--id ID] [--replace SHA256]` | write a bundle's recipe and layers into the user directories (checked first; audited) |
| `GET recipes/matrix` | `hyperloom recipe matrix` | every recipe by model x hardware class, with its status, headline figures and their sources, and the command that runs it |
| `POST recipes/validate` | `hyperloom recipe validate ID [PROFILE]` | schema and layers; with a profile also the hardware match and the `up` decision |
| `POST recipes/explain` | `hyperloom recipe explain ID [PROFILE]` | each field's value, layer and origin; with a profile the decision, a simulated request and what-ifs |
| `POST recipes/check` | `hyperloom recipe validate - [PROFILE] < draft.toml` | a draft's text, nothing written |
| `POST recipes/diff` | `hyperloom recipe diff A B` | field-level differences of two effective recipes |
| `POST recipes/select` | `hyperloom recipe select MODEL [--profile P]` | the recipe `up --recipe auto` would run for a model on each profile serving it, every candidate's result |
| `POST recipes/pick` | `hyperloom recipe pick PROFILE` | the same for one profile (the CLI's `--dir` is CLI-only) |
| `POST recipes/plan` | `hyperloom recipe plan ID PROFILE` | launch eligibility and the placement preview (vramplan), on a snapshot of the inputs |
| `POST recipes/save` | `hyperloom recipe save ID --from FILE` / `--base ID --base-sha256 S --set ...` | write a user recipe |
| `POST recipes/delete` | `hyperloom recipe delete ID --sha256 S` | remove a user recipe |
| `POST recipes/save-layer` | `hyperloom recipe save-layer NAME --recipe ID --set ...` / `--from FILE` | write an overrides layer or a tuning profile |
| `POST recipes/delete-layer` | `hyperloom recipe delete-layer NAME --sha256 S` | remove a layer |
| `POST recipes/save-receipt` | `hyperloom recipe save-receipt NAME --from FILE` | store a node disk-read receipt |
| `POST recipes/probe` | `hyperloom recipe probe PROFILE [--disks] [--descriptors] [--evidence N=R] [--out F]` | audited job (exclusive slot, profile resources): the hardware facts; the flags read model files or store evidence |
| `POST recipes/deploy` | `hyperloom recipe deploy DIGEST [--stop P ...]` | audited job (exclusive slot, resource hold on `*`): stop the reviewed profiles and launch exactly a planned snapshot |
| `GET recipes/active` | `hyperloom recipe active [P]` | what each running profile was launched with, as launched |
| `GET recipes/routes` | `hyperloom recipe routes [P]` | per-route and per-request routing of the running recipe (RTX vs nodes) |

Common arguments of the evaluation calls (they map to the CLI's layer options):

| JSON | CLI | |
|---|---|---|
| `set: ["path=value", ...]` | `--recipe-set path=value` (repeated) | one value per entry, applied one by one, with the CLI's refusals |
| `edits: {"path": value, ...}` | `--recipe-overrides edits.json` | the UI's field editor: ONE overrides layer, so a `rank`/`list` pair changes together |
| `overrides: ["name.json", ...]` | `--recipe-overrides <layers dir>/name.json` | layer files, in order |
| `tuning_profile: "name.json"` | `--recipe-profile <layers dir>/name.json` | refused as stale (and the run is untuned) when it does not fit |
| `workload: "w"` | `--recipe-workload w` | |
| `facts: "name"` | `--facts <config dir>/run/facts/name.json` | a saved observation instead of live probes (read-only calls; `up` and deploy always probe live) |

#### GET recipes

```sh
hyperloom recipe list --model qwen3.8-flash-next
```

```json
{"schema": "hyperloom/recipes-list-v1", "operation": "list", "exit_code": 0, "ok": true,
 "cli": "hyperloom recipe list --model qwen3.8-flash-next --config /home/op/.config/hyperloom/cluster.json",
 "result": {
  "recipes": [{
    "id": "qwen3.8-flash-next.lab", "name": "qwen3.8-flash-next-lab", "model": "qwen3.8-flash-next",
    "source": "builtin", "status": "builtin", "writable": false, "sha256": "ee0f6036...", "error": null,
    "description": "Qwen3.8-Flash-Next: two node replicas; prompts >= 1K prefill on the RTX PRO (...)",
    "headline": {"ttft_1k_s": {"value": 1.36, "unit": "s", "basis": "measured", "route": "prefill_rtx", "label": "TTFT"},
                 "ttft_8k_s": {"value": 2.94, "unit": "s", "basis": "measured", "route": "prefill_rtx", "label": "TTFT"},
                 "decode_tok_s": {"value": 65.5, "unit": "tok/s", "basis": "measured", "label": "serve, wall aggregate"},
                 "memory_gib": {"value": 83.94, "unit": "GiB", "basis": "measured", "label": "VRAM"}},
    "tradeoffs": [{"route": "prefill_rtx", "metric": "ttft_s", "label": "TTFT", "unit": "s", "kind": "speed",
                   "value": 2.94, "at_prompt_tokens": 8192, "basis": "measured", "evidence": "qw-rtx-8k"}]}],
  "models": {"qwen3.8-flash-next": ["qwen-replicas"], "deepseek-v4.1-flash": ["ds41-pair", "ds41-rtx"]},
  "layers": [], "receipts": [], "facts": [],
  "user_dir": "/home/op/.config/hyperloom/recipes",
  "builtin": {"repo": "strix-stream", "path": "recipes/examples", "commit": "4f95e1dd..."}},
 "error": null}
```

(Trimmed: `recipes` holds every recipe.) `tradeoffs` are the recipe's own `[[x-predicted]]` rows: speed, memory and
quality (`top1_delta_pts`, `kl_ratio_vs_q8`) with how each was obtained (`basis`: measured, derived, projected, target,
upper/lower bound). `headline` keeps the best TTFT at 1K/8K/32K, the single-request decode rate, the peak memory and the
quality cost, from **enabled** routes only.

#### GET recipes/<id>

```sh
hyperloom recipe show qwen3.8-flash-next.lab
```

`result` = the list row plus `text` (the file), `structure` (match, limits, pins, routes) and `fields`:

```json
{"path": "routes.prefill_rtx.when.prompt_tokens_min", "value": 1024, "written": true, "type": "integer",
 "min": 0, "max": 9223372036854775807, "group": "route prefill_rtx", "pinned": false}
```

`type` is `number`, `integer`, `enum` (`values`), `boolean`, `string`, `count` (`"fill"` or a count), `list` or
`layers`. The ranges are hints for an editor: the recipe reader stays the judge.

#### POST recipes/validate, explain, check

```sh
hyperloom recipe validate qwen3.8-flash-next.lab --recipe-set routes.prefill_rtx.execute=tp
```

```json
POST recipes/validate {"recipe": "qwen3.8-flash-next.lab", "set": ["routes.prefill_rtx.execute=tp"]}
-> 200 {"exit_code": 1, "ok": false, "result": null,
        "error": "\"routes.prefill_rtx.execute\": not a settable path (§6.2)",
        "cli": "hyperloom recipe validate qwen3.8-flash-next.lab --recipe-set routes.prefill_rtx.execute=tp --config ..."}
```

With a deployment profile the hardware is probed (or a saved observation is used) and the answer says whether the
recipe matches, each route's availability, and what `up` would decide:

```sh
hyperloom recipe explain deepseek-v4.1-flash.lab ds41-pair --prompt-tokens 9000 --link-down pc-nodes
```

```json
POST recipes/explain {"recipe": "deepseek-v4.1-flash.lab", "profile": "ds41-pair", "prompt_tokens": 9000,
                      "link_down": ["pc-nodes"]}
```

`result.fields` = every non-default field with `value`, `layer` (default, recipe, profile, override), `origin` and the
`shadowed` values; `result.hardware` = binding, match problems, availability, `deploy` (decode route, each prefill
route placed `local`/`offload` or why not, the RTX service start or why not) and `refused`;
`result.simulated_request` = the route a 9000-token request would take. What-ifs: `unavailable` (device ids),
`link_down` (link ids), `free_ram` (`HOST=GIB`).

`POST recipes/check {"text": "<TOML or JSON>", "mode": "validate" | "explain", "profile": ...}` checks a draft; it is
`hyperloom recipe validate - < draft.toml`.

#### POST recipes/select and pick

```sh
hyperloom recipe select deepseek-v4.1-flash --profile ds41-rtx
```

```json
POST recipes/select {"model": "deepseek-v4.1-flash", "profile": "ds41-rtx"}
-> {"result": {"model": "deepseek-v4.1-flash", "reader": "core /opt/strix-stream/build/ss-recipe",
    "profiles": [{"profile": "ds41-rtx", "chosen": "deepseek-v4.1-flash.lab",
                  "deploy": {"...": "the chosen recipe's decision"},
                  "candidates": [{"id": "deepseek-v4.1-flash.lab", "result": "passes", "specificity": 9},
                                 {"id": "deepseek-v4.1-flash.lab-hetero", "result": "refused: ..."}]}]}}
```

Without `profile`, every deployment profile serving the model is evaluated. A model with no passing recipe exits 1 and
still lists every candidate with its reason.

#### POST recipes/plan

```sh
hyperloom recipe plan deepseek-v4.1-flash.lab ds41-rtx
```

```json
POST recipes/plan {"recipe": "deepseek-v4.1-flash.lab", "profile": "ds41-rtx"}
-> {"exit_code": 0, "result": {
     "profile": "ds41-rtx", "recipe": "deepseek-v4.1-flash.lab",
     "inputs": "055e5fe569e51cdbb2c69911ee71546ac131d502fc24e8ac65407f5aba3d8cbd",
     "intent": {"profile": "ds41-rtx", "router": "local", "deadline_s": 1140, "queue_timeout_s": 3600},
     "up": {"ok": true, "production": true, "conditions": "conditional preflight: ..."},
     "service": {"start": true, "vram_cap_gib": 88.0, "host_gib": 48.0, "plan": {"...": "the vramplan tiers"}},
     "route_table": {"...": "what the router receives"},
     "evaluation": {"...": "the launch record up would keep"},
     "checked_at_start": ["the GPU turn / lease and its approval", "prelaunch: ...", "the PC reservation at start",
                          "the RTX PRO idle and free RAM/commit ...", "every node's free-RAM floor ..."]}}
```

`plan` first copies everything the launch depends on (the recipe, its layers, the cluster configuration, the `set`
values, the profile and launch arguments) into a content-addressed snapshot, `<config dir>/run/recipe-inputs/<inputs>/`,
and evaluates that snapshot: the profile, node and router checks of `up`, then the recipe decision and the RTX service
planned at the recipe's budget (the same `prepare` that `up --recipe` runs before any lease). `up.ok` is **conditional**:
the checks in `checked_at_start` run when `up` starts and can still refuse it. When `up` would refuse, `up.ok` is false,
`up.refused` says why, and the decision and planner output are still shown under `preview` (labelled, not deployable).
Snapshots that no running launch references are removed after 24 hours.

#### POST recipes/deploy (job)

```sh
hyperloom recipe deploy 055e5fe5...d8cbd --stop qwen-replicas
```

```json
POST recipes/deploy {"inputs": "055e5fe5...d8cbd", "stop": ["qwen-replicas"]}
-> 202 {"job": "9f3c...", "action": "recipes/deploy", "state": "running", ...}
```

The switch as one job, holding the portal's single job slot (other mutations get 409; `down` and `cancel` keep
priority). In order, and refusing before anything is stopped: the snapshot (digest and every file), the running set
(must be exactly `stop`), the launch checks and the recipe decision on fresh facts. Then each listed profile is stopped
and `up --recipe-inputs <inputs>` launches exactly the snapshot: its configuration copy, its profile, its layers. Poll
`GET jobs/<id>`.

`POST cancel {"job": "<deploy job>"}` undoes only what the deploy did. Before the hand-over to `up` it only writes the
deploy's cancel marker: the deploy stops before its next step and `up`, right after it takes the profile, starts
nothing; profiles the deploy already stopped stay stopped (start them again with `up`). Once `up` owns the target
profile, the cancel stops that profile (`down`), the one the deploy started. The answer says what happened: `phase`
(`not started`, `checks`, `stopping`, `launching`, `done`, `failed`), `profile`, `stopped_by_deploy`, `stopping`,
`down` (the target's stop as `down` answered it: `stopped`, or `stop-requested` while its launch is still winding down;
null when nothing of the deploy runs) and `summary`; the cancelled deploy's `error` says the same, with how each
profile it stopped answered. When the deploy's progress cannot be read, the cancel fails (500) and says so; its marker
is written all the same. A `down` (or a cancel by profile) while a deploy runs cancels the deploy the same way but
stops only the profile it names. A job that has finished is not cancelled (409, at once).

#### POST up with a recipe (job)

```sh
hyperloom up --recipe qwen3.8-flash-next.lab          # the profile is the one serving the recipe's model
hyperloom up ds41-rtx --recipe deepseek-v4.1-flash.lab --recipe-set limits.host_free_ram_floor_gib=12
```

```json
POST up {"recipe": "qwen3.8-flash-next.lab"}
POST up {"profile": "ds41-rtx", "recipe": "deepseek-v4.1-flash.lab", "recipe_set": ["limits.host_free_ram_floor_gib=12"],
         "recipe_overrides": ["mine.json"], "recipe_profile": "tuned.json", "recipe_workload": "default"}
POST up {"recipe_inputs": "055e5fe5...d8cbd"}
```

Any model with a recipe can be started this way (the portal's plain `up` stays limited to Qwen replica profiles).
Without `profile`, the recipe's model must be served by exactly one deployment profile (or the snapshot names it);
the accepted job's `arguments.profile` is that profile. `cancel {job}` works as for a deploy (the job runs
`up --job <id>`): before the hand-over only its marker, and `up` starts nothing; once `up` owns the profile, the
cancel stops it; the answer has the same fields. A `down` (or a cancel by profile) of the profile it launches
writes its marker too, so it starts nothing if it has not taken the profile yet. `recipe` is an id or
`auto` (a file only on the command line). A recipe `up` runs on the portal's own configuration (a `config` naming
another file is refused). Refusals come back as the job's `error`, with the CLI's text. A pending recipe is
refused. Some profiles cannot run a recipe yet; the refusal says why (for example, a MiniMax pair binds loopback and
needs its router on rank 0, while recipes need the local router).

#### POST recipes/save, delete

```sh
hyperloom recipe save qwen3.8-flash-next.mine --from mine.toml
hyperloom recipe save qwen3.8-flash-next.fast --base qwen3.8-flash-next.lab --base-sha256 ee0f6036... \
    --set routes.prefill_rtx.when.prompt_tokens_min=2048
hyperloom recipe delete qwen3.8-flash-next.mine --sha256 <sha256 of the file>
```

```json
POST recipes/save {"id": "qwen3.8-flash-next.mine", "text": "<TOML or JSON>"}
POST recipes/save {"id": "qwen3.8-flash-next.fast", "base": "qwen3.8-flash-next.lab", "base_sha256": "ee0f6036...",
                   "set": ["routes.prefill_rtx.when.prompt_tokens_min=2048"], "name": "qwen-fast"}
POST recipes/save {"id": "qwen3.8-flash-next.mine", "text": "...", "replace": "<sha256 it had when read>"}
POST recipes/delete {"id": "qwen3.8-flash-next.mine", "sha256": "<sha256 it had when read>"}
-> {"exit_code": 0, "result": {"id": "qwen3.8-flash-next.fast", "name": "qwen-fast",
                               "path": "/home/op/.config/hyperloom/recipes/qwen3.8-flash-next.fast.json",
                               "sha256": "...", "replaced": false, "reader": "core ..."}}
```

Rules (CLI and API alike, one lock across processes):

- The text is validated first, by the same reader the CLI uses (the strix-stream core when installed). Nothing is
  written on a refusal.
- Only the user directory is written. Built-in and configured recipes are never written or deleted.
- An id is `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, does not end in `.`, is not a Windows device name or one of the reserved
  words (`active`, `routes`, `validate`, ...). Ids compare without case; one file per id (`.toml` or `.json`).
- The id and the recipe name must be free across every source (409 otherwise).
- Replacing or deleting needs the sha256 the file had when it was read (409 when it changed); creating needs a free id.
- A copy from a base (`base` + `set`) is written as JSON: the base's own document with the values set, its name, and
  `x-derived-from` (base id, name, sha256, the values). The values are applied as **one overrides layer** first and the
  copy must resolve exactly like the base with that layer. The base's predictions move to
  `x-derived-from.base_predictions` (shown as "base, not re-measured") and its tuning profiles do not apply to the copy.

#### POST recipes/save-layer, delete-layer, save-receipt

```sh
hyperloom recipe save-layer qwen-floor --recipe qwen3.8-flash-next.lab --set limits.host_free_ram_floor_gib=6
hyperloom recipe save-layer tuned --from tuned-profile.toml
hyperloom recipe save-receipt halo1-disk --from receipt.json
```

```json
POST recipes/save-layer {"name": "qwen-floor", "recipe": "qwen3.8-flash-next.lab", "set": ["limits.host_free_ram_floor_gib=6"]}
POST recipes/save-layer {"name": "tuned", "text": "<an overrides or tuning-profile document>"}
POST recipes/save-receipt {"name": "halo1-disk",
                           "text": "{\"disk_read_gbytes_ps\": 6.1, \"method\": \"direct\", \"host\": \"halo-1\", \"path\": \"/models/ds41.gguf\"}"}
```

A layer is checked against its recipe before it is written (an overrides layer is resolved with it; a tuning profile's
schema, recipe and sha256 are checked, its fit to the hardware is judged where it is used). Layer documents are read
with the renamed `tierstream/*` schema ids too (same kinds, and a given document is stored as it is); the layers
the portal builds keep the `strix-stream/*` ids.

#### POST recipes/probe (job)

```sh
hyperloom recipe probe ds41-rtx --descriptors
hyperloom recipe probe ds41-pair --evidence halo-1=halo1-disk
```

```json
POST recipes/probe {"profile": "ds41-rtx", "descriptors": true}
POST recipes/probe {"profile": "ds41-pair", "evidence": ["halo-1=halo1-disk"], "out": "ds41-pair-today"}
```

A probe runs as a job (one mutating job at a time) and holds the runtime lock while it writes the descriptor store, the
disk cache or evidence. `descriptors` reads the model file's header; `disks` measures a direct-I/O read (at most 256 MiB,
2 s); `evidence` stores a node's receipt; `out` saves the observation as `<config dir>/run/facts/<name>.json` for later
`facts` arguments.

#### GET recipes/active

```sh
hyperloom recipe active ds41-rtx
```

```json
{"result": {"deployments": [{
  "profile": "ds41-rtx", "phase": "running", "recipe": "deepseek-v4.1-flash.lab", "production": true,
  "effective": {"...": "the effective recipe as launched"},
  "explain": {"fields": ["...: value, layer, origin, shadowed as launched"]},
  "inputs": [{"role": "recipe", "id": "deepseek-v4.1-flash.lab", "original": ".../deepseek-v4.1-flash.lab.toml",
              "sha256": "cfee1c28...", "now": "unchanged"}],
  "record": {"deploy": {"...": "decode route, prefill placement"}, "service": {"start": true, "why": null},
             "route_table": {"effective_sha256": "...", "routes": [{"id": "prefill_long_rtx", "enabled": true}]},
             "inputs_digest": "055e5fe5...", "layers": {"set": [], "overrides": [], "profile": null}}}]}}
```

Everything comes from the launch record, so a recipe edited or deleted since the launch is still explained as it was
launched; `now` says `unchanged`, `changed since the launch` or `deleted since the launch`.

#### GET recipes/routes

```sh
hyperloom recipe routes ds41-rtx
```

```json
{"result": {"deployments": [{
  "profile": "ds41-rtx", "effective_sha256": "...",
  "routes": [{"id": "prefill_long", "enabled": true, "on": "rtx", "admitted": 12},
             {"id": "prefill_nodes", "enabled": true, "on": "nodes", "admitted": 30}],
  "totals": {"rtx": 12, "nodes": 30, "refused": 0, "handoff_failed": 1},
  "recent": [{"id": "4f0c1e2a9b7d6c55", "at": 1792877018.2, "prompt_tokens": 9120, "uncached_tokens": 9120,
              "context_tokens": 10144, "replica": "halo-1",
              "attempts": [{"replica": "halo-1", "route": "prefill_long", "place": "offload", "outcome": "admitted"}],
              "route": "prefill_long", "place": "offload", "on": "rtx", "outcome": "served", "status": 200,
              "ttfb_ms": 3110.4, "total_ms": 9022.7}],
  "state": {"at": 1792877030.0, "age_s": 0.6, "devices": {"...": "availability the router uses"}},
  "why": {}}]}}
```

- `admitted`: requests admitted to each route when the router chose it (not completions).
- `on`: `rtx` or `nodes`, from the **recorded** launch decision.
- A failed hand-off counts in `handoff_failed`, and the request's admission on its fallback route.
- `recent`: the router's own record of the last 256 recipe-routed requests: each attempt (`admitted`,
  `handoff_failed`, `not_admitted_after_wait`, `connect_failed`, `refused`), the final route and `outcome`
  (`served`, `refused`, `no_replica`, `upstream_error` including a complete non-2xx answer, `client_gone`), the HTTP
  `status`, and router-measured first byte and total time. The id is returned to the client as `X-TensorMeld-Route-Id`
  and sent to the engine as `X-Request-Id`. It needs the TensorMeld router with per-request route records (branch
  `router/recipe-routes-recent`); with an older router `recent` is null and `why.recent` says so. Engine-internal
  per-request timings are not joined (the engines do not report request ids).
- Counters and records restart with the router (`instance` in the router status changes).
- `why.identity` appears when the router serves another route table than the launch recorded.

## Other endpoints

Core identifiers (`profile`, `machine`, `build_on`, `router`) match
`[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. Core string arguments and string-array entries must be
nonempty and cannot start with `-`. Arrays contain at most 256 entries. Recipe launch fields use
the recipe reference/layer rules above; `config`, where accepted, must name this portal's own configuration.

| API | Returns / does | Arguments (JSON for POST; query for GET) | Execution / audit | Refusals |
|---|---|---|---|---|
| `GET devices` | Machine inventory and health ([screen schemas](ui/UI-SPEC.md)) | None used | Sync; no audit | Snapshot reports unavailable sources |
| `GET topology` | Links, routes and topology | None used | Sync; no audit | Snapshot reports unavailable sources |
| `GET models` | Models and asset status | None used | Sync; no audit | Snapshot reports unavailable sources |
| `GET profiles` | Profile state and launch configuration | None used | Sync; no audit | Snapshot reports unavailable sources |
| `GET stats` | Engine/router counters and recent requests | None used | Sync; no audit | Snapshot reports unavailable sources |
| `GET logs` | Snapshot logs, or one process tail | Optional `profile`, `process` (default router), `tail` integer 1..65536 (default 100; used with profile) | Sync; no audit | 400 tail outside bounds |
| `GET settings` | Configuration, capabilities, feature load outcomes | None used | Sync; no audit | Snapshot reports unavailable sources |
| `GET jobs` | Retained jobs and active mutation id | None used | Sync; no audit | — |
| `GET jobs/<id>` | One retained job, result/error/progress | Retained job id; no query used | Sync; no audit | 404 unknown job |
| `POST up` | Launch a profile; [recipe launch](#post-up-with-a-recipe-job) | `profile` id (required without recipe), `router` id, `node`/`device` string arrays 1..256, `deadline`/`approval_lease` positive integers, `approval_ref`/`approval_end` strings; recipe keys above; optional same `config` | Audited job; exclusive slot; profile resources (all resources for recipe_inputs) | 400 invalid arguments/preflight; 409 occupied resources/slot |
| `POST down` | Stop one profile | Required `profile` id; `force` boolean | Audited priority job; stops/cancels overlapping work | 400 invalid arguments |
| `POST install` | Install one profile | Required `profile` id; optional `build_on` id, same `config` | Audited job; exclusive slot; profile resources | 400 invalid arguments; 409 occupied resources/slot |
| `POST doctor` | Run diagnostics | Optional `machine` id, same `config` | Audited job | 400 invalid arguments |
| `POST hosts/pin` | Pin a compared SSH host fingerprint | Required `machine` id, `fingerprint` nonempty string; optional same `config` | Audited job | 400 missing fingerprint/invalid arguments |
| `POST keys/rotate` | Rotate a named secret | Required `name` nonempty string | Audited job | 400 invalid arguments |
| `POST cancel` | Cancel feature or launch job, or stop a profile | Exactly one of `job` string or `profile` id | Audited priority job | 400 unknown/unsupported job; 409 already finished |
| `POST jobs/renew` | Renew owner heartbeat for running feature jobs | Exactly `jobs`: string array, at most 32 ids; unknown/finished ids returned in `not_running` | Sync; no audit | 400 invalid list |

Mutations answer `202` with a job (`job`, `state`, `action`, `arguments`, `result`, `error`). One mutating job runs at a
time; `down` and `cancel` never wait. `up`, `down`, `install` and `recipes/deploy` run as their own CLI process, which
finishes even if the portal stops.

Every mutation runs on the portal's own configuration: a `config` argument naming another file is refused (400),
for `up`, `install`, `doctor` and `hosts/pin` alike.

`cancel {job}` of a plain `up` works as for an `up` with a recipe (the job runs `up --job <id>`): before `up` owns the
profile, only its marker (it starts nothing; a profile that was already running, which `up` refuses, is never
stopped); once it owns it, the cancel stops it. A `down` (or a cancel by profile) of the profile an `up` job launches
writes that job's marker too.

## GET results

Returns a standard `hyperloom/ui-results-v1` document with `published`, `live` and `why`.
Optional `?model=<id>` filters both collections by exact model id; ids must match
`[A-Za-z0-9._-]{1,128}` (invalid or unknown query arguments return 400). An unknown valid id
returns empty collections. The package file path is fixed and cannot be supplied by a client.

`published` is the hand-transcribed `hyperloom/results-v1` package document: `source`
(repo, ref, file, retrieved) and `models` (id, name, quant, generic hardware, status, rows).
Each row carries mode, hardware, prompt, concurrency, metric, best, nullable typical, unit,
date, build, note and the source README section. Best may also be null when only a typical
value was published. Metrics are decode_tok_s, ttft_s, ms_per_token, ms_per_step,
aggregate_tok_s and prefill_tok_s. Units and workload conditions are retained; rates in
parentheses are copied only when literally present, never recomputed. Wall aggregate and
summed per-request decode remain separate modes. A README-published measurement can still
name an engine build pending publication; its note/status retains that qualification.
Source: hyukudan/strix-halo-engine-lab README.md at 62d7b0ac, retrieved 2026-10-26; method
links use docs/performance.md at that same ref. Missing/invalid data (including non-finite
numbers) yields published: null and why.published, never an internal error or a local path.

`live` contains one entry per running profile: profile, model (or null with why when its
recipe/preset identity cannot be mapped), and values. Each value has value, unit, label,
metric, n, window_s and why when unavailable; observations may also name a replica, group,
percentile or as_of. Completed-token throughput comes from the existing core stats cache,
per replica and over its actual counter window, falling back to existing fleet/observe
counter rates when core stats have none. Stale cached stats are unavailable. Decode
p50 and TTFT p50/p95 reuse observe/latency's measured engine-side per-request distributions
over 600 seconds, with n for each metric. Cached stats TTFT can fill an absent measured
TTFT distribution, explicitly labelled as the queue + prefill approximation over the last
50 requests / 600 s. Per-model groups and per-replica counter/approximation rows remain
separate; no percentiles are combined into a fleet percentile.
Every label starts with live and identifies observer and counting basis. Missing sources
produce null plus why; no running profile produces live: [], never zero measurements.
GET reads launch records, the shared snapshot and existing observability storage only: it
never refreshes the snapshot, opens an engine connection, starts a collector or creates
telemetry storage. The normal API redaction applies to the whole document.

### Launch progress

`GET launch` returns `hyperloom/ui-launch-v1` with `launches`: the latest retained `up` or
`recipes/deploy` job for each known profile, including finished jobs. Jobs are retained by this portal process
(the last 100 jobs, with running jobs never evicted). A deploy whose target has not yet been recorded cannot
appear under a profile yet.

`GET launch/<job>` returns the same document schema for one launch job. The id must be hex only:
malformed ids and query parameters return 400; unknown ids and ids of other job types return 404.
Neither endpoint accepts client paths or query parameters. Both are read-only and use portal redaction.

| field | meaning |
|---|---|
| `job`, `profile`, `action`, `job_state` | Launch identity and the portal job's state; profile may be null until recorded. |
| `elapsed_s` | Seconds since the job started, frozen at its finish time; null without a start time. Includes checks and switch stops. |
| `phases` | Ordered `checks`, `stopping`, `lease`, `processes`, `loading`, `ready`. Each has `id`, `label`, `state`, `started_at`, `finished_at`, `detail`. |
| `eta` | `seconds` (estimated total time to READY), `n`, `min`, `max`, `label`. With no history these numbers are null (except `n: 0`) and `why` explains the absence. |
| `failure` | Null, or `{phase, reason}` for a failed/cancelled job or an attributable POISONED process. An unobservable failure phase is null with `why`. |
| `why` | Source availability explanations, such as unreadable progress. |

Phase states are `pending`, `running`, `done`, `failed`, `skipped`, or `unknown`.
An unknown phase includes `why`. Times are Unix seconds or null when not recorded; boundaries are never
interpolated. Stop details name the stopped/stopping profiles; process details name machine/role and their
recorded state, start time and ready-after value where present. Loading runs from process start until READY:
engines cold-load at startup. There is no separate hot-load operation.

Evidence is limited to the job, its existing progress file, the cached profiles and captured supervise logs.
GET never polls nodes, starts work, or writes history. Untimed log events cannot be attributed to a launch;
they leave an explanation instead of changing its phases. A successful orchestrator result (`phase: running`)
establishes all READY because it returns only after its readiness waits.

ETA is the median of the last 20 successful starts observed for that profile by this portal, with the label
"estimate from N previous starts on this portal". Only the launch job thread appends observations after
an up/deploy returns all READY, after the job completes and its audit is written, to `run/launch-history.json`
beside the portal configuration. History stores only the config key, profile and elapsed seconds, keeping
at most 20 rows per config/profile and 500 rows overall (newest first retained). Historical estimates persist across portal restarts. No published model
load times or fabricated fallback are used. Remaining time is estimated total minus elapsed; exceeding the
estimate is shown as such and does not mean the engine is ready.

## Route documentation convention

Every API route has exactly one table row whose first cell is `METHOD path`, relative to
`/hyperloom/api/`, without query strings or body examples. A registered GET prefix is written
`prefix/<id>`; the suffix may be a series name, export name or other handler-specific identifier.
`tests/test_api_docs.py` loads the real feature modules and compares both directions, reporting missing,
stale and duplicate entries. It suppresses only the planner's optional on-disk capability probe; no API
instance, server, collector or handler is started. Collectors and capabilities are metadata, not routes.
Add a row when adding a route and remove it when removing the route.

In the feature tables, arguments are query keys for GET and JSON body keys for POST; `?` means optional.
All numbers must satisfy the stated bounds; the server rejects non-finite JSON numbers globally.
GET documents and synchronous POSTs normally return 200; jobs return 202 with the core job envelope.
Validation raises 400, unknown objects raise 404, and permission/conflict refusals raise 409 unless
otherwise stated. Failures after acceptance appear in the job's state/error, not as a new HTTP response.
All jobs are audited (acceptance, refusal and completion); synchronous audit is indicated per row,
including auditing performed inside a handler. GETs are unaudited. Common POST security checks above
also apply (403 for Origin/CSRF, 415 for content type, 413 for oversized bodies).

Admitted benchmark jobs use class `bench`: refused with 409 for a core mutation, an overlapping
resource hold or the lab cap (2 admitted jobs). Resources stay held through cleanup; admission-store
failure returns 503. Renew the owner through `POST jobs/renew` while the page is open (120 s without a renewal cancels the job); `cancel` and priority stops request cancellation. Exclusive feature jobs take the
core mutation slot and a core resource hold. Jobs with neither admission nor exclusive mode take
no shared hold. Each table names the deadline and actual resource policy.

## Feature routes

The sections follow `hyperloom.web.features.MODULES`. Source links point at handlers and validators;
longer format/CLI documentation is linked where available. Missing observations generally return null
with `why`, rather than invented zeroes.

### overview

[Source](../hyperloom/web/features/overview.py).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET overview` | Fleet KPIs and machine cards from existing telemetry/cache | None | Sync; no audit | 400 any query |

### results

[Source](../hyperloom/web/features/results.py); [document fields and counting basis](#get-results).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET results` | Published results and measured live values | `model?` string matching `[A-Za-z0-9._-]{1,128}` | Sync; no audit | 400 invalid/unknown query; unknown valid model yields empty lists |

### sessioncache

[Source](../hyperloom/web/features/sessioncache.py). Read-only A1; no management actions.

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET sessioncache` | Retained session cache per running profile | `profile?` id matching `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` | Sync, store only; no audit | 400 invalid/unknown query |

The `hyperloom/ui-sessioncache-v1` document contains `profiles` (null when the store is unavailable),
`why` and `stale_after_s`. Each profile has `profile, engine, supported, why, as_of, stats, sessions, coverage, sources`.
`supported` is true after both queries succeed, false for absent/disabled routes, and null for collection failures.
Stats: `ram_bytes, ram_budget_bytes, entries, entry_limit, evictions, hits, misses, refusals, why`; sums across
replicas only when every replica reports the field; TP2 rank 0 bytes do not include peer RAM. Sessions: `id, replica, location, tokens, position, bytes, generation, last_used, why`.
Missing fields are null with a reason; unknown location is `unknown`. Invalid IDs are dropped without echoing them.
`coverage` discloses returned/parsed counts, omissions, the 256-row limit and ordering: newest first when recency
is reported, engine order otherwise. `sources` retains per-replica support, stats and reasons.

The collector runs at most every 30 s per endpoint, only while a portal page is open, and backs off
for 10 min per endpoint reported unsupported. With no page open or no node poller, it collects nothing
and keeps previous observations with the reason `collected only while a portal page is open`. Engine
reads require a poll-budget grant before credential lookup; foreign GPU turns pause collection.
Profile and discovery storage, including reconciliation, are partitioned by configuration identity. It performs bounded reads (2 s/request, 4 s/run, 1 MiB/response) using node engine
keys from the secret store, only after checking each replica address against its configured machine
(or a loopback address for loopback bindings; these are not queried). GET never collects, discovers launches, creates a store or contacts a node. Stopped
profiles are reconciled on collection; clients should mark observations older than `stale_after_s` stale.
DeepSeek uses only read-only POST `/admin/session-cache` ops `list` and `stats`. MiniMax first requires
`GET /health` mode `session-cache`, then only `list/stats` operations at `/v1/session-cache`.
Normal MiniMax serving is unsupported. Node-loopback endpoints cannot be queried from the portal and are disclosed
as unavailable; no SSH or node writes are attempted. Launch discovery is bounded as in observe; omissions are disclosed.

Field mapping follows the supplied code: DeepSeek `sessions` is an ID array; `ram_bytes, ram_entries, evictions, refused`
map to RAM bytes, entries, evictions, refusals. MiniMax `sessions` is an ID array; `bytes, entries, evictions`
map to RAM bytes, entries, evictions. Neither emits per-session detail, limits, recency or hit/miss counts.
Qwen uses only `GET /v1/sessions` and `GET /v1/cache/stats`. Its `qwen-hip_session_store.hpp` serializer
emits session objects with `id, parked, disk`: `parked` means RAM presence; both true means `ram+disk`,
only `parked` means `ram`, only `disk` means `disk`, neither means `unknown` with a reason. Missing or
invalid booleans leave location unknown. Stats `ram_bytes, ram_budget, parked, max_parked, evicted, hits, misses`
map to `ram_bytes, ram_budget_bytes, entries, entry_limit, evictions, hits, misses`; refusals are not emitted.
RAM bytes/budget include the tombstone reservation. Tombstone counts and disk fallback status are extra fields
and ignored. Qwen emits no per-session tokens/position, bytes, generation or recency. The docs call hits/misses
"prefix" counts; the code counts session matching hits/misses. Presence of a disk file does not guarantee it
is eligible for implicit fallback after a newer RAM turn. The supplied Qwen measurement is 22.6 s prefill / 1.04 s restore
for the qualified r1b target-only CLI; it does not qualify the draft HTTP paths. The requested 1.05 s example
is absent from the supplied docs and is therefore omitted from the page.

### launch

[Source](../hyperloom/web/features/launch.py); [phases and ETA](#launch-progress).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET launch` | Latest retained launch per known profile | None | Sync; no audit | 400 any query |
| `GET launch/<id>` | One up/deploy job's observed phases and ETA | Hex job id; no query | Sync; no audit | 400 malformed id/query; 404 missing or non-launch job |

### machines

[Source and threshold validator](../hyperloom/web/features/machines.py).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET machines/<id>` | GPU, memory, CPU, processes, network, disks, engines, events and history links | Configured machine id; no query | Sync; no audit | 400 query; 404 unknown machine |
| `GET fleet/thresholds` | Effective/default memory headroom thresholds | None | Sync; no audit | 400 query |
| `POST fleet/thresholds/set` | Merge/save headroom thresholds | Nonempty subset of `unified`/`rtx`/`commit`: `{amber_gib?, red_gib?}` numbers 0..1024; red ≤ amber; only `commit.red_gib` nullable; or exactly `{reset:true}` | Sync; audited | 400 invalid group/value/order |

### history

[Source](../hyperloom/web/features/history.py); [telemetry export](prometheus.md).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET history` | Series descriptors and collector states | Query ignored | Sync; no audit | Missing store yields null + why |
| `GET history/<id>` | Series buckets, labels, tier, coverage and truncation (≤3000 points per line) | Series `[a-z][a-z0-9_]{0,63}`; `range?` seconds (default 3600), `start?`/`end?` finite epochs, 0 ≤ end−start ≤ 31 days; `tier?` raw/m1/m15; `l.<label>?` string ≤128, label `[a-z][a-z0-9_]{0,31}` | Sync; no audit | 400 name/query/range/tier; missing store returns null lines |

### observe

[Handlers and bounds](../hyperloom/web/features/observe.py). Filter strings match
`[A-Za-z0-9._:/@+-]{1,128}` (the @ character is allowed).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET observe/requests` | Joined router/engine request records, filters and source gaps | `profile?`/`model?`/`route?`/`outcome?`/`caller?`/`state?` filter strings; `limit?` integer 1..200 (50); `before?` finite epoch | Sync; no audit | 400 invalid/unknown query |
| `GET observe/requests/<id>` | One request with attempts and engine observations | Exactly 16 hex characters; no query | Sync; no audit | 400 id/query; 404 unknown retained request; unopened store returns null |
| `GET observe/latency` | Per-request quantiles/histograms and coverage (newest 20,000 rows) | `profile?` filter; `range?` integer 60..2678400 (3600); `by?` model/route/caller (model) | Sync; no audit | 400 invalid/unknown query |
| `GET observe/engines` | Engine state, memory, queue, cache and accounting | `profile?` filter string | Sync; no audit | 400 invalid/unknown query |
| `GET observe/speculative` | Speculative counters, depth, acceptance and estimated savings | `profile?` filter string | Sync; no audit | 400 invalid/unknown query |
| `GET observe/sessions` | Router sessions or retained-request grouping, newest 500 | `profile?` filter string | Sync; no audit | 400 invalid/unknown query |
| `GET observe/settings` | Affinity expiry, retention, defaults and bounds | None | Sync; no audit | 400 query |
| `POST observe/settings/save` | Merge/save observability settings | Nonempty subset: `affinity_idle_s` integer 60..86400, `retention_days` integer 1..30 | Sync; audited | 400 invalid/unknown field |

### ledger

[Handlers and validators](../hyperloom/web/features/ledger.py). Ledger ranges are `3600`, `86400`
(default), `604800`, `2592000` or `all`; intervals cover whole UTC hours.

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET ledger` | Token KPIs, time buckets, model totals, gaps and coverage | `range?` ledger range; `by?` kind/model/machine (kind) | Sync; no audit | 400 invalid/unknown query |
| `GET ledger/export` | CSV text, filename and truncated-row count | `range?` ledger range | Sync; no audit | 400 query; 409 unopened store |
| `GET ledger/census` | Last collector snapshot of artifacts and live census | None | Sync; no audit | 400 query; unavailable snapshot yields null + why |
| `GET ledger/rtx` | Last RTX service/run listing and last parsed run | None | Sync; no audit | 400 query; unavailable snapshot yields null + why |
| `GET ledger/value` | Model token costs, cache savings and price coverage | `range?` ledger range | Sync; no audit | 400 unknown query (including by) |
| `POST ledger/reset` | Delete ledger records/gaps and reset counting boundary | Exactly `{confirm:"reset"}` | Sync; audited | 400 confirmation |
| `POST ledger/settings` | Save/clear census or run-log directory and refresh snapshot | `census_dir?`/`logs_dir?`: null or existing absolute plain directory string 1..400; at least one | Sync; audited | 400 links/junctions, missing/unreadable/relative paths |
| `POST ledger/prices` | Save price table per million tokens | `currency` 1..8 letters/currency signs/spaces, nonblank; `default?` null or `{input,cached,output}` finite prices 0..100000; `models?` map ≤64 names (1..128 chars) to same price objects | Sync; audited | 400 invalid fields/prices |
| `POST ledger/rtx-parse` | Parse/store last stats of a listed run | Exactly `id`: 16 lowercase hex characters from listing | Sync; audited | 400 id/unreadable file; 404 changed/missing listing; 409 unavailable configured directory |

### endpoints

[Handlers and validators](../hyperloom/web/features/endpoints.py).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET endpoints` | Saved targets and cached probe results | None | Sync; no audit | 400 query |
| `GET endpoints/exposure` | Cached exposure verdicts and freshness | None | Sync; no audit | 400 query |
| `POST endpoints/save` | Create/replace an endpoint target (at most 32) | `label` printable 1..64; `host` string ≤64, private/loopback IP or configured machine id; `port` integer 1..65535; `scheme?` http/https (http); `id?` `[a-z0-9][a-z0-9-]{0,39}`; `key_name?` null/empty or `[a-z0-9-]{1,40}` | Sync; audited | 400 public/unresolved host, portal's own loopback port, duplicate destination, cap/fields |
| `POST endpoints/remove` | Remove target and derived probe state | Exactly `id`: string | Sync; audited | 400 body; 404 unknown target |
| `POST endpoints/check` | Force identification and exposure probe rounds now | Empty object | Sync; no audit | 400 nonempty body; probe failures reported in results |

### usb4

[Source](../hyperloom/web/features/usb4.py).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET usb4` | Cached link/layout/IRQ/journal status and exchange series | None | Sync; no audit | 400 query |
| `POST usb4/refresh` | Request or join a USB4 collection | Empty object | Sync; no audit | 400 nonempty body; 409 samplers absent |

### energy

[Handlers and validators](../hyperloom/web/features/energy.py). Energy is sensor-based estimation.

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET energy` | Energy buckets/totals, cost, efficiency and archive summary | `range?` 24h/7d/30d (24h) | Sync; no audit | 400 query; oversized/changing snapshot yields null + why |
| `GET energy/archive` | Paginated monthly energy totals | `before?` month YYYY-MM | Sync; no audit | 400 invalid/unknown query |
| `GET thermals` | Temperature history, sampled throttle periods and thresholds | `range?` day/week (day) | Sync; no audit | 400 query; excessive history yields null + why |
| `POST energy/price` | Save or clear electricity price | Exactly `price_per_kwh`: number (0,100], `currency`: three uppercase letters; or exactly `{clear:true}` | Sync; audited | 400 fields/range/currency |
| `POST energy/thresholds` | Replace temperature thresholds (Celsius) | Required `gpu`, optional `cpu`/`sensor`: `{warn,crit}` numbers 30..120, warn < crit; null optional groups skipped | Sync; audited | 400 missing GPU/invalid fields/order |
| `POST energy/reset` | Reset accumulated energy records | Exactly `{confirm:"reset"}` | Sync; audited | 400 confirmation/extra keys |

### health

[Rules and validators](../hyperloom/web/features/health.py). Rule-specific parameter names and numeric
bounds are returned by `GET health/rules` and enforced against that rule's definition.
`gpu-hot` accepts `warn_c`/`crit_c` 40..120 °C; `gpu-low-clock` accepts `busy_percent` 1..100
and `min_clock_mhz` 50..5000; `disk-full` accepts `percent`/`critical_percent` 50..100;
`route-state-stale.stale_s` is 5..3600; `usb4-below-rate.rated_gbps` is 1..200;
`node-tool-stale.max_age_s` is 60..86400. Other rules have no settable parameters.

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET health` | Active/pending findings, evaluation age and source availability | None | Sync; no audit | 400 query |
| `GET health/rules` | Rule descriptions, effective settings, defaults and parameter bounds | None | Sync; no audit | 400 query |
| `GET health/summary` | Severity counts, newest 20 findings and thermal banner | None | Sync; no audit | 400 query |
| `POST health/rules/set` | Merge override, or reset one rule to defaults | `rule` known id; `enabled?` boolean; `hold_s?`/`clear_s?` finite numbers 0..3600; `params?` object of rule-defined numeric bounds; `reset?` true overrides other settings | Sync; audited by handler | 400 fields/bounds or warning > critical; 404 unknown rule; 409 concurrent save conflict |

### alerts

[Handlers and URL validation](../hyperloom/web/features/alerts.py). URLs are stored separately; audit uses
masked URLs. HTTPS or numeric-loopback HTTP only, URL ≤2048 characters, no whitespace, credentials or fragment.

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET alerts` | Public channels, outbox counts, recent deliveries and choices | None | Sync; no audit | 400 query |
| `POST alerts/channels` | Create/update channel and catch-up bookkeeping | `id?` ch- + 8 lowercase hex; new: `name` printable 1..64, `kind` webhook/push, `url`; updates merge, kind immutable; `min_severity?` info/warning/critical (warning); `reminders?` none/1h/4h/12h/24h (none) | Sync; audited by handler | 400 URL/fields; 404 unknown id; 409 cap of 20 channels or repeated concurrent conflict |
| `POST alerts/channels/delete` | Delete channel, queued deliveries and URL versions | Exactly `id`: ch- + 8 lowercase hex | Sync; audited by handler | 400 id/fields; 404 unknown channel |
| `POST alerts/test` | Attempt one notification and record outcome | Exactly `id`: ch- + 8 lowercase hex | Sync; audited by handler | 400 id/fields; 404 unknown channel; 409 send busy; delivery failure returns ok:false |

### events

[Source and confirmation rules](../hyperloom/web/features/events.py).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET events` | Activity rows, pagination, filters and clear confirmation codes | `category?`/`machine?` `[A-Za-z0-9._:/-]{1,64}`; `severity?` info/warning/critical; `before?` positive integer id; `limit?` integer 1..200 (100) | Sync; no audit | 400 invalid/unknown query |
| `POST events/clear` | Delete selected old activity rows and rotate codes | `older_than`: 24h/7d/all; `confirm`: current unexpired string code from GET for that option | Sync; audited by handler | 400 fields/wrong, expired or used code; 409 repeated concurrent changes |

### bench

[Portal handlers](../hyperloom/web/features/bench.py), [job validators](../hyperloom/bench/),
[exactness and fidelity](replica-fidelity.md).
Run ids are 16 lowercase hex characters. Kinds are `decode`, `prefill`, `ab`, `quality`, `tools` and `demo`.
Common job arguments (except A/B): required `target` = `{profile: id}` or `{target: id}` from the listing;
`model?` string 1..200; `token_budget?` integer 1000..16000000 (400000); `trace?` boolean;
`thinking?` boolean only when declared; `sampling?` object `{temperature?,top_p?,seed?}` only when
honoured, with temperature (0,2], top_p (0,1], seed integer 0..2147483647 (defaults 0.7,1,1).
The normalizer also accepts but replaces `run_id`, `planned_tokens` and `target_digest`; clients do not choose them.
All jobs refuse a planned output above token_budget (400), unoffered targets (404), or a target blocker (409).
They freeze/recheck target identity, retain run details and poll cancellation during work.
All six use `bench` admission. A profile claims its configured machines, prefill machine and router;
an ad-hoc target claims its declared machines, `host:<host>` for `external`, or `*` for unknown placement.
A/B claims the union of both arms. Shared 409/503 admission rules above apply to each job.

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET bench/targets` | Running profile/ad-hoc targets and honoured capabilities | None | Sync; no audit | 400 query |
| `GET bench/guard` | Admission rules, holds, running runs and refusal preview | `target?` `profile:ID` or `target:ID` | Sync; no audit | 400 query/name; unknown target becomes preview why |
| `GET bench/corpus` | Prompt sets, quality categories, tool presets and limits | Query ignored | Sync; no audit | — |
| `GET bench/runs` | Stored run summaries | `kind?` known kind; `target?` `profile:ID` or `target:ID`, id `[A-Za-z0-9._-]{1,64}`; `limit?` integer 1..200 (50) | Sync; no audit | 400 invalid/unknown query |
| `GET bench/runs/<id>` | Run including detail and live progress | Run id; no query | Sync; no audit | 400 query/id; 404 absent run/store |
| `GET bench/compare` | Two runs side by side, paired quality statistics | Exactly `a` and `b`: run ids | Sync; no audit | 400 different kinds/quality suites or bad ids; 404 absent run/store |
| `GET bench/share/<id>` | Allow-listed run summary and share text | Run id; no query | Sync; no audit | 400 query/id; 404 absent run/store |
| `GET bench/receipt/<id>` | A/B exactness receipt | Run id; no query | Sync; no audit | 400 non-A/B/id/query; 404 absent run/receipt/store |
| `POST bench/targets/save` | Create/replace ad-hoc target (at most 64) | `id` `[a-z][a-z0-9-]{0,39}`, `scheme` http/https, `host` DNS name ≤253 or IP, `port` int 1..65535; `label?` 1..60 without ASCII 0..31 or angle brackets; `key?` secret name; `machines?` external or 1..32 configured ids; `sampling?`/`thinking?` bool; `context_tokens?` int 1024..4194304 | Sync; audited | 400 invalid target/unknown machines/cap |
| `POST bench/targets/remove` | Remove one ad-hoc target | Exactly `id`: string | Sync; audited | 400 body; 404 absent target |
| `POST bench/targets/capabilities` | Save profile thinking-switch declaration | Exactly `profile`: string and `thinking`: boolean | Sync; audited | 400 body; 404 unknown profile |
| `POST bench/runs/clear` | Clear all or selected run history | `confirm`: clear; `kind?` known kind; `run?` run id | Sync; audited | 400 fields/confirmation/kind/id |
| `POST bench/preview` | Equivalent CLI command; does not validate a run | Exactly `kind`: known kind and `arguments`: object (unknown inner keys ignored) | Sync; no audit | 400 unknown kind/body |
| `POST bench/decode` | Concurrency sweep with aggregate/per-stream rates and latency | Common job args; `prompt_set?` structured/prose/code/json; `levels?` 1..8 distinct integers 1..32; `max_tokens?` int 16..4096 (256) | Audited job; bench resources above; deadline 1800 s | 400 plan/fields; 404 target; 409 blocker/admission; 503 admission unavailable |
| `POST bench/prefill` | Calibrated prefill/TTFT sweep | Common job args (greedy only); `sizes?` 1..16 integers 1024..4194304; `max_context?` int 2048..4194304; `max_tokens?` int 1..64 (8); `trials?` int 1..5 (2) | Audited job; bench resources above; deadline 3600 s | 400 sampling/plan/fields; 404 target; 409 blocker/admission; 503 admission unavailable |
| `POST bench/ab` | Three A/B/A rounds, exactness receipt and speed delta | `arms` exactly `{a:target,b:target}`; `tokenizers?` a/b strings 1..200 without whitespace; `prompt_set?` structured/prose/code/json; `cases?` int 1..64 (8); `max_tokens?` int 8..1024 (64); `seed?` int 0..2147483647 (1); `token_budget?`/`trace?` as above | Audited job; bench union of arms; deadline 3600 s | 400 sampling/thinking/plan/fields; 404 target; 409 blocker/admission; 503 admission unavailable |
| `POST bench/quality` | Deterministic suite with category scores | Common job args; `categories?` nonempty list from corpus; `max_tokens?` int 16..4096; `concurrency?` int 1..8 (4) | Audited job; bench resources above; deadline 3600 s | 400 plan/fields; 404 target; 409 blocker/admission; 503 admission unavailable |
| `POST bench/tools` | Tool-call schema/argument scores and follow-up checks | Common job args; `preset?` short/regular/hard (short); `repeats?` int 1..5 (1); `max_tokens?` int 64..2048 (256) | Audited job; bench resources above; deadline 1800 s | 400 plan/fields; 404 target; 409 blocker/admission; 503 admission unavailable |
| `POST bench/demo` | Parallel live text tiles and measured rates | Common job args; `texts?` 1..32 strings 1..4000, else `prompt_set?` structured/prose/code/json and `count?` int 1..32 (8); `max_tokens?` int 16..2048 (512); serialized body ≤56 KiB | Audited job; bench resources above; deadline 900 s | 400 body/plan/fields; 404 target; 409 blocker/admission; 503 admission unavailable |

### planner

[Handlers and validators](../hyperloom/web/features/planner.py), [placement](placement-core.md),
[solver snapshots and layers](placement-solver.md).
Planner ids match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`; recipe ids follow the recipe-reference rules above.
Saved facts names cannot be Windows device names. POSTs share the recipe runner (2 running + 4 waiting):
503 busy, 504 after 150 s, 502 overflow/bad JSON/unexpected exit, 499 cancellation; exits 0/1 return
200 with `ok` and result/refusal, exit 2 is 400 and exit 3 is 404. POSTs have no registry audit flag,
including `pick` which writes a layer through the CLI.

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET planner` | Recipe profiles, saved facts, placement availability and limits | None | Sync; no audit | 400 query |
| `POST planner/whatif` | Evaluate variants alongside the base, no deployment | `recipe`; `profile?`/`facts?` ids; `vary?` object ≤8 paths to value lists; `variants?` ≤11 objects `{name?,set}` with name ≤64 one-line chars, set 1..256 paths; ≤12 total incl. base/cartesian variants; `prompt_tokens?` int 1..16777216 | Sync; no audit | 400 paths/values/variant cap; shared runner statuses above |
| `POST planner/placement` | Advisory placements for catalog model | `model` id; `facts?` id; `prompt?` int 1..1048576; `output?` int 1..65536; `slots?` int 1..64; `top_k?` int 1..10; `objective?` latency/decode/prefill/throughput | Sync; no audit | 400 fields/bounds; shared runner statuses above |

What-if paths match `[A-Za-z0-9_.-]{1,256}`. Values are finite numbers, booleans, one-line strings
≤1024 (no NUL), or recursively arrays ≤256; null and objects are refused. The CLI recipe reader
still checks settable paths, value types, layer names and snapshot applicability.

### turns

[Source](../hyperloom/web/features/turns.py); [lab operation](non-lab-mode.md).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET turns` | Collected node turns/leases, pair limit and RTX big-job ledger state | None | Sync; no audit | 400 query |
| `POST turns/settings` | Merge turn/ledger settings | `ledger_path?`: null/empty to clear, or host-OS absolute path ≤1024 without controls/.. (no Windows UNC); `ledger_cap_s?`/`pair_cap_s?`: integers 60..7200 | Sync; audited | 400 fields/path/bounds |

### library

[Handlers](../hyperloom/web/features/library.py), [root and verification rules](../hyperloom/downloads.py),
[model manifests and node jobs](model-downloads.md).
String arguments are nonempty, at most 1024 characters. Machine/job ids match
`[A-Za-z0-9][A-Za-z0-9_.-]{0,79}`; unknown machines return 404.
Library jobs take no shared admission/resources and no GPU turn; node guards pause disk work for live
leases/turns. Verification checks configured roots/manifests and cache-free I/O at execution time;
control-machine lab verification needs the RTX ledger guard. Those failures are recorded in the accepted job.

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET library` | Roots, last scans/verifications, recent jobs and ledger guard | None | Sync; no audit | 400 query |
| `POST library/roots` | Replace extra model roots for machine | Exactly `machine`: id, `roots`: array ≤16 absolute directory strings ≤1024 for machine OS, no controls/.. | Sync; audited | 400 path/list; 404 machine |
| `POST library/ledger-accept` | Accept current RTX ledger guard baseline | Empty object | Sync; audited | 400 extra keys/unconfigured, oversized or changed ledger; 503 I/O/timeout |
| `POST library/scan` | Scan selected/all machines and retain inventories | `machine?`: id | Audited job; no admission/hold; deadline 600 s | 400 fields; 404 machine; 409 another scan of machine within 600 s |

### logsearch

[Handlers and search limits](../hyperloom/web/features/logsearch.py).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET logsearch` | Searchable root ids, machine access, regex rules and limits | None | Sync; no audit | 400 query |
| `POST logsearch/query` | Bounded search with redacted matches, context and per-machine outcomes | Exactly one `text`/`regex`: printable 1..128; `ignore_case?` bool; `machines?` 1..64 string ids; `roots?` ≤64 string root ids; `window_s?` number 60..2592000 or `since?`/`until?` numbers 0..1e11; `process?` `[A-Za-z0-9._-]{1,64}`; `level?` any/warning/error; `context?` int 0..2; `max_results?` int 1..500 (200) | Sync; no audit; ≤20 s wait, per machine ≤5 s, 64 MiB/2000 files | 400 fields/no chosen roots; 404 machine; busy/node errors in machine outcomes |
| `POST logsearch/roots` | Replace configured searchable roots | Exactly `machine`: known id, `roots`: ≤16 absolute paths ≤1024 for machine OS, no controls/.. | Sync; audited | 400 root/system/credential directories or invalid paths; 404 machine; nodes recheck links |

Regex search forbids backreferences, lookarounds and quantified groups; matching is per line cut at 4 KiB,
in a deadline-limited worker process. Literal and regex text are never used as client-supplied file paths.

### ops

[Handlers and validators](../hyperloom/web/features/ops.py); [configuration schema](SPEC.md).
Machine edits use `op` add/edit/remove/move; `id` identifies an existing machine for edit/remove/move;
`machine` is the complete schema-valid entry for add/edit (serialized size ≤16384 characters);
`index` is an integer 0..machine-count for add or 0..machine-count−1 for move.
The resulting configuration must pass the real schema and fit 256 KiB. Identity/removal is frozen while
in use or occupancy cannot be verified; saving checks authoritative state and the reviewed version again.
The four GET handlers ignore query keys.

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET ops/nodes` | Expected/deployed node tool digests, profiles and update blockers | Query ignored | Sync; no audit | Unavailable state is explained |
| `GET ops/power` | Wake/shutdown eligibility and profile state | Query ignored | Sync; no audit | Unavailable state is explained |
| `GET ops/config` | Raw machine entries, version, pinned keys and frozen identities | Query ignored | Sync; no audit | Unreadable config yields null + why |
| `GET ops/collectors` | Collector states, effective/default/bounded intervals | Query ignored | Sync; no audit | Unavailable samplers are explained |
| `POST ops/wake` | Send UDP magic packets | Exactly `machine`: known id | Sync; audited | 400 body; 404 machine; 409 no MAC/wake blocker |
| `POST ops/hostkey/scan` | SSH keyscan candidates for manual comparison | Exactly `machine`: known SSH id | Sync; no audit | 400 non-SSH/body; 404 machine |
| `POST ops/config/validate` | Validate proposed machine edit, returns valid/error | Machine edit keys above, optional `version` | Sync; no audit | 400 malformed outer edit; edit/schema/occupancy refusals returned valid:false |
| `POST ops/config/save` | Version-check and atomically save one machine edit | Machine edit keys above, required `version`: string from GET | Sync; audited; core hold on edited machine (or *) | 400 schema/size/fields; 404 machine; 409 version/in-use/unknown occupancy/job/admission/lock |
| `POST ops/machine-extras` | Merge machine display/WOL metadata | `machine` id; `mac?` valid MAC or null/empty; `wol_broadcast?` private IPv4 or null/empty; `hide_disks?`/`hide_interfaces?` ≤32 nonempty strings ≤128 without controls | Sync; audited | 400 values/fields; 404 machine |
| `POST telemetry/refresh` | Wake collector or join an in-flight collection | Exactly `collector`: registered collector name | Sync; no audit | 400 fields; 404 unknown collector; 409 samplers absent |
| `POST telemetry/interval` | Persist interval, apply now if sampler exists | Exactly `collector` and `seconds`: finite number in max(1,default/2)..min(3600,max(60,default×30)) | Sync; audited | 400 fields/bounds; 404 unknown collector |
| `POST ops/nodes-update` | Update node tools serially without force; report updated/refused/skipped | `profile`: configured string id; `machine?`: member id | Audited exclusive job; core hold on selected/all profile members; deadline 900 s | 400 fields/nonmember; 404 profile; 409 slot/resources; live leases/unknown state fail job |
| `POST ops/shutdown` | Run configured shutdown helper after occupancy check | Exactly `machine`: SSH id and `confirm`: same id | Audited exclusive job; core hold on machine; deadline 90 s (helper 30 s) | 400 confirmation; 404 machine; 409 local/non-SSH/helper/slot/resources; occupancy rechecked in job |
| `POST ops/machine-test` | SSH/node tool/GPU/engine/USB4 pass/fail/skipped report | Exactly `machine`: known id | Audited job; no admission/hold; deadline 180 s | 400 fields; 404 machine; unpinned SSH yields skipped checks |

A node update already in flight cannot be interrupted. A new machine is only started with ≥180 s
remaining; this is a start threshold, not a completion guarantee. Its hold stays until cleanup finishes.

### integrations

[Handlers, export allow-list and MQTT validator](../hyperloom/web/features/integrations.py),
[Prometheus integration](prometheus.md).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET integrations` | Public MQTT settings/state, stream limits and available exports | None | Sync; no audit | 400 query |
| `GET integrations/export/<id>` | CSV text in JSON envelope with columns/rows/filename | Suffix devices.csv, stats.csv, ledger.csv, energy.csv, bench/runs.csv or history/<series>.csv; `share?` 0/1; source query keys below | Sync; no audit | 404 unsupported/unavailable source; 400 query, >50000 rows or >8 MiB CSV |
| `GET stream` | Allow-listed changing devices/profiles/stats/overview projections via SSE | None; GET only, ≤4 clients, every 2 s, heartbeat 15 s, reconnect after 1800 s | Streaming read; no audit | 400 query; 405 HEAD; 503 client cap; >1 MiB snapshot emits error event |
| `POST integrations/mqtt/save` | Merge settings and reconnect publisher on new revision | `enabled?`/`tls?` bool; `host?` null or DNS/IPv4 name ≤253; `port?` null or int 1..65535; `topic_prefix?` 1..4 segments each 1..32 alnum/_/-; `username?` null or 1..128 no NUL; `client_id?` null or 1..21 alnum/_/-; `interval_s?` int 5..300; `password?` 1..512 no NUL; `clear_password?` bool | Sync; audited by handler, password masked | 400 invalid settings, enabled without host, password without user, password + clear; storage error mapped 400 |
| `POST integrations/mqtt/test` | One saved-broker connection and QoS 1 non-retained test publish | Empty object | Sync; audited by handler after attempt | 400 body; 409 no broker; connection failure returns ok:false |

Export `devices`/`stats` accept only `share`; `ledger`/`energy` add `range` (export syntax:
1..8 digits, `all`, or 1..3 digits + h/d; source enforces its actual allowed ranges).
`bench/runs` adds `kind` lowercase 1..16, `target` alnum/dot/underscore/hyphen 1..64 and `limit`
1..3 digits, then source validation applies. `history/<series>` adds `range` digits/dot ≤12,
`start`/`end` digits/dot ≤16, `tier` raw/m1/m15 and `l.<label>` filters as in history.
CSV cells are formula-protected; share mode projects only the source's explicit share columns.
MQTT passwords require a username; omitting password leaves it unchanged, `clear_password:true` removes it.

### prefs

[Defaults, choices and validators](../hyperloom/web/features/prefs.py).

| API | Returns / does | Arguments | Execution / audit | Notable refusals |
|---|---|---|---|---|
| `GET prefs` | Effective/default preferences, saved flags, zones and related settings | None | Sync; no audit | 400 query |
| `GET prefs/timezones` | Accepted IANA zone names, or null if database absent | None | Sync; no audit | 400 query |
| `POST prefs/save` | Merge/save preferences (default values remove stored overrides) | Nonempty subset: `refresh_s` int 1/2/5/10/30; `temperature_unit` C/F; `timezone` local/UTC/accepted IANA string ≤64; `language` en/es; `theme` system/light/dark/black; `density` comfortable/compact; `hidden_panels` ≤64 ids `[a-z][a-z0-9-]{0,40}` | Sync; audited | 400 fields/choice/unknown zone; settings cannot be hidden; without zone DB only local/UTC |
