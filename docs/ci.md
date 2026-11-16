# CI — Genesis VisualForge (GEN-49)

Workflow: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)

## Qué hace

| Trigger | Evento |
|---|---|
| `push` a `main` | valida la rama tras cada merge |
| `pull_request` contra `main` | valida cada PR antes de fusionar |

Un único job (`gates`) con matriz de Pythons **3.11 y 3.13** (el rango soportado por `requires-python >= 3.11`), que ejecuta en orden los **dos únicos gates del repo**:

1. **Lint** — `ruff check .` (ruff `0.17.1`, configurado en `pyproject.toml`).
2. **Tests** — `python -m pytest` (pytest `9.1.1`, suite completa, sin red del proveedor).

Todo comando fallido **aborta el job al instante** (fail-fast) y ningún gate se salta ni se degrada a advertencia.

## Instalación reproducible

Las dependencias y sus versiones están **fijadas en `pyproject.toml`** (numerales exactos, cero rangos), así que el paso de instalación produce el mismo árbol en CI y en local:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

`setup-python` añade caché de pip (clave por `pyproject.toml`) solo como aceleración: el hash de la key cambia con cualquier cambio de dependencias y las versiones instaladas siempre son las fijadas.

## Sin consumo de créditos Higgsfield

En **todas** las ejecuciones automatizadas (PR y push a main):

- El workflow **no referencia ningún secret**: no hay forma de que un `HF_API_KEY` real llegue a CI.
- El entorno fuerza `HF_API_KEY=""` y `VISUALFORGE_PROVIDER=mock`.
- Con la selección deliberada de proveedor de `config.py` (GEN-30), `build_provider` construye siempre `MockProvider`: generación simulada local (Pillow), sin red del proveedor y sin cargos.
- `load_dotenv` se invoca con `override=False`, así que las variables del workflow no pueden ser pisadas por un `.env`.
- La propia suite usa el mock directamente (`tests/conftest.py`: la fixture `provider` construye `MockProvider`): los tests no tocan red del proveedor ni generan invocaciones facturables.

## Reproducción local (misma secuencia exacta que CI)

```bash
python -m venv .venv                # Windows: .venv\Scripts\activate ; Unix: source .venv/bin/activate
pip install -e ".[dev]"             # mismas versiones fijadas que instala CI

ruff check .                        # Gate 1: lint
python -m pytest                    # Gate 2: suite de tests completa
```

### Por qué `python -m pytest` y no `pytest`

`tests/` no es un package (no tiene `__init__.py`), y varios tests importan helpers de `tests.conftest` (p. ej. `make_png_bytes`). El ejecutable `pytest` no añade el directorio de trabajo a `sys.path`, por lo que el import falla con `ModuleNotFoundError: No module named 'tests'`. `python -m pytest` añade el cwd al `sys.path`, `tests` se resuelve como namespace package (PEP 420) y la suite pasa completa. Es el comando que usa CI y el que toca usar en local: **`python -m pytest`**.

## Cobertura

No existe gate de cobertura en el repo (ninguna herramienta de coverage está declarada en `pyproject.toml`), y esta issue **no inventa métricas**: la CI ejecuta exactamente los gates reales (tests + lint). Añadir cobertura requeriría decidir el umbral en otra issue.

## Fallos y debugging

- Cualquier fallo de gate pone el job en rojo y cancela la matriz (`fail-fast: true`).
- Para reproducir el rojo, ejecuta localmente el comando del paso fallido (misma versión de Python recomendada: 3.11 o 3.13).
- La CI corre en `ubuntu-latest`; si un fallo solo aparece en CI, revisa diferencias de rutas/timing, y compara con `python -m pytest -p no:cacheprovider` para descartar la caché local de pytest.
