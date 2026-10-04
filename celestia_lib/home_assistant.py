"""
Cliente Home Assistant para Celestia.

Permite que Celestia controle TODO lo que Home Assistant integra (>2000
marcas: bombillas, enchufes, TVs, AC, cerraduras, robots aspiradoras, etc.).

Requiere:
- Una instancia de Home Assistant corriendo (Raspberry Pi, mini-PC, etc.).
- Un Long-Lived Access Token: en HA → perfil → abajo → "Crear token".

Config:
- HA_URL  (env)  ej. http://192.168.1.50:8123 o https://ha.mihogar.es
- HA_TOKEN (env) el token largo

Uso desde agent.py:
    from .home_assistant import HomeAssistant
    ha = HomeAssistant()
    if ha.disponible():
        ha.encender("light.salon")
        ha.apagar("switch.cafetera")
        ha.ejecutar_servicio("media_player", "play_media",
                             {"entity_id": "media_player.tv_salon", "..."})

Diseño:
- Cliente síncrono con `requests` (no introduce asyncio en celestia_lib).
- Timeouts cortos (3s) para no bloquear el endpoint /mensaje.
- Resoluciones de alias en español ("luz del salón" → entidad correcta) se
  hacen vía `_buscar_entidad` con fuzzy matching contra friendly_name.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

logger = logging.getLogger("celestia_v1")

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class HomeAssistant:
    """Cliente REST mínimo de Home Assistant."""

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None,
                 timeout: float = 3.0):
        self.url = (url or os.environ.get("HA_URL", "")).rstrip("/")
        self.token = token or os.environ.get("HA_TOKEN", "")
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        self._cache_entidades: Optional[List[Dict[str, Any]]] = None
        self._cache_ts: float = 0.0

    # ── Estado ─────────────────────────────────────────────────────────
    def disponible(self) -> bool:
        """True si está configurado y responde."""
        if not HAS_REQUESTS or not self.url or not self.token:
            return False
        try:
            r = requests.get(urljoin(self.url + "/", "api/"),
                             headers=self._headers, timeout=self.timeout)
            return r.status_code == 200
        except Exception:
            return False

    def estados(self, refrescar: bool = False) -> List[Dict[str, Any]]:
        """Lista de todas las entidades con su estado actual. Cacheado 30s."""
        import time
        if (not refrescar and self._cache_entidades is not None
                and time.time() - self._cache_ts < 30):
            return self._cache_entidades
        try:
            r = requests.get(urljoin(self.url + "/", "api/states"),
                             headers=self._headers, timeout=self.timeout)
            r.raise_for_status()
            self._cache_entidades = r.json()
            self._cache_ts = time.time()
            return self._cache_entidades
        except Exception as e:
            logger.debug("HA estados falló: %s", e)
            return []

    def estado(self, entity_id: str) -> Optional[Dict[str, Any]]:
        try:
            r = requests.get(
                urljoin(self.url + "/", f"api/states/{entity_id}"),
                headers=self._headers, timeout=self.timeout,
            )
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            return None

    # ── Resolución de alias en español ─────────────────────────────────
    def buscar_entidad(self, nombre_amigable: str,
                       dominios: Optional[List[str]] = None) -> Optional[str]:
        """Devuelve entity_id que más parezca al nombre amigable.

        Ejemplo: "luz salón" → "light.salon" o "light.lampara_salon".
        `dominios` filtra por tipo: ["light", "switch", "media_player"].
        """
        target = self._normalizar(nombre_amigable)
        if not target:
            return None
        ent = self.estados()
        candidatos: List[Tuple[float, str]] = []
        for e in ent:
            eid: str = e.get("entity_id", "")
            if dominios and eid.split(".")[0] not in dominios:
                continue
            attr = e.get("attributes", {}) or {}
            friendly = self._normalizar(attr.get("friendly_name", ""))
            eid_norm = self._normalizar(eid.split(".", 1)[-1])
            # Puntuación: cuántas palabras del target están en friendly/eid
            palabras_t = set(target.split())
            palabras_f = set(friendly.split()) | set(eid_norm.split())
            if not palabras_t:
                continue
            comunes = palabras_t & palabras_f
            if not comunes:
                continue
            score = len(comunes) / len(palabras_t)
            # Bonus si friendly_name contiene el target completo
            if target in friendly:
                score += 0.5
            candidatos.append((score, eid))
        if not candidatos:
            return None
        candidatos.sort(reverse=True)
        return candidatos[0][1]

    @staticmethod
    def _normalizar(s: str) -> str:
        s = (s or "").lower()
        repl = str.maketrans("áéíóúñü", "aeiounu")
        s = s.translate(repl)
        # Quita stopwords frecuentes en peticiones domóticas
        s = re.sub(r"\b(la|el|los|las|de|del|mi|tu|un|una)\b", " ", s)
        s = re.sub(r"[^a-z0-9\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    # ── Acciones ───────────────────────────────────────────────────────
    def ejecutar_servicio(self, dominio: str, servicio: str,
                          datos: Optional[Dict[str, Any]] = None) -> bool:
        """Llama /api/services/<dominio>/<servicio> con `datos` como JSON.

        Ejemplos:
            ejecutar_servicio("light", "turn_on", {"entity_id": "light.salon"})
            ejecutar_servicio("media_player", "play_media",
                              {"entity_id": "media_player.tv",
                               "media_content_id": "https://...",
                               "media_content_type": "music"})
        """
        try:
            r = requests.post(
                urljoin(self.url + "/", f"api/services/{dominio}/{servicio}"),
                headers=self._headers,
                json=datos or {},
                timeout=self.timeout,
            )
            return r.status_code in (200, 201)
        except Exception as e:
            logger.debug("HA ejecutar_servicio falló: %s", e)
            return False

    # Helpers comunes ----------------------------------------------------
    def encender(self, entity_id: str) -> bool:
        dom = entity_id.split(".")[0]
        return self.ejecutar_servicio(dom, "turn_on", {"entity_id": entity_id})

    def apagar(self, entity_id: str) -> bool:
        dom = entity_id.split(".")[0]
        return self.ejecutar_servicio(dom, "turn_off", {"entity_id": entity_id})

    def alternar(self, entity_id: str) -> bool:
        dom = entity_id.split(".")[0]
        return self.ejecutar_servicio(dom, "toggle", {"entity_id": entity_id})

    def luz(self, entity_id: str, brillo_pct: Optional[int] = None,
            color: Optional[str] = None) -> bool:
        """Enciende luz con brillo (0-100) y/o color ('rojo', '#ff0000', 'warm')."""
        datos: Dict[str, Any] = {"entity_id": entity_id}
        if brillo_pct is not None:
            datos["brightness_pct"] = max(0, min(100, brillo_pct))
        if color:
            colores = {
                "rojo": [255, 0, 0], "verde": [0, 255, 0], "azul": [0, 0, 255],
                "amarillo": [255, 255, 0], "naranja": [255, 165, 0],
                "violeta": [128, 0, 128], "morado": [128, 0, 128],
                "rosa": [255, 192, 203], "blanco": [255, 255, 255],
                "cyan": [0, 255, 255], "magenta": [255, 0, 255],
                "warm": [255, 200, 120], "frio": [200, 220, 255],
            }
            if color.lower() in colores:
                datos["rgb_color"] = colores[color.lower()]
            elif color.startswith("#") and len(color) == 7:
                datos["rgb_color"] = [
                    int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16),
                ]
        return self.ejecutar_servicio("light", "turn_on", datos)

    def reproducir_media(self, entity_id: str, url: str,
                         tipo: str = "music") -> bool:
        return self.ejecutar_servicio(
            "media_player", "play_media",
            {"entity_id": entity_id, "media_content_id": url,
             "media_content_type": tipo},
        )

    def pausar(self, entity_id: str) -> bool:
        return self.ejecutar_servicio(
            "media_player", "media_pause", {"entity_id": entity_id})

    def volumen(self, entity_id: str, nivel_0_1: float) -> bool:
        return self.ejecutar_servicio(
            "media_player", "volume_set",
            {"entity_id": entity_id,
             "volume_level": max(0.0, min(1.0, nivel_0_1))},
        )

    def termostato(self, entity_id: str, temperatura: float) -> bool:
        return self.ejecutar_servicio(
            "climate", "set_temperature",
            {"entity_id": entity_id, "temperature": temperatura},
        )

    # ── Resumen útil para Celestia ─────────────────────────────────────
    def resumen_corto(self) -> str:
        """Devuelve un texto compacto del estado del hogar para usar como
        contexto cuando el usuario pregunta '¿qué pasa en casa?'."""
        ents = self.estados()
        if not ents:
            return "Home Assistant no responde."
        partes: List[str] = []
        encendidas = [e for e in ents
                       if e.get("entity_id", "").startswith("light.")
                       and e.get("state") == "on"]
        if encendidas:
            partes.append(
                "Luces encendidas: " + ", ".join(
                    (e["attributes"].get("friendly_name", e["entity_id"]))
                    for e in encendidas[:10]
                )
            )
        clima = [e for e in ents if e.get("entity_id", "").startswith("climate.")]
        for e in clima[:3]:
            attr = e.get("attributes", {}) or {}
            partes.append(
                f"{attr.get('friendly_name', e['entity_id'])}: "
                f"{e.get('state', '?')} "
                f"({attr.get('current_temperature', '?')}°C → "
                f"{attr.get('temperature', '?')}°C)"
            )
        media = [e for e in ents
                 if e.get("entity_id", "").startswith("media_player.")
                 and e.get("state") not in (None, "off", "unavailable", "idle")]
        for e in media[:3]:
            attr = e.get("attributes", {}) or {}
            partes.append(
                f"{attr.get('friendly_name', e['entity_id'])}: "
                f"{e.get('state', '?')} — "
                f"{attr.get('media_title', '')}"
            )
        if not partes:
            return "Todo tranquilo en casa."
        return " · ".join(partes)
