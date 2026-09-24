# Celestia · página de visita

Esta es la página pública de Celestia. Sirve para que alguien que no tiene
acceso al móvil donde vive Celestia pueda hablar con ella como visita: se
abre en el navegador, se escribe un mensaje y se recibe la respuesta. No
guarda lo que escribes más allá de la pestaña abierta.

## Cómo encuentra la dirección

La página no lleva la dirección de Celestia escrita dentro. Al cargar,
pide un fichero llamado `direccion.json` que publica la propia Celestia
junto a esta página. Ese fichero tiene una sola cosa:

    { "url": "https://algo.trycloudflare.com" }

Si el fichero no está, o la dirección no tiene la forma esperada, la página
enseña un aviso de que Celestia está apagada y ofrece un botón para
reintentar. Así la dirección puede cambiar sin tocar el código.

## Ficheros

- `index.html` — la página.
- `estilo.css` — el aspecto, con modo claro y oscuro según el sistema.
- `app.js` — la conversación: pinta los mensajes y habla con Celestia.
- `conexion.js` — la lógica pura de conexión (buscar la dirección,
  comprobar que está encendida, enviar un mensaje). No tiene efectos
  secundarios y recibe `fetch` inyectado, para poder probarla.
- `manifest.webmanifest` — para poder instalarla como aplicación.
- `favicon.svg` — el icono: un orbe con degradado.

## Cómo pasar los tests

Los tests usan el runner que trae Node, sin dependencias. Desde la raíz del
repositorio:

    node --test

Para pasar solo los tests de esta carpeta:

    node --test tests/

## Aspecto

El aspecto sigue al del chat de Celestia: mismos colores, misma tipografía,
burbujas redondeadas y cabecera con el nombre. El modo claro y el oscuro se
eligen solos según lo que tenga puesto el sistema (`prefers-color-scheme`),
y en el móvil la caja de escribir queda abajo, sin scroll horizontal.
