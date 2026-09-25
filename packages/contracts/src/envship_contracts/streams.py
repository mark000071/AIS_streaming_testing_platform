"""Redis Streams topics (roadmap §4.1) and protocol constants."""

PROTOCOL_VERSION = "0.1"

GRID_S = 20
HIST_STEPS = 30
FUT_STEPS = 30
HORIZON_S = GRID_S * FUT_STEPS
TOKEN_DIM = 8
GEOM_DIM = 19
PREDICT_TIMEOUT_S = 5.0

FEEDS = ("fi", "no")


def raw(feed: str) -> str:
    return f"ais.raw.{feed}"


WINDOWS = "windows.eligible"
TRUTH = "truth.realized"
SCORES = "scores"
HEALTH = "sys.health"


def predictions(predictor_id: str) -> str:
    return f"predictions.{predictor_id}"


PREDICTOR_REGISTRY = "registry:predictors"

# Seconds of history each topic keeps (trimmed with XADD MINID).
RETENTION_S = {
    "ais.raw": 3600,
    WINDOWS: 1800,
    "predictions": 1800,
    TRUTH: 1800,
    SCORES: 3600,
    HEALTH: 600,
}


def retention_for(topic: str) -> int:
    if topic in RETENTION_S:
        return RETENTION_S[topic]
    return RETENTION_S[topic.rsplit(".", 1)[0]]
