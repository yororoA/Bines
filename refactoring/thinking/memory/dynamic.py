import json
import os
import threading
import copy
import time
from datetime import datetime, timezone, timedelta
from .relationship import apply_relationship_delta


def _acquire_file_lock(lock_path: str, timeout: float = 5.0, poll_interval: float = 0.05):
    start = time.time()
    last_error = None
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(time.time()).encode("utf-8"))
            except Exception:
                pass
            return fd
        except FileExistsError as e:
            last_error = e
            if time.time() - start > timeout:
                try:
                    os.remove(lock_path)
                    start = time.time()
                    continue
                except OSError:
                    start = time.time()
            time.sleep(poll_interval)
        except OSError as e:
            last_error = e
            break
    print(f"[FileLock] Failed to acquire lock {lock_path}: {last_error}")
    return None


def _release_file_lock(fd, lock_path: str):
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(lock_path)
    except OSError:
        pass


class DynamicMemory:
    def __init__(self, filepath="memory_dynamic.json"):
        self.filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), filepath)
        self.lock = threading.RLock()
        self.state = {
            "current_time": "",
            "current_location": "",
            "npc_state": {
                "name": "",
                "attire": "",
                "visual_module_status": "",
                "current_activity": "",
            },
            "relationship_level": "",
            "relationship_score": 0,
            "relationship_distribution": {},
            "inventory": [],
            "active_quest": "",
            "important_thing": "",
            "memory_highlights": [],
        }
        self.load()
        self._ensure_fields()

    def load(self):
        with self.lock:
            if os.path.exists(self.filepath):
                try:
                    lock_fd = _acquire_file_lock(self.filepath + ".lock")
                    try:
                        with open(self.filepath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    finally:
                        _release_file_lock(lock_fd, self.filepath + ".lock")
                    if isinstance(data, dict):
                        for k in ("banned_topics", "user_state", "user_name", "user_appearance", "user_mood"):
                            data.pop(k, None)
                        self._deep_update(self.state, data)
                except Exception as e:
                    print(f"[DynamicMemory] Load error: {e}")

    def _deep_update(self, base_dict, update_dict):
        for key, value in update_dict.items():
            if key in base_dict and isinstance(base_dict[key], dict) and isinstance(value, dict):
                self._deep_update(base_dict[key], value)
            else:
                base_dict[key] = value

    def _ensure_fields(self):
        defaults = {
            "current_time": "",
            "npc_state": {
                "name": "",
                "attire": "",
                "visual_module_status": "",
                "current_activity": "",
            },
            "important_thing": "",
            "memory_highlights": [],
        }
        for key, default_value in defaults.items():
            if key not in self.state:
                self.state[key] = default_value
            elif isinstance(default_value, dict) and isinstance(self.state[key], dict):
                for sub_key, sub_default in default_value.items():
                    if sub_key not in self.state[key]:
                        self.state[key][sub_key] = sub_default

    def save(self):
        with self.lock:
            try:
                now = datetime.now()
                try:
                    if now.utcoffset() is not None:
                        offset_seconds = now.utcoffset().total_seconds()
                        offset_hours = int(offset_seconds / 3600)
                        tz = timezone(timedelta(hours=offset_hours))
                        current_time_str = now.astimezone(tz).isoformat()
                    else:
                        tz = timezone(timedelta(hours=8))
                        current_time_str = now.astimezone(tz).isoformat()
                except Exception:
                    current_time_str = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")
                self.state["current_time"] = current_time_str

                temp_filepath = self.filepath + ".tmp"
                lock_fd = _acquire_file_lock(self.filepath + ".lock")
                try:
                    with open(temp_filepath, "w", encoding="utf-8") as f:
                        json.dump(self.state, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_filepath, self.filepath)
                finally:
                    _release_file_lock(lock_fd, self.filepath + ".lock")
            except Exception as e:
                print(f"[DynamicMemory] Save error: {e}")
                temp_filepath = self.filepath + ".tmp"
                if os.path.exists(temp_filepath):
                    try:
                        os.remove(temp_filepath)
                    except Exception:
                        pass

    def to_prompt_str(self) -> str:
        with self.lock:
            state_snapshot = copy.deepcopy(self.state)
        lines = ["--- DYNAMIC STATUS ---"]
        for k, v in state_snapshot.items():
            lines.append(f"{k}: {v}")
        return "\n".join(lines)

    def to_json_str(self) -> str:
        with self.lock:
            state_snapshot = copy.deepcopy(self.state)
        return json.dumps(state_snapshot, ensure_ascii=False, indent=2)

    def update_status(self, **kwargs) -> str:
        with self.lock:
            updated_fields = []

            if "current_time" in kwargs and kwargs["current_time"]:
                self.state["current_time"] = kwargs["current_time"]
                updated_fields.append("current_time")

            if "current_location" in kwargs and kwargs["current_location"]:
                self.state["current_location"] = kwargs["current_location"]
                updated_fields.append("current_location")

            if "relationship_delta" in kwargs:
                delta = kwargs["relationship_delta"]
                if isinstance(delta, (int, float)):
                    new_score, new_level, new_dist = apply_relationship_delta(
                        self.state.get("relationship_score", 0), int(delta)
                    )
                    self.state["relationship_score"] = new_score
                    self.state["relationship_level"] = new_level
                    self.state["relationship_distribution"] = new_dist
                    updated_fields.append(f"relationship({new_level}, score={new_score})")

            if "add_item" in kwargs and kwargs["add_item"]:
                inventory = self.state.get("inventory", [])
                if kwargs["add_item"] not in inventory:
                    inventory.append(kwargs["add_item"])
                    self.state["inventory"] = inventory
                    updated_fields.append(f"+item:{kwargs['add_item']}")

            if "remove_item" in kwargs and kwargs["remove_item"]:
                inventory = self.state.get("inventory", [])
                if kwargs["remove_item"] in inventory:
                    inventory.remove(kwargs["remove_item"])
                    self.state["inventory"] = inventory
                    updated_fields.append(f"-item:{kwargs['remove_item']}")

            if "active_quest" in kwargs and kwargs["active_quest"]:
                self.state["active_quest"] = kwargs["active_quest"]
                updated_fields.append("active_quest")

            npc_fields = {
                "npc_name": "name",
                "npc_attire": "attire",
                "npc_visual_status": "visual_module_status",
                "npc_activity": "current_activity",
            }
            for kw_key, npc_key in npc_fields.items():
                if kw_key in kwargs and kwargs[kw_key]:
                    self.state.setdefault("npc_state", {})[npc_key] = kwargs[kw_key]
                    updated_fields.append(f"npc_{npc_key}")

            if "add_memory_highlight" in kwargs and kwargs["add_memory_highlight"]:
                highlights = self.state.get("memory_highlights", [])
                highlights.append(kwargs["add_memory_highlight"])
                self.state["memory_highlights"] = highlights
                updated_fields.append(f"+highlight")

            if "remove_memory_highlight" in kwargs and kwargs["remove_memory_highlight"]:
                highlights = self.state.get("memory_highlights", [])
                target = kwargs["remove_memory_highlight"]
                if target in highlights:
                    highlights.remove(target)
                    self.state["memory_highlights"] = highlights
                    updated_fields.append(f"-highlight")

            if "important_thing" in kwargs and kwargs["important_thing"]:
                self.state["important_thing"] = kwargs["important_thing"]
                updated_fields.append("important_thing")

            self.save()

        result = f"Updated: {', '.join(updated_fields)}" if updated_fields else "No changes"
        print(f"[DynamicMemory] {result}")
        return result
