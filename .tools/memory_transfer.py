"""记忆迁移：把 memory_data 与 persona 打包带走，或在新设备上还原。

用法（项目根目录下）：

    python .tools/memory_transfer.py export  D:\\bines-memory-20261022.tar.gz
    python .tools/memory_transfer.py verify  D:\\bines-memory-20261022.tar.gz
    python .tools/memory_transfer.py import  D:\\bines-memory-20261022.tar.gz

为什么不能直接复制文件夹：
  memory.db 跑着 WAL 模式，2 MB 的热数据还在 memory.db-wal 里，
  只拷 memory.db 会丢掉最近的对话。export 会先停容器再打包。
  index/ 是 Chroma 向量库（384 维 all-MiniLM-L6-v2），和 memory.db 里的
  source_ref 一一对应，两者必须同进同出，否则搜索会对不上。

包里不含：.env（你的 API Key）、thingking/models（88 MB 嵌入模型）。
新设备跑一遍 install.py 就有模型；密钥请单独安全地传。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = ROOT / "thingking" / "memory_data"
PERSONA_DIR = ROOT / "thingking" / "persona"
MANIFEST = "bines-memory-manifest.json"


def docker_bin() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe",
        Path("C:/Program Files/Docker/Docker/resources/bin/docker.exe"),
    ]
    return str(candidates[0]) if candidates[0].is_file() else None


def running_containers() -> list[str]:
    docker = docker_bin()
    if not docker:
        return []
    try:
        out = subprocess.run(
            [docker, "ps", "--filter", "name=bines-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=20,
        )
        return [x for x in (out.stdout or "").split() if x]
    except (OSError, subprocess.SubprocessError):
        return []


def service_names(containers: list[str]) -> list[str]:
    """bines-rag-web-1 -> rag-web"""
    names = []
    for item in containers:
        name = item
        if name.startswith("bines-"):
            name = name[len("bines-"):]
        if name.endswith("-1"):
            name = name[:-2]
        names.append(name)
    return names


def toggle_containers(action: str, services: list[str] | None = None) -> None:
    docker = docker_bin()
    if not docker:
        return
    compose = ROOT / "deploy" / "docker" / "compose.yaml"
    env_file = ROOT / "deploy" / "napcat" / ".env"
    profiles = ["--profile", "full"] if not services or "rag-web" in services else []
    cmd = [docker, "compose", "-f", str(compose)]
    if env_file.is_file():
        cmd += ["--env-file", str(env_file)]
    cmd += profiles
    if action == "stop":
        cmd += ["stop"] + (services or [])
    else:
        cmd += ["up", "-d"] + (services or [])
    subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=240)


def sqlite_counts(db: Path) -> dict:
    if not db.is_file():
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        counts = {}
        for table in ("memories", "messages", "conversations"):
            try:
                counts[table] = con.execute(f"select count(*) from {table}").fetchone()[0]
            except sqlite3.DatabaseError:
                continue
        con.close()
        return counts
    except sqlite3.DatabaseError as exc:
        return {"error": str(exc)}


def chroma_dimension() -> int | None:
    db = MEMORY_DIR / "index" / "chroma.sqlite3"
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("select dimension from collections limit 1").fetchone()
        con.close()
        return row[0] if row else None
    except sqlite3.DatabaseError:
        return None


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def build_manifest() -> dict:
    counts = sqlite_counts(MEMORY_DIR / "memory.db")
    return {
        "format": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_root": str(ROOT),
        "counts": counts,
        "chroma_dimension": chroma_dimension(),
        "embedding_model": "all-MiniLM-L6-v2 (384)",
        "has_wal": (MEMORY_DIR / "memory.db-wal").is_file(),
    }


def do_export(target: Path) -> int:
    if not MEMORY_DIR.is_dir():
        print(f"[错误] 找不到 {MEMORY_DIR}")
        return 1
    if not PERSONA_DIR.is_dir():
        print(f"[错误] 找不到 {PERSONA_DIR}")
        return 1

    live = running_containers()
    services = service_names(live)
    stopped = False
    if live:
        print(f"检测到 {len(live)} 个容器在运行（{', '.join(services)}），先停掉以便打包一致的数据库…")
        toggle_containers("stop", services)
        stopped = True
        time.sleep(2)

    manifest = build_manifest()
    print(f"记忆条目 {manifest['counts'].get('memories', '?')} 条，"
          f"消息 {manifest['counts'].get('messages', '?')} 条，"
          f"向量维度 {manifest['chroma_dimension']}")

    target.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = target.parent / MANIFEST
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        with tarfile.open(target, "w:gz") as tar:
            tar.add(MEMORY_DIR, arcname="thingking/memory_data")
            tar.add(PERSONA_DIR, arcname="thingking/persona")
            tar.add(manifest_path, arcname=MANIFEST)
    finally:
        manifest_path.unlink(missing_ok=True)

    size = target.stat().st_size / 2 ** 20
    print(f"已打包 {target}（{size:.1f} MB）")
    print(f"  校验值 sha256 = {digest(target)}")

    if stopped:
        print("恢复容器…")
        toggle_containers("up", services)
    print("\n迁移到新设备后：")
    print("  1) 先在新设备跑一遍 install.py，确保嵌入模型与依赖就位")
    print("  2) 把 .env 单独安全地传过去（包里不含密钥）")
    print("  3) python .tools/memory_transfer.py import <包路径>")
    return 0


def open_archive(source: Path) -> tarfile.TarFile:
    if not source.is_file():
        raise FileNotFoundError(source)
    tar = tarfile.open(source, "r:gz")
    for member in tar.getmembers():
        name = member.name.replace("\\", "/")
        if name.startswith("/") or ".." in Path(name).parts:
            tar.close()
            raise ValueError(f"包内含有可疑路径：{member.name}")
    return tar


def do_verify(source: Path) -> int:
    with open_archive(source) as tar:
        try:
            member = tar.getmember(MANIFEST)
        except KeyError:
            print("[错误] 包里没有 manifest，可能不是本工具产出的归档")
            return 1
        manifest = json.loads(tar.extractfile(member).read().decode("utf-8"))
    print(f"导出时间   {manifest.get('created_at')}")
    print(f"来源路径   {manifest.get('source_root')}")
    print(f"记忆 / 消息 {manifest['counts'].get('memories')} / {manifest['counts'].get('messages')}")
    print(f"向量维度   {manifest.get('chroma_dimension')}  ({manifest.get('embedding_model')})")
    print(f"含 WAL     {manifest.get('has_wal')}")
    print(f"包校验     sha256 = {digest(source)}")
    return 0


def do_import(source: Path) -> int:
    if do_verify(source):
        return 1
    live = running_containers()
    if live:
        print("\n先停容器…")
        toggle_containers("stop", service_names(live))
        time.sleep(2)

    temp = ROOT / ".tools" / "cache" / "memory-import"
    if temp.exists():
        shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)

    try:
        with open_archive(source) as tar:
            try:
                tar.extractall(path=temp, filter="data")
            except TypeError:  # Python 3.11 没有 filter 参数
                tar.extractall(path=temp)

        incoming = temp / "thingking" / "memory_data"
        incoming_persona = temp / "thingking" / "persona"
        if not (incoming / "memory.db").is_file():
            print("[错误] 包里缺少 memory.db，原数据未动")
            return 1
        counts = sqlite_counts(incoming / "memory.db")
        print(f"包内记忆 {counts.get('memories', '?')} 条，消息 {counts.get('messages', '?')} 条")
        if not counts.get("memories"):
            print("[错误] 解出来的库里没有记忆，中止，原数据未动")
            return 1

        stamp = time.strftime("%Y%m%d_%H%M%S")
        if MEMORY_DIR.exists():
            backup = MEMORY_DIR.with_name(f"memory_data.bak-{stamp}")
            MEMORY_DIR.rename(backup)
            print(f"原记忆目录已保留为 {backup.name}")
        shutil.move(str(incoming), str(MEMORY_DIR))
        print("记忆数据已就位")

        if (incoming_persona / "PERSONA.md").is_file():
            if PERSONA_DIR.exists():
                backup_persona = PERSONA_DIR.with_name(f"persona.bak-{stamp}")
                PERSONA_DIR.rename(backup_persona)
                print(f"原人格目录已保留为 {backup_persona.name}")
            shutil.move(str(incoming_persona), str(PERSONA_DIR))
            print("人格定义已就位")
    except Exception as exc:  # noqa: BLE001 - 迁移脚本要给出明确失败原因
        print(f"[失败] {type(exc).__name__}: {exc}")
        return 1
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    print("\n导入完成。接下来：")
    print("  Linux/macOS 上要让容器内 UID 1000 能写：")
    print(f"    sudo chown -R 1000:1000 {MEMORY_DIR}")
    print("  然后启动：python bines.py --profile chatbot")
    print("  如果搜索不灵，重建向量索引：")
    print("    docker compose -f deploy/docker/compose.yaml exec memory-server \\")
    print("      python thingking/memory_admin.py rebuild-index")
    return 0


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    action, raw = sys.argv[1], sys.argv[2]
    target = Path(raw).expanduser().resolve()
    handlers = {"export": do_export, "import": do_import, "verify": do_verify}
    handler = handlers.get(action)
    if not handler:
        print(f"未知动作：{action}\n\n{__doc__}")
        return 2
    return handler(target)


if __name__ == "__main__":
    raise SystemExit(main())
