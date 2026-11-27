# v6
"""Run one Google Form/GitHub Actions report request using the existing Selenium pipeline."""
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
import time

from fk_common import BASE_DIR, load_config, resolve_dir, setup_logging
from fk_excel import safe_filename
from request_flow import ReportRequest
import drive_store

log = setup_logging()


def _safe_request_dir(request_id):
    if not re.fullmatch(r"REQ-[0-9]{8}-[A-Za-z0-9_-]{1,40}", request_id):
        raise ValueError("Invalid request id")
    root = (BASE_DIR / "reports").resolve()
    path = (root / request_id).resolve()
    if path.parent != root:
        raise ValueError("Invalid report path")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _alert_admin(subject, body):
    try:
        from Auto_Email_Flipkart_OnlyJk import send_alert
        send_alert(subject, body)
    except Exception as exc:
        log.error("Administrator alert could not be sent (%s)", type(exc).__name__)


def _cleanup_old_diagnostics(days):
    root = BASE_DIR / "diagnostics"
    if not root.exists():
        return
    cutoff = time.time() - max(int(days), 1) * 86400
    for item in root.rglob("*"):
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
        except OSError:
            log.warning("Could not remove expired diagnostic file %s", item.name)


def _cleanup_request_temps(request_dir):
    for item in request_dir.glob("*.tmp"):
        try:
            if item.is_file():
                item.unlink()
        except OSError:
            log.warning("Could not remove temporary report file %s", item.name)


def _send_with_retry(*args, attempts=6, delay=30):
    """The report e-mail is the point of the whole run, so a brief DNS/network drop (gaierror) must not
    lose it: retry for a few minutes before giving up."""
    from Auto_Email_Flipkart_OnlyJk import send_request_report
    last = None
    for n in range(1, attempts + 1):
        try:
            return send_request_report(*args)
        except Exception as exc:
            last = exc
            log.warning("Report e-mail attempt %d/%d failed (%s); retrying in %ds", n, attempts, type(exc).__name__, delay)
            time.sleep(delay)
    raise last


def run(data):
    request = ReportRequest.from_mapping(data)
    out = _safe_request_dir(request.request_id)
    diag = resolve_dir(BASE_DIR / "diagnostics" / request.request_id)
    status_path = out / "request_status.json"
    state = {**request.as_dict(), "status": "Running", "started_at": datetime.now().isoformat(timespec="seconds")}
    state.pop("recipient_email", None)
    status_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    settings, selectors = load_config()
    _cleanup_old_diagnostics(settings.get("diagnostics_keep_days", 7))
    settings.update(start_date=request.start_date.isoformat(), end_date=request.end_date.isoformat(), headless=True,
                    output_dir=str(out), diagnostics_dir=str(diag), request_id=request.request_id,
                    request_duration=request.duration, request_duration_unit=request.duration_unit)
    # Optional wall-clock budget (run_time_budget_seconds, default 0 = none). Keep the Actions timeout well above
    # the real run time instead: a budget would make the report incomplete.
    import signal
    import time
    import fk_collect
    if float(settings.get("run_time_budget_seconds", 0) or 0) > 0:      # off by default: nothing is skipped
        settings["deadline_epoch"] = time.time() + float(settings["run_time_budget_seconds"])

    def _graceful_stop(signum, _frame):
        log.warning("stop signal %s received - finishing with the reviews collected so far", signum)
        fk_collect.STOP.set()

    for _sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if _sig is not None:
            try:
                signal.signal(_sig, _graceful_stop)
            except (ValueError, OSError):
                pass
    driver = None
    try:
        from Flipkart_Reviews_Hearables import process_product, start_session
        from product_resolver import resolve_product
        driver, login = start_session(settings)
        state["login"] = {
            "status": login.status,
            "method": login.method,
            "reason": login.reason,
            "state": login.state,
            "login_id_masked": login.login_id_masked,
        }
        if settings.get("login_required") and not login.logged_in:
            raise RuntimeError("Flipkart login is required but did not succeed")
        resolved_name, resolved_url = resolve_product(driver, request.product_url,
                                                       settings.get("page_wait_seconds", 25))
        log.info("Request %s resolved to product %s", request.request_id, resolved_name)
        workbook = out / safe_filename(resolved_name)
        # Same e-mail + same product -> the stored report is downloaded and the new reviews are appended to it.
        # Anything else -> a new report. A Drive problem never blocks the request: a new report is built instead.
        existing = None
        try:
            existing = drive_store.find_report(request.recipient_email, resolved_url)
            if existing:
                drive_store.download_report(existing["file_id"], workbook)
                from openpyxl import load_workbook
                load_workbook(workbook, read_only=True).close()       # must be a readable workbook
        except Exception as exc:
            log.warning("Stored report could not be used (%s); a new report will be created", type(exc).__name__)
            if existing:
                _alert_admin("Flipkart report: stored report unreadable",
                             f"Request {request.request_id}: {type(exc).__name__}. A new report was built; the old Drive file was kept.")
            existing = None
            workbook.unlink(missing_ok=True)
        if existing and request.request_id in (existing.get("request_ids") or []):
            # A retry of a request that already saved its update: send the stored report, never append twice.
            log.info("Request %s was already stored; re-sending the saved report without appending again", request.request_id)
            delivery = _send_with_retry(request.request_id, request.recipient_email, resolved_name,
                                        request.start_date.isoformat(), request.end_date.isoformat(), workbook,
                                        0, "success", request.duration, request.duration_unit)
            state.update(status="Email Sent", email_delivery=delivery, report_mode="resent",
                         completed_at=datetime.now().isoformat(timespec="seconds"))
            status_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
            return 0
        settings["append_mode"] = bool(existing)
        state["report_mode"] = "updated" if existing else "new"
        result = process_product(driver, resolved_url, resolved_name, settings, selectors, {"out": out, "diag": diag})
        result["request_id"] = request.request_id
        result["login_status"] = login.status
        result["login_method"] = login.method
        result["login_reason"] = login.reason
        result["resolved_product"] = resolved_name
        result["resolved_url"] = resolved_url
        state.update({"result": result, "product_name_resolved": resolved_name,
                      "completed_at": datetime.now().isoformat(timespec="seconds")})
        if result.get("no_reviews_found"):
            # No reviews in the requested date range: no empty report, just a message e-mail.
            state["status"] = "Completed"
            delivery = _send_with_retry(request.request_id, request.recipient_email, resolved_name,
                                        request.start_date.isoformat(), request.end_date.isoformat(), None,
                                        0, "success", request.duration, request.duration_unit, "Flipkart", True)
            state["email_delivery"] = delivery
            state["status"] = "Email Sent"
            if result.get("status") == "no_reviews":   # page showed no reviews at all: let the admin double-check
                _alert_admin("Flipkart report: no reviews found",
                             f"Request {request.request_id} for {resolved_name}: {result.get('failure_reason') or 'no review entries found'}")
        elif result.get("status") in ("success", "partial") and workbook.is_file():
            state["status"] = "Partial" if result["status"] == "partial" else "Completed"
            # Store the report (and its review data) BEFORE delivering it.
            try:
                saved = drive_store.save_report(request.recipient_email, resolved_url, resolved_name, workbook,
                                                request.request_id,
                                                existing["file_id"] if existing else None)
                state["drive"] = {"file_name": (saved or {}).get("file_name"), "updates": (saved or {}).get("updates")}
            except Exception as exc:
                log.error("Report could not be stored in Drive (%s)", type(exc).__name__)
                state["drive_error"] = type(exc).__name__
                _alert_admin("Flipkart report: Drive save failed",
                             f"Request {request.request_id} for {resolved_name}: {type(exc).__name__}. "
                             "The report was still e-mailed, but it is not stored in Drive.")
            delivery = _send_with_retry(request.request_id, request.recipient_email, resolved_name,
                                           request.start_date.isoformat(), request.end_date.isoformat(), workbook,
                                           result.get("valid_reviews", 0), result["status"],
                                           request.duration, request.duration_unit)
            state["email_delivery"] = delivery
            state["status"] = "Email Sent" if state["status"] == "Completed" else "Partial"
        else:
            state["status"] = "Failed"
            _alert_admin("Flipkart report request failed", f"Request {request.request_id} for {resolved_name} ended with status {result.get('status')}: {result.get('failure_reason') or 'no usable workbook was generated'}")
        status_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        return 0 if state["status"] in ("Completed", "Email Sent", "Partial") else 2
    except Exception as exc:
        logging.getLogger("fk").exception("Request %s failed", request.request_id)
        state.update(status="Failed", error=f"{type(exc).__name__}: {str(exc)[:400]}",
                     completed_at=datetime.now().isoformat(timespec="seconds"))
        status_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        _alert_admin("Flipkart report request failed", f"Request {request.request_id} failed: {type(exc).__name__}: {str(exc)[:250]}")
        return 2
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        _cleanup_request_temps(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-env", action="store_true", help="read request parameters from the environment")
    for key in ("REQUEST_ID", "RECIPIENT_EMAIL", "DURATION", "DURATION_UNIT"):
        parser.add_argument("--" + key.lower().replace("_", "-"), required=False)
    parser.add_argument("--product-url", default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    args = vars(parser.parse_args(argv))
    keys = ("REQUEST_ID", "RECIPIENT_EMAIL", "DURATION", "DURATION_UNIT")
    data = {key: (os.environ.get(key, "") if args["from_env"] else args[key.lower()] or "") for key in keys}
    data.update(PRODUCT_URL=(os.environ.get("PRODUCT_URL", "") if args["from_env"] else args["product_url"] or ""),
                START_DATE=(os.environ.get("START_DATE", "") if args["from_env"] else args["start_date"] or ""),
                END_DATE=(os.environ.get("END_DATE", "") if args["from_env"] else args["end_date"] or ""))
    try:
        return run(data)
    except (ValueError, OSError) as exc:
        log.error("Invalid request: %s", exc)
        request_id = data.get("REQUEST_ID", "")
        if re.fullmatch(r"REQ-[0-9]{8}-[A-Za-z0-9_-]{1,40}", request_id):
            try:
                out = _safe_request_dir(request_id)
                (out / "request_status.json").write_text(json.dumps({
                    "request_id": request_id, "status": "Failed", "error": str(exc)[:400],
                    "completed_at": datetime.now().isoformat(timespec="seconds")}, indent=2), encoding="utf-8")
            except OSError:
                log.error("Could not record failed request status")
        _alert_admin("Flipkart report request validation failed", f"Request {request_id or '(missing id)'}: {str(exc)[:250]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())