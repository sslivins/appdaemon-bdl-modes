"""Step definitions for the lodge's vacation / preheat / home modes.

Ported from BlackDiamondHub ``vacation_mode/steps.py``. Each operation is a
list of *groups*; each group is an ordered list of steps; each step is an
ordered list of HA service actions:

    {"action": "domain/service", "data": {...},
     "description": "...", "delay_after": s, "verify_delay": s}

Step keys:
    id        stable slug (used for skip_steps and status)
    alias     human label, icon  Font Awesome class
    retry     "quick"   - a few tries over ~1 min, then failed
              "patient" - quick tries, then keeps retrying in the background
                          (flaky devices, e.g. the hot tub controller)
              "once"    - no retries (waits)

Hardware notes (keep when editing):
  * The AECO-1988 is a changeover heat pump: flip the permanent-demand
    switches and wait MODE_SWITCH_DELAY before writing the active tank's
    number entities (the other mode's entities are ``unavailable``).
  * Zone thermostats are Nest units and are rate limited: one
    ``climate/set_temperature`` per action with a ``delay_after``; never batch
    them. The engine also enforces a minimum gap between climate writes.
"""

SEASON_HEATING = "heating"
SEASON_COOLING = "cooling"

ZONE_THERMOSTATS = [
    ("climate.master_bedroom", "Master Bedroom"),
    ("climate.basement_bunk_rooms", "Basement Bunk Rooms"),
    ("climate.main_floor", "Main Floor"),
    ("climate.basement_master", "Basement Master"),
    ("climate.ski_room", "Ski Room"),
]

# away heating matches the thermal battery's 12C idle setpoint
ZONE_SETPOINTS = {
    SEASON_HEATING: {
        "home": {"climate.ski_room": 19, "_default": 20},
        "away": {"_default": 12},
    },
    SEASON_COOLING: {
        "home": {"_default": 22},
        "away": {"_default": 28},
    },
}

OCCUPANCY_ENTITY = "input_select.bdl_occupancy"
AWAY_BOOLEAN = "input_boolean.home_away_mode_enabled"
_HEAT_DEMAND = "switch.aeco_1988_permanent_heat_demand"
_COOL_DEMAND = "switch.aeco_1988_permanent_cool_demand"
MODE_SWITCH_DELAY = 10

# pseudo action handled by the engine (not an HA service)
WAIT_THERMAL_BATTERY = "bdl_modes/wait_thermal_battery"


def _zone_setpoint(season, occupancy, entity_id):
    table = ZONE_SETPOINTS[season][occupancy]
    return table.get(entity_id, table["_default"])


def _step(sid, alias, icon, actions, retry="quick"):
    return {"id": sid, "alias": alias, "icon": icon, "actions": actions,
            "retry": retry}


def _occupancy_step(occupancy):
    """Select the occupancy and keep the legacy away boolean in step.

    The select is written first so the HA mirror automation (boolean ->
    select) never sees ``off`` while the select still says ``away``."""
    away = occupancy == "away"
    label = {"away": "Away", "preheat": "Preheating", "home": "Home"}
    return _step(
        "occupancy_" + occupancy, "Set Occupancy: %s" % label[occupancy],
        "fas fa-plane-departure" if away else "fas fa-house-flag",
        [
            {"action": "input_select/select_option",
             "data": {"entity_id": OCCUPANCY_ENTITY, "option": occupancy},
             "description": "Occupancy -> %s" % occupancy},
            {"action": "input_boolean/turn_%s" % ("on" if away else "off"),
             "data": {"entity_id": AWAY_BOOLEAN},
             "description": "%s away mode flag" % (
                 "Enabling" if away else "Disabling")},
        ])


def _wait_tb_step():
    return _step(
        "wait_thermal_battery", "Hand Over From Thermal Battery",
        "fas fa-battery-half",
        [{"action": WAIT_THERMAL_BATTERY, "data": {},
          "description": "Waiting for thermal battery to stand down"}],
        retry="once")


def _mode_changeover_actions(season):
    if season == SEASON_COOLING:
        off_sw, on_sw, label = _HEAT_DEMAND, _COOL_DEMAND, "cooling"
    else:
        off_sw, on_sw, label = _COOL_DEMAND, _HEAT_DEMAND, "heating"
    return [
        {"action": "switch/turn_off", "data": {"entity_id": off_sw},
         "description": "Clearing opposite demand (-> %s)" % label,
         "delay_after": 1},
        {"action": "switch/turn_on", "data": {"entity_id": on_sw},
         "description": "Setting system to %s mode" % label.upper(),
         "delay_after": MODE_SWITCH_DELAY},
    ]


def _num(entity_id, value, description):
    return {"action": "number/set_value",
            "data": {"entity_id": entity_id, "value": str(value)},
            "description": description, "verify_delay": 10}


def _heatpump_step(season, occupancy):
    actions = _mode_changeover_actions(season)
    if season == SEASON_HEATING:
        icon, alias = "fas fa-fire", "Heat Pump Setup - Heating"
        if occupancy == "home":
            actions += [
                {"action": "switch/turn_on",
                 "data": {"entity_id":
                          "switch.aeco_1988_hot_tank_outdoor_reset"},
                 "description": "Enabling outdoor reset", "delay_after": 2},
                _num("number.aeco_1988_hot_tank_outdoor_reset", -20,
                     "Setting outdoor reset to -20C"),
                _num("number.aeco_1988_hot_tank_min_temperature", 27,
                     "Setting hot tank min to 27C"),
                _num("number.aeco_1988_hot_tank_max_temperature", 38,
                     "Setting hot tank max to 38C"),
            ]
        else:
            actions += [
                {"action": "switch/turn_off",
                 "data": {"entity_id":
                          "switch.aeco_1988_hot_tank_outdoor_reset"},
                 "description": "Disabling outdoor reset", "delay_after": 2},
                _num("number.aeco_1988_hot_tank_target_temperature", 28,
                     "Setting hot tank target to 28C"),
                _num("number.aeco_1988_backup_differential", 8,
                     "Setting backup differential to 8C"),
            ]
    else:
        icon, alias = "fas fa-snowflake", "Heat Pump Setup - Cooling"
        # Cold target must stay >= 13.5C: the AECO clears cooling demand at
        # target - 1.5C and the heat pump's floor is 12C.
        cold = "14.4" if occupancy == "home" else "18"
        actions += [
            {"action": "switch/turn_off",
             "data": {"entity_id": "switch.aeco_1988_cold_tank_outdoor_reset"},
             "description": "Disabling cold tank outdoor reset",
             "delay_after": 2},
            _num("number.aeco_1988_cold_tank_target_temperature", cold,
                 "Setting cold tank target to %sC" % cold),
        ]
    return _step("heatpump_" + occupancy, alias, icon, actions)


def _thermostats_step(season, occupancy):
    hvac_mode = "cool" if season == SEASON_COOLING else "heat"
    actions = []
    if occupancy == "home":
        # Nest API is rate limited: one thermostat per call, never batched.
        for entity_id, name in ZONE_THERMOSTATS:
            actions.append({
                "action": "climate/set_preset_mode",
                "data": {"entity_id": entity_id, "preset_mode": "none"},
                "description": "Clearing %s preset" % name,
                "delay_after": 1})
    for entity_id, name in ZONE_THERMOSTATS:
        temp = _zone_setpoint(season, occupancy, entity_id)
        actions.append({
            "action": "climate/set_temperature",
            "data": {"entity_id": entity_id, "temperature": temp,
                     "hvac_mode": hvac_mode},
            "description": "%s -> %sC (%s)" % (name, temp, hvac_mode),
            "delay_after": 1})
    temps = sorted({_zone_setpoint(season, occupancy, e)
                    for e, _ in ZONE_THERMOSTATS})
    alias = "Set Thermostats to %s %s (%sC)" % (
        "Home" if occupancy == "home" else "Away",
        "Cool" if season == SEASON_COOLING else "Heat",
        "/".join("%g" % t for t in temps))
    icon = ("fas fa-snowflake" if season == SEASON_COOLING else
            "fas fa-house-chimney" if occupancy == "home" else
            "fas fa-temperature-arrow-down")
    return _step("thermostats_" + occupancy, alias, icon, actions)


def _preset(sid, alias, icon, entity_id, preset):
    return _step(sid, alias, icon, [
        {"action": "climate/set_preset_mode",
         "data": {"entity_id": entity_id, "preset_mode": preset},
         "description": "%s -> %s" % (entity_id, preset)}], retry="patient")


def _garage_step(temp):
    return _step("garage_%d" % temp, "Set Garage Heaters to %dC" % temp,
                 "fas fa-warehouse", [
        {"action": "climate/set_temperature",
         "data": {"entity_id": "climate.garage_thermostat_thermostat",
                  "temperature": temp},
         "description": "Garage -> %dC" % temp, "delay_after": 1},
        {"action": "climate/set_temperature",
         "data": {"entity_id": "climate.storage_room_thermostat_thermostat",
                  "temperature": temp},
         "description": "Storage Room -> %dC" % temp},
    ])


def _fridge_step(away):
    on, off = ("switch/turn_on", "switch/turn_off")
    return _step(
        "fridge_" + ("away" if away else "home"),
        "Set Fridge & Freezer to Vacation Mode" if away
        else "Setup Fridge/Freezer for Arrival", "fas fa-icicles", [
            {"action": on if away else off,
             "data": {"entity_id": "switch.freezer_eco_mode"},
             "description": "Freezer eco mode %s" % ("on" if away else "off")},
            {"action": off if away else on,
             "data": {"entity_id": "switch.freezer_dispenser"},
             "description": "Freezer dispenser %s" % (
                 "off" if away else "on")},
            {"action": on if away else off,
             "data": {"entity_id": "switch.refrigerator_eco_mode"},
             "description": "Fridge eco mode %s" % ("on" if away else "off")},
        ])


_MEDIA = [
    ("switch.living_room_tv_socket_1", "Living Room TV", 0),
    ("switch.living_room_tv_socket_2", "Living Room Sonos", 1),
    ("switch.basement_master_tv_socket_1", "Basement Master TV", 0),
    ("switch.media_room_tv_socket_1", "Games Room TV", 0),
    ("switch.media_room_tv_socket_2", "Games Room Sonos", 1),
    ("switch.office_tv_socket_1", "Office TV", 0),
    ("switch.office_tv_socket_2", "Office Sonos", 1),
    ("switch.master_bedroom_tv_socket_1", "Master Bedroom TV", 0),
    ("switch.master_bedroom_tv_socket_2", "Master Bedroom Sonos", 0),
]


def _media_step(on):
    acts = []
    for eid, name, delay in _MEDIA:
        a = {"action": "switch/turn_on" if on else "switch/turn_off",
             "data": {"entity_id": eid}, "description": name}
        if delay:
            a["delay_after"] = delay
        acts.append(a)
    return _step("media_" + ("on" if on else "off"),
                 "Turn %s Televisions & Sonos" % ("On" if on else "Off"),
                 "fas fa-tv", acts)


def _vacation_steps(season):
    return [
        _preset("water_heater_off", "Turn Off Water Heater", "fas fa-fire",
                "climate.econet_hpwh", "Off"),
        _preset("hot_tub_away", "Set Hot Tub to Vacation Mode",
                "fas fa-hot-tub-person", "climate.hot_tub_heater",
                "Away From Home"),
        _heatpump_step(season, "away"),
        _thermostats_step(season, "away"),
        _garage_step(5),
        _fridge_step(True),
        _media_step(False),
        _step("patio_off", "Turn Off Patio Heaters",
              "fas fa-fire-flame-curved", [
                  {"action": "switch/turn_off",
                   "data": {"entity_id": "switch.patio_deck_heaters"},
                   "description": "Patio deck heaters off"}]),
        _step("fireplaces_off", "Turn Off Fireplaces", "fas fa-fire", [
            {"action": "climate/set_hvac_mode",
             "data": {"entity_id": "climate.master_bedroom_2",
                      "hvac_mode": "off"},
             "description": "Master Bedroom fireplace heater off",
             "delay_after": 1},
            {"action": "switch/turn_off",
             "data": {"entity_id": "switch.master_bedroom"},
             "description": "Master Bedroom fireplace flames off",
             "delay_after": 1},
            {"action": "climate/set_hvac_mode",
             "data": {"entity_id": "climate.living_room_fireplace",
                      "hvac_mode": "off"},
             "description": "Living Room fireplace heater off",
             "delay_after": 1},
            {"action": "switch/turn_off",
             "data": {"entity_id": "switch.living_room_fireplace"},
             "description": "Living Room fireplace flames off"},
        ]),
        _occupancy_step("away"),
    ]


def build_groups(kind, season):
    """Ordered groups for an operation kind: list of (name, steps).

    vacation  -> [vacation]
    home      -> [house, amenities, arrive]   (all due now)
    preheat   -> [house, amenities, arrive]   (due at arrival - leads)
    """
    if kind == "vacation":
        return [("vacation", _vacation_steps(season))]
    house = [
        _occupancy_step("preheat" if kind == "preheat" else "home"),
        _wait_tb_step(),
        _heatpump_step(season, "home"),
        _thermostats_step(season, "home"),
    ]
    amenities = [
        _preset("water_heater_on", "Start Water Heater", "fas fa-fire",
                "climate.econet_hpwh", "Eco Mode"),
        _preset("hot_tub_on", "Start Hot Tub", "fas fa-hot-tub-person",
                "climate.hot_tub_heater", "Standard"),
    ]
    arrive = [_garage_step(10), _fridge_step(False), _media_step(True)]
    if kind == "preheat":
        arrive.append(_occupancy_step("home"))
    return [("house", house), ("amenities", amenities), ("arrive", arrive)]


GROUP_LABELS = {
    "vacation": "Vacation mode",
    "house": "Heat the house",
    "amenities": "Hot tub & water heater",
    "arrive": "Arrival",
}
