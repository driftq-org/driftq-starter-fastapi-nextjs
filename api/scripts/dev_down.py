#!/usr/bin/env python3
"""
dev_down.py — Bring the DriftQ demo stack down (optionally wipe volumes + images).

Typical:
  python -m scripts.dev_down              # docker compose down
  python -m scripts.dev_down --stop       # docker compose stop (keep containers)
  python -m scripts.dev_down --wipe       # down + remove volumes (WAL/data gone)

More aggressive:
  python -m scripts.dev_down --wipe --rmi local --prune --yes
  python -m scripts.dev_down --wipe --rmi all --prune --yes   # ⚠️ can remove lots of images

Notes:
- `--wipe` deletes the named volume that holds DriftQ data/WAL for this compose project.
- `--prune` runs `docker image prune -f` (only dangling images).
- `--prune-volumes` runs `docker volume prune -f` (dangling volumes) ⚠️ can affect other projects.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import List


def repo_root() -> Path:
    # api/scripts/dev_down.py -> repo root is ../../
    return Path(__file__).resolve().parents[2]


def default_compose_file() -> Path:
    return repo_root() / "docker-compose.yml"


def compose_base_cmd(args: argparse.Namespace) -> List[str]:
    cmd = ["docker", "compose", "-f", str(Path(args.file).resolve())]
    if args.project:
        cmd += ["-p", args.project]

    return cmd


def run(cmd: List[str], *, check: bool = True) -> int:
    p = subprocess.run(cmd)
    if check and p.returncode != 0:
        raise SystemExit(p.returncode)

    return p.returncode


def confirm_or_exit(args: argparse.Namespace, message: str) -> None:
    if args.yes:
        return
    print(message)
    ans = input("Type 'yes' to continue: ").strip().lower()

    if ans != "yes":
        print("Aborted.")
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(prog="dev_down.py")

    ap.add_argument(
        "-f",
        "--file",
        default=str(default_compose_file()),
        help="Path to docker-compose.yml (default: repo root docker-compose.yml)"
    )

    ap.add_argument(
        "-p",
        "--project",
        default="",
        help="Compose project name override (default: compose decides)."
    )

    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--stop", action="store_true", help="Stop containers only (keeps them).")
    mode.add_argument("--down", action="store_true", help="Bring stack down (default behavior).")

    ap.add_argument("--wipe", action="store_true", help="Remove volumes too (deletes DriftQ WAL/data).")
    ap.add_argument(
        "--rmi",
        choices=["none", "local", "all"],
        default="none",
        help="Remove images used by this compose stack: none/local/all."
    )

    # Back-compat alias (older README wording)
    ap.add_argument(
        "--prune-images",
        action="store_true",
        help="Alias for: --rmi local --prune (kept for backwards compatibility).",
    )

    ap.add_argument("--prune", action="store_true", help="Run `docker image prune -f` after down (dangling only).")
    ap.add_argument(
        "--prune-volumes",
        action="store_true",
        help="Run `docker volume prune -f` (dangling volumes) ⚠️ can affect other projects."
    )
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompts.")

    args = ap.parse_args()
    do_stop = bool(args.stop)
    # do_down = not do_stop  # kept for readability

    # Alias behavior
    if args.prune_images:
        if args.rmi == "none":
            args.rmi = "local"
        args.prune = True

    base = compose_base_cmd(args)

    destructive = []
    if args.wipe:
        destructive.append("remove volumes (WAL/data will be deleted)")

    if args.rmi != "none":
        destructive.append(f"remove images ({args.rmi})")

    if args.prune_volumes:
        destructive.append("docker volume prune (global dangling volumes)")

    if destructive:
        confirm_or_exit(args, "⚠️ This will " + ", ".join(destructive) + ".")

    if do_stop:
        print("🛑 docker compose stop ...")
        run(base + ["stop"], check=True)
        print("✅ stopped")

    else:
        cmd = base + ["down"]
        if args.wipe:
            cmd.append("-v")

        if args.rmi != "none":
            cmd += ["--rmi", args.rmi]

        print("🧹 docker compose down ...")
        run(cmd, check=True)
        print("✅ down")

    if args.prune:
        print("🧽 docker image prune -f ...")
        run(["docker", "image", "prune", "-f"], check=True)

    if args.prune_volumes:
        print("🧽 docker volume prune -f ...")
        run(["docker", "volume", "prune", "-f"], check=True)

    print("Done ✅")


if __name__ == "__main__":
    main()
