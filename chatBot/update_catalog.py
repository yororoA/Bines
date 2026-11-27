"""Refresh the bundled, versioned official OpenAPI reference; never calls QQ."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.request import urlopen

API_DIR = Path(__file__).with_name("api")


def update(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("version must have the form 4.18.38")
    url = f"https://raw.githubusercontent.com/NapNeko/NapCatDocs/main/src/api/{version}/openapi.json"
    with urlopen(url, timeout=30) as response:
        raw = response.read()
    document = json.loads(raw)
    if document.get("info", {}).get("version") != version or not document.get("paths"):
        raise ValueError("Official OpenAPI version or paths are invalid")
    if any("post" not in item for item in document["paths"].values()):
        raise ValueError("Expected OneBot POST actions")
    API_DIR.mkdir(exist_ok=True)
    target = API_DIR / f"napcat-{version}.openapi.json"
    # Write bytes, not text: write_text() would turn "\n" into "\r\n" on Windows,
    # and the manifest hash would then never match on a checkout with LF endings.
    payload = (json.dumps(document, ensure_ascii=False, separators=(",", ":"))+"\n").encode("utf-8")
    target.write_bytes(payload)
    manifest = {
        "version": version, "file": target.name, "source": url,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "actions": len(document["paths"]),
    }
    (API_DIR / "manifest.json").write_bytes((json.dumps(manifest, indent=2)+"\n").encode("utf-8"))
    print(f"NapCatDocs {version}: {manifest['actions']} actions saved in {target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    update(parser.parse_args().version)
