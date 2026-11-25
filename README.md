# WARP Rotator

**English** | [Bahasa Indonesia](README.id.md)

WARP Rotator runs multiple WireGuard configurations through [`wireproxy`](https://github.com/windtf/wireproxy) and exposes one load-balanced HTTP proxy with a web dashboard.

## Features

- Upload, edit, enable/disable, restart, and inspect logs for WireGuard configurations in the dashboard.
- One `wireproxy` process per configuration; upstream SOCKS5 listeners bind to localhost only.
- One HTTP proxy supporting HTTP `CONNECT` (HTTPS) and plain HTTP.
- Round-robin or random selection, health probes, and failover.
- Per-tunnel egress IP checks through `https://api64.ipify.org/`.
- Global IPv6-only toggle. It changes generated configs only; source configs remain untouched.
- Scheduled tunnel rotation and **Restart All**, restarting tunnels in sequence.
- Python standard library only at runtime; no pip dependencies.

## Requirements

- Python 3.9+
- [`wireproxy`](https://github.com/windtf/wireproxy/releases) in `PATH`, or its full path in `pool.ini`.
- Linux, macOS, or Windows. The deployment example below uses Linux/systemd.

## Get WireGuard configurations

Generate a Cloudflare WARP WireGuard configuration at:

**[WireGuard WARP Config Generator](https://lanrat.github.io/wireguard-warp-generator/)**

Download the generated config, then upload it through the dashboard. Treat generated config files as secrets: they contain private keys.

## Quick start

```bash
git clone https://github.com/ManyoDs/warp-rotator.git
cd warp-rotator
python3 -m wgpool init
python3 -m wgpool passwd
python3 -m wgpool start
```

Open the dashboard at <http://127.0.0.1:9099>. The HTTP proxy listens at `http://127.0.0.1:8080`. Upload WireGuard config files in the dashboard.

For remote dashboard access, use an SSH tunnel:

```bash
ssh -L 9099:127.0.0.1:9099 user@server
```

Then open <http://127.0.0.1:9099> locally.

## CLI commands

```bash
python3 -m wgpool status
python3 -m wgpool configtest
python3 -m wgpool probe
python3 -m wgpool stop
```

Use `-c /path/to/pool.ini` to select another app config.

## Dashboard settings

- **Strategy:** `round` rotates across active tunnels for each new proxy connection; `random` selects one randomly.
- **Proxy authentication:** optionally require HTTP Basic Auth. Enable the toggle, set a username, and enter a new password. Clients use `http://USERNAME:PASSWORD@HOST:8080`. Basic Auth is not encryption; use TLS or a trusted private network.
- **IPv6-only:** global toggle. ON generates `AllowedIPs = ::/0`; OFF restores `AllowedIPs` from each source config. Automatic mode supports one `[Peer]` section per config.
- **Auto rotate:** restart interval in minutes; `0` disables it. A restart does not guarantee that the VPN provider assigns a different IP.
- **Restart All:** restart active tunnels in sequence.

Browsers may reuse HTTPS connections, so refreshing a page may keep the same exit IP. Test with new connections:

```bash
curl --http1.1 -H 'Connection: close' \
  -x http://127.0.0.1:8080 https://api64.ipify.org/
```

## Linux + systemd deployment

This example installs to `/opt/warp-rotator` and uses a dedicated service account. Install `wireproxy` first and ensure it is in `PATH`.

```bash
sudo git clone https://github.com/ManyoDs/warp-rotator.git /opt/warp-rotator
sudo useradd --system --home-dir /opt/warp-rotator --shell /usr/sbin/nologin warp-rotator
sudo chown -R warp-rotator:warp-rotator /opt/warp-rotator
cd /opt/warp-rotator
sudo -u warp-rotator python3 -m wgpool init
sudo -u warp-rotator python3 -m wgpool passwd
```

Edit `/opt/warp-rotator/pool.ini` if needed. Default listeners bind to localhost. For network access, bind only to a private/Tailscale interface and configure the firewall. Proxy Basic Auth is optional and does not encrypt traffic; use TLS/private networking when exposing the proxy outside localhost.

Install and start the service:

```bash
sudo cp deploy/wgpool.service /etc/systemd/system/warp-rotator.service
sudo systemctl daemon-reload
sudo systemctl enable --now warp-rotator
sudo systemctl status warp-rotator
```

The sample service assumes `/opt/warp-rotator`; update `WorkingDirectory` and `ExecStart` if using a different path.

## Tests

```bash
python3 -m compileall -q wgpool tests
python3 -m unittest discover -s tests -t . -v
node --check wgpool/web/app.js
```

Tests use local mock SOCKS5 servers and do not require a live VPN endpoint.

## Security and local data

- `pool.ini`, WireGuard configs/private keys, logs, PID files, and generated configs are excluded from Git.
- Dashboard passwords are hashed with scrypt.
- The dashboard binds to localhost by default; use TLS or an SSH tunnel for remote access.
- The proxy binds to localhost by default; optional Basic Auth can protect proxy clients. Use TLS/private networking beyond localhost.
- IPv6-only changes routing in generated configs. IPv6 availability and IP rotation depend on the VPN provider.
