# appdaemon-bdl-modes

AppDaemon app that switches the Black Diamond Lodge (Sun Peaks) between
**vacation**, **preheat** and **home**. It runs the steps in the background,
retries flaky devices and sends a phone notification only when something
finally fails.

The [BlackDiamondHub](https://github.com/sslivins/BlackDiamondHub) UI is a
thin client: it sends commands to this app's AppDaemon endpoint and shows
the progress the app reports.

## Operations

| kind | groups | when |
|---|---|---|
| `vacation` | vacation | now |
| `home` | house, amenities, arrive | now |
| `preheat` | house, amenities, arrive | house at arrival - 24 h, amenities (hot tub, water heater) at arrival - 6 h, arrive at arrival |

* **house** sets occupancy (`input_select.bdl_occupancy` = `preheat`/`home`)
  and turns the legacy away boolean off. It then waits until the thermal
  battery app reports `quiesced` (its sessions have ended and its setpoints
  are restored), and only then sets up the heat pump and thermostats.
* **amenities**: water heater and hot tub.
* **arrive**: garage, fridge/freezer, TVs/Sonos, then occupancy `home`.

### Rules

* Starting any operation supersedes the current one. A generation counter
  stops stale work, including background retries.
* `cancel` stops pending work and **never rolls anything back**. To go back
  to vacation, start `vacation` explicitly.
* A preheat whose arrival has passed by more than `arrive_grace_h` skips the
  remaining groups and sends a notification, instead of running late.

### Retries

* Every step gets quick retries at 5, 15 and 30 s. Only the actions that
  failed are retried.
* The water heater and hot tub (`retry: patient`) then keep retrying in the
  background every 2, 5 and then 10 min for up to 1 h. Other groups are not
  blocked while this happens.
* Each group sends at most one notification, and only on failure or skip.
  The "notified" flag survives restarts.
* Each write is verified against HA state, as the hub does.
* **Nest rate limit:** climate writes are never batched and are spaced at
  least `climate_gap_s` apart. Every `delay_after` is honoured, even after a
  failure.

State lives in `bdl_modes.db` (SQLite). After an AppDaemon restart, anything
that was mid-flight is re-run; every action is an idempotent set-to-value.

## Endpoint

`POST http://<appdaemon>:5050/api/appdaemon/bdl_modes`, with header
`X-API-Key: <bdl_modes_api_key>` and a JSON body:

| `cmd` | extra fields |
|---|---|
| `status` | - |
| `preview` | `kind`, optional `season` |
| `vacation` / `home` | optional `skip_steps`, `request_id`, `dry_run` |
| `preheat` | `arrival` (ISO time, lodge local if no offset; or epoch), optional `skip_steps`, `request_id` |
| `update_arrival` | `arrival` |
| `arrive_now` | - |
| `cancel` | - |

Every command returns the status payload. The app also publishes
`sensor.bdl_modes_status` to HA every 30 s.

## Install

1. Copy `bdl_modes.py`, `bm_engine.py`, `bm_steps.py` and `bdl_modes.yaml`
   to `conf/apps/bdl_modes/`.
2. Add these to `conf/secrets.yaml`:
   * `bdl_modes_api_key`: a random string; the hub uses the same value.
   * `bdl_modes_ha_token`: a long-lived HA token.
3. Merge `install/logs.yaml` into the `logs:` section of `appdaemon.yaml`.
4. HA helpers: `input_select.bdl_occupancy` (home/preheat/away),
   `input_datetime.bdl_arrival`, and the template
   `binary_sensor.bdl_unoccupied` (on unless occupancy is `home`).
5. The app starts with `dry_run: true`. Set it to `false` once the hub is
   wired up.

## Tests

```
python -m unittest discover -s tests
```
