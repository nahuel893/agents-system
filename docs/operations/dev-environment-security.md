# Dev environment security

This document exists because the local development stack was compromised on
2026-07-28. It records what happened, what the evidence does and does not
prove, and the rules that follow from it.

---

## The incident

`docker-compose.yml` published PostgreSQL on `0.0.0.0:5432` with
`POSTGRES_PASSWORD: postgres`, and Redis on `0.0.0.0:6379` with no
authentication at all. An automated attacker:

- dropped the `badie` database and every other database it could reach;
- created a `readme_to_recover` database holding a ransom note;
- created a `r0` role and an `rdb` database;
- planted a `pwn()` trigger function as a privilege-escalation foothold;
- set the `postgres` superuser role to `NOLOGIN`.

No ransom was paid and nothing was lost. All application data in the dev
environment came from `scripts/seed_demo_data.py`, which is deterministic and
idempotent, so the database was rebuilt byte-identically from source.

**That is luck, not resilience.** The same exposure on a machine holding real
BADIE data would have been an unrecoverable breach with a client-notification
obligation attached.

---

## Containment

1. Stopped both containers.
2. Rebound every published port to `127.0.0.1` in `docker-compose.yml`.
3. Destroyed the compromised `pgdata` volume rather than cleaning it — a
   database an attacker held superuser on cannot be audited back to trusted.
4. Recreated the volume and re-ran the seeder.

The current stack is the post-containment one. The compose file carries a
header comment explaining the binding; do not "simplify" it back to
`"5432:5432"`.

---

## Entry vector: what the evidence shows

The obvious hypothesis was that this machine sits directly on the internet.
It does not.

| Check | Result |
|---|---|
| Host addresses | `enp34s0` = `192.168.1.3/24`, private. No public address on any interface. |
| Default route | via `192.168.1.1` — the host is behind NAT. |
| Public IP seen from outside | `181.86.125.183` — belongs to the **router**, not the host. |
| Tailscale | `tailnet only`. The Open WebUI proxy on `:3333` was never funnelled to the public internet. |
| Router UPnP | IGD present and responding, **mapping table empty** — nothing had punched an automatic hole. |
| Non-loopback listeners today | `0.0.0.0:22` (SSH) and Tailscale's own `:443`, bound to the tailnet address only. |

So the traffic did not arrive at a public interface on this host. For an
internet-based attacker to have reached `5432`, the router must forward it —
a static port-forward or DMZ rule, which UPnP would not list. The alternative
is that the source was already inside the LAN.

### Answered: the router had DMZ enabled toward this host

The operator confirmed it. The router was configured with **DMZ pointing at
`192.168.1.3`**, which forwards all unsolicited inbound traffic to this
machine. That is why the UPnP mapping table was empty and still misleading —
UPnP lists only *dynamic* mappings; a DMZ is a static setting it never
reports. Treat an empty UPnP table as no evidence either way.

**DMZ does not expose a port. It exposes the host.** During the window,
everything bound to `0.0.0.0` was reachable from the internet:

| Port | Service | Exposure |
|---|---|---|
| `5432` | Postgres, password `postgres` | compromised |
| `6379` | Redis, no authentication | an RCE vector, not just a data leak |
| `8080` | Open WebUI, `WEBUI_AUTH=False` | unauthenticated admin, API key in its env |
| `22` | OpenSSH | **accepting passwords** — see below |

**SSH is the one that matters most.** There is no explicit
`PasswordAuthentication no` anywhere in `/etc/ssh/sshd_config` or
`sshd_config.d/`, so the OpenSSH default of `yes` applied: internet-facing
password authentication for the whole exposure window.

A lost database of regenerable demo data is an inconvenience. A shell is not.

### What `lastb` showed: 97,919 failed attempts over 18 days

`btmp` begins 2026-07-10 08:41 and the last attempt lands 2026-07-28 23:35 —
roughly 220 attempts an hour, sustained, for eighteen days. This was not
opportunistic scanning. The door was being worked on continuously.

| | |
|---|---|
| Failed attempts | **97,919** |
| Distinct source addresses | 339 |
| Distinct usernames tried | 2,187 |
| Attempts against the real account (`nh`) | 24 |

Traffic concentrates in a handful of hosting ranges — `91.92.0.0/16` alone
accounts for 41,271 attempts, followed by `45.153.0.0/16` (13,729) and
`45.156.0.0/16` (8,609).

The username list is the usual dictionary — `root` (18,696), `admin`, `user`,
`ubuntu`, `deploy`, `postgres` — with one detail worth noting: **`claude`
(769 attempts) and `openclaw` (521)**. Scanners now include AI-agent tooling
accounts in their wordlists. Assume any convention-named service account is
already on someone's list.

### Verdict: SSH held

Checked along four independent lines, and all four agree:

- **`wtmp`** — every successful login originates from the physical console or
  a Tailscale CGNAT address (`100.64.0.0/10`). Not one from a public address.
- **Accounts** — only `nh` (uid 1000) and `root` (uid 0). No account was added;
  `/etc/passwd` and `/etc/shadow` were last written 2026-07-26, before the
  database attack, with no resulting account.
- **Key persistence** — no `authorized_keys` file exists anywhere under
  `/home` or `/root`. Nothing was planted, and the operator genuinely has no
  keys installed (see below).
- **Scheduled persistence** — the only user crontab is a personal `rclone`
  sync; `/etc/cron.d` holds stock entries only; no systemd unit in
  `/etc/systemd/system` was modified in 25 days. The only `/etc` files touched
  on the day of the attack were `ld.so.cache` and Tailscale's `resolv.conf`
  management.

The database fell and the host did not. That is worth being precise about:
Postgres was reachable with a guessable password, while SSH — despite
accepting passwords, despite 97,919 tries — was not guessed.

**This is not a reason to relax.** It is a near miss measured in password
strength alone, on a service that should never have been answering the
internet in the first place. See `scripts/harden_host.sh`.

### What could not be determined

**The source IP was never recorded.** The `pgvector/pgvector:pg16` image ships
with `log_connections` off, so PostgreSQL logs session *errors* but not who
connected. The current container's log confirms it: the seeder opened many
connections and not one `connection received` line exists.

This is worth stating precisely — destroying the volume did *not* destroy the
evidence, because the evidence was never written. Even a perfectly preserved
volume would not have answered the question.

> **Reading the log later**: it still says
> `listening on IPv4 address "0.0.0.0", port 5432`. That is PostgreSQL inside
> the container and it is correct — the server must accept connections from
> the Docker bridge. What matters is the host-side publish in
> `docker-compose.yml`. Do not confuse the two and "fix" the wrong one.

### Actions this implies

1. **Disable DMZ on the router.** Nothing on this host should be reachable
   from the internet by default. Forward individual ports deliberately, or
   use Tailscale, which is already installed.
2. **Set `PasswordAuthentication no`** and use keys. This is worth doing
   whether or not DMZ comes back.
3. **Rotate the API key that was in the Open WebUI container.** It sat behind
   an unauthenticated admin panel on a port that was internet-facing, not
   merely LAN-facing. Absence of evidence of abuse is not evidence of absence
   — that container's volume was destroyed during containment, so its history
   is gone.
4. **Run `sudo lastb`** to see the volume of failed SSH attempts during the
   window. Informational, but it calibrates how hard the door was tried.

---

## Standing rules

**1. Docker's port publishing bypasses host firewalls.** A rule in `ufw` or
`nftables` does not protect a published container port; Docker inserts its own
forwarding rules ahead of the host `INPUT` chain. The binding in the compose
file *is* the access control.

**2. Bind to `127.0.0.1`, always.** Not because the host is believed to be
unreachable, but because that belief is exactly what failed here. If another
machine needs access, tunnel it:

```bash
ssh -L 5432:localhost:5432 user@host       # from the client machine
tailscale serve --bg --tcp 5432 tcp://localhost:5432   # tailnet only
```

**3. Weak dev credentials are conditional on the binding.** `postgres/postgres`
is tolerable only while the port is not routable. Widening the binding is not
a one-line change — it means rotating the credential first.

**4. Enable connection logging before you need it.** Add
`command: ["postgres", "-c", "log_connections=on"]` to the compose service if
this stack is ever reachable from anything but loopback. An incident you
cannot attribute is an incident you cannot close.

**5. Real client data does not belong in this stack.** The BI agent reaches
production-like data through a dedicated read-only role
(`docs/architecture/bi-readonly-db-role`), not through the compose Postgres.
Keep it that way.
