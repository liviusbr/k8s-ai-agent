"""
k8s_client.py — thin, honest wrapper around the kubectl CLI.

Every function returns a KubectlResult. Nothing here ever pretends an
operation succeeded when the underlying kubectl call failed — the raw
stdout/stderr/returncode is always passed back up so the caller (and
ultimately the dashboard) can show the real outcome instead of an LLM's
guess about what probably happened.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

KUBECTL_TIMEOUT = 20  # seconds — generous enough for a slower remote cluster

# Deliberately narrow allow-lists for anything the agent can act on
# imperatively (delete/scale/restart) without a manifest in between.
# Cluster-scoped or security-sensitive kinds (namespace, node, clusterrole,
# persistentvolume, etc.) are intentionally excluded — those carry a much
# bigger blast radius than "remove one Deployment" and aren't worth handing
# to an LLM-driven confirm button. Extend this list deliberately, not by
# trusting whatever Claude happens to propose.
ALLOWED_RESOURCE_KINDS = {
    "deployment",
    "statefulset",
    "daemonset",
    "replicaset",
    "service",
    "pod",
    "configmap",
    "secret",
    "job",
    "ingress",
}

ALLOWED_ACTIONS = {"delete", "scale", "restart"}

# Permissive RFC1123-ish check — good enough to catch a malformed name
# before it ever reaches a subprocess call, not meant to be a full spec.
_VALID_NAME = re.compile(r"^[a-z0-9]([a-z0-9\-\.]*[a-z0-9])?$")


def is_valid_k8s_name(value: str) -> bool:
    return bool(value) and len(value) <= 253 and bool(_VALID_NAME.match(value))


@dataclass
class KubectlResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = -1
    command: str = ""


def run_kubectl(args: list[str], context: str | None = None, timeout: int = KUBECTL_TIMEOUT) -> KubectlResult:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += args

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return KubectlResult(
            success=proc.returncode == 0,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            returncode=proc.returncode,
            command=" ".join(cmd),
        )
    except FileNotFoundError:
        return KubectlResult(
            success=False,
            stderr="kubectl was not found on PATH. Install it and make sure it's reachable from this shell.",
            command=" ".join(cmd),
        )
    except subprocess.TimeoutExpired:
        return KubectlResult(
            success=False,
            stderr=f"kubectl timed out after {timeout}s — cluster unreachable, or this kubeconfig context is invalid?",
            command=" ".join(cmd),
        )
    except Exception as e:  # still report honestly rather than swallowing anything unexpected
        return KubectlResult(success=False, stderr=f"Unexpected error running kubectl: {e}", command=" ".join(cmd))


def list_contexts() -> KubectlResult:
    return run_kubectl(["config", "get-contexts", "-o", "name"])


def get_current_context() -> KubectlResult:
    return run_kubectl(["config", "current-context"])


def namespace_exists(namespace: str, context: str | None) -> bool:
    res = run_kubectl(["get", "namespace", namespace, "-o", "name"], context)
    return res.success


def create_namespace(namespace: str, context: str | None) -> KubectlResult:
    return run_kubectl(["create", "namespace", namespace], context)


def apply_manifest(filepath: str, context: str | None) -> KubectlResult:
    return run_kubectl(["apply", "-f", filepath], context)


def dry_run_apply(filepath: str, context: str | None) -> KubectlResult:
    return run_kubectl(["apply", "-f", filepath, "--dry-run=server"], context)


def delete_manifest(filepath: str, context: str | None) -> KubectlResult:
    return run_kubectl(["delete", "-f", filepath], context)


def delete_resource(kind: str, name: str, namespace: str, context: str | None) -> KubectlResult:
    return run_kubectl(["delete", kind, name, "-n", namespace], context)


def scale_resource(kind: str, name: str, namespace: str, replicas: int, context: str | None) -> KubectlResult:
    return run_kubectl(["scale", kind, name, f"--replicas={replicas}", "-n", namespace], context)


def restart_resource(kind: str, name: str, namespace: str, context: str | None) -> KubectlResult:
    # rollout restart only applies to controller kinds — kubectl itself will
    # give an honest error if pointed at something like a bare pod or service,
    # which is exactly the behavior we want rather than masking it here.
    return run_kubectl(["rollout", "restart", f"{kind}/{name}", "-n", namespace], context)


def get_json(
    kind: str,
    context: str | None,
    namespace: str | None = None,
    all_namespaces: bool = False,
) -> tuple[bool, list[dict], str]:
    """Run `kubectl get <kind> -o json` and return (ok, items, error_message)."""
    args = ["get", kind, "-o", "json"]
    if all_namespaces:
        args.append("-A")
    elif namespace:
        args += ["-n", namespace]

    res = run_kubectl(args, context)
    if not res.success:
        return False, [], res.stderr

    try:
        data = json.loads(res.stdout)
        return True, data.get("items", []), ""
    except json.JSONDecodeError as e:
        return False, [], f"Failed to parse kubectl JSON output: {e}"
