"""Judge numbers that a live topology test measured.

Nothing in this module opens a socket, runs tc, or invents a counter.
Callers pass bits per second they read from iperf3 or from sysfs.
Exit status 1 is a failed check. Exit status 2 means the run cannot prove
enforcement, which must not be reported as a pass.
"""

import argparse
import json
import sys


class CheckError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def judge_cap(
    *,
    uncapped_bps: float,
    capped_bps: float,
    allocation_bps: float,
    min_ratio: float = 0.50,
    max_ratio: float = 1.30,
    drop_ratio: float = 0.70,
    uncap_factor: float = 1.5,
) -> str:
    """Require capped goodput to fall toward the allocation.

    The uncapped run must be fast enough that a ceiling could have been
    visible. Otherwise the result is inconclusive, not a pass.
    """

    if allocation_bps <= 0 or uncapped_bps < 0 or capped_bps < 0:
        raise CheckError(1, "goodput and allocation must be non-negative, and allocation must be positive")
    if uncapped_bps < uncap_factor * allocation_bps:
        raise CheckError(
            2,
            (
                f"INCONCLUSIVE: uncapped {uncapped_bps:.0f} bps is not fast enough "
                f"to show an allocation of {allocation_bps:.0f} bps"
            ),
        )
    low = min_ratio * allocation_bps
    high = max_ratio * allocation_bps
    ceiling = drop_ratio * uncapped_bps
    if not (low <= capped_bps <= high and capped_bps < ceiling):
        raise CheckError(
            1,
            (
                f"FAIL: uncapped {uncapped_bps:.0f} bps, capped {capped_bps:.0f} bps, "
                f"allocation {allocation_bps:.0f} bps; expected {low:.0f}..{high:.0f} "
                f"and below {ceiling:.0f}"
            ),
        )
    return (
        f"PASS: uncapped {uncapped_bps:.0f} bps fell to {capped_bps:.0f} bps "
        f"near allocation {allocation_bps:.0f} bps"
    )


def judge_clients(rows: list[dict[str, float]]) -> str:
    """Judge every client whose uncapped rate can show the ceiling."""

    if not rows:
        raise CheckError(1, "FAIL: no client measurements")
    judged = 0
    messages: list[str] = []
    for row in rows:
        try:
            messages.append(
                f"{row['client_id']}: "
                + judge_cap(
                    uncapped_bps=float(row["uncapped_bps"]),
                    capped_bps=float(row["capped_bps"]),
                    allocation_bps=float(row["allocation_bps"]),
                )
            )
            judged += 1
        except CheckError as exc:
            if exc.status != 2:
                raise
            messages.append(f"{row['client_id']}: {exc.message}")
    if judged < 1:
        raise CheckError(2, "INCONCLUSIVE: no client was fast enough before tc to prove a ceiling")
    return "\n".join(messages)


def judge_allocation(capacity_bps: int, shares: dict[str, int]) -> str:
    if capacity_bps <= 0:
        raise CheckError(1, "FAIL: capacity must be positive")
    if not shares:
        raise CheckError(1, "FAIL: allocation is empty")
    total = 0
    for client_id, share in shares.items():
        if not isinstance(share, int) or isinstance(share, bool) or share <= 0:
            raise CheckError(1, f"FAIL: share for {client_id} is not a positive int")
        total += share
    if total > capacity_bps:
        raise CheckError(1, f"FAIL: allocations sum to {total} bps, above capacity {capacity_bps} bps")
    return f"PASS: {len(shares)} shares sum to {total} bps, within capacity {capacity_bps} bps"


def judge_recovery(*, capped_bps: float, recovered_bps: float, allocation_bps: float) -> str:
    """After tc is removed, goodput must rise above the old ceiling."""

    if recovered_bps <= capped_bps:
        raise CheckError(
            1,
            f"FAIL: recovered {recovered_bps:.0f} bps did not rise above capped {capped_bps:.0f} bps",
        )
    if recovered_bps <= allocation_bps:
        raise CheckError(
            1,
            (
                f"FAIL: recovered {recovered_bps:.0f} bps is still within the removed "
                f"allocation {allocation_bps:.0f} bps"
            ),
        )
    return (
        f"PASS: recovered {recovered_bps:.0f} bps is above capped {capped_bps:.0f} bps "
        f"and above the removed allocation {allocation_bps:.0f} bps"
    )


def rates_close(monitor_bps: float, counter_bps: float, *, tolerance: float = 0.25) -> str:
    """MonitoringService throughput must agree with a raw sysfs delta."""

    if monitor_bps <= 0 or counter_bps <= 0:
        raise CheckError(2, "INCONCLUSIVE: a compared rate is not positive")
    gap = abs(monitor_bps - counter_bps) / max(monitor_bps, counter_bps)
    if gap > tolerance:
        raise CheckError(
            1,
            (
                f"FAIL: monitor {monitor_bps:.0f} bps disagrees with sysfs delta "
                f"{counter_bps:.0f} bps ({gap:.0%} > {tolerance:.0%})"
            ),
        )
    return f"PASS: monitor {monitor_bps:.0f} bps matches sysfs {counter_bps:.0f} bps within {tolerance:.0%}"


def throughput_bps(before_bytes: int, after_bytes: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        raise CheckError(2, "INCONCLUSIVE: elapsed time is not positive")
    if after_bytes < before_bytes:
        raise CheckError(2, "INCONCLUSIVE: byte counter went backwards")
    return (after_bytes - before_bytes) * 8 / elapsed_seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Judge live topology measurements. Does not generate them.")
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("judge-cap")
    cap.add_argument("--uncapped", type=float, required=True)
    cap.add_argument("--capped", type=float, required=True)
    cap.add_argument("--allocation", type=float, required=True)

    clients = sub.add_parser("judge-clients")
    clients.add_argument("--json", type=str, required=True)

    alloc = sub.add_parser("judge-allocation")
    alloc.add_argument("--capacity", type=int, required=True)
    alloc.add_argument("--json", type=str, required=True)

    recovery = sub.add_parser("judge-recovery")
    recovery.add_argument("--capped", type=float, required=True)
    recovery.add_argument("--recovered", type=float, required=True)
    recovery.add_argument("--allocation", type=float, required=True)

    rates = sub.add_parser("rates-close")
    rates.add_argument("--monitor", type=float, required=True)
    rates.add_argument("--counter", type=float, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "judge-cap":
            message = judge_cap(
                uncapped_bps=args.uncapped,
                capped_bps=args.capped,
                allocation_bps=args.allocation,
            )
        elif args.command == "judge-clients":
            message = judge_clients(json.loads(args.json))
        elif args.command == "judge-allocation":
            raw = json.loads(args.json)
            message = judge_allocation(args.capacity, {str(key): int(value) for key, value in raw.items()})
        elif args.command == "judge-recovery":
            message = judge_recovery(
                capped_bps=args.capped,
                recovered_bps=args.recovered,
                allocation_bps=args.allocation,
            )
        else:
            message = rates_close(args.monitor, args.counter)
    except CheckError as exc:
        print(exc.message, file=sys.stderr)
        return exc.status
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"FAIL: could not read measurements: {exc}", file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
