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
const { default: theaterMcpBridge } = await import(
	"../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts"
);

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

	get sessionManager(): { getSessionId(): string } {
		return { getSessionId: () => this.api.sessionId };
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
	readonly authGate = new Deferred<void>();
	private settingsAdmission: Deferred<void> | undefined;
	sessionId = "pi-session-a";
	idle = true;
	pending = false;
	thinking = "high";
	model: FakeModel = {
		provider: "openai",
		id: "gpt-5.6",
		reasoning: true,
		thinkingLevelMap: { off: "off", low: "low", medium: "medium", high: "high", max: "max" },
	};
	authDelayedModel: FakeModel = {
		provider: "openai",
		id: "gpt-5.7",
		reasoning: true,
		thinkingLevelMap: { off: "off", low: "low", medium: "medium", high: "high", max: "max" },
	};
	setModelCalls = 0;
	setThinkingCalls = 0;

	on(name: string, handler: Handler): void {
		const callbacks = this.handlers.get(name) ?? [];
		callbacks.push(handler);
		this.handlers.set(name, callbacks);
	}

	registerFlag(name: string): void {
		this.flags.push(name);
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

	async listen(): Promise<number> {
		await new Promise<void>((resolve, reject) => {
			this.server.once("error", reject);
			this.server.listen(0, "127.0.0.1", () => {
				this.server.off("error", reject);
				resolve();
			});
		});
		const address = this.server.address();
		assert.ok(isRecord(address));
		assert.equal(address.address, "127.0.0.1");
		return address.port as number;
	}

	async take(predicate: (frame: JsonRecord) => boolean, label: string): Promise<JsonRecord> {
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
		socket.write(`${JSON.stringify({ type: "request", id, method, params })}\n`);
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
	return (frame) => isRecord(frame.event) && frame.type === "event" && frame.event.name === name;
}

function snapshotFrom(frame: JsonRecord): JsonRecord {
	assert.equal(frame.type, "snapshot");
	assert.ok(isRecord(frame.snapshot));
	return frame.snapshot;
}

async function main(): Promise<void> {
	const host = new LoopbackHost();
	const tempRoot = await mkdtemp(join(tmpdir(), "theater-pi-frontend-"));
	const previousArgv = [...process.argv];
	const api = new FakeExtensionApi();
	try {
		const port = await host.listen();
		const configPath = join(tempRoot, "frontend.json");
		await writeFile(
			configPath,
			JSON.stringify({
				protocol: PROTOCOL,
				participant_id: "pi-conformance",
				endpoint: `tcp://127.0.0.1:${port}`,
				token: "test-token-0123456789",
			}),
			"utf8",
		);
		process.argv.push(`--theater-frontend-config=${configPath}`);

		// The default export is the actual production registration.  A duplicate
		// extension copy must not register a second lifecycle observer.
		await theaterMcpBridge(api as never);
		await theaterMcpBridge(api as never);
		assert.equal(api.handlers.get("session_start")?.length, 1);

		const started = await api.emit("session_start", { reason: "startup" });
		const helloA = await host.take((frame) => frame.type === "hello", "first hello");
		assert.equal(helloA.protocol, PROTOCOL);
		assert.equal(helloA.participant_id, "pi-conformance");
		const initialSnapshot = snapshotFrom(await host.take((frame) => frame.type === "snapshot", "initial snapshot"));
		const epochA = initialSnapshot.bridge_epoch;
		assert.equal(typeof epochA, "number");
		assert.equal(initialSnapshot.native_session_id, "pi-session-a");
		const initialHistory = await host.take((frame) => frame.type === "history", "initial history");
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
		const beforeAgent = await host.take(eventNamed("before_agent_start"), "before-agent event");
		assert.equal((beforeAgent.event as JsonRecord).execution_state, "active");
		// Fresh contexts must not make these transitions disappear as they did
		// with object-identity correlation.
		const agentStart = await host.take(eventNamed("agent_start"), "agent-start event");
		const agentEnd = await host.take(eventNamed("agent_end"), "agent-end event");
		const settled = await host.take(eventNamed("agent_settled"), "settled event");
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
		const retryEnd = await host.take(eventNamed("agent_end"), "retry agent-end event");
		const compact = await host.take(eventNamed("session_before_compact"), "compaction event");
		const retrySettled = await host.take(eventNamed("agent_settled"), "retry settled event");
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
		assert.equal((modelReply.error as JsonRecord).code, "model_update_proof_gated");
		assert.equal(api.setModelCalls, 0);
		assert.equal(api.model.id, "gpt-5.6");

		api.idle = true;
		await api.emit("session_start", { reason: "new" });
		const helloB = await host.take((frame) => frame.type === "hello", "second hello");
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

		await api.emit("session_shutdown", { reason: "exit" });
	} finally {
		api.authGate.resolve(undefined);
		process.argv.splice(0, process.argv.length, ...previousArgv);
		await host.close();
	}

	console.log("pi frontend bridge executable conformance: ok");
}

await main();
