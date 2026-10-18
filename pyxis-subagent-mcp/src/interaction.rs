//! Validate host answers before they reach a native permission/question seam.
//! Native services retain final authority over their full response contract.
use crate::config::Harness;
use anyhow::{Context, Result, bail, ensure};
use serde_json::{Value, json};
use std::collections::BTreeSet;

pub fn validate(harness: Harness, request: &Value, mut answer: Value) -> Result<Value> {
    ensure!(answer.is_object(), "response must be an object");
    ensure!(
        answer.to_string().len() < 1024 * 1024,
        "answer exceeds 1 MiB"
    );
    if harness == Harness::Claude {
        let input = &request["request"]["input"];
        if request["request"]["tool_name"] == "AskUserQuestion" && answer.get("answers").is_some() {
            let answers = answer["answers"]
                .as_object()
                .context("answers must be an object")?;
            let questions = input["questions"]
                .as_array()
                .context("missing question constraints")?;
            ensure!(
                answers.len() == questions.len(),
                "answer each question exactly once"
            );
            for q in questions {
                let text = q["question"].as_str().context("question text missing")?;
                ensure!(
                    answers.get(text).is_some_and(Value::is_string),
                    "missing string answer for {text}"
                );
            }
            let mut updated = input.clone();
            updated["answers"] = answer["answers"].clone();
            return Ok(json!({"behavior":"allow","updatedInput":updated}));
        }
        match answer["behavior"].as_str() {
            Some("allow") => {
                if answer.get("updatedInput").is_none() {
                    answer["updatedInput"] = input.clone();
                }
                ensure!(
                    answer["updatedInput"].is_object(),
                    "updatedInput must be an object"
                );
                if let Some(permissions) = answer.get("updatedPermissions") {
                    ensure!(
                        permissions.is_array(),
                        "updatedPermissions must be an array"
                    );
                }
            }
            Some("deny") => {
                ensure!(answer["message"].is_string(), "deny requires a message");
            }
            _ => bail!("Claude response requires behavior allow or deny"),
        }
        return Ok(answer);
    }
    let params = &request["params"];
    match request["method"].as_str().unwrap_or("") {
        "item/commandExecution/requestApproval" | "item/fileChange/requestApproval" => {
            let decision = &answer["decision"];
            ensure!(
                matches!(
                    decision.as_str(),
                    Some("accept" | "acceptForSession" | "decline" | "cancel")
                ) || decision.get("acceptWithExecpolicyAmendment").is_some(),
                "invalid approval decision"
            );
            if let Some(options) = params["availableDecisions"].as_array() {
                ensure!(
                    options.contains(decision),
                    "decision is not among the native availableDecisions"
                );
            }
        }
        "item/permissions/requestApproval" => {
            ensure!(
                answer["permissions"].is_object(),
                "permissions must be an object"
            );
            ensure!(
                answer.get("scope").is_none()
                    || matches!(answer["scope"].as_str(), Some("turn" | "session")),
                "scope must be turn or session"
            );
        }
        "item/tool/requestUserInput" => {
            let questions = params["questions"]
                .as_array()
                .context("missing native questions")?;
            let answers = answer["answers"]
                .as_object()
                .context("answers must be an object")?;
            ensure!(
                answers.len() == questions.len(),
                "answer each native question exactly once"
            );
            for question in questions {
                let id = question["id"].as_str().context("missing question id")?;
                let values = answers.get(id).context("missing question answer")?["answers"]
                    .as_array()
                    .context("each answer requires an answers string array")?;
                ensure!(
                    values.iter().all(Value::is_string),
                    "answers must contain strings"
                );
            }
        }
        "mcpServer/elicitation/request" => {
            ensure!(
                matches!(
                    answer["action"].as_str(),
                    Some("accept" | "decline" | "cancel")
                ),
                "invalid elicitation action"
            );
            if answer["action"] == "accept" && params["requestedSchema"].is_object() {
                validate_schema(&params["requestedSchema"], &answer["content"])?;
            }
        }
        "pyxis/interaction/approval" => {
            ensure!(
                matches!(
                    answer["outcome"].as_str(),
                    Some("allowed-once" | "rejected" | "cancelled")
                ),
                "DSH outcome must be allowed-once, rejected or cancelled"
            );
        }
        "pyxis/interaction/questions" => {
            let questions = params["questions"]
                .as_array()
                .context("missing native questions")?;
            let answers = answer["answers"]
                .as_array()
                .context("answers must be an array")?;
            let mut ids = BTreeSet::new();
            ensure!(
                questions.len() == answers.len(),
                "answer every question exactly once"
            );
            for answer in answers {
                let id = answer["id"].as_str().context("answer requires id")?;
                ensure!(ids.insert(id), "duplicate answer id");
                let question = questions
                    .iter()
                    .find(|q| q["id"] == id)
                    .context("unknown question id")?;
                let selected = answer["selected"]
                    .as_array()
                    .context("selected must be a string array")?;
                ensure!(
                    selected.iter().all(Value::is_string),
                    "selected must contain strings"
                );
                ensure!(
                    question["multiSelect"] == true || selected.len() <= 1,
                    "question permits only one selection"
                );
                if let Some(options) = question["options"].as_array() {
                    ensure!(
                        selected
                            .iter()
                            .all(|label| options.iter().any(|o| o["label"] == *label)),
                        "selected option was not offered"
                    );
                }
                ensure!(
                    answer.get("custom").is_none() || answer["custom"].is_string(),
                    "custom must be a string"
                );
            }
        }
        _ => bail!("unsupported interaction response"),
    }
    Ok(answer)
}

fn validate_schema(schema: &Value, value: &Value) -> Result<()> {
    if let Some(options) = schema["enum"].as_array() {
        ensure!(options.contains(value), "elicitation value is outside enum");
    }
    match schema["type"].as_str() {
        Some("object") => {
            let object = value
                .as_object()
                .context("elicitation content must be an object")?;
            if let Some(required) = schema["required"].as_array() {
                for key in required {
                    ensure!(
                        key.as_str().is_some_and(|k| object.contains_key(k)),
                        "missing required elicitation property {key}"
                    );
                }
            }
            if let Some(properties) = schema["properties"].as_object() {
                for (key, val) in object {
                    if let Some(property) = properties.get(key) {
                        validate_schema(property, val)?;
                    } else {
                        ensure!(
                            schema["additionalProperties"] != false,
                            "unknown elicitation property {key}"
                        );
                    }
                }
            }
        }
        Some("string") => {
            ensure!(value.is_string(), "elicitation value must be a string");
        }
        Some("boolean") => {
            ensure!(value.is_boolean(), "elicitation value must be boolean");
        }
        Some("integer") => {
            ensure!(
                value.is_i64() || value.is_u64(),
                "elicitation value must be integer"
            );
        }
        Some("number") => {
            ensure!(value.is_number(), "elicitation value must be number");
        }
        Some("array") => {
            for item in value
                .as_array()
                .context("elicitation value must be an array")?
            {
                validate_schema(&schema["items"], item)?;
            }
        }
        _ => {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn approval_does_not_accept_unoffered_decision() {
        let request = json!({"method":"item/commandExecution/requestApproval","params":{"availableDecisions":["decline","cancel"]}});
        assert!(validate(Harness::Codex, &request, json!({"decision":"accept"})).is_err());
        assert!(validate(Harness::Codex, &request, json!({"decision":"decline"})).is_ok());
    }
    #[test]
    fn structured_questions_reject_duplicates_and_invalid_selections() {
        let request = json!({"method":"pyxis/interaction/questions","params":{"questions":[{"id":"a","options":[{"label":"yes"}]}]}});
        assert!(
            validate(
                Harness::Dsh,
                &request,
                json!({"answers":[{"id":"a","selected":["no"]}]})
            )
            .is_err()
        );
        assert!(
            validate(
                Harness::Dsh,
                &request,
                json!({"answers":[{"id":"a","selected":["yes"]}]})
            )
            .is_ok()
        );
    }
}
