"""
Logistics Route Engine
======================
Calculates realistic maritime, air, and truck routes between two coordinates.

Sea  : Dijkstra shortest-path on a pre-defined waypoint graph of shipping lanes,
       including all major chokepoints (Suez, Malacca, Gibraltar, Panama, Cape).
Air  : Great-circle arc (SLERP) with 5 % detour factor.
Truck: Haversine × 1.30 detour factor (practical only intra-continental).
"""

import math
import heapq

# ── Constants ─────────────────────────────────────────────────────────────────────────────

EARTH_R_KM = 6371.0

# Emission factors (kg CO2 / tonne·km)
CO2 = {
    "sea":   0.012,   # modern container ship, IMO 4th GHG Study
    "air":   0.600,   # belly freight average
    "truck": 0.075,   # EU average EURO VI truck
}

# Cost factors
COST_SEA_PER_TONNE_KM   = 0.060   # USD / tonne·km  (~$1 500/TEU over 25 000 km)
COST_AIR_PER_KG         = 5.5     # USD / kg
COST_TRUCK_PER_TONNE_KM = 0.12    # USD / tonne·km  (≈ €0.10)

# Speed / transit
SPEED_SEA_KM_DAY   = 500    # 14 kn × 24 h, plus port handling
SPEED_AIR_KM_DAY   = 800    # effective (incl. ground, customs) — min 2 days
SPEED_TRUCK_KM_DAY = 700    # EU regulated driving hours


# ── Haversine ──────────────────────────────────────────────────────────────────────────────

def haversine(lat1, lng1, lat2, lng2):
    """Γρεατ-circle distance in km."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lng2 - lng1)
    a = math.sin(Δφ/2)**2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ/2)**2
    return EARTH_R_KM * 2 * math.asin(math.sqrt(min(1.0, a)))


# ── Maritime waypoint graph ────────────────────────────────────────────────────────────────────

NODES = {
    # ── NORTH SEA / ENGLISH CHANNEL ──────────────────────────────────────────
    "HAMBURG":       (53.55,   9.80),
    "ROTTERDAM":     (51.90,   4.10),
    "ANTWERP":       (51.23,   4.40),
    "FELIXSTOWE":    (51.96,   1.35),
    "LE_HAVRE":      (49.49,   0.10),
    "N_SEA":         (55.00,   3.00),
    "DOVER":         (51.10,   1.30),
    "CHANNEL_W":     (49.50,  -5.00),
    # ── ATLANTIC EUROPE ───────────────────────────────────────────────────
    "BISCAY":        (46.00,  -9.00),
    "BILBAO":        (43.35,  -3.00),
    "LISBON":        (38.72,  -9.14),
    # ── MEDITERRANEAN ───────────────────────────────────────────────────────
    "GIBRALTAR":     (35.90,  -5.40),
    "BARCELONA":     (41.35,   2.17),
    "MARSEILLE":     (43.30,   5.35),
    "GENOA":         (44.42,   8.93),
    "W_MED":         (38.00,   5.50),
    "STRAIT_SIC":    (37.60,  11.00),   # Channel of Sicily
    "PIRAEUS":       (37.95,  23.62),
    "E_MED":         (35.00,  27.00),
    "ISTANBUL":      (41.00,  29.00),   # Bosphorus / Black Sea exit
    "PORT_SAID":     (31.26,  32.32),   # Suez Canal north
    # ── SUEZ / RED SEA ────────────────────────────────────────────────────────
    "SUEZ_S":        (29.90,  32.50),   # Suez Canal south
    "JEDDAH":        (21.49,  39.19),
    "BAB_MANDEB":    (12.58,  43.38),   # chokepoint
    "ADEN":          (12.79,  45.04),
    # ── ARABIAN SEA ──────────────────────────────────────────────────────────────
    "ARAB_SEA":      (12.00,  63.00),
    "HORMUZ":        (26.60,  56.50),   # Strait of Hormuz
    "MUSCAT":        (23.61,  58.59),
    "MUMBAI":        (18.96,  72.82),
    "COLOMBO":       ( 6.93,  79.85),
    # ── INDIAN OCEAN ──────────────────────────────────────────────────────────────
    "ANDAMAN":       ( 9.00,  87.00),
    "IND_OC":        (-5.00,  75.00),
    "MAURITIUS":     (-20.20, 57.50),
    "MOMBASA":       ( -4.05, 39.66),
    "DURBAN":        (-29.86, 31.05),
    # ── CAPE OF GOOD HOPE ───────────────────────────────────────────────────────────
    "CAPE_TOWN":     (-33.90, 18.42),
    "GD_HOPE":       (-34.35, 18.30),
    # ── WEST AFRICA ───────────────────────────────────────────────────────────────
    "DAKAR":         (14.70, -17.44),
    "LAGOS":         ( 6.45,   3.40),
    "LUANDA":        (-8.84,  13.23),
    # ── MALACCA / SE ASIA ───────────────────────────────────────────────────────────
    "MALACCA_W":     ( 5.60,  95.30),
    "MALACCA_MID":   ( 3.50, 100.50),
    "SINGAPORE":     ( 1.20, 104.00),
    "SUNDA":         (-6.00, 105.80),   # Sunda Strait (alt to Malacca)
    "LOMBOK":        (-8.50, 116.50),   # Lombok Strait (VLCC alt)
    # ── SOUTH CHINA SEA ─────────────────────────────────────────────────────────────
    "SCS_S":         ( 3.00, 108.00),
    "SCS_N":         (16.00, 115.00),
    "HK":            (22.30, 114.17),
    "SCS_LUZON":     (20.50, 121.50),   # Luzon Strait
    # ── EAST CHINA SEA ─────────────────────────────────────────────────────────────
    "SHANGHAI":      (31.23, 121.47),
    "QINGDAO":       (36.07, 120.38),
    "ECS":           (27.00, 124.00),
    # ── JAPAN / KOREA ──────────────────────────────────────────────────────────────
    "TOKYO":         (35.65, 139.77),
    "OSAKA":         (34.65, 135.44),
    "BUSAN":         (35.10, 129.04),
    # ── AUSTRALIA ─────────────────────────────────────────────────────────────────────
    "PERTH":         (-32.00, 115.86),
    "MELBOURNE":     (-37.82, 144.96),
    "SYDNEY":        (-33.87, 151.21),
    "BRISBANE":      (-27.47, 153.02),
    # ── PACIFIC ─────────────────────────────────────────────────────────────────────────
    "N_PAC":         (40.00,  170.00),
    "N_PAC_E":       (40.00, -150.00),
    "C_PAC_W":       ( 5.00, -170.00),
    "HAWAII":        (21.30, -157.87),
    # ── NORTH AMERICA WEST ─────────────────────────────────────────────────────────
    "VANCOUVER":     (49.28, -123.10),
    "SEATTLE":       (47.60, -122.33),
    "LA":            (33.75, -118.25),
    "MANZANILLO_MX": (19.05, -104.32),
    # ── PANAMA ─────────────────────────────────────────────────────────────────────────────
    "PANAMA_W":      ( 8.90,  -79.52),
    "PANAMA_E":      ( 9.36,  -79.91),
    # ── SOUTH AMERICA WEST ─────────────────────────────────────────────────────────
    "CALLAO":        (-12.05, -77.15),
    "BUENOS_AIRES":  (-34.60, -58.37),
    "CAPE_HORN":     (-56.00, -67.50),
    # ── SOUTH AMERICA EAST ─────────────────────────────────────────────────────────
    "SANTOS":        (-23.96, -46.33),
    "FORTALEZA":     (-3.72,  -38.52),
    "PARANAGUA":     (-25.52, -48.52),
    # ── NORTH AMERICA EAST ─────────────────────────────────────────────────────────
    "NEW_YORK":      (40.70,  -74.00),
    "NORFOLK":       (36.85,  -76.30),
    "SAVANNAH":      (31.97,  -81.10),
    "MIAMI":         (25.77,  -80.19),
    "NEW_ORLEANS":   (29.95,  -90.07),
    "HOUSTON":       (29.75,  -95.37),
    # ── CARIBBEAN ───────────────────────────────────────────────────────────────────
    "CARIBBEAN":     (15.00,  -70.00),
    # ── ATLANTIC OPEN SEA ─────────────────────────────────────────────────────────────
    "N_ATL":         (40.00,  -40.00),
    "MID_ATL":       (20.00,  -30.00),
    "S_ATL":         (-20.00, -10.00),
}

# Undirected edges — physically navigable sea connections only
_EDGE_PAIRS = [
    # North Sea / English Channel
    ("N_SEA",    "HAMBURG"),   ("N_SEA",    "ROTTERDAM"),  ("N_SEA",    "ANTWERP"),
    ("N_SEA",    "FELIXSTOWE"),("FELIXSTOWE","DOVER"),      ("DOVER",    "CHANNEL_W"),
    ("LE_HAVRE", "CHANNEL_W"), ("FELIXSTOWE","LE_HAVRE"),  ("CHANNEL_W","BISCAY"),
    ("ROTTERDAM","ANTWERP"),
    # Atlantic Europe
    ("BISCAY",   "BILBAO"),    ("BISCAY",   "LISBON"),     ("BISCAY",   "GIBRALTAR"),
    ("BILBAO",   "LISBON"),    ("LISBON",   "GIBRALTAR"),
    # Mediterranean
    ("GIBRALTAR","W_MED"),     ("W_MED",    "BARCELONA"),  ("W_MED",    "MARSEILLE"),
    ("W_MED",    "STRAIT_SIC"),("MARSEILLE","GENOA"),       ("BARCELONA","GENOA"),
    ("GENOA",    "STRAIT_SIC"),("STRAIT_SIC","PIRAEUS"),   ("PIRAEUS",  "E_MED"),
    ("E_MED",    "PORT_SAID"), ("E_MED",    "ISTANBUL"),
    # Suez Canal
    ("PORT_SAID","SUEZ_S"),
    # Red Sea
    ("SUEZ_S",   "JEDDAH"),    ("JEDDAH",   "BAB_MANDEB"), ("BAB_MANDEB","ADEN"),
    ("ADEN",     "ARAB_SEA"),
    # Arabian Sea
    ("ARAB_SEA", "HORMUZ"),    ("HORMUZ",   "MUSCAT"),     ("MUSCAT",   "MUMBAI"),
    ("MUMBAI",   "COLOMBO"),   ("ARAB_SEA", "COLOMBO"),   ("ARAB_SEA", "ANDAMAN"),
    # Malacca Strait
    ("ANDAMAN",  "MALACCA_W"), ("MALACCA_W","MALACCA_MID"),("MALACCA_MID","SINGAPORE"),
    ("SINGAPORE","SCS_S"),
    # Indian Ocean
    ("COLOMBO",  "IND_OC"),    ("ANDAMAN",  "IND_OC"),    ("IND_OC",   "MAURITIUS"),
    ("IND_OC",   "PERTH"),     ("IND_OC",   "MOMBASA"),   ("IND_OC",   "DURBAN"),
    ("MAURITIUS","DURBAN"),    ("MOMBASA",  "MAURITIUS"),
    # Cape of Good Hope (alternative to Suez)
    ("DURBAN",   "GD_HOPE"),   ("GD_HOPE",  "CAPE_TOWN"), ("CAPE_TOWN","DURBAN"),
    ("GD_HOPE",  "IND_OC"),    ("GD_HOPE",  "S_ATL"),
    # West Africa
    ("DAKAR",    "LAGOS"),     ("LAGOS",    "LUANDA"),     ("LUANDA",   "CAPE_TOWN"),
    ("DAKAR",    "MID_ATL"),   ("DAKAR",    "LISBON"),
    # South Atlantic
    ("S_ATL",    "BUENOS_AIRES"),("S_ATL",  "LUANDA"),     ("S_ATL",    "MID_ATL"),
    ("FORTALEZA","S_ATL"),     ("BUENOS_AIRES","CAPE_HORN"),("CAPE_HORN","S_ATL"),
    # South America East
    ("FORTALEZA","SANTOS"),    ("SANTOS",   "PARANAGUA"),  ("PARANAGUA","BUENOS_AIRES"),
    ("FORTALEZA","MID_ATL"),
    # Mid Atlantic
    ("MID_ATL",  "MIAMI"),     ("MID_ATL",  "NEW_YORK"),   ("MID_ATL",  "N_ATL"),
    # North Atlantic
    ("N_ATL",    "NEW_YORK"),  ("N_ATL",    "LISBON"),     ("N_ATL",    "LE_HAVRE"),
    ("N_ATL",    "ROTTERDAM"), ("N_ATL",    "HAMBURG"),    ("N_ATL",    "MID_ATL"),
    # North America East coast
    ("NEW_YORK", "NORFOLK"),   ("NORFOLK",  "SAVANNAH"),   ("SAVANNAH", "MIAMI"),
    ("MIAMI",    "HOUSTON"),   ("HOUSTON",  "NEW_ORLEANS"),
    # Caribbean / Gulf / Panama
    ("MIAMI",    "CARIBBEAN"), ("CARIBBEAN","PANAMA_E"),   ("PANAMA_E", "PANAMA_W"),
    ("HOUSTON",  "CARIBBEAN"),
    # Panama Canal → Pacific
    ("PANAMA_W", "MANZANILLO_MX"),("MANZANILLO_MX","LA"), ("LA",       "SEATTLE"),
    ("SEATTLE",  "VANCOUVER"),
    # South America West
    ("PANAMA_W", "CALLAO"),    ("CALLAO",   "BUENOS_AIRES"),("BUENOS_AIRES","CAPE_HORN"),
    ("CAPE_HORN","CALLAO"),
    # Trans-Pacific North
    ("TOKYO",    "N_PAC"),     ("N_PAC",    "N_PAC_E"),    ("N_PAC_E",  "SEATTLE"),
    ("N_PAC_E",  "LA"),        ("VANCOUVER","N_PAC_E"),
    ("TOKYO",    "HAWAII"),    ("HAWAII",   "LA"),          ("HAWAII",   "SEATTLE"),
    # Trans-Pacific Central/South
    ("C_PAC_W",  "HAWAII"),    ("C_PAC_W",  "LA"),         ("SYDNEY",   "C_PAC_W"),
    ("SYDNEY",   "PANAMA_W"),                                # direct S Pacific route
    # East China Sea / Japan
    ("ECS",      "SHANGHAI"),  ("ECS",      "QINGDAO"),    ("ECS",      "BUSAN"),
    ("ECS",      "TOKYO"),     ("BUSAN",    "OSAKA"),       ("TOKYO",    "OSAKA"),
    ("SCS_LUZON","ECS"),
    # South China Sea
    ("SCS_S",    "SCS_N"),     ("SCS_N",    "HK"),         ("SCS_N",    "SCS_LUZON"),
    ("HK",       "SHANGHAI"),  ("HK",       "ECS"),
    # Alternative SE Asia passages (for very large ships)
    ("SINGAPORE","SUNDA"),     ("SUNDA",    "IND_OC"),
    ("SINGAPORE","LOMBOK"),    ("LOMBOK",   "IND_OC"),
    ("PERTH",    "LOMBOK"),    ("PERTH",    "SUNDA"),
    # Australia internal
    ("PERTH",    "MELBOURNE"), ("MELBOURNE","SYDNEY"),      ("SYDNEY",   "BRISBANE"),
    ("SYDNEY",   "TOKYO"),     ("BRISBANE", "N_PAC"),
]


def _build_graph():
    graph = {n: [] for n in NODES}
    for a, b in _EDGE_PAIRS:
        if a not in NODES or b not in NODES:
            continue
        d = haversine(*NODES[a], *NODES[b])
        graph[a].append((d, b))
        graph[b].append((d, a))
    return graph

_GRAPH = _build_graph()


# ── Dijkstra ──────────────────────────────────────────────────────────────────────────────

def _nearest_nodes(lat, lng, k=4):
    dists = [(haversine(lat, lng, *NODES[n]), n) for n in NODES]
    return [name for _, name in sorted(dists)[:k]]


def _dijkstra(graph, starts, ends):
    INF = float("inf")
    dist = {n: INF for n in graph}
    prev = {n: None for n in graph}
    heap = []
    for s in starts:
        dist[s] = 0
        heapq.heappush(heap, (0.0, s))

    end_set = set(ends)
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u]:
            continue
        if u in end_set:
            break
        for w, v in graph.get(u, []):
            alt = dist[u] + w
            if alt < dist[v]:
                dist[v] = alt
                prev[v] = u
                heapq.heappush(heap, (alt, v))

    best = min(ends, key=lambda n: dist[n])
    if dist[best] == INF:
        return None, []

    path, cur = [], best
    while cur is not None:
        path.append(cur)
        cur = prev[cur]
    path.reverse()
    return dist[best], path


def _normalize_lng(points):
    """Unwrap longitudes so polylines don't jump at the antimeridian."""
    if not points:
        return points
    result = [list(points[0])]
    for pt in points[1:]:
        lat, lng = pt
        prev_lng = result[-1][1]
        while lng - prev_lng > 180:
            lng -= 360
        while prev_lng - lng > 180:
            lng += 360
        result.append([lat, lng])
    return result


# ── Great-circle arc (SLERP) ───────────────────────────────────────────────────────────────

def _great_circle_arc(lat1, lng1, lat2, lng2, n=80):
    φ1, λ1 = math.radians(lat1), math.radians(lng1)
    φ2, λ2 = math.radians(lat2), math.radians(lng2)
    x1, y1, z1 = math.cos(φ1)*math.cos(λ1), math.cos(φ1)*math.sin(λ1), math.sin(φ1)
    x2, y2, z2 = math.cos(φ2)*math.cos(λ2), math.cos(φ2)*math.sin(λ2), math.sin(φ2)
    d = math.acos(max(-1.0, min(1.0, x1*x2 + y1*y2 + z1*z2)))
    if d < 1e-8:
        return [[lat1, lng1], [lat2, lng2]]
    sin_d = math.sin(d)
    points = []
    for i in range(n + 1):
        t = i / n
        a = math.sin((1 - t) * d) / sin_d
        b = math.sin(t * d) / sin_d
        x, y, z = a*x1 + b*x2, a*y1 + b*y2, a*z1 + b*z2
        lat = math.degrees(math.atan2(z, math.sqrt(x**2 + y**2)))
        lng = math.degrees(math.atan2(y, x))
        points.append([round(lat, 4), round(lng, 4)])
    return _normalize_lng(points)


# ── Public route calculators ──────────────────────────────────────────────────────────────────

def sea_route(olat, olng, dlat, dlng):
    """Dijkstra shortest sea path via the maritime waypoint graph."""
    o_nodes = _nearest_nodes(olat, olng)
    d_nodes = _nearest_nodes(dlat, dlng)

    # Build temporary graph with virtual origin / dest nodes
    tg = {k: list(v) for k, v in _GRAPH.items()}
    tg["__O__"] = []
    tg["__D__"] = []
    for n in o_nodes:
        d = haversine(olat, olng, *NODES[n])
        tg["__O__"].append((d, n))
        tg[n].append((d, "__O__"))
    for n in d_nodes:
        d = haversine(dlat, dlng, *NODES[n])
        tg["__D__"].append((d, n))
        tg[n].append((d, "__D__"))

    km, path = _dijkstra(tg, ["__O__"], ["__D__"])
    if km is None:
        return {"ok": False, "error": "No sea route found between these points"}

    # Build waypoint list (exclude virtual nodes)
    wps = [[olat, olng]]
    for name in path:
        if name not in ("__O__", "__D__"):
            wps.append(list(NODES[name]))
    wps.append([dlat, dlng])
    wps = _normalize_lng(wps)

    days = max(1, round(km / SPEED_SEA_KM_DAY))
    return {
        "ok":              True,
        "km":              round(km),
        "waypoints":       wps,
        "transit_days":    days,
        "co2_per_tonne":   round(km * CO2["sea"], 1),
        "cost_per_tonne":  round(km * COST_SEA_PER_TONNE_KM),
        "via":             [n for n in path if n not in ("__O__", "__D__")],
    }


def air_route(olat, olng, dlat, dlng):
    """Great-circle air route with 5 % detour factor."""
    km = haversine(olat, olng, dlat, dlng) * 1.05
    wps = _great_circle_arc(olat, olng, dlat, dlng)
    days = max(2, round(km / SPEED_AIR_KM_DAY))
    return {
        "ok":              True,
        "km":              round(km),
        "waypoints":       wps,
        "transit_days":    days,
        "co2_per_tonne":   round(km * CO2["air"]),
        "cost_per_tonne":  round(COST_AIR_PER_KG * 1000),
        "cost_per_kg":     COST_AIR_PER_KG,
    }


def truck_route(olat, olng, dlat, dlng):
    """Road distance estimate: Haversine × 1.30 detour factor."""
    straight = haversine(olat, olng, dlat, dlng)
    km = round(straight * 1.30)
    days = max(1, round(km / SPEED_TRUCK_KM_DAY))
    return {
        "ok":              True,
        "km":              km,
        "waypoints":       _normalize_lng([[olat, olng], [dlat, dlng]]),
        "transit_days":    days,
        "co2_per_tonne":   round(km * CO2["truck"], 1),
        "cost_per_tonne":  round(km * COST_TRUCK_PER_TONNE_KM),
        "note":            "Approx. (Haversine × 1.30). Use OSRM for exact road distance.",
    }


def calculate_all(olat, olng, dlat, dlng, cargo_tonnes=1.0):
    """
    Calculate all three modes. Returns a dict with 'sea', 'air', 'truck' keys
    and cargo-scaled totals.
    """
    sea   = sea_route(olat, olng, dlat, dlng)
    air   = air_route(olat, olng, dlat, dlng)
    truck = truck_route(olat, olng, dlat, dlng)

    def scale(r):
        if not r["ok"]:
            return r
        return {
            **r,
            "total_co2_kg":   round(r["co2_per_tonne"] * cargo_tonnes, 1),
            "total_cost_usd": round(r["cost_per_tonne"] * cargo_tonnes),
        }

    return {
        "sea":          scale(sea),
        "air":          scale(air),
        "truck":        scale(truck),
        "cargo_tonnes": cargo_tonnes,
        "origin":       [olat, olng],
        "destination":  [dlat, dlng],
    }


# ── Port directory (for autocomplete) ────────────────────────────────────────────────────────

PORTS = {
    # Europe
    "Rotterdam":      (51.90,   4.10), "Hamburg":       (53.55,   9.80),
    "Antwerp":        (51.23,   4.40), "Felixstowe":    (51.96,   1.35),
    "Le Havre":       (49.49,   0.10), "Bilbao":        (43.35,  -3.00),
    "Lisbon":         (38.72,  -9.14), "Barcelona":     (41.35,   2.17),
    "Marseille":      (43.30,   5.35), "Genoa":         (44.42,   8.93),
    "Piraeus":        (37.95,  23.62), "Istanbul":      (41.00,  29.00),
    "Valencia":       (39.45,  -0.34), "Algeciras":     (36.12,  -5.44),
    "Gdansk":         (54.36,  18.65), "Gothenburg":    (57.71,  11.97),
    # Middle East / Asia
    "Port Said":      (31.26,  32.32), "Jeddah":        (21.49,  39.19),
    "Dubai":          (25.27,  55.30), "Muscat":        (23.61,  58.59),
    "Mumbai":         (18.96,  72.82), "Colombo":       ( 6.93,  79.85),
    "Singapore":      ( 1.36, 103.82), "Shanghai":      (31.23, 121.47),
    "Shenzhen":       (22.54, 113.99), "Hong Kong":     (22.30, 114.17),
    "Guangzhou":      (22.34, 113.85), "Qingdao":       (36.07, 120.38),
    "Tianjin":        (39.02, 117.71), "Ningbo":        (29.87, 121.55),
    "Busan":          (35.10, 129.04), "Tokyo":         (35.65, 139.77),
    "Osaka":          (34.65, 135.44), "Nagoya":        (34.98, 136.90),
    "Taipei/Kaohsiung":(22.62, 120.28),"Ho Chi Minh":  (10.65, 106.93),
    "Bangkok":        (13.10, 100.92), "Jakarta":       (-6.11, 106.88),
    "Klang":          ( 3.00, 101.39), "Tanjung Pelepas":( 1.36, 103.55),
    # Africa
    "Mombasa":        (-4.05,  39.66), "Durban":        (-29.86, 31.05),
    "Cape Town":      (-33.90, 18.42), "Lagos":         ( 6.45,   3.40),
    "Dakar":          (14.70, -17.44), "Djibouti":      (11.60,  43.14),
    # Americas
    "New York":       (40.70, -74.00), "Houston":       (29.75, -95.37),
    "Los Angeles":    (33.75,-118.25), "Seattle":       (47.60,-122.33),
    "Miami":          (25.77, -80.19), "Savannah":      (31.97, -81.10),
    "New Orleans":    (29.95, -90.07), "Vancouver":     (49.28,-123.10),
    "Santos":         (-23.96,-46.33), "Buenos Aires":  (-34.60,-58.37),
    "Callao":         (-12.05,-77.15), "Manzanillo":    (19.05,-104.32),
    "Colón":          ( 9.36, -79.91),
    # Australia / Pacific
    "Sydney":         (-33.87, 151.21),"Melbourne":     (-37.82, 144.96),
    "Brisbane":       (-27.47, 153.02),"Perth":         (-32.00, 115.86),
    "Auckland":       (-36.84, 174.77),
}
