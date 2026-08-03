/** The zero-configuration Smart MCP surface visible to Pi agents. */
export const EXTENSION_VERSION = "0.1.0";

export const CORE_DIRECT_TOOLS = [
	"engraphis_session",
	"engraphis_recall_context",
	"engraphis_remember",
	"engraphis_discover_actions",
	"engraphis_execute_read",
	"engraphis_execute_action",
] as const;

type Environment = Readonly<Record<string, string | undefined>>;

export type EngraphisRuntimeConfig = {
	args?: string[];
	command: string;
	cwd?: string;
	defaultRepo?: string;
	defaultWorkspace?: string;
	environment: Record<string, string>;
};

function nonBlank(value: string | undefined): string | undefined {
	const normalized = value?.trim();
	return normalized || undefined;
}

/**
 * The MCP SDK intentionally starts child processes with a minimal safe environment.
 * Preserve only the executable lookup/runtime variables needed to launch the public
 * console script, plus Engraphis settings for its database and backend.  Forwarding
 * the complete Pi environment would unnecessarily expose unrelated credentials.
 */
function engraphisEnvironment(environment: Environment): Record<string, string> {
	const forwarded: Record<string, string> = {};
	for (const [key, value] of Object.entries(environment)) {
		if (
			typeof value === "string" &&
			(key.startsWith("ENGRAPHIS_") ||
				["PATH", "Path", "SystemRoot", "ComSpec"].includes(key))
		) {
			forwarded[key] = value;
		}
	}
	return forwarded;
}

/**
 * Return the local server configuration for the native Pi extension.
 *
 * `engraphis-mcp` is the public console entry point installed by
 * `pip install "engraphis[mcp]"`. Callers can override it for pipx, a virtual
 * environment, or development checkout through ENGRAPHIS_MCP_COMMAND. The server
 * receives the explicitly allowlisted Engraphis settings, so all clients can share one store.
 */
export function buildEngraphisRuntimeConfig(environment: Environment = process.env): EngraphisRuntimeConfig {
	const command = nonBlank(environment.ENGRAPHIS_MCP_COMMAND) ?? "engraphis-mcp";
	const config: EngraphisRuntimeConfig = {
		command,
		environment: engraphisEnvironment(environment),
	};

	const workspace = nonBlank(environment.ENGRAPHIS_WORKSPACE);
	const repo = nonBlank(environment.ENGRAPHIS_REPO);
	if (workspace) config.defaultWorkspace = workspace;
	if (repo) config.defaultRepo = repo;

	return config;
}
