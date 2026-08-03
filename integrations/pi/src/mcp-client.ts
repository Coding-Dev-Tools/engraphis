import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

import { CORE_DIRECT_TOOLS, EXTENSION_VERSION, type EngraphisRuntimeConfig } from "./config.ts";

export type McpTool = {
	description?: string;
	inputSchema: Record<string, unknown>;
	name: string;
};

export type McpResult = {
	content?: Array<{ text?: string; type: string }>;
	isError?: boolean;
	[key: string]: unknown;
};

export type DiscoveredAction = {
	canonicalAction: string;
	capabilityId: string;
	schemaDigest: string;
	sideEffect: "write" | "admin" | "destructive";
	title: string;
};

/** A tool-level rejection returned by the MCP server, as opposed to a transport failure. */
export class EngraphisMcpToolError extends Error {
	constructor(readonly publicMessage: string) {
		super(publicMessage);
		this.name = "EngraphisMcpToolError";
	}
}

export class EngraphisCompatibilityError extends Error {
	constructor(readonly publicMessage: string) {
		super(publicMessage);
		this.name = "EngraphisCompatibilityError";
	}
}

// The default MCP timeout is one minute. A local model's cold start or an intentional
// repository index can reasonably take longer, while Pi can still cancel through its signal.
const TOOL_REQUEST_TIMEOUT_MS = 5 * 60 * 1_000;

/** A session-owned connection to the local Engraphis MCP process. */
export class EngraphisMcpClient {
	private client: Client | undefined;
	private connectAbort: AbortController | undefined;
	private connecting: Promise<Client> | undefined;
	private diagnostic = "";
	private lifecycle = 0;
	private tools: McpTool[] | undefined;

	constructor(private readonly config: EngraphisRuntimeConfig) {}

	async connect(): Promise<Client> {
		if (this.client) return this.client;
		if (this.connecting) return this.connecting;

		const generation = this.lifecycle;
		const controller = new AbortController();
		const connection = this.open(controller.signal);
		this.connectAbort = controller;
		this.connecting = connection;
		try {
			const client = await connection;
			if (generation !== this.lifecycle) {
				await client.close().catch(() => undefined);
				throw new Error("Engraphis connection closed during startup.");
			}
			this.client = client;
			return client;
		} finally {
			if (this.connecting === connection) this.connecting = undefined;
			if (this.connectAbort === controller) this.connectAbort = undefined;
		}
	}

	async close(): Promise<void> {
		this.lifecycle += 1;
		this.connectAbort?.abort();
		this.connectAbort = undefined;
		const client = this.client;
		const connecting = this.connecting;
		this.client = undefined;
		this.tools = undefined;
		if (client) await client.close();
		if (connecting) {
			const openingClient = await connecting.catch(() => undefined);
			if (openingClient && openingClient !== client) {
				await openingClient.close().catch(() => undefined);
			}
		}
	}

	async callTool(name: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<McpResult> {
		return this.withClient(async (client) =>
			(await client.callTool(
				{ name, arguments: args },
				undefined,
				{ signal, timeout: TOOL_REQUEST_TIMEOUT_MS },
			)) as McpResult,
		);
	}

	diagnosticHint(): string | undefined {
		if (/python 3\.10|requires python 3\.10/i.test(this.diagnostic)) {
			return "The Engraphis MCP server requires Python 3.10 or later.";
		}
		if (/no module named ["']?mcp/i.test(this.diagnostic)) {
			return "The Engraphis MCP dependency is missing. Install `engraphis[mcp]>=1.4.0,<2`.";
		}
		if (/no module named ["']?engraphis/i.test(this.diagnostic)) {
			return "Engraphis is not installed for the configured MCP command.";
		}
		return undefined;
	}

	async status(): Promise<Record<string, unknown>> {
		const tools = await this.withClient((client) => this.listTools(client));
		return { connected: true, server: "engraphis", toolCount: tools.length };
	}

	async searchTools(query: string): Promise<Record<string, unknown>> {
		const normalized = query.trim().toLowerCase();
		const tools = await this.withClient((client) => this.listTools(client));
		const matches = !normalized
			? tools
			: tools.filter((tool) => `${tool.name} ${tool.description ?? ""}`.toLowerCase().includes(normalized));
		return {
			count: matches.length,
			tools: matches.map(({ name, description }) =>
				normalized ? { name, description } : { name },
			),
		};
	}

	async describeTool(name: string): Promise<Record<string, unknown>> {
		if (!name.trim()) throw new Error("Specify a tool name to describe.");
		const tools = await this.withClient((client) => this.listTools(client));
		const tool = tools.find((candidate) => candidate.name === name);
		if (!tool) throw new Error(`Engraphis does not expose a tool named '${name}'.`);
		return { tool };
	}

	private async open(signal: AbortSignal): Promise<Client> {
		this.diagnostic = "";
		const client = new Client(
			{ name: "@engraphis/pi", version: EXTENSION_VERSION },
			{ capabilities: {} },
		);
		try {
			const transport = new StdioClientTransport({
				command: this.config.command,
				args: this.config.args,
				cwd: this.config.cwd,
				env: this.config.environment,
				// Keep diagnostics out of Pi's TUI while retaining only a bounded buffer
				// for allowlisted, non-sensitive setup hints.
				stderr: "pipe",
			});
			transport.stderr?.on("data", (chunk) => {
				this.diagnostic = (this.diagnostic + String(chunk)).slice(-4_096);
			});
			await client.connect(
				transport,
				{ signal, timeout: 60_000 },
			);
			const tools = await this.listTools(client, signal);
			const available = new Set(tools.map((tool) => tool.name));
			const missing = CORE_DIRECT_TOOLS.filter((name) => !available.has(name));
			if (missing.length) {
				throw new EngraphisCompatibilityError(
					`Engraphis 1.4.x Smart MCP is required; the server is missing: ${missing.join(", ")}.`,
				);
			}
			return client;
		} catch (error) {
			await client.close().catch(() => undefined);
			throw error;
		}
	}

	/** Reset an unhealthy stdio connection so the next Pi tool call can start a fresh server. */
	private async withClient<T>(operation: (client: Client) => Promise<T>): Promise<T> {
		try {
			return await operation(await this.connect());
		} catch (error) {
			await this.close().catch(() => undefined);
			throw error;
		}
	}

	private async listTools(client: Client, signal?: AbortSignal): Promise<McpTool[]> {
		if (this.tools) return this.tools;
		const all: McpTool[] = [];
		let cursor: string | undefined;
		do {
			const page = await client.listTools(
				cursor ? { cursor } : undefined,
				{ signal, timeout: 60_000 },
			);
			all.push(...(page.tools as McpTool[]));
			cursor = page.nextCursor;
		} while (cursor);
		this.tools = all;
		return all;
	}
}

/** Convert an MCP result into Pi's standard text result without losing structured details. */
export function formatMcpResult(result: unknown) {
	const payload = result as McpResult;
	const text = payload.content
		?.filter((block) => block.type === "text" && typeof block.text === "string")
		.map((block) => block.text)
		.join("\n\n");
	const normalizedText = text?.trim();
	const declaredError = normalizedText?.match(/^Error:\s*([a-z0-9_]+)\s*$/i);
	// Classic handlers return a deliberately anchored plain-text ``Error:`` envelope
	// for semantic rejections. Its details are not safe to expose to the model, and
	// the payload can contain spaces, quoted IDs, or other non-token text.
	const serverError = /^Error:/i.test(normalizedText ?? "");
	if (payload.isError || serverError) {
		const message = declaredError
			? `Engraphis rejected the request: ${declaredError[1]}.`
			: "Engraphis rejected the request. Verify the parameters and inspect the local Engraphis logs.";
		throw new EngraphisMcpToolError(message);
	}
	return {
		content: [{ type: "text" as const, text: text || JSON.stringify(result, null, 2) }],
		details: result,
	};
}

function cleanLabel(value: unknown, fallback: string): string {
	if (typeof value !== "string") return fallback;
	const cleaned = value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim();
	return cleaned.slice(0, 200) || fallback;
}

/** Extract server-issued action metadata used only to render and bind Pi's approval gate. */
export function discoveredActionsFromResult(result: unknown): DiscoveredAction[] {
	const payload = result as McpResult;
	const actions: DiscoveredAction[] = [];
	for (const block of payload.content ?? []) {
		if (block.type !== "text" || typeof block.text !== "string") continue;
		let parsed: unknown;
		try {
			parsed = JSON.parse(block.text);
		} catch {
			continue;
		}
		const candidates = (parsed as { actions?: unknown })?.actions;
		if (!Array.isArray(candidates)) continue;
		for (const candidate of candidates) {
			if (!candidate || typeof candidate !== "object") continue;
			const item = candidate as Record<string, unknown>;
			if (
				typeof item.capability_id !== "string" ||
				typeof item.schema_digest !== "string" ||
				!item.capability_id.startsWith("cap_") ||
				item.capability_id.length > 128 ||
				item.schema_digest.length < 8 ||
				item.schema_digest.length > 128 ||
				!(["write", "admin", "destructive"] as unknown[]).includes(item.side_effect)
			) continue;
			const canonicalAction = cleanLabel(item.canonical_action, "advanced action");
			actions.push({
				canonicalAction,
				capabilityId: item.capability_id,
				schemaDigest: item.schema_digest,
				sideEffect: item.side_effect as DiscoveredAction["sideEffect"],
				title: cleanLabel(item.title, canonicalAction),
			});
		}
	}
	return actions;
}

/** Avoid surfacing stack traces or inherited environment details to the model. */
export function safeErrorMessage(error: unknown): string {
	if (error instanceof EngraphisCompatibilityError) return error.publicMessage;
	if (error instanceof EngraphisMcpToolError) return error.publicMessage;
	if (error instanceof Error) {
		if (error.name === "AbortError") return error.message;
		if (
			error.message.startsWith("Specify a tool name") ||
			error.message.startsWith("Specify `tool`") ||
			error.message.startsWith("`args` must") ||
			error.message.startsWith("Engraphis does not expose")
		) {
			return error.message;
		}
	}
	return "Engraphis is unavailable. Verify `pip install \"engraphis[mcp]>=1.4.0,<2\"` and ENGRAPHIS_MCP_COMMAND.";
}
