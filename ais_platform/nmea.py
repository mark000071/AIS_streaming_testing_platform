"""AIS AIVDM encoding/decoding (message types 1/2/3 and 5) plus NMEA 4.0 tag blocks."""

from dataclasses import dataclass


def checksum(body: str) -> str:
    x = 0
    for ch in body:
        x ^= ord(ch)
    return f"{x:02X}"


class BitWriter:
    def __init__(self):
        self.bits: list[int] = []

    def uint(self, value: int, n: int):
        value &= (1 << n) - 1
        self.bits.extend((value >> (n - 1 - i)) & 1 for i in range(n))

    def int(self, value: int, n: int):
        self.uint(value, n)

    def text(self, s: str, nchars: int):
        s = s.upper()[:nchars].ljust(nchars, "@")
        for ch in s:
            c = ord(ch)
            self.uint(c - 64 if c >= 64 else c, 6)

    def armor(self) -> tuple[str, int]:
        fill = (-len(self.bits)) % 6
        bits = self.bits + [0] * fill
        out = []
        for i in range(0, len(bits), 6):
            v = 0
            for b in bits[i:i + 6]:
                v = (v << 1) | b
            c = v + 48
            if c > 87:
                c += 8
            out.append(chr(c))
        return "".join(out), fill


class BitReader:
    def __init__(self, payload: str, fill: int):
        bits: list[int] = []
        for ch in payload:
            v = ord(ch) - 48
            if v > 40:
                v -= 8
            if not 0 <= v < 64:
                raise ValueError(f"invalid armor char {ch!r}")
            bits.extend((v >> (5 - i)) & 1 for i in range(6))
        self.bits = bits[:len(bits) - fill] if fill else bits
        self.pos = 0

    def uint(self, n: int) -> int:
        if self.pos + n > len(self.bits):
            raise ValueError("payload too short")
        v = 0
        for b in self.bits[self.pos:self.pos + n]:
            v = (v << 1) | b
        self.pos += n
        return v

    def int(self, n: int) -> int:
        v = self.uint(n)
        return v - (1 << n) if v & (1 << (n - 1)) else v

    def text(self, nchars: int) -> str:
        out = []
        for _ in range(nchars):
            v = self.uint(6)
            out.append(chr(v + 64 if v < 32 else v))
        return "".join(out).split("@")[0].rstrip()


@dataclass
class PositionReport:
    mmsi: int
    lat: float
    lon: float
    sog: float
    cog: float
    heading: int
    nav_status: int = 0
    second: int = 60
    msg_type: int = 1


@dataclass
class StaticVoyage:
    mmsi: int
    name: str
    callsign: str
    ship_type: int
    destination: str
    imo: int = 0
    to_bow: int = 0
    to_stern: int = 0
    to_port: int = 0
    to_starboard: int = 0
    draught: float = 0.0
    msg_type: int = 5


def encode_position(r: PositionReport) -> str:
    w = BitWriter()
    w.uint(r.msg_type, 6)
    w.uint(0, 2)
    w.uint(r.mmsi, 30)
    w.uint(r.nav_status, 4)
    w.int(-128, 8)
    w.uint(min(1022, round(r.sog * 10)), 10)
    w.uint(1, 1)
    w.int(round(r.lon * 600000), 28)
    w.int(round(r.lat * 600000), 27)
    w.uint(round(r.cog * 10) % 3600, 12)
    w.uint(r.heading % 360, 9)
    w.uint(r.second, 6)
    w.uint(0, 2)
    w.uint(0, 3)
    w.uint(0, 1)
    w.uint(0, 19)
    return w.armor()[0]


def encode_static(s: StaticVoyage) -> tuple[str, int]:
    w = BitWriter()
    w.uint(5, 6)
    w.uint(0, 2)
    w.uint(s.mmsi, 30)
    w.uint(0, 2)
    w.uint(s.imo, 30)
    w.text(s.callsign, 7)
    w.text(s.name, 20)
    w.uint(s.ship_type, 8)
    w.uint(s.to_bow, 9)
    w.uint(s.to_stern, 9)
    w.uint(s.to_port, 6)
    w.uint(s.to_starboard, 6)
    w.uint(1, 4)
    w.uint(0, 4)
    w.uint(0, 5)
    w.uint(24, 5)
    w.uint(60, 6)
    w.uint(round(s.draught * 10), 8)
    w.text(s.destination, 20)
    w.uint(0, 1)
    w.uint(0, 1)
    return w.armor()


def to_sentences(payload: str, fill: int, seq_id: int | None, channel: str = "A", max_len: int = 60) -> list[str]:
    parts = [payload[i:i + max_len] for i in range(0, len(payload), max_len)] or [""]
    total = len(parts)
    sid = "" if total == 1 or seq_id is None else str(seq_id % 10)
    out = []
    for i, part in enumerate(parts, 1):
        f = fill if i == total else 0
        body = f"AIVDM,{total},{i},{sid},{channel},{part},{f}"
        out.append(f"!{body}*{checksum(body)}")
    return out


def with_tag_block(sentence: str, epoch_ms: int, line_no: int) -> str:
    # 'c' carries milliseconds here (spec says seconds) so latency can be measured at ms resolution.
    tag = f"c:{epoch_ms},n:{line_no}"
    return f"\\{tag}*{checksum(tag)}\\{sentence}"


class NmeaError(ValueError):
    pass


@dataclass
class ParsedLine:
    tags: dict
    sentence: str
    checksum_ok: bool


def parse_line(line: str) -> ParsedLine:
    line = line.strip()
    tags: dict = {}
    if line.startswith("\\"):
        end = line.find("\\", 1)
        if end < 0:
            raise NmeaError("unterminated tag block")
        tag = line[1:end]
        content, _, cs = tag.partition("*")
        if cs and cs.upper() == checksum(content):
            for kv in content.split(","):
                k, _, v = kv.partition(":")
                tags[k] = v
        line = line[end + 1:]
    if not line.startswith(("!", "$")) or "*" not in line:
        raise NmeaError("not an NMEA sentence")
    body, _, cs = line[1:].rpartition("*")
    return ParsedLine(tags, line, cs.strip().upper() == checksum(body))


class Decoder:
    """Stateful AIVDM decoder that reassembles multi-sentence messages."""

    def __init__(self):
        self._partial: dict[tuple[str, str], list[str]] = {}

    def feed(self, sentence: str):
        body = sentence[1:].rpartition("*")[0]
        f = body.split(",")
        if len(f) != 7 or not f[0].endswith("VDM"):
            raise NmeaError("unsupported sentence")
        total, num, sid, chan, payload, fill = int(f[1]), int(f[2]), f[3], f[4], f[5], int(f[6] or 0)
        if total == 1:
            return decode_payload(payload, fill)
        key = (sid, chan)
        if num == 1:
            self._partial[key] = [payload]
        elif key in self._partial and len(self._partial[key]) == num - 1:
            self._partial[key].append(payload)
        else:
            self._partial.pop(key, None)
            return None
        if num == total:
            return decode_payload("".join(self._partial.pop(key)), fill)
        return None


def decode_payload(payload: str, fill: int):
    r = BitReader(payload, fill)
    mtype = r.uint(6)
    if mtype in (1, 2, 3):
        r.uint(2)
        mmsi = r.uint(30)
        nav = r.uint(4)
        r.int(8)
        sog = r.uint(10) / 10
        r.uint(1)
        lon = r.int(28) / 600000
        lat = r.int(27) / 600000
        cog = r.uint(12) / 10
        heading = r.uint(9)
        second = r.uint(6)
        return PositionReport(mmsi, lat, lon, sog, cog, heading, nav, second, mtype)
    if mtype == 5:
        r.uint(2)
        mmsi = r.uint(30)
        r.uint(2)
        imo = r.uint(30)
        callsign = r.text(7)
        name = r.text(20)
        ship_type = r.uint(8)
        bow, stern, port, stbd = r.uint(9), r.uint(9), r.uint(6), r.uint(6)
        r.uint(4)
        r.uint(4); r.uint(5); r.uint(5); r.uint(6)
        draught = r.uint(8) / 10
        dest = r.text(20)
        return StaticVoyage(mmsi, name, callsign, ship_type, dest, imo, bow, stern, port, stbd, draught)
    return None
