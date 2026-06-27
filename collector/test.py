import sqlite3
from pathlib import Path

db_path = Path.home() / "PowerMonitor" / "data" / "power.sqlite"

conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    production_wh REAL,
    consumption_wh REAL,
    solar_to_home_wh REAL,
    solar_to_grid_wh REAL,
    grid_to_home_wh REAL
)
""")

conn.commit()
conn.close()

print(f"SQLite OK: {db_path}")
