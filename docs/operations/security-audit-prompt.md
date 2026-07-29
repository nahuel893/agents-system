# Prompt: full host security audit

Paste the block below into a dedicated session. It is written to be
self-contained — the session should not need this repository's history to do
the work.

Run it from `/home/nh/agents-system` so the agent can reach the incident record
in `docs/operations/dev-environment-security.md`.

---

```
You are auditing the security posture of a Linux workstation after a confirmed
compromise. This is the owner's own machine and the audit is authorised by
them; you are working defensively, on their behalf.

## Machine

- Manjaro Linux (Arch-based), kernel 6.12.95, package manager `pacman`
- Single human account `nh` (uid 1000, zsh). Only other uid-0 account is `root`
- LAN 192.168.1.0/24, host at 192.168.1.3, router at 192.168.1.1
- Tailscale active: interface `tailscale0`, host address 100.76.213.43.
  `tailscale serve` proxies `/` to localhost:3333, tailnet-only (not funnel)
- Docker installed, several containers (Postgres, Redis, Open WebUI)
- `ufw` installed; enabled state unverified
- Tooling available: `rg`, `fd`, `bat`, `eza`. Prefer them over grep/find/cat/ls

## What already happened — do not re-derive this, but DO re-verify it

On 2026-07-28 the local Docker Postgres was published on 0.0.0.0:5432 with
password `postgres`. An automated attacker dropped every database, created a
`readme_to_recover` ransom database, created role `r0` and database `rdb`,
planted a `pwn()` trigger designed to grant `r0` superuser the next time an
admin touched the table, and set the `postgres` role NOLOGIN.

The route in was the router: **DMZ was enabled toward 192.168.1.3**, forwarding
all unsolicited inbound traffic to this host. Everything bound to 0.0.0.0 was
internet-facing, including OpenSSH — which had no `PasswordAuthentication`
directive anywhere, so the OpenSSH default of `yes` applied.

`lastb` recorded 97,919 failed SSH attempts between 2026-07-10 and 2026-07-28
from 339 source addresses trying 2,187 usernames. The real account `nh` was
tried 24 times. A prior review concluded SSH was never breached, based on:
`wtmp` showing only console and Tailscale-CGNAT logins; no added accounts; no
`authorized_keys` anywhere; and no cron or `/etc/systemd/system` persistence.

**Treat that conclusion as a hypothesis, not a finding.** It was reached
without root, so it could not read `journalctl`, could not run `lastb`
directly, and explicitly did NOT check systemd *timers*, *user* units under
`~/.config/systemd/user/`, or shell rc files. Re-verify everything and assume
the earlier reviewer missed things, because it did.

Containment already done: containers stopped, compose rebound to 127.0.0.1,
compromised Postgres volume destroyed and rebuilt from a deterministic seeder.

## Your job

A full audit. You will need `sudo` for most of it — ask the operator to run
anything you cannot, and tell them exactly what to paste.

**1. Log review** — the part the previous pass could not do.
   - `journalctl` in full for the window 2026-07-01 onward: `sshd`, `sudo`,
     `systemd`, kernel. Look for `Accepted` lines, `session opened for user
     root`, unexpected `sudo` invocations, OOM/segfault patterns around the
     attack window, and any service that started that nobody installed.
   - Cross-check `journalctl` against `wtmp`/`btmp`. They are separate records;
     a discrepancy between them is itself a finding.
   - Docker daemon logs and per-container logs for anything still present.

**2. Integrity.** On Arch this is unusually strong — use it.
   - `pacman -Qkk` to verify every installed file against the package database.
     Triage the output: config-file differences are expected, a modified
     binary in `/usr/bin` is not.
   - `pacman -Qm` for foreign/AUR packages, which `-Qkk` cannot verify.
   - SUID/SGID inventory across the filesystem; flag anything outside the
     standard set.
   - Files modified in `/usr`, `/etc`, `/opt`, `/usr/local` between 2026-07-10
     and now.

**3. Persistence — every vector, not the obvious three.**
   - systemd: system units, **timers**, `systemd-run` transient units, and
     **user units** under `~/.config/systemd/user/` and `/etc/systemd/user/`
   - cron: user crontabs, `/etc/cron*`, at jobs
   - shell: `.zshrc`, `.zprofile`, `.zshenv`, `.bashrc`, `.profile`,
     `/etc/profile.d/*` — check for appended or obfuscated lines
   - SSH: `authorized_keys` anywhere, `~/.ssh/config`, `ssh_config.d`
   - `~/.config/autostart/`, XDG autostart, desktop entries
   - LD_PRELOAD in `/etc/ld.so.preload` and environment files
   - kernel modules: `lsmod` against what the packages provide
   - dropped binaries in `/tmp`, `/var/tmp`, `/dev/shm`

**4. Network exposure, current state.**
   - Everything listening, and on which address — distinguish 0.0.0.0 from
     127.0.0.1 from the tailnet address
   - `ufw` status and whether it is actually enabled at boot
   - The `DOCKER-USER` iptables chain. Docker inserts rules ahead of the host
     INPUT chain, so ufw does NOT protect a published container port. Verify
     this is guarded, not assumed
   - Established outbound connections; anything talking to an address nobody
     can account for
   - Every `docker-compose*.yml` on the machine for `"PORT:PORT"` bindings,
     which publish on all interfaces

**5. Docker specifically.**
   - Is the docker socket exposed over TCP, or bind-mounted into any container
   - Containers running as root, with `--privileged`, or with host networking
   - Image provenance and age; anything pulled that nobody remembers pulling

**6. Secrets hygiene.**
   - Which credentials were reachable during the exposure window and must be
     rotated. The Open WebUI container ran with `WEBUI_AUTH=False` on an
     internet-facing port with an API key in its environment — treat that key
     as disclosed
   - Scan the git history of `/home/nh/agents-system` for committed secrets
   - **Never print a credential value.** Report variable names, file paths and
     an assessment. If you must confirm a value exists, confirm existence only

**7. Patch state.** How far behind is the system, and are there known
   vulnerabilities in what is installed and exposed.

## Hard constraints

- **Read-only by default.** Diagnose and report. Do not change configuration,
  do not restart services, do not delete anything without explicit approval
  for that specific action.
- **Never take the operator's access away.** There is currently NO
  `~/.ssh/authorized_keys` on this machine, so disabling SSH password
  authentication right now would end remote access. If you recommend it, say
  in the same breath that a key must be installed first.
- **Never print secret values** — not from `.env`, not from container
  environments, not from logs.
- `scripts/harden_host.sh` in this repo already implements a set of remedies
  and is dry-run by default. Read it before recommending anything, and say
  plainly where you disagree with it.
- The DMZ itself cannot be changed from this host. If it is still enabled,
  that is the finding that outranks everything else.

## Deliverable

A written report, ordered by severity, where each finding carries:

- what you observed, with the exact command and its output
- whether it is **evidence of compromise**, **an exposure**, or **hygiene** —
  keep these three separate and never blur them
- the concrete remediation, and whether it risks losing access

End with two explicit lists: **confirmed clean** (what you checked and found
sound, so nobody re-checks it) and **could not determine** (what you could not
establish and why). A gap you name is useful; a gap you paper over is not.

Save the report to `docs/operations/security-audit-2026-07-29.md`.
```
