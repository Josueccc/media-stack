#!/usr/bin/env bash
# Convierte los request headers de YouTube Music en headers_auth.json.
#
#   1. music.youtube.com (con sesión iniciada) -> F12 -> pestaña Network
#   2. filtra por /browse, clic en cualquier petición POST
#   3. Request Headers -> vista "Raw" -> copiar todo
#   4. pégalo en un fichero de texto y pásalo a este script
#
# Uso: ./make-auth.sh ~/ytm-headers.txt
#
# Evita los dos vicios del CLI de ytmusicapi: que se queda esperando Ctrl-D
# y que devuelve exit 1 aunque haya escrito bien el fichero.
set -euo pipefail

SRC="${1:?uso: make-auth.sh <fichero-con-los-headers-pegados>}"
DEST="${DEST:-/opt/appdata/ytm-bridge}"
IMAGE="${IMAGE:-ytm-bridge:local}"
OUT="$DEST/headers_auth.json"

[[ -r "$SRC" ]] || { echo "No puedo leer $SRC"; exit 1; }
[[ -d "$DEST" ]] || {
  echo "No existe $DEST. Créalo primero:"
  echo "  sudo mkdir -p $DEST/takeout && sudo chown -R 1000:1000 $DEST"
  exit 1
}

# Los headers que ytmusicapi exige de verdad. Avisamos antes para no dar
# un error críptico a mitad del proceso.
for h in cookie x-goog-authuser; do
  grep -qi "^[[:space:]]*$h:" "$SRC" || {
    echo "Falta '$h' en $SRC."
    echo "Copiaste los Response Headers o una petición sin sesión."
    echo "Repite con una POST a /browse y la vista Raw de Request Headers."
    exit 1
  }
done

docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "==> Construyendo $IMAGE"
  docker build -t "$IMAGE" "$(dirname "$0")"
}

echo "==> Escribiendo $OUT"
# El exit 1 es esperado incluso cuando funciona: validamos por el fichero.
docker run --rm -i -v "$DEST:/data" "$IMAGE" \
  ytmusicapi browser --file /data/headers_auth.json <"$SRC" >/dev/null 2>&1 || true

if [[ -s "$OUT" ]] && python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$OUT" 2>/dev/null; then
  echo "LISTO. Ahora: docker compose up -d ytm-bridge"
else
  echo "No se generó un JSON válido. Revisa que $SRC sean los Request Headers en crudo."
  exit 1
fi
