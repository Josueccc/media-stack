#!/usr/bin/env bash
# Cambia un access token de NordVPN por la llave privada WireGuard (NordLynx).
# Ni el token ni la llave se imprimen en pantalla.
set -euo pipefail

OUT="$(cd -- "$(dirname -- "$0")" && pwd)/nordlynx.key"

read -rsp "Pega tu NordVPN access token (no se muestra): " TOKEN
echo

if [[ -z "${TOKEN}" ]]; then
  echo "No se recibió ningún token. Abortando." >&2
  exit 1
fi

echo "==> Consultando la API de NordVPN..."
RESP="$(curl -sS --fail-with-body -u "token:${TOKEN}" \
  https://api.nordvpn.com/v1/users/services/credentials)" || {
    echo "La API rechazó la petición. Token inválido o expirado." >&2
    exit 1
  }

KEY="$(printf '%s' "$RESP" | jq -r '.nordlynx_private_key // empty')"

if [[ -z "$KEY" || "$KEY" == "null" ]]; then
  echo "La respuesta no traía nordlynx_private_key." >&2
  echo "Campos recibidos: $(printf '%s' "$RESP" | jq -r 'keys | join(", ")')" >&2
  exit 1
fi

umask 077
printf '%s\n' "$KEY" > "$OUT"
chmod 600 "$OUT"

echo "==> Llave guardada en $OUT (permisos 600)"
echo "    Longitud: ${#KEY} caracteres (se esperan 44)"
echo "    Vista previa: ${KEY:0:4}...${KEY: -4}"
echo
echo "Siguiente paso: ábrela con 'cat $OUT' cuando la vayas a pegar en"
echo "Portainer -> Stack 'media' -> Environment variables -> WIREGUARD_PRIVATE_KEY"
