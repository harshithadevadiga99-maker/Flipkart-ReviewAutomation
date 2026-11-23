/**
 * Request queue + GitHub Actions dispatcher for the Flipkart review automation.
 *
 * Flow
 *   1. A Form submission fires the installable onFormSubmit trigger. The request is validated, given a
 *      Request ID and stored on its own sheet row as "Queued" (nothing is sent to GitHub yet).
 *   2. processQueue_() starts the OLDEST queued request only when no other request is running, so requests
 *      always run strictly one after another, in submission order. Nothing is ever cancelled or dropped.
 *   3. When the GitHub job finishes it calls doPost (the web-app callback). The row gets its final status
 *      (Succeeded / Failed), a failed run is retried automatically up to MAX_ATTEMPTS, and the next queued
 *      request is started immediately.
 *   4. A 5-minute timer (queueTick) is the safety net: it starts the next request if a callback was missed,
 *      retries a failed GitHub dispatch, and marks a request Failed if no result arrives in time.
 *
 * Script Properties
 *   SPREADSHEET_ID, RESPONSE_SHEET_NAME, SHEETS_CALLBACK_SECRET
 *   GITHUB_TOKEN          fine-grained token, this repo only, Actions: read and write
 *   GITHUB_REPOSITORY     owner/repository
 *   GITHUB_WORKFLOW       optional, default flipkart-request.yml
 *   GITHUB_REF            optional, default main
 *   MAX_ATTEMPTS          optional, default 2  (1 = never retry a failed run)
 *   QUEUE_STALE_MINUTES   optional, default 150 (a run with no result after this long is marked Failed)
 *   MAX_DISPATCH_FAILURES optional, default 5  (GitHub refusing the dispatch this many times = Failed)
 * Never put credentials in this source.
 */
const STATUS_HEADERS = ["Request ID", "Start Date", "End Date", "Request Status", "GitHub Actions Run", "Status Details",
  "Updated At", "Attempts", "Dispatched At", "Dispatch Failures", "Queue Payload"];
const QUEUE_DEFAULTS = {QUEUE_STALE_MINUTES: 150, MAX_ATTEMPTS: 2, MAX_DISPATCH_FAILURES: 5};
const SUCCESS_STATUSES = ["Succeeded", "Email Sent", "Completed", "Partial"];

// ---------------------------------------------------------------------------------------------- entry points
function onFormSubmit(e) {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const sheet = enqueueSubmission_(e);
    if (sheet) {
      try { processQueue_(sheet); }
      catch (err) { console.error("processQueue_ failed after enqueue: " + (err && err.message || err)); }  // row stays Queued; the timer retries
    }
  } finally {
    lock.releaseLock();
  }
}

// Time-driven safety net (see setupQueueTrigger).
function queueTick() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(20000)) return;
  try { processQueue_(); } finally { lock.releaseLock(); }
}

// Run ONCE from the editor: creates the 5-minute timer that drives queueTick.
function setupQueueTrigger() {
  ScriptApp.getProjectTriggers().forEach(t => { if (t.getHandlerFunction() === "queueTick") ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger("queueTick").timeBased().everyMinutes(5).create();
}

function doPost(e) {
  const props = PropertiesService.getScriptProperties();
  const expected = props.getProperty("SHEETS_CALLBACK_SECRET");
  let payload = {};
  try { payload = JSON.parse(e && e.postData && e.postData.contents || "{}"); } catch (_) { return json_({ok: false, error: "bad request"}); }
  if (!expected || !payload.secret || payload.secret !== expected) return json_({ok: false, error: "unauthorized"});
  const id = String(payload.request_id || "");
  if (!/^REQ-\d{8}-[A-Za-z0-9_-]{1,40}$/.test(id)) return json_({ok: false, error: "invalid request id"});

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(25000)) return json_({ok: false, error: "busy"});      // the caller retries
  try {
    const sheet = responseSheet_();
    if (!sheet) return json_({ok: false, error: "response sheet not found"});
    const cols = ensureStatusColumns_(sheet);
    const found = sheet.getRange(2, cols["Request ID"], Math.max(sheet.getLastRow() - 1, 1), 1)
      .createTextFinder(id).matchEntireCell(true).findNext();
    if (!found) return json_({ok: false, error: "request not found"});
    const row = found.getRow();
    const details = String(payload.details || "").slice(0, 400);
    const link = String(payload.job_url || "");
    const rawStatus = String(payload.status || "Failed");
    const current = String(sheet.getRange(row, cols["Request Status"]).getValue());
    const attemptsCell = sheet.getRange(row, cols["Attempts"]).getValue();

    if (attemptsCell === "" || attemptsCell === null) {
      // A request created before the queue existed: write the result straight through, as before.
      writeStatus_(sheet, row, cols, {"Request Status": rawStatus, "Status Details": details,
        "GitHub Actions Run": link, "Updated At": new Date()});
    } else if (current === "Running" || current === "Failed") {
      const attempts = Number(attemptsCell) || 1;
      const maxAttempts = queueSetting_("MAX_ATTEMPTS");
      if (SUCCESS_STATUSES.indexOf(rawStatus) >= 0) {
        writeStatus_(sheet, row, cols, {"Request Status": "Succeeded", "Status Details": details,
          "GitHub Actions Run": link, "Updated At": new Date()});
      } else if (current === "Running" && attempts < maxAttempts) {
        writeStatus_(sheet, row, cols, {"Request Status": "Queued", "GitHub Actions Run": link, "Updated At": new Date(),
          "Status Details": ("Attempt " + attempts + " of " + maxAttempts + " failed: " + details + " - retrying automatically").slice(0, 500)});
      } else {
        writeStatus_(sheet, row, cols, {"Request Status": "Failed", "Status Details": details,
          "GitHub Actions Run": link, "Updated At": new Date()});
      }
    }   // anything else (already Succeeded, or waiting in the queue): a duplicate callback, ignored
    try { processQueue_(sheet); } catch (err) { console.error("processQueue_ failed after callback: " + (err && err.message || err)); }
    return json_({ok: true});
  } finally {
    lock.releaseLock();
  }
}

// ---------------------------------------------------------------------------------------------- the queue
function enqueueSubmission_(e) {
  let sheet, row, cols;
  try {
    if (!e || !e.range) throw new Error("Installable spreadsheet form-submit event is required");
    sheet = e.range.getSheet();
    row = e.range.getRow();
    cols = ensureStatusColumns_(sheet);
    if (sheet.getRange(row, cols["Request ID"]).getValue()) return null;      // this row was already handled by an earlier run
    const vals = e.namedValues || {};
    const pick = (label) => (vals[label] && vals[label][0] || "").trim();
    const durationRaw = pick("Duration");
    const unit = pick("Duration Unit").toLowerCase();
    const recipient = recipientFromForm_(vals);
    const productUrl = pick("Flipkart Product URL");
    if (!productUrl || !validProductUrl_(productUrl)) throw new Error("Enter a valid HTTPS Flipkart product URL");
    if (!/^\d+$/.test(durationRaw) || Number(durationRaw) < 1 || Number(durationRaw) > 120) throw new Error("Duration must be an integer from 1 to 120");
    if (!["weeks", "months", "years"].includes(unit)) throw new Error("Duration Unit must be Weeks, Months, or Years");
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(recipient)) throw new Error("Collect respondent email addresses in Google Forms or provide a valid Recipient Email Address");

    const now = new Date();
    const tz = Session.getScriptTimeZone();
    const end = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const start = new Date(end);
    const n = Number(durationRaw);
    if (unit === "weeks") start.setDate(start.getDate() - 7 * n);
    else subtractMonthsClamped_(start, unit === "months" ? n : n * 12);
    const startDate = Utilities.formatDate(start, tz, "yyyy-MM-dd");
    const endDate = Utilities.formatDate(end, tz, "yyyy-MM-dd");
    const id = nextRequestId_(now, tz);
    const inputs = buildWorkflowInputs_({requestId: id, productUrl: productUrl,
      duration: n, durationUnit: unit, recipientEmail: recipient, startDate: startDate, endDate: endDate});
    writeStatus_(sheet, row, cols, {"Request ID": id, "Start Date": startDate, "End Date": endDate,
      "Request Status": "Queued", "GitHub Actions Run": "", "Updated At": new Date(),
      "Status Details": "Queued; it starts automatically when earlier requests have finished",
      "Attempts": 0, "Dispatched At": "", "Dispatch Failures": 0, "Queue Payload": JSON.stringify(inputs)});
    return sheet;
  } catch (err) {
    if (sheet && row && cols) writeStatus_(sheet, row, cols, {
      "Request Status": "Failed", "Status Details": String(err && err.message || err).slice(0, 500), "Updated At": new Date()});
    throw err;
  }
}

// Starts the oldest queued request when nothing else is running. The caller holds the script lock.
function processQueue_(sheetOpt) {
  const sheet = sheetOpt || responseSheet_();
  if (!sheet) return "no sheet";
  const cols = ensureStatusColumns_(sheet);
  const maxAttempts = queueSetting_("MAX_ATTEMPTS");
  const staleMs = queueSetting_("QUEUE_STALE_MINUTES") * 60000;
  const maxFailures = queueSetting_("MAX_DISPATCH_FAILURES");
  for (let guard = 0; guard < 100; guard++) {
    const rows = readQueueRows_(sheet, cols);
    const now = new Date();
    let changed = false;
    for (const r of rows) {
      if (r.status !== "Running") continue;
      const age = r.dispatchedAt instanceof Date ? now.getTime() - r.dispatchedAt.getTime() : Infinity;
      if (age > staleMs) {
        writeStatus_(sheet, r.row, cols, {"Request Status": "Failed", "Updated At": new Date(),
          "Status Details": "No result was received from GitHub Actions in time; check the run history"});
        changed = true;
      } else {
        return "busy";
      }
    }
    if (changed) continue;
    const next = rows.find(r => r.status === "Queued");
    if (!next) return "idle";
    let inputs;
    try { inputs = JSON.parse(next.payload); } catch (_) { inputs = null; }
    if (!inputs || !inputs.REQUEST_ID) {
      writeStatus_(sheet, next.row, cols, {"Request Status": "Failed", "Updated At": new Date(),
        "Status Details": "The queued request data is missing; please submit the form again"});
      continue;
    }
    try {
      dispatchWorkflow_(inputs);
    } catch (err) {
      const failures = next.failures + 1;
      const message = String(err && err.message || err).slice(0, 300);
      if (failures >= maxFailures) {
        writeStatus_(sheet, next.row, cols, {"Request Status": "Failed", "Dispatch Failures": failures, "Updated At": new Date(),
          "Status Details": ("Could not start the GitHub workflow after " + failures + " tries: " + message).slice(0, 500)});
        continue;                                   // do not let one broken request block the others
      }
      writeStatus_(sheet, next.row, cols, {"Dispatch Failures": failures, "Updated At": new Date(),
        "Status Details": ("Could not start the GitHub workflow yet (try " + failures + " of " + maxFailures + "): " + message).slice(0, 500)});
      return "retry later";
    }
    const attempt = next.attempts + 1;
    writeStatus_(sheet, next.row, cols, {"Request Status": "Running", "Attempts": attempt, "Dispatched At": new Date(),
      "Dispatch Failures": 0, "Updated At": new Date(),
      "Status Details": "Running on GitHub Actions (attempt " + attempt + " of " + maxAttempts + ")"});
    return "dispatched";
  }
  return "loop guard";
}

// Only rows created by the queue (they carry an Attempts number) are managed; older rows are left alone.
function readQueueRows_(sheet, cols) {
  const last = sheet.getLastRow();
  if (last < 2) return [];
  const values = sheet.getRange(2, 1, last - 1, sheet.getLastColumn()).getValues();
  const out = [];
  values.forEach((v, i) => {
    const attempts = v[cols["Attempts"] - 1];
    if (attempts === "" || attempts === null || attempts === undefined) return;
    out.push({
      row: i + 2,
      status: String(v[cols["Request Status"] - 1]),
      attempts: Number(attempts) || 0,
      failures: Number(v[cols["Dispatch Failures"] - 1]) || 0,
      dispatchedAt: v[cols["Dispatched At"] - 1],
      payload: String(v[cols["Queue Payload"] - 1] || "")
    });
  });
  return out;
}

function queueSetting_(name) {
  const raw = PropertiesService.getScriptProperties().getProperty(name);
  const n = Number(raw);
  return raw && isFinite(n) && n >= 1 ? Math.floor(n) : QUEUE_DEFAULTS[name];
}

function responseSheet_() {
  const ss = SpreadsheetApp.openById(requiredProp_("SPREADSHEET_ID"));
  return ss.getSheetByName(requiredProp_("RESPONSE_SHEET_NAME"));
}

// ---------------------------------------------------------------------------------------------- helpers
// workflow_dispatch input values must all be strings.
function buildWorkflowInputs_(fields) {
  return {
    REQUEST_ID: String(fields.requestId),
    PRODUCT_URL: String(fields.productUrl),
    DURATION: String(fields.duration),
    DURATION_UNIT: String(fields.durationUnit),
    RECIPIENT_EMAIL: String(fields.recipientEmail),
    START_DATE: String(fields.startDate),
    END_DATE: String(fields.endDate)
  };
}

function dispatchWorkflow_(inputs) {
  const props = PropertiesService.getScriptProperties();
  const repo = requiredProp_("GITHUB_REPOSITORY");
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) throw new Error("GITHUB_REPOSITORY must look like owner/repository");
  const workflow = props.getProperty("GITHUB_WORKFLOW") || "flipkart-request.yml";
  const ref = props.getProperty("GITHUB_REF") || "main";
  const url = "https://api.github.com/repos/" + repo + "/actions/workflows/" + encodeURIComponent(workflow) + "/dispatches";
  const response = UrlFetchApp.fetch(url, {
    method: "post",
    contentType: "application/json",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: "Bearer " + requiredProp_("GITHUB_TOKEN"),
      "X-GitHub-Api-Version": "2022-11-28"
    },
    payload: JSON.stringify({ref: ref, inputs: inputs}),
    muteHttpExceptions: true
  });
  const code = response.getResponseCode();
  if (code !== 204) {
    throw new Error("GitHub rejected the workflow dispatch (HTTP " + code + "): " + response.getContentText().slice(0, 300));
  }
}

function recipientFromForm_(values) {
  const supplied = (values["Recipient Email Address"] || [""])[0].trim();
  const collected = (values["Email Address"] || values["Email address"] || [""])[0].trim();
  return supplied || collected;
}

function validProductUrl_(value) {
  const m = /^https:\/\/([^\/?#@:]+)(?::(\d+))?(\/[^?#]*)?(?:[?#].*)?$/i.exec(String(value).trim());
  if (!m) return false;
  const host = m[1].toLowerCase();
  const port = m[2] || "";
  const path = m[3] || "";
  return (port === "" || port === "443") &&
    (host === "flipkart.com" || host.endsWith(".flipkart.com")) && /\/p\//.test(path);
}

function ensureStatusColumns_(sheet) {
  const width = Math.max(sheet.getLastColumn(), 1);
  const headers = sheet.getRange(1, 1, 1, width).getValues()[0].map(String);
  STATUS_HEADERS.forEach(h => { if (headers.indexOf(h) < 0) { headers.push(h); sheet.getRange(1, headers.length).setValue(h); } });
  const map = {}; headers.forEach((h, i) => { if (STATUS_HEADERS.indexOf(h) >= 0) map[h] = i + 1; });
  return map;
}

function writeStatus_(sheet, row, cols, values) {
  Object.keys(values).forEach(k => { if (cols[k]) sheet.getRange(row, cols[k]).setValue(values[k]); });
}

function nextRequestId_(now, tz) {
  const day = Utilities.formatDate(now, tz, "yyyyMMdd");
  const props = PropertiesService.getScriptProperties();
  const key = "REQUEST_COUNTER_" + day;
  const next = Number(props.getProperty(key) || "0") + 1;
  props.setProperty(key, String(next));
  return "REQ-" + day + "-" + String(next).padStart(3, "0");
}

function subtractMonthsClamped_(day, months) {
  const originalDay = day.getDate();
  day.setDate(1);
  day.setMonth(day.getMonth() - months);
  const last = new Date(day.getFullYear(), day.getMonth() + 1, 0).getDate();
  day.setDate(Math.min(originalDay, last));
}

function requiredProp_(name) {
  const value = PropertiesService.getScriptProperties().getProperty(name);
  if (!value) throw new Error("Missing Apps Script property: " + name);
  return value;
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
