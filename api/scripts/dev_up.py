import subprocess
import sys

SERVICES = {
    "web": ("3000", "UI", "/"),
    "api": ("8000", "API Docs", "/docs"),
    "driftq": ("8080", "DriftQ Health", "/v1/healthz"),
}

def compose_base_cmd():
    # Prefer "docker compose" (new) but support "docker-compose" (old)
    try:
        subprocess.run(["docker", "compose", "version"], check=True, capture_output=True, text=True)
        return ["docker", "compose"]
    except Exception:
        return ["docker-compose"]

def port_for(service: str, container_port: str) -> str | None:
    base = compose_base_cmd()
    # docker compose port <service> <port>  ->  0.0.0.0:3000
    p = subprocess.run(base + ["port", service, container_port], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    out = p.stdout.strip()
    if not out:
        return None
    # handle "0.0.0.0:3000" or "[::]:3000"
    return out.rsplit(":", 1)[-1]

def main():
    base = compose_base_cmd()

    # Bring everything up detached (so the script can continue and print URLs)
    subprocess.check_call(base + ["up", "--build", "-d"])

    print("\n✅ Services are up. Open these:\n")
    for svc, (cport, label, path) in SERVICES.items():
        hp = port_for(svc, cport) or cport
        print(f"- {label}: http://localhost:{hp}{path}")

    print("\nLogs:")
    print("  " + " ".join(base + ["logs", "-f"]))
    print("\nStop:")
    print("  " + " ".join(base + ["down"]))
    print("Wipe data (incl. WAL volume):")
    print("  " + " ".join(base + ["down", "-v"]))

if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Command failed with exit code {e.returncode}", file=sys.stderr)
        sys.exit(e.returncode)
