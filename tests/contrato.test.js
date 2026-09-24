import { test } from "node:test";
import assert from "node:assert/strict";

import { enviar, recortarHistorial } from "../conexion.js";

// Contrato que exige la puerta (el servidor):
//   - cuerpo JSON: { "mensajes": [ ... ] }
//   - entre 1 y 20 mensajes
//   - cada mensaje tiene SÓLO las claves "role" y "content"
//   - "role" es "user" o "assistant"
//   - "content" es string de hasta 2000 caracteres
//   - el último mensaje es de "user"

const MAX_MENSAJES = 20;
const MAX_CONTENIDO = 2000;

function cumpleContrato(cuerpo) {
  if (!cuerpo || typeof cuerpo !== "object") return false;
  if (!Array.isArray(cuerpo.mensajes)) return false;
  const mensajes = cuerpo.mensajes;
  if (mensajes.length < 1 || mensajes.length > MAX_MENSAJES) return false;
  for (const mensaje of mensajes) {
    if (!mensaje || typeof mensaje !== "object") return false;
    const claves = Object.keys(mensaje).sort();
    if (claves.length !== 2) return false;
    if (claves[0] !== "content" || claves[1] !== "role") return false;
    if (mensaje.role !== "user" && mensaje.role !== "assistant") return false;
    if (typeof mensaje.content !== "string") return false;
    if (mensaje.content.length > MAX_CONTENIDO) return false;
  }
  if (mensajes[mensajes.length - 1].role !== "user") return false;
  return true;
}

function respuestaFalsa() {
  return {
    ok: true,
    status: 200,
    async json() {
      return { respuesta: "ok" };
    },
  };
}

test("enviar manda un cuerpo que cumple el contrato de la puerta", async () => {
  let capturado = null;
  const fetch = async (url, opciones) => {
    capturado = JSON.parse(opciones.body);
    return respuestaFalsa();
  };

  const mensajes = [
    { role: "user", content: "hola" },
    { role: "assistant", content: "hola, ¿qué tal?" },
    { role: "user", content: "bien" },
  ];

  await enviar("https://abc.trycloudflare.com", mensajes, fetch);

  assert.ok(cumpleContrato(capturado), "el cuerpo no cumple el contrato");
  assert.deepEqual(capturado, { mensajes });
});

test("enviar no añade ni quita claves a los mensajes", async () => {
  let capturado = null;
  const fetch = async (url, opciones) => {
    capturado = JSON.parse(opciones.body);
    return respuestaFalsa();
  };

  const mensajes = [{ role: "user", content: "sólo esto" }];
  await enviar("https://abc.trycloudflare.com", mensajes, fetch);

  assert.deepEqual(Object.keys(capturado.mensajes[0]).sort(), [
    "content",
    "role",
  ]);
});

test("recortarHistorial sobre 31 mensajes alternos cumple el contrato", () => {
  // Como en la página: se recorta justo después de añadir el mensaje de la
  // visita. 31 mensajes alternos que empiezan y acaban en user.
  const historial = Array.from({ length: 31 }, (_, i) => ({
    role: i % 2 === 0 ? "user" : "assistant",
    content: "m" + i,
  }));
  assert.equal(historial[0].role, "user");
  assert.equal(historial[historial.length - 1].role, "user");

  const recortado = recortarHistorial(historial);
  assert.ok(
    cumpleContrato({ mensajes: recortado }),
    "el historial recortado no cumple el contrato",
  );
  assert.equal(recortado[recortado.length - 1].role, "user");
  assert.ok(recortado.length >= 1 && recortado.length <= MAX_MENSAJES);
});

test("recortarHistorial no deja el primero en assistant", () => {
  const historial = Array.from({ length: 31 }, (_, i) => ({
    role: i % 2 === 0 ? "user" : "assistant",
    content: "m" + i,
  }));
  const recortado = recortarHistorial(historial);
  assert.equal(recortado[0].role, "user");
});
