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
import { mkdirSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const OWNER = Symbol.for("theater.pi.mcp-bridge.owner");
const STARTUP_TIMEOUT_MS = 10_000;
const MAX_FRAME_CHARS = 1024 * 1024;
const CORE_MCP_SERVERS = new Set(["theater", "theater_wait"]);
const IDLE_STATUS_KEY = "theater.pi.idle";
const IDLE_STATUS_TEXT = "Theater: idle";
const SWITCH_MARKER = ".theater-pi-switch.json";
const SWITCHES_DIR = ".theater-pi-switches";
const SWITCH_MARKER_VERSION = 1;

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
function lifecycleData(phase: LifecyclePhase, extra: LifecycleExtras = {}): {
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
	on(event: "session_before_compact", handler: (event: { willRetry: unknown }, ctx: unknown) => void): void;
	on(event: "agent_settled", handler: (event: unknown, ctx: unknown) => void): void;
	on(event: "session_shutdown", handler: (event: unknown, ctx: unknown) => void): void;
}
function writeMarker(pi: LifecycleExtensionApi, phase: LifecyclePhase, extra?: LifecycleExtras): void {
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
			writeMarker(pi, LIFECYCLE_PHASE.compactionWillRetry, { reason: "overflow" });
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
function joinErrorText(content: ToolResultContent[] | undefined, server: string, toolName: string): string {
	const parts: string[] = [];
	if (Array.isArray(content)) {
		for (const item of content) {
			if (typeof item?.text === "string" && item.text.trim()) parts.push(item.text);
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

function registerIdleStatus(pi: ExtensionAPI): void {
	// Pi's status lifecycle, rather than its static screen chrome, is the
	// authority for an idle reading.  `agent_settled` includes retries,
	// compaction/retry, and queued continuations.
	pi.on("session_start", (_event, ctx) => showIdleStatus(ctx));
	pi.on("before_agent_start", (_event, ctx) => clearIdleStatus(ctx));
	pi.on("agent_start", (_event, ctx) => clearIdleStatus(ctx));
	pi.on("agent_settled", (_event, ctx) => showIdleStatus(ctx));

	// These operations can run without an agent lifecycle event.  Clear first
	// so a custom or hidden working indicator cannot leave an old idle marker
	// on the screen, then restore only when Pi itself reports it is idle.
	pi.on("session_before_compact", (_event, ctx) => clearIdleStatus(ctx));
	pi.on("session_compact", (_event, ctx) => showIdleStatus(ctx));
	pi.on("session_before_tree", (_event, ctx) => clearIdleStatus(ctx));
	pi.on("session_tree", (_event, ctx) => showIdleStatus(ctx));
	pi.on("session_shutdown", (_event, ctx) => clearIdleStatus(ctx));
}

function removeSwitchMarker(ctx: ExtensionContext): void {
	const marker = join(resolve(ctx.sessionManager.getSessionDir()), SWITCH_MARKER);
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

function writeSwitchDocument(root: string, location: string, document: object): void {
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
	if (dirname(location) !== root || dirname(previous) !== root || location === previous) return;

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
			if ((error as NodeJS.ErrnoException).code !== "ENOENT" || records === undefined) throw error;
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
		const records = event.reason === "new" ? 0 : ctx.sessionManager.getEntries().length + 1;
		writeSwitchMarker(event.reason, event.previousSessionFile, target, ctx, records);
	});
	pi.on("session_shutdown", (event, ctx) => {
		if (event.reason === "new" || event.reason === "resume" || event.reason === "fork") {
			writeSwitchMarker(event.reason, ctx.sessionManager.getSessionFile(), event.targetSessionFile, ctx);
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
		throw new Error(`cannot read Theater MCP config ${path}: ${(error as Error).message}`);
	}
	if (!record(document) || !record(document.mcpServers)) {
		throw new Error(`Theater MCP config ${path} must define an mcpServers object`);
	}
	const servers = Object.entries(document.mcpServers);
	if (servers.length === 0) {
		throw new Error(`Theater MCP config ${path} must define at least one MCP server`);
	}
	return servers.map(([name, value]) => loadServerConfig(path, name, value));
}

function loadServerConfig(path: string, name: string, value: unknown): ServerConfig {
	if (!name.trim() || !record(value)) {
		throw new Error(`Theater MCP config ${path} has an invalid server entry`);
	}
	if (typeof value.command !== "string" || !value.command.trim()) {
		throw new Error(`Theater MCP config ${path} has no executable for ${name}`);
	}
	if (value.args !== undefined && (!Array.isArray(value.args) || !value.args.every((arg) => typeof arg === "string"))) {
		throw new Error(`Theater MCP config ${path} has invalid arguments for ${name}`);
	}
	if (
		value.env !== undefined &&
		(!record(value.env) || !Object.values(value.env).every((entry) => typeof entry === "string"))
	) {
		throw new Error(`Theater MCP config ${path} has invalid environment for ${name}`);
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
		if (!record(value) || typeof value.name !== "string" || !value.name || !record(value.inputSchema)) {
			throw new Error(`${server} MCP tools/list returned an invalid tool definition`);
		}
		return {
			name: value.name,
			description: typeof value.description === "string" ? value.description : undefined,
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
		return tools(await this.request("tools/list", undefined, undefined, STARTUP_TIMEOUT_MS), this.config.name);
	}

	async callTool(name: string, arguments_: unknown, signal?: AbortSignal): Promise<ToolResult> {
		return (await this.request("tools/call", { name, arguments: arguments_ }, signal)) as ToolResult;
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
		child.on("error", (error) => this.fail(new Error(`${this.config.name} MCP process failed: ${error.message}`)));
		child.on("exit", (code, signal) => {
			if (!this.closed) this.fail(new Error(`${this.config.name} MCP process exited (${code ?? signal ?? "unknown"})`));
		});
	}

	private consume(chunk: string): void {
		this.buffer += chunk;
		if (this.buffer.length > MAX_FRAME_CHARS && !this.buffer.includes("\n")) {
			this.fail(new Error(`${this.config.name} MCP emitted an oversized JSON-RPC frame`));
			return;
		}
		for (;;) {
			const newline = this.buffer.indexOf("\n");
			if (newline < 0) return;
			const line = this.buffer.slice(0, newline);
			this.buffer = this.buffer.slice(newline + 1);
			if (!line.trim()) continue;
			if (line.length > MAX_FRAME_CHARS) {
				this.fail(new Error(`${this.config.name} MCP emitted an oversized JSON-RPC frame`));
				return;
			}
			let response: Response;
			try {
				response = JSON.parse(line) as Response;
			} catch {
				this.fail(new Error(`${this.config.name} MCP emitted malformed JSON-RPC`));
				return;
			}
			if (typeof response.id !== "number") continue;
			const pending = this.finish(response.id);
			if (!pending) continue;
			if (response.error) {
				pending.reject(new Error(`JSON-RPC error ${response.error.code}: ${response.error.message}`));
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
					this.notify("notifications/cancelled", { requestId: id, reason: "Pi tool invocation cancelled" });
				} catch {
					// The pending request still receives its local cancellation below.
				}
				this.finish(id)?.reject(new Error(`${this.config.name} MCP request cancelled`));
			};
			const pending: Pending = { resolve, reject, timeout: undefined, removeAbort: undefined };
			if (signal) {
				signal.addEventListener("abort", abort, { once: true });
				pending.removeAbort = () => signal.removeEventListener("abort", abort);
			}
			if (timeoutMs) {
				pending.timeout = setTimeout(() => {
					this.finish(id)?.reject(new Error(`${this.config.name} MCP ${method} timed out`));
				}, timeoutMs);
			}
			this.pending.set(id, pending);
			try {
				this.send({ jsonrpc: "2.0", id, method, params });
			} catch (error) {
				this.finish(id)?.reject(error instanceof Error ? error : new Error(String(error)));
			}
		});
	}

	private send(message: object): void {
		if (this.closed || !this.child?.stdin.writable) throw new Error(`${this.config.name} MCP process is not available`);
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
	const names = discovered.map((tool) => ({ tool, name: toolName(server.name, tool.name) }));
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
				if (signal?.aborted) return { content: [{ type: "text", text: "Cancelled" }], details: {} };
				try {
					const result = await client.callTool(tool.name, params, signal);
					const content = (result.content ?? []).map((item) => ({
						type: "text" as const,
						text: item.text ?? JSON.stringify(item),
					}));
					if (result.isError) {
						throw new Error(joinErrorText(result.content, server.name, tool.name));
					}
					return { content, details: { server: server.name, tool: tool.name } };
				} catch (error) {
					if (signal?.aborted) return { content: [{ type: "text", text: "Cancelled" }], details: {} };
					throw error;
				}
			},
		});
	}
	for (const { name } of names) registered.add(name);
}

export default async function theaterMcpBridge(pi: ExtensionAPI) {
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
