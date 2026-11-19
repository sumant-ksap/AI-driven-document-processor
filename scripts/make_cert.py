"""Create a self-signed HTTPS certificate so tablets/phones on the LAN can use the live camera.

Browsers only allow camera access on https:// (or localhost). Each device shows a one-time
"connection not private" warning for a self-signed certificate; tap Advanced -> Proceed.

Usage:  .venv\\Scripts\\python scripts\\make_cert.py            (uses this PC's current LAN IPs)
        .venv\\Scripts\\python scripts\\make_cert.py 192.168.1.50 (add specific IPs)
Re-run it if the laptop's IP address changes.
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CERT_DIR = Path(__file__).resolve().parent.parent / "certs"


def lan_ips() -> set[str]:
    ips = {"127.0.0.1"}
    try:  # the address used for outbound traffic = the Wi-Fi/LAN address
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = info[4][0]
        if not ip.startswith(("127.", "169.254.")):
            ips.add(ip)
    return ips


def main(extra_ips: list[str]) -> None:
    ips = sorted(lan_ips() | set(extra_ips))
    host = socket.gethostname()
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Gate Scanner ({host})")])
    now = datetime.datetime.now(datetime.timezone.utc)
    san = [x509.DNSName("localhost"), x509.DNSName(host), x509.DNSName(f"{host}.local")]
    san += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))  # Apple's maximum for TLS certificates
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    CERT_DIR.mkdir(exist_ok=True)
    (CERT_DIR / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    (CERT_DIR / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print(f"Certificate written to {CERT_DIR}")
    print("Valid for:", ", ".join(["localhost", host] + ips))


if __name__ == "__main__":
    main(sys.argv[1:])
