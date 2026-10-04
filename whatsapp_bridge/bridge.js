#!/usr/bin/env node
/**
 * Bridge WhatsApp — Celestia v1.5 (Fase 3: visión continua + control UI)
 * Monitor de pantalla en background. Celestia ve la pantalla en todo momento.
 */

const {
    default: makeWASocket,
    useMultiFileAuthState,
    downloadMediaMessage,
    DisconnectReason,
    fetchLatestBaileysVersion,
    Browsers,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const fs   = require("fs");
const path = require("path");
const http = require("http");
const { exec, execFile, execSync } = require("child_process");
const shizuku = require("./shizuku");

// En un PC el puerto de Celestia no siempre es el 8765: se lo dice la API al
// lanzar el puente (canales.py, CELESTIA_API_URL).
const API_URL      = process.env.CELESTIA_API_URL || "http://127.0.0.1:8765";
const BRIDGE_PORT  = 8766;
const AUDIO_DIR    = path.join(process.env.TMPDIR || require("os").tmpdir(), "wa_audio");
const AUTH_DIR     = path.join(__dirname, "auth");

// ── B-19: Filtro anti-flood para errores Signal del protocolo ───────────────
// Baileys imprime "Session error: Bad MAC" y "MessageCounterError" cientos de
// veces cuando hay claves Signal corruptas. Inundan el log y mi contexto.
// Estrategia: agrupar; mostrar 1 cada N ocurrencias del mismo patrón.
(function instalarFiltroLogFlood() {
    const PATRONES_AGRUPABLES = [
        /Session error:.*Bad MAC/,
        /Session error:.*MessageCounterError/,
        /Key used already or never filled/,
    ];
    const contadores = new Map();
    const VENTANA_MS = 30_000;
    const MOSTRAR_CADA = 50;
    function debeMostrar(linea) {
        for (const re of PATRONES_AGRUPABLES) {
            if (re.test(linea)) {
                const clave = re.source;
                const ahora = Date.now();
                const st = contadores.get(clave) || { count: 0, ult: 0, primero: 0 };
                if (ahora - st.ult > VENTANA_MS) {
                    if (st.count > 0) {
                        process.stderr.write(
                            `  ⚠ (resumen) "${clave}" ${st.count} veces en últimos ${Math.round((st.ult - st.primero)/1000)}s\n`
                        );
                    }
                    st.count = 1;
                    st.primero = ahora;
                    st.ult = ahora;
                    contadores.set(clave, st);
                    return true;  // primero de la ventana → mostrar
                }
                st.count++;
                st.ult = ahora;
                contadores.set(clave, st);
                return st.count % MOSTRAR_CADA === 0;
            }
        }
        return true;
    }
    const origErr = console.error.bind(console);
    console.error = (...args) => {
        const linea = args.map(a => typeof a === "string" ? a : (a && a.message) || String(a)).join(" ");
        if (debeMostrar(linea)) origErr(...args);
    };
    const origLog = console.log.bind(console);
    console.log = (...args) => {
        const linea = args.map(a => typeof a === "string" ? a : String(a)).join(" ");
        if (debeMostrar(linea)) origLog(...args);
    };
})();

// ── Token de auth opcional: lee CELESTIA_API_TOKEN del entorno o del .env ──
// Si la API exige token, los fetches deben incluir el header X-Celestia-Token.
function leerToken() {
    if (process.env.CELESTIA_API_TOKEN) return process.env.CELESTIA_API_TOKEN;
    for (const ruta of ["/sdcard/Celestia/.env", path.join(__dirname, "..", ".env")]) {
        try {
            const txt = fs.readFileSync(ruta, "utf8");
            // Gana la ÚLTIMA asignación no vacía, que es con la que se queda el
            // arranque (`set -a; . .env`). Antes era `match` sin /g, o sea la
            // PRIMERA: con la clave escrita dos veces —una vacía y otra con la
            // llave— el puente mandaba la vacía y la API le contestaba 401.
            let ultimo = "";
            for (const m of txt.matchAll(/^CELESTIA_API_TOKEN\s*=\s*(.*)$/gm)) {
                const v = m[1].trim().replace(/^["']|["']$/g, "");
                if (v) ultimo = v;
            }
            if (ultimo) return ultimo;
        } catch (_) { /* siguiente */ }
    }
    return "";
}
const API_TOKEN = leerToken();
function apiHeaders(extra = {}) {
    const h = { ...extra };
    if (API_TOKEN) h["X-Celestia-Token"] = API_TOKEN;
    return h;
}
const CONFIG_FILE  = path.join(__dirname, "numero.txt");
const RECIBIDOS_DIR = "/sdcard/Celestia/recibidos";

/* El nombre de un archivo que llega de fuera no es un nombre: es lo que el
   remitente ha querido escribir. Se queda solo la última parte (sin carpetas),
   sin `..`, sin caracteres raros y con un tope de largo. Si no queda nada
   aprovechable, devuelve "" y el llamante pone uno suyo. */
function nombreSeguro(nombre) {
    const base = path.basename(String(nombre || "").replace(/\\/g, "/"));
    const limpio = base
        .replace(/[\x00-\x1f]/g, "")
        .replace(/[^\w.\- ]+/g, "_")
        .replace(/^\.+/, "")
        .trim()
        .slice(0, 120);
    return (limpio && limpio !== "." && limpio !== "..") ? limpio : "";
}
// Lo que se cruza con el móvil vive en Celestia/.movil (antes, suelto en /sdcard).
try { fs.mkdirSync("/sdcard/Celestia/.movil", { recursive: true }); } catch (_) {}
const CODIGO_FILE  = "/sdcard/Celestia/.movil/codigo_whatsapp.txt";
const ESTADO_FILE  = "/sdcard/Celestia/.movil/estado.txt";
const SCREEN_FILE  = "/sdcard/Celestia/.movil/screen.png";
// Sesión 31 (Fase 1.5): OCR de pantalla y lista de apps instaladas.
// `_BASE` para tesseract porque añade ".txt" automáticamente.
const OCR_OUT_BASE  = "/sdcard/Celestia/.movil/ocr";
const OCR_OUT_FILE  = OCR_OUT_BASE + ".txt";
const APPS_LIST_FILE = "/sdcard/Celestia/.movil/apps.txt";

// Comandos UI embebidos en respuestas de Celestia
// Sesión 31 (Fase 1.5): añadidos OPEN_APP, CLOSE_APP, APP_SWITCH, VOLUME, POWER,
// LOCK, OCR_SCREEN, LIST_APPS, BRIGHTNESS, TOGGLE_WIFI/BT/AIRPLANE. Algunos
// requieren Shizuku/rish para funcionar plenamente — ver runPrivileged() abajo.
// `:` separador ahora es opcional para comandos sin parámetros (APP_SWITCH, etc.)
// Sesión 32: añadidos CALL, SMS, OPEN_URL, LIST_CONTACTS.
const UI_CMD_RE = /\[(TAP|SWIPE|INPUT_TEXT|BACK|HOME|PINCH_ZOOM|ROTATE|KEYEVENT|LONG_PRESS|OPEN_APP|CLOSE_APP|APP_SWITCH|VOLUME|POWER|LOCK|UNLOCK|OCR_SCREEN|LIST_APPS|LIST_CONTACTS|BRIGHTNESS|TOGGLE_WIFI|TOGGLE_BT|TOGGLE_AIRPLANE|TOGGLE_FLASHLIGHT|CALL|SMS|OPEN_URL)(?::([^\]]*))?\]/g;
// Comandos de habilidades inventados por el modelo — se eliminan siempre
const SKILL_CMD_RE = /\[(USA\s+TU\s+HABILIDAD|APRENDE\s+A|EJECUTA\s+HABILIDAD)[^\]]*\]/gi;
// Intent: el usuario pide visión inmediata de la pantalla
const PIDE_VER_PANTALLA_RE = /(?:qu[eé]\s+(?:ves|hay|aparece|sale|muestra)|mira(?:r)?\s+(?:mi\s+)?pantalla|ver?\s+(?:mi\s+)?pantalla|describe(?:me)?\s+(?:mi\s+)?pantalla|qu[eé]\s+(?:está|esta)\s+en\s+(?:la\s+)?pantalla|qu[eé]\s+veo|en\s+mi\s+pantalla)/i;

// ── Estado del monitor de pantalla ────────────────────────────────────────
const pantalla = {
    descripcion:    null,   // última descripción analizada
    tsAnalisis:     0,      // timestamp del último análisis completado
    pendiente:      false,  // true mientras hay un análisis en curso
    tamanoPrevio:   0,      // tamaño base64 del último PNG enviado a analizar
    monitorActivo:  false,  // solo monitorear cuando WhatsApp está conectado
};

fs.mkdirSync(AUDIO_DIR, { recursive: true });
fs.mkdirSync(AUTH_DIR,  { recursive: true });

// ── Estado global para mensajes proactivos ────────────────────────────────
let sockGlobal       = null;   // socket WhatsApp activo
let ultimoSender     = null;   // JID del último usuario que escribió
// Contador de reintentos consecutivos de reconexión — se resetea al lograr "open".
// Sin esto, una caída persistente entra en bucle de reconnect agresivo cada 5s.
let reconnectIntentos = 0;
// B-16: evitar re-anunciar "WhatsApp vinculado" cada vez que Baileys re-emite
// connection==="open" (handshakes intermedios, pre-key replenish, etc.).
// Solo verdadero entre el primer open y el siguiente close.
let vinculadoAnunciado = false;

// ── Servidor HTTP para mensajes proactivos de Celestia ────────────────────
http.createServer(async (req, res) => {
    if (req.method === "GET" && req.url === "/shizuku") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify(shizuku.estado()));
        return;
    }
    if (req.method === "POST" && req.url === "/shizuku/reconectar") {
        const r = shizuku.intentarArrancar();
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ...r, estado: shizuku.estado() }));
        return;
    }
    if (req.method !== "POST" || req.url !== "/enviar") {
        res.writeHead(404); res.end(); return;
    }
    let body = "";
    req.on("data", c => body += c);
    req.on("end", async () => {
        try {
            const { texto, audio_b64, imagen_b64, documento_ruta, documento_mime } = JSON.parse(body);
            const sender = ultimoSender;
            if (!sockGlobal || !sender) {
                res.writeHead(503); res.end(JSON.stringify({ error: "sin conexión activa" })); return;
            }
            if (texto)     await sockGlobal.sendMessage(sender, { text: texto });
            if (audio_b64) {
                const buf = Buffer.from(audio_b64, "base64");
                await sockGlobal.sendMessage(sender, { audio: buf, mimetype: "audio/ogg; codecs=opus", ptt: true });
            }
            if (imagen_b64) {
                const buf = Buffer.from(imagen_b64, "base64");
                await sockGlobal.sendMessage(sender, { image: buf });
            }
            if (documento_ruta && fs.existsSync(documento_ruta)) {
                const buf = fs.readFileSync(documento_ruta);
                await sockGlobal.sendMessage(sender, {
                    document: buf,
                    mimetype: documento_mime || "application/octet-stream",
                    fileName: path.basename(documento_ruta),
                });
            }
            res.writeHead(200); res.end(JSON.stringify({ ok: true }));
        } catch (e) {
            res.writeHead(500); res.end(JSON.stringify({ error: String(e) }));
        }
    });
}).listen(BRIDGE_PORT, "127.0.0.1", () => {
    console.log(`  Celestia puede enviar mensajes proactivos (puerto ${BRIDGE_PORT})`);
    shizuku.iniciar();
});

function leerNumero() {
    try { return fs.readFileSync(CONFIG_FILE, "utf8").trim().replace(/\D/g, ""); }
    catch { return ""; }
}
const TELEFONO = (process.argv[2] || "").replace(/\D/g, "") || leerNumero();

// ── Notificaciones Android ────────────────────────────────────────────────
function notificar(titulo, mensaje, id = "celestia") {
    execFile("termux-notification", [
        "--id", id, "--title", titulo, "--content", mensaje,
        "--priority", "high", "--ongoing",
    ], () => {});
}
function notificarCancelar(id = "celestia") {
    execFile("termux-notification-remove", [id], () => {});
}
function escribirEstado(texto) {
    try { fs.writeFileSync(ESTADO_FILE, texto + "\n"); } catch {}
}

// ── Captura de pantalla ───────────────────────────────────────────────────
async function tomarCaptura() {
    return new Promise((resolve) => {
        exec(`/system/bin/screencap -p ${SCREEN_FILE}`, (err) => {
            if (err) return resolve(null);
            try {
                const buf = fs.readFileSync(SCREEN_FILE);
                if (buf.length < 5000) return resolve(null);
                resolve(buf.toString("base64"));
            } catch { resolve(null); }
        });
    });
}

// ── Análisis de imagen vía Celestia ──────────────────────────────────────
// IMPORTANTE: usar try/finally para garantizar reset de pantalla.pendiente.
// Sin esto, una excepción en res.json() o un return temprano dejaría la flag
// en true para siempre y el monitor nunca volvería a analizar.
async function analizarCaptura(img_b64) {
    try {
        const res = await fetch(API_URL + "/captura", {
            method:  "POST",
            headers: apiHeaders({ "Content-Type": "application/json" }),
            body:    JSON.stringify({ imagen_b64: img_b64 }),
            signal:  AbortSignal.timeout(90000),  // 90s máx (CPU puede ser lento)
        });
        if (res.ok) {
            const data = await res.json();
            if (data.descripcion) {
                pantalla.descripcion = data.descripcion;
                pantalla.tsAnalisis  = Date.now();
            }
        } else {
            console.error(`  ⚠ /captura HTTP ${res.status}`);
        }
    } catch (e) {
        // Loggear: antes se tragaba todo silenciosamente (bug oculto en demos)
        console.error("  ⚠ analizarCaptura falló:", e.message || e);
    } finally {
        pantalla.pendiente = false;
    }
}

// ── Monitor continuo de pantalla ──────────────────────────────────────────
async function iniciarMonitorPantalla() {
    pantalla.monitorActivo = true;
    console.log("  👁  Monitor de pantalla activo\n");

    while (pantalla.monitorActivo) {
        await new Promise(r => setTimeout(r, 5000));

        if (pantalla.pendiente) continue;   // ya hay un análisis en curso

        const img = await tomarCaptura();
        if (!img) continue;

        const tamano = img.length;
        const edadAnalisis = Date.now() - pantalla.tsAnalisis;

        // Analizar si: cambio significativo en pantalla O han pasado >45s sin actualizar
        const cambio = Math.abs(tamano - pantalla.tamanoPrevio) / (pantalla.tamanoPrevio || tamano) > 0.04;
        const caducado = edadAnalisis > 45000;

        if (cambio || caducado) {
            pantalla.tamanoPrevio = tamano;
            pantalla.pendiente    = true;
            analizarCaptura(img); // sin await: background
        }
    }
}

// ── Wrapper de privilegio (Shizuku/rish si está, exec si no) ──────────────
// Sesión 31 (Fase 1.5): preparado para cuando el usuario active Shizuku y
// `rish` esté disponible en PATH. Si no, hace fallback a exec directo —
// muchos comandos (input, screencap, am start, settings put system) ya
// funcionan así. Los que necesitan privilegios reales (force-stop, toggle
// wifi/bt, statusbar) fallarán hasta que Shizuku esté activo.
// Sesión 31: Shizuku autorizó Termux. Ruta absoluta a `rish` para evitar
// problemas de PATH cuando el bridge se lanza fuera del shell normal.
const RISH_BIN = shizuku.RISH_BIN;
const tieneShizuku = shizuku.verificar;
function runPrivileged(cmd, callback) {
    if (tieneShizuku()) {
        // rish ejecuta `sh -c "comando"` con privilegios shell (uid=2000).
        // -c es la forma directa de pasar el comando.
        execFile(RISH_BIN, ["-c", cmd], callback || (() => {}));
    } else {
        exec(cmd, callback || (() => {}));
    }
}

// Apps comunes — nombres legibles → paquete Android. El LLM debería conocer
// los nombres exactos del paquete vía system prompt, pero damos atajos
// frecuentes por si emite el nombre amigable.
const APP_ALIAS = {
    "whatsapp": "com.whatsapp",
    "instagram": "com.instagram.android",
    "telegram": "org.telegram.messenger",
    "spotify": "com.spotify.music",
    "youtube": "com.google.android.youtube",
    "chrome": "com.android.chrome",
    "gmail": "com.google.android.gm",
    "maps": "com.google.android.apps.maps",
    "google maps": "com.google.android.apps.maps",
    "camera": "com.android.camera",
    "camara": "com.android.camera",
    "ajustes": "com.android.settings",
    "settings": "com.android.settings",
    "play store": "com.android.vending",
    "tiktok": "com.zhiliaoapp.musically",
    "twitter": "com.twitter.android",
    "x": "com.twitter.android",
    "facebook": "com.facebook.katana",
    "calendar": "com.google.android.calendar",
    "calendario": "com.google.android.calendar",
    "reloj": "com.google.android.deskclock",
    "clock": "com.google.android.deskclock",
    "calculadora": "com.google.android.calculator",
    "calculator": "com.google.android.calculator",
    "fotos": "com.google.android.apps.photos",
    "photos": "com.google.android.apps.photos",
    "drive": "com.google.android.apps.docs",
    "files": "com.google.android.documentsui",
    "archivos": "com.google.android.documentsui",
};
function resolveApp(s) {
    if (!s) return null;
    const t = s.trim().toLowerCase();
    // Si ya parece package (tiene punto), úsalo tal cual
    if (/^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/i.test(t)) return s.trim();
    return APP_ALIAS[t] || null;
}

// ── Control de UI Android ─────────────────────────────────────────────────
function ejecutarUI(textoRespuesta) {
    const matches = [...textoRespuesta.matchAll(UI_CMD_RE)];
    for (const m of matches) {
        const tipo   = m[1];
        const params = (m[2] || "").trim();
        console.log(`  → [UI:${tipo}] ${params}`);
        // Helpers de validación contra inyección de shell. Los comandos van con shell
        // interpretado (exec()), así que cualquier param que no sea numérico puro
        // o que contenga caracteres especiales debe rechazarse o escaparse.
        const isInt = (s) => /^-?\d+$/.test(String(s).trim());
        const clampCoord = (s) => {
            if (!isInt(s)) return null;
            const n = parseInt(s, 10);
            // Pantallas reales no tienen >10000 px; rechazamos rangos absurdos
            if (n < -10 || n > 10000) return null;
            return String(n);
        };

        switch (tipo) {
            case "TAP": {
                const [xs, ys] = params.split(",").map(s => s.trim());
                const x = clampCoord(xs), y = clampCoord(ys);
                if (x === null || y === null) {
                    console.error(`  ✗ TAP coords inválidas: ${params}`); break;
                }
                exec(`/system/bin/input tap ${x} ${y}`);
                break;
            }
            case "SWIPE": {
                const p = params.split(",").map(s => s.trim());
                const a = clampCoord(p[0]), b = clampCoord(p[1]);
                const c = clampCoord(p[2]), d = clampCoord(p[3]);
                const ms = isInt(p[4]) ? Math.max(50, Math.min(5000, parseInt(p[4], 10))) : 300;
                if ([a, b, c, d].some(v => v === null)) {
                    console.error(`  ✗ SWIPE coords inválidas: ${params}`); break;
                }
                exec(`/system/bin/input swipe ${a} ${b} ${c} ${d} ${ms}`);
                break;
            }
            case "INPUT_TEXT": {
                // Pasar el texto como argv, no construir shell string. Si tuviese
                // caracteres como ';' o '`' antes podrían escapar el quoting.
                execFile("/system/bin/input", ["text", params], () => {});
                break;
            }
            case "BACK":
                exec("/system/bin/input keyevent KEYCODE_BACK");
                break;
            case "HOME":
                exec("/system/bin/input keyevent KEYCODE_HOME");
                break;
            case "LONG_PRESS": {
                // Implementado como swipe en el mismo punto con duración 800ms
                const [xs, ys] = params.split(",").map(s => s.trim());
                const x = clampCoord(xs), y = clampCoord(ys);
                if (x === null || y === null) {
                    console.error(`  ✗ LONG_PRESS coords inválidas: ${params}`); break;
                }
                exec(`/system/bin/input swipe ${x} ${y} ${x} ${y} 800`);
                break;
            }
            case "PINCH_ZOOM": {
                // [PINCH_ZOOM:x,y,scale] — scale<1 zoom out, scale>1 zoom in
                // Implementado como dos swipes simultáneos divergentes/convergentes
                // desde el punto central. scale=2 → swipe 200px desde el centro.
                const [cx, cy, scaleRaw] = params.split(",").map(s => s.trim());
                const scale = parseFloat(scaleRaw) || 1.5;
                const dist = Math.round(150 * Math.abs(scale - 1) + 50);
                const sign = scale >= 1 ? 1 : -1;
                const x1a = parseInt(cx) - sign * dist;
                const x2a = parseInt(cx) - (scale >= 1 ? 1 : -1) * 50;
                const x1b = parseInt(cx) + sign * dist;
                const x2b = parseInt(cx) + (scale >= 1 ? 1 : -1) * 50;
                // Lanzamos ambos en paralelo (Android maneja multitouch via 2 swipes en serie no funciona,
                // pero approximamos con un solo swipe diagonal — suficiente para muchos casos)
                exec(`/system/bin/input swipe ${x1a} ${cy} ${x2a} ${cy} 400`);
                exec(`/system/bin/input swipe ${x1b} ${cy} ${x2b} ${cy} 400`);
                break;
            }
            case "ROTATE": {
                // [ROTATE:0|90|180|270] o [ROTATE:landscape|portrait]
                const dir = (params || "0").toLowerCase();
                const mapping = {"0": 0, "90": 1, "180": 2, "270": 3,
                                  "portrait": 0, "landscape": 1};
                const val = mapping[dir];
                if (val !== undefined) {
                    // Desactiva auto-rotation y fija orientación
                    exec(`settings put system accelerometer_rotation 0`);
                    exec(`settings put system user_rotation ${val}`);
                }
                break;
            }
            case "KEYEVENT": {
                // [KEYEVENT:KEYCODE_VOLUME_UP] etc. — keycode arbitrario
                const code = params.trim().toUpperCase();
                if (/^[A-Z0-9_]+$/.test(code)) {
                    exec(`/system/bin/input keyevent ${code}`);
                }
                break;
            }
            // ── Sesión 31 Fase 1.5: comandos nuevos ──────────────────────
            case "OPEN_APP": {
                // [OPEN_APP:com.whatsapp] o [OPEN_APP:whatsapp]
                const pkg = resolveApp(params);
                if (!pkg || !/^[a-z][a-z0-9_.]+$/i.test(pkg)) {
                    console.error(`  ✗ OPEN_APP paquete inválido: ${params}`); break;
                }
                // Sesión 32 (BUG-S129): usar runPrivileged (Shizuku/rish) para
                // que el lanzamiento funcione realmente. Antes `exec()` directo
                // desde Termux no tenía permisos y la app jamás se abría.
                // `am start -W` espera a que la activity arranque; con LAUNCHER
                // intent funciona para cualquier app sin saber la activity.
                // Sesión 32: forzar --user 0 (perfil principal). Sin esto,
                // `cmd package resolve-activity` puede no encontrar apps que
                // estén solo en el usuario principal cuando hay multi-perfil
                // (Private space, DualApps) en el dispositivo.
                runPrivileged(
                    `am start -W --user 0 -a android.intent.action.MAIN ` +
                    `-c android.intent.category.LAUNCHER ` +
                    `$(cmd package resolve-activity --user 0 --brief ${pkg} | tail -n1)`
                );
                break;
            }
            case "CLOSE_APP": {
                // [CLOSE_APP:com.whatsapp] — requiere Shizuku para force-stop real.
                // Sin Shizuku, hacemos el "best effort": HOME + abrir recents +
                // swipe up en la primera card. No es perfecto pero a veces sirve.
                const pkg = resolveApp(params);
                if (!pkg) { console.error(`  ✗ CLOSE_APP paquete inválido: ${params}`); break; }
                if (tieneShizuku()) {
                    runPrivileged(`am force-stop '${pkg}'`);
                } else {
                    // Fallback sin Shizuku: HOME para "minimizar". Avisamos en log.
                    exec("/system/bin/input keyevent KEYCODE_HOME");
                    console.log(`  ⚠ CLOSE_APP ${pkg}: sin Shizuku sólo minimizo (HOME).`);
                }
                break;
            }
            case "APP_SWITCH": {
                // Abre el "recientes" (multitarea)
                exec("/system/bin/input keyevent KEYCODE_APP_SWITCH");
                break;
            }
            case "VOLUME": {
                // [VOLUME:up], [VOLUME:down], [VOLUME:mute], [VOLUME:N] (0-15)
                const v = params.toLowerCase();
                if (v === "up") exec("/system/bin/input keyevent KEYCODE_VOLUME_UP");
                else if (v === "down") exec("/system/bin/input keyevent KEYCODE_VOLUME_DOWN");
                else if (v === "mute") exec("/system/bin/input keyevent KEYCODE_VOLUME_MUTE");
                else if (/^\d{1,2}$/.test(v)) {
                    const n = Math.min(15, Math.max(0, parseInt(v, 10)));
                    runPrivileged(`cmd media_session volume --set ${n} --stream 3`);
                }
                break;
            }
            case "POWER":
            case "LOCK": {
                // Botón de encendido — apaga o enciende pantalla (toggle)
                exec("/system/bin/input keyevent KEYCODE_POWER");
                break;
            }
            case "UNLOCK": {
                // Despertar pantalla con WAKEUP (no desbloquea PIN/huella; sólo prende)
                exec("/system/bin/input keyevent KEYCODE_WAKEUP");
                break;
            }
            case "BRIGHTNESS": {
                // [BRIGHTNESS:N] N=0..255. Necesita WRITE_SETTINGS — algunas roms
                // lo permiten al usuario, otras requieren Shizuku.
                if (/^\d{1,3}$/.test(params)) {
                    const n = Math.min(255, Math.max(0, parseInt(params, 10)));
                    runPrivileged(`settings put system screen_brightness ${n}`);
                }
                break;
            }
            case "TOGGLE_WIFI": {
                // on / off — siempre requiere Shizuku
                if (!tieneShizuku()) {
                    console.error("  ⚠ TOGGLE_WIFI requiere Shizuku activo");
                    break;
                }
                const v = params.toLowerCase();
                if (v === "on" || v === "off") {
                    runPrivileged(`svc wifi ${v === "on" ? "enable" : "disable"}`);
                }
                break;
            }
            case "TOGGLE_BT": {
                if (!tieneShizuku()) {
                    console.error("  ⚠ TOGGLE_BT requiere Shizuku activo");
                    break;
                }
                const v = params.toLowerCase();
                if (v === "on" || v === "off") {
                    runPrivileged(`svc bluetooth ${v === "on" ? "enable" : "disable"}`);
                }
                break;
            }
            case "TOGGLE_AIRPLANE": {
                if (!tieneShizuku()) {
                    console.error("  ⚠ TOGGLE_AIRPLANE requiere Shizuku activo");
                    break;
                }
                const v = params.toLowerCase();
                if (v === "on" || v === "off") {
                    const flag = v === "on" ? 1 : 0;
                    runPrivileged(`settings put global airplane_mode_on ${flag}`,
                        () => runPrivileged(
                            `am broadcast -a android.intent.action.AIRPLANE_MODE --ez state ${flag === 1}`));
                }
                break;
            }
            case "TOGGLE_FLASHLIGHT": {
                // [TOGGLE_FLASHLIGHT:on|off]. Requiere Termux:API.
                const v = params.toLowerCase();
                if (v === "on") exec("termux-torch on");
                else if (v === "off") exec("termux-torch off");
                break;
            }
            case "OCR_SCREEN": {
                // Captura + tesseract si está. Devuelve nada al usuario aquí —
                // el LLM debe pedir luego "qué dice la pantalla". El monitor
                // de pantalla ya provee un análisis con visión. Esto es OCR
                // puro para extraer texto preciso (números, URLs, etc.).
                //
                // Sesión 56: esto llevaba tiempo sin hacer nada. `tesseract` no
                // estaba instalado en Termux, así que el `command -v` fallaba en
                // silencio... mientras al usuario se le decía «Leyendo pantalla».
                // Ahora, si no está, se dice — que es la misma regla de honestidad
                // que ya rige para «estoy cerrando la app».
                exec(`/system/bin/screencap -p ${SCREEN_FILE}`, (err) => {
                    if (err) {
                        console.error("  ✗ OCR_SCREEN: no pude capturar la pantalla");
                        return;
                    }
                    exec("command -v tesseract", (falta) => {
                        if (falta) {
                            console.error("  ✗ OCR_SCREEN: tesseract no está instalado " +
                                          "en Termux (pkg install tesseract) — no he leído nada");
                            return;
                        }
                        exec(`tesseract ${SCREEN_FILE} ${OCR_OUT_BASE} -l spa+eng 2>/dev/null`,
                            () => { /* texto queda en /sdcard/Celestia/.movil/ocr.txt */ });
                    });
                });
                break;
            }
            case "LIST_APPS": {
                // [LIST_APPS] — escribe la lista a fichero para que Celestia
                // la lea cuando se le pregunte. Sin Shizuku sólo lista apps
                // visibles al usuario actual.
                exec(`pm list packages -3 2>/dev/null | sed 's/^package://' > ${APPS_LIST_FILE}`);
                break;
            }
            // Sesión 32 (BUG-S132): llamar a un número de teléfono.
            // Requiere permiso CALL_PHONE; con Shizuku se otorga automáticamente.
            // El número debe venir en formato internacional o local sin espacios.
            case "CALL": {
                const tel = (params || "").replace(/[^\d+]/g, "");
                if (!tel) { console.error("  ✗ CALL número vacío"); break; }
                runPrivileged(
                    `am start --user 0 -a android.intent.action.CALL ` +
                    `-d 'tel:${tel}'`
                );
                break;
            }
            // Sesión 32 (BUG-S132): abrir compositor de SMS con número y
            // cuerpo pre-cargados. Formato: [SMS:NUMERO|texto del mensaje]
            // El usuario debe pulsar enviar (no se auto-envía por seguridad).
            case "SMS": {
                const sep = (params || "").indexOf("|");
                if (sep < 0) { console.error("  ✗ SMS formato inválido"); break; }
                const tel  = params.slice(0, sep).replace(/[^\d+]/g, "");
                const body = params.slice(sep + 1)
                    .replace(/'/g, "'\\''")
                    .replace(/\n/g, " ");
                if (!tel) { console.error("  ✗ SMS número vacío"); break; }
                runPrivileged(
                    `am start --user 0 -a android.intent.action.SENDTO ` +
                    `-d 'sms:${tel}' --es sms_body '${body}'`
                );
                break;
            }
            // Sesión 32 (BUG-S132): abrir una URL en el navegador por defecto.
            case "OPEN_URL": {
                let url = (params || "").trim();
                if (!/^https?:\/\//i.test(url)) {
                    url = "https://" + url;
                }
                // Sanitizar para shell — solo permitir caracteres URL válidos.
                if (!/^https?:\/\/[\w\-._~:/?#\[\]@!$&'()*+,;=%]+$/i.test(url)) {
                    console.error(`  ✗ OPEN_URL URL inválida: ${url}`); break;
                }
                runPrivileged(
                    `am start --user 0 -a android.intent.action.VIEW -d '${url}'`
                );
                break;
            }
            // Sesión 32 (BUG-S132): exportar contactos a fichero para que
            // Celestia los lea al resolver «llama a Juan».
            case "LIST_CONTACTS": {
                runPrivileged(
                    `content query --user 0 ` +
                    `--uri content://com.android.contacts/data ` +
                    `--projection display_name:data1 ` +
                    `--where "mimetype='vnd.android.cursor.item/phone_v2'" ` +
                    `> /sdcard/Celestia/.movil/contactos.txt 2>/dev/null`
                );
                break;
            }
        }
    }
}
function limpiarComandosUI(texto) {
    // Eliminar comandos de habilidades inventados por el modelo
    let limpio = texto.replace(SKILL_CMD_RE, "");
    // Eliminar comandos UI (se ejecutan por separado)
    limpio = limpio.replace(UI_CMD_RE, "").replace(/\s{2,}/g, " ").trim();
    if (limpio) return limpio;
    // Si solo había comandos UI, rescatar el texto de INPUT_TEXT como fallback
    const inputTexts = [...texto.matchAll(/\[INPUT_TEXT:([^\]]+)\]/g)].map(m => m[1].trim());
    const rescate = inputTexts.join(" ").trim();
    if (rescate) return rescate;
    // Sesión 32 (BUG-S145/S147): si tras limpiar tokens UI no queda nada y
    // tampoco hay INPUT_TEXT, generar feedback breve según el primer comando
    // detectado para que el usuario no reciba una burbuja vacía.
    const cmdMatch = texto.match(/\[([A-Z_]+)(?::([^\]]*))?\]/);
    if (cmdMatch) {
        const cmd = cmdMatch[1].toUpperCase();
        const param = (cmdMatch[2] || "").trim();
        const TXT = {
            "OPEN_APP":          `Abriendo ${param || "app"}.`,
            "CLOSE_APP":         `Cerrando ${param || "app"}.`,
            "APP_SWITCH":        "Cambiando de app.",
            "OPEN_URL":          `Abriendo enlace.`,
            "CALL":              `Llamando a ${param || "contacto"}.`,
            "SMS":               "Abriendo SMS.",
            "VOLUME":            param === "mute" ? "Silenciado." :
                                 param === "up"   ? "Volumen subido." :
                                 param === "down" ? "Volumen bajado." :
                                 `Volumen a ${param}.`,
            "BRIGHTNESS":        `Brillo cambiado.`,
            "TOGGLE_WIFI":       param === "on" ? "WiFi activado." :
                                 param === "off"? "WiFi apagado."   : "WiFi alternado.",
            "TOGGLE_BT":         param === "on" ? "Bluetooth activado." :
                                 param === "off"? "Bluetooth apagado."   : "Bluetooth alternado.",
            "TOGGLE_FLASHLIGHT": param === "on" ? "Linterna encendida." :
                                 param === "off"? "Linterna apagada."   : "Linterna alternada.",
            "TOGGLE_AIRPLANE":   "Modo avión alternado.",
            "LOCK":              "Pantalla bloqueada.",
            "UNLOCK":            "Desbloqueando.",
            "POWER":             "Pulsando power.",
            "HOME":              "Yendo a inicio.",
            "BACK":              "Atrás.",
            "KEYEVENT":          `Tecla ${param}.`,
            "OCR_SCREEN":        "Leyendo pantalla.",
            "LIST_APPS":         "Listando apps.",
            "LIST_CONTACTS":     "Listando contactos.",
        };
        return TXT[cmd] || "✓";
    }
    return "✓";
}

// ── API de Celestia ───────────────────────────────────────────────────────
// B-17: esperar a que la API responda /estado antes de fetch'es críticos.
// Evita "fetch failed" cuando celestia.py se está reiniciando.
async function esperarAPI(timeoutMs = 30_000) {
    const deadline = Date.now() + timeoutMs;
    let espera = 500;
    while (Date.now() < deadline) {
        if (await verificarAPI()) return true;
        await new Promise(r => setTimeout(r, espera));
        espera = Math.min(espera * 1.5, 4000);
    }
    return false;
}

async function llamarAPI(endpoint, cuerpo) {
    // Reintento corto con esperarAPI si el primer fetch falla por ECONNREFUSED.
    let ultimaErr = null;
    for (let intento = 0; intento < 2; intento++) {
        try {
            const res = await fetch(API_URL + endpoint, {
                method:  "POST",
                headers: apiHeaders({ "Content-Type": "application/json" }),
                body:    JSON.stringify(cuerpo),
                signal:  AbortSignal.timeout(120000),
            });
            if (!res.ok) throw new Error(`API ${endpoint} → HTTP ${res.status}`);
            return res.json();
        } catch (e) {
            ultimaErr = e;
            const msg = (e && e.message) || String(e);
            const transitorio = /fetch failed|ECONNREFUSED|ENOTFOUND|ETIMEDOUT|EAI_AGAIN/i.test(msg);
            if (!transitorio || intento >= 1) break;
            console.error(`  ⚠ API ${endpoint} falló (${msg}) — esperando que vuelva...`);
            const ok = await esperarAPI(15_000);
            if (!ok) break;
        }
    }
    throw ultimaErr;
}

async function verificarAPI() {
    try {
        const res = await fetch(API_URL + "/estado", { signal: AbortSignal.timeout(3000) });
        return res.ok;
    } catch { return false; }
}

// ── Mensajes entrantes ────────────────────────────────────────────────────
async function procesarMensaje(sock, msg) {
    if (msg.key.fromMe || !msg.message) return;
    const sender = msg.key.remoteJid;
    sockGlobal   = sock;
    ultimoSender = sender;
    const tipo   = Object.keys(msg.message)[0];
    let resultado = null;

    if (tipo === "conversation" || tipo === "extendedTextMessage") {
        const texto = msg.message.conversation ?? msg.message.extendedTextMessage?.text ?? "";
        if (!texto.trim()) return;
        console.log(`  ← [texto] ${texto.slice(0, 80)}`);

        // `canal` marca por dónde entra: lo que Celestia tarde en preparar
        // (un PDF) vuelve por aquí y no por otro chat que esté abierto.
        // `remitente` es quién escribe: el servidor decide si le atiende. Sin
        // esto, cualquiera que le escribiera a este número hablaba con la
        // Celestia de su dueño, con su memoria y sus herramientas.
        const cuerpo = { texto, canal: "whatsapp", remitente: sender };

        // Si pide ver la pantalla, forzar captura fresca AHORA (sin esperar al ciclo de 5s)
        if (PIDE_VER_PANTALLA_RE.test(texto)) {
            console.log("  👁  Captura forzada (usuario pide ver pantalla)");
            const img = await tomarCaptura();
            if (img) {
                cuerpo.imagen_b64 = img;  // /mensaje analizará y describirá
            }
        }

        // Incluir siempre el contexto visual actual si está disponible y es reciente
        // (excepto si ya estamos enviando imagen_b64 fresca)
        if (!cuerpo.imagen_b64) {
            const edadMs = Date.now() - pantalla.tsAnalisis;
            if (pantalla.descripcion && edadMs < 120000) {
                cuerpo.contexto_pantalla = pantalla.descripcion;
                cuerpo.seg_pantalla = Math.round(edadMs / 1000);
            }
        }

        resultado = await llamarAPI("/mensaje", cuerpo);

    } else if (tipo === "audioMessage") {
        console.log("  ← [audio] transcribiendo...");
        const buffer    = await downloadMediaMessage(msg, "buffer", {});
        const rutaAudio = path.join(AUDIO_DIR, `${Date.now()}.ogg`);
        fs.writeFileSync(rutaAudio, buffer);

        const cuerpo = { ruta: rutaAudio, canal: "whatsapp", remitente: sender };
        const edadMs = Date.now() - pantalla.tsAnalisis;
        if (pantalla.descripcion && edadMs < 120000) {
            cuerpo.contexto_pantalla = pantalla.descripcion;
        }

        resultado = await llamarAPI("/audio", cuerpo);
        if (resultado?.transcripcion)
            console.log(`  ← [transcripción] ${resultado.transcripcion.slice(0, 80)}`);

    } else if (tipo === "imageMessage") {
        const caption = msg.message.imageMessage?.caption || "¿Qué ves en esta imagen?";
        console.log(`  ← [imagen] ${caption.slice(0, 60)}`);
        const buffer  = await downloadMediaMessage(msg, "buffer", {});
        const b64     = buffer.toString("base64");
        resultado = await llamarAPI("/mensaje", {
            canal: "whatsapp",
            remitente: sender,
            texto: caption,
            imagen_b64: b64,
            es_foto_usuario: true,
        });

    } else if (tipo === "documentMessage") {
        const doc      = msg.message.documentMessage;
        // El nombre lo elige QUIEN ENVÍA. Tal cual venía, un `fileName` como
        // `../../../.bashrc` se salía de la carpeta —`path.join` resuelve los
        // `..`— y encima `mkdirSync` creaba el camino: escribir donde quisiera
        // con solo mandar un archivo. Se queda el nombre a secas y limpio.
        const nombre   = nombreSeguro(doc?.fileName) || `doc_${Date.now()}`;
        const caption  = doc?.caption || `Recibí el archivo ${nombre}`;
        console.log(`  ← [documento] ${nombre}`);
        const buffer   = await downloadMediaMessage(msg, "buffer", {});
        const rutaDoc  = path.join(RECIBIDOS_DIR, nombre);
        fs.mkdirSync(RECIBIDOS_DIR, { recursive: true });
        fs.writeFileSync(rutaDoc, buffer);
        resultado = await llamarAPI("/mensaje", {
            canal: "whatsapp",
            remitente: sender,
            texto: `${caption}\n[ARCHIVO RECIBIDO: ${rutaDoc} — ${buffer.length} bytes]`,
        });

    } else { return; }

    if (!resultado) return;
    if (resultado.texto) {
        ejecutarUI(resultado.texto);
        const textoUsuario = limpiarComandosUI(resultado.texto);
        if (textoUsuario) {
            console.log(`  → [texto] ${textoUsuario.slice(0, 80)}`);
            await sock.sendMessage(sender, { text: textoUsuario });
        }
    }
    if (resultado.audio_b64) {
        console.log("  → [audio]");
        const buf = Buffer.from(resultado.audio_b64, "base64");
        await sock.sendMessage(sender, { audio: buf, mimetype: "audio/ogg; codecs=opus", ptt: true });
    }
    if (resultado.imagen_b64) {
        console.log("  → [imagen]");
        const buf = Buffer.from(resultado.imagen_b64, "base64");
        await sock.sendMessage(sender, { image: buf });
    }
    if (resultado.documento_ruta && fs.existsSync(resultado.documento_ruta)) {
        const nombre = path.basename(resultado.documento_ruta);
        console.log(`  → [documento] ${nombre}`);
        const buf = fs.readFileSync(resultado.documento_ruta);
        await sock.sendMessage(sender, {
            document: buf,
            mimetype: resultado.documento_mime || "application/octet-stream",
            fileName: nombre,
        });
    }
}

// ── Conexión Baileys ──────────────────────────────────────────────────────
async function iniciar(telefono) {
    const { version }          = await fetchLatestBaileysVersion();
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

    const sock = makeWASocket({
        version,
        auth:              state,
        logger:            pino({ level: "silent" }),
        printQRInTerminal: false,
        browser:           Browsers.macOS("Chrome"),
    });

    sock.ev.on("creds.update", saveCreds);

    let codigoPedido = false;

    sock.ev.on("connection.update", async ({ connection, lastDisconnect, qr }) => {

        if (qr && !codigoPedido) {
            codigoPedido = true;
            try {
                await new Promise(r => setTimeout(r, 1500));
                const codigo = await sock.requestPairingCode(telefono);
                const expiraTs = Date.now() + 60_000;
                const expiraStr = new Date(expiraTs).toLocaleTimeString("es");
                console.log("\n");
                console.log("  ╔══════════════════════════════════════╗");
                console.log(`  ║  CÓDIGO: ${codigo.padEnd(29)}║`);
                console.log(`  ║  Válido hasta: ${expiraStr.padEnd(23)}║`);
                console.log("  ╚══════════════════════════════════════╝\n");
                const instrucciones =
                    `╔══════════════════════════════════════╗\n` +
                    `║  CÓDIGO CELESTIA: ${codigo.padEnd(20)}║\n` +
                    `║  Válido hasta: ${expiraStr.padEnd(23)}║\n` +
                    `╚══════════════════════════════════════╝\n\n` +
                    `En WhatsApp → ⋮ → Dispositivos vinculados\n` +
                    `→ Vincular dispositivo → "Vincular con número de teléfono"\n` +
                    `→ Ingresá: ${codigo}\n\nTenés ~60 segundos (hasta ${expiraStr}).\n`;
                try { fs.writeFileSync(CODIGO_FILE, instrucciones); } catch {}
                escribirEstado(`⏳ Esperando vinculación — código: ${codigo} (hasta ${expiraStr})`);
                notificar("Celestia — Vincular WhatsApp",
                            `${codigo} · expira ${expiraStr}`, "celestia-pair");

                // Countdown en consola cada 15s: feedback visible mientras el usuario lee el código.
                const countdownTimer = setInterval(() => {
                    const restante = Math.max(0, Math.round((expiraTs - Date.now()) / 1000));
                    if (restante <= 0) {
                        clearInterval(countdownTimer);
                        console.log("  ⏰ Código de vinculación expirado — pedí uno nuevo si hace falta.");
                        return;
                    }
                    console.log(`  ⏳ Código ${codigo} — quedan ${restante}s`);
                }, 15000);
                // Reset al conectarse
                sock.ev.on("connection.update", ({ connection }) => {
                    if (connection === "open") clearInterval(countdownTimer);
                });
            } catch (e) {
                console.error("  ✗ Error al pedir código:", e.message);
            }
        }

        if (connection === "open") {
            if (!vinculadoAnunciado) {
                console.log("  ✓ ¡WhatsApp vinculado! Celestia está lista.\n");
                try { fs.unlinkSync(CODIGO_FILE); } catch {}
                escribirEstado(`✓ Celestia activa — ${new Date().toLocaleString("es")}`);
                notificar("Celestia activa", "WhatsApp vinculado y listo", "celestia");
                notificarCancelar("celestia-pair");
                vinculadoAnunciado = true;
            }
            // Reset del contador de reintentos al lograr una conexión estable
            reconnectIntentos = 0;
            // Iniciar monitor de pantalla al conectarse
            if (!pantalla.monitorActivo) iniciarMonitorPantalla();
        }

        if (connection === "close") {
            pantalla.monitorActivo = false;   // pausar monitor
            vinculadoAnunciado = false;       // permitir re-anunciar tras reconectar
            const statusCode = lastDisconnect?.error?.output?.statusCode;
            const razon      = lastDisconnect?.error?.message || "";

            // Backoff exponencial: 5s, 10s, 20s, 40s, 80s, 160s, 300s (cap).
            // Tras 5 fallos consecutivos avisamos al usuario porque algo va mal.
            reconnectIntentos++;
            const delay = Math.min(5000 * Math.pow(2, Math.max(reconnectIntentos - 1, 0)), 300000);
            if (reconnectIntentos === 5) {
                notificar("Celestia", `Llevo ${reconnectIntentos} intentos fallidos de reconectar — revisá tu conexión.`, "celestia-warn");
            }

            if (statusCode === DisconnectReason.loggedOut) {
                console.log(`\n  WhatsApp cerró la sesión — limpiando y reconectando en ${delay/1000}s (intento ${reconnectIntentos})...\n`);
                escribirEstado("↻ Sesión cerrada — reconectando...");
                notificar("Celestia", "Sesión cerrada — reconectando...", "celestia");
                try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch {}
                fs.mkdirSync(AUTH_DIR, { recursive: true });
                setTimeout(() => iniciar(telefono), delay);
            } else {
                escribirEstado(`↻ Reconectando (intento ${reconnectIntentos}, en ${delay/1000}s)...`);
                setTimeout(() => iniciar(telefono), delay);
            }
        }
    });

    sock.ev.on("messages.upsert", async ({ messages, type }) => {
        if (type !== "notify") return;
        for (const msg of messages) {
            try { await procesarMensaje(sock, msg); }
            catch (err) {
                console.error(`  ✗ Error: ${err.message}`);
                escribirEstado(`✗ Error: ${err.message}`);
            }
        }
    });
}

// ── Arranque ──────────────────────────────────────────────────────────────
async function main() {
    console.log("\n  ═══════════════════════════════════════");
    console.log("    Bridge WhatsApp — Celestia v1.5");
    console.log("  ═══════════════════════════════════════\n");

    if (!TELEFONO) {
        const msg = `✗ Número no configurado — guardalo en: ${CONFIG_FILE}`;
        console.error("  " + msg);
        escribirEstado(msg);
        process.exit(1);
    }
    console.log(`  Número: +${TELEFONO}\n`);
    escribirEstado("⏳ Iniciando bridge...");

    process.stdout.write("  Esperando API de Celestia");
    let intentos = 0;
    // 90 intentos × 2s = 180s — alineado con el timeout del launcher
    while (!(await verificarAPI())) {
        if (++intentos > 90) {
            const msg = "✗ Celestia API no responde — verificá la conexión";
            console.error("\n\n  " + msg + "\n");
            escribirEstado(msg);
            notificar("Celestia — Error", msg, "celestia");
            process.exit(1);
        }
        process.stdout.write(".");
        await new Promise(r => setTimeout(r, 2000));
    }
    console.log(" ✓\n");

    await iniciar(TELEFONO);
}

main().catch(err => {
    const msg = `Error fatal: ${err.message}`;
    console.error(msg);
    escribirEstado("✗ " + msg);
    process.exit(1);
});
