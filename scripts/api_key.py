"""Create, list and revoke API keys for systems that call /api/v1 (YMS, kiosk, mobile app).

    .venv\\Scripts\\python scripts\\api_key.py create "YMS integration"
    .venv\\Scripts\\python scripts\\api_key.py list
    .venv\\Scripts\\python scripts\\api_key.py revoke 3

The key is shown once, at creation; only its SHA-256 hash is stored. Clients send it as
'X-API-Key: <key>' or 'Authorization: Bearer <key>'. Keys are checked when the server runs with a
PIN (network mode, scripts/serve_network.py); in local mode on 127.0.0.1 the API is open.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "create":
        with db.connect() as conn:
            key_id, key = db.create_api_key(conn, " ".join(argv[1:]))
        print(f"API key #{key_id} created for '{' '.join(argv[1:])}'. Copy it now; it is not shown again:\n\n  {key}\n")
        return 0
    if argv[:1] == ["list"]:
        with db.connect() as conn:
            keys = db.list_api_keys(conn)
        if not keys:
            print("No API keys yet.")
        for k in keys:
            state = f"REVOKED {k['revoked_at'][:16]}" if k["revoked_at"] else "active"
            print(f"#{k['id']:<3} {k['prefix']}...  {k['name']:<30} created {k['created_at'][:16]}  "
                  f"last used {(k['last_used_at'] or 'never')[:16]}  {state}")
        return 0
    if len(argv) == 2 and argv[0] == "revoke" and argv[1].isdigit():
        with db.connect() as conn:
            ok = db.revoke_api_key(conn, int(argv[1]))
        print(f"API key #{argv[1]} revoked." if ok else f"No active API key #{argv[1]}.")
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
