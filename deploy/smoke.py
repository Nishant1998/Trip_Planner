"""Public HTTP smoke check: no login, database writes, AI calls or provider lookups."""

import argparse
import json
import re
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--ready", action="store_true", help="Also require provider configuration and DB readiness")
    args = parser.parse_args()
    base = args.url.rstrip("/") + "/"
    checked = []
    assets = set()
    for path in ("", "examples/jaipur-heritage", "examples/lonavala-family", "examples/paris-culture", "health/live"):
        with urlopen(urljoin(base, path), timeout=15) as response:
            assert response.status == 200, path
            body = response.read()
            if path == "health/live":
                assert json.loads(body)["status"] == "ok"
            else:
                assert b"<div id=\"root\">" in body, "SPA route missing"
                assert b"127.0.0.1:8000" not in body, "Development API URL leaked"
                assets.update(item.decode() for item in re.findall(rb'(?:src|href)="(/assets/[^\"]+)"', body))
            checked.append(path or "/")
    assert assets, "Production assets were not linked"
    for asset in assets:
        with urlopen(urljoin(base, asset), timeout=15) as response:
            assert response.status == 200 and response.read(), asset
            assert "immutable" in response.headers.get("Cache-Control", ""), asset
            assert "text/html" not in response.headers.get("Content-Type", ""), asset
    try:
        urlopen(urljoin(base, "api/trips"), timeout=15)
        raise AssertionError("Anonymous trip access unexpectedly succeeded")
    except HTTPError as error:
        assert error.code == 401
    if args.ready:
        with urlopen(urljoin(base, "health/ready"), timeout=15) as response:
            assert json.load(response)["status"] == "ready"
    print(json.dumps({"status": "passed", "routes": checked, "assets": len(assets),
                      "anonymous_access": "blocked"}))


if __name__ == "__main__":
    main()
