# Hyperloom portal API

The portal (`hyperloom ui`) serves the web UI and a JSON API on `http://127.0.0.1:18700/hyperloom/api/`. It only
listens on loopback. This page documents the API, with the command line that does the same thing next to each call.
The shapes of the examples are exact; their values are illustrative (a test fixture), and long fields are trimmed.

- [Access and authentication](#access-and-authentication)
- [Recipes and profiles](#recipes-and-profiles): list, show, validate, explain, check, diff, select, pick, plan, save,
  delete, layers, receipts, probe, deploy, `up` with a recipe, the running deployment and its route statistics
- [Other endpoints](#other-endpoints): the screen documents, `up`/`down`/`install`/`doctor`/`hosts/pin`/
  `keys/rotate`/`cancel`, jobs

## Access and authentication

| | rule |
|---|---|
| Listener | `127.0.0.1` only. `Host` must be `127.0.0.1:<port>` or `localhost:<port>`. |
| Reads | `GET` (and `HEAD`). No body, no token. |
| Everything else | `POST` with `Content-Type: application/json`, a JSON object body, `Origin: http://<the Host>`, and the header `X-Hyperloom-CSRF: <token>`. |
| CSRF token | Random per portal start, embedded in the page: `<meta name="hyperloom-csrf" content="...">` of `GET /`. A script reads it from there. |
| Body size | 64 KiB; 1 MiB for `recipes/save`, `recipes/check` and `recipes/save-layer` (they carry recipe text, at most 256 KiB). |
| JSON | Strict: duplicate keys, `NaN`/`Infinity` and non-objects are refused (400). |
| Audit | Every mutation, recipe write and job is appended to `audit.jsonl` next to the cluster configuration. Recipe and layer text is recorded as its size and sha256, not copied. |

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
| `GET recipes[?model=M]` | `hyperloom recipe list [--model M]` | the catalog, models and the deployment profiles serving each, layers, receipts, saved facts |
| `GET recipes/<id>` | `hyperloom recipe show ID` | one recipe: text, structure, every settable field with its type, range and default |
| `POST recipes/ready` `{recipe, profile?, facts?}` | `hyperloom recipe ready ID [P] [--facts F]` | what is still missing before `up` can run the recipe on the profile, step by step, each with the next command (offline) |
| `POST recipes/export` `{recipe, layer?}` | `hyperloom recipe export ID [--layer NAME]...` | the recipe and those user layers as one data-only bundle |
| `POST recipes/import` `{text, id?, replace?}` | `hyperloom recipe import - [--id ID] [--replace SHA256]` | write a bundle's recipe and layers into the user directories (checked first; audited) |
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
| `POST recipes/probe` (job) | `hyperloom recipe probe PROFILE [--disks] [--descriptors] [--evidence N=R] [--out F]` | the hardware facts; the flags read model files or store evidence |
| `POST recipes/deploy` (job) | `hyperloom recipe deploy DIGEST [--stop P ...]` | stop the reviewed profiles and launch exactly a planned snapshot |
| `POST up` with `recipe` (job) | `hyperloom up [PROFILE] --recipe ID` | launch a model with a recipe |
| `GET recipes/active[?profile=P]` | `hyperloom recipe active [P]` | what each running profile was launched with, as launched |
| `GET recipes/routes[?profile=P]` | `hyperloom recipe routes [P]` | per-route and per-request routing of the running recipe (RTX vs nodes) |

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

| API | CLI |
|---|---|
| `GET devices`, `topology`, `models`, `profiles`, `stats`, `logs`, `settings` | screen documents (`hyperloom/ui-<screen>-v1`, UI-SPEC §9) |
| `POST up {profile, router, node, device, deadline, approval_*}` | `hyperloom up PROFILE ...` (Qwen replica profiles; any profile with a recipe, above) |
| `POST down {profile, force}` | `hyperloom down PROFILE [--force]` |
| `POST install {profile, build_on}` | `hyperloom install PROFILE [--build-on M]` |
| `POST doctor {machine}` | `hyperloom doctor` |
| `POST hosts/pin {machine, fingerprint}` | `hyperloom hosts pin` |
| `POST keys/rotate {name}` | `hyperloom keys rotate NAME` |
| `POST cancel {job | profile}` | `job`: cancels a running `up` (with a recipe or without) or `recipes/deploy`, as above; `profile`: `hyperloom down PROFILE` |
| `GET jobs`, `GET jobs/<id>` | job progress and results |

Mutations answer `202` with a job (`job`, `state`, `action`, `arguments`, `result`, `error`). One mutating job runs at a
time; `down` and `cancel` never wait. `up`, `down`, `install` and `recipes/deploy` run as their own CLI process, which
finishes even if the portal stops.

Every mutation runs on the portal's own configuration: a `config` argument naming another file is refused (400),
for `up`, `install`, `doctor` and `hosts/pin` alike.

`cancel {job}` of a plain `up` works as for an `up` with a recipe (the job runs `up --job <id>`): before `up` owns the
profile, only its marker (it starts nothing; a profile that was already running, which `up` refuses, is never
stopped); once it owns it, the cancel stops it. A `down` (or a cancel by profile) of the profile an `up` job launches
writes that job's marker too.
