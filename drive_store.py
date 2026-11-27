"""Persistent report storage in Google Drive (through the existing Apps Script web app).

GitHub Actions starts with an empty disk on every run, so reports are kept in Google Drive:

    Flipkart Review Reports/
        _index.json                      email + product  ->  Drive file
        abc@gmail.com/
            boAt Sailor Nav Kids Watch.xlsx
            <another product>.xlsx
        other@gmail.com/
            boAt Sailor Nav Kids Watch.xlsx   (separate copy, never mixed with abc@gmail.com)

One report = one (e-mail, product) pair. The Apps Script runs as the Drive owner, so no Google Cloud
project or service account is needed. By default it reuses SHEETS_CALLBACK_URL / SHEETS_CALLBACK_SECRET;
set DRIVE_STORE_URL / DRIVE_STORE_SECRET to keep the reports in a different Google account's Drive.
"""
import base64
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

log = logging.getLogger("fk")


def normalize_product_url(url):
    """Stable identity of a Flipkart product, so tracking parameters never create a 'different' product.

    Flipkart's product id (?pid=...) wins; otherwise the /p/itm... part of the path is used."""
    parts = urlsplit(str(url or "").strip())
    pid = (parse_qs(parts.query).get("pid") or [""])[0].strip().upper()
    if pid:
        return "pid:" + pid
    m = re.search(r"/p/(itm[a-z0-9]+)", parts.path, re.I)
    if m:
        return "itm:" + m.group(1).lower()
    return "path:" + parts.path.lower().rstrip("/")


def product_key(email, url):
    """SHA-256 of 'email|product' - the same e-mail + same product always gives the same key."""
    ident = f"{str(email).strip().lower()}|{normalize_product_url(url)}"
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()


def _call(payload, attempts=4):
    # A separate storage script (another Google account's Drive) wins; otherwise the status-callback script is used.
    url = os.environ.get("DRIVE_STORE_URL") or os.environ.get("SHEETS_CALLBACK_URL", "")
    secret = os.environ.get("DRIVE_STORE_SECRET") or os.environ.get("SHEETS_CALLBACK_SECRET", "")
    if not url.startswith("https://") or not secret:
        raise RuntimeError("DRIVE_STORE_URL / DRIVE_STORE_SECRET (or SHEETS_CALLBACK_URL / SHEETS_CALLBACK_SECRET) are not configured")
    body = json.dumps({**payload, "secret": secret}).encode("utf-8")
    last = None
    for n in range(1, attempts + 1):
        if n > 1:
            time.sleep(5 * n)
        try:
            req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(req, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last = RuntimeError(f"Drive storage could not be reached: HTTP {exc.code}")
            continue
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last = RuntimeError(f"Drive storage could not be reached: {type(exc).__name__}")
            continue
        if result.get("ok"):
            return result
        reason = str(result.get("error", "no reason given"))[:120]
        last = RuntimeError(f"Drive storage refused the request: {reason}")
        if reason != "busy":
            break
    raise last


def find_report(email, product_url):
    """Return the stored record for this e-mail + product, or None when this is a first request."""
    result = _call({"action": "drive_lookup", "email": email, "key": product_key(email, product_url)})
    return result.get("record") if result.get("found") else None


def download_report(file_id, dest):
    """Download the stored workbook to `dest`."""
    result = _call({"action": "drive_download", "file_id": file_id})
    data = base64.b64decode(result["data"])
    if data[:2] != b"PK":
        raise RuntimeError("the stored report is not a valid .xlsx file")
    Path(dest).write_bytes(data)
    return Path(dest)


def save_report(email, product_url, product_name, path, request_id, replace_file_id=None):
    """Create (first request) or replace (update) the stored workbook and refresh the index.

    The index entry keeps the e-mail, the product URL, the Drive file and every request id that has
    written to this report."""
    path = Path(path)
    result = _call({
        "action": "drive_save", "email": email, "key": product_key(email, product_url),
        "product_url": product_url, "product_name": product_name, "file_name": path.name,
        "request_id": request_id, "replace_file_id": replace_file_id or "",
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
    })
    return result.get("record")
