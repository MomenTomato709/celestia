#!/usr/bin/env bash
# Instala un paquete de Celestia como lo haría cualquiera, la abre y comprueba
# que contesta. Lo usa el flujo «Instaladores» en máquinas Windows, Mac y Linux
# de GitHub: así los instaladores se prueban en su sistema de verdad, no sólo
# se fabrican.
#
#   bash instalador/probar_paquete.sh <destino> <carpeta con el paquete>
set -euo pipefail
destino="$1"; carpeta="$2"
PUERTO=8765
fallo() { echo "✗ $*"; exit 1; }

case "$destino" in
  windows-*)
    # /S: instalación silenciosa (la misma que con las ventanas, sin preguntar).
    echo "▸ Instalando…"
    # MSYS2_ARG_CONV_EXCL: la consola Bash de Windows convierte lo que empieza
    # por «/» en una ruta, y el /S llegaba cambiado — el instalador abría sus
    # ventanas y esperaba a que alguien pulsara «Siguiente».
    MSYS2_ARG_CONV_EXCL="*" "$carpeta/Celestia-Instalador-Windows.exe" /S &
    inst="$(cygpath -u "$LOCALAPPDATA")/Programs/Celestia"
    # Cuántos archivos van, cada 30 s: distingue «va lento» (el antivirus
    # revisando miles de archivos) de «se ha colgado».
    t0=$(date +%s)
    for i in $(seq 1 240); do
      [ -f "$inst/Desinstalar.exe" ] && break
      [ $((i % 10)) = 0 ] && echo "  … $(( $(date +%s) - t0 )) s, $(find "$inst" -type f 2>/dev/null | wc -l) archivos"
      sleep 3
    done
    [ -f "$inst/Desinstalar.exe" ] || fallo "el instalador no termina en 12 minutos"
    sleep 5                                    # que termine de escribir
    echo "▸ Instalado en $inst en $(( $(date +%s) - t0 )) s ($(find "$inst" -type f | wc -l) archivos)"
    [ -f "$inst/python/pythonw.exe" ] || fallo "no se instaló en $inst"
    [ -f "$(cygpath -u "$USERPROFILE")/Desktop/Celestia.lnk" ] || echo "! sin acceso directo en el escritorio"
    datos="$inst"
    echo "▸ Abriendo Celestia…"
    "$inst/python/python.exe" "$inst/lanzador.py" > lanzador.out 2>&1 &
    ;;
  macos-*)
    unzip -q "$carpeta"/Celestia-Mac-*.zip -d app
    datos="$HOME/Library/Application Support/Celestia"
    app/Celestia.app/Contents/MacOS/Celestia > lanzador.out 2>&1 &
    ;;
  linux-*)
    tar -xzf "$carpeta"/Celestia-Linux-*.tar.gz
    datos="$PWD/Celestia"
    ./Celestia/Celestia.sh > lanzador.out 2>&1 &
    ;;
  *) fallo "destino desconocido: $destino" ;;
esac

mostrar_logs() {
  echo "── lanzador.out"; cat lanzador.out || true
  echo "── logs de Celestia"; tail -n 60 "$datos"/logs/*.log 2>/dev/null || true
}
trap 'mostrar_logs' ERR

echo "▸ Esperando a que conteste…"
for i in $(seq 1 90); do
  curl -sf "http://127.0.0.1:$PUERTO/estado" -o estado.json && break
  sleep 2
  [ "$i" = 90 ] && { mostrar_logs; fallo "no contesta en 3 minutos"; }
done
echo "✓ arrancó en ~$((i * 2)) s"

# En el Windows de GitHub el Python del sistema se llama `python`.
PY="$(command -v python3 || command -v python)"
# Y su consola es cp1252: sin esto, imprimir «✓» rompía la prueba.
PYTHONIOENCODING=utf-8 "$PY" - "$PUERTO" <<'EOF' || { mostrar_logs; fallo "las comprobaciones no pasan"; }
import json, sys, urllib.request
puerto = sys.argv[1]
base = f"http://127.0.0.1:{puerto}"

def mensaje(texto):
    req = urllib.request.Request(base + "/mensaje", method="POST",
                                 data=json.dumps({"texto": texto, "canal": "web"}).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))

estado = json.load(open("estado.json"))
assert estado.get("sin_cerebro") is True, f"sin_cerebro: {estado.get('sin_cerebro')}"
print("✓ /estado: recién instalada, sin clave")

chat = urllib.request.urlopen(base + "/chat", timeout=30).read().decode()
assert "portadaSinCerebro" in chat, "el chat no trae la portada de la clave"
assert 'id="pestanas"' in chat, "el chat no trae las pestañas"
print("✓ /chat se sirve, con sus pestañas")

for tipo in ("proyectos", "documentos", "imagenes"):
    r = json.load(urllib.request.urlopen(f"{base}/espacio/archivos?tipo={tipo}", timeout=30))
    assert isinstance(r.get("elementos"), list), r
print("✓ las pestañas Proyectos y Archivos leen sus carpetas")

assert "Elige tu sistema" in urllib.request.urlopen(base + "/descargar", timeout=30).read().decode()
print("✓ /descargar se sirve")

# Sin clave hay dos salidas buenas: contesta por el proveedor sin clave (desde
# las máquinas de GitHub suele funcionar) o explica cómo conseguir una. Lo
# malo sería el «no puedo conectar» o un silencio.
r = mensaje("¿Cuál es la capital de Francia?")
texto = r.get("texto", "")
assert texto and "no puedo conectar" not in texto, r
assert "París" in texto or "clave de IA" in texto, r
print("✓ sin clave contesta" if "París" in texto else "✓ sin clave explica cómo conseguirla")

r = mensaje("mi clave es AIzaSy" + "x" * 33)
assert r.get("era_clave") and "no la acepta" in r.get("texto", ""), r
print("✓ una clave falsa se comprueba contra Google y se rechaza")
EOF

[ ! -e "$datos/.env" ] || ! grep -q "AIzaSy" "$datos/.env" || fallo "guardó la clave falsa"

# Instalar ENCIMA con Celestia abierta (3 oct 2026: «no deja instalarlo» — los
# archivos en uso no se podían sobrescribir). El instalador tiene que cerrarla,
# poner los archivos nuevos y dejar que vuelva a abrirse.
if [[ "$destino" == windows-* ]]; then
  echo "▸ Instalando encima con Celestia abierta…"
  # Una marca en un archivo del programa: si el instalador lo sobrescribe, se
  # va. (La fecha de python.exe no sirve: con el proceso vivo ni se puede
  # cambiar, y el instalador conserva la fecha original del archivo.)
  echo "# marca de la prueba" >> "$inst/lanzador.py"
  MSYS2_ARG_CONV_EXCL="*" "$carpeta/Celestia-Instalador-Windows.exe" /S
  ! curl -sf -m 5 "http://127.0.0.1:$PUERTO/estado" -o /dev/null \
    || fallo "la Celestia vieja sigue abierta tras reinstalar"
  ! grep -q "marca de la prueba" "$inst/lanzador.py" \
    || fallo "con Celestia abierta no sobrescribió sus archivos"
  # Y python.exe, el que estaba en uso: escribible ya, porque nadie lo usa.
  ( exec 3>>"$inst/python/python.exe" ) 2>/dev/null \
    || fallo "python.exe sigue en uso tras reinstalar"
  "$inst/python/python.exe" "$inst/lanzador.py" > lanzador2.out 2>&1 &
  for i in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$PUERTO/estado" -o /dev/null && break
    sleep 2
    [ "$i" = 60 ] && { cat lanzador2.out; fallo "no vuelve a abrir tras reinstalar encima"; }
  done
  echo "✓ instalar encima cierra la abierta, sobrescribe y vuelve a abrir"
fi
echo "✓ $destino: instalado, abierto y contestando"
