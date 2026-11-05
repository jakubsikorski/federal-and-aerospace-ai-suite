# Create VLANs on All Machines

The demo uses three VLANs on the TSN NIC (`tsn0`):

| VLAN | Subnet | Priority (PCP) | Carries |
|------|--------|----------------|---------|
| 1 | `192.168.127.x` | 1 | Camera data: RTSP and MQTT. Switch management |
| 3 | `192.168.3.x` | 3 | Basler cameras (Basler test only) |
| 5 | `192.168.5.x` | 5 | Background load |

## Automatic

Run once per machine with `setup_tsn_nic.sh`: see [First-time setup](../../first-time-setup.md#2-nic-and-vlans-every-machine).
It renames the NIC to `tsn0`, creates `tsn0.1/.3/.5` with address `192.168.<vlan>.<id>` and maps every
frame priority to PCP = VLAN id. Without the map all frames leave with priority 0 and the switch
cannot tell the traffic apart.

## Manual (one VLAN)

```bash
sudo ip link add link tsn0 name tsn0.1 type vlan id 1
sudo ip link set tsn0.1 type vlan egress-qos-map 0:1 1:1 2:1 3:1 4:1 5:1 6:1 7:1
sudo ip addr add 192.168.127.11/24 dev tsn0.1
sudo ip link set tsn0.1 up
```

Repeat for VLAN 3 and 5 with the matching id and priority. Then assign the VLANs on the switch:
[Configure VLAN on the Moxa switch](./configure-vlan-on-moxa-switch.md).
