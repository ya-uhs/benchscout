"""The agent proposes how to use a tool benchscout ships nothing for (a vendor's programmer); the human confirms it.

2026-11-02: "for an FPGA openFPGALoader cannot program, does benchscout look for the vendor's tool, or is FPGA =
openFPGALoader hard-wired?" It was: only openFPGALoader actions were shipped, and nothing let the agent bring the
vendor's tool through benchscout's gate. Now the agent finds the tool and proposes the action; the human sees the
exact command and its declared effect, and only their OK makes it an action, under the same policy as any other.
"""

import json
from pathlib import Path

import pytest

from benchscout import actions as A
from benchscout import knowledge as K
from benchscout.broker import Broker
from benchscout.core import agent_dispatch
from benchscout.io import FixtureIo
from benchscout.policy import Effect, classify

FIXTURE = Path(__file__).parent.parent / "fixtures" / "bench_unseen.json"
CABLE = {"name": "Platform Cable USB II", "vendor": "Xilinx", "vid": "0x03fd", "pid": "0x0013", "serial": "000013C1B4A101",
         "location": "0x01200000"}  # a programmer cable benchscout ships nothing about

PROGRAM = {"name": "vendortool.program", "argv": ["vendortool", "--cable", "{device.serial}", "--program", "{bitstream}"],
           "effect": "dangerous", "reason": "loads a bitstream into the FPGA's SRAM: everything in the fabric restarts",
           "doc": "program the FPGA's SRAM over JTAG with the vendor's tool",
           "args": {"bitstream": {"type": "file", "required": True, "ext": [".bit"]}}, "port_kinds": ["jtag"],
           "releases_device": True, "timeout": 300}
EVIDENCE = "vendor user guide, ch. 4: 'program loads the bitstream into the device over JTAG'"


@pytest.fixture
def broker(tmp_path):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["usb"].append(CABLE)
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    br = Broker(FixtureIo(f), tmp_path / "ws" / ".benchscout", approval_wait=0)
    br.scan()
    yield br
    br.end_session()
    K.load()


def test_a_proposed_tool_becomes_an_action_only_when_the_human_confirms(broker):
    events = []
    broker.on_event = events.append
    before = next(d for d in broker.bench.devices.values() if d.vid == 0x03FD)
    assert before.other and not broker.bench.ports_of(before.id), "the agent sees the cable, but it is not bench hardware yet"
    res = broker.define_tool(PROGRAM, EVIDENCE, [{"vid": "0x03fd", "pid": "0x0013", "probe": "jtag"}])
    assert res["status"] == "waiting"
    card = next(e["proposal"] for e in events if e["event"] == "tool_proposed")
    assert card["tool"]["argv"][0] == "vendortool" and card["evidence"] == EVIDENCE and card["probes"][0]["vid"] == 0x03FD
    assert "vendortool.program" not in A.all_actions(), "not before the human's OK"

    broker.decide_tool(res["proposal_id"], True)
    assert broker.define_tool(proposal_id=res["proposal_id"])["status"] == "defined"
    assert "vendortool.program" in A.all_actions()
    written = (broker.state / "knowledge" / "agent-tools.toml").read_text(encoding="utf-8")
    assert 'name = "vendortool.program"' in written and "confirmed by the human" in written and EVIDENCE in written
    cable = next(d for d in broker.bench.devices.values() if d.vid == 0x03FD)
    assert not cable.other
    port = next(p for p in broker.bench.ports_of(cable.id))
    assert port.kind == "jtag", "the cable it named is now a port the action can use"
    imp = classify(broker.bench, "vendortool.program", port.id, A.get("vendortool.program").normalize({"bitstream": "top.bit"}))
    assert imp.effect == Effect.DANGEROUS, "used under the same policy: the declared effect"


def test_rejected_or_wrong_proposals_change_nothing(broker):
    res = broker.define_tool(PROGRAM, EVIDENCE)
    broker.decide_tool(res["proposal_id"], False)
    assert broker.define_tool(proposal_id=res["proposal_id"])["status"] == "rejected"
    assert "vendortool.program" not in A.all_actions()
    assert not (broker.state / "knowledge" / "agent-tools.toml").exists()

    def bad(change, evidence=EVIDENCE):
        return broker.define_tool(dict(PROGRAM, **change), evidence)

    assert "evidence" in bad({}, evidence="")["message"]
    assert "effect" in bad({"effect": "harmless"})["message"]
    assert "reason" in bad({"reason": ""})["message"]
    assert "argv[0]" in bad({"argv": ["{port.path}"]})["message"]
    assert "only" in bad({"argv": ["vendortool", "{secret}"]})["message"]
    assert "already an action" in bad({"name": "fpga.program"})["message"], "a shipped action cannot be replaced"
    assert "already an action" in bad({"name": "serial.send"})["message"]
    assert "download" in broker.define_tool(dict(PROGRAM, get={"url": "x"}), EVIDENCE)["message"], "nothing to download"
    assert broker.proposals._items and all(p.status != "accepted" for p in broker.proposals._items.values())


def test_only_the_human_decides_and_an_untrusted_folder_takes_none(broker, tmp_path):
    res = agent_dispatch(broker, "define_tool", {"tool": PROGRAM, "evidence": EVIDENCE})
    assert res["status"] == "waiting"
    with pytest.raises(ValueError, match="no method"):
        agent_dispatch(broker, "tool_decide", {"id": res["proposal_id"], "accept": True})
    untrusted = Broker(FixtureIo(FIXTURE), tmp_path / "other" / ".benchscout", workspace_knowledge=False)
    assert untrusted.define_tool(PROGRAM, EVIDENCE)["status"] == "rejected"


def test_the_agent_hears_defined_only_once_it_can_use_the_action(broker):
    """Real bench, 2026-11-02 (Espressif's OpenOCD on the ESP32-S3): the agent's waiting call returned "defined" as
    soon as the human pressed the button, before the definition was written and loaded, and its next bench_do was
    told there was no such action."""
    import threading

    broker.approval_wait = 10
    out = {}
    def agent():
        out.update(broker.define_tool(PROGRAM, EVIDENCE))
        out["usable"] = "vendortool.program" in A.all_actions()  # what the agent's next bench_do would meet

    t = threading.Thread(target=agent)
    t.start()
    for _ in range(100):
        if broker.proposals._items:
            break
        __import__("time").sleep(0.02)
    (pid,) = broker.proposals._items
    broker.decide_tool(pid, True)
    t.join(10)
    assert out["status"] == "defined" and out["usable"]
