#!/bin/bash
#
# ptp-install.sh - one-time PTP setup for a TSN machine.
#
# Installs linuxptp (if missing), the ptp4l/phc2sys systemd services, the two
# PTP profiles (802.1AS gPTP and IEEE 1588v2), the `ptp-profile` switch command,
# and a narrow sudoers rule so that user can run ONLY `ptp-profile` without a
# password.
#
# Run once per machine (e.g. after it is flashed), as root:
#   scp ptp-install.sh <host>:/tmp/ && ssh -t <host> 'sudo bash /tmp/ptp-install.sh'
#
# Options / environment:
#   PTP_IF=<nic>        PTP interface name              (default: tsn0)
#   PTP_USER=<user>     user granted passwordless ptp-profile
#                       (default: the user who ran sudo)
#   --profile gptp|ieee1588   profile to activate         (default: ieee1588)
#   --no-start          install and enable only, do not start the services
#   --no-sudoers        skip the sudoers rule
#   --uninstall         stop and remove everything this script installed (services, profiles,
#                       ptp-profile, sudoers rule). Keeps the linuxptp package and does NOT
#                       re-enable NTP clients or distro ptp4l/phc2sys units.
#
# Safe to re-run: existing profile env files and gPTP.cfg are left untouched.

set -euo pipefail

PTP_IF="${PTP_IF:-tsn0}"
PTP_USER="${PTP_USER:-${SUDO_USER:-}}"
PROFILE="ieee1588"
START=1
SUDOERS=1
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --profile)    PROFILE="${2:-}"; shift 2 ;;
    --no-start)   START=0; shift ;;
    --no-sudoers) SUDOERS=0; shift ;;
    --uninstall)  UNINSTALL=1; shift ;;
    -h|--help)    sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

case "$PROFILE" in
  gptp|ieee1588) ;;
  *) echo "ERROR: --profile must be gptp or ieee1588" >&2; exit 1 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root (sudo bash $0)" >&2
  exit 1
fi

ETC=/etc/linuxptp
UNIT_DIR=/etc/systemd/system

if [ "$UNINSTALL" -eq 1 ]; then
  echo "Uninstalling PTP services..."
  systemctl disable --now phc2sys-tsn.service ptp4l-tsn.service 2>/dev/null || true
  rm -f "$UNIT_DIR/ptp4l-tsn.service" "$UNIT_DIR/phc2sys-tsn.service"
  systemctl daemon-reload
  rm -f /usr/local/bin/ptp-profile /etc/sudoers.d/ptp-profile
  rm -f "$ETC/active.env" "$ETC/gptp.env" "$ETC/ieee1588.env" "$ETC/gPTP.cfg"
  rmdir "$ETC" 2>/dev/null || echo "      kept $ETC (contains other files)"
  echo "Done. linuxptp package left installed; NTP clients and distro ptp4l/phc2sys not re-enabled."
  exit 0
fi

if [ "$SUDOERS" -eq 1 ]; then
  if [ -z "$PTP_USER" ] || [ "$PTP_USER" = "root" ]; then
    echo "ERROR: cannot determine a non-root user for the sudoers rule." >&2
    echo "       Set PTP_USER=<user> or pass --no-sudoers." >&2
    exit 1
  fi
  if ! [[ "$PTP_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || ! id "$PTP_USER" >/dev/null 2>&1; then
    echo "ERROR: invalid or unknown user '$PTP_USER'" >&2
    exit 1
  fi
fi

# --- 1. Install linuxptp if missing -----------------------------------------
if ! command -v ptp4l >/dev/null 2>&1 || ! command -v phc2sys >/dev/null 2>&1; then
  echo "[1/6] Installing linuxptp..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y linuxptp
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y linuxptp
  elif command -v yum >/dev/null 2>&1; then
    yum install -y linuxptp
  elif command -v zypper >/dev/null 2>&1; then
    zypper --non-interactive install linuxptp
  else
    echo "ERROR: no supported package manager found; install linuxptp manually." >&2
    exit 1
  fi
else
  echo "[1/6] linuxptp already installed."
fi

PTP4L_BIN="$(command -v ptp4l)"
PHC2SYS_BIN="$(command -v phc2sys)"
echo "      ptp4l:   $PTP4L_BIN"
echo "      phc2sys: $PHC2SYS_BIN"

# --- 2. Interface sanity check ----------------------------------------------
echo "[2/6] Checking interface $PTP_IF..."
if [ ! -d "/sys/class/net/$PTP_IF" ]; then
  echo "WARNING: interface '$PTP_IF' not found on this machine." >&2
  echo "         Available: $(ls /sys/class/net | tr '\n' ' ')" >&2
  echo "         Re-run with PTP_IF=<nic> or edit $ETC/*.env afterwards." >&2
fi

# --- 3. Profiles ------------------------------------------------------------
echo "[3/6] Writing profiles to $ETC..."
install -d -m 755 -o root -g root "$ETC"

write_if_missing() {  # <path> ; content on stdin
  if [ -e "$1" ]; then
    echo "      keeping existing $1"
    cat >/dev/null
  else
    cat >"$1"
    chown root:root "$1"
    chmod 644 "$1"
    echo "      wrote $1"
  fi
}

write_if_missing "$ETC/gptp.env" <<EOF
# 802.1AS (gPTP)
PTP_IF=$PTP_IF
PTP4L_ARGS=-f $ETC/gPTP.cfg --step_threshold=1 -s
PHC2SYS_ARGS=--step_threshold=1 --transportSpecific=1 -w
EOF

write_if_missing "$ETC/ieee1588.env" <<EOF
# IEEE 1588v2 (default profile, UDP/IPv4)
PTP_IF=$PTP_IF
PTP4L_ARGS=-4 -E -s --priority1=255 --domain=0
PHC2SYS_ARGS=--step_threshold=1 -w -O 0
EOF

# Same content as linuxptp's configs/gPTP.cfg.
write_if_missing "$ETC/gPTP.cfg" <<'EOF'
[global]
#
# Default Data Set
#
twoStepFlag		1
slaveOnly		0
socket_priority		0
priority1		248
priority2		248
domainNumber		0
#utc_offset		37
clockClass		248
clockAccuracy		0xFE
offsetScaledLogVariance	0x436A
free_running		0
freq_est_interval	1
dscp_event		0
dscp_general		0
dataset_comparison	G.8275.x
G.8275.defaultDS.localPriority	128
maxStepsRemoved		255
#
# Port Data Set
#
logAnnounceInterval	0
logSyncInterval		-3
syncReceiptTimeout	3
neighborPropDelayThresh	800
min_neighbor_prop_delay	-20000000
assume_two_step		1
logMinPdelayReqInterval	0
announceReceiptTimeout	3
delay_mechanism		P2P
network_transport	L2
#
# Run time options
#
logging_level		6
path_trace_enabled	1
follow_up_info		1
transportSpecific	0x1
ptp_dst_mac		01:80:C2:00:00:0E
p2p_dst_mac		01:80:C2:00:00:0E
EOF

# --- 4. systemd units -------------------------------------------------------
echo "[4/6] Installing systemd units..."

# Distro-provided units would fight with ours over the same NIC/clock.
for u in ptp4l.service phc2sys.service; do
  if systemctl list-unit-files "$u" 2>/dev/null | grep -q "^$u"; then
    systemctl disable --now "$u" 2>/dev/null || true
    echo "      disabled distro $u"
  fi
done

cat >"$UNIT_DIR/ptp4l-tsn.service" <<EOF
[Unit]
Description=PTP daemon (ptp4l)
Wants=network-online.target
After=network-online.target

[Service]
EnvironmentFile=$ETC/active.env
ExecStart=$PTP4L_BIN -i \${PTP_IF} -m \$PTP4L_ARGS
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat >"$UNIT_DIR/phc2sys-tsn.service" <<EOF
[Unit]
Description=phc2sys (PHC to CLOCK_REALTIME)
Requires=ptp4l-tsn.service
After=ptp4l-tsn.service
BindsTo=ptp4l-tsn.service

[Service]
EnvironmentFile=$ETC/active.env
ExecStart=$PHC2SYS_BIN -s \${PTP_IF} -c CLOCK_REALTIME -m \$PHC2SYS_ARGS
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT_DIR/ptp4l-tsn.service" "$UNIT_DIR/phc2sys-tsn.service"

# --- 5. ptp-profile command -------------------------------------------------
echo "[5/6] Installing /usr/local/bin/ptp-profile..."
cat >/usr/local/bin/ptp-profile <<'EOF'
#!/bin/bash
# ptp-profile gptp|ieee1588|stop|status
set -euo pipefail
ETC=/etc/linuxptp

case "${1:-}" in
  gptp|ieee1588)
    [ -f "$ETC/$1.env" ] || { echo "missing $ETC/$1.env" >&2; exit 1; }
    systemctl stop ptp4l-tsn            # also stops phc2sys-tsn (BindsTo)
    ln -sfn "$ETC/$1.env" "$ETC/active.env"
    systemctl start ptp4l-tsn phc2sys-tsn
    echo "active profile: $1"
    ;;
  stop)
    systemctl stop ptp4l-tsn
    echo "PTP stopped"
    ;;
  status)
    echo "profile:   $(basename "$(readlink "$ETC/active.env" 2>/dev/null || echo none)" .env)"
    for s in ptp4l-tsn phc2sys-tsn; do
      printf '%-12s %s (at boot: %s)\n' "$s" \
        "$(systemctl is-active "$s" || true)" "$(systemctl is-enabled "$s" || true)"
    done
    echo "--- recent log ---"
    journalctl -u ptp4l-tsn -u phc2sys-tsn -n 6 --no-pager -o cat || true
    ;;
  *)
    echo "usage: ptp-profile gptp|ieee1588|stop|status" >&2
    exit 1
    ;;
esac
EOF
chown root:root /usr/local/bin/ptp-profile
chmod 755 /usr/local/bin/ptp-profile

# --- 6. sudoers, enable, start ----------------------------------------------
if [ "$SUDOERS" -eq 1 ]; then
  echo "[6/6] Granting $PTP_USER passwordless 'ptp-profile' only..."
  TMP="$(mktemp)"
  echo "$PTP_USER ALL=(root) NOPASSWD: /usr/local/bin/ptp-profile" >"$TMP"
  if visudo -cf "$TMP" >/dev/null; then
    install -m 440 -o root -g root "$TMP" /etc/sudoers.d/ptp-profile
  else
    rm -f "$TMP"
    echo "ERROR: generated sudoers rule failed validation; not installed." >&2
    exit 1
  fi
  rm -f "$TMP"
else
  echo "[6/6] Skipping sudoers rule (--no-sudoers)."
fi

ln -sfn "$ETC/$PROFILE.env" "$ETC/active.env"
systemctl daemon-reload
systemctl enable ptp4l-tsn.service phc2sys-tsn.service

# phc2sys steps CLOCK_REALTIME; an NTP client would fight it, so disable any.
for ntp in chronyd chrony systemd-timesyncd ntp ntpd ntpsec; do
  if systemctl is-active --quiet "$ntp" 2>/dev/null || \
     systemctl is-enabled --quiet "$ntp" 2>/dev/null; then
    systemctl disable --now "$ntp" 2>/dev/null || true
    echo "      disabled NTP client $ntp"
  fi
done

if [ "$START" -eq 1 ]; then
  systemctl restart ptp4l-tsn.service phc2sys-tsn.service
  echo "Started with profile '$PROFILE'."
else
  echo "Enabled with profile '$PROFILE' (not started)."
fi

echo
echo "Done. Use:  sudo ptp-profile gptp|ieee1588|stop|status"
echo "Logs:       journalctl -fu ptp4l-tsn -u phc2sys-tsn"
