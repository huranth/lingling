import ipaddress
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import settings

SERVICE = settings.SERVICE
CONFIG = settings.CONFIG_NAME
PROFILE = settings.PROFILE_NAME

KEYS = {
    ("interface", "privatekey"): "PrivateKey",
    ("interface", "address"): "Address",
    ("interface", "dns"): "DNS",
    ("interface", "mtu"): "MTU",
    ("interface", "listenport"): "ListenPort",
    ("interface", "table"): "Table",
    ("interface", "preup"): "PreUp",
    ("interface", "postup"): "PostUp",
    ("interface", "predown"): "PreDown",
    ("interface", "postdown"): "PostDown",
    ("peer", "publickey"): "PublicKey",
    ("peer", "presharedkey"): "PresharedKey",
    ("peer", "allowedips"): "AllowedIPs",
    ("peer", "endpoint"): "Endpoint",
    ("peer", "persistentkeepalive"): "PersistentKeepalive",
}


def config_path(folder):
    return Path(folder) / CONFIG


def profile_path(folder):
    return Path(folder) / PROFILE


def _pairs(text):
    section = ""

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()

        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        yield section, key.strip().lower(), value.strip().strip('"')


def _lines(text):
    return {(section, key): value for section, key, value in _pairs(text)}


def clean(text):
    """WireGuard keys."""
    buckets = {}

    for section, key, value in _pairs(text):
        name = KEYS.get((section, key))

        if name and value:
            buckets.setdefault(section, []).append((name, value))

    out = []

    for section, title in (("interface", "Interface"), ("peer", "Peer")):
        rows = buckets.get(section, [])
        if not rows:
            continue
        out.append("[" + title + "]")
        out.extend(name + " = " + value for name, value in rows)

    return "\n".join(out) + "\n"


def parse(text):
    values = _lines(text)

    private = values.get(("interface", "privatekey"), "")
    address = values.get(("interface", "address"), "")
    public = values.get(("peer", "publickey"), "")
    endpoint = values.get(("peer", "endpoint"), "")
    allowed = values.get(("peer", "allowedips"), "")

    missing = [
        name for name, value in (
            ("PrivateKey", private),
            ("Address", address),
            ("PublicKey", public),
            ("Endpoint", endpoint),
            ("AllowedIPs", allowed),
        ) if not value
    ]

    if missing:
        raise ValueError("missing " + ", ".join(missing))

    first = address.split(",", 1)[0].strip()
    iface = ipaddress.ip_interface(first)

    if iface.version != 6:
        raise ValueError("Address must be IPv6")
    if iface.network.prefixlen > settings.LANE_BITS:
        raise ValueError("Address must be /" + str(settings.LANE_BITS) + " or larger")

    host, port = endpoint.rsplit(":", 1)
    if not host or not port.isdigit():
        raise ValueError("Endpoint must be host:port")

    return {
        "address": first,
        "endpoint": endpoint,
    }


def prefix_network(prefix):
    net = ipaddress.ip_network(prefix.strip(), strict=False)

    if net.version != 6:
        raise ValueError("prefix must be IPv6")
    if net.prefixlen > settings.LANE_BITS:
        raise ValueError("prefix must be /" + str(settings.LANE_BITS) + " or larger")
    if net.num_addresses // (1 << settings.LANE_BITS) > settings.MAX_LANES:
        raise ValueError("prefix is too large")

    return str(net)


def save(folder, text, prefix):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    conf = config_path(folder)
    conf.write_text(clean(text), encoding="utf-8")

    try:
        os.chmod(conf, settings.PRIVATE_MODE)
    except OSError:
        pass

    values = _lines(text)
    address = values.get(("interface", "address"), "").split(",", 1)[0].strip()

    data = {
        "prefix": prefix_network(prefix),
        "service": SERVICE,
        "address": address,
        "endpoint": values.get(("peer", "endpoint"), ""),
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = profile_path(folder)
    tmp = path.with_suffix(settings.TMP_SUFFIX)
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(folder):
    try:
        data = json.loads(profile_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(data, dict):
        return None
    if not config_path(folder).is_file():
        return None

    try:
        data["prefix"] = prefix_network(str(data.get("prefix") or ""))
    except ValueError:
        return None

    if not data.get("service"):
        return None

    return data


def complete(folder):
    return load(folder) is not None


def remove(folder, config=True):
    paths = [profile_path(folder)]
    if config:
        paths.append(config_path(folder))

    for path in paths:
        try:
            Path(path).unlink()
        except OSError:
            pass
