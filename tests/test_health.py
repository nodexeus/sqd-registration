"""Judging whether a worker has stopped earning.

The costly mistake here is a false alarm. Workers are paid on a rotation, so a
worker missing from any single distribution is usually just waiting its turn --
call that an outage and an operator goes and restarts a healthy node. The
second mistake is the opposite: blaming a node for an event that took its whole
cohort out.
"""

from tools import health


def epochs_from(rewards_by_epoch):
    """rewards_by_epoch: {from_block: {worker_id: reward}}"""
    return [
        {"from": block, "rewards": rewards}
        for block, rewards in sorted(rewards_by_epoch.items())
    ]


# Four-slot rotation, 600-block epochs: each worker is paid every 2400 blocks.
def rotating(n_epochs=8, workers=(1, 2, 3, 4), start=1000, paid=10):
    out = {}
    for i in range(n_epochs):
        block = start + i * 600
        out[block] = {workers[i % len(workers)]: paid}
    return out


def test_the_rotation_period_is_derived_not_assumed():
    """Hardcoding the cycle would misreport every worker the day it changes."""
    epochs = epochs_from(rotating(n_epochs=12))
    history = health.build_history(epochs)

    assert health.rotation_period(epochs, history) == 2400


def test_a_worker_waiting_its_turn_is_not_an_outage():
    """The false alarm this tool exists to avoid: absent from 3 of every 4
    distributions is exactly what a healthy worker looks like."""
    epochs = epochs_from(rotating(n_epochs=12))
    history = health.build_history(epochs)
    period = health.rotation_period(epochs, history)

    verdict = health.assess(1, history, epochs, period)

    assert verdict["state"] == health.EARNING
    assert verdict["appearances"] == 3      # seen in 3 of 12 distributions
    assert verdict["zeros"] == 0


def test_zero_payouts_since_the_last_real_one_are_counted():
    data = rotating(n_epochs=12)
    for block, rewards in data.items():
        if 1 in rewards and block >= 1000 + 4 * 600:
            rewards[1] = 0          # earns once, then zero on later turns
    epochs = epochs_from(data)
    history = health.build_history(epochs)
    period = health.rotation_period(epochs, history)

    verdict = health.assess(1, history, epochs, period)

    assert verdict["state"] == health.ZEROED
    assert verdict["zeros"] == 2
    assert verdict["last_paid"] == 1000


def test_missing_its_own_slot_reads_as_dropped_not_merely_zero():
    """Being left out of the payout set is a different state from being paid
    zero, and an operator chasing it needs to know which."""
    data = rotating(n_epochs=12)
    for block in list(data):
        if 1 in data[block] and block > 1000:
            del data[block][1]      # paid once, then absent from its own slots
    epochs = epochs_from(data)
    history = health.build_history(epochs)
    period = health.rotation_period(epochs, history)

    verdict = health.assess(1, history, epochs, period)

    assert verdict["state"] == health.DROPPED
    assert verdict["missed"] == 2


def test_a_worker_never_seen_is_reported_as_such():
    epochs = epochs_from(rotating())
    history = health.build_history(epochs)

    assert health.assess(999, history, epochs, 2400)["state"] == health.UNSEEN


def test_a_cohort_wide_outage_is_distinguished_from_a_lone_failure():
    """Same symptom, opposite conclusion: one node to inspect, or somebody
    else's incident."""
    # Slot A: workers 1 and 5 share it; both stop. Slot B: worker 2 keeps going.
    data = {}
    for i in range(8):
        block = 1000 + i * 600
        if i % 4 == 0:
            data[block] = {1: 10 if i == 0 else 0, 5: 10 if i == 0 else 0}
        elif i % 4 == 1:
            data[block] = {2: 10}
        else:
            data[block] = {3: 10}
    epochs = epochs_from(data)
    history = health.build_history(epochs)
    period = health.rotation_period(epochs, history)
    verdicts = {w: health.assess(w, history, epochs, period) for w in history}

    slot = verdicts[1]["slot"]
    serving, unwell, retired = health.cohort_state(
        history, epochs, period, slot, verdicts
    )

    assert (serving, unwell, retired) == (2, 2, 0)   # the whole cohort is out
    assert verdicts[2]["state"] == health.EARNING


def test_cohort_members_lists_only_the_requested_states():
    """The count printed above the list and the list itself must agree, or it
    reads as a discrepancy in the evidence."""
    data = {}
    for i in range(8):
        block = 1000 + i * 600
        if i % 4 == 0:
            # 1 stops dead, 5 is paid zero, 6 keeps earning -- same slot.
            data[block] = {1: 10 if i == 0 else 0,
                           5: 10 if i < 4 else 0,
                           6: 10}
        else:
            data[block] = {2 + (i % 3): 10}
    epochs = epochs_from(data)
    history = health.build_history(epochs)
    period = health.rotation_period(epochs, history)
    verdicts = {w: health.assess(w, history, epochs, period) for w in history}
    slot = verdicts[1]["slot"]

    both = health.cohort_members(
        history, epochs, period, slot, verdicts,
        {health.ZEROED, health.DROPPED},
    )
    serving, unwell, retired = health.cohort_state(
        history, epochs, period, slot, verdicts
    )

    assert both == [1, 5]              # 6 is earning, so excluded
    assert unwell == len(both)         # the count matches what gets listed
    assert verdicts[6]["state"] == health.EARNING


def test_workers_that_stopped_at_different_times_are_not_one_event():
    """The defect this replaces: counting how many workers share a state and
    concluding they went out together. Workers drop out continuously, so a slot
    accumulates unrelated casualties."""
    data = {}
    for i in range(12):
        block = 1000 + i * 600
        if i % 4 == 0:
            paid = {}
            # 1 stops after its first turn, 5 keeps going until much later.
            paid[1] = 10 if i == 0 else 0
            paid[5] = 10 if i < 8 else 0
            data[block] = paid
        else:
            data[block] = {2: 10}
    epochs = epochs_from(data)
    history = health.build_history(epochs)
    period = health.rotation_period(epochs, history)
    verdicts = {w: health.assess(w, history, epochs, period) for w in history}

    groups = health.cutoff_groups(history, epochs, period,
                                  verdicts[1]["slot"], verdicts)

    # Both are not earning, but they stopped in different periods.
    assert sorted(len(g) for g in groups.values()) == [1, 1]
    assert len(groups) == 2
    assert groups[verdicts[1]["last_paid"]] == [1]


def test_workers_that_stopped_in_the_same_period_group_together():
    """The signal that does mean something: one cutoff, several workers."""
    data = {}
    for i in range(12):
        block = 1000 + i * 600
        if i % 4 == 0:
            data[block] = {w: (10 if i == 0 else 0) for w in (1, 5, 7)}
        else:
            data[block] = {2: 10}
    epochs = epochs_from(data)
    history = health.build_history(epochs)
    period = health.rotation_period(epochs, history)
    verdicts = {w: health.assess(w, history, epochs, period) for w in history}

    groups = health.cutoff_groups(history, epochs, period,
                                  verdicts[1]["slot"], verdicts)

    assert len(groups) == 1
    assert groups[1000] == [1, 5, 7]


def test_a_deregistered_worker_is_not_counted_as_a_casualty():
    """A deregistered worker stops earning because its operator asked it to.
    Counting one inflates the total and, worse, puts a wrong peer ID in a
    report sent to somebody else."""
    data = {}
    for i in range(12):
        block = 1000 + i * 600
        if i % 4 == 0:
            # 1 broke; 9 was deregistered on purpose; 6 is fine.
            data[block] = {1: 10 if i == 0 else 0,
                           9: 10 if i == 0 else 0,
                           6: 10}
        else:
            data[block] = {2: 10}
    epochs = epochs_from(data)
    history = health.build_history(epochs)
    period = health.rotation_period(epochs, history)
    verdicts = {w: health.assess(w, history, epochs, period) for w in history}
    slot = verdicts[1]["slot"]

    serving, unwell, retired = health.cohort_state(
        history, epochs, period, slot, verdicts, exclude={9}
    )
    listed = health.cohort_members(
        history, epochs, period, slot, verdicts,
        {health.ZEROED, health.DROPPED}, exclude={9},
    )
    groups = health.cutoff_groups(
        history, epochs, period, slot, verdicts, exclude={9}
    )

    assert (serving, unwell, retired) == (2, 1, 1)   # 9 in neither part of 2/1
    assert listed == [1]                             # and never named
    assert groups[1000] == [1]                       # nor treated as co-stopped
