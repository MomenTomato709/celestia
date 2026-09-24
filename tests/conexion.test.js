import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buscarDireccion,
  estaEncendida,
  enviar,
  recortarHistorial,
  leerLlave,
  enlaceCompleto,
} from "../conexion.js";

function respuestaFalsa({ ok = true, status = 200, json = null } = {}) {
  return {
    ok,
    status,
    async json() {
      if (json instanceof Error) throw json;
      return json;
    },
  };
}

function almacenFalso(inicial = {}) {
  const datos = { ...inicial };
  return {
    getItem(clave) {
      return Object.prototype.hasOwnProperty.call(datos, clave)
        ? datos[clave]
        : null;
    },
    setItem(clave, valor) {
      datos[clave] = String(valor);
    },
  };
}

// --- buscarDireccion ---

test("buscarDireccion devuelve la url válida", async () => {
  const fetch = async () =>
    respuestaFalsa({ json: { url: "https://abc-123.trycloudflare.com" } });
  assert.equal(
    await buscarDireccion(fetch),
    "https://abc-123.trycloudflare.com",
  );
});

test("buscarDireccion pide direccion.json con cache no-store", async () => {
  let capturada = null;
  const fetch = async (url, opciones) => {
    capturada = { url, opciones };
    return respuestaFalsa({ json: { url: "https://abc.trycloudflare.com" } });
  };
  await buscarDireccion(fetch);
  assert.match(capturada.url, /^direccion\.json\?t=\d+$/);
  assert.equal(capturada.opciones.cache, "no-store");
});

test("buscarDireccion rechaza http://", async () => {
  const fetch = async () =>
    respuestaFalsa({ json: { url: "http://abc.trycloudflare.com" } });
  assert.equal(await buscarDireccion(fetch), null);
});

test("buscarDireccion rechaza otro dominio", async () => {
  const fetch = async () =>
    respuestaFalsa({ json: { url: "https://abc.example.com" } });
  assert.equal(await buscarDireccion(fetch), null);
});

test("buscarDireccion rechaza url con ruta", async () => {
  const fetch = async () =>
    respuestaFalsa({ json: { url: "https://abc.trycloudflare.com/chat" } });
  assert.equal(await buscarDireccion(fetch), null);
});

test("buscarDireccion rechaza api.trycloudflare.com", async () => {
  const fetch = async () =>
    respuestaFalsa({ json: { url: "https://api.trycloudflare.com" } });
  assert.equal(await buscarDireccion(fetch), null);
});

test("buscarDireccion rechaza url vacía", async () => {
  const fetch = async () => respuestaFalsa({ json: { url: "" } });
  assert.equal(await buscarDireccion(fetch), null);
});

test("buscarDireccion devuelve null si falla la red", async () => {
  const fetch = async () => {
    throw new Error("sin red");
  };
  assert.equal(await buscarDireccion(fetch), null);
});

test("buscarDireccion devuelve null si la respuesta no es ok", async () => {
  const fetch = async () => respuestaFalsa({ ok: false, status: 500 });
  assert.equal(await buscarDireccion(fetch), null);
});

// --- estaEncendida ---

test("estaEncendida true si 200 y ok === true", async () => {
  const fetch = async () => respuestaFalsa({ status: 200, json: { ok: true } });
  assert.equal(await estaEncendida("https://abc.trycloudflare.com", fetch), true);
});

test("estaEncendida false si ok !== true", async () => {
  const fetch = async () => respuestaFalsa({ status: 200, json: { ok: false } });
  assert.equal(
    await estaEncendida("https://abc.trycloudflare.com", fetch),
    false,
  );
});

test("estaEncendida false si status no es 200", async () => {
  const fetch = async () => respuestaFalsa({ status: 503, json: { ok: true } });
  assert.equal(
    await estaEncendida("https://abc.trycloudflare.com", fetch),
    false,
  );
});

test("estaEncendida false si falla la red", async () => {
  const fetch = async () => {
    throw new Error("sin red");
  };
  assert.equal(
    await estaEncendida("https://abc.trycloudflare.com", fetch),
    false,
  );
});

test("estaEncendida llama a /estado", async () => {
  let capturada = null;
  const fetch = async (url) => {
    capturada = url;
    return respuestaFalsa({ status: 200, json: { ok: true } });
  };
  await estaEncendida("https://abc.trycloudflare.com", fetch);
  assert.equal(capturada, "https://abc.trycloudflare.com/estado");
});

// --- enviar ---

test("enviar devuelve el texto de la respuesta", async () => {
  const fetch = async () =>
    respuestaFalsa({ status: 200, json: { respuesta: "Hola, soy Celestia" } });
  const texto = await enviar(
    "https://abc.trycloudflare.com",
    [{ rol: "user", texto: "hola" }],
    fetch,
  );
  assert.equal(texto, "Hola, soy Celestia");
});

test("enviar hace POST a /visita/mensaje con JSON", async () => {
  let capturada = null;
  const fetch = async (url, opciones) => {
    capturada = { url, opciones };
    return respuestaFalsa({ status: 200, json: { respuesta: "ok" } });
  };
  await enviar("https://abc.trycloudflare.com", [{ rol: "user", texto: "x" }], fetch);
  assert.equal(capturada.url, "https://abc.trycloudflare.com/visita/mensaje");
  assert.equal(capturada.opciones.method, "POST");
  assert.equal(capturada.opciones.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(capturada.opciones.body), {
    mensajes: [{ rol: "user", texto: "x" }],
  });
});

test("enviar 429 lanza el mensaje de mucha gente", async () => {
  const fetch = async () => respuestaFalsa({ ok: false, status: 429 });
  await assert.rejects(
    () => enviar("https://abc.trycloudflare.com", [], fetch),
    /Estoy atendiendo a mucha gente a la vez\. Espera un poco\./,
  );
});

test("enviar 400 lanza el mensaje de demasiado largo", async () => {
  const fetch = async () => respuestaFalsa({ ok: false, status: 400 });
  await assert.rejects(
    () => enviar("https://abc.trycloudflare.com", [], fetch),
    /Ese mensaje es demasiado largo\./,
  );
});

test("enviar 413 lanza el mensaje de demasiado largo", async () => {
  const fetch = async () => respuestaFalsa({ ok: false, status: 413 });
  await assert.rejects(
    () => enviar("https://abc.trycloudflare.com", [], fetch),
    /Ese mensaje es demasiado largo\./,
  );
});

test("enviar 503 lanza el error del servidor", async () => {
  const fetch = async () =>
    respuestaFalsa({ ok: false, status: 503, json: { error: "En mantenimiento" } });
  await assert.rejects(
    () => enviar("https://abc.trycloudflare.com", [], fetch),
    /En mantenimiento/,
  );
});

test("enviar fallo de red lanza el mensaje de apagada", async () => {
  const fetch = async () => {
    throw new Error("sin red");
  };
  await assert.rejects(
    () => enviar("https://abc.trycloudflare.com", [], fetch),
    /No consigo hablar con Celestia\. ¿Está encendida\?/,
  );
});

// --- recortarHistorial ---

test("recortarHistorial deja los últimos max", () => {
  const mensajes = Array.from({ length: 30 }, (_, i) => ({
    rol: i % 2 === 0 ? "user" : "celestia",
    texto: String(i),
  }));
  const recortado = recortarHistorial(mensajes, 20);
  assert.equal(recortado.length, 20);
  assert.equal(recortado[0].texto, "10");
});

test("recortarHistorial quita hasta que el primero sea de user", () => {
  const mensajes = [
    { rol: "celestia", texto: "a" },
    { rol: "user", texto: "b" },
    { rol: "celestia", texto: "c" },
  ];
  const recortado = recortarHistorial(mensajes, 20);
  assert.equal(recortado[0].rol, "user");
  assert.equal(recortado.length, 2);
});

test("recortarHistorial con max por defecto 20", () => {
  const mensajes = Array.from({ length: 25 }, (_, i) => ({
    rol: "user",
    texto: String(i),
  }));
  assert.equal(recortarHistorial(mensajes).length, 20);
});

// --- leerLlave ---

test("leerLlave guarda y devuelve la llave del hash", () => {
  const almacen = almacenFalso();
  const llave = leerLlave("#t=secreta", almacen);
  assert.equal(llave, "secreta");
  assert.equal(almacen.getItem("celestia_llave"), "secreta");
});

test("leerLlave devuelve la guardada si no hay hash", () => {
  const almacen = almacenFalso({ celestia_llave: "guardada" });
  assert.equal(leerLlave("", almacen), "guardada");
});

test("leerLlave devuelve cadena vacía si no hay nada", () => {
  const almacen = almacenFalso();
  assert.equal(leerLlave("", almacen), "");
});

// --- enlaceCompleto ---

test("enlaceCompleto construye la url del chat", () => {
  assert.equal(
    enlaceCompleto("https://abc.trycloudflare.com", "mi llave"),
    "https://abc.trycloudflare.com/chat#t=mi%20llave",
  );
});
