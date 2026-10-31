"""Source-ordered state effects, independent of worker delivery order."""
import hashlib
import json
import uuid

from .repository import StateConflict


def _clock_key(action, value):
    if action in {"add_item", "remove_item"}:
        return "inventory:" + json.dumps(value, ensure_ascii=False)
    if action in {"add_memory_highlight", "remove_memory_highlight"}:
        return "memory_highlights:" + json.dumps(value, ensure_ascii=False)
    return {"npc_name": "npc_state.name", "npc_attire": "npc_state.attire",
            "npc_visual_status": "npc_state.visual_module_status",
            "npc_activity": "npc_state.current_activity"}.get(action, action)


def _flat(data):
    result = dict(data)
    npc = result.pop("npc_state", {})
    if isinstance(npc, dict):
        result.update({"npc_state." + key: value for key, value in npc.items()})
    return result


def _patch(data, patch):
    data = json.loads(json.dumps(data))
    for key, change in patch.items():
        target, field = (data.setdefault("npc_state", {}), key[10:]) if key.startswith("npc_state.") else (data, key)
        if change[0]:
            target[field] = change[1]
        else:
            target.pop(field, None)
    return data


_RELATIONSHIP = ("relationship_score", "relationship_level", "relationship_distribution")


def _apply(data, changes, event_key):
    from tools.memory_tool import _apply_changes
    data = _apply_changes(data, **{key: value for key, value in changes.items() if key != "relationship_delta"})
    if "relationship_delta" in changes:
        from relationship_state import SCORE_MIN, SCORE_MAX, compute_relationship_weights
        score = max(SCORE_MIN, min(SCORE_MAX, int(data.get("relationship_score", 0) or 0) + changes["relationship_delta"]))
        weights = compute_relationship_weights(score)
        # One stable draw per physical event: rebuilding a scope must not roll
        # an unrelated new relationship label or change global random state.
        draw = int(hashlib.sha256(event_key.encode()).hexdigest()[:16], 16) / 2**64
        level = max(weights, key=weights.get)
        for name, weight in sorted(weights.items()):
            draw -= weight
            if draw <= 0:
                level = name
                break
        data.update(relationship_score=score, relationship_level=level, relationship_distribution=weights)
    return data


class StateEffects:
    def __init__(self, repo):
        self.repo, self.conn = repo, repo.conn
        self.conn.execute("""CREATE TABLE IF NOT EXISTS state_effect_baselines (
            scope TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS state_effects (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
            scope TEXT NOT NULL, conversation_id TEXT, message_id INTEGER,
            event_ts REAL NOT NULL, kind TEXT NOT NULL, changes TEXT NOT NULL)""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS state_effect_scope ON state_effects(scope,conversation_id)")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS state_field_clocks (
            scope TEXT NOT NULL, field TEXT NOT NULL, event_ts REAL NOT NULL, message_id INTEGER NOT NULL,
            PRIMARY KEY(scope,field))""")

    def _baseline(self, scope):
        current = self.repo.get_state(scope)
        created = self.conn.execute("INSERT OR IGNORE INTO state_effect_baselines VALUES(?,?,?)",
                                   (scope, json.dumps(current["data"], ensure_ascii=False), current["updated_at"] or 0))
        if created.rowcount:
            history = self.conn.execute("SELECT * FROM state_history WHERE scope=? AND redacted=0 ORDER BY version", (scope,)).fetchall()
            if history and history[-1]["version"] == current["version"]:
                # Upgrade old sourced snapshots into patches. Unsourced changes
                # stay scope-owned; never guess that a shared owner baseline
                # belongs to whichever private account is being unlinked.
                self.conn.execute("UPDATE state_effect_baselines SET data='{}',updated_at=0 WHERE scope=?", (scope,))
                before = {}
                for row in history:
                    after = _flat(json.loads(row["data"]))
                    patch = {key: [key in after, after.get(key)] for key in before.keys() | after.keys()
                             if (key in before) != (key in after) or before.get(key) != after.get(key)}
                    ref = row["source_ref"] or ""
                    message = self.conn.execute("SELECT * FROM messages WHERE source_ref=? OR 'message:'||id=?", (ref, ref)).fetchone()
                    stamp = (message["ts"], message["id"]) if message else (row["effective_at"], 0)
                    event_key = ref if message and ref.startswith("message:") else f"legacy-state:{scope}:{row['version']}"
                    self.conn.execute("INSERT OR IGNORE INTO state_effects(event_key,scope,conversation_id,message_id,event_ts,kind,changes) "
                                      "VALUES(?,?,?,?,?,'patch',?)", (event_key, scope,
                                      message["conversation_id"] if message else None, message["id"] if message else None,
                                      stamp[0], json.dumps(patch, ensure_ascii=False)))
                    for key in patch:
                        if self._accept(scope, key, stamp):
                            self._mark(scope, key, stamp)
                    before = after
            else:
                for key in _flat(current["data"]):
                    self._mark(scope, key, (current["updated_at"] or 0, 0))
        return current

    def _accept(self, scope, key, stamp):
        keys = (key, key.split(":", 1)[0])
        row = self.conn.execute("SELECT event_ts,message_id FROM state_field_clocks WHERE scope=? "
                                "AND field IN (?,?) ORDER BY event_ts DESC,message_id DESC LIMIT 1",
                                (scope, *keys)).fetchone()
        return row is None or stamp >= tuple(row)

    def _mark(self, scope, key, stamp):
        self.conn.execute("INSERT INTO state_field_clocks VALUES(?,?,?,?) ON CONFLICT(scope,field) "
                          "DO UPDATE SET event_ts=excluded.event_ts,message_id=excluded.message_id",
                          (scope, key, *stamp))

    def automatic(self, scope, message, changes, expected_version=None):
        current = self._baseline(scope)
        event_key = "message:" + str(message["id"])
        if self.conn.execute("SELECT 1 FROM state_effects WHERE event_key=?", (event_key,)).fetchone():
            return False
        if expected_version is not None and current["version"] != expected_version:
            raise StateConflict(f"state version is {current['version']}, expected {expected_version}")
        stamp = (message["ts"], message["id"])
        accepted = {}
        for action, value in changes.items():
            key = _clock_key(action, value)
            if action == "relationship_delta" or self._accept(scope, key, stamp):
                accepted[action] = value
                if action != "relationship_delta":
                    self._mark(scope, key, stamp)
        self.conn.execute("INSERT INTO state_effects(event_key,scope,conversation_id,message_id,event_ts,kind,changes) "
                          "VALUES(?,?,?,?,?,'automatic',?)", (event_key, scope, message["conversation_id"],
                          message["id"], message["ts"], json.dumps(changes, ensure_ascii=False)))
        updated = _apply(current["data"], accepted, event_key) if accepted else current["data"]
        if "relationship_delta" in accepted:
            if not self._accept(scope, "relationship_score", stamp):
                # Clamping makes addition noncommutative at score bounds. A late
                # delta is folded in source order, never arrival order.
                replayed = self._relationship(scope)
                for field in _RELATIONSHIP:
                    if field in replayed:
                        updated[field] = replayed[field]
                    else:
                        updated.pop(field, None)
            else:
                for field in _RELATIONSHIP:
                    self._mark(scope, field, stamp)
        if updated != current["data"]:
            self.repo.put_state(scope, updated, current["version"], source_ref=event_key)
            return True
        return False

    def manual(self, scope, data, expected_version=None, source_ref=None):
        current = self._baseline(scope)
        message = self.conn.execute("SELECT * FROM messages WHERE source_ref=?", (source_ref,)).fetchone() if source_ref else None
        before, after = _flat(current["data"]), _flat(data)
        patch = {key: [key in after, after.get(key)] for key in before.keys() | after.keys()
                 if (key in before) != (key in after) or before.get(key) != after.get(key)}
        stamp = (message["ts"], message["id"]) if message else (self.repo.clock(), 0)
        version = self.repo.put_state(scope, data, expected_version, source_ref=source_ref)
        if patch:
            self.conn.execute("INSERT INTO state_effects(event_key,scope,conversation_id,message_id,event_ts,kind,changes) "
                              "VALUES(?,?,?,?,?,'patch',?)", ("manual:" + uuid.uuid4().hex, scope,
                              message["conversation_id"] if message else None, message["id"] if message else None,
                              stamp[0], json.dumps(patch, ensure_ascii=False)))
            for key in patch:
                self._mark(scope, key, stamp)
        return version

    def _relationship(self, scope):
        baseline = self.conn.execute("SELECT data FROM state_effect_baselines WHERE scope=?", (scope,)).fetchone()
        data = {key: value for key, value in json.loads(baseline[0]).items() if key in _RELATIONSHIP} if baseline else {}
        for row in self.conn.execute("SELECT * FROM state_effects WHERE scope=? ORDER BY event_ts,COALESCE(message_id,0),seq", (scope,)):
            if not self._source_valid(row):
                continue
            changes = json.loads(row["changes"])
            if row["kind"] == "automatic" and "relationship_delta" in changes:
                data = _apply(data, {"relationship_delta": changes["relationship_delta"]}, row["event_key"])
            elif row["kind"] == "patch":
                data = _patch(data, {key: value for key, value in changes.items() if key in _RELATIONSHIP})
        return data

    def _source_valid(self, effect):
        if effect["message_id"] is None:
            return True  # An unsourced manual patch remains owned by its scope.
        from .evidence import _active
        message = self.repo.get_message(effect["message_id"])
        return bool(message and _active(message, "message", include_archived=True))

    def _rebuild(self, scope, source_ref=None):
        baseline = self.conn.execute("SELECT * FROM state_effect_baselines WHERE scope=?", (scope,)).fetchone()
        data = json.loads(baseline["data"]) if baseline else {}
        self.conn.execute("DELETE FROM state_field_clocks WHERE scope=?", (scope,))
        for key in _flat(data):
            self._mark(scope, key, (baseline["updated_at"], 0))
        for row in self.conn.execute("SELECT * FROM state_effects WHERE scope=? ORDER BY event_ts,COALESCE(message_id,0),seq", (scope,)):
            if not self._source_valid(row):
                continue
            changes = json.loads(row["changes"])
            stamp = (row["event_ts"], row["message_id"] or 0)
            changes = {action: value for action, value in changes.items()
                       if (row["kind"] == "automatic" and action == "relationship_delta")
                       or self._accept(scope, _clock_key(action, value) if row["kind"] == "automatic" else action, stamp)}
            data = _apply(data, changes, row["event_key"]) if row["kind"] == "automatic" else _patch(data, changes)
            for action, value in changes.items():
                if action == "relationship_delta":
                    for field in _RELATIONSHIP:
                        self._mark(scope, field, stamp)
                else:
                    self._mark(scope, _clock_key(action, value) if row["kind"] == "automatic" else action,
                               stamp)
        self.repo.put_state(scope, data, source_ref=source_ref)

    def migrate_conversation(self, conversation_id, old_scope, new_scope):
        self._baseline(old_scope)
        self._baseline(new_scope)
        if old_scope != "owner":
            # A private baseline belongs to this conversation. Represent it as
            # a sourced patch, so a later unlink can separate it from owner state.
            baseline = self.conn.execute("SELECT * FROM state_effect_baselines WHERE scope=?", (old_scope,)).fetchone()
            patch = {key: [True, value] for key, value in _flat(json.loads(baseline["data"])).items()}
            if patch:
                self.conn.execute("INSERT INTO state_effects(event_key,scope,conversation_id,event_ts,kind,changes) "
                                  "VALUES(?,?,?,?, 'patch',?)", ("baseline:" + uuid.uuid4().hex, old_scope,
                                  conversation_id, baseline["updated_at"], json.dumps(patch, ensure_ascii=False)))
            self.conn.execute("UPDATE state_effect_baselines SET data='{}' WHERE scope=?", (old_scope,))
            self.conn.execute("UPDATE state_effects SET conversation_id=? WHERE scope=?",
                              (conversation_id, old_scope))
        self.conn.execute("UPDATE state_effects SET scope=? WHERE scope=? AND conversation_id=?",
                          (new_scope, old_scope, conversation_id))
        # Full historical snapshots may contain several identities; never move
        # those aggregate payloads into a different person's scope.
        marker = "identity:" + conversation_id
        self.conn.execute("UPDATE state_history SET data=NULL,source_ref=NULL,source_memory_id=NULL,redacted=1 "
                          "WHERE scope=? AND version >= COALESCE((SELECT MIN(h.version) FROM state_history h "
                          "WHERE h.scope=? AND (h.source_ref=? OR EXISTS (SELECT 1 FROM messages m "
                          "WHERE m.conversation_id=? AND (h.source_ref='message:'||m.id OR h.source_ref=m.source_ref)))),9223372036854775807)",
                          (old_scope, old_scope, marker, conversation_id))
        self._rebuild(old_scope, marker)
        self._rebuild(new_scope, marker)
