"""The safety gate under random sequences of calls (hypothesis): whatever the agent calls, in any order, with any
approval id it got hold of, nothing that is not SAFE runs unless the human allowed exactly that.

  - a DANGEROUS call runs only with a granted approval for the same action, port and arguments, once
  - a DISRUPTIVE call runs with such an approval, or a session grant the human gave for that action on that port
    (for SCPI: for a line whose commands include every command of this call), or what the human allowed always
  - an approval is used at most once, and never for a different call
  - the agent only calls `request`; the human decides through `decide` (that the agent's channel has no way to decide
    is test_core's `test_agent_channel_has_no_way_to_approve_or_select`)

And, over every action and every port of two benches, a call's effect is never below its action's own effect
except where a rule says why (the command line for SCPI, memory the human confirmed for firmware commands).
"""

import json
import os
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule

from benchscout import actions as A
from benchscout import recon
from benchscout.broker import Broker
from benchscout.io import FixtureIo
from benchscout.policy import Effect, classify

ROOT = Path(__file__).parent.parent
FIXTURE = ROOT / "fixtures" / "bench_example.json"
CH340 = "serial:usbserial-10"  # opening may reset it: listening is DISRUPTIVE
PSU = "visa:TCPIP0::192.168.1.50::INSTR"

CALLS = [
    ("serial.listen", CH340, {"seconds": 0.05}),
    ("serial.send", CH340, {"data": "status", "seconds": 0.05}),
    ("serial.send", CH340, {"data": "reboot", "seconds": 0.05}),
    ("scpi.query", PSU, {"command": "*IDN?"}),
    ("scpi.query", PSU, {"command": "MEAS:VOLT?"}),
    ("scpi.query", PSU, {"command": "READ?"}),
    ("scpi.write", PSU, {"command": "OUTP CH1,ON"}),
    ("scpi.write", PSU, {"command": "VOLT 5"}),
    ("scpi.write", PSU, {"command": "*CLS"}),
    ("scpi.write", PSU, {"command": "DISP:BRIG 5"}),
    ("scpi.write", PSU, {"command": "DISP:BRIG 7"}),
    ("scpi.write", PSU, {"command": "SYST:BEEP"}),
    ("scpi.write", PSU, {"command": "DISP:BRIG 5;SYST:BEEP"}),
]


class Gate(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.tmp = tempfile.TemporaryDirectory()
        # each example its own memory: "always" writes there, and must not leak into the next example
        self.memory_env = os.environ.get("BENCHSCOUT_MEMORY")
        os.environ["BENCHSCOUT_MEMORY"] = str(Path(self.tmp.name) / "memory")
        f = Path(self.tmp.name) / "bench.json"
        f.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        self.br = Broker(FixtureIo(f), Path(self.tmp.name) / "ws" / ".benchscout", approval_wait=0)
        self.br.scan()
        self.br.select(sorted({self.br.bench.ports[p].device_id for p in (CH340, PSU)}))
        # approval id -> what it was asked for. Ids are random: draw from them in the order they came, never sorted,
        # or a replayed example draws a different one (hypothesis: FlakyStrategyDefinition)
        self.issued: dict[str, tuple[str, str, str, Effect]] = {}
        self.granted: set[str] = set()
        self.used: set[str] = set()
        # (action, port, SCPI headers or None) the human allowed for the session
        self.session: set[tuple[str, str, frozenset | None]] = set()
        self.allow_all = False  # the human allowed every DISRUPTIVE call

    def teardown(self):
        self.br.end_session()
        if self.memory_env is None:
            os.environ.pop("BENCHSCOUT_MEMORY", None)
        else:
            os.environ["BENCHSCOUT_MEMORY"] = self.memory_env
        self.tmp.cleanup()

    def _impact(self, action, port, args):
        return classify(self.br.bench, action, port, A.get(action).normalize(args))

    def _effect(self, action, port, args) -> Effect:
        return self._impact(action, port, args).effect

    def _check(self, action, port, args, res, approval_id=None):
        key = A.canonical(A.get(action).normalize(args))
        effect = self._effect(action, port, args)
        if res.get("status") == "approval_required":
            if approval_id is not None:  # waiting on an id it already had: it must be that call's own
                assert self.issued[approval_id][:3] == (action, port, key), f"{action} waits on another call's approval"
            self.issued.setdefault(res["approval_id"], (action, port, key, effect))
            return
        ran = res.get("status") == "done"
        if not ran or effect == Effect.SAFE:
            return
        if approval_id is not None:
            assert approval_id in self.granted, f"{action} ran on an approval the human did not grant"
            assert approval_id not in self.used, f"{action} ran twice on one approval"
            assert self.issued[approval_id][:3] == (action, port, key), f"{action} {args} ran on another call's approval"
            self.used.add(approval_id)
        else:
            subjects = self._impact(action, port, args).subjects
            assert effect == Effect.DISRUPTIVE and self.allow_all or effect == Effect.DISRUPTIVE and any(
                a == action and p == port and (s is None or subjects is not None and set(subjects) <= s)
                for a, p, s in self.session), f"{action} {args} ({effect.name}) ran without the human's OK for it"

    @rule(call=st.sampled_from(CALLS))
    def agent_calls(self, call):
        action, port, args = call
        self._check(action, port, args, self.br.request(action, port, args, wait=0))

    @rule(call=st.sampled_from(CALLS), data=st.data())
    def agent_calls_with_an_approval_id(self, call, data):
        """Any id the agent has seen, for this call or another; or a made-up one."""
        action, port, args = call
        aid = data.draw(st.sampled_from(list(self.issued) + ["000000"]))
        self._check(action, port, args, self.br.request(action, port, args, approval_id=aid, wait=0), approval_id=aid)

    @rule(data=st.data())
    def agent_calls_again_with_its_approval_id(self, data):
        """The usual path: the same call again, with the id it was given (once more after it was used, too)."""
        if not self.issued:
            return
        aid = data.draw(st.sampled_from(list(self.issued)))
        action, port, key, _ = self.issued[aid]
        args = json.loads(key)
        self._check(action, port, args, self.br.request(action, port, args, approval_id=aid, wait=0), approval_id=aid)

    def _allow_all_on(self):
        """Turning it on lets the agent's DISRUPTIVE calls already waiting go ahead."""
        self.allow_all = True
        for aid, (_, _, _, effect) in self.issued.items():
            a = self.br.approvals.get(aid)
            if effect == Effect.DISRUPTIVE and a is not None and a.status == "granted":
                self.granted.add(aid)

    @rule(on=st.booleans())
    def human_switches_allow_all(self, on):
        self.br.set_allow_all(on)
        if on:
            self._allow_all_on()
        else:
            self.allow_all = False

    @rule(data=st.data(), grant=st.booleans(), scope=st.sampled_from(["once", "session", "always", "all"]))
    def human_decides(self, data, grant, scope):
        waiting = [a.id for a in self.br.approvals.pending() if a.by == "agent"]
        if not waiting:
            return
        aid = data.draw(st.sampled_from(waiting))
        action, port, _, effect = self.issued[aid]
        try:
            self.br.decide(aid, grant, scope)
        except ValueError:
            assert grant and scope != "once", "allowing once is always possible"
            assert effect == Effect.DANGEROUS or scope == "always" and not action.startswith("scpi."), \
                f"{action} ({effect.name}) could not be allowed {scope}"
            return
        if grant:
            self.granted.add(aid)
            if scope == "all":
                self._allow_all_on()
            if scope == "session":
                subjects = self.br.approvals.get(aid).impact.get("subjects")
                self.session.add((action, port, frozenset(subjects) if subjects is not None else None))


Gate.TestCase.settings = settings(max_examples=150, stateful_step_count=30, deadline=None)
TestGate = Gate.TestCase


# ---- every action on every port: never weaker than the action itself, unless a rule says why ----------

BENCHES = [recon.run(FixtureIo(FIXTURE)).bench, recon.run(FixtureIo(ROOT / "fixtures" / "bench_unseen.json")).bench]
COMMAND_DECIDES = {"scpi.query", "scpi.write", "serial.send"}  # the command line or confirmed memory decides


def _value(p: A.Param):
    if p.type is bool:
        return st.booleans()
    if p.type is int:
        return st.integers(int(p.min if p.min is not None else 0), int(p.max if p.max is not None else 10_000))
    if p.type is float:
        return st.floats(p.min if p.min is not None else 0.0, p.max if p.max is not None else 100.0, allow_nan=False)
    if p.type is A.FlashMap:
        return st.just("0x0:a.bin")
    if p.type is A.FilePath:
        return st.just("image" + (p.ext[0] if p.ext else ".bin"))
    return st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=12)


@st.composite
def any_call(draw):
    bench = draw(st.sampled_from(BENCHES))
    name = draw(st.sampled_from(sorted(A.all_actions())))
    spec = A.get(name)
    port = draw(st.sampled_from(sorted(bench.ports)))
    args = {p.name: draw(_value(p)) for p in spec.params if p.required or draw(st.booleans())}
    return bench, name, spec, port, args


@settings(max_examples=500, deadline=None)
@given(any_call())
def test_no_call_is_weaker_than_its_action_unless_a_rule_says_why(call):
    bench, name, spec, port, args = call
    try:
        imp = classify(bench, name, port, spec.normalize(args))
    except A.ArgError:
        return  # refused, never run
    if name not in COMMAND_DECIDES:
        assert imp.effect >= spec.effect, f"{name} on {port} with {args}: {imp.effect.name} below {spec.effect.name}"
    assert imp.reason, "every effect says why"


def test_an_action_nobody_declared_is_dangerous_everywhere():
    for bench in BENCHES:
        for port in bench.ports:
            assert classify(bench, "no.such.action", port).effect == Effect.DANGEROUS


def test_the_example_calls_cover_every_effect():
    """So the state machine above meets SAFE, DISRUPTIVE and DANGEROUS calls."""
    b = BENCHES[0]
    seen = {classify(b, a, p, A.get(a).normalize(args)).effect for a, p, args in CALLS}
    assert seen == {Effect.SAFE, Effect.DISRUPTIVE, Effect.DANGEROUS}, json.dumps([e.name for e in seen])


def test_an_approval_id_for_another_call_is_refused_at_once(tmp_path):
    """Found by the state machine: the call waited on a card showing something else, and the agent was told to
    wait for the human with this call's impact. It never ran (the final check refused it), but it misled."""
    f = tmp_path / "bench.json"
    f.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    br = Broker(FixtureIo(f), tmp_path / "ws" / ".benchscout", approval_wait=0)
    br.scan()
    br.select(sorted({br.bench.ports[p].device_id for p in (CH340, PSU)}))
    aid = br.request("scpi.write", PSU, {"command": "OUTP CH1,ON"})["approval_id"]
    res = br.request("serial.listen", CH340, {"seconds": 0.05}, approval_id=aid)
    assert res["status"] == "approval_mismatch" and "scpi.write" in res["message"]
    assert br.request("scpi.write", PSU, {"command": "OUTP CH1,ON", }, approval_id=aid)["status"] == "approval_required"
    br.end_session()
