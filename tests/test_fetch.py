"""Tools are fetched on first use, from a pinned release, only after the human's OK (fetch.py)."""

import hashlib
import io as _io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from benchscout import fetch
from benchscout import knowledge as K
from benchscout.broker import Broker
from benchscout.io import FixtureIo

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_example.json"
VCP = "/dev/cu.usbmodem21403"


def _archive(tmp_path: Path, members: dict[str, str]) -> tuple[Path, str]:
    a = tmp_path / "hello-1.0.tar.gz"
    with tarfile.open(a, "w:gz") as t:
        for name, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            t.addfile(info, _io.BytesIO(data))
    return a, hashlib.sha256(a.read_bytes()).hexdigest()


def _knowledge(ws: Path, archive: Path, sha: str, effect: str = "safe") -> None:
    k = ws / ".benchscout" / "knowledge"
    k.mkdir(parents=True, exist_ok=True)
    (k / "lab.toml").write_text(f'''
[[tool]]
name = "lab.hello"
effect = "{effect}"
reason = "prints its arguments"
port_kinds = ["serial"]
argv = ["benchscout-test-hello", "{{port.path}}"]

[get.benchscout-test-hello]
version = "1.0"
{fetch.platform_key()} = {{ url = "{archive.as_uri()}", sha256 = "{sha}", exe = "hello/benchscout-test-hello" }}
''', encoding="utf-8")


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCHSCOUT_TOOLS", str(tmp_path / "tools"))
    yield tmp_path / "tools"
    K.load()


def test_a_pinned_tool_is_fetched_checked_and_kept(tmp_path, tools):
    archive, sha = _archive(tmp_path, {"hello/benchscout-test-hello": "#!/bin/sh\necho hi\n"})
    _knowledge(tmp_path / "ws", archive, sha)
    K.load(tmp_path / "ws")
    assert fetch.cached("benchscout-test-hello") is None
    path = fetch.get("benchscout-test-hello")
    assert Path(path).is_file() and Path(path).is_relative_to(tools)
    archive.unlink()  # the second time nothing is downloaded
    assert fetch.get("benchscout-test-hello") == path == fetch.cached("benchscout-test-hello")


def test_a_wrong_hash_or_an_escaping_path_is_refused(tmp_path, tools):
    archive, sha = _archive(tmp_path, {"hello/benchscout-test-hello": "#!/bin/sh\n"})
    _knowledge(tmp_path / "ws", archive, "0" * 64)
    K.load(tmp_path / "ws")
    with pytest.raises(fetch.FetchError, match="SHA-256"):
        fetch.get("benchscout-test-hello")
    assert not (tools / "benchscout-test-hello" / "1.0").exists()

    bad, bad_sha = _archive(tmp_path, {"../escaped": "x", "hello/benchscout-test-hello": "#!/bin/sh\n"})
    _knowledge(tmp_path / "ws", bad, bad_sha)
    K.load(tmp_path / "ws")
    with pytest.raises(fetch.FetchError, match="outside"):
        fetch.get("benchscout-test-hello")
    assert not (tmp_path / "escaped").exists()


def test_nothing_unpinned_is_ever_fetched(tmp_path, tools):
    archive, _ = _archive(tmp_path, {"hello/benchscout-test-hello": "x"})
    _knowledge(tmp_path / "ws", archive, "")
    K.load(tmp_path / "ws")
    assert fetch.plan("benchscout-test-hello") is None


@pytest.mark.skipif(sys.platform == "win32", reason="the test tool is a shell script")
def test_a_missing_tool_waits_for_the_human_then_is_fetched_and_runs(tmp_path, tools):
    archive, sha = _archive(tmp_path, {"hello/benchscout-test-hello": '#!/bin/sh\necho "hello $1"\n'})
    ws = tmp_path / "ws"
    _knowledge(ws, archive, sha)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    br = Broker(FixtureIo(f), ws / ".benchscout")
    events = []
    br.on_event = events.append
    br.scan()
    br.select([br.bench.ports[br.resolve_port(VCP)].device_id])
    first = br.request("lab.hello", VCP)
    assert first["status"] == "approval_required", "even a SAFE action asks before anything is downloaded"
    card = next(e["approval"] for e in events if e["event"] == "approval_requested" and e["approval"]["action"] == "lab.hello")
    assert "benchscout-test-hello 1.0" in card["fetch"] and "SHA-256 checked" in card["fetch"]
    assert fetch.cached("benchscout-test-hello") is None, "nothing fetched before the OK"
    br.decide(first["approval_id"], True)
    done = br.request("lab.hello", VCP, approval_id=first["approval_id"])
    assert done["status"] == "done" and f"hello {VCP}" in done["result"]["preview"]
    again = br.request("lab.hello", VCP)
    assert again["status"] == "done", "fetched once; a SAFE action now runs without asking"
    br.end_session()


def test_a_folder_not_trusted_has_its_own_knowledge_ignored(tmp_path, tools):
    """VS Code's Workspace Trust: a project from elsewhere could declare its actions safe."""
    from benchscout import actions as A

    archive, sha = _archive(tmp_path, {"hello/benchscout-test-hello": "#!/bin/sh\n"})
    ws = tmp_path / "ws"
    _knowledge(ws, archive, sha)
    Broker(FixtureIo(FIXTURE), ws / ".benchscout")
    assert "lab.hello" in A.all_actions()
    Broker(FixtureIo(FIXTURE), ws / ".benchscout", workspace_knowledge=False)
    assert "lab.hello" not in A.all_actions() and fetch.plan("benchscout-test-hello") is None


def test_the_persons_own_tool_comes_first_then_the_shipped_one(tmp_path, monkeypatch):
    name = "benchscout-test-own" + (".exe" if sys.platform == "win32" else "")
    shipped = tmp_path / "bundled" / "benchscout-test-own"
    shipped.mkdir(parents=True)
    (shipped / name).write_text("", encoding="utf-8")
    monkeypatch.setenv("BENCHSCOUT_BUNDLED_TOOLS", str(tmp_path / "bundled"))
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    assert Path(fetch.find("benchscout-test-own")) == shipped / name, "shipped with benchscout"
    own = tmp_path / "own"
    own.mkdir()
    (own / name).write_text("#!/bin/sh\n", encoding="utf-8")
    (own / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(own))
    assert Path(fetch.find("benchscout-test-own")) == own / name, "the person's own install wins"  # (case-blind on Windows)


@pytest.mark.skipif(sys.platform == "win32", reason="the test tool is a shell script")
def test_a_long_download_runs_in_the_background_and_the_call_says_so(tmp_path, tools, monkeypatch):
    """2026-11-02 (Windows VM): a first-use download (esptool, 65 MB) can outlast one agent call. After the human's
    OK it runs in the background, the inbox hears its progress, and the call says "downloading" instead of timing
    out; the same approval_id continues. A failed download can be tried again with that approval."""
    import threading

    archive, sha = _archive(tmp_path, {"hello/benchscout-test-hello": '#!/bin/sh\necho "hello $1"\n'})
    ws = tmp_path / "ws"
    _knowledge(ws, archive, sha)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    br = Broker(FixtureIo(f), ws / ".benchscout", download_wait=0.2)
    events = []
    br.on_event = events.append
    br.scan()
    br.select([br.bench.ports[br.resolve_port(VCP)].device_id])

    real_get, release, attempts = fetch.get, threading.Event(), []

    def slow_get(exe, progress=None):
        attempts.append(exe)
        if len(attempts) == 1:
            raise fetch.FetchError("could not download: network unreachable")
        progress(3_000_000, 10_000_000)
        release.wait(10)
        return real_get(exe, progress)

    monkeypatch.setattr(fetch, "get", slow_get)
    aid = br.request("lab.hello", VCP)["approval_id"]
    br.decide(aid, True)
    failed = br.request("lab.hello", VCP, approval_id=aid)
    assert failed["status"] == "download_failed" and "network unreachable" in failed["message"]
    waiting = br.request("lab.hello", VCP, approval_id=aid)  # tries again with the same approval
    assert waiting["status"] == "downloading" and "3 of 10 MB" in waiting["download"] and waiting["approval_id"] == aid
    assert any(e["event"] == "download" and e["state"] == "running" and e["total"] == 10_000_000 for e in events)
    release.set()
    done = br.request("lab.hello", VCP, approval_id=aid, wait=5)
    assert done["status"] == "done" and f"hello {VCP}" in done["result"]["preview"]
    assert [e["state"] for e in events if e["event"] == "download"][-1] == "done"
    assert len(attempts) == 2, "one download running at a time, the failed one retried once"
    br.end_session()
