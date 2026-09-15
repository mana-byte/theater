/**
 * Stock-Pi steer proof through the public SDK and shipped interrupt bridge.
 * Exit codes: 0 ok, 1 failed, 77 skipped.
 */

import assert from "node:assert/strict";
import { execSync } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { existsSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const PROTOCOL = "theater-frontend-v1";
const STEP_MS = 10;
const ADMISSION_DEADLINE_MS = 8_000;

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
	private nextRequestId = 1;
	private sockets: Array<{
		socket: import("node:net").Socket;
		buffer: string;
	}> = [];
	private pending = new Map<
		string,
		{ resolve: (v: JsonRecord) => void; reject: (e: Error) => void }
	>();

	async listen(socketPath: string): Promise<void> {
		const net = await import("node:net");
		this.server = net.createServer((socket) => {
			const entry = { socket, buffer: "" };
			this.sockets.push(entry);
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
			socket.on("close", () => {
				const index = this.sockets.indexOf(entry);
				if (index >= 0) this.sockets.splice(index, 1);
			});
		});
		await new Promise<void>((resolve) =>
			this.server!.listen(socketPath, resolve),
		);
	}

	private server: import("node:net").Server | undefined;

	private consume(frame: HostFrame, socket: import("node:net").Socket): void {
		if (
			frame.type === "hello" ||
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
			socket.write(
				`${JSON.stringify({
					type: "response",
					id,
					error: {
						code: "method_not_found",
						message: "proof host handles nothing",
					},
				})}\n`,
			);
			return;
		}
		throw new Error(`unexpected host frame type: ${String(frame.type)}`);
	}

	request(params: JsonRecord): Promise<JsonRecord> {
		assert.ok(this.sockets.length > 0, "no bridge socket is connected");
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
		this.sockets[this.sockets.length - 1]!.socket.write(
			`${JSON.stringify({ type: "request", id, ...params })}\n`,
		);
		return pending;
	}

	async close(): Promise<void> {
		for (const { socket } of this.sockets) socket.destroy();
		this.sockets = [];
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
			if (Date.now() > deadline)
				throw new Error(`timed out waiting for ${label}`);
			await sleep(STEP_MS);
		}
	}
}

function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollSnapshot(host: LoopbackHost): Promise<JsonRecord> {
	const reply = await host.request({ method: "pi.snapshot", params: {} });
	assert.ok(
		isRecord(reply.result),
		`snapshot must answer, got ${JSON.stringify(reply)}`,
	);
	return reply.result as JsonRecord;
}

async function waitUntil(
	predicate: () => Promise<boolean>,
	label: string,
	deadlineMs = ADMISSION_DEADLINE_MS,
): Promise<void> {
	const deadline = Date.now() + deadlineMs;
	for (;;) {
		if (await predicate()) return;
		if (Date.now() > deadline)
			throw new Error(`timed out waiting for ${label}`);
		await sleep(STEP_MS);
	}
}

interface ProofEntry {
	id: string;
	parentId: string | null;
	type?: string;
	message?: { role?: string; stopReason?: string; content?: unknown };
}

function entryText(entry: ProofEntry): string {
	const content = entry.message?.content;
	if (!Array.isArray(content)) return "";
	const parts: string[] = [];
	for (const part of content) {
		if (
			isRecord(part) &&
			part.type === "text" &&
			typeof part.text === "string"
		) {
			parts.push(part.text);
		}
	}
	return parts.join("\n");
}

function userEntriesWithText(
	sessionManager: { getEntries(): ProofEntry[] },
	text: string,
): ProofEntry[] {
	return sessionManager
		.getEntries()
		.filter(
			(entry) =>
				entry.type === "message" &&
				entry.message?.role === "user" &&
				entryText(entry) === text,
		);
}

async function main(): Promise<number> {
	const stock = resolveStockPi();
	if (stock === undefined) {
		console.log(
			"pi steer admission proof: skipped (stock pi 0.84.x not resolvable)",
		);
		return 77;
	}
	const sdk = (await import(
		pathToFileURL(stock.distIndex).href
	)) as typeof import("pi-sdk");
	const agentCore = (await import(
		pathToFileURL(
			join(
				stock.root,
				"node_modules",
				"@earendil-works",
				"pi-agent-core",
				"dist",
				"index.js",
			),
		).href
	)) as typeof import("pi-agent-core");
	const { EventStream, getModel } = (await import(
		pathToFileURL(
			join(
				stock.root,
				"node_modules",
				"@earendil-works",
				"pi-ai",
				"dist",
				"compat.js",
			),
		).href
	)) as typeof import("pi-ai");

	const tempRoot = await mkdtemp(join(tmpdir(), "pi-steer-proof-"));
	const cwd = join(tempRoot, "cwd");
	const agentDir = join(tempRoot, "agent");
	const sessionDir = join(tempRoot, "sessions");
	for (const dir of [cwd, agentDir, sessionDir]) {
		await mkdir(dir, { recursive: true });
	}
	await writeFile(
		join(agentDir, "auth.json"),
		JSON.stringify({ anthropic: { type: "api_key", key: "proof-key" } }),
	);

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

	const host = new LoopbackHost();
	await host.listen(socketPath);

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
		kind: "done" | "hold";
	}
	interface OpenStream {
		push: (event: Record<string, unknown>) => void;
	}
	const plans: StreamPlan[] = [];
	const openStreams: OpenStream[] = [];
	const releaseHeld = () => {
		for (const open of openStreams.splice(0)) {
			open.push({ type: "done", ...doneEvent() });
		}
	};
	const model = getModel("anthropic", "claude-sonnet-4-5")!;
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
			cost: { input: 0, output: 0, cacheRead: 0, totalTokens: 0 },
		},
		timestamp: Date.now(),
	});
	let streamCallCount = 0;
	const streamContexts: Array<{ messages: unknown[] }> = [];
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
		convertToLlm: sdk.convertToLlm,
		streamFn: (_model, context, options) => {
			streamCallCount += 1;
			streamContexts.push({ messages: context.messages as unknown[] });
			const stream = new MockAssistantStream();
			const plan = plans.shift() ?? { kind: "done" as const };
			const signal = (options as { signal?: AbortSignal } | undefined)?.signal;
			queueMicrotask(() => {
				const open: OpenStream = { push: (event) => stream.push(event) };
				const abortHandler = () => {
					const index = openStreams.indexOf(open);
					if (index >= 0) openStreams.splice(index, 1);
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
				if (signal) {
					if (signal.aborted) abortHandler();
					else signal.addEventListener("abort", abortHandler, { once: true });
				}
				if (plan.kind !== "hold") open.push({ type: "done", ...doneEvent() });
				else openStreams.push(open);
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
	await resourceLoader.reload();

	let abortCount = 0;
	const bindAbortHandler = (target: {
		clearQueue: () => void;
		agent: { abort: () => void };
	}) => {
		return () => {
			abortCount += 1;
			try {
				target.clearQueue();
			} catch {
				// the real restore tolerates a missing editor
			}
			target.agent.abort();
		};
	};

	const session = new sdk.AgentSession({
		agent,
		sessionManager,
		settingsManager,
		cwd,
		modelRuntime,
		resourceLoader,
	} as ConstructorParameters<typeof sdk.AgentSession>[0]);
	session.subscribe(() => undefined);

	await session.bindExtensions({ abortHandler: bindAbortHandler(session) });
	await host.waitFor((frame) => frame.type === "hello", "bridge hello");

	const interrupt = async (expectedTurn: string) => {
		const live = await pollSnapshot(host);
		return host.request({
			method: "pi.control.interrupt",
			params: {
				operation_id: "steer-proof-interrupt",
				native_session_id: live.native_session_id,
				expected_native_turn_id: expectedTurn,
				expected_bridge_epoch: live.bridge_epoch,
			},
		});
	};
	const waitForActiveRun = async () => {
		let snapshot: JsonRecord | undefined;
		await waitUntil(async () => {
			snapshot = await pollSnapshot(host);
			return (
				snapshot.execution_state === "active" &&
				snapshot.native_turn_id !== null
			);
		}, "the held run to establish identity");
		return snapshot!;
	};

	// ===== S1: admission is an in-memory push with no durable id and no
	// delivery evidence; the steer queue stays separate from follow-ups.
	plans.push({ kind: "hold" });
	const runOne = session.prompt("Human turn one");
	await waitForActiveRun();
	const entriesBeforeSteer = sessionManager.getEntries().length;
	const steerOne = session.sendUserMessage("Steer text one", {
		deliverAs: "steer",
	});
	await steerOne;
	assert.equal(
		session.isStreaming,
		true,
		"admission resolves before any delivery evidence exists",
	);
	assert.equal(session.pendingMessageCount, 1);
	assert.deepEqual(session.getSteeringMessages(), ["Steer text one"]);
	assert.deepEqual(session.getFollowUpMessages(), []);
	assert.equal(
		sessionManager.getEntries().length,
		entriesBeforeSteer,
		"admission must not persist a durable steered entry",
	);
	assert.equal(userEntriesWithText(sessionManager, "Steer text one").length, 0);

	// ===== S2: the durable id materializes only at the next turn boundary of
	// the same run, when the steered text reaches the LLM context.
	releaseHeld();
	await runOne;
	const delivered = userEntriesWithText(sessionManager, "Steer text one");
	assert.equal(delivered.length, 1, "delivery persists exactly one user entry");
	assert.equal(session.pendingMessageCount, 0);
	const contextTexts = streamContexts.map((context) =>
		(context.messages as Array<{ role?: string; content?: unknown }>)
			.filter((message) => message.role === "user")
			.map((message) => entryText({ id: "", parentId: null, message })),
	);
	assert.ok(
		contextTexts.some((texts) => texts.includes("Steer text one")),
		"the steered text must reach a later LLM call's context",
	);

	// ===== S3: the production abort route silently discards a queued steer.
	plans.push({ kind: "hold" });
	const runTwo = session.prompt("Human turn two");
	await waitForActiveRun();
	await session.sendUserMessage("Steer text two", { deliverAs: "steer" });
	assert.equal(session.pendingMessageCount, 1);
	const interruptReply = await interrupt(
		sessionManager.getEntries().at(-1)!.id,
	);
	assert.ok(
		isRecord(interruptReply.result),
		`the production interrupt must be accepted, got ${JSON.stringify(interruptReply)}`,
	);
	await runTwo;
	assert.equal(abortCount, 1);
	assert.equal(
		session.pendingMessageCount,
		0,
		"the production abortHandler clears the steer queue",
	);
	assert.equal(
		userEntriesWithText(sessionManager, "Steer text two").length,
		0,
		"the discarded steer leaves no durable trace and no observable outcome",
	);

	// ===== S4: the public abort route converts a queued steer into an
	// automatic replacement run: the aborted run's post-run hook sees the
	// queued message and continue() delivers it as the new run's own prompt.
	plans.push({ kind: "hold" });
	const runThree = session.prompt("Human turn three");
	await waitForActiveRun();
	await session.sendUserMessage("Steer text three", { deliverAs: "steer" });
	assert.equal(session.pendingMessageCount, 1);
	const callsBeforeAbort = streamCallCount;
	await session.abort();
	await runThree;
	assert.ok(
		streamCallCount > callsBeforeAbort,
		"the automatic continuation must run without any new prompt",
	);
	const spilled = userEntriesWithText(sessionManager, "Steer text three");
	assert.equal(
		spilled.length,
		1,
		"the queued steer must spill into an automatic replacement run",
	);
	assert.equal(session.pendingMessageCount, 0);
	const abortedAssistant = sessionManager
		.getEntries()
		.filter(
			(entry) =>
				entry.type === "message" &&
				entry.message?.role === "assistant" &&
				entry.message.stopReason === "aborted",
		)
		.at(-1);
	assert.ok(
		abortedAssistant,
		"the aborted turn must persist its assistant entry",
	);
	assert.equal(
		spilled[0]!.parentId,
		abortedAssistant.id,
		"the spilled entry is parented after the aborted run, not inside it",
	);

	// ===== S5: a steer while idle silently becomes a new run's own prompt.
	const callsBeforeIdleSteer = streamCallCount;
	await session.sendUserMessage("Steer while idle", { deliverAs: "steer" });
	assert.equal(
		streamCallCount,
		callsBeforeIdleSteer + 1,
		"an idle steer must silently start a new run instead of rejecting",
	);
	assert.equal(
		userEntriesWithText(sessionManager, "Steer while idle").length,
		1,
	);

	// ===== S6: duplicate steers enqueue twice and deliver twice.
	plans.push({ kind: "hold" });
	const runFive = session.prompt("Human turn four");
	await waitForActiveRun();
	await session.sendUserMessage("Dup steer", { deliverAs: "steer" });
	await session.sendUserMessage("Dup steer", { deliverAs: "steer" });
	assert.equal(session.pendingMessageCount, 2);
	releaseHeld();
	await runFive;
	assert.equal(
		userEntriesWithText(sessionManager, "Dup steer").length,
		2,
		"duplicate steers must enqueue and deliver twice",
	);
	assert.equal(session.pendingMessageCount, 0);

	await host.close();
	await rm(tempRoot, { recursive: true, force: true });
	console.log("pi steer admission proof: ok");
	return 0;
}

main()
	.then((code) => process.exit(code))
	.catch((error: unknown) => {
		console.error(error);
		process.exit(1);
	});
