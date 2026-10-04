"""Blueprints de la API HTTP de Celestia (Camino 2 de la hoja de ruta).

`api.py` concentraba 42 rutas en un único método `WhatsAppAPI._registrar_rutas`.
Aquí se agrupan por dominio. Cada módulo expone `crear(api) -> Blueprint`: una
factory que recibe la instancia `WhatsAppAPI` y define las rutas como closures
que usan `api` (la instancia). Los hooks globales (`before_request`,
`errorhandler`) y los helpers de negocio permanecen en `WhatsAppAPI` — aquí solo
viven las rutas, sin cambio de comportamiento.
"""
