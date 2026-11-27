"""GitHub Actions completion callback to update the originating Google Sheet row."""
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fk_common import BASE_DIR


REQUEST_ID_PATTERN = r"REQ-[0-9]{8}-[A-Za-z0-9_-]{1,40}"


def main():
    request_id = os.environ.get("REQUEST_ID", "")

    if not re.fullmatch(REQUEST_ID_PATTERN, request_id):
        raise RuntimeError("REQUEST_ID has an invalid format")

    status_file = BASE_DIR / "reports" / request_id / "request_status.json"

    if not status_file.is_file():
        raise RuntimeError("Request status file was not found")

    status = json.loads(status_file.read_text(encoding="utf-8"))
    result = status.get("result", {})

    details = (
        status.get("error")
        or result.get("failure_reason")
        or "; ".join(result.get("warnings", []))
    )

    # The sheet shows only two outcomes. A workbook that was built and e-mailed counts as Succeeded,
    # including runs the scraper labelled Partial (the coverage note stays in Status Details).
    raw_status = status.get("status", "Failed")
    final_status = "Succeeded" if raw_status in ("Completed", "Email Sent", "Partial") else "Failed"

    payload = {
        "secret": os.environ.get("SHEETS_CALLBACK_SECRET", ""),
        "request_id": request_id,
        "status": final_status,
        "details": details,
        "job_url": os.environ.get("BUILD_URL", ""),
    }

    url = os.environ.get("SHEETS_CALLBACK_URL", "")

    if not url.startswith("https://"):
        raise RuntimeError(
            "SHEETS_CALLBACK_URL must be an HTTPS Apps Script web-app URL"
        )

    if not payload["secret"]:
        raise RuntimeError(
            "SHEETS_CALLBACK_SECRET is not configured in GitHub Actions secrets"
        )

    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    # The Apps Script web app answers "busy" while it is handling another request and the network can
    # blip: retry those a few times. Real rejections (wrong secret, unknown request) fail at once.
    delays = (0, 5, 15, 30)
    last_error = None
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            time.sleep(delay)
        try:
            with urlopen(req, timeout=60) as response:
                response_body = response.read().decode("utf-8")
                status_code = response.status
        except HTTPError as exc:
            # Show the real HTTP code (401/403/404/...) and the first words of Google's answer (never the secret).
            try:
                snippet = " ".join(exc.read().decode("utf-8", "replace").split())[:120]
            except Exception:
                snippet = ""
            last_error = RuntimeError(f"Google Sheets callback could not be reached: HTTP {exc.code} {snippet}".strip())
            continue
        except (URLError, TimeoutError, OSError) as exc:
            last_error = RuntimeError(f"Google Sheets callback could not be reached: {type(exc).__name__}")
            continue

        try:
            callback_result = json.loads(response_body)
        except json.JSONDecodeError:
            last_error = RuntimeError(f"Apps Script returned a non-JSON response (HTTP {status_code})")
            continue

        if status_code < 300 and callback_result.get("ok"):
            last_error = None
            break
        reason = str(callback_result.get("error", "no reason given"))[:80]
        last_error = RuntimeError(f"Google Sheets callback was rejected (HTTP {status_code}): {reason}")
        if reason != "busy":
            break
    if last_error:
        raise last_error

    print(f"Google Sheets callback succeeded for {request_id}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(
            f"Sheets status callback failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
