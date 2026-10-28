# Frozen function sources from main 8539c28; executed with the current module globals.

def evaluate(cluster, profile, path, layers, source, backend=None):
    """``source(ctx)`` -> (inventory, facts, raw availability, notes): live probes or a saved facts file. One resolved
    document (the backend's) is used for binding, match, admission and the decision."""
    backend = backend or core.StubBackend()
    ctx = facts.context(cluster, profile)
    base = resolve(path, Layers(), backend=backend).recipe   # structure only: binding never depends on layers
    inventory, gathered, raw, notes = source(ctx)
    ctx.limits = dict(ctx.limits or {}, node_runtime=facts.current_runtime(cluster, ctx, gathered.get("node_runtime")))
    bound = apply.bind(base, ctx, inventory)
    report = apply.probe_report(base, bound, gathered)
    identity = dict(report, binding=bound["binding"], workload=layers.workload,
                    descriptors=apply.descriptors(bound, ctx))
    resolved = resolve(path, layers, identity, backend)
    recipe = resolved.recipe
    if isinstance(backend, core.CoreBackend):
        problems, binding, effective_sha = backend.match(path, layers, report, identity)
        if not problems and binding != bound["binding"]:
            problems = [f"the core bound {binding}, hyperloom launches {bound['binding']}"]
    else:
        problems = apply.match(recipe, report, bound)
        effective_sha = backend.effective_sha256(path, layers, resolved, report)
    availability = facts.availability(recipe, bound, raw)
    decision, refusal = None, None
    if not problems:
        def select(state):
            return backend.select(path, layers, resolved, state, effective_sha, identity) \
                if isinstance(backend, core.CoreBackend) else reader.select(recipe, state, effective_sha)
        try:
            decision = apply.deploy(recipe, ctx, bound, availability, effective_sha, select=select)
        except RecipeError as exc:
            refusal = str(exc)
    return Evaluation(str(path), backend.label, resolved, effective_sha or "", ctx, bound, report, problems,
                      availability, decision, refusal, list(notes))


def pick(cluster, profile, directories, layers, source, backend=None):
    """Auto-pick: candidates for the profile's model that match and deploy; the most specific wins, ties by name.
    Returns (chosen Evaluation or None, rows for every candidate with why it was or was not chosen)."""
    ctx = facts.context(cluster, profile)
    cached = {}

    def once(c):
        if "v" not in cached:
            cached["v"] = source(c)
        return cached["v"]
    rows, passing = [], []
    for row in candidates(directories):
        out = {"path": row["path"], "name": row["name"]}
        if row["error"]:
            out["result"] = "invalid: " + row["error"]
        elif row["model"] != ctx.model:
            out["result"] = f'other model ("{row["model"]}")'
        else:
            try:
                evaluation = evaluate(cluster, profile, row["path"], layers, once, backend)
            except RecipeError as exc:
                out["result"] = "refused: " + str(exc)
            else:
                if evaluation.ok:
                    out["result"] = "passes"
                    out["specificity"] = specificity(evaluation.effective.recipe)
                    passing.append((-out["specificity"], row["name"], evaluation))
                else:
                    out["result"] = "refused: " + "; ".join(evaluation.problems or [evaluation.refusal])
        rows.append(out)
    if not passing:
        return None, rows
    passing.sort(key=lambda item: (item[0], item[1]))
    return passing[0][2], rows
