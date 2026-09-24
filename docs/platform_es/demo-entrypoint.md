# Entrada de la API de demostración

`agents_system.demo` sirve la base de datos de la empresa ficticia a través del
adaptador compatible con OpenAI de la plataforma. Es la mitad de **servirlo** de
[`demo/load_demo_company.py`](../../demo/load_demo_company.py): primero cargá
los datos repetibles de demostración y después iniciá una API sobre ellos. Es
una demostración local manual, no una configuración de despliegue.

## 1. Cargá la base de datos de demostración

Iniciá el servicio local de Postgres y creá la base de datos temporal si hace
falta; después cargá su esquema, vistas y datos deterministas:

```bash
uv run python demo/load_demo_company.py
```

El loader rechaza objetos de bases de datos que no sean de demostración y es
destructivo deliberadamente solo para una base que él creó. Consultá
[`demo/README.md`](../../demo/README.md) para la configuración y los detalles
de seguridad de la base de datos.

## 2. Configurá el entorno

Toda la configuración de la entrada viene de variables de entorno (o del
archivo `.env` del proyecto). La URL de la base demo es independiente de
`DATABASE_URL` de la aplicación:

| Variable | Valor por defecto / requisito | Descripción |
|----------|-------------------------------|-------------|
| `DEMO_DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agents_system_demo` | Pool de conexión para la base demo cargada. |
| `EVAL_PROVIDER` | `ollama` | Proveedor del modelo de chat: `ollama`, `groq`, `anthropic` u `openai_compatible`. Es intencionalmente independiente de `ADAPTER_PROVIDER`. |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Modelo de Ollama cuando `EVAL_PROVIDER=ollama`. |
| `OLLAMA_BASE_URL` | opcional | URL del servidor Ollama; sin definir usa el valor por defecto del proveedor. |
| `GROQ_API_KEY` | obligatoria para Groq | Credencial usada cuando `EVAL_PROVIDER=groq`. |
| `ANTHROPIC_API_KEY` | obligatoria para Anthropic | Credencial usada cuando `EVAL_PROVIDER=anthropic`. |
| `OPENAI_COMPATIBLE_BASE_URL` | obligatoria para `openai_compatible` | URL base de un endpoint de chat compatible con OpenAI. |
| `OPENAI_COMPATIBLE_MODEL` | obligatoria para `openai_compatible` | Identificador del modelo solicitado a ese endpoint. |
| `OPENAI_COMPATIBLE_API_KEY` | opcional | Credencial para ese endpoint; omitila para servidores locales sin clave. |
| `DEMO_HOST` | `127.0.0.1` | Host de enlace HTTP. |
| `DEMO_PORT` | `8000` | Puerto de enlace HTTP. |
| `ADAPTER_RUNTIMES` | obligatoria para exponer un rol | Lista JSON de IDs de runtime publicados, como `["_generic__sales-agent"]`. Vacía (el valor por defecto) no expone modelos en `/v1/*`. |
| `ADAPTER_API_KEY` | obligatoria al publicar un rol | Token Bearer requerido por `/v1/*`. |

Por ejemplo, elegí un proveedor y definí sus variables; después publicá un rol
genérico sin guardar valores de credenciales en el control de versiones:

```bash
export EVAL_PROVIDER=openai_compatible
export OPENAI_COMPATIBLE_BASE_URL=https://your-compatible-endpoint.example/v1
export OPENAI_COMPATIBLE_MODEL=your-model-id
export OPENAI_COMPATIBLE_API_KEY="$YOUR_PROVIDER_API_KEY"
export ADAPTER_RUNTIMES='["_generic__sales-agent"]'
export ADAPTER_API_KEY="$YOUR_DEMO_ADAPTER_API_KEY"
```

`ADAPTER_RUNTIMES` y `ADAPTER_API_KEY` son los que efectivamente publican y
protegen un rol en `/v1/*`; esta entrada no los define. El modelo de permisos
en progreso además requerirá grants explícitos con `DEPLOY_GRANTS` cuando esté
listo. No anticipes esa forma futura de configuración acá.

## 3. Corrélo

```bash
uv run python -m agents_system.demo
```

## 4. Llamá a la API compatible con OpenAI

Con un rol publicado y el servidor corriendo, listá los modelos disponibles:

```bash
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $ADAPTER_API_KEY"
```

Después enviá una completitud de chat a uno de esos IDs de modelo:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $ADAPTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "_generic__sales-agent",
    "messages": [{"role": "user", "content": "What items are available?"}]
  }'
```

El proveedor del modelo y la base demo son decisiones locales del operador. No
corras este recorrido contra una base de producción ni expongas el servidor
local a una red no confiable.
