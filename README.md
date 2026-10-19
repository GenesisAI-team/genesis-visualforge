# Genesis VisualForge

Herramienta **local** para generar lotes de imágenes profesionales para webs y landings mediante la API de [Higgsfield](https://open.higgsfield.ai).

> Estado: desarrollo en curso (issue [GEN-11](https://linear.app/genesisnavarra/issue/GEN-11)).

## Inicio rápido

```bash
pip install -e ".[dev]"
cp .env.example .env   # introduce tu HF_API_KEY
streamlit run app.py
```

Documentación completa en breve: `README.md`, `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/WORKFLOW.md`.

## Seguridad

- Las claves de API viven en `.env` (ignorado por git) o se introducen en sesión local.
- Nunca subir `.env`, claves ni fotos de clientes a este repositorio público.
