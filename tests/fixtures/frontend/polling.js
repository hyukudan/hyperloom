"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("hyperloom/web/static/app.js", "utf8");
const poll = source.slice(source.indexOf("  async function refresh()"), source.indexOf("  function fetchDocument("));
const gate = source.match(/  const mutationReason = .*;/)[0];
const flush = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  const state = {generation: 0, job: {job: "abc", action: "up", state: "succeeded"},
    fresh: false, docs: {"launch/abc": {old: true}}, fetched: {}, errors: {}, warnings: new Set()};
  let scenario = "missing", jobs = [], activeId = null, calls = [];
  const context = {state, AbortController, setTimeout, clearTimeout, Date, console,
    document: {hidden: false}, navigator: {onLine: true}, inFlight: null, pollAbort: null,
    endpoints: ["profiles"], pageEndpoints: () => [], route: () => "engines", prefsSync: {epoch: 0},
    sequence: () => ({}), OPTIONAL: [], rows: value => value || [], documentsAged: () => false,
    machines: () => [], render: () => {}, renderConnection: () => {}, refreshOptional: () => {}, announce: () => {},
    activeJob: () => state.job?.state === "running",
    fetchDocument: async name => {
      calls.push(name);
      if (name.startsWith("launch")) {
        if (scenario === "hanging") return new Promise(() => {});
        if (scenario === "missing") throw Object.assign(new Error("unknown launch job"), {status:404});
        if (scenario === "other404") throw Object.assign(new Error("route unavailable"), {status:404});
        if (scenario === "error") throw new Error("timeline failure");
        return {launches: []};
      }
      return name === "jobs" ? {jobs, active: activeId} : {};
    }};
  vm.createContext(context);
  vm.runInContext(poll + gate + "\nglobalThis.poll = refresh; globalThis.reason = mutationReason;", context);
  return {state, context, calls, mode: value => {scenario = value;}, jobs: value => {jobs = value;}, active: value => {activeId = value;}};
}
(async () => {
  // Backend restart: no jobs, old launch 404, successive authoritative polls remain usable.
  const restart = harness();
  await restart.context.poll(); await flush();
  assert.equal(restart.state.job, null);
  assert.equal(restart.state.docs["launch/abc"], undefined);
  await restart.context.poll(); await flush();
  assert.equal(restart.state.fresh, true);
  assert.equal(restart.context.reason(), "");
  assert.equal(restart.calls.filter(n => n === "launch/abc").length, 1);
  assert.ok(!restart.state.current.some(n => n.startsWith("launch")));
  // A stale jobs roster racing the 404 must not resurrect the forgotten reference.
  const staleRoster = harness(); staleRoster.jobs([staleRoster.state.job]);
  await staleRoster.context.poll(); await flush();
  await staleRoster.context.poll(); await flush();
  assert.equal(staleRoster.state.job, null);
  assert.equal(staleRoster.context.reason(), "");
  // Eviction: the portal forgot the job, so its launch document answers 404 and the reference is dropped.
  const eviction = harness();
  eviction.jobs([{job: "def", action: "down", state: "succeeded"}]);
  await eviction.context.poll(); await flush();
  await eviction.context.poll(); await flush();
  assert.equal(eviction.state.job, null);
  assert.equal(eviction.context.reason(), "");
  // A roster requested before a mutation was accepted (here naming the previous job as active) arrives after the
  // POST: the accepted job is kept.
  const accepted = harness(); accepted.mode("ok");
  accepted.jobs([{job: "old1", action: "up", state: "running"}]); accepted.active("old1");
  const pending = accepted.context.poll();
  accepted.state.job = {job: "new1", action: "up", state: "running"}; accepted.state.accepted = 1;
  await pending; await flush();
  assert.equal(accepted.state.job?.job, "new1");
  assert.equal(accepted.state.job?.state, "running");
  // The next roster is newer than the acceptance and lists the job: it refreshes it.
  accepted.jobs([{job: "new1", action: "down", state: "succeeded"}]); accepted.active(null);
  await accepted.context.poll(); await flush();
  assert.equal(accepted.state.job?.state, "succeeded");
  // Portal restart while a non-launch job ran: a roster newer than every acceptance forgets it, actions are usable.
  const restartDown = harness(); restartDown.mode("ok");
  restartDown.state.job = {job: "d1", action: "down", state: "running"}; restartDown.jobs([]);
  await restartDown.context.poll(); await flush();
  assert.equal(restartDown.state.job, null);
  assert.equal(restartDown.context.reason(), "");
  // Timeline failure: ordinary actions use fresh core documents while unavailable is disclosed.
  const failure = harness(); failure.mode("error");
  failure.jobs([failure.state.job]);
  await failure.context.poll(); await flush();
  assert.equal(failure.state.fresh, true);
  assert.equal(failure.context.reason(), "");
  assert.equal(failure.state.launchErrors["launch/abc"], "Launch timeline unavailable");
  assert.equal(failure.state.docs["launch/abc"], undefined);
  // Generic errors and unrelated 404s preserve a RUNNING launch and its Cancel/Stop controls.
  for (const mode of ["other404", "error"]) {
    const running = harness(); running.mode(mode);
    running.state.job.state = "running"; running.jobs([running.state.job]);
    await running.context.poll(); await flush();
    assert.equal(running.state.job?.state, "running");
    assert.equal(running.state.launchErrors["launch/abc"], "Launch timeline unavailable");
    assert.ok(!running.state.expiredLaunches?.has("abc"));
    const panel = {hidden: true, innerHTML: ""};
    Object.assign(running.context, {$: () => panel, esc: String, statusPill: String,
      display: String, button: (label, action) => `<button data-action="${action}">${label}</button>`,
      window: {HyperloomLaunch: {render: () => ""}}});
    vm.runInContext(source.slice(source.indexOf("  function renderJob()"), source.indexOf("  async function request(")) +
      "\nrenderJob();", running.context);
    assert.equal(panel.hidden, false);
    assert.ok(panel.innerHTML.includes('data-action="cancel"'));
    assert.ok(panel.innerHTML.includes('data-action="stop"'));
  }
  // A timeline promise that never settles must not delay authoritative polling either.
  const hanging = harness(); hanging.mode("hanging"); hanging.jobs([hanging.state.job]);
  await hanging.context.poll();
  assert.equal(hanging.state.fresh, true);
  assert.equal(hanging.context.reason(), "");
  // Real timeline renderer retains ETA while roster is incomplete and labels requested stops.
  const window = {};
  vm.runInNewContext(fs.readFileSync("hyperloom/web/static/pages/launch.js", "utf8"), {window});
  const html = window.HyperloomLaunch.render({job_state: "running", elapsed_s: 20,
    eta: {n: 3, seconds: 100}, phases: [{id: "ready", label: "All READY", state: "pending"},
      {id:"stopping", label:"Stop previous profiles", state:"running", detail:{stopped:[], requested:["old"]}}]});
  assert.ok(html.includes("Estimated remaining 80 s"));
  assert.ok(html.includes("Stop requested: old"));
  assert.ok(!html.includes("Stopped: old"));
  console.log("PASS restart, eviction, failed/hanging timeline, ETA and requested stop rendering");
})().catch(error => { console.error(error); process.exitCode = 1; });
