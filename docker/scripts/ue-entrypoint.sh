#!/bin/bash
set -euo pipefail

# Ensure /dev/net/tun exists for the UE TUN interface
if [ ! -c /dev/net/tun ]; then
    mkdir -p /dev/net
    mknod /dev/net/tun c 10 200 || true
    chmod 600 /dev/net/tun || true
fi

# Give gNB time to complete NG Setup with AMF
echo "[ue-entrypoint] waiting 5s for gNB to register with AMF..."
sleep 5

CFG=/UERANSIM/config/ue.yaml
mkdir -p /UERANSIM/config
cp /configs/ue.yaml "$CFG"

# Order matters: replace longer keys first so we don't mangle IMEISV/IMEI etc.
sed -i "s|UE_IMEISV|${UE_IMEISV}|g" "$CFG"
sed -i "s|UE_IMEI|${UE_IMEI}|g"     "$CFG"
sed -i "s|UE_IMSI|${UE_IMSI}|g"     "$CFG"
sed -i "s|UE_KI|${UE_KI}|g"         "$CFG"
sed -i "s|UE_OP|${UE_OP}|g"         "$CFG"
sed -i "s|UE_AMF|${UE_AMF}|g"       "$CFG"
sed -i "s|MCC|${MCC}|g"             "$CFG"
sed -i "s|MNC|${MNC}|g"             "$CFG"
sed -i "s|GNB_IP|${GNB_IP}|g"       "$CFG"

echo "===== UE Config ====="
cat "$CFG"
echo "====================="

cd /UERANSIM/build
exec ./nr-ue -c "$CFG"
