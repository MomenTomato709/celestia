// Manager de conexión con Shizuku.
//
// Responsabilidades:
//   - Probe sincrónico (`verificar`) y asincrónico (`verificarAsync`) sobre `rish -c id`.
//   - Heartbeat periódico que sólo loguea cuando el estado cambia (no spam).
//   - Intento de re-arranque sin intervención del usuario cuando Shizuku está caído:
//       1) Si hay `adb` con dispositivos pareados (Wireless Debug), ejecutar
//          `adb shell sh /sdcard/Android/data/moe.shizuku.privileged.api/start.sh`.
//       2) Como último recurso, abrir la app Shizuku via Termux:API
//          (`termux-open` no requiere privilegios) — el usuario sólo necesita un tap.
//   - Cache TTL para que un cambio de estado en el móvil se descubra como muy tarde
//     en `_TTL_MS` segundos.
//
// Limitación honesta: sin Wireless Debug pareado y sin root, no podemos arrancar
// Shizuku service por completo desde el bridge. En ese caso devolvemos motivo
// `needs_user_tap` y dejamos que la API avise al usuario por WhatsApp.

const { execSync, execFile, exec } = require("child_process");
const fs = require("fs");

const RISH_BIN  = "/data/data/com.termux/files/usr/bin/rish";
const ADB_BIN   = "/data/data/com.termux/files/usr/bin/adb";
const SHIZUKU_PKG = "moe.shizuku.privileged.api";
const SHIZUKU_ACTIVITY = `${SHIZUKU_PKG}/.MainActivity`;
const START_SH_CANDIDATOS = [
    "/sdcard/Android/data/moe.shizuku.privileged.api/start.sh",
    "/storage/emulated/0/Android/data/moe.shizuku.privileged.api/start.sh",
    "/sdcard/Android/data/moe.shizuku.privileged.api/files/start.sh",
];

const _TTL_MS = 60_000;
const HEARTBEAT_MS = 60_000;

let _estado = {
    conectado: null,        // bool | null (null = sin probar todavía)
    motivo: "no_probado",   // "ok" | "service_down" | "rish_missing" | "needs_user_tap" | ...
    ultimoIntento: 0,
    ultimoCambio: 0,
    intentosArranque: 0,
};

function _probeOnce() {
    if (!fs.existsSync(RISH_BIN)) return { ok: false, motivo: "rish_missing" };
    // Estrategia: si execSync no lanza (exit 0), asumimos OK aunque el output
    // venga truncado — bajo carga del móvil `rish -c "id 2>&1"` puede devolver
    // cadena vacía aunque el servicio esté perfectamente. Sólo declaramos KO
    // si execSync lanza o el output contiene un error explícito.
    try {
        const r = execSync(`${RISH_BIN} -c "id 2>&1"`, {
            encoding: "utf8", timeout: 4000,
            stdio: ["ignore", "pipe", "pipe"],
        });
        if (/(not\s+running|service.*not|connect.*failed|denied|no\s+such\s+(file|process)|cannot\s+(execute|connect))/i.test(r)) {
            return { ok: false, motivo: "service_down" };
        }
        return { ok: true, motivo: "ok" };
    } catch (_) {
        return { ok: false, motivo: "service_down" };
    }
}

function _probeSync() {
    // Reintento doble: evita falsos negativos por race con otros comandos
    // ejecutándose contra rish en paralelo. Sólo declaramos caída si dos
    // probes consecutivos fallan.
    const r1 = _probeOnce();
    if (r1.ok) return r1;
    try { execSync("sleep 0.5"); } catch (_) {}
    return _probeOnce();
}

function _adbDevices() {
    if (!fs.existsSync(ADB_BIN)) return [];
    try {
        const out = execSync(`${ADB_BIN} devices`, {
            encoding: "utf8", timeout: 3000,
            stdio: ["ignore", "pipe", "pipe"],
        });
        return out.split("\n")
            .map(l => l.trim())
            .filter(l => /\tdevice$/.test(l))
            .map(l => l.split("\t")[0]);
    } catch (_) { return []; }
}

function _intentarPorAdb() {
    const devs = _adbDevices();
    if (devs.length === 0) return { ok: false, motivo: "no_adb_pareado" };
    for (const start of START_SH_CANDIDATOS) {
        try {
            execSync(`${ADB_BIN} -s ${devs[0]} shell "test -f '${start}' && sh '${start}'"`, {
                encoding: "utf8", timeout: 8000,
                stdio: ["ignore", "pipe", "pipe"],
            });
            return { ok: true, motivo: "adb_start_sh" };
        } catch (_) { /* probar siguiente candidato */ }
    }
    try {
        execSync(`${ADB_BIN} -s ${devs[0]} shell am start -n ${SHIZUKU_ACTIVITY}`, {
            encoding: "utf8", timeout: 5000,
            stdio: ["ignore", "pipe", "pipe"],
        });
        return { ok: true, motivo: "adb_app_abierta" };
    } catch (_) { return { ok: false, motivo: "adb_falla" }; }
}

function _abrirShizukuPorTermux() {
    try {
        execSync(`command -v termux-open >/dev/null 2>&1 && termux-open --send "shizuku://" 2>&1`, {
            timeout: 3000, stdio: ["ignore", "pipe", "pipe"],
        });
        return true;
    } catch (_) { return false; }
}

function intentarArrancar() {
    // Antes de intentar arrancar nada, comprobar si Shizuku YA responde
    // (p.ej. el usuario acaba de activarlo a mano). Evita marcar
    // needs_user_tap cuando rish funciona perfectamente — el bug era que
    // sin ADB pareado siempre caía a needs_user_tap aunque estuviera OK.
    const yaOk = _probeSync();
    if (yaOk.ok) {
        _setEstado(true, "ok");
        return { ok: true, motivo: "ok" };
    }
    _estado.intentosArranque += 1;
    const porAdb = _intentarPorAdb();
    if (porAdb.ok) {
        for (let i = 0; i < 5; i++) {
            const r = _probeSync();
            if (r.ok) {
                _setEstado(true, porAdb.motivo);
                return { ok: true, motivo: porAdb.motivo };
            }
            try { execSync("sleep 1"); } catch (_) {}
        }
    }
    _abrirShizukuPorTermux();
    _setEstado(false, "needs_user_tap");
    return { ok: false, motivo: "needs_user_tap" };
}

function _setEstado(conectado, motivo) {
    const prev = _estado.conectado;
    const cambio = prev !== conectado;
    _estado.conectado = conectado;
    _estado.motivo = motivo;
    _estado.ultimoIntento = Date.now();
    if (cambio) {
        _estado.ultimoCambio = Date.now();
        if (prev !== null) {
            if (conectado) {
                console.log(`  ✓ Shizuku reconectado (${motivo})`);
            } else {
                console.log(`  ✗ Shizuku desconectado (${motivo})`);
            }
        }
    }
}

function verificar(opts = {}) {
    const ahora = Date.now();
    if (!opts.force && _estado.conectado !== null && (ahora - _estado.ultimoIntento) < _TTL_MS) {
        return _estado.conectado;
    }
    const r = _probeSync();
    _setEstado(r.ok, r.motivo);
    return r.ok;
}

function estado() {
    return {
        conectado: _estado.conectado === true,
        motivo: _estado.motivo,
        ultimo_intento_ts: _estado.ultimoIntento,
        ultimo_cambio_ts: _estado.ultimoCambio,
        intentos_arranque: _estado.intentosArranque,
    };
}

let _heartbeatHandle = null;
function iniciarHeartbeat() {
    if (_heartbeatHandle) return;
    _heartbeatHandle = setInterval(() => {
        const prev = _estado.conectado;
        const ahora = verificar({ force: true });
        if (prev === true && ahora === false) {
            intentarArrancar();
        }
    }, HEARTBEAT_MS);
    if (_heartbeatHandle.unref) _heartbeatHandle.unref();
}

function iniciar() {
    const ok = verificar({ force: true });
    if (ok) {
        console.log(`  ✓ Shizuku conectado al arranque (rish OK)`);
    } else {
        console.log(`  ⚠ Shizuku no responde al arranque (${_estado.motivo}) — intentando re-arranque…`);
        const r = intentarArrancar();
        if (r.ok) {
            console.log(`  ✓ Shizuku re-arrancado vía ${r.motivo}`);
        } else if (r.motivo === "needs_user_tap") {
            console.log(`  ⚠ Shizuku necesita un toque del usuario: abrir Shizuku app → Start.`);
        }
    }
    iniciarHeartbeat();
}

module.exports = { iniciar, verificar, estado, intentarArrancar, RISH_BIN };
