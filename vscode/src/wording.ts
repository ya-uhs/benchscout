// The panel's words. English only, as Claude Code and Copilot CLI keep theirs: what the agent says is in the person's
// language (benchscout.language; design.md section 2, 12 2026-11-10). Plain words first; the exact action and port follow.
// What the core says (an approval's reason, a memory entry it wrote) is shown as it says it.

export const S = {
  running: "Running. Agents in this folder (Copilot, Claude Code) reach the bench through benchscout.",
  stopped: "Not running.",
  start: "Start",
  rescan: "Rescan",
  stop: "Stop",
  programRunning: "Program running",
  programFinished: (status: string) => `Program ended: ${status}`,
  observation: (name: string) => `${name}: new observation`,
  alsoAffects: "Also affects",
  fetchFirst: "Downloads first",
  allowOnce: "Allow once",
  allowSession: "Allow while this session lasts",
  allowCommandSession: "Allow this command for the session",
  allowAllSession: "Allow everything for this session",
  allowAllOn: "Allowing everything: whatever only changes a device's state runs without asking; what cannot be undone still asks.",
  allowAllOff: "Turn off",
  autonomyAuto: "Autonomy: investigates on its own",
  autonomySuggest: "Autonomy: only suggests",
  autonomyHint: "Press to switch. On its own: when a chosen device reconnects or matches an observation rule, benchscout wakes the agent to look. Only suggests: it asks you first. Anything that changes a device still waits for your OK either way.",
  toolTitle: (name: string) => `The agent wants to add a tool: ${name}`,
  toolBody: (cmd: string, effect: string, reason: string, evidence: string, probes: string) =>
    `Runs: \`${cmd}\`\nWhat it does: ${reason} (${effect})\nThe agent's source: ${evidence}`
    + (probes ? `\nAlso treats this USB device as a programmer: ${probes}` : "")
    + "\nOnce added it is used like any other action: it asks you as its effect says. Nothing is downloaded; you install the tool.",
  toolAccept: "Add it",
  toolReject: "Do not add",
  downloading: (tool: string) => `Downloading ${tool}`,
  downloaded: (tool: string) => `Downloaded ${tool}`,
  answerFailed: "Could not apply your answer",
  downloadFailed: (tool: string) => `Could not download ${tool}`,
  downloadBody: (got: string, total: string | null) => (total ? `${got} of ${total} MB` : `${got} MB so far`) +
    " · from its official release, checked against its pinned SHA-256. The agent's request goes on when it is done.",
  statusAllowAll: "allowing all",
  allowAlwaysHere: "Always on this device",
  allowAlwaysModel: "Always on this model",
  modelWide: "Every unit of this model carries the same serial number, so this is kept for the model: it applies whenever one of them is plugged in alone (with two plugged in, it asks).",
  commandCovers: "“For the session” lets this same command run without asking until benchscout stops, with any values (only within the limits remembered for it, if there are any); “Always” remembers it for this device, limits included (forget it in the inbox any time). Other commands still ask.",
  commandCoversSession: "“For the session” lets this same command run without asking until benchscout stops, with any values (only within the limits remembered for it, if there are any). Other commands still ask.",
  sessionCovers: "“Allow while this session lasts” also lets other calls of this kind on this port run without asking, as long as they only change state; anything that cannot be undone still asks each time.",
  // what "for the session" covers on a card for something that may not be undone (core: Approval.again)
  again: {
    program: { label: "Allow this program for the session",
      covers: "“For the session” runs this same program again without asking until benchscout stops. If its files change, it asks again. “Allow all” never covers programs." },
    tool: { label: "Allow this on this device for the session",
      covers: "“For the session” lets this same tool do this again on this device without asking until benchscout stops, with any file (each build is a new image). A changed tool, or another device, asks again." },
    bounded: { label: "Allow up to these values for the session",
      covers: "“For the session” lets these commands run again without asking until benchscout stops, with values between 0 and the ones shown (the same words where a value is not a number). Anything more asks again." },
  } as Record<string, { label: string; covers: string }>,
  deny: "Deny",
  devicesTitle: "Devices on the bench",
  useDevice: "Use",
  releaseDevice: "Release",
  stateWatched: "watched",
  stateSelected: "in use by benchscout",
  stateFree: "not used",
  stateNoDriver: "Windows has no driver for it",
  memoryTitle: "Remembered",
  memoryOpenFile: "Open file",
  memoryPending: "waiting for you",
  memoryHere: "kept for this folder",
  memoryHint: "Written without asking; what loosens safety waits for you. Each device's memory is a plain TOML file: open it to fix anything.",
  memoryKinds: { identity: "name", observation: "seen", config: "setup", interpretation: "watch for", command: "command", safety: "safety" } as Record<string, string>,
  stateNeedsWinusb: "needs the WinUSB driver",
  stateNeedsUdev: "needs a udev rule",
  udevTitle: (name: string) => `${name}: Linux needs a udev rule`,
  udevBody: (node: string, tools: string) => `Your user cannot open ${node || "this USB device"}, which ${tools || "a tool"} opens through libusb. `
    + "Install a udev rule that gives access (the tool's package often brings one), then unplug and plug "
    + "the device in again, or run `sudo udevadm control --reload-rules && sudo udevadm trigger`. Then press Rescan.",
  otherDevices: "and {n} other USB devices benchscout has no data for (the agent sees them, e.g. a programmer cable)",
  driverTitle: (name: string) => `${name}: Windows needs a driver change`,
  driverBody: (none: string, winusb: string, tools: string) =>
    (none ? `Windows has no driver for its USB interface ${none}, so nothing can open it. For a serial port, install the maker's driver (Windows Update often does). ` : "")
    + (winusb ? `${tools || "A tool"} reaches interface ${winusb} through libusb, which on Windows needs the WinUSB driver there. ` : "")
    + "With Zadig (https://zadig.akeo.ie): Options › List All Devices, pick this device's interface, choose WinUSB, Replace Driver. "
    + "An interface switched to WinUSB no longer appears as a COM port. Then press Rescan. "
    + "Windows may refuse Zadig's driver (\"signer is not trusted\") on Arm PCs or where Code Integrity is enforced; "
    + "then use the vendor's own programming tool instead: ask the agent to add it.",
  releaseHint: "Release a device to use it with your own tools (flashing, a terminal); Use takes it back.",
  inUse: "In use in another window",
  notifyShow: "Show",
  statusWaiting: (n: number) => `benchscout: ${n} waiting for you`,
  statusIdle: "benchscout",
  coreMissing: "benchscout could not start. Install it (uv tool install ...) or set benchscout.corePath.",
  noFolder: "Open a folder first: benchscout keeps its logs and captures in the folder you work in.",
  firstQuestion: "Found on the bench:",
  learned: (name: string) => `Remembered about ${name}`,
  commandAs: (cmd: string, effect: string) =>
    `\`${cmd}\`: ${({ safe: "runs without asking", disruptive: "asks before it runs (can be allowed for the session)", dangerous: "asks every time" } as Record<string, string>)[effect] ?? effect}`,
  limitsAs: (t: string) => `Values allowed: ${t}. Outside them, it asks every time.`,
  learnedSafety: (name: string) => `Remember about ${name}? This loosens safety`,
  memorySources: { agent: "agent", benchscout: "benchscout", human: "you", user: "you" } as Record<string, string>,
  accept: "Accept",
  reject: "Reject",
  forget: "Forget",
  deviceAlert: (name: string) => `${name}: something to look at`,
  deviceVanished: (name: string) => `${name}: the port vanished (reset or unplugged?)`,
  deviceReset: (name: string) => `${name}: the port vanished and came back (a reset or a USB dropout?)`,
  hostSlept: (name: string, n: number) => `${name}: reopened after the computer slept${n > 1 ? ` (${n} times)` : ""}`,
  deviceGone: (name: string) => `${name}: the port did not come back`,
  pluggedIn: (name: string) => `Plugged in: ${name}`,
  throughBenchscout: "Just before, through benchscout:",
  knowledgeBroken: (file: string) => `Could not read ${file}`,
  knowledgeBrokenBody: "Nothing in it is in effect until it is fixed. benchscout reads the other files and leaves this one as it is. Fix it, then press Rescan.",
  knowledgeFixed: "read now",
  pluggedInBody: "A new device. Look at it?",
  unplugged: (name: string) => `Unplugged: ${name}`,
  unpluggedBody: "It stays chosen: benchscout watches it again when it is back. What was allowed for it was withdrawn (another board could come back in its place).",
  pluggedBack: "back: watched again",
  investigate: "Investigate",
  ignore: "Ignore",
  logAt: "Log",
  runningWith: "Running",
  allowAlways: "Always allow",
  firstAsk: "What should we look at?",
  lookAt: (name: string) => `Look at ${name}`,
  lookAll: "Check all of them",
  lookOthers: (n: number) => `Look at the other USB devices (${n})`,
  otherDevicesFound: (names: string) => `USB devices benchscout has no data for: ${names}. Your agent can find out what they are.`,
  typeHere: "Write what you want instead…",
  send: "Send",
  wakeFailed: (agent: string) => `Could not start ${agent}`,
  wakeFix: "How to fix",
  budget: (n: number) => `Paused: ${n} agent turns in the last hour (the limit). Continue when you are ready.`,
  investigating: "Investigating",
  report: "Report",
  working: "Working…",
  showProgress: "Show progress",
  continueIt: "Keep going",
  doneIt: "Done",
  agentAsks: "The agent asks",
  agentWantsDevices: (names: string) => `The agent asks to use: ${names}`,
  allowDevices: "Allow",
  denyDevices: "Don't",
  agentWantsTool: "The agent wants to run",
  denyInstead: "Or deny it and tell the agent what to do instead…",
  deniedGoesOn: (tool: string) => `It stopped because you denied ${tool}; benchscout asked it to go on without it.`,
  chooseAgent: "Which agent should benchscout wake?",
  chooseAgentFirst: "Choose the agent first; benchscout wakes it with this request then.",
  requestWaits: "Your request is waiting for this choice: benchscout wakes the agent with it.",
  noAgentFound: "No agent to wake was found",
  noAgentFix: "Install GitHub Copilot CLI (npm install -g @github/copilot) or Claude Code, sign in to it, then press Rescan.",
  noAgentYet: "no agent chosen yet",
  offerWatch: (name: string) => `Watch ${name}?`,
  offerWatchWhy: "Keeping its serial port open lets benchscout see everything it prints and notice reconnections and matching log lines.",
  watchIt: "Watch it",
  notNow: "Not now",
  watching: (name: string) => `Watching ${name}`,
  newInput: "Tell benchscout what you want…",
  effect: { safe: "Safe", disruptive: "Changes device state", dangerous: "May not be undoable" } as Record<string, string>,
};

export type Wording = typeof S;

/** The language the agent is told to reply in, by its English name ("ja-JP" -> "Japanese"), from the standard
 *  Intl.DisplayNames (no table of languages here); English if the tag names none. */
export function languageName(language: string): string {
  try {
    const base = language.replace("_", "-").split("-")[0].toLowerCase();
    return new Intl.DisplayNames(["en"], { type: "language", fallback: "none" }).of(base) ?? "English";
  } catch {
    return "English";
  }
}

/**
 * The language the agent replies in: the setting if set; else VS Code's display language when it is not English
 * (someone installed a language pack for it); else the operating system's UI language, since many people keep VS Code
 * in English (no language pack) but read their own language more easily.
 */
export function pickLanguage(setting: string | undefined, vscodeLanguage: string, osLanguage: string): string {
  if (setting && setting !== "auto") return setting;
  if (!vscodeLanguage.startsWith("en")) return vscodeLanguage;
  return osLanguage || "en";
}
