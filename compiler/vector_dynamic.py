"""
vector_dynamic.py -- Realisation-aware dynamic-operation model (R4.2.5).

Both vector clients need the same accounting, and after R4.2.5 that accounting
depends on WHICH realisation lowering picked, so it lives in one place rather than
being duplicated (and drifting) in each client.

    unrolled   the straight-line chunk body executes exactly once, so its emitted
               length IS the executed count. (For dot/reduction this reproduces
               R4.1's hand-derived `5*chunks + 4` and `4*chunks + 4` exactly --
               those constants are now derived from the emitted code instead of
               hardcoded.)

    compact    `chunks` iterations of the loop body, each paying the compare, the
               branch and the IV update that the unrolled form does not, plus the
               final failing exit test.

Both then add the scalar remainder: `body_ops * remainder`.

The gate the pipeline applies is unchanged -- DYNAMIC operations must fall. What
R4.2.5 changes is that the compact form's honest per-chunk overhead is now
counted, so a kernel whose compact form would not actually pay off is declined
rather than flattered by an unrolled-shaped estimate.
"""

# instructions executed by the exit test that fails and leaves the loop:
# IRLoadAddr + IRLoad + IRCondJump  (see vector_compact_loop.build_compact_chunk_loop)
_EXIT_TEST_OPS = 3


def model_realisation(plan, desc):
    """A DynamicModel for `plan`, whose `realisation` lowering has already set."""
    from vector_pipeline import DynamicModel
    body_ops = getattr(desc, 'body_inst_count', 0) or 1
    scalar_ops = body_ops * plan.trip
    real = plan.realisation or 'unrolled'
    if real.endswith('+peeled'):
        # the scalar tail LOOP is gone; the peeled straight-line tail executes
        # exactly once and its length is known exactly
        tail = getattr(plan, 'peel_len', 0) or 0
    else:
        tail = body_ops * plan.remainder

    if real.startswith('compact'):
        vector_ops = plan.chunks * plan.compact_per_iter + _EXIT_TEST_OPS + tail
    else:
        vector_ops = _emitted_len(plan) + tail
    return DynamicModel(scalar_ops, vector_ops, chunks=plan.chunks,
                        remainder=plan.remainder)


def _emitted_len(plan):
    """The unrolled body's exact instruction count. `unrolled_len` is set by the
    dot/reduction lowering; the elementwise lowering records the same thing as
    `body_len`."""
    n = getattr(plan, 'unrolled_len', 0) or 0
    return n or (getattr(plan, 'body_len', 0) or 0)
