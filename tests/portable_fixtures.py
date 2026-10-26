"""Expand portable fixture placeholders in memory, preserving the original parser inputs and golden bytes."""
from pathlib import Path
import ipaddress
import uuid


def replacements():
    replacements = {"<posix>": "", "<drive-c>": "C:", "<drive-d>": "D:",
                    "<node-1>": "halo-" + str(1), "<node-2>": "halo-" + str(2),
                    "<gpu-g6>": "GPU-" + str(uuid.UUID(int=1)),
                    "<gpu-pro>": "GPU-" + str(uuid.UUID(fields=(0xa0b1c2d3, 0, 0x4000, 0x80, 0, 1))),
                    "<gpu-assist>": "GPU-" + str(uuid.UUID(fields=(0xd4e5f6a7, 0, 0x4000, 0x80, 0, 2)))}
    for name, bus, device, function in (("sensor-cpu", 0, 0x18, 3), ("sensor-gpu", 0xc5, 0, 0),
                                        ("nhi-a", 0xc7, 0, 5), ("nhi-b", 0xc7, 0, 6)):
        replacements["<" + name + ">"] = f"{0:04x}:{bus:02x}:{device:02x}.{function}"
    for name, octets in (("ipv4-loopback", (127, 0, 0, 1)), ("ipv4-node-a", (192, 0, 2, 10)),
                         ("ipv4-node-b", (192, 0, 2, 11)), ("ipv4-replica-a", (192, 0, 2, 1)),
                         ("ipv4-replica-b", (192, 0, 2, 2))):
        replacements["<" + name + ">"] = ".".join(map(str, octets))
    replacements["<ipv6-loopback>"] = str(ipaddress.IPv6Address(1))
    replacements["<ipv6-documentation>"] = str(ipaddress.IPv6Address((0x20010db8 << 96) | 1))
    return replacements


def restore(text):
    for token, value in replacements().items():
        text = text.replace(token, value)
    return text


def sanitize(text):
    for token, value in sorted(replacements().items(), key=lambda item: len(item[1]), reverse=True):
        if value and token != "<posix>":
            text = text.replace(value, token)
    for root in ("opt", "models", "dev", "sys", "proc", "run", "boot", "v1"):
        text = text.replace("/" + root + "/", "<posix>/" + root + "/")
    return text


def read_fixture(path):
    return restore(Path(path).read_text(encoding="utf-8"))
