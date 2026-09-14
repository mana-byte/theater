/**
 * Executable isolated conformance coverage for Theater's rendered OpenCode
 * TUI plugin.  It imports the exact rendered plugin file (passed as argv[2]
 * because the renderer is Python-owned), drives it against a loopback-only
 * NDJSON host plus a fake public TUI api with the exact upstream hey-api
 * promptAsync result shapes, and asserts the request/response contract:
 * once-only mutations, cached duplicate receipts, session-epoch scoping, and
 * unknown (never retried) delivery on any ambiguity.  It does not start
 * OpenCode, tmux, or Theater's daemon; the opt-in stock gate covers those.
 */

import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { createServer, type Server, type Socket } from "node:net";
import { join } from "node:path";
import { tmpdir } from "node:os";

const pluginUrl = process.argv[2];
assert.ok(pluginUrl, "usage: node opencode_frontend_control_conformance.mts <plugin file url>");
const { default: plugin } = await import(pluginUrl);

type JsonRecord = Record<string, unknown>;
type Deferred = {
	resolve: (value: unknown) => void;
	promise: Promise<unknown>;
};

const PROTOCOL = "theater-frontend-v1";
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
const waitFor = async (predicate: () => boolean, label: string) => {
	for (let i = 0; i < 500; i += 1) {
		if (predicate()) return;
		await sleep(10);
	}
	throw new Error(`timed out waiting for ${label}`);
};

function deferred(): Deferred {
	let resolve!: (value: unknown) => void;
	const promise = new Promise((res) => {
		resolve = res;
	});
	return { resolve, promise };
}

class Host {
	readonly frames: JsonRecord[] = [];
	private readonly sockets = new Set<Socket>();
	private server: Server | null = null;
	private buffer = "";

	async listen(path: string): Promise<void> {
		this.server = createServer((connection) => {
			this.sockets.add(connection);
			connection.setEncoding("utf8");
			connection.on("data", (chunk: string) => {
				this.buffer += chunk;
				for (;;) {
					const newline = this.buffer.indexOf("\n");
					if (newline < 0) return;
					this.frames.push(JSON.parse(this.buffer.slice(0, newline)));
					this.buffer = this.buffer.slice(newline + 1);
				}
			});
			connection.on("close", () => this.sockets.delete(connection));
		});
		await new Promise<void>((resolve, reject) => {
			this.server!.once("error", reject);
			this.server!.listen(path, resolve);
		});
	}

	write(line: string): void {
		for (const socket of this.sockets) socket.write(line);
	}

	get destroyed(): boolean {
		return [...this.sockets].every((socket) => socket.destroyed);
	}

	async close(): Promise<void> {
		for (const socket of this.sockets) socket.destroy();
		await new Promise<void>((resolve) => this.server!.close(() => resolve()));
	}
}

const request = (host: Host, id: string, method: string, params: JsonRecord) => {
	host.write(`${JSON.stringify({ type: "request", id, method, params })}\n`);
};

const responseFor = (frames: JsonRecord[], id: string): JsonRecord | undefined =>
	frames.find((frame) => frame.type === "response" && frame.id === id);

const sendParams = (operationId: string, session: string, prompt: string): JsonRecord => ({
	operation_id: operationId,
	native_session_id: session,
	prompt,
});

// argv[3] is the shared temp root whose socket and token paths were rendered
// into the plugin by the Python test; an isolated fallback keeps the fixture
// runnable on its own with a freshly rendered plugin.
const root = process.argv[3] ?? (await mkdtemp(join(tmpdir(), "oc-control-")));
const socketPath = join(root, "bridge.sock");
const tokenPath = join(root, "token");
await writeFile(tokenPath, "conformance-token\n");

const host = new Host();
await host.listen(socketPath);

let activeSession = "ses_A";
let statusType = "idle";
const promptCalls: Array<{ sessionID: string; messageID: string; text: string }> = [];
let pendingPrompt: Deferred | null = null;
let dispose: (() => void) | null = null;
const handlers = new Map<string, (event: unknown) => void>();

const api = {
	route: {
		get current() {
			return activeSession === null
				? { name: "home" }
				: { name: "session", params: { sessionID: activeSession } };
		},
	},
	state: {
		session: {
			status: () => ({ type: statusType }),
			messages: () => [],
			permission: () => [],
			question: () => [],
		},
	},
	client: {
		session: {
			// Exact upstream hey-api v2 shape: success {data, request, response},
			// definite non-2xx {error, request, response}, transport failure
			// {error, request, response: undefined}; errors are returned, not thrown.
			promptAsync: async (input: {
				sessionID: string;
				messageID: string;
				parts: Array<{ type: string; text: string }>;
			}) => {
				promptCalls.push({
					sessionID: input.sessionID,
					messageID: input.messageID,
					text: input.parts[0]!.text,
				});
				if (pendingPrompt) {
					await pendingPrompt.promise;
				}
				return { data: undefined, request: {}, response: { status: 204 } };
			},
		},
	},
	event: {
		on: (name: string, handler: (event: unknown) => void) => {
			handlers.set(name, handler);
			return () => handlers.delete(name);
		},
	},
	lifecycle: {
		onDispose: (handler: () => void) => {
			dispose = handler;
		},
	},
};

await plugin.tui(api);
await waitFor(
	() => host.frames.some((frame) => frame.type === "hello" && frame.protocol === PROTOCOL),
	"hello",
);

// Accepted send: exact session, idle status, generated msg id, 204 admission.
request(host, "r1", "opencode.send", sendParams("op-1", "ses_A", "first prompt"));
await waitFor(() => responseFor(host.frames, "r1") !== undefined, "r1 accepted");
const accepted = responseFor(host.frames, "r1")!;
assert.ok(accepted.result, `r1 must be a success: ${JSON.stringify(accepted)}`);
assert.equal(accepted.result.status, "accepted");
assert.equal(accepted.result.operation_id, "op-1");
assert.equal(accepted.result.native_session_id, "ses_A");
assert.ok(
	String(accepted.result.native_turn_id).startsWith("msg_"),
	"messageID must satisfy the upstream msg prefix schema",
);
assert.equal(typeof accepted.result.session_epoch, "number");
assert.equal(promptCalls.length, 1);
assert.ok(promptCalls[0]!.text === "first prompt" && promptCalls[0]!.sessionID === "ses_A");

// Exact duplicate: the cached terminal reply, never a second mutation.
request(host, "r2", "opencode.send", sendParams("op-1", "ses_A", "first prompt"));
await waitFor(() => responseFor(host.frames, "r2") !== undefined, "r2 cached");
assert.deepEqual(responseFor(host.frames, "r2")!.result, accepted.result);
assert.equal(promptCalls.length, 1);

// Busy session: definite pre-mutation rejection.
statusType = "busy";
request(host, "r3", "opencode.send", sendParams("op-3", "ses_A", "busy prompt"));
await waitFor(() => responseFor(host.frames, "r3") !== undefined, "r3 busy");
assert.equal(responseFor(host.frames, "r3")!.error?.code, "busy");
assert.equal(promptCalls.length, 1);
statusType = "idle";

// Wrong session: the visible route is not the requested session.
request(host, "r4", "opencode.send", sendParams("op-4", "ses_B", "other prompt"));
await waitFor(() => responseFor(host.frames, "r4") !== undefined, "r4 wrong_session");
assert.equal(responseFor(host.frames, "r4")!.error?.code, "wrong_session");
assert.equal(promptCalls.length, 1);

// Invalid params and unsupported methods never reach the SDK.
request(host, "r5", "opencode.send", { operation_id: "", native_session_id: "ses_A", prompt: "x" });
await waitFor(() => responseFor(host.frames, "r5") !== undefined, "r5 invalid");
assert.equal(responseFor(host.frames, "r5")!.error?.code, "invalid_request");
request(host, "r6", "opencode.abort", { operation_id: "op-6" });
await waitFor(() => responseFor(host.frames, "r6") !== undefined, "r6 unsupported");
assert.equal(responseFor(host.frames, "r6")!.error?.code, "invalid_request");
request(host, "r7", "opencode.send", { operation_id: "op-7", native_session_id: "ses_A", prompt: "" });
await waitFor(() => responseFor(host.frames, "r7") !== undefined, "r7 blank prompt");
assert.equal(responseFor(host.frames, "r7")!.error?.code, "invalid_request");
assert.equal(promptCalls.length, 1);

// In-flight duplicate: serialized mutations, one receipt per operation.
pendingPrompt = deferred();
request(host, "r8", "opencode.send", sendParams("op-8", "ses_A", "slow prompt"));
await sleep(50);
request(host, "r9", "opencode.send", sendParams("op-8", "ses_A", "slow prompt"));
await waitFor(() => responseFor(host.frames, "r9") !== undefined, "r9 in progress");
assert.equal(responseFor(host.frames, "r9")!.error?.code, "operation_in_progress");
pendingPrompt.resolve(undefined);
pendingPrompt = null;
await waitFor(() => responseFor(host.frames, "r8") !== undefined, "r8 accepted after slow send");
assert.equal(responseFor(host.frames, "r8")!.result?.status, "accepted");
assert.equal(promptCalls.length, 2);

// Transport failure without a definite response: UNKNOWN.
const originalPromptAsync = api.client.session.promptAsync;
api.client.session.promptAsync = async () => {
	return { error: new Error("fetch failed"), request: {}, response: undefined };
};
request(host, "r10", "opencode.send", sendParams("op-10", "ses_A", "lost prompt"));
await waitFor(() => responseFor(host.frames, "r10") !== undefined, "r10 transport");
assert.equal(responseFor(host.frames, "r10")!.error?.code, "delivery_unknown");
// A duplicate of an unknown delivery answers from the cache, never re-mutates.
request(host, "r11", "opencode.send", sendParams("op-10", "ses_A", "lost prompt"));
await waitFor(() => responseFor(host.frames, "r11") !== undefined, "r11 cached unknown");
assert.equal(responseFor(host.frames, "r11")!.error?.code, "delivery_unknown");
assert.equal(promptCalls.length, 2);

// Definite HTTP rejection: REJECTED with the status, no mutation applied.
api.client.session.promptAsync = async () => {
	return { error: { message: "not found" }, request: {}, response: { status: 404 } };
};
request(host, "r12", "opencode.send", sendParams("op-12", "ses_A", "missing session"));
await waitFor(() => responseFor(host.frames, "r12") !== undefined, "r12 rejected");
assert.equal(responseFor(host.frames, "r12")!.error?.code, "native_rejected");
assert.equal(promptCalls.length, 2);
api.client.session.promptAsync = originalPromptAsync;

// Session route moves while a prompt is in flight: UNKNOWN, never a guess.
pendingPrompt = deferred();
request(host, "r13", "opencode.send", sendParams("op-13", "ses_A", "doomed prompt"));
await sleep(50);
assert.equal(promptCalls.length, 3);
activeSession = "ses_B";
pendingPrompt.resolve(undefined);
pendingPrompt = null;
await waitFor(() => responseFor(host.frames, "r13") !== undefined, "r13 unknown");
assert.equal(responseFor(host.frames, "r13")!.error?.code, "delivery_unknown");
// The route switch also cleared the receipt cache for the new epoch.
request(host, "r14", "opencode.send", sendParams("op-1", "ses_B", "replayed prompt"));
await waitFor(() => responseFor(host.frames, "r14") !== undefined, "r14 not cached");
assert.equal(promptCalls.length, 4);
assert.equal(responseFor(host.frames, "r14")!.result?.status, "accepted");
activeSession = "ses_A";
await sleep(50);

// Assistant lineage events forward for the visible session only.
handlers.get("message.updated")!({
	id: "event-message",
	type: "message.updated",
	properties: {
		sessionID: "ses_A",
		info: { id: "asst_1", role: "assistant", parentID: "msg_user_1", time: { completed: 1234 } },
	},
});
await waitFor(
	() => host.frames.some((frame) => frame.type === "event" && frame.event?.type === "message.updated"),
	"lineage forwarded",
);
const lineage = host.frames.find(
	(frame) => frame.type === "event" && frame.event?.type === "message.updated",
)!;
assert.equal(lineage.session_id, "ses_A");
assert.equal(lineage.route_session_id, "ses_A");
assert.equal(typeof lineage.session_epoch, "number");
handlers.get("message.updated")!({
	id: "event-message-user",
	type: "message.updated",
	properties: {
		sessionID: "ses_A",
		info: { id: "msg_user_2", role: "user", parentID: null, time: { created: 1234 } },
	},
});
handlers.get("message.updated")!({
	id: "event-message-other",
	type: "message.updated",
	properties: {
		sessionID: "ses_B",
		info: { id: "asst_2", role: "assistant", parentID: "msg_user_9", time: { completed: 1234 } },
	},
});
await sleep(100);
assert.equal(
	host.frames.filter((frame) => frame.type === "event" && frame.event?.type === "message.updated")
		.length,
	1,
	"only visible-session assistant lineage is forwarded",
);

// Bounded receipt cache: the 65th distinct operation is refused, no mutation.
for (let i = 0; i < 64; i += 1) {
	request(host, `cap-${i}`, "opencode.send", sendParams(`cap-op-${i}`, "ses_A", `prompt ${i}`));
}
await waitFor(
	() =>
		host.frames.filter(
			(frame) => frame.type === "response" && String(frame.id).startsWith("cap-"),
		).length === 64,
	"capacity batch settled",
);
assert.equal(promptCalls.length, 4 + 64);
request(host, "cap-overflow", "opencode.send", sendParams("cap-op-overflow", "ses_A", "overflow"));
await waitFor(() => responseFor(host.frames, "cap-overflow") !== undefined, "capacity refusal");
assert.equal(responseFor(host.frames, "cap-overflow")!.error?.code, "operation_capacity");
assert.equal(promptCalls.length, 4 + 64);

// Oversized inbound frame: the connection is destroyed, fail closed.
assert.ok(!host.destroyed, "connection is alive before the oversized frame");
host.write(`${"x".repeat(70000)}\n`);
await waitFor(() => host.destroyed, "oversized frame destroys the connection");

dispose?.();
await host.close();
console.log("opencode frontend control conformance: ok");
