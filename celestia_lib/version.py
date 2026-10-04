"""La versión de Celestia: un solo sitio.

La app la compara con la última publicada para saber si hay una nueva
(`actualizar.py`). Al sacar una versión se sube AQUÍ y en
`android/pyproject.toml` (las dos tienen que coincidir: lo comprueba
`tests/test_actualizar.py`), y se etiqueta igual: `git tag v<VERSION>`.
"""
VERSION = "2.4.0"
