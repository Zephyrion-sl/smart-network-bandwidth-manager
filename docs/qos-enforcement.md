# QoS enforcement

`QoSManager` turns an allocation into Linux HTB qdiscs. `TcCommandBuilder` only builds argument arrays. `TcExecutor` runs those arrays with `subprocess` and `shell=False`. Nothing in this path is an API route.

A limit is enforced only when `enforced` is true. That flag is set only after every `tc` command exits 0. Dry-run prints the commands and leaves `enforced` false.

## Where the limit sits

`tc` shapes traffic leaving an interface. It does not shape ingress with HTB.

```text
client  -- veth --  bw-gw                         bw-wan
                     bwv-gw-b   egress HTB         upload is not shaped here
                     (download toward client B)

                     bwv-gw-wan egress HTB
                     filter match ip src <client>
                     (upload from that client)
```

Download is the path toward the client. Those frames leave the gateway on that client's own veth (`bwv-gw-a`, `bwv-gw-b`, `bwv-gw-c`). The veth has one client, so the HTB default class is that client's class. A `u32` filter also matches `ip dst <client>/32`.

Upload is the path from the client toward the simulated upstream. This lab does not use NAT, so the client source address is still present on `bwv-gw-wan`. One HTB qdisc on that interface has one class per client and a `u32` filter on `ip src`. An IFB device is not required for this topology. It would be required if the only place to see the client address were the LAN ingress, or if NAT had already replaced the source address.

Ingress policing is not used. A police filter can drop or mark, but it is not the classful share HTB provides.

## Qdisc, class, filter

A **qdisc** is the queue attached to a device. The root qdisc is HTB, handle `1:`:

```text
tc qdisc replace dev <iface> root handle 1: htb default <minor>
```

`replace` installs this qdisc or swaps an existing root qdisc. A second apply does not add a second root.

A **class** is a rate inside that qdisc. The parent class `1:1` holds the leaves. Each client leaf uses `rate = ceil` at the allocated bit rate, so the class cannot borrow above its allocation:

```text
tc class replace dev <iface> parent 1:1 classid 1:10 htb rate 20000000bit ceil 20000000bit
```

A **filter** chooses the class. `u32` matches one IPv4 address:

```text
tc filter replace dev <iface> parent 1: protocol ip prio 10 handle 10 u32 \
    match ip dst 192.168.100.11/32 flowid 1:10
```

`replace` updates the filter with that handle. The handle is the class minor, not a new number on every apply, so repeats do not pile up filters.

Class ids in `tc` are hexadecimal. Config `class_minor: 16` is class `1:10`. The HTB `default` value is the same hex minor. `1` and `0x999` are reserved for the parent and the WAN default class.

On the shared WAN interface, traffic that matches no client goes to class `1:999`. That class is also `rate = ceil`, at `default_class_bps` (1 Mbps in the example). It cannot borrow the clients' bandwidth. Gateway-originated packets use it because their source is `10.200.0.1`, not a client address.

The parent rate is the sum of the leaf rates, including that default class when it exists. HTB requires each child rate to be less than or equal to the parent rate.

## Units

The allocator and the manager speak **bits per second**.

| Value | tc argument |
|---|---|
| 20 Mbps | `20000000bit` |
| 50 Mbps | `50000000bit` |
| 30 Mbps | `30000000bit` |

`tc` defines `kbit` as 1000 bits and `mbit` as 1 000 000 bits. The commands pass `bit` so there is no kilo/kibi conversion. `kbps` would mean kilobytes per second and would overshoot by a factor of eight.

## Safety

- The process refuses any `tc` command that is not `ip netns exec <configured namespace> tc ...`.
- Every `dev` argument must be one of the interfaces in `config/qos.example.yaml`. The example lists only `bwv-gw-a`, `bwv-gw-b`, `bwv-gw-c`, and `bwv-gw-wan`.
- Bandwidth must be an integer from 1 through `max_bandwidth_bps`.
- Arguments with shell metacharacters are rejected. `shell=True` is not used.
- `clear_all_rules()` deletes the root qdisc only on those allowlisted devices. A missing qdisc is success.
- If an apply fails after a previous success, the previous plan is submitted again. `enforced` stays false for the failed plan.

## Commands

From the repository root, on the Linux machine, after `sudo ./scripts/setup_network.sh`:

```bash
sudo python -m bandwidth_manager.enforcement --config config/qos.example.yaml \
    apply-allocation client-a=20000000 client-b=50000000 client-c=30000000
sudo python -m bandwidth_manager.enforcement --config config/qos.example.yaml \
    apply-limit client-b 20000000
sudo python -m bandwidth_manager.enforcement --config config/qos.example.yaml clear
```

`--dry-run` prints the argv lines and does not call `tc`.

`apply_limit(client, bandwidth)`, `remove_limit(client)`, `apply_allocation(allocation)`, and `clear_all_rules()` are the methods on `QoSManager`. `bandwidth` and allocation values are bits per second. An allocation of 0 removes that client instead of installing a zero rate.

## Verifying with iperf3

`sudo ./scripts/test_qos.sh` builds nothing itself. It expects the namespace topology to be up. It measures client B with iperf3, installs a 20 Mbps ceiling, measures again, then deletes the qdiscs.

Pass rule, per direction (upload and download):

- Uncapped goodput is at least 1.5 times 20 Mbps. If the namespace is slower than that, the script stops and reports the run as inconclusive.
- Capped goodput is between 50% and 130% of 20 Mbps (10 Mbps to 26 Mbps).
- Capped goodput is also below 70% of the uncapped goodput.

TCP goodput sits below an HTB ceiling because of headers, ACKs, the token-bucket burst, and slow start. The script uses `iperf3 -t 10 -O 2` so the first two seconds are omitted. Do not expect a reading of exactly 20.000 Mbps.

Upload is client B sending to `10.200.0.2`. That exercises the WAN source filter. Download is `iperf3 -R`. That exercises the HTB qdisc on `bwv-gw-b`.

## Limitations

- These rules cap IP traffic at the gateway. They do not schedule Wi-Fi airtime.
- The upload filter is correct only while the client source address is unchanged. Adding NAT later requires a mark before NAT, or an IFB on the LAN side.
- `qdisc replace` rebuilds the tree. There is a short gap, and HTB counters reset.
- A leaf with `rate = ceil` will not use spare capacity. That is the cap, not a minimum guarantee. Minimums from the allocator are a separate input; this subsystem installs the numbers it is given.
- Shaping the bridge device `bw-lan` would miss forwarded frames. Forwarded frames leave through the port veth, which is why the download qdisc is on `bwv-gw-*`.
