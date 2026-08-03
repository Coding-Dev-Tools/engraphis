import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import test from "node:test";

import { discoverAndLoadExtensions } from "@earendil-works/pi-coding-agent";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

test("Pi's actual package loader recognizes and loads the extension manifest", async () => {
	const result = await discoverAndLoadExtensions([packageRoot], packageRoot, resolve(packageRoot, ".missing-agent-dir"));

	assert.deepEqual(result.errors, []);
	assert.equal(result.extensions.length, 1);
	const extension = result.extensions[0];
	assert.equal(extension.handlers.has("before_agent_start"), false);
	assert.equal(extension.handlers.has("session_start"), false);
	assert.equal(extension.handlers.has("session_shutdown"), true);
	assert.deepEqual([...extension.tools.keys()], [
		"engraphis_session",
		"engraphis_recall_context",
		"engraphis_remember",
		"engraphis_discover_actions",
		"engraphis_execute_read",
		"engraphis_execute_action",
	]);
});
