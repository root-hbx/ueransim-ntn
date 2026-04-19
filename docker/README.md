# ntn-litesys UERANSIM Docker

Builds UERANSIM from the local `ntn-litesys/` source and runs gNB + UE containers
on the existing Open5GS 5GC network (`docker_open5gs_default`, 172.22.0.0/24).

## Prerequisites

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

## Layout

```
docker/
  Dockerfile             # multi-stage: builds from ../ (ntn-litesys root)
  docker-compose.yaml    # ntn_gnb (172.22.0.43) + ntn_ue (172.22.0.44)
  .env                   # PLMN, IPs, subscriber creds
  configs/{gnb,ue}.yaml  # config templates (placeholders replaced at runtime)
  scripts/{gnb,ue}-entrypoint.sh
```

## Build & run

```bash
cd ntn-litesys/docker
docker compose build      # first time only
docker compose up -d
```

## Verify

```bash
docker logs -f ntn_gnb         # expect: "NG Setup procedure is successful"
docker logs -f ntn_ue          # expect: "Registration procedure ... successful"
                               #         "PDU Session establishment is successful"

docker exec ntn_ue ip addr show uesimtun0   # IP in 192.168.100.0/24
docker exec ntn_ue ping -I uesimtun0 -c 4 8.8.8.8
docker exec ntn_ue /UERANSIM/build/nr-cli imsi-001011234567895 -e status
```

## Teardown

```bash
docker compose down
```

## Multi-pair mode (`ueran.py`)

For N independent UE-gNB pairs, use the top-level launcher at
`ntn-litesys/ueran.py` (not this directory). It generates a compose file
into `docker/generated/`, registers N subscribers in MongoDB, and
spawns `ntn_gnb_<i>` + `ntn_ue_<i>` (IPs `172.22.0.{80+i}` / `172.22.0.{110+i}`,
IMSI `0010100000000{i:02d}`, NCI `0x0000000{i:02x}`). Stop the single-pair
compose first — multi-pair reuses the same image.

```bash
cd ntn-litesys
(cd docker && docker compose down)   # stop single-pair

python ueran.py up -n 10             # spawn 10 pairs
python ueran.py status                # table of states + TUN IPs
python ueran.py logs 5                # last 50 lines for pair 5
python ueran.py ping 5                # ping 8.8.8.8 via ntn_ue_5's uesimtun0
python ueran.py down                  # tear down + deregister
```

IP layout (needs wider subnet — see below):
- gNB: `172.22.{1+(i-1)/254}.{1+(i-1)%254}` → `.1.1 .. .1.254 .. .2.1 ..`
- UE:  `172.22.{10+(i-1)/254}.{1+(i-1)%254}` → `.10.1 .. .10.254 .. .11.1 ..`
- IMSI: `00101000000{i:04d}` (up to 9999)

### One-time: expand `docker_open5gs_default` from /24 to /16

The default subnet `172.22.0.0/24` holds only ~200 usable IPs and is the hard
ceiling on pair count. Open5GS services (IPs in `172.22.0.x`) remain valid
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

### One-time per boot: raise host kernel thresholds

Two kernel limits bite at scale and must be bumped before any large-N run:

1. **ARP/neighbor cache** — default `gc_thresh3=1024`. Breaks at N ≳ 240.
   Later containers never reach PDU-session state (empty TUN IP in
   `ueran.py status`) and the host's own outbound network (`curl`, `ping`)
   hangs — recoverable only by `ueran.py down`. `dmesg` shows
   `neighbour: arp_cache: neighbor table overflow!` repeatedly.

2. **inotify instances** — default `fs.inotify.max_user_instances=128`.
   Breaks around N ≳ 300 because every containerd-shim/runc claims
   instances; `docker compose up` fails with "No space left on device"
   even though disk is fine.

Run once per boot (runtime-only, no persistent change to `/etc/sysctl.d`):

```bash
./scripts/host-prepare.sh         # from ntn-litesys/
```

The script sets gc_thresh3 to 16384 and inotify instances to 8192, which
covers N up to the 500-pair hard ceiling (see below). Monitor during launch:
```bash
watch -n 1 'ip -4 neigh | wc -l; cat /proc/sys/fs/inotify/max_user_instances'
sudo dmesg -wT | grep -iE 'neighbor table overflow|No space left|inotify'
```

### Open5GS config (tuned for multi-pair)

The in-repo Open5GS config is already provisioned with generous headroom:

| Knob | File | Value | Reason |
|---|---|---|---|
| `MAX_NUM_UE` | `open5gs-docker/.env` | 2048 | total UE cap |
| `UE_IPV4_INTERNET` | `open5gs-docker/.env` | `192.168.96.0/21` | UE IP pool = 2046 IPs |
| `global.max.peer` | `{amf,smf,upf}.yaml` | 2048 | NGAP + PFCP + SBI peer cap |

These are over-provisioned relative to the 500-pair hard ceiling below —
no harm, just room if the ceiling ever lifts. Changes require
`docker compose -f sa-deploy.yaml down && up -d` on the 5GC side;
MongoDB subscriber data survives since the volume is preserved.

### Hard ceiling: Linux bridge BR_MAX_PORTS = 1024

The per-bridge port table in the Linux kernel is a fixed 10-bit index
(`#define BR_PORT_BITS 10` in `net/bridge/br_private.h` → 1024 slots per
bridge). It is a compile-time constant — no sysctl, no module param.

One Docker network = one bridge. Each container's veth occupies one slot.
On `docker_open5gs_default`:

```
  15 Open5GS services  +  N pairs × 2 veth  ≤  1024
                                   ⇒  N ≤ 504
```

Beyond ~500 pairs, `docker compose up` fails with
`adding interface veth... to bridge br-... failed: exchange full`.
`MAX_PAIRS=500` in `ueran.py` reflects this physical limit; raising the
constant alone will not help.

Going past 500 requires splitting containers across two Docker bridges
(with AMF/UPF attached to both) — designed but not implemented.

## Notes

- `nr-gnb` uses SCTP for NGAP; if the host kernel lacks SCTP, run
  `sudo modprobe sctp` before `up`.
- Container names `ntn_gnb` / `ntn_ue` and IPs `.43`/`.44` are chosen so they
  don't collide with the existing OAI or open5gs-docker UERANSIM deployments.
- No NTN features — this is baseline E2E only.
