# ADR-012 — Confined code execution, and the untrusted-input rule

**Status:** Proposed · **Date:** 2026-10-08 · **Amends:** ADR-002 C.11 (scope note only), ADR-003 R4 (scope note only) · **Issue:** #284

## Summary

An agent that serves end users over a channel (`untrusted_input: true`) cannot run code it writes with the kit today: `use_term` is T3, which R4 bars from such an agent, and a command tool runs only a fixed argv with checked parameters. Adopters who need data analysis per end user therefore build their own sandboxes outside the kit's grants, audit and limits. This ADR proposes a new tool family, **confined code execution**: one tool, `run_code`, that runs Python or a shell script inside a box that holds no secret, reaches no network and sees exactly one directory, the workspace of the turn's subject. Its permission, `compute:workspace`, belongs to a new top-level `Compute` family at **T2**, because what the code can reach is fixed by the box and not by the model. R4 and the T3 ban stay absolute. The box is a port; phase 1 ships a hardened bubblewrap box (namespaces, a seccomp allowlist, cgroup v2 limits, no nested user namespaces), and phase 2 adds a gVisor box behind the same port and the same conformance suite. The agent opts in, the deployer grants and configures, output returns as an untrusted-data block, and every execution is audited.

## Context

**What runs code today.** Two tool families, both on one seam, `_run_argv` (`src/agents_system/connectors/operator.py:537-669`):

- `use_term` and `read_file`, tier T3 under `exec:command` and `read:files` (`operator.py:1011-1053`). `use_term` checks only `argv[0]` against an allowlist (`operator.py:695-703`) and binds one deployment-chosen `TerminalPolicy.root` read-write for every call (`operator.py:152-163`), shared by every user of the runtime.
- Declarative command tools: a fixed argv whose parameters are checked against a pattern (`src/agents_system/connectors/command_tools.py:145-217`), each call in a fresh scratch directory removed afterwards (`command_tools.py:198-215`), limited to T2 (`src/agents_system/harness/loader.py:659-671`).

**The box they share.** `_bwrap_argv` runs `bwrap --unshare-all --die-with-parent`, binds `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64` read-only (`_SYSTEM_RO_BINDS`, `operator.py:259`) and a seven-entry `/etc` allowlist (`_SYSTEM_ETC_BINDS`, `operator.py:281-289`), mounts its own `/proc`, `/dev` and a `/tmp` tmpfs, binds the root read-write and clears the environment to four fixed variables (`operator.py:384-417`, `:221-235`). `SandboxPolicy` defaults to no network and refuses binds of the secrets directory, `/`, `/home`, `$HOME`, `/etc` and `/run` (`operator.py:87-137`). Limits are `RLIMIT_AS` and `RLIMIT_CPU` through `prlimit` (`operator.py:440-458`), a wall-clock timeout and a per-stream output cap (`operator.py:173-181`); a timeout or cancellation kills the whole process group (`operator.py:644-661`). Missing `bwrap` or `prlimit` refuses every call, never runs unsandboxed (`operator.py:566-583`). Host requirements are in `docs/operations/sandbox-bwrap.md:10-41`.

**What that box does not have.** No seccomp filter, no cgroup, so no process-count limit and no memory accounting beyond address space, no file-size limit, no size cap on `/tmp`. Measured on a Linux 6.18 host with bubblewrap 0.11.2: under today's flags a sandboxed process can create a nested user namespace and become uid 0 inside it (`unshare -U -r id -u` prints `0`); adding `--unshare-user --disable-userns` makes the same call fail. `--disable-userns` requires an explicit `--unshare-user`, because `--unshare-all` only *tries* the user namespace ([bwrap manual](https://raw.githubusercontent.com/containers/bubblewrap/main/bwrap.xml)).

**The rule.** ADR-002 C.11 made `untrusted_input` and `exec:*` mutually exclusive; ADR-003 R4 generalized it to every T3 class (`docs/architecture/adr-003-permission-hierarchy.md:36`). It is enforced three times: at load (`loader.py:3064-3092`), at grant time in `build_runtime` (`src/agents_system/harness/factory.py:625-635`) and at injection (`src/agents_system/harness/injector.py:120-128`). Its reason holds: an instruction planted in a message must not become code running on the host.

**What exists to build on.** The turn subject and a scope handle bound to (runtime id, subject key) that a `subject_scoped` tool receives and the model never sees (`src/agents_system/harness/registry.py:69-74`, `src/agents_system/harness/interceptor.py:230-250`, `src/agents_system/scope.py:20-35`); the subject data registry with export, delete and retention (`src/agents_system/subject_data.py:42-59`, `:80`); untrusted-data blocks with a per-turn random boundary and size caps (`src/agents_system/agent/untrusted.py:23-61`, `:84-100`), which today reach the model only next to the system prompt (`src/agents_system/agent/graph.py:478-483`), while tool output still enters as a plain tool message (`graph.py:676-684`); deployment-raised execution limits bounded by the outbox lease (`src/agents_system/execution_limits.py:1-14`, `:51-53`); and the audit `actor` and `subject` fields (`src/agents_system/audit/events.py:27-28`). A precedent: ADR-007 put model-written SQL at T2, because its reach is bounded by the database, not by the model (`docs/architecture/adr-007-read-only-sql-tool.md:55-60`).

**No approval mechanism exists.** `autonomy: confirm` is parsed and checked for monotonicity (`loader.py:3668`, `:3850`), and nothing outside the loader reads it at call time.

**Numbering.** ADR-006 is reserved for delegation (#170), ADR-008 (#159) and ADR-009 (#189) are proposed in open pull requests, and ADR-010 and ADR-011 are accepted. This is ADR-012.

## Decision

### 1. A confined code tool, distinct from operator tools

- **One tool, `run_code`**, with inputs `language` (`python` or `shell`) and `code` (a string, capped at 64 KiB). The code arrives on stdin of a fixed interpreter (`python3 -I -` or `sh -s`); no argv is model-chosen. Both languages run in the same box: Python can start the shell inside the box anyway, so the language is a convenience, and the box is the boundary.
- **Permission `compute:workspace`**, in a new top-level `Compute` family at **T2**. Not a `Run` or `Exec` subclass, so no existing grant covers it (ADR-003 R3), and being T2 it is revalidated on every call (`interceptor.py:50`).
- **Why T2 and not T3.** T3 means host execution with a deployer-chosen reach. Here the reach is fixed by the kit: one workspace, no network, no secret. As with ADR-007, declaring it T3 would borrow R4 for a different reason; the residual risk of running on the host kernel is stated in the threat model instead of hidden in a tier.
- **Subject-scoped.** `run_code` sets `subject_scoped=True`, so a turn without a subject is refused and audited before the connector runs (`interceptor.py:230-250`).
- **Operator tools do not change.** `use_term`, `read_file` and command tools keep their seam, tiers and host requirements.

### 2. The untrusted-input rule

- **R4 and C.11 stay absolute for T3.** An `untrusted_input` agent still holds no T3 permission and receives no T3 tool. C.11 and R4 gain one scope note: confined compute is not host execution in their sense, because its reach does not depend on the deployer's choices.
- **The confined profile is the only profile.** There is no field that adds network, binds, environment variables or capabilities to `run_code`, for any agent. An agent that needs more is an operator agent with `use_term`, and R4 keeps it away from untrusted input.
- **What a fully attacker-controlled model gains**: the ability to compute over files this subject already has, within limits, and nothing else. The threat model below states what it can and cannot do.

### 3. The box

- **A port, `CodeBox`**, with one call: run this code, in this workspace directory, under these limits, and return exit status, capped output and resource figures. The kit ships the implementations; each must pass the conformance suite (part 7).
- **Phase 1, `BubblewrapBox`**, a separate argv builder that reuses the seam's pieces (`_SYSTEM_RO_BINDS`, `_SYSTEM_ETC_BINDS`, `_child_env`, the kill and reap) and adds:
  - `--unshare-all --unshare-user --disable-userns`, so the code cannot create namespaces; bwrap runs unprivileged, and the code holds no capability (measured: `CapEff` is `0` inside);
  - a **seccomp allowlist** passed with `--seccomp`, default deny with `EPERM`, in the shape of Docker's default profile, which "disables around 44 system calls out of 300+" ([Docker docs](https://docs.docker.com/engine/security/seccomp/)); it denies, among others, `mount`, `unshare`, `setns`, `ptrace`, `bpf`, `keyctl`, `perf_event_open`, `userfaultfd` and `io_uring_*`. The kit ships it compiled to cBPF for x86_64 and aarch64; any other architecture refuses;
  - a **cgroup v2 leaf per execution**, created before the code starts, with `pids.max`, `memory.max` (swap off) and `cpu.max`, and torn down with `cgroup.kill` ([kernel docs](https://docs.kernel.org/admin-guide/cgroup-v2.html)). It needs a cgroup v2 subtree delegated to the platform's service, such as systemd `Delegate=yes` ([systemd](https://systemd.io/CGROUP_DELEGATION/)). `RLIMIT_NPROC` cannot replace `pids.max`: it counts every thread of the real user id ([getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html)), the platform's own included;
  - `prlimit` for `RLIMIT_CPU` (backstop), `RLIMIT_FSIZE` and `RLIMIT_NOFILE`, and `--size` on the `/tmp` tmpfs;
  - **no network**: no `--share-net` and no `resolv.conf`, with no field to change it;
  - **a cleared environment**: `PATH`, `HOME=/workspace`, `LANG`, `LC_ALL`, nothing else;
  - **a read-only system**: the fixed system paths and the `/etc` allowlist, plus read-only toolchain directories the deployer names (a Python environment with its packages), validated like `extra_ro_binds` today (`operator.py:117-137`); never anything the agent names;
  - the workspace bound read-write at the fixed path `/workspace`, so the host layout is not shown.
- **Default limits**, set by the deployer per runtime and only tightened by the agent, as with #278:

| Limit | Default | Enforced by |
|---|---|---|
| Wall time | 10 s, never above the runtime's `tool_call_timeout_s`, so within the outbox lease (`execution_limits.py:51-53`) | Timeout, then `cgroup.kill` |
| CPU | 1 CPU; CPU time as a backstop | `cpu.max`; `RLIMIT_CPU` |
| Memory | 512 MiB, no swap | `memory.max` |
| Processes | 64 | `pids.max` |
| File size; open files | 64 MiB; 256 | `RLIMIT_FSIZE`; `RLIMIT_NOFILE` |
| `/tmp` | 64 MiB | tmpfs `--size` |
| Output | 8 KiB per stream | Capped read, as `_read_capped` (`operator.py:499-534`) |
| Concurrency | 4 boxes per process, then the call refuses with `busy` | A semaphore in the box |

- **Fail closed.** No `bwrap`, no `prlimit`, no delegated cgroup, an unsupported architecture or a box that fails its boot self-check: the tool refuses every call with `sandbox_unavailable`, logged at boot as today (`operator.py:461-493`).
- **Linux only.** Elsewhere the library imports and `run_code` refuses.
- **Weighing the options:**

| Option | Boundary | Host needs | Operating cost | Start latency |
|---|---|---|---|---|
| bubblewrap + seccomp + cgroups | Host kernel behind namespaces and a syscall filter | Unprivileged user namespaces (AppArmor profile on Ubuntu 23.10+), `bwrap`, `prlimit`, cgroup v2 delegation | Low: no daemon, nothing as root | Measured: `true` 4 ms, `python3 -c 'print(1)'` 15 ms (median, same host as above) |
| gVisor (`runsc`) | A user-space kernel in Go intercepts syscalls; the host kernel sees the Sentry's calls ([gVisor](https://gvisor.dev/docs/)) | `runsc` binary; systrap platform needs no KVM ([platforms](https://gvisor.dev/docs/architecture_guide/platforms/)); rootless mode is "mainly only suitable for `runsc do`" and has no netstack ([rootless](https://gvisor.dev/docs/user_guide/rootless/)) | Medium: a second runtime to install and update; "poor performance for system call heavy workloads" and some syscalls unimplemented | Not measured here; the docs attribute most container start time to Docker, not `runsc` |
| Firecracker microVM | Hardware virtualization through KVM, five emulated devices, plus the jailer ([Firecracker](https://firecracker-microvm.github.io/)) | `/dev/kvm` (bare metal or nested virtualization), a guest kernel and root filesystem image, the jailer started as root ([jailer](https://github.com/firecracker-microvm/firecracker/blob/main/docs/jailer.md)) | High: images, root setup, a VM per execution | Vendor figure: boot under 125 ms, under 5 MiB overhead; not measured here |

- **Strength, plainly.** The bubblewrap box shares the host kernel: a kernel bug reachable through an allowed syscall is an escape. Unprivileged user namespaces are "broadly used as a step in several privilege escalation exploit chains", and Ubuntu cites Google's finding that 44% of the exploits seen by kCTF needed them ([Ubuntu](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces)). Denying nested namespaces and capabilities removes that step for the code inside; this is an inference, not a measurement. gVisor puts a memory-safe kernel between the code and the host; a microVM puts hardware virtualization there.
- **Recommendation: phase 1 bubblewrap, phase 2 gVisor.** Phase 1 runs on every host the kit already supports, at a few milliseconds per call. Phase 2 adds `GVisorBox`, selected by the deployer, after a spike measures latency and rootless operation with no network. A microVM stays outside the kit: the port lets an adopter with KVM plug one in.

### 4. The workspace

- **One per (runtime id, subject key)**, at `<workspace_root>/<sha256(runtime id)>/<sha256(subject key)>`. The deployer sets `workspace_root`, validated like `TerminalPolicy.root` (`operator.py:183-193`) and outside the secrets directory. The platform derives the path from the scope handle; the model never names it, and a hash cannot carry path syntax.
- **One execution per workspace at a time**, by a lock, so a scheduled turn and a channel turn of one subject cannot interleave.
- **Data in**: files written into the workspace by other tools through a `WorkspaceFiles` method on the scope handle (a report export, an uploaded attachment). Never database credentials or tokens: the box has no network to use them and no environment to receive them.
- **Data out**: the capped output (part 5), and files left in the workspace that other tools read through the same handle.
- **Host-side access never follows a link.** Every host-side read of a workspace (tools, export, quota check) opens regular files only and refuses symlinks, FIFOs and devices, because code inside can plant `leak -> /etc/agents-system/agents-system.env` for the host to follow.
- **Quota.** `RLIMIT_FSIZE` caps each file; a size check after each call marks a workspace over its quota (256 MiB by default), and the next call refuses until it shrinks.
- **Retention, export, delete.** A `SubjectWorkspaceStore` implements `SubjectDataStore` and `RetainedStore` (`subject_data.py:42-59`) and registers in the subject data registry, so `export_subject` returns the files and `delete_subject` removes the directory; retention purges by age through the one purger (ADR-011 part 6). `run_code` refuses to build when the store is not registered.

### 5. Output and audit

- **Output is an untrusted-data block.** stdout and stderr, each capped, return as `UntrustedData(source="run_code", ...)` rendered with a fresh boundary inside the tool message, and count toward `max_context_tokens`. This is ADR-011 part 4's third consumer, tool output: `ToolSpec` gains a typed `untrusted_output: bool`, and `run_code` sets it with no way to unset it. With an attacker-controlled model the block changes nothing; it protects an honest model from a hostile file it was asked to analyze.
- **Every execution is audited**, refusals included, as a `code_executed` event with the actor and subject in their columns, and in the payload the runtime id, box name and version, language, SHA-256 and size of the code, the limits applied, the outcome (`ok`, `exit_nonzero`, `timeout`, `memory`, `pids`, `file_size`, `busy`, `refused:<reason>`), exit code, wall and CPU seconds, peak memory, output bytes and truncation, and workspace size. The code text goes in the payload only when the deployment's audit policy keeps tool inputs, through the existing redactor.

### 6. Who enables it

- **Three must agree**, as with registration (ADR-004 4b): the agent declares `run_code` and `compute:workspace` in its manifest and may tighten limits with typed `policy.md` fields; the deployer grants `compute:workspace` in `DEPLOY_GRANTS` or `grants=`; and the deployer passes a `ComputePolicy` (workspace root, toolchain directories, limits, box) to `create_app(compute=...)` or `build_runtime(compute=...)`. Without the policy the tool refuses every call, like an unconfigured `use_term` (`operator.py:910`).
- **No human approval checkpoint** (open question 4). For an untrusted-input agent the approver would be the end user, who may be the attacker, or an operator per message, which ends the use case. The box is the control.

### 7. Migration and conformance

- **An adopter running its own sandbox** maps its tool to `run_code`, copies each user's files into the kit's workspace path, removes every credential its box held, grants and configures as in part 6, runs the conformance suite on its host, and retires its box. Keeping it is possible as a `CodeBox` implementation that passes the same suite; the kit supports only the boxes it ships.
- **The conformance suite** is a mechanism-suite set of escape attempts (ADR-010 part 1): a scripted model always emits the attack, and each guard has a twin with the guard removed that must fail (`tests/test_mechanism_suite.py:1-11`). It covers network (an external address, DNS, a host loopback port), secrets (environment, the secrets directory, host home), other workspaces (path guessing, `..`, a planted symlink read by a host-side tool, a FIFO), writes outside the workspace, nested user namespaces, `mount`, `ptrace`, `bpf`, `keyctl`, `io_uring`, a fork bomb, a memory bomb, a CPU spin past wall time, an oversized file, a quota overrun, a process that tries to outlive the call, terminal injection, host `/proc`, output floods and a forged block boundary. It extends today's sandbox tests (`tests/test_sandbox_integration.py:64-121`) and is shipped so an adopter can run it on a deployment host.

## Threat model

The attacker controls every token the model emits: every `run_code` input, every other tool call and the reply. They do not control the tools, grants, limits, box profile, workspace path or system prompt, all fixed before the turn (ADR-011 part 4).

- **Inside the box the attacker can:** run any code within the limits; read, change and delete every file in this subject's workspace; read the public system tree and `/proc/self/mountinfo`, which lists host mount sources (accepted in #239, `docs/operations/sandbox-bwrap.md:204`); use CPU, memory, processes and time up to the limits on each call, as many calls as `max_tool_calls` and the subject's budget allow; plant links, FIFOs or large files for host-side readers; and call any syscall the allowlist permits.
- **The attacker cannot:** open a network connection; read a secret, since the environment is cleared, the secrets directory is never bound and no credential enters the workspace; see another subject's workspace, since one directory is bound and the platform chose it; outlive the call; create a namespace, mount or gain a capability; change its own grants, limits, tools or prompt; or widen the profile, since no field does.
- **Beyond the box**, the reach is the agent's other tools. No delivering tool takes a model-chosen address (ADR-011 part 5), so workspace data reaches the subject who owns it, and whatever the deployer's other grants allow.
- **Residual risk:**
  - **Kernel escape.** A bug reachable through an allowed syscall gives the attacker the platform's own user, which can read the deployment's secrets file, or root. Phase 2 shrinks this surface; a dedicated host or VM for a deployment serving untrusted input limits the blast radius.
  - **Exhaustion within the limits.** Up to 4 boxes per process at their limits, on every call the turn and budget allow. Per-user budgets price model tokens, not CPU seconds.
  - **The user's own data.** Hostile code, or an honest model misled by a hostile file, can destroy or alter the workspace; phase 1 keeps no snapshots.
  - **Side channels.** Shared CPU caches and timing are not closed by namespaces.
  - **Deployer configuration.** A toolchain directory that holds a secret is readable inside; validation catches the known paths only.

## Migration

No existing behavior changes. Operator tools, command tools, R4, C.11 and their enforcement points are untouched; `ToolSpec` gains a defaulted field; `compute:workspace` is new, so no existing grant covers it. Hosts gain requirements only when they enable `run_code`: cgroup v2 delegation and a bubblewrap with `--disable-userns`. Adopters move as in part 7, and the docs give the steps.

## Consequences

- An agent behind a channel can analyze its user's data with code, inside the kit's grants, audit and limits, instead of an adopter's own sandbox.
- The kit owns a security boundary against arbitrary code. A kernel escape becomes the kit's incident class; the conformance suite and phase 2 are how it answers.
- Untrusted-input deployments with `run_code` need a host with cgroup delegation and, by recommendation, a dedicated host.
- Tool output becomes untrusted data for the first time, starting with `run_code`.
- ADR-002 C.11 and ADR-003 R4 get a pointer to this ADR on acceptance.

## Open questions for the owner

1. **The untrusted-input rule.** Keep C.11 absolute, so no code for an `untrusted_input` agent, or allow the confined profile as a T2 `Compute` family. **Recommendation:** allow it. R4 stays absolute for T3, and the box, not the tier, carries the guarantee.
2. **Which box an untrusted-input agent may use.** Phase 1 bubblewrap, or only the phase-2 gVisor box. **Recommendation:** phase 1, with the kernel residual in the docs and a dedicated host recommended; the deployer's grant is the acceptance.
3. **What runs.** Python, a shell, or both. **Recommendation:** both under one tool, since Python can start the shell inside the box anyway.
4. **Human approval for untrusted-input agents.** **Recommendation:** none; the approver would be the end user or an operator per message.
5. **A host without cgroup delegation.** Refuse, or run with rlimits only. **Recommendation:** refuse; `pids.max` is what stops a fork bomb.
6. **Workspace quota.** A size check after each call, or filesystem project quotas. **Recommendation:** the check plus `RLIMIT_FSIZE` in phase 1; project quotas as optional hardening.

## Follow-ups

Issues to file once this ADR is accepted (none is filed now), in dependency order:

1. **Escape-attempt suite.** The mechanism-suite escape attempts of part 7 with their guard-off twins, run first against today's seam to record the baseline (nested user namespaces, process count and file size are expected to fail).
2. **`BubblewrapBox` and the `CodeBox` port**, with the seccomp allowlist, cgroup leaf, rlimits and boot self-check, until item 1 passes.
3. **`Compute` family, `compute:workspace`, `ComputePolicy` and `run_code`**, subject-scoped, with its capability live scenarios.
4. **`SubjectWorkspaceStore` and `WorkspaceFiles`**, with link-free host access, quota, retention, export and delete.
5. **Untrusted tool output**: `ToolSpec.untrusted_output` and its rendering. Items 3 to 5 can run in parallel once item 2 lands.
6. **`code_executed` audit event**, and CPU seconds in the usage records.
7. **Docs EN + ES**: a code-execution page, the host requirements, the adopter migration guide, the features row, and the C.11 and R4 pointers.
8. **Later: `GVisorBox`**, after a spike on rootless `runsc` with no network and its latency.
