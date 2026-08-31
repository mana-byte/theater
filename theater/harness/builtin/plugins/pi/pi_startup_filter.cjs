"use strict";

const key = "THEATER_PI_EXPECTED_NEW_SESSION_ID";
const expectedId = process.env[key];
delete process.env[key];

if (expectedId) {
	const expected = `Warning: No project session found with id '${expectedId}'; creating a new session with that id.`;
	const original = console.error;
	let suppressed = false;
	console.error = function (...args) {
		const text = typeof args[0] === "string" ? args[0].replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "") : undefined;
		if (!suppressed && args.length === 1 && text === expected) {
			suppressed = true;
			console.error = original;
			return;
		}
		original.apply(console, args);
	};
}
