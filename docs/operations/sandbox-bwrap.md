# Bubblewrap sandbox — host requirements (ADR-002 C.14)

Every T3 host execution (`use_term`, and every declared `command_tools`
entry) runs inside a `bwrap` sandbox, with resource limits applied via
`prlimit`. This is not optional and has no unsandboxed fallback: a host that
cannot satisfy the requirements below gets a tool that refuses every call,
never a tool that quietly runs unsandboxed (`connectors/operator.py::
_run_argv`, ADR-002 C.14).

## Requirements on the host

- **`bwrap` (bubblewrap)** installed and on `$PATH`.
- **`prlimit`** (part of `util-linux`, present on virtually every
  mainstream Linux distribution) installed and on `$PATH`. It applies
  `RLIMIT_AS`/`RLIMIT_CPU` to the sandboxed process — see "Why `prlimit`,
  not `preexec_fn`" below.
- **Unprivileged user namespaces allowed** for the user the platform runs
  as. Ubuntu 23.10+ (including 24.04 LTS) restricts these by default via
  AppArmor (`kernel.apparmor_restrict_unprivileged_userns=1`), and the
  restriction is triggered specifically by `bwrap`'s default no-network
  posture (`--unshare-net`'s loopback setup needs a capability the
  restriction denies) — so a host that otherwise looks fine will fail every
  sandboxed call until this is addressed. Two ways to fix it, in order of
  preference:
  1. Load the AppArmor profile Debian/Ubuntu ship for exactly this case:
     `sudo apt install apparmor-profiles apparmor-utils`, then install and
     load `/usr/share/apparmor/extra-profiles/bwrap-userns-restrict`. This
     preserves the restriction for every other program on the host.
  2. Disable the restriction outright:
     `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`
     (persist it via a file under `/etc/sysctl.d/`). Broader than option 1;
     acceptable on a host dedicated to running this platform, less so on a
     shared one.

  This is exactly what `.github/workflows/ci.yml` applies for GitHub's
  `ubuntu-latest` runners, which carry the same restriction (see the `ci`
  and `sandbox-integration` jobs).
- Distributions without this AppArmor restriction (most non-Ubuntu Linux,
  and Ubuntu releases before 23.10) need no extra configuration — `bwrap`
  works out of the box for an unprivileged user with `CONFIG_USER_NS`
  enabled, which is the kernel default almost everywhere.

## Why `prlimit`, not `preexec_fn`

An earlier version applied `RLIMIT_AS`/`RLIMIT_CPU` via `preexec_fn` on the
`bwrap`-spawning `asyncio.create_subprocess_exec` call. Python's own docs
call `preexec_fn` unsafe in the presence of threads: it runs in the child
between `fork()` and `exec()`, and if another thread in this (asyncio, and
in production, a server-hosted) process held a lock at the moment of the
fork, the child can deadlock forever holding a lock nothing will ever
release. `prlimit COMMAND` sets the limits on itself before it `exec`s the
real command, so applying it is just another `execvp`, never a fork from
inside the running platform process — this is why `prlimit` is a hard
requirement alongside `bwrap`, not an optional extra.

## Verifying manually

```bash
which bwrap prlimit
prlimit --as=536870912 --cpu=10 -- \
  bwrap --unshare-all --die-with-parent --new-session \
    --ro-bind /usr /usr --proc /proc --dev /dev -- /usr/bin/true \
  && echo "sandbox OK"
```

If this fails with a namespace or permission error (not a missing-binary
error), apply one of the two AppArmor fixes above.

## What `sandbox_unavailable` means

`_run_argv` checks both `bwrap` and `prlimit` availability on **every**
call and fails closed — it never falls back to running a command
unsandboxed, or sandboxed without enforced resource limits. A tool result
with `error_kind: "sandbox_unavailable"` means one of those two binaries is
missing, or the sandbox failed to start (most commonly the user-namespace
restriction above). The accompanying warning-level structlog line
(`operator.sandbox_unavailable` or, for `command_tools`,
the same event under that connector) names which binary and which program
was refused.

## Boot-time signal

`build_terminal_connector` and `command_tools.build_command_tool_connector`
each check availability once, at construction time — which coincides with
application boot, since runtimes are built once at startup (`main.py`'s
`lifespan`), not per request. If either binary is missing, they log an
**error**-level line: `operator.sandbox_unavailable_at_boot`, naming the
role/tool context and which binary is missing. This does **not** stop the
deployment from booting (an unrelated role should not go down because one
role's operator/command tools are misconfigured) — watch application boot
logs for this line rather than assuming a clean start means the sandbox
works; the actual enforcement still happens per call regardless of whether
this line was read.

## Workspace scoping

`TerminalPolicy.root` — the directory bound **read-write** inside every
sandbox — must be a dedicated, purpose-created workspace. Construction
refuses a `root` that resolves to the filesystem root, `/home`, `$HOME`,
`/root`, `/run`, `/var/run`, or the process's own current working directory
(`connectors/operator.py::TerminalPolicy.__post_init__`, ADR-002 C.14
review follow-up). `command_tools` never needs a deployment-configured
root at all: every call gets its own fresh, single-use scratch directory,
created and removed around that one call.
