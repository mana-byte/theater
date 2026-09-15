/**
 * Durable stock-Pi proof for Theater's native interrupt design: the real
 * installed Pi SDK plus the shipped bridge through the genuine extension
 * loader, with a TUI-shaped abortHandler like production's interactive mode.
 * Proves the exact active-run identity per turn source, once-only abort
 * evidence, the retry-backoff not-cancellable window, the post-abort UNKNOWN
 * timeout, and session-replacement isolation.
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
const ADMISSION_DEADLINE_MS = 8_000;
const EVIDENCE_WINDOW_MS = 5_000;

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
	helloCount = 0;
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
		if (frame.type === "hello") {
			this.helloCount += 1;
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

	// Requests ride the most recently connected bridge: after a session
	// replacement that is the replacement's bridge, never the disposed one.
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
		deadlineMs = ADMISSION_DEADLINE_MS,
	): Promise<HostFrame> {
		const deadline = Date.now() + deadlineMs;
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
	customType?: string;
	details?: unknown;
	message?: { role?: string; stopReason?: string };
}

async function findSendEntry(
	sessionManager: { getEntries(): ProofEntry[] },
	operationId: string,
): Promise<ProofEntry | undefined> {
	const deadline = Date.now() + ADMISSION_DEADLINE_MS;
	for (;;) {
		const entries = sessionManager.getEntries();
		for (let index = entries.length - 1; index >= 0; index -= 1) {
			const entry = entries[index]!;
			if (entry.customType !== SEND_CUSTOM_TYPE) continue;
			if (
				isRecord(entry.details) &&
				entry.details.operation_id === operationId
			) {
				return entry;
			}
		}
		if (Date.now() > deadline) return undefined;
		await sleep(STEP_MS);
	}
}

function errorCode(reply: JsonRecord): string {
	assert.ok(
		isRecord(reply.error) && typeof reply.error.code === "string",
		`expected an error reply, got ${JSON.stringify(reply)}`,
	);
	return reply.error.code as string;
}

async function main(): Promise<number> {
	const stock = resolveStockPi();
	if (stock === undefined) {
		console.log(
			"pi active-run interrupt proof: skipped (stock pi 0.84.x not resolvable)",
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

	const tempRoot = await mkdtemp(join(tmpdir(), "pi-interrupt-proof-"));
	const cwd = join(tempRoot, "cwd");
	const agentDir = join(tempRoot, "agent");
	const sessionDir = join(tempRoot, "sessions-a");
	const sessionDirB = join(tempRoot, "sessions-b");
	for (const dir of [cwd, agentDir, sessionDir, sessionDirB]) {
		await mkdir(dir, { recursive: true });
	}
	// A real retry backoff window long enough to interrogate, with one retry.
	await writeFile(
		join(agentDir, "settings.json"),
		JSON.stringify({
			retry: { enabled: true, maxRetries: 1, baseDelayMs: 1_000 },
		}),
	);
	// prompt() preflights provider auth; the mock stream never uses the key.
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

	// Mock stream after Pi's own test methodology: each call pops a plan;
	// held streams stay open until released, and aborts unwind them unless
	// the plan says the stream swallows the abort (the UNKNOWN timeout case).
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
		kind: "done" | "hold" | "retryable-error";
		ignoreAbort?: boolean;
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
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		timestamp: Date.now(),
	});
	let streamCallCount = 0;
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
	const buildAgent = () =>
		new agentCore.Agent({
			getApiKey: () => "proof-key",
			initialState: { model, systemPrompt: "You are a proof.", tools: [] },
			convertToLlm: sdk.convertToLlm,
			streamFn: (_model, _context, options) => {
				streamCallCount += 1;
				const stream = new MockAssistantStream();
				const plan = plans.shift() ?? { kind: "done" as const };
				const signal = (options as { signal?: AbortSignal } | undefined)
					?.signal;
				queueMicrotask(() => {
					const open: OpenStream = { push: (event) => stream.push(event) };
					openStreams.push(open);
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
					// A swallowing plan models a provider that ignores the abort
					// signal: the evidence window must time out to UNKNOWN.
					if (signal && plan.ignoreAbort !== true) {
						if (signal.aborted) abortHandler();
						else signal.addEventListener("abort", abortHandler, { once: true });
					}
					if (plan.kind === "retryable-error") {
						open.push({
							type: "error",
							reason: "error",
							error: {
								...baseMessage(),
								content: [],
								stopReason: "error",
								errorMessage: "provider returned error: overloaded 429",
							},
						});
						return;
					}
					stream.push({
						type: "start",
						partial: {
							...baseMessage(),
							content: [{ type: "text", text: `partial-${streamCallCount}` }],
							stopReason: "pending",
						},
					});
					if (plan.kind === "done") open.push({ type: "done", ...doneEvent() });
				});
				return stream;
			},
		});
	const agent = buildAgent();

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

	// Production launches `pi` interactive, whose abortHandler clears the
	// queues then aborts the agent; the proof binds the same shape.
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

	const loaded = resourceLoader.getExtensions();
	assert.deepEqual(
		loaded.errors.map((error) => String(error)),
		[],
		"the shipped bridge must load through the genuine extension loader",
	);
	await session.bindExtensions({ abortHandler: bindAbortHandler(session) });
	await host.waitFor((frame) => frame.type === "hello", "bridge hello");

	const interrupt = (
		operationId: string,
		expectedTurn: string,
		sessionId: string,
	) =>
		host.request({
			method: "pi.control.interrupt",
			params: {
				operation_id: operationId,
				native_session_id: sessionId,
				expected_native_turn_id: expectedTurn,
			},
		});

	// ===== S1: a human turn identifies as its own durable user entry, and an
	// interrupt on that exact id aborts exactly once with settled evidence.
	const leafBefore = sessionManager.getLeafId();
	plans.push({ kind: "hold" });
	const promptPromise = session.prompt("Human turn one");
	let humanSnapshot: JsonRecord | undefined;
	await waitUntil(async () => {
		humanSnapshot = await pollSnapshot(host);
		return (
			humanSnapshot.execution_state === "active" &&
			humanSnapshot.native_turn_id !== null
		);
	}, "the human run to establish identity");
	const userEntry = sessionManager
		.getEntries()
		.find(
			(candidate) =>
				candidate.type === "message" &&
				candidate.message?.role === "user" &&
				candidate.parentId === leafBefore,
		);
	assert.ok(userEntry, "the human turn must persist a user entry");
	assert.equal(
		humanSnapshot!.native_turn_id,
		userEntry.id,
		"the active run id must be the human turn's own durable entry",
	);
	const sessionAId = humanSnapshot!.native_session_id as string;
	const humanReply = await interrupt(
		"op-interrupt-human",
		userEntry.id,
		sessionAId,
	);
	assert.ok(
		isRecord(humanReply.result),
		`human interrupt must be accepted: ${JSON.stringify(humanReply)}`,
	);
	assert.equal((humanReply.result as JsonRecord).status, "accepted");
	assert.equal((humanReply.result as JsonRecord).native_turn_id, userEntry.id);
	assert.equal((humanReply.result as JsonRecord).execution_state, "idle");
	assert.equal(abortCount, 1, "the abortHandler must fire exactly once");
	await promptPromise;
	const lastAssistantA = [...sessionManager.getEntries()]
		.reverse()
		.find(
			(candidate) =>
				candidate.type === "message" && candidate.message?.role === "assistant",
		);
	assert.equal(
		lastAssistantA?.message?.stopReason,
		"aborted",
		"the interrupted run must settle with a durable aborted stopReason",
	);

	// ===== S2: a Theater send identifies as its theater:send entry; the
	// previous human turn id is stale, and the replacement is unharmed.
	plans.push({ kind: "hold" });
	const heldSend = await host.request({
		method: "pi.control.send",
		params: {
			operation_id: "op-held-send",
			native_session_id: sessionAId,
			prompt: "Held Theater turn",
		},
	});
	assert.ok(
		isRecord(heldSend.result),
		`held send must be accepted: ${JSON.stringify(heldSend)}`,
	);
	const heldEntry = await findSendEntry(sessionManager, "op-held-send");
	assert.ok(heldEntry, "the held send must be durable");
	assert.equal(
		(heldSend.result as JsonRecord).native_turn_id,
		heldEntry.id,
		"the send reply must name the send entry as the active turn",
	);
	let heldSnapshot: JsonRecord | undefined;
	await waitUntil(async () => {
		heldSnapshot = await pollSnapshot(host);
		return heldSnapshot.native_turn_id === heldEntry.id;
	}, "the held run to report the send entry id");
	assert.equal(heldSnapshot!.execution_state, "active");
	const staleReply = await interrupt("op-stale", userEntry.id, sessionAId);
	assert.equal(
		errorCode(staleReply),
		"stale_turn",
		"the settled human turn id must be rejected as stale",
	);
	assert.equal(abortCount, 1, "a stale interrupt must not abort");
	const heldInterrupt = await interrupt(
		"op-interrupt-send",
		heldEntry.id,
		sessionAId,
	);
	assert.ok(
		isRecord(heldInterrupt.result),
		`the held send interrupt must be accepted: ${JSON.stringify(heldInterrupt)}`,
	);
	assert.equal((heldInterrupt.result as JsonRecord).status, "accepted");
	assert.equal(abortCount, 2);

	// ===== S3: once settled there is no active run to interrupt.
	await waitUntil(async () => {
		const snapshot = await pollSnapshot(host);
		return snapshot.execution_state === "idle";
	}, "the session to settle after the accepted interrupt");
	const idleReply = await interrupt("op-idle", heldEntry.id, sessionAId);
	assert.equal(errorCode(idleReply), "no_active_run");
	assert.equal(abortCount, 2);

	// ===== S4: during the retry backoff there is no live cancellable signal
	// (not_cancellable), and the retried continuation keeps the same identity.
	plans.push({ kind: "retryable-error" }, { kind: "hold" });
	const retrySend = await host.request({
		method: "pi.control.send",
		params: {
			operation_id: "op-retry-send",
			native_session_id: sessionAId,
			prompt: "Retry me",
		},
	});
	assert.ok(
		isRecord(retrySend.result),
		`retry send must be admitted: ${JSON.stringify(retrySend)}`,
	);
	const retryEntry = await findSendEntry(sessionManager, "op-retry-send");
	assert.ok(retryEntry, "the retry send must be durable");
	await host.waitFor(
		(frame) =>
			isRecord(frame.event) &&
			frame.type === "event" &&
			(frame.event as JsonRecord).name === "agent_end",
		"the failed attempt's agent_end",
	);
	const backoffReply = await interrupt(
		"op-retry-backoff",
		retryEntry!.id,
		sessionAId,
	);
	assert.equal(
		errorCode(backoffReply),
		"not_cancellable",
		"the retry backoff window must reject without a live signal",
	);
	assert.equal(abortCount, 2, "a backoff rejection must not abort");
	// The backoff itself keeps the outer run active with the same identity,
	// so only the continuation's own stream call distinguishes the retried
	// attempt from the backoff window.
	const callsAfterFailure = streamCallCount;
	await waitUntil(
		() => Promise.resolve(streamCallCount > callsAfterFailure),
		"the retried attempt to start",
		4_000,
	);
	let retriedSnapshot: JsonRecord | undefined;
	await waitUntil(async () => {
		retriedSnapshot = await pollSnapshot(host);
		return (
			retriedSnapshot.execution_state === "active" &&
			retriedSnapshot.native_turn_id === retryEntry!.id
		);
	}, "the retried continuation to keep the same identity");
	releaseHeld();
	await waitUntil(async () => {
		const snapshot = await pollSnapshot(host);
		return snapshot.execution_state === "idle";
	}, "the retried run to settle");
	const lastAssistantB = [...sessionManager.getEntries()]
		.reverse()
		.find(
			(candidate) =>
				candidate.type === "message" && candidate.message?.role === "assistant",
		);
	assert.equal(
		lastAssistantB?.message?.stopReason,
		"stop",
		"the unrejected retry must complete normally",
	);

	// ===== S5: an abort the run swallows leaves the outcome unordered, so
	// the reply stays UNKNOWN after the evidence window — never ACCEPTED.
	plans.push({ kind: "hold", ignoreAbort: true });
	const stuckSend = await host.request({
		method: "pi.control.send",
		params: {
			operation_id: "op-stuck-send",
			native_session_id: sessionAId,
			prompt: "Swallow the abort",
		},
	});
	assert.ok(
		isRecord(stuckSend.result),
		`stuck send must be admitted: ${JSON.stringify(stuckSend)}`,
	);
	const stuckEntry = await findSendEntry(sessionManager, "op-stuck-send");
	assert.ok(stuckEntry, "the stuck send must be durable");
	let stuckSnapshot: JsonRecord | undefined;
	await waitUntil(async () => {
		stuckSnapshot = await pollSnapshot(host);
		return stuckSnapshot.native_turn_id === stuckEntry!.id;
	}, "the stuck run to report identity");
	const stuckReply = await interrupt(
		"op-stuck-interrupt",
		stuckEntry!.id,
		sessionAId,
	);
	assert.equal(
		errorCode(stuckReply),
		"interrupt_unconfirmed",
		"a swallowed abort must stay UNKNOWN, never ACCEPTED",
	);
	assert.equal(abortCount, 3, "the swallowed abort still fired exactly once");
	releaseHeld();
	await waitUntil(async () => {
		const snapshot = await pollSnapshot(host);
		return snapshot.execution_state === "idle";
	}, "the stuck run to settle after release");

	// ===== S6: a session replacement is fully isolated: the old session id
	// is wrong_session, and the replacement's own run interrupts exactly once.
	// This is the genuine replacement path: the runtime host aborts and
	// disposes the outgoing session (session_shutdown → bridge dispose) and
	// builds the replacement with a fresh agent, exactly as production does.
	const createRuntime: NonNullable<
		ConstructorParameters<typeof sdk.AgentSessionRuntime>[2]
	> = async (options) => {
		// Production builds fresh services per session; a fresh loader gives
		// the replacement its own bridge instance bound to its own runtime.
		const settingsB = sdk.SettingsManager.create(cwd, agentDir);
		const loaderB = new sdk.DefaultResourceLoader({
			cwd,
			agentDir,
			settingsManager: settingsB,
			eventBus: sdk.createEventBus(),
			additionalExtensionPaths: [shippedBridge],
			noSkills: true,
			noThemes: true,
			noPromptTemplates: true,
			noContextFiles: true,
		});
		await loaderB.reload();
		const replacement = new sdk.AgentSession({
			agent: buildAgent(),
			sessionManager: options.sessionManager,
			settingsManager: settingsB,
			cwd,
			modelRuntime,
			resourceLoader: loaderB,
			sessionStartEvent: options.sessionStartEvent,
		} as ConstructorParameters<typeof sdk.AgentSession>[0]);
		replacement.subscribe(() => undefined);
		await replacement.bindExtensions({
			abortHandler: bindAbortHandler(replacement),
		});
		return {
			session: replacement,
			extensionsResult: loaderB.getExtensions(),
			services: {
				cwd,
				agentDir,
				modelRuntime,
				settingsManager: settingsB,
				resourceLoader: loaderB,
				diagnostics: [],
			},
			diagnostics: [],
		};
	};
	const runtimeHost = new sdk.AgentSessionRuntime(
		session,
		{
			cwd,
			agentDir,
			modelRuntime,
			settingsManager,
			resourceLoader,
			diagnostics: [],
		},
		createRuntime,
	);
	const sessionBFile = sdk.SessionManager.create(
		cwd,
		sessionDirB,
	).getSessionFile();
	assert.ok(sessionBFile, "the replacement session must have a file");
	await runtimeHost.switchSession(sessionBFile);
	await waitUntil(
		() => Promise.resolve(host.helloCount === 2),
		"the replacement bridge hello",
	);
	const sessionB = runtimeHost.session;
	const sessionManagerB = sessionB.sessionManager;
	let sessionBSnapshot: JsonRecord | undefined;
	await waitUntil(async () => {
		sessionBSnapshot = await pollSnapshot(host);
		return typeof sessionBSnapshot!.native_session_id === "string";
	}, "the replacement snapshot");
	const sessionBId = sessionBSnapshot!.native_session_id as string;
	assert.notEqual(
		sessionBId,
		sessionAId,
		"the replacement must be a new session",
	);
	const wrongSessionReply = await interrupt(
		"op-wrong-session",
		"any-turn",
		sessionAId,
	);
	assert.equal(
		errorCode(wrongSessionReply),
		"wrong_session",
		"the disposed session id must be rejected before any run check",
	);
	assert.equal(abortCount, 3);

	plans.push({ kind: "hold" });
	const replacementSend = await host.request({
		method: "pi.control.send",
		params: {
			operation_id: "op-session-b",
			native_session_id: sessionBId,
			prompt: "Replacement turn",
		},
	});
	assert.ok(
		isRecord(replacementSend.result),
		`the replacement send must be admitted: ${JSON.stringify(replacementSend)}`,
	);
	const replacementEntry = await findSendEntry(sessionManagerB, "op-session-b");
	assert.ok(
		replacementEntry,
		"the replacement send must be durable in its own session",
	);
	let replacementSnapshot: JsonRecord | undefined;
	await waitUntil(async () => {
		replacementSnapshot = await pollSnapshot(host);
		return replacementSnapshot.native_turn_id === replacementEntry!.id;
	}, "the replacement run to report identity");
	const replacementInterrupt = await interrupt(
		"op-interrupt-b",
		replacementEntry!.id,
		sessionBId,
	);
	assert.ok(
		isRecord(replacementInterrupt.result),
		`the replacement interrupt must be accepted: ${JSON.stringify(replacementInterrupt)}`,
	);
	assert.equal((replacementInterrupt.result as JsonRecord).status, "accepted");
	assert.equal(abortCount, 4);

	await sessionB.dispose?.();
	await host.close();
	await rm(tempRoot, { recursive: true, force: true });

	console.log("pi active-run interrupt proof: ok");
	return 0;
}

const code = await main();
process.exit(code);
