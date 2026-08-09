import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import test from "node:test";

import { CORE_DIRECT_TOOLS, buildEngraphisRuntimeConfig } from "../src/config.ts";
import {
	CONFLICT_REVIEW_PARAMETERS,
	GET_MEMORY_PARAMETERS,
	UPDATE_MEMORY_PARAMETERS,
	applyScopeDefaults,
} from "../src/tool-schemas.ts";

const execFileAsync = promisify(execFile);

test("uses the public server entry point by default", () => {
	assert.deepEqual(buildEngraphisRuntimeConfig({}), { command: "engraphis-mcp", environment: {} });
});

test("reads scoped defaults and a server-command override", () => {
	assert.deepEqual(
		buildEngraphisRuntimeConfig({
			ENGRAPHIS_MCP_COMMAND: "C:/venv/Scripts/engraphis-mcp.exe",
			ENGRAPHIS_REPO: "backend",
			ENGRAPHIS_WORKSPACE: "acme",
		}),
		{
			command: "C:/venv/Scripts/engraphis-mcp.exe",
			defaultRepo: "backend",
			defaultWorkspace: "acme",
			environment: {
				ENGRAPHIS_MCP_COMMAND: "C:/venv/Scripts/engraphis-mcp.exe",
				ENGRAPHIS_REPO: "backend",
				ENGRAPHIS_WORKSPACE: "acme",
			},
		},
	);
});

test("forwards only explicitly scoped Engraphis settings to the MCP server", () => {
	const config = buildEngraphisRuntimeConfig({
		ENGRAPHIS_DB_PATH: "C:/data/engraphis.db",
		UNRELATED_SECRET: "do-not-forward",
	});

	assert.deepEqual(config.environment, { ENGRAPHIS_DB_PATH: "C:/data/engraphis.db" });
});

test("preserves only the runtime variables required to launch the public command", () => {
	const config = buildEngraphisRuntimeConfig({
		PATH: "/venv/bin:/usr/bin",
		Path: "C:/venv/Scripts;C:/Windows/System32",
		SystemRoot: "C:/Windows",
		ComSpec: "C:/Windows/System32/cmd.exe",
		UNRELATED_SECRET: "do-not-forward",
	});

	assert.deepEqual(config.environment, {
		PATH: "/venv/bin:/usr/bin",
		Path: "C:/venv/Scripts;C:/Windows/System32",
		SystemRoot: "C:/Windows",
		ComSpec: "C:/Windows/System32/cmd.exe",
	});
});

test("ignores whitespace-only optional configuration", () => {
	const config = buildEngraphisRuntimeConfig({
		ENGRAPHIS_MCP_COMMAND: "\t",
		ENGRAPHIS_REPO: " ",
		ENGRAPHIS_WORKSPACE: "  ",
	});

	assert.equal(config.command, "engraphis-mcp");
	assert.equal(config.defaultRepo, undefined);
	assert.equal(config.defaultWorkspace, undefined);
});

test("keeps exactly the nine Smart MCP tools in the direct surface", () => {
	assert.deepEqual(CORE_DIRECT_TOOLS, [
		"engraphis_session",
		"engraphis_recall_context",
		"engraphis_remember",
		"engraphis_discover_actions",
		"engraphis_execute_read",
		"engraphis_execute_action",
		"engraphis_get_memory",
		"engraphis_update_memory",
		"engraphis_conflict_review",
	]);
});

test("matches the three governed Smart tool schemas", () => {
	const getMemory = GET_MEMORY_PARAMETERS as Record<string, any>;
	const updateMemory = UPDATE_MEMORY_PARAMETERS as Record<string, any>;
	const conflictReview = CONFLICT_REVIEW_PARAMETERS as Record<string, any>;

	assert.deepEqual(Object.keys(getMemory.properties).sort(), ["memory_id", "repo", "workspace"]);
	assert.deepEqual(getMemory.required, ["memory_id"]);
	assert.deepEqual(
		Object.keys(updateMemory.properties).sort(),
		["actor", "importance", "memory_id", "mtype", "repo", "title", "workspace"],
	);
	assert.deepEqual(updateMemory.required, ["memory_id"]);
	assert.deepEqual(
		Object.keys(conflictReview.properties).sort(),
		["limit", "repo", "workspace"],
	);
	assert.deepEqual(conflictReview.required ?? [], []);
	assert.equal(getMemory.properties.workspace.default, "default");
	assert.equal(updateMemory.properties.repo.default, null);
	assert.equal(conflictReview.properties.limit.default, 50);
});

test("applies configured repo only inside its configured workspace", () => {
	assert.deepEqual(
		applyScopeDefaults({ query: "decision" }, { command: "engraphis-mcp", environment: {} }),
		{ query: "decision" },
	);
	assert.deepEqual(
		applyScopeDefaults(
			{ query: "decision" },
			{
				command: "engraphis-mcp",
				defaultRepo: "backend",
				defaultWorkspace: "acme",
				environment: {},
			},
		),
		{ query: "decision", repo: "backend", workspace: "acme" },
	);
	assert.deepEqual(
		applyScopeDefaults(
			{ query: "decision" },
			{ command: "engraphis-mcp", defaultRepo: "backend", environment: {} },
		),
		{ query: "decision" },
	);
	assert.deepEqual(
		applyScopeDefaults(
			{ query: "decision", workspace: "other" },
			{
				command: "engraphis-mcp",
				defaultRepo: "backend",
				defaultWorkspace: "acme",
				environment: {},
			},
		),
		{ query: "decision", workspace: "other" },
	);
});

test("publishes canonical Engraphis repository metadata", async () => {
	const packageJson = JSON.parse(
		await readFile(new URL("../package.json", import.meta.url), "utf8"),
	);
	assert.equal(
		packageJson.repository.url,
		"git+https://github.com/Coding-Dev-Tools/engraphis.git",
	);
	assert.equal(packageJson.bugs.url, "https://github.com/Coding-Dev-Tools/engraphis/issues");
	assert.equal(
		packageJson.homepage,
		"https://github.com/Coding-Dev-Tools/engraphis/tree/main/integrations/pi",
	);
});

test("the npm tarball carries the Apache license and applicable notice", async () => {
	assert.ok(process.env.npm_execpath, "npm_execpath is required for the package-artifact test");
	const { stdout } = await execFileAsync(
		process.execPath,
		[process.env.npm_execpath, "pack", "--dry-run", "--ignore-scripts", "--json"],
		{
			cwd: fileURLToPath(new URL("..", import.meta.url)),
			encoding: "utf8",
		},
	);
	const packed = JSON.parse(stdout)[0];
	const files = new Set(packed.files.map((entry: { path: string }) => entry.path));
	assert.ok(files.has("LICENSE"));
	assert.ok(files.has("NOTICE"));
	assert.ok(files.has("npm-shrinkwrap.json"));
	assert.equal(files.has("test/config.test.ts"), false);
});
