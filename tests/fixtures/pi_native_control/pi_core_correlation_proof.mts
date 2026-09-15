/**
 * Durable Pi core correlation proof for Theater's native send design: the
 * real stock Pi SDK plus the shipped bridge through the genuine extension
 * loader. Phase A proves the correlation matrix; Phase B drives
 * pi.control.send over loopback into the model's own context.
 * Exit codes: 0 ok, 1 failed, 77 skipped (stock Pi not resolvable).
 */

import assert from "node:assert/strict";
import { execSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { existsSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const PROTOCOL = "theater-frontend-v1";
const SEND_CUSTOM_TYPE = "theater:send";
const STEP_MS = 10;
const ADMISSION_DEADLINE_MS = 5_000;

interface StockPi {
	root: string;
	distIndex: string;
	version: string;
}

function resolveStockPi(): StockPi | undefined {
	let binPath: string;
	try {
		binPath = execSync("command -v pi", { encoding: "utf8" }).trim();
	} catch {
		return undefined;
	}
	if (!binPath) return undefined;
	const resolved = realpathSync(binPath);
	if (!resolved.includes("pi-coding-agent")) return undefined;
	const root = dirname(dirname(dirname(resolved)));
	const manifestPath = join(root, "package.json");
	if (!existsSync(manifestPath)) return undefined;
	let version = "";
	try {
		version =
			(
				JSON.parse(
					execSync(`cat ${JSON.stringify(manifestPath)}`, { encoding: "utf8" }),
				) as {
					version?: string;
				}
			).version ?? "";
	} catch {
		return undefined;
	}
	const distIndex = join(root, "dist", "index.js");
	if (!existsSync(distIndex)) return undefined;
	if (!/^0\.84\./.test(version)) return undefined;
	return { root, distIndex, version };
}

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

interface HostFrame {
	type: string;
	[eventKey: string]: unknown;
}

/** Loopback-only NDJSON host standing in for Theater's daemon frontend host. */
class LoopbackHost {
	readonly frames: HostFrame[] = [];
	readonly onRequest: (
		request: JsonRecord,
		respond: (reply: JsonRecord) => void,
	) => void;
	private nextRequestId = 1;
	private sockets = new Set<{
		socket: import("node:net").Socket;
		buffer: string;
	}>();
	// The bridge only answers string request ids; numbers are dropped silently.
	private pending = new Map<
		string,
		{ resolve: (v: JsonRecord) => void; reject: (e: Error) => void }
	>();

	constructor(onRequest: (request: JsonRecord, respond: (reply: JsonRecord) => void) => void) {
		this.onRequest = onRequest;
	}

	async listen(socketPath: string): Promise<void> {
		const net = await import("node:net");
		this.server = net.createServer((socket) => {
			const entry = { socket, buffer: "" };
			this.sockets.add(entry);
			socket.setEncoding("utf8");
			socket.on("data", (chunk: string) => {
				entry.buffer += chunk;
				for (;;) {
					const newline = entry.buffer.indexOf("\n");
					if (newline < 0) return;
					const line = entry.buffer.slice(0, newline);
					entry.buffer = entry.buffer.slice(newline + 1);
					if (!line.trim()) continue;
					this.consume(JSON.parse(line), socket);
				}
			});
			socket.on("close", () => this.sockets.delete(entry));
		});
		await new Promise<void>((resolve) =>
			this.server!.listen(socketPath, resolve),
		);
	}

	private server: import("node:net").Server | undefined;

	private consume(frame: HostFrame, socket: import("node:net").Socket): void {
		if (frame.type === "hello") {
			this.frames.push(frame);
			return;
		}
		if (
			frame.type === "event" ||
			frame.type === "snapshot" ||
			frame.type === "history"
		) {
			this.frames.push(frame);
			return;
		}
		if (frame.type === "response") {
			const id = frame.id;
			const entry = typeof id === "string" ? this.pending.get(id) : undefined;
			if (entry) {
				this.pending.delete(id as string);
				entry.resolve(frame);
			}
			return;
		}
		if (frame.type === "request") {
			const id = frame.id;
			this.onRequest(frame, (reply) => {
				socket.write(`${JSON.stringify({ type: "response", id, ...reply })}\n`);
			});
			return;
		}
		throw new Error(`unexpected host frame type: ${String(frame.type)}`);
	}

	request(params: JsonRecord): Promise<JsonRecord> {
		const sockets = [...this.sockets];
		assert.ok(sockets.length > 0, "no bridge socket is connected");
		const id = `proof-req-${this.nextRequestId++}`;
		const pending = new Promise<JsonRecord>((resolve, reject) => {
			this.pending.set(id, { resolve, reject });
			setTimeout(() => {
				if (this.pending.has(id)) {
					this.pending.delete(id);
					reject(new Error("host request timed out"));
				}
			}, ADMISSION_DEADLINE_MS);
		});
		sockets[0]!.socket.write(
			`${JSON.stringify({ type: "request", id, ...params })}\n`,
		);
		return pending;
	}

	respond(id: string, reply: JsonRecord): void {
		const entry = this.pending.get(id);
		if (entry) {
			this.pending.delete(id);
			entry.resolve(reply);
		}
	}

	async close(): Promise<void> {
		for (const { socket } of this.sockets) socket.destroy();
		this.sockets.clear();
		await new Promise<void>((resolve) => this.server?.close(() => resolve()));
	}

	async waitFor(
		predicate: (frame: HostFrame) => boolean,
		label: string,
	): Promise<HostFrame> {
		const deadline = Date.now() + ADMISSION_DEADLINE_MS;
		for (;;) {
			const found = this.frames.find(predicate);
			if (found) return found;
			if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
			await sleep(STEP_MS);
		}
	}
}

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

interface AdmissionEntry {
	id: string;
	parentId: string | null;
	customType?: string;
	details?: unknown;
}

async function findSendEntry(
	sessionManager: {
		getEntries(): Array<{
			id: string;
			parentId: string | null;
			customType?: string;
			details?: unknown;
		}>;
	},
	operationId: string,
): Promise<AdmissionEntry | undefined> {
	const deadline = Date.now() + ADMISSION_DEADLINE_MS;
	for (;;) {
		const entries = sessionManager.getEntries();
		for (let index = entries.length - 1; index >= 0; index -= 1) {
			const entry = entries[index]!;
			if (entry.customType !== SEND_CUSTOM_TYPE) continue;
			if (isRecord(entry.details) && entry.details.operation_id === operationId) {
				return {
					id: entry.id,
					parentId: entry.parentId,
					customType: entry.customType,
					details: entry.details,
				};
			}
		}
		if (Date.now() > deadline) return undefined;
		await sleep(STEP_MS);
	}
}

async function main(): Promise<number> {
	const stock = resolveStockPi();
	if (stock === undefined) {
		console.log(
			"pi core correlation proof: skipped (stock pi 0.84.x not resolvable)",
		);
		return 77;
	}
	const stockUrl = (pkg: string, entry = "dist/index.js") =>
		pathToFileURL(join(stock.root, "node_modules", pkg, entry)).href;
	const sdk = (await import(
		pathToFileURL(stock.distIndex).href
	)) as typeof import("pi-sdk");
	const agentCore = (await import(
		stockUrl("@earendil-works/pi-agent-core")
	)) as typeof import("pi-agent-core");
	const { EventStream, getModel } = (await import(
		stockUrl("@earendil-works/pi-ai", "dist/compat.js")
	)) as typeof import("pi-ai");

	const tempRoot = await mkdtemp(join(tmpdir(), "pi-native-control-proof-"));
	const cwd = join(tempRoot, "cwd");
	const agentDir = join(tempRoot, "agent");
	const sessionDir = join(tempRoot, "sessions");
	await mkdir(cwd, { recursive: true });
	await mkdir(agentDir, { recursive: true });
	await mkdir(sessionDir, { recursive: true });

	// The genuine load path: --extension feeds additionalExtensionPaths.
	const shippedBridge = fileURLToPath(
		new URL(
			"../../../theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts",
			import.meta.url,
		),
	);

	const socketPath = join(tempRoot, "host.sock");
	const token = "proof-token-0123456789abcdef";
	const configPath = join(tempRoot, "frontend-bridge.json");
	await writeFile(
		configPath,
		JSON.stringify({
			protocol: PROTOCOL,
			participant_id: "proof",
			endpoint: `unix://${socketPath}`,
			token,
		}),
	);
	process.env.PI_CODING_AGENT_DIR = agentDir;
	process.env.THEATER_PI_FRONTEND_CONFIG = configPath;

	const host = new LoopbackHost((request, respond) => {
		assert.ok(isRecord(request.params));
		// Phase B drives pi.control.send; anything else here is unexpected.
		respond({
			error: { code: "method_not_found", message: "proof host handles nothing" },
		});
	});
	await host.listen(socketPath);

	// Mock stream after Pi's own test methodology: each call pops a plan;
	// held streams stay open until released or aborted.
	class MockAssistantStream extends EventStream<
		Record<string, unknown>,
		Record<string, unknown>
	> {
		constructor() {
			super(
				(event) => event.type === "done" || event.type === "error",
				(event) => event.message ?? event.error,
			);
		}
	}
	interface StreamPlan {
		hold: boolean;
	}
	interface OpenStream {
		push: (event: Record<string, unknown>) => void;
		aborted: boolean;
	}
	const plans: StreamPlan[] = [];
	const openStreams: OpenStream[] = [];
	const releaseHeld = () => {
		for (const open of openStreams.splice(0)) {
			if (!open.aborted) open.push({ type: "done", ...doneEvent() });
		}
	};
	const model = getModel("anthropic", "claude-sonnet-4-5")!;
	let streamCallCount = 0;
	const streamContexts: Array<{ messages: unknown[] }> = [];
	const baseMessage = () => ({
		role: "assistant",
		api: "anthropic-messages",
		provider: "anthropic",
		model: "mock",
		usage: {
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 0,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		timestamp: Date.now(),
	});
	function doneEvent(): Record<string, unknown> {
		const text = `reply-${streamCallCount}`;
		return {
			type: "done",
			reason: "stop",
			message: {
				...baseMessage(),
				content: [{ type: "text", text }],
				stopReason: "stop",
			},
		};
	}
	const agent = new agentCore.Agent({
		getApiKey: () => "proof-key",
		initialState: { model, systemPrompt: "You are a proof.", tools: [] },
		// Stock conversion turns custom messages into user messages; the
		// default silently drops them.
		convertToLlm: sdk.convertToLlm,
		streamFn: (_model, context, options) => {
			streamCallCount += 1;
			streamContexts.push({ messages: context.messages as unknown[] });
			const stream = new MockAssistantStream();
			const plan = plans.shift() ?? { hold: false };
			const signal = (options as { signal?: AbortSignal } | undefined)?.signal;
			queueMicrotask(() => {
				stream.push({
					type: "start",
					partial: {
						...baseMessage(),
						content: [{ type: "text", text: `partial-${streamCallCount}` }],
						stopReason: "pending",
					},
				});
				const open: OpenStream = {
					push: (event) => stream.push(event),
					aborted: false,
				};
				openStreams.push(open);
				if (signal) {
					const onAbort = () => {
						open.aborted = true;
						openStreams.splice(openStreams.indexOf(open), 1);
						stream.push({
							type: "error",
							reason: "aborted",
							error: {
								...baseMessage(),
								content: [],
								stopReason: "aborted",
								errorMessage: "aborted",
							},
						});
					};
					if (signal.aborted) onAbort();
					else signal.addEventListener("abort", onAbort, { once: true });
				}
				if (!plan.hold) open.push({ type: "done", ...doneEvent() });
			});
			return stream;
		},
	});

	const sessionManager = sdk.SessionManager.create(cwd, sessionDir);
	const settingsManager = sdk.SettingsManager.create(cwd, agentDir);
	const modelRuntime = await sdk.ModelRuntime.create({
		authPath: join(agentDir, "auth.json"),
		modelsPath: null,
		refreshOnCreate: false,
	});
	const resourceLoader = new sdk.DefaultResourceLoader({
		cwd,
		agentDir,
		settingsManager,
		eventBus: sdk.createEventBus(),
		additionalExtensionPaths: [shippedBridge],
		noSkills: true,
		noThemes: true,
		noPromptTemplates: true,
		noContextFiles: true,
	});
	// reload() is the genuine pre-construction discovery pass.
	await resourceLoader.reload();

	const session = new sdk.AgentSession({
		agent,
		sessionManager,
		settingsManager,
		cwd,
		modelRuntime,
		resourceLoader,
	} as ConstructorParameters<typeof sdk.AgentSession>[0]);
	session.subscribe(() => undefined);

	// ---- genuine extension start: the shipped bridge connects to the host ----
	const loaded = resourceLoader.getExtensions();
	const loadErrors = loaded.errors.map((error) => String(error));
	assert.deepEqual(
		loadErrors,
		[],
		"the shipped bridge must load through the genuine extension loader",
	);
	assert.equal(
		loaded.extensions.length,
		1,
		"the shipped bridge must be the one loaded extension",
	);
	await session.bindExtensions({});
	await host.waitFor((frame) => frame.type === "hello", "bridge hello");
	assert.equal(
		(host.frames.find((frame) => frame.type === "hello") as { token?: string })
			?.token,
		token,
	);

	// ===== Phase A: core correlation matrix =====
	const leafBefore = sessionManager.getLeafId();
	const operation = "op-proof-1";
	const sendPromise = session.sendCustomMessage(
		{
			customType: SEND_CUSTOM_TYPE,
			content: [{ type: "text", text: "Hello from Theater" }],
			display: true,
			details: { operation_id: operation, participant_id: "proof" },
		},
		{ triggerTurn: true },
	);

	// A1: the fire-and-forget send starts the run synchronously — the public
	// session surface reports streaming before the caller's next statement.
	assert.equal(
		session.isStreaming,
		true,
		"the triggered run must be active immediately after the void send",
	);

	// A2: exactly one durable entry maps to the operation id, tree-child of the prior leaf.
	const entry = await findSendEntry(sessionManager, operation);
	assert.ok(
		entry,
		"the custom message entry must persist within the admission window",
	);
	assert.equal(
		entry.parentId,
		leafBefore,
		"the send entry must be a tree-child of the prior leaf",
	);
	const sendEntryId = entry.id;

	// A3: the run's reply is a tree-child of the send entry (terminal
	// correlation by durable tree, not timestamps or event ordering).
	await sendPromise;
	const replyChild = sessionManager
		.getEntries()
		.find(
			(candidate) =>
				candidate.type === "message" && candidate.parentId === sendEntryId,
		);
	assert.ok(
		replyChild,
		"the assistant reply must be a durable tree-child of the send entry",
	);

	// A4: duplicate operation ids are NOT deduplicated by Pi core — the
	// bridge must enforce once-only admission itself.
	await session.sendCustomMessage(
		{
			customType: SEND_CUSTOM_TYPE,
			content: [{ type: "text", text: "duplicate" }],
			display: true,
			details: { operation_id: operation },
		},
		{ triggerTurn: true },
	);
	const duplicates = sessionManager
		.getEntries()
		.filter(
			(candidate) =>
				candidate.customType === SEND_CUSTOM_TYPE &&
				isRecord(candidate.details) &&
				candidate.details.operation_id === operation,
		);
	assert.equal(
		duplicates.length,
		2,
		"Pi core must not deduplicate operation ids",
	);
	await sleep(50);

	// A5: an unguarded send while a run streams is silently absorbed as
	// steering — no rejection, no second run — so busy admission must be
	// refused before delivery, never discovered by the send itself.
	plans.push({ hold: true });
	const heldRun = session.sendCustomMessage(
		{
			customType: SEND_CUSTOM_TYPE,
			content: [{ type: "text", text: "hold" }],
			display: true,
			details: { operation_id: "op-hold" },
		},
		{ triggerTurn: true },
	);
	await findSendEntry(sessionManager, "op-hold");
	assert.equal(
		session.isStreaming,
		true,
		"the held run must still be streaming",
	);
	const callsBeforeSteer = streamCallCount;
	await session.sendCustomMessage(
		{
			customType: SEND_CUSTOM_TYPE,
			content: [{ type: "text", text: "steered" }],
			display: true,
			details: { operation_id: "op-steer" },
		},
		{ triggerTurn: true },
	);
	assert.equal(
		streamCallCount,
		callsBeforeSteer,
		"an unguarded send while streaming must not start a second run",
	);
	assert.equal(
		session.isStreaming,
		true,
		"the steered send is absorbed, not rejected",
	);
	releaseHeld();
	await heldRun;
	const steeredEntries = sessionManager
		.getEntries()
		.filter(
			(candidate) =>
				candidate.customType === SEND_CUSTOM_TYPE &&
				isRecord(candidate.details) &&
				candidate.details.operation_id === "op-steer",
		);
	assert.ok(
		steeredEntries.length >= 1,
		"the steered message is persisted within the same run",
	);

	// A6: abort mid-run terminates with stopReason "aborted" on the final
	// assistant message — the exact-turn interrupted classification fact.
	plans.push({ hold: true });
	const abortRun = session.sendCustomMessage(
		{
			customType: SEND_CUSTOM_TYPE,
			content: [{ type: "text", text: "abort me" }],
			display: true,
			details: { operation_id: "op-abort" },
		},
		{ triggerTurn: true },
	);
	await findSendEntry(sessionManager, "op-abort");
	assert.equal(
		session.isStreaming,
		true,
		"the abort run must still be streaming",
	);
	agent.abort();
	await abortRun;
	const finalMessages = sessionManager
		.getEntries()
		.filter((candidate) => candidate.type === "message")
		.map(
			(candidate) => candidate as unknown as { message?: { stopReason?: string } },
		);
	const lastAssistant = [...finalMessages]
		.reverse()
		.find((candidate) => candidate.message?.stopReason === "aborted");
	assert.ok(
		lastAssistant,
		"an aborted run must leave a durable assistant message with stopReason aborted",
	);

	// A8: a fast-settling run is correlated post-hoc from the durable tree —
	// no event observed during the run, no reliance on ordering.
	const fastRun = session.sendCustomMessage(
		{
			customType: SEND_CUSTOM_TYPE,
			content: [{ type: "text", text: "fast" }],
			display: true,
			details: { operation_id: "op-fast" },
		},
		{ triggerTurn: true },
	);
	await fastRun;
	assert.equal(
		session.isStreaming,
		false,
		"the fast run must have settled before polling",
	);
	const fastEntry = sessionManager
		.getEntries()
		.find(
			(candidate) =>
				candidate.customType === SEND_CUSTOM_TYPE &&
				isRecord(candidate.details) &&
				candidate.details.operation_id === "op-fast",
		);
	assert.ok(fastEntry, "the fast run's entry must be durable after settle");
	const fastReply = sessionManager
		.getEntries()
		.find(
			(candidate) =>
				candidate.type === "message" && candidate.parentId === fastEntry.id,
		);
	assert.ok(
		fastReply,
		"the fast run's reply must still be the durable tree-child",
	);

	// ===== Phase B: the shipped pi.control.send over the loopback host =====
	const phaseBOp = "op-proof-phase-b";
	const phaseBPrompt = "Phase B Theater prompt";
	const phaseBSession = sessionManager.getSessionId();
	const streamCallsBefore = streamContexts.length;
	const phaseBReply = await host.request({
		method: "pi.control.send",
		params: {
			operation_id: phaseBOp,
			native_session_id: phaseBSession,
			prompt: phaseBPrompt,
		},
	});
	assert.ok(isRecord(phaseBReply.result), "pi.control.send must be accepted");
	const phaseBResult = phaseBReply.result as JsonRecord;
	assert.equal(phaseBResult.status, "accepted");
	const phaseBEntry = await findSendEntry(sessionManager, phaseBOp);
	assert.ok(phaseBEntry, "the phase B send must be durable");
	assert.equal(phaseBResult.native_turn_id, phaseBEntry.id);

	// B1: the delivered prompt reached the model's own message context —
	// not merely a custom entry with an assistant tree-child.
	assert.ok(
		streamContexts.length > streamCallsBefore,
		"the send must have triggered a model stream call",
	);
	const phaseBContext = JSON.stringify(
		streamContexts[streamContexts.length - 1]!.messages,
	);
	assert.ok(
		phaseBContext.includes(phaseBPrompt),
		"the model context must contain the Theater prompt text",
	);

	// B2: the settled boundary reports the exact durable turn as completed.
	const phaseBTerminal = await host.waitFor(
		(frame) =>
			isRecord(frame.event) &&
			frame.type === "event" &&
			(frame.event as JsonRecord).name === "turn_terminal" &&
			(frame.event as JsonRecord).operation_id === phaseBOp,
		"phase B turn terminal event",
	);
	const phaseBTerminalEvent = phaseBTerminal.event as JsonRecord;
	assert.equal(phaseBTerminalEvent.native_turn_id, phaseBEntry.id);
	assert.equal(phaseBTerminalEvent.terminal, "completed");
	assert.equal(phaseBTerminalEvent.result, `reply-${streamCallCount}`);

	// A7: persisted identity survives a session reopen — reload keeps entry
	// ids and operation details byte-identical.
	await session.dispose?.();
	const sessionFile = sessionManager.getSessionFile();
	assert.ok(sessionFile, "the proof session must have a file");
	const reopened = sdk.SessionManager.open(sessionFile);
	const reopenedSend = reopened
		.getEntries()
		.find(
			(candidate) =>
				candidate.customType === SEND_CUSTOM_TYPE &&
				isRecord(candidate.details) &&
				candidate.details.operation_id === operation,
		);
	assert.ok(reopenedSend, "the send entry must survive a session reopen");
	assert.equal(
		reopenedSend!.id,
		sendEntryId,
		"entry ids must be stable across reopen",
	);
	assert.deepEqual(
		reopenedSend!.details,
		entry.details,
		"operation details must be stable across reopen",
	);

	await host.close();
	await rm(tempRoot, { recursive: true, force: true });

	console.log("pi core correlation proof: ok");
	return 0;
}

const code = await main();
process.exit(code);
