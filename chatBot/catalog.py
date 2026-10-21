"""Offline official API reference, portable schemas and an exhaustive model index."""
import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

API_DIR = Path(__file__).with_name("api")


@lru_cache(maxsize=1)
def bundled_document():
    manifest = json.loads((API_DIR / "manifest.json").read_text(encoding="utf-8"))
    path = API_DIR / manifest["file"]
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
        raise RuntimeError("NapCat API 文档校验失败；请重新生成官方目录")
    document = json.loads(raw)
    if len(document["paths"]) != manifest["actions"]:
        raise RuntimeError("NapCat API 路径数量与 manifest 不一致")
    return manifest, document


def portable_schema(schema, definitions):
    """Replace TypeBox/OpenAPI references with self-contained JSON Schema $defs."""
    required_defs = {}
    unresolved = set()

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key == "$id":
                continue  # Nested $id would change the base URI of our local references.
            if key == "$ref":
                if item.startswith("#/$defs/"):
                    name = item.removeprefix("#/$defs/")
                else:
                    name = item.removeprefix("#/components/schemas/")
                if name in definitions:
                    result[key] = "#/$defs/"+name
                    if name not in required_defs:
                        required_defs[name] = {}  # Mark before descending into recursive nodes.
                        required_defs[name] = visit(definitions[name])
                else:
                    unresolved.add(item)
                    result["x-napcat-unresolved-ref"] = item
            else:
                result[key] = visit(item)
        return result

    result = visit(copy.deepcopy(schema or {"type": "object", "properties": {}}))
    if required_defs:
        result["$defs"] = required_defs
    if unresolved:
        result["x-napcat-schema-warning"] = "Unresolved schema references: "+", ".join(sorted(unresolved))
    return result


@lru_cache(maxsize=1)
def reference_catalog():
    manifest, document = bundled_document()
    definitions = document.get("components", {}).get("schemas", {})
    result = {}
    for path, methods in document["paths"].items():
        action = path.removeprefix("/")
        method = methods["post"]
        request = method.get("requestBody", {}).get("content", {}).get("application/json", {})
        response = method.get("responses", {}).get("200", {}).get("content", {}).get("application/json", {})
        examples = request.get("examples", {})
        example = next((entry.get("value") for entry in examples.values() if "value" in entry), None)
        result[action] = {
            "description": method.get("summary") or method.get("description") or action,
            "payload": portable_schema(request.get("schema"), definitions),
            "response": portable_schema(response.get("schema"), definitions),
            "payloadExample": example, "tags": method.get("tags", []),
            "deprecated": method.get("deprecated", False),
            "schema_source": "NapCatDocs",
            "documented_version": manifest["version"],
            "runtime_supported": None,
        }
    return result


def with_runtime_schema(action, runtime_entry):
    _, document = bundled_document()
    base = reference_catalog().get(action, {})
    entry = {**base, **runtime_entry, "schema_source": "NapCat WebUI", "runtime_supported": True}
    definitions = document.get("components", {}).get("schemas", {})
    for key in ("payload", "response"):
        schema = runtime_entry.get(key)
        # Keep official parameters when the running action doesn't publish a schema.
        entry[key] = portable_schema(schema, definitions) if schema is not None else base.get(key, {
            "type": "object", "properties": {}, "x-napcat-schema-warning": "Runtime action publishes no schema",
        })
    return entry


def model_api_index(privileged=True, allowed=None):
    """All API names/descriptions reach the model; full parameter schemas are fetched on demand."""
    catalog = reference_catalog()
    names = sorted(catalog if privileged else set(catalog) & set(allowed or ()))
    manifest, _ = bundled_document()
    rows = [f"- {name}: {catalog[name]['description']}" for name in names]
    return (
        f"NapCatDocs {manifest['version']} API 索引：本身份可调用 {len(names)}/{len(catalog)} 个文档接口。\n"
        "这是文档目录，不能据此声称运行中的 NapCat 已支持或已执行接口。"
        "先用 napcat_catalog 查询运行状态，napcat_discover(action=接口名) 获取参数，"
        "再用 napcat_call(action=接口名, params={...}) 调用。"
        "运行时的新接口通过 napcat_discover() 发现。\n" + "\n".join(rows)
    )


def mcp_action_name(action):
    """MCP names must not contain dots; retain a stable, collision-free action mapping."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", action) and not action.startswith("encoded_"):
        return "napcat__"+action
    return "napcat__encoded_"+action.encode("utf-8").hex()


def action_tool_specs(catalog):
    names = set()
    result = []
    for action, entry in sorted(catalog.items()):
        name = mcp_action_name(action)
        if name in names or len(name) > 128:
            raise ValueError(f"Invalid or conflicting MCP action name for {action}")
        names.add(name)
        description = entry.get("description") or action
        if entry.get("runtime_supported") is not True:
            description += " [官方文档接口，运行时支持尚未确认]"
        payload = copy.deepcopy(entry.get("payload") or {"type": "object", "properties": {}})
        definitions = payload.pop("$defs", None)
        input_schema = {
            "type": "object", "properties": {
                "params": payload, "operation_id": {"type": "string"},
                "timeout": {"type": "number", "minimum": 1, "maximum": 120},
            }, "required": ["params"],
        }
        if definitions:
            input_schema["$defs"] = definitions
        result.append({
            "name": name, "description": description, "inputSchema": input_schema,
            "_meta": {"napcat_action": action, "schema_source": entry.get("schema_source", "unknown"),
                      "runtime_supported": entry.get("runtime_supported")},
        })
    return result
