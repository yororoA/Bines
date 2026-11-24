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
_OPERATION_KINDS = {"automatic", "manual_operations", "autonomous_operations"}


def _manual_fields(kind, changes):
    fields = set()
    for action, value in changes.items():
        if kind == "patch":
            fields.add(action)
        elif action == "relationship_delta":
            fields.update(_RELATIONSHIP)
        else:
            fields.add(_clock_key(action, value))
            # The state worker can phrase the same single proposed highlight
            # differently. An explicit addition already owns that extraction slot.
            if action == "add_memory_highlight":
                fields.add(action)
    return fields


def _remaining_automatic(changes, owned):
    available = {}
    for action, value in changes.items():
        field = _clock_key(action, value)
        if action == "relationship_delta" and owned.intersection(_RELATIONSHIP):
            continue
        if action not in owned and field not in owned and field.split(":", 1)[0] not in owned:
            available[action] = value
    return available


def _state_fields(kind, action, value):
    if kind in _OPERATION_KINDS and action == "relationship_delta":
        return set(_RELATIONSHIP)
    field = _clock_key(action, value).split(":", 1)[0] if kind in _OPERATION_KINDS else action
    return {field}


def _apply(data, changes, event_key, *, highlight_limit=None):
    from tools.memory_tool import _apply_changes
    data = _apply_changes(data, **{key: value for key, value in changes.items() if key != "relationship_delta"})
    if highlight_limit is not None and isinstance(data["memory_highlights"], list):
        # Reproduce the original FIFO policy only for pre-upgrade events.
        data["memory_highlights"] = data["memory_highlights"][-highlight_limit:]
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
            event_ts REAL NOT NULL, kind TEXT NOT NULL, changes TEXT NOT NULL, source_ref TEXT,
            highlight_limit INTEGER DEFAULT 32)""")
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(state_effects)")}
        if "source_ref" not in columns:
            self.conn.execute("ALTER TABLE state_effects ADD COLUMN source_ref TEXT")
        if "highlight_limit" not in columns:
            self.conn.execute("ALTER TABLE state_effects ADD COLUMN highlight_limit INTEGER DEFAULT 32")
        self.conn.execute("CREATE INDEX IF NOT EXISTS state_effect_scope ON state_effects(scope,conversation_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS state_effect_source ON state_effects(source_ref,scope)")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS state_field_clocks (
            scope TEXT NOT NULL, field TEXT NOT NULL, event_ts REAL NOT NULL, message_id INTEGER NOT NULL,
            PRIMARY KEY(scope,field))""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS state_effect_time ON state_effects(scope,event_ts)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS state_effect_message ON state_effects(scope,message_id) WHERE message_id IS NOT NULL")

    def conversation_state(self, scope, day, day_start_hour):
        """Yesterday's autonomous activity is still in the ledger, outside today's dialogue state."""
        from .models import day_key

        data = self.repo.get_state(scope)["data"]
        clocks = self.conn.execute(
            "SELECT field,event_ts,message_id FROM state_field_clocks WHERE scope=? "
            "AND field IN ('important_thing','npc_state.current_activity')", (scope,)
        ).fetchall()
        for field, timestamp, message_id in clocks:
            if day_key(timestamp, day_start_hour) >= day:
                continue
            action = "npc_activity" if field == "npc_state.current_activity" else field
            target = data.get("npc_state") if action == "npc_activity" else data
            key = "current_activity" if action == "npc_activity" else field
            if not isinstance(target, dict) or not target.get(key):
                continue
            # A later human confirmation of the very same value owns the field.
            # Use its field clock; unrelated state updates cannot refresh or expire it.
            for row in self.conn.execute(
                "SELECT kind,changes FROM state_effects WHERE scope=? AND event_ts=? "
                "AND COALESCE(message_id,0)=? ORDER BY seq DESC", (scope, timestamp, message_id)
            ):
                changes = json.loads(row["changes"])
                if (field if row["kind"] == "patch" else action) in changes:
                    if row["kind"] == "autonomous_operations" and changes[action] == target[key]:
                        del target[key]
                    break
        return data

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

    def _fold(self, scope, data, kind, changes, key, stamp, highlight_limit=None):
        operations = kind in _OPERATION_KINDS
        accepted = {}
        for action, value in changes.items():
            field = _clock_key(action, value) if operations else action
            if operations and action == "relationship_delta":
                fields = _RELATIONSHIP
                if not all(self._accept(scope, name, stamp) for name in fields):
                    continue
            elif self._accept(scope, field, stamp):
                fields = (field,)
            else:
                continue
            accepted[action] = value
            for name in fields:
                self._mark(scope, name, stamp)
        if operations:
            if not accepted and highlight_limit is None:
                return data
            return _apply(data, accepted, key, highlight_limit=highlight_limit)
        return _patch(data, accepted)

    def _commit_effect(self, scope, current, kind, changes, key, stamp, conversation, message_id,
                       source_ref=None, *, rebuild=False, snapshot=None):
        # Fold late proposals in their source order. Even two distinct list
        # additions, or two deltas near the score cap, can depend on that order.
        if changes and not rebuild:
            newer = self.conn.execute(
                "SELECT 1 FROM state_effects WHERE scope=? AND event_ts>=? "
                "AND (event_ts,COALESCE(message_id,0))>(?,?) LIMIT 1", (scope, stamp[0], *stamp)).fetchone()
            earlier_than_baseline = self.conn.execute(
                "SELECT 1 FROM state_effect_baselines WHERE scope=? AND updated_at>?", (scope, stamp[0])).fetchone()
            rebuild = bool(newer or earlier_than_baseline)
        automatic = kind == "automatic"
        if changes or automatic:
            self.conn.execute(
                "INSERT INTO state_effects(event_key,scope,conversation_id,message_id,event_ts,kind,changes,source_ref,highlight_limit) "
                "VALUES(?,?,?,?,?,?,?,?,NULL)", (key, scope, conversation, message_id, stamp[0], kind,
                                                  json.dumps(changes, ensure_ascii=False), source_ref))
        history_ref = key if automatic else source_ref
        if rebuild:
            fields = set().union(*(_state_fields(kind, action, value) for action, value in changes.items()))
            return self._rebuild(scope, history_ref, only_if_changed=automatic, fields=fields)
        updated = self._fold(scope, current["data"], kind, changes, key, stamp)
        if snapshot is not None:
            updated = snapshot  # Preserve the exact object for a current full-snapshot writer.
        if automatic and updated == current["data"]:
            return current["version"]
        return self.repo.put_state(scope, updated, current["version"], source_ref=history_ref)

    def import_missing(self, scope, data, source_ref):
        """Record only the fields a legacy snapshot actually contributes."""
        current = self._baseline(scope)
        missing = {key: value for key, value in data.items() if key not in current["data"]}
        patch = {key: [True, value] for key, value in _flat(missing).items()}
        ref = "import:" + source_ref
        return self._commit_effect(scope, current, "patch", patch, ref, (self.repo.clock(), 0),
                                   None, None, source_ref=ref, snapshot={**current["data"], **missing})

    def automatic(self, scope, message, changes, expected_version=None, *, defer_stale=False) -> bool | None:
        """Return None only when a fenced worker needs fresh materials before writing."""
        current = self._baseline(scope)
        event_key = "message:" + str(message["id"])
        if self.conn.execute("SELECT 1 FROM state_effects WHERE event_key=?", (event_key,)).fetchone():
            return False
        owned = set()
        for row in self.conn.execute("SELECT kind,changes FROM state_effects WHERE scope=? AND message_id=? "
                                     "AND kind IN ('manual_operations','patch')", (scope, message["id"])):
            owned.update(_manual_fields(row["kind"], json.loads(row["changes"])))
        changes = _remaining_automatic(changes, owned)
        if expected_version is not None and current["version"] != expected_version:
            # A tool can finish while the extraction is in flight. Merge the
            # untouched fields only if every intervening snapshot is its source.
            same_source = self.conn.execute(
                "SELECT COUNT(*) FROM state_history WHERE scope=? AND version>? AND version<=? "
                "AND redacted=0 AND source_ref=?", (scope, expected_version, current["version"], message["source_ref"])
            ).fetchone()[0]
            if not same_source or same_source != current["version"] - expected_version:
                if defer_stale:
                    return None
                raise StateConflict(f"state version is {current['version']}, expected {expected_version}")
        version = self._commit_effect(scope, current, "automatic", changes, event_key, (message["ts"], message["id"]),
                                      message["conversation_id"], message["id"])
        return version != current["version"]

    def manual(self, scope, data, expected_version=None, source_ref=None, changes=None, autonomous_conversation=None):
        current = self._baseline(scope)
        if expected_version is not None and current["version"] != expected_version:
            raise StateConflict(f"state version is {current['version']}, expected {expected_version}")
        message = None
        if source_ref and autonomous_conversation is None:
            message = self.conn.execute("SELECT * FROM messages WHERE source_ref=?", (source_ref,)).fetchone()
        if message and self._message_state_scope(message) != scope:
            raise ValueError("state source message must belong to the same state scope")
        event_key = "manual:" + uuid.uuid4().hex
        if changes is not None:
            import inspect
            from tools.memory_tool import _apply_changes
            allowed = set(inspect.signature(_apply_changes).parameters) - {"state"}
            if not isinstance(changes, dict) or set(changes) - allowed:
                raise ValueError("状态操作包含未知字段")
            changes = {key: value for key, value in changes.items() if value is not None}
            if any(type(value) is not int if key == "relationship_delta" else not isinstance(value, str)
                   for key, value in changes.items()):
                raise ValueError("状态操作需要字符串字段和整数好感度增量")
            # Explicit operations distinguish a fresh same-value confirmation
            # from fields merely carried forward in the caller's full snapshot.
            kind = "autonomous_operations" if autonomous_conversation is not None else "manual_operations"
            effect = changes
            if "relationship_delta" in effect and kind == "manual_operations" and source_ref:
                # One real user message owns one explicit relationship delta,
                # including retries arriving before the source message outbox.
                previous = self.conn.execute(
                    "SELECT 1 FROM state_effects WHERE scope=? AND source_ref=? AND kind='manual_operations' "
                    "AND json_type(changes,'$.relationship_delta') IS NOT NULL LIMIT 1", (scope, source_ref)).fetchone()
                if previous:
                    effect.pop("relationship_delta")
        else:
            before, after = _flat(current["data"]), _flat(data)
            effect = {key: [key in after, after.get(key)] for key in before.keys() | after.keys()
                      if (key in before) != (key in after) or before.get(key) != after.get(key)}
            kind = "patch"
        stamp = (message["ts"], message["id"]) if message else (self.repo.clock(), 0)
        superseded = False
        if message and effect:
            # The worker may have completed first. Retain its independent
            # fields but replace any inference the explicit operation owns.
            auto_key = "message:" + str(message["id"])
            row = self.conn.execute("SELECT changes FROM state_effects WHERE event_key=? AND kind='automatic'",
                                    (auto_key,)).fetchone()
            if row:
                prior = json.loads(row["changes"])
                remaining = _remaining_automatic(prior, _manual_fields(kind, effect))
                if remaining != prior:
                    self.conn.execute("UPDATE state_effects SET changes=? WHERE event_key=?",
                                      (json.dumps(remaining, ensure_ascii=False), auto_key))
                    superseded = True
        return self._commit_effect(scope, current, kind, effect, event_key, stamp,
                                   message["conversation_id"] if message else autonomous_conversation,
                                   message["id"] if message else None, source_ref, rebuild=superseded,
                                   snapshot=data if kind == "patch" else None)

    def _source_valid(self, effect):
        if effect["kind"] == "autonomous_operations":
            # The turn receipt may arrive through the outbox after this effect.
            return not self.conn.execute(
                "SELECT 1 FROM temporal_retractions WHERE scope=? AND source_ref=?",
                (effect["scope"], effect["source_ref"]),
            ).fetchone()
        if effect["message_id"] is None:
            return True  # An unsourced manual patch remains owned by its scope.
        from .evidence import _active
        message = self.repo.get_message(effect["message_id"])
        return bool(message and _active(message, "message", include_archived=True))

    def _message_state_scope(self, message):
        from .models import CHANNEL_QQ_GROUP
        conversation = self.conn.execute("SELECT channel FROM conversations WHERE id=?",
                                         (message["conversation_id"],)).fetchone()
        if conversation and conversation["channel"] == CHANNEL_QQ_GROUP:
            return message["scope"] + ":user:" + message["actor_id"].removeprefix("qq:")
        return message["scope"]

    def link_message(self, message, *, rebuild=True):
        """Attach desktop state operations whose raw message arrived via outbox later."""
        if message.get("source_ref"):
            scope = self._message_state_scope(message)
            linked = self.conn.execute("UPDATE state_effects SET message_id=?,conversation_id=?,event_ts=? "
                                       "WHERE source_ref=? AND scope=? AND message_id IS NULL AND kind!='autonomous_operations' "
                                       "AND event_key NOT LIKE 'import:%'",
                                       (message["id"], message["conversation_id"], message["ts"],
                                        message["source_ref"], scope)).rowcount
            if linked and rebuild:
                # Delivery time previously ordered these operations. Replaying
                # also retimes field clocks, including unchanged current values.
                self._rebuild(scope, only_if_changed=True)

    def retract_message(self, message):
        """Remove only this invalid source's effects, including group-user state."""
        refs = {"message:" + str(message["id"])}
        if message.get("source_ref"):
            refs.add(message["source_ref"])
        self.link_message(message, rebuild=False)
        state_scope = self._message_state_scope(message)
        marks = ",".join("?" for _ in refs)
        scopes = {row[0] for row in self.conn.execute(
            "SELECT DISTINCT scope FROM state_effects WHERE message_id=? AND scope=? "
            "UNION SELECT DISTINCT scope FROM state_history WHERE redacted=0 AND scope=? "
            f"AND source_ref IN ({marks})", (message["id"], state_scope, state_scope, *sorted(refs)))}
        # Old databases may only have sourced snapshots. Convert those into
        # patches before temporal retraction redacts their aggregate payloads.
        for scope in sorted(scopes):
            self._baseline(scope)
        for scope in sorted(scopes | {message["scope"]}):
            for ref in sorted(refs):
                self.repo.temporal.retract_source(scope, ref)
        for scope in sorted(scopes):
            self._rebuild(scope, only_if_changed=True)

    def retract_experience(self, experience):
        from .models import CHANNEL_QQ_GROUP
        scopes = {row[0] for row in self.conn.execute(
            "SELECT DISTINCT scope FROM state_effects WHERE source_ref=? AND conversation_id IS ? "
            "AND kind='autonomous_operations'", (experience["source_ref"], experience["conversation_id"]))}
        if experience["channel"] == CHANNEL_QQ_GROUP:
            scopes = {scope for scope in scopes if scope.startswith(experience["scope"] + ":user:")}
            if experience["member"]:
                scopes.intersection_update({experience["member"]})
            elif len(scopes) != 1:
                return  # An older receipt cannot distinguish a reused turn ref across members.
        else:
            scopes.intersection_update({experience["scope"]})
        for scope in scopes:
            if scope != experience["scope"]:
                self.repo.temporal.retract_source(scope, experience["source_ref"])
            self._rebuild(scope, only_if_changed=True)

    def _rebuild(self, scope, source_ref=None, *, only_if_changed=False, fields=None):
        baseline = self.conn.execute("SELECT * FROM state_effect_baselines WHERE scope=?", (scope,)).fetchone()
        data = json.loads(baseline["data"]) if baseline else {}
        current = self.repo.get_state(scope)
        if fields is None:
            self.conn.execute("DELETE FROM state_field_clocks WHERE scope=?", (scope,))
        else:
            # Older gateway imports bypassed the effect ledger. Updating a
            # late field must never replace unrelated live fields or clocks.
            if fields.intersection(_RELATIONSHIP):
                fields.update(_RELATIONSHIP)
            requested = fields.copy()
            # A first status operation also creates empty containers. Follow
            # their subsequent explicit edits to retain the normal write shape.
            missing_defaults = {key: empty for key, empty in (("inventory", []), ("memory_highlights", []), ("npc_state", {}))
                                if key not in current["data"] and key not in fields}
            fields.update(missing_defaults)
            for field in fields:
                self.conn.execute("DELETE FROM state_field_clocks WHERE scope=? AND "
                                  "(field=? OR substr(field,1,length(?)+1)=?||':')", (scope, field, field, field))
        for key in _flat(data):
            if fields is None or key in fields:
                self._mark(scope, key, (baseline["updated_at"], 0))
        for row in self.conn.execute("SELECT * FROM state_effects WHERE scope=? ORDER BY event_ts,COALESCE(message_id,0),seq", (scope,)):
            changes = json.loads(row["changes"])
            limit = row["highlight_limit"]
            if fields is not None:
                changes = {action: value for action, value in changes.items()
                           if fields.intersection(_state_fields(row["kind"], action, value))}
                if "memory_highlights" not in fields:
                    limit = None
                if not changes and limit is None:
                    continue
            if not self._source_valid(row):
                continue
            stamp = (row["event_ts"], row["message_id"] or 0)
            data = self._fold(scope, data, row["kind"], changes, row["event_key"], stamp, limit)
        if fields is not None:
            after = _flat(data)
            merged = _patch(current["data"], {key: [key in after, after.get(key)] for key in requested})
            for key, empty in missing_defaults.items():
                if key in data and data[key] == empty:
                    merged.setdefault(key, empty)
            data = merged
        current_snapshot = self.conn.execute("SELECT 1 FROM state_history WHERE scope=? AND version=? AND redacted=0",
                                             (scope, current["version"])).fetchone()
        if not only_if_changed or data != current["data"] or not current_snapshot:
            return self.repo.put_state(scope, data, source_ref=source_ref)
        return current["version"]

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
        # Full historical snapshots may contain several identities; never move
        # those aggregate payloads into a different person's scope.
        marker = "identity:" + conversation_id
        self.conn.execute("UPDATE state_history SET data=NULL,source_ref=NULL,source_memory_id=NULL,redacted=1 "
                          "WHERE scope=? AND version >= COALESCE((SELECT MIN(h.version) FROM state_history h "
                          "WHERE h.scope=? AND (h.source_ref=? OR EXISTS (SELECT 1 FROM messages m "
                          "WHERE m.conversation_id=? AND (h.source_ref='message:'||m.id OR h.source_ref=m.source_ref)) "
                          "OR EXISTS (SELECT 1 FROM state_effects e WHERE e.scope=h.scope AND e.source_ref=h.source_ref "
                          "AND e.conversation_id=? AND e.kind='autonomous_operations'))),9223372036854775807)",
                          (old_scope, old_scope, marker, conversation_id, conversation_id))
        self.conn.execute("UPDATE state_effects SET scope=? WHERE scope=? AND conversation_id=?",
                          (new_scope, old_scope, conversation_id))
        self._rebuild(old_scope, marker)
        self._rebuild(new_scope, marker)
