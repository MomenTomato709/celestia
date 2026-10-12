"""La jaula de la caja: se copia como `sitecustomize.py` y Python la carga al arrancar.

Enzo, 11 oct 2026: que Celestia ejecute las pruebas de lo que programa, pero en una
caja cerrada. En el móvil (PRoot) no hay bwrap, firejail ni Landlock, así que la caja
se monta dentro de Python con ganchos de auditoría (PEP 578), que no se pueden quitar
una vez puestos. Lo que hace el código de dentro:

  - escribir, borrar o cambiar ficheros: sólo dentro de la carpeta de la caja;
  - leer: la caja y lo que Python necesita para funcionar (su instalación, /usr, /etc);
    nada de la carpeta de Celestia, su memoria ni la casa de Enzo;
  - red: ninguna, salvo a un puerto de 127.0.0.1 que haya abierto la propia caja;
  - otros programas: sólo el mismo Python, que arranca otra vez con esta jaula;
  - ctypes, os.system, exec, matar procesos ajenos: no.

Sirve contra el código generado que se equivoca (borra lo que no es suyo, escribe
donde no debe, llama a internet); no es una cárcel contra quien la quiera romper a
propósito (PEP 578 lo dice: los ganchos no son un sandbox). No importa nada de
Celestia: corre sola, en el Python de la caja.
"""
import os
import sys


def _instalar():
    raiz = os.environ.get("CAJA_RAIZ")
    if not raiz:
        return
    P = os.path

    def real(ruta):
        try:
            return P.normcase(P.realpath(P.abspath(os.fsdecode(ruta))))
        except Exception:
            return None

    caja = real(raiz)
    jaula_r = P.join(caja, "_jaula")
    puertos = P.join(jaula_r, "puertos")
    apuntando = False                       # sólo la jaula escribe en _jaula/puertos
    lectura = {caja}
    for r in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix,
              P.dirname(sys.executable or ""), os.environ.get("SYSTEMROOT", ""),
              "/usr", "/lib", "/lib64", "/etc", "/proc/self", "/dev"):
        if r and P.exists(r):
            lectura.add(real(r))
    lectura.discard(None)
    escritura_dev = {real(d) for d in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")}

    def dentro(ruta, raices):
        if ruta is None:
            return False
        return any(ruta == r or ruta.startswith(r.rstrip(os.sep) + os.sep) for r in raices)

    def no(que, detalle=""):
        raise PermissionError(f"caja cerrada: no se puede {que}{': ' if detalle else ''}{detalle}")

    def ruta_de(x, dir_fd=None):
        if x is None or isinstance(x, int):
            return None
        # Sin dir_fd, Python pasa -1 (o None): la ruta es la de siempre.
        if isinstance(dir_fd, int) and dir_fd >= 0 and not P.isabs(os.fsdecode(x)):
            # «os.remove(nombre, dir_fd=fd)»: lo usa shutil.rmtree. Se mira la carpeta
            # de verdad de ese descriptor; si no se puede saber, se toma como de fuera.
            try:
                base = os.readlink(f"/proc/self/fd/{int(dir_fd)}")
            except (OSError, ValueError, TypeError):
                return "<carpeta desconocida>"
            return real(P.join(base, os.fsdecode(x)))
        return real(x)

    def solo_en_la_caja(que, *rutas, dir_fd=None):
        for x in rutas:
            if x is None or isinstance(x, int):
                continue
            r = ruta_de(x, dir_fd)
            if not dentro(r, {caja}) or (dentro(r, {jaula_r}) and not apuntando):
                no(que, os.fsdecode(x))

    def es_python(programa):
        if not programa:
            return False
        nombre = P.basename(os.fsdecode(programa)).lower()
        return (real(programa) == real(sys.executable or "") or nombre in (
            "python", "python3", "python.exe", "python3.exe")
            or (nombre.startswith("python3.") and nombre[8:].replace(".exe", "").isdigit()))

    jaula = P.join(raiz, "_jaula")

    def sin_jaula(opciones):
        """¿Lleva -I, -E o -S (solas o juntas, «-Es»)? Sin ellas no carga esta jaula."""
        i = 0
        while i < len(opciones):
            a = opciones[i]
            if not a.startswith("-") or a in ("-", "--") or a.startswith("--"):
                return False if not a.startswith("--") else sin_jaula(opciones[i + 1:])
            for j, letra in enumerate(a[1:]):
                if letra in "IES":
                    return True
                if letra in "cm":
                    return False                      # lo que sigue es código o módulo
                if letra in "WX":
                    i += 1 if j == len(a) - 2 else 0   # «-X utf8»: el valor va aparte
                    break
            i += 1
        return False

    escribe = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
               | getattr(os, "O_TEMPORARY", 0))
    # Los que llevan dir_fd como último argumento (os.remove(path, dir_fd)…).
    DIR_FD_AL_FINAL = {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown",
                       "os.utime", "os.mkfifo", "os.mknod", "shutil.rmtree"}
    cambia = {
        "os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.utime",
        "os.truncate", "os.setxattr", "os.removexattr", "os.chflags", "os.lchflags",
        "shutil.rmtree", "shutil.chown", "os.mkfifo", "os.mknod",
    }

    def gancho(evento, args):
        nonlocal apuntando
        if evento == "open":
            ruta, _, flags = args
            if ruta is None or isinstance(ruta, int):
                return
            r = ruta_de(ruta)
            if flags & escribe:
                if not (dentro(r, {caja}) or r in escritura_dev):
                    no("escribir fuera de la caja", os.fsdecode(ruta))
                if dentro(r, {jaula_r}) and not apuntando:
                    no("cambiar la propia caja", os.fsdecode(ruta))
            elif not dentro(r, lectura):
                no("leer fuera de la caja", os.fsdecode(ruta))
        elif evento in cambia:
            solo_en_la_caja("tocar ficheros fuera de la caja", args[0], dir_fd=args[-1]
                            if evento in DIR_FD_AL_FINAL else None)
        elif evento in ("os.rename", "os.link"):
            solo_en_la_caja("tocar ficheros fuera de la caja", args[0], dir_fd=args[2])
            solo_en_la_caja("tocar ficheros fuera de la caja", args[1], dir_fd=args[3])
        elif evento in ("shutil.move", "shutil.copyfile", "shutil.copytree"):
            solo_en_la_caja("tocar ficheros fuera de la caja", args[1])
            if evento == "shutil.move":
                solo_en_la_caja("tocar ficheros fuera de la caja", args[0])
        elif evento == "os.symlink":
            destino, enlace = args[0], args[1]
            solo_en_la_caja("crear enlaces fuera de la caja", enlace, dir_fd=args[2])
            objetivo = P.join(P.dirname(P.abspath(os.fsdecode(enlace))), os.fsdecode(destino))
            solo_en_la_caja("enlazar a algo de fuera de la caja", objetivo)
        elif evento in ("os.listdir", "os.scandir", "glob.glob", "pathlib.Path.glob",
                        "pathlib.Path.rglob"):
            sitio = args[0] if args and args[0] is not None else "."
            if evento.startswith("glob"):
                sitio = P.dirname(os.fsdecode(sitio)) or "."
            if not isinstance(sitio, int) and not dentro(ruta_de(sitio), lectura):
                no("mirar carpetas fuera de la caja", os.fsdecode(sitio))
        elif evento in ("sqlite3.connect", "dbm.open"):
            nombre = args[0]
            if isinstance(nombre, (str, bytes, os.PathLike)) and os.fsdecode(nombre) not in (
                    ":memory:", ""):
                solo_en_la_caja("abrir una base de datos fuera de la caja", nombre)
        elif evento in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            direccion = args[1] if len(args) > 1 else None
            if isinstance(direccion, (str, bytes)):          # socket de Unix
                solo_en_la_caja("conectar fuera de la caja", direccion)
                return
            if not (isinstance(direccion, tuple) and len(direccion) >= 2
                    and str(direccion[0]) in ("127.0.0.1", "::1", "localhost")):
                no("salir a la red", str(direccion))
            try:
                with open(puertos, encoding="utf-8") as f:
                    abiertos = {int(x) for x in f.read().split()}
            except (OSError, ValueError):
                abiertos = set()
            if int(direccion[1]) not in abiertos:
                no("conectar a un servicio del aparato", f"puerto {direccion[1]}")
        elif evento == "socket.bind":
            direccion = args[1] if len(args) > 1 else None
            if isinstance(direccion, tuple) and len(direccion) >= 2:
                if str(direccion[0]) not in ("127.0.0.1", "::1", "localhost"):
                    no("abrir un puerto que no sea de 127.0.0.1", str(direccion))
                # El gancho salta antes de enlazar: sólo se sabe el puerto que se pide
                # (con el 0 lo elige el sistema y la caja no podrá hablarse a sí misma).
                if direccion[1]:
                    apuntando = True
                    try:
                        with open(puertos, "a", encoding="utf-8") as f:
                            f.write(f"{int(direccion[1])}\n")
                    finally:
                        apuntando = False
        elif evento in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
                        "socket.gethostbyaddr"):
            nombre = args[0]
            if isinstance(nombre, bytes):
                nombre = nombre.decode("ascii", "replace")
            if nombre not in (None, "", "localhost", "127.0.0.1", "::1"):
                no("salir a la red", str(nombre))
        elif evento == "subprocess.Popen":
            programa, argumentos, _, entorno = args
            primero = programa or (argumentos[0] if isinstance(argumentos, (list, tuple))
                                   and argumentos else argumentos)
            if not es_python(primero):
                no("ejecutar otros programas", os.fsdecode(primero) if primero else "?")
            lista = [os.fsdecode(a) for a in argumentos] if isinstance(
                argumentos, (list, tuple)) else []
            if sin_jaula(lista[1:]):
                no("arrancar Python sin la caja (-I, -E o -S)")
            if entorno is not None and (entorno.get("CAJA_RAIZ") != raiz or jaula not in str(
                    entorno.get("PYTHONPATH", ""))):
                no("arrancar Python sin la caja (entorno nuevo)")
        elif evento in ("os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile",
                        "webbrowser.open", "os.add_dll_directory", "pty.spawn"):
            no("ejecutar otros programas")
        elif evento in ("os.kill", "os.killpg"):
            pid = args[0]
            if pid <= 1 or pid == os.getppid():
                no("matar procesos que no son suyos")
        elif evento.startswith(("ctypes.", "winreg.")):
            no("usar " + evento.split(".")[0])

    sys.addaudithook(gancho)


_instalar()
del _instalar
