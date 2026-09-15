/**
 * Executable isolated conformance coverage for Theater's stock Pi extension.
 *
 * This deliberately imports the shipped factory rather than a copied bridge,
 * gives each emitted public lifecycle event a fresh ExtensionContext, and uses
 * a loopback-only NDJSON host.  It does not start Pi, tmux, or Theater's
 * daemon; the separate stock-Pi gate still verifies the upstream runtime.
 */

import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { registerHooks } from "node:module";
import { createServer, type Socket } from "node:net";
import { join } from "node:path";
import { tmpdir } from "node:os";

// Pi supplies typebox to extensions through its documented loader aliases. The
// standalone Node host has no Pi package tree, so provide only the one runtime
// function this extension imports before dynamically loading the actual file.
registerHooks({
	resolve(specifier, context, nextResolve) {
		if (specifier === "typebox") {
			return {
				shortCircuit: true,
				url: "data:text/javascript,export const Type={Unsafe:(value)=>value}",
			};
		}
		return nextResolve(specifier, context);
	},
});
const { default: theaterMcpBridge } =
	await import("../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts");

const PROTOCOL = "theater-frontend-v1";
const TIMEOUT_MS = 3_000;

type JsonRecord = Record<string, unknown>;
type Handler = (event: JsonRecord, ctx: FakeContext) => void | Promise<void>;

function isRecord(value: unknown): value is JsonRecord {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

class Deferred<T> {
	readonly promise: Promise<T>;
	resolve!: (value: T | PromiseLike<T>) => void;

	constructor() {
		this.promise = new Promise<T>((resolve) => {
			this.resolve = resolve;
		});
	}
}

class FakeContext {
	constructor(private readonly api: FakeExtensionApi) {}

	get sessionManager(): {
		getSessionId(): string;
		getEntries(): Array<JsonRecord & { id: unknown }>;
		getLeafId(): string | undefined;
	} {
		return {
			getSessionId: () => this.api.sessionId,
			getEntries: () => this.api.entries.slice(),
			getLeafId: () =>
				this.api.entries.length > 0
					? String(this.api.entries[this.api.entries.length - 1]!.id)
					: undefined,
		};
	}

	get signal(): AbortSignal | undefined {
		return this.api.currentSignal;
	}

	abort(): void {
		this.api.abortCalls += 1;
		this.api.currentController?.abort();
	}

	get model(): FakeModel {
		return this.api.model;
	}

	get scopedModels(): Array<{ model: FakeModel }> {
		return [{ model: this.api.model }, { model: this.api.authDelayedModel }];
	}

	get modelRegistry(): { getAvailable(): FakeModel[] } {
		return { getAvailable: () => [this.api.model, this.api.authDelayedModel] };
	}

	isIdle(): boolean {
		this.api.noteSettingsAdmission();
		return this.api.idle;
	}

	hasPendingMessages(): boolean {
		return this.api.pending;
	}

	get ui(): { setStatus(key: string, text: string | undefined): void } {
		return { setStatus: (key, text) => this.api.statuses.set(key, text) };
	}
}

interface FakeModel {
	provider: string;
	id: string;
	reasoning: boolean;
	thinkingLevelMap: Record<string, string | null>;
}

/** A minimal public ExtensionAPI double; no private Pi runtime is emulated. */
class FakeExtensionApi {
	readonly handlers = new Map<string, Handler[]>();
	readonly contexts: FakeContext[] = [];
	readonly flags: string[] = [];
	readonly entries: Array<JsonRecord & { id: unknown }> = [];
	sendCalls = 0;
	deferSendSettle = false;
	readonly authGate = new Deferred<void>();
	readonly statuses = new Map<string, string | undefined>();
	private settingsAdmission: Deferred<void> | undefined;
	sessionId = "pi-session-a";
	idle = true;
	pending = false;
	thinking = "high";
	model: FakeModel = {
		provider: "openai",
		id: "gpt-5.6",
		reasoning: true,
		thinkingLevelMap: {
			off: "off",
			low: "low",
			medium: "medium",
			high: "high",
			max: "max",
		},
	};
	authDelayedModel: FakeModel = {
		provider: "openai",
		id: "gpt-5.7",
		reasoning: true,
		thinkingLevelMap: {
			off: "off",
			low: "low",
			medium: "medium",
			high: "high",
			max: "max",
		},
	};
	setModelCalls = 0;
	setThinkingCalls = 0;
	currentController: AbortController | undefined;
	currentSignal: AbortSignal | undefined;
	abortCalls = 0;

	on(name: string, handler: Handler): void {
		const callbacks = this.handlers.get(name) ?? [];
		callbacks.push(handler);
		this.handlers.set(name, callbacks);
	}

	registerFlag(name: string): void {
		this.flags.push(name);
	}

	appendEntry(_type: string, _data: JsonRecord): void {
		// Lifecycle markers record entries through the public API; the awaiting
		// status section only needs the calls to be harmless.
	}

	getFlag(_name: string): undefined {
		return undefined;
	}

	getThinkingLevel(): string {
		return this.thinking;
	}

	setThinkingLevel(level: string): void {
		this.setThinkingCalls += 1;
		this.thinking = level;
	}

	armSettingsAdmission(): Promise<void> {
		this.settingsAdmission = new Deferred<void>();
		return this.settingsAdmission.promise;
	}

	noteSettingsAdmission(): void {
		this.settingsAdmission?.resolve(undefined);
		this.settingsAdmission = undefined;
	}

	async setModel(model: FakeModel): Promise<boolean> {
		// This is intentionally an auth-delayed mutation.  A regression that calls
		// public setModel() would be held here while the test switches sessions.
		this.setModelCalls += 1;
		await this.authGate.promise;
		this.model = model;
		return true;
	}

	sendMessage(
		message: {
			customType?: unknown;
			content?: unknown;
			display?: unknown;
			details?: unknown;
		},
		_options: { triggerTurn?: boolean } | undefined,
	): void {
		// Mirrors the durable custom_message entry the real core persists.
		this.sendCalls += 1;
		const entry: JsonRecord & { id: unknown } = {
			id: `entry-${this.entries.length + 1}`,
			customType: message.customType,
			content: message.content,
			display: message.display,
			details: message.details,
		};
		this.idle = false;
		if (!this.deferSendSettle) {
			this.entries.push(entry);
			return;
		}
		// Fast settle: the run completes (and the entry becomes durable)
		// only after the admission poll's first synchronous scan misses it.
		void Promise.resolve().then(async () => {
			await this.emit("message_end", {
				message: {
					role: "assistant",
					stopReason: "stop",
					content: [{ type: "text", text: "fast done" }],
				},
			});
			this.idle = true;
			await this.emit("agent_settled");
			this.entries.push(entry);
		});
	}

	async emit(name: string, event: JsonRecord = {}): Promise<FakeContext> {
		// ExtensionRunner.createContext() returns a new object for each emit().
		const ctx = new FakeContext(this);
		this.contexts.push(ctx);
		for (const handler of this.handlers.get(name) ?? []) {
			await handler({ type: name, ...event }, ctx);
		}
		return ctx;
	}
}

interface PendingFrame {
	predicate: (frame: JsonRecord) => boolean;
	resolve: (frame: JsonRecord) => void;
	reject: (error: Error) => void;
	timer: NodeJS.Timeout;
}

class LoopbackHost {
	private readonly server = createServer((socket) => this.attach(socket));
	private readonly sockets = new Set<Socket>();
	private readonly buffered: JsonRecord[] = [];
	private readonly waiters: PendingFrame[] = [];
	private active: Socket | undefined;
	private nextRequestId = 0;

	async listen(socketPath?: string): Promise<string> {
		await new Promise<void>((resolve, reject) => {
			this.server.once("error", reject);
			this.server.listen(
				socketPath ? { path: socketPath } : { port: 0, host: "127.0.0.1" },
				() => {
					this.server.off("error", reject);
					resolve();
				},
			);
		});
		const address = this.server.address();
		if (typeof address === "string") return `unix://${address}`;
		assert.ok(isRecord(address));
		assert.equal(address.address, "127.0.0.1");
		return `tcp://127.0.0.1:${address.port}`;
	}

	async take(
		predicate: (frame: JsonRecord) => boolean,
		label: string,
	): Promise<JsonRecord> {
		const index = this.buffered.findIndex(predicate);
		if (index >= 0) return this.buffered.splice(index, 1)[0]!;
		return new Promise<JsonRecord>((resolve, reject) => {
			const timer = setTimeout(() => {
				this.removeWaiter(waiter);
				reject(new Error(`timed out waiting for ${label}`));
			}, TIMEOUT_MS);
			const waiter: PendingFrame = { predicate, resolve, reject, timer };
			this.waiters.push(waiter);
		});
	}

	async request(method: string, params: JsonRecord): Promise<JsonRecord> {
		const socket = this.active;
		if (socket === undefined || socket.destroyed || !socket.writable) {
			throw new Error(`no active bridge socket for ${method}`);
		}
		const id = `request-${++this.nextRequestId}`;
		socket.write(
			`${JSON.stringify({ type: "request", id, method, params })}\n`,
		);
		return this.take(
			(frame) => frame.type === "response" && frame.id === id,
			`response for ${method}`,
		);
	}

	async close(): Promise<void> {
		for (const socket of this.sockets) socket.destroy();
		await new Promise<void>((resolve) => this.server.close(() => resolve()));
	}

	private attach(socket: Socket): void {
		this.sockets.add(socket);
		socket.setEncoding("utf8");
		let buffer = "";
		socket.on("data", (chunk: string) => {
			buffer += chunk;
			for (;;) {
				const newline = buffer.indexOf("\n");
				if (newline < 0) return;
				const line = buffer.slice(0, newline);
				buffer = buffer.slice(newline + 1);
				if (!line.trim()) continue;
				const frame: unknown = JSON.parse(line);
				assert.ok(isRecord(frame));
				if (frame.type === "hello") this.active = socket;
				this.record(frame);
			}
		});
		socket.once("close", () => {
			this.sockets.delete(socket);
			if (this.active === socket) this.active = undefined;
		});
	}

	private record(frame: JsonRecord): void {
		const index = this.waiters.findIndex((waiter) => waiter.predicate(frame));
		if (index < 0) {
			this.buffered.push(frame);
			return;
		}
		const waiter = this.waiters.splice(index, 1)[0]!;
		clearTimeout(waiter.timer);
		waiter.resolve(frame);
	}

	private removeWaiter(waiter: PendingFrame): void {
		const index = this.waiters.indexOf(waiter);
		if (index >= 0) this.waiters.splice(index, 1);
	}
}

function eventNamed(name: string): (frame: JsonRecord) => boolean {
	return (frame) =>
		isRecord(frame.event) &&
		frame.type === "event" &&
		frame.event.name === name;
}

function snapshotFrom(frame: JsonRecord): JsonRecord {
	assert.equal(frame.type, "snapshot");
	assert.ok(isRecord(frame.snapshot));
	return frame.snapshot;
}

const tick = (): Promise<void> =>
	new Promise((resolve) => {
		setTimeout(resolve, 10);
	});

async function main(): Promise<void> {
	const host = new LoopbackHost();
	const unixTokenFile = process.argv.includes("--unix-token-file");
	const tempRoot = await mkdtemp(
		join(unixTokenFile ? "/tmp" : tmpdir(), "theater-pi-frontend-"),
	);
	const previousArgv = [...process.argv];
	const previousConfigEnv = process.env.THEATER_PI_FRONTEND_CONFIG;
	const api = new FakeExtensionApi();
	try {
		const endpoint = await host.listen(
			unixTokenFile ? join(tempRoot, "frontend.sock") : undefined,
		);
		const configPath = join(tempRoot, "frontend.json");
		const tokenPath = join(tempRoot, "frontend.token");
		if (unixTokenFile)
			await writeFile(tokenPath, "test-token-0123456789\n", { mode: 0o600 });
		await writeFile(
			configPath,
			JSON.stringify({
				protocol: PROTOCOL,
				participant_id: "pi-conformance",
				endpoint,
				...(unixTokenFile
					? { token_file: tokenPath }
					: { token: "test-token-0123456789" }),
			}),
			"utf8",
		);
		if (unixTokenFile) process.env.THEATER_PI_FRONTEND_CONFIG = configPath;
		else process.argv.push(`--theater-frontend-config=${configPath}`);
		// A launch-style invocation needs a Theater MCP config for the status
		// registration branch to run.  The single server is non-core and exits
		// immediately, so the factory swallows its startup failure exactly as
		// production swallows an optional server's.
		const mcpConfigPath = join(tempRoot, "mcp.json");
		await writeFile(
			mcpConfigPath,
			JSON.stringify({
				mcpServers: { "conformance-fake": { command: "/bin/false", args: [] } },
			}),
			"utf8",
		);

		// The default export is the actual production registration.  A duplicate
		// extension copy must not register a second lifecycle observer.
		await theaterMcpBridge(api as never);
		await theaterMcpBridge(api as never);
		assert.equal(api.handlers.get("session_start")?.length, 1);

		const started = await api.emit("session_start", { reason: "startup" });
		const helloA = await host.take(
			(frame) => frame.type === "hello",
			"first hello",
		);
		assert.equal(helloA.protocol, PROTOCOL);
		assert.equal(helloA.token, "test-token-0123456789");
		assert.equal(helloA.participant_id, "pi-conformance");
		const initialSnapshot = snapshotFrom(
			await host.take((frame) => frame.type === "snapshot", "initial snapshot"),
		);
		const epochA = initialSnapshot.bridge_epoch;
		assert.equal(typeof epochA, "number");
		assert.equal(initialSnapshot.native_session_id, "pi-session-a");
		const initialHistory = await host.take(
			(frame) => frame.type === "history",
			"initial history",
		);
		assert.ok(Array.isArray(initialHistory.events));

		api.idle = false;
		const before = await api.emit("before_agent_start");
		const startedRun = await api.emit("agent_start");
		const endedRun = await api.emit("agent_end");
		api.idle = true;
		const settledRun = await api.emit("agent_settled");
		assert.notEqual(started, before);
		assert.notEqual(before, startedRun);
		assert.notEqual(startedRun, endedRun);
		assert.notEqual(endedRun, settledRun);
		const beforeAgent = await host.take(
			eventNamed("before_agent_start"),
			"before-agent event",
		);
		assert.equal((beforeAgent.event as JsonRecord).execution_state, "active");
		// Fresh contexts must not make these transitions disappear as they did
		// with object-identity correlation.
		const agentStart = await host.take(
			eventNamed("agent_start"),
			"agent-start event",
		);
		const agentEnd = await host.take(
			eventNamed("agent_end"),
			"agent-end event",
		);
		const settled = await host.take(
			eventNamed("agent_settled"),
			"settled event",
		);
		for (const frame of [agentStart, agentEnd, settled]) {
			assert.equal((frame.event as JsonRecord).bridge_epoch, epochA);
		}
		assert.equal((agentEnd.event as JsonRecord).execution_state, "active");
		assert.equal((settled.event as JsonRecord).execution_state, "idle");

		// Retry/compaction stays active through agent_end.  Only the outer settled
		// event may return idle, even though each callback gets another context.
		api.idle = false;
		await api.emit("agent_start");
		await api.emit("agent_end");
		await api.emit("session_before_compact", { willRetry: true });
		await api.emit("session_compact");
		await api.emit("agent_start");
		await api.emit("agent_end");
		api.idle = true;
		await api.emit("agent_settled");
		const retryEnd = await host.take(
			eventNamed("agent_end"),
			"retry agent-end event",
		);
		const compact = await host.take(
			eventNamed("session_before_compact"),
			"compaction event",
		);
		const retrySettled = await host.take(
			eventNamed("agent_settled"),
			"retry settled event",
		);
		assert.equal((retryEnd.event as JsonRecord).execution_state, "active");
		assert.equal((compact.event as JsonRecord).execution_state, "active");
		assert.equal((retrySettled.event as JsonRecord).execution_state, "idle");

		const thinkingReply = await host.request("pi.settings.update", {
			operation_id: "thinking-1",
			native_session_id: "pi-session-a",
			reasoning_effort: "max",
		});
		assert.ok(isRecord(thinkingReply.result));
		assert.equal((thinkingReply.result as JsonRecord).status, "accepted");
		assert.equal(api.thinking, "max");
		assert.equal(api.setThinkingCalls, 1);

		// Native send admission: exact-session, idle-guarded, durably
		// attributed, one cached receipt per operation id.
		const sendReply = await host.request("pi.control.send", {
			operation_id: "send-1",
			native_session_id: "pi-session-a",
			prompt: "hello",
		});
		assert.ok(isRecord(sendReply.result));
		const sendResult = sendReply.result as JsonRecord;
		assert.equal(sendResult.status, "accepted");
		assert.equal(sendResult.operation_id, "send-1");
		assert.equal(sendResult.native_turn_id, "entry-1");
		assert.equal(api.sendCalls, 1);

		const duplicateReply = await host.request("pi.control.send", {
			operation_id: "send-1",
			native_session_id: "pi-session-a",
			prompt: "hello",
		});
		assert.deepEqual(duplicateReply.result, sendReply.result);
		assert.equal(api.sendCalls, 1);

		// sendMessage made the session busy: a different operation is
		// refused, never absorbed into the in-flight turn.
		const busyReply = await host.request("pi.control.send", {
			operation_id: "send-2",
			native_session_id: "pi-session-a",
			prompt: "while streaming",
		});
		assert.ok(isRecord(busyReply.error));
		assert.equal((busyReply.error as JsonRecord).code, "busy");

		const wrongSessionReply = await host.request("pi.control.send", {
			operation_id: "send-3",
			native_session_id: "pi-session-b",
			prompt: "elsewhere",
		});
		assert.ok(isRecord(wrongSessionReply.error));
		assert.equal((wrongSessionReply.error as JsonRecord).code, "wrong_session");

		// The settled boundary classifies the attributed turn from the
		// final assistant message, never from event ordering.
		await api.emit("message_end", {
			message: {
				role: "assistant",
				stopReason: "stop",
				content: [{ type: "text", text: "all done" }],
			},
		});
		api.idle = true;
		await api.emit("agent_settled");
		const turnTerminal = await host.take(
			eventNamed("turn_terminal"),
			"turn terminal event",
		);
		const terminalEvent = turnTerminal.event as JsonRecord;
		assert.equal(terminalEvent.operation_id, "send-1");
		assert.equal(terminalEvent.native_turn_id, "entry-1");
		assert.equal(terminalEvent.terminal, "completed");
		assert.equal(terminalEvent.result, "all done");

		// --- active-run identity and native interrupt -------------------------
		// A held run with a live signal: identity begins at agent_start and is
		// the first durable entry after the baseline, exactly as stock persists
		// the trigger entry inside the same unwind that started the run.
		const heldRun = new AbortController();
		api.currentController = heldRun;
		api.currentSignal = heldRun.signal;
		api.idle = false;
		await api.emit("agent_start");
		// The pre-identity window publishes "unknown", never "active".
		const unknownSnapshot = snapshotFrom(
			await host.take(
				(frame) =>
					frame.type === "snapshot" &&
					isRecord(frame.snapshot) &&
					frame.snapshot.execution_state === "unknown",
				"unestablished-identity snapshot",
			),
		);
		assert.equal(unknownSnapshot.native_turn_id, null);
		assert.equal(unknownSnapshot.capabilities.interrupt, true);
		const interruptTurnReply = await host.request("pi.control.interrupt", {
			operation_id: "interrupt-unestablished",
			native_session_id: "pi-session-a",
			expected_native_turn_id: "entry-2",
		});
		assert.ok(isRecord(interruptTurnReply.error));
		assert.equal(
			(interruptTurnReply.error as JsonRecord).code,
			"no_active_run",
		);
		assert.equal(api.abortCalls, 0);

		api.entries.push({
			id: "entry-2",
			type: "custom_message",
			customType: "theater:send",
			parentId: "entry-1",
			details: { operation_id: "send-held" },
		});
		const identityReply = await host.request("pi.snapshot", {});
		assert.ok(isRecord(identityReply.result));
		const identitySnapshot = identityReply.result as JsonRecord;
		assert.equal(identitySnapshot.execution_state, "active");
		assert.equal(identitySnapshot.native_turn_id, "entry-2");

		// A stale turn id is rejected before the mutation: no abort, no signal.
		const staleReply = await host.request("pi.control.interrupt", {
			operation_id: "interrupt-stale",
			native_session_id: "pi-session-a",
			expected_native_turn_id: "entry-1",
		});
		assert.ok(isRecord(staleReply.error));
		assert.equal((staleReply.error as JsonRecord).code, "stale_turn");
		assert.equal(api.abortCalls, 0);

		// Wrong session is rejected pre-mutation as well.
		const wrongSessionInterrupt = await host.request("pi.control.interrupt", {
			operation_id: "interrupt-wrong-session",
			native_session_id: "pi-session-b",
			expected_native_turn_id: "entry-2",
		});
		assert.ok(isRecord(wrongSessionInterrupt.error));
		assert.equal(
			(wrongSessionInterrupt.error as JsonRecord).code,
			"wrong_session",
		);
		assert.equal(api.abortCalls, 0);

		// The exact turn id aborts exactly once; the reply waits for evidence.
		const interruptPending = host.request("pi.control.interrupt", {
			operation_id: "interrupt-1",
			native_session_id: "pi-session-a",
			expected_native_turn_id: "entry-2",
		});
		await tick();
		assert.equal(api.abortCalls, 1);
		assert.ok(heldRun.signal.aborted);
		// A duplicate while evidence is pending is bounded, never a second abort.
		const inProgressReply = await host.request("pi.control.interrupt", {
			operation_id: "interrupt-1",
			native_session_id: "pi-session-a",
			expected_native_turn_id: "entry-2",
		});
		assert.ok(isRecord(inProgressReply.error));
		assert.equal(
			(inProgressReply.error as JsonRecord).code,
			"operation_in_progress",
		);
		assert.equal(api.abortCalls, 1);
		// Evidence completes only on this run's aborted terminal and settle.
		await api.emit("message_end", {
			message: { role: "assistant", stopReason: "aborted", content: [] },
		});
		api.idle = true;
		await api.emit("agent_settled");
		const interruptReply = await interruptPending;
		assert.ok(isRecord(interruptReply.result));
		const interruptResult = interruptReply.result as JsonRecord;
		assert.equal(interruptResult.status, "accepted");
		assert.equal(interruptResult.operation_id, "interrupt-1");
		assert.equal(interruptResult.native_turn_id, "entry-2");
		assert.equal(interruptResult.execution_state, "idle");
		// The completed duplicate returns the cached receipt, no re-abort.
		const cachedReply = await host.request("pi.control.interrupt", {
			operation_id: "interrupt-1",
			native_session_id: "pi-session-a",
			expected_native_turn_id: "entry-2",
		});
		assert.deepEqual(cachedReply.result, interruptReply.result);
		assert.equal(api.abortCalls, 1);

		// After settle the same id names no active run: rejected, no mutation.
		const idleInterrupt = await host.request("pi.control.interrupt", {
			operation_id: "interrupt-idle",
			native_session_id: "pi-session-a",
			expected_native_turn_id: "entry-2",
		});
		assert.ok(isRecord(idleInterrupt.error));
		assert.equal((idleInterrupt.error as JsonRecord).code, "no_active_run");
		assert.equal(api.abortCalls, 1);

		// A replacement run gets a fresh identity; the old id can never reach it.
		const replacement = new AbortController();
		api.currentController = replacement;
		api.currentSignal = replacement.signal;
		api.idle = false;
		await api.emit("agent_start");
		api.entries.push({
			id: "entry-3",
			type: "message",
			parentId: "entry-2",
			message: { role: "user", content: [] },
		});
		const staleAfterReplace = await host.request("pi.control.interrupt", {
			operation_id: "interrupt-stale-replacement",
			native_session_id: "pi-session-a",
			expected_native_turn_id: "entry-2",
		});
		assert.ok(isRecord(staleAfterReplace.error));
		assert.equal((staleAfterReplace.error as JsonRecord).code, "stale_turn");
		assert.equal(api.abortCalls, 1);
		assert.ok(!replacement.signal.aborted);

		// An already-aborted signal is not cancellable: rejected pre-mutation.
		const finishedSignal = new AbortController();
		finishedSignal.abort();
		api.currentSignal = finishedSignal.signal;
		const notCancellable = await host.request("pi.control.interrupt", {
			operation_id: "interrupt-not-cancellable",
			native_session_id: "pi-session-a",
			expected_native_turn_id: "entry-3",
		});
		assert.ok(isRecord(notCancellable.error));
		assert.equal((notCancellable.error as JsonRecord).code, "not_cancellable");
		assert.equal(api.abortCalls, 1);
		api.currentSignal = replacement.signal;

		// The replacement aborts but settles with a completed terminal: the
		// outcome cannot be confirmed, so UNKNOWN — never ACCEPTED, never retried.
		const unknownPending = host.request("pi.control.interrupt", {
			operation_id: "interrupt-unknown",
			native_session_id: "pi-session-a",
			expected_native_turn_id: "entry-3",
		});
		await tick();
		assert.equal(api.abortCalls, 2);
		await api.emit("message_end", {
			message: { role: "assistant", stopReason: "stop", content: [] },
		});
		api.idle = true;
		await api.emit("agent_settled");
		const unknownReply = await unknownPending;
		assert.ok(isRecord(unknownReply.error));
		assert.equal(
			(unknownReply.error as JsonRecord).code,
			"interrupt_unconfirmed",
		);
		assert.equal(api.abortCalls, 2);

		// Fast settle: the run settles before the admission poll sees the
		// durable entry.  The readback honestly reports no active turn, but
		// the receipt must still carry the durable entry id.
		api.deferSendSettle = true;
		const fastReply = await host.request("pi.control.send", {
			operation_id: "send-fast",
			native_session_id: "pi-session-a",
			prompt: "fast",
		});
		assert.ok(isRecord(fastReply.result));
		const fastResult = fastReply.result as JsonRecord;
		assert.equal(fastResult.status, "accepted");
		assert.equal(fastResult.execution_state, "idle");
		assert.equal(fastResult.native_turn_id, "entry-4");
		const fastTerminal = await host.take(
			(frame) =>
				isRecord(frame.event) &&
				frame.type === "event" &&
				(frame.event as JsonRecord).name === "turn_terminal" &&
				(frame.event as JsonRecord).operation_id === "send-fast",
			"fast turn terminal event",
		);
		const fastTerminalEvent = fastTerminal.event as JsonRecord;
		assert.equal(fastTerminalEvent.native_turn_id, "entry-4");
		assert.equal(fastTerminalEvent.terminal, "completed");
		assert.equal(fastTerminalEvent.result, "fast done");

		// The fake public setModel has an unresolved auth await.  The admission
		// probe makes the old unsafe implementation yield at that await; changing
		// the live session before releasing it reproduces the wrong-session
		// mutation race. The fixed bridge rejects before entering setModel.
		const modelAdmission = api.armSettingsAdmission();
		const pendingModelReply = host.request("pi.settings.update", {
			operation_id: "model-auth-race",
			native_session_id: "pi-session-a",
			model: "openai/gpt-5.7",
			reasoning_effort: "high",
		});
		await modelAdmission;
		api.sessionId = "pi-session-b";
		api.authGate.resolve(undefined);
		const modelReply = await pendingModelReply;
		assert.ok(isRecord(modelReply.error));
		assert.equal(
			(modelReply.error as JsonRecord).code,
			"model_update_proof_gated",
		);
		assert.equal(api.setModelCalls, 0);
		assert.equal(api.model.id, "gpt-5.6");

		api.idle = true;
		await api.emit("session_start", { reason: "new" });
		const helloB = await host.take(
			(frame) => frame.type === "hello",
			"second hello",
		);
		assert.equal(helloB.protocol, PROTOCOL);
		const sessionBSnapshot = snapshotFrom(
			await host.take(
				(frame) =>
					frame.type === "snapshot" &&
					isRecord(frame.snapshot) &&
					frame.snapshot.native_session_id === "pi-session-b",
				"session-b snapshot",
			),
		);
		assert.equal(sessionBSnapshot.native_session_id, "pi-session-b");
		assert.ok((sessionBSnapshot.bridge_epoch as number) > (epochA as number));
		const sessionBHistory = await host.take(
			(frame) =>
				frame.type === "history" &&
				isRecord(frame.snapshot) &&
				frame.snapshot.native_session_id === "pi-session-b",
			"session-b history",
		);
		assert.ok(Array.isArray(sessionBHistory.events));
		for (const event of sessionBHistory.events as unknown[]) {
			assert.ok(isRecord(event));
			assert.equal(event.native_session_id, "pi-session-b");
			assert.equal(event.bridge_epoch, sessionBSnapshot.bridge_epoch);
		}
		// --- the awaiting-input footer contract ---------------------------------
		// A launch-style factory call registers the idle and awaiting footers.
		// A user-input tool call parks Pi on a human decision mid-turn, and the
		// marker is the exact display contract Theater's screen classifier
		// reads; every turn boundary clears it.
		process.argv.push(`--theater-mcp-config=${mcpConfigPath}`);
		const statusApi = new FakeExtensionApi();
		statusApi.idle = false;
		await theaterMcpBridge(statusApi as never);
		process.argv.pop();
		const statusOf = (key: string): string | undefined =>
			statusApi.statuses.get(key);
		await statusApi.emit("before_agent_start");
		await statusApi.emit("tool_call", { toolName: "bash", toolCallId: "b1" });
		assert.equal(statusOf("theater.pi.awaiting"), undefined);
		await statusApi.emit("tool_call", {
			toolName: "ask_user_question",
			toolCallId: "q1",
		});
		assert.equal(statusOf("theater.pi.awaiting"), "Theater: awaiting input");
		// The frontend bridge pushes a snapshot the moment the interaction
		// changes, carrying the pending interaction for the daemon's live
		// channel.  Only the question tool call produces a non-null entry.
		const interactionSnapshot = snapshotFrom(
			await host.take(
				(frame) =>
					frame.type === "snapshot" &&
					isRecord(frame.snapshot) &&
					frame.snapshot.pending_interaction !== null,
				"pending-interaction snapshot",
			),
		);
		const pending = interactionSnapshot.pending_interaction as JsonRecord;
		assert.equal(pending.kind, "clarification");
		assert.equal(pending.details, "ask_user_question");
		assert.equal(pending.native_turn_id, null);
		await statusApi.emit("tool_result", { toolCallId: "b1" });
		assert.equal(statusOf("theater.pi.awaiting"), "Theater: awaiting input");
		await statusApi.emit("tool_result", { toolCallId: "q1" });
		assert.equal(statusOf("theater.pi.awaiting"), undefined);
		const clearedSnapshot = snapshotFrom(
			await host.take(
				(frame) =>
					frame.type === "snapshot" &&
					isRecord(frame.snapshot) &&
					(frame.snapshot.snapshot_revision as number) >
						(interactionSnapshot.snapshot_revision as number),
				"cleared-interaction snapshot",
			),
		);
		assert.equal(clearedSnapshot.pending_interaction, null);
		statusApi.idle = true;
		await statusApi.emit("agent_settled");
		assert.equal(statusOf("theater.pi.idle"), "Theater: idle");
		statusApi.idle = false;
		await statusApi.emit("agent_start");
		await statusApi.emit("tool_call", {
			toolName: "ask_user_question",
			toolCallId: "q2",
		});
		assert.equal(statusOf("theater.pi.awaiting"), "Theater: awaiting input");
		await statusApi.emit("agent_settled");
		assert.equal(statusOf("theater.pi.awaiting"), undefined);

		await api.emit("session_shutdown", { reason: "exit" });
	} finally {
		api.authGate.resolve(undefined);
		process.argv.splice(0, process.argv.length, ...previousArgv);
		if (previousConfigEnv === undefined)
			delete process.env.THEATER_PI_FRONTEND_CONFIG;
		else process.env.THEATER_PI_FRONTEND_CONFIG = previousConfigEnv;
		await host.close();
	}

	console.log("pi frontend bridge executable conformance: ok");
}

await main();
