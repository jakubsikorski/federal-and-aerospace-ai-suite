# First-Time Setup

Everything you run **once** per machine or switch. After this, only `make up` / `make down`
from [Get started](./get-started.md) is needed. Do the steps in order: 1 to 3 run on every machine,
4 and 5 on the switch, 6 on machine-1.

You work on machine-1, in a clone of this repository (see [Get started](./get-started.md#clone-the-project)).
The scripts in `src/setup/` exist only there. Run them directly on machine-1; for machines 2 to 4
copy them over with `scp` and run them with `ssh -t` (the `-t` lets `sudo` ask for the password).
In the commands below, `<user>` is your login on the machines and `<machine-N lab address>` is
the address of machine N on the normal lab network (not a TSN address).

## 1. ssh and Docker (every machine)

Machine-1 must reach machines 2 to 4 without a password, and your user must be allowed to run Docker.

```bash
ssh-keygen -t ed25519                           # only if you have no ssh key yet
ssh-copy-id <user>@<machine-2 lab address>      # repeat for machines 3 and 4
ssh <user>@<machine-2 lab address> docker ps    # check: no password prompt, no "permission denied"
```

If `docker ps` is denied, add the user to the `docker` group on that machine and log in again.
On machine-1 run the first command locally; on machines 2 to 4:

```bash
sudo usermod -aG docker <user>                                   # machine-1
ssh -t <user>@<machine-2 lab address> 'sudo usermod -aG docker <user>'   # machines 2 to 4
```

Docker Engine with Compose V2 must already be installed
([install guide](https://docs.docker.com/engine/install/ubuntu/)).

## 2. NIC and VLANs (every machine)

```mermaid
flowchart LR
    NIC["tsn0 (I225/I226)"] --> V1["tsn0.1<br/>192.168.127.id, prio 1"]
    NIC --> V3["tsn0.3<br/>192.168.3.id, prio 3"]
    NIC --> V5["tsn0.5<br/>192.168.5.id, prio 5"]
```

Names the TSN NIC `tsn0`, creates `tsn0.1`, `tsn0.3`, `tsn0.5` and sets the priority maps
(VLAN 1/3/5 become frame priority 1/3/5). `--id` is the last address octet: 11, 12, 13, 14 for
machines 1 to 4. The script stops if the machine has no supported TSN NIC, or more than one.

```bash
sudo src/setup/setup_tsn_nic.sh --id 11 --persist        # machine-1
scp src/setup/setup_tsn_nic.sh <user>@<machine-2 lab address>:/tmp/
ssh -t <user>@<machine-2 lab address> 'sudo bash /tmp/setup_tsn_nic.sh --id 12 --persist'   # machines 3, 4: --id 13, 14
```

Reboot each machine afterwards (`sudo reboot`): the NIC rename takes effect at the next boot.

Check: `ip -d link show tsn0.1 | grep egress_qos` lists eight mappings. Priority maps only
(no interface reset), `--find` (show the detected NIC) and `--uninstall` are also available.
Manual commands: [Create VLANs on all machines](./how-to-guides/moxa/create-vlan-on-all-machines.md).

## 3. PTP (every machine)

Installs `linuxptp`, the `ptp4l`/`phc2sys` services and the `ptp-profile` command, and disables
NTP clients. The switch is the grandmaster, the machines are slaves.

```bash
sudo src/setup/ptp-install.sh               # machine-1; default profile: IEEE 1588v2
scp src/setup/ptp-install.sh <user>@<machine-2 lab address>:/tmp/
ssh -t <user>@<machine-2 lab address> 'sudo bash /tmp/ptp-install.sh'          # machines 2 to 4
sudo ptp-profile status                     # check, on each machine: offset well below 1 us
```

Use `sudo ptp-profile gptp` for IEEE 802.1AS. The switch and every machine must use the same
profile, so also set it on the switch (web page: System > Time > Time Synchronization):
[1588v2](./how-to-guides/moxa/configure-ptp-1588v2.md),
[gPTP](./how-to-guides/moxa/configure-ptp-gptp.md). The machines only lock once the switch
sends PTP, so run `sudo ptp-profile status` again after step 4.

## 4. Switch VLANs

Open the switch web page in a browser (after a reset: address `192.168.127.253`, user `admin`, password `moxa`;
give your computer an address in `192.168.127.0/24`, for example `192.168.127.100`). Create VLANs
1, 3, 5 and make ports 1 to 4 **tagged** members (ports 5 and 6 for Basler). See
[Configure VLAN on the Moxa switch](./how-to-guides/moxa/configure-vlan-on-moxa-switch.md).
Check: from machine-1, `ping 192.168.127.12` reaches machine-2 over VLAN 1.

## 5. Switch Time-Aware Shaper

In the switch web page turn on **Schedule Gate** for port 1 (Layer 2 Switching > Time-aware
Shaper). The dashboard then adds and removes the schedule. See
[Enable TSN traffic shaping](./how-to-guides/moxa/enable-tsn-traffic-shaping.md).
Check: once the dashboard runs, its Qbv toggle is enabled and shows no warning banner.

## 6. Lab configuration (machine-1)

`lab.env` holds all addresses and stays out of git. Fill in every line; the comments in the file
explain each one:

| Entry | Value |
|-------|-------|
| `SSH_USER` | the login `<user>` used above (the same on all four machines) |
| `M1_LAB_IP` ... `M4_LAB_IP` | lab-network addresses of the four machines |
| `M1_TSN_IP` ... `M3_TSN_IP` | VLAN 1 addresses, `192.168.127.11` to `.13` if you used the ids above |
| `M1_NOISE_IP`, `M4_NOISE_IP` | VLAN 5 addresses, `192.168.5.11` and `192.168.5.14` |
| `SWITCH_IP` | switch management address, `192.168.127.253` after a reset |

```bash
cp lab.env.example lab.env
make -C src/dashboard deploy-switch SWITCH_HOST=<user>@<machine-2 lab address>
```

The switch scripts run from machine-2, so the switch is never reached over the shaped port.
`deploy-switch` copies them to `~/moxa` on machine-2.

The first `make up` creates `src/dashboard/.env`. To let the dashboard toggle Qbv, set
`SWITCH_ALLOW_WRITE=1` in that file and run `make up` again (the default is read-only).

## Basler only

Camera PTP, the switch port priority and building the Basler-enabled pipeline server image are one-time steps too:
[Integrate Basler cameras with SceneScape](./how-to-guides/basler/integrate-basler-camera-with-scenescape.md).
