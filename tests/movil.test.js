import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const aqui = dirname(fileURLToPath(import.meta.url));
const raiz = join(aqui, "..");

function leer(nombre) {
  try {
    return readFileSync(join(raiz, nombre), "utf8");
  } catch {
    return null;
  }
}

// ── app.js: el foco no abre el teclado solo ──────────────────────────────

test("app.js no llama a focus() al cargar ni al mostrar el chat", () => {
  const js = leer("app.js");
  assert.ok(js, "falta app.js");
  // El único focus() va detrás de la condición de puntero fino.
  const llamadas = js.match(/\.focus\(\)/g) || [];
  assert.equal(llamadas.length, 1);
  assert.match(js, /if \(hayRaton\(\)\) campo\.focus\(\);/);
});

test("app.js comprueba pointer: fine antes de devolver el foco", () => {
  const js = leer("app.js");
  assert.match(js, /matchMedia\("\(pointer: fine\)"\)\.matches/);
});

// ── app.js: botón de enviar con icono y etiqueta ─────────────────────────

test("el botón de enviar lleva aria-label y un svg", () => {
  const js = leer("app.js");
  assert.match(js, /setAttribute\("aria-label", "Enviar"\)/);
  assert.match(js, /title = "Enviar"/);
  assert.match(js, /createElementNS\("http:\/\/www\.w3\.org\/2000\/svg", "svg"\)/);
});

test("el botón de enviar no usa emoji", () => {
  const js = leer("app.js");
  assert.doesNotMatch(js, /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u);
});

// ── app.js: el saludo no se guarda ni se manda ───────────────────────────

test("el saludo de bienvenida no entra en el historial", () => {
  const js = leer("app.js");
  assert.match(js, /const SALUDO =/);
  // El saludo se pinta aparte, sin pasar por estado.historial.
  assert.doesNotMatch(js, /estado\.historial\.push\(\{[^}]*SALUDO/);
  assert.doesNotMatch(js, /guardarHistorial\(\)[\s\S]{0,80}SALUDO/);
});

test("el historial que llega a enviar() nunca incluye el saludo", () => {
  const js = leer("app.js");
  // enviar() recibe el historial recortado, no la burbuja de bienvenida.
  assert.match(js, /enviar\(estado\.url, recortado, fetch\)/);
  assert.doesNotMatch(js, /enviar\([^)]*SALUDO/);
});

// ── estilo.css: maquetación de móvil ─────────────────────────────────────

test("body usa height: 100dvh y no hace scroll propio", () => {
  const css = leer("estilo.css");
  assert.ok(css, "falta estilo.css");
  assert.match(css, /body\s*\{[^}]*height:\s*100dvh/s);
  assert.match(css, /body\s*\{[^}]*overflow:\s*hidden/s);
});

test("la conversación es lo único que hace scroll", () => {
  const css = leer("estilo.css");
  assert.match(css, /\.conversacion\s*\{[^}]*overflow-y:\s*auto/s);
  assert.match(css, /\.conversacion\s*\{[^}]*min-height:\s*0/s);
});
