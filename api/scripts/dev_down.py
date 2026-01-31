from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional


def repo_root() -> Path:
    # scripts/dev_down.py -> scripts -> repo root
    return Path(__file__).resolve().parent.parent


def pick_compose_cmd() -> List[str]:
    """
    Prefer Docker Compose v2: `docker compose`
    Fallback to legacy: `docker-compose`
    """
    if shutil.which("docker"):
        # We won't "probe" with a command here; just assume v2 is present if docker exists.
        # If `docker compose` isn't supported, the call will fail and we fallback below.
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]

    raise RuntimeError("Neither 'docker' nor 'docker-compose' found in PATH.")


def run(cmd: List[str], cwd: Optional[Path] = None) -> int:
    print("▶", " ".join(cmd))
    return subprocess.call(cmd, cwd=str(cwd) if cwd else None)


def main() -> int:
    p = argparse.ArgumentParser(description="Bring DriftQ starter stack down (optionally wipe volumes/WAL).")

    p.add_argument(
        "-f",
        "--file",
        default="docker-compose.yml",
        help="Compose file path relative to repo root (default: docker-compose.yml)."
    )

    p.add_argument(
        "-p",
        "--project",
        default=None,
        help="Optional Compose project name (-p)."
    )

    mode = p.add_mutually_exclusive_group()

    mode.add_argument(
        "--stop",
        action="store_true",
        help="Stop containers only (keeps networks, containers, volumes)."
    )

    mode.add_argument(
        "--down",
        action="store_true",
        help="Down containers + networks (default if neither --stop nor --down specified)."
    )

    p.add_argument(
        "--wipe",
        action="store_true",
        help="Also remove volumes (WAL wipe). Equivalent to `docker compose down -v`."
    )

    p.add_argument(
        "--rmi",
        choices=["none", "local", "all"],
        default="none",
        help="Remove images used by services (none/local/all)."
    )

    p.add_argument(
        "--prune",
        action="store_true",
        help="Run `docker system prune -f` after bringing stack down."
    )

    p.add_argument(
        "--prune-volumes",
        action="store_true",
        help="Include volumes in prune (`docker system prune --volumes -f`). Requires --wipe.",
    )

    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for destructive actions.",
    )

    args = p.parse_args()

    root = repo_root()
    compose_file = root / args.file
    if not compose_file.exists():
        print(f"❌ Compose file not found: {compose_file}")
        return 2

    if args.prune_volumes and not args.wipe:
        print("❌ --prune-volumes is only allowed when --wipe is set (safety).")
        return 2

    # Default behavior: down
    want_stop = bool(args.stop)
    want_down = bool(args.down) or not args.stop

    compose_base = pick_compose_cmd()

    def compose_cmd(extra: List[str]) -> List[str]:
        cmd = compose_base + ["-f", str(compose_file)]
        if args.project:
            cmd += ["-p", args.project]
        cmd += extra
        return cmd

    # Confirmation for destructive ops
    destructive = args.wipe or args.rmi != "none" or args.prune or args.prune_volumes
    if destructive and not args.yes:
        print("⚠️  You requested destructive cleanup:")
        if args.wipe:
            print("   - remove volumes (WAL wipe)")

        if args.rmi != "none":
            print(f"   - remove images: {args.rmi}")

        if args.prune:
            print("   - docker system prune")

        if args.prune_volumes:
            print("   - docker system prune --volumes")

        ans = input("Type 'yes' to continue: ").strip().lower()
        if ans != "yes":
            print("Aborted.")
            return 1

    # 1) Stop or down
    if want_stop:
        rc = run(compose_cmd(["stop"]), cwd=root)
        if rc != 0:
            return rc
        print("✅ Stopped containers (not removed).")

    elif want_down:
        down_args: List[str] = ["down", "--remove-orphans"]
        if args.wipe:
            down_args.append("-v")

        if args.rmi != "none":
            down_args += ["--rmi", args.rmi]

        # Try docker compose, then fallback to docker-compose if needed
        rc = run(compose_cmd(down_args), cwd=root)
        if rc != 0 and compose_base == ["docker", "compose"] and shutil.which("docker-compose"):
            print("↪️  `docker compose` failed; retrying with `docker-compose`…")
            compose_base[:] = ["docker-compose"]
            rc = run(compose_cmd(down_args), cwd=root)

        if rc != 0:
            return rc
        print("✅ Brought stack down." + (" (volumes wiped)" if args.wipe else ""))

    # 2) Optional prune
    if args.prune:
        prune_cmd = ["docker", "system", "prune", "-f"]
        if args.prune_volumes:
            prune_cmd.insert(3, "--volumes")  # docker system prune --volumes -f
        rc = run(prune_cmd, cwd=root)
        if rc != 0:
            return rc
        print("✅ Docker system prune completed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
