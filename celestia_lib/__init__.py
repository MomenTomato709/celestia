"""Celestia — agente personal autónomo.

La versión vive en `version.py` (la que compara el actualizador y la que lleva
la app de Android). `__version__` es la misma, para quien la busque aquí: hasta
el 4 oct 2026 eran dos números distintos y /estado decía «2.0.0» en una 1.0.0.
"""

from .version import VERSION as __version__  # noqa: F401
