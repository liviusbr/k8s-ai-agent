# K8s AI Agent

Describe what you want in plain English — *"deploy nginx with 3 replicas in
the test namespace"* or *"delete the nginx deployment in test"* or *"scale
nginx to 5 replicas"* — and the agent figures out whether that's something
to create (generates a manifest, creates the namespace if needed, applies
it) or something to act on directly (delete/scale/restart an existing
resource), and waits for you to confirm either way. A live dashboard shows
what's actually running in the cluster.

Built with **FastAPI** + the **Claude API** (Anthropic) + vanilla
HTML/CSS/JS, talking to your cluster through `kubectl`.

## Why a confirm step?

The original spec for this kind of tool auto-applies on every request. This
version generates the YAML and shows it to you first — you click **Apply**
or **Discard**. An LLM that silently mutates a live cluster (including
whichever one happens to be `current-context` in your kubeconfig) is a
sharper edge than it looks; one extra click is cheap insurance. If you want
the original fire-and-forget behavior, see "Going further" below.

## Setup

1. **Requirements on your machine:** Python 3.10+, `kubectl` on your PATH,
   and a working kubeconfig (minikube, kind, EKS, GKE, AKS, on-prem — any
   context `kubectl config get-contexts` can see).

2. **API key:**
   ```bash
   cp .env.example .env
   # edit .env and paste your ANTHROPIC_API_KEY
   ```

3. **Run it:**
   ```bash
   python3 server.py
   ```
   First run installs `fastapi`, `uvicorn`, `anthropic`, `pyyaml`, and
   `python-dotenv` automatically, then opens `http://127.0.0.1:8000` in
   your browser. No manual `pip install -r requirements.txt` needed,
   though it'll work fine inside a venv too if you'd rather isolate it:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python3 server.py
   ```

## How it works

```
static/index.html, style.css, app.js   →  dashboard UI, talks to the API
server.py                              →  FastAPI app, all /api/* routes
claude_agent.py                        →  prompts Claude, parses + validates the YAML it returns
k8s_client.py                          →  every kubectl call, as subprocess, with real stdout/stderr/returncode
manifests/                             →  every generated YAML is saved here, named <timestamp>-<slug>.yaml
```

Request flow:

1. You type a request → `POST /api/generate` asks Claude for a manifest.
   Claude is constrained to return structured JSON (namespace + YAML +
   one-line explanation); the response is `yaml.safe_load`-validated
   server-side before it's ever shown to you, so you don't get handed
   broken YAML to apply.
2. You click **Apply** → `POST /api/apply`:
   - checks whether the target namespace exists, creates it if not
   - writes the manifest to `manifests/<timestamp>-<slug>.yaml`
   - runs `kubectl apply -f <file> --context <ctx>`
   - returns the *actual* `kubectl` stdout/stderr — if `apply` fails, the
     UI shows the real error and the file stays on disk so you can fix and
     retry, rather than reporting success it didn't earn.
3. The dashboard polls `GET /api/cluster` every 6s for live namespaces,
   pods, deployments, and services (also a manual **Refresh** button).
4. **Manifests** drawer lists every saved file; **Delete** runs
   `kubectl delete -f <file>` and only removes the file from disk if that
   actually succeeded.

### Deleting, scaling, restarting — the action path

Not every request fits the "generate a manifest" shape — "delete the nginx
deployment" isn't a YAML object you can apply. For these, Claude responds
with a small structured action instead (`{"type": "action", "action":
"delete", "kind": "deployment", "name": "nginx", "namespace": "test"}`)
rather than YAML, and the dashboard shows a confirm card for that specific
command instead of an Apply/Discard manifest card.

Two allow-lists in `k8s_client.py` keep this tightly scoped on purpose —
`ALLOWED_ACTIONS` (`delete`, `scale`, `restart`) and `ALLOWED_RESOURCE_KINDS`
(common namespaced workloads: deployments, statefulsets, services, pods,
configmaps, etc.). Cluster-scoped or higher-blast-radius kinds — namespaces,
nodes, ClusterRoles — are deliberately left out; deleting an entire
namespace cascades to everything in it, which is a different risk tier than
removing one Deployment. Both `claude_agent.py` and `server.py` check
against these lists independently (defense in depth — even a tampered
frontend request can't reach `kubectl` with something outside them), and
resource names are validated against Kubernetes' own naming rules before
ever reaching a subprocess call.

Multi-cluster: the context dropdown is populated from
`kubectl config get-contexts`, and every `kubectl` call (apply, delete,
get) is run with `--context <selected>` rather than mutating your global
`current-context` — so you can flip between minikube/kind/EKS/whatever
without it bleeding into your shell's default context.

## Going further

A few things deliberately left out of this first pass, in roughly the
order I'd reach for them:

- **Pod logs in chat** — `kubectl logs` wired into a chat command, so a
  crash loop is debuggable without leaving the dashboard.
- **Helm support** — a different shape of problem (charts, values, releases
  vs. raw manifests); worth its own pass rather than bolting onto this one.
- **Multi-resource templates** — most real requests are a Deployment +
  Service (+ Ingress/ConfigMap) together; the system prompt in
  `claude_agent.py` already allows multiple `---`-separated resources, but
  there's no UI affordance yet for previewing/applying them as a labeled
  group.
- **Tighter RBAC** — right now this uses whatever permissions your current
  kubeconfig context has. For anything beyond a homelab, point it at a
  ServiceAccount scoped to just the namespaces/verbs it actually needs.
- **Auto-apply mode** — if you want the original "no confirm, just do it"
  behavior back, the only change needed is calling `/api/apply` directly
  from the `/api/generate` success handler in `app.js` instead of rendering
  the Apply/Discard card.
