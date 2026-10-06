# Network topology

This is the Linux networking foundation for the bandwidth project. It builds a gateway, three LAN clients, and a simulated upstream network inside network namespaces. It does not configure bandwidth control, NAT, a database, or the API.

Every command below is run as root. From the repository root:

```bash
sudo ./scripts/setup_network.sh
sudo ./scripts/show_network.sh
sudo ./scripts/test_connectivity.sh
sudo ./scripts/cleanup_network.sh
```

Packages on Debian or Ubuntu: `iproute2` and `iputils-ping`. The scripts do not install packages and do not change apt sources.

## Layout

```text
bw-client-a 192.168.100.11/24
bw-client-b 192.168.100.12/24
bw-client-c 192.168.100.13/24
        |            |            |
     bwv-a        bwv-b        bwv-c          (veth, client end)
        |            |            |
     bwv-gw-a     bwv-gw-b     bwv-gw-c       (veth, gateway end)
        \            |            /
         \           |           /
          +------- bw-lan -------+            bridge in bw-gw
                  192.168.100.1/24
                       |
                    bw-gw
              IPv4 forwarding = 1
                       |
                 bwv-gw-wan 10.200.0.1/30
                       |
                 bwv-wan    10.200.0.2/30
                       |
                    bw-wan                          simulated upstream
         route 192.168.100.0/24 via 10.200.0.1
```

The bridge is the LAN switch. It lives inside the gateway namespace, so client-to-client frames stay off the host. The host keeps its own interfaces, default route, and firewall.

| Namespace | Role | Interface | Address |
|---|---|---|---|
| `bw-client-a` | Client A | `bwv-a` | `192.168.100.11/24` |
| `bw-client-b` | Client B | `bwv-b` | `192.168.100.12/24` |
| `bw-client-c` | Client C | `bwv-c` | `192.168.100.13/24` |
| `bw-gw` | Gateway LAN | `bw-lan` | `192.168.100.1/24` |
| `bw-gw` | Gateway WAN | `bwv-gw-wan` | `10.200.0.1/30` |
| `bw-wan` | Upstream host | `bwv-wan` | `10.200.0.2/30` |

Client default route: `default via 192.168.100.1`.

Gateway default route, inside `bw-gw` only: `default via 10.200.0.2`.

Upstream return route, inside `bw-wan` only: `192.168.100.0/24 via 10.200.0.1`.

`10.200.0.0/30` has two usable addresses, `.1` and `.2`. That is the point-to-point link between the gateway and the simulated upstream.

## Why there is no NAT

Clients reach `10.200.0.2` by normal forwarding. The upstream namespace has a static route back to `192.168.100.0/24`, so reply packets have a path. NAT is not required for this topology, and the scripts never call `iptables` or `nft`.

A later milestone that attaches this gateway to a real upstream which does not route `192.168.100.0/24` would need masquerade on the gateway WAN interface. That is a separate, explicit change. It is not part of this lab.

## Safety

The scripts will:

- create and delete only `bw-gw`, `bw-wan`, `bw-client-a`, `bw-client-b`, and `bw-client-c`
- create veth pairs only with the names in the table above, with both ends placed directly in lab namespaces so those interfaces never exist on the host
- enable `net.ipv4.ip_forward` only inside `bw-gw`
- record the host default route and the host forwarding flag in `/run/bw-net-foundation` and refuse to finish setup if either changes
- stop if a lab namespace or a lab interface name is already present

The scripts will not:

- replace the host default route
- write the host `ip_forward` sysctl
- flush or edit iptables or nftables rules
- delete a host interface unless the name matches this lab and the device type is `veth` or, for `bw-lan`, `bridge`

`setup_network.sh` rolls the partial lab back if a command fails.

## Commands, in the order setup runs them

Human-readable equivalents use `sysctl`. The script writes `/proc/sys/net/ipv4/ip_forward` inside the namespace so it does not depend on the `sysctl` binary. Both do the same thing.

```bash
sudo ip netns add bw-gw
sudo ip netns add bw-wan
sudo ip netns add bw-client-a
sudo ip netns add bw-client-b
sudo ip netns add bw-client-c

sudo ip netns exec bw-gw ip link set lo up
sudo ip netns exec bw-wan ip link set lo up
sudo ip netns exec bw-client-a ip link set lo up
sudo ip netns exec bw-client-b ip link set lo up
sudo ip netns exec bw-client-c ip link set lo up

# Equivalent: sudo ip netns exec bw-gw sysctl -w net.ipv4.ip_forward=1
sudo ip netns exec bw-gw sh -c 'echo 1 > /proc/sys/net/ipv4/ip_forward'

sudo ip link add bwv-wan netns bw-wan type veth peer name bwv-gw-wan netns bw-gw
sudo ip netns exec bw-wan ip link set bwv-wan up
sudo ip netns exec bw-wan ip addr add 10.200.0.2/30 dev bwv-wan
sudo ip netns exec bw-gw ip link set bwv-gw-wan up
sudo ip netns exec bw-gw ip addr add 10.200.0.1/30 dev bwv-gw-wan
sudo ip netns exec bw-wan ip route add 192.168.100.0/24 via 10.200.0.1
sudo ip netns exec bw-gw ip route add default via 10.200.0.2

sudo ip netns exec bw-gw ip link add bw-lan type bridge
sudo ip netns exec bw-gw ip link set bw-lan type bridge stp_state 0 forward_delay 0
sudo ip netns exec bw-gw ip link set bw-lan up
sudo ip netns exec bw-gw ip addr add 192.168.100.1/24 dev bw-lan
```

STP is turned off and the forward delay is set to 0 so bridge ports forward immediately. With STP left on, a new port can spend about 30 seconds learning and the first pings fail.

For client A, then the same pattern for B (`bwv-b`, `bwv-gw-b`, `192.168.100.12`) and C (`bwv-c`, `bwv-gw-c`, `192.168.100.13`):

The veth is created with both ends already in their namespaces, so it never shows up as a host interface for NetworkManager to claim.

```bash
sudo ip link add bwv-a netns bw-client-a type veth peer name bwv-gw-a netns bw-gw
sudo ip netns exec bw-gw ip link set bwv-gw-a master bw-lan
sudo ip netns exec bw-gw ip link set bwv-gw-a up
sudo ip netns exec bw-client-a ip link set bwv-a up
sudo ip netns exec bw-client-a ip addr add 192.168.100.11/24 dev bwv-a
sudo ip netns exec bw-client-a ip route add default via 192.168.100.1
```

Cleanup deletes those five namespaces and then deletes a leftover host interface only when the name and device type match this lab:

```bash
sudo ip netns delete bw-client-a
sudo ip netns delete bw-client-b
sudo ip netns delete bw-client-c
sudo ip netns delete bw-wan
sudo ip netns delete bw-gw
# only if that device is still on the host and is a veth or the bw-lan bridge:
# sudo ip link delete dev <lab-interface>
```

Running cleanup when nothing exists is a no-op that prints `already absent`.

## Inspect

```bash
sudo ./scripts/show_network.sh
```

That runs, inside each present namespace:

```bash
sudo ip netns exec <ns> ip -br link
sudo ip netns exec <ns> ip -br addr
sudo ip netns exec <ns> ip route
```

Inside `bw-gw` it also shows bridge ports, `/proc/sys/net/ipv4/ip_forward`, and `ip neigh show`. It then prints the host default route. That host route must still be whatever it was before setup, and it must not use `bwv-*` or `bw-lan`.

Illustrative gateway addresses after a successful setup (indexes and MAC addresses will differ):

```text
bw-lan           UP    192.168.100.1/24
bwv-gw-wan       UP    10.200.0.1/30
bwv-gw-a         UP
bwv-gw-b         UP
bwv-gw-c         UP
```

Gateway routes:

```text
default via 10.200.0.2 dev bwv-gw-wan
10.200.0.0/30 dev bwv-gw-wan proto kernel scope link
192.168.100.0/24 dev bw-lan proto kernel scope link
```

Gateway forwarding value: `1`.

## Connectivity test

```bash
sudo ./scripts/test_connectivity.sh
```

Expected output:

```text
[pass] Client bw-client-a pings gateway 192.168.100.1
[pass] Client bw-client-a has default route via 192.168.100.1
[pass] Client bw-client-a (192.168.100.11) reaches upstream 10.200.0.2
[pass] Upstream reaches client 192.168.100.11
[pass] Client bw-client-b pings gateway 192.168.100.1
[pass] Client bw-client-b has default route via 192.168.100.1
[pass] Client bw-client-b (192.168.100.12) reaches upstream 10.200.0.2
[pass] Upstream reaches client 192.168.100.12
[pass] Client bw-client-c pings gateway 192.168.100.1
[pass] Client bw-client-c has default route via 192.168.100.1
[pass] Client bw-client-c (192.168.100.13) reaches upstream 10.200.0.2
[pass] Upstream reaches client 192.168.100.13
[pass] Gateway IPv4 forwarding is enabled
[pass] Upstream has a route to 192.168.100.0/24 via 10.200.0.1
[pass] Host default route and ip_forward are unchanged
All connectivity checks passed.
```

A client ping to `10.200.0.2` is the forwarding test. The echo request is routed by `bw-gw`, and the echo reply is routed back. The upstream ping to each client checks the return route in the other direction.

Destroy and create the lab twice:

```bash
sudo ./scripts/test_connectivity.sh --recreate
```

That runs cleanup, setup, the checks, cleanup, setup, and the checks again. On success the topology is left up. Expected closing line:

```text
Topology was destroyed and recreated successfully. It is up now.
```

Manual checks, if you want to see the packets yourself:

```bash
sudo ip netns exec bw-client-a ping -c 2 192.168.100.1
sudo ip netns exec bw-client-b ping -c 2 192.168.100.1
sudo ip netns exec bw-client-c ping -c 2 192.168.100.1
sudo ip netns exec bw-client-a ping -c 2 10.200.0.2
sudo ip netns exec bw-gw cat /proc/sys/net/ipv4/ip_forward
```

## Troubleshooting

| Symptom | What it usually means |
|---|---|
| `network namespaces require root` | The script was started without root. Run it with `sudo`. |
| `Refusing to overwrite` | A previous lab is still present, or a host interface already uses a lab name. Run `sudo ./scripts/cleanup_network.sh` and inspect any warning about an interface that was left in place. |
| `ping: command not found` | Install `iputils-ping`. |
| `$'\r': command not found` | The script has Windows line endings. Convert the `scripts` directory to LF and run it again. |
| Client can ping `192.168.100.1` but not `10.200.0.2` | Forwarding is off in `bw-gw`, or `bw-wan` has no route to `192.168.100.0/24`. `show_network.sh` prints both. |
| `Network is unreachable` from a client | The client default route is missing. Setup installs `default via 192.168.100.1`. |
| First pings fail and later succeed | STP was left on. Setup sets `stp_state 0`. If you built the bridge by hand, set that before testing. |
| `setup failed` and the lab disappeared | The error handler deleted the partial topology. Fix the reported line and run setup again. |
| Cleanup says a namespace could not be deleted | A process is still inside it. `sudo ip netns pids bw-gw` shows the pids. Stop those processes and run cleanup again. |
| Host lost its default route | These scripts do not edit host routes. Compare `ip -4 route show default` with `/run/bw-net-foundation/host-default-route.txt` while the lab is up. If they differ, setup's own check should already have failed and rolled back. |
| `RTNETLINK answers: File exists` on route add | Setup was run twice without cleanup. Cleanup, then setup. |
| `ip link add ... netns ...` says not supported or `Invalid argument` | iproute2 is too old to create a veth directly inside a namespace. Use Ubuntu 20.04+ or Debian 11+, or any iproute2 that accepts `peer name ... netns`. |

Interface names are at most 15 characters. Do not lengthen `bwv-gw-wan` or the client veth names without checking that limit.
