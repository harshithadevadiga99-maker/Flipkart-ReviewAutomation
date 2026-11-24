"""The Sheets callback retries transient problems (busy, network) but fails fast on real rejections."""
import io
import json
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import update_google_sheet as ugs                                                       # noqa: E402

RID = "REQ-20261123-001"


class FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def env(tmp_path, monkeypatch):
    report = tmp_path / "reports" / RID
    report.mkdir(parents=True)
    (report / "request_status.json").write_text(json.dumps({"status": "Email Sent", "result": {}}), encoding="utf-8")
    monkeypatch.setattr(ugs, "BASE_DIR", tmp_path)
    monkeypatch.setattr(ugs.time, "sleep", lambda s: None)
    monkeypatch.setenv("REQUEST_ID", RID)
    monkeypatch.setenv("SHEETS_CALLBACK_URL", "https://script.google.com/macros/s/x/exec")
    monkeypatch.setenv("SHEETS_CALLBACK_SECRET", "s")
    monkeypatch.setenv("BUILD_URL", "https://github.com/run/1")
    return monkeypatch


def script(monkeypatch, replies):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(json.loads(req.data.decode("utf-8")))
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(ugs, "urlopen", fake_urlopen)
    return calls


OK = FakeResponse('{"ok": true}')
BUSY = FakeResponse('{"ok": false, "error": "busy"}')


def test_success_sends_succeeded_status_once(env):
    calls = script(env, [OK])
    assert ugs.main() == 0
    assert len(calls) == 1 and calls[0]["status"] == "Succeeded" and calls[0]["request_id"] == RID


def test_busy_is_retried_until_it_works(env):
    calls = script(env, [BUSY, BUSY, OK])
    assert ugs.main() == 0
    assert len(calls) == 3


def test_network_errors_are_retried(env):
    calls = script(env, [URLError("down"), TimeoutError(), OK])
    assert ugs.main() == 0
    assert len(calls) == 3


def test_non_json_reply_is_retried(env):
    calls = script(env, [FakeResponse("<html>login</html>"), OK])
    assert ugs.main() == 0
    assert len(calls) == 2


def test_always_busy_gives_up_after_four_tries(env):
    calls = script(env, [BUSY])
    with pytest.raises(RuntimeError, match="busy"):
        ugs.main()
    assert len(calls) == 4


@pytest.mark.parametrize("reason", ["unauthorized", "request not found", "invalid request id"])
def test_real_rejections_fail_immediately(env, reason):
    calls = script(env, [FakeResponse(json.dumps({"ok": False, "error": reason}))])
    with pytest.raises(RuntimeError, match=reason):
        ugs.main()
    assert len(calls) == 1
