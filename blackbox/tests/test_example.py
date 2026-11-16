"""The form of a black-box test: drive the program as its users do (the human's control channel, the agent's MCP
tools, a fake bench), and check only what they, and the devices, would see."""

PORT = "/dev/cu.usbmodem21403"  # a serial port of the example bench (bench_example.json)


def test_the_first_read_sends_nothing_to_any_device(world):
    world.start()
    scan = world.core.call("scan")
    assert scan["bench"]["devices"], "the example bench has devices"
    assert world.bench.sent() == [] and world.bench.seen("visa_write") == []


def test_a_send_waits_for_the_human_and_one_ok_runs_it_once(world):
    world.start()
    port = next(p for p in world.core.call("scan")["bench"]["ports"] if p["path"] == PORT)
    world.core.call("select", devices=[port["device_id"]])
    asked = world.agent.call_json("bench_do", action="serial.send", port=PORT, args={"data": "status"})
    assert asked["status"] == "approval_required"
    assert world.bench.sent(PORT) == [], "nothing reached the device before the human said yes"
    world.core.call("decide", approval_id=asked["approval_id"], granted=True, scope="once")
    done = world.agent.call_json("bench_do", action="serial.send", port=PORT, args={"data": "status"},
                                 approval_id=asked["approval_id"])
    assert done["status"] == "done"
    again = world.agent.call_json("bench_do", action="serial.send", port=PORT, args={"data": "status"},
                                  approval_id=asked["approval_id"])
    assert again["status"] != "done", "an OK given once is used once"
    assert world.bench.sent(PORT) == ["status\n"]
