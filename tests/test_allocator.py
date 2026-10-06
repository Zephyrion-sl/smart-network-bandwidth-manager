"""Weighted max-min allocation. Inputs are numbers, not a network."""

import pytest

from bandwidth_manager.allocation import AllocationResult, ClientRequest, allocate


def _by_id(result: AllocationResult) -> dict[str, int]:
    return {row.client_id: row.allocated_bandwidth for row in result.allocations}


def _clients(*rows: tuple) -> list[ClientRequest]:
    parsed = []
    for row in rows:
        client_id, demand, weight = row[:3]
        minimum = row[3] if len(row) > 3 else 0
        maximum = row[4] if len(row) > 4 else None
        parsed.append(
            ClientRequest(
                id=client_id,
                demand=demand,
                weight=weight,
                min_bandwidth=minimum,
                max_bandwidth=maximum,
            )
        )
    return parsed


def assert_feasible(result: AllocationResult, capacity: int, clients: list[ClientRequest]) -> None:
    amounts = _by_id(result)
    assert set(amounts) == {client.id for client in clients}
    assert sum(amounts.values()) <= capacity
    for client in clients:
        amount = amounts[client.id]
        ceiling = client.demand if client.max_bandwidth is None else min(client.demand, client.max_bandwidth)
        assert 0 <= amount <= ceiling
        if client.demand == 0 or ceiling == 0:
            assert amount == 0


def test_equal_clients_split_capacity_in_half():
    clients = _clients(("a", 100, 1), ("b", 100, 1))
    result = allocate(100, clients)
    assert _by_id(result) == {"a": 50, "b": 50}
    assert result.guarantees_scaled is False
    assert_feasible(result, 100, clients)


def test_unequal_weights_share_the_surplus_by_weight():
    clients = _clients(("a", 1000, 2), ("b", 1000, 1))
    result = allocate(100, clients)
    # floor(100 * 2 / 3) = 66, floor(100 * 1 / 3) = 33, one leftover bit to "a".
    assert _by_id(result) == {"a": 67, "b": 33}
    assert_feasible(result, 100, clients)


def test_one_heavy_consumer_leaves_the_light_client_at_its_demand():
    clients = _clients(("heavy", 1000, 1), ("light", 10, 1))
    result = allocate(100, clients)
    assert _by_id(result) == {"heavy": 90, "light": 10}
    assert_feasible(result, 100, clients)


def test_one_idle_client_receives_nothing():
    clients = _clients(("busy", 1000, 1), ("idle", 0, 1, 40))
    result = allocate(100, clients)
    assert _by_id(result) == {"busy": 100, "idle": 0}
    assert_feasible(result, 100, clients)


def test_capacity_shortage_scales_minimums_that_do_not_fit():
    clients = _clients(("a", 100, 1, 60), ("b", 100, 1, 60))
    result = allocate(90, clients)
    assert result.guarantees_scaled is True
    assert _by_id(result) == {"a": 45, "b": 45}
    assert sum(_by_id(result).values()) == 90
    assert_feasible(result, 90, clients)


def test_minimum_guarantees_are_honored_and_weights_share_the_rest():
    clients = _clients(("a", 100, 1, 30), ("b", 100, 3, 30))
    result = allocate(100, clients)
    assert result.guarantees_scaled is False
    assert _by_id(result) == {"a": 40, "b": 60}
    assert_feasible(result, 100, clients)


def test_maximum_limit_frees_capacity_for_the_other_client():
    clients = _clients(("a", 100, 1, 0, 10), ("b", 100, 1))
    result = allocate(100, clients)
    assert _by_id(result) == {"a": 10, "b": 90}
    assert_feasible(result, 100, clients)


def test_zero_demand_allocates_zero_even_when_capacity_remains():
    clients = _clients(("a", 0, 5, 20), ("b", 0, 1))
    result = allocate(100, clients)
    assert _by_id(result) == {"a": 0, "b": 0}
    assert sum(_by_id(result).values()) == 0
    assert_feasible(result, 100, clients)


def test_total_demand_below_capacity_does_not_inflate_allocations():
    clients = _clients(("a", 10, 1), ("b", 20, 9))
    result = allocate(100, clients)
    assert _by_id(result) == {"a": 10, "b": 20}
    assert sum(_by_id(result).values()) == 30
    assert_feasible(result, 100, clients)


def test_total_demand_above_capacity_is_split_by_weight():
    clients = _clients(("a", 100, 1), ("b", 100, 1))
    result = allocate(50, clients)
    assert _by_id(result) == {"a": 25, "b": 25}
    assert sum(_by_id(result).values()) == 50
    assert_feasible(result, 50, clients)


def test_one_client_is_limited_by_demand_maximum_and_capacity():
    alone = _clients(("only", 40, 1, 10, 100))
    assert _by_id(allocate(100, alone)) == {"only": 40}

    capped = _clients(("only", 80, 1, 10, 30))
    assert _by_id(allocate(100, capped)) == {"only": 30}

    tight = _clients(("only", 80, 1, 10))
    assert _by_id(allocate(25, tight)) == {"only": 25}


def test_many_clients_share_an_exact_capacity():
    clients = _clients(*((f"c{i}", 100, 1) for i in range(10)))
    result = allocate(100, clients)
    amounts = _by_id(result)
    assert amounts == {f"c{i}": 10 for i in range(10)}
    assert sum(amounts.values()) == 100
    assert_feasible(result, 100, clients)


def test_many_clients_receive_leftover_bits_in_id_order():
    clients = _clients(*((f"c{i}", 100, 1) for i in range(10)))
    result = allocate(95, clients)
    amounts = _by_id(result)
    # 95 // 10 = 9, and the five lowest ids each receive one of the five leftover bits.
    assert amounts == {f"c{i}": (10 if i < 5 else 9) for i in range(10)}
    assert sum(amounts.values()) == 95
    assert_feasible(result, 95, clients)


def test_input_order_does_not_change_the_allocation():
    forward = _clients(("b", 1000, 1), ("a", 1000, 3), ("c", 5, 1))
    backward = list(reversed(forward))
    assert allocate(100, forward) == allocate(100, backward)


def test_repeating_the_same_inputs_is_stable():
    clients = _clients(("a", 80, 2, 10, 40), ("b", 5, 1, 0, 50), ("c", 0, 4, 30))
    assert allocate(60, clients) == allocate(60, clients)


def test_demand_below_the_minimum_is_not_raised_to_the_minimum():
    clients = _clients(("a", 5, 1, 30), ("b", 100, 1))
    result = allocate(100, clients)
    assert _by_id(result) == {"a": 5, "b": 95}
    assert result.guarantees_scaled is False


def test_minimum_above_the_maximum_is_cut_by_the_maximum():
    clients = _clients(("a", 100, 1, 50, 10), ("b", 100, 1))
    result = allocate(100, clients)
    assert _by_id(result) == {"a": 10, "b": 90}


def test_zero_capacity_allocates_nothing():
    clients = _clients(("a", 10, 1, 5), ("b", 10, 1, 5))
    result = allocate(0, clients)
    assert result.guarantees_scaled is True
    assert _by_id(result) == {"a": 0, "b": 0}


def test_empty_client_list_allocates_nothing():
    result = allocate(100, [])
    assert result.allocations == ()
    assert result.guarantees_scaled is False


@pytest.mark.parametrize(
    ("capacity", "clients"),
    [
        (-1, [("a", 1, 1)]),
        (10, [("a", -1, 1)]),
        (10, [("a", 1, 0)]),
        (10, [("a", 1, 1, -5)]),
        (10, [("a", 1, 1, 0, -1)]),
        (10, [("", 1, 1)]),
        (10, [("a", 1, 1), ("a", 2, 1)]),
    ],
)
def test_invalid_inputs_are_rejected(capacity, clients):
    with pytest.raises((ValueError, TypeError)):
        allocate(capacity, _clients(*clients))


def test_bool_is_not_accepted_as_bandwidth():
    with pytest.raises(TypeError):
        allocate(True, _clients(("a", 1, 1)))
