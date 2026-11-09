# Genesis VisualForge

Herramienta **local** para generar lotes de imágenes profesionales para webs y landings mediante la API de [Higgsfield](https://open.higgsfield.ai).

Issue: [GEN-11](https://linear.app/genesisnavarra/issue/GEN-11) · Rama: `feat/GEN-11-genesis-visualforge-mvp`

## Inicio rápido

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows; en Unix: source .venv/bin/activate
pip install -e ".[dev]"
copy .env.example .env        # pega tu HF_API_KEY completa; para API real: VISUALFORGE_PROVIDER=higgsfield
streamlit run app.py
```

La app arranca en **modo mock** (generación simulada, sin coste), incluso con clave cargada: la
selección del proveedor real siempre es deliberada (ver «Mock vs Higgsfield API»).

## Mock vs Higgsfield API (GEN-30)

| Estado | Activación | Créditos | Validación de clave |
|---|---|---|---|
| `VISUALFORGE_PROVIDER=mock` (por defecto al copiar `.env.example`) | explícita en `.env` | no consume | no se envía nada |
| `VISUALFORGE_PROVIDER=higgsfield` | explícita en `.env` | consume créditos reales | los errores 401/403/429 indican clave rechazada/sin créditos/límite |

- Pega la **credencial completa tal y como la copias** del panel de Higgsfield, sin separarla ni
  modificarla (no se exige el formato `key-id:key-secret`): ver [quick-start](https://open.higgsfield.ai/quick-start).
- Tener `HF_API_KEY` en `.env` **no activa** la API real ni equivale a acceso validado.
- Toda la app muestra siempre una insignia con el proveedor activo; si la clave cargada no está
  siendo usada por estar en `mock`, lo avisa explícitamente.

## Qué hace

1. **Proyectos** — un proyecto por cliente: brief, dirección de arte (prompt base) y fotos de referencia.
2. **Prompts** — importación masiva «uno por línea» (`Nombre :: prompt` o solo el prompt) + editor de tabla con campo «mantener» por asset.
3. **Lote** — modelo (`marketing-studio/image` o `.../flare`), resolución, ratio, calidad y concurrencia **por lote** (selector 1–4, por defecto 2: lo elegido manda para ese lote; véase «Concurrencia: elegida por lote vs. arranque vs. techo global»). Estimación de coste antes de enviar. Cada imagen se envía como job independiente con su `idempotency_key` persistida.
4. **Resultados** — polling con backoff, cancelación real en el proveedor, reanudación tras reiniciar la app, regeneración selectiva por imagen (`slug-vN`) y exportación a `originals/` + `web/` (WebP/JPEG/PNG) + ZIP con `README-assets.md`, `manifest.json` y `prompts.json`.

## Garantías de ejecución

- **Concurrencia: elegida por lote vs. valor de arranque vs. techo global (GEN-15).**
  - El selector del lote (UI, **1–4**, por defecto **2**) manda para **ese lote**: con 4 se
    lanzan hasta 4 solicitudes simultáneas y el resto del lote espera en cola su turno,
    lanzándose automáticamente al liberarse capacidad (sin nuevos clics y sin límite de
    tamaño del lote).
  - `VISUALFORGE_CONCURRENCY` (**1–8**) es solo la **preferencia/valor de arranque**:
    sugiere el valor inicial del selector (recortado a 1–4 con `.env=8` o superior) y
    sirve de fallback si un lote carece de concurrencia válida. **Nunca** actúa como
    techo silencioso: `.env=2` + selector 4 ejecutará 4.
  - **Techo global de seguridad: 8 jobs activos** como máximo, sin importar cuántos
    lotes se ejecuten a la vez (la capacidad se reparte entre lotes sin penalizar
    cola, reintentos, polling, idempotencia ni cancelación).
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
  jobs.py               # BatchRunner: cola por lote, reintentos, cancelación, reanudación
  export.py             # optimización web (Pillow) y ZIP
  provider/
    base.py             # contrato ImageProvider
    higgsfield.py       # adapter REST verificado contra la documentación
    mock.py             # proveedor simulado (tests y modo sin clave)
  ui/                   # páginas Streamlit
tests/                  # pytest: suite completa, sin red y sin cargos
```

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `HF_API_KEY` | — | Credencial completa de Higgsfield, pegada tal cual (sin reformatear). Con `mock` no se envía. |
| `VISUALFORGE_PROVIDER` | `mock` | `mock` o `higgsfield`. La selección del proveedor real es siempre deliberada. |
| `VISUALFORGE_CONCURRENCY` | `2` | Preferencia/valor de arranque de concurrencia (1–8). No es el techo de un lote: el selector de la UI manda (1–4, default 2) y el límite de seguridad global del proceso es 8 jobs activos. |
| `VISUALFORGE_PROJECTS_DIR` | `projects/` | Ruta de proyectos (relativa a la raíz o absoluta). |
| `VISUALFORGE_MAX_REF_MB` | `25` | Límite local por foto de referencia. |

## Desarrollo

```bash
pytest            # suite completa (sin red y sin cargos)
ruff check .
```

## Seguridad

- Las claves de API viven en `.env` (ignorado por git) o se introducen en sesión local.
- Nunca subir `.env`, claves ni fotos de clientes a este repositorio público.
- Los proyectos (`projects/`) y sus imágenes quedan fuera de git por diseño.
- El manifiesto de lote no incluye claves ni URLs firmadas de subida.
