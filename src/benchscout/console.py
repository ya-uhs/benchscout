"""Human-readable view of the control channel, for a developer running `benchscout core` in a
terminal. The VS Code extension gets the same messages as JSON lines instead."""

from __future__ import annotations

HELP = ("Commands: select <number|port>...   approve <id> [once|session|<N>m]   deny <id>   "
        "pending   scan   quit")


def _devices(bench: dict) -> list[str]:
    ports: dict[str, list[str]] = {}
    for p in bench.get("ports", []):
        ports.setdefault(p["device_id"], []).append((p.get("path") or p["id"]).split("/")[-1].removeprefix("cu."))
    return [f"  {i}. {d['name']}  ({', '.join(ports.get(d['id'], [])) or d['id']})"
            for i, d in enumerate(bench.get("devices", []), 1)]


def _names(broker, device_ids) -> str:
    devs = broker.last.bench.devices if broker.last else {}
    return ", ".join(devs[d].name if d in devs else d for d in device_ids) or "nothing"


def format_human(msg: dict, broker) -> str | None:
    ev = msg.get("event")
    if ev == "ready":
        return "benchscout core is running. Agents opened in this folder connect to it.\n" + HELP
    if ev == "scanned":
        return None  # the scan result below says it all
    if ev == "approval_requested":
        a = msg["approval"]
        imp = a["impact"]
        also = f"\n   also disturbs: {', '.join(imp['affected_ports'])}" if imp.get("affected_ports") else ""
        scope = f"   approve {a['id']} session | approve {a['id']} 10m | " if a["effect"] == "disruptive" else "   "
        return (f"\n>>> APPROVAL NEEDED  [{a['effect'].upper()}]  {a['action']} on {a['port']}  {a['args']}\n"
                f"   why: {imp['reason']}{also}\n"
                f"   approve {a['id']}  (once) |{scope.removeprefix('  ')}deny {a['id']}")
    if ev == "approval_decided":
        a = msg["approval"]
        return f"<<< {a['id']}: {a['status']}" + (f" ({a['scope']})" if a["status"] == "granted" else "")
    if ev == "selection_changed":
        busy = msg.get("busy") or {}
        held = "".join(f"\n   {_names(broker, [d])} is in use by another window ({h.get('workspace') or h.get('pid')})"
                       for d, h in busy.items())
        return f"Selected: {_names(broker, msg['selected'])}{held}"
    if ev == "stopped":
        return "benchscout core stopped; devices released."
    if "error" in msg:
        return f"error: {msg['error']}"
    result = msg.get("result")
    if isinstance(result, dict) and "text" in result:  # a scan
        return result["text"] + "\n\nDevices (select by number or port name):\n" + "\n".join(_devices(result.get("bench", {})))
    if isinstance(result, list):  # pending approvals
        if not result:
            return "No approvals pending."
        return "\n".join(f"  {a['id']}  [{a['effect'].upper()}] {a['action']} on {a['port']} {a['args']}" for a in result)
    return None  # select/decide/bye: the event lines above already said it
