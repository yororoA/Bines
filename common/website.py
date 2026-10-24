"""Website interaction with durable receipts and owner-granted social authority."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from typing import Callable
from urllib.parse import parse_qsl, urljoin, urlsplit


MAX_CONTENT = 20_000
MAX_TITLE = 200
MAX_STEPS = 30
MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
    ".mp4", ".mov", ".m4v", ".webm",
}
CHALLENGE_PATTERN = re.compile(
    r"验证码|人机验证|安全验证|扫码登录|请先登录|登录后(?:即可|才能)|滑块验证|captcha|verify you are human",
    re.IGNORECASE,
)
PROTECTED_INTERACTION = re.compile(
    r"密码|验证码|登录|登陆|注册|扫码|二次验证|password|captcha|sign.in|log.in|verification", re.I,
)
SENSITIVE_INTERACTION = re.compile(r"支付|付款|购买|删除|注销|转账|退款|银行卡|pay|checkout|delete.account", re.I)


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    entry_url: str
    publish_urls: dict[str, str]
    domains: tuple[str, ...]
    commit_names: dict[str, tuple[str, ...]]
    navigation_names: tuple[str, ...]


PLATFORMS = {
    "qzone": Platform(
        key="qzone", label="QQ空间", entry_url="https://qzone.qq.com/",
        publish_urls={kind: "https://qzone.qq.com/" for kind in ("post", "comment", "video")},
        domains=("qq.com", "*.qq.com", "qpic.cn", "*.qpic.cn", "gtimg.cn", "*.gtimg.cn",
                 "gtimg.com", "*.gtimg.com"),
        commit_names={"post": ("发表", "发布"), "comment": ("评论", "回复", "发表"),
                      "video": ("发表", "发布")},
        navigation_names=("说说", "发表", "评论", "相册", "视频"),
    ),
    "xiaoheihe": Platform(
        key="xiaoheihe",
        label="小黑盒",
        entry_url="https://www.xiaoheihe.cn/",
        publish_urls={
            "post": "https://www.xiaoheihe.cn/",
            "comment": "https://www.xiaoheihe.cn/",
            "video": "https://www.xiaoheihe.cn/",
        },
        domains=(
            "xiaoheihe.cn", "*.xiaoheihe.cn", "heybox.hk", "*.heybox.hk",
            "xhcdn.com", "*.xhcdn.com",
        ),
        commit_names={
            "post": ("发布", "发帖", "立即发布"),
            "comment": ("发布", "评论", "回复", "发送"),
            "video": ("发布", "投稿", "立即发布"),
        },
        navigation_names=("发帖", "写评论", "评论", "投稿", "上传视频", "下一步", "我知道了"),
    ),
    "douyin": Platform(
        key="douyin",
        label="抖音",
        entry_url="https://www.douyin.com/",
        publish_urls={
            "post": "https://creator.douyin.com/creator-micro/content/publish",
            "comment": "https://www.douyin.com/",
            "video": "https://creator.douyin.com/creator-micro/content/upload",
        },
        domains=(
            "douyin.com", "*.douyin.com", "douyinvod.com", "*.douyinvod.com",
            "douyinpic.com", "*.douyinpic.com", "byteimg.com", "*.byteimg.com",
            "snssdk.com", "*.snssdk.com", "bytedance.com", "*.bytedance.com",
            "bytegoofy.com", "*.bytegoofy.com",
        ),
        commit_names={
            "post": ("发布", "发布图文", "发布作品", "立即发布"),
            "comment": ("发送", "发布", "评论"),
            "video": ("发布", "立即发布"),
        },
        navigation_names=("发布作品", "发布图文", "上传视频", "评论", "下一步", "我知道了"),
    ),
    "bilibili": Platform(
        key="bilibili",
        label="哔哩哔哩",
        entry_url="https://www.bilibili.com/",
        publish_urls={
            "post": "https://t.bilibili.com/",
            "comment": "https://www.bilibili.com/",
            "video": "https://member.bilibili.com/platform/upload/video/frame",
        },
        domains=(
            "bilibili.com", "*.bilibili.com", "biliapi.net", "*.biliapi.net",
            "hdslb.com", "*.hdslb.com", "bilivideo.com", "*.bilivideo.com",
            "biliimg.com", "*.biliimg.com",
        ),
        commit_names={
            "post": ("发布", "立即发布"),
            "comment": ("发布", "发表评论", "评论"),
            "video": ("立即投稿", "投稿"),
        },
        navigation_names=("投稿", "发布动态", "上传视频", "评论", "下一步", "我知道了"),
    ),
}


def platform_config(value: str) -> Platform:
    key = str(value or "").strip().lower()
    aliases = {"小黑盒": "xiaoheihe", "抖音": "douyin", "哔哩哔哩": "bilibili", "b站": "bilibili", "QQ空间": "qzone", "qq空间": "qzone", "空间": "qzone"}
    key = aliases.get(key, key)
    if key not in PLATFORMS:
        raise ValueError("platform 仅支持 xiaoheihe、douyin、bilibili、qzone")
    return PLATFORMS[key]


def _domain_matches(host: str, rule: str) -> bool:
    host, rule = host.lower().rstrip("."), rule.lower().rstrip(".")
    suffix = rule.removeprefix("*.")
    return host == suffix or (rule.startswith("*.") and host.endswith("." + suffix))


def validate_url(platform: Platform, value: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("网站地址必须是无内嵌凭据的 HTTPS URL")
    if not any(_domain_matches(parsed.hostname, rule) for rule in platform.domains):
        raise ValueError(f"地址不属于{platform.label}允许域名")
    return url


def navigation_identity(url):
    parsed = urlsplit(str(url or ""))
    tracking = {"vt", "spm_id_from", "vd_source", "from_spmid", "spmid", "share_source"}
    query = sorted((key, value) for key, value in parse_qsl(parsed.query)
                   if key not in tracking and not key.startswith("utm_"))
    return parsed.hostname, parsed.path.rstrip("/"), query, parsed.fragment


def _default_steps(platform: Platform, action: str, title: str, content: str, media: list[str], target_url: str):
    open_url = target_url or platform.publish_urls[action]
    steps = [{"op": "open", "url": open_url}, {"op": "snapshot"}]
    if action == "video":
        if not media:
            raise ValueError("视频投稿至少需要一个媒体文件")
        steps.append({"op": "upload", "selector": "input[type=file]", "media_index": 0})
        steps.append({"op": "wait", "milliseconds": 1500})
        if title:
            steps.append({
                "op": "fill", "target": {"by": "label", "value": "标题"}, "field": "title",
            })
        if content:
            steps.append({
                "op": "fill", "target": {"by": "placeholder", "value": "简介"}, "field": "content",
            })
    elif action == "comment":
        placeholder = {
            "bilibili": "发一条友善的评论",
            "douyin": "留下你的精彩评论吧",
            "xiaoheihe": "写下你的评论",
            "qzone": "评论",
        }[platform.key]
        steps.append({
            "op": "fill", "target": {"by": "placeholder", "value": placeholder}, "field": "content",
        })
    else:
        placeholder = {
            "bilibili": "有什么想和大家分享的？",
            "douyin": "添加作品简介",
            "xiaoheihe": "分享你的想法",
            "qzone": "说点什么吧",
        }[platform.key]
        if title:
            steps.append({
                "op": "fill", "target": {"by": "label", "value": "标题"}, "field": "title",
            })
        steps.append({
            "op": "fill", "target": {"by": "placeholder", "value": placeholder}, "field": "content",
        })
        if media:
            steps.append({"op": "upload", "selector": "input[type=file]", "media_index": 0})
    steps.extend([
        {
            "op": "click",
            "target": {"by": "role", "role": "button", "name": platform.commit_names[action][0]},
            "commit": True,
        },
        {"op": "wait", "milliseconds": 1500},
        {"op": "snapshot"},
    ])
    return steps


def _clean_target(target, *, click=False):
    if not isinstance(target, dict):
        raise ValueError("步骤 target 必须是对象")
    by = str(target.get("by") or "").strip()
    if by == "role":
        allowed = {"by", "role", "name"}
        role, name = str(target.get("role") or ""), str(target.get("name") or "")
        if set(target) - allowed or role not in {"button", "link", "textbox"} or not name:
            raise ValueError("role target 需要合法的 role 与 name")
        return {"by": by, "role": role[:40], "name": name[:120]}
    if by in {"label", "placeholder", "text"}:
        if set(target) - {"by", "value"} or not str(target.get("value") or "").strip():
            raise ValueError(f"{by} target 需要 value")
        if click and by not in {"text"}:
            raise ValueError("点击仅允许 role 或 text 定位")
        return {"by": by, "value": str(target["value"]).strip()[:120]}
    if by == "selector" and not click:
        value = str(target.get("value") or "").strip()
        if set(target) - {"by", "value"} or not value or len(value) > 200:
            raise ValueError("selector target 无效")
        return {"by": by, "value": value}
    raise ValueError("不支持的 target 定位方式")


def normalize_steps(
    platform: Platform,
    action: str,
    title: str,
    content: str,
    media: list[str],
    target_url: str,
    steps,
):
    rows = _default_steps(platform, action, title, content, media, target_url) if steps is None else steps
    if not isinstance(rows, list) or not rows or len(rows) > MAX_STEPS:
        raise ValueError(f"steps 必须是 1–{MAX_STEPS} 个步骤的数组")
    result, commits, committed = [], 0, False
    allowed_names = set(platform.navigation_names)
    allowed_commits = set(platform.commit_names[action])
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("每个步骤必须是对象")
        op = str(raw.get("op") or "").strip()
        if committed and op not in {"wait", "snapshot", "verify_text"}:
            raise ValueError("提交步骤之后只允许等待和验证")
        if op == "open":
            if set(raw) != {"op", "url"}:
                raise ValueError("open 步骤只接受 url")
            row = {"op": op, "url": validate_url(platform, raw["url"])}
        elif op == "snapshot":
            if set(raw) != {"op"}:
                raise ValueError("snapshot 步骤不接受其他字段")
            row = {"op": op}
        elif op == "wait":
            if set(raw) != {"op", "milliseconds"}:
                raise ValueError("wait 步骤只接受 milliseconds")
            milliseconds = int(raw["milliseconds"])
            if not 100 <= milliseconds <= 10_000:
                raise ValueError("等待时间必须为 100–10000 毫秒")
            row = {"op": op, "milliseconds": milliseconds}
        elif op == "fill":
            if set(raw) != {"op", "target", "field"} or raw.get("field") not in {"title", "content"}:
                raise ValueError("fill 步骤只能引用 title 或 content")
            if raw["field"] == "title" and not title:
                raise ValueError("计划没有标题，不能填充 title")
            if raw["field"] == "content" and not content:
                raise ValueError("计划没有正文，不能填充 content")
            row = {"op": op, "target": _clean_target(raw["target"]), "field": raw["field"]}
        elif op == "select":
            if set(raw) != {"op", "target", "value"}:
                raise ValueError("select 步骤需要 target 与 value")
            value = str(raw["value"])
            if not value or len(value) > 120:
                raise ValueError("select value 无效")
            row = {"op": op, "target": _clean_target(raw["target"]), "value": value}
        elif op == "upload":
            if set(raw) != {"op", "selector", "media_index"}:
                raise ValueError("upload 步骤需要 selector 与 media_index")
            selector = str(raw["selector"]).replace('"', "'").replace(" ", "")
            if selector not in {"input[type=file]", "input[type='file']"}:
                raise ValueError("上传仅允许文件输入框")
            index = int(raw["media_index"])
            if index < 0 or index >= len(media):
                raise ValueError("media_index 超出媒体文件范围")
            row = {"op": op, "selector": "input[type=file]", "media_index": index}
        elif op == "click":
            if set(raw) != {"op", "target", "commit"} or not isinstance(raw["commit"], bool):
                raise ValueError("click 步骤必须显式声明 commit")
            target = _clean_target(raw["target"], click=True)
            name = target.get("name") or target.get("value")
            if raw["commit"]:
                if name not in allowed_commits:
                    raise ValueError(f"提交按钮仅允许：{', '.join(sorted(allowed_commits))}")
                commits += 1
                committed = True
            elif name not in allowed_names:
                raise ValueError(f"非提交点击仅允许：{', '.join(sorted(allowed_names))}")
            row = {"op": op, "target": target, "commit": raw["commit"]}
        elif op == "verify_text":
            if set(raw) != {"op", "text"}:
                raise ValueError("verify_text 步骤只接受 text")
            text = str(raw["text"]).strip()
            if not text or len(text) > 120:
                raise ValueError("验证文本无效")
            row = {"op": op, "text": text}
        else:
            raise ValueError(f"不支持的步骤：{op}")
        result.append(row)
    if commits != 1:
        raise ValueError("每个公开写入计划必须且只能有一个提交步骤")
    return result


class PlanStore:
    """SQLite outbox. An interrupted commit is uncertain and is never replayed."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=20000")
        return db

    def _initialize(self):
        with self.lock, self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS website_plans (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    platform TEXT NOT NULL,
                    action TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    media_json TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    next_step INTEGER NOT NULL DEFAULT 0,
                    approved_at REAL,
                    started_at REAL,
                    completed_at REAL,
                    receipt_json TEXT,
                    error TEXT
                )
                """
            )
            db.execute(
                """
                UPDATE website_plans
                SET status='uncertain', updated_at=?, error='进程在执行中中断；禁止自动重放，请人工核查网站。'
                WHERE status='executing'
                """,
                (time.time(),),
            )
            db.execute("""CREATE TABLE IF NOT EXISTS website_actions (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, created REAL NOT NULL)""")
            db.execute("UPDATE website_actions SET status='uncertain' WHERE status='started'")

    def begin_action(self, action_id, payload):
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM website_actions WHERE id=?", (action_id,)).fetchone()
            if row:
                if row["payload"] != encoded:
                    raise ValueError("operation_id 已绑定不同操作")
                return False, (json.loads(row["result"]) if row["result"] else
                               {"status": "uncertain", "operation_id": action_id})
            db.execute("INSERT INTO website_actions VALUES(?,?,'started',NULL,?)",
                       (action_id, encoded, time.time()))
            return True, None

    def action_result(self, action_id, payload):
        with self.lock, self._connect() as db:
            row = db.execute("SELECT * FROM website_actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            return None
        if row["payload"] != json.dumps(payload, sort_keys=True, ensure_ascii=False):
            raise ValueError("operation_id 已绑定不同操作")
        return (json.loads(row["result"]) if row["result"] else
                {"status": "uncertain", "operation_id": action_id, "retry_allowed": False})

    def finish_action(self, action_id, result):
        with self.lock, self._connect() as db:
            db.execute("UPDATE website_actions SET status=?,result=? WHERE id=?",
                       (result["status"], json.dumps(result, ensure_ascii=False), action_id))
        return result

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        result["media"] = json.loads(result.pop("media_json"))
        result["steps"] = json.loads(result.pop("steps_json"))
        result["receipt"] = json.loads(result.pop("receipt_json")) if result.get("receipt_json") else None
        return result

    def create(self, payload):
        now = time.time()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.lock, self._connect() as db:
            previous = db.execute(
                "SELECT id FROM website_plans WHERE payload_hash=? AND created_at>? ORDER BY created_at DESC LIMIT 1",
                (digest, now - 86400),
            ).fetchone()
        if previous:
            return self.get(previous["id"])
        for _ in range(10):
            plan_id = os.urandom(5).hex().upper()
            try:
                with self.lock, self._connect() as db:
                    db.execute(
                        """
                        INSERT INTO website_plans
                        (id,created_at,updated_at,platform,action,title,content,target_url,
                         media_json,steps_json,payload_hash,status)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending_approval')
                        """,
                        (
                            plan_id, now, now, payload["platform"], payload["action"],
                            payload["title"], payload["content"], payload["target_url"],
                            json.dumps(payload["media"], ensure_ascii=False),
                            json.dumps(payload["steps"], ensure_ascii=False),
                            digest,
                        ),
                    )
                return self.get(plan_id)
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("无法生成唯一计划编号")

    def get(self, plan_id: str):
        with self.lock, self._connect() as db:
            return self._decode(db.execute(
                "SELECT * FROM website_plans WHERE id=?", (str(plan_id).upper(),),
            ).fetchone())

    def recent(self, limit=20):
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM website_plans ORDER BY created_at DESC LIMIT ?", (int(limit),),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def approve(self, plan_id: str):
        plan_id = str(plan_id).upper()
        with self.lock, self._connect() as db:
            row = db.execute("SELECT status FROM website_plans WHERE id=?", (plan_id,)).fetchone()
            if not row:
                raise ValueError("计划不存在")
            if row["status"] == "pending_approval":
                now = time.time()
                db.execute(
                    "UPDATE website_plans SET status='approved',approved_at=?,updated_at=? WHERE id=?",
                    (now, now, plan_id),
                )
            elif row["status"] not in {"approved", "blocked_human"}:
                raise ValueError(f"计划状态 {row['status']} 不能批准")
        return self.get(plan_id)

    def begin(self, plan_id: str):
        plan_id = str(plan_id).upper()
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM website_plans WHERE id=?", (plan_id,)).fetchone()
            if not row:
                raise ValueError("计划不存在")
            if row["status"] == "completed":
                db.commit()
                return self.get(plan_id)
            if row["status"] not in {"approved", "blocked_human"}:
                db.rollback()
                raise ValueError(f"计划状态 {row['status']} 不能执行")
            now = time.time()
            db.execute(
                """
                UPDATE website_plans
                SET status='executing',started_at=COALESCE(started_at,?),updated_at=?,error=NULL
                WHERE id=?
                """,
                (now, now, plan_id),
            )
            db.commit()
        return self.get(plan_id)

    def advance(self, plan_id: str, next_step: int):
        with self.lock, self._connect() as db:
            db.execute(
                "UPDATE website_plans SET next_step=?,updated_at=? WHERE id=?",
                (next_step, time.time(), str(plan_id).upper()),
            )

    def finish(self, plan_id: str, status: str, *, receipt=None, error=None):
        if status not in {"completed", "failed", "blocked_human", "uncertain"}:
            raise ValueError("无效的计划终态")
        now = time.time()
        completed = now if status == "completed" else None
        with self.lock, self._connect() as db:
            db.execute(
                """
                UPDATE website_plans
                SET status=?,updated_at=?,completed_at=?,receipt_json=?,error=?
                WHERE id=?
                """,
                (
                    status, now, completed,
                    json.dumps(receipt, ensure_ascii=False) if receipt is not None else None,
                    str(error)[:1000] if error else None,
                    str(plan_id).upper(),
                ),
            )
        return self.get(plan_id)


class BrowserCommandError(RuntimeError):
    def __init__(self, message, *, timeout=False):
        super().__init__(message)
        self.timeout = timeout


class AgentBrowser:
    """Small argv-only wrapper; no page text is interpreted as a command."""

    def __init__(
        self,
        command: list[str],
        *,
        profile_root: Path,
        executable_path: str = "",
        headed: bool = True,
        runner: Callable | None = None,
    ):
        self.command = list(command)
        self.profile_root = Path(profile_root)
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self.executable_path = executable_path
        self.headed = headed
        self.runner = runner or subprocess.run
        self.lock = threading.RLock()
        self.runtime_failures = {}
        from common.website_cdp import ExistingPageReader
        self.page_reader = ExistingPageReader(self.profile_root)

    def read_existing(self, platform, args, timeout=10):
        with self.lock:
            return self.page_reader.run(platform, list(args), timeout=timeout)

    def capture_video(self, platform, index, path, timeout=4):
        with self.lock:
            return self.page_reader.capture_video(platform, index, path, timeout=timeout)

    def failure(self, platform, stage, reason):
        self.runtime_failures[platform.key] = {
            "stage": stage, "reason": reason, "failed_at": time.time(),
        }
        logging.getLogger("bines.website.runtime").warning(
            "browser platform=%s stage=%s reason=%s; automatic relaunch disabled",
            platform.key, stage, reason,
        )

    def existing_platforms(self):
        result = []
        for config in PLATFORMS.values():
            try:
                if self.page_reader._pages(config.key):
                    result.append(config.key)
            except Exception:
                continue
        return result

    def _argv(self, platform: Platform, args):
        profile = self.profile_root / platform.key
        profile.mkdir(parents=True, exist_ok=True)
        argv = [
            *self.command,
            "--session", f"bines-{platform.key}",
            "--profile", str(profile),
            "--content-boundaries",
            "--max-output", "50000",
            "--allowed-domains", ",".join(platform.domains),
        ]
        if self.headed:
            argv.append("--headed")
        if self.executable_path:
            argv.extend(["--executable-path", self.executable_path])
        return [*argv, *map(str, args)]

    def run(self, platform: Platform, args, timeout=60):
        argv = self._argv(platform, args)
        env = dict(os.environ)
        env["AGENT_BROWSER_DEBUG"] = "1"
        # Let the browser report its own timeout before the CLI is terminated.
        env["AGENT_BROWSER_DEFAULT_TIMEOUT"] = str(min(25_000, max(1000, int((timeout - 3) * 1000))))
        try:
            with self.lock:
                if platform.key in self.runtime_failures:
                    raise BrowserCommandError("浏览器启动失败后已暂停自动重试，请修复运行环境后重启浏览器桥")
                result = self.runner(
                    argv, capture_output=True, text=True, timeout=timeout,
                    env=env,
                )
        except subprocess.TimeoutExpired as exc:
            self.failure(platform, str(args[0]), "command_timeout")
            raise BrowserCommandError("浏览器命令超时，写入结果可能未知", timeout=True) from exc
        except OSError as exc:
            raise BrowserCommandError(f"无法启动浏览器运行时：{exc}") from exc
        if result.returncode:
            output = ((result.stdout or "") + ("\n" + result.stderr if result.stderr else "")).strip()
            if re.search(r"Chrome exited|DevToolsActivePort|browser.*launch|EAGAIN|daemon.*(?:failed|timeout)", output, re.I):
                self.failure(platform, str(args[0]), "launch_or_connection_failed")
            raise BrowserCommandError(output[-1500:] or f"浏览器命令退出码 {result.returncode}")
        return (result.stdout or "").strip()[:50_000]

    def _active_bines_sessions(self):
        try:
            result = self.runner(
                [*self.command, "session", "list"],
                capture_output=True,
                text=True,
                timeout=10,
                env=dict(os.environ),
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        names = {f"bines-{platform.key}" for platform in PLATFORMS.values()}
        return {line.strip() for line in result.stdout.splitlines() if line.strip() in names}

    def close(self):
        active = self._active_bines_sessions()
        for platform in PLATFORMS.values():
            if f"bines-{platform.key}" not in active:
                continue
            try:
                self.run(platform, ["close"], timeout=10)
            except Exception:
                pass
        deadline = time.monotonic() + 5
        while active and time.monotonic() < deadline:
            active = self._active_bines_sessions()
            if active:
                time.sleep(0.1)


def _locator_args(target, action, value=None):
    by = target["by"]
    if by == "selector":
        args = [action, target["value"]]
        if value is not None:
            args.append(value)
        return args
    if by == "role":
        args = ["find", "role", target["role"], action, "--name", target["name"]]
    else:
        args = ["find", by, target["value"], action]
    if value is not None:
        args.append(value)
    return args


class WebsiteService:
    def __init__(self, store: PlanStore, browser: AgentBrowser, *, root: Path, media_roots=None):
        self.store, self.browser = store, browser
        self.root = Path(root).resolve()
        roots = media_roots or [
            self.root / "static",
            self.root / "deploy" / "napcat" / "data" / "media",
        ]
        extra = [Path(item).expanduser() for item in os.environ.get(
            "BINES_WEBSITE_MEDIA_DIRS", "",
        ).split(os.pathsep) if item.strip()]
        self.media_roots = [Path(path).resolve() for path in [*roots, *extra]]
        self.execute_lock = threading.RLock()
        self.observed_refs = {}
        self.observed_pages = {}
        self.observer = None
        self.paused = os.getenv("BINES_WEBSITE_PAUSED", "false").lower() in {"1", "true", "yes"}
        self.load_wait_seconds = 6.0
        self.watch_lock = threading.Lock()

    def _media(self, paths):
        if paths is None:
            return []
        if not isinstance(paths, list) or len(paths) > 20:
            raise ValueError("media_paths 必须是最多 20 个路径的数组")
        result = []
        for value in paths:
            raw = Path(str(value)).expanduser()
            raw_text = raw.as_posix()
            if raw_text.startswith("/bines-media/"):
                raw = self.root / "deploy" / "napcat" / "data" / "media" / raw_text.removeprefix("/bines-media/")
            elif raw_text.startswith("/app/"):
                raw = self.root / raw_text.removeprefix("/app/")
            resolved = (raw if raw.is_absolute() else self.root / raw).resolve()
            if not any(resolved == base or base in resolved.parents for base in self.media_roots):
                raise ValueError("媒体文件必须位于 static、NapCat media 或 BINES_WEBSITE_MEDIA_DIRS")
            if resolved.suffix.lower() not in MEDIA_EXTENSIONS:
                raise ValueError("媒体文件类型仅支持常见图片和视频")
            if not resolved.is_file():
                raise ValueError(f"媒体文件不存在：{raw.name}")
            result.append(str(resolved))
        return result

    @staticmethod
    def public_plan(plan):
        if not plan:
            return None
        return {
            key: value for key, value in plan.items()
            if key not in {"media", "payload_hash", "started_at", "receipt_json"}
        } | {
            "media": [Path(path).name for path in plan["media"]],
            "content_sha256": hashlib.sha256(plan["content"].encode()).hexdigest(),
        }

    def status(self, platform=""):
        selected = platform_config(platform).key if platform else ""
        plans = self.store.recent(20)
        if selected:
            plans = [plan for plan in plans if plan["platform"] == selected]
        failures = getattr(self.browser, "runtime_failures", {})
        failed = failures.get(selected) if selected else bool(failures)
        return {
            "status": "ok",
            "runtime": "paused" if self.paused else "degraded" if failed else "ready",
            "browser_failures": {key: value for key, value in failures.items() if not selected or key == selected},
            "bridge_ready": True,
            "platforms": [
                {"key": row.key, "label": row.label, "entry_url": row.entry_url}
                for row in PLATFORMS.values()
            ],
            "plans": [self.public_plan(plan) for plan in plans],
        }

    def page_state(self, config):
        """Observe the viewport; a successful scroll command alone proves no movement."""
        from common.website_cdp import ExistingBrowserUnavailable
        script = """JSON.stringify({url:location.href,title:document.title,
            document_id:performance.timeOrigin,ready_state:document.readyState,
            body_text_length:document.body?.innerText?.trim().length||0,
            loading_visible:Array.from(document.querySelectorAll(
                '[aria-busy="true"],[role="progressbar"],[class*="loading"],[class*="skeleton"]'))
                .some(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                    return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}),
            scroll_x:window.scrollX,scroll_y:window.scrollY,viewport_height:innerHeight,
            document_height:document.scrollingElement?.scrollHeight,
            scrollers:Array.from(document.querySelectorAll('*')).filter(
                e=>e.scrollTop>0&&e.scrollHeight>e.clientHeight).slice(0,5).map(
                e=>({tag:e.tagName,scroll_y:e.scrollTop,height:e.clientHeight})),
            visible_links:Array.from(document.querySelectorAll('a[href]')).filter(
                a=>{const r=a.getBoundingClientRect();return r.bottom>0&&r.top<innerHeight&&
                    r.width>0&&r.height>0&&a.innerText.trim()}).slice(0,12).map(
                a=>({text:a.innerText.trim().slice(0,100),url:a.href}))})"""
        try:
            output = self.read_browser(config, ["eval", script], timeout=5).strip()
            if output.startswith("--- AGENT_BROWSER_PAGE_CONTENT "):
                output = "\n".join(output.splitlines()[1:-1])
            state = json.loads(output)
            return json.loads(state) if isinstance(state, str) else state
        except (BrowserCommandError, ExistingBrowserUnavailable, TypeError, ValueError) as exc:
            return {"observation_error": str(exc)[:300]}

    def _snapshot_ready(self, config, interactive=True):
        """Bounded DOM settling; network-idle is unsuitable for live/video pages."""
        args = ["snapshot", "-c", *(["-i"] if interactive else [])]
        deadline = time.monotonic() + self.load_wait_seconds
        attempts = 0
        while True:
            tree = self.browser.run(config, args)
            state = self.page_state(config)
            refs = bool(re.search(r"\[ref=e\d+\]", tree))
            empty_tree = not tree.strip() or "(no interactive elements)" in tree.lower()
            loading = state.get("ready_state") in {"loading", "interactive"} or state.get("loading_visible")
            # Empty asynchronous shells can report complete before hydration.
            shell = empty_tree and state.get("body_text_length", 100) < 40
            wait = (loading or shell or empty_tree) and not CHALLENGE_PATTERN.search(tree)
            if not wait or time.monotonic() >= deadline:
                state.update(load_status="loading" if loading or shell else "ready",
                             interactive_elements=bool(refs), load_checks=attempts + 1)
                if state["load_status"] == "loading":
                    state["loading_note"] = "页面仍在加载；空快照不代表没有内容，可稍后重新观察"
                return tree, state
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            attempts += 1

    def _remember(self, config, tree, state):
        self.observed_refs[config.key] = {
            match.group(1): line for line in tree.splitlines()
            if (match := re.search(r"\[ref=(e\d+)\]", line))
        }
        self.observed_pages[config.key] = (state.get("url"), state.get("document_id"))

    def _observe_automatically(self, config, *, invalidate=True):
        if self.observer:
            self.observer.activate(config.key, invalidate=invalidate)

    def read_browser(self, config, args, timeout=10):
        """Background reads never enter the CLI's automatic browser launch path."""
        reader = getattr(self.browser, "read_existing", None)
        if reader is not None:
            return reader(config, args, timeout=timeout)
        from common.website_cdp import ExistingBrowserUnavailable
        raise ExistingBrowserUnavailable("浏览器不支持只读连接；后台不会启动窗口")

    def capture_video_frame(self, config, state, path):
        from common.website_cdp import VideoPixelsUnavailable
        capture = getattr(self.browser, "capture_video", None)
        if capture is not None:
            try:
                media_time = capture(config, state["video"]["index"], path, timeout=4)
                if isinstance(media_time, (float, int)):
                    state["video"]["current_time"] = media_time
                return "video_pixels"
            except VideoPixelsUnavailable:
                pass  # Cross-origin/DRM video keeps the visible-window fallback.
        self.read_browser(config, ["screenshot", str(path)], timeout=5)
        return "browser_viewport"

    def open(self, platform, url="", snapshot=True):
        config = platform_config(platform)
        selected = validate_url(config, url) if url else config.entry_url
        output = self.browser.run(config, ["open", selected])
        current = self.browser.run(config, ["get", "url"])
        tree, state = self._snapshot_ready(config) if snapshot else ("", self.page_state(config))
        self._remember(config, tree, state)
        self._observe_automatically(config)
        return {
            "status": "ok", "platform": config.key, "url": current or selected,
            "output": output[-2000:], "snapshot": tree,
            "page_state": state, "automatic_observation": self.observer is not None,
            "human_action_required": bool(CHALLENGE_PATTERN.search(tree + "\n" + output)),
        }

    def snapshot(self, platform, interactive=True):
        config = platform_config(platform)
        output, state = self._snapshot_ready(config, interactive)
        self._remember(config, output, state)
        self._observe_automatically(config, invalidate=False)
        return {
            "status": "ok", "platform": config.key, "snapshot": output,
            "page_state": state,
            "human_action_required": bool(CHALLENGE_PATTERN.search(output)),
        }

    def scroll(self, platform, direction="down", pixels=600):
        config = platform_config(platform)
        if direction not in {"up", "down", "left", "right"}:
            raise ValueError("direction 仅支持 up/down/left/right")
        pixels = int(pixels)
        if not 1 <= pixels <= 5000:
            raise ValueError("pixels 必须为 1–5000")
        before = self.page_state(config)
        output = self.browser.run(config, ["scroll", direction, pixels])
        self._observe_automatically(config)
        return {"status": "ok", "platform": config.key, "output": output[-2000:],
                "page_state_before": before, "page_state": self.page_state(config)}

    def tabs(self, platform):
        config = platform_config(platform)
        return {"status": "ok", "platform": config.key,
                "tabs": self.browser.run(config, ["tab", "list"])[:10000]}

    def watch(self, platform, question="", duration_seconds=6, frames=3):
        """Read a short sequence of visible video frames and captions."""
        from common.website_watch import watch
        if not self.watch_lock.acquire(blocking=False):
            return {"status": "observing", "platform": platform, "audio_observed": False,
                    "message": "连续帧观察正在进行，稍后读取最新自动观察"}
        try:
            return watch(self, platform, question, duration_seconds, frames)
        finally:
            self.watch_lock.release()

    def media_status(self, platform):
        """Read actual HTML media state without inventing video content."""
        config = platform_config(platform)
        script = """JSON.stringify(Array.from(document.querySelectorAll('video,audio')).slice(0,5).map(
            v=>({kind:v.tagName.toLowerCase(),paused:v.paused,ended:v.ended,
                current_time:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:null,
                muted:v.muted,ready_state:v.readyState})))"""
        output = self.read_browser(config, ["eval", script], timeout=5)
        try:
            payload = output.strip()
            if payload.startswith("--- AGENT_BROWSER_PAGE_CONTENT "):
                payload = "\n".join(payload.splitlines()[1:-1])
            state = json.loads(payload)
            if isinstance(state, str):
                state = json.loads(state)
        except (TypeError, ValueError):
            state = output[:3000]
        return {"status": "ok", "platform": config.key, "media_state": state}

    def interact(self, platform, action, ref="", value="", operation_id="", contact_allowed=True):
        config = platform_config(platform)
        action = str(action)
        if action not in {"click", "fill", "select", "press", "hover", "check", "upload", "back", "reload", "switch_tab"}:
            raise ValueError("action 仅支持 click/fill/select/press/hover/check/upload/back/reload/switch_tab")
        if action == "switch_tab" and not re.fullmatch(r"t\d+", str(value)):
            raise ValueError("switch_tab 的 value 必须是 website_tabs 返回的 tN")
        if action not in {"back", "reload", "press", "switch_tab"} and not re.fullmatch(r"@?e\d+", ref):
            raise ValueError("请使用当前 snapshot 返回的 @eN 元素编号")
        if len(str(value)) > MAX_CONTENT:
            raise ValueError("输入内容过长")
        with self.execute_lock:
            payload = {"platform": config.key, "action": action, "ref": ref, "value": value}
            action_id = operation_id or uuid.uuid4().hex
            cached = self.store.action_result(action_id, payload)
            if cached is not None:
                return cached
            remembered = self.observed_refs.get(config.key, {})
            before_state = self.page_state(config)
            old_page = self.observed_pages.get(config.key)
            if ref and old_page and old_page != (before_state.get("url"), before_state.get("document_id")):
                raise ValueError("页面已切换，请刷新 snapshot 后重新选择")
            tree = self.browser.run(config, ["snapshot", "-i", "-c"])
            if CHALLENGE_PATTERN.search(tree):
                return {"status": "blocked_human", "human_action_required": True,
                        "message": "请主人完成登录或验证码后恢复活动", "snapshot": tree}
            if action == "press" and value in {"Enter", "Tab"} and not ref:
                raise ValueError("Enter/Tab 需提供当前输入框的 ref，避免误提交登录")
            line = next((line for line in tree.splitlines()
                         if re.search(r"\b" + re.escape(ref.lstrip("@")) + r"\b", line)), "")
            if ref and not line:
                raise ValueError("元素编号已失效，请刷新 snapshot")
            if PROTECTED_INTERACTION.search(line):
                return {"status": "blocked_human", "human_action_required": True,
                        "message": "登录、密码与验证控件由主人操作"}
            if SENSITIVE_INTERACTION.search(line):
                raise PermissionError("支付、删除或账号安全操作不属于自主社交活动")
            old_line = remembered.get(ref.lstrip("@"))
            if ref and not old_line:
                raise ValueError("元素尚未观察，请先读取 snapshot")
            if old_line and old_line != line:
                raise ValueError("元素含义已变化，请刷新 snapshot 后重新选择")
            search_input = bool(re.search(r'\bsearchbox\b|(?:textbox|combobox).*?(?:搜索|search)', line, re.I))
            if not contact_allowed and not search_input and re.search(r"\b(?:textbox|combobox)\b", line):
                # Sites often use a trending query as the accessible name.
                input_type = self.browser.run(config, ["get", "attr", ref, "type"]).strip()
                input_class = self.browser.run(config, ["get", "attr", ref, "class"]).strip()
                search_input = input_type == "search" or bool(re.search(r"(?:^|[-_\s])search(?:[-_\s]|$)", input_class, re.I))
            quiet_input = search_input and action in {"fill", "select", "press"}
            quiet_key = action == "press" and value in {
                "Space", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight",
            }
            if not contact_allowed and (
                (action in {"fill", "select", "check", "upload", "press"} and not quiet_input and not quiet_key)
                or re.search(r"发布|发表|发送|评论|回复|点赞|关注|收藏|转发", line)
            ):
                raise PermissionError("静默活动仅可阅读与导航，不公开互动")
            if action == "press" and value not in {"Enter", "Tab", "Space", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"}:
                raise ValueError("仅允许常用页面与播放控制按键")
            paths = self._media([value]) if action == "upload" else []
            fresh, cached = self.store.begin_action(action_id, payload)
            if not fresh:
                return cached
            target_url = ""
            tabs_before = ""
            if action == "click":
                try:
                    target_url = self.browser.run(config, ["get", "attr", ref, "href"]).strip()
                    tabs_before = self.browser.run(config, ["tab", "list"])
                except BrowserCommandError:
                    pass
            # Invalidate old analysis before any action, including a timed-out write.
            self._observe_automatically(config)
            try:
                if action == "upload":
                    args = ["upload", ref, paths[0]]
                elif action == "switch_tab":
                    args = ["tab", value]
                elif action in {"back", "reload"}:
                    args = [action]
                elif action == "press":
                    if ref:
                        self.browser.run(config, ["focus", ref])
                    args = ["press", value]
                else:
                    args = [action, ref, *([value] if action in {"fill", "select"} else [])]
                output = self.browser.run(config, args, timeout=300 if action == "upload" else 60)
            except BrowserCommandError as exc:
                public_write = (action in {"click", "check", "upload", "select"}
                                or (action == "press" and value == "Enter" and not search_input))
                result = {"status": "uncertain" if public_write else "failed", "operation_id": action_id,
                          "message": str(exc)[:1500], "retry_allowed": False,
                          "reobserve_required": not public_write}
                return self.store.finish_action(action_id, result)
            # The interaction succeeded. An observation failure must not erase its
            # known receipt or pretend that a publication may need repeating.
            try:
                after, after_state = self._snapshot_ready(config)
                self._remember(config, after, after_state)
                current = self.browser.run(config, ["get", "url"])
                result = {"status": "ok", "operation_id": action_id, "url": current,
                          "output": output[-2000:], "snapshot": after,
                          "page_state": after_state,
                          "human_action_required": bool(CHALLENGE_PATTERN.search(after))}
                if action == "click":
                    tabs_after = self.browser.run(config, ["tab", "list"])
                    navigated = navigation_identity(before_state.get("url")) != navigation_identity(after_state.get("url"))
                    new_tabs = sorted(set(re.findall(r"\[?(t\d+)\]?", tabs_after))
                                      - set(re.findall(r"\[?(t\d+)\]?", tabs_before)))
                    target_reached = bool(target_url.startswith(("https://", "/"))
                                          and navigation_identity(urljoin(before_state.get("url") or "", target_url))
                                          == navigation_identity(after_state.get("url")))
                    result["click_evidence"] = {
                        "target_href": target_url[:2000], "url_changed": navigated,
                        "new_tabs": new_tabs, "tabs": tabs_after[:10000],
                        "target_reached": target_reached,
                        "navigation_observed": bool(navigated or new_tabs),
                    }
                    if target_url.startswith(("https://", "/")) and not navigated and not new_tabs:
                        result["click_evidence"]["note"] = "点击已执行，但尚未观察到导航；请核查页面，不自动重试点击"
            except BrowserCommandError as exc:
                result = {"status": "ok", "operation_id": action_id, "output": output[-2000:],
                          "observation_error": str(exc)[:1500], "reobserve_required": True}
            return self.store.finish_action(action_id, result)

    def create_plan(
        self,
        platform,
        action,
        content,
        *,
        title="",
        target_url="",
        media_paths=None,
        steps=None,
    ):
        config = platform_config(platform)
        action = str(action or "").strip().lower()
        if action not in {"post", "comment", "video"}:
            raise ValueError("action 仅支持 post、comment、video")
        title, content = str(title or "").strip(), str(content or "").strip()
        if len(title) > MAX_TITLE or len(content) > MAX_CONTENT:
            raise ValueError("标题最多 200 字，正文最多 20000 字")
        if action in {"post", "comment"} and not content:
            raise ValueError("发帖或评论正文不能为空")
        if action == "comment" and not target_url:
            raise ValueError("评论必须提供目标页面 target_url")
        target = validate_url(config, target_url) if target_url else ""
        media = self._media(media_paths)
        rows = normalize_steps(config, action, title, content, media, target, steps)
        plan = self.store.create({
            "platform": config.key,
            "action": action,
            "title": title,
            "content": content,
            "target_url": target,
            "media": media,
            "steps": rows,
        })
        return {
            "status": "ok",
            "plan_status": plan["status"],
            "approval_instruction": f"主人核对后需明确回复：批准 {plan['id']}，然后执行 {plan['id']}",
            "plan": self.public_plan(plan),
        }

    def get_plan(self, plan_id):
        plan = self.store.get(plan_id)
        if not plan:
            raise ValueError("计划不存在")
        return {"status": "ok", "plan_status": plan["status"], "plan": self.public_plan(plan)}

    def approve(self, plan_id):
        plan = self.store.approve(plan_id)
        return {"status": "ok", "plan_status": plan["status"], "plan": self.public_plan(plan)}

    def _perform(self, plan, step):
        config = platform_config(plan["platform"])
        op = step["op"]
        if op == "open":
            return self.browser.run(config, ["open", step["url"]])
        if op == "snapshot":
            return self.browser.run(config, ["snapshot", "-i", "-c"])
        if op == "wait":
            return self.browser.run(config, ["wait", step["milliseconds"]])
        if op == "fill":
            return self.browser.run(
                config,
                _locator_args(step["target"], "fill", plan[step["field"]]),
            )
        if op == "select":
            return self.browser.run(config, _locator_args(step["target"], "select", step["value"]))
        if op == "upload":
            return self.browser.run(
                config,
                ["upload", step["selector"], plan["media"][step["media_index"]]],
                timeout=300,
            )
        if op == "click":
            return self.browser.run(config, _locator_args(step["target"], "click"))
        if op == "verify_text":
            snapshot = self.browser.run(config, ["snapshot", "-c"])
            if step["text"] not in snapshot:
                raise BrowserCommandError(f"页面中没有出现验证文本：{step['text']}")
            return snapshot
        raise ValueError(f"不支持的步骤：{op}")

    def execute(self, plan_id):
        with self.execute_lock:
            plan = self.store.begin(plan_id)
            if plan["status"] == "completed":
                return {
                    "status": "ok", "plan_status": "completed",
                    "replayed": False, "plan": self.public_plan(plan),
                }
            self._observe_automatically(platform_config(plan["platform"]))
            committed = any(
                step.get("commit") for step in plan["steps"][:plan["next_step"]]
            )
            for index in range(plan["next_step"], len(plan["steps"])):
                step = plan["steps"][index]
                is_commit = bool(step.get("commit"))
                try:
                    output = self._perform(plan, step)
                except BrowserCommandError as exc:
                    uncertain = committed or is_commit or exc.timeout
                    status = "uncertain" if uncertain else "failed"
                    result = self.store.finish(plan["id"], status, error=str(exc))
                    return {
                        "status": status, "plan_status": status,
                        "replayed": False, "plan": self.public_plan(result),
                    }
                if CHALLENGE_PATTERN.search(output or ""):
                    status = "uncertain" if committed or is_commit else "blocked_human"
                    message = (
                        "提交后出现验证，结果未知，禁止自动重发。"
                        if status == "uncertain"
                        else "浏览器等待登录、验证码或安全确认；请在可见窗口完成人工操作。"
                    )
                    result = self.store.finish(plan["id"], status, error=message)
                    return {
                        "status": status, "plan_status": status,
                        "human_action_required": True, "plan": self.public_plan(result),
                    }
                self.store.advance(plan["id"], index + 1)
                committed = committed or is_commit
            config = platform_config(plan["platform"])
            try:
                final_url = self.browser.run(config, ["get", "url"])
            except BrowserCommandError:
                final_url = ""
            receipt = {
                "plan_id": plan["id"],
                "payload_hash": plan["payload_hash"],
                "completed_at": time.time(),
                "final_url": final_url[:1000],
                "step_count": len(plan["steps"]),
            }
            result = self.store.finish(plan["id"], "completed", receipt=receipt)
            return {
                "status": "ok", "plan_status": "completed",
                "replayed": False, "receipt": receipt, "plan": self.public_plan(result),
            }
