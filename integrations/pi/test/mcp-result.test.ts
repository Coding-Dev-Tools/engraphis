import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { EXTENSION_VERSION } from "../src/config.ts";
import {
	EngraphisMcpClient,
	discoveredActionsFromResult,
	formatMcpResult,
	safeErrorMessage,
} from "../src/mcp-client.ts";

test("throws sanitized failures for MCP error flags and Engraphis error envelopes", () => {
	assert.throws(
		() => formatMcpResult({ isError: true, content: [{ type: "text", text: "secret details" }] }),
		/Engraphis rejected the request/,
	);
	assert.throws(
		() => formatMcpResult({ isError: false, content: [{ type: "text", text: "Error: invalid_arguments" }] }),
		/invalid_arguments/,
	);
	assert.doesNotThrow(() => formatMcpResult({
		isError: false,
		content: [{ type: "text", text: "An Error: inside successful prose is not an error envelope." }],
	}));
});

test("does not expose arbitrary transport error details", () => {
	assert.equal(
		safeErrorMessage(new Error("spawn C:/Users/name/secret-token ENOENT")),
		"Engraphis is unavailable. Verify `pip install \"engraphis[mcp]>=1.4.0,<2\"` and ENGRAPHIS_MCP_COMMAND.",
	);
});

test("extracts only bounded stateful capability metadata for the approval gate", () => {
	const actions = discoveredActionsFromResult({
		content: [{ type: "text", text: JSON.stringify({ actions: [
			{
				capability_id: "cap_12345678",
				canonical_action: "retire\nspoof",
				schema_digest: "1234567890abcdef",
				side_effect: "destructive",
				title: "Retire\u0000 memory",
			},
			{
				capability_id: "cap_readonly",
				canonical_action: "stats",
				schema_digest: "abcdef1234567890",
				side_effect: "read",
				title: "Stats",
			},
		] }) }],
	});
	assert.deepEqual(actions, [{
		canonicalAction: "retire spoof",
		capabilityId: "cap_12345678",
		schemaDigest: "1234567890abcdef",
		sideEffect: "destructive",
		title: "Retire memory",
	}]);
});

test("keeps the MCP client handshake version synchronized with package metadata", async () => {
	const packageJson = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
	assert.equal(EXTENSION_VERSION, packageJson.version);
});

test("shutdown during startup closes the late client instead of publishing it", async () => {
	const client = new EngraphisMcpClient({ command: "unused", environment: {} });
	let publishClient!: (value: { close: () => Promise<void> }) => void;
	let closes = 0;
	const fakeClient = { close: async () => { closes += 1; } };
	(client as unknown as { open: () => Promise<typeof fakeClient> }).open = () =>
		new Promise((resolve) => { publishClient = resolve; });

	const connecting = client.connect();
	const closing = client.close();
	publishClient(fakeClient);
	await closing;
	await assert.rejects(connecting, /closed during startup/);
	assert.ok(closes >= 1);
});
