"""
claude_agent.py — turns a plain-English request into a Kubernetes manifest
using the Claude API.

Never silently fails: if Claude's output can't be parsed into a manifest,
that's surfaced as an explicit error rather than guessed at or faked.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import yaml
from anthropic import Anthropic

# Swap for "claude-opus-4-8" if you want more careful reasoning on gnarly
# requests, or "claude-haiku-4-5-20251001" for the cheapest/fastest option.
MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are the manifest-generation engine inside "K8s AI Agent", a tool that turns plain-English requests into Kubernetes manifests.

Respond with ONLY a single JSON object — no markdown fences, no commentary before or after it — shaped exactly like this:

{
  "namespace": "<namespace the resources should live in>",
  "yaml": "<one or more complete Kubernetes manifests as a single YAML string, multiple resources separated by '---'>",
  "explanation": "<one short sentence describing what you generated>"
}

Rules:
- Always set a namespace. If the user doesn't specify one, use "default".
- Generate complete, directly-applyable manifests: apiVersion, kind, metadata.name, metadata.namespace, and a sensible spec.
- Do not include a Namespace object in "yaml" — namespace creation is handled separately by the system.
- When the user is vague (no image, no port, no replica count) fill in sensible, commonly-used defaults and say so briefly in "explanation".
- Never invent a CRD or API group the user didn't ask for by name.
- If the request can't reasonably become a Kubernetes manifest, set "yaml" to an empty string and use "explanation" to say why.
- Output raw JSON only.
"""


@dataclass
class AgentResult:
    ok: bool
    namespace: str = "default"
    yaml_text: str = ""
    explanation: str = ""
    error: str = ""
    raw_response: str = ""


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


def generate_manifest(user_request: str) -> AgentResult:
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
            error="Could not parse a manifest spec out of Claude's response.",
            raw_response=raw_text,
        )

    yaml_text = (parsed.get("yaml") or "").strip()
    namespace = parsed.get("namespace") or "default"
    explanation = parsed.get("explanation", "")

    if not yaml_text:
        return AgentResult(
            ok=False,
            namespace=namespace,
            explanation=explanation or "The model didn't generate a manifest for this request.",
            raw_response=raw_text,
        )

    # Validate the YAML actually parses before the dashboard ever shows
    # an "Apply" button for it.
    try:
        list(yaml.safe_load_all(yaml_text))
    except yaml.YAMLError as e:
        return AgentResult(ok=False, error=f"Model produced invalid YAML: {e}", raw_response=raw_text)

    return AgentResult(ok=True, namespace=namespace, yaml_text=yaml_text, explanation=explanation, raw_response=raw_text)
