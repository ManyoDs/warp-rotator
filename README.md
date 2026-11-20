# WARP Rotator

**English** | [Bahasa Indonesia](#bahasa-indonesia)

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

Edit `/opt/warp-rotator/pool.ini` if needed. Default listeners bind to localhost. For network access, bind only to a private/Tailscale interface and configure the firewall. **The HTTP proxy has no client authentication; do not expose it to the public Internet.**

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
- The proxy binds to localhost by default and has no client authentication.
- IPv6-only changes routing in generated configs. IPv6 availability and IP rotation depend on the VPN provider.

---

# Bahasa Indonesia

WARP Rotator menjalankan beberapa konfigurasi WireGuard lewat [`wireproxy`](https://github.com/windtf/wireproxy), lalu menyediakan satu HTTP proxy load-balanced dengan dashboard web.

## Fitur

- Upload, edit, enable/disable, restart, dan lihat log konfigurasi WireGuard dari dashboard.
- Satu proses `wireproxy` per konfigurasi; listener SOCKS5 upstream hanya bind ke localhost.
- Satu HTTP proxy yang mendukung HTTP `CONNECT` (HTTPS) dan HTTP biasa.
- Pemilihan round-robin atau random, health probe, dan failover.
- Cek egress IP setiap tunnel melalui `https://api64.ipify.org/`.
- Toggle IPv6-only global. Yang diubah hanya config generated; config sumber tetap utuh.
- Auto-rotate berkala dan tombol **Restart All**; tunnel direstart bergiliran.
- Saat runtime hanya memakai Python standard library; tidak perlu dependency pip.

## Kebutuhan

- Python 3.9+
- [`wireproxy`](https://github.com/windtf/wireproxy/releases) di `PATH`, atau path lengkapnya di `pool.ini`.
- Linux, macOS, atau Windows. Contoh deployment di bawah untuk Linux/systemd.

## Membuat config WireGuard

Buat config Cloudflare WARP WireGuard melalui:

**[WireGuard WARP Config Generator](https://lanrat.github.io/wireguard-warp-generator/)**

Download config yang dihasilkan, lalu upload lewat dashboard. Jaga config sebagai rahasia karena berisi private key.

## Mulai cepat

```bash
git clone https://github.com/ManyoDs/warp-rotator.git
cd warp-rotator
python3 -m wgpool init
python3 -m wgpool passwd
python3 -m wgpool start
```

Buka dashboard di <http://127.0.0.1:9099>. HTTP proxy tersedia di `http://127.0.0.1:8080`. Upload config WireGuard lewat dashboard.

Untuk akses dashboard jarak jauh, gunakan SSH tunnel:

```bash
ssh -L 9099:127.0.0.1:9099 user@server
```

Kemudian buka <http://127.0.0.1:9099> di komputer lokal.

## Perintah CLI

```bash
python3 -m wgpool status
python3 -m wgpool configtest
python3 -m wgpool probe
python3 -m wgpool stop
```

Gunakan `-c /path/to/pool.ini` untuk memilih config aplikasi lain.

## Pengaturan dashboard

- **Strategy:** `round` bergiliran memakai tunnel aktif pada setiap koneksi proxy baru; `random` memilih tunnel secara acak.
- **IPv6-only:** toggle global. ON menghasilkan `AllowedIPs = ::/0`; OFF mengembalikan `AllowedIPs` dari config sumber. Mode otomatis mendukung satu section `[Peer]` per config.
- **Auto rotate:** interval restart dalam menit; `0` berarti nonaktif. Restart tidak menjamin provider memberikan IP berbeda.
- **Restart All:** restart semua tunnel aktif secara bergiliran.

Browser dapat memakai ulang koneksi HTTPS, sehingga refresh halaman belum tentu mengganti exit IP. Tes memakai koneksi baru:

```bash
curl --http1.1 -H 'Connection: close' \
  -x http://127.0.0.1:8080 https://api64.ipify.org/
```

## Deployment Linux + systemd

Contoh ini memasang aplikasi ke `/opt/warp-rotator` dengan akun service khusus. Install `wireproxy` lebih dahulu dan pastikan tersedia di `PATH`.

```bash
sudo git clone https://github.com/ManyoDs/warp-rotator.git /opt/warp-rotator
sudo useradd --system --home-dir /opt/warp-rotator --shell /usr/sbin/nologin warp-rotator
sudo chown -R warp-rotator:warp-rotator /opt/warp-rotator
cd /opt/warp-rotator
sudo -u warp-rotator python3 -m wgpool init
sudo -u warp-rotator python3 -m wgpool passwd
```

Edit `/opt/warp-rotator/pool.ini` bila perlu. Default listener hanya bind ke localhost. Untuk akses jaringan, bind hanya ke interface privat/Tailscale dan atur firewall. **HTTP proxy tidak memiliki autentikasi client; jangan expose ke Internet publik.**

Pasang dan jalankan service:

```bash
sudo cp deploy/wgpool.service /etc/systemd/system/warp-rotator.service
sudo systemctl daemon-reload
sudo systemctl enable --now warp-rotator
sudo systemctl status warp-rotator
```

Contoh service memakai path `/opt/warp-rotator`; ubah `WorkingDirectory` dan `ExecStart` jika path berbeda.

## Test

```bash
python3 -m compileall -q wgpool tests
python3 -m unittest discover -s tests -t . -v
node --check wgpool/web/app.js
```

Test menggunakan mock SOCKS5 lokal dan tidak memerlukan endpoint VPN aktif.

## Keamanan dan data lokal

- `pool.ini`, config WireGuard/private key, log, PID, dan config generated tidak disertakan di Git.
- Password dashboard di-hash dengan scrypt.
- Dashboard default bind ke localhost; gunakan TLS atau SSH tunnel untuk akses remote.
- Proxy default bind ke localhost dan tidak punya autentikasi client.
- IPv6-only mengubah routing pada config generated. Ketersediaan IPv6 dan rotasi IP bergantung pada provider VPN.
