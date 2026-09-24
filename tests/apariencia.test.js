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

// ── manifest.webmanifest ─────────────────────────────────────────────────

test("manifest.webmanifest existe y es JSON válido", () => {
  const crudo = leer("manifest.webmanifest");
  assert.ok(crudo, "falta manifest.webmanifest");
  assert.doesNotThrow(() => JSON.parse(crudo));
});

test("manifest.webmanifest se llama Celestia y es standalone", () => {
  const datos = JSON.parse(leer("manifest.webmanifest"));
  assert.equal(datos.name, "Celestia");
  assert.equal(datos.display, "standalone");
});

test("manifest.webmanifest trae colores del tema", () => {
  const datos = JSON.parse(leer("manifest.webmanifest"));
  assert.match(datos.theme_color, /^#[0-9A-Fa-f]{6}$/);
  assert.match(datos.background_color, /^#[0-9A-Fa-f]{6}$/);
});

// ── favicon.svg ──────────────────────────────────────────────────────────

test("favicon.svg existe y es un SVG", () => {
  const crudo = leer("favicon.svg");
  assert.ok(crudo, "falta favicon.svg");
  assert.match(crudo, /<svg[\s>]/);
});

test("favicon.svg dibuja un orbe con degradado", () => {
  const crudo = leer("favicon.svg");
  assert.match(crudo, /<radialGradient/);
  assert.match(crudo, /<circle/);
});

// ── index.html ───────────────────────────────────────────────────────────

test("index.html enlaza el favicon y el manifest", () => {
  const html = leer("index.html");
  assert.ok(html, "falta index.html");
  assert.match(html, /rel="icon"[^>]*href="favicon\.svg"/);
  assert.match(html, /rel="manifest"[^>]*href="manifest\.webmanifest"/);
});

test("index.html declara color-scheme y theme-color", () => {
  const html = leer("index.html");
  assert.match(html, /name="color-scheme"/);
  assert.match(html, /name="theme-color"/);
});

test("index.html lleva la cabecera con el nombre Celestia", () => {
  const html = leer("index.html");
  assert.match(html, /class="nombre"[^>]*>Celestia</);
});

// ── estilo.css ───────────────────────────────────────────────────────────

test("estilo.css usa la paleta del chat", () => {
  const css = leer("estilo.css");
  assert.ok(css, "falta estilo.css");
  assert.match(css, /--c-violeta:\s*#8b7cf6/i);
  assert.match(css, /--c-iris:\s*#a78bfa/i);
  assert.match(css, /--c-cian:\s*#4fd1e0/i);
});

test("estilo.css tiene modo claro y oscuro según el sistema", () => {
  const css = leer("estilo.css");
  assert.match(css, /@media\s*\(prefers-color-scheme:\s*light\)/);
});

test("estilo.css evita el scroll horizontal", () => {
  const css = leer("estilo.css");
  assert.match(css, /overflow-x:\s*hidden/);
});

test("estilo.css deja la caja de escribir abajo", () => {
  const css = leer("estilo.css");
  // El formulario va después de la conversación, que es la que crece.
  assert.match(css, /\.conversacion\s*\{[^}]*flex:\s*1/s);
});

// ── app.js ───────────────────────────────────────────────────────────────

test("app.js pinta los textos con textContent, no con innerHTML", () => {
  const js = leer("app.js");
  assert.ok(js, "falta app.js");
  assert.doesNotMatch(js, /\.innerHTML\s*=/);
  assert.match(js, /\.textContent\s*=/);
});

// ── README.md ────────────────────────────────────────────────────────────

test("README.md existe y no está vacío", () => {
  const md = leer("README.md");
  assert.ok(md, "falta README.md");
  assert.ok(md.trim().length > 200, "README.md parece vacío");
});

test("README.md explica qué es la página", () => {
  const md = leer("README.md");
  assert.match(md, /Celestia/);
  assert.match(md, /visita/i);
});

test("README.md explica que la dirección la publica Celestia en direccion.json", () => {
  const md = leer("README.md");
  assert.match(md, /direccion\.json/);
});

test("README.md explica cómo pasar los tests con node --test", () => {
  const md = leer("README.md");
  assert.match(md, /node --test/);
});

test("README.md no usa bloques de código con tres comillas invertidas", () => {
  const md = leer("README.md");
  assert.doesNotMatch(md, /```/);
});
