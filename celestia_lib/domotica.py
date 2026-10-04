"""DomoticaManager: registro y control de dispositivos del hogar.

Extraído del monolito en sesión 15. Persiste en `memoria/dispositivos.json`.
Skills aprendidas para controlar dispositivos van en `skills/domotica/`.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import MEM_DIR, ROOT, SKILLS_DIR

logger = logging.getLogger("celestia_v1")

DOMOTICA_SKILLS_DIR = SKILLS_DIR / "domotica"


class DomoticaManager:
    """
    Registro y control de dispositivos del hogar.
    Cada dispositivo puede controlarse con un script Python aprendido
    o con parámetros de conexión conocidos.
    """

    _REGISTRO  = MEM_DIR / "dispositivos.json"
    _AUTH_RE   = re.compile(
        r"401|403|unauthorized|invalid[_\s]?(?:token|key|api)|"
        r"authentication|forbidden|access[_\s]?denied|"
        r"requires?[_\s]?(?:auth|key|token)|credencial|no[_\s]?autorizado",
        re.I,
    )

    def __init__(self):
        self._dispositivos: Dict[str, Dict] = {}
        self._cargar()
        DOMOTICA_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    def _cargar(self):
        if self._REGISTRO.exists():
            try:
                self._dispositivos = json.loads(self._REGISTRO.read_text(encoding="utf-8"))
            except Exception:
                self._dispositivos = {}

    def _guardar(self):
        self._REGISTRO.parent.mkdir(parents=True, exist_ok=True)
        self._REGISTRO.write_text(
            json.dumps(self._dispositivos, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _clave(self, nombre: str) -> str:
        return re.sub(r"[^\w]", "_", nombre.lower().strip())

    def _buscar(self, nombre: str) -> Optional[Dict]:
        clave = self._clave(nombre)
        if clave in self._dispositivos:
            return self._dispositivos[clave]
        for k, d in self._dispositivos.items():
            if clave in k or any(w in k for w in clave.split("_") if len(w) > 2):
                return d
        return None

    def registrar(self, nombre: str, ip: str = "", protocolo: str = "",
                  tipo: str = "dispositivo", **extras) -> str:
        clave = self._clave(nombre)
        entrada = {"nombre": nombre, "tipo": tipo}
        if ip:        entrada["ip"]        = ip
        if protocolo: entrada["protocolo"] = protocolo
        entrada.update(extras)
        self._dispositivos[clave] = entrada
        self._guardar()
        detalles = f" ({protocolo} · {ip})" if ip else ""
        return f"✓ Dispositivo '{nombre}' registrado{detalles}."

    def guardar_credencial(self, nombre: str, clave_cred: str, valor: str) -> str:
        """Guarda una credencial (token, api_key, ip…) para un dispositivo."""
        clave = self._clave(nombre)
        if clave not in self._dispositivos:
            self._dispositivos[clave] = {"nombre": nombre}
        self._dispositivos[clave][clave_cred] = valor
        self._guardar()
        return f"✓ Credencial '{clave_cred}' guardada para '{nombre}'."

    def guardar_skill(self, nombre: str, accion: str, codigo: str) -> Path:
        """Guarda un script Python aprendido para controlar el dispositivo."""
        clave = self._clave(nombre)
        ruta = DOMOTICA_SKILLS_DIR / f"{clave}_{accion}.py"
        ruta.write_text(codigo, encoding="utf-8")
        if clave not in self._dispositivos:
            self._dispositivos[clave] = {"nombre": nombre}
        self._dispositivos[clave][f"skill_{accion}"] = str(ruta)
        self._guardar()
        return ruta

    def _ejecutar_skill(self, ruta: str, timeout: int = 15) -> Tuple[bool, str]:
        try:
            res = subprocess.run(
                ["python3", ruta], capture_output=True, text=True, timeout=timeout
            )
            if res.returncode == 0:
                return True, res.stdout.strip()
            return False, (res.stderr.strip() or res.stdout.strip())
        except subprocess.TimeoutExpired:
            return False, "Timeout"
        except Exception as e:
            return False, str(e)

    def ejecutar(self, nombre: str, accion: str, valor: int = None) -> str:
        """
        Ejecuta una acción sobre el dispositivo.
        Devuelve resultado, '__APRENDER__' si no sabe cómo, o '__AUTH__:descripción'.
        """
        d = self._buscar(nombre)
        if not d:
            return "__APRENDER__"

        # Intentar skill aprendido primero
        skill_path = d.get(f"skill_{accion}")
        if skill_path and Path(skill_path).exists():
            ok, salida = self._ejecutar_skill(skill_path)
            if ok:
                return salida or f"✓ {d['nombre']} — {accion} ejecutado."
            if self._AUTH_RE.search(salida):
                return f"__AUTH__:{salida[:200]}"
            # Skill falló por otro motivo — re-aprender
            return "__APRENDER__"

        # Sin skill → aprender
        return "__APRENDER__"

    def listar(self) -> str:
        if not self._dispositivos:
            return ("No tengo dispositivos registrados todavía.\n"
                    "Dime qué dispositivo quieres controlar y lo aprendo.")
        lineas = ["Dispositivos registrados:\n"]
        for _, d in self._dispositivos.items():
            skills = [k.replace("skill_", "") for k in d if k.startswith("skill_")]
            info = f" · acciones: {', '.join(skills)}" if skills else ""
            lineas.append(f"• {d['nombre']} ({d.get('tipo', 'dispositivo')}){info}")
        return "\n".join(lineas)

