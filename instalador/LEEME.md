# Instaladores de Celestia

Para quien la instala: **descargar, abrir y usar**. Sin instalar Python ni escribir comandos.

| Sistema | Archivo | Cómo se abre |
|---|---|---|
| Windows 10/11 | `Celestia-Instalador-Windows.exe` | Doble clic → Siguiente → «Abrir Celestia ahora». Deja un acceso directo en el escritorio. No pide permisos de administrador. |
| Mac (M1 o posterior) | `Celestia-Mac-arm64.zip` | Descomprimir y arrastrar Celestia a Aplicaciones. La primera vez macOS la bloquea porque no está firmada por Apple: Ajustes del Sistema → Privacidad y seguridad → «Abrir igualmente». |
| Mac (Intel) | `Celestia-Mac-x64.zip` | Igual que el anterior. |
| Linux | `Celestia-Linux-x64.tar.gz` / `-arm64` | Descomprimir y abrir `Celestia.sh`. `instalar-acceso-directo.sh` la pone en el menú. |
| Android 8+ | `Celestia-Android.apk` (flujo «Android») | Abrir el archivo y permitir «instalar apps de origen desconocido». Celestia corre dentro de la app, sin Termux. |

**Probados de verdad en cada sistema** (máquinas de GitHub, en cada fabricación): se instalan,
arrancan, sirven el chat y contestan. Windows se instala en ~30 s y arranca en ~26 s; Mac en
~8-22 s; la APK arranca en ~12 s en un emulador de Android.

La APK v1 no maneja el móvil (Shizuku), no juega al ZZZ ni hace calcos de fotos (no hay
scipy para su Python), y Android la congela al cerrarla. El resto, igual que en el PC.

Al abrirse sale una ventanita («Celestia está encendida», con *Abrir el chat* y *Apagar*) y el chat en el navegador.
La primera vez el chat explica cómo sacar una clave gratis de Gemini o Groq: se pega en el chat y ya piensa.
Cerrar la ventanita apaga Celestia.

Lo de la persona (conversaciones, memoria, claves en `.env`) se queda en su aparato, junto al programa.
Reinstalar o actualizar no lo toca. Al desinstalar en Windows se pregunta si borrarlo.

## Qué no hace en un PC

Lo que necesita un móvil Android con Shizuku: abrir apps, llamar, linterna, WiFi, jugar al ZZZ.
En un PC lo dice en vez de fingirlo. Tampoco lleva modelos locales (torch): piensa con proveedores de la nube.

## Fabricarlos

```bash
python3 instalador/construir.py windows-x64   # o macos-arm64, macos-x64, linux-x64, linux-arm64
```

Hace falta Python 3.11+, `pip`, Pillow (para el icono) y, para el de Windows, NSIS (`apt install nsis`).
Funciona desde cualquier sistema, también desde el móvil: no compila nada.
Empaqueta el **último commit** (`--commit` para otro); los cambios sin commitear no entran.
En GitHub (repositorio privado `MomenTomato709/celestia-app`) lo hacen solos
`.github/workflows/instaladores.yml` y `android.yml`, al lanzarlos a mano (pestaña Actions) o al
etiquetar una versión (`git tag v2.1 && git push --tags`): entonces se publican en «Releases».
