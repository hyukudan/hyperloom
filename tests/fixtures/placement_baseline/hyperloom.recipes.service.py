# Frozen function sources from main 8539c28; executed with the current module globals.

def deploy(digest, stop=(), *, source=None, orchestrate=None, approval=None, job=None):
    """The switch as one operation: launch exactly the planned snapshot ``digest`` after stopping the reviewed running
    profiles ``stop``. Everything that can refuse deterministically is checked BEFORE anything is stopped: the snapshot
    (manifest digest and every file), the running set (must equal ``stop``), the profile/node/router checks and the
    recipe decision + RTX plan on fresh facts. Then each listed profile is stopped and `up` runs on the snapshot (its
    configuration copy, its launch intent).

    ``job``: the portal's job id. Its cancel marker stops the deploy before the next step; up checks it once more right
    after it owns the profile (the hand-over) and then starts nothing. The progress file says how far the deploy got,
    so a cancel undoes only what the deploy did (a cancelled deploy's error says what it had stopped)."""
    from .. import orchestrate as default_orchestrate
    orchestrate = orchestrate or default_orchestrate
    with launch_progress(job, "deploy") as progress:
        return _deploy(digest, stop, source, orchestrate, approval, progress)


def _deploy_pinned(digest, stop, source, orchestrate, approval, progress):
    from .. import orchestrate as default_orchestrate
    from . import commands, launch
    check = progress.check
    cluster, intent, options = options_from_inputs(digest)
    profile = intent["profile"]
    progress.record(profile=profile)
    # The launch arguments' own checks (deadline, approval, queue timeout) before anything is stopped.
    default_orchestrate._approval(intent["deadline_s"], approval)
    if type(intent["queue_timeout_s"]) is not int or not 60 <= intent["queue_timeout_s"] <= default_orchestrate.MAX_QUEUE_S:
        raise RecipeError("the snapshot's queue timeout is out of range")
    running = sorted(_states())
    if sorted(set(stop)) != running:
        raise Conflict(f"the running profiles are {', '.join(running) or 'none'}, not the reviewed "
                       f"{', '.join(sorted(set(stop))) or 'none'}: review the switch again")
    if profile not in running and default_orchestrate._state_path(profile).exists():
        # Before anything is stopped: up would refuse this profile's saved state of another configuration (or one
        # without the record) only after the switch had stopped the reviewed profiles.
        try:
            kind = default_orchestrate.ownership(default_orchestrate._read(default_orchestrate._state_path(profile)),
                                                 config_file())
        except (OSError, ValueError):
            kind = "unreadable"
        if kind == "unknown":
            raise Conflict(f"{profile} has a saved launch state of an unknown configuration (launched before launch "
                           "states recorded it): stop it first (it is listed and can be stopped here)")
        raise Conflict(f"{profile} has a saved launch state of another configuration: stop it there first")
    orchestrate.check_launch(cluster, profile, intent["router"], recipe=True)
    check()
    with tempfile.TemporaryDirectory(prefix="hl-recipe-deploy-") as directory:
        launch.prepare(cluster, profile, options, router=intent["router"], vram_cap_gib=None, state_dir=directory,
                       source=source or commands.live_source(cluster))
    stopped = []
    for name in sorted(set(stop)):
        progress.before_stop(name)
        stopped.append({"profile": name, "down": orchestrate.down(name)})
        progress.record(stopping=None, stopped=progress.state["stopped"] + [name],
                        stop_states=dict(progress.state.get("stop_states") or {},
                                         **{name: stop_outcome(stopped[-1]["down"])}))
    check()
    # The hand-over is checked again by up right after it takes ownership of the profile (no gap: a later cancel
    # sees "launching" and stops the launching profile through its own stop marker).
    try:
        result = orchestrate.up(cluster, profile, router=intent["router"], deadline_s=intent["deadline_s"],
                                queue_timeout_s=intent["queue_timeout_s"], approval=approval, recipe=options,
                                cancelled=progress.handoff)
    except (default_orchestrate.OrchestrationError, OSError, ValueError) as exc:
        if not progress.cancelled():
            raise
        if progress.handoff_refused or progress.state["phase"] != "launching":
            raise RecipeError("deploy cancelled at the launch hand-over: nothing was started; "
                              + _had(progress.state)) from exc
        raise RecipeError(f"deploy cancelled after the launch began: the cancel stops {profile} (its answer says how); "
                          + _had(progress.state)) from exc
    return {"inputs": digest, "profile": profile, "stopped": stopped, "up": result}
