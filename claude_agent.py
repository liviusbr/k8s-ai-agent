"""
claude_agent.py — turns a plain-English request into either a Kubernetes
manifest (for creating/deploying something new) or a direct action (for
deleting, scaling, or restarting something that already exists), using the
Claude API.

Never silently fails: if Claude's output can't be parsed, or describes an
action outside the allowed scope, that's surfaced as an explicit error
rather than guessed at or faked.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import yaml
from anthropic import Anthropic

from k8s_client import ALLOWED_RESOURCE_KINDS, ALLOWED_ACTIONS

# Swap for "claude-opus-4-8" if you want more careful reasoning on gnarly
# requests, or "claude-haiku-4-5-20251001" for the cheapest/fastest option.
MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = f"""You are the request-interpretation engine inside "K8s AI Agent", a tool that turns plain-English requests into either a Kubernetes manifest or a direct cluster action.

First decide which of two categories the request falls into:
- "manifest": the user wants to create, deploy, or add something new.
- "action": the user wants to delete, scale, or restart something that already exists.

Respond with ONLY a single JSON object — no markdown fences, no commentary before or after it.

For a "manifest" request, use this shape:
{{
  "type": "manifest",
  "namespace": "<namespace the resources should live in>",
  "yaml": "<one or more complete Kubernetes manifests as a single YAML string, multiple resources separated by '---'>",
  "explanation": "<one short sentence describing what you generated>"
}}

For an "action" request, use this shape:
{{
  "type": "action",
  "action": "<one of: {', '.join(sorted(ALLOWED_ACTIONS))}>",
  "kind": "<one of: {', '.join(sorted(ALLOWED_RESOURCE_KINDS))}>",
  "name": "<the resource's name>",
  "namespace": "<its namespace>",
  "replicas": <integer, ONLY include this field when action is "scale">,
  "explanation": "<one short sentence describing what this will do>"
}}

Rules:
- Always set a namespace. If the user doesn't specify one, use "default".
- For "manifest": generate complete, directly-applyable YAML (apiVersion, kind, metadata.name, metadata.namespace, a sensible spec). Don't include a Namespace object — that's handled separately. Fill in sensible defaults when the user is vague, and say so briefly in "explanation".
- For "action": "kind" and "action" MUST come from the allowed lists above — never invent a kind or action outside them. If a request like "delete the X replicas" is ambiguous between scaling to zero and deleting the resource entirely, prefer "delete" and say so in the explanation.
- Never invent a CRD or API group the user didn't name explicitly.
- If the request can't reasonably become either shape, respond with {{"type": "manifest", "namespace": "default", "yaml": "", "explanation": "<why not, in plain language>"}}.
- Output raw JSON only.
"""


@dataclass
class AgentResult:
    ok: bool
    kind: str = "manifest"  # "manifest" or "action"
    namespace: str = "default"
    yaml_text: str = ""
    explanation: str = ""
    error: str = ""
    raw_response: str = ""
    # action-specific fields
    action: str = ""
    resource_kind: str = ""
    name: str = ""
    replicas: int | None = None


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def interpret_request(user_request: str) -> AgentResult:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return AgentResult(ok=False, error="ANTHROPIC_API_KEY is not set. Add it to your .env file.")

    client = Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_request}],
        )
    except Exception as e:
        return AgentResult(ok=False, error=f"Claude API call failed: {e}")

    raw_text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")

    parsed = _extract_json(raw_text)
    if parsed is None:
        return AgentResult(
            ok=False,
            error="Could not parse a response out of what Claude returned.",
            raw_response=raw_text,
        )

    response_type = parsed.get("type", "manifest")
    namespace = parsed.get("namespace") or "default"
    explanation = parsed.get("explanation", "")

    if response_type == "action":
        action = (parsed.get("action") or "").strip()
        resource_kind = (parsed.get("kind") or "").strip()
        name = (parsed.get("name") or "").strip()
        replicas = parsed.get("replicas")

        # Defense in depth: don't trust the model's own claim that it stayed
        # inside the allowed lists — check again here, server-side, before
        # this ever reaches a confirm button.
        if action not in ALLOWED_ACTIONS:
            return AgentResult(
                ok=False,
                error=f"Model proposed an action ('{action}') outside what this tool allows: {sorted(ALLOWED_ACTIONS)}.",
                raw_response=raw_text,
            )
        if resource_kind not in ALLOWED_RESOURCE_KINDS:
            return AgentResult(
                ok=False,
                error=f"Model proposed a resource kind ('{resource_kind}') outside what this tool allows: {sorted(ALLOWED_RESOURCE_KINDS)}.",
                raw_response=raw_text,
            )
        if not name:
            return AgentResult(ok=False, error="Model didn't specify which resource to act on.", raw_response=raw_text)
        if action == "scale" and not isinstance(replicas, int):
            return AgentResult(ok=False, error="A 'scale' action needs an integer replica count.", raw_response=raw_text)

        return AgentResult(
            ok=True,
            kind="action",
            namespace=namespace,
            explanation=explanation,
            action=action,
            resource_kind=resource_kind,
            name=name,
            replicas=replicas if action == "scale" else None,
            raw_response=raw_text,
        )

    # Otherwise, a manifest response
    yaml_text = (parsed.get("yaml") or "").strip()

    if not yaml_text:
        return AgentResult(
            ok=False,
            kind="manifest",
            namespace=namespace,
            explanation=explanation or "The model didn't generate a manifest for this request.",
            raw_response=raw_text,
        )

    # Validate the YAML actually parses before the dashboard ever shows
    # an "Apply" button for it.
    try:
        list(yaml.safe_load_all(yaml_text))
    except yaml.YAMLError as e:
        return AgentResult(ok=False, kind="manifest", error=f"Model produced invalid YAML: {e}", raw_response=raw_text)

    return AgentResult(
        ok=True, kind="manifest", namespace=namespace, yaml_text=yaml_text, explanation=explanation, raw_response=raw_text
    )
