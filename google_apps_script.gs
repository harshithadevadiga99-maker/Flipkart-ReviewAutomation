/**
 * Request queue + GitHub Actions dispatcher for the Flipkart review automation.
 *
 * Flow
 *   1. A Form submission fires the installable onFormSubmit trigger. The request is validated, given a
 *      Request ID and stored on its own sheet row as "Queued" (nothing is sent to GitHub yet).
 *   2. processQueue_() starts the OLDEST queued request only when no other request is running, so requests
 *      always run strictly one after another, in submission order. Nothing is ever cancelled or dropped.
 *      The data for each run is rebuilt from the sheet row itself (no separate payload column).
 *   3. When the GitHub job finishes it calls doPost (the web-app callback). The row gets its final status
 *      (Succeeded / Failed), a failed run is retried automatically up to MAX_ATTEMPTS, and the next queued
 *      request is started immediately.
 *   4. A 5-minute timer (queueTick) is the safety net: it starts the next request if a callback was missed,
 *      retries a failed GitHub dispatch, and marks a request Failed if no result arrives in time.
 *   5. REPORT STORAGE (Drive): the same web app also stores the Excel reports for GitHub, because a GitHub
 *      run forgets everything when it ends. Reports are kept in
 *      "Flipkart Review Reports/<email>/<product>.xlsx" and "Flipkart Review Reports/_index.json" maps
 *      (email + product) to the file. See driveAction_ at the end of this file.
 *      After pasting this version: run authorizeDrive once from the editor (accept the Drive permission),
 *      then Deploy > Manage deployments > Edit > New version.
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
const STATUS_HEADERS = ["Request ID", "Product Name", "Start Date", "End Date", "Request Status", "Status Details",
  "Updated At", "Attempts", "Dispatched At", "Dispatch Failures"];
const QUEUE_DEFAULTS = {QUEUE_STALE_MINUTES: 150, MAX_ATTEMPTS: 2, MAX_DISPATCH_FAILURES: 5};
const SUCCESS_STATUSES = ["Succeeded", "Email Sent", "Completed", "Partial"];

// ---------------------------------------------------------------------------------------------- entry points
function onFormSubmit(e) {
  console.log("Script version: 2026-11-27-drive");   // if this line is missing from the log, an OLD copy of the script is running
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const sheet = enqueueSubmission_(e);
    if (sheet) {
      try { processQueue_(sheet); }
      catch (err) { console.error("processQueue_ failed after enqueue: " + (err && err.message || err) + " | " + (err && err.stack || "")); }  // row stays Queued; the timer retries
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
  if (payload.action) return driveAction_(payload);      // report storage in Drive (end of this file); not a status callback
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
    const rawStatus = String(payload.status || "Failed");
    const current = String(sheet.getRange(row, cols["Request Status"]).getValue());
    const attemptsCell = sheet.getRange(row, cols["Attempts"]).getValue();

    if (attemptsCell === "" || attemptsCell === null) {
      // A request created before the queue existed: write the result straight through, as before.
      writeStatus_(sheet, row, cols, {"Request Status": rawStatus, "Status Details": details, "Updated At": new Date()});
    } else if (current === "Running" || current === "Failed") {
      const attempts = Number(attemptsCell) || 1;
      const maxAttempts = queueSetting_("MAX_ATTEMPTS");
      if (SUCCESS_STATUSES.indexOf(rawStatus) >= 0) {
        writeStatus_(sheet, row, cols, {"Request Status": "Succeeded", "Status Details": details, "Updated At": new Date()});
      } else if (current === "Running" && attempts < maxAttempts) {
        writeStatus_(sheet, row, cols, {"Request Status": "Queued", "Updated At": new Date(),
          "Status Details": ("Attempt " + attempts + " of " + maxAttempts + " failed: " + details + " - retrying automatically").slice(0, 500)});
      } else {
        writeStatus_(sheet, row, cols, {"Request Status": "Failed", "Status Details": details, "Updated At": new Date()});
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
    const pick = (label) => pickValue_(vals, label);
    const durationRaw = pick("Duration");
    let unit = pick("Duration Unit").toLowerCase().replace(/[^a-z]/g, "");
    if (unit && !unit.endsWith("s")) unit += "s";
    const recipient = recipientFromForm_(vals);
    const productUrl = pick("Flipkart Product URL");
    if (!productUrl || !validProductUrl_(productUrl)) throw new Error("Enter a valid HTTPS Flipkart product URL");
    if (!/^\d+$/.test(durationRaw) || Number(durationRaw) < 1 || Number(durationRaw) > 120) throw new Error("Duration must be an integer from 1 to 120");
    if (["weeks", "months", "years"].indexOf(unit) < 0) throw new Error("Duration Unit must be Weeks, Months, or Years");
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(recipient)) throw new Error("Collect respondent email addresses in Google Forms");

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

    // One combined Duration value, e.g. "2 Weeks" or "1 Month".
    let unitLabel = unit.charAt(0).toUpperCase() + unit.slice(1);
    if (n === 1) unitLabel = unitLabel.slice(0, -1);
    setDurationCell_(sheet, row, durationRaw, n + " " + unitLabel);

    writeStatus_(sheet, row, cols, {"Request ID": id, "Product Name": productNameFromUrl_(productUrl),
      "Start Date": startDate, "End Date": endDate,
      "Request Status": "Queued", "Updated At": new Date(),
      "Status Details": "Queued; it starts automatically when earlier requests have finished",
      "Attempts": 0, "Dispatched At": "", "Dispatch Failures": 0});
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
  const headers = sheet.getRange(1, 1, 1, Math.max(sheet.getLastColumn(), 1)).getValues()[0];
  const tz = sheet.getParent().getSpreadsheetTimeZone();
  const maxAttempts = queueSetting_("MAX_ATTEMPTS");
  const staleMs = queueSetting_("QUEUE_STALE_MINUTES") * 60000;
  const maxFailures = queueSetting_("MAX_DISPATCH_FAILURES");
  for (let guard = 0; guard < 100; guard++) {
    const rows = readQueueRows_(sheet, cols);
    const now = new Date();
    let changed = false;
    for (const r of rows) {
      if (r.status !== "Running") continue;
      let started = r.dispatchedAt instanceof Date ? r.dispatchedAt : new Date(r.dispatchedAt);
      if (isNaN(started.getTime())) started = r.updatedAt instanceof Date ? r.updatedAt : new Date(r.updatedAt);
      const age = isNaN(started.getTime()) ? 0 : now.getTime() - started.getTime();   // unknown start time: never treat as stale
      if (age > staleMs) {
        writeStatus_(sheet, r.row, cols, {"Request Status": "Failed", "Updated At": new Date(),
          "Status Details": "No result was received in time; please check the run history"});
        changed = true;
      } else {
        return "busy";
      }
    }
    if (changed) continue;
    const next = rows.find(r => r.status === "Queued");
    if (!next) return "idle";
    const inputs = inputsFromRow_(headers, next.values, cols, tz);
    if (!inputs) {
      writeStatus_(sheet, next.row, cols, {"Request Status": "Failed", "Updated At": new Date(),
        "Status Details": "The request data in this row is incomplete; please submit the form again"});
      continue;
    }
    try {
      dispatchWorkflow_(inputs);
    } catch (err) {
      const failures = next.failures + 1;
      const message = String(err && err.message || err).slice(0, 300);
      if (failures >= maxFailures) {
        writeStatus_(sheet, next.row, cols, {"Request Status": "Failed", "Dispatch Failures": failures, "Updated At": new Date(),
          "Status Details": ("Could not start the report after " + failures + " tries: " + message).slice(0, 500)});
        continue;                                   // do not let one broken request block the others
      }
      writeStatus_(sheet, next.row, cols, {"Dispatch Failures": failures, "Updated At": new Date(),
        "Status Details": ("Could not start the report yet (try " + failures + " of " + maxFailures + "): " + message).slice(0, 500)});
      return "retry later";
    }
    const attempt = next.attempts + 1;
    writeStatus_(sheet, next.row, cols, {"Request Status": "Running", "Attempts": attempt, "Dispatched At": new Date(),
      "Dispatch Failures": 0, "Updated At": new Date(),
      "Status Details": "Report is being generated (attempt " + attempt + " of " + maxAttempts + ")"});
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
      values: v,
      status: String(v[cols["Request Status"] - 1]),
      attempts: Number(attempts) || 0,
      failures: Number(v[cols["Dispatch Failures"] - 1]) || 0,
      dispatchedAt: v[cols["Dispatched At"] - 1],
      updatedAt: v[cols["Updated At"] - 1]
    });
  });
  return out;
}

/**
 * Rebuilds the workflow inputs from one sheet row, so no separate payload column is needed.
 * Duration is read as "2 Weeks" (new rows) or as a number plus the Duration Unit column (older rows).
 * Returns null if anything required is missing.
 */
function inputsFromRow_(headers, values, cols, tz) {
  const byTitle = (title) => rowValueByTitle_(headers, values, title);
  const dateText = (v) => v instanceof Date ? Utilities.formatDate(v, tz, "yyyy-MM-dd") : String(v || "").trim();

  const requestId = String(values[cols["Request ID"] - 1] || "").trim();
  const productUrl = String(byTitle("Flipkart Product URL") || "").trim();
  const recipient = String(byTitle("Email Address") || "").trim();
  const startDate = dateText(values[cols["Start Date"] - 1]);
  const endDate = dateText(values[cols["End Date"] - 1]);

  const dm = String(byTitle("Duration") || "").trim().match(/^(\d+)\s*([A-Za-z]+)?$/);
  if (!dm) return null;
  let unit = String(dm[2] || byTitle("Duration Unit") || "").toLowerCase().replace(/[^a-z]/g, "");
  if (unit && !unit.endsWith("s")) unit += "s";
  if (["weeks", "months", "years"].indexOf(unit) < 0) return null;

  if (!requestId || !validProductUrl_(productUrl) || !recipient || !startDate || !endDate) return null;
  return buildWorkflowInputs_({requestId: requestId, productUrl: productUrl, duration: Number(dm[1]),
    durationUnit: unit, recipientEmail: recipient, startDate: startDate, endDate: endDate});
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

/** Normalizes a title: lowercase, punctuation and extra spaces removed. */
function normTitle_(s) {
  return String(s).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

/**
 * Finds a form answer by question title in the form-submit event. Ignores case, spaces and punctuation,
 * and checks every matching column and value, so renamed or duplicate columns do not hide the answer.
 */
function pickValue_(values, label) {
  const target = normTitle_(label);
  const keys = Object.keys(values);
  for (let i = 0; i < keys.length; i++) {
    if (normTitle_(keys[i]) !== target) continue;
    const arr = [].concat(values[keys[i]]);
    for (let j = 0; j < arr.length; j++) {
      const t = String(arr[j] || "").trim();
      if (t) return t;
    }
  }
  return "";
}

/** Same idea for a sheet row: the first non-empty cell under a matching header. */
function rowValueByTitle_(headers, values, title) {
  const target = normTitle_(title);
  for (let i = 0; i < headers.length; i++) {
    if (normTitle_(headers[i]) !== target) continue;
    const v = values[i];
    if (v !== "" && v !== null && v !== undefined) return v;
  }
  return "";
}

// The respondent email is collected automatically by Google Forms ("Email address").
function recipientFromForm_(values) {
  return pickValue_(values, "Email Address");
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

/**
 * Gets the product name for a Flipkart URL.
 * 1) Tries the page title. Flipkart sometimes blocks Google's servers, so this can fail.
 * 2) Falls back to the readable text in the URL path, which always works.
 */
function productNameFromUrl_(url) {
  const clean = String(url || "").trim();
  try {
    const resp = UrlFetchApp.fetch(clean, {
      muteHttpExceptions: true,
      followRedirects: true,
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-IN,en;q=0.9"
      }
    });
    const code = resp.getResponseCode();
    if (code === 200) {
      const m = resp.getContentText().match(/<title[^>]*>([\s\S]*?)<\/title>/i);
      if (m) {
        let t = decodeEntities_(m[1]).replace(/\s+/g, " ").trim();
        t = t.split(/\s+Price in India\b/i)[0];
        t = t.split(/\s+-\s+Buy\b/i)[0];
        t = t.split(/\s*:\s*Flipkart\.com/i)[0];
        t = t.trim();
        // Ignore robot-check pages and generic store titles.
        if (t && !/recaptcha|access denied|just a moment|robot|online shopping|^flipkart(\.com)?$/i.test(t)) {
          const name = shortName_(t, 5);
          if (name) { console.log("Product name from page title: " + name); return name; }
        }
      }
    } else {
      console.log("Product page returned HTTP " + code + "; using the link text instead");
    }
  } catch (err) {
    console.log("Product name fetch failed: " + err);
  }
  const fromLink = productNameFromSlug_(clean);
  console.log("Product name from link: " + fromLink);
  return fromLink;
}

// Never returns an empty name: uses the readable part of the link, then the item id, then a generic label.
function productNameFromSlug_(url) {
  const text = String(url || "").trim();
  const m = text.match(/^https:\/\/[^\/?#]+\/([^\/?#]+)\/p\//i);
  let name = "";
  if (m) {
    let slug = m[1];
    try { slug = decodeURIComponent(slug); } catch (_) {}
    name = shortName_(slug.replace(/-+/g, " ").replace(/\b([a-z])/g, c => c.toUpperCase()), 4);
  }
  if (name) return name;
  const id = text.match(/\/p\/(itm[a-z0-9]+)/i);
  return id ? "Flipkart " + id[1] : "Flipkart product";
}

// Run from the editor to test: put a real product link below, click Run, then read the Execution log.
function testProductName() {
  const url = "PASTE A FULL FLIPKART PRODUCT LINK HERE";
  Logger.log("Name: " + productNameFromUrl_(url));
}

// Short display name: cut at the first comma, pipe, bracket, " with " or " - ", then keep at most maxWords words.
// "boAt Airdopes Plus 223,Spatial Audio,60H Battery,..." becomes "boAt Airdopes Plus 223".
function shortName_(text, maxWords) {
  let t = String(text || "").replace(/\s+/g, " ").trim();
  t = t.split(/\s*[,|(\[]\s*|\s+with\s+|\s+-\s+/i)[0].trim();
  return t.split(" ").filter(Boolean).slice(0, maxWords || 5).join(" ").slice(0, 60);
}

function decodeEntities_(s) {
  return String(s)
    .replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&#x27;|&#39;|&apos;/g, "'");
}

/**
 * Replaces the form's "Duration" cell in this row with the combined text, e.g. "2 Weeks".
 * Only the column whose current value equals the number typed in the form is changed.
 */
function setDurationCell_(sheet, row, durationRaw, combined) {
  const width = sheet.getLastColumn();
  const headers = sheet.getRange(1, 1, 1, width).getValues()[0];
  for (let i = 0; i < headers.length; i++) {
    if (normTitle_(headers[i]) !== "duration") continue;
    const cell = sheet.getRange(row, i + 1);
    if (String(cell.getDisplayValue()).trim() === String(durationRaw).trim()) {
      try { cell.setValue(combined); } catch (err) { console.error("Could not write Duration: " + (err && err.message || err)); }
      return;
    }
  }
}

function ensureStatusColumns_(sheet) {
  const width = Math.max(sheet.getLastColumn(), 1);
  const headers = sheet.getRange(1, 1, 1, width).getValues()[0].map(String);
  STATUS_HEADERS.forEach(h => {
    if (headers.indexOf(h) < 0) {
      headers.push(h);
      try { sheet.getRange(1, headers.length).setValue(h); }
      catch (err) { console.error("Could not add header '" + h + "': " + (err && err.message || err)); }
    }
  });
  const map = {}; headers.forEach((h, i) => { if (STATUS_HEADERS.indexOf(h) >= 0) map[h] = i + 1; });
  return map;
}

function writeStatus_(sheet, row, cols, values) {
  Object.keys(values).forEach(k => {
    if (!cols[k]) return;
    let v = values[k];
    // A Date object makes Apps Script set a number format, which Table columns refuse. Write timestamps as text instead.
    if (v instanceof Date) v = Utilities.formatDate(v, Session.getScriptTimeZone(), "yyyy-MM-dd HH:mm:ss");
    try {
      sheet.getRange(row, cols[k]).setValue(v);
    } catch (err) {
      console.error("Could not write '" + k + "': " + (err && err.message || err));   // one cell must never break the queue
    }
  });
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

// ---------------------------------------------------------------------------------------------- report storage (Drive)
const DRIVE_ROOT_NAME = "Flipkart Review Reports";
const INDEX_NAME = "_index.json";
const XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

// Run once from the editor to grant Drive access.
function authorizeDrive() { rootFolder_(); }

function driveAction_(p) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(25000)) return json_({ok: false, error: "busy"});
  try {
    const email = String(p.email || "").trim().toLowerCase();
    const key = String(p.key || "");
    if (p.action !== "drive_download") {
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return json_({ok: false, error: "invalid email"});
      if (!/^[a-f0-9]{64}$/.test(key)) return json_({ok: false, error: "invalid key"});
    }
    const root = rootFolder_();
    const indexFile = indexFile_(root);
    const index = JSON.parse(indexFile.getBlob().getDataAsString() || "{}");

    if (p.action === "drive_lookup") {
      const rec = index[key];
      if (!rec) return json_({ok: true, found: false});
      try {
        const f = DriveApp.getFileById(rec.file_id);
        if (f.isTrashed()) return json_({ok: true, found: false});
      } catch (_) { return json_({ok: true, found: false}); }
      return json_({ok: true, found: true, record: rec});
    }

    if (p.action === "drive_download") {
      const fileId = String(p.file_id || "");
      const known = Object.keys(index).some(k => index[k].file_id === fileId);     // only files this system created
      if (!known) return json_({ok: false, error: "unknown file"});
      return json_({ok: true, data: Utilities.base64Encode(DriveApp.getFileById(fileId).getBlob().getBytes())});
    }

    if (p.action === "drive_save") {
      const folder = emailFolder_(root, email);
      let name = String(p.file_name || "report.xlsx").replace(/[\\/*?:"<>|]/g, "").slice(0, 120) || "report.xlsx";
      const old = index[key];
      const replaceId = String(p.replace_file_id || "");
      // a different product with the same short name must not overwrite another report
      const clash = Object.keys(index).some(k => k !== key && index[k].folder_id === folder.getId() && index[k].file_name === name);
      if (clash) name = name.replace(/\.xlsx$/i, "") + " (" + key.slice(0, 6) + ").xlsx";
      const blob = Utilities.newBlob(Utilities.base64Decode(String(p.data || "")), XLSX_MIME, name);
      const created = folder.createFile(blob);
      const now = new Date().toISOString();
      const ids = (old && old.request_ids || []).concat([String(p.request_id || "")]);
      const rec = {email: email, product_url: String(p.product_url || ""), product_name: String(p.product_name || ""),
        file_id: created.getId(), file_name: name, folder_id: folder.getId(),
        created_at: old ? old.created_at : now, updated_at: now, updates: ids.length, request_ids: ids};
      index[key] = rec;
      indexFile.setContent(JSON.stringify(index, null, 1));
      // the superseded copy goes to the Drive trash (recoverable for 30 days) only after the new one is stored
      if (replaceId && replaceId !== created.getId() && old && old.file_id === replaceId) {
        try { DriveApp.getFileById(replaceId).setTrashed(true); } catch (_) {}
      }
      return json_({ok: true, record: rec});
    }
    return json_({ok: false, error: "unknown action"});
  } catch (err) {
    return json_({ok: false, error: String(err && err.message || err).slice(0, 200)});
  } finally {
    lock.releaseLock();
  }
}

function rootFolder_() {
  const it = DriveApp.getFoldersByName(DRIVE_ROOT_NAME);
  return it.hasNext() ? it.next() : DriveApp.createFolder(DRIVE_ROOT_NAME);
}

function emailFolder_(root, email) {
  const it = root.getFoldersByName(email);
  return it.hasNext() ? it.next() : root.createFolder(email);
}

function indexFile_(root) {
  const it = root.getFilesByName(INDEX_NAME);
  return it.hasNext() ? it.next() : root.createFile(INDEX_NAME, "{}", MimeType.PLAIN_TEXT);
}