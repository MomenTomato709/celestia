import {
  buscarDireccion,
  estaEncendida,
  enviar,
  recortarHistorial,
  leerLlave,
  enlaceCompleto,
} from "./conexion.js";

const CLAVE_HISTORIAL = "celestia_visita_historial";

const estado = {
  url: null,
  historial: [],
  esperando: false,
};

const pantalla = document.getElementById("pantalla");
const aviso = document.getElementById("aviso");

function mostrarBuscando() {
  pantalla.replaceChildren();
  const p = document.createElement("p");
  p.className = "estado";
  p.textContent = "Buscando a Celestia…";
  pantalla.appendChild(p);
}

function mostrarApagada() {
  pantalla.replaceChildren();
  const p = document.createElement("p");
  p.className = "estado";
  p.textContent = "Celestia está apagada ahora mismo. Vuelve más tarde.";
  const boton = document.createElement("button");
  boton.type = "button";
  boton.textContent = "Reintentar";
  boton.addEventListener("click", iniciar);
  pantalla.appendChild(p);
  pantalla.appendChild(boton);
}

function cargarHistorial() {
  try {
    const crudo = sessionStorage.getItem(CLAVE_HISTORIAL);
    if (!crudo) return [];
    const datos = JSON.parse(crudo);
    return Array.isArray(datos) ? datos : [];
  } catch {
    return [];
  }
}

function guardarHistorial() {
  try {
    sessionStorage.setItem(CLAVE_HISTORIAL, JSON.stringify(estado.historial));
  } catch {
    // sin almacenamiento disponible: seguimos en memoria
  }
}

function pintarMensaje(contenedor, mensaje) {
  const burbuja = document.createElement("div");
  burbuja.className = "burbuja " + (mensaje.rol === "user" ? "usuario" : "celestia");
  const texto = document.createElement("p");
  texto.textContent = mensaje.texto;
  burbuja.appendChild(texto);
  contenedor.appendChild(burbuja);
}

function pintarHistorial(contenedor) {
  contenedor.replaceChildren();
  for (const mensaje of estado.historial) {
    pintarMensaje(contenedor, mensaje);
  }
}

function mostrarChat() {
  pantalla.replaceChildren();

  const conversacion = document.createElement("div");
  conversacion.className = "conversacion";
  conversacion.id = "conversacion";

  const formulario = document.createElement("form");
  formulario.className = "formulario";

  const campo = document.createElement("textarea");
  campo.id = "campo";
  campo.rows = 1;
  campo.placeholder = "Escribe un mensaje…";
  campo.setAttribute("aria-label", "Mensaje");

  const boton = document.createElement("button");
  boton.type = "submit";
  boton.textContent = "Enviar";

  formulario.appendChild(campo);
  formulario.appendChild(boton);

  pantalla.appendChild(conversacion);
  pantalla.appendChild(formulario);

  pintarHistorial(conversacion);
  conversacion.scrollTop = conversacion.scrollHeight;

  campo.addEventListener("keydown", (evento) => {
    if (evento.key === "Enter" && !evento.shiftKey) {
      evento.preventDefault();
      formulario.requestSubmit();
    }
  });

  formulario.addEventListener("submit", (evento) => {
    evento.preventDefault();
    manejarEnvio(campo, boton, conversacion);
  });

  campo.focus();
}

function mostrarEscribiendo(contenedor) {
  const burbuja = document.createElement("div");
  burbuja.className = "burbuja celestia escribiendo";
  burbuja.id = "escribiendo";
  const texto = document.createElement("p");
  texto.textContent = "Celestia está escribiendo…";
  burbuja.appendChild(texto);
  contenedor.appendChild(burbuja);
  contenedor.scrollTop = contenedor.scrollHeight;
}

function quitarEscribiendo() {
  const burbuja = document.getElementById("escribiendo");
  if (burbuja) burbuja.remove();
}

function mostrarAviso(texto) {
  aviso.textContent = texto;
  aviso.hidden = false;
}

function ocultarAviso() {
  aviso.textContent = "";
  aviso.hidden = true;
}

async function manejarEnvio(campo, boton, conversacion) {
  if (estado.esperando) return;
  const texto = campo.value.trim();
  if (!texto) return;

  ocultarAviso();

  const mensajeUsuario = { rol: "user", texto };
  estado.historial.push(mensajeUsuario);
  guardarHistorial();
  pintarMensaje(conversacion, mensajeUsuario);
  campo.value = "";
  conversacion.scrollTop = conversacion.scrollHeight;

  estado.esperando = true;
  boton.disabled = true;
  mostrarEscribiendo(conversacion);

  try {
    const recortado = recortarHistorial(estado.historial);
    const respuesta = await enviar(estado.url, recortado, fetch);
    quitarEscribiendo();
    const mensajeCelestia = { rol: "celestia", texto: respuesta };
    estado.historial.push(mensajeCelestia);
    guardarHistorial();
    pintarMensaje(conversacion, mensajeCelestia);
    conversacion.scrollTop = conversacion.scrollHeight;
  } catch (error) {
    quitarEscribiendo();
    mostrarAviso(error.message);
  } finally {
    estado.esperando = false;
    boton.disabled = false;
    campo.focus();
  }
}

async function iniciar() {
  mostrarBuscando();
  ocultarAviso();

  const llave = leerLlave(location.hash, localStorage);
  if (location.hash) {
    history.replaceState(null, "", location.pathname + location.search);
  }

  const url = await buscarDireccion(fetch);
  if (!url) {
    mostrarApagada();
    return;
  }

  const encendida = await estaEncendida(url, fetch);
  if (!encendida) {
    mostrarApagada();
    return;
  }

  estado.url = url;

  if (llave) {
    location.replace(enlaceCompleto(url, llave));
    return;
  }

  estado.historial = cargarHistorial();
  mostrarChat();
}

iniciar();
