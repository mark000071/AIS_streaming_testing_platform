"""Parquet table schemas (roadmap §4.3)."""

import pyarrow as pa

f32 = pa.float32()
f64 = pa.float64()

RAW_AIS = pa.schema(
    [
        ("mmsi", pa.int64()),
        ("recv_ts", f64),
        ("ingest_wall", f64),
        ("lat", f64),
        ("lon", f64),
        ("sog", f64),
        ("cog", f64),
        ("heading", pa.int32()),
        ("nav_status", pa.int32()),
        ("ais_class", pa.string()),
    ]
)

WINDOWS = pa.schema(
    [
        ("window_id", pa.string()),
        ("mmsi", pa.int64()),
        ("feed", pa.string()),
        ("anchor_ts", f64),
        ("enqueue_wall", f64),
        ("anchor_lat", f64),
        ("anchor_lon", f64),
        ("abs_xy", pa.list_(f64)),
        ("tokens", pa.list_(f32)),
        ("tags", pa.list_(pa.string())),
        ("protocol_version", pa.string()),
    ]
)

PREDICTIONS = pa.schema(
    [
        ("window_id", pa.string()),
        ("mmsi", pa.int64()),
        ("model_revision", pa.string()),
        ("anchor_ts", f64),
        ("enqueue_wall", f64),
        ("issue_wall", f64),
        ("k", pa.int32()),
        ("paths", pa.list_(f32)),
        ("scores", pa.list_(f32)),
        ("selected", pa.int32()),
        ("compute_ms", f64),
    ]
)

TRUTH = pa.schema(
    [
        ("window_id", pa.string()),
        ("mmsi", pa.int64()),
        ("feed", pa.string()),
        ("anchor_ts", f64),
        ("future_xy", pa.list_(f32)),
        ("valid", pa.list_(pa.bool_())),
        ("coverage", f64),
        ("fill_method", pa.string()),
    ]
)

SCORES = pa.schema(
    [
        ("window_id", pa.string()),
        ("mmsi", pa.int64()),
        ("feed", pa.string()),
        ("anchor_ts", f64),
        ("scored_wall", f64),
        ("miss", pa.bool_()),
        ("served_ade", f64),
        ("served_fde", f64),
        ("oracle_ade", f64),
        ("per_horizon", pa.list_(f32)),
        ("k", pa.int32()),
        ("comparable", pa.bool_()),
        ("coverage", f64),
        ("scene", pa.string()),
        ("speed_band", pa.string()),
        ("queue_ms", f64),
        ("compute_ms", f64),
    ]
)

# Partition columns (Hive-style directories) per table; they are not stored inside the files.
PARTITIONS = {
    "raw_ais": ("feed", "date", "hour"),
    "windows": ("date", "hour"),
    "predictions": ("predictor_id", "date"),
    "truth": ("date",),
    "scores": ("predictor_id", "date"),
    "leaderboard_hourly": ("date",),
    "sla_hourly": ("date",),
}

TABLES = {
    "raw_ais": RAW_AIS,
    "windows": WINDOWS,
    "predictions": PREDICTIONS,
    "truth": TRUTH,
    "scores": SCORES,
}
