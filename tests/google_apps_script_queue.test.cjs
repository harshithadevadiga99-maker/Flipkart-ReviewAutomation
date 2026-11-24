// Offline tests for the request queue in google_apps_script.gs (Google services are mocked).
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "..", "google_apps_script.gs"), "utf8");
const SECRET = "test-secret";
const FORM_HEADERS = ["Timestamp", "Flipkart Product URL", "Duration", "Duration Unit", "Email Address"];
const GOOD_URL = "https://www.flipkart.com/boat-airdopes/p/itm123?pid=ABC&lid=XYZ";

class MockSheet {
  constructor(headers) { this.data = [headers.slice()]; }
  lastColumn() { return Math.max(...this.data.map(r => r.length)); }
  get(r, c) { const v = (this.data[r - 1] || [])[c - 1]; return v === undefined ? "" : v; }
  set(r, c, v) {
    while (this.data.length < r) this.data.push([]);
    const row = this.data[r - 1];
    while (row.length < c) row.push("");
    row[c - 1] = v;
  }
  getLastColumn() { return this.lastColumn(); }
  getLastRow() { return this.data.length; }
  getRange(r, c, nr = 1, nc = 1) {
    const sheet = this;
    return {
      getRow: () => r,
      getSheet: () => sheet,
      getValue: () => sheet.get(r, c),
      setValue: (v) => sheet.set(r, c, v),
      getValues: () => Array.from({length: nr}, (_, i) => Array.from({length: nc}, (_, j) => sheet.get(r + i, c + j))),
      createTextFinder: (text) => ({
        matchEntireCell() { return this; },
        findNext() {
          for (let i = 0; i < nr; i++) if (String(sheet.get(r + i, c)) === text) return {getRow: () => r + i};
          return null;
        },
      }),
    };
  }
}

function pad(n, w = 2) { return String(n).padStart(w, "0"); }

function makeEnv(initialProps = {}) {
  const state = {now: Date.UTC(2026, 10, 23, 10, 0, 0), fetchCalls: [], fetchCode: 204, lockBusy: false, triggers: []};
  const RealDate = Date;
  class FakeDate extends RealDate {
    constructor(...args) { if (args.length === 0) super(state.now); else super(...args); }
    static now() { return state.now; }
  }
  const store = Object.assign({
    SPREADSHEET_ID: "sheet-id", RESPONSE_SHEET_NAME: "Form Responses 1", SHEETS_CALLBACK_SECRET: SECRET,
    GITHUB_TOKEN: "t", GITHUB_REPOSITORY: "owner/repo",
  }, initialProps);
  const sheet = new MockSheet(FORM_HEADERS);
  const ctx = {
    Date: FakeDate, console: {error() {}, log() {}},
    Session: {getScriptTimeZone: () => "UTC"},
    Utilities: {
      formatDate(d, tz, fmt) {
        const y = d.getUTCFullYear(), m = pad(d.getUTCMonth() + 1), day = pad(d.getUTCDate());
        return fmt === "yyyyMMdd" ? `${y}${m}${day}` : `${y}-${m}-${day}`;
      },
    },
    PropertiesService: {getScriptProperties: () => ({
      getProperty: (k) => (k in store ? store[k] : null), setProperty: (k, v) => { store[k] = v; }})},
    LockService: {getScriptLock: () => ({
      waitLock() {}, tryLock: () => !state.lockBusy, releaseLock() {}})},
    SpreadsheetApp: {openById: () => ({getSheetByName: (n) => (n === store.RESPONSE_SHEET_NAME ? sheet : null)})},
    UrlFetchApp: {fetch(url, opts) {
      const body = JSON.parse(opts.payload);
      state.fetchCalls.push({url, inputs: body.inputs, ref: body.ref});
      return {getResponseCode: () => state.fetchCode, getContentText: () => "gh says no"};
    }},
    ContentService: {
      MimeType: {JSON: "json"},
      createTextOutput: (s) => ({setMimeType() { return this; }, text: s}),
    },
    ScriptApp: {
      getProjectTriggers: () => state.triggers,
      deleteTrigger: (t) => { state.triggers = state.triggers.filter(x => x !== t); },
      newTrigger: (fn) => ({timeBased() { return this; }, everyMinutes(n) { this.n = n; return this; },
        create() { state.triggers.push({getHandlerFunction: () => fn, minutes: this.n}); }}),
    },
  };
  vm.createContext(ctx);
  vm.runInContext(SOURCE, ctx);

  const env = {ctx, sheet, state, store};
  env.col = (name) => sheet.data[0].indexOf(name) + 1;
  env.cell = (row, name) => sheet.get(row, env.col(name));
  env.status = (row) => env.cell(row, "Request Status");
  env.submit = (url = GOOD_URL, duration = "1", unit = "weeks", email = "user@example.com") => {
    const row = sheet.getLastRow() + 1;
    [new RealDate(state.now), url, duration, unit, email].forEach((v, i) => sheet.set(row, i + 1, v));
    ctx.onFormSubmit({range: sheet.getRange(row, 1, 1, 5), namedValues: {
      "Flipkart Product URL": [url], "Duration": [duration], "Duration Unit": [unit], "Email Address": [email]}});
    return row;
  };
  env.callback = (row, status, details = "", extra = {}) => {
    const out = ctx.doPost({postData: {contents: JSON.stringify(Object.assign({
      secret: SECRET, request_id: env.cell(row, "Request ID"), status, details, job_url: "https://github.com/run/1"}, extra))}});
    return JSON.parse(out.text);
  };
  env.advance = (minutes) => { state.now += minutes * 60000; };
  env.dispatched = () => state.fetchCalls.map(c => c.inputs.REQUEST_ID);
  return env;
}

test("a single request starts immediately with the exact workflow inputs", () => {
  const env = makeEnv();
  const row = env.submit(GOOD_URL, "3", "months", "me@example.com");
  assert.equal(env.status(row), "Running");
  assert.equal(env.state.fetchCalls.length, 1);
  const call = env.state.fetchCalls[0];
  assert.match(call.url, /owner\/repo\/actions\/workflows\/flipkart-request\.yml\/dispatches$/);
  assert.equal(call.ref, "main");
  assert.deepEqual(Object.keys(call.inputs).sort(),
    ["DURATION", "DURATION_UNIT", "END_DATE", "PRODUCT_URL", "RECIPIENT_EMAIL", "REQUEST_ID", "START_DATE"]);
  assert.equal(call.inputs.DURATION, "3");
  assert.equal(call.inputs.START_DATE, "2026-08-23");
  assert.equal(call.inputs.END_DATE, "2026-11-23");
  assert.equal(call.inputs.RECIPIENT_EMAIL, "me@example.com");
  assert.equal(call.inputs.PRODUCT_URL, GOOD_URL);
  assert.equal(env.cell(row, "Request ID"), "REQ-20261123-001");
});

test("several requests at once: only one runs, the others wait Queued, none is lost", () => {
  const env = makeEnv();
  const rows = [env.submit(), env.submit(), env.submit(), env.submit()];
  assert.deepEqual(rows.map(r => env.status(r)), ["Running", "Queued", "Queued", "Queued"]);
  assert.equal(env.state.fetchCalls.length, 1);
  assert.deepEqual(rows.map(r => env.cell(r, "Request ID")),
    ["REQ-20261123-001", "REQ-20261123-002", "REQ-20261123-003", "REQ-20261123-004"]);
});

test("requests run strictly one after another, in submission order, driven by the callbacks", () => {
  const env = makeEnv();
  const rows = [env.submit(), env.submit(), env.submit()];
  assert.deepEqual(env.callback(rows[0], "Succeeded", "ok"), {ok: true});
  assert.deepEqual(rows.map(r => env.status(r)), ["Succeeded", "Running", "Queued"]);
  assert.equal(env.state.fetchCalls.length, 2);
  env.callback(rows[1], "Succeeded");
  assert.deepEqual(rows.map(r => env.status(r)), ["Succeeded", "Succeeded", "Running"]);
  env.callback(rows[2], "Succeeded");
  assert.deepEqual(rows.map(r => env.status(r)), ["Succeeded", "Succeeded", "Succeeded"]);
  assert.deepEqual(env.dispatched(), ["REQ-20261123-001", "REQ-20261123-002", "REQ-20261123-003"]);
  assert.equal(env.cell(rows[0], "GitHub Actions Run"), "https://github.com/run/1");
});

test("a failed run is retried automatically once, then reported Failed, and the queue keeps moving", () => {
  const env = makeEnv();
  const [a, b] = [env.submit(), env.submit()];
  env.callback(a, "Failed", "login blocked");
  assert.equal(env.status(a), "Running");                   // retried before the later request
  assert.equal(env.cell(a, "Attempts"), 2);
  assert.equal(env.status(b), "Queued");
  assert.deepEqual(env.dispatched(), ["REQ-20261123-001", "REQ-20261123-001"]);
  env.callback(a, "Failed", "login blocked again");
  assert.equal(env.status(a), "Failed");
  assert.match(env.cell(a, "Status Details"), /login blocked again/);
  assert.equal(env.status(b), "Running");                   // the next request is not blocked by the failure
});

test("MAX_ATTEMPTS=1 turns retries off", () => {
  const env = makeEnv({MAX_ATTEMPTS: "1"});
  const [a, b] = [env.submit(), env.submit()];
  env.callback(a, "Failed", "boom");
  assert.equal(env.status(a), "Failed");
  assert.equal(env.status(b), "Running");
  assert.equal(env.state.fetchCalls.length, 2);
});

test("a run that never reports back is marked Failed by the timer and the queue moves on", () => {
  const env = makeEnv();
  const [a, b] = [env.submit(), env.submit()];
  env.advance(60);
  env.ctx.queueTick();
  assert.equal(env.status(a), "Running");                   // still inside the allowed time
  assert.equal(env.status(b), "Queued");
  env.advance(100);                                          // 160 minutes in total > 150
  env.ctx.queueTick();
  assert.equal(env.status(a), "Failed");
  assert.match(env.cell(a, "Status Details"), /No result/);
  assert.equal(env.status(b), "Running");
});

test("a missed callback does not stall: the timer starts the next request once the stale run is cleared", () => {
  const env = makeEnv({QUEUE_STALE_MINUTES: "30"});
  const [a, b] = [env.submit(), env.submit()];
  env.advance(31);
  env.ctx.queueTick();
  assert.deepEqual([env.status(a), env.status(b)], ["Failed", "Running"]);
});

test("a late callback for a run already marked Failed still records the real result", () => {
  const env = makeEnv();
  const a = env.submit();
  env.advance(200);
  env.ctx.queueTick();
  assert.equal(env.status(a), "Failed");
  assert.deepEqual(env.callback(a, "Succeeded", "report sent"), {ok: true});
  assert.equal(env.status(a), "Succeeded");
});

test("GitHub refusing the dispatch keeps the request Queued and the timer retries it", () => {
  const env = makeEnv();
  env.state.fetchCode = 500;
  const a = env.submit();
  assert.equal(env.status(a), "Queued");
  assert.equal(env.cell(a, "Dispatch Failures"), 1);
  assert.match(env.cell(a, "Status Details"), /HTTP 500/);
  env.state.fetchCode = 204;                                 // GitHub recovers
  env.ctx.queueTick();
  assert.equal(env.status(a), "Running");
  assert.equal(env.cell(a, "Dispatch Failures"), 0);
});

test("a request GitHub keeps refusing is Failed after MAX_DISPATCH_FAILURES and does not block the next one", () => {
  const env = makeEnv({MAX_DISPATCH_FAILURES: "3"});
  env.state.fetchCode = 401;
  const [a, b] = [env.submit(), env.submit()];
  env.ctx.queueTick();
  env.ctx.queueTick();
  assert.equal(env.status(a), "Failed");
  assert.match(env.cell(a, "Status Details"), /after 3 tries/);
  assert.equal(env.status(b), "Queued");
  env.state.fetchCode = 204;
  env.ctx.queueTick();
  assert.equal(env.status(b), "Running");
});

test("rows from before the queue existed never block it or get started", () => {
  const env = makeEnv();
  const legacy = [["Running"], ["Queued"]];
  const statusCol = 9;
  legacy.forEach(([s], i) => {
    const row = i + 2;
    env.sheet.set(row, 1, new Date(env.state.now));
    env.sheet.set(row, statusCol, s);
  });
  env.ctx.ensureStatusColumns_(env.sheet);                   // adds the new columns
  const status = env.col("Request Status");
  env.sheet.set(2, status, "Running"); env.sheet.set(3, status, "Queued");
  const fresh = env.submit();
  assert.equal(env.status(fresh), "Running");
  assert.equal(env.state.fetchCalls.length, 1);
  assert.equal(env.status(2), "Running");                    // untouched
  assert.equal(env.status(3), "Queued");
});

test("a callback for an old (pre-queue) request is written straight through", () => {
  const env = makeEnv();
  env.ctx.ensureStatusColumns_(env.sheet);
  env.sheet.set(2, env.col("Request ID"), "REQ-20261120-007");
  env.sheet.set(2, env.col("Request Status"), "Queued");
  const reply = JSON.parse(env.ctx.doPost({postData: {contents: JSON.stringify(
    {secret: SECRET, request_id: "REQ-20261120-007", status: "Succeeded", details: "d", job_url: "u"})}}).text);
  assert.deepEqual(reply, {ok: true});
  assert.equal(env.status(2), "Succeeded");
});

test("the same form row submitted twice (duplicate trigger) creates one request and one dispatch", () => {
  const env = makeEnv();
  const row = env.submit();
  env.ctx.onFormSubmit({range: env.sheet.getRange(row, 1, 1, 5), namedValues: {
    "Flipkart Product URL": [GOOD_URL], "Duration": ["1"], "Duration Unit": ["weeks"], "Email Address": ["user@example.com"]}});
  assert.equal(env.state.fetchCalls.length, 1);
  assert.equal(env.cell(row, "Request ID"), "REQ-20261123-001");
  assert.equal(env.store.REQUEST_COUNTER_20261123, "1");
});

test("a duplicate callback after success changes nothing and does not restart anything", () => {
  const env = makeEnv();
  const [a, b] = [env.submit(), env.submit()];
  env.callback(a, "Succeeded");
  const before = env.state.fetchCalls.length;
  assert.deepEqual(env.callback(a, "Failed", "late duplicate"), {ok: true});
  assert.equal(env.status(a), "Succeeded");
  assert.equal(env.state.fetchCalls.length, before);
  assert.equal(env.status(b), "Running");
});

test("invalid submissions are marked Failed on their row and never enter the queue", () => {
  const env = makeEnv();
  const cases = [["https://example.com/p/1", "1", "weeks", "a@b.com", /valid HTTPS Flipkart/],
                 [GOOD_URL, "0", "weeks", "a@b.com", /integer from 1 to 120/],
                 [GOOD_URL, "2", "days", "a@b.com", /Weeks, Months, or Years/],
                 [GOOD_URL, "2", "weeks", "not-an-email", /valid Recipient Email/]];
  for (const [url, d, u, e, re] of cases) {
    assert.throws(() => env.submit(url, d, u, e), re);
  }
  assert.equal(env.state.fetchCalls.length, 0);
  for (let r = 2; r <= 5; r++) assert.equal(env.status(r), "Failed");
  const ok = env.submit();                                   // a valid one after the bad ones still runs
  assert.equal(env.status(ok), "Running");
});

test("the callback rejects a wrong secret, a bad id, an unknown id and a bad body", () => {
  const env = makeEnv();
  const a = env.submit();
  const post = (obj) => JSON.parse(env.ctx.doPost({postData: {contents: typeof obj === "string" ? obj : JSON.stringify(obj)}}).text);
  assert.equal(post({secret: "nope", request_id: "REQ-20261123-001", status: "Succeeded"}).error, "unauthorized");
  assert.equal(post({secret: SECRET, request_id: "bad", status: "Succeeded"}).error, "invalid request id");
  assert.equal(post({secret: SECRET, request_id: "REQ-20261123-099", status: "Succeeded"}).error, "request not found");
  assert.equal(post("{not json").error, "bad request");
  assert.equal(env.status(a), "Running");
});

test("a callback arriving while the script is busy gets 'busy' so the caller can retry", () => {
  const env = makeEnv();
  const a = env.submit();
  env.state.lockBusy = true;
  assert.equal(env.callback(a, "Succeeded").error, "busy");
  assert.equal(env.status(a), "Running");
  env.state.lockBusy = false;
  assert.equal(env.callback(a, "Succeeded").ok, true);
  assert.equal(env.status(a), "Succeeded");
});

test("the timer does nothing harmful when the script lock is busy or the queue is empty", () => {
  const env = makeEnv();
  env.ctx.queueTick();
  env.state.lockBusy = true;
  env.ctx.queueTick();
  assert.equal(env.state.fetchCalls.length, 0);
});

test("setupQueueTrigger creates a single 5-minute timer even if run twice", () => {
  const env = makeEnv();
  env.ctx.setupQueueTrigger();
  env.ctx.setupQueueTrigger();
  assert.equal(env.state.triggers.length, 1);
  assert.equal(env.state.triggers[0].getHandlerFunction(), "queueTick");
  assert.equal(env.state.triggers[0].minutes, 5);
});

test("a burst of ten requests is processed one by one to the end without any being lost", () => {
  const env = makeEnv();
  const rows = Array.from({length: 10}, () => env.submit());
  const order = [];
  for (let i = 0; i < 10; i++) {
    const running = rows.filter(r => env.status(r) === "Running");
    assert.equal(running.length, 1);                         // never two at the same time
    order.push(env.cell(running[0], "Request ID"));
    env.advance(28);
    env.callback(running[0], "Succeeded");
  }
  assert.deepEqual(rows.map(r => env.status(r)), Array(10).fill("Succeeded"));
  assert.deepEqual(order, rows.map(r => env.cell(r, "Request ID")));
  assert.equal(env.state.fetchCalls.length, 10);
});
