import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bm_engine  # noqa: E402
import bm_steps  # noqa: E402

H = 3600.0
T0 = 1_000_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeHA:
    """Applies service calls to an in-memory state table."""

    def __init__(self, clock):
        self.clock = clock
        self.states = {}
        self.calls = []          # (t, action, data)
        self.down = {}           # entity_id -> down until t (None = forever)
        self.ignore = set()      # accepts call but state never changes

    def _down(self, e):
        if e not in self.down:
            return False
        until = self.down[e]
        return until is None or self.clock() < until

    def call(self, action, data):
        self.calls.append((self.clock(), action, dict(data)))
        eids = data.get("entity_id")
        eids = eids if isinstance(eids, list) else [eids]
        for e in eids:
            if self._down(e):
                return False, "%s unreachable" % e
        domain, svc = action.split("/")
        for e in eids:
            if e in self.ignore:
                continue
            st = self.states.setdefault(e, {"state": "off", "attributes": {}})
            a = st["attributes"]
            if svc in ("turn_on", "turn_off"):
                st["state"] = svc[5:]
            elif svc == "select_option":
                st["state"] = data["option"]
            elif svc == "set_temperature":
                a["temperature"] = data["temperature"]
                if data.get("hvac_mode"):
                    st["state"] = data["hvac_mode"]
            elif svc == "set_preset_mode":
                a["preset_mode"] = (None if data["preset_mode"] == "none"
                                    else data["preset_mode"])
            elif svc == "set_hvac_mode":
                st["state"] = data["hvac_mode"]
            elif svc == "set_value":
                st["state"] = data["value"]
        return True, None

    def get(self, e):
        if self._down(e):
            return {"state": "unavailable", "attributes": {}}
        return self.states.get(e)

    def calls_for(self, e, action=None):
        out = []
        for t, a, d in self.calls:
            ids = d.get("entity_id")
            ids = ids if isinstance(ids, list) else [ids]
            if e in ids and (action is None or a == action):
                out.append((t, a, d))
        return out


class MemStore:
    def __init__(self):
        self.state = None
        self.archived = []

    def load(self):
        import copy
        return copy.deepcopy(self.state)

    def save(self, st):
        import json
        self.state = json.loads(json.dumps(st))

    def archive(self, op):
        self.archived.append(op["id"])


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ha = FakeHA(self.clock)
        self.store = MemStore()
        self.notes = []
        self.e = self.make()

    def make(self):
        return bm_engine.Engine(
            self.ha, self.store, lambda t, m: self.notes.append((t, m)),
            lambda m, level="INFO": None, clock=self.clock,
            sleep=self.clock.sleep)

    def drain(self, until=None, max_iter=10000):
        """Run the engine like its thread would, jumping the fake clock to
        the next wake time; stop when idle or past ``until``."""
        for _ in range(max_iter):
            if self.e.run_once():
                continue
            w = self.e.next_wake()
            if w is None:
                return
            if until is not None and self.clock.t + w > until:
                self.clock.t = until
                return
            self.clock.t += w
        self.fail("engine did not settle")

    def step(self, op, sid):
        for g in op["groups"]:
            for s in g["steps"]:
                if s["id"] == sid:
                    return s
        raise KeyError(sid)

    def group(self, op, name):
        return [g for g in op["groups"] if g["name"] == name][0]


class TestHome(Base):
    def test_home_runs_everything_and_sets_state(self):
        self.e.start("home", "heating")
        self.drain()
        op = self.e.status()
        self.assertEqual(op["status"], "done")
        self.assertEqual(self.ha.states["input_select.bdl_occupancy"]["state"],
                         "home")
        self.assertEqual(self.ha.states[bm_steps.AWAY_BOOLEAN]["state"], "off")
        lr = self.ha.states["climate.main_floor"]
        self.assertEqual(lr["attributes"]["temperature"], 20)
        self.assertEqual(lr["state"], "heat")
        self.assertEqual(self.ha.states["climate.hot_tub_heater"][
            "attributes"]["preset_mode"], "Standard")
        self.assertEqual(self.notes, [])

    def test_select_written_before_boolean(self):
        self.e.start("home", "heating")
        self.drain()
        acts = [a for _, a, d in self.ha.calls
                if d.get("entity_id") in (bm_steps.OCCUPANCY_ENTITY,
                                          bm_steps.AWAY_BOOLEAN)]
        self.assertEqual(acts[:2], ["input_select/select_option",
                                    "input_boolean/turn_off"])

    def test_climate_writes_are_spaced_and_never_batched(self):
        self.e.start("home", "heating")
        self.drain()
        times = [t for t, a, d in self.ha.calls if a.startswith("climate/")]
        for t, a, d in self.ha.calls:
            if a.startswith("climate/"):
                self.assertIsInstance(d["entity_id"], str)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(gaps and min(gaps) >= 1.5 - 1e-9, gaps)

    def test_dry_run_makes_no_calls(self):
        self.e.start("home", "heating", dry_run=True)
        self.drain()
        self.assertEqual(self.ha.calls, [])
        self.assertEqual(self.e.status()["status"], "done")

    def test_skip_steps(self):
        self.e.start("home", "heating", skip_steps=["media_on"])
        self.drain()
        self.assertEqual(self.ha.calls_for("switch.office_tv_socket_1"), [])
        self.assertEqual(self.step(self.e.status(), "media_on")["status"],
                         "skipped")
        self.assertEqual(self.e.status()["status"], "done")

    def test_request_id_dedupes(self):
        a = self.e.start("home", "heating", request_id="r1")
        b = self.e.start("home", "heating", request_id="r1")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.e.gen, 1)

    def test_status_is_running_while_a_group_executes(self):
        seen = []
        real = self.ha.call

        def spy(action, data):
            seen.append(self.e.op["status"])
            return real(action, data)
        self.ha.call = spy
        self.e.start("vacation", "heating")
        self.drain()
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"running"})

    def test_vacation_ok_when_fireplace_entities_unavailable(self):
        # Napoleon marks everything but the power switch unavailable while
        # the fireplace is off.
        for e in ("climate.master_bedroom_2", "climate.living_room_fireplace"):
            self.ha.down[e] = None
        self.e.start("vacation", "heating")
        self.drain()
        self.assertEqual(self.e.status()["status"], "done")
        self.assertEqual(self.ha.states["switch.living_room_fireplace"]
                         ["state"], "off")


class TestRetries(Base):
    def test_quick_retry_recovers(self):
        self.e.start("vacation", "heating")
        # patio is unreachable for 10 s from its first attempt
        orig = self.ha.call

        def call(action, data):
            if (data.get("entity_id") == "switch.patio_deck_heaters"
                    and "switch.patio_deck_heaters" not in self.ha.down):
                self.ha.down["switch.patio_deck_heaters"] = self.clock() + 10
            return orig(action, data)
        self.ha.call = call
        self.drain()
        s = self.step(self.e.status(), "patio_off")
        self.assertEqual(s["status"], "success")
        self.assertGreater(s["attempt"], 1)
        self.assertEqual(self.notes, [])

    def test_quick_failure_notifies_once(self):
        self.ha.down["switch.patio_deck_heaters"] = None
        self.e.start("vacation", "heating")
        self.drain()
        op = self.e.status()
        self.assertEqual(self.step(op, "patio_off")["status"], "failed")
        self.assertEqual(op["status"], "failed")
        self.assertEqual(len(self.notes), 1)
        self.assertIn("Patio", self.notes[0][1])
        # only the failing action is retried, everything else ran once
        self.assertEqual(len(self.ha.calls_for(
            "switch.patio_deck_heaters")), 4)

    def test_hot_tub_recovers_in_background(self):
        self.ha.down["climate.hot_tub_heater"] = T0 + 40 * 60
        self.e.start("home", "heating")
        self.drain(until=T0 + 60)
        op = self.e.status()
        # other groups are not blocked by the patient step
        self.assertEqual(self.group(op, "arrive")["status"], "done")
        self.assertEqual(self.step(op, "hot_tub_on")["status"], "retrying")
        self.assertEqual(op["status"], "retrying")
        self.drain()
        op = self.e.status()
        self.assertEqual(self.step(op, "hot_tub_on")["status"], "success")
        self.assertEqual(op["status"], "done")
        self.assertEqual(self.notes, [])
        self.assertEqual(self.ha.states["climate.hot_tub_heater"][
            "attributes"]["preset_mode"], "Standard")

    def test_hot_tub_gives_up_after_window(self):
        self.ha.down["climate.hot_tub_heater"] = None
        self.e.start("home", "heating")
        self.drain()
        op = self.e.status()
        s = self.step(op, "hot_tub_on")
        self.assertEqual(s["status"], "failed")
        self.assertGreaterEqual(s["finished"] - s["first_fail"], 3600)
        self.assertLess(s["finished"] - s["first_fail"], 3600 + 700)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("Hot Tub", self.notes[0][1])

    def test_patient_delays_back_off(self):
        self.ha.down["climate.hot_tub_heater"] = None
        self.e.start("home", "heating")
        self.drain()
        ts = [t for t, _, _ in self.ha.calls_for("climate.hot_tub_heater")]
        gaps = [round(b - a) for a, b in zip(ts, ts[1:])]
        # quick 5/15/30 (+ verify time), then 120, 300, 600, 600...
        self.assertEqual(gaps[3:6], [120, 300, 600])

    def test_verify_failure_is_retried(self):
        self.ha.ignore.add("switch.patio_deck_heaters")
        self.ha.states["switch.patio_deck_heaters"] = {
            "state": "on", "attributes": {}}
        self.e.start("vacation", "heating")
        self.drain()
        s = self.step(self.e.status(), "patio_off")
        self.assertEqual(s["status"], "failed")
        self.assertIn("verify", s["error"])

    def test_delay_after_honoured_on_failure(self):
        # master fireplace power fails; the next action must still wait 1 s
        self.ha.down["switch.master_bedroom"] = None
        self.e.start("vacation", "heating", skip_steps=[
            s["id"] for _, steps in bm_steps.build_groups(
                "vacation", "heating") for s in steps
            if s["id"] != "fireplaces_off"])
        self.drain(until=T0 + 1)
        calls = self.ha.calls
        t_fail = [t for t, a, d in calls
                  if d["entity_id"] == "switch.master_bedroom"][0]
        t_next = [t for t, a, d in calls
                  if d["entity_id"] == "switch.living_room_fireplace"][0]
        self.assertGreaterEqual(t_next - t_fail, 1)


class TestPreheat(Base):
    def test_groups_due_at_leads(self):
        arr = T0 + 48 * H
        op = self.e.start("preheat", "heating", arrival=arr)
        self.assertEqual(self.group(op, "house")["due"], arr - 24 * H)
        self.assertEqual(self.group(op, "amenities")["due"], arr - 6 * H)
        self.assertEqual(self.group(op, "arrive")["due"], arr)
        self.assertEqual(op["status"], "scheduled")
        self.drain(until=arr - 24 * H - 1)
        self.assertEqual(self.ha.calls, [])
        self.drain(until=arr - 6 * H - 1)
        self.assertEqual(self.ha.states["input_select.bdl_occupancy"][
            "state"], "preheat")
        # boolean off during preheat so TB stands down
        self.assertEqual(self.ha.states[bm_steps.AWAY_BOOLEAN]["state"], "off")
        self.assertNotIn("climate.hot_tub_heater", self.ha.states)
        self.drain(until=arr - 1)
        self.assertIn("climate.hot_tub_heater", self.ha.states)
        self.assertNotIn("switch.office_tv_socket_1", self.ha.states)
        self.drain()
        op = self.e.status()
        self.assertEqual(op["status"], "done")
        self.assertEqual(self.ha.states["input_select.bdl_occupancy"][
            "state"], "home")

    def test_short_notice_runs_house_now(self):
        arr = T0 + 3 * H
        op = self.e.start("preheat", "heating", arrival=arr)
        self.assertEqual(self.group(op, "house")["due"], T0)
        self.assertEqual(self.group(op, "amenities")["due"], T0)

    def test_update_arrival_moves_pending_groups(self):
        arr = T0 + 48 * H
        self.e.start("preheat", "heating", arrival=arr)
        self.e.update_arrival(arr + 24 * H)
        op = self.e.status()
        self.assertEqual(self.group(op, "house")["due"], arr)
        self.drain(until=arr - 1)
        self.assertEqual(self.ha.calls, [])

    def test_arrive_now(self):
        self.e.start("preheat", "heating", arrival=T0 + 48 * H)
        self.e.arrive_now("heating")
        self.drain()
        self.assertEqual(self.e.status()["status"], "done")
        self.assertEqual(self.clock.t < T0 + H, True)

    def test_late_arrive_group_skipped_and_notified(self):
        arr = T0 + 48 * H
        self.e.start("preheat", "heating", arrival=arr)
        self.drain(until=arr - 1)
        self.clock.t = arr + 3 * H      # engine was down over arrival
        self.drain()
        op = self.e.status()
        self.assertEqual(self.group(op, "arrive")["status"], "skipped")
        self.assertEqual(op["status"], "failed")
        self.assertEqual(len(self.notes), 1)

    def test_rejects_past_and_far_arrival(self):
        with self.assertRaises(ValueError):
            self.e.start("preheat", "heating", arrival=T0 - 10)
        with self.assertRaises(ValueError):
            self.e.start("preheat", "heating", arrival=T0 + 90 * 86400)


class TestSupersede(Base):
    def test_new_op_kills_background_retries(self):
        self.ha.down["climate.hot_tub_heater"] = None
        self.e.start("home", "heating")
        self.drain(until=T0 + 60)
        self.assertEqual(self.e.status()["status"], "retrying")
        n = len(self.ha.calls_for("climate.hot_tub_heater"))
        self.e.start("vacation", "heating")
        self.ha.down.clear()
        self.drain()
        tub = self.ha.calls_for("climate.hot_tub_heater")[n:]
        # only the vacation write, never another "Standard" retry
        self.assertTrue(tub)
        self.assertTrue(all(d["preset_mode"] == "Away From Home"
                            for _, _, d in tub))
        self.assertEqual(self.e.status()["kind"], "vacation")

    def test_cancel_does_not_roll_back(self):
        arr = T0 + 48 * H
        self.e.start("preheat", "heating", arrival=arr)
        self.drain(until=arr - 23 * H)
        n = len(self.ha.calls)
        self.e.cancel()
        self.drain()
        self.assertEqual(len(self.ha.calls), n)
        op = self.e.status()
        self.assertEqual(op["status"], "cancelled")
        self.assertEqual(self.group(op, "amenities")["status"], "cancelled")
        self.assertEqual(self.ha.states["input_select.bdl_occupancy"][
            "state"], "preheat")

    def test_supersede_mid_group_stops_old_writes(self):
        calls = []
        orig = self.ha.call

        def call(action, data):
            calls.append(action)
            if len(calls) == 3:
                self.e.start("vacation", "heating")
            return orig(action, data)
        self.ha.call = call
        old = self.e.start("home", "heating")
        self.e.run_once()   # runs the old op's first group until superseded
        self.assertEqual(self.e.status()["kind"], "vacation")
        self.assertNotEqual(self.e.status()["id"], old["id"])
        self.assertLessEqual(len(calls), 4)


class TestRecovery(Base):
    def test_resumes_after_restart(self):
        arr = T0 + 48 * H
        self.e.start("preheat", "heating", arrival=arr)
        self.drain(until=arr - 23 * H)
        self.e = self.make()  # restart
        op = self.e.status()
        self.assertEqual(op["kind"], "preheat")
        self.assertEqual(self.group(op, "house")["status"], "done")
        self.drain()
        self.assertEqual(self.e.status()["status"], "done")

    def test_running_step_rerun_after_restart(self):
        self.e.start("home", "heating")
        st = self.store.state
        st["op"]["groups"][0]["status"] = "running"
        st["op"]["groups"][0]["steps"][0]["status"] = "running"
        self.e = self.make()
        op = self.e.status()
        self.assertEqual(op["groups"][0]["status"], "pending")
        self.drain()
        self.assertEqual(self.e.status()["status"], "done")

    def test_notified_flag_persists(self):
        self.ha.down["switch.patio_deck_heaters"] = None
        self.e.start("vacation", "heating")
        self.drain()
        self.e = self.make()
        self.drain()
        self.assertEqual(len(self.notes), 1)


class TestThermalBatteryHandoff(Base):
    TB = "sensor.thermal_battery_status"

    def test_waits_for_quiesced(self):
        self.ha.states[self.TB] = {"state": "charging",
                                   "attributes": {"quiesced": False}}
        flip_at = T0 + 300
        seen = {}

        def sleep(s):
            self.clock.sleep(s)
            if self.clock.t >= flip_at and not seen:
                seen["writes_before"] = len(
                    self.ha.calls_for("climate.main_floor"))
                self.ha.states[self.TB] = {"state": "home",
                                           "attributes": {"quiesced": True}}
        self.e._sleep_fn = sleep
        self.e.start("home", "heating")
        self.drain()
        self.assertEqual(seen["writes_before"], 0)
        op = self.e.status()
        self.assertEqual(self.step(op, "wait_thermal_battery")["status"],
                         "success")
        self.assertEqual(op["status"], "done")
        first = self.ha.calls_for("climate.main_floor")[0][0]
        self.assertGreaterEqual(first, flip_at)

    def test_tb_timeout_fails_step_but_continues(self):
        self.ha.states[self.TB] = {"state": "charging", "attributes": {}}
        self.e.start("home", "heating")
        self.drain()
        op = self.e.status()
        self.assertEqual(self.step(op, "wait_thermal_battery")["status"],
                         "failed")
        self.assertTrue(self.ha.calls_for("climate.main_floor"))
        self.assertEqual(len(self.notes), 1)


class TestThread(unittest.TestCase):
    def test_loop_runs_and_stops(self):
        import threading
        import time
        ha = FakeHA(time.time)
        e = bm_engine.Engine(ha, MemStore(), lambda t, m: None,
                             lambda m, level="INFO": None,
                             config={"climate_gap_s": 0, "verify_poll_s": 0})
        th = threading.Thread(target=e.loop, daemon=True)
        th.start()
        e.start("home", "heating", dry_run=True)
        for _ in range(100):
            if e.status()["status"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(e.status()["status"], "done")
        arr = time.time() + 48 * H
        e.start("preheat", "heating", arrival=arr)
        time.sleep(0.2)
        e.stop()
        th.join(timeout=3)
        self.assertFalse(th.is_alive())
        self.assertEqual(ha.calls, [])


class TestChecks(unittest.TestCase):
    def test_preset_none_matches_null(self):
        c = bm_engine.expected_checks("climate/set_preset_mode",
                                      {"preset_mode": "none"})
        self.assertEqual(bm_engine.check_state(
            "x", {"state": "heat", "attributes": {"preset_mode": None}}, c),
            [])

    def test_numeric_tolerance(self):
        c = bm_engine.expected_checks("number/set_value", {"value": "28"})
        self.assertEqual(bm_engine.check_state(
            "x", {"state": "28.0", "attributes": {}}, c), [])
        self.assertTrue(bm_engine.check_state(
            "x", {"state": "25", "attributes": {}}, c))

    def test_unavailable_fails(self):
        c = bm_engine.expected_checks("switch/turn_on", {})
        self.assertTrue(bm_engine.check_state(
            "x", {"state": "unavailable", "attributes": {}}, c))


if __name__ == "__main__":
    unittest.main()
