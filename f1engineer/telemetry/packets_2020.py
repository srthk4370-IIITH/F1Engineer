"""F1 2020 UDP packet layouts (packetFormat 2020) as numpy structured dtypes.

All little-endian, packed. Sizes are asserted at import against the official spec.
"""
import numpy as np

NUM_CARS = 22

HEADER = np.dtype([
    ("packetFormat", "<u2"), ("gameMajorVersion", "u1"), ("gameMinorVersion", "u1"),
    ("packetVersion", "u1"), ("packetId", "u1"), ("sessionUID", "<u8"), ("sessionTime", "<f4"),
    ("frameIdentifier", "<u4"), ("playerCarIndex", "u1"), ("secondaryPlayerCarIndex", "u1"),
])

CAR_MOTION = np.dtype([
    ("worldPositionX", "<f4"), ("worldPositionY", "<f4"), ("worldPositionZ", "<f4"),
    ("worldVelocityX", "<f4"), ("worldVelocityY", "<f4"), ("worldVelocityZ", "<f4"),
    ("worldForwardDirX", "<i2"), ("worldForwardDirY", "<i2"), ("worldForwardDirZ", "<i2"),
    ("worldRightDirX", "<i2"), ("worldRightDirY", "<i2"), ("worldRightDirZ", "<i2"),
    ("gForceLateral", "<f4"), ("gForceLongitudinal", "<f4"), ("gForceVertical", "<f4"),
    ("yaw", "<f4"), ("pitch", "<f4"), ("roll", "<f4"),
])
MOTION = np.dtype([
    ("header", HEADER), ("carMotionData", CAR_MOTION, (NUM_CARS,)),
    # wheel arrays order: RL, RR, FL, FR
    ("suspensionPosition", "<f4", (4,)), ("suspensionVelocity", "<f4", (4,)),
    ("suspensionAcceleration", "<f4", (4,)), ("wheelSpeed", "<f4", (4,)), ("wheelSlip", "<f4", (4,)),
    ("localVelocityX", "<f4"), ("localVelocityY", "<f4"), ("localVelocityZ", "<f4"),
    ("angularVelocityX", "<f4"), ("angularVelocityY", "<f4"), ("angularVelocityZ", "<f4"),
    ("angularAccelerationX", "<f4"), ("angularAccelerationY", "<f4"), ("angularAccelerationZ", "<f4"),
    ("frontWheelsAngle", "<f4"),
])

MARSHAL_ZONE = np.dtype([("zoneStart", "<f4"), ("zoneFlag", "i1")])
WEATHER_SAMPLE = np.dtype([
    ("sessionType", "u1"), ("timeOffset", "u1"), ("weather", "u1"),
    ("trackTemperature", "i1"), ("airTemperature", "i1"),
])
SESSION = np.dtype([
    ("header", HEADER), ("weather", "u1"), ("trackTemperature", "i1"), ("airTemperature", "i1"),
    ("totalLaps", "u1"), ("trackLength", "<u2"), ("sessionType", "u1"), ("trackId", "i1"),
    ("formula", "u1"), ("sessionTimeLeft", "<u2"), ("sessionDuration", "<u2"), ("pitSpeedLimit", "u1"),
    ("gamePaused", "u1"), ("isSpectating", "u1"), ("spectatorCarIndex", "u1"),
    ("sliProNativeSupport", "u1"), ("numMarshalZones", "u1"), ("marshalZones", MARSHAL_ZONE, (21,)),
    ("safetyCarStatus", "u1"), ("networkGame", "u1"), ("numWeatherForecastSamples", "u1"),
    ("weatherForecastSamples", WEATHER_SAMPLE, (20,)),
])

LAP = np.dtype([
    ("lastLapTime", "<f4"), ("currentLapTime", "<f4"), ("sector1TimeInMS", "<u2"),
    ("sector2TimeInMS", "<u2"), ("bestLapTime", "<f4"), ("bestLapNum", "u1"),
    ("bestLapSector1TimeInMS", "<u2"), ("bestLapSector2TimeInMS", "<u2"),
    ("bestLapSector3TimeInMS", "<u2"), ("bestOverallSector1TimeInMS", "<u2"),
    ("bestOverallSector1LapNum", "u1"), ("bestOverallSector2TimeInMS", "<u2"),
    ("bestOverallSector2LapNum", "u1"), ("bestOverallSector3TimeInMS", "<u2"),
    ("bestOverallSector3LapNum", "u1"), ("lapDistance", "<f4"), ("totalDistance", "<f4"),
    ("safetyCarDelta", "<f4"), ("carPosition", "u1"), ("currentLapNum", "u1"), ("pitStatus", "u1"),
    ("sector", "u1"), ("currentLapInvalid", "u1"), ("penalties", "u1"), ("gridPosition", "u1"),
    ("driverStatus", "u1"), ("resultStatus", "u1"),
])
LAP_DATA = np.dtype([("header", HEADER), ("lapData", LAP, (NUM_CARS,))])

EVENT = np.dtype([("header", HEADER), ("eventStringCode", "S4"), ("details", "u1", (7,))])

PARTICIPANT = np.dtype([
    ("aiControlled", "u1"), ("driverId", "u1"), ("teamId", "u1"), ("raceNumber", "u1"),
    ("nationality", "u1"), ("name", "S48"), ("yourTelemetry", "u1"),
])
PARTICIPANTS = np.dtype([("header", HEADER), ("numActiveCars", "u1"), ("participants", PARTICIPANT, (NUM_CARS,))])

CAR_SETUP = np.dtype([
    ("frontWing", "u1"), ("rearWing", "u1"), ("onThrottle", "u1"), ("offThrottle", "u1"),
    ("frontCamber", "<f4"), ("rearCamber", "<f4"), ("frontToe", "<f4"), ("rearToe", "<f4"),
    ("frontSuspension", "u1"), ("rearSuspension", "u1"), ("frontAntiRollBar", "u1"),
    ("rearAntiRollBar", "u1"), ("frontSuspensionHeight", "u1"), ("rearSuspensionHeight", "u1"),
    ("brakePressure", "u1"), ("brakeBias", "u1"), ("rearLeftTyrePressure", "<f4"),
    ("rearRightTyrePressure", "<f4"), ("frontLeftTyrePressure", "<f4"), ("frontRightTyrePressure", "<f4"),
    ("ballast", "u1"), ("fuelLoad", "<f4"),
])
CAR_SETUPS = np.dtype([("header", HEADER), ("carSetups", CAR_SETUP, (NUM_CARS,))])

CAR_TELEMETRY = np.dtype([
    ("speed", "<u2"), ("throttle", "<f4"), ("steer", "<f4"), ("brake", "<f4"), ("clutch", "u1"),
    ("gear", "i1"), ("engineRPM", "<u2"), ("drs", "u1"), ("revLightsPercent", "u1"),
    ("brakesTemperature", "<u2", (4,)), ("tyresSurfaceTemperature", "u1", (4,)),
    ("tyresInnerTemperature", "u1", (4,)), ("engineTemperature", "<u2"),
    ("tyresPressure", "<f4", (4,)), ("surfaceType", "u1", (4,)),
])
CAR_TELEMETRY_PACKET = np.dtype([
    ("header", HEADER), ("carTelemetryData", CAR_TELEMETRY, (NUM_CARS,)), ("buttonStatus", "<u4"),
    ("mfdPanelIndex", "u1"), ("mfdPanelIndexSecondaryPlayer", "u1"), ("suggestedGear", "i1"),
])

CAR_STATUS = np.dtype([
    ("tractionControl", "u1"), ("antiLockBrakes", "u1"), ("fuelMix", "u1"), ("frontBrakeBias", "u1"),
    ("pitLimiterStatus", "u1"), ("fuelInTank", "<f4"), ("fuelCapacity", "<f4"),
    ("fuelRemainingLaps", "<f4"), ("maxRPM", "<u2"), ("idleRPM", "<u2"), ("maxGears", "u1"),
    ("drsAllowed", "u1"), ("drsActivationDistance", "<u2"), ("tyresWear", "u1", (4,)),
    ("actualTyreCompound", "u1"), ("visualTyreCompound", "u1"), ("tyresAgeLaps", "u1"),
    ("tyresDamage", "u1", (4,)), ("frontLeftWingDamage", "u1"), ("frontRightWingDamage", "u1"),
    ("rearWingDamage", "u1"), ("drsFault", "u1"), ("engineDamage", "u1"), ("gearBoxDamage", "u1"),
    ("vehicleFiaFlags", "i1"), ("ersStoreEnergy", "<f4"), ("ersDeployMode", "u1"),
    ("ersHarvestedThisLapMGUK", "<f4"), ("ersHarvestedThisLapMGUH", "<f4"), ("ersDeployedThisLap", "<f4"),
])
CAR_STATUS_PACKET = np.dtype([("header", HEADER), ("carStatusData", CAR_STATUS, (NUM_CARS,))])

FINAL_CLASS = np.dtype([
    ("position", "u1"), ("numLaps", "u1"), ("gridPosition", "u1"), ("points", "u1"),
    ("numPitStops", "u1"), ("resultStatus", "u1"), ("bestLapTime", "<f4"), ("totalRaceTime", "<f8"),
    ("penaltiesTime", "u1"), ("numPenalties", "u1"), ("numTyreStints", "u1"),
    ("tyreStintsActual", "u1", (8,)), ("tyreStintsVisual", "u1", (8,)),
])
FINAL_CLASSIFICATION = np.dtype([("header", HEADER), ("numCars", "u1"), ("classificationData", FINAL_CLASS, (NUM_CARS,))])

LOBBY_PLAYER = np.dtype([("aiControlled", "u1"), ("teamId", "u1"), ("nationality", "u1"), ("name", "S48"), ("readyStatus", "u1")])
LOBBY_INFO = np.dtype([("header", HEADER), ("numPlayers", "u1"), ("lobbyPlayers", LOBBY_PLAYER, (NUM_CARS,))])

# packetId -> (name, dtype)
PACKETS = {
    0: ("motion", MOTION), 1: ("session", SESSION), 2: ("lap_data", LAP_DATA), 3: ("event", EVENT),
    4: ("participants", PARTICIPANTS), 5: ("car_setups", CAR_SETUPS), 6: ("car_telemetry", CAR_TELEMETRY_PACKET),
    7: ("car_status", CAR_STATUS_PACKET), 8: ("final_classification", FINAL_CLASSIFICATION),
    9: ("lobby_info", LOBBY_INFO),
}
EXPECTED_SIZES = {0: 1464, 1: 251, 2: 1190, 3: 35, 4: 1213, 5: 1102, 6: 1307, 7: 1344, 8: 839, 9: 1169}
for _pid, (_n, _dt) in PACKETS.items():
    assert _dt.itemsize == EXPECTED_SIZES[_pid], (_n, _dt.itemsize, EXPECTED_SIZES[_pid])


def parse(data: bytes):
    """Return (packetId, numpy record) or None for unknown/short packets."""
    if len(data) < 6:
        return None
    pid = data[5]
    entry = PACKETS.get(pid)
    if entry is None or len(data) < entry[1].itemsize:
        return None
    return pid, np.frombuffer(data, dtype=entry[1], count=1)[0]


# --- lookup tables ---------------------------------------------------------
TRACKS = {
    0: "Melbourne", 1: "Paul Ricard", 2: "Shanghai", 3: "Sakhir", 4: "Catalunya", 5: "Monaco",
    6: "Montreal", 7: "Silverstone", 8: "Hockenheim", 9: "Hungaroring", 10: "Spa", 11: "Monza",
    12: "Singapore", 13: "Suzuka", 14: "Abu Dhabi", 15: "Texas", 16: "Brazil", 17: "Austria",
    18: "Sochi", 19: "Mexico", 20: "Baku", 21: "Sakhir Short", 22: "Silverstone Short",
    23: "Texas Short", 24: "Suzuka Short", 25: "Hanoi", 26: "Zandvoort",
}
SESSION_TYPES = {
    0: "unknown", 1: "P1", 2: "P2", 3: "P3", 4: "Short P", 5: "Q1", 6: "Q2", 7: "Q3", 8: "Short Q",
    9: "OSQ", 10: "Race", 11: "Race 2", 12: "Time Trial",
}
WEATHER = {0: "clear", 1: "light cloud", 2: "overcast", 3: "light rain", 4: "heavy rain", 5: "storm"}
VISUAL_COMPOUND = {16: "soft", 17: "medium", 18: "hard", 7: "inter", 8: "wet"}
SAFETY_CAR = {0: "none", 1: "full", 2: "virtual"}
BUTTONS = {
    0x0001: "A", 0x0002: "Y", 0x0004: "B", 0x0008: "X", 0x0010: "DPAD_LEFT", 0x0020: "DPAD_RIGHT",
    0x0040: "DPAD_UP", 0x0080: "DPAD_DOWN", 0x0100: "MENU", 0x0200: "LB", 0x0400: "RB",
    0x0800: "LT", 0x1000: "RT", 0x2000: "LS", 0x4000: "RS",
}
