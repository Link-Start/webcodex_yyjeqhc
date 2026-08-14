//! Bounded independent text searches built from the canonical single-search core.

use super::files::{SearchOptions, SearchRequest};
use super::project_resolution::ResolvedProject;
use super::{SearchProjectTextsQuery, ToolResult, ToolRuntime};
use futures_util::{stream, StreamExt};
use serde_json::{json, Value};
use std::time::Duration;
use tokio::time::Instant;
use webcodex_workspace::file_read_normalize::MODEL_RESULT_ENVELOPE_RESERVE_BYTES;
use webcodex_workspace::file_read_range::MAX_SERIALIZED_OUTPUT_BYTES;

pub(crate) const MAX_SEARCH_PROJECT_TEXTS_QUERIES: usize = 8;
pub(crate) const MAX_SEARCH_PROJECT_TEXTS_CONCURRENCY: usize = 2;
pub(crate) const DEFAULT_SEARCH_PROJECT_TEXTS_DEADLINE: Duration = Duration::from_secs(30);

impl From<SearchProjectTextsQuery> for SearchRequest {
    fn from(query: SearchProjectTextsQuery) -> Self {
        Self {
            pattern: query.pattern,
            path: query.path,
            limit: query.limit,
            context_before: query.context_before,
            context_after: query.context_after,
            include_globs: query.include_globs,
            exclude_globs: query.exclude_globs,
            result_mode: query.result_mode,
            timeout_secs: query.timeout_secs,
        }
    }
}

fn batch_output(
    project: &str,
    requested_count: usize,
    items: Vec<Value>,
    output_truncated: bool,
    next_index: Option<usize>,
) -> Value {
    let succeeded_count = items
        .iter()
        .filter(|item| item["success"].as_bool() == Some(true))
        .count();
    let returned_count = items.len();
    json!({
        "project": project,
        "requested_count": requested_count,
        "returned_count": returned_count,
        "succeeded_count": succeeded_count,
        "failed_count": returned_count - succeeded_count,
        "items": items,
        "output_truncated": output_truncated,
        "next_index": next_index,
    })
}

fn serialized_batch_fits(output: &Value) -> bool {
    serde_json::to_vec(&ToolResult::ok(output.clone()))
        .map(|bytes| {
            bytes.len()
                <= MAX_SERIALIZED_OUTPUT_BYTES.saturating_sub(MODEL_RESULT_ENVELOPE_RESERVE_BYTES)
        })
        .unwrap_or(false)
}

fn retryable_agent_request_failure(result: &ToolResult) -> bool {
    !result.success
        && result.output.get("code").and_then(Value::as_str) == Some("search_request_dropped")
}

fn apply_output_budget(project: &str, requested_count: usize, completed: Vec<Value>) -> Value {
    let mut returned = Vec::with_capacity(completed.len());
    let mut next_index = None;

    for item in completed {
        let index = item["index"].as_u64().unwrap_or(returned.len() as u64) as usize;
        let mut candidate_items = returned.clone();
        candidate_items.push(item.clone());
        let candidate = batch_output(project, requested_count, candidate_items, false, None);
        if !serialized_batch_fits(&candidate) {
            next_index = Some(index);
            break;
        }
        returned.push(item);
    }

    batch_output(
        project,
        requested_count,
        returned,
        next_index.is_some(),
        next_index,
    )
}

fn failure_reason_code(result: &ToolResult) -> &'static str {
    match result.output.get("code").and_then(Value::as_str) {
        Some("invalid_search_request") => {
            match result.output.get("field").and_then(Value::as_str) {
                Some("pattern") => "invalid_pattern",
                Some("path") => "invalid_path",
                Some("include_globs" | "exclude_globs") => "invalid_glob",
                _ => "invalid_search_request",
            }
        }
        Some("search_timeout") => "timeout",
        Some("search_backend_feature_unavailable") => "search_backend_feature_unavailable",
        Some("search_execution_failed") => "search_execution_failed",
        Some("search_request_dropped") => "search_request_dropped",
        _ if result.output.get("format").and_then(Value::as_str)
            == Some("webcodex.external_provider_error.v1") =>
        {
            "external_provider_error"
        }
        _ => "agent_unavailable",
    }
}

fn batch_item(index: usize, mut result: ToolResult) -> Value {
    if result.success {
        if let Some(output) = result.output.as_object_mut() {
            // Project identity and Session/permission metadata are outer-batch
            // concerns. The input index identifies the original pattern.
            for key in [
                "project",
                "pattern",
                "session_recorded",
                "session_id",
                "session_event_id",
                "session_hint",
                "permission",
            ] {
                output.remove(key);
            }
        }
        return json!({
            "index": index,
            "success": true,
            "output": result.output,
            "error": null,
        });
    }

    let reason_code = failure_reason_code(&result);
    json!({
        "index": index,
        "success": false,
        "output": {
            "error_kind": "search_project_text_failed",
            "reason_code": reason_code,
            "state_changed": false,
        },
        "error": format!("search_project_text failed: {reason_code}"),
    })
}

impl ToolRuntime {
    pub(crate) async fn search_project_texts(
        &self,
        project: String,
        queries: Vec<SearchProjectTextsQuery>,
    ) -> ToolResult {
        let resolved = match self.resolve_project_input(&project).await {
            Ok(project) => project,
            Err(error) => return error.into_tool_result(),
        };
        self.search_project_texts_resolved(&resolved, queries).await
    }

    pub(crate) async fn search_project_texts_resolved(
        &self,
        resolved: &ResolvedProject,
        queries: Vec<SearchProjectTextsQuery>,
    ) -> ToolResult {
        if !(1..=MAX_SEARCH_PROJECT_TEXTS_QUERIES).contains(&queries.len()) {
            return ToolResult::err("search_project_texts requires 1 to 8 queries");
        }

        let runtime_project_id = resolved.resolved_id.clone();
        let requested_count = queries.len();
        let deadline = Instant::now() + self.search_project_texts_deadline;

        // Validation, Runner enqueue, and response waiting all happen inside
        // the concurrency slot. A third query cannot enter the Runner queue
        // while two earlier queries still hold their slots.
        let mut completed: Vec<Value> =
            stream::iter(queries.into_iter().enumerate().map(|(index, query)| {
                let project = &resolved.config;
                let output_project = runtime_project_id.as_str();
                async move {
                    let result = match SearchOptions::normalize(query.into()) {
                        Ok(options) if project.is_agent() => {
                            let first = self
                                .search_one_resolved_project_text(
                                    project,
                                    output_project,
                                    options.clone(),
                                    Some(deadline),
                                )
                                .await;
                            if retryable_agent_request_failure(&first) && Instant::now() < deadline
                            {
                                self.search_one_resolved_project_text(
                                    project,
                                    output_project,
                                    options,
                                    Some(deadline),
                                )
                                .await
                            } else {
                                first
                            }
                        }
                        Ok(options) => {
                            self.search_one_resolved_project_text(
                                project,
                                output_project,
                                options,
                                Some(deadline),
                            )
                            .await
                        }
                        Err(error) => error.into_tool_result(),
                    };
                    batch_item(index, result)
                }
            }))
            .buffer_unordered(MAX_SEARCH_PROJECT_TEXTS_CONCURRENCY)
            .collect()
            .await;
        completed.sort_by_key(|item| item["index"].as_u64().unwrap_or(u64::MAX));

        ToolResult::ok(apply_output_budget(
            &runtime_project_id,
            requested_count,
            completed,
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn output_budget_keeps_whole_items_and_reserves_outer_metadata_space() {
        let item = |index, text: String| {
            json!({
                "index": index,
                "success": true,
                "output": {
                    "backend": "rg",
                    "result_mode": "matches",
                    "count": 1,
                    "matches": [{
                        "path": format!("src/{index}.rs"),
                        "line": 1,
                        "preview": text,
                        "context_before": [],
                        "context_after": []
                    }],
                    "truncated": false,
                    "truncation_reason": null
                },
                "error": null
            })
        };
        let output = apply_output_budget(
            "agent:oe:demo",
            3,
            vec![
                item(0, "x".repeat(120 * 1024)),
                item(1, "y".repeat(120 * 1024)),
                item(2, "z".repeat(120 * 1024)),
            ],
        );
        assert_eq!(output["returned_count"], 2);
        assert_eq!(output["next_index"], 2);
        assert_eq!(output["output_truncated"], true);

        let mut result = ToolResult::ok(output);
        result.output["session_recorded"] = json!(true);
        result.output["session_id"] = json!(format!("wc_sess_{}", "s".repeat(64)));
        result.output["session_event_id"] = json!(format!("evt_{}", "e".repeat(64)));
        result.output["session_hint"] = json!({
            "has_open_messages": true,
            "open_counts": {
                "guidance": u64::MAX,
                "question": u64::MAX,
                "todo": u64::MAX,
                "risk": u64::MAX
            },
            "highest_priority": "high",
            "suggested_next_tool": "session_discussion_summary"
        });
        assert!(serde_json::to_vec(&result).unwrap().len() <= MAX_SERIALIZED_OUTPUT_BYTES);
    }
}
