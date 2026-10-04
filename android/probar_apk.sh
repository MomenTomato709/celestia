#!/usr/bin/env bash
# Instala la APK en un emulador, la abre y comprueba que Celestia contesta
# dentro del móvil. Deja una captura de pantalla y el logcat en prueba/.
# Lo usa .github/workflows/android.yml dentro del emulador de GitHub.
set -uo pipefail
APK="$1"
PAQUETE="io.github.momentomato709.celestia_movil"
mkdir -p prueba
fallo() { echo "✗ $*"; adb logcat -d > prueba/logcat.txt; adb exec-out screencap -p > prueba/captura.png; exit 1; }

adb install -r "$APK" || fallo "no se instala"
echo "✓ instalada"
adb logcat -c
adb shell monkey -p "$PAQUETE" -c android.intent.category.LAUNCHER 1 >/dev/null || fallo "no se abre"
echo "▸ abierta; esperando a Celestia dentro del móvil…"
adb forward tcp:8765 tcp:8765

for i in $(seq 1 120); do
  curl -sf http://127.0.0.1:8765/estado -o prueba/estado.json && break
  sleep 3
  [ "$i" = 120 ] && fallo "Celestia no contesta en 6 minutos"
done
echo "✓ Celestia contesta dentro del móvil (~$((i * 3)) s)"
sleep 8                                   # que la vista web cargue el chat
adb exec-out screencap -p > prueba/captura.png

python3 - <<'EOF' || fallo "las comprobaciones no pasan"
import json, urllib.request
base = "http://127.0.0.1:8765"
estado = json.load(open("prueba/estado.json"))
assert estado.get("sin_cerebro") is True, estado
print("✓ /estado: recién instalada, sin clave")
chat = urllib.request.urlopen(base + "/chat", timeout=30).read().decode()
assert "portadaSinCerebro" in chat
print("✓ /chat se sirve")
req = urllib.request.Request(base + "/mensaje", method="POST",
      data=json.dumps({"texto": "¿Cuál es la capital de Francia?", "canal": "web"}).encode(),
      headers={"Content-Type": "application/json"})
r = json.load(urllib.request.urlopen(req, timeout=180))
texto = r.get("texto", "")
print("  respuesta:", texto[:120])
assert texto and "no puedo conectar" not in texto, r
print("✓ contesta")
# Hablar y buscar de verdad dentro del móvil (4 oct 2026: la app no hablaba, y
# el metabuscador depende de primp, una rueda fabricada para Android aparte).
s = json.load(urllib.request.urlopen(base + "/sentidos?probar=1", timeout=180))
json.dump(s, open("prueba/sentidos.json", "w"), ensure_ascii=False, indent=1)
sentidos = {x["id"]: x for x in s["sentidos"]}
print("  sentidos:", {k: (v["ok"], v["como"]) for k, v in sentidos.items()})
print("  pruebas:", s.get("pruebas"))
assert sentidos["hablar"]["ok"], sentidos["hablar"]
assert s["pruebas"]["hablar"] is True, s["pruebas"]
print("✓ habla (edge-tts dentro de la app)")
assert sentidos["buscar"]["como"] == "varios buscadores a la vez", sentidos["buscar"]
assert s["pruebas"]["primp"] in (200, 204), s["pruebas"]
print("✓ primp (el motor del metabuscador) funciona dentro de la app")
assert isinstance(s["pruebas"]["buscar"], int) and s["pruebas"]["buscar"] > 0, s["pruebas"]
print("✓ busca con el metabuscador")
EOF
adb logcat -d > prueba/logcat.txt
echo "✓ la APK funciona"
