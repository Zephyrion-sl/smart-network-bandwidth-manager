#!/usr/bin/env bash
# Remove only the bandwidth-lab namespaces and leftover lab interfaces.
# Requires root: sudo ./scripts/cleanup_network.sh
#
# Safe to run more than once. Does not flush firewall rules and does not
# edit the host default route.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/topology.sh
source "${SCRIPT_DIR}/lib/topology.sh"

require_root "$@"
require_ip

destroy_topology
echo "Lab topology removed."
