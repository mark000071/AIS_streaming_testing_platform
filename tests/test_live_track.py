from envship_contracts import RawPosition, encode, streams
from envship_core.api.state import TRACK_MAX_GAP_S, TRACK_MIN_DT_S, TRACK_S, VESSEL_TTL_S, LiveState


def _pos(state: LiveState, mmsi: int, ts: float, lon: float, lat: float = 60.0) -> None:
    msg = RawPosition(mmsi=mmsi, feed="fi", recv_ts=ts, ingest_wall=ts, lat=lat, lon=lon, sog=10.0, cog=90.0)
    state._apply(streams.raw("fi"), encode(msg))


def _state() -> LiveState:
    return LiveState("redis://localhost:6379/0")  # redis client is lazy; nothing connects here


def _vessel(state: LiveState, mmsi: int) -> dict:
    v = state.vessel(mmsi)
    assert v is not None
    return v


def _lons(state: LiveState, mmsi: int) -> list[list[float]]:
    return [[p[0] for p in seg] for seg in _vessel(state, mmsi)["track"]]


def test_track_is_thinned_and_keeps_the_newest_point():
    st = _state()
    for i in range(7):  # every 5 s for 30 s, ~5 kn
        _pos(st, 1, 1000.0 + 5 * i, lon=20.0 + 0.0003 * i)
    (seg,) = _vessel(st, 1)["track"]
    # one point per TRACK_MIN_DT_S, and the last one is the latest report
    assert len(seg) == int(30 / TRACK_MIN_DT_S) + 1
    assert seg[-1] == [round(20.0 + 0.0018, 6), 60.0]


def test_out_of_order_reports_are_ignored():
    st = _state()
    _pos(st, 1, 1000.0, lon=20.0)
    _pos(st, 1, 1020.0, lon=20.001)
    _pos(st, 1, 1005.0, lon=99.0)
    assert _lons(st, 1) == [[20.0, 20.001]]


def test_track_only_covers_the_last_track_window():
    st = _state()
    for i in range(0, int(2 * TRACK_S), 60):
        _pos(st, 1, 1000.0 + i, lon=20.0 + i * 1e-5)
    (seg,) = _vessel(st, 1)["track"]
    assert len(seg) == int(TRACK_S / 60) + 1


def test_expired_vessel_drops_its_track():
    st = _state()
    _pos(st, 1, 1000.0, lon=20.0)
    _pos(st, 2, 1000.0 + VESSEL_TTL_S + 60, lon=21.0)
    st._expire()
    assert st.vessel(1) is None
    assert 1 not in st.tracks
    assert _vessel(st, 2)["track"] == [[[21.0, 60.0]]]


def test_implausible_jump_starts_a_new_segment():
    # e.g. the replay looping back to the start of the fixture: same ship, far away, shortly after
    st = _state()
    for i in range(5):
        _pos(st, 1, 1000.0 + 20 * i, lon=20.0 + 0.001 * i)  # ~55 m / 20 s, a normal ~5 kn
    _pos(st, 1, 1100.0, lon=21.0)  # ~55 km in 20 s
    _pos(st, 1, 1120.0, lon=21.001)
    assert _lons(st, 1) == [[20.0, 20.001, 20.002, 20.003, 20.004], [21.0, 21.001]]


def test_long_reporting_gap_starts_a_new_segment_without_losing_history():
    st = _state()
    _pos(st, 1, 1000.0, lon=20.0)
    _pos(st, 1, 1020.0, lon=20.001)
    # plausible speed, but silent for longer than TRACK_MAX_GAP_S: don't bridge it with a straight line
    _pos(st, 1, 1020.0 + TRACK_MAX_GAP_S + 60, lon=20.05)
    assert _lons(st, 1) == [[20.0, 20.001], [20.05]]


def test_segment_closes_at_the_last_report_before_a_break():
    # the newest report inside the thinning interval must not be lost when the next one breaks the track
    st = _state()
    _pos(st, 1, 1000.0, lon=20.0)
    _pos(st, 1, 1004.0, lon=20.0002)  # thinned away as a committed point, kept as the head
    _pos(st, 1, 1010.0 + TRACK_MAX_GAP_S, lon=20.05)
    assert _lons(st, 1) == [[20.0, 20.0002], [20.05]]


def test_sentinel_sog_and_cog_are_reported_as_unknown():
    st = _state()
    msg = RawPosition(
        mmsi=7, feed="fi", recv_ts=1000.0, ingest_wall=1000.0, lat=60.0, lon=20.0, sog=102.3, cog=360.0
    )
    st._apply(streams.raw("fi"), encode(msg))
    v = _vessel(st, 7)["vessel"]
    assert v["sog"] is None and v["cog"] is None
