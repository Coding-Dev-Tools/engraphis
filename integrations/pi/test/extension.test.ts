import assert from "node:assert/strict";
import test from "node:test";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import extension from "../index.ts";
import { EngraphisMcpClient } from "../src/mcp-client.ts";

type RegisteredTool = {
	executionMode?: string;
	name: string;
	promptGuidelines?: string[];
	promptSnippet?: string;
	execute: (...args: any[]) => Promise<unknown>;
};

function extensionHarness() {
	const tools: RegisteredTool[] = [];
	const handlers = new Map<string, unknown>();
	const pi = {
		on: (event: string, handler: unknown) => handlers.set(event, handler),
		registerTool: (tool: RegisteredTool) => tools.push(tool),
	} as unknown as ExtensionAPI;
	extension(pi);
	return { handlers, tools };
}

test("registers the daily memory loop with tool-scoped guidance", () => {
	const { handlers, tools } = extensionHarness();

	assert.deepEqual(
		tools.map((tool) => tool.name),
		[
			"engraphis_session",
			"engraphis_recall_context",
			"engraphis_remember",
			"engraphis_discover_actions",
			"engraphis_execute_read",
			"engraphis_execute_action",
		],
	);
	assert.ok(handlers.has("session_shutdown"));
	assert.equal(handlers.has("session_start"), false, "the MCP process should start lazily on tool use");
	for (const tool of tools) {
		assert.ok(tool.promptSnippet, `${tool.name} should be discoverable without a global prompt hook`);
		assert.ok(tool.promptGuidelines?.length, `${tool.name} should provide Pi-native guidance`);
	}
	assert.deepEqual(
		Object.fromEntries(tools.map((tool) => [tool.name, tool.executionMode])),
		{
			engraphis_session: "sequential",
			engraphis_recall_context: "sequential",
			engraphis_remember: "sequential",
			engraphis_discover_actions: "parallel",
			engraphis_execute_read: "parallel",
			engraphis_execute_action: "sequential",
		},
	);
});

test("requires a fresh discovery and explicit Pi approval for every advanced action", async () => {
	const original = EngraphisMcpClient.prototype.callTool;
	const calls: string[] = [];
	EngraphisMcpClient.prototype.callTool = async function (name: string) {
		calls.push(name);
		if (name === "engraphis_discover_actions") {
			return {
				isError: false,
				content: [{
					type: "text",
					text: JSON.stringify({ actions: [{
						capability_id: "cap_test-capability",
						canonical_action: "secure_erase",
						schema_digest: "1234567890abcdef",
						side_effect: "destructive",
						title: "Securely erase a leaked memory",
					}] }),
				}],
			};
		}
		return { isError: false, content: [{ type: "text", text: "{\"executed\":true}" }] };
	};

	try {
		const { tools } = extensionHarness();
		const discover = tools.find((tool) => tool.name === "engraphis_discover_actions")!;
		const execute = tools.find((tool) => tool.name === "engraphis_execute_action")!;
		const params = {
			arguments: { memory_id: "mem_example", workspace: "default" },
			capability_id: "cap_test-capability",
			schema_digest: "1234567890abcdef",
		};
		await discover.execute("discover", { task: "securely erase a leaked memory" }, undefined);
		await assert.rejects(
			execute.execute("action", params, undefined, undefined, {
				hasUI: true,
				ui: { confirm: async () => false },
			}),
			/user denied approval/,
		);
		assert.equal(calls.filter((name) => name === "engraphis_execute_action").length, 0);

		await assert.rejects(
			execute.execute("action", params, undefined, undefined, {
				hasUI: true,
				ui: { confirm: async () => true },
			}),
			/not issued by the current Engraphis discovery session/,
		);

		await discover.execute("discover", { task: "securely erase a leaked memory" }, undefined);
		let prompt = "";
		await execute.execute("action", params, undefined, undefined, {
			hasUI: true,
			ui: {
				confirm: async (_title: string, message: string) => {
					prompt = message;
					return true;
				},
			},
		});
		assert.match(prompt, /secure_erase; destructive/);
		assert.equal(calls.filter((name) => name === "engraphis_execute_action").length, 1);
	} finally {
		EngraphisMcpClient.prototype.callTool = original;
	}
});

test("fails closed when Pi cannot present an action approval dialog", async () => {
	const original = EngraphisMcpClient.prototype.callTool;
	EngraphisMcpClient.prototype.callTool = async function (name: string) {
		return name === "engraphis_discover_actions"
			? { content: [{ type: "text", text: JSON.stringify({ actions: [{
				capability_id: "cap_noninteractive",
				canonical_action: "record_event",
				schema_digest: "abcdef1234567890",
				side_effect: "write",
				title: "Record an event",
			}] }) }] }
			: { content: [{ type: "text", text: "{}" }] };
	};
	try {
		const { tools } = extensionHarness();
		await tools.find((tool) => tool.name === "engraphis_discover_actions")!
			.execute("discover", { task: "record an event" }, undefined);
		await assert.rejects(
			tools.find((tool) => tool.name === "engraphis_execute_action")!.execute(
				"action",
				{ arguments: {}, capability_id: "cap_noninteractive", schema_digest: "abcdef1234567890" },
				undefined,
				undefined,
				{ hasUI: false, ui: {} },
			),
			/cannot request user approval/,
		);
	} finally {
		EngraphisMcpClient.prototype.callTool = original;
	}
});
