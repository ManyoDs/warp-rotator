# WARP Rotator

Web app Python untuk menjalankan beberapa config WireGuard lewat `wireproxy`, lalu membagikan satu HTTP proxy dengan strategi round-robin/random.

## Fitur

- Upload, edit, enable/disable, restart, dan lihat log config WireGuard lewat dashboard.
- Satu proses `wireproxy` per config; SOCKS5 upstream hanya listen di localhost.
- Front HTTP proxy mendukung `CONNECT` (HTTPS) dan HTTP biasa.
- Load balancing round-robin/random, health probe, failover.
- Tampilkan egress IP setiap tunnel melalui `https://api64.ipify.org/`.
- Toggle IPv6-only global. ON menghasilkan `AllowedIPs = ::/0` di config generated; OFF mengembalikan `AllowedIPs` dari config sumber.
- Auto-rotate berkala dan tombol **Restart All**; tunnel direstart bergiliran.
- Python standard library saja; tidak perlu dependency pip saat dijalankan.

## Kebutuhan

- Python 3.9+
- Binary [`wireproxy`](https://github.com/windtf/wireproxy/releases) di `PATH` atau path lengkap pada `pool.ini`.
- Linux, macOS, atau Windows. Contoh service deployment di bawah menggunakan systemd/Linux.

## Mulai lokal

```bash
git clone <URL-REPOSITORY>
cd <NAMA-REPOSITORY>
python3 -m wgpool init
python3 -m wgpool passwd
python3 -m wgpool start
```


Untuk masuk dari komputer lain, lebih aman gunakan SSH port forwarding:

```bash
ssh -L 9099:127.0.0.1:9099 user@server
```

Lalu buka <http://127.0.0.1:9099>.

## Perintah

```bash
python3 -m wgpool status
python3 -m wgpool configtest
python3 -m wgpool probe
python3 -m wgpool stop
```

Pilih file config lain dengan `-c pool.ini`.

## Pengaturan dashboard

- **Strategi**: `round` memilih tunnel aktif bergiliran per koneksi baru; `random` memilih secara acak.
- **IPv6-only**: toggle global. Aplikasi hanya mengubah file generated di `run/`; file config sumber tidak disentuh. Mode otomatis memerlukan tepat satu `[Peer]` per config.
- **Auto rotate**: interval dalam menit; `0` nonaktif. Restart tidak menjamin provider memberikan IP baru.
- **Restart All**: restart semua tunnel aktif secara bergiliran.

Browser dapat memakai ulang koneksi HTTPS, sehingga refresh halaman tidak selalu memicu pemilihan tunnel baru. Tes tiap koneksi baru, misalnya:

```bash
curl --http1.1 -H 'Connection: close' \
  -x http://127.0.0.1:8080 https://api64.ipify.org/
```

## Deploy Linux + systemd

Contoh ini memakai `/opt/warp-rotator` dan user service khusus. Pastikan binary `wireproxy` sudah di `PATH`.

```bash
sudo git clone <URL-REPOSITORY> /opt/warp-rotator
sudo useradd --system --home-dir /opt/warp-rotator --shell /usr/sbin/nologin warp-rotator
sudo chown -R warp-rotator:warp-rotator /opt/warp-rotator
cd /opt/warp-rotator
sudo -u warp-rotator python3 -m wgpool init
sudo -u warp-rotator python3 -m wgpool passwd
```

Edit `/opt/warp-rotator/pool.ini`: set binary/config paths if needed. Default bind adalah localhost. Untuk akses jaringan, bind hanya ke interface privat/Tailscale dan atur firewall. **HTTP proxy tidak mempunyai autentikasi client**; jangan expose port proxy ke Internet publik.

Pasang service:

```bash
sudo cp deploy/wgpool.service /etc/systemd/system/warp-rotator.service
sudo systemctl daemon-reload
sudo systemctl enable --now warp-rotator
sudo systemctl status warp-rotator
```

Service unit contoh memakai `/opt/warp-rotator`; sesuaikan `WorkingDirectory` dan `ExecStart` bila path berbeda.

## Test

```bash
python3 -m compileall -q wgpool tests
python3 -m unittest discover -s tests -t . -v
node --check wgpool/web/app.js
```

Test memakai mock SOCKS5 lokal; tidak memerlukan endpoint VPN aktif.

## Keamanan dan data

- `pool.ini`, config WireGuard, private key, log, PID, dan file generated tidak masuk Git.
- Password dashboard di-hash dengan scrypt.
- Dashboard default bind ke localhost; atur TLS atau gunakan SSH tunnel untuk akses remote.
- Proxy default bind ke localhost dan tidak memiliki autentikasi client.
- IPv6-only mengubah routing pada config generated. Provider tetap menentukan ketersediaan IPv6 dan apakah egress IP berubah setelah restart.
