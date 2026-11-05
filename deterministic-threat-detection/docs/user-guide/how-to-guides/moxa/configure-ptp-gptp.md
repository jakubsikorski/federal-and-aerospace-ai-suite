# Configure PTP, IEEE 802.1AS (gPTP)

The switch is the PTP grandmaster. Each machine runs `ptp4l` (NIC clock) and `phc2sys` (system
clock). gPTP is one of two profiles; the other is [IEEE 1588v2](./configure-ptp-1588v2.md), which
the Basler cameras need. A port is in one profile at a time: set the switch and **all** machines
to the same one.

## Machines

`ptp-install.sh` is run once ([First-time setup](../../first-time-setup.md#3-ptp-every-machine)). Then switch profile with:

```bash
sudo ptp-profile gptp
sudo ptp-profile status
```

Equivalent manual commands (`ptp-install.sh` creates `/etc/linuxptp/gPTP.cfg`):

```bash
sudo ptp4l -i tsn0 -f /etc/linuxptp/gPTP.cfg --step_threshold=1 -m -s
sudo phc2sys -s tsn0 -c CLOCK_REALTIME --step_threshold=1 --transportSpecific=1 -w -m
```

`phc2sys` steps `CLOCK_REALTIME`, so NTP clients (`chrony`, `systemd-timesyncd`) must be off.
`ptp-install.sh` does that.

## Switch

Set the profile in the web page (System > Time > Time Synchronization) or with
`python3 ptp_profile.py 802.1as` from `src/switch/` on a machine that reaches the switch
(`python3 ptp_profile.py --help` lists the options).

## Verify

`phc2sys` should report an offset in the tens of nanoseconds:

```text
phc2sys[1234.567]: CLOCK_REALTIME phc offset 12 s2 freq +0 delay 512
```
