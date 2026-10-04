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
  bwrap --unshare-all --die-with-parent \
    --ro-bind /usr /usr --proc /proc --dev /dev -- /usr/bin/true \
  && echo "sandbox OK"
```

If this fails with a namespace or permission error (not a missing-binary
error), apply one of the two AppArmor fixes above.

## Timeouts and cancellation

Two timers can stop a sandboxed command. `TerminalPolicy.timeout_s` is the
sandbox's own kill timer. The interceptor's `tool_call_timeout_s`
(`agent/graph.py`) bounds the whole tool call from outside and is normally
the tighter of the two. A cancelled task or a shutdown stops the call the
same way the outer timeout does.

Whichever comes first, `_run_argv` kills the command's whole process group
with `SIGKILL` and reaps it before the call returns or the cancellation
propagates. A cancellation is always re-raised, never turned into a
result. That group holds everything the sandbox runs: the launcher
(`prlimit`, which then becomes the outer `bwrap`), bwrap's PID-namespace
init, and the command. So one kill reaches all of them at any moment,
including while bwrap is still starting the sandbox. A process the
command moves into a session of its own leaves the group but not the PID
namespace, and the kernel kills everything in the namespace once its init
is gone. Before #105 the kill ran only in `_run_argv`'s own timeout
handler, so when `tool_call_timeout_s` fired first the command kept
running on the host after the agent had already been told it timed out.

Reaping drains any output still in the pipes and is capped at 5 seconds.
If that runs out, `operator.reap_incomplete` is logged at error level and
the call ends anyway.

A cancellation can also land while the command is still being spawned,
after the child has started but before its pipes are connected. asyncio's
own cleanup for that moment kills only the direct child and then waits for
the pipes to close, which the rest of the group can hold open for as long
as it runs. So `_run_argv` never lets a cancellation reach the spawn: it
awaits the spawn through `asyncio.shield`, lets it finish, and then kills
the group as it would at any other moment. That wait is capped at 5
seconds as well. A spawn still unfinished by then is cancelled and
`operator.spawn_incomplete` is logged at error level.

A second cancellation can land during that wait, for example
`total_execution_timeout_s` expiring a few milliseconds after
`tool_call_timeout_s`. It ends the call at once, before the spawn is done,
so the call never holds the process. The kill does not depend on the call
for this: before it starts waiting, `_run_argv` attaches a callback to the
spawn, and that callback kills the group the moment the spawn completes,
however the call ended. Only the reap is left to asyncio, which collects
the exit status on its own. Without the callback, the whole sandbox kept
running with no time limit.

### Why the sandbox runs without `--new-session`

`--new-session` made bwrap's namespace init call `setsid()`, which moved
the init and the command out of the group `_run_argv` kills. They then
died only through `--die-with-parent`, and bwrap arms that on the init
only after the `setsid()`. A kill that landed between the two orphaned
the whole sandbox, command included, and the reap then waited out its
5-second cap on the pipes the orphans still held open. A test that kills
at that moment leaked the sandbox in about 4 of 10 attempts before the
flag was removed.

What `--new-session` protected against stays closed. `_run_argv` starts
the launcher with `start_new_session=True`, a new session with no
controlling terminal that everything in the sandbox inherits. A sandboxed
command therefore cannot open the caller's terminal to push input into it
with `TIOCSTI` (CVE-2017-5226).
`test_a_sandboxed_command_cannot_open_the_callers_terminal` proves this
from a caller that does have a terminal.

The trade-off is that the sandbox now shares a process group with its own
launcher, so it can signal that launcher, for example with `kill(0, …)`.
That can only end or stall its own call: the timeouts still kill the
group, and the platform process is not in it.

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

## What the sandbox exposes of `/etc`

The sandbox never binds the host `/etc` whole (#238). It holds the
deployment's secrets file (`/etc/agents-system/agents-system.env`) and
`passwd`/`group`, and `use_term` checks only `argv[0]`, so an allowed `cat`
would print them. Only an explicit allowlist is bound read-only
(`_SYSTEM_ETC_BINDS` in `connectors/operator.py`): `ld.so.cache`,
`nsswitch.conf`, `localtime`, `ssl/certs`, `ca-certificates`,
`pki/tls/certs` and `alternatives`. A tool that declares `network=True` also
gets `resolv.conf`; `/etc/hosts` is not bound (#239): public names still
resolve through DNS without it (measured with `getent hosts` under `bwrap`,
with and without the file), and the host's `hosts` file can list internal
names and addresses. A tool that must resolve a private name gets it through
DNS, or through `extra_ro_binds` at the deployer's explicit risk. `passwd`, `group`, `shadow` and
`/etc/agents-system` are never bound.

The list was found by experiment with `bwrap`: `factor`, `ls`, `cat`,
`sleep`, `echo` and a venv Python run with no `/etc` at all, so the entries
exist for portability (library cache, time zone, TLS roots, DNS). The
`alternatives` entry (Debian-family symlinks such as `awk`) was verified in
a `debian:stable-slim` container (#239): without it `awk` fails with
`execvp awk: No such file or directory`, with it `awk` runs. It holds only
symlinks into `/usr`. (Docker blocks mounting `/proc` for `bwrap`, so that
check ran without `--proc`.)

A command that needs another `/etc` file gets it through
`SandboxPolicy.extra_ro_binds` (for example `/etc/ssl`). **Risk:** each extra
bind is readable by every command that tool runs; `SandboxPolicy` refuses `/etc` itself and
`/etc/agents-system` with everything under it (compared by resolved path,
so a symlink into it is refused too). Other `/etc` sub-paths are not refused
by name: a deployer legitimately binds tool config, and the bind list is
their explicit statement. Other
reads outside the root were probed and are closed: a symlink in the root
to a host file resolves to nothing (the target is not mounted), `/proc` is
the sandbox's own PID namespace, and the environment is cleared. The
directory holding `argv[0]` is also readable, as before.

**Known, accepted exposure (#239):** `/proc/self/mountinfo` (and
`/proc/1/mountinfo`) inside the sandbox lists the host's mount sources, such
as device names and btrfs subvolume paths. It holds no secrets and cannot be
hidden with `bwrap` options without breaking things: `self` resolves to the
bind-setup process rather than the command, so only `/proc/1` could be
masked, and `/proc/self` would still show the same data.

## Workspace scoping

`TerminalPolicy.root` — the directory bound **read-write** inside every
sandbox — must be a dedicated, purpose-created workspace. Construction
refuses a `root` that resolves to the filesystem root, `/home`, `$HOME`,
`/root`, `/run`, `/var/run`, or the process's own current working directory
(`connectors/operator.py::TerminalPolicy.__post_init__`, ADR-002 C.14
review follow-up). `command_tools` never needs a deployment-configured
root at all: every call gets its own fresh, single-use scratch directory,
created and removed around that one call.
