// Lógica pura de Celestia. Sin dependencias, sin efectos secundarios.
// Todas las funciones reciben `fetch` (y `almacen` cuando hace falta) inyectados.

const PATRON_URL = /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/;
const SUBDOMINIO_RESERVADO = "api";

export async function buscarDireccion(fetch) {
  try {
    const respuesta = await fetch("direccion.json?t=" + Date.now(), {
      cache: "no-store",
    });
    if (!respuesta || !respuesta.ok) return null;
    const datos = await respuesta.json();
    if (!datos || typeof datos.url !== "string") return null;
    if (!PATRON_URL.test(datos.url)) return null;
    const subdominio = datos.url.slice("https://".length).split(".")[0];
    if (subdominio === SUBDOMINIO_RESERVADO) return null;
    return datos.url;
  } catch {
    return null;
  }
}

export async function estaEncendida(url, fetch) {
  const controlador = new AbortController();
  const temporizador = setTimeout(() => controlador.abort(), 8000);
  try {
    const respuesta = await fetch(url + "/estado", {
      signal: controlador.signal,
    });
    if (!respuesta || respuesta.status !== 200) return false;
    const datos = await respuesta.json();
    return datos && datos.ok === true;
  } catch {
    return false;
  } finally {
    clearTimeout(temporizador);
  }
}

export async function enviar(url, mensajes, fetch) {
  let respuesta;
  try {
    respuesta = await fetch(url + "/visita/mensaje", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mensajes }),
    });
  } catch {
    throw new Error("No consigo hablar con Celestia. ¿Está encendida?");
  }

  if (respuesta.status === 429) {
    throw new Error("Estoy atendiendo a mucha gente a la vez. Espera un poco.");
  }
  if (respuesta.status === 413) {
    throw new Error("Ese mensaje es demasiado largo.");
  }
  if (respuesta.status === 400) {
    let mensaje = "";
    try {
      const datos = await respuesta.json();
      mensaje = datos && typeof datos.error === "string" ? datos.error : "";
    } catch {
      mensaje = "";
    }
    throw new Error(mensaje || "No he podido mandar ese mensaje.");
  }
  if (respuesta.status === 503) {
    let mensaje = "";
    try {
      const datos = await respuesta.json();
      mensaje = datos && datos.error ? datos.error : "";
    } catch {
      mensaje = "";
    }
    throw new Error(mensaje);
  }
  if (!respuesta.ok) {
    throw new Error("No consigo hablar con Celestia. ¿Está encendida?");
  }

  const datos = await respuesta.json();
  return datos && typeof datos.respuesta === "string" ? datos.respuesta : "";
}

export function recortarHistorial(mensajes, max = 20) {
  let recortado = mensajes.slice(-max);
  while (recortado.length > 0 && recortado[0].role !== "user") {
    recortado = recortado.slice(1);
  }
  return recortado;
}

export function leerLlave(hash, almacen) {
  if (typeof hash === "string" && hash.startsWith("#t=")) {
    const llave = hash.slice(3);
    almacen.setItem("celestia_llave", llave);
    return llave;
  }
  const guardada = almacen.getItem("celestia_llave");
  return guardada ? guardada : "";
}

export function enlaceCompleto(url, llave) {
  return url + "/chat#t=" + encodeURIComponent(llave);
}
