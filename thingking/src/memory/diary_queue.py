"""Persistent dirty-day scheduling; source writes and queue updates share a transaction."""
from __future__ import annotations


class DiaryQueue:
    def __init__(self, repo):
        self.repo, self.conn = repo, repo.conn
        with repo.transaction():
            self.conn.execute("""CREATE TABLE IF NOT EXISTS diary_dirty_days (
                scope TEXT NOT NULL,day_key TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 1,
                available_at REAL NOT NULL DEFAULT 0,PRIMARY KEY(scope,day_key))""")
            self.conn.execute("CREATE INDEX IF NOT EXISTS diary_dirty_due ON diary_dirty_days(scope,available_at,day_key)")
            self.conn.execute("INSERT INTO meta VALUES('diary_backfill_cursor','0') ON CONFLICT DO NOTHING")
            for table, columns in (("memories", "scope,kind,day_key,content,meta,origin,deleted"),
                                   ("messages", "scope,day_key,content,role,origin,episode_id,ts")):
                for event, suffix, aliases in (("INSERT", "ai", ("new",)), ("UPDATE OF " + columns, "au", ("old", "new")),
                                               ("DELETE", "ad", ("old",))):
                    body = ""
                    for alias in aliases:
                        condition = f"{alias}.scope='owner' AND {alias}.day_key IS NOT NULL"
                        if table == "memories":
                            condition += f" AND {alias}.kind IN ('episode','diary')"
                        body += (f"INSERT INTO diary_dirty_days(scope,day_key) SELECT {alias}.scope,{alias}.day_key "
                                 f"WHERE {condition} ON CONFLICT(scope,day_key) DO UPDATE SET "
                                 "generation=diary_dirty_days.generation+1,available_at=0;")
                    self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS diary_dirty_{table}_{suffix} AFTER {event} ON {table} BEGIN {body} END")

    def mark(self, scope, day):
        # Daily journals currently belong to owner scope only. Private/group
        # episodes become eligible automatically if later moved into owner.
        if scope == "owner" and day:
            self.conn.execute("INSERT INTO diary_dirty_days(scope,day_key) VALUES(?,?) "
                              "ON CONFLICT(scope,day_key) DO UPDATE SET generation=generation+1,available_at=0", (scope, day))

    def backfill(self, limit=256):
        """Resume one bounded legacy scan; new rows are already covered by triggers."""
        with self.repo.transaction():
            cursor = self.conn.execute("SELECT value FROM meta WHERE key='diary_backfill_cursor'").fetchone()[0]
            if cursor == "done":
                return 0
            rows = self.conn.execute("SELECT rowid,scope,day_key,kind FROM memories WHERE rowid>? ORDER BY rowid LIMIT ?",
                                     (int(cursor), max(1, int(limit)))).fetchall()
            for row in rows:
                if row["kind"] in {"episode", "diary"}:
                    self.mark(row["scope"], row["day_key"])
            next_cursor = str(rows[-1]["rowid"]) if len(rows) == max(1, int(limit)) else "done"
            self.conn.execute("UPDATE meta SET value=? WHERE key='diary_backfill_cursor'", (next_cursor,))
            return len(rows)

    def due(self, before_day, limit=32):
        return [dict(row) for row in self.conn.execute("SELECT * FROM diary_dirty_days "
            "WHERE scope='owner' AND day_key<? AND available_at<=? ORDER BY available_at,day_key LIMIT ?",
            (before_day, self.repo.clock(), max(1, int(limit))))]

    def ack(self, row):
        self.conn.execute("DELETE FROM diary_dirty_days WHERE scope=? AND day_key=? AND generation=?",
                          (row["scope"], row["day_key"], row["generation"]))

    def defer(self, row, seconds=600):
        self.conn.execute("UPDATE diary_dirty_days SET available_at=? WHERE scope=? AND day_key=? AND generation=?",
                          (self.repo.clock() + seconds, row["scope"], row["day_key"], row["generation"]))
