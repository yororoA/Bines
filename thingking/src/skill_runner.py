"""Local SKILL.md catalog and constrained text-protocol dispatcher."""
from __future__ import annotations

import html
import inspect
import json
import ast
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping


CALL_START = "[[SKILL_CALL]]"
CALL_END = "[[/SKILL_CALL]]"

QQ_ALLOWED_ACTIONS = {
    "get_time", "send_qq_group_msg", "send_qq_private_msg",
    "napcat_call", "napcat_discover", "napcat_catalog", "napcat_send", "napcat_forward",
    "napcat_events", "update_status", "search_memory", "browser_search", "web_fetch",
}
QQ_GROUP_OWNER_ACTIONS = {"napcat_custom_faces", "napcat_send_custom_face"}
NAPCAT_ACTIONS = {
    "napcat_call", "napcat_discover", "napcat_catalog", "napcat_operation", "napcat_events",
    "napcat_send", "napcat_forward", "napcat_custom_faces", "napcat_send_custom_face",
}
QQ_OWNER_ACTIONS = QQ_ALLOWED_ACTIONS | QQ_GROUP_OWNER_ACTIONS | {
    "napcat_operation", "mcp_discover", "mcp_call",
    "get_qq_group_list", "get_qq_friend_list", "broadcast_to_all_friends",
    "broadcast_to_all_groups", "at_each_group_member",
    "get_ark_profile", "get_ark_conversations", "get_ark_chat_history", "send_ark_chat", "get_ark_operation",
    "get_moments", "add_moment", "comment_moment", "get_comments", "like_moment", "like_comment",
}


class SkillProtocolError(ValueError):
    pass


class SkillRunner:
    """Expose documented local skills and dispatch only registered Python actions."""

    def __init__(
        self,
        registry: Mapping[str, Any],
        *,
        skills_dir: Path,
        enabled_actions_path: Path | None = None,
        qq_allowed_actions: Iterable[str] = (),
    ):
        self.registry = dict(registry)
        self.skills_dir = Path(skills_dir)
        self.enabled_actions_path = Path(enabled_actions_path) if enabled_actions_path else None
        self.qq_allowed_actions = set(qq_allowed_actions)
        self.skills = self._load_skills()
        self.action_descriptions = self._load_action_descriptions()
        self.action_to_skill = {
            action: skill_name
            for skill_name, skill in self.skills.items()
            for action in skill["actions"]
        }

    def _load_skills(self) -> Dict[str, Dict[str, Any]]:
        skills: Dict[str, Dict[str, Any]] = {}
        if not self.skills_dir.is_dir():
            raise FileNotFoundError(f"Skills directory not found: {self.skills_dir}")
        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            content = path.read_text(encoding="utf-8")
            if not content.startswith("---\n"):
                raise ValueError(f"Missing SKILL.md frontmatter: {path}")
            _, frontmatter, body = content.split("---", 2)
            metadata: Dict[str, str] = {}
            for line in frontmatter.splitlines():
                key, sep, value = line.partition(":")
                if sep:
                    metadata[key.strip()] = value.strip()
            name = metadata.get("name")
            description = metadata.get("description")
            actions = [item.strip() for item in metadata.get("actions", "").split(",") if item.strip()]
            if not name or not description or not actions:
                raise ValueError(f"SKILL.md requires name, description, and actions: {path}")
            if name in skills:
                raise ValueError(f"Duplicate skill name '{name}' in {path}")
            unknown = set(actions) - self.registry.keys()
            if unknown:
                raise ValueError(f"Unknown actions in {path}: {', '.join(sorted(unknown))}")
            skills[name] = {
                "name": name,
                "description": description,
                "actions": actions,
                "instructions": body.strip(),
                "path": path,
            }
        if not skills:
            raise ValueError(f"No SKILL.md files found under {self.skills_dir}")
        action_owners: Dict[str, str] = {}
        for skill_name, skill in skills.items():
            for action in skill["actions"]:
                owner = action_owners.get(action)
                if owner is not None:
                    raise ValueError(
                        f"Action '{action}' is declared by both '{owner}' and '{skill_name}'"
                    )
                action_owners[action] = skill_name
        return skills

    def _load_action_descriptions(self) -> Dict[str, str]:
        if not self.enabled_actions_path or not self.enabled_actions_path.is_file():
            return {}
        try:
            rows = json.loads(self.enabled_actions_path.read_text(encoding="utf-8")).get("actions") or []
            return {
                str(row["name"]): str(row["description"])
                for row in rows
                if isinstance(row, dict) and row.get("name") and row.get("description")
            }
        except Exception:
            return {}

    def _enabled_actions(self) -> set[str]:
        if not self.enabled_actions_path or not self.enabled_actions_path.is_file():
            return set(self.registry)
        try:
            data = json.loads(self.enabled_actions_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("actions"), list):
                return set()
            rows = data["actions"]
            if any(
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or not isinstance(row.get("enabled"), bool)
                for row in rows
            ):
                return set()
            listed = {str(row.get("name")) for row in rows if isinstance(row, dict)}
            enabled = {str(row.get("name")) for row in rows if isinstance(row, dict) and row.get("enabled") is True}
            # Preserve the existing UI contract: newly registered actions default to enabled.
            return enabled | (set(self.registry) - listed)
        except Exception:
            # A malformed settings file must never re-enable every side-effecting action.
            return set()

    def _action_description(self, action: str) -> str:
        function = self.registry[action]
        # Read the source signature without importing desktop drivers.
        if hasattr(function, "module"):
            import ast
            path = self.skills_dir.parent / "tools" / f"{function.module}.py"
            if path.exists():
                tree = ast.parse(path.read_text(encoding="utf-8"))
                node = next((item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function.name), None)
                if node:
                    description = ast.get_docstring(node) or action
                    return f"{description}\nArguments: ({ast.unparse(node.args)})"
        doc = inspect.getdoc(function) or ""
        description = doc.splitlines()[0].strip() if doc else ""
        description = self.action_descriptions.get(action, description)
        try:
            signature = inspect.signature(function)
            arguments = []
            for name, parameter in signature.parameters.items():
                if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                    continue
                annotation = parameter.annotation
                kind = getattr(annotation, "__name__", "any") if annotation is not inspect.Parameter.empty else "any"
                required = parameter.default is inspect.Parameter.empty
                suffix = "required" if required else f"optional, default={parameter.default!r}"
                arguments.append(f"{name}: {kind} ({suffix})")
            argument_text = ", ".join(arguments) if arguments else "no arguments"
        except Exception:
            argument_text = "arguments depend on the action"
        return f"{description or action}\nArguments: {argument_text}"

    def actions_for_source(self, source: str | None = None, allowed_skills: Iterable[str] | None = None) -> set[str]:
        enabled = self._enabled_actions()
        if source in ("QQ", "qq"):
            from tools.context import CURRENT_CONTEXT
            context = CURRENT_CONTEXT.get()
            if not context.event_id:
                return set()  # Legacy QQ callers lack a trusted event context.
            if context.identity.privileged:
                enabled &= QQ_OWNER_ACTIONS
            elif context.identity.owner:
                enabled &= self.qq_allowed_actions | NAPCAT_ACTIONS
            else:
                enabled &= self.qq_allowed_actions
        if allowed_skills is not None:
            allowed_actions = {
                action
                for skill_name in allowed_skills
                for action in self.skills.get(skill_name, {}).get("actions", [])
            }
            enabled &= allowed_actions
        return enabled

    @staticmethod
    def _parameter_schema(annotation) -> Dict[str, Any]:
        """Render simple Python annotations without importing desktop modules."""
        if isinstance(annotation, ast.AST):
            annotation = ast.unparse(annotation)
        elif annotation is inspect.Parameter.empty:
            return {}
        else:
            annotation = getattr(annotation, "__name__", str(annotation))
        variants = str(annotation).replace("typing.", "").split("|")
        schemas = []
        types = {"str": "string", "int": "integer", "float": "number",
                 "bool": "boolean", "dict": "object", "list": "array",
                 "None": "null", "NoneType": "null"}
        for variant in variants:
            kind = types.get(variant.strip())
            if kind:
                schema = {"type": kind}
                if kind == "array":
                    schema["items"] = {}
                schemas.append(schema)
        if not schemas:
            return {}
        return schemas[0] if len(schemas) == 1 else {"anyOf": schemas}

    def function_tools(self, source: str | None = None, allowed_skills=None) -> List[Dict[str, Any]]:
        """Native function calling with source signatures and dynamic NapCat dispatch."""
        tools = []
        for action in sorted(self.actions_for_source(source, allowed_skills)):
            function = self.registry[action]
            properties, required = {}, []
            if hasattr(function, "module"):
                path = self.skills_dir.parent / "tools" / f"{function.module}.py"
                tree = ast.parse(path.read_text(encoding="utf-8"))
                node = next(item for item in tree.body
                            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function.name)
                arguments = [*node.args.posonlyargs, *node.args.args]
                optional_from = len(arguments) - len(node.args.defaults)
                for index, arg in enumerate(arguments):
                    properties[arg.arg] = self._parameter_schema(arg.annotation) if arg.annotation else {}
                    if index < optional_from:
                        required.append(arg.arg)
                for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                    properties[arg.arg] = self._parameter_schema(arg.annotation) if arg.annotation else {}
                    if default is None:
                        required.append(arg.arg)
            else:
                for name, parameter in inspect.signature(function).parameters.items():
                    if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
                        continue
                    properties[name] = self._parameter_schema(parameter.annotation)
                    if parameter.default is inspect.Parameter.empty:
                        required.append(name)
            tools.append({"type": "function", "function": {
                "name": action, "description": self._action_description(action),
                "parameters": {"type": "object", "properties": properties, "required": required},
            }})
        return tools

    def instructions_for_source(
        self,
        source: str | None = None,
        allowed_skills: Iterable[str] | None = None,
        *,
        native: bool = False,
    ) -> str:
        enabled = self.actions_for_source(source, allowed_skills)
        sections = []
        allowed_skill_set = set(allowed_skills) if allowed_skills is not None else None
        for name, skill in self.skills.items():
            if allowed_skill_set is not None and name not in allowed_skill_set:
                continue
            actions = [action for action in skill["actions"] if action in enabled]
            if not actions:
                continue
            sections.append(f"### {name}: {skill['description']}\n{skill['instructions']}")
            if name == "napcat" and "napcat_call" in enabled:
                from chatBot.catalog import model_api_index
                from chatBot.capabilities import SCOPED
                from tools.context import CURRENT_CONTEXT
                identity = CURRENT_CONTEXT.get().identity
                sections.append(model_api_index(identity.local or identity.owner, SCOPED))
            for action in actions:
                sections.append(f"Action `{action}` — {self._action_description(action)}")
        if native:
            return (
                "通过原生工具调用执行操作，可在一轮中调用多个工具，按返回结果继续组合接口。"
                "NapCat 原生接口统一使用 napcat_call，参数和消息段由你决定；"
                "需要参数说明时调用 napcat_discover，无需套用固定 action 模板。"
                "消息数量、分段、图文组合、引用、@及目标会话由你自主选择。"
                "已通过接口发送的回复不在最终文本重复；需要继续说话时可以继续发送。\n\n"
                "工具回执只报告本次操作结果，不代表对方发来了新消息，也不代表等待时间已经过去。"
                "本轮只回应已经收到的消息；想等待对方回复时直接结束，"
                "不要自行续演对方沉默、离开、正在忙或下一轮聊天。\n\n"
                + "\n\n".join(sections)
            )
        return (
            "本地技能通过纯文本协议调用。需要执行操作时，整条回复只能包含一个如下块，不能附加解释：\n"
            f"{CALL_START}\n"
            '{{"skill":"技能名","action":"动作名","args":{}}}\n'
            f"{CALL_END}\n"
            "args 必须是 JSON 对象。收到技能结果后，你可以继续调用技能；任务完成后用普通自然语言回答。\n\n"
            "当前可用的本地技能与动作：\n" + "\n\n".join(sections)
        )

    @staticmethod
    def parse_command(text: str) -> Dict[str, Any] | None:
        stripped = (text or "").strip()
        if len(stripped) > 32 * 1024:
            raise SkillProtocolError("技能调用超过 32 KiB 限制")
        if not stripped.startswith(CALL_START):
            return None
        if not stripped.endswith(CALL_END):
            raise SkillProtocolError("技能调用没有完整结束标记")
        payload = stripped[len(CALL_START):-len(CALL_END)].strip()
        try:
            command = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SkillProtocolError(f"技能调用 JSON 无效：{exc.msg}") from exc
        if not isinstance(command, dict):
            raise SkillProtocolError("技能调用必须是 JSON 对象")
        if set(command) != {"skill", "action", "args"}:
            raise SkillProtocolError("技能调用必须且只能包含 skill、action、args")
        if not isinstance(command["skill"], str) or not isinstance(command["action"], str):
            raise SkillProtocolError("skill 与 action 必须是字符串")
        if not isinstance(command["args"], dict):
            raise SkillProtocolError("args 必须是 JSON 对象")
        return command

    def parse_model_command(self, text: str) -> Dict[str, Any] | None:
        """Accept the project protocol and DeepSeek's equivalent DSML text fallback."""
        command = self.parse_command(text)
        if command is not None:
            return command
        normalized = (text or "").strip().replace("｜", "|")
        wrapper = re.fullmatch(
            r"<\|\|DSML\|\|\s+calls>\s*(.*?)\s*</\|\|DSML\|\|\s+calls>",
            normalized,
            re.DOTALL,
        )
        if not wrapper:
            return None
        invoke = re.fullmatch(
            r"<\|\|DSML\|\|\s+invoke\s+name=\"([^\"]+)\">\s*(.*?)\s*"
            r"</\|\|DSML\|\|\s+invoke>",
            wrapper.group(1),
            re.DOTALL,
        )
        if not invoke:
            raise SkillProtocolError("DSML 必须且只能包含一个完整 invoke")
        action = html.unescape(invoke.group(1))
        skill = self.action_to_skill.get(action)
        if not skill:
            raise SkillProtocolError(f"DSML 使用了未知动作 {action!r}")
        args: Dict[str, Any] = {}
        body = invoke.group(2)
        parameter_pattern = re.compile(
            r"<\|\|DSML\|\|\s+parameter\s+([^>]*)>(.*?)"
            r"</\|\|DSML\|\|\s+parameter>",
            re.DOTALL,
        )
        matches = list(parameter_pattern.finditer(body))
        if re.sub(parameter_pattern, "", body).strip():
            raise SkillProtocolError("DSML invoke 中包含无法识别的内容")
        for match in matches:
            attributes = dict(re.findall(r"([A-Za-z_][\w-]*)=\"([^\"]*)\"", match.group(1)))
            name = attributes.get("name")
            if not name or name in args:
                raise SkillProtocolError("DSML parameter 缺少唯一 name")
            value = html.unescape(match.group(2).strip())
            if attributes.get("string", "").lower() == "true":
                args[name] = value
            else:
                try:
                    args[name] = json.loads(value)
                except json.JSONDecodeError:
                    args[name] = value
        return {"skill": skill, "action": action, "args": args}

    def execute(
        self,
        command: Dict[str, Any],
        *,
        source: str | None = None,
        allowed_skills: Iterable[str] | None = None,
    ) -> str:
        skill_name = command.get("skill")
        action = command.get("action")
        args = command.get("args")
        if skill_name not in self.skills:
            return f"技能执行失败：未知技能 {skill_name!r}。"
        if self.action_to_skill.get(action) != skill_name:
            return f"技能执行失败：动作 {action!r} 不属于技能 {skill_name!r}。"
        if action not in self.actions_for_source(source, allowed_skills):
            return f"技能执行失败：动作 {action!r} 未启用或当前消息来源无权调用。"
        if not isinstance(args, dict):
            return "技能执行失败：args 必须是对象。"
        function = self.registry.get(action)
        if function is None:
            return f"技能执行失败：Python 动作 {action!r} 不存在。"
        try:
            target = function.resolve() if hasattr(function, "resolve") else function
            inspect.signature(target).bind(**args)
            result = function(**args)
            return str(result)
        except TypeError as exc:
            return f"技能执行失败：参数不匹配：{exc}"
        except Exception as exc:
            return f"技能执行失败：{type(exc).__name__}: {exc}"
