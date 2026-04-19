# UERANSIM NTN-LiteSys

Builds UERANSIM from the local `ntn-litesys/` source and runs gNB + UE containers
on the existing Open5GS 5GC network (`docker_open5gs_default`, 172.22.0.0/24).

## Quick Start

```sh
# Daily Use
# 5gc
cd open5gs-docker && docker compose -f sa-deploy.yaml down
cd open5gs-docker && docker compose -f sa-deploy.yaml up -d
# kernel for network functions
cd ntn-litesys
./scripts/host-prepare.sh
# large-scale test
python ueran.py up -n 500 --batch-size 20 --batch-delay 20
```

(1) Prerequisites

1. Open5GS 5GC is running:
   ```bash
   cd ../../open5gs-docker && docker compose -f sa-deploy.yaml up -d
   ```
2. The subscriber IMSI `001011234567895` is registered in Open5GS MongoDB
   (already provisioned by the default open5gs-docker setup).
3. The existing UERANSIM containers (`nr_gnb`, `nr_ue`) are stopped — they
   share the same IMSI:
   ```bash
   cd ../../open5gs-docker && docker compose -f nr-gnb.yaml -f nr-ue.yaml down
   ```

(2) Build & run

```bash
cd ntn-litesys/docker
docker compose build      # first time only
docker compose up -d
```

(3) Verify

```bash
docker logs -f ntn_gnb         # expect: "NG Setup procedure is successful"
docker logs -f ntn_ue          # expect: "Registration procedure ... successful"
                               #         "PDU Session establishment is successful"

docker exec ntn_ue ip addr show uesimtun0   # IP in 192.168.100.0/24
docker exec ntn_ue ping -I uesimtun0 -c 4 8.8.8.8
docker exec ntn_ue /UERANSIM/build/nr-cli imsi-001011234567895 -e status
```

(3) Teardown

```bash
docker compose down
```

## Modifications

Compared to existing works like UERANSIM, OpenAirInterface, Open5GS, we have made the following modifications to ensure support for large-scale UE/gNB emulations:

### (1) Multi-pair mode

For N independent UE-gNB pairs, use the top-level launcher at
`ntn-litesys/ueran.py`.

It generates a compose file into `docker/generated/`, registers N subscribers in MongoDB, and
spawns `ntn_gnb_<i>` + `ntn_ue_<i>` (IPs `172.22.0.{80+i}` / `172.22.0.{110+i}`,
IMSI `0010100000000{i:02d}`, NCI `0x0000000{i:02x}`).

```bash
cd ntn-litesys

python ueran.py up -n 10              # spawn 10 pairs
python ueran.py status                # table of states + TUN IPs
python ueran.py logs 5                # last 50 lines for pair 5
python ueran.py ping 5                # ping 8.8.8.8 via ntn_ue_5's uesimtun0
python ueran.py down                  # tear down + deregister from 5GC
```

IP layout (needs wider subnet — see below):

- gNB: `172.22.{1+(i-1)/254}.{1+(i-1)%254}` → `.1.1 .. .1.254 .. .2.1 ..`
- UE:  `172.22.{10+(i-1)/254}.{1+(i-1)%254}` → `.10.1 .. .10.254 .. .11.1 ..`
- IMSI: `00101000000{i:04d}` (up to 9999)

### (2) Expand `docker_open5gs_default` from /24 to /16

The default subnet `172.22.0.0/24` holds only ~200 usable IPs and is the hard
ceiling on pair count. 

Open5GS services (IPs in `172.22.0.x`) remain valid
under a wider `172.22.0.0/16`, so this is non-breaking for the 5GC — but the
network must be recreated.

```bash
cd ~/paper/SaTrinity/open5gs-docker

# 1. Stop everything on the network (5GC + any UERANSIM + ntn-litesys pairs)
docker compose -f sa-deploy.yaml down

# 2. Edit .env: change  TEST_NETWORK=172.22.0.0/24  →  TEST_NETWORK=172.22.0.0/16
sed -i 's|^TEST_NETWORK=.*|TEST_NETWORK=172.22.0.0/16|' .env

# 3. Remove the old network (compose will recreate it with the new subnet)
docker network rm docker_open5gs_default

# 4. Bring Open5GS back up — subscriber MongoDB data is preserved in the volume
docker compose -f sa-deploy.yaml up -d

# 5. Verify
docker network inspect docker_open5gs_default \
  --format '{{(index .IPAM.Config 0).Subnet}}'   # should print 172.22.0.0/16
```

After that, `ueran.py` supports up to 500 pairs (its internal cap — see
"Hard ceiling" below). `ueran.py` runs a preflight check and will print
exactly these steps if the subnet is still too narrow.

### (3) One-time per boot: raise host kernel thresholds

Two kernel limits bite at scale and must be bumped before any large-N run:

1. **ARP/neighbor cache** 
   — Default `gc_thresh3=1024`. Breaks at N ≳ 240.
   - Later containers never reach PDU-session state (empty TUN IP in
   `ueran.py status`).
   - `dmesg` shows `neighbour: arp_cache: neighbor table overflow!` repeatedly.

2. **inotify instances** 
   — Default `fs.inotify.max_user_instances=128`.
   - Breaks around N ≳ 300 because every containerd-shim/runc claims
   instances
   - `docker compose up` fails with "No space left on device" even though disk is fine.

Run once per boot (runtime-only, no persistent change to `/etc/sysctl.d`):

```bash
./scripts/host-prepare.sh         # from ntn-litesys/
```

The script sets gc_thresh3 to 16384 and inotify instances to 8192, which
covers N up to the 500-pair hard ceiling (see below). Monitor during launch:

```bash
sudo dmesg -wT | grep -iE 'neighbor table overflow|No space left|inotify'
```

### (4) Tuned Open5GS configs

In-repo Open5GS config is already provisioned with generous headroom:

| Knob | File | Value | Reason |
|---|---|---|---|
| `MAX_NUM_UE` | `open5gs-docker/.env` | 2048 | total UE cap |
| `UE_IPV4_INTERNET` | `open5gs-docker/.env` | `192.168.96.0/21` | UE IP pool = 2046 IPs |
| `global.max.peer` | `{amf,smf,upf}.yaml` | 2048 | NGAP + PFCP + SBI peer cap |

### (5) Hard ceiling: Linux bridge BR_MAX_PORTS

> We didn't solve this kernel "issue", just bypass, since it's not relevant to our "DTCN Emulation" task.

In linux kernel, per-bridge port table is a fixed 10-bit index, and **MAX at 1024** (`#define BR_PORT_BITS 10`, `2<<10=1024`).

It is a compile-time constant — no sysctl, no module param.

On `docker_open5gs_default`:

```
15 Open5GS services  +  N pairs × 2 veth  ≤  1024
⇒  N ≤ 504
```

Beyond ~500 pairs, `docker compose up` fails with
`adding interface veth... to bridge br-... failed: exchange full`.

`MAX_PAIRS=500` in `ueran.py` reflects this physical limit; raising the
constant alone will **NOT** help!!!

## Q&A

- `nr-gnb` uses SCTP for NGAP; if the host kernel lacks SCTP, run
  `sudo modprobe sctp` before `up`.
- Container names `ntn_gnb` / `ntn_ue` and IPs `.43`/`.44` are chosen so they
  don't collide with the existing OAI or open5gs-docker UERANSIM deployments.
- No NTN features — this is baseline E2E only.
