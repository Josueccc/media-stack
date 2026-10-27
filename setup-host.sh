#!/usr/bin/env bash
# Prepara el host para el media stack. Ejecutar con sudo.
#
# Variables:
#   DISK_UUID    (obligatoria) UUID del disco de datos. Mírala con `lsblk -f`.
#   MOUNT_POINT  (opcional, /mnt/disk por defecto)
#   APPDATA_DIR  (opcional, /opt/appdata por defecto)
#   PUID/PGID    (opcional, 1000 por defecto)
#
# Ejemplo:
#   sudo DISK_UUID=$(lsblk -no UUID /dev/sdX2) bash setup-host.sh
set -euo pipefail

UUID="${DISK_UUID:?exporta DISK_UUID con el UUID de tu disco (lo ves con lsblk -f)}"
MP="${MOUNT_POINT:-/mnt/disk}"
APPDATA="${APPDATA_DIR:-/opt/appdata}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
OPTS="defaults,nofail,noatime,uid=${PUID},gid=${PGID},umask=002,windows_names,x-systemd.device-timeout=10"

SERVICES="prowlarr sonarr radarr bazarr qbittorrent jellyfin jellyseerr lidarr navidrome mixarr ytm-bridge"

echo "==> Desmontando el disco si el automontador lo puso en otro sitio (/media/...)"
CURRENT="$(findmnt -rn -o TARGET --source "UUID=$UUID" 2>/dev/null || true)"
if [[ -n "$CURRENT" && "$CURRENT" != "$MP" ]]; then
  umount "$CURRENT" 2>/dev/null || true
fi

echo "==> Creando punto de montaje $MP"
mkdir -p "$MP"

# Guarda anti-desastre: si el disco NO monta, este directorio queda inmutable
# y ningún contenedor puede escribir dentro llenando el SSD del sistema.
chattr +i "$MP" 2>/dev/null || echo "    (chattr no soportado, se omite)"

echo "==> Añadiendo entrada a /etc/fstab"
cp /etc/fstab "/etc/fstab.bak.$(date +%F-%H%M%S)"
if ! grep -q "$UUID" /etc/fstab; then
  printf 'UUID=%s  %s  ntfs3  %s  0  0\n' "$UUID" "$MP" "$OPTS" >> /etc/fstab
else
  echo "    Ya existía una entrada para $UUID, no se toca."
fi

echo "==> Recargando systemd y montando"
systemctl daemon-reload
mount "$MP"
findmnt "$MP"

echo "==> Creando estructura de carpetas (todo bajo el MISMO mount => hardlinks)"
mkdir -p "$MP"/data/torrents/{movies,tv,music}
mkdir -p "$MP"/data/media/{movies,tv,music}

echo "==> Creando appdata en el SSD (ext4, no NTFS: las BD SQLite lo exigen)"
for s in $SERVICES; do
  mkdir -p "$APPDATA/$s"
  chown "$PUID:$PGID" "$APPDATA/$s"
done

echo
echo "LISTO. Verificación:"
df -h "$MP"
ls -la "$MP/data"
