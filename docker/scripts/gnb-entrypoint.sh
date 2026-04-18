#!/bin/bash
set -euo pipefail

CFG=/UERANSIM/config/gnb.yaml
mkdir -p /UERANSIM/config
cp /configs/gnb.yaml "$CFG"

sed -i "s|MCC|${MCC}|g"         "$CFG"
sed -i "s|MNC|${MNC}|g"         "$CFG"
sed -i "s|TAC|${TAC}|g"         "$CFG"
sed -i "s|GNB_IP|${GNB_IP}|g"   "$CFG"
sed -i "s|AMF_IP|${AMF_IP}|g"   "$CFG"

echo "===== gNB Config ====="
cat "$CFG"
echo "======================"

cd /UERANSIM/build
exec ./nr-gnb -c "$CFG"
