/**
 * Pure, dependency-free helpers for the Theater Pi MCP bridge.
 *
 * Split out of `theater_mcp_bridge.ts` so they can be exercised by deterministic
 * Node/jiti tests without loading typebox or the Pi type-only package.  The
 * bridge re-exports and calls these; nothing here imports anything.
 *
 * Three concerns live here:
 *
 * 1. Durable lifecycle markers.  Pi does NOT expose `auto_retry_start` to
 *    extensions (it is an internal `_emit` only), and the extension-visible
 *    `agent_end` event drops the `willRetry` flag the internal event carries.
 *    There is therefore no extension-visible signal that reliably distinguishes
 *    a retried error from a final one.  Inferring `retry-scheduled` from a
 *    `message_end` whose `stopReason === "error"` would misclassify final errors
 *    and strand the turn open, so we do not emit it at all.  The parser defers
 *    error/length/aborted terminals itself and releases exactly one `turn_end`
 *    when `agent_settled` arrives.  We persist two first-class extension events:
 *
 *      - `agent_settled`                 -> `settled`
 *      - `session_before_compact`         -> `compaction-will-retry` (only when
 *        `willRetry === true`, i.e. overflow recovery retries the turn)
 *
 *    Every marker write is wrapped so a session-store failure can never crash Pi.
 *
 * 2. A process-global registration guard distinct from the MCP lease guard, so
 *    two copies of this extension (Theater's shipped copy plus a user-local
 *    copy) cannot double-register lifecycle handlers and double-write markers.
 *    The guard is released on `session_shutdown`/replacement so a fresh
 *    extension runtime can register again after the owner tears down.
 *
 * 3. Actionable MCP error text: join the textual content a failing tool
 *    returned instead of discarding it for a generic message.
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

export type LifecyclePhase =
	| typeof LIFECYCLE_PHASE.compactionWillRetry
	| typeof LIFECYCLE_PHASE.settled;

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
 * Release the registration guard.
 *
 * Called on `session_shutdown`/replacement so a fresh extension runtime (reload,
 * new/resume/fork session switch) can register its own handlers.  Also used by
 * tests to reset between cases.  Idempotent.
 */
export function releaseLifecycleGuard(): void {
	const owners = globalThis as Record<symbol, boolean | undefined>;
	delete owners[LIFECYCLE_REGISTERED];
}

/**
 * Minimal structural view of the Pi extension API we touch.  Defining it here
 * keeps this module free of the Pi type-only package so tests can import it.
 */
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
		// A session-store failure (closed file, readonly mount, ...) must not
		// crash Pi or break the turn.  The parser tolerates a missing marker by
		// leaving the existing terminal handling in place.
	}
}

/**
 * Register the durable lifecycle marker handlers on `pi`.
 *
 * Idempotent across two extension copies via `acquireLifecycleGuard`: when the
 * guard is already taken this is a no-op.  Call before acquiring the MCP bridge
 * lease so markers fire even when a user-local copy owns the connection.
 *
 * Also registers a `session_shutdown` handler that releases the guard, so a
 * fresh extension runtime (after reload or session replacement) can register
 * again once the owning instance tears down.  The guard release never disables
 * a live owner: `session_shutdown` fires for the instance that owns the guard.
 */
export function registerLifecycleMarkers(pi: LifecycleExtensionApi): void {
	if (!acquireLifecycleGuard()) return;

	// Overflow-recovery compaction retries the interrupted turn afterwards;
	// threshold/manual compaction does not.  Only the retry case needs the
	// marker that keeps the turn open.  The parser must not depend on this
	// marker for correctness, but it is a useful durable hint.
	pi.on("session_before_compact", (event) => {
		if (event.willRetry === true) {
			writeMarker(pi, LIFECYCLE_PHASE.compactionWillRetry, { reason: "overflow" });
		}
	});

	// `agent_settled` fires once no automatic retry, compaction, or queued
	// continuation will run.  This is the authoritative "turn is really done"
	// signal that lets the parser release any pending terminal candidate.
	pi.on("agent_settled", () => {
		writeMarker(pi, LIFECYCLE_PHASE.settled);
	});

	// Release the process-global guard on shutdown so the next extension
	// runtime can register.  This instance is tearing down, so giving up the
	// guard is safe: a live owner never reaches here.
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

/**
 * Join the textual content of a failing MCP tool result into a single error
 * message.  Empty/whitespace-only items are dropped; when no usable text
 * remains a generic fallback is returned.  The result is always a non-empty
 * string so the thrown `Error` carries actionable context.
 */
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
