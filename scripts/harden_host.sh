#!/usr/bin/env bash
#
# harden_host.sh — host hardening for the agents-system development machine.
#
# Written after the 2026-07-28 compromise, in which the router had DMZ enabled
# toward this host and every service bound to 0.0.0.0 was reachable from the
# internet. See docs/operations/dev-environment-security.md.
#
# DESIGN NOTE — this script refuses to lock you out.
#
#   Disabling SSH password authentication without an installed public key is
#   the single most common way a hardening script bricks remote access. This
#   one checks first and skips that step, loudly, rather than applying it.
#
# It is DRY RUN by default. Nothing changes until you pass --apply.
#
#   sudo ./scripts/harden_host.sh              # show what would change
#   sudo ./scripts/harden_host.sh --apply      # do it
#   sudo ./scripts/harden_host.sh --audit      # report current state only
#
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename -- "${BASH_SOURCE[0]}")"
readonly SCRIPT_DIR SCRIPT_PATH

readonly SSHD_DROPIN="/etc/ssh/sshd_config.d/00-hardening.conf"
readonly SYSCTL_DROPIN="/etc/sysctl.d/99-hardening.conf"
readonly INSTALLED_COPY="/usr/local/sbin/agentsys-harden"
readonly DOCKER_GUARD_UNIT="/etc/systemd/system/agentsys-docker-guard.service"

DRY_RUN=true
AUDIT_ONLY=false
WITH_FAIL2BAN=false
DOCKER_GUARD_ONLY=false
LAN_CIDR=""
BACKUP_DIR=""
declare -a WARNINGS=()
declare -a SKIPPED=()

# ---------------------------------------------------------------- logging ---

log()      { printf '%s\n' "$*" >&2; }
log_step() { printf '\n\033[1m== %s\033[0m\n' "$*" >&2; }
log_info() { printf '   %s\n' "$*" >&2; }
log_ok()   { printf '   \033[32m✓\033[0m %s\n' "$*" >&2; }
log_warn() { printf '   \033[33m!\033[0m %s\n' "$*" >&2; WARNINGS+=("$*"); }
log_skip() { printf '   \033[33m→ SKIPPED\033[0m %s\n' "$*" >&2; SKIPPED+=("$*"); }
log_err()  { printf '   \033[31m✗\033[0m %s\n' "$*" >&2; }

trap 'log_err "failed at line $LINENO (exit $?)"' ERR

usage() {
    cat <<'EOF'
Usage: sudo ./scripts/harden_host.sh [OPTIONS]

Applies host hardening: SSH, ufw, the Docker firewall bypass, and sysctl.
DRY RUN unless --apply is given.

Options:
      --apply             Actually make changes (default is dry run)
      --audit             Report current state and exit; changes nothing
      --lan-cidr CIDR     Trusted LAN, e.g. 192.168.1.0/24 (auto-detected)
      --with-fail2ban     Also install and enable fail2ban for sshd
      --docker-guard-only Re-apply only the DOCKER-USER rules (used by systemd)
  -h, --help              This message

What it will NOT do:
  * Disable SSH password authentication when no authorized_keys exists.
  * Touch net.ipv4.ip_forward — Docker and Tailscale both require it.
  * Turn off the router's DMZ. That is not reachable from this host.
EOF
}

# ------------------------------------------------------------ arg parsing ---

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)             DRY_RUN=false; shift ;;
        --audit)             AUDIT_ONLY=true; shift ;;
        --with-fail2ban)     WITH_FAIL2BAN=true; shift ;;
        --docker-guard-only) DOCKER_GUARD_ONLY=true; shift ;;
        --lan-cidr)
            [[ $# -ge 2 ]] || { log_err "--lan-cidr needs a value"; exit 1; }
            LAN_CIDR="$2"; shift 2 ;;
        -h|--help)           usage; exit 0 ;;
        *) log_err "unknown option: $1"; usage; exit 1 ;;
    esac
done

# --------------------------------------------------------------- helpers ---

run() {
    if [[ "$DRY_RUN" == true ]]; then
        printf '   [dry-run] %s\n' "$*" >&2
        return 0
    fi
    "$@"
}

# Write a file through a temp file so a partial write can never land.
write_file() {
    local -r target="$1"
    local content; content="$(cat)"

    if [[ -f "$target" ]] && [[ "$(cat -- "$target")" == "$content" ]]; then
        log_ok "$target already up to date"
        return 0
    fi

    if [[ "$DRY_RUN" == true ]]; then
        printf '   [dry-run] would write %s (%d lines)\n' \
            "$target" "$(printf '%s' "$content" | grep -c '' || true)" >&2
        return 0
    fi

    backup_file "$target"

    local tmp; tmp="$(mktemp)" || return 1
    printf '%s\n' "$content" >"$tmp"
    chmod 0644 "$tmp"
    mv -- "$tmp" "$target"
    log_ok "wrote $target"
}

backup_file() {
    local -r src="$1"
    [[ -e "$src" ]] || return 0
    [[ -n "$BACKUP_DIR" ]] || return 0
    mkdir -p -- "$BACKUP_DIR"
    cp -a -- "$src" "$BACKUP_DIR/$(basename -- "$src")"
}

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        log_err "must run as root — try: sudo $SCRIPT_PATH $*"
        exit 1
    fi
}

have() { command -v "$1" &>/dev/null; }

# The user who invoked sudo, whose keys actually matter.
target_user() { printf '%s' "${SUDO_USER:-root}"; }

target_home() {
    local -r u="$(target_user)"
    getent passwd "$u" | cut -d: -f6
}

detect_lan_cidr() {
    [[ -z "$LAN_CIDR" ]] || { printf '%s' "$LAN_CIDR"; return 0; }
    local iface
    iface="$(ip -o route show default 2>/dev/null | awk '{print $5; exit}')" || true
    [[ -n "$iface" ]] || return 1
    ip -o route show dev "$iface" proto kernel scope link 2>/dev/null \
        | awk '{print $1; exit}'
}

tailscale_iface() {
    ip -o link show 2>/dev/null | awk -F': ' '/tailscale[0-9]/{print $2; exit}'
}

count_ssh_keys() {
    local -r keyfile="$(target_home)/.ssh/authorized_keys"
    [[ -f "$keyfile" ]] || { printf '0'; return 0; }
    grep -c -E '^\s*(ssh-|ecdsa-|sk-)' -- "$keyfile" 2>/dev/null || printf '0'
}

# ------------------------------------------------------------ step: sshd ---

harden_ssh() {
    log_step "SSH"

    local -r keys="$(count_ssh_keys)"
    local -r user="$(target_user)"
    local disable_passwords=true

    if [[ "$keys" -eq 0 ]]; then
        disable_passwords=false
        log_skip "PasswordAuthentication stays ENABLED — $user has no authorized_keys"
        log_info ""
        log_info "  Disabling it now would end remote SSH for you immediately."
        log_info "  Install a key from your other machine, then re-run:"
        log_info ""
        log_info "      ssh-copy-id ${user}@$(hostname -s)"
        log_info ""
        log_info "  Everything else below still applies."
    else
        log_ok "$user has $keys authorized key(s) — safe to require them"
    fi

    {
        cat <<'EOF'
# Managed by scripts/harden_host.sh — see docs/operations/dev-environment-security.md
#
# This file is named 00- on purpose. sshd_config includes this directory at
# line 2, and for each keyword the FIRST value obtained wins, so a drop-in
# must sort before 20-systemd-userdb.conf and 99-archlinux.conf to take effect.

PermitRootLogin no
PermitEmptyPasswords no
PubkeyAuthentication yes
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
AllowAgentForwarding no
ClientAliveInterval 300
ClientAliveCountMax 2

# AllowTcpForwarding is deliberately left at the default (yes): the runbook
# tells operators to reach the database with `ssh -L` instead of widening a
# container port binding. Removing it would push them back toward the habit
# that caused the 2026-07-28 incident.
EOF
        if [[ "$disable_passwords" == true ]]; then
            cat <<'EOF'

PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
        else
            cat <<'EOF'

# PasswordAuthentication intentionally NOT set: no authorized_keys was present
# when this file was generated. Install a key and re-run the script.
EOF
        fi
    } | write_file "$SSHD_DROPIN"

    if [[ "$DRY_RUN" == true ]]; then
        log_info "[dry-run] would validate with sshd -t and reload sshd"
        return 0
    fi

    if ! sshd -t 2>/dev/null; then
        log_err "sshd -t rejected the new config — reverting"
        rm -f -- "$SSHD_DROPIN"
        sshd -t || log_err "sshd config is broken independently of this script"
        return 1
    fi
    log_ok "sshd -t passed"

    # reload, not restart: existing sessions survive.
    systemctl reload sshd 2>/dev/null || systemctl reload ssh 2>/dev/null \
        || log_warn "could not reload sshd — reload it yourself"
    log_ok "sshd reloaded (existing sessions kept)"
}

# ------------------------------------------------------------- step: ufw ---

configure_ufw() {
    log_step "Firewall (ufw)"

    if ! have ufw; then
        log_warn "ufw not installed — install with: pacman -S ufw"
        return 0
    fi

    local lan ts
    lan="$(detect_lan_cidr || true)"
    ts="$(tailscale_iface || true)"

    [[ -n "$lan" ]] && log_info "trusted LAN: $lan" \
                    || log_warn "could not detect the LAN CIDR — pass --lan-cidr"
    [[ -n "$ts" ]]  && log_info "tailnet interface: $ts" \
                    || log_info "no tailscale interface found"

    # Allow rules FIRST, enable LAST. Enabling a default-deny firewall before
    # permitting your own path in is how remote hardening goes wrong.
    run ufw --force default deny incoming
    run ufw --force default allow outgoing

    if [[ -n "$ts" ]]; then
        run ufw allow in on "$ts" comment 'tailnet — trusted overlay'
    fi

    if [[ -n "$lan" ]]; then
        run ufw allow from "$lan" to any port 22 proto tcp comment 'ssh from LAN only'
    else
        log_warn "SSH not explicitly allowed — verify you keep access before enabling"
    fi

    run ufw logging low
    run ufw --force enable
    log_ok "ufw configured: deny incoming, tailnet trusted, SSH limited to the LAN"
    log_info "note: this does NOT cover published Docker ports — see the next step"
}

# ---------------------------------------------------- step: docker bypass ---

# Docker inserts its own rules ahead of the host INPUT chain, so a published
# container port is reachable even with ufw set to deny incoming. DOCKER-USER
# is the one chain Docker guarantees it will not overwrite.
configure_docker_guard() {
    log_step "Docker firewall bypass (DOCKER-USER)"

    if ! have docker; then
        log_info "docker not installed — nothing to guard"
        return 0
    fi
    if ! have iptables; then
        log_warn "iptables not available — cannot install the DOCKER-USER guard"
        return 0
    fi

    local lan ts
    lan="$(detect_lan_cidr || true)"
    ts="$(tailscale_iface || true)"

    if [[ "$DRY_RUN" == true ]]; then
        log_info "[dry-run] would flush DOCKER-USER and install:"
        log_info "          RETURN  conntrack ESTABLISHED,RELATED"
        [[ -n "$ts" ]]  && log_info "          RETURN  in/out $ts"
        [[ -n "$lan" ]] && log_info "          RETURN  from $lan"
        log_info "          RETURN  from docker bridges"
        log_info "          DROP    everything else"
        return 0
    fi

    iptables -N DOCKER-USER 2>/dev/null || true
    iptables -F DOCKER-USER

    # Return traffic for connections the host or a container opened.
    iptables -A DOCKER-USER -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN

    if [[ -n "$ts" ]]; then
        iptables -A DOCKER-USER -i "$ts" -j RETURN
        iptables -A DOCKER-USER -o "$ts" -j RETURN
    fi
    [[ -n "$lan" ]] && iptables -A DOCKER-USER -s "$lan" -j RETURN

    # Container-to-container and container-to-internet egress.
    iptables -A DOCKER-USER -i docker0 -j RETURN
    iptables -A DOCKER-USER -s 172.16.0.0/12 -j RETURN

    iptables -A DOCKER-USER -m limit --limit 5/min -j LOG --log-prefix 'DOCKER-USER-DROP: '
    iptables -A DOCKER-USER -j DROP

    log_ok "DOCKER-USER guard installed"

    [[ "$DOCKER_GUARD_ONLY" == true ]] && return 0

    # These rules do not survive a reboot or a docker restart.
    install -m 0755 -- "$SCRIPT_PATH" "$INSTALLED_COPY"
    write_file "$DOCKER_GUARD_UNIT" <<EOF
[Unit]
Description=Re-apply the DOCKER-USER firewall guard
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=$INSTALLED_COPY --docker-guard-only --apply
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now agentsys-docker-guard.service &>/dev/null \
        || log_warn "could not enable agentsys-docker-guard.service"
    log_ok "guard will be re-applied on boot and after docker restarts"
}

# ---------------------------------------------------------- step: sysctl ---

configure_sysctl() {
    log_step "Kernel network parameters"

    write_file "$SYSCTL_DROPIN" <<'EOF'
# Managed by scripts/harden_host.sh

# NOT SET ON PURPOSE: net.ipv4.ip_forward.
# Both Docker and Tailscale require forwarding. Turning it off is a classic
# hardening-checklist move that silently breaks every container's networking.

net.ipv4.tcp_syncookies = 1

# Loose (2), not strict (1). Tailscale's own documentation warns that strict
# reverse-path filtering breaks subnet routes and exit nodes.
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.default.rp_filter = 2

net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv4.conf.all.log_martians = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1

net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_source_route = 0

kernel.dmesg_restrict = 1
kernel.kptr_restrict = 1
fs.protected_hardlinks = 1
fs.protected_symlinks = 1
EOF

    run sysctl --system >/dev/null
    log_ok "sysctl applied"
}

# -------------------------------------------------------- step: fail2ban ---

configure_fail2ban() {
    [[ "$WITH_FAIL2BAN" == true ]] || return 0
    log_step "fail2ban"

    if ! have fail2ban-server; then
        if have pacman; then
            run pacman -S --needed --noconfirm fail2ban
        else
            log_warn "fail2ban not installed and pacman not found — install it manually"
            return 0
        fi
    fi

    write_file /etc/fail2ban/jail.d/sshd.local <<'EOF'
# Managed by scripts/harden_host.sh
[sshd]
enabled  = true
backend  = systemd
maxretry = 4
findtime = 10m
bantime  = 1h
EOF

    run systemctl enable --now fail2ban
    log_ok "fail2ban enabled for sshd"
}

# ----------------------------------------------------------------- audit ---

audit() {
    log_step "Current state"

    log_info "--- listening on non-loopback addresses ---"
    ss -tlnH 2>/dev/null | awk '{print $4}' | grep -v '^127\.\|^\[::1\]' | sort -u \
        | sed 's/^/   /' >&2 || log_info "   (none)"

    log_info ""
    log_info "--- ufw ---"
    if have ufw; then
        ufw status verbose 2>/dev/null | sed 's/^/   /' >&2 || log_info "   (needs root)"
    else
        log_info "   not installed"
    fi

    log_info ""
    log_info "--- effective sshd auth settings ---"
    if have sshd; then
        sshd -T 2>/dev/null \
            | grep -E '^(passwordauthentication|permitrootlogin|pubkeyauthentication|kbdinteractiveauthentication|permitemptypasswords|maxauthtries) ' \
            | sed 's/^/   /' >&2 || log_info "   (needs root)"
    fi

    log_info ""
    log_info "--- DOCKER-USER chain ---"
    if have iptables; then
        iptables -S DOCKER-USER 2>/dev/null | sed 's/^/   /' >&2 \
            || log_info "   (absent — docker not running, or needs root)"
    fi

    log_info ""
    log_info "--- compose files binding all interfaces ---"
    local -a offenders=()
    if have rg; then
        mapfile -t offenders < <(
            rg -l --no-messages -e '^\s*-\s*"?[0-9]+:[0-9]+"?\s*$' \
               -g 'docker-compose*.y*ml' -g 'compose*.y*ml' "$SCRIPT_DIR/.." 2>/dev/null || true
        )
    fi
    if [[ ${#offenders[@]} -gt 0 ]]; then
        for f in "${offenders[@]}"; do
            log_warn "publishes on 0.0.0.0: $f — prefer \"127.0.0.1:PORT:PORT\""
        done
    else
        log_ok "no compose port binds all interfaces"
    fi
}

# ---------------------------------------------------------------- report ---

final_report() {
    log_step "Summary"

    if [[ ${#SKIPPED[@]} -gt 0 ]]; then
        log_info "Skipped, on purpose:"
        for s in "${SKIPPED[@]}"; do printf '     - %s\n' "$s" >&2; done
        log_info ""
    fi

    if [[ ${#WARNINGS[@]} -gt 0 ]]; then
        log_info "Warnings:"
        for w in "${WARNINGS[@]}"; do printf '     - %s\n' "$w" >&2; done
        log_info ""
    fi

    if [[ -n "$BACKUP_DIR" && -d "$BACKUP_DIR" ]]; then
        log_info "Originals backed up to: $BACKUP_DIR"
        log_info "Revert a file with: cp -a $BACKUP_DIR/<file> /etc/..."
        log_info ""
    fi

    cat >&2 <<'EOF'
   THIS SCRIPT CANNOT FIX THE ACTUAL ROOT CAUSE.

   The 2026-07-28 compromise happened because the router had DMZ enabled
   toward this host, which forwards all unsolicited inbound traffic here.
   Nothing on this machine can turn that off.

   Open the router at 192.168.1.1 and disable DMZ. Everything above is a
   second line of defense for the day someone turns it back on.

   Also still outstanding: rotate the API key that lived in the Open WebUI
   container. It sat behind an unauthenticated admin panel on a port that
   was internet-facing.
EOF
}

# ------------------------------------------------------------------ main ---

main() {
    if [[ "$AUDIT_ONLY" == true ]]; then
        audit
        return 0
    fi

    require_root "$@"

    if [[ "$DOCKER_GUARD_ONLY" == true ]]; then
        configure_docker_guard
        return 0
    fi

    if [[ "$DRY_RUN" == true ]]; then
        log ""
        log "  DRY RUN — nothing will be changed. Re-run with --apply to commit."
    else
        BACKUP_DIR="/root/harden-backup-$(date +%Y%m%d-%H%M%S)"
        mkdir -p -- "$BACKUP_DIR"
        log ""
        log "  APPLYING. Originals go to $BACKUP_DIR"
    fi

    harden_ssh
    configure_ufw
    configure_docker_guard
    configure_sysctl
    configure_fail2ban
    audit
    final_report
}

main "$@"
