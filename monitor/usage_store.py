"""Small, monitor-owned ledger; never stores rollout text or titles."""
import json
from pathlib import Path
import sqlite3


class UsageStore:
    def __init__(self, path):
        self.path = Path(path)
        self.conn = None

    def _open(self):
        if self.conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, timeout=0.05)
            self.conn.execute("CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, ledger TEXT NOT NULL)")
            self.conn.commit()
        return self.conn

    def load(self, sid):
        row = self._open().execute("SELECT ledger FROM usage WHERE id=?", (sid,)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        if not isinstance(value, dict) or not isinstance(value.get("raw"), list) or not isinstance(value.get("total"), list):
            raise ValueError("invalid usage ledger")
        if len(value["raw"]) != 3 or len(value["total"]) != 3:
            raise ValueError("invalid usage counters")
        for counter in value["raw"] + value["total"]:
            if counter is not None and (type(counter) is not int or counter < 0):
                raise ValueError("invalid usage counter")
        return value

    def save(self, rows):
        conn = self._open()
        with conn:
            conn.executemany("INSERT OR REPLACE INTO usage(id,ledger) VALUES (?,?)",
                             [(sid, json.dumps(value, separators=(",", ":"))) for sid, value in rows.items()])

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
