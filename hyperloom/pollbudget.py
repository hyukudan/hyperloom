"""What the portal's background readers may ask of the nodes: a bounded cadence, and nothing during a GPU turn.

The pollers are everything that reads a node or an engine without a person asking for it: the collectors
(``fleet-nodes``, ``turns``, ``usb4``, ``ops-nodes``, the engine readers) and the core snapshot's machine inventory,
launch status and engine statistics. Every poll asks this budget first; a refused poll is skipped (the previous
answer stays, with the reason), it never waits. Core actions a person starts (doctor, up, down, a job) never ask it.

Node (ssh) polls (one grant = one ssh session: the transport opens one session per verb call):
- each poller reads a node at most once per ``CYCLE_S`` (30 s);
- all other pollers together open at most one ssh session to a node per ``NODE_SPACING_S`` (5 s), so never a burst:
  at most twelve a minute, plus the turn check's two (the review measured about 64 before);
- the turn check goes first: it is not held back by the spacing, and while it is due (it asked and was last granted
  a cycle ago or more) every other poller of that node yields, so turn detection never starves behind the others;
- while the latest turn reading of a node shows a GPU turn holder, only the turn check runs there, once per
  ``TURN_CHECK_S`` (60 s); every other node poll of that machine pauses until a turn reading shows the GPU free (a
  holder never expires by age: a turn check that stops answering keeps the node paused).

Engine (HTTP) polls of a node's engines: at most once per ``ENGINE_CYCLE_S`` (30 s) per poller and engine, and none
while a turn of another holder (not a lease of this portal's own launches) holds the engine's machine.

Times are the caller's clock (``now``, wall seconds); the state lives in this process (the portal); the command
line does not poll.
"""

import threading
import time


CYCLE_S = 30.0
NODE_SPACING_S = 5.0
TURN_CHECK_S = 60.0
ENGINE_CYCLE_S = 30.0
TURN_POLLER = "turns"


class Budget:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._polls = {}        # (machine, poller) -> last granted
        self._last = {}         # machine -> last granted node poll (any poller)
        self._turns = {}        # machine -> (holder label or None, own, read at)
        self._engines = {}      # (endpoint, poller) -> last granted
        self._asked = {}        # machine -> when the turn check last asked
        self._undo = {}         # (machine, poller) -> (previous grant of the poller, previous grant of the node)

    def _now(self, now):
        return self.clock() if now is None else now

    def _turn(self, machine_id, now):
        reading = self._turns.get(machine_id)
        if reading is None or reading[0] is None:
            return None
        return reading[0], reading[1]

    def turn(self, machine_id, now=None):
        """(holder, own) of a GPU turn the latest reading shows on this machine, or None."""
        with self._lock:
            return self._turn(machine_id, self._now(now))

    def note_turn(self, machine_id, holder, own=False, now=None):
        """Record a turn reading of a machine: the holder's label (None: the GPU is free) and whether the holder is
        a lease of this portal's own launches."""
        with self._lock:
            self._turns[machine_id] = (holder, bool(own), self._now(now))

    @staticmethod
    def _recent(last, now, every):
        return last is not None and 0 <= now - last < every

    def node(self, machine_id, poller, now=None):
        """None when this poller may open one ssh session to the machine now (the slot is taken), else why not."""
        with self._lock:
            now = self._now(now)
            held = self._turn(machine_id, now)
            if held is not None and poller != TURN_POLLER:
                return f"paused: a GPU turn holds {machine_id} ({held[0]}); node polls wait for its end"
            every = TURN_CHECK_S if held is not None else CYCLE_S
            last = self._polls.get((machine_id, poller))
            if poller == TURN_POLLER:
                self._asked[machine_id] = now
            if self._recent(last, now, every):
                return f"polled {now - last:.0f} s ago (once per {every:.0f} s per node)"
            if poller != TURN_POLLER and self._turn_check_due(machine_id, now):
                return f"the turn check of {machine_id} goes first"
            previous = self._last.get(machine_id)
            if poller != TURN_POLLER and self._recent(previous, now, NODE_SPACING_S):
                return f"another poll of {machine_id} ran {now - previous:.0f} s ago (at most one per " \
                       f"{NODE_SPACING_S:.0f} s per node)"
            self._undo[(machine_id, poller)] = (last, previous)
            self._polls[(machine_id, poller)] = now
            self._last[machine_id] = now
            return None

    def refund(self, machine_id, poller):
        """Give back the last grant of this poller on this machine when its call did not happen (the node gate was
        busy): its cadence and the node's spacing are as before, so the turn check keeps its priority."""
        with self._lock:
            undo = self._undo.pop((machine_id, poller), None)
            if undo is None:
                return
            for table, key, value in ((self._polls, (machine_id, poller), undo[0]), (self._last, machine_id, undo[1])):
                if value is None:
                    table.pop(key, None)
                else:
                    table[key] = value

    def _turn_check_due(self, machine_id, now):
        """The turn check asked recently (it runs for this node) and its own cadence allows it now."""
        asked = self._asked.get(machine_id)
        if asked is None or not 0 <= now - asked <= 2 * CYCLE_S:
            return False
        return not self._recent(self._polls.get((machine_id, TURN_POLLER)), now, CYCLE_S)

    def engine_paused(self, machine_id, now=None):
        """Why engine reads of this machine wait (a turn of another holder holds it), or None."""
        if machine_id is None:
            return None
        with self._lock:
            held = self._turn(machine_id, self._now(now))
        if held is not None and not held[1]:
            return f"paused: a GPU turn of {held[0]} holds {machine_id}; engine reads wait for its end"
        return None

    def engine(self, machine_id, endpoint, poller, now=None):
        """None when this poller may read the engine at ``endpoint`` (on ``machine_id``, or None when it is no
        configured machine) now, else why not."""
        paused = self.engine_paused(machine_id, now)
        if paused:
            return paused
        with self._lock:
            now = self._now(now)
            last = self._engines.get((endpoint, poller))
            if self._recent(last, now, ENGINE_CYCLE_S):
                return f"read {now - last:.0f} s ago (once per {ENGINE_CYCLE_S:.0f} s per engine)"
            self._engines[(endpoint, poller)] = now
            return None

    def describe(self, now=None):
        with self._lock:
            now = self._now(now)
            return {"cycle_s": CYCLE_S, "node_spacing_s": NODE_SPACING_S, "turn_check_s": TURN_CHECK_S,
                    "engine_cycle_s": ENGINE_CYCLE_S,
                    "paused": sorted(m for m in self._turns if self._turn(m, now) is not None)}


_guard = threading.Lock()


def for_api(api):
    """The budget of one portal (kept on its API object)."""
    with _guard:
        budget = vars(api).get("_poll_budget")
        if budget is None:
            budget = Budget()
            api._poll_budget = budget
        return budget


def holder_label(text):
    """The label of a GPU turn holder line ("[PAIR ]LABEL ... since HH:MM:SS ..."), or None for an empty line."""
    words = str(text or "").split()
    if words and words[0] == "PAIR":
        words = words[1:]
    return words[0][:128] if words else None


def own_labels(config_path, read=None):
    """The lease labels of this portal's launches (a turn they hold is this portal's own)."""
    from hyperloom import orchestrate
    labels = set()
    try:
        states = orchestrate.launch_states(config_path, read=read, unknown=True)
    except Exception:  # noqa: BLE001 - unreadable launch states: no own turn
        return labels
    for state in states.values():
        for lease in state.get("leases") or []:
            if isinstance(lease, dict) and isinstance(lease.get("label"), str):
                labels.add(lease["label"])
    return labels


def note_holder(api, machine_id, holder, now=None):
    """Record a turn reading of a machine for the portal ``api``: ``holder`` is the holder's label (None: free)."""
    own = False
    if holder is not None:
        own = holder in own_labels(getattr(api, "config_path", None) or "",
                                   read=getattr(getattr(api, "orchestrate", None), "_read", None))
    for_api(api).note_turn(machine_id, holder, own, now=now)


def machine_of_address(cluster, address):
    """The configured machine whose address this is (None when it is none or the cluster is unreadable)."""
    try:
        for mid, machine in cluster.machines.items():
            if getattr(machine, "http_address", None) == address:
                return mid
    except Exception:  # noqa: BLE001 - an unreadable configuration: no machine
        return None
    return None
