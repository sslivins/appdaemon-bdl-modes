"""Execution engine for bdl_modes (pure Python, no AppDaemon imports).

One *operation* is active at a time (vacation, home or preheat). Starting a
new one, or cancelling, bumps a persisted ``gen`` counter; every HA call and
every background retry re-checks it, so stale work (e.g. a patient hot-tub
retry from a superseded preheat) can never act after a newer intent.

All work runs on one thread through ``run_once()``, which serialises every HA
write (Nest thermostats are rate limited) and keeps the state machine simple.
Commands (start/cancel/...) may come from other threads; they only mutate
state under ``lock`` and wake the loop.
"""

import copy
import threading
import time
import uuid

import bm_steps

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_RETRYING = "retrying"      # patient step waiting for its next try
STEP_SUCCESS = "success"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_CANCELLED = "cancelled"
STEP_FINAL = (STEP_SUCCESS, STEP_FAILED, STEP_SKIPPED, STEP_CANCELLED)

NUMERIC_TOL = 0.5

DEFAULTS = {
    "house_lead_h": 24.0,
    "amenities_lead_h": 6.0,
    "arrive_grace_h": 2.0,          # run "arrive" at most this late
    "max_arrival_days": 60,
    "quick_delays_s": [5, 15, 30],  # waits between quick attempts
    "patient_delays_s": [120, 300, 600],  # then repeats the last one
    "patient_window_s": 3600,       # give up this long after first failure
    "verify_default_s": 3,
    "verify_poll_s": 1,
    "climate_gap_s": 1.5,           # min gap between climate.* writes
    "tb_entity": "sensor.thermal_battery_status",
    "tb_wait_s": 900,
}


class Superseded(Exception):
    """The operation this work belongs to is no longer current."""


class Stopping(Exception):
    """The engine is shutting down; resume from persisted state later."""


class Engine:
    def __init__(self, ha, store, notify, log, clock=time.time, config=None,
                 sleep=None):
        """ha:     object with call(action, data) -> (ok, err) and
                   get(entity_id) -> state dict | None
        store:  object with load() -> dict|None, save(dict),
                   archive(op dict)
        notify: callable(title, message)
        log:    callable(msg, level="INFO")
        sleep:  optional callable(seconds) for tests (default waits on the
                   wake condition so commands/stop interrupt it)"""
        self.ha, self.store, self.notify, self.log = ha, store, notify, log
        self.clock = clock
        self.cfg = dict(DEFAULTS)
        self.cfg.update(config or {})
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self._sleep_fn = sleep
        self.stopping = False
        self._last_climate = 0.0
        st = store.load() or {}
        self.gen = int(st.get("gen", 0))
        self.op = st.get("op")
        self.requests = st.get("requests", {})
        if self.op:
            self._recover()

    # ----------------------------------------------------------- persistence
    def _save(self):
        with self.lock:
            self.store.save({"gen": self.gen, "op": self.op,
                             "requests": self.requests})

    def _recover(self):
        """After a restart: anything mid-flight is re-run (all actions are
        idempotent set-to-value calls)."""
        if self.op["status"] in ("done", "failed", "cancelled", "superseded"):
            return
        for g in self.op["groups"]:
            if g["status"] == "running":
                g["status"] = "pending"
            for s in g["steps"]:
                if s["status"] == STEP_RUNNING:
                    s["status"] = STEP_PENDING
                    s["progress"] = None
        self._log_op("resumed after restart")

    # -------------------------------------------------------------- commands
    def start(self, kind, season, arrival=None, dry_run=False,
              skip_steps=None, request_id=None):
        with self.lock:
            if request_id and request_id in self.requests:
                return self.op if (self.op or {}).get(
                    "id") == self.requests[request_id] else None
            now = self.clock()
            if kind not in ("vacation", "home", "preheat"):
                raise ValueError("unknown kind %r" % kind)
            if kind == "preheat":
                if arrival is None:
                    raise ValueError("preheat needs an arrival time")
                if arrival <= now + 60:
                    raise ValueError("arrival must be in the future")
                if arrival > now + self.cfg["max_arrival_days"] * 86400:
                    raise ValueError("arrival is too far away")
            self._supersede("superseded by %s" % kind)
            self.gen += 1
            skip = set(skip_steps or [])
            groups = []
            for name, steps in bm_steps.build_groups(kind, season):
                st = []
                for s in steps:
                    s = copy.deepcopy(s)
                    s.update({"status": STEP_SKIPPED if s["id"] in skip
                              else STEP_PENDING, "attempt": 0,
                              "error": None, "progress": None,
                              "failed_actions": None, "next_retry": None,
                              "first_fail": None, "finished": None})
                    st.append(s)
                groups.append({"name": name,
                               "label": bm_steps.GROUP_LABELS[name],
                               "due": self._due(kind, name, arrival, now),
                               "status": "pending", "started": None,
                               "finished": None, "notified": False,
                               "steps": st})
            self.op = {"id": uuid.uuid4().hex[:12], "kind": kind,
                       "gen": self.gen, "season": season,
                       "arrival": arrival, "created": now,
                       "dry_run": bool(dry_run), "status": "scheduled",
                       "groups": groups, "log": []}
            self._log_op("created (%s, season %s%s)" % (
                kind, season, ", DRY RUN" if dry_run else ""))
            if request_id:
                self.requests[request_id] = self.op["id"]
                if len(self.requests) > 50:
                    for k in list(self.requests)[:-50]:
                        del self.requests[k]
            self._refresh_status()
            self._save()
            self.wake.notify_all()
            return self.op

    def update_arrival(self, arrival):
        with self.lock:
            op = self.op
            if not op or op["kind"] != "preheat" or op["status"] in (
                    "done", "failed", "cancelled", "superseded"):
                raise ValueError("no active preheat to update")
            now = self.clock()
            if arrival <= now + 60:
                raise ValueError("arrival must be in the future")
            op["arrival"] = arrival
            for g in op["groups"]:
                if g["status"] == "pending":
                    g["due"] = self._due("preheat", g["name"], arrival, now)
            self._log_op("arrival changed")
            self._save()
            self.wake.notify_all()
            return op

    def arrive_now(self, season):
        """Run everything still pending now (or start a home op)."""
        with self.lock:
            op = self.op
            if (op and op["kind"] == "preheat"
                    and op["status"] in ("scheduled", "running", "retrying")):
                now = self.clock()
                for g in op["groups"]:
                    if g["status"] == "pending":
                        g["due"] = now
                op["arrival"] = min(op["arrival"], now)
                self._log_op("arrive now")
                self._save()
                self.wake.notify_all()
                return op
        return self.start("home", season)

    def cancel(self):
        """Stop pending/retrying work. Never rolls anything back."""
        with self.lock:
            if not self.op or self.op["status"] in (
                    "done", "failed", "cancelled", "superseded"):
                return self.op
            self._supersede("cancelled", status="cancelled")
            self.gen += 1
            self._save()
            self.wake.notify_all()
            return self.op

    def stop(self):
        with self.lock:
            self.stopping = True
            self.wake.notify_all()

    def status(self):
        with self.lock:
            return copy.deepcopy(self.op)

    # ------------------------------------------------------------ scheduling
    def _due(self, kind, group, arrival, now):
        if kind != "preheat":
            return now
        lead = {"house": self.cfg["house_lead_h"],
                "amenities": self.cfg["amenities_lead_h"],
                "arrive": 0.0}[group]
        return max(now, arrival - lead * 3600.0)

    def _supersede(self, why, status="superseded"):
        op = self.op
        if not op or op["status"] in ("done", "failed", "cancelled",
                                      "superseded"):
            return
        for g in op["groups"]:
            for s in g["steps"]:
                if s["status"] not in STEP_FINAL:
                    s["status"] = STEP_CANCELLED
                    s["progress"] = None
                    s["next_retry"] = None
            if g["status"] in ("pending", "running", "retrying"):
                g["status"] = "cancelled"
        op["status"] = status
        self._log_op(why)
        try:
            self.store.archive(op)
        except Exception as exc:  # noqa: BLE001
            self.log("archive failed: %s" % exc, "WARNING")

    def next_wake(self):
        """Seconds until there might be work (None = idle)."""
        with self.lock:
            op = self.op
            if not op or op["status"] in ("done", "failed", "cancelled",
                                          "superseded"):
                return None
            times = []
            for g in op["groups"]:
                if g["status"] == "pending":
                    times.append(g["due"])
                for s in g["steps"]:
                    if s["status"] == STEP_RETRYING and s["next_retry"]:
                        times.append(s["next_retry"])
            if not times:
                return None
            return max(0.0, min(times) - self.clock())

    def run_once(self):
        """Do the next piece of due work. Returns True if anything ran."""
        with self.lock:
            op = self.op
            if not op or op["status"] in ("done", "failed", "cancelled",
                                          "superseded"):
                return False
            now = self.clock()
            work = None
            for g in op["groups"]:
                if g["status"] == "pending" and g["due"] <= now:
                    work = ("group", g)
                    break
            if work is None:
                for g in op["groups"]:
                    for s in g["steps"]:
                        if (s["status"] == STEP_RETRYING and s["next_retry"]
                                and s["next_retry"] <= now):
                            work = ("retry", g, s)
                            break
                    if work:
                        break
            if work is None:
                return False
            gen = op["gen"]
        try:
            if work[0] == "group":
                self._run_group(op, work[1], gen)
            else:
                self._retry_step(op, work[1], work[2], gen)
        except Superseded:
            self.log("work for op %s dropped (superseded)" % op["id"])
        except Stopping:
            self._save()
            raise
        self._flush_notify(op)
        with self.lock:
            self._refresh_status()
            self._save()
        return True

    def loop(self):
        """Thread body."""
        while True:
            with self.lock:
                if self.stopping:
                    return
            try:
                ran = self.run_once()
            except Stopping:
                return
            except Exception as exc:  # noqa: BLE001
                self.log("engine error: %s" % exc, "ERROR")
                ran = False
            if ran:
                continue
            with self.lock:
                if self.stopping:
                    return
                w = self.next_wake()
                self.wake.wait(timeout=60 if w is None else min(w + 0.1, 60))

    # ------------------------------------------------------------- execution
    def _check(self, gen):
        with self.lock:
            if self.stopping:
                raise Stopping()
            if gen != self.gen:
                raise Superseded()

    def _sleep(self, seconds, gen):
        if seconds <= 0:
            return
        if self._sleep_fn:
            self._sleep_fn(seconds)
            self._check(gen)
            return
        end = time.time() + seconds
        with self.lock:
            while True:
                self._check(gen)
                left = end - time.time()
                if left <= 0:
                    return
                self.wake.wait(timeout=left)

    def _run_group(self, op, g, gen):
        now = self.clock()
        late = self._late_reason(op, g, now)
        if late:
            with self.lock:
                for s in g["steps"]:
                    if s["status"] not in STEP_FINAL:
                        s["status"] = STEP_SKIPPED
                g["status"] = "skipped"
                g["finished"] = now
                g["needs_notify"] = True
                g["notify_extra"] = late
                self._log_op("%s skipped: %s" % (g["name"], late))
            return
        with self.lock:
            g["status"] = "running"
            g["started"] = g["started"] or now
            self._log_op("%s started" % g["name"])
            self._refresh_status()
            self._save()
        for s in g["steps"]:
            if s["status"] != STEP_PENDING:
                continue
            self._run_step_first(op, s, gen)
        with self.lock:
            self._settle_group(op, g)

    def _late_reason(self, op, g, now):
        if op["kind"] != "preheat":
            return None
        arr = op["arrival"]
        if g["name"] == "arrive":
            if now > arr + self.cfg["arrive_grace_h"] * 3600:
                return "arrival time passed %.1f h ago" % ((now - arr) / 3600)
            return None
        if now > arr + self.cfg["arrive_grace_h"] * 3600:
            return "arrival time already passed"
        return None

    def _run_step_first(self, op, s, gen):
        with self.lock:
            s["status"] = STEP_RUNNING
            self._save()
        delays = list(self.cfg["quick_delays_s"])
        if s.get("retry") == "once":
            delays = []
        failed = None
        while True:
            self._check(gen)
            with self.lock:
                s["attempt"] += 1
            ok, failed, err = self._exec_actions(op, s, failed, gen)
            if ok:
                self._step_done(op, s, STEP_SUCCESS)
                return
            with self.lock:
                s["error"] = err
                s["failed_actions"] = sorted(failed)
                s["first_fail"] = s["first_fail"] or self.clock()
            if not delays:
                break
            self._sleep(delays.pop(0), gen)
        if s.get("retry") == "patient":
            with self.lock:
                s["status"] = STEP_RETRYING
                s["next_retry"] = self.clock() + self._patient_delay(s)
                s["progress"] = None
                self._log_op("%s failing, retrying in background: %s" % (
                    s["id"], s["error"]))
                self._save()
            return
        self._step_done(op, s, STEP_FAILED)

    def _patient_delay(self, s):
        d = self.cfg["patient_delays_s"]
        n = max(0, s["attempt"] - len(self.cfg["quick_delays_s"]) - 1)
        return d[min(n, len(d) - 1)]

    def _retry_step(self, op, g, s, gen):
        with self.lock:
            s["status"] = STEP_RUNNING
            s["attempt"] += 1
            self._save()
        ok, failed, err = self._exec_actions(
            op, s, set(s.get("failed_actions") or []) or None, gen)
        if ok:
            mins = (self.clock() - (s["first_fail"] or self.clock())) / 60
            with self.lock:
                self._log_op("%s recovered after %.0f min" % (s["id"], mins))
            self._step_done(op, s, STEP_SUCCESS)
        else:
            with self.lock:
                s["error"] = err
                s["failed_actions"] = sorted(failed)
                if (self.clock() - s["first_fail"]
                        >= self.cfg["patient_window_s"]):
                    s["status"] = STEP_FAILED
                    s["finished"] = self.clock()
                    s["next_retry"] = None
                    self._log_op("%s gave up: %s" % (s["id"], err))
                else:
                    s["status"] = STEP_RETRYING
                    s["next_retry"] = self.clock() + self._patient_delay(s)
                s["progress"] = None
        with self.lock:
            self._settle_group(op, g)

    def _step_done(self, op, s, status):
        with self.lock:
            s["status"] = status
            s["finished"] = self.clock()
            s["progress"] = None
            s["next_retry"] = None
            if status == STEP_SUCCESS:
                s["error"] = None
                s["failed_actions"] = None
            self._save()

    def _settle_group(self, op, g):
        sts = [s["status"] for s in g["steps"]]
        if any(x in (STEP_PENDING, STEP_RUNNING) for x in sts):
            return
        if any(x == STEP_RETRYING for x in sts):
            g["status"] = "retrying"
            return
        if g["status"] in ("done", "failed", "skipped", "cancelled"):
            return
        g["status"] = "failed" if STEP_FAILED in sts else "done"
        g["finished"] = self.clock()
        self._log_op("%s %s" % (g["name"], g["status"]))
        if g["status"] == "failed":
            g["needs_notify"] = True

    def _flush_notify(self, op):
        """Send pending group notifications (call without holding lock)."""
        for g in op["groups"]:
            if g.get("needs_notify") and not g.get("notified"):
                self._notify_group(op, g, g.get("notify_extra"))

    def _refresh_status(self):
        op = self.op
        if not op or op["status"] in ("cancelled", "superseded"):
            return
        gs = [g["status"] for g in op["groups"]]
        if any(x in ("running",) for x in gs):
            st = "running"
        elif any(x == "retrying" for x in gs):
            st = "retrying"
        elif any(x == "pending" for x in gs):
            st = ("scheduled" if all(x == "pending" for x in gs)
                  else "running")
        elif any(x in ("failed", "skipped") for x in gs):
            st = "failed"
        else:
            st = "done"
        if st != op["status"]:
            op["status"] = st
            if st in ("done", "failed"):
                self._log_op("finished: %s" % st)
                try:
                    self.store.archive(op)
                except Exception as exc:  # noqa: BLE001
                    self.log("archive failed: %s" % exc, "WARNING")

    def _notify_group(self, op, g, extra=None):
        with self.lock:
            if g.get("notified"):
                return
            g["notified"] = True
            self._save()
        bad = ["%s: %s" % (s["alias"], s["error"] or s["status"])
               for s in g["steps"] if s["status"] in (STEP_FAILED,)]
        msg = "%s (%s)%s" % (
            g["label"], op["kind"], (": " + extra) if extra else "")
        if bad:
            msg += "\n" + "\n".join(bad)
        try:
            self.notify("Lodge %s problem" % op["kind"], msg)
        except Exception as exc:  # noqa: BLE001
            self.log("notify failed: %s" % exc, "ERROR")

    def _log_op(self, msg):
        if self.op is not None:
            self.op["log"].append([round(self.clock()), msg])
            del self.op["log"][:-60]
            self.log("[%s %s] %s" % (self.op["kind"], self.op["id"], msg))

    # --------------------------------------------------------------- actions
    def _exec_actions(self, op, s, only, gen):
        """Run (a subset of) a step's actions. Every action is attempted and
        its delay_after always honoured (rate limits) even on failure."""
        failed, errors = set(), []
        for i, a in enumerate(s["actions"]):
            if only is not None and i not in only:
                continue
            self._check(gen)
            with self.lock:
                s["progress"] = a.get("description")
            ok, err = self._exec_action(op, a, gen)
            if not ok:
                failed.add(i)
                errors.append("%s: %s" % (a.get("description")
                                          or a["action"], err))
            self._sleep(0 if op["dry_run"] else a.get("delay_after", 0), gen)
        return (not failed), failed, "; ".join(errors) or None

    def _exec_action(self, op, a, gen):
        action, data = a["action"], a.get("data", {})
        if action == bm_steps.WAIT_THERMAL_BATTERY:
            return self._wait_tb(op, gen)
        if op["dry_run"]:
            self.log("[dry_run] %s %s" % (action, data))
            return True, None
        if action.startswith("climate/"):
            gap = self._last_climate + self.cfg["climate_gap_s"] - self.clock()
            if gap > 0:
                self._sleep(gap, gen)
        self._check(gen)
        ok, err = self.ha.call(action, data)
        if action.startswith("climate/"):
            self._last_climate = self.clock()
        if not ok:
            return False, err
        return self._verify(a, gen)

    def _verify(self, a, gen):
        checks = expected_checks(a["action"], a.get("data", {}))
        eids = a.get("data", {}).get("entity_id")
        if not checks or not eids:
            return True, None
        eids = eids if isinstance(eids, list) else [eids]
        wait = a.get("verify_delay", self.cfg["verify_default_s"])
        t0 = self.clock()
        while True:
            self._sleep(self.cfg["verify_poll_s"], gen)
            errs = []
            for e in eids:
                errs += check_state(e, self.ha.get(e), checks)
            if not errs:
                return True, None
            if self.clock() - t0 >= wait:
                return False, "verify: " + "; ".join(errs)

    def _wait_tb(self, op, gen):
        """Wait until the thermal battery has ended its sessions, restored
        its setpoints and tank boost (it publishes ``quiesced``)."""
        if op["dry_run"]:
            return True, None
        t0 = self.clock()
        while True:
            st = self.ha.get(self.cfg["tb_entity"])
            if st is None:
                return True, None   # no thermal battery installed
            attrs = st.get("attributes") or {}
            if st.get("state") in ("home", "dry_run") and attrs.get(
                    "quiesced", st.get("state") == "dry_run"):
                return True, None
            if self.clock() - t0 >= self.cfg["tb_wait_s"]:
                return False, "thermal battery still %s after %d min" % (
                    st.get("state"), self.cfg["tb_wait_s"] // 60)
            self._sleep(15, gen)


# ------------------------------------------------------------- verification
def unresolved(op, get):
    """Aliases of the op's failed steps that are still wrong right now.

    A failed step drops out once every verifiable action checks out (e.g. a
    fireplace that wouldn't turn off has since been turned off). Steps with
    nothing to verify stay listed.
    """
    out = []
    for g in (op or {}).get("groups", []):
        for s in g["steps"]:
            if s["status"] != STEP_FAILED:
                continue
            verifiable, bad = False, False
            for a in s.get("actions", []):
                checks = expected_checks(a["action"], a.get("data", {}))
                eids = a.get("data", {}).get("entity_id")
                if not checks or not eids:
                    continue
                verifiable = True
                for e in eids if isinstance(eids, list) else [eids]:
                    if check_state(e, get(e), checks):
                        bad = True
            if bad or not verifiable:
                out.append(s["alias"])
    return out


def expected_checks(action, data):
    """List of (kind, key, expected) checks after a service call."""
    domain, service = action.split("/")
    if service == "turn_on":
        return [("state", None, "on")]
    if service == "turn_off":
        return [("state", None, "off")]
    if domain == "input_select" and service == "select_option":
        return [("state", None, str(data.get("option")))]
    if domain == "climate" and service == "set_temperature":
        out = []
        if data.get("temperature") is not None:
            out.append(("attr_num", "temperature", float(data["temperature"])))
        if data.get("hvac_mode"):
            out.append(("state", None, data["hvac_mode"]))
        return out
    if domain == "climate" and service == "set_preset_mode":
        return [("attr", "preset_mode", data.get("preset_mode"))]
    if domain == "climate" and service == "set_hvac_mode":
        return [("state", None, data.get("hvac_mode"))]
    if domain == "number" and service == "set_value":
        return [("state_num", None, float(data.get("value")))]
    return []


def check_state(entity_id, st, checks):
    if st is None:
        return ["%s: no state" % entity_id]
    state = st.get("state")
    if state in ("unavailable", "unknown"):
        return ["%s is %s" % (entity_id, state)]
    attrs = st.get("attributes") or {}
    errs = []
    for kind, key, exp in checks:
        if kind == "state" and str(state) != str(exp):
            errs.append("%s: expected %s, got %s" % (entity_id, exp, state))
        elif kind == "state_num":
            try:
                if abs(float(state) - exp) > NUMERIC_TOL:
                    errs.append("%s: expected ~%s, got %s" % (
                        entity_id, exp, state))
            except (TypeError, ValueError):
                errs.append("%s: %r not numeric" % (entity_id, state))
        elif kind == "attr" and not (
                str(attrs.get(key)) == str(exp)
                or (str(exp).lower() == "none" and attrs.get(key) in (
                    None, "none", "None"))):
            errs.append("%s: expected %s=%s, got %s" % (
                entity_id, key, exp, attrs.get(key)))
        elif kind == "attr_num":
            try:
                if abs(float(attrs.get(key)) - exp) > NUMERIC_TOL:
                    errs.append("%s: expected %s~%s, got %s" % (
                        entity_id, key, exp, attrs.get(key)))
            except (TypeError, ValueError):
                errs.append("%s: %s=%r not numeric" % (
                    entity_id, key, attrs.get(key)))
    return errs
