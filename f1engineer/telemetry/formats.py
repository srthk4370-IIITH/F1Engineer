"""All F1 game UDP formats (F1 2020 .. F1 25 / 2026 season) -> one canonical packet shape.

The rest of the engineer only ever sees the F1 2020 layout (`packets_2020`): every newer format is parsed with its own
layout and converted field by field (renames, unit changes, data that moved to other packets). Layouts follow the
official EA/Codemasters UDP specifications; each per-car packet is checked against the official total size, and a
mismatch is logged once and skipped (never parsed as garbage).

Format -> what changed (that matters here):
  2021  lap times in ms, best lap gone from LapData, buttons moved to a BUTN event, damage/wear moved to CarDamage (10),
        revLightsBitValue in telemetry, more session types (Race 3) and weather samples (8 bytes, 56)
  2022  LapData gains time-trial PB/rival car indexes; CarDamage gains ersFault/engineBlown/engineSeized
  2023  29-byte header (gameYear, overallFrameIdentifier); player motion extras moved to MotionEx (13);
        sector times split into ms+minutes; deltas to car in front/leader; engine power in CarStatus
  2024  sprint shootout session types (renumbered), LapData minute parts + speed trap, engineBraking in setups,
        64 weather samples, MotionEx aero/roll fields, techLevel in participants
  2025  participant names 32 chars + livery colours, tyre blisters in CarDamage, MotionEx camber fields
  2026  (F1 25's 2026-season update) assumed to keep the 2025 layout; size checks will tell
"""
from __future__ import annotations

import numpy as np

from f1engineer.telemetry import packets_2020 as C   # canonical layout

N = 22
GAMES = {2020: "F1 2020", 2021: "F1 2021", 2022: "F1 22", 2023: "F1 23", 2024: "F1 24", 2025: "F1 25", 2026: "F1 25 (2026 season)"}
SUPPORTED = tuple(GAMES)

H20 = C.HEADER
H23 = np.dtype([
    ("packetFormat", "<u2"), ("gameYear", "u1"), ("gameMajorVersion", "u1"), ("gameMinorVersion", "u1"),
    ("packetVersion", "u1"), ("packetId", "u1"), ("sessionUID", "<u8"), ("sessionTime", "<f4"),
    ("frameIdentifier", "<u4"), ("overallFrameIdentifier", "<u4"), ("playerCarIndex", "u1"),
    ("secondaryPlayerCarIndex", "u1"),
])
assert H20.itemsize == 24 and H23.itemsize == 29

U1 = "u1"
_LAP_TAIL = [(n, U1) for n in ("carPosition", "currentLapNum", "pitStatus", "numPitStops", "sector", "currentLapInvalid",
                                "penalties")]
LAP21 = np.dtype([("lastLapTimeInMS", "<u4"), ("currentLapTimeInMS", "<u4"), ("sector1TimeInMS", "<u2"),
                  ("sector2TimeInMS", "<u2"), ("lapDistance", "<f4"), ("totalDistance", "<f4"), ("safetyCarDelta", "<f4"),
                  *_LAP_TAIL, ("warnings", U1), ("numUnservedDriveThroughPens", U1), ("numUnservedStopGoPens", U1),
                  ("gridPosition", U1), ("driverStatus", U1), ("resultStatus", U1), ("pitLaneTimerActive", U1),
                  ("pitLaneTimeInLaneInMS", "<u2"), ("pitStopTimerInMS", "<u2"), ("pitStopShouldServePen", U1)])
LAP23 = np.dtype([("lastLapTimeInMS", "<u4"), ("currentLapTimeInMS", "<u4"), ("sector1TimeInMS", "<u2"),
                  ("sector1TimeMinutes", U1), ("sector2TimeInMS", "<u2"), ("sector2TimeMinutes", U1),
                  ("deltaToCarInFrontInMS", "<u2"), ("deltaToRaceLeaderInMS", "<u2"),
                  ("lapDistance", "<f4"), ("totalDistance", "<f4"), ("safetyCarDelta", "<f4"),
                  *_LAP_TAIL, ("totalWarnings", U1), ("cornerCuttingWarnings", U1), ("numUnservedDriveThroughPens", U1),
                  ("numUnservedStopGoPens", U1), ("gridPosition", U1), ("driverStatus", U1), ("resultStatus", U1),
                  ("pitLaneTimerActive", U1), ("pitLaneTimeInLaneInMS", "<u2"), ("pitStopTimerInMS", "<u2"),
                  ("pitStopShouldServePen", U1)])
LAP24 = np.dtype([("lastLapTimeInMS", "<u4"), ("currentLapTimeInMS", "<u4"), ("sector1TimeInMS", "<u2"),
                  ("sector1TimeMinutes", U1), ("sector2TimeInMS", "<u2"), ("sector2TimeMinutes", U1),
                  ("deltaToCarInFrontInMS", "<u2"), ("deltaToCarInFrontMinutes", U1), ("deltaToRaceLeaderInMS", "<u2"),
                  ("deltaToRaceLeaderMinutes", U1), ("lapDistance", "<f4"), ("totalDistance", "<f4"),
                  ("safetyCarDelta", "<f4"), *_LAP_TAIL, ("totalWarnings", U1), ("cornerCuttingWarnings", U1),
                  ("numUnservedDriveThroughPens", U1), ("numUnservedStopGoPens", U1), ("gridPosition", U1),
                  ("driverStatus", U1), ("resultStatus", U1), ("pitLaneTimerActive", U1), ("pitLaneTimeInLaneInMS", "<u2"),
                  ("pitStopTimerInMS", "<u2"), ("pitStopShouldServePen", U1), ("speedTrapFastestSpeed", "<f4"),
                  ("speedTrapFastestLap", U1)])
assert (LAP21.itemsize, LAP23.itemsize, LAP24.itemsize) == (43, 50, 57)

TEL21 = np.dtype([("speed", "<u2"), ("throttle", "<f4"), ("steer", "<f4"), ("brake", "<f4"), ("clutch", U1), ("gear", "i1"),
                  ("engineRPM", "<u2"), ("drs", U1), ("revLightsPercent", U1), ("revLightsBitValue", "<u2"),
                  ("brakesTemperature", "<u2", (4,)), ("tyresSurfaceTemperature", U1, (4,)),
                  ("tyresInnerTemperature", U1, (4,)), ("engineTemperature", "<u2"), ("tyresPressure", "<f4", (4,)),
                  ("surfaceType", U1, (4,))])
assert TEL21.itemsize == 60

_STATUS_HEAD = [("tractionControl", U1), ("antiLockBrakes", U1), ("fuelMix", U1), ("frontBrakeBias", U1),
                ("pitLimiterStatus", U1), ("fuelInTank", "<f4"), ("fuelCapacity", "<f4"), ("fuelRemainingLaps", "<f4"),
                ("maxRPM", "<u2"), ("idleRPM", "<u2"), ("maxGears", U1), ("drsAllowed", U1), ("drsActivationDistance", "<u2"),
                ("actualTyreCompound", U1), ("visualTyreCompound", U1), ("tyresAgeLaps", U1), ("vehicleFiaFlags", "i1")]
_ERS = [("ersStoreEnergy", "<f4"), ("ersDeployMode", U1), ("ersHarvestedThisLapMGUK", "<f4"),
        ("ersHarvestedThisLapMGUH", "<f4"), ("ersDeployedThisLap", "<f4"), ("networkPaused", U1)]
STATUS21 = np.dtype(_STATUS_HEAD + _ERS)
STATUS23 = np.dtype(_STATUS_HEAD + [("enginePowerICE", "<f4"), ("enginePowerMGUK", "<f4")] + _ERS)
assert (STATUS21.itemsize, STATUS23.itemsize) == (47, 55)

_DMG_TAIL21 = [(n, U1) for n in ("frontLeftWingDamage", "frontRightWingDamage", "rearWingDamage", "floorDamage",
                                  "diffuserDamage", "sidepodDamage", "drsFault", "gearBoxDamage", "engineDamage",
                                  "engineMGUHWear", "engineESWear", "engineCEWear", "engineICEWear", "engineMGUKWear",
                                  "engineTCWear")]
_DMG_TAIL22 = [(n, U1) for n in ("frontLeftWingDamage", "frontRightWingDamage", "rearWingDamage", "floorDamage",
                                  "diffuserDamage", "sidepodDamage", "drsFault", "ersFault", "gearBoxDamage", "engineDamage",
                                  "engineMGUHWear", "engineESWear", "engineCEWear", "engineICEWear", "engineMGUKWear",
                                  "engineTCWear", "engineBlown", "engineSeized")]
_WEAR = [("tyresWear", "<f4", (4,)), ("tyresDamage", U1, (4,)), ("brakesDamage", U1, (4,))]
DMG21 = np.dtype(_WEAR + _DMG_TAIL21)
DMG22 = np.dtype(_WEAR + _DMG_TAIL22)
DMG25 = np.dtype(_WEAR + [("tyreBlisters", U1, (4,))] + _DMG_TAIL22)
assert (DMG21.itemsize, DMG22.itemsize, DMG25.itemsize) == (39, 42, 46)

_PART = [("aiControlled", U1), ("driverId", U1), ("networkId", U1), ("teamId", U1), ("myTeam", U1), ("raceNumber", U1),
         ("nationality", U1)]
PART21 = np.dtype(_PART + [("name", "S48"), ("yourTelemetry", U1)])
PART23 = np.dtype(_PART + [("name", "S48"), ("yourTelemetry", U1), ("showOnlineNames", U1), ("platform", U1)])
PART24 = np.dtype(_PART + [("name", "S48"), ("yourTelemetry", U1), ("showOnlineNames", U1), ("techLevel", "<u2"),
                           ("platform", U1)])
PART25 = np.dtype(_PART + [("name", "S32"), ("yourTelemetry", U1), ("showOnlineNames", U1), ("techLevel", "<u2"),
                           ("platform", U1), ("numColours", U1), ("liveryColours", U1, (12,))])
assert (PART21.itemsize, PART23.itemsize, PART24.itemsize, PART25.itemsize) == (56, 58, 60, 57)

_SETUP = list(C.CAR_SETUP.descr)
_bb = [n for n, *_ in _SETUP].index("brakeBias") + 1
SETUP24 = np.dtype(_SETUP[:_bb] + [("engineBraking", U1)] + _SETUP[_bb:])
assert SETUP24.itemsize == 50

# MotionEx (2023+): only the leading fields are needed; the rest is padding up to the official size
MOTION_EX_HEAD = np.dtype([("suspensionPosition", "<f4", (4,)), ("suspensionVelocity", "<f4", (4,)),
                           ("suspensionAcceleration", "<f4", (4,)), ("wheelSpeed", "<f4", (4,)),
                           ("wheelSlipRatio", "<f4", (4,)), ("wheelSlipAngle", "<f4", (4,)), ("wheelLatForce", "<f4", (4,)),
                           ("wheelLongForce", "<f4", (4,)), ("heightOfCOGAboveGround", "<f4"),
                           ("localVelocityX", "<f4"), ("localVelocityY", "<f4"), ("localVelocityZ", "<f4"),
                           ("angularVelocityX", "<f4"), ("angularVelocityY", "<f4"), ("angularVelocityZ", "<f4"),
                           ("angularAccelerationX", "<f4"), ("angularAccelerationY", "<f4"), ("angularAccelerationZ", "<f4"),
                           ("frontWheelsAngle", "<f4"), ("wheelVertForce", "<f4", (4,))])
assert MOTION_EX_HEAD.itemsize == 188

# session prefix is identical in every year; forecast samples grew from 5 to 8 bytes
SAMPLE21 = np.dtype([("sessionType", U1), ("timeOffset", U1), ("weather", U1), ("trackTemperature", "i1"),
                     ("trackTemperatureChange", "i1"), ("airTemperature", "i1"), ("airTemperatureChange", "i1"),
                     ("rainPercentage", U1)])
_SESSION_PREFIX = [d for d in C.SESSION.descr if d[0] not in ("header", "weatherForecastSamples")]
_SESSION_NAMES = [d[0] for d in _SESSION_PREFIX]

# canonical session-type numbering = F1 2020's, extended with the newer kinds
C.SESSION_TYPES.update({13: "Race 3", 14: "Sprint Shootout 1", 15: "Sprint Shootout 2", 16: "Sprint Shootout 3",
                        17: "Short Sprint Shootout", 18: "One-Shot Sprint Shootout"})
SESSION_MAP = {
    2021: {12: 13, 13: 12}, 2022: {12: 13, 13: 12}, 2023: {12: 13, 13: 12},
    2024: {10: 14, 11: 15, 12: 16, 13: 17, 14: 18, 15: 10, 16: 11, 17: 13, 18: 12},
}
SESSION_MAP[2025] = SESSION_MAP[2026] = SESSION_MAP[2024]
C.TRACKS.update({27: "Imola", 28: "Portimao", 29: "Jeddah", 30: "Miami", 31: "Las Vegas", 32: "Losail",
                 39: "Silverstone Reverse", 40: "Austria Reverse", 41: "Zandvoort Reverse"})


def _layout(fmt: int) -> dict:
    """packetId -> (raw dtype, official size) for one format."""
    new_hdr = fmt >= 2023
    H = H23 if new_hdr else H20
    lap = LAP21 if fmt <= 2022 else LAP23 if fmt == 2023 else LAP24
    lap_tail = [("timeTrialPBCarIdx", U1), ("timeTrialRivalCarIdx", U1)] if fmt >= 2022 else []
    part = PART21 if fmt <= 2022 else PART23 if fmt == 2023 else PART24 if fmt == 2024 else PART25
    dmg = DMG21 if fmt == 2021 else DMG22 if fmt <= 2024 else DMG25
    setup = C.CAR_SETUP if fmt <= 2023 else SETUP24
    nsamp = 56 if fmt <= 2023 else 64
    out = {
        2: (np.dtype([("header", H), ("lapData", lap, (N,))] + lap_tail), None),
        6: (np.dtype([("header", H), ("carTelemetryData", TEL21, (N,)), ("mfdPanelIndex", U1),
                      ("mfdPanelIndexSecondaryPlayer", U1), ("suggestedGear", "i1")]), None),
        7: (np.dtype([("header", H), ("carStatusData", STATUS21 if fmt <= 2022 else STATUS23, (N,))]), None),
        10: (np.dtype([("header", H), ("carDamageData", dmg, (N,))]), None),
        4: (np.dtype([("header", H), ("numActiveCars", U1), ("participants", part, (N,))]), None),
        5: (np.dtype([("header", H), ("carSetups", setup, (N,))] + ([("nextFrontWingValue", "<f4")] if fmt >= 2024 else [])),
            None),
        1: (np.dtype([("header", H)] + _SESSION_PREFIX + [("weatherForecastSamples", SAMPLE21, (nsamp,))]), "min"),
        3: (np.dtype([("header", H), ("eventStringCode", "S4"), ("details", U1, (12,))]), "min"),
    }
    if new_hdr:
        out[0] = (np.dtype([("header", H), ("carMotionData", C.CAR_MOTION, (N,))]), None)
        ex = {2023: 217, 2024: 237}.get(fmt, 273)
        pad = [("pad", f"V{ex - 29 - 188}")] if ex > 29 + 188 else []
        out[13] = (np.dtype([("header", H), ("ex", MOTION_EX_HEAD)] + pad), None)
    else:
        out[0] = (np.dtype([("header", H)] + C.MOTION.descr[1:]), None)
    return out


# official sizes (bytes) of the packets we parse strictly
OFFICIAL = {
    2021: {0: 1464, 2: 970, 4: 1257, 5: 1102, 6: 1347, 7: 1058, 10: 882},
    2022: {0: 1464, 2: 972, 4: 1257, 5: 1102, 6: 1347, 7: 1058, 10: 948},
    2023: {0: 1349, 2: 1131, 4: 1306, 5: 1107, 6: 1352, 7: 1239, 10: 953, 13: 217},
    2024: {0: 1349, 2: 1285, 4: 1350, 5: 1133, 6: 1352, 7: 1239, 10: 953, 13: 237},
    2025: {0: 1349, 2: 1285, 4: 1284, 5: 1133, 6: 1352, 7: 1239, 10: 1041, 13: 273},
}
OFFICIAL[2026] = OFFICIAL[2025]
for _f, _sizes in OFFICIAL.items():
    _lay = _layout(_f)
    for _pid, _size in _sizes.items():
        assert _lay[_pid][0].itemsize == _size, (_f, _pid, _lay[_pid][0].itemsize, _size)


_COMMON: dict = {}


def _copy(dst, src, names=None):
    key = (dst.dtype, src.dtype, tuple(names) if names else None)
    common = _COMMON.get(key)
    if common is None:   # field-name intersection is computed once per layout pair
        common = _COMMON[key] = [n for n in (names or src.dtype.names) if n in dst.dtype.names]
    for n in common:
        dst[n] = src[n]


class Adapter:
    """Stateful converter for one packet format (data that moved between packets is cached and merged back)."""

    def __init__(self, fmt: int, log=print):
        self.fmt = fmt
        self.layout = _layout(fmt)
        self.pid_offset = 6 if fmt >= 2023 else 5
        self.log = log
        self.warned: set = set()
        self.buttons = 0
        self.damage = None            # last CarDamage array
        self.motion_ex = None         # last MotionEx record
        self.best = np.zeros(N, np.float32)
        self.last_seen = np.zeros(N, np.float32)
        self.uid = None
        self.game = GAMES.get(fmt, f"F1 ({fmt})")

    def _warn(self, key, msg):
        if key not in self.warned:
            self.warned.add(key)
            self.log(f"[telemetry] {self.game}: {msg}")

    def _header(self, raw) -> np.ndarray:
        h = np.zeros((), C.HEADER)
        _copy(h, raw)
        if "gameYear" in raw.dtype.names and raw["gameYear"]:
            self.game = f"F1 {int(raw['gameYear'])}" if int(raw["gameYear"]) < 100 else self.game
        uid = int(raw["sessionUID"])
        if uid != self.uid:
            self.uid = uid
            self.best[:] = 0
            self.last_seen[:] = 0
        return h

    def parse(self, data: bytes):
        if len(data) <= self.pid_offset:
            return None
        pid = data[self.pid_offset]
        entry = self.layout.get(pid)
        if entry is None:
            return (pid, None) if pid < 16 else None   # a real packet we don't need (lobby, history, tyre sets...)
        dt, mode = entry
        if mode == "min":   # variable tail (forecast samples / event details): pad or trim to our layout
            need = dt.fields["numWeatherForecastSamples"][1] + 1 if pid == 1 else dt.fields["eventStringCode"][1] + 4
            if len(data) < need:
                return None
            buf = data[:dt.itemsize] if len(data) >= dt.itemsize else data + bytes(dt.itemsize - len(data))
        else:
            if len(data) != dt.itemsize:
                self._warn(("size", pid), f"packet {pid} is {len(data)} bytes, expected {dt.itemsize}: skipped "
                                          "(layout differs from the spec this was built from - please report)")
                return None
            buf = data
        raw = np.frombuffer(buf, dtype=dt, count=1)[0]
        conv = getattr(self, f"_p{pid}", None)
        return conv(raw) if conv else None

    # ---- converters (return (canonical pid, canonical record) or None) ----
    def _p0(self, raw):
        out = np.zeros((), C.MOTION)
        out["header"] = self._header(raw["header"])
        out["carMotionData"] = raw["carMotionData"]
        if "ex" in raw.dtype.names or self.fmt >= 2023:
            ex = self.motion_ex
            if ex is not None:
                _copy(out, ex)
                out["wheelSlip"] = ex["wheelSlipRatio"]
        else:
            _copy(out, raw)
        return 0, out[()]

    def _p13(self, raw):
        self.motion_ex = raw["ex"].copy()
        return 13, None

    def _p1(self, raw):
        out = np.zeros((), C.SESSION)
        out["header"] = self._header(raw["header"])
        _copy(out, raw, _SESSION_NAMES)
        smap = SESSION_MAP.get(self.fmt, {})
        out["sessionType"] = smap.get(int(raw["sessionType"]), int(raw["sessionType"]))
        n = min(int(raw["numWeatherForecastSamples"]), 20, len(raw["weatherForecastSamples"]))
        out["numWeatherForecastSamples"] = n
        for k in ("sessionType", "timeOffset", "weather", "trackTemperature", "airTemperature"):
            out["weatherForecastSamples"][k][:n] = raw["weatherForecastSamples"][k][:n]
        if smap:
            st = out["weatherForecastSamples"]["sessionType"]
            st[:n] = [smap.get(int(v), int(v)) for v in st[:n]]
        return 1, out[()]

    def _p2(self, raw):
        out = np.zeros((), C.LAP_DATA)
        out["header"] = self._header(raw["header"])
        src, dst = raw["lapData"], out["lapData"]
        _copy(dst, src)
        last = src["lastLapTimeInMS"] / 1000.0
        dst["lastLapTime"] = last
        dst["currentLapTime"] = src["currentLapTimeInMS"] / 1000.0
        if "sector1TimeMinutes" in src.dtype.names:
            dst["sector1TimeInMS"] = np.minimum(src["sector1TimeInMS"] + src["sector1TimeMinutes"].astype(np.int64) * 60000, 65535)
            dst["sector2TimeInMS"] = np.minimum(src["sector2TimeInMS"] + src["sector2TimeMinutes"].astype(np.int64) * 60000, 65535)
        # best lap is no longer sent in LapData: track it from completed laps
        new = (last > 0) & (last != self.last_seen)
        self.last_seen[:] = last
        upd = new & ((self.best == 0) | (last < self.best))
        self.best[upd] = last[upd]
        dst["bestLapTime"] = self.best
        return 2, out[()]

    def _p3(self, raw):
        code = bytes(raw["eventStringCode"])
        if code == b"BUTN":
            self.buttons = int.from_bytes(bytes(raw["details"][:4]), "little")
        out = np.zeros((), C.EVENT)
        out["header"] = self._header(raw["header"])
        out["eventStringCode"] = raw["eventStringCode"]
        out["details"] = raw["details"][:7]
        return 3, out[()]

    def _p4(self, raw):
        out = np.zeros((), C.PARTICIPANTS)
        out["header"] = self._header(raw["header"])
        out["numActiveCars"] = raw["numActiveCars"]
        _copy(out["participants"], raw["participants"])
        return 4, out[()]

    def _p5(self, raw):
        out = np.zeros((), C.CAR_SETUPS)
        out["header"] = self._header(raw["header"])
        _copy(out["carSetups"], raw["carSetups"])
        return 5, out[()]

    def _p6(self, raw):
        out = np.zeros((), C.CAR_TELEMETRY_PACKET)
        out["header"] = self._header(raw["header"])
        _copy(out["carTelemetryData"], raw["carTelemetryData"])
        out["buttonStatus"] = self.buttons     # 2021+: buttons arrive as BUTN events
        _copy(out, raw, ["mfdPanelIndex", "mfdPanelIndexSecondaryPlayer", "suggestedGear"])
        return 6, out[()]

    def _p7(self, raw):
        out = np.zeros((), C.CAR_STATUS_PACKET)
        out["header"] = self._header(raw["header"])
        dst = out["carStatusData"]
        _copy(dst, raw["carStatusData"])
        if self.damage is not None:   # wear and damage live in CarDamage since 2021
            d = self.damage
            dst["tyresWear"] = np.clip(np.round(d["tyresWear"]), 0, 255)
            _copy(dst, d, ["tyresDamage", "frontLeftWingDamage", "frontRightWingDamage", "rearWingDamage", "drsFault",
                           "engineDamage", "gearBoxDamage"])
        return 7, out[()]

    def _p10(self, raw):
        self.damage = raw["carDamageData"].copy()
        return 10, None


class Parsers:
    """packetFormat -> parser. 2020 is native; newer formats go through an Adapter (created on first sight)."""

    def __init__(self, log=print):
        self.log = log
        self.adapters: dict[int, Adapter] = {}

    def get(self, fmt: int):
        if fmt == 2020:
            return C.parse
        if fmt not in SUPPORTED:
            return None
        if fmt not in self.adapters:
            self.adapters[fmt] = Adapter(fmt, self.log)
            self.log(f"[telemetry] {GAMES[fmt]} telemetry detected (packet format {fmt})")
        return self.adapters[fmt].parse

    def game(self, fmt: int) -> str:
        a = self.adapters.get(fmt)
        return a.game if a else GAMES.get(fmt, f"format {fmt}")
