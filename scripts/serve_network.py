"""Run the scanner for tablets and phones on the local network: HTTPS + PIN login.

    .venv\\Scripts\\python scripts\\serve_network.py

- Creates the HTTPS certificate on first run (scripts/make_cert.py); re-create it if the PC's IP changes.
- Creates a random 6-digit PIN on first run in data/gate_pin.txt. Edit that file to change it
  (then restart); changing it logs every device out.
- Windows asks once to allow Python through the firewall: allow it on Private networks.
"""

from __future__ import annotations

import os
import secrets
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CERT, KEY = ROOT / "certs" / "cert.pem", ROOT / "certs" / "key.pem"
PIN_FILE = ROOT / "data" / "gate_pin.txt"
PORT = int(os.environ.get("GATE_PORT", "8443"))


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main() -> None:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    if not (CERT.exists() and KEY.exists()):
        sys.path.insert(0, str(ROOT / "scripts"))
        import make_cert
        make_cert.main([])
    if not PIN_FILE.exists():
        PIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        PIN_FILE.write_text(f"{secrets.randbelow(10**6):06d}\n", encoding="utf-8")
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    if len(pin) < 4:
        sys.exit(f"PIN in {PIN_FILE} is too short; use at least 4 digits.")
    os.environ["GATE_PIN"] = pin

    ip = lan_ip()
    print("\n  Gate Check-In Scanner (network mode)")
    print(f"  On tablets/phones (same Wi-Fi):  https://{ip}:{PORT}")
    print(f"  On this PC:                      https://localhost:{PORT}")
    print(f"  PIN: {pin}   (change it in {PIN_FILE.relative_to(ROOT)})")
    print("  First visit on each device: 'connection not private' -> Advanced -> Proceed.\n")

    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT,
                ssl_certfile=str(CERT), ssl_keyfile=str(KEY), log_level="info")


if __name__ == "__main__":
    main()
