import os
import random
import sqlite3
from datetime import datetime, timedelta

import config

# Journeys are created relative to today so that "tomorrow" and "yesterday"
# always mean something. Re-run this script to reset the database.
today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

if os.path.exists(config.DB_PATH):
    os.remove(config.DB_PATH)
db = sqlite3.connect(config.DB_PATH)

db.executescript("""
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, railcard TEXT);
CREATE TABLE stations  (code TEXT PRIMARY KEY, name TEXT);
CREATE TABLE journeys  (
    id INTEGER PRIMARY KEY, origin TEXT, dest TEXT, depart_at TEXT, arrive_at TEXT,
    operator TEXT, status TEXT, delay_mins INTEGER
);
CREATE TABLE bookings  (
    id TEXT PRIMARY KEY, customer_id INTEGER, journey_id INTEGER, fare_type TEXT,
    price REAL, seat TEXT, status TEXT, booked_at TEXT
);
CREATE TABLE refunds   (
    id INTEGER PRIMARY KEY, booking_id TEXT, amount REAL, reason TEXT, status TEXT, created_at TEXT
);
CREATE TABLE tickets   (
    id INTEGER PRIMARY KEY, customer_id INTEGER, summary TEXT, created_at TEXT
);
""")

# 1. CUSTOMERS
db.executemany("INSERT INTO customers VALUES (?, ?, ?, ?)", [
    (1, "Alice Smith", "alice@example.com", "16-25 Railcard"),
    (2, "Ben Jones", "ben@example.com", None),
])

# 2. STATIONS
stations = {
    "KGX": "London King's Cross", "EUS": "London Euston", "STP": "London St Pancras International",
    "PAD": "London Paddington", "WAT": "London Waterloo", "LST": "London Liverpool Street",
    "VIC": "London Victoria", "YRK": "York", "EDB": "Edinburgh Waverley", "GLC": "Glasgow Central",
    "NCL": "Newcastle", "MAN": "Manchester Piccadilly", "LIV": "Liverpool Lime Street",
    "LDS": "Leeds", "SHF": "Sheffield", "NOT": "Nottingham", "BHM": "Birmingham New Street",
    "BRI": "Bristol Temple Meads", "CDF": "Cardiff Central", "EXD": "Exeter St Davids",
    "RDG": "Reading", "OXF": "Oxford", "CBG": "Cambridge", "NRW": "Norwich",
    "SOU": "Southampton Central", "BTN": "Brighton",
}
db.executemany("INSERT INTO stations VALUES (?, ?)", stations.items())

# 3. JOURNEYS: every route runs in both directions from 3 days ago to 7 days ahead,
# first train at `first`, then every `every` minutes until 21:30
routes = [
    # origin, dest, journey minutes, operator, first train, every N minutes
    ("KGX", "YRK", 115, "LNER", "06:30", 30),
    ("KGX", "EDB", 260, "LNER", "06:00", 60),
    ("KGX", "LDS", 135, "LNER", "06:30", 60),
    ("KGX", "NCL", 175, "LNER", "06:15", 60),
    ("KGX", "CBG", 50, "Great Northern", "06:05", 30),
    ("EUS", "MAN", 130, "Avanti West Coast", "06:20", 60),
    ("EUS", "BHM", 85, "Avanti West Coast", "06:10", 30),
    ("EUS", "LIV", 135, "Avanti West Coast", "06:45", 60),
    ("EUS", "GLC", 270, "Avanti West Coast", "06:30", 120),
    ("STP", "SHF", 120, "East Midlands Railway", "06:30", 30),
    ("STP", "NOT", 100, "East Midlands Railway", "06:00", 60),
    ("PAD", "BRI", 100, "GWR", "06:00", 30),
    ("PAD", "CDF", 125, "GWR", "06:15", 60),
    ("PAD", "OXF", 60, "GWR", "06:20", 30),
    ("PAD", "RDG", 25, "GWR", "06:00", 30),
    ("PAD", "EXD", 150, "GWR", "07:00", 120),
    ("WAT", "SOU", 80, "South Western Railway", "06:30", 30),
    ("LST", "NRW", 110, "Greater Anglia", "06:00", 30),
    ("VIC", "BTN", 60, "Southern", "06:00", 30),
    ("MAN", "LDS", 55, "TransPennine Express", "06:10", 30),
    ("MAN", "LIV", 45, "Northern", "06:00", 30),
    ("MAN", "SHF", 50, "Northern", "06:15", 60),
    ("LDS", "YRK", 25, "Northern", "06:05", 30),
    ("LDS", "SHF", 45, "Northern", "06:20", 60),
    ("YRK", "NCL", 60, "LNER", "06:40", 60),
    ("NCL", "EDB", 90, "LNER", "07:00", 60),
    ("EDB", "GLC", 50, "ScotRail", "06:00", 30),
    ("BHM", "MAN", 90, "CrossCountry", "06:30", 60),
    ("BHM", "BRI", 85, "CrossCountry", "06:45", 60),
    ("BHM", "SHF", 75, "CrossCountry", "07:00", 60),
    ("BHM", "NOT", 70, "CrossCountry", "06:50", 60),
]
journey_ids = {}  # (origin, dest, day offset, "HH:MM") -> id
next_id = 100
last_train = 21 * 60 + 30
for day in range(-3, 8):
    for origin, dest, minutes, operator, first, every in routes:
        start = int(first[:2]) * 60 + int(first[3:])
        for leg in [(origin, dest), (dest, origin)]:
            for m in range(start, last_train + 1, every):
                depart = today + timedelta(days=day, minutes=m)
                arrive = depart + timedelta(minutes=minutes)
                db.execute(
                    "INSERT INTO journeys VALUES (?, ?, ?, ?, ?, ?, 'on time', 0)",
                    (next_id, leg[0], leg[1], depart.isoformat(timespec="minutes"),
                     arrive.isoformat(timespec="minutes"), operator),
                )
                journey_ids[(leg[0], leg[1], day, f"{m // 60:02d}:{m % 60:02d}")] = next_id
                next_id += 1

# Random disruption across the network (fixed seed, so every reset looks the same):
# about 5% of trains delayed and 1.5% cancelled
rng = random.Random(42)
for journey_id in range(100, next_id):
    roll = rng.random()
    if roll < 0.015:
        db.execute("UPDATE journeys SET status = 'cancelled' WHERE id = ?", (journey_id,))
    elif roll < 0.065:
        db.execute("UPDATE journeys SET status = 'delayed', delay_mins = ? WHERE id = ?",
                   (rng.choice([5, 10, 15, 20, 25, 35, 45, 70]), journey_id))

# The trains in the sample bookings get a fixed status so the demo conversations always work
fixed_status = [
    (("KGX", "YRK", -1, "10:30"), "delayed", 45),      # Alice's late train yesterday (Delay Repay)
    (("KGX", "EDB", 1, "12:00"), "on time", 0),        # Alice's train tomorrow
    (("KGX", "EDB", 2, "09:00"), "cancelled", 0),      # Alice's cancelled trip
    (("EUS", "BHM", 3, "12:10"), "on time", 0),
    (("EUS", "MAN", -2, "15:20"), "delayed", 130),     # Ben's very late train
    (("MAN", "EUS", 2, "07:20"), "on time", 0),
]
for key, status, delay in fixed_status:
    db.execute("UPDATE journeys SET status = ?, delay_mins = ? WHERE id = ?",
               (status, delay, journey_ids[key]))

# 4. BOOKINGS
booked = (today - timedelta(days=10)).isoformat(timespec="minutes")
db.executemany("INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, 'active', ?)", [
    ("BK1001", 1, journey_ids[("KGX", "YRK", -1, "10:30")], "Advance", 35.00, "C14", booked),
    ("BK1002", 1, journey_ids[("KGX", "EDB", 1, "12:00")], "Anytime", 152.40, "D22", booked),
    ("BK1003", 1, journey_ids[("KGX", "EDB", 2, "09:00")], "Advance", 49.90, "B07", booked),
    ("BK1004", 1, journey_ids[("EUS", "BHM", 3, "12:10")], "Off-Peak", 28.60, "A31", booked),
    ("BK2001", 2, journey_ids[("EUS", "MAN", -2, "15:20")], "Anytime", 98.00, "F12", booked),
    ("BK2002", 2, journey_ids[("MAN", "EUS", 2, "07:20")], "Advance", 32.50, "F09", booked),
])

db.commit()
db.close()
print(f"Created {config.DB_PATH}: 2 customers, {len(stations)} stations, "
      f"{next_id - 100} journeys, 6 bookings")
