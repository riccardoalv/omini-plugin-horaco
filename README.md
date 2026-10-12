# omini-plugin-horaco

[Omini](https://github.com/riccardoalv/omini) plugin for the **Horaco HC-SWTGW218AS** managed switch and other switches with the same web interface (keepLink, Sodola and similar Realtek-based OEM models). These switches have no SNMP on their stock firmware, so the plugin reads their web interface.

## What it reads

| Data | Used for |
|---|---|
| Model, firmware version, MAC, IP, uptime (`/info.cgi`) | The switch on the map |
| Port status: link, speed, duplex (`/info.cgi`) | Port front view, link speed |
| Port counters (`/port.cgi?page=stats`) | Errors (packets; these models count no bytes) |
| Front panel (`/panel.cgi`) | RJ45 or SFP per port |
| MAC table (`/mac.cgi?page=fwd_tbl`) | Which device is on which port |
| Port settings (`/port.cgi`) | Whether each port is enabled |
| 802.1Q VLANs (`/vlan.cgi?page=static`, `/vlan.cgi?page=port_based`) | The switch's VLANs, each port's (access, or trunk with its native VLAN) |
| Netmask (`/info.cgi`) | Configuration backups (below) |

## What it changes

**Enable or disable a port** (`port.enabled`), from the port's card in Omini. The plugin posts the port's form of **Port Setting** (`/port.cgi`, `cmd=port`) with its other settings (duplex, speed, flow control) as they are, reads the page again to check the switch took it, and only then posts **Save Configuration** (`/save.cgi`, `cmd=save`). A change the switch did not take is never saved, so a reboot undoes it.

**Create or rename a VLAN** (`vlan.set`): **Add VLAN** (`/vlan.cgi?page=static`, fields `vid` and `name`; names are cut at 14 characters, the switch's limit), read back, then saved. Deleting a VLAN is not supported yet: delete it on the switch.

**Set a port's VLANs** (`port.vlans`): **802.1Q VLAN Setting** (`/vlan.cgi?page=port_based`) for that port alone: access (one VLAN) or trunk (a native VLAN, which these switches always have, and the tagged VLANs); the port's accepted frames go back to "All", the switch's default. Hybrid ports are left alone. Read back, then saved. VLAN Manage must be on.

Collecting only reads: the plugin signs in (the same form as the login page), reads pages and turns the pages of the MAC table (`cmd=goto`). It never reboots the switch or changes anything else.

## Configuration backups

Omini keeps a history of the switch's configuration (once a day and after each change it makes). No configuration file download has been seen on this firmware, so the backup is the switch's settings as sorted JSON, from pages collect already reads (GET only):

- **system**: device name, IP address and netmask (`/info.cgi`);
- **ports**: each port's state, duplex, speed and flow control as configured (`/port.cgi`);
- **VLANs**: VLAN Manage on or off, the VLANs and their names (`/vlan.cgi?page=static`), and each port's mode, access, native and trunk VLANs (`/vlan.cgi?page=port_based`).

No status (link, uptime, firmware version, counters, MAC table) and no password (the pages never show it): two backups differ only when the configuration changed. If one of these pages does not answer, no backup is made (a partial one would look like a change). It needs nothing beyond the login collect uses. Omini never restores it.

## Not read yet: spanning tree and link aggregation

Omini can show each port's STP state and role and the ports of a LAG, but no page of these switches has been captured for them, so the plugin does not read them (it never guesses page names on a switch this fragile). To add them, these pages of a real switch are needed (saved from the browser while signed in):

- `/menu.cgi` (the left menu: the names of the spanning tree / loop prevention and trunk / link aggregation pages on this firmware);
- the spanning tree page(s): global settings (bridge priority, root bridge) and the per-port table (state, role), with STP on;
- the link aggregation (trunk) page with at least one group configured, and the Port Setting and VLAN pages while that group exists.

## Not verified on hardware

- Backups use pages captured on a real unit (firmware V200.2.4); the info grid's `Netmask` label is assumed to be the same on other firmware versions (`Subnet Mask` is read too).

## Install

In Omini: **Integrations → Add → Plugin store → +** and paste `https://github.com/riccardoalv/omini-plugin-horaco`. Then add the integration with the switch's address, username (`admin` by default) and password.

## Notes

- The switch's web server is slow and drops connections when it gets several requests at once: the plugin reads one page at a time, with a short pause, and retries. A collection takes a few seconds; once a minute is plenty.
- Signing in from Omini may sign you out of the switch's web interface in your browser (the firmware keeps one session).
- The last pages read are kept in the plugin's state folder (`<data>/plugins/horaco/state/<integration>/pages`). If your model is not read correctly, attaching them to an issue helps (remove the MACs first if you prefer).

## Development

```bash
uv run pytest -q          # tests (the SDK comes from ../omini/sdk/python)
uv run ruff check . && uv run ruff format --check .
OMINI_PLUGIN_DIRS=$PWD    # in Omini, to load the plugin in place
```

## License

MIT
