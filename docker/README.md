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

## Notes

- `nr-gnb` uses SCTP for NGAP; if the host kernel lacks SCTP, run
  `sudo modprobe sctp` before `up`.
- Container names `ntn_gnb` / `ntn_ue` and IPs `.43`/`.44` are chosen so they
  don't collide with the existing OAI or open5gs-docker UERANSIM deployments.
- No NTN features — this is baseline E2E only.
