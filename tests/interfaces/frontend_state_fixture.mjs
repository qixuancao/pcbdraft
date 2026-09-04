// Offline behavior tests with fake API/DOM boundaries; no browser or network.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const app = fs.readFileSync(new URL("../../src/pcbdraft/web/app.js", import.meta.url), "utf8");
const chat = fs.readFileSync(new URL("../../src/pcbdraft/web/conversation.js", import.meta.url), "utf8");
function section(start, end) {
  const first = app.indexOf(start);
  const last = app.indexOf(end, first);
  assert.ok(first >= 0 && last > first);
  return app.slice(first, last);
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
const node = () => ({
  children: [], disabled: false, value: "", textContent: "", dataset: {}, handlers: {},
  append(...values) { this.children.push(...values); },
  replaceChildren(...values) { this.children = values; },
  addEventListener(name, callback) { this.handlers[name] = callback; },
  querySelectorAll() { return []; }, setAttribute() {},
});

let refreshes = 0;
let displayed = 0;
const events = {
  URL,
  state: { selectedProject: "board-a", projectEpoch: 1, eventCursor: 0, eventStreamId: "stream-1" },
  elements: { stream: {} },
  api: { url: () => new URL("http://localhost/events"), projectPath: () => "events" },
  EventSource: class {
    constructor() { this.handlers = {}; }
    addEventListener(name, callback) { this.handlers[name] = callback; }
  },
  stopEvents() {}, stopDisconnectedPolling() {}, updateIndicator() {},
  scheduleDisconnectedPolling() {}, recoverEventStream() {}, clean: (x) => x,
  DISCONNECTED_POLL_MIN_MS: 1000,
  conversation: { addEvent() { displayed++; } },
  refreshSnapshot(options) { assert.equal(options.invalidate, true); refreshes++; },
};
vm.createContext(events);
vm.runInContext(section("function startEvents()", "function stopDisconnectedPolling()"), events);
events.startEvents();
const oldSource = events.state.eventSource;
oldSource.handlers.open();
oldSource.handlers.update({ data: JSON.stringify({
  sequence: 1, stream_id: "stream-1", kind: "external_revision.detected",
}) });
assert.equal(refreshes, 1);
assert.equal(events.state.eventStreamHealthy, true);
events.startEvents();
oldSource.handlers.update({ data: JSON.stringify({ sequence: 2, kind: "job.complete" }) });
assert.equal(displayed, 1, "closed streams cannot alter the selected project");

const requested = [];
const completions = [];
const snapshots = {
  state: { selectedProject: "board-a", projectEpoch: 1, snapshotInFlight: null },
  api: {
    projectPath: (id, suffix) => `${id}/${suffix}`,
    get(path) { requested.push(path); const item = deferred(); completions.push(item); return item.promise; },
  },
  board: { getScene: () => null, setScene: () => false },
  elements: Object.fromEntries(["boardBusy", "busyMessage", "importExternal", "boardEmpty", "ipc"].map((key) => [key, node()])),
  conversation: { setSession() {} },
  i18n: { t: (key) => key }, clean: (x) => x, updateIndicator() {},
  showToast() { assert.fail("unexpected snapshot error"); },
};
vm.createContext(snapshots);
vm.runInContext(section("async function refreshSnapshot(", "async function importExternalChange()"), snapshots);
const first = snapshots.refreshSnapshot();
snapshots.state.selectedProject = "board-b";
snapshots.state.projectEpoch++;
const second = snapshots.refreshSnapshot();
assert.deepEqual(requested, ["board-a/snapshot", "board-b/snapshot"]);
completions[0].resolve({ external_change: { state: "stale-a" } });
await first;
assert.equal(snapshots.state.snapshotInFlight.projectId, "board-b");
completions[1].resolve({ external_change: { state: "current-b" } });
await second;
assert.equal(snapshots.state.externalChange.state, "current-b");
assert.equal(snapshots.state.snapshotInFlight, null);

// An event during an in-flight snapshot must cause another read. A concurrent
// stream recovery must also retain its cursor-reset request when coalesced.
const pending = snapshots.refreshSnapshot();
const coalesced = snapshots.refreshSnapshot({ resetStreamCursor: true, invalidate: true });
completions[2].resolve({ stream: { last_sequence: 4, stream_id: "b" } });
await new Promise((resolve) => setImmediate(resolve));
assert.equal(requested.length, 4);
completions[3].resolve({ stream: { last_sequence: 5, stream_id: "b" } });
await Promise.all([pending, coalesced]);
assert.equal(snapshots.state.eventCursor, 5);

const dom = { document: { createDocumentFragment: node, createElement: node, createTextNode: (textContent) => ({ textContent }) } };
vm.createContext(dom);
vm.runInContext(chat.replace("export function createConversation", "function createConversation"), dom);
const elements = Object.fromEntries([
  "tabs", "form", "input", "stop", "send", "activityType", "activityState",
  "chatLog", "chatEmpty", "turnState", "activityList", "activityEmpty", "conversationPane", "activityPane",
].map((name) => [name, node()]));
const sessionReads = [];
const conversation = dom.createConversation({ elements, t: (x) => x, onToast() { assert.fail("unexpected conversation error"); }, api: {
  projectPath: (id, suffix) => `${id}/${suffix}`,
  get() { const item = deferred(); sessionReads.push(item); return item.promise; },
  post() { assert.fail("an active turn cannot be submitted again by keyboard"); },
} });
const loadA = conversation.setProject("board-a");
const loadB = conversation.setProject("board-b");
sessionReads[1].resolve({ status: "queued", messages: [] });
await loadB;
sessionReads[0].resolve({ status: "idle", messages: [] });
await loadA;
assert.equal(elements.turnState.textContent, "queued");
for (const status of ["queued", "running", "cancel_requested"]) {
  conversation.setSession({ status, messages: [] });
  assert.equal(elements.send.disabled, true, status);
  assert.equal(elements.stop.disabled, false, status);
  elements.input.value = "message";
  await elements.form.handlers.submit({ preventDefault() {} });
}
console.log("FRONTEND_STATE_OK: SSE invalidation, project isolation, request coalescing and active jobs");
