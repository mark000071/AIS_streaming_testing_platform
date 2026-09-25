"""ingest-fi: Digitraffic marine AIS (Finland, CC BY 4.0) → dedupe/blacklist → raw archive + ``ais.raw.fi``.

Default transport is MQTT push over WSS (``vessels-v2/+/location``); ``fi_mode: rest`` polls the REST
endpoint instead. Honours HTTPS_PROXY for MQTT when PySocks is installed.
"""

from __future__ import annotations

import gzip
import json
import os
import queue
import time
import urllib.parse
from collections import OrderedDict

import httpx
from envship_contracts import RawPosition, streams
from envship_sdk import bus
from prometheus_client import Counter

from .archive import ParquetSink, date_hour
from .service import Stopper, setup
from .settings import Settings, get_settings

FEED = "fi"
M_IN = Counter("ingest_messages_total", "messages seen", ["feed", "result"])


class Deduper:
    def __init__(self, size: int = 200_000):
        self.seen: OrderedDict[tuple[int, float], None] = OrderedDict()
        self.size = size

    def fresh(self, key: tuple[int, float]) -> bool:
        if key in self.seen:
            return False
        self.seen[key] = None
        if len(self.seen) > self.size:
            self.seen.popitem(last=False)
        return True


def valid_mmsi(mmsi: int, blacklist: set[int]) -> bool:
    return 100_000_000 <= mmsi <= 999_999_999 and mmsi not in blacklist


def mqtt_source(s: Settings, q: queue.Queue, log):
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion

    def on_connect(c, userdata, flags, rc, props=None):
        log.info("MQTT connected (%s); subscribing", rc)
        c.subscribe("vessels-v2/+/location", qos=0)

    def on_message(c, userdata, m):
        try:
            mmsi = int(m.topic.split("/")[1])
            d = json.loads(m.payload)
            q.put((mmsi, float(d["time"]), d))
        except Exception:
            M_IN.labels(FEED, "malformed").inc()

    client = mqtt.Client(
        CallbackAPIVersion.VERSION2,
        transport="websockets",
        client_id=f"{s.digitraffic_user}-{os.getpid()}",
    )
    client.tls_set(ca_certs=os.environ.get("SSL_CERT_FILE"))
    client.ws_set_options(path="/mqtt")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        import socks

        p = urllib.parse.urlparse(proxy)
        client.proxy_set(proxy_type=socks.HTTP, proxy_addr=p.hostname, proxy_port=p.port)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(1, 30)
    client.connect_async(s.fi_mqtt_host, 443, keepalive=30)
    client.loop_start()
    return client


def rest_poll(s: Settings, http: httpx.Client, since_ms: int, q: queue.Queue) -> int:
    resp = http.get(s.fi_rest_url, params={"from": since_ms})
    resp.raise_for_status()
    body = resp.content
    data = json.loads(gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body)
    newest = since_ms
    for f in data["features"]:
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        ts_ms = int(p["timestampExternal"])
        newest = max(newest, ts_ms)
        q.put((int(p["mmsi"]), ts_ms / 1000.0, {**p, "lat": lat, "lon": lon}))
    return newest


def main() -> None:
    s = get_settings()
    log = setup("ingest-fi", s)
    r = bus.connect(s.redis_url)
    stop = Stopper()
    q: queue.Queue = queue.Queue(maxsize=100_000)
    sink = ParquetSink(s.data_dir, "raw_ais", s.parquet_flush_s)
    dedupe = Deduper()
    blacklist = set(s.mmsi_blacklist)
    topic = streams.raw(FEED)

    client = None
    http = None
    since_ms = int(time.time() * 1000) - 60_000
    next_poll = 0.0
    if s.fi_mode == "mqtt":
        client = mqtt_source(s, q, log)
    else:
        http = httpx.Client(
            headers={"Accept-Encoding": "gzip", "Digitraffic-User": s.digitraffic_user}, timeout=20
        )
    log.info("ingest-fi started in %s mode", s.fi_mode)

    published = 0.0
    last_beat = 0.0
    while not stop.stopped:
        if http is not None and time.time() >= next_poll:
            try:
                since_ms = rest_poll(s, http, since_ms, q) - 5000
            except Exception as e:
                log.warning("REST poll failed: %s", e)
            next_poll = time.time() + s.fi_poll_s
        pipe = r.pipeline(transaction=False)
        n = 0
        deadline = time.time() + 0.5
        while time.time() < deadline and n < 2000:
            try:
                mmsi, ts, d = q.get(timeout=0.1)
            except queue.Empty:
                continue
            if not valid_mmsi(mmsi, blacklist):
                M_IN.labels(FEED, "blacklisted").inc()
                continue
            if not dedupe.fresh((mmsi, ts)):
                M_IN.labels(FEED, "duplicate").inc()
                continue
            msg = RawPosition(
                mmsi=mmsi,
                feed=FEED,
                recv_ts=ts,
                ingest_wall=time.time(),
                lat=float(d["lat"]),
                lon=float(d["lon"]),
                sog=d.get("sog"),
                cog=d.get("cog"),
                heading=d.get("heading"),
                nav_status=d.get("navStat"),
            )
            bus.publish(pipe, topic, msg)
            day, hour = date_hour(ts)
            sink.add(
                {
                    k: getattr(msg, k)
                    for k in (
                        "mmsi",
                        "recv_ts",
                        "ingest_wall",
                        "lat",
                        "lon",
                        "sog",
                        "cog",
                        "heading",
                        "nav_status",
                        "ais_class",
                    )
                },
                feed=FEED,
                date=day,
                hour=hour,
            )
            M_IN.labels(FEED, "ok").inc()
            n += 1
        if n:
            pipe.execute()
            published += n
        sink.maybe_flush()
        if time.time() - last_beat >= 5:
            bus.beat(
                r,
                "ingest-fi",
                queue_depth=q.qsize(),
                last_event_ts=time.time(),
                counters={"published": published},
            )
            last_beat = time.time()
    if client is not None:
        client.loop_stop()
    sink.flush()


if __name__ == "__main__":
    main()
