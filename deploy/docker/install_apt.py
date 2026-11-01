"""Install Debian packages with verified HTTPS sources and bounded fallback."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from urllib.parse import urlsplit, urlunsplit


DEFAULT_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn"
OFFICIAL_HOSTS = {"deb.debian.org", "security.debian.org"}
APT_OPTIONS = [
    "-o", "Acquire::Retries=2",
    "-o", "Acquire::http::Timeout=20", "-o", "Acquire::https::Timeout=20",
    "-o", "APT::Update::Error-Mode=any",
    "-o", "Acquire::AllowInsecureRepositories=false",
    "-o", "Acquire::AllowDowngradeToInsecureRepositories=false",
    "-o", "APT::Get::AllowUnauthenticated=false",
    "-o", "Acquire::https::Verify-Peer=true", "-o", "Acquire::https::Verify-Host=true",
]


class AptInstallError(RuntimeError):
    pass


def mirror_url(value):
    """Accept the old host-only build argument, always using HTTPS."""
    value = str(value).strip()
    if not value or re.search(r"[\s\\]", value):
        raise AptInstallError("APT_MIRROR must be a hostname or HTTPS base URL")
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        _ = parsed.port
    except ValueError as exc:
        raise AptInstallError("APT_MIRROR contains an invalid port") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or "?" in value or "#" in value):
        raise AptInstallError("APT_MIRROR must not contain credentials, query parameters or fragments")
    # Accessing .port above validates bracketed IPv6 and numeric port syntax.
    return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def rewrite_source(text, *, deb822, mirror=None):
    """Change only Debian URI fields, retaining suites, security paths and keys."""
    def replace(match):
        parsed = urlsplit(match.group())
        if parsed.hostname not in OFFICIAL_HOSTS:
            return match.group()
        if mirror:
            base = urlsplit(mirror)
            return urlunsplit(("https", base.netloc, base.path + parsed.path, parsed.query, parsed.fragment))
        return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment))

    uri = re.compile(r"https?://[^\s#]+")
    uri_field = False
    result = []
    for line in text.splitlines(keepends=True):
        if deb822:
            if not line.startswith((" ", "\t")):
                uri_field = bool(re.match(r"(?i)^URIs\s*:", line))
            if uri_field and not line.lstrip().startswith("#"):
                line = uri.sub(replace, line)
        elif re.match(r"^\s*deb(?:-src)?\s+", line):
            # The URI follows optional [arch=... signed-by=...] options.
            match = re.match(r"^(\s*deb(?:-src)?\s+(?:\[[^\]]*\]\s+)?)(https?://\S+)", line)
            if match:
                start, end = match.span(2)
                line = line[:start] + uri.sub(replace, line[start:end]) + line[end:]
        result.append(line)
    return "".join(result)


def source_files(root):
    files = [root / "sources.list", *sorted((root / "sources.list.d").glob("*.list")),
             *sorted((root / "sources.list.d").glob("*.sources"))]
    return {path: path.read_text(encoding="utf-8") for path in files if path.is_file()}


def _terminate_group(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    process.wait()


def run_apt(arguments, *, timeout, env):
    """Keep apt's diagnostic output, and reap its entire group on timeout."""
    try:
        with subprocess.Popen(arguments, env=env, start_new_session=True) as process:
            try:
                status = process.wait(timeout=timeout)
            except BaseException as exc:
                _terminate_group(process)
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise AptInstallError(f"APT exceeded {timeout}s; its process group was terminated") from exc
                raise
    except OSError as exc:
        raise AptInstallError(f"Cannot start APT: {exc}") from exc
    if status:
        raise AptInstallError(f"APT exited with status {status}")


def clear_lists(root):
    if root.is_dir():
        for path in root.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()


def install_packages(packages, *, mirror=DEFAULT_MIRROR, source_root=Path("/etc/apt"),
                     lists_root=Path("/var/lib/apt/lists"), runner=run_apt):
    if not packages or any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.+:=-]*", p) for p in packages):
        raise AptInstallError("Provide explicit Debian package names, not APT options")
    mirror = mirror_url(mirror)
    sources = source_files(source_root)
    if not sources:
        raise AptInstallError("No Debian APT .list or deb822 .sources files found")
    if not any(rewrite_source(text, deb822=path.suffix == ".sources", mirror="https://debian-source-check.invalid") != text
               for path, text in sources.items()):
        raise AptInstallError("No supported official Debian URI found; third-party repositories were not rewritten")
    preferred = {path: rewrite_source(text, deb822=path.suffix == ".sources", mirror=mirror)
                 for path, text in sources.items()}
    official = {path: rewrite_source(text, deb822=path.suffix == ".sources") for path, text in sources.items()}
    attempts = [("preferred HTTPS mirror " + mirror, preferred)]
    if preferred != official:
        attempts.append(("official Debian HTTPS sources", official))
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    failures = []
    for label, contents in attempts:
        print(f"[APT] Trying {label}; retries=2, request timeout=20s", flush=True)
        for path, text in contents.items():
            path.write_text(text, encoding="utf-8")
        clear_lists(lists_root)
        stage = "update"
        try:
            runner(["apt-get", *APT_OPTIONS, "update"], timeout=120, env=env)
            stage = "install"
            runner(["apt-get", *APT_OPTIONS, "install", "-y", "--no-install-recommends", *packages],
                   timeout=600, env=env)
        except AptInstallError as exc:
            failure = f"{label}: {stage} failed: {exc}"
            failures.append(failure)
            print(f"[APT] {failure}", flush=True)
            continue
        clear_lists(lists_root)
        print(f"[APT] Package installation succeeded using {label}", flush=True)
        return
    raise AptInstallError("All APT source attempts failed; no successful installation: " + "; ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", default=DEFAULT_MIRROR)
    parser.add_argument("packages", nargs="+")
    args = parser.parse_args()
    try:
        install_packages(args.packages, mirror=args.mirror)
    except AptInstallError as exc:
        parser.exit(1, f"[APT] {exc}\n")


if __name__ == "__main__":
    main()
