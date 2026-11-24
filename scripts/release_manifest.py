"""Write the manifest to commit before creating a signed channel tag."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("macos-arm64", "macos-x86_64", "windows-x86_64")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("sequence", type=int)
    parser.add_argument("--target", action="append", choices=TARGETS)
    args = parser.parse_args()
    if args.sequence < 1:
        parser.error("发布序号须为正整数")
    data = dict(version=args.version, sequence=args.sequence, platforms=args.target or list(TARGETS),
                protocol=1, state_format=1, lock_sha256=hashlib.sha256((ROOT/"uv.lock").read_bytes()).hexdigest())
    (ROOT/"release.json").write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
