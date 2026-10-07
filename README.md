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

**Read-only:** the plugin signs in (the same form as the login page) and reads pages. It never changes a setting, reboots or saves anything on the switch.

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
