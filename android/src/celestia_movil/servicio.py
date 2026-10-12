"""Celestia sigue viva con la app cerrada: un servicio en primer plano.

Enzo (12 oct 2026), que usa la app y no la Celestia de Termux: «empieza por que siga
viva en segundo plano». Sin esto, al salir de la app Android congela el proceso (y
con él el servidor de Celestia, que corre en un hilo de la app): ni recordatorios a
su hora, ni avisos, ni trabajos largos que acaben solos.

Android sólo deja seguir trabajando a una app cerrada si lo dice con una
notificación fija: «Celestia está activa». Tocarla abre el chat; «Apagar» la cierra
del todo (servicio y proceso). Es de tipo «specialUse» (Android 14+): ninguno de los
tipos con nombre («dataSync», que además tiene un tope de 6 h al día en Android 15…)
describe un asistente que espera a que le hablen.

Aquí también está `Despertador`, el que recoge el despertador de los recordatorios
(vigilia.py). No va en el manifiesto: lo registra la app mientras vive.

Son clases de Java hechas en Python (`static_proxy`), como `cromo.py`: Chaquopy las
genera al fabricar la APK porque el módulo está en `build_gradle_extra_content`, y el
manifiesto declara el servicio en `android_manifest_application_extra_content`
(android/pyproject.toml). Nada de lo de aquí puede arrancar en un proceso nuevo: ahí
Python aún no está encendido y la app se cerraría con un error. Por eso
`START_NOT_STICKY` (si Android mata el proceso, no levanta él solo el servicio; lo
levanta Enzo al abrir la app) y el Despertador fuera del manifiesto.
"""
from android.app import Notification, NotificationChannel, NotificationManager, PendingIntent, Service
from android.content import BroadcastReceiver, Context, Intent
from android.content.pm import ServiceInfo
from android.os import Build, IBinder, Process
from java import Override, jclass, jint, jvoid, static_proxy

CANAL = "celestia_activa"
AVISO_ID = 4242
APAGAR = "io.github.momentomato709.celestia_movil.APAGAR"
DESPERTAR = "io.github.momentomato709.celestia_movil.DESPERTAR"
CLASE = "celestia_movil.servicio.ServicioCelestia"


class ServicioCelestia(static_proxy(Service)):

    @Override(jint, [Intent, jint, jint])
    def onStartCommand(self, intent, flags, start_id):
        if intent is not None and intent.getAction() == APAGAR:
            # «Apagar»: fuera la notificación, el chat, el servicio y el proceso entero
            # (con el servidor). La próxima vez que se abra la app, arranca de cero.
            try:
                actividad = jclass("org.beeware.android.MainActivity").singletonThis
                if actividad is not None:
                    actividad.finishAndRemoveTask()
            except Exception as e:
                print(f"Celestia: no pude cerrar el chat ({e})")
            self.stopForeground(Service.STOP_FOREGROUND_REMOVE)
            self.stopSelf()
            Process.killProcess(Process.myPid())
            return Service.START_NOT_STICKY
        aviso = self._aviso()
        if Build.VERSION.SDK_INT >= 34:
            self.startForeground(AVISO_ID, aviso, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        else:
            self.startForeground(AVISO_ID, aviso)
        return Service.START_NOT_STICKY

    @Override(IBinder, [Intent])
    def onBind(self, intent):
        return None

    def _aviso(self):
        gestor = self.getSystemService(Context.NOTIFICATION_SERVICE)
        canal = NotificationChannel(CANAL, "Celestia activa", NotificationManager.IMPORTANCE_LOW)
        canal.setDescription("Celestia sigue encendida para avisarte y contestarte aunque "
                             "cierres la app.")
        canal.setShowBadge(False)
        gestor.createNotificationChannel(canal)
        abrir = self.getPackageManager().getLaunchIntentForPackage(self.getPackageName())
        tocar = PendingIntent.getActivity(self, 0, abrir, PendingIntent.FLAG_IMMUTABLE
                                          | PendingIntent.FLAG_UPDATE_CURRENT)
        apagar = Intent(self, jclass(CLASE))
        apagar.setAction(APAGAR)
        boton = PendingIntent.getService(self, 1, apagar, PendingIntent.FLAG_IMMUTABLE)
        b = Notification.Builder(self, CANAL)
        b.setSmallIcon(self.getApplicationInfo().icon)
        b.setContentTitle("Celestia está activa")
        b.setContentText("Sigue encendida para tus avisos. Toca para abrir el chat.")
        b.setOngoing(True)
        b.setShowWhen(False)
        b.setContentIntent(tocar)
        b.addAction(Notification.Action.Builder(None, "Apagar", boton).build())
        if Build.VERSION.SDK_INT >= 31:
            # Sin esto Android 12+ puede tardar 10 s en enseñarla.
            b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE)
        return b.build()


class Despertador(static_proxy(BroadcastReceiver)):
    """Suena a la hora del próximo recordatorio: despierta a Celestia un rato."""

    def __init__(self, vigilia):
        super().__init__()
        self.vigilia = vigilia                # la Vigilia (Python) del proceso

    @Override(jvoid, [Context, Intent])
    def onReceive(self, contexto, intent):
        self.vigilia.al_sonar()
