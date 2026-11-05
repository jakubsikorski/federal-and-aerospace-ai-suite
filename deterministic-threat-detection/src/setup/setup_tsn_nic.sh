#!/usr/bin/env bash
# Find the Intel I225-LM / I226-LM NIC, give it a standard name + MAC, and create the TSN VLAN interfaces.
#
# Same script on every machine; only --id differs:
#
#   sudo ./setup_tsn_nic.sh --id 31 --persist        # write once to disk; applied at every boot
#   sudo ./setup_tsn_nic.sh --id 31 --runtime        # apply right now only (lost on reboot)
#   ./setup_tsn_nic.sh --id 31 --persist --dry-run   # print what would be done (no sudo needed)
#   ./setup_tsn_nic.sh --find                        # only report the NIC that was found
#   sudo ./setup_tsn_nic.sh --qos --persist-qos      # apply the VLAN priority maps now AND keep them across
#                                                    # reboots (dispatcher script only; no interface bounce)
#   sudo ./setup_tsn_nic.sh --qos                    # only (re)apply the VLAN priority maps, live, no NIC reset
#   sudo ./setup_tsn_nic.sh --uninstall              # remove what --persist and --runtime created
#   (--persist and --runtime can be combined)
#
# Options: --id N (1-254, last IP octet)   --name tsn0   --mac xx:xx:xx:xx:xx:xx
#          (default MAC 00:a0:c9:00:00:<id in hex>)
#
# 1. Detect the NIC by PCI id: 8086:15f2 (I225-LM) or 8086:125b (I226-LM). Other igc parts such as
#    the I226-V (125c) / I226-IT (125d) are NOT matched. Zero or several matches -> abort.
# 2. --persist: write /etc/systemd/network/10-<name>.link (name + MAC, matched on driver + PCI id, so
#    the old name enp5s0/enp3s0/... does not matter) and one NetworkManager profile per VLAN
#    (<name>.1/.3/.5: vlan id, egress map 0:<vid>, static IP, autoconnect) plus a profile for <name>
#    itself with 192.168.11.<id>/24, so untagged traffic and PTP over UDP/IPv4 (IEEE 1588v2 default
#    profile) have an address on the NIC. Takes effect at next boot.
# 3. --runtime: rename, set the MAC and create the VLAN interfaces with `ip` right now.
#    Priority maps: every VLAN interface maps ALL socket priorities 0-7 to PCP = its VLAN id (VLAN 1 ->
#    PCP 1, VLAN 3 -> PCP 3, VLAN 5 -> PCP 5), so a switch can tell the traffic classes apart. Mapping only
#    priority 0 would leave e.g. ssh (priority 6) untagged. NetworkManager's profile map alone was not
#    reliably applied to the kernel device, so --persist also installs a NetworkManager dispatcher script
#    that re-applies the maps whenever a VLAN interface comes up, and every mode verifies the result.
#    --qos does only that (no rename, MAC change or link down), so it is safe on a machine in use;
#    --persist-qos installs just the dispatcher script (no NetworkManager profile changes).
# 4. --uninstall: delete the .link file, the NetworkManager profiles (<name>, <name>.1/.3/.5), the
#    dispatcher script and any live <name>.N VLAN interfaces. Does not need --id and works even if the NIC is gone. The rename and
#    MAC revert to the hardware defaults at next boot (a live interface is not renamed back).
# Idempotent. --persist needs NetworkManager (nmcli).
set -euo pipefail

NAME=tsn0 ID="" MAC="" FIND=0 UNINSTALL=0 QOS=0 PERSIST_QOS=0 PERSIST=0 RUNTIME=0 DRY=0
LINK_DIR=/etc/systemd/network
DISPATCH_DIR=/etc/NetworkManager/dispatcher.d
declare -A MODELS=([15f2]=I225-LM [125b]=I226-LM)   # supported PCI device ids (vendor 8086)
declare -A NETS=([1]=192.168.127 [3]=192.168.3 [5]=192.168.5)
BASE_NET=192.168.11   # address of the NIC itself (untagged)

die() { echo "error: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case $1 in
        --id)      ID=${2:?}; shift 2 ;;
        --name)    NAME=${2:?}; shift 2 ;;
        --mac)     MAC=${2:?}; shift 2 ;;
        --find)    FIND=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --qos)     QOS=1; shift ;;
        --persist-qos) PERSIST_QOS=1; shift ;;
        --persist) PERSIST=1; shift ;;
        --runtime) RUNTIME=1; shift ;;
        --dry-run) DRY=1; shift ;;
        -h|--help) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done

run() { echo "+ $*"; [ "$DRY" = 1 ] || "$@"; }
sh_run() { echo "+ $1"; [ "$DRY" = 1 ] || bash -c "$1"; }

# "0:5 1:5 ... 7:5" (ip syntax) and "0:5,1:5,...,7:5" (NetworkManager syntax) for a VLAN id.
qos_ip()  { local v=$1 i out=""; for i in 0 1 2 3 4 5 6 7; do out+="$i:$v "; done; echo "${out% }"; }
qos_csv() { local v=$1 i out=""; for i in 0 1 2 3 4 5 6 7; do out+="$i:$v,"; done; echo "${out%,}"; }

# Check that the kernel really holds the map; an unset map silently sends every frame with PCP 0.
verify_qos() {
    local sub=$1 vid=$2
    [ "$DRY" != 1 ] || return 0
    if ip -d -j link show "$sub" | grep -q '"egress_qos"'; then
        echo "  $sub: egress priority map active (all priorities -> PCP $vid)"
    else
        echo "  WARNING: $sub has no egress priority map in the kernel (frames leave with PCP 0)" >&2
    fi
}

install_dispatcher() {  # re-apply the maps whenever a VLAN interface comes up
    local file=$DISPATCH_DIR/90-$NAME-vlan-qos vid body
    body='#!/bin/sh
# Installed by setup_tsn_nic.sh: all socket priorities -> PCP = VLAN id on the TSN VLAN interfaces.
[ "$2" = up ] || exit 0
case "$1" in'
    for vid in 1 3 5; do
        body+="
    $NAME.$vid) ip link set dev \"\$1\" type vlan egress-qos-map $(qos_ip "$vid") ;;"
    done
    body+="
esac"
    echo "+ write $file:"; sed 's/^/    /' <<<"$body"
    if [ "$DRY" != 1 ]; then
        install -d "$DISPATCH_DIR"
        printf '%s\n' "$body" >"$file"
        chmod 755 "$file"
    fi
}

apply_qos() {  # live, on existing VLAN interfaces; touches nothing else
    local vid sub
    for vid in 1 3 5; do
        sub=$NAME.$vid
        if [ -e "/sys/class/net/$sub" ] || [ "$DRY" = 1 ]; then
            run ip link set dev "$sub" type vlan egress-qos-map $(qos_ip "$vid")
            verify_qos "$sub" "$vid"
        else
            echo "  skip $sub: interface does not exist"
        fi
    done
}

# --- 0. uninstall ----------------------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
    [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run with sudo (or use --dry-run)"
    if command -v nmcli >/dev/null; then
        for con in "$NAME" "$NAME.1" "$NAME.3" "$NAME.5"; do
            sh_run "nmcli -t -f NAME con show | grep -qx '$con' && nmcli con delete '$con' || true"
        done
    fi
    for vid in 1 3 5; do
        [ ! -e "/sys/class/net/$NAME.$vid" ] || run ip link delete "$NAME.$vid"
    done
    [ ! -e "$LINK_DIR/10-$NAME.link" ] || run rm -f "$LINK_DIR/10-$NAME.link"
    [ ! -e "$DISPATCH_DIR/90-$NAME-vlan-qos" ] || run rm -f "$DISPATCH_DIR/90-$NAME-vlan-qos"
    echo "uninstalled; interface name and MAC return to the defaults at next boot"
    exit 0
fi

# --- 0b. priority maps only ------------------------------------------------------------------
if [ "$QOS" = 1 ]; then
    [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run with sudo (or use --dry-run)"
    apply_qos
    [ "$PERSIST_QOS" != 1 ] || install_dispatcher
    exit 0
fi
if [ "$PERSIST_QOS" = 1 ]; then
    [ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run with sudo (or use --dry-run)"
    install_dispatcher
    exit 0
fi

# --- 1. find the I225-LM / I226-LM -----------------------------------------------------------
SUPPORTED=$(printf '8086:%s ' "${!MODELS[@]}")
mapfile -t FOUND < <(
    for d in /sys/class/net/*/device; do
        [ -r "$d/vendor" ] || continue
        dev=$(cat "$d/device")
        if [ "$(cat "$d/vendor")" = 0x8086 ] && [ -n "${MODELS[${dev#0x}]:-}" ]; then
            n=${d%/device}; n=${n##*/}
            echo "$n $(basename "$(readlink -f "$d")") $(cat "/sys/class/net/$n/address") ${dev#0x}"
        fi
    done
)
[ ${#FOUND[@]} -ge 1 ] || die "no supported NIC found (looked for PCI ${SUPPORTED% })"
[ ${#FOUND[@]} -eq 1 ] || die "more than one supported NIC found, refusing to guess: ${FOUND[*]}"
read -r IFNAME PCI CUR_MAC DEVID <<<"${FOUND[0]}"
echo "${MODELS[$DEVID]} found: interface=$IFNAME pci=$PCI id=8086:$DEVID mac=$CUR_MAC"
[ "$FIND" = 1 ] && exit 0

[ "$PERSIST" = 1 ] || [ "$RUNTIME" = 1 ] || die "choose --persist and/or --runtime"
[[ $ID =~ ^[0-9]+$ ]] && [ "$ID" -ge 1 ] && [ "$ID" -le 254 ] || die "--id 1-254 is required"
[ -n "$MAC" ] || MAC=$(printf '00:a0:c9:00:00:%02x' "$ID")
[[ $MAC =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]] || die "bad MAC '$MAC'"
[ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || die "run with sudo (or use --dry-run)"

# --- 2. persistent configuration -------------------------------------------------------------
if [ "$PERSIST" = 1 ]; then
    command -v nmcli >/dev/null || die "--persist needs NetworkManager (nmcli)"
    LINK_FILE=$LINK_DIR/10-$NAME.link
    LINK_BODY="[Match]
Driver=igc
Property=ID_VENDOR_ID=0x8086
Property=ID_MODEL_ID=0x$DEVID

[Link]
Name=$NAME
MACAddress=$MAC"
    echo "+ write $LINK_FILE:"; sed 's/^/    /' <<<"$LINK_BODY"
    if [ "$DRY" != 1 ]; then
        install -d "$LINK_DIR"
        printf '%s\n' "$LINK_BODY" >"$LINK_FILE"
        echo "check what systemd would do with the current NIC:"
        udevadm test-builtin net_setup_link "/sys/class/net/$IFNAME" 2>&1 | grep -i -E "name|address|link file" || true
    fi
    sh_run "nmcli -t -f NAME con show | grep -qx '$NAME' && nmcli con delete '$NAME' || true"
    run nmcli con add type ethernet con-name "$NAME" ifname "$NAME" \
        ipv4.method manual ipv4.addresses "$BASE_NET.$ID/24" ipv6.method disabled connection.autoconnect yes
    for vid in 1 3 5; do
        sub=$NAME.$vid
        sh_run "nmcli -t -f NAME con show | grep -qx '$sub' && nmcli con delete '$sub' || true"
        run nmcli con add type vlan con-name "$sub" ifname "$sub" dev "$NAME" id "$vid" \
            vlan.egress-priority-map "$(qos_csv "$vid")" ipv4.method manual ipv4.addresses "${NETS[$vid]}.$ID/24" \
            ipv6.method disabled connection.autoconnect yes
    done
    install_dispatcher
    echo "persisted; takes effect at next boot (use --qos to apply the priority maps right now)"
fi

# --- 3. immediate configuration --------------------------------------------------------------
if [ "$RUNTIME" = 1 ]; then
    run ip link set dev "$IFNAME" down
    [ "$IFNAME" = "$NAME" ] || run ip link set dev "$IFNAME" name "$NAME"
    run ip link set dev "$NAME" address "$MAC"
    run ip addr replace "$BASE_NET.$ID/24" dev "$NAME"
    run ip link set dev "$NAME" up
    for vid in 1 3 5; do
        sub=$NAME.$vid
        [ -e "/sys/class/net/$sub" ] || run ip link add link "$NAME" name "$sub" type vlan id "$vid"
        run ip link set "$sub" type vlan egress-qos-map $(qos_ip "$vid")
        run ip addr replace "${NETS[$vid]}.$ID/24" dev "$sub"
        run ip link set "$sub" up
        verify_qos "$sub" "$vid"
    done
fi
