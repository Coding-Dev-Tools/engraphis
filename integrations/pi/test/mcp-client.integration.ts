import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { EngraphisMcpClient } from "../src/mcp-client.ts";

const PROJECT_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

test("discovers and calls the installed Engraphis MCP server", { timeout: 30_000 }, async () => {
	const database = join(tmpdir(), `engraphis-pi-${randomUUID()}.db`);
	const publicCommand = process.env.ENGRAPHIS_PI_TEST_COMMAND;
	const client = new EngraphisMcpClient({
		// Exercise the same public console entry that a published Pi package launches.
		// Release/CI sets the override after installing this checkout. Local development
		// uses the checkout module so an older globally installed console script cannot
		// invalidate the source-under-test result.
		command: publicCommand ?? process.env.PYTHON ?? "python",
		args: publicCommand ? undefined : ["-m", "engraphis.mcp_server"],
		cwd: PROJECT_ROOT,
		environment: {
			ENGRAPHIS_DB_PATH: database,
			// Keep CI deterministic and avoid downloading/loading the optional embedding model.
			ENGRAPHIS_EMBED_MODEL: "",
		},
	});

	try {
		const status = await client.status();
		assert.equal(status.connected, true);
		assert.equal(Number(status.toolCount), 9);

		const tools = (await client.searchTools("")).tools as Array<{ name: string }>;
		const names = new Set(tools.map((tool) => tool.name));
		for (const required of [
			"engraphis_session",
			"engraphis_recall_context",
			"engraphis_remember",
			"engraphis_discover_actions",
			"engraphis_execute_read",
			"engraphis_execute_action",
			"engraphis_get_memory",
			"engraphis_update_memory",
			"engraphis_conflict_review",
		]) {
			assert.ok(names.has(required), `expected ${required} in the MCP tool catalog`);
		}

		const started = await client.callTool("engraphis_session", {
			action: "start",
			workspace: "default",
			goal: "Inspect local memory health.",
		});
		assert.equal(started.isError, false);

		const discovered = await client.callTool("engraphis_discover_actions", {
			task: "Show memory store statistics.",
		});
		assert.equal(discovered.isError, false);
		const discovery = JSON.parse(discovered.content?.[0]?.text ?? "{}");
		const action = discovery.actions?.[0];
		assert.equal(action?.canonical_action, "stats");

		const result = await client.callTool("engraphis_execute_read", {
			capability_id: action.capability_id,
			schema_digest: action.schema_digest,
			arguments: { workspace: "default" },
		});
		assert.equal(result.isError, false);
		assert.match(result.content?.[0]?.text ?? "", /"memories"/);
	} finally {
		await client.close();
		await Promise.all([database, `${database}-wal`, `${database}-shm`].map((path) => rm(path, { force: true })));
	}
});
