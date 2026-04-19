#!/usr/bin/env python3
"""Multi-pair UERANSIM launcher for Open5GS 5GC.

Spawns N independent UE-gNB pairs as Docker containers on the existing
`docker_open5gs_default` network, registers each UE in Open5GS MongoDB,
and waits for PDU session establishment.

Typical usage:
    python ueran.py up -n 10
    python ueran.py status
    python ueran.py ping 5
    python ueran.py down
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCKER_DIR = ROOT / "docker"
GEN_DIR = DOCKER_DIR / "generated"
COMPOSE_FILE = GEN_DIR / "docker-compose.multi.yaml"
PAIRS_FILE = GEN_DIR / "pairs.json"

# Shared with open5gs-docker/.env
MCC = "001"
MNC = "01"
TAC = "1"
AMF_IP = "172.22.0.10"
NETWORK = "docker_open5gs_default"
IMAGE = "ntn-litesys-ueransim:latest"
MONGO_CONTAINER = "mongo"

# Shared UICC (same as docker/.env — single-pair setup)
UE_KI = "8baf473f2f8fd09487cccbd7097c6862"
UE_OP = "11111111111111111111111111111111"
UE_AMF = "8000"
UE_IMEI = "356938035643803"
UE_IMEISV = "4370816125816151"

MAX_PAIRS = 500  # hard cap: Linux bridge BR_MAX_PORTS=1024 (15 Open5GS + 2*N ≤ 1024)


def _block_ip(base2: int, base3: int, i: int) -> str:
    """Allocate the i-th IP starting at 172.22.{base2}.{base3}, rolling
    into the next 3rd-octet block when .254 is reached. 172.22.0.x is
    reserved for Open5GS services."""
    idx = i - 1
    octet2 = base2 + idx // 254
    octet3 = base3 + idx % 254
    return f"172.22.{octet2}.{octet3}"


def pair(i: int) -> dict:
    """Deterministic allocation for pair id `i` (1-indexed).
    gNBs live in 172.22.{1,2,...}.x; UEs live in 172.22.{10,11,...}.x."""
    return {
        "id": i,
        "gnb_name": f"ntn_gnb_{i}",
        "ue_name": f"ntn_ue_{i}",
        "gnb_ip": _block_ip(1, 1, i),
        "ue_ip": _block_ip(10, 1, i),
        "nci": f"0x{i:09x}",
        "imsi": f"00101000000{i:04d}",
    }


# ---------- shell helpers ----------

def run(cmd, check=True, capture=False, stdin=None):
    """Run a subprocess command, return CompletedProcess."""
    if isinstance(cmd, str):
        cmd_pretty = cmd
        shell = True
    else:
        cmd_pretty = " ".join(cmd)
        shell = False
    print(f"$ {cmd_pretty}", file=sys.stderr)
    return subprocess.run(
        cmd,
        check=check,
        shell=shell,
        input=stdin,
        text=True,
        capture_output=capture,
    )


def docker_inspect(container: str, fmt: str) -> str | None:
    r = subprocess.run(
        ["docker", "inspect", "-f", fmt, container],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip()


# ---------- preflight ----------

def _subnet_prefix() -> int | None:
    """Return the /N prefix length of docker_open5gs_default (e.g., 24 or 16),
    or None if the network doesn't exist."""
    r = subprocess.run(
        ["docker", "network", "inspect", NETWORK,
         "--format", "{{range .IPAM.Config}}{{.Subnet}}{{end}}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or "/" not in r.stdout:
        return None
    try:
        return int(r.stdout.strip().split("/", 1)[1])
    except ValueError:
        return None


def preflight(n: int):
    """Fail fast if 5GC prerequisites are missing or subnet is too narrow."""
    net = docker_inspect(NETWORK, "{{.Name}}")
    if not net:
        sys.exit(
            f"error: docker network '{NETWORK}' not found. "
            "Start Open5GS first:\n"
            "  cd ../open5gs-docker && docker compose -f sa-deploy.yaml up -d"
        )
    prefix = _subnet_prefix()
    # For n pairs we touch IPs up to 172.22.{1 + (n-1)//254}.x (gNBs) and
    # 172.22.{10 + (n-1)//254}.x (UEs). Require the subnet to cover those.
    # /24 covers only 172.22.0.x; /16 covers all of 172.22.0.0-172.22.255.255.
    max_octet2 = 10 + (n - 1) // 254  # highest 2nd octet we'll allocate
    # For subnet mask /p, 3rd+4th octets we can span = 32 - p bits.
    # We need max_octet2 to fit in the subnet. /24 → only octet2=0; /22 → 0..3; /16 → 0..255.
    allowed_max_octet2 = (1 << (32 - (prefix or 24))) // 256 - 1
    if prefix is None or max_octet2 > allowed_max_octet2:
        sys.exit(
            f"error: {NETWORK} is /{prefix} (covers 172.22.0..{allowed_max_octet2}.x), "
            f"but n={n} needs up to 172.22.{max_octet2}.x.\n"
            "Expand the subnet to /16:\n"
            "  1. Stop all containers on the network:\n"
            "       cd ../open5gs-docker && docker compose -f sa-deploy.yaml down\n"
            f"  2. Edit open5gs-docker/.env: TEST_NETWORK=172.22.0.0/16\n"
            "  3. Remove and recreate the network:\n"
            f"       docker network rm {NETWORK}\n"
            "       docker compose -f sa-deploy.yaml up -d"
        )
    mongo = docker_inspect(MONGO_CONTAINER, "{{.State.Status}}")
    if mongo != "running":
        sys.exit(
            f"error: container '{MONGO_CONTAINER}' is not running (state={mongo}). "
            "Start Open5GS first."
        )
    img = subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        capture_output=True, text=True,
    )
    if img.returncode != 0:
        sys.exit(
            f"error: image '{IMAGE}' not found. Build it first:\n"
            "  cd docker && docker compose build"
        )


# ---------- compose generation ----------

SERVICE_TMPL = """\
  {name}:
    image: {image}
    container_name: {name}
    stdin_open: true
    tty: true
    environment:
      MCC: "{mcc}"
      MNC: "{mnc}"
      TAC: "{tac}"
      AMF_IP: "{amf_ip}"
{extra_env}
    volumes:
      - ../configs:/configs:ro
      - ../scripts:/scripts:ro
      - /etc/timezone:/etc/timezone:ro
      - /etc/localtime:/etc/localtime:ro
{extra_mount}
    entrypoint: ["/bin/bash", "{entrypoint}"]
    cap_add:
      - NET_ADMIN
    privileged: true
{depends}
    networks:
      default:
        ipv4_address: {ipv4}
"""


def render_gnb_service(p: dict) -> str:
    extra_env = (
        f'      NCI: "{p["nci"]}"\n'
        f'      GNB_IP: "{p["gnb_ip"]}"'
    )
    return SERVICE_TMPL.format(
        name=p["gnb_name"],
        image=IMAGE,
        mcc=MCC, mnc=MNC, tac=TAC, amf_ip=AMF_IP,
        extra_env=extra_env,
        extra_mount="",
        entrypoint="/scripts/gnb-entrypoint.sh",
        depends="",
        ipv4=p["gnb_ip"],
    )


def render_ue_service(p: dict) -> str:
    extra_env = (
        f'      GNB_IP: "{p["gnb_ip"]}"\n'
        f'      UE_IMSI: "{p["imsi"]}"\n'
        f'      UE_KI: "{UE_KI}"\n'
        f'      UE_OP: "{UE_OP}"\n'
        f'      UE_AMF: "{UE_AMF}"\n'
        f'      UE_IMEI: "{UE_IMEI}"\n'
        f'      UE_IMEISV: "{UE_IMEISV}"'
    )
    extra_mount = ""
    depends = (
        f"    depends_on:\n"
        f"      - {p['gnb_name']}\n"
    )
    return SERVICE_TMPL.format(
        name=p["ue_name"],
        image=IMAGE,
        mcc=MCC, mnc=MNC, tac=TAC, amf_ip=AMF_IP,
        extra_env=extra_env,
        extra_mount="    devices:\n      - /dev/net/tun:/dev/net/tun\n",
        entrypoint="/scripts/ue-entrypoint.sh",
        depends=depends.rstrip("\n"),
        ipv4=p["ue_ip"],
    )


def render_compose(pairs: list[dict]) -> str:
    services = []
    for p in pairs:
        services.append(render_gnb_service(p))
        services.append(render_ue_service(p))
    return (
        "# Auto-generated by ueran.py - do not edit by hand.\n"
        "services:\n"
        + "".join(services)
        + "\nnetworks:\n"
        "  default:\n"
        "    external: true\n"
        f"    name: {NETWORK}\n"
    )


# ---------- MongoDB subscriber management ----------

_SUB_DOC_TMPL = """{{
  imsi: "{imsi}",
  security: {{ k: "{k}", amf: "{amf}", op: "{op}", opc: null }},
  ambr: {{ downlink: {{ value: 1, unit: 3 }},
          uplink:   {{ value: 1, unit: 3 }} }},
  slice: [{{
    sst: 1, default_indicator: true,
    session: [{{
      name: "internet", type: 3,
      ambr: {{ downlink: {{ value: 1, unit: 3 }},
              uplink:   {{ value: 1, unit: 3 }} }},
      qos: {{ index: 9,
              arp: {{ priority_level: 8,
                      pre_emption_capability: 1,
                      pre_emption_vulnerability: 1 }} }}
    }}]
  }}],
  schema_version: 1,
  access_restriction_data: 32
}}"""


def _mongosh(script: str, check: bool = True):
    """Run a mongosh script in the open5gs database.

    Writes the script to a temp file, copies it into the mongo container,
    and runs `mongosh --file`. Avoids the noisy `...` continuation prompts
    that appear when piping multi-line JS through stdin.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False
    ) as f:
        f.write(script)
        local_path = f.name
    remote_path = f"/tmp/{os.path.basename(local_path)}"
    try:
        run(["docker", "cp", local_path,
             f"{MONGO_CONTAINER}:{remote_path}"])
        run(
            ["docker", "exec", MONGO_CONTAINER,
             "mongosh", "--quiet", "open5gs", "--file", remote_path],
            check=check,
        )
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass
        subprocess.run(
            ["docker", "exec", MONGO_CONTAINER, "rm", "-f", remote_path],
            capture_output=True, text=True,
        )


def mongo_upsert_many(imsis: list[str]):
    """Batch-upsert all subscribers in one mongosh call (bulkWrite)."""
    ops = []
    for imsi in imsis:
        doc = _SUB_DOC_TMPL.format(imsi=imsi, k=UE_KI, op=UE_OP, amf=UE_AMF)
        ops.append(
            f'{{ updateOne: {{ filter: {{ imsi: "{imsi}" }}, '
            f'update: {{ $set: {doc} }}, upsert: true }} }}'
        )
    script = (
        "db.subscribers.bulkWrite([\n  "
        + ",\n  ".join(ops)
        + "\n], { ordered: false });\n"
    )
    _mongosh(script)


def mongo_delete_many(imsis: list[str]):
    """Batch-delete subscribers in one mongosh call."""
    quoted = ", ".join(f'"{i}"' for i in imsis)
    script = f'db.subscribers.deleteMany({{ imsi: {{ $in: [{quoted}] }} }});\n'
    _mongosh(script, check=False)


# ---------- commands ----------

def cmd_up(args):
    n = args.n
    if not (1 <= n <= MAX_PAIRS):
        sys.exit(f"error: n must be in [1, {MAX_PAIRS}], got {n}")

    batch_size = args.batch_size
    batch_delay = args.batch_delay
    if batch_size < 1:
        sys.exit(f"error: --batch-size must be >= 1, got {batch_size}")
    if batch_delay < 0:
        sys.exit(f"error: --batch-delay must be >= 0, got {batch_delay}")

    preflight(n)

    pairs = [pair(i) for i in range(1, n + 1)]
    GEN_DIR.mkdir(parents=True, exist_ok=True)

    COMPOSE_FILE.write_text(render_compose(pairs))
    print(f"wrote {COMPOSE_FILE}")

    PAIRS_FILE.write_text(json.dumps(pairs, indent=2) + "\n")
    print(f"wrote {PAIRS_FILE}")

    print(f"\nRegistering {n} subscribers in Open5GS MongoDB (batched)...")
    mongo_upsert_many([p["imsi"] for p in pairs])

    num_batches = (n + batch_size - 1) // batch_size
    print(f"\nStarting {n} pair(s) in {num_batches} batch(es) of up to "
          f"{batch_size} (delay {batch_delay}s between batches)...")
    for bi in range(num_batches):
        batch_pairs = pairs[bi * batch_size : (bi + 1) * batch_size]
        svcs = []
        for p in batch_pairs:
            svcs += [p["gnb_name"], p["ue_name"]]
        first_id = batch_pairs[0]["id"]
        last_id = batch_pairs[-1]["id"]
        print(f"\n[batch {bi + 1}/{num_batches}] pairs {first_id}..{last_id} "
              f"({len(batch_pairs)} pair(s))")
        run(["docker", "compose", "-f", str(COMPOSE_FILE),
             "up", "-d", *svcs])
        if bi < num_batches - 1 and batch_delay > 0:
            print(f"  sleeping {batch_delay}s before next batch...")
            time.sleep(batch_delay)

    print(f"\n{n} pair(s) launched. First/last few:")
    print(f"{'id':>4} {'IMSI':<16} {'gNB IP':<15} {'UE IP':<15} {'NCI':<14}")
    for p in pairs[:3] + (pairs[-3:] if n > 6 else []):
        print(f"{p['id']:>4} {p['imsi']:<16} {p['gnb_ip']:<15} "
              f"{p['ue_ip']:<15} {p['nci']:<14}")
    print("\nWatch a pair:   docker logs -f ntn_ue_1")
    print("Status:         python ueran.py status")
    print("Ping from UE:   python ueran.py ping <id>")


def cmd_down(args):
    if not COMPOSE_FILE.exists() and not PAIRS_FILE.exists():
        print("nothing to tear down")
        return

    if COMPOSE_FILE.exists():
        run(["docker", "compose", "-f", str(COMPOSE_FILE), "down"],
            check=False)

    if PAIRS_FILE.exists():
        pairs = json.loads(PAIRS_FILE.read_text())
        print(f"\nRemoving {len(pairs)} subscriber(s) from MongoDB (batched)...")
        mongo_delete_many([p["imsi"] for p in pairs])

    for f in (COMPOSE_FILE, PAIRS_FILE):
        if f.exists():
            f.unlink()
            print(f"removed {f}")


def cmd_status(args):
    if not PAIRS_FILE.exists():
        sys.exit("no pairs.json — nothing started")
    pairs = json.loads(PAIRS_FILE.read_text())
    print(f"{'id':>3} {'gNB state':<12} {'UE state':<12} {'UE TUN IP':<20} "
          f"{'IMSI':<16}")
    for p in pairs:
        gnb_state = docker_inspect(p["gnb_name"], "{{.State.Status}}") or "-"
        ue_state  = docker_inspect(p["ue_name"],  "{{.State.Status}}") or "-"
        tun_ip = "-"
        if ue_state == "running":
            r = subprocess.run(
                ["docker", "exec", p["ue_name"],
                 "ip", "-4", "-o", "addr", "show", "uesimtun0"],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout:
                # "3: uesimtun0 inet 192.168.100.2/32 scope global uesimtun0"
                parts = r.stdout.split()
                if len(parts) >= 4:
                    tun_ip = parts[3]
        print(f"{p['id']:>3} {gnb_state:<12} {ue_state:<12} "
              f"{tun_ip:<20} {p['imsi']:<16}")


def cmd_logs(args):
    if not PAIRS_FILE.exists():
        sys.exit("no pairs.json — nothing started")
    pairs = {p["id"]: p for p in json.loads(PAIRS_FILE.read_text())}
    if args.id not in pairs:
        sys.exit(f"pair {args.id} not found (known: {sorted(pairs)})")
    p = pairs[args.id]
    print(f"===== {p['gnb_name']} =====")
    run(["docker", "logs", "--tail", "50", p["gnb_name"]], check=False)
    print(f"\n===== {p['ue_name']} =====")
    run(["docker", "logs", "--tail", "50", p["ue_name"]], check=False)


def cmd_ping(args):
    if not PAIRS_FILE.exists():
        sys.exit("no pairs.json — nothing started")
    pairs = {p["id"]: p for p in json.loads(PAIRS_FILE.read_text())}
    if args.id not in pairs:
        sys.exit(f"pair {args.id} not found (known: {sorted(pairs)})")
    p = pairs[args.id]
    run(
        ["docker", "exec", p["ue_name"],
         "ping", "-I", "uesimtun0", "-c", str(args.count), args.target],
        check=False,
    )


# ---------- argparse ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_up = sub.add_parser("up", help="spawn N pairs + register subscribers")
    p_up.add_argument("-n", type=int, required=True,
                      help=f"number of pairs (1..{MAX_PAIRS})")
    p_up.add_argument("--batch-size", type=int, default=30,
                      help="pairs per startup batch (default: 30)")
    p_up.add_argument("--batch-delay", type=int, default=15,
                      help="seconds between batches (default: 15)")
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="tear down + deregister")
    p_down.set_defaults(func=cmd_down)

    p_status = sub.add_parser("status", help="list pairs and TUN IPs")
    p_status.set_defaults(func=cmd_status)

    p_logs = sub.add_parser("logs", help="show last 50 log lines for a pair")
    p_logs.add_argument("id", type=int)
    p_logs.set_defaults(func=cmd_logs)

    p_ping = sub.add_parser("ping", help="ping from a UE via its PDU session")
    p_ping.add_argument("id", type=int)
    p_ping.add_argument("--target", default="8.8.8.8")
    p_ping.add_argument("-c", "--count", type=int, default=4)
    p_ping.set_defaults(func=cmd_ping)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
