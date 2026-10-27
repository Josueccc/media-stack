# media-stack

Self-hosted media stack for a single Ubuntu box: discover, download, organise
and stream movies, TV and music. Eleven containers behind a gluetun
(WireGuard/NordLynx) kill-switched tunnel for the torrent client, deployed
with docker compose or Portainer.

## What's inside

| Service | Purpose |
|---|---|
| [gluetun](https://github.com/qdm12/gluetun) | VPN tunnel + kill switch (only qBittorrent routes through it) |
| qBittorrent | Downloads |
| Prowlarr (+ FlareSolverr) | Indexer manager |
| Sonarr / Radarr / Lidarr | TV / movies / music automation |
| Bazarr | Subtitles |
| Jellyfin | Video streaming (QSV hardware transcoding) |
| Jellyseerr | Requests front-end |
| Navidrome | Music streaming (Subsonic API) |
| Mixarr | Music discovery (Spotify/TIDAL/Last.fm recommendations) |
| **ytm-bridge** | Small FastAPI service in this repo: turns YouTube Music recommendations into a Lidarr Custom List via MusicBrainz lookups, with Google Takeout history as a fallback source |

Everything mounts a single parent data directory so Sonarr/Radarr/Lidarr can
**hardlink** finished downloads into the library instead of copying them.

## Layout

```
docker-compose.yml     the stack
setup-host.sh          one-shot host prep: fstab mount, folder layout, appdata
get-nordlynx-key.sh    trades a NordVPN access token for the NordLynx WG key
ytm-bridge/            YouTube Music -> Lidarr bridge (Python)
guide.html             full step-by-step runbook (open it in a browser)
```

## Setup

1. **Prep the host** (data disk in `/etc/fstab`, folder layout, `/opt/appdata`):

   ```bash
   lsblk -f   # copy the UUID of your data disk
   sudo DISK_UUID=<uuid> MOUNT_POINT=/mnt/disk bash setup-host.sh
   ```

2. **Get the VPN key** (NordVPN only; swap the gluetun provider if you use
   another one):

   ```bash
   ./get-nordlynx-key.sh   # writes nordlynx.key (gitignored, chmod 600)
   ```

3. **Configure**: copy `.env.example` to `.env` and fill it in — LAN IP,
   subnets, data path, and the three secrets (`WIREGUARD_PRIVATE_KEY`,
   `SESSION_SECRET`, `LIDARR_API_KEY`). Deploying via Portainer instead? Set
   the same variables under *Stacks → Environment variables*; Portainer
   ignores `.env`.

4. **Deploy**:

   ```bash
   docker build -t ytm-bridge:local ytm-bridge   # ytm-bridge has no registry image
   docker compose up -d
   ```

5. Walk `guide.html` in a browser for the per-service configuration
   (Prowlarr → Sonarr/Radarr → Jellyfin → …), the end-to-end test and
   troubleshooting.

## Notes

- `guide.html` uses `192.0.2.x` (RFC 5737 documentation range) wherever a
  real LAN IP would go.
- Secrets and session data (`nordlynx.key`, `.env`, YouTube Music headers)
  are gitignored; the repo contains no credentials.
- ytm-bridge auth: paste your YouTube Music request headers into a file and
  run `ytm-bridge/make-auth.sh <file>` to produce `headers_auth.json`.
