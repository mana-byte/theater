// Deterministic behavior tests for bridge_lifecycle.ts.
//
// Loaded with jiti so the TypeScript is transpiled on the fly.  This module
// imports only bridge_lifecycle.ts (no external runtime dependencies), so no
// typebox or @earendil-works/pi-coding-agent resolution is needed.  The
// bridge_lifecycle path is resolved relative to this file via import.meta.url,
// so it works from any checkout/worktree and never hard-codes an absolute path.
//
// Run shape (used by tests/test_pi_harness.py::test_pi_bridge_lifecycle_helpers_behave_under_jiti):
//   node -e 'require(<jiti.cjs>)(null, {})(<this file>)'
// Exit code is non-zero on the first failing assertion; stdout lists each check.

import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

import {
	LIFECYCLE_CUSTOM_TYPE,
	LIFECYCLE_VERSION,
	LIFECYCLE_PHASE,
	lifecycleData,
	acquireLifecycleGuard,
	releaseLifecycleGuard,
	registerLifecycleMarkers,
	joinErrorText,
} from "../theater/harness/builtin/plugins/pi/bridge_lifecycle.ts";

// Sanity: the relative import resolved (guards against a path regression).
void fileURLToPath(import.meta.url);

let checks = 0;
function check(name: string, fn: () => void): void {
	checks += 1;
	try {
		fn();
		console.log(`ok - ${name}`);
	} catch (error) {
		console.error(`not ok - ${name}: ${(error as Error).message}`);
		process.exitCode = 1;
	}
}

/** Record every appendEntry call and every registered handler. */
function fakePi(): {
	entries: { customType: string; data: unknown }[];
	api: {
		appendEntry: (customType: string, data?: unknown) => void;
		on: (event: string, handler: (event: unknown, ctx: unknown) => void) => void;
	};
	fire: (event: string, payload: unknown) => void;
	hasHandler: (event: string) => boolean;
} {
	const entries: { customType: string; data: unknown }[] = [];
	const handlers = new Map<string, (event: unknown, ctx: unknown) => void>();
	return {
		entries,
		api: {
			appendEntry(customType, data) {
				entries.push({ customType, data });
			},
			on(event, handler) {
				handlers.set(event, handler);
			},
		},
		fire(event, payload) {
			const handler = handlers.get(event);
			if (!handler) throw new Error(`no handler for ${event}`);
			handler(payload, {});
		},
		hasHandler(event) {
			return handlers.has(event);
		},
	};
}

// --- lifecycleData shape -----------------------------------------------------

check("lifecycleData sets version and phase", () => {
	const data = lifecycleData(LIFECYCLE_PHASE.settled);
	assert.equal(data.version, LIFECYCLE_VERSION);
	assert.equal(data.phase, "settled");
	assert.equal(Object.keys(data).length, 2);
});

check("lifecycleData merges reason without mutating input", () => {
	const extra = { reason: "overflow" };
	const data = lifecycleData(LIFECYCLE_PHASE.compactionWillRetry, extra);
	assert.equal(data.reason, "overflow");
	// Input untouched.
	assert.deepEqual(extra, { reason: "overflow" });
});

check("lifecycleData omits undefined reason", () => {
	const data = lifecycleData(LIFECYCLE_PHASE.settled, { reason: undefined });
	assert.equal("reason" in data, false);
});

check("retry-scheduled phase is not exported (no fabricated retry signal)", () => {
	assert.equal("retryScheduled" in LIFECYCLE_PHASE, false);
	assert.equal("retry-scheduled" in LIFECYCLE_PHASE, false);
});

// --- duplicate-registration guard -------------------------------------------

check("registerLifecycleMarkers registers handlers on first call", () => {
	releaseLifecycleGuard();
	const { api, hasHandler } = fakePi();
	registerLifecycleMarkers(api);
	assert.equal(hasHandler("session_before_compact"), true);
	assert.equal(hasHandler("agent_settled"), true);
	assert.equal(hasHandler("session_shutdown"), true);
	// No message_end handler: we do not infer retry from error messages.
	assert.equal(hasHandler("message_end"), false);
});

check("registerLifecycleMarkers is a no-op on a second copy (guard taken)", () => {
	// Guard is already taken from the previous check (process-global Symbol.for).
	const { api, hasHandler } = fakePi();
	registerLifecycleMarkers(api);
	assert.equal(hasHandler("session_before_compact"), false);
	assert.equal(hasHandler("agent_settled"), false);
	assert.equal(hasHandler("session_shutdown"), false);
});

check("acquireLifecycleGuard returns true once then false", () => {
	releaseLifecycleGuard();
	assert.equal(acquireLifecycleGuard(), true);
	assert.equal(acquireLifecycleGuard(), false);
	releaseLifecycleGuard();
	assert.equal(acquireLifecycleGuard(), true);
});

// --- marker emission ---------------------------------------------------------

check("agent_settled writes a settled marker", () => {
	releaseLifecycleGuard();
	const { api, entries, fire } = fakePi();
	registerLifecycleMarkers(api);
	fire("agent_settled", {});
	assert.equal(entries.length, 1);
	assert.equal(entries[0].customType, LIFECYCLE_CUSTOM_TYPE);
	assert.equal((entries[0].data as { phase: string }).phase, "settled");
	assert.equal((entries[0].data as { version: number }).version, 1);
});

check("session_before_compact with willRetry=true writes compaction-will-retry", () => {
	releaseLifecycleGuard();
	const { api, entries, fire } = fakePi();
	registerLifecycleMarkers(api);
	fire("session_before_compact", { willRetry: true });
	assert.equal(entries.length, 1);
	assert.equal((entries[0].data as { phase: string }).phase, "compaction-will-retry");
});

check("session_before_compact with willRetry=false writes nothing", () => {
	releaseLifecycleGuard();
	const { api, entries, fire } = fakePi();
	registerLifecycleMarkers(api);
	fire("session_before_compact", { willRetry: false });
	assert.equal(entries.length, 0);
});

check("session_shutdown releases the guard so a fresh runtime can register", () => {
	releaseLifecycleGuard();
	const first = fakePi();
	registerLifecycleMarkers(first.api);
	// Guard taken: a second register while the first owns it is a no-op.
	const second = fakePi();
	registerLifecycleMarkers(second.api);
	assert.equal(second.hasHandler("agent_settled"), false);
	// On shutdown the owner releases the guard...
	first.fire("session_shutdown", {});
	// ...so a fresh runtime can now register its own handlers.
	const third = fakePi();
	registerLifecycleMarkers(third.api);
	assert.equal(third.hasHandler("agent_settled"), true);
});

// --- final-error sequence: NO marker on error, settled on agent_settled -----

check("a final assistant error writes no marker (no message_end handler)", () => {
	releaseLifecycleGuard();
	const { api, entries, fire, hasHandler } = fakePi();
	registerLifecycleMarkers(api);
	// There is no message_end handler to misclassify a final error as retry.
	assert.equal(hasHandler("message_end"), false);
	// Firing an error message_end (if it existed) would do nothing here; the
	// only terminal-releasing marker is settled.
	fire("agent_settled", {});
	assert.equal(entries.length, 1);
	assert.equal((entries[0].data as { phase: string }).phase, "settled");
});

// --- exception safety: marker write failure must not throw ------------------

check("appendEntry throwing does not propagate", () => {
	releaseLifecycleGuard();
	const api = {
		appendEntry() {
			throw new Error("session file closed");
		},
		on(event: string, handler: (event: unknown, ctx: unknown) => void) {
			if (event === "agent_settled") this._h = handler;
		},
		_h: null as ((event: unknown, ctx: unknown) => void) | null,
	};
	registerLifecycleMarkers(api);
	assert.doesNotThrow(() => api._h?.({}, {}));
});

// --- joinErrorText (actionable MCP errors) -----------------------------------

check("joinErrorText preserves server text", () => {
	const text = joinErrorText(
		[{ type: "text", text: "permission denied: write to /etc" }],
		"edit_file",
	);
	assert.equal(text, "permission denied: write to /etc");
});

check("joinErrorText joins multiple nonempty text items with newline", () => {
	const text = joinErrorText(
		[
			{ type: "text", text: "first" },
			{ type: "text", text: "  " },
			{ type: "text", text: "second" },
		],
		"t",
	);
	assert.equal(text, "first\nsecond");
});

check("joinErrorText falls back to generic when no text", () => {
	const text = joinErrorText([], "search");
	assert.equal(text, "Theater tool search returned an error");
});

check("joinErrorText falls back when all items are empty", () => {
	const text = joinErrorText([{ type: "text", text: "" }, { type: "image" }], "x");
	assert.equal(text, "Theater tool x returned an error");
});

check("joinErrorText handles undefined content", () => {
	const text = joinErrorText(undefined, "y");
	assert.equal(text, "Theater tool y returned an error");
});

check("joinErrorText uses item.text only, not other fields", () => {
	const text = joinErrorText([{ type: "text", text: "real", extra: "ignored" }], "z");
	assert.equal(text, "real");
});

console.log(`\n${checks} checks run`);
if (process.exitCode) {
	console.error("FAIL");
} else {
	console.log("ok");
}
