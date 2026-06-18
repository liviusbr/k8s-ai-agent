#!/usr/bin/env python3
"""
K8s AI Agent — server.py

Single entry point. Run it with:

    python3 server.py

First run installs any missing Python dependencies automatically, then
starts the API + dashboard and opens it in your browser. No virtualenv
ceremony required (though using one is still good practice).
"""

import importlib
import os
import subprocess
import sys
import threading
import time
import webbrowser

REQUIRED = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
    "anthropic": "anthropic",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
}

HOST = "127.0.0.1"
PORT = 8000


def _pip_install(packages: list[str]) -> None:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *packages])
    except subprocess.CalledProcessError:
        # Debian/Ubuntu system Python (PEP 668) blocks unmanaged global installs.
        print("[bootstrap] Plain install failed — retrying with --break-system-packages...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "--break-system-packages", *packages]
        )


def ensure_dependencies() -> None:
    missing = []
    for module_name, pip_name in REQUIRED.items():
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(pip_name)

    if missing:
        print(f"[bootstrap] Installing missing packages: {', '.join(missing)}")
        _pip_install(missing)
        print("[bootstrap] Done — relaunching...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    ensure_dependencies()


# --- Everything below only gets imported once dependencies are guaranteed present ---

import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import k8s_client as k8s
from claude_agent import interpret_request

load_dotenv()

BASE_DIR = Path(__file__).parent.resolve()
MANIFEST_DIR = BASE_DIR / "manifests"
MANIFEST_DIR.mkdir(exist_ok=True)
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="K8s AI Agent")


@app.middleware("http")
async def no_cache_for_static(request: Request, call_next):
    # This is a local dev tool, not a production site fronted by a CDN —
    # the file you just edited should show up on the next refresh, not
    # require a manual hard-reload because the browser cached app.js from
    # ten minutes ago. Cheap insurance against a confusing class of bug.
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- models ----

class GenerateRequest(BaseModel):
    message: str
    context: str | None = None


class ApplyRequest(BaseModel):
    yaml: str
    namespace: str
    context: str | None = None
    description: str | None = None


class ActionRequest(BaseModel):
    action: str
    kind: str
    name: str
    namespace: str
    replicas: int | None = None
    context: str | None = None


# ----------------------------------------------------------- static UI -----

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ------------------------------------------------------------- contexts ----

@app.get("/api/contexts")
def api_contexts():
    res = k8s.list_contexts()
    current = k8s.get_current_context()
    if not res.success:
        raise HTTPException(status_code=502, detail=res.stderr or "Failed to list kubeconfig contexts.")
    contexts = [c for c in res.stdout.splitlines() if c.strip()]
    return {"contexts": contexts, "current": current.stdout if current.success else None}


# --------------------------------------------------------- cluster view ----

def _summarize_namespaces(items):
    return [{"name": n["metadata"]["name"], "status": n.get("status", {}).get("phase", "Unknown")} for n in items]


def _summarize_pods(items):
    out = []
    for p in items:
        status = p.get("status", {})
        containers = status.get("containerStatuses", [])
        ready = sum(1 for c in containers if c.get("ready"))
        out.append(
            {
                "name": p["metadata"]["name"],
                "namespace": p["metadata"]["namespace"],
                "phase": status.get("phase", "Unknown"),
                "ready": f"{ready}/{len(containers)}",
                "restarts": sum(c.get("restartCount", 0) for c in containers),
            }
        )
    return out


def _summarize_deployments(items):
    out = []
    for d in items:
        spec, status = d.get("spec", {}), d.get("status", {})
        out.append(
            {
                "name": d["metadata"]["name"],
                "namespace": d["metadata"]["namespace"],
                "desired": spec.get("replicas", 0),
                "ready": status.get("readyReplicas", 0),
            }
        )
    return out


def _summarize_services(items):
    out = []
    for s in items:
        spec = s.get("spec", {})
        ports = spec.get("ports", [])
        out.append(
            {
                "name": s["metadata"]["name"],
                "namespace": s["metadata"]["namespace"],
                "type": spec.get("type", "ClusterIP"),
                "ports": [f"{p.get('port')}:{p.get('targetPort')}" for p in ports],
            }
        )
    return out


@app.get("/api/cluster")
def api_cluster(context: str | None = None):
    ok_ns, ns_items, err_ns = k8s.get_json("namespace", context)
    ok_pods, pod_items, err_pods = k8s.get_json("pods", context, all_namespaces=True)
    ok_dep, dep_items, err_dep = k8s.get_json("deployments", context, all_namespaces=True)
    ok_svc, svc_items, err_svc = k8s.get_json("services", context, all_namespaces=True)

    errors = [e for e in (err_ns, err_pods, err_dep, err_svc) if e]
    return {
        "namespaces": _summarize_namespaces(ns_items) if ok_ns else [],
        "pods": _summarize_pods(pod_items) if ok_pods else [],
        "deployments": _summarize_deployments(dep_items) if ok_dep else [],
        "services": _summarize_services(svc_items) if ok_svc else [],
        "errors": errors,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ------------------------------------------------------- generate (LLM) ----

@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    result = interpret_request(req.message)
    if not result.ok:
        return {
            "ok": False,
            "error": result.error or result.explanation or "Could not interpret that request.",
        }
    if result.kind == "action":
        return {
            "ok": True,
            "kind": "action",
            "action": result.action,
            "resource_kind": result.resource_kind,
            "name": result.name,
            "namespace": result.namespace,
            "replicas": result.replicas,
            "explanation": result.explanation,
        }
    return {
        "ok": True,
        "kind": "manifest",
        "namespace": result.namespace,
        "yaml": result.yaml_text,
        "explanation": result.explanation,
    }


# ------------------------------------------------------------ apply flow ---

def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", text.strip().lower()).strip("-")
    return slug[:40] or "manifest"


@app.post("/api/apply")
def api_apply(req: ApplyRequest):
    context = req.context

    if not k8s.namespace_exists(req.namespace, context):
        ns_res = k8s.create_namespace(req.namespace, context)
        if not ns_res.success and "AlreadyExists" not in ns_res.stderr:
            return {
                "ok": False,
                "stage": "namespace",
                "message": f"Failed to create namespace '{req.namespace}'.",
                "stderr": ns_res.stderr,
            }

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{_slugify(req.description or req.namespace)}.yaml"
    filepath = MANIFEST_DIR / filename
    filepath.write_text(req.yaml)

    apply_res = k8s.apply_manifest(str(filepath), context)

    return {
        "ok": apply_res.success,
        "stage": "apply",
        "message": apply_res.stdout if apply_res.success else "kubectl apply failed — manifest kept on disk for inspection.",
        "stderr": apply_res.stderr,
        "file": filename,
        "command": apply_res.command,
    }


# ----------------------------------------------------------- action flow ---

@app.post("/api/action")
def api_action(req: ActionRequest):
    # Defense in depth, same as in claude_agent.py: never trust the request
    # body's claim that it stayed inside bounds. This endpoint enforces the
    # same allow-lists independently, so even a malformed or tampered
    # request from the frontend can't reach kubectl with something outside
    # the small set of actions/kinds this tool is willing to run.
    if req.action not in k8s.ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Action '{req.action}' is not allowed. Allowed: {sorted(k8s.ALLOWED_ACTIONS)}")
    if req.kind not in k8s.ALLOWED_RESOURCE_KINDS:
        raise HTTPException(status_code=400, detail=f"Resource kind '{req.kind}' is not allowed. Allowed: {sorted(k8s.ALLOWED_RESOURCE_KINDS)}")
    if not k8s.is_valid_k8s_name(req.name) or not k8s.is_valid_k8s_name(req.namespace):
        raise HTTPException(status_code=400, detail="Resource name or namespace doesn't look like a valid Kubernetes name.")

    if req.action == "delete":
        res = k8s.delete_resource(req.kind, req.name, req.namespace, req.context)
    elif req.action == "scale":
        if req.replicas is None or req.replicas < 0:
            raise HTTPException(status_code=400, detail="A 'scale' action needs a non-negative integer replica count.")
        res = k8s.scale_resource(req.kind, req.name, req.namespace, req.replicas, req.context)
    elif req.action == "restart":
        res = k8s.restart_resource(req.kind, req.name, req.namespace, req.context)
    else:  # unreachable given the allow-list check above, but never silently fall through
        raise HTTPException(status_code=400, detail=f"Unhandled action '{req.action}'.")

    return {
        "ok": res.success,
        "message": res.stdout if res.success else f"kubectl {req.action} failed.",
        "stderr": res.stderr,
        "command": res.command,
    }


# -------------------------------------------------------- manifest browser -

@app.get("/api/manifests")
def api_list_manifests():
    out = [
        {"filename": f.name, "size": f.stat().st_size, "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat()}
        for f in sorted(MANIFEST_DIR.glob("*.yaml"), reverse=True)
    ]
    return {"manifests": out}


def _safe_manifest_path(filename: str) -> Path:
    filepath = (MANIFEST_DIR / filename).resolve()
    if filepath.parent != MANIFEST_DIR.resolve() or not filepath.exists():
        raise HTTPException(status_code=404, detail="Manifest not found.")
    return filepath


@app.get("/api/manifests/{filename}")
def api_get_manifest(filename: str):
    filepath = _safe_manifest_path(filename)
    return {"filename": filename, "yaml": filepath.read_text()}


@app.delete("/api/manifests/{filename}")
def api_delete_manifest(filename: str, context: str | None = None):
    filepath = _safe_manifest_path(filename)
    del_res = k8s.delete_manifest(str(filepath), context)
    if del_res.success:
        filepath.unlink()
        return {"ok": True, "message": del_res.stdout or "Deleted from cluster and removed manifest file."}
    return {"ok": False, "message": "kubectl delete failed — file kept on disk.", "stderr": del_res.stderr}


# ----------------------------------------------------------------- main ----

def _open_browser_when_ready():
    import urllib.request

    url = f"http://{HOST}:{PORT}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url, timeout=0.5)
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(0.25)


if __name__ == "__main__":
    import uvicorn

    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    print(f"[K8s AI Agent] Starting on http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
