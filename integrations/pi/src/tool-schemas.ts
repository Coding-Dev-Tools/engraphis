import { Type } from "typebox";

import type { EngraphisRuntimeConfig } from "./config.ts";

const OPTIONAL_REPO = Type.Optional(Type.Union([
	Type.String({ description: "Repository scope within the workspace.", maxLength: 200 }),
	Type.Null(),
], { default: null }));

const WRITABLE_SCOPE = {
	repo: OPTIONAL_REPO,
	workspace: Type.Optional(Type.String({ default: "default", description: "Top-level memory workspace.", maxLength: 200 })),
};

const RECALL_SCOPE = {
	repo: OPTIONAL_REPO,
	workspace: Type.Optional(Type.Union([
		Type.String({ description: "Optional workspace; omit for local cross-workspace recall.", maxLength: 200 }),
		Type.Null(),
	], { default: null })),
};

/** The Smart session tool starts/resumes and ends sessions with one stable schema. */
export const SESSION_PARAMETERS = Type.Object({
	...WRITABLE_SCOPE,
	action: Type.Optional(Type.Union([
		Type.Literal("start"),
		Type.Literal("end"),
	], { default: "start", description: "Start/resume work or save its handoff." })),
	agent: Type.Optional(Type.String({ default: "pi", description: "Agent label. Defaults to pi.", maxLength: 200 })),
	force_new: Type.Optional(Type.Boolean({ default: false, description: "Start a new session instead of reusing an exact active session." })),
	goal: Type.Optional(Type.String({ default: "", description: "What this session is trying to accomplish.", maxLength: 1_000 })),
	session_id: Type.Optional(Type.String({ default: "", description: "Session id required when action is end.", maxLength: 200 })),
	summary: Type.Optional(Type.String({ default: "", description: "Concise handoff for the next session.", maxLength: 100_000 })),
	outcome: Type.Optional(Type.String({ default: "", description: "Short outcome, such as shipped or blocked.", maxLength: 1_000 })),
	open_threads: Type.Optional(Type.Union([
		Type.Array(Type.String({ description: "Unresolved item to carry forward." })),
		Type.Null(),
	], { default: null })),
	token_budget: Type.Optional(Type.Integer({ default: 512, description: "Goal-context token budget (0-32768).", minimum: 0, maximum: 32768 })),
});

export const RECALL_CONTEXT_PARAMETERS = Type.Object({
	...RECALL_SCOPE,
	k: Type.Optional(Type.Integer({ default: 8, description: "Candidate-memory limit (1-50).", minimum: 1, maximum: 50 })),
	query: Type.String({ description: "The prior context needed for the current task.", minLength: 1, maxLength: 100_000 }),
	session_id: Type.Optional(Type.Union([
		Type.String({ description: "Active Engraphis session id, if known." }),
		Type.Null(),
	], { default: null })),
	token_budget: Type.Optional(Type.Integer({ default: 1_024, description: "Maximum packed-context tokens (0-32768).", minimum: 0, maximum: 32768 })),
});

export const REMEMBER_PARAMETERS = Type.Object({
	...WRITABLE_SCOPE,
	content: Type.String({ description: "Durable fact, decision, preference, bug cause/fix, or reusable procedure.", minLength: 1, maxLength: 100_000 }),
	importance: Type.Optional(Type.Number({ default: 0, description: "Salience from 0 to 1.", minimum: 0, maximum: 1 })),
	mtype: Type.Optional(Type.Union([
		Type.Literal("semantic"),
		Type.Literal("episodic"),
		Type.Literal("procedural"),
		Type.Literal("working"),
	], { default: "semantic" })),
	session_id: Type.Optional(Type.Union([
		Type.String({ description: "Active Engraphis session id, if known." }),
		Type.Null(),
	], { default: null })),
});

export const GET_MEMORY_PARAMETERS = Type.Object({
	...WRITABLE_SCOPE,
	memory_id: Type.String({ description: "Memory id to read.", minLength: 1, maxLength: 200 }),
});

export const UPDATE_MEMORY_PARAMETERS = Type.Object({
	...WRITABLE_SCOPE,
	memory_id: Type.String({ description: "Memory id to edit.", minLength: 1, maxLength: 200 }),
	title: Type.Optional(Type.Union([
		Type.String({ description: "Replacement title.", maxLength: 500 }),
		Type.Null(),
	], { default: null })),
	mtype: Type.Optional(Type.Union([
		Type.Literal("semantic"),
		Type.Literal("episodic"),
		Type.Literal("procedural"),
		Type.Literal("working"),
		Type.Null(),
	], { default: null })),
	importance: Type.Optional(Type.Union([
		Type.Number({ description: "Replacement salience from 0 to 1.", minimum: 0, maximum: 1 }),
		Type.Null(),
	], { default: null })),
	actor: Type.Optional(Type.String({ default: "user", description: "Audit actor label.", maxLength: 200 })),
});

export const CONFLICT_REVIEW_PARAMETERS = Type.Object({
	...WRITABLE_SCOPE,
	limit: Type.Optional(Type.Integer({ default: 50, description: "Maximum review items (1-100).", minimum: 1, maximum: 100 })),
});

export const DISCOVER_ACTIONS_PARAMETERS = Type.Object({
	task: Type.String({ description: "Describe the advanced capability needed without pasting memory content.", minLength: 1, maxLength: 2_000 }),
	category: Type.Optional(Type.Union([
		Type.Literal("memory"),
		Type.Literal("governance"),
		Type.Literal("code"),
		Type.Literal("audit"),
		Type.Literal("ops"),
	], { default: "", maxLength: 100 })),
	intent: Type.Optional(Type.Union([
		Type.Literal("any"),
		Type.Literal("read"),
		Type.Literal("write"),
		Type.Literal("admin"),
		Type.Literal("destructive"),
	], { default: "any" })),
	limit: Type.Optional(Type.Integer({ default: 1, description: "Number of matching actions (1-3).", minimum: 1, maximum: 3 })),
});

const EXECUTE_PARAMETERS = {
	capability_id: Type.String({ description: "Capability id returned by engraphis_discover_actions.", minLength: 8, maxLength: 128 }),
	schema_digest: Type.String({ description: "Schema digest returned by engraphis_discover_actions.", minLength: 8, maxLength: 128 }),
	arguments: Type.Record(Type.String(), Type.Unknown({ description: "Arguments matching the discovered action schema." })),
};

export const EXECUTE_READ_PARAMETERS = Type.Object(EXECUTE_PARAMETERS);

export const EXECUTE_ACTION_PARAMETERS = Type.Object(EXECUTE_PARAMETERS);

/** Add explicit configured defaults without overriding a model-supplied scope. */
export function applyScopeDefaults(
	params: Record<string, unknown>,
	config: EngraphisRuntimeConfig,
	extra: Record<string, unknown> = {},
): Record<string, unknown> {
	const result = { ...extra, ...params };
	if (result.workspace === undefined && config.defaultWorkspace) {
		result.workspace = config.defaultWorkspace;
	}
	if (
		result.repo === undefined &&
		config.defaultRepo &&
		config.defaultWorkspace &&
		result.workspace === config.defaultWorkspace
	) {
		result.repo = config.defaultRepo;
	}
	return result;
}
