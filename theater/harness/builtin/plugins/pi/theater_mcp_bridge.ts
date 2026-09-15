/**
 * Theater's bundled Pi extension.
 *
 * It is deliberately self-contained: Theater passes this file with --extension
 * on every Pi launch, then supplies the per-participant stdio command through
 * --theater-mcp-config. The user's optional general-purpose MCP extension can
 * coexist with it; the global owner marker keeps two copies from registering
 * the same Theater tools in one Pi process.
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createHash } from "node:crypto";
import {
	mkdirSync,
	renameSync,
	statSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { readFile } from "node:fs/promises";
import { createConnection, type Socket } from "node:net";
import { dirname, join, resolve } from "node:path";

import type {
	ExtensionAPI,
	ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const OWNER = Symbol.for("theater.pi.mcp-bridge.owner");
const STARTUP_TIMEOUT_MS = 10_000;
const MAX_FRAME_CHARS = 1024 * 1024;
const CORE_MCP_SERVERS = new Set(["theater", "theater_wait"]);
const IDLE_STATUS_KEY = "theater.pi.idle";
const IDLE_STATUS_TEXT = "Theater: idle";
const AWAITING_STATUS_KEY = "theater.pi.awaiting";
const AWAITING_STATUS_TEXT = "Theater: awaiting input";
const SWITCH_MARKER = ".theater-pi-switch.json";
const SWITCHES_DIR = ".theater-pi-switches";
const SWITCH_MARKER_VERSION = 1;
const FRONTEND_PROTOCOL = "theater-frontend-v1";
const FRONTEND_MAX_FRAME_BYTES = 1024 * 1024;
const FRONTEND_MAX_HISTORY = 64;
const FRONTEND_MAX_OPERATIONS = 64;
const FRONTEND_RECONNECT_MAX_MS = 5_000;
const FRONTEND_OWNER = Symbol.for("theater.pi.frontend-bridge.owner");
const FRONTEND_SEND_CUSTOM_TYPE = "theater:send";
const FRONTEND_SEND_POLL_MS = 25;
const FRONTEND_SEND_ADMISSION_MS = 5_000;
const FRONTEND_SEND_PROMPT_MAX_CHARS = 100_000;
const FRONTEND_SEND_RESULT_MAX_CHARS = 65_536;
// The interrupt evidence window must close before the Python control timeout.
const FRONTEND_INTERRUPT_SETTLE_MS = 5_000;
type FrontendSendTerminal = "completed" | "failed" | "interrupted";
interface FrontendSendTurn {
	readonly operationId: string;
	nativeTurnId: string | undefined;
	terminal:
		| {
				terminal: FrontendSendTerminal;
				errorCode: string | null;
				result: string | null;
		  }
		| undefined;
	stopReason: string | undefined;
	resultText: string | undefined;
}

type FrontendRunSource = "human" | "theater" | "unknown";

// The authoritative identity of the run executing now — kept strictly
// separate from FrontendSendTurn, which attributes Theater operations to
// durable entries whether or not that run is still executing.
interface FrontendActiveRun {
	readonly epoch: number;
	readonly baseline: number;
	readonly priorLeaf: string | null | undefined;
	entryId: string | undefined;
	source: FrontendRunSource;
	lastStopReason: string | undefined;
}

type FrontendInterruptOutcome =
	| { readonly kind: "settled"; readonly abortedStop: boolean }
	| { readonly kind: "drift" }
	| { readonly kind: "timeout" };

interface FrontendInterruptWait {
	readonly operationId: string;
	readonly epoch: number;
	readonly entryId: string;
	readonly signal: AbortSignal;
	readonly completion: Promise<FrontendReply>;
	resolveCompletion: ((reply: FrontendReply) => void) | undefined;
	timer: ReturnType<typeof setTimeout> | undefined;
}

// --- Durable lifecycle markers ------------------------------------------------
//
// Pi does NOT expose auto_retry_start to extensions (internal _emit only) and the
// extension agent_end event drops willRetry, so there is no extension-visible
// signal that reliably distinguishes a retried error from a final one.  We do NOT
// emit retry-scheduled: inferring it from a message_end error would misclassify
// final errors and strand the turn open.  The parser defers error/length/aborted
// terminals itself and releases exactly one turn_end on settled.
const LIFECYCLE_CUSTOM_TYPE = "theater:lifecycle";
const LIFECYCLE_VERSION = 1;
const LIFECYCLE_PHASE = {
	compactionWillRetry: "compaction-will-retry",
	settled: "settled",
} as const;
type LifecyclePhase = (typeof LIFECYCLE_PHASE)[keyof typeof LIFECYCLE_PHASE];
interface LifecycleExtras {
	readonly reason?: string;
}
function lifecycleData(
	phase: LifecyclePhase,
	extra: LifecycleExtras = {},
): {
	readonly version: number;
	readonly phase: LifecyclePhase;
	readonly reason?: string;
} {
	const data: { version: number; phase: LifecyclePhase; reason?: string } = {
		version: LIFECYCLE_VERSION,
		phase,
	};
	if (extra.reason !== undefined) data.reason = extra.reason;
	return data;
}

// Process-global guard so two extension copies (Theater's shipped copy plus a
// user-local copy) cannot double-register lifecycle handlers.  Distinct from
// the MCP lease owner (OWNER) and released on session_shutdown so a fresh
// extension runtime can register after the owner tears down.
const LIFECYCLE_REGISTERED = Symbol.for("theater.pi.lifecycle.registered");
function acquireLifecycleGuard(): boolean {
	const owners = globalThis as Record<symbol, boolean | undefined>;
	if (owners[LIFECYCLE_REGISTERED]) return false;
	owners[LIFECYCLE_REGISTERED] = true;
	return true;
}
function releaseLifecycleGuard(): void {
	const owners = globalThis as Record<symbol, boolean | undefined>;
	delete owners[LIFECYCLE_REGISTERED];
}

// Minimal structural view of the Pi extension API touched by the markers.
interface LifecycleExtensionApi {
	appendEntry(customType: string, data?: unknown): void;
	on(
		event: "session_before_compact",
		handler: (event: { willRetry: unknown }, ctx: unknown) => void,
	): void;
	on(
		event: "agent_settled",
		handler: (event: unknown, ctx: unknown) => void,
	): void;
	on(
		event: "session_shutdown",
		handler: (event: unknown, ctx: unknown) => void,
	): void;
}
function writeMarker(
	pi: LifecycleExtensionApi,
	phase: LifecyclePhase,
	extra?: LifecycleExtras,
): void {
	try {
		pi.appendEntry(LIFECYCLE_CUSTOM_TYPE, lifecycleData(phase, extra));
	} catch {
		// A session-store failure must not crash Pi or break the turn; the parser
		// tolerates a missing marker by leaving the existing terminal handling.
	}
}
function registerLifecycleMarkers(pi: LifecycleExtensionApi): void {
	if (!acquireLifecycleGuard()) return;
	// Overflow-recovery compaction retries the interrupted turn afterwards;
	// threshold/manual compaction does not.  Only the retry case hints the parser.
	pi.on("session_before_compact", (event) => {
		if (event.willRetry === true) {
			writeMarker(pi, LIFECYCLE_PHASE.compactionWillRetry, {
				reason: "overflow",
			});
		}
	});
	// agent_settled is the authoritative "turn is really done" signal that lets
	// the parser release any pending terminal candidate.
	pi.on("agent_settled", () => {
		writeMarker(pi, LIFECYCLE_PHASE.settled);
	});
	// Release the guard on shutdown so a fresh extension runtime can register.
	pi.on("session_shutdown", () => {
		releaseLifecycleGuard();
	});
}

// --- Actionable MCP error text ---
//
// Join the textual content a failing MCP tool returned instead of discarding
// it for a generic message; generic fallback only when the text is empty.
interface ToolResultContent {
	readonly type: string;
	readonly text?: string;
	[key: string]: unknown;
}
function joinErrorText(
	content: ToolResultContent[] | undefined,
	server: string,
	toolName: string,
): string {
	const parts: string[] = [];
	if (Array.isArray(content)) {
		for (const item of content) {
			if (typeof item?.text === "string" && item.text.trim())
				parts.push(item.text);
		}
	}
	if (parts.length > 0) return parts.join("\n");
	return `${server} MCP tool ${toolName} returned an error`;
}

function acquireBridge(): symbol | undefined {
	const owners = globalThis as Record<symbol, symbol | undefined>;
	if (owners[OWNER] !== undefined) return undefined;
	const lease = Symbol("theater.pi.mcp-bridge.lease");
	owners[OWNER] = lease;
	return lease;
}

function releaseBridge(lease: symbol): void {
	const owners = globalThis as Record<symbol, symbol | undefined>;
	if (owners[OWNER] === lease) delete owners[OWNER];
}

function clearIdleStatus(ctx: ExtensionContext): void {
	ctx.ui.setStatus(IDLE_STATUS_KEY, undefined);
}

function showIdleStatus(ctx: ExtensionContext): void {
	if (ctx.isIdle()) ctx.ui.setStatus(IDLE_STATUS_KEY, IDLE_STATUS_TEXT);
}

// Tools whose execution parks Pi on a human decision mid-turn: the
// question UI is up, the turn is open, and no spinner runs.  Permission
// prompts Pi renders itself have no extension event, so only tools count
// here; add tool names to this set when Theater layers on more.
const AWAITING_INPUT_TOOLS = new Set(["ask_user_question"]);
const awaitingToolCalls = new Map<string, string>();

// The frontend bridge pushes a fresh snapshot whenever the pending
// interaction changes, so the daemon learns about a question the moment
// it opens rather than at the next lifecycle event.
const interactionListeners = new Set<() => void>();

function notifyInteractionChanged(): void {
	for (const listener of interactionListeners) listener();
}

function pendingInteractionDescription(): {
	kind: "clarification";
	native_turn_id: string | null;
	details: string;
} | null {
	if (awaitingToolCalls.size === 0) return null;
	const names = [...awaitingToolCalls.values()].slice(0, 4).join(", ");
	return {
		kind: "clarification",
		native_turn_id: null,
		details: names.length > 200 ? names.slice(0, 200) : names,
	};
}

function clearAwaitingStatus(ctx: ExtensionContext): void {
	ctx.ui.setStatus(AWAITING_STATUS_KEY, undefined);
}

function showAwaitingStatus(ctx: ExtensionContext): void {
	ctx.ui.setStatus(AWAITING_STATUS_KEY, AWAITING_STATUS_TEXT);
}

function registerIdleStatus(pi: ExtensionAPI): void {
	// Pi's status lifecycle, rather than its static screen chrome, is the
	// authority for an idle reading.  `agent_settled` includes retries,
	// compaction/retry, and queued continuations.
	const clearStatuses = (ctx: ExtensionContext): void => {
		awaitingToolCalls.clear();
		clearIdleStatus(ctx);
		clearAwaitingStatus(ctx);
	};
	pi.on("session_start", (_event, ctx) => {
		awaitingToolCalls.clear();
		clearAwaitingStatus(ctx);
		showIdleStatus(ctx);
	});
	pi.on("before_agent_start", (_event, ctx) => clearStatuses(ctx));
	pi.on("agent_start", (_event, ctx) => clearStatuses(ctx));
	pi.on("agent_settled", (_event, ctx) => {
		awaitingToolCalls.clear();
		clearAwaitingStatus(ctx);
		showIdleStatus(ctx);
	});

	// A user-input tool call parks Pi on a human decision mid-turn.  The footer
	// is the display contract the screen classifier reads, and the frontend
	// bridge reports the same fact as a pending interaction so the daemon's
	// live channel can carry it.  Both clear on the matching tool_result (the
	// human answered, or cancelled) and on every turn boundary above, so a
	// question abandoned any other way cannot leave a stale marker behind.
	pi.on("tool_call", (event, ctx) => {
		if (!AWAITING_INPUT_TOOLS.has(event.toolName)) return;
		awaitingToolCalls.set(event.toolCallId, event.toolName);
		showAwaitingStatus(ctx);
		notifyInteractionChanged();
	});
	pi.on("tool_result", (event, ctx) => {
		if (!awaitingToolCalls.delete(event.toolCallId)) return;
		if (awaitingToolCalls.size === 0) clearAwaitingStatus(ctx);
		notifyInteractionChanged();
	});

	// These operations can run without an agent lifecycle event.  Clear first
	// so a custom or hidden working indicator cannot leave an old idle marker
	// on the screen, then restore only when Pi itself reports it is idle.
	pi.on("session_before_compact", (_event, ctx) => clearStatuses(ctx));
	pi.on("session_compact", (_event, ctx) => {
		awaitingToolCalls.clear();
		clearAwaitingStatus(ctx);
		showIdleStatus(ctx);
	});
	pi.on("session_before_tree", (_event, ctx) => clearStatuses(ctx));
	pi.on("session_tree", (_event, ctx) => {
		awaitingToolCalls.clear();
		clearAwaitingStatus(ctx);
		showIdleStatus(ctx);
	});
	pi.on("session_shutdown", (_event, ctx) => clearStatuses(ctx));
}

function removeSwitchMarker(ctx: ExtensionContext): void {
	const marker = join(
		resolve(ctx.sessionManager.getSessionDir()),
		SWITCH_MARKER,
	);
	try {
		unlinkSync(marker);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
	}
}

function forkFlag(argv: string[]): string | undefined {
	for (let index = 0; index < argv.length; index += 1) {
		const value = argv[index];
		if (value.startsWith("--fork=")) return value.slice("--fork=".length);
		if (value === "--fork") return argv[index + 1];
	}
	return undefined;
}

function writeSwitchDocument(
	root: string,
	location: string,
	document: Record<string, unknown>,
): void {
	const body = `${JSON.stringify(document)}\n`;
	const marker = join(root, SWITCH_MARKER);
	const directory = join(root, SWITCHES_DIR);
	mkdirSync(directory, { recursive: true, mode: 0o700 });
	const digest = createHash("sha256").update(location).digest("hex");
	for (const path of [join(directory, `${digest}.json`), marker]) {
		const temporary = `${path}.${process.pid}.tmp`;
		writeFileSync(temporary, body, { encoding: "utf8", mode: 0o600 });
		renameSync(temporary, path);
	}
}

function writeStartupForkMarker(source: string, ctx: ExtensionContext): void {
	const target = ctx.sessionManager.getSessionFile();
	if (!target) return;
	const root = resolve(ctx.sessionManager.getSessionDir());
	const location = resolve(target);
	if (dirname(location) !== root) return;
	const stat = statSync(location);
	if (!stat.isFile()) return;
	writeSwitchDocument(root, location, {
		version: SWITCH_MARKER_VERSION,
		reason: "startup-fork",
		location,
		previous_location: resolve(source),
		offset: stat.size,
		dev: stat.dev,
		ino: stat.ino,
	});
}

function writeSwitchMarker(
	reason: "new" | "resume" | "fork",
	previousLocation: string | undefined,
	targetLocation: string | undefined,
	ctx: ExtensionContext,
	records?: number,
): void {
	if (!previousLocation || !targetLocation) return;
	const root = resolve(ctx.sessionManager.getSessionDir());
	const location = resolve(targetLocation);
	const previous = resolve(previousLocation);
	if (
		dirname(location) !== root ||
		dirname(previous) !== root ||
		location === previous
	)
		return;

	let offset: number | undefined;
	let dev: number | undefined;
	let ino: number | undefined;
	if (reason === "new") {
		offset = 0;
		records = 0;
	} else {
		try {
			const stat = statSync(location);
			if (!stat.isFile()) return;
			offset = stat.size;
			records = undefined;
			dev = stat.dev;
			ino = stat.ino;
		} catch (error) {
			if (
				(error as NodeJS.ErrnoException).code !== "ENOENT" ||
				records === undefined
			)
				throw error;
		}
	}
	if (offset === undefined && records === undefined) return;

	writeSwitchDocument(root, location, {
		version: SWITCH_MARKER_VERSION,
		reason,
		location,
		previous_location: previous,
		offset,
		records,
		dev,
		ino,
	});
}

function registerTranscriptSwitches(pi: ExtensionAPI): void {
	const startupFork = forkFlag(process.argv);
	pi.on("session_start", (event, ctx) => {
		if (event.reason === "startup") {
			if (startupFork) writeStartupForkMarker(startupFork, ctx);
			else removeSwitchMarker(ctx);
			return;
		}
		if (event.reason === "reload") return;
		const target = ctx.sessionManager.getSessionFile();
		const records =
			event.reason === "new" ? 0 : ctx.sessionManager.getEntries().length + 1;
		writeSwitchMarker(
			event.reason,
			event.previousSessionFile,
			target,
			ctx,
			records,
		);
	});
	pi.on("session_shutdown", (event, ctx) => {
		if (
			event.reason === "new" ||
			event.reason === "resume" ||
			event.reason === "fork"
		) {
			writeSwitchMarker(
				event.reason,
				ctx.sessionManager.getSessionFile(),
				event.targetSessionFile,
				ctx,
			);
		}
	});
}

interface ServerConfig {
	name: string;
	command: string;
	args: string[];
	env?: Record<string, string>;
}

interface Tool {
	name: string;
	description?: string;
	inputSchema: Record<string, unknown>;
}

interface ToolResult {
	content?: Array<{ type: string; text?: string; [key: string]: unknown }>;
	isError?: boolean;
}

interface Response {
	id?: number;
	result?: unknown;
	error?: { code: number; message: string };
}

interface Pending {
	resolve(value: unknown): void;
	reject(error: Error): void;
	timeout: ReturnType<typeof setTimeout> | undefined;
	removeAbort: (() => void) | undefined;
}

function record(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function configFlag(argv: string[]): string | undefined {
	for (let index = 0; index < argv.length; index += 1) {
		const value = argv[index];
		if (value.startsWith("--theater-mcp-config=")) {
			return value.slice("--theater-mcp-config=".length);
		}
		if (value === "--theater-mcp-config") return argv[index + 1];
	}
	return undefined;
}

async function loadConfig(path: string): Promise<ServerConfig[]> {
	let document: unknown;
	try {
		document = JSON.parse(await readFile(path, "utf8"));
	} catch (error) {
		throw new Error(
			`cannot read Theater MCP config ${path}: ${(error as Error).message}`,
		);
	}
	if (!record(document) || !record(document.mcpServers)) {
		throw new Error(
			`Theater MCP config ${path} must define an mcpServers object`,
		);
	}
	const servers = Object.entries(document.mcpServers);
	if (servers.length === 0) {
		throw new Error(
			`Theater MCP config ${path} must define at least one MCP server`,
		);
	}
	return servers.map(([name, value]) => loadServerConfig(path, name, value));
}

function loadServerConfig(
	path: string,
	name: string,
	value: unknown,
): ServerConfig {
	if (!name.trim() || !record(value)) {
		throw new Error(`Theater MCP config ${path} has an invalid server entry`);
	}
	if (typeof value.command !== "string" || !value.command.trim()) {
		throw new Error(`Theater MCP config ${path} has no executable for ${name}`);
	}
	if (
		value.args !== undefined &&
		(!Array.isArray(value.args) ||
			!value.args.every((arg) => typeof arg === "string"))
	) {
		throw new Error(
			`Theater MCP config ${path} has invalid arguments for ${name}`,
		);
	}
	if (
		value.env !== undefined &&
		(!record(value.env) ||
			!Object.values(value.env).every((entry) => typeof entry === "string"))
	) {
		throw new Error(
			`Theater MCP config ${path} has invalid environment for ${name}`,
		);
	}
	return {
		name,
		command: value.command,
		args: (value.args as string[] | undefined) ?? [],
		env: value.env as Record<string, string> | undefined,
	};
}

function tools(result: unknown, server: string): Tool[] {
	if (!record(result) || !Array.isArray(result.tools)) {
		throw new Error(`${server} MCP tools/list returned no tools array`);
	}
	return result.tools.map((value) => {
		if (
			!record(value) ||
			typeof value.name !== "string" ||
			!value.name ||
			!record(value.inputSchema)
		) {
			throw new Error(
				`${server} MCP tools/list returned an invalid tool definition`,
			);
		}
		return {
			name: value.name,
			description:
				typeof value.description === "string" ? value.description : undefined,
			inputSchema: value.inputSchema,
		};
	});
}

class McpClient {
	private child: ChildProcessWithoutNullStreams | undefined;
	private nextId = 1;
	private pending = new Map<number, Pending>();
	private buffer = "";
	private closed = false;

	constructor(private readonly config: ServerConfig) {}

	async initialize(): Promise<void> {
		this.start();
		await this.request(
			"initialize",
			{
				protocolVersion: "2025-06-18",
				capabilities: {},
				clientInfo: { name: "theater-pi-bridge", version: "1.0.0" },
			},
			undefined,
			STARTUP_TIMEOUT_MS,
		);
		this.notify("notifications/initialized");
	}

	async listTools(): Promise<Tool[]> {
		return tools(
			await this.request(
				"tools/list",
				undefined,
				undefined,
				STARTUP_TIMEOUT_MS,
			),
			this.config.name,
		);
	}

	async callTool(
		name: string,
		arguments_: unknown,
		signal?: AbortSignal,
	): Promise<ToolResult> {
		return (await this.request(
			"tools/call",
			{ name, arguments: arguments_ },
			signal,
		)) as ToolResult;
	}

	async close(): Promise<void> {
		if (this.closed) return;
		this.closed = true;
		this.fail(new Error(`${this.config.name} MCP bridge closed`));
		this.child?.kill();
		this.child = undefined;
	}

	private start(): void {
		if (this.child) return;
		const child = spawn(this.config.command, this.config.args, {
			stdio: "pipe",
			env: { ...process.env, ...this.config.env },
		});
		this.child = child;
		child.stdout.setEncoding("utf8");
		child.stdout.on("data", (chunk: string) => this.consume(chunk));
		child.stderr.resume();
		child.stdin.on("error", () =>
			this.fail(new Error(`${this.config.name} MCP process is not available`)),
		);
		child.on("error", (error) =>
			this.fail(
				new Error(`${this.config.name} MCP process failed: ${error.message}`),
			),
		);
		child.on("exit", (code, signal) => {
			if (!this.closed)
				this.fail(
					new Error(
						`${this.config.name} MCP process exited (${code ?? signal ?? "unknown"})`,
					),
				);
		});
	}

	private consume(chunk: string): void {
		this.buffer += chunk;
		if (this.buffer.length > MAX_FRAME_CHARS && !this.buffer.includes("\n")) {
			this.fail(
				new Error(
					`${this.config.name} MCP emitted an oversized JSON-RPC frame`,
				),
			);
			return;
		}
		for (;;) {
			const newline = this.buffer.indexOf("\n");
			if (newline < 0) return;
			const line = this.buffer.slice(0, newline);
			this.buffer = this.buffer.slice(newline + 1);
			if (!line.trim()) continue;
			if (line.length > MAX_FRAME_CHARS) {
				this.fail(
					new Error(
						`${this.config.name} MCP emitted an oversized JSON-RPC frame`,
					),
				);
				return;
			}
			let response: Response;
			try {
				response = JSON.parse(line) as Response;
			} catch {
				this.fail(
					new Error(`${this.config.name} MCP emitted malformed JSON-RPC`),
				);
				return;
			}
			if (typeof response.id !== "number") continue;
			const pending = this.finish(response.id);
			if (!pending) continue;
			if (response.error) {
				pending.reject(
					new Error(
						`JSON-RPC error ${response.error.code}: ${response.error.message}`,
					),
				);
			} else {
				pending.resolve(response.result);
			}
		}
	}

	private request(
		method: string,
		params?: unknown,
		signal?: AbortSignal,
		timeoutMs?: number,
	): Promise<unknown> {
		const id = this.nextId++;
		return new Promise<unknown>((resolve, reject) => {
			if (signal?.aborted) {
				reject(new Error(`${this.config.name} MCP request cancelled`));
				return;
			}
			const abort = () => {
				try {
					this.notify("notifications/cancelled", {
						requestId: id,
						reason: "Pi tool invocation cancelled",
					});
				} catch {
					// The pending request still receives its local cancellation below.
				}
				this.finish(id)?.reject(
					new Error(`${this.config.name} MCP request cancelled`),
				);
			};
			const pending: Pending = {
				resolve,
				reject,
				timeout: undefined,
				removeAbort: undefined,
			};
			if (signal) {
				signal.addEventListener("abort", abort, { once: true });
				pending.removeAbort = () => signal.removeEventListener("abort", abort);
			}
			if (timeoutMs) {
				pending.timeout = setTimeout(() => {
					this.finish(id)?.reject(
						new Error(`${this.config.name} MCP ${method} timed out`),
					);
				}, timeoutMs);
			}
			this.pending.set(id, pending);
			try {
				this.send({ jsonrpc: "2.0", id, method, params });
			} catch (error) {
				this.finish(id)?.reject(
					error instanceof Error ? error : new Error(String(error)),
				);
			}
		});
	}

	private send(message: Record<string, unknown>): void {
		if (this.closed || !this.child?.stdin.writable)
			throw new Error(`${this.config.name} MCP process is not available`);
		this.child.stdin.write(`${JSON.stringify(message)}\n`);
	}

	private notify(method: string, params?: unknown): void {
		this.send({ jsonrpc: "2.0", method, params });
	}

	private finish(id: number): Pending | undefined {
		const pending = this.pending.get(id);
		if (!pending) return undefined;
		this.pending.delete(id);
		if (pending.timeout) clearTimeout(pending.timeout);
		pending.removeAbort?.();
		return pending;
	}

	private fail(error: Error): void {
		for (const id of [...this.pending.keys()]) this.finish(id)?.reject(error);
	}
}

function toolName(server: string, name: string): string {
	return `${server.replace(/[^a-zA-Z0-9_-]/g, "_")}__${name.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}

async function registerServerTools(
	pi: ExtensionAPI,
	server: ServerConfig,
	client: McpClient,
	registered: Set<string>,
): Promise<void> {
	const discovered = await client.listTools();
	const names = discovered.map((tool) => ({
		tool,
		name: toolName(server.name, tool.name),
	}));
	const localNames = new Set<string>();
	for (const { name } of names) {
		if (registered.has(name) || localNames.has(name)) {
			throw new Error(`MCP tools collide after Pi name normalization: ${name}`);
		}
		localNames.add(name);
	}
	for (const { tool, name } of names) {
		pi.registerTool({
			name,
			label: `${server.name}/${tool.name}`,
			description: tool.description ?? `${server.name} MCP tool ${tool.name}`,
			promptSnippet: `${server.name}: ${tool.description ?? tool.name}`,
			parameters: Type.Unsafe(tool.inputSchema),
			async execute(_id, params, signal) {
				if (signal?.aborted)
					return {
						content: [{ type: "text", text: "Cancelled" }],
						details: {},
					};
				try {
					const result = await client.callTool(tool.name, params, signal);
					const content = (result.content ?? []).map((item) => ({
						type: "text" as const,
						text: item.text ?? JSON.stringify(item),
					}));
					if (result.isError) {
						throw new Error(
							joinErrorText(result.content, server.name, tool.name),
						);
					}
					return { content, details: { server: server.name, tool: tool.name } };
				} catch (error) {
					if (signal?.aborted)
						return {
							content: [{ type: "text", text: "Cancelled" }],
							details: {},
						};
					throw error;
				}
			},
		});
	}
	for (const { name } of names) registered.add(name);
}

// --- Authenticated frontend bridge ------------------------------------------
//
// This is intentionally separate from the MCP bridge above.  MCP remains
// outbound-only and cannot give Theater a live native-session control path.
// The daemon-owned frontend host accepts this extension's authenticated raw
// NDJSON connection, and supplies the request/notification peer consumed by
// Pi's Python-side runtime.  A host disconnect never affects Pi's TUI or its
// work: this client drops live notifications, reconnects with one bounded
// timer, and never queues or replays a mutation.

interface FrontendConfig {
	readonly protocol: string;
	readonly participant_id: string;
	readonly endpoint: string;
	readonly token: string;
}

type FrontendEndpoint =
	{ readonly host: string; readonly port: number } | { readonly path: string };

type FrontendExecutionState = "unknown" | "idle" | "active";
type FrontendThinkingLevel =
	"off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";

interface FrontendSnapshot {
	readonly protocol: string;
	readonly native_session_id: string;
	readonly bridge_epoch: number;
	readonly snapshot_revision: number;
	readonly sequence: number;
	readonly settings: {
		readonly model: string | null;
		readonly reasoning_effort: string | null;
	};
	readonly execution_state: FrontendExecutionState;
	readonly native_turn_id: string | null;
	readonly pending_interaction: {
		readonly kind: "clarification";
		readonly native_turn_id: string | null;
		readonly details: string;
	} | null;
	readonly capabilities: {
		readonly send: boolean;
		readonly settings_update: boolean;
		readonly model_update: false;
		readonly reasoning_effort_update: true;
		readonly interrupt: boolean;
	};
}

interface FrontendEvent {
	readonly name: string;
	readonly native_session_id: string;
	readonly bridge_epoch: number;
	readonly execution_state: FrontendExecutionState;
	readonly sequence: number;
	readonly operation_id?: string;
	readonly native_turn_id?: string;
	readonly terminal?: FrontendSendTerminal;
	readonly error_code?: string | null;
	readonly result?: string | null;
}

interface FrontendSession {
	// ExtensionRunner deliberately creates a fresh context for every emit().
	// Retain the latest same-session context for snapshots, but never use object
	// identity as a lifecycle correlation key.
	ctx: ExtensionContext;
	readonly nativeSessionId: string;
	readonly epoch: number;
	executionState: FrontendExecutionState;
}

interface FrontendReply {
	readonly result?: Record<string, unknown>;
	readonly error?: { readonly code: string; readonly message: string };
}

const FRONTEND_THINKING_LEVELS: readonly FrontendThinkingLevel[] = [
	"off",
	"minimal",
	"low",
	"medium",
	"high",
	"xhigh",
	"max",
];

function acquireFrontendBridge(): symbol | undefined {
	const owners = globalThis as Record<symbol, symbol | undefined>;
	if (owners[FRONTEND_OWNER] !== undefined) return undefined;
	const lease = Symbol("theater.pi.frontend-bridge.lease");
	owners[FRONTEND_OWNER] = lease;
	return lease;
}

function releaseFrontendBridge(lease: symbol): void {
	const owners = globalThis as Record<symbol, symbol | undefined>;
	if (owners[FRONTEND_OWNER] === lease) delete owners[FRONTEND_OWNER];
}

function frontendConfigFlag(argv: string[]): string | undefined {
	for (let index = 0; index < argv.length; index += 1) {
		const value = argv[index];
		if (value.startsWith("--theater-frontend-config=")) {
			return value.slice("--theater-frontend-config=".length);
		}
		if (value === "--theater-frontend-config") return argv[index + 1];
	}
	return undefined;
}

function boundedFrontendEpoch(value: unknown): number | undefined {
	if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1)
		return undefined;
	return value;
}

function boundedFrontendString(value: unknown): string | undefined {
	if (typeof value !== "string" || !value.trim() || value.length > 512)
		return undefined;
	return value;
}

function loopbackEndpoint(value: string): FrontendEndpoint | undefined {
	let parsed: URL;
	try {
		parsed = new URL(value);
	} catch {
		return undefined;
	}
	if (
		parsed.protocol === "unix:" &&
		!parsed.host &&
		parsed.pathname.startsWith("/") &&
		!parsed.search &&
		!parsed.hash &&
		!parsed.username &&
		!parsed.password
	) {
		try {
			return { path: decodeURIComponent(parsed.pathname) };
		} catch {
			return undefined;
		}
	}
	const port = Number(parsed.port);
	if (
		parsed.protocol !== "tcp:" ||
		parsed.hostname !== "127.0.0.1" ||
		!Number.isInteger(port) ||
		port < 1 ||
		port > 65_535 ||
		(parsed.pathname !== "" && parsed.pathname !== "/") ||
		parsed.search ||
		parsed.hash ||
		parsed.username ||
		parsed.password
	) {
		return undefined;
	}
	return { host: "127.0.0.1", port };
}

async function loadFrontendConfig(
	path: string,
): Promise<FrontendConfig | undefined> {
	let document: unknown;
	try {
		document = JSON.parse(await readFile(path, "utf8"));
	} catch {
		return undefined;
	}
	if (!record(document)) return undefined;
	const protocol = boundedFrontendString(document.protocol);
	const participantId = boundedFrontendString(document.participant_id);
	const endpoint = boundedFrontendString(document.endpoint);
	let token = boundedFrontendString(document.token);
	if (document.token_file !== undefined) {
		const tokenPath = boundedFrontendString(document.token_file);
		if (token !== undefined || tokenPath === undefined) return undefined;
		try {
			if (statSync(tokenPath).size > 1024) return undefined;
			token = boundedFrontendString((await readFile(tokenPath, "utf8")).trim());
		} catch {
			return undefined;
		}
	}
	if (
		protocol !== FRONTEND_PROTOCOL ||
		participantId === undefined ||
		endpoint === undefined ||
		token === undefined ||
		token.length < 16 ||
		loopbackEndpoint(endpoint) === undefined
	) {
		return undefined;
	}
	return { protocol, participant_id: participantId, endpoint, token };
}

function modelReference(
	model: { provider: unknown; id: unknown } | undefined,
): string | null {
	if (
		!model ||
		typeof model.provider !== "string" ||
		!model.provider ||
		typeof model.id !== "string" ||
		!model.id
	) {
		return null;
	}
	const value = `${model.provider}/${model.id}`;
	return value.length <= 512 ? value : null;
}

function sessionIdOf(ctx: ExtensionContext): string | undefined {
	try {
		return boundedFrontendString(ctx.sessionManager.getSessionId());
	} catch {
		return undefined;
	}
}

function stateOf(ctx: ExtensionContext): FrontendExecutionState {
	try {
		return ctx.isIdle() ? "idle" : "active";
	} catch {
		return "unknown";
	}
}

function availableThinkingLevel(
	model: {
		readonly reasoning: boolean;
		readonly thinkingLevelMap?: Partial<
			Record<FrontendThinkingLevel, string | null>
		>;
	},
	level: FrontendThinkingLevel,
): boolean {
	if (!model.reasoning) return level === "off";
	const mapped = model.thinkingLevelMap?.[level];
	if (mapped === null) return false;
	return level !== "xhigh" && level !== "max" ? true : mapped !== undefined;
}

class FrontendBridge {
	private socket: Socket | undefined;
	private config: FrontendConfig | undefined;
	private current: FrontendSession | undefined;
	private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
	private reconnectAttempt = 0;
	private buffer = "";
	private epoch = 0;
	private sequence = 0;
	private snapshotRevision = 0;
	private history: FrontendEvent[] = [];
	private settingsTail: Promise<void> = Promise.resolve();
	private operations = new Map<string, FrontendReply | undefined>();
	private sendTurn: FrontendSendTurn | undefined;
	private activeRun: FrontendActiveRun | undefined;
	private interruptWaiters = new Map<string, FrontendInterruptWait>();
	private disposed = false;

	constructor(
		private readonly pi: ExtensionAPI,
		private readonly configPath: string,
	) {}

	start(ctx: ExtensionContext): void {
		const nativeSessionId = sessionIdOf(ctx);
		if (nativeSessionId === undefined) return;
		this.dispose();
		this.disposed = false;
		const epoch = ++this.epoch;
		this.current = {
			ctx,
			nativeSessionId,
			epoch,
			executionState: stateOf(ctx),
		};
		this.sequence = 0;
		this.snapshotRevision = 0;
		this.history = [];
		this.operations.clear();
		this.sendTurn = undefined;
		this.record("session_start");
		void this.connectFromConfig(epoch);
	}

	dispose(): void {
		const current = this.current;
		if (current !== undefined) this.record("session_shutdown");
		this.disposed = true;
		this.epoch += 1;
		this.current = undefined;
		this.config = undefined;
		this.buffer = "";
		this.operations.clear();
		this.sendTurn = undefined;
		this.activeRun = undefined;
		// Session replacement invalidates pending evidence: the abort may have
		// applied, so waiters resolve UNKNOWN and never re-run against the new run.
		this.finishInterruptWaiters({ kind: "drift" });
		this.settingsTail = Promise.resolve();
		if (this.reconnectTimer !== undefined) clearTimeout(this.reconnectTimer);
		this.reconnectTimer = undefined;
		const socket = this.socket;
		this.socket = undefined;
		if (socket !== undefined) socket.destroy();
	}

	transition(
		ctx: ExtensionContext,
		name: string,
		state?: FrontendExecutionState,
	): void {
		const current = this.current;
		// Pi's public runner creates a new ExtensionContext for every event.  The
		// bridge generation plus the live native session ID are the durable
		// correlation pair: an old event can neither cross a session switch nor
		// revive a disposed bridge, while ordinary fresh contexts are accepted.
		if (
			current === undefined ||
			this.disposed ||
			current.epoch !== this.epoch ||
			sessionIdOf(ctx) !== current.nativeSessionId
		) {
			return;
		}
		current.ctx = ctx;
		if (!this.isCurrent(current)) return;
		if (state !== undefined) current.executionState = state;
		this.record(name);
	}

	// --- Active-run identity -------------------------------------------------
	//
	// The run's first durable entry is its stable public identity: Pi appends
	// it inside the same synchronous unwind that started the run, so a baseline
	// entry count captured at the start boundary names it without event ordering
	// or timing.  An unestablished identity publishes "unknown", never "active".

	beginRun(ctx: ExtensionContext): void {
		const current = this.current;
		if (
			current === undefined ||
			this.disposed ||
			sessionIdOf(ctx) !== current.nativeSessionId
		)
			return;
		// One run owns the slot at a time: settle is always processed before a
		// replacement run can start, and inner retries/compactions are the same
		// run, so a repeated start boundary keeps the established identity.
		if (this.activeRun !== undefined) return;
		let baseline: number;
		let priorLeaf: string | null | undefined;
		try {
			baseline = ctx.sessionManager.getEntries().length;
			priorLeaf = ctx.sessionManager.getLeafId();
		} catch {
			return;
		}
		this.activeRun = {
			epoch: current.epoch,
			baseline,
			priorLeaf,
			entryId: undefined,
			source: "unknown",
			lastStopReason: undefined,
		};
		if (this.establishRunIdentity()) return;
		// Stock persistence is a microtask cascade; the macrotask boundary is
		// guaranteed to observe the trigger entry if one is coming.
		setImmediate(() => {
			this.establishRunIdentity();
		});
	}

	private establishRunIdentity(): boolean {
		const run = this.activeRun;
		const current = this.current;
		if (run === undefined || run.entryId !== undefined) return false;
		if (
			current === undefined ||
			!this.isCurrent(current) ||
			run.epoch !== current.epoch
		)
			return false;
		let entryId: string | undefined;
		let source: FrontendRunSource = "unknown";
		try {
			const entries = current.ctx.sessionManager.getEntries();
			const entry = entries[run.baseline] as
				| {
						id?: unknown;
						parentId?: unknown;
						type?: unknown;
						customType?: unknown;
						message?: unknown;
				  }
				| undefined;
			if (
				record(entry) &&
				typeof entry.id === "string" &&
				entry.id &&
				(run.priorLeaf === undefined || entry.parentId === run.priorLeaf)
			) {
				if (
					entry.type === "custom_message" &&
					entry.customType === FRONTEND_SEND_CUSTOM_TYPE
				) {
					entryId = entry.id;
					source = "theater";
				} else if (
					entry.type === "message" &&
					record(entry.message) &&
					entry.message.role === "user"
				) {
					entryId = entry.id;
					source = "human";
				}
			}
		} catch {
			return false;
		}
		if (entryId === undefined) return false;
		run.entryId = entryId;
		run.source = source;
		this.publishSnapshot();
		return true;
	}

	// Settle is the terminal boundary for interrupt evidence and the only
	// place the active-run slot is released.
	settleRun(ctx: ExtensionContext): void {
		const current = this.current;
		const run = this.activeRun;
		if (
			current === undefined ||
			run === undefined ||
			sessionIdOf(ctx) !== current.nativeSessionId ||
			run.epoch !== current.epoch
		)
			return;
		this.activeRun = undefined;
		const abortedStop = run.lastStopReason === "aborted";
		for (const wait of [...this.interruptWaiters.values()]) {
			if (wait.epoch !== current.epoch) continue;
			this.finishInterruptWait(wait, { kind: "settled", abortedStop });
		}
	}

	private async connectFromConfig(epoch: number): Promise<void> {
		const config = await loadFrontendConfig(this.configPath);
		if (config === undefined || this.disposed || this.current?.epoch !== epoch)
			return;
		this.config = config;
		this.connect(epoch);
	}

	private connect(epoch: number): void {
		const config = this.config;
		if (config === undefined || this.disposed || this.current?.epoch !== epoch)
			return;
		const endpoint = loopbackEndpoint(config.endpoint);
		if (endpoint === undefined) return;
		const socket = createConnection(endpoint);
		this.socket = socket;
		socket.setEncoding("utf8");
		socket.on("data", (chunk: string) => this.consume(socket, chunk));
		// The close handler owns recovery.  An error listener is still required
		// so a refused local host never becomes an unhandled process error.
		socket.on("error", () => undefined);
		socket.once("connect", () => {
			if (!this.ownsSocket(socket, epoch)) {
				socket.destroy();
				return;
			}
			this.writeFrame(socket, {
				type: "hello",
				protocol: FRONTEND_PROTOCOL,
				participant_id: config.participant_id,
				token: config.token,
			});
			this.publishSnapshot(socket);
			const snapshot = this.snapshot();
			if (snapshot !== undefined) {
				this.writeFrame(socket, {
					type: "history",
					events: this.history,
					snapshot,
				});
			}
		});
		socket.once("close", () => {
			if (this.socket !== socket) return;
			this.socket = undefined;
			this.buffer = "";
			this.scheduleReconnect(epoch);
		});
	}

	private scheduleReconnect(epoch: number): void {
		if (
			this.disposed ||
			this.config === undefined ||
			this.current?.epoch !== epoch ||
			this.reconnectTimer !== undefined
		) {
			return;
		}
		const delay = Math.min(
			100 * 2 ** this.reconnectAttempt,
			FRONTEND_RECONNECT_MAX_MS,
		);
		this.reconnectAttempt += 1;
		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = undefined;
			this.connect(epoch);
		}, delay);
		this.reconnectTimer.unref();
	}

	private ownsSocket(socket: Socket, epoch: number): boolean {
		return (
			!this.disposed && this.socket === socket && this.current?.epoch === epoch
		);
	}

	private consume(socket: Socket, chunk: string): void {
		if (socket !== this.socket) return;
		this.buffer += chunk;
		if (
			Buffer.byteLength(this.buffer) > FRONTEND_MAX_FRAME_BYTES &&
			!this.buffer.includes("\n")
		) {
			socket.destroy();
			return;
		}
		for (;;) {
			const newline = this.buffer.indexOf("\n");
			if (newline < 0) return;
			const line = this.buffer.slice(0, newline);
			this.buffer = this.buffer.slice(newline + 1);
			if (!line.trim()) continue;
			if (Buffer.byteLength(line) > FRONTEND_MAX_FRAME_BYTES) {
				socket.destroy();
				return;
			}
			let frame: unknown;
			try {
				frame = JSON.parse(line);
			} catch {
				socket.destroy();
				return;
			}
			this.handleFrame(socket, frame);
		}
	}

	private handleFrame(socket: Socket, frame: unknown): void {
		if (!record(frame) || frame.type !== "request") return;
		const id = boundedFrontendString(frame.id);
		const method = boundedFrontendString(frame.method);
		const params = record(frame.params) ? frame.params : undefined;
		if (id === undefined || method === undefined || params === undefined)
			return;
		// A host only reaches this point after it accepted our authenticated
		// hello.  Reset backoff here, rather than on TCP connect, so a local
		// listener that immediately rejects a bad descriptor cannot make Pi hot
		// loop at the minimum reconnect delay.
		this.reconnectAttempt = 0;
		if (method === "pi.snapshot") {
			const snapshot = this.snapshot();
			if (snapshot === undefined)
				this.respond(socket, id, {
					error: { code: "not_ready", message: "Pi session is not ready" },
				});
			else this.respond(socket, id, { result: snapshot });
			return;
		}
		if (
			method !== "pi.settings.update" &&
			method !== "pi.control.send" &&
			method !== "pi.control.interrupt"
		) {
			this.respond(socket, id, {
				error: {
					code: "method_not_found",
					message: "unsupported Pi frontend method",
				},
			});
			return;
		}
		const operationId = boundedFrontendString(params.operation_id);
		if (operationId === undefined) {
			this.respond(socket, id, {
				error: {
					code: "invalid_request",
					message: `${method} requires operation_id`,
				},
			});
			return;
		}
		if (this.operations.has(operationId)) {
			const cached = this.operations.get(operationId);
			this.respond(
				socket,
				id,
				cached ?? {
					error: {
						code: "operation_in_progress",
						message: `${method} operation is still running`,
					},
				},
			);
			return;
		}
		if (!this.reserveOperation(operationId)) {
			this.respond(socket, id, {
				error: {
					code: "operation_capacity",
					message: "too many Pi frontend receipts are retained",
				},
			});
			return;
		}
		if (method === "pi.control.send") {
			const sendEpoch = this.current?.epoch;
			const scheduled = this.performSend(params, operationId, sendEpoch);
			void scheduled.then(
				(reply) => {
					if (this.current?.epoch === sendEpoch)
						this.operations.set(operationId, reply);
					this.respond(socket, id, reply);
				},
				() => {
					const reply: FrontendReply = {
						error: {
							code: "send_unconfirmed",
							message: "Pi send result was not confirmed",
						},
					};
					if (this.current?.epoch === sendEpoch)
						this.operations.set(operationId, reply);
					this.respond(socket, id, reply);
				},
			);
			return;
		}
		if (method === "pi.control.interrupt") {
			const interruptEpoch = this.current?.epoch;
			const outcome = this.performInterrupt(operationId, params);
			void Promise.resolve(outcome).then((reply) => {
				if (this.current?.epoch === interruptEpoch)
					this.operations.set(operationId, reply);
				this.respond(socket, id, reply);
			});
			return;
		}
		const epoch = this.current?.epoch;
		const scheduled = this.settingsTail.then(async () =>
			this.performSettings(params, operationId, epoch),
		);
		this.settingsTail = scheduled.then(
			() => undefined,
			() => undefined,
		);
		void scheduled.then(
			(reply) => {
				if (this.current?.epoch === epoch)
					this.operations.set(operationId, reply);
				this.respond(socket, id, reply);
			},
			() => {
				const reply: FrontendReply = {
					error: {
						code: "settings_unconfirmed",
						message: "Pi settings result was not confirmed",
					},
				};
				if (this.current?.epoch === epoch)
					this.operations.set(operationId, reply);
				this.respond(socket, id, reply);
			},
		);
	}

	// No await sits between the idle guard and delivery, so no human
	// prompt can interleave.
	private async performSend(
		params: Record<string, unknown>,
		operationId: string,
		epoch: number | undefined,
	): Promise<FrontendReply> {
		const current = this.current;
		if (
			current === undefined ||
			epoch === undefined ||
			current.epoch !== epoch ||
			!this.isCurrent(current)
		) {
			return {
				error: {
					code: "session_changed",
					message: "Pi session changed before send admission",
				},
			};
		}
		const requestedSession = boundedFrontendString(params.native_session_id);
		if (
			requestedSession === undefined ||
			requestedSession !== current.nativeSessionId
		) {
			return {
				error: {
					code: "wrong_session",
					message: "send target is not the current Pi session",
				},
			};
		}
		const prompt = params.prompt;
		if (typeof prompt !== "string" || !prompt.trim()) {
			return {
				error: {
					code: "invalid_request",
					message: "send requires a non-empty prompt",
				},
			};
		}
		if (prompt.length > FRONTEND_SEND_PROMPT_MAX_CHARS) {
			return {
				error: {
					code: "prompt_too_large",
					message: "send prompt exceeds the Pi bridge limit",
				},
			};
		}
		if (!current.ctx.isIdle() || current.ctx.hasPendingMessages()) {
			return {
				error: {
					code: "busy",
					message: "Pi send requires an idle session with no queued turn",
				},
			};
		}
		if (this.sendTurn !== undefined) {
			return {
				error: {
					code: "busy",
					message: "a Pi send turn is still being attributed",
				},
			};
		}
		const turn: FrontendSendTurn = {
			operationId,
			nativeTurnId: undefined,
			terminal: undefined,
			stopReason: undefined,
			resultText: undefined,
		};
		this.sendTurn = turn;
		try {
			// Void fire-and-forget: the run starts synchronously.
			this.pi.sendMessage(
				{
					customType: FRONTEND_SEND_CUSTOM_TYPE,
					content: [{ type: "text", text: prompt }],
					display: true,
					details: { operation_id: operationId },
				},
				{ triggerTurn: true },
			);
		} catch {
			if (this.sendTurn === turn) this.sendTurn = undefined;
			return {
				error: {
					code: "send_unconfirmed",
					message: "Pi send delivery was not accepted",
				},
			};
		}
		const entryId = await this.pollSendEntry(current, operationId);
		if (entryId === undefined) {
			if (this.sendTurn === turn) this.sendTurn = undefined;
			return {
				error: {
					code: "send_unconfirmed",
					message: "Pi send was not confirmed on the durable session tree",
				},
			};
		}
		turn.nativeTurnId = entryId;
		if (!this.isCurrent(current)) {
			return {
				error: {
					code: "send_unconfirmed",
					message: "Pi session changed while the send turn was attributed",
				},
			};
		}
		this.emitSendTerminal(turn);
		const confirmed = this.record("send_accepted");
		if (confirmed === undefined || !this.isCurrent(current)) {
			return {
				error: {
					code: "session_changed",
					message: "Pi session changed before send readback",
				},
			};
		}
		// The durable entry id wins over the readback: a fast-settling turn
		// already cleared the active turn.
		return {
			result: {
				status: "accepted",
				operation_id: operationId,
				...confirmed,
				native_turn_id: entryId,
			},
		};
	}

	// The durable entry is the only turn identity — never event ordering.
	private async pollSendEntry(
		current: FrontendSession,
		operationId: string,
	): Promise<string | undefined> {
		const deadline = Date.now() + FRONTEND_SEND_ADMISSION_MS;
		for (;;) {
			if (!this.isCurrent(current)) return undefined;
			let entries: Array<{
				id: unknown;
				customType?: unknown;
				details?: unknown;
			}> = [];
			try {
				entries = current.ctx.sessionManager.getEntries();
			} catch {
				return undefined;
			}
			for (let index = entries.length - 1; index >= 0; index -= 1) {
				const entry = entries[index]!;
				if (entry.customType !== FRONTEND_SEND_CUSTOM_TYPE) continue;
				if (
					!record(entry.details) ||
					entry.details.operation_id !== operationId
				)
					continue;
				if (typeof entry.id === "string" && entry.id) return entry.id;
			}
			if (Date.now() >= deadline) return undefined;
			await new Promise<void>((resolve) => {
				const timer = setTimeout(resolve, FRONTEND_SEND_POLL_MS);
				timer.unref?.();
			});
		}
	}

	// Attribution accumulation only; a run steered by human input stays ours.
	observeMessageEnd(ctx: ExtensionContext, message: unknown): void {
		const run = this.activeRun;
		if (run !== undefined && record(message) && message.role === "assistant") {
			const runCurrent = this.current;
			if (
				runCurrent !== undefined &&
				sessionIdOf(ctx) === runCurrent.nativeSessionId &&
				run.epoch === runCurrent.epoch &&
				typeof message.stopReason === "string"
			)
				run.lastStopReason = message.stopReason;
		}
		const turn = this.sendTurn;
		if (turn === undefined) return;
		const current = this.current;
		if (current === undefined || sessionIdOf(ctx) !== current.nativeSessionId)
			return;
		if (!record(message) || message.role !== "assistant") return;
		if (typeof message.stopReason === "string")
			turn.stopReason = message.stopReason;
		let text: string | undefined;
		if (Array.isArray(message.content)) {
			const parts: string[] = [];
			for (const item of message.content) {
				if (
					record(item) &&
					item.type === "text" &&
					typeof item.text === "string"
				)
					parts.push(item.text);
			}
			text = parts.join("\n");
		}
		turn.resultText =
			text === undefined
				? turn.resultText
				: text.length > FRONTEND_SEND_RESULT_MAX_CHARS
					? text.slice(0, FRONTEND_SEND_RESULT_MAX_CHARS)
					: text;
	}

	// One settled boundary per send, stashed when settle beat attribution.
	settleSendTurn(ctx: ExtensionContext): void {
		const turn = this.sendTurn;
		if (turn === undefined) return;
		const current = this.current;
		if (current === undefined || sessionIdOf(ctx) !== current.nativeSessionId)
			return;
		this.sendTurn = undefined;
		const stop = turn.stopReason;
		let terminal: FrontendSendTerminal;
		let errorCode: string | null = null;
		if (stop === "aborted") terminal = "interrupted";
		else if (stop === "stop") terminal = "completed";
		else {
			terminal = "failed";
			errorCode =
				stop === "error"
					? "turn_failed"
					: stop === "length"
						? "length_exhausted"
						: stop === undefined
							? "no_assistant_message"
							: "unresolved_stop_reason";
		}
		turn.terminal = {
			terminal,
			errorCode,
			result: turn.resultText ?? null,
		};
		if (turn.nativeTurnId !== undefined && this.current === current)
			this.emitSendTerminal(turn);
	}

	private emitSendTerminal(turn: FrontendSendTurn): void {
		const terminal = turn.terminal;
		if (terminal === undefined || turn.nativeTurnId === undefined) return;
		const current = this.current;
		if (current === undefined) return;
		this.record("turn_terminal", {
			operation_id: turn.operationId,
			native_turn_id: turn.nativeTurnId,
			terminal: terminal.terminal,
			error_code: terminal.errorCode,
			result: terminal.result,
		});
	}

	// --- Native interrupt -----------------------------------------------------
	//
	// One synchronous admission section: every check below runs without an
	// await, so the identity compared is the identity aborted.  Failures that
	// are provably emitted before the single ctx.abort() call are honest
	// rejections; anything after the call — or that cannot be ordered against
	// it — resolves UNKNOWN and is never retried or replayed.

	performInterrupt(
		operationId: string,
		params: Record<string, unknown>,
	): FrontendReply | Promise<FrontendReply> {
		const current = this.current;
		if (current === undefined || !this.isCurrent(current)) {
			return {
				error: {
					code: "not_ready",
					message: "Pi session is not ready for an interrupt",
				},
			};
		}
		const requestedSession = boundedFrontendString(params.native_session_id);
		if (
			requestedSession === undefined ||
			requestedSession !== current.nativeSessionId
		) {
			return {
				error: {
					code: "wrong_session",
					message: "interrupt target is not the current Pi session",
				},
			};
		}
		const expectedEpoch = boundedFrontendEpoch(params.expected_bridge_epoch);
		if (expectedEpoch === undefined) {
			return {
				error: {
					code: "invalid_request",
					message: "interrupt requires the live bridge epoch",
				},
			};
		}
		if (expectedEpoch !== current.epoch) {
			return {
				error: {
					code: "stale_bridge",
					message: "the interrupt targets a superseded bridge generation",
				},
			};
		}
		const expectedTurn = boundedFrontendString(params.expected_native_turn_id);
		if (expectedTurn === undefined) {
			return {
				error: {
					code: "invalid_request",
					message: "interrupt requires the expected active turn id",
				},
			};
		}
		// A control request may establish identity on demand, exactly like a
		// snapshot: the id compared below is never a stale guess.
		this.establishRunIdentity();
		const run = this.activeRun;
		if (
			run === undefined ||
			run.epoch !== current.epoch ||
			run.entryId === undefined
		) {
			return {
				error: {
					code: "no_active_run",
					message: "Pi has no identified active run to interrupt",
				},
			};
		}
		let idle = false;
		try {
			idle = current.ctx.isIdle();
		} catch {
			return {
				error: {
					code: "not_ready",
					message: "Pi session state is unavailable",
				},
			};
		}
		if (idle) {
			return {
				error: {
					code: "no_active_run",
					message: "Pi is idle; the expected run already settled",
				},
			};
		}
		const signal = current.ctx.signal;
		if (signal === undefined || signal.aborted) {
			return {
				error: {
					code: "not_cancellable",
					message:
						"Pi's run is between cancellable inner runs (retry or compaction backoff)",
				},
			};
		}
		if (run.entryId !== expectedTurn) {
			return {
				error: {
					code: "stale_turn",
					message: "the expected turn is not the identified active run",
				},
			};
		}
		// Reserve the evidence record before the single mutation; nothing below
		// yields until ctx.abort() has returned.
		const wait: FrontendInterruptWait = {
			operationId,
			epoch: current.epoch,
			entryId: run.entryId,
			signal,
			completion: Promise.resolve({
				error: {
					code: "interrupt_unconfirmed",
					message: "Pi interrupt evidence was not awaited",
				},
			}),
			resolveCompletion: undefined,
			timer: undefined,
		};
		wait.completion = new Promise<FrontendReply>((resolve) => {
			wait.resolveCompletion = resolve;
		});
		this.interruptWaiters.set(operationId, wait);
		try {
			current.ctx.abort();
		} catch {
			// A throw from abort() cannot be ordered against Pi's own queue
			// restore, so it is treated as possibly applied: UNKNOWN.
			this.finishInterruptWait(wait, { kind: "drift" });
			return wait.completion;
		}
		// Possibly applied from here on: the reply is evidence-driven only.
		wait.timer = setTimeout(() => {
			this.finishInterruptWait(wait, { kind: "timeout" });
		}, FRONTEND_INTERRUPT_SETTLE_MS);
		wait.timer.unref();
		return wait.completion;
	}

	private finishInterruptWaiters(outcome: FrontendInterruptOutcome): void {
		for (const wait of [...this.interruptWaiters.values()]) {
			this.finishInterruptWait(wait, outcome);
		}
	}

	private finishInterruptWait(
		wait: FrontendInterruptWait,
		outcome: FrontendInterruptOutcome,
	): void {
		if (wait.timer !== undefined) {
			clearTimeout(wait.timer);
			wait.timer = undefined;
		}
		if (this.interruptWaiters.get(wait.operationId) === wait)
			this.interruptWaiters.delete(wait.operationId);
		wait.resolveCompletion?.(this.interruptReply(wait, outcome));
	}

	private interruptReply(
		wait: FrontendInterruptWait,
		outcome: FrontendInterruptOutcome,
	): FrontendReply {
		// ACCEPTED needs every fact: our captured signal aborted, this run's
		// terminal assistant stopReason "aborted", and the same session settled.
		// Anything less may still have interrupted the run, so it stays UNKNOWN.
		if (
			outcome.kind === "settled" &&
			wait.signal.aborted &&
			outcome.abortedStop
		) {
			const confirmed = this.snapshot();
			return {
				result: {
					status: "accepted",
					operation_id: wait.operationId,
					...(confirmed ?? {}),
					native_turn_id: wait.entryId,
				},
			};
		}
		return {
			error: {
				code: "interrupt_unconfirmed",
				message: "Pi interrupt outcome is unconfirmed for the expected run",
			},
		};
	}

	private async performSettings(
		params: Record<string, unknown>,
		operationId: string,
		epoch: number | undefined,
	): Promise<FrontendReply> {
		const current = this.current;
		if (
			current === undefined ||
			epoch === undefined ||
			current.epoch !== epoch ||
			!this.isCurrent(current)
		) {
			return {
				error: {
					code: "session_changed",
					message: "Pi session changed before settings admission",
				},
			};
		}
		const requestedSession = boundedFrontendString(params.native_session_id);
		if (
			requestedSession === undefined ||
			requestedSession !== current.nativeSessionId
		) {
			return {
				error: {
					code: "wrong_session",
					message: "settings target is not the current Pi session",
				},
			};
		}
		if (!current.ctx.isIdle() || current.ctx.hasPendingMessages()) {
			return {
				error: {
					code: "busy",
					message: "Pi settings require an idle session with no queued turn",
				},
			};
		}
		const requestedModel =
			params.model === undefined
				? undefined
				: boundedFrontendString(params.model);
		if (params.model !== undefined && requestedModel === undefined) {
			return {
				error: {
					code: "invalid_request",
					message: "settings model must be a bounded provider/id",
				},
			};
		}
		const requestedThinking =
			params.reasoning_effort === undefined
				? undefined
				: boundedFrontendString(params.reasoning_effort);
		if (
			params.reasoning_effort !== undefined &&
			requestedThinking === undefined
		) {
			return {
				error: {
					code: "invalid_request",
					message: "settings reasoning_effort must be a known level",
				},
			};
		}
		if (requestedModel === undefined && requestedThinking === undefined) {
			return {
				error: {
					code: "invalid_request",
					message: "no Pi setting was supplied",
				},
			};
		}
		// Pi's supported setModel() awaits provider auth before it mutates the
		// active native session.  There is no public expected-session/idle guard
		// spanning that await, so a session switch or a native prompt can land the
		// mutation in a different live session.  Never call it until upstream
		// exposes an atomic session-scoped mutation.  Reject a mixed request before
		// touching thinking too, so its result cannot be mistaken for a confirmed
		// model update.
		if (requestedModel !== undefined) {
			return {
				error: {
					code: "model_update_proof_gated",
					message:
						"Pi model updates remain disabled pending an atomic public session guard",
				},
			};
		}

		if (requestedThinking !== undefined) {
			if (
				!FRONTEND_THINKING_LEVELS.includes(
					requestedThinking as FrontendThinkingLevel,
				)
			) {
				return {
					error: {
						code: "unsupported_thinking",
						message: "requested Pi thinking level is unsupported",
					},
				};
			}
			const target = current.ctx.model;
			if (
				target === undefined ||
				!availableThinkingLevel(
					target,
					requestedThinking as FrontendThinkingLevel,
				)
			) {
				return {
					error: {
						code: "unsupported_thinking",
						message: "requested Pi thinking level is unavailable for the model",
					},
				};
			}
		}

		if (requestedThinking !== undefined) {
			try {
				// There is no await between the exact-session/idle/no-pending guard
				// above and this supported binding.  Pi's default persist=false keeps
				// this transcript-local rather than changing a future-session default.
				this.pi.setThinkingLevel(requestedThinking as FrontendThinkingLevel);
			} catch {
				return {
					error: {
						code: "settings_unconfirmed",
						message: "Pi thinking update was not confirmed",
					},
				};
			}
			if (!this.isCurrent(current)) {
				return {
					error: {
						code: "session_changed",
						message: "Pi session changed during thinking update",
					},
				};
			}
		}

		const snapshot = this.snapshot();
		if (snapshot === undefined || !this.isCurrent(current)) {
			return {
				error: {
					code: "session_changed",
					message: "Pi session changed before settings readback",
				},
			};
		}
		// Emit a fresh snapshot whose event sequence includes settings_updated.
		// The response and notification share that revision, so a local host can
		// accept either arrival order without mistaking it for stale state.
		const confirmed = this.record("settings_updated");
		if (confirmed === undefined || !this.isCurrent(current)) {
			return {
				error: {
					code: "session_changed",
					message: "Pi session changed during settings confirmation",
				},
			};
		}
		return {
			result: {
				status: "accepted",
				operation_id: operationId,
				...confirmed,
			},
		};
	}

	private snapshot(): FrontendSnapshot | undefined {
		const current = this.current;
		if (current === undefined || !this.isCurrent(current)) return undefined;
		// A control-side snapshot establishes identity on demand: the daemon's
		// interrupt reads the turn id here first, then compares it exactly.
		this.establishRunIdentity();
		let executionState = current.executionState;
		try {
			if (!current.ctx.isIdle() || current.ctx.hasPendingMessages())
				executionState = "active";
		} catch {
			executionState = "unknown";
		}
		// Identity, not scheduling, gates "active": an unnamed run cannot be an
		// interrupt target, so the unestablished window publishes "unknown".
		if (executionState === "active" && this.activeRun?.entryId === undefined)
			executionState = "unknown";
		current.executionState = executionState;
		let thinking: string | null;
		try {
			thinking = this.pi.getThinkingLevel();
		} catch {
			thinking = null;
		}
		return {
			protocol: FRONTEND_PROTOCOL,
			native_session_id: current.nativeSessionId,
			bridge_epoch: current.epoch,
			snapshot_revision: ++this.snapshotRevision,
			sequence: Math.max(0, this.sequence - 1),
			settings: {
				model: modelReference(current.ctx.model),
				reasoning_effort: thinking,
			},
			execution_state: executionState,
			native_turn_id: this.activeRun?.entryId ?? null,
			pending_interaction: pendingInteractionDescription(),
			capabilities: {
				send: true,
				settings_update: true,
				model_update: false,
				reasoning_effort_update: true,
				interrupt: true,
			},
		};
	}

	private isCurrent(current: FrontendSession): boolean {
		return (
			!this.disposed &&
			this.current === current &&
			current.epoch === this.epoch &&
			sessionIdOf(current.ctx) === current.nativeSessionId
		);
	}

	private record(
		name: string,
		payload?: Pick<
			FrontendEvent,
			"operation_id" | "native_turn_id" | "terminal" | "error_code" | "result"
		>,
	): FrontendSnapshot | undefined {
		const current = this.current;
		if (current === undefined || !this.isCurrent(current)) return undefined;
		const event: FrontendEvent = {
			name,
			native_session_id: current.nativeSessionId,
			bridge_epoch: current.epoch,
			execution_state: current.executionState,
			sequence: this.sequence++,
			...payload,
		};
		this.history.push(event);
		if (this.history.length > FRONTEND_MAX_HISTORY) this.history.shift();
		// Snapshot after assigning the event sequence.  Its watermark therefore
		// describes every lifecycle fact already reflected in the snapshot.
		const snapshot = this.snapshot();
		this.write({ type: "event", event });
		if (snapshot !== undefined) this.write({ type: "snapshot", snapshot });
		return snapshot;
	}
	/** Push a fresh snapshot because the pending interaction changed.
	 *
	 * A question tool blocks mid-turn with no lifecycle event, so the
	 * daemon would otherwise hold a stale snapshot until the answer.
	 */
	publishInteractionSnapshot(): void {
		this.publishSnapshot();
	}

	private publishSnapshot(socket?: Socket, known?: FrontendSnapshot): void {
		const snapshot = known ?? this.snapshot();
		if (snapshot !== undefined)
			this.write({ type: "snapshot", snapshot }, socket);
	}

	private respond(socket: Socket, id: string, reply: FrontendReply): void {
		if (reply.error === undefined)
			this.writeFrame(socket, {
				type: "response",
				id,
				result: reply.result ?? {},
			});
		else this.writeFrame(socket, { type: "response", id, error: reply.error });
	}

	private write(frame: Record<string, unknown>, socket = this.socket): void {
		if (socket !== undefined) this.writeFrame(socket, frame);
	}

	private writeFrame(socket: Socket, frame: Record<string, unknown>): void {
		if (
			socket.destroyed ||
			!socket.writable ||
			socket.writableLength > FRONTEND_MAX_FRAME_BYTES
		) {
			socket.destroy();
			return;
		}
		const text = `${JSON.stringify(frame)}\n`;
		if (Buffer.byteLength(text) > FRONTEND_MAX_FRAME_BYTES) {
			socket.destroy();
			return;
		}
		try {
			socket.write(text);
		} catch {
			socket.destroy();
		}
	}

	private reserveOperation(operationId: string): boolean {
		if (this.operations.size >= FRONTEND_MAX_OPERATIONS) {
			for (const [candidate, reply] of this.operations) {
				if (reply !== undefined) {
					this.operations.delete(candidate);
					break;
				}
			}
		}
		if (this.operations.size >= FRONTEND_MAX_OPERATIONS) return false;
		this.operations.set(operationId, undefined);
		return true;
	}
}

function registerFrontendBridge(pi: ExtensionAPI): void {
	const lease = acquireFrontendBridge();
	if (lease === undefined) return;
	try {
		pi.registerFlag("theater-frontend-config", {
			description: "Private Theater Pi frontend bridge configuration",
			type: "string",
		});
		const configured =
			frontendConfigFlag(process.argv) ??
			pi.getFlag("theater-frontend-config") ??
			process.env.THEATER_PI_FRONTEND_CONFIG;
		if (typeof configured !== "string" || !configured.trim()) {
			releaseFrontendBridge(lease);
			return;
		}
		const bridge = new FrontendBridge(pi, configured);
		const publishInteraction = () => bridge.publishInteractionSnapshot();
		interactionListeners.add(publishInteraction);
		pi.on("session_start", (_event, ctx) => bridge.start(ctx));
		pi.on("before_agent_start", (_event, ctx) => {
			// before_agent_start is human-prompt-only; Theater sends begin at
			// agent_start.  Both boundaries share one identity rule: the first
			// durable entry after the baseline.
			bridge.beginRun(ctx);
			bridge.transition(ctx, "before_agent_start", "active");
		});
		pi.on("agent_start", (_event, ctx) => {
			bridge.beginRun(ctx);
			bridge.transition(ctx, "agent_start", "active");
		});
		pi.on("agent_end", (_event, ctx) =>
			bridge.transition(ctx, "agent_end", "active"),
		);
		pi.on("message_end", (event, ctx) =>
			bridge.observeMessageEnd(ctx, event.message),
		);
		pi.on("agent_settled", (_event, ctx) => {
			// The idle boundary is recorded first so interrupt evidence
			// resolves against a settled snapshot; the settled event keeps
			// the finished run's id, then settle releases the identity.
			bridge.transition(
				ctx,
				"agent_settled",
				stateOf(ctx) === "idle" ? "idle" : "unknown",
			);
			bridge.settleRun(ctx);
			bridge.settleSendTurn(ctx);
		});
		pi.on("session_before_compact", (_event, ctx) =>
			bridge.transition(ctx, "session_before_compact", "active"),
		);
		pi.on("session_compact", (_event, ctx) =>
			bridge.transition(ctx, "session_compact", stateOf(ctx)),
		);
		pi.on("session_compact_failed", (_event, ctx) =>
			bridge.transition(ctx, "session_compact_failed", stateOf(ctx)),
		);
		pi.on("model_select", (_event, ctx) =>
			bridge.transition(ctx, "model_select", stateOf(ctx)),
		);
		pi.on("thinking_level_select", (_event, ctx) =>
			bridge.transition(ctx, "thinking_level_select", stateOf(ctx)),
		);
		pi.on("session_shutdown", () => {
			bridge.dispose();
			interactionListeners.delete(publishInteraction);
			releaseFrontendBridge(lease);
		});
	} catch (error) {
		releaseFrontendBridge(lease);
		throw error;
	}
}

export default async function theaterMcpBridge(pi: ExtensionAPI) {
	registerFrontendBridge(pi);
	const launchConfigPath = configFlag(process.argv);
	if (launchConfigPath?.trim()) {
		registerIdleStatus(pi);
		registerTranscriptSwitches(pi);
		registerLifecycleMarkers(pi);
	}
	const bridgeLease = acquireBridge();
	if (bridgeLease === undefined) {
		// A user-local copy may own the MCP connection.  The status protocol is
		// independent and still belongs to this Theater-launched Pi session.
		return;
	}
	pi.registerFlag("theater-mcp-config", {
		description: "Launch-local Theater stdio MCP configuration",
		type: "string",
	});
	const configPath = launchConfigPath ?? pi.getFlag("theater-mcp-config");
	if (configPath === undefined) {
		releaseBridge(bridgeLease);
		return;
	}
	if (typeof configPath !== "string" || !configPath.trim()) {
		releaseBridge(bridgeLease);
		throw new Error("--theater-mcp-config requires a configuration path");
	}
	if (!launchConfigPath) {
		registerIdleStatus(pi);
		registerTranscriptSwitches(pi);
		registerLifecycleMarkers(pi);
	}

	const clients: McpClient[] = [];
	try {
		const registered = new Set<string>();
		for (const server of await loadConfig(configPath)) {
			const client = new McpClient(server);
			try {
				await client.initialize();
				await registerServerTools(pi, server, client, registered);
				clients.push(client);
			} catch (error) {
				await client.close();
				if (CORE_MCP_SERVERS.has(server.name)) throw error;
			}
		}
	} catch (error) {
		// Any setup failure — config read/parse, process startup, tool listing,
		// or tool registration — must not strand the process-wide lease.  Close
		// the client when one was created and release exactly once, then rethrow
		// with the original context.
		await Promise.all(clients.map((client) => client.close()));
		releaseBridge(bridgeLease);
		throw error instanceof Error
			? error
			: new Error(`required Theater MCP startup failed: ${String(error)}`);
	}
	pi.on("session_shutdown", async () => {
		// Each replacement creates a fresh extension instance. Tear down this
		// instance before the next factory acquires a new process-wide lease.
		await Promise.all(clients.map((client) => client.close()));
		releaseBridge(bridgeLease);
	});
}
