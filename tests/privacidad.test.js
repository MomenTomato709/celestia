import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const aqui = dirname(fileURLToPath(import.meta.url));
const raiz = join(aqui, "..");

// Ficheros que se sirven al público. Si alguno no existe todavía, se ignora:
// así el test no se rompe por un fichero que aún no se ha creado.
const PUBLICOS = [
  "index.html",
  "estilo.css",
  "app.js",
  "conexion.js",
  "manifest.webmanifest",
  "favicon.svg",
  "README.md",
];

function leer(nombre) {
  try {
    return readFileSync(join(raiz, nombre), "utf8");
  } catch {
    return null;
  }
}

// Un correo con forma de correo. Se busca en el texto servido, no en los
// tests ni en la configuración.
const CORREO = /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/i;

// Un teléfono español con 9 dígitos seguidos, con o sin prefijo +34 y con
// separadores habituales (espacios, guiones, puntos).
const TELEFONO = /(?:\+?34[\s.-]?)?(?:\d[\s.-]?){9}/;

test("ningún fichero público lleva correos ni teléfonos", () => {
  for (const nombre of PUBLICOS) {
    const texto = leer(nombre);
    if (texto === null) continue;
    assert.equal(
      CORREO.test(texto),
      false,
      `${nombre} parece contener un correo`,
    );
    assert.equal(
      TELEFONO.test(texto),
      false,
      `${nombre} parece contener un teléfono`,
    );
  }
});

test("app.js pinta el texto con textContent, nunca con innerHTML", () => {
  const texto = leer("app.js");
  assert.ok(texto !== null, "app.js debería existir");
  assert.equal(
    /innerHTML/.test(texto),
    false,
    "app.js no debe usar innerHTML",
  );
});

test("no hay secretos escritos en el código servido", () => {
  // Nombres de variables de entorno o claves con pinta de secreto. La
  // dirección de Celestia llega por direccion.json, no escrita aquí.
  const SOSPECHOSO = /(api[_-]?key|secret|password|contraseña|token)\s*[:=]\s*["'][^"']+["']/i;
  for (const nombre of PUBLICOS) {
    const texto = leer(nombre);
    if (texto === null) continue;
    assert.equal(
      SOSPECHOSO.test(texto),
      false,
      `${nombre} parece llevar un secreto escrito`,
    );
  }
});

test("los ficheros públicos esperados existen", () => {
  const presentes = readdirSync(raiz);
  for (const nombre of ["index.html", "estilo.css", "app.js", "conexion.js"]) {
    assert.ok(
      presentes.includes(nombre),
      `falta ${nombre} en la raíz`,
    );
  }
});
