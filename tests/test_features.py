import numpy as np
from envship_contracts import streams
from envship_core.features import Track, build_tokens, causal_sample, history, parity_check
from hypothesis import given, settings
from hypothesis import strategies as st


def test_golden_parity():
    ok, why = parity_check()
    assert ok, why


def _track(times):
    tr = Track()
    for i, t in enumerate(times):
        tr.append(t, 60 + i * 1e-4, 24 + i * 1e-4, 10.0, 45.0)
    return tr


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.floats(0, 3000), min_size=2, max_size=60, unique=True), st.floats(0, 3000), st.floats(1, 500)
)
def test_sampling_is_causal(times, t, extra):
    """A report that arrives after t can never change the sample at t."""
    times = sorted(times)
    tr = _track(times)
    before = causal_sample(tr, t, 60.0)
    if times[-1] < t + extra:
        tr.append(t + extra, 61.0, 25.0, 30.0, 180.0)
    assert causal_sample(tr, t, 60.0) == before


def test_history_reasons():
    tr = _track([100, 110, 120])  # first report after the window would have to start
    assert history(tr, 600, 60)[1] == "warmup"
    tr = _track(list(range(0, 300, 10)) + list(range(400, 700, 10)))  # 100 s silence
    assert history(tr, 600, 60)[1] == "gap"
    tr = _track(list(range(0, 700, 10)))
    samples, reason = history(tr, 600, 60)
    assert reason == "ok" and samples is not None and samples.shape == (streams.HIST_STEPS, 5)


def test_tokens_are_relative_to_anchor():
    tr = _track(list(range(0, 700, 10)))
    samples, _ = history(tr, 600, 60)
    assert samples is not None
    tokens, abs_xy, lat0, lon0 = build_tokens(samples)
    assert tokens.shape == (30, 8) and tokens.dtype == np.float32
    assert np.allclose(tokens[-1, :2], 0.0, atol=1e-3)
    assert (lat0, lon0) == tuple(abs_xy[-1])
