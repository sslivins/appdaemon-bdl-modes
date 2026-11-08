"""bdl_modes - background runner for the lodge's vacation / preheat / home
modes (Black Diamond Lodge, Sun Peaks).

The BlackDiamondHub UI calls this app's AppDaemon endpoint
(``/api/appdaemon/bdl_modes``); the engine (``bm_engine``) runs the steps on
its own thread, retries flaky devices in the background and pushes a phone
notification only when something finally fails. See README.md.
"""

import datetime as dt
import hmac
import json
import os
import sqlite3
import threading
import time

import requests

try:
    import appdaemon.plugins.hass.hassapi as hass
except ImportError:  # unit tests
    hass = None

import bm_engine
import bm_steps

STATUS_ENTITY = "sensor.bdl_modes_status"


class HAClient:
    """Minimal HA REST client (independent of AppDaemon's state cache, which
    misses entities created after AppDaemon started)."""

    def __init__(self, url, token, timeout=20):
        self.url = url.rstrip("/")
        self.h = {"Authorization": "Bearer %s" % token}
        self.timeout = timeout

    def call(self, action, data):
        domain, service = action.split("/")
        try:
            r = requests.post("%s/api/services/%s/%s" % (
                self.url, domain, service), headers=self.h, json=data,
                timeout=self.timeout)
        except requests.RequestException as exc:
            return False, str(exc)
        if r.status_code in (200, 201):
            return True, None
        return False, "HTTP %s: %s" % (r.status_code, r.text[:200])

    def get(self, entity_id):
        try:
            r = requests.get("%s/api/states/%s" % (self.url, entity_id),
                             headers=self.h, timeout=self.timeout)
        except requests.RequestException:
            return None
        return r.json() if r.status_code == 200 else None

    def set_state(self, entity_id, state, attributes):
        try:
            requests.post("%s/api/states/%s" % (self.url, entity_id),
                          headers=self.h, timeout=self.timeout,
                          json={"state": state, "attributes": attributes})
        except requests.RequestException:
            pass


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY,"
                        " v TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS ops (id TEXT PRIMARY KEY,"
                        " kind TEXT, created REAL, status TEXT, body TEXT)")
        self.db.commit()
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            r = self.db.execute("SELECT v FROM kv WHERE k='engine'").fetchone()
        return json.loads(r[0]) if r else None

    def save(self, state):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES ('engine', ?)",
                            (json.dumps(state),))
            self.db.commit()

    def archive(self, op):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO ops VALUES (?,?,?,?,?)",
                            (op["id"], op["kind"], op["created"],
                             op["status"], json.dumps(op)))
            self.db.commit()

    def history(self, n=10):
        with self.lock:
            rows = self.db.execute(
                "SELECT id, kind, created, status FROM ops ORDER BY created"
                " DESC LIMIT ?", (n,)).fetchall()
        return [{"id": r[0], "kind": r[1], "created": r[2], "status": r[3]}
                for r in rows]


_Base = hass.Hass if hass else object


class BdlModes(_Base):
    def initialize(self):
        a = self.args
        self.tz = dt.timezone.utc
        try:
            import zoneinfo
            self.tz = zoneinfo.ZoneInfo(a.get("timezone",
                                              "America/Vancouver"))
        except Exception:  # noqa: BLE001
            pass
        self.api_key = str(a["api_key"])
        self.dry_run = bool(a.get("dry_run", True))
        self.season_entity = a.get(
            "season_entity", "sensor.hvac_load_shift_metrics_outdoor_avg_day")
        self.season_threshold = float(a.get("season_threshold_c", 15.0))
        self.arrival_entity = a.get("arrival_entity",
                                    "input_datetime.bdl_arrival")
        self.remote_notify = [r for r in (a.get("remote_notify") or [])
                              if r.get("url") and r.get("token")]
        self.ha = HAClient(a["ha_url"], a["ha_token"])
        here = os.path.dirname(os.path.abspath(__file__))
        self.store = Store(os.path.join(here, a.get("db", "bdl_modes.db")))
        self.engine = bm_engine.Engine(
            self.ha, self.store, self._push, self._say, config=a.get("engine"))
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="bdl_modes")
        self._thread.start()
        self.register_endpoint(self._endpoint, "bdl_modes")
        self.run_every(self._publish, "now+5", 30)
        self._say("initialized (dry_run=%s)" % self.dry_run)

    def terminate(self):
        self.engine.stop()
        self._thread.join(timeout=15)

    def _loop(self):
        self.engine.loop()
        self._say("engine thread stopped")

    def _say(self, msg, level="INFO"):
        self.log(msg, level=level)

    # ------------------------------------------------------------ endpoint
    def _endpoint(self, data, kwargs):
        req = kwargs.get("request")
        key = req.headers.get("X-API-Key", "") if req is not None else ""
        if not hmac.compare_digest(str(key), self.api_key):
            return {"error": "unauthorized"}, 401
        data = dict(data or {})
        cmd = data.get("cmd", "status")
        try:
            return self._command(cmd, data), 200
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except Exception as exc:  # noqa: BLE001
            self._say("endpoint %s failed: %s" % (cmd, exc), "ERROR")
            return {"error": "internal error"}, 500

    def _command(self, cmd, data):
        e = self.engine
        dry = self.dry_run or bool(data.get("dry_run"))
        rid = data.get("request_id")
        if cmd == "status":
            return self._status_payload()
        if cmd == "preview":
            kind = data.get("kind", "home")
            season = data.get("season") or self._season()[0]
            return {"season": season, "groups": [
                {"name": n, "label": bm_steps.GROUP_LABELS[n],
                 "steps": [{"id": s["id"], "alias": s["alias"],
                            "icon": s["icon"]} for s in steps]}
                for n, steps in bm_steps.build_groups(kind, season)]}
        if cmd in ("vacation", "home"):
            season = self._season()[0]
            e.start(cmd, season, dry_run=dry,
                    skip_steps=data.get("skip_steps"), request_id=rid)
        elif cmd == "preheat":
            arrival = self._parse_ts(data.get("arrival"))
            season = self._season()[0]
            e.start("preheat", season, arrival=arrival, dry_run=dry,
                    skip_steps=data.get("skip_steps"), request_id=rid)
            self._mirror_arrival(arrival)
        elif cmd == "update_arrival":
            arrival = self._parse_ts(data.get("arrival"))
            e.update_arrival(arrival)
            self._mirror_arrival(arrival)
        elif cmd == "arrive_now":
            e.arrive_now(self._season()[0])
        elif cmd == "cancel":
            e.cancel()
        else:
            raise ValueError("unknown cmd %r" % cmd)
        self._publish({})
        return self._status_payload()

    def _parse_ts(self, v):
        if v is None:
            raise ValueError("arrival is required")
        if isinstance(v, (int, float)):
            return float(v)
        t = dt.datetime.fromisoformat(str(v))
        if t.tzinfo is None:
            t = t.replace(tzinfo=self.tz)
        return t.timestamp()

    def _season(self):
        st = self.ha.get(self.season_entity)
        try:
            avg = float(st["state"])
        except (TypeError, KeyError, ValueError):
            return bm_steps.SEASON_HEATING, None
        return (bm_steps.SEASON_COOLING if avg >= self.season_threshold
                else bm_steps.SEASON_HEATING), avg

    def _mirror_arrival(self, ts):
        local = dt.datetime.fromtimestamp(ts, self.tz)
        self.ha.call("input_datetime/set_datetime", {
            "entity_id": self.arrival_entity,
            "datetime": local.strftime("%Y-%m-%d %H:%M:%S")})

    # -------------------------------------------------------------- status
    def _iso(self, ts):
        return (dt.datetime.fromtimestamp(ts, self.tz).isoformat()
                if ts else None)

    def _status_payload(self):
        op = self.engine.status()
        occ = self.ha.get(bm_steps.OCCUPANCY_ENTITY) or {}
        season, avg = self._season()
        out = {"active": None, "history": self.store.history(5),
               "dry_run": self.dry_run, "server_time": self._iso(time.time()),
               "occupancy": occ.get("state"), "season": season,
               "season_avg": avg,
               "leads_h": {k: self.engine.cfg[k + "_lead_h"]
                           for k in ("house", "amenities")}}
        if op:
            op = dict(op)
            op["arrival_iso"] = self._iso(op.get("arrival"))
            for g in op["groups"]:
                g["due_iso"] = self._iso(g["due"])
                for s in g["steps"]:
                    s["next_retry_iso"] = self._iso(s.get("next_retry"))
                    s.pop("actions", None)
            out["active"] = op
        return out

    def _publish(self, kwargs):
        op = self.engine.status()
        if not op:
            self.ha.set_state(STATUS_ENTITY, "idle",
                              {"friendly_name": "BDL Modes"})
            return
        nxt = [g for g in op["groups"] if g["status"] == "pending"]
        retrying = [s["alias"] for g in op["groups"] for s in g["steps"]
                    if s["status"] == "retrying"]
        failed = [s["alias"] for g in op["groups"] for s in g["steps"]
                  if s["status"] == "failed"]
        self.ha.set_state(STATUS_ENTITY, op["status"], {
            "friendly_name": "BDL Modes", "icon": "mdi:home-clock",
            "kind": op["kind"], "arrival": self._iso(op.get("arrival")),
            "next_group": nxt[0]["label"] if nxt else None,
            "next_due": self._iso(nxt[0]["due"]) if nxt else None,
            "retrying": retrying, "failed": failed,
            "issues": bm_engine.unresolved(op, self.ha.get),
            "dry_run": op["dry_run"]})

    # -------------------------------------------------------------- notify
    def _push(self, title, message):
        self._say("NOTIFY %s: %s" % (title, message), "WARNING")
        for r in self.remote_notify:
            svc = str(r["service"]).replace("notify.", "").replace(
                "notify/", "")
            try:
                resp = requests.post(
                    "%s/api/services/notify/%s" % (
                        str(r["url"]).rstrip("/"), svc),
                    headers={"Authorization": "Bearer %s" % r["token"]},
                    json={"title": title, "message": message}, timeout=10)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                self._say("remote push via %s failed: %s" % (r["url"], exc),
                          "ERROR")
