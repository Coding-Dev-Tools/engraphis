import { Type } from "typebox";

import type { EngraphisRuntimeConfig } from "./config.ts";

import { SMART_SCHEMAS } from "./generated-contract.ts";

export const SESSION_PARAMETERS = Type.Unsafe<Record<string, unknown>>(SMART_SCHEMAS.engraphis_session);
export const RECALL_CONTEXT_PARAMETERS = Type.Unsafe<Record<string, unknown>>(SMART_SCHEMAS.engraphis_recall_context);
export const REMEMBER_PARAMETERS = Type.Unsafe<Record<string, unknown>>(SMART_SCHEMAS.engraphis_remember);
export const GET_MEMORY_PARAMETERS = Type.Unsafe<Record<string, unknown>>(SMART_SCHEMAS.engraphis_get_memory);
export const UPDATE_MEMORY_PARAMETERS = Type.Unsafe<Record<string, unknown>>(SMART_SCHEMAS.engraphis_update_memory);
export const CONFLICT_REVIEW_PARAMETERS = Type.Unsafe<Record<string, unknown>>(SMART_SCHEMAS.engraphis_conflict_review);
export const DISCOVER_ACTIONS_PARAMETERS = Type.Unsafe<Record<string, unknown>>(SMART_SCHEMAS.engraphis_discover_actions);
export const EXECUTE_READ_PARAMETERS = Type.Unsafe<Record<string, unknown>>(SMART_SCHEMAS.engraphis_execute_read);
export const EXECUTE_ACTION_PARAMETERS = Type.Unsafe<Record<string, unknown>>(SMART_SCHEMAS.engraphis_execute_action);

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
