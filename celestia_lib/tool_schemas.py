"""JSON Schemas de las herramientas para *function calling* nativo.

Bloque 4 de la hoja de ruta: el núcleo de decisión histórico son ~80 regex en
español evaluadas en orden. Eso es frágil (insertar una rompe otra), O(n) por
mensaje, solo-español y difícil de testear. Groq (Llama 3.3) y OpenRouter
soportan *function calling* nativo: el modelo devuelve qué tool usar y los
parámetros ya parseados en JSON.

Estrategia GRADUAL y SEGURA (no rompe nada):
- Las regex (`AgentPlanner.detect`) siguen siendo la PRIMERA vía: cubren todos
  los casos ya testeados y son deterministas.
- Cuando el regex NO detecta intención y el backend soporta tools, se ofrece
  esta lista al modelo. Resuelve de golpe el idioma (inglés/catalán/…), las
  frases nuevas y la extracción de parámetros, sin escribir una regex nueva.

Por seguridad SOLO se exponen aquí herramientas de LECTURA / CÁLCULO sin
efectos secundarios destructivos. Las acciones con efecto real (borrar,
crear_archivo, ejecutar_comando, abrir_app, llamar, enviar_mensaje, vault_*,
domótica sobre hardware…) NO se delegan al modelo: siguen pasando por sus
detectores regex anclados y sus guardarraíles (ver Bloques 1-3). Esto mantiene
el principio de la hoja de ruta: ampliar capacidad sin ampliar superficie de
daño.

AMPLIACIÓN (S55g, 5 sep 2026) — que sepa mirar en su propio disco.
Enzo pidió poder encargarle cualquier cosa «y que aunque no sepa, busque la
manera». Preguntándole cuántas líneas tenía un fichero SUYO contestaba «dame la
URL del repositorio», y por sus conversaciones guardadas, «no tengo acceso desde
esta interfaz». No mentía: con las diez herramientas que se le ofrecían era
verdad — ninguna sabía abrir un fichero.

Se añaden las de LEER: leer_archivo, listar_archivos, buscar_archivos,
listar_recordatorios, porcentaje, convertir_divisa y silabas. Todas leen o
calculan, y las de ficheros pasan por el sandbox de rutas de
`AgentTools._ruta_segura`: la política de arriba se mantiene intacta.

`ejecutar_comando` sigue FUERA a propósito, aunque tenga su lista blanca:
decidir QUÉ ejecutar no es lo mismo que leer un fichero, y casi todo lo que
faltaba se resuelve leyendo. Lo que tiene efectos de verdad se propone y se
pide confirmación (ver `celestia_lib/empeno.py`).

Los nombres y los parámetros coinciden EXACTAMENTE con el `dispatch` de
`AgentTools.execute`, de modo que un `tool_call` del modelo se ejecuta sin
traducción intermedia.
"""
from typing import Dict, List

# Cada entrada sigue el formato de OpenAI/Groq `tools=[...]`.
TOOL_SCHEMAS: List[Dict] = [
    {
        "type": "function",
        "function": {
            "name": "calcular",
            "description": (
                "Resuelve una expresión aritmética o matemática "
                "(sumas, restas, multiplicaciones, potencias, raíces, "
                "porcentajes). Úsala siempre que el usuario pida un cálculo "
                "numérico, en cualquier idioma."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expresion": {
                        "type": "string",
                        "description": (
                            "La expresión a evaluar en notación matemática, "
                            "p. ej. '5 * 8', '2**10', 'sqrt(144) + 5'."
                        ),
                    },
                },
                "required": ["expresion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contar_letras",
            "description": (
                "Cuenta cuántas veces aparece una letra concreta dentro de un "
                "texto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string", "description": "El texto a analizar."},
                    "letra": {"type": "string", "description": "La letra a contar."},
                },
                "required": ["texto", "letra"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contar_palabras",
            "description": "Cuenta cuántas palabras tiene un texto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string", "description": "El texto a analizar."},
                },
                "required": ["texto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "longitud_texto",
            "description": (
                "Mide la longitud de un texto en caracteres, letras o símbolos."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string", "description": "El texto a medir."},
                    "unidad": {
                        "type": "string",
                        "enum": ["caracteres", "letras", "simbolos"],
                        "description": "Qué contar. Por defecto 'caracteres'.",
                    },
                },
                "required": ["texto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hora_ciudad",
            "description": (
                "Devuelve la hora local actual de una ciudad del mundo. Úsala "
                "cuando pregunten qué hora es en un lugar concreto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ciudad": {
                        "type": "string",
                        "description": "Nombre de la ciudad, p. ej. 'Tokio', 'Nueva York'.",
                    },
                },
                "required": ["ciudad"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dias_hasta",
            "description": (
                "Calcula cuántos días faltan hasta una fecha o evento señalado "
                "(navidad, año nuevo, una fecha dd/mm, etc.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evento": {
                        "type": "string",
                        "description": "El evento o fecha objetivo, p. ej. 'navidad', '15/08'.",
                    },
                },
                "required": ["evento"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_clima",
            "description": (
                "Consulta el clima/tiempo actual de una ubicación. Úsala cuando "
                "pregunten por el tiempo, la temperatura o si lloverá en un lugar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ubicacion": {
                        "type": "string",
                        "description": "Ciudad o lugar, p. ej. 'Madrid'.",
                    },
                },
                "required": ["ubicacion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_web",
            "description": (
                "Busca información actualizada en internet sobre un tema. Úsala "
                "cuando el usuario pida buscar, investigar o necesite datos que "
                "podrían haber cambiado recientemente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Qué buscar, en palabras clave.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_noticias",
            "description": "Trae titulares de noticias recientes, opcionalmente de un tema.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tema": {
                        "type": "string",
                        "description": "Tema de las noticias (vacío = generales).",
                    },
                    "max_titulares": {
                        "type": "integer",
                        "description": "Cuántos titulares devolver (por defecto 6).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "info_sistema",
            "description": (
                "Informa del estado del sistema donde corre Celestia (CPU, RAM, "
                "disco, batería). Úsala si preguntan por el estado del dispositivo."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "leer_archivo",
            "description": (
                "Lee el contenido de un archivo del propio dispositivo. Úsala "
                "siempre que pregunten por lo que hay DENTRO de un fichero "
                "(cuántas líneas tiene, qué dice, buscar algo dentro) en vez de "
                "pedir que te lo peguen o buscarlo en internet. Rutas del "
                "proyecto como CHANGELOG.md o README.md valen tal cual."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ruta": {"type": "string",
                             "description": "Ruta del archivo, relativa al proyecto o absoluta."}
                },
                "required": ["ruta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_memoria",
            "description": (
                "Cuánto hay guardado en la memoria de Celestia: cuántas "
                "conversaciones habéis tenido, cuántas cosas sabe del usuario, "
                "desde cuándo, cuánto ocupa. Úsala para «cuántas "
                "conversaciones tenemos», «desde cuándo me conoces» o «cuánto "
                "recuerdas». Devuelve recuentos, no el contenido."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "que": {
                        "type": "string",
                        "enum": ["resumen", "conversaciones", "hechos", "episodios",
                                 "aprendizajes", "errores", "entidades"],
                        "description": "Qué parte mirar. Por defecto, el resumen.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "info_archivo",
            "description": (
                "Cuenta cuántas LÍNEAS, palabras, caracteres y bytes tiene un "
                "archivo. Úsala SIEMPRE para «cuántas líneas/palabras tiene X» "
                "o «cuánto ocupa X» — nunca leas el archivo para contarlo a "
                "ojo: el contenido se recorta y el número saldría mal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ruta": {"type": "string", "description": "Ruta del archivo."}
                },
                "required": ["ruta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_archivos",
            "description": (
                "Lista lo que hay en una carpeta del dispositivo. Útil para "
                "saber qué ficheros existen antes de leer uno."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ruta": {"type": "string", "description": "Carpeta a listar."}
                },
                "required": ["ruta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_archivos",
            "description": (
                "Busca archivos por nombre cuando no se sabe dónde están. "
                "Ejemplo de patrón: '*.db' o 'CHANGELOG*'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patron": {"type": "string", "description": "Patrón del nombre, admite *."},
                    "ruta": {"type": "string", "description": "Dónde buscar; por defecto el proyecto."},
                },
                "required": ["patron"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_recordatorios",
            "description": "Lista los recordatorios pendientes del usuario.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "porcentaje",
            "description": "Calcula el P% de N («cuánto es el 15% de 240»).",
            "parameters": {
                "type": "object",
                "properties": {
                    "parte": {"type": "string", "description": "El porcentaje, p.ej. '15'."},
                    "total": {"type": "string", "description": "La cantidad, p.ej. '240'."},
                },
                "required": ["parte", "total"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convertir_divisa",
            "description": "Convierte una cantidad entre monedas con el cambio del día.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cantidad": {"type": "string", "description": "Cuánto convertir."},
                    "origen": {"type": "string", "description": "Moneda de origen (EUR, USD…)."},
                    "destino": {"type": "string", "description": "Moneda de destino."},
                },
                "required": ["cantidad", "origen", "destino"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "silabas",
            "description": "Separa una palabra en sílabas y dice cuántas tiene.",
            "parameters": {
                "type": "object",
                "properties": {"palabra": {"type": "string"}},
                "required": ["palabra"],
            },
        },
    },
]

# Conjunto de nombres permitidos vía function calling. Cualquier `tool_call`
# del modelo cuyo nombre NO esté aquí se ignora (defensa en profundidad: aunque
# el modelo alucine "borrar" o "ejecutar_comando", no se ejecuta por esta vía).
SAFE_TOOL_NAMES = frozenset(
    s["function"]["name"] for s in TOOL_SCHEMAS
)


def tools_payload() -> List[Dict]:
    """Devuelve la lista de schemas lista para pasar como `tools=[...]`."""
    return TOOL_SCHEMAS
