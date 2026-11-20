"""Repair legacy autonomous status writes using the tool's committed version receipt."""
import json
import re
from collections import defaultdict

from .state_effects import _RELATIONSHIP, _clock_key, _flat

_FIELD = "memory_highlights"
_ACTIONS = {"add_memory_highlight", "remove_memory_highlight"}
_POLICY = "autonomous_highlight_receipts_v2"
_SUCCESS = re.compile(r"动态状态已更新 \(版本: ([0-9]{1,19}) -> ([0-9]{1,19})\)")


def _confirmed(effects, scope, receipts):
    """A receipt must identify one committed operation, even when old source refs repeat."""
    matches = {}
    for receipt in receipts:
        record = json.loads(receipt["record"])
        result = record.get("result")
        match = _SUCCESS.match(result) if isinstance(result, str) else None
        if not match or record.get("source_message_refs"):
            continue
        version = int(match[2])
        if version != int(match[1]) + 1 or version > 2**63 - 1:
            continue
        snapshot = effects.conn.execute(
            "SELECT source_ref,recorded_at FROM state_history WHERE scope=? AND version=? AND redacted=0",
            (scope, version)).fetchone()
        try:
            started, finished = float(record["started_at"]), float(record["finished_at"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not snapshot or not started <= snapshot["recorded_at"] <= finished:
            continue
        args = record.get("args")
        if not isinstance(args, dict):
            continue
        candidates = [row for row in effects.conn.execute(
            "SELECT * FROM state_effects WHERE scope=? AND source_ref IS ? AND kind='manual_operations'",
            (scope, snapshot["source_ref"])) if json.loads(row["changes"]) == args]
        if len(candidates) == 1 and effects._source_valid(candidates[0]):
            seq = candidates[0]["seq"]
            # Duplicate attestations for a single operation are ambiguous.
            matches[seq] = None if seq in matches else (receipt, snapshot["recorded_at"], version)
    return {seq: attestation for seq, attestation in matches.items() if attestation}


def _project(initial, rows, repaired=None):
    if not isinstance(initial, list) or not all(isinstance(item, str) for item in initial):
        return None
    from .state_effects import _apply

    highlights = list(initial)
    for row in rows:
        changes = row["changes"]
        if row["kind"] == "patch":
            if _FIELD in changes:
                patch = changes[_FIELD]
                if not isinstance(patch, list) or len(patch) != 2:
                    return None
                present, value = patch
                if present and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
                    return None
                highlights = list(value) if present else []
            continue
        operations = {key: value for key, value in changes.items() if key in _ACTIONS}
        if repaired is not None and row["seq"] in repaired:
            operations = {}
        highlights = _apply({_FIELD: highlights}, operations, row["event_key"],
                            highlight_limit=row["highlight_limit"] if repaired is None else None)[_FIELD]
    return highlights


def _refresh_clocks(effects, scope, fields, baseline, rows):
    """Re-time just the affected clocks, retaining newer independent changes."""
    latest = {}
    if baseline:
        for field in _flat(json.loads(baseline["data"])).keys() & fields:
            latest[field] = (baseline["updated_at"], 0)
    for row in rows:
        stamp = (row["event_ts"], row["message_id"] or 0)
        if row["kind"] == "patch":
            written = row["changes"].keys()
        else:
            written = {_clock_key(key, value) for key, value in row["changes"].items()}
            if "relationship_delta" in row["changes"]:
                written.update(_RELATIONSHIP)
        for field in written & fields:
            latest[field] = max(stamp, latest.get(field, stamp))
    for field in fields:
        if field in latest:
            effects._mark(scope, field, latest[field])
        else:
            effects.conn.execute("DELETE FROM state_field_clocks WHERE scope=? AND field=?", (scope, field))


def _repair_scope(effects, scope, receipts):
    confirmed = _confirmed(effects, scope, receipts)
    if not confirmed:
        return
    rows = [dict(row) for row in effects.conn.execute("SELECT * FROM state_effects WHERE scope=? ORDER BY seq", (scope,))]
    for row in rows:
        row["changes"] = json.loads(row["changes"])
    baseline = effects.conn.execute("SELECT data,updated_at FROM state_effect_baselines WHERE scope=?", (scope,)).fetchone()
    current = effects.repo.get_state(scope)
    valid = [row for row in rows if effects._source_valid(row)]
    # An explicit replacement owns the entire list, including any deliberate omissions.
    anchor = max((i for i, row in enumerate(valid) if row["kind"] == "patch" and _FIELD in row["changes"]), default=-1)
    baseline_data = json.loads(baseline["data"]) if baseline else {}
    initial = baseline_data.get(_FIELD, [])
    tail = valid[anchor:] if anchor >= 0 else valid
    # Unlogged edits, retractions or late jobs can make the old operation stream
    # disagree with the active state. Never overwrite an unverified current list.
    repair_list = any(row["seq"] in confirmed and _ACTIONS.intersection(row["changes"]) for row in tail)
    if repair_list and (not isinstance(current["data"].get(_FIELD), list)
                        or _project(initial, tail) != current["data"][_FIELD]):
        return
    restored = _project(initial, tail, confirmed) if repair_list else None
    if repair_list:
        # The legacy worker stored proposed automatic operations even when a
        # newer field clock rejected one. FIFO can conceal that discrepancy in
        # today's 32 entries. Require the unbounded source-order replay (the
        # order used by withdrawal and identity migration) to agree as well.
        def source_order(row):
            if row["seq"] in confirmed:
                return confirmed[row["seq"]][1], 0, row["seq"]
            return row["event_ts"], row["message_id"] or 0, row["seq"]

        source_rows = sorted(valid, key=source_order)
        # A materialized baseline already owns the complete field at its
        # timestamp. Older proposed additions may never have been applied.
        if _FIELD in baseline_data and any(source_order(row)[0] < baseline["updated_at"]
                                           and (_FIELD in row["changes"] or _ACTIONS.intersection(row["changes"]))
                                           for row in source_rows):
            return
        source_anchor = max((i for i, row in enumerate(source_rows)
                             if row["kind"] == "patch" and _FIELD in row["changes"]), default=0)
        if _project(initial, source_rows[source_anchor:], confirmed) != restored:
            return
    fields = set()
    for row in rows:
        if row["seq"] not in confirmed:
            continue
        receipt, stamp, version = confirmed[row["seq"]]
        changes = row["changes"]
        fields.update(_clock_key(action, value) for action, value in changes.items())
        if "relationship_delta" in changes:
            fields.update(_RELATIONSHIP)
        changes = {key: value for key, value in changes.items() if key not in _ACTIONS}
        row.update(kind="autonomous_operations", message_id=None, changes=changes, event_ts=stamp)
        effects.conn.execute(
            "UPDATE state_effects SET kind=?,message_id=NULL,conversation_id=?,source_ref=?,event_ts=?,changes=? WHERE seq=?",
            (row["kind"], receipt["conversation_id"], receipt["source_ref"], stamp,
             json.dumps(changes, ensure_ascii=False), row["seq"]))
        # The old snapshot body stays verbatim. Correct its dependency too,
        # so deleting this receipt redacts the snapshots it actually produced.
        effects.conn.execute("UPDATE state_history SET source_ref=?,source_memory_id=? WHERE scope=? AND version=?",
                             (receipt["source_ref"], receipt["memory_id"], scope, version))
    _refresh_clocks(effects, scope, fields, baseline, valid)
    if repair_list:
        # Only the attested suffix adopts the new unbounded semantics. Older
        # events still replay with the policy in force before the last replacement.
        effects.conn.execute("UPDATE state_effects SET highlight_limit=NULL WHERE scope=? AND seq>=?",
                             (scope, tail[0]["seq"]))
        if restored != current["data"][_FIELD]:
            # The source-aware effects remain authoritative on future unlink or
            # withdrawal. Do not create an unsourced patch containing their text.
            effects.repo.put_state(scope, {**current["data"], _FIELD: restored}, current["version"])


def _receipts(conn, condition="", params=()):
    return conn.execute("""
        SELECT e.scope,e.source_ref,e.record,e.memory_id,m.conversation_id FROM experiences e
        JOIN memories m ON m.id=e.memory_id AND m.scope=e.scope AND m.deleted=0
        WHERE json_extract(e.record,'$.action')='update_status'
          AND json_extract(e.record,'$.autonomous')=1 AND json_extract(e.record,'$.status')='succeeded'
          AND NOT EXISTS (SELECT 1 FROM temporal_retractions r WHERE r.scope=e.scope
                          AND r.source_ref IN (e.source_ref, e.memory_id, m.source_ref))
    """ + condition, params)


def repair_autonomous_highlights(effects, *, receipt_ref=None):
    with effects.repo.transaction():
        if receipt_ref is not None:
            # New turn-scoped effects have no legacy manual operation to repair.
            # Probe only this receipt before examining other witnesses for its scope.
            row = _receipts(effects.conn, " AND e.source_ref=?", (receipt_ref,)).fetchone()
            if row and _confirmed(effects, row["scope"], [row]):
                scoped = _receipts(effects.conn, " AND e.scope=?", (row["scope"],))
                _repair_scope(effects, row["scope"], scoped)
            return
        if effects.conn.execute("SELECT 1 FROM meta WHERE key=?", (_POLICY,)).fetchone():
            return
        receipts = defaultdict(list)
        for row in _receipts(effects.conn):
            receipts[row["scope"]].append(row)
        for scope, scoped in receipts.items():
            _repair_scope(effects, scope, scoped)
        effects.conn.execute("INSERT INTO meta(key,value) VALUES(?, '1')", (_POLICY,))
