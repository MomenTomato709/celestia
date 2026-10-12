"""Que Celestia no se duerma cuando tiene algo que hacer.

Con la pantalla apagada Android para la CPU aunque la app siga viva (servicio.py):
nada corre hasta que algo la despierta. Para lo que Enzo espera, se despierta:

- mientras hay encargos en marcha (un proyecto, investigar…), con un cerrojo de
  vigilia («wake lock») hasta que acaban (trabajo.AL_CAMBIAR_EN_MARCHA);
- a la hora del próximo recordatorio, con un despertador de Android y un minuto y
  medio de vigilia para que el hilo de los recordatorios, que mira cada 20 s, lo
  mande (reminders.AL_CAMBIAR_PROXIMO).

El resto del tiempo duerme: nada de gastar batería mirando cada 20 s.

El despertador es de los que suenan aunque el móvil lleve horas quieto
(«AllowWhileIdle»; sin eso, «recuérdamelo a las 8» llegaba cuando Android abría su
siguiente ventana, a veces horas tarde). Suena con un aviso a la propia app que sólo
recoge un receptor registrado mientras la app vive (Despertador, servicio.py): si
Android mata el proceso, a esa hora no se arranca nada a medias.

Un objeto por proceso (`poner`): Android vuelve a ejecutar la app cada vez que se
reabre, pero el proceso, los cerrojos y el despertador son los mismos.
"""
from __future__ import annotations

import threading
from typing import Optional

# Por si un encargo se cuelga: nunca despierta sin fin (se renueva con cada encargo).
TRABAJO_MAX_MS = 30 * 60 * 1000
# El hilo de los recordatorios mira cada 20 s y luego publica el aviso.
AVISO_MS = 90 * 1000

_UNA = {"vigilia": None}
_UNA_LOCK = threading.Lock()


def poner(contexto) -> "Vigilia":
    """La vigilia del proceso, enganchada a Celestia. `contexto`: el de la aplicación."""
    with _UNA_LOCK:
        if _UNA["vigilia"] is None:
            from celestia_lib import reminders, trabajo
            v = Vigilia(contexto)
            trabajo.AL_CAMBIAR_EN_MARCHA = v.en_marcha
            reminders.AL_CAMBIAR_PROXIMO = v.proximo
            _UNA["vigilia"] = v
        return _UNA["vigilia"]


class Vigilia:
    def __init__(self, contexto):
        from android.app import PendingIntent
        from android.content import Context, Intent, IntentFilter
        from android.os import Build, PowerManager
        from .servicio import DESPERTAR, Despertador
        self._lock = threading.Lock()
        self._puesto: Optional[int] = None          # ms del despertador que hay puesto
        energia = contexto.getSystemService(Context.POWER_SERVICE)
        self._trabajo = energia.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "celestia:trabajos")
        self._trabajo.setReferenceCounted(False)
        self._aviso = energia.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "celestia:recordatorio")
        self._aviso.setReferenceCounted(False)
        self._alarmas = contexto.getSystemService(Context.ALARM_SERVICE)
        self._despertador = Despertador(self)        # que no lo recoja el recolector
        filtro = IntentFilter(DESPERTAR)
        if Build.VERSION.SDK_INT >= 33:
            contexto.registerReceiver(self._despertador, filtro, Context.RECEIVER_NOT_EXPORTED)
        else:
            contexto.registerReceiver(self._despertador, filtro)
        sonar = Intent(DESPERTAR)
        sonar.setPackage(contexto.getPackageName())
        self._sonar = PendingIntent.getBroadcast(contexto, 2, sonar, PendingIntent.FLAG_IMMUTABLE
                                                 | PendingIntent.FLAG_UPDATE_CURRENT)

    def en_marcha(self, n: int) -> None:
        """Cuántos encargos hay en marcha (desde sus hilos)."""
        with self._lock:
            if n > 0:
                self._trabajo.acquire(TRABAJO_MAX_MS)    # otra vez: renueva el plazo
            elif self._trabajo.isHeld():
                self._trabajo.release()

    def proximo(self, cuando: Optional[float]) -> None:
        """La hora (time.time()) del próximo recordatorio, o None si no hay."""
        from android.app import AlarmManager
        from android.os import Build
        ms = None if cuando is None else int(cuando * 1000)
        with self._lock:
            if ms == self._puesto:
                return
            self._alarmas.cancel(self._sonar)
            self._puesto = ms
            if ms is None:
                return
            try:
                # Exacta si Android deja (USE_EXACT_ALARM en el manifiesto); si no,
                # con unos minutos de margen, pero también con el móvil en reposo.
                if Build.VERSION.SDK_INT < 31 or self._alarmas.canScheduleExactAlarms():
                    self._alarmas.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, ms,
                                                            self._sonar)
                    return
            except Exception as e:
                print(f"Celestia: sin despertador exacto ({e})")
            self._alarmas.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, ms, self._sonar)

    def al_sonar(self) -> None:
        """Despertador (en el hilo principal): un rato despierta para avisar."""
        with self._lock:
            self._puesto = None
            self._aviso.acquire(AVISO_MS)
