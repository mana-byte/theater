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
import { readFile } from "node:fs/promises";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const OWNER = Symbol.for("theater.pi.mcp-bridge.owner");
const ownsBridge = !(globalThis as Record<symbol, boolean | undefined>)[OWNER];
const STARTUP_TIMEOUT_MS = 10_000;
const MAX_FRAME_CHARS = 1024 * 1024;

if (ownsBridge) {
	(globalThis as Record<symbol, boolean | undefined>)[OWNER] = true;
}

interface ServerConfig {
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

async function loadConfig(path: string): Promise<ServerConfig> {
	let document: unknown;
	try {
		document = JSON.parse(await readFile(path, "utf8"));
	} catch (error) {
		throw new Error(`cannot read Theater MCP config ${path}: ${(error as Error).message}`);
	}
	if (!record(document) || !record(document.mcpServers) || !record(document.mcpServers.theater)) {
		throw new Error(`Theater MCP config ${path} must define mcpServers.theater`);
	}
	const server = document.mcpServers.theater;
	if (typeof server.command !== "string" || !server.command.trim()) {
		throw new Error(`Theater MCP config ${path} has no executable theater command`);
	}
	if (server.args !== undefined && (!Array.isArray(server.args) || !server.args.every((arg) => typeof arg === "string"))) {
		throw new Error(`Theater MCP config ${path} has invalid theater arguments`);
	}
	if (
		server.env !== undefined &&
		(!record(server.env) || !Object.values(server.env).every((value) => typeof value === "string"))
	) {
		throw new Error(`Theater MCP config ${path} has invalid theater environment`);
	}
	return {
		command: server.command,
		args: (server.args as string[] | undefined) ?? [],
		env: server.env as Record<string, string> | undefined,
	};
}

function tools(result: unknown): Tool[] {
	if (!record(result) || !Array.isArray(result.tools)) {
		throw new Error("Theater MCP tools/list returned no tools array");
	}
	return result.tools.map((value) => {
		if (!record(value) || typeof value.name !== "string" || !value.name || !record(value.inputSchema)) {
			throw new Error("Theater MCP tools/list returned an invalid tool definition");
		}
		return {
			name: value.name,
			description: typeof value.description === "string" ? value.description : undefined,
			inputSchema: value.inputSchema,
		};
	});
}

class TheaterClient {
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
		return tools(await this.request("tools/list", undefined, undefined, STARTUP_TIMEOUT_MS));
	}

	async callTool(name: string, arguments_: unknown, signal?: AbortSignal): Promise<ToolResult> {
		return (await this.request("tools/call", { name, arguments: arguments_ }, signal)) as ToolResult;
	}

	async close(): Promise<void> {
		if (this.closed) return;
		this.closed = true;
		this.fail(new Error("Theater MCP bridge closed"));
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
		child.on("error", (error) => this.fail(new Error(`Theater MCP process failed: ${error.message}`)));
		child.on("exit", (code, signal) => {
			if (!this.closed) this.fail(new Error(`Theater MCP process exited (${code ?? signal ?? "unknown"})`));
		});
	}

	private consume(chunk: string): void {
		this.buffer += chunk;
		if (this.buffer.length > MAX_FRAME_CHARS && !this.buffer.includes("\n")) {
			this.fail(new Error("Theater MCP emitted an oversized JSON-RPC frame"));
			return;
		}
		for (;;) {
			const newline = this.buffer.indexOf("\n");
			if (newline < 0) return;
			const line = this.buffer.slice(0, newline);
			this.buffer = this.buffer.slice(newline + 1);
			if (!line.trim()) continue;
			if (line.length > MAX_FRAME_CHARS) {
				this.fail(new Error("Theater MCP emitted an oversized JSON-RPC frame"));
				return;
			}
			let response: Response;
			try {
				response = JSON.parse(line) as Response;
			} catch {
				this.fail(new Error("Theater MCP emitted malformed JSON-RPC"));
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
				reject(new Error("Theater MCP request cancelled"));
				return;
			}
			const abort = () => {
				try {
					this.notify("notifications/cancelled", { requestId: id, reason: "Pi tool invocation cancelled" });
				} catch {
					// The pending request still receives its local cancellation below.
				}
				this.finish(id)?.reject(new Error("Theater MCP request cancelled"));
			};
			const pending: Pending = { resolve, reject, timeout: undefined, removeAbort: undefined };
			if (signal) {
				signal.addEventListener("abort", abort, { once: true });
				pending.removeAbort = () => signal.removeEventListener("abort", abort);
			}
			if (timeoutMs) {
				pending.timeout = setTimeout(() => {
					this.finish(id)?.reject(new Error(`Theater MCP ${method} timed out`));
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
		if (this.closed || !this.child?.stdin.writable) throw new Error("Theater MCP process is not available");
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

function toolName(name: string): string {
	return `theater__${name.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}

export default async function theaterMcpBridge(pi: ExtensionAPI) {
	if (!ownsBridge) return;
	pi.registerFlag("theater-mcp-config", {
		description: "Launch-local Theater stdio MCP configuration",
		type: "string",
	});
	const configPath = configFlag(process.argv) ?? pi.getFlag("theater-mcp-config");
	if (configPath === undefined) return;
	if (typeof configPath !== "string" || !configPath.trim()) {
		throw new Error("--theater-mcp-config requires a configuration path");
	}

	const client = new TheaterClient(await loadConfig(configPath));
	let discovered: Tool[];
	try {
		await client.initialize();
		discovered = await client.listTools();
	} catch (error) {
		await client.close();
		throw new Error(`required Theater MCP startup failed: ${(error as Error).message}`);
	}
	for (const tool of discovered) {
		pi.registerTool({
			name: toolName(tool.name),
			label: `theater/${tool.name}`,
			description: tool.description ?? `Theater MCP tool ${tool.name}`,
			promptSnippet: `Theater: ${tool.description ?? tool.name}`,
			parameters: Type.Unsafe(tool.inputSchema),
			async execute(_id, params, signal) {
				if (signal?.aborted) return { content: [{ type: "text", text: "Cancelled" }], details: {} };
				try {
					const result = await client.callTool(tool.name, params, signal);
					const content = (result.content ?? []).map((item) => ({
						type: "text" as const,
						text: item.text ?? JSON.stringify(item),
					}));
					if (result.isError) throw new Error(`Theater tool ${tool.name} returned an error`);
					return { content, details: { tool: tool.name } };
				} catch (error) {
					if (signal?.aborted) return { content: [{ type: "text", text: "Cancelled" }], details: {} };
					throw error;
				}
			},
		});
	}
	pi.on("session_shutdown", async () => client.close());
}
