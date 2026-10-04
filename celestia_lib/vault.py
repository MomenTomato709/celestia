"""Vault de contraseñas cifrado con AES-256 + PBKDF2 (opcionalmente con USB).

Extraído del monolito en sesión 15.
"""
import base64
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .paths import MEM_DIR
from .resources import PLATAFORMA

logger = logging.getLogger("celestia_v1")

# `cryptography` es opcional: si no está, el vault queda deshabilitado y avisa
# al usuario con mensaje claro en vez de reventar.
try:
    from cryptography.fernet import Fernet as _Fernet  # noqa: F401
    CRYPTO_DISPONIBLE = True
except ImportError:
    CRYPTO_DISPONIBLE = False
    logger.info("Paquete 'cryptography' no instalado — vault deshabilitado. "
                "Instala con: pip install cryptography")

# Argon2id es opcional pero preferido: resistente a GPU/ASIC. Si no está,
# se usa PBKDF2-SHA256 (sigue siendo seguro, solo más barato de atacar en
# hardware especializado). El KDF usado se registra en el meta de cada vault,
# así que un vault creado con PBKDF2 se sigue abriendo aunque luego se instale
# Argon2 (y viceversa) — sin romper datos existentes.
try:
    import argon2  # noqa: F401
    ARGON2_DISPONIBLE = True
except ImportError:
    ARGON2_DISPONIBLE = False

_MSG_SIN_CRYPTO = (
    "🔒 Vault deshabilitado: falta el paquete 'cryptography'. "
    "Instálalo con: pip install cryptography"
)


class GestorContrasenas:
    """Vault local de contraseñas cifrado con AES-256 (Fernet + PBKDF2).

    La contraseña maestra nunca se guarda — solo su hash de verificación.
    Los datos nunca salen del dispositivo. Opcionalmente soporta llave en USB
    como factor adicional o como reemplazo de la maestra.
    """

    _VAULT  = MEM_DIR / "vault.enc"
    _META   = MEM_DIR / "vault.meta"
    _ITERS  = 600_000  # PBKDF2 iteraciones — equilibrio seguridad/velocidad
    _KEYFILE_NAME = ".celestia.key"     # modo comodidad: clave Fernet (1 factor: el USB)
    _SECRET2FA_NAME = ".celestia.2fa"   # modo 2FA real: secreto aleatorio independiente
    # Parámetros Argon2id (recomendación OWASP para uso interactivo): 64 MiB,
    # 3 pasadas, paralelismo 2. ~0.1-0.3 s por derivación en un móvil.
    _ARGON_TIME = 3
    _ARGON_MEM  = 64 * 1024  # KiB → 64 MiB
    _ARGON_PAR  = 2

    def __init__(self):
        self._fernet = None
        self._desbloqueado_hasta: float = 0.0  # timestamp expiración sesión

    # ── Derivación de claves ───────────────────────────────────────────────
    @staticmethod
    def _kdf_preferido() -> str:
        """KDF a usar para vaults NUEVOS: Argon2id si está disponible."""
        return "argon2id" if ARGON2_DISPONIBLE else "pbkdf2"

    def _derivar_raw(self, master: str, salt: bytes, kdf: str) -> bytes:
        """Deriva 32 bytes CRUDOS de la maestra. `kdf` viene del meta del vault
        (no se asume) para poder abrir vaults creados con cualquier algoritmo."""
        if kdf == "argon2id":
            from argon2.low_level import hash_secret_raw, Type
            return hash_secret_raw(
                secret=master.encode(), salt=salt,
                time_cost=self._ARGON_TIME, memory_cost=self._ARGON_MEM,
                parallelism=self._ARGON_PAR, hash_len=32, type=Type.ID)
        # PBKDF2-SHA256 (compatibilidad con vaults antiguos o sin argon2)
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                          salt=salt, iterations=self._ITERS).derive(master.encode())

    @staticmethod
    def _fernet_key(raw: bytes) -> bytes:
        """Convierte 32 bytes crudos en una clave Fernet (base64 urlsafe)."""
        return base64.urlsafe_b64encode(raw)

    def _derivar_clave(self, master: str, salt: bytes, kdf: str = "pbkdf2") -> bytes:
        """Clave Fernet derivada solo de la maestra (modo 1-factor / 'master')."""
        return self._fernet_key(self._derivar_raw(master, salt, kdf))

    def _clave_2fa(self, master: str, salt: bytes, kdf: str, secreto_usb: bytes) -> bytes:
        """Clave Fernet del modo 2FA REAL: derivar(maestra) XOR secreto_usb.

        El secreto_usb es os.urandom(32) INDEPENDIENTE de la maestra (vive solo
        en el USB). Así, sin la maestra el USB es inútil y sin el USB la maestra
        es inútil — auténtico 'algo que sabes + algo que tienes'. (El esquema
        anterior hacía XOR de la clave consigo misma → 0, una clave constante.)
        """
        raw = self._derivar_raw(master, salt, kdf)
        mezcla = bytes(a ^ b for a, b in zip(raw, secreto_usb))
        return self._fernet_key(mezcla)

    # ── Metadatos del vault (versionados, retrocompatibles) ────────────────
    def _leer_meta(self) -> Dict:
        """Lee el meta. Formato nuevo = JSON {v,kdf,salt,mode}. Formato antiguo
        = salt crudo de 16 bytes (se interpreta como pbkdf2 / modo master)."""
        crudo = self._META.read_bytes()
        try:
            m = json.loads(crudo.decode())
            if isinstance(m, dict) and "salt" in m:
                m["salt"] = base64.b64decode(m["salt"])
                m.setdefault("kdf", "pbkdf2")
                m.setdefault("mode", "master")
                return m
        except (ValueError, UnicodeDecodeError):
            pass
        return {"v": 1, "kdf": "pbkdf2", "salt": crudo, "mode": "master"}

    def _escribir_meta(self, salt: bytes, kdf: str, mode: str) -> None:
        self._META.write_bytes(json.dumps({
            "v": 2, "kdf": kdf, "mode": mode,
            "salt": base64.b64encode(salt).decode(),
        }).encode())

    def existe_vault(self) -> bool:
        return self._VAULT.exists() and self._META.exists()

    def inicializar(self, master: str) -> str:
        """Crea un vault vacío con la contraseña maestra dada."""
        if not CRYPTO_DISPONIBLE:
            return _MSG_SIN_CRYPTO
        if self.existe_vault():
            return "✗ Ya existe un vault. Bórralo manualmente si quieres reiniciar."
        if len(master) < 6:
            return "✗ La contraseña maestra debe tener al menos 6 caracteres."
        from cryptography.fernet import Fernet
        salt = os.urandom(16)
        kdf = self._kdf_preferido()
        clave = self._derivar_clave(master, salt, kdf)
        f = Fernet(clave)
        vault_vacio = f.encrypt(json.dumps({}).encode())
        self._VAULT.write_bytes(vault_vacio)
        self._escribir_meta(salt, kdf, "master")
        self._fernet = f
        self._desbloqueado_hasta = time.time() + 900  # 15 min
        algo = "Argon2id" if kdf == "argon2id" else "PBKDF2"
        return f"✓ Vault de contraseñas creado y desbloqueado por 15 minutos ({algo})."

    def desbloquear(self, master: str, minutos: int = 15) -> bool:
        if not CRYPTO_DISPONIBLE:
            return False
        if not self.existe_vault():
            return False
        try:
            from cryptography.fernet import Fernet
            meta = self._leer_meta()
            if meta.get("mode") == "2fa":
                # Vault en modo doble factor: la maestra sola no basta.
                return False
            clave = self._derivar_clave(master, meta["salt"], meta["kdf"])
            f = Fernet(clave)
            f.decrypt(self._VAULT.read_bytes())
            self._fernet = f
            self._desbloqueado_hasta = time.time() + minutos * 60
            return True
        except Exception:
            self._fernet = None
            return False

    def esta_desbloqueado(self) -> bool:
        return self._fernet is not None and time.time() < self._desbloqueado_hasta

    def bloquear(self) -> None:
        self._fernet = None
        self._desbloqueado_hasta = 0.0

    def _leer(self) -> Dict[str, Dict]:
        if not self.esta_desbloqueado():
            raise PermissionError("vault bloqueado")
        return json.loads(self._fernet.decrypt(self._VAULT.read_bytes()))

    def _escribir(self, datos: Dict[str, Dict]) -> None:
        if not self.esta_desbloqueado():
            raise PermissionError("vault bloqueado")
        self._VAULT.write_bytes(self._fernet.encrypt(json.dumps(datos).encode()))

    def guardar(self, sitio: str, usuario: str, password: str, nota: str = "") -> str:
        try:
            datos = self._leer()
        except PermissionError:
            return "🔒 Vault bloqueado. Dime la contraseña maestra primero."
        clave = re.sub(r"\W+", "_", sitio.lower().strip())
        datos[clave] = {
            "sitio":    sitio,
            "usuario":  usuario,
            "password": password,
            "nota":     nota,
            "ts":       datetime.now().isoformat(),
        }
        self._escribir(datos)
        return f"✓ Contraseña de '{sitio}' guardada en el vault."

    def obtener(self, sitio: str) -> str:
        try:
            datos = self._leer()
        except PermissionError:
            return "🔒 Vault bloqueado. Dime la contraseña maestra primero."
        clave = re.sub(r"\W+", "_", sitio.lower().strip())
        d = datos.get(clave)
        if not d:
            for k, v in datos.items():
                if clave in k or any(w in k for w in clave.split("_") if len(w) > 2):
                    d = v
                    break
        if not d:
            return f"No encontré contraseña para '{sitio}'."
        # Privacidad: la contraseña viaja por WhatsApp y queda en historial.
        pw = d["password"]
        pw_masked = pw[:2] + "•" * max(4, len(pw) - 4) + pw[-2:] if len(pw) >= 4 else "••••"
        try:
            import shutil as _sh
            if _sh.which("termux-clipboard-set"):
                subprocess.run(["termux-clipboard-set", pw], timeout=3,
                                capture_output=True, check=False)
                copiado = " (copié la contraseña al portapapeles)"
            else:
                copiado = ""
        except Exception:
            copiado = ""
        return (f"🔑 {d['sitio']}\n"
                f"   Usuario: {d['usuario']}\n"
                f"   Password: {pw_masked}{copiado}\n"
                + (f"   Nota: {d['nota']}\n" if d.get("nota") else "")
                + "   (dime 'muéstrame la contraseña' si la quieres ver completa)")

    def obtener_completo(self, sitio: str) -> str:
        """Devuelve la contraseña sin enmascarar — solo cuando el usuario lo pide explícitamente."""
        try:
            datos = self._leer()
        except PermissionError:
            return "🔒 Vault bloqueado. Dime la contraseña maestra primero."
        clave = re.sub(r"\W+", "_", sitio.lower().strip())
        d = datos.get(clave) or next(
            (v for k, v in datos.items() if clave in k), None)
        if not d:
            return f"No encontré contraseña para '{sitio}'."
        return (f"🔑 {d['sitio']}\n"
                f"   Usuario: {d['usuario']}\n"
                f"   Password: {d['password']}\n"
                + (f"   Nota: {d['nota']}" if d.get("nota") else ""))

    def listar(self) -> str:
        try:
            datos = self._leer()
        except PermissionError:
            return "🔒 Vault bloqueado. Dime la contraseña maestra primero."
        if not datos:
            return "Vault vacío. Guarda una contraseña con: 'guarda contraseña de [sitio]: usuario X, password Y'"
        lineas = [f"🔐 {len(datos)} contraseñas guardadas:\n"]
        for d in datos.values():
            lineas.append(f"  • {d['sitio']} (usuario: {d['usuario']})")
        return "\n".join(lineas)

    def eliminar(self, sitio: str) -> str:
        try:
            datos = self._leer()
        except PermissionError:
            return "🔒 Vault bloqueado. Dime la contraseña maestra primero."
        clave = re.sub(r"\W+", "_", sitio.lower().strip())
        if clave not in datos:
            return f"No encontré contraseña para '{sitio}'."
        del datos[clave]
        self._escribir(datos)
        return f"✓ Contraseña de '{sitio}' eliminada."

    # ── Soporte de llave en USB / unidad externa ──────────────────────────
    @staticmethod
    def detectar_usbs() -> List[str]:
        """Devuelve rutas de unidades externas montadas (USB, SD, etc.)."""
        candidatas: List[str] = []
        bases = []
        if PLATAFORMA == "android":
            bases = ["/mnt/media_rw", "/storage", "/mnt/usbotg"]
        elif PLATAFORMA == "linux":
            user = os.environ.get("USER", "root")
            bases = ["/media", f"/media/{user}", "/run/media",
                     f"/run/media/{user}", "/mnt"]
        elif PLATAFORMA == "macos":
            bases = ["/Volumes"]
        elif PLATAFORMA == "windows":
            import string
            for letra in string.ascii_uppercase[3:]:
                ruta = f"{letra}:\\"
                if os.path.exists(ruta):
                    candidatas.append(ruta)
            return candidatas
        for base in bases:
            if os.path.isdir(base):
                try:
                    for entrada in os.listdir(base):
                        ruta = os.path.join(base, entrada)
                        if os.path.isdir(ruta) and entrada not in ("emulated", "self"):
                            candidatas.append(ruta)
                except PermissionError:
                    continue
        return candidatas

    def buscar_keyfile(self) -> Optional[str]:
        """Busca el archivo de llave en USBs conectados."""
        for usb in self.detectar_usbs():
            ruta_key = os.path.join(usb, self._KEYFILE_NAME)
            if os.path.isfile(ruta_key):
                return ruta_key
        return None

    def buscar_secreto_2fa(self) -> Optional[str]:
        """Busca el archivo de secreto 2FA (os.urandom) en USBs conectados."""
        for usb in self.detectar_usbs():
            ruta = os.path.join(usb, self._SECRET2FA_NAME)
            if os.path.isfile(ruta):
                return ruta
        return None

    def exportar_llave_a_usb(self, master: str, ruta_usb: str = "") -> str:
        """Modo COMODIDAD (1 factor): guarda en el USB la clave Fernet del vault
        para desbloquear con solo conectar el USB, sin teclear la maestra.

        OJO: esto NO es doble factor — quien tenga el USB abre el vault. Para
        2FA real (maestra + USB) usa `activar_2fa_usb`.
        """
        if not self.existe_vault():
            return "✗ No hay vault todavía. Crea uno primero."
        meta = self._leer_meta()
        if meta.get("mode") == "2fa":
            return ("✗ Este vault está en modo doble factor. La llave de "
                    "comodidad solo aplica a vaults de un factor.")
        clave = self._derivar_clave(master, meta["salt"], meta["kdf"])
        try:
            from cryptography.fernet import Fernet
            Fernet(clave).decrypt(self._VAULT.read_bytes())
        except Exception:
            return "✗ Contraseña maestra incorrecta."
        if not ruta_usb:
            usbs = self.detectar_usbs()
            if not usbs:
                return ("✗ No detecté ningún USB conectado.\n"
                        "Conecta uno y vuelve a intentarlo, o di la ruta exacta.")
            ruta_usb = usbs[0]
        if not os.path.isdir(ruta_usb):
            return f"✗ La ruta '{ruta_usb}' no existe o no es una carpeta."
        keyfile_path = os.path.join(ruta_usb, self._KEYFILE_NAME)
        try:
            Path(keyfile_path).write_bytes(clave)
            if PLATAFORMA == "windows":
                subprocess.run(["attrib", "+h", keyfile_path], capture_output=True)
            return (f"✓ Llave guardada en USB: {keyfile_path}\n"
                    f"Ahora podrás desbloquear el vault con solo conectar el USB.\n"
                    f"⚠ Es UN factor: quien tenga ese USB puede abrir tu vault.")
        except Exception as e:
            return f"✗ No pude escribir en el USB: {e}"

    def desbloquear_con_usb(self, minutos: int = 15) -> str:
        """Desbloquea el vault (modo comodidad) leyendo la clave del USB."""
        if not self.existe_vault():
            return "✗ No hay vault todavía."
        if self._leer_meta().get("mode") == "2fa":
            return ("✗ Vault en modo doble factor: necesitas la contraseña "
                    "maestra ADEMÁS del USB. Dime la maestra.")
        keyfile = self.buscar_keyfile()
        if not keyfile:
            return ("✗ No encontré la llave en ningún USB conectado.\n"
                    "Conecta el USB con la llave guardada y vuelve a intentar.")
        try:
            from cryptography.fernet import Fernet
            clave = Path(keyfile).read_bytes()
            f = Fernet(clave)
            f.decrypt(self._VAULT.read_bytes())
            self._fernet = f
            self._desbloqueado_hasta = time.time() + minutos * 60
            return f"🔓 Vault desbloqueado con USB ({os.path.dirname(keyfile)}). Activo {minutos} min."
        except Exception as e:
            return f"✗ La llave del USB no es válida para este vault: {e}"

    def activar_2fa_usb(self, master: str, ruta_usb: str = "") -> str:
        """Convierte el vault a DOBLE FACTOR REAL (maestra + USB).

        Genera un secreto aleatorio independiente (`os.urandom(32)`), lo guarda
        en el USB, y RE-CIFRA el vault con la clave combinada
        `derivar(maestra) XOR secreto_usb`. A partir de aquí hacen falta AMBOS
        factores: la maestra sola no abre, el USB solo tampoco.
        """
        if not CRYPTO_DISPONIBLE:
            return _MSG_SIN_CRYPTO
        if not self.existe_vault():
            return "✗ No hay vault todavía. Crea uno primero."
        from cryptography.fernet import Fernet
        meta = self._leer_meta()
        if meta.get("mode") == "2fa":
            return "✗ El vault ya está en modo doble factor."
        # 1) Verificar maestra y leer datos en claro
        try:
            clave_actual = self._derivar_clave(master, meta["salt"], meta["kdf"])
            datos_claros = Fernet(clave_actual).decrypt(self._VAULT.read_bytes())
        except Exception:
            return "✗ Contraseña maestra incorrecta."
        # 2) Localizar el USB destino
        if not ruta_usb:
            usbs = self.detectar_usbs()
            if not usbs:
                return ("✗ No detecté ningún USB conectado.\n"
                        "Conecta uno y vuelve a intentarlo, o di la ruta exacta.")
            ruta_usb = usbs[0]
        if not os.path.isdir(ruta_usb):
            return f"✗ La ruta '{ruta_usb}' no existe o no es una carpeta."
        # 3) Secreto aleatorio INDEPENDIENTE de la maestra → al USB
        secreto_usb = os.urandom(32)
        secret_path = os.path.join(ruta_usb, self._SECRET2FA_NAME)
        # 4) Re-cifrar con derivar(maestra) XOR secreto_usb
        nueva_clave = self._clave_2fa(master, meta["salt"], meta["kdf"], secreto_usb)
        try:
            Path(secret_path).write_bytes(secreto_usb)
            if PLATAFORMA == "windows":
                subprocess.run(["attrib", "+h", secret_path], capture_output=True)
        except Exception as e:
            return f"✗ No pude escribir el secreto en el USB: {e}"
        nf = Fernet(nueva_clave)
        self._VAULT.write_bytes(nf.encrypt(datos_claros))
        self._escribir_meta(meta["salt"], meta["kdf"], "2fa")
        self._fernet = nf
        self._desbloqueado_hasta = time.time() + 900
        return ("🔒🗝️ Doble factor activado. Ahora necesitas tu contraseña "
                "maestra Y este USB para abrir el vault. Guarda el USB a buen "
                "recaudo: sin él, ni tú podrás abrirlo.")

    def desbloquear_con_master_y_usb(self, master: str, minutos: int = 15) -> str:
        """Desbloqueo DOBLE FACTOR REAL: contraseña maestra + secreto del USB.

        Clave = derivar(maestra) XOR secreto_usb. Faltando cualquiera de los
        dos, no abre.
        """
        if not self.existe_vault():
            return "✗ No hay vault todavía."
        meta = self._leer_meta()
        if meta.get("mode") != "2fa":
            return "✗ Este vault no está en modo doble factor. Usa la contraseña maestra."
        secret_path = self.buscar_secreto_2fa()
        if not secret_path:
            return "✗ Necesitas el USB con el secreto conectado para el modo doble factor."
        try:
            secreto_usb = Path(secret_path).read_bytes()
            clave = self._clave_2fa(master, meta["salt"], meta["kdf"], secreto_usb)
            from cryptography.fernet import Fernet
            f = Fernet(clave)
            f.decrypt(self._VAULT.read_bytes())
            self._fernet = f
            self._desbloqueado_hasta = time.time() + minutos * 60
            return f"🔓🗝️ Vault desbloqueado con doble factor (contraseña + USB). Activo {minutos} min."
        except Exception:
            self._fernet = None
            return "✗ Falló el desbloqueo doble factor: contraseña o USB incorrectos."
