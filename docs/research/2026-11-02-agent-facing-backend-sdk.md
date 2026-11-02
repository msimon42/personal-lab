# Building an agent-facing SDK for a backend API

**Research date:** 2026-11-02 UTC  
**Status:** Research and proposed architecture, not an implementation or measured benchmark  
**Primary use case:** An MCP interface over an existing backend API  
**Repository case study:** Personal Lab

## Executive recommendation

Build an **agent-friendly capability layer**, not a second backend and not a mandatory agent runtime. Keep authoritative domain operations and permissions in the backend. Expose the same operations through a typed SDK, machine-readable CLI, MCP, and optional provider-specific function adapters. Generate repetitive transport code, but curate the operations, descriptions, result shapes, and recovery behavior that agents actually use.

For an existing enterprise backend, put a thin MCP adapter in front of approved application operations. Preserve the backend’s authentication, object-level permissions, validation, and auditing. Start with a narrow read-only pilot before introducing writes. The Personal Lab case study later in this report illustrates transport-independent contracts; its local stdio deployment is not a prescription for enterprise hosting.

### The five most useful developments

1. **Progressive discovery:** load a small relevant tool surface instead of placing every schema in context.
2. **Programmatic composition:** let sandboxed code call typed operations, join/filter results, and return compact answers when that beats repeated model turns.
3. **Task-oriented tools over generated coverage:** OpenAPI generation is useful plumbing, but one tool per endpoint is not automatically a good agent interface.
4. **Explicit long-running operation contracts:** distinguish asynchronous I/O, backend jobs, protocol tasks, and model-side asynchronous tool calling.
5. **Outcome-based evaluation plus deterministic safety:** score actual results and side effects, not just schema-valid calls or plausible explanations.

These are supported by current primary documentation and engineering reports below. They are not evidence that any one framework is universally best.

## 1. Clarify what “agent SDK” means

Three different products are often conflated:

- **Backend client SDK:** typed methods, authentication, serialization, pagination, errors, retries, and job handles.
- **Agent tool adapter:** presents selected SDK operations as discoverable tools, with descriptions and provider-compatible schemas.
- **Agent runtime SDK:** runs the model loop, orchestration, context management, tracing, and possibly sandboxes.

This report primarily recommends the first two. An external agent can supply the third. An existing REST backend would usually retain its API and add a curated facade; Personal Lab already has an in-process application boundary and does not require REST as an intermediate hop.

## 2. What the current evidence says

### A. Task-shaped interfaces matter more than endpoint count

Anthropic's September 11, 2025 engineering guidance emphasizes deliberate tool selection, clear namespaces, useful result content, and iteration against realistic tasks. This is vendor engineering experience, not a universal controlled benchmark. The practical lesson is to retain useful primitives while adding narrowly scoped operations that remove unnecessary coordination. Avoid both hundreds of ambiguous CRUD tools and one opaque `do_everything` tool. [Source](https://www.anthropic.com/research/writing-tools-for-agents)

**Recommendation:** begin with a few representative user workflows. Determine which operations an agent must discover, what identifiers it needs, and what evidence establishes completion. Improve that path before expanding the catalog.

### B. Generate clients; curate the agent-facing surface

Speakeasy documents standalone MCP generation from OpenAPI and a gateway that selects which generated tools to expose. Its older SDK-level `enableMCPServer` setting is deprecated. Stainless documents configurable pagination and retry behavior. These are product capabilities, not independent proof of reliability or a recommendation to purchase either product. [Speakeasy generation](https://www.speakeasy.com/docs/standalone-mcp/overview), [curation](https://www.speakeasy.com/docs/ai-control-plane/mcp-gateway/building-servers), [Stainless pagination](https://www.stainless.com/docs/sdks/configure/pagination/), [client behavior](https://www.stainless.com/docs/sdks/configure/client/)

**Recommendation:** use one authoritative contract to generate repetitive bindings and documentation. Review operation semantics manually. Auto-pagination needs a page/item/time budget, and generated retry defaults must not silently replay unsafe writes. For an existing HTTP API, use OpenAPI as the transport contract; do not assume it contains sufficient authorization, workflow, or agent-use guidance.

### C. Discovery and code composition are complementary

OpenAI documents deferred loading through tool search. It is provider-specific behavior, not a guarantee of every MCP host. Search must retrieve the right tools, so evaluate discovery recall and its extra latency against an eager catalog. [Tool search](https://developers.openai.com/api/docs/guides/tools-tool-search)

Anthropic's November 4, 2025 report describes code calling MCP-backed operations and filtering intermediate data outside the model context. Its dramatic token-reduction example is a vendor demonstration, not an expected saving for this project. [Code execution with MCP](https://www.anthropic.com/research/code-execution-with-mcp)

Cloudflare's Code Mode documentation, updated June 24, 2026, similarly exposes typed methods for composition and progressive discovery. It explicitly labels Code Mode experimental. This supports an optional optimization path, not replacing ordinary calls everywhere. [Cloudflare documentation](https://developers.cloudflare.com/agents/tools/codemode/)

OpenAI's current Programmatic Tool Calling guide describes isolated JavaScript execution, allowlisted calls, and output schemas. It recommends direct calls for writes or approval-sensitive operations. Availability must be checked for the selected model; this research did not establish a publication date for the living guide. [Programmatic Tool Calling](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling)

**Recommendation:** start with direct tools. Benchmark code composition on read-heavy joins, filtering, and pagination. Keep credentials, permissions, rate limits, and audit enforcement at each backend operation. A sandbox must not become a route around those controls. Do not add unrestricted shell execution merely to achieve “code mode.”

### D. A common schema still needs adapters

OpenAI strict function calling imposes requirements including closed objects and required declared fields, with nullable values representing optional inputs. Gemini also documents a supported schema subset. A Pydantic-generated JSON Schema is therefore not automatically accepted with identical meaning everywhere. [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling), [Gemini function calling](https://aistudio.google.com/docs/function-calling)

**Recommendation:** preserve domain semantics in the authoritative model and test explicit projections for each supported tool provider. Snapshot generated schemas. Validate inputs again on the server; model-side schema enforcement is not authorization or business validation.

### E. MCP has moved; ecosystem compatibility still has to be tested

The official versioning page identifies **2026-07-28** as current. It describes per-request protocol versions and `server/discover`, contrasting the older handshake-based revisions. Do not design against an old tutorial without identifying its protocol revision. Equally, do not assume every installed host supports the newest revision. [Versioning](https://modelcontextprotocol.io/docs/2026-07-28/learn/versioning)

The official Tasks extension describes durable task handles. However, the Python SDK migration page says its redesigned Tasks extension is not implemented by that SDK yet. Treat this as a documented support gap to investigate against exact installed versions, not proof about all SDK distributions or hosts. [Tasks](https://modelcontextprotocol.io/extensions/tasks/overview), [Python migration](https://py.sdk.modelcontextprotocol.io/migration/)

**Recommendation:** record a compatibility matrix: client/version, SDK/version, protocol revision, transport, structured results, discovery, cancellation, and extension support. Maintain ordinary start/status/result/cancel operations when needed rather than depending on an optional extension. Do not upgrade Personal Lab's pinned MCP dependency merely because a newer protocol exists.

### F. “Async” is four separate decisions

| Layer | What it provides | What it does not establish |
|---|---|---|
| Python async I/O | Concurrent waiting | Durable jobs or restart survival |
| Backend job service | Stored work identity and lifecycle | Automatic model orchestration |
| MCP Tasks extension | Negotiated protocol lifecycle | Universal host/SDK support |
| Model async tool calling | Model can proceed while tools run | Backend execution or durability |

OpenAI documents async client-run function/custom tools, with model and mode restrictions; application code still owns execution. Microsoft's asynchronous request-reply pattern documents accepting work and returning a status location. [Async tool calling](https://developers.openai.com/api/docs/guides/async-tool-calling), [request-reply pattern](https://learn.microsoft.com/en-gb/azure/architecture/patterns/async-request-reply)

**Recommendation:** use synchronous operations for short tasks. For long work, specify job identity, owner, status, result retention, deadlines, and cancellation outcomes. Progress is not persistence, and cancellation requested is not cancellation completed. Preserve the application's actual restart semantics instead of claiming durability it does not provide.

## 3. Proposed contract checklist

The following is our design synthesis, not a claim that any single standard mandates this exact envelope.

| Concern | Contract to define |
|---|---|
| Identity | Authenticated actor context comes from trusted transport/runtime, never a model-supplied user ID. |
| Permissions | Check operation and object access on every call, including jobs, artifacts, and continuation tokens. |
| Inputs | Stable IDs, bounded ranges, explicit units/time zones, unknown-field rules, and domain constraints. |
| Outputs | Typed data, freshness/provenance, limitations, explicit truncation, and bounded continuations. |
| Errors | Stable codes and safe recovery advice; distinguish invalid input, denial, conflict, throttling, transient error, and uncertain commit outcome. |
| Mutations | Idempotency scope/retention/mismatch behavior plus version preconditions where needed. |
| Retries | One retry owner with an attempt/deadline budget; backoff and jitter; no blind mutation replay. |
| Jobs | Explicit lifecycle, owner, expiry, result retrieval, and cancellation-versus-completion race handling. |
| Approvals | Bind trusted approval to exact effects, arguments, version, destination, and expiry; a model flag is not authority. |
| Observability | Correlate tool/backend/job operations; redact secrets and minimize payload capture. |
| Versioning | Test schema and semantic compatibility; publish capability availability rather than guessing. |
| Documentation | Short examples, limitations, error recovery, and tested workflow recipes close to each operation. |

### Authentication and injection boundaries

For remote HTTP MCP, current authorization guidance describes OAuth resource-server/client roles and discovery. It references OAuth 2.1 **draft-13**, not a finalized OAuth 2.1 RFC. Stdio has different credential guidance; do not bolt the remote flow onto a local subprocess without a requirement. [MCP authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)

Use established OAuth security practice, including privilege restriction. MCP security guidance addresses audience validation, token passthrough, confused-deputy risks, discovery-related SSRF, and handle access. Model-generated arguments must never decide which identity is authoritative. [RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html), [MCP security](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices)

OpenAI's March 11, 2026 prompt-injection analysis emphasizes limiting damage even if manipulation succeeds. Treat retrieved records and third-party descriptions as untrusted data. Restrict secret access, outbound destinations, and mutations independently of the agent's prose. Structured JSON alone does not eliminate injection. [Prompt-injection containment](https://openai.com/index/designing-agents-to-resist-prompt-injection/)

### Recovery and telemetry

AWS's established retry guidance explains that a timeout does not establish that a side effect failed. Stripe's documented idempotency behavior illustrates why replay semantics and retention must be explicit; do not assume every provider behaves identically. [AWS retries](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/), [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests)

HTTP APIs can use RFC 9457 Problem Details, while MCP adapters preserve protocol-versus-tool-execution error distinctions. Do not turn every business failure into an opaque transport exception. [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html), [MCP tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

OpenTelemetry's GenAI documentation now points to a dedicated official repository. Pin the conventions you use and verify their stability rather than assuming all agent telemetry is standardized. Never log secrets merely to obtain a complete trace. [Migration notice](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/), [current repository](https://github.com/open-telemetry/semantic-conventions-genai)

## 4. Practical pilot: MCP over an existing backend

This is a proposed generic pilot, not a description of a particular employer or an authorization to deploy. Keep internal endpoints, identifiers, credentials, schemas, and records out of a public research repository.

### Architecture

Agent host → MCP adapter → approved backend API → existing application services and data.

The adapter should translate and constrain operations, not bypass the API by reaching directly into its database. It can use an existing internal SDK or a generated HTTP client; a new standalone SDK package is optional. Keep domain rules in their current owner so ordinary API callers and agents receive consistent validation.

### Start with one useful workflow

Choose a workflow with a verifiable answer and low operational risk: for example, investigating the status of a synthetic request across a few backend records. Expose approximately five to ten narrowly scoped tools, adjusted to the actual task, such as search, retrieve summary, retrieve bounded history, and explain permitted next steps. These are illustrative operation categories, not assumed existing endpoints.

Use approved nonproduction data or synthetic fixtures. “Read-only” is not equivalent to “safe”: reads can expose sensitive records, invoke expensive searches, or leak results to an external model provider. Establish approved data destinations, field-level disclosure, result limits, and account access before connecting the host. Do not paste internal data into public documentation or evaluation examples.

### Separate three identities and permissions

1. **The human or workload using the agent.** Establish who initiated the operation through trusted authentication.
2. **The agent host accessing MCP.** Establish client identity using the supported authorization flow, and authorize the authenticated user or workload’s tool access and scopes. Do not assume every host is a confidential OAuth client.
3. **The MCP adapter calling the backend.** Use the backend's approved credential and delegation mechanism. Preserve user/object authorization; do not silently substitute a highly privileged shared identity.

A user ID in tool arguments is not identity. If the backend supports approved on-behalf-of delegation, use that mechanism with audience-correct credentials. Otherwise explicitly decide which service identity is permitted, restrict its data/actions, and define how user entitlements are enforced. Do not pass an MCP access token through to an unrelated backend resource or treat an agent's claim of permission as proof. The applicable standards are the [MCP authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization) and [security guidance](https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices); the organization’s actual identity architecture still needs review.

### Select transport for the actual client

A local stdio process may be sufficient for a developer-only prototype. A shared remote server typically needs an approved network endpoint and supported HTTP MCP transport, TLS, authentication, egress controls, and operational ownership. Verify the chosen host's exact protocol, transport, authentication, and schema support. Do not expose a development server publicly just to make a demo reachable.

### Implement the read-only contract first

Each tool should have a purpose-oriented name, a short description of when to use it, strict inputs, bounded structured outputs, stable identifiers, safe errors, and explicit limitations. Enforce read-only access in server/backend permissions; a tool annotation is not an access control. Build server-side allowlists rather than a generic arbitrary-URL proxy. Apply row/object checks to detail endpoints and pagination just as strictly as search. Set per-operation deadlines, concurrency and response-size limits, and record correlation IDs without capturing unnecessary payloads.

### Demonstrate value and failure containment

Run a small held-out set of realistic tasks, including ambiguous identifiers, missing records, unauthorized objects, large result sets, upstream errors, and malicious text in retrieved records. Test at least two differently privileged identities, expired credentials, and revoked access. Compare the agent's answer with known backend state. Report correct completion, unauthorized disclosure/action attempts blocked, false success claims, latency, and calls per successful task. A successful happy-path demo is not production qualification.

Only after the read pilot is useful should a separate write phase add exact previews, approval boundaries, idempotency, optimistic concurrency, audit receipts, and recovery for ambiguous outcomes. Agree on deployment, security review, support, and rollback requirements before production use.

## 5. Concrete fit for Personal Lab

Repository inspection used public main at commit **97cd1ad74cc5577fbe1b78cce4ae27ef7ac2ba57**, not unpublished local changes. Evidence: [README](../../README.md), [architecture](../architecture.md), [adding a toolset](../adding-a-toolset.md), and [toolkit package](../../apps/toolkit/pyproject.toml).

Observed:

- Core and domain services are deliberately independent of MCP and model SDKs.
- CLI and MCP share typed application operations; toolset registration is trusted, not driven by untrusted tool arguments.
- Outputs are bounded; continuations, artifacts, effects, and approval paths already have explicit contracts.
- The toolkit pins `mcp==2.3.0`; this review did not run that installed distribution or verify host interoperability.
- Runtime history survives disconnection, but the architecture explicitly says work does not survive process exit. Do not advertise restart-safe execution without adding and validating it.

### Recommended sequence

**First: inventory and measure.** Map the existing operations and generated contracts to representative tasks. Record baseline task success, unsupported success claims, schema errors, tool calls, tokens, latency, and safety violations. Test the current pinned client/SDK combination before proposing upgrades.

**Second: improve the facade.** Make the existing operation boundary easy to call as typed Python methods if a stable public client facade is missing. Keep a stable JSON CLI contract. Improve names, descriptions, examples, filters, bounded results, and error recovery. Reuse current approval and evidence machinery.

**Third: add adapters only for actual consumers.** A provider function adapter can project the same registry. If a remote HTTP consumer is needed, introduce a separately secured HTTP/MCP adapter and appropriate API contract without importing its SDK into domain code. Neither a browser dashboard nor an SDK requires moving forecasting calculations into the frontend.

**Fourth: test optional efficiency features.** Compare direct calls with discovery and sandboxed composition on the same held-out workflows. Add durable job execution only if restart survival is required. Keep a compatible fallback for hosts without optional protocol features.

**Do not do now:** add a mandatory internal model loop, expose all filesystem/network capabilities through a generic execution tool, automatically migrate protocol versions, or replace existing exact-plan approval with an `approved: true` parameter.

## 6. Evaluation and acceptance plan

Anthropic's January 9, 2026 evaluation guidance distinguishes the trajectory from final environment state and recommends repeated trials. A correct-looking response is not proof that the requested operation succeeded. Use final-state and invariant checks rather than insisting on a single valid tool sequence. [Agent evaluations](https://www.anthropic.com/research/demystifying-evals-for-ai-agents)

| Test family | Representative case | Required evidence |
|---|---|---|
| Contract parity | Same request through SDK, CLI, MCP | Equivalent normalized outcomes and authority checks |
| Validation | Unknown fields, invalid enum, oversized page | Bounded typed rejection; no effect |
| Authorization | Another actor's artifact/job/cursor | Denial without disclosure |
| Replay | Commit succeeds, response is lost, retry follows | One logical mutation or explicit unresolved outcome |
| Concurrency | Stale version; reused key with new input | Defined conflict, not silent overwrite |
| Overload | Repeated 429/503 and slow dependencies | Bounded retry/deadline behavior |
| Pagination | Source changes; empty page; resumed cursor | No false completeness claim; documented consistency |
| Job lifecycle | Restart, expiry, cancellation race | Honest state and no assumed rollback |
| Approval | Denied, expired, changed, or replayed plan | No unauthorized effect |
| Injection/redaction | Malicious source text and secret-bearing errors | No authority escalation or secret leakage |
| Agent usability | Ambiguous request and multi-tool comparison | Correct result, necessary clarification, evidence |
| Compatibility | Supported client/SDK/provider matrix | Recorded successful and unsupported feature paths |

Compare eager/direct, discovery/direct, and discovery/composition using identical fixtures and repeated runs. Report cost and latency **per successful task**, not only per call. Keep authorization violations and false success claims separate from ordinary failures. Establish targets after measuring the baseline; this research supplies no invented success rates.

For the election module, a useful fixture compares races with different snapshot dates and missing values, follows bounded pages, preserves provenance, and ignores a malicious instruction planted in a source note. This is a proposed test—not a claim that new tests were executed.

## Research limits and implementation boundary

Sources were retrieved on November 2, 2026. Dated engineering reports are distinguished from living documentation; an access date is not a release date. Official documentation establishes described behavior, not independent performance or availability in every account/client. Vendor examples are not project benchmarks. The report deliberately combines newer agent-interface features with older, still-essential distributed-systems and security practices.

Only repository documentation and current primary sources were inspected. No SDK was implemented, dependencies upgraded, backend deployed, or benchmark run for this report. Recommendations require separate implementation and validation. Publication is limited to this research document.
