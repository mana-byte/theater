/**
 * Self-contained pure helpers exercised by the Pi bridge behavior suite.
 *
 * The bridge inlines this same logic into `theater_mcp_bridge.ts` (it cannot
 * import a separate module without a wheel force-include, which the bridge
 * worker does not own).  This file is test-only and ships nothing: it keeps
 * the deterministic jiti suite executable without typebox or the Pi type
 * package, and a source-contract test in `test_pi_harness.py` asserts the
 * bridge contains the matching logic so the two cannot silently drift.
 *
 * Nothing here imports anything.
 */

/** Wire customType persisted alongside every lifecycle marker. */
export const LIFECYCLE_CUSTOM_TYPE = "theater:lifecycle";

/** Wire data version; bump if the marker schema gains a field. */
export const LIFECYCLE_VERSION = 1;

/**
 * Phases the Theater parser consumes.
 *
 * `retry-scheduled` is intentionally absent: Pi exposes no extension-visible
 * event that reliably signals a retry, so fabricating it would misclassify
 * final errors.  The parser releases a deferred terminal on `settled` instead.
 */
export const LIFECYCLE_PHASE = {
	compactionWillRetry: "compaction-will-retry",
	settled: "settled",
} as const;

export type LifecyclePhase = (typeof LIFECYCLE_PHASE)[keyof typeof LIFECYCLE_PHASE];

/** Extra fields persisted for compaction markers. */
export interface LifecycleExtras {
	readonly reason?: string;
}

/**
 * Build the durable marker payload.  Always sets `version` and `phase`; merges
 * optional diagnostic fields without mutating its input.  Kept pure so tests
 * can assert the exact wire shape.
 */
export function lifecycleData(
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

/** Process-global key recording that lifecycle handlers are already registered. */
const LIFECYCLE_REGISTERED = Symbol.for("theater.pi.lifecycle.registered");

/** True when this process has not yet registered lifecycle markers. */
export function acquireLifecycleGuard(): boolean {
	const owners = globalThis as Record<symbol, boolean | undefined>;
	if (owners[LIFECYCLE_REGISTERED]) return false;
	owners[LIFECYCLE_REGISTERED] = true;
	return true;
}

/**
 * Release the registration guard (called on session_shutdown so a fresh
 * extension runtime can register again).  Idempotent.
 */
export function releaseLifecycleGuard(): void {
	const owners = globalThis as Record<symbol, boolean | undefined>;
	delete owners[LIFECYCLE_REGISTERED];
}

/** Minimal structural view of the Pi extension API we touch. */
export interface LifecycleExtensionApi {
	appendEntry(customType: string, data?: unknown): void;
	on(
		event: "session_before_compact",
		handler: (event: { willRetry: unknown }, ctx: unknown) => void,
	): void;
	on(event: "agent_settled", handler: (event: unknown, ctx: unknown) => void): void;
	on(event: "session_shutdown", handler: (event: unknown, ctx: unknown) => void): void;
}

/** Persist one lifecycle marker, never throwing. */
function writeMarker(
	pi: LifecycleExtensionApi,
	phase: LifecyclePhase,
	extra?: LifecycleExtras,
): void {
	try {
		pi.appendEntry(LIFECYCLE_CUSTOM_TYPE, lifecycleData(phase, extra));
	} catch {
		// A session-store failure must not crash Pi or break the turn.
	}
}

/**
 * Register the durable lifecycle marker handlers on `pi`.  Idempotent across
 * two extension copies via `acquireLifecycleGuard`.  Registers a
 * `session_shutdown` handler that releases the guard so a fresh runtime can
 * register after the owner tears down.
 */
export function registerLifecycleMarkers(pi: LifecycleExtensionApi): void {
	if (!acquireLifecycleGuard()) return;
	pi.on("session_before_compact", (event) => {
		if (event.willRetry === true) {
			writeMarker(pi, LIFECYCLE_PHASE.compactionWillRetry, { reason: "overflow" });
		}
	});
	pi.on("agent_settled", () => {
		writeMarker(pi, LIFECYCLE_PHASE.settled);
	});
	pi.on("session_shutdown", () => {
		releaseLifecycleGuard();
	});
}

/** A content item as returned by an MCP tools/call result. */
export interface ToolResultContent {
	readonly type: string;
	readonly text?: string;
	[key: string]: unknown;
}

/** Join the textual content of a failing MCP tool result; generic fallback only when empty. */
export function joinErrorText(
	content: ToolResultContent[] | undefined,
	toolName: string,
): string {
	const parts: string[] = [];
	if (Array.isArray(content)) {
		for (const item of content) {
			if (typeof item?.text === "string" && item.text.trim()) parts.push(item.text);
		}
	}
	if (parts.length > 0) return parts.join("\n");
	return `Theater tool ${toolName} returned an error`;
}
