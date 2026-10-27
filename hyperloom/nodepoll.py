"""The portal's background node reads: one combined read-only ``poll`` session per node and cycle.

Every background reader of an ssh node (the fleet's host reading, USB4, the node tool check, the inventory's doctor
and gpu, launch status, process logs and the GPU turn check) names what it wants here (a verb and its payload,
under a key) and takes the latest answer; it never opens an ssh session itself. One thread per portal reads the
nodes with the node tool's ``poll`` verb, which runs all the wanted read-only verbs in one session and one Python
process on the node:

- while a page of this portal is open (an API GET or the live stream within ``PAGE_S``): one poll of everything
  wanted, at most once per ``CYCLE_S`` (30 s), with the turn check of a lab node in the same session;
- with no page open: only the turn check of a lab node, once per ``CYCLE_S``; any other node is not read;
- while the latest turn reading shows a GPU turn holder: only the turn check, once per ``TURN_CHECK_S`` (60 s),
  until a reading shows the GPU free.

So a node sees at most two background sessions a minute from a portal. The thread starts with the portal, whatever
the history store does (``HYPERLOOM_TELEMETRY=0`` or a store that failed to open): the turn check always runs. A
poll takes the node gate now or not at all (a node busy with another call is tried again at the next tick). What a
person starts (doctor, up, down, a job, a logs search) is not a poll and does not come here.
"""

import hashlib
import json
import threading
import time

from hyperloom import nodegate, pollbudget, remote


CYCLE_S = pollbudget.CYCLE_S
TURN_CHECK_S = pollbudget.TURN_CHECK_S
PAGE_S = 45.0               # a page polls the API every few seconds; one quiet for this long is closed
WANT_S = 2 * CYCLE_S + 15   # a want not renewed for this long is dropped (its reader stopped asking)
TICK_S = 1.0
CALL_TIMEOUT_S = 40.0       # the node's poll stops starting parts after 20 s; ssh start and transfer on top
TURNS_KEY = "turns"
LOCAL = "@local"            # the bookkeeping slot of the local lab machines' turn check
MAX_PARTS = 32              # the node tool's POLL_MAX_PARTS
NOT_YET = "not read yet: a node is read once per 30 s while a portal page is open"


class NodePoll:
    def __init__(self, api, clock=time.time, call=None):
        self.api, self.clock = api, clock
        self._call = call or self._remote_call
        self._lock = threading.Lock()
        self._wants = {}        # machine -> {key: (verb, payload, asked at)}
        self._answers = {}      # machine -> {key: (answer or None, error or None, read at)}
        self._taken = {}        # (reader, machine, key) -> read at of the answer the reader last took
        self._last = {}         # machine -> when its last poll started
        self._busy = set()      # machines with a poll in flight
        self._page = None       # last page activity
        self._stop = threading.Event()
        self._thread = None
        self.sessions = []      # (machine, started at, keys) of recent polls (bounded; tests and diagnostics)

    # -- readers -----------------------------------------------------------------------------------------------
    def page_seen(self, now=None):
        with self._lock:
            self._page = self.clock() if now is None else now

    def page_open(self, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            return self._page is not None and 0 <= now - self._page < PAGE_S

    def paused(self, machine_id, now=None):
        """Why a node's readings wait (a GPU turn holds it), or None."""
        held = pollbudget.for_api(self.api).turn(machine_id, now=now)
        if held is None:
            return None
        return f"paused: a GPU turn holds {machine_id} ({held[0]}); node reads wait for its end"

    def read(self, machine_id, key, verb, payload, now=None, reader=None):
        """Ask for ``verb`` with ``payload`` under ``key`` at the node's next poll and return the latest answer:
        (answer, error, read at); answer None and error the reason when there is none (not read yet, or a GPU turn
        holds the node). With ``reader``, an answer this reader already took is not returned again."""
        now = self.clock() if now is None else now
        with self._lock:
            self._wants.setdefault(machine_id, {})[key] = (verb, dict(payload or {}), now)
        paused = self.paused(machine_id, now)
        if paused is not None:
            return None, paused, None
        with self._lock:
            got = self._answers.get(machine_id, {}).get(key)
            if got is None:
                return None, NOT_YET, None
            if reader is not None:
                if self._taken.get((reader, machine_id, key)) == got[2]:
                    return None, "no new node reading since the last round (once per 30 s)", None
                self._taken[(reader, machine_id, key)] = got[2]
            return got

    # -- the poll ----------------------------------------------------------------------------------------------
    def _machines(self):
        from hyperloom.web.features import turns
        try:
            cluster = self.api.cluster()
        except Exception:  # noqa: BLE001 - an unreadable configuration: nothing to read
            return [], set()
        eligible = turns.eligible(cluster)[0]
        self._local_lab = [m.id for m in eligible if m.access == "local"]
        return [m for m in cluster.machines.values() if m.access == "ssh"], {m.id for m in eligible}

    def plan(self, machine, lab, now):
        """The parts of this node's poll now ([{key, verb, payload}]), or None when none is due."""
        turn = [{"key": TURNS_KEY, "verb": "turns", "payload": {"lab_dir": machine.lab_dir}}] if lab else []
        with self._lock:
            last = self._last.get(machine.id)
            wants = self._wants.get(machine.id, {})
            for key in [key for key, want in wants.items() if not 0 <= now - want[2] < WANT_S]:
                del wants[key]
            # The most recently asked first, as many as one poll takes (the node refuses more than MAX_PARTS).
            recent = sorted((item for item in wants.items() if item[0] != TURNS_KEY), key=lambda item: -item[1][2])
            wanted = [{"key": key, "verb": verb, "payload": payload}
                      for key, (verb, payload, _) in recent[:MAX_PARTS - 1]]
            page = self._page is not None and 0 <= now - self._page < PAGE_S
        if pollbudget.for_api(self.api).turn(machine.id, now=now) is not None:
            parts, every = turn, TURN_CHECK_S
        elif page and wanted:
            parts, every = turn + wanted, CYCLE_S
        else:
            parts, every = turn, CYCLE_S
        if not parts or (last is not None and 0 <= now - last < every):
            return None
        return parts

    def tick(self, now=None):
        """Start the polls due now (one worker per node; a node with a poll in flight is left alone)."""
        now = self.clock() if now is None else now
        machines, lab = self._machines()
        started = []
        local = getattr(self, "_local_lab", [])
        with self._lock:
            last = self._last.get(LOCAL)
            due = local and LOCAL not in self._busy and (last is None or not 0 <= now - last < CYCLE_S)
            if due:
                self._busy.add(LOCAL)
                self._last[LOCAL] = now
        if due:
            # A local lab machine's turn check (no ssh): the turns reader itself, on this thread's cadence.
            worker = threading.Thread(target=self._local_turns, args=(local,), daemon=True,
                                      name="hyperloom-node-poll-local")
            try:
                worker.start()
                started.append(worker)
            except Exception:  # noqa: BLE001 - no worker: the slot is given back, the next tick tries again
                with self._lock:
                    self._busy.discard(LOCAL)
                    self._last.pop(LOCAL, None)
        for machine in machines:
            with self._lock:
                if machine.id in self._busy:
                    continue
            parts = self.plan(machine, machine.id in lab, now)
            if parts is None:
                continue
            release = nodegate.try_hold(self.api.config_path, machine.id)
            if release is None:
                continue                      # another call holds the node: the next tick tries again
            with self._lock:
                self._busy.add(machine.id)
                self._last[machine.id] = now
                self.sessions = (self.sessions + [(machine.id, now, [part["key"] for part in parts])])[-256:]
            worker = threading.Thread(target=self._poll, args=(machine, parts, release), daemon=True,
                                      name="hyperloom-node-poll-" + machine.id)
            try:
                worker.start()
            except Exception:  # noqa: BLE001 - no worker: the gate is given back, the next tick tries again
                release()
                with self._lock:
                    self._busy.discard(machine.id)
                continue
            started.append(worker)
        return started

    def _local_turns(self, machine_ids):
        from hyperloom.web.features import turns
        try:
            turns.fetch(self.api, machines=machine_ids, clock=self.clock, skip_busy=True)
        except Exception:  # noqa: BLE001 - a failed local read is that machine's reading (kept by turns)
            pass
        finally:
            with self._lock:
                self._busy.discard(LOCAL)

    def _remote_call(self, machine, payload):
        client = self.api.remote.Remote(machine, self.api.orchestrate._known_hosts())
        return client.call("poll", payload, timeout=CALL_TIMEOUT_S)

    def _poll(self, machine, parts, release):
        sent = self.clock()
        try:
            try:
                reply = self._call(machine, {"parts": parts})
                if not isinstance(reply, dict) or reply.get("schema") != "hyperloom/node-v1":
                    raise ValueError("unexpected reply from the node tool")
                if reply.get("error"):
                    raise ValueError(str(reply["error"]))
                answers, error = reply.get("parts") if isinstance(reply.get("parts"), dict) else {}, None
            except Exception as exc:  # noqa: BLE001 - every failure is this node's reading, never the thread's
                answers, error = {}, f"{type(exc).__name__}: {exc}"[:300]
        finally:
            release()
        received = self.clock()
        with self._lock:
            store = self._answers.setdefault(machine.id, {})
            for part in parts:
                answer = answers.get(part["key"])
                if error is None and not isinstance(answer, dict):
                    store[part["key"]] = (None, "the node's poll did not answer this part", received)
                else:
                    store[part["key"]] = (answer if error is None else None, error, received)
            self._busy.discard(machine.id)
        if any(part["key"] == TURNS_KEY for part in parts):
            from hyperloom.web.features import turns
            turns.record(self.api, machine, answers.get(TURNS_KEY), error, sent, received)

    # -- the thread --------------------------------------------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="hyperloom-node-poll", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - one bad tick (an unreadable configuration) never ends the reads
                pass


class NotRead(remote.RemoteError):
    """No answer of the node poll to give (not read yet, or a GPU turn holds the node)."""


def reader(api, now=None):
    """``read(machine id, verb, payload)`` through the portal's node poll (orchestrate.status / logs), or None
    when the portal has none. A missing answer raises NotRead with the reason."""
    poll = of(api)
    if poll is None:
        return None

    def read(machine_id, verb, payload):
        # A fixed-size key whatever the payload (a long install root must not exceed the node's key limit).
        key = verb + ":" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:32]
        answer, error, _ = poll.read(machine_id, key, verb, payload, now=now)
        if answer is None:
            raise NotRead(error)
        return answer
    return read


def of(api):
    """The portal's node poll, or None (the command line and test fixtures read nodes directly)."""
    return vars(api).get("node_poll") if hasattr(api, "__dict__") else None


def start(api):
    """Start the node poll of a portal (once; kept as ``api.node_poll``)."""
    poll = of(api)
    if poll is None:
        poll = api.node_poll = NodePoll(api)
    return poll.start()
