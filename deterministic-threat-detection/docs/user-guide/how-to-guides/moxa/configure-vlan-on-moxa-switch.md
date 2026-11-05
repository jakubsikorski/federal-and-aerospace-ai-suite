# Configure VLAN on the Moxa Switch

## Prerequisites

Switch reachable as in [Configure the Moxa TSN switch](./configure-moxa-switch.md).

## Moxa Switch VLAN Configuration

Identify the switch port for each connected machine. You will then configure each port to
handle tagged VLAN traffic for the specific VLAN IDs the connected machine requires.

> **In this demo:** ports 1 to 4 (machines 1 to 4) carry VLANs 1, 3 and 5 tagged. Ports 5 and 6
> (Basler cameras) are untagged members of VLAN 3 with PVID 3.

1. Open the Moxa switch web interface by entering the switch's IP address (`192.168.127.253`
   after a reset) in a web browser.
2. Log in (after a reset: user `admin`, password `moxa`).
3. Navigate to the "`VLAN` > `IEEE 802.1Q`" section in the menu.
4. Create VLANs with IDs 1, 3, and 5 if they do not already exist.
   ![MOXA VLAN Creation](../../_assets/moxa-vlan-configuration.png)
5. Assign the appropriate ports to the VLANs as tagged ports by clicking edit (🖉) on the
   specific port.
   ![MOXA VLAN Port Configuration](../../_assets/moxa-vlan-port-configuration.png)
6. Apply the configuration and reboot the switch if necessary.
7. Verify the VLAN configuration by checking the port settings and ensuring that the correct
   VLANs are assigned.

Refer to the
[Moxa Managed Switch TSN-G5000 Series User Manual](https://www.moxa.com/getmedia/a0db0ef9-2741-4bad-91c6-1ec1827aca64/moxa-tsn-g5000-series-web-console-manual-v2.3.pdf)
for detailed instructions specific to your switch model.
