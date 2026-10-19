# Genesis VisualForge

Herramienta **local** para generar lotes de imágenes profesionales para webs y landings mediante la API de [Higgsfield](https://open.higgsfield.ai).

Issue: [GEN-11](https://linear.app/genesisnavarra/issue/GEN-11) · Rama: `feat/GEN-11-genesis-visualforge-mvp`

## Inicio rápido

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows; en Unix: source .venv/bin/activate
pip install -e ".[dev]"
copy .env.example .env        # introduce tu HF_API_KEY (formato key-id:key-secret)
streamlit run app.py
```

Sin clave, la app arranca en **modo mock** (generación simulada, sin coste).

## Qué hace

1. **Proyectos** — un proyecto por cliente: brief, dirección de arte (prompt base) y fotos de referencia.
2. **Prompts** — importación masiva «uno por línea» (`Nombre :: prompt` o solo el prompt) + editor de tabla con campo «mantener» por asset.
3. **Lote** — modelo (`marketing-studio/image` o `.../flare`), resolución, ratio, calidad y concurrencia (por defecto 2). Estimación de coste antes de enviar. Cada imagen se envía como job independiente con su `idempotency_key` persistida.
4. **Resultados** — polling con backoff, cancelación real en el proveedor, reanudación tras reiniciar la app, regeneración selectiva por imagen (`slug-vN`) y exportación a `originals/` + `web/` (WebP/JPEG/PNG) + ZIP con `README-assets.md`, `manifest.json` y `prompts.json`.

## Garantías de ejecución

- Concurrencia limitada (2 por defecto, tope 8).
- La misma `idempotency_key` se reutiliza ante fallos ambiguos: el proveedor devuelve el mismo `request_id` y no se duplica el cargo.
- Un job con `request_id` **jamás se vuelve a enviar**: solo se consulta estado.
- Cancelar antes de enviar no genera cargo; cancelar en curso usa el endpoint documentado de Higgsfield.
- Manifiesto por lote trazable y sin secretos.

## Arquitectura

```
app.py                  # streamlit run app.py
src/visualforge/
  models.py             # dominio + validación de payloads por modelo
  config.py             # .env / variables de entorno
  prompts.py            # slugs, importador, composición del prompt final
  storage.py            # proyectos en disco + lotes/jobs en SQLite (WAL)
  jobs.py               # BatchRunner: cola, reintentos, cancelación, reanudación
  export.py             # optimización web (Pillow) y ZIP
  provider/
    base.py             # contrato ImageProvider
    higgsfield.py       # adapter REST verificado contra la documentación
    mock.py             # proveedor simulado (tests y modo sin clave)
  ui/                   # páginas Streamlit
tests/                  # pytest: 122 tests, sin red y sin cargos
```

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `HF_API_KEY` | — | Clave de Higgsfield (`key-id:key-secret`). Sin ella → modo mock. |
| `VISUALFORGE_PROVIDER` | auto | `mock` o `higgsfield` (auto: higgsfield si hay clave). |
| `VISUALFORGE_CONCURRENCY` | `2` | 1–8 jobs en paralelo. |
| `VISUALFORGE_PROJECTS_DIR` | `projects/` | Ruta de proyectos (relativa a la raíz o absoluta). |
| `VISUALFORGE_MAX_REF_MB` | `25` | Límite local por foto de referencia. |

## Desarrollo

```bash
pytest            # 122 tests
ruff check .
```

## Seguridad

- Las claves de API viven en `.env` (ignorado por git) o se introducen en sesión local.
- Nunca subir `.env`, claves ni fotos de clientes a este repositorio público.
- Los proyectos (`projects/`) y sus imágenes quedan fuera de git por diseño.
- El manifiesto de lote no incluye claves ni URLs firmadas de subida.
