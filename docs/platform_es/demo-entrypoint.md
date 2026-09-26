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
| `AGENT_REGISTRATIONS` | obligatoria para servir un rol | Objeto JSON que mapea cada ID de runtime que elegís al rol predefinido que sirve, como `"{role}"` o `"{role}@{client}"`, por ejemplo `{"demo-sales-agent": "sales-agent"}`. El ID es opaco: nada lo parsea. Una entrada malformada hace fallar el arranque, nombrándola. |
| `ADAPTER_RUNTIMES` | obligatoria para exponer un rol | Lista JSON de los IDs de runtime registrados a publicar, como `["demo-sales-agent"]`. Vacía (el valor por defecto) no expone modelos en `/v1/*`. |
| `ADAPTER_API_KEY` | obligatoria al publicar un rol | Token Bearer requerido por `/v1/*`. |
| `DEPLOY_GRANTS` | obligatoria para cada runtime registrado | Objeto JSON que mapea cada ID registrado a la lista de nombres de permisos efectivamente otorgados, por ejemplo `{"demo-sales-agent": ["read:catalog"]}`. Un runtime registrado sin una entrada correspondiente hace fallar el arranque de forma explícita, nombrando ese runtime. |

Por ejemplo, elegí un proveedor y definí sus variables; después publicá un rol
genérico sin guardar valores de credenciales en el control de versiones:

```bash
export EVAL_PROVIDER=openai_compatible
export OPENAI_COMPATIBLE_BASE_URL=https://your-compatible-endpoint.example/v1
export OPENAI_COMPATIBLE_MODEL=your-model-id
export OPENAI_COMPATIBLE_API_KEY="$YOUR_PROVIDER_API_KEY"
export AGENT_REGISTRATIONS='{"demo-sales-agent": "sales-agent"}'
export ADAPTER_RUNTIMES='["demo-sales-agent"]'
export DEPLOY_GRANTS='{"demo-sales-agent": ["read:catalog", "read:client_registry", "write:orders", "write:order_items", "read:price_lists", "send:message"]}'
export ADAPTER_API_KEY="$YOUR_DEMO_ADAPTER_API_KEY"
```

`AGENT_REGISTRATIONS` construye el runtime, y `ADAPTER_RUNTIMES` y
`ADAPTER_API_KEY` son los que efectivamente lo publican y protegen en
`/v1/*`; esta entrada no define ninguno por defecto. El arranque también
requiere una entrada explícita de `DEPLOY_GRANTS` para cada runtime
registrado (issue #38) — si falta, el lifespan de `main.py` lanza
`DefinitionError` nombrando ese runtime. Los IDs de runtime con la vieja forma
`{deployment}__{role}` ya no se parsean; ver
[cómo migrar](deployment.md#migrar-desde-los-ids-de-runtime-viejos). El
ejemplo de arriba le otorga a `demo-sales-agent` su conjunto completo de
permisos declarados
(`platform/roles/sales-agent/manifest.md`): `read:catalog`,
`read:client_registry`, `write:orders`, `write:order_items`,
`read:price_lists`, `send:message`. `sales-agent` declara
`untrusted_input: true` (`platform/roles/sales-agent/policy.md`), y ninguno de
esos seis permisos es T3 (como `exec:command` o `read:files`), así que otorgar
su conjunto completo acá nunca le da un permiso T3 a un rol de entrada no
confiable (ADR-002 C.11/C.13).

## 3. Cumplí las dos verificaciones de seguridad de arranque

`uv run python -m agents_system.demo` arranca a través de `create_app`, y se
niega a iniciar salvo que se cumplan dos cosas. Ambas se aplican de forma
fail-closed (cierran en caso de duda):

1. **Un secreto de webhook no vacío.** El lifespan de `create_app` ejecuta
   `Settings.validate_security_fail_closed` al arrancar, que lanza un error
   si `META_WEBHOOK_SECRET` está vacío — una clave HMAC vacía hace que las
   firmas de webhook sean falsificables. Definila con cualquier valor no
   vacío para una corrida de demo local; no necesita ser un secreto real de
   Meta, ya que esta entrada nunca recibe webhooks de WhatsApp:

   ```bash
   export META_WEBHOOK_SECRET=demo-local-no-es-un-secreto-real
   ```

2. **Un rol de `DEMO_DATABASE_URL` genuinamente de solo lectura.** Antes de
   servir nada, `main()` verifica que el rol detrás de `DEMO_DATABASE_URL`
   tenga `default_transaction_read_only = on` — la misma verificación que la
   plataforma corre para `BI_DATABASE_URL`. Un rol que puede escribir es
   rechazado. Creá un rol dedicado de solo lectura para la base demo:

   ```sql
   CREATE ROLE agents_system_demo_ro LOGIN PASSWORD 'cambiame';
   GRANT CONNECT ON DATABASE agents_system_demo TO agents_system_demo_ro;
   GRANT USAGE ON SCHEMA public TO agents_system_demo_ro;
   GRANT SELECT ON agents_system_customers, agents_system_sales,
     agents_system_sale_items, agents_system_stock
     TO agents_system_demo_ro;
   ALTER ROLE agents_system_demo_ro SET default_transaction_read_only = on;
   ```

   Después apuntá `DEMO_DATABASE_URL` a ese rol en lugar de la conexión por
   defecto con el superusuario `postgres`, por ejemplo
   `postgresql+asyncpg://agents_system_demo_ro:cambiame@127.0.0.1:5432/agents_system_demo`.

`ALLOW_INSECURE=true` evita las dos verificaciones, pero es el martillo más
grande: *también* permite que `ADAPTER_RUNTIMES` se configure sin
`ADAPTER_API_KEY` (un `/v1/*` abierto y sin autenticar). Preferí
`META_WEBHOOK_SECRET` junto con un rol genuinamente de solo lectura para una
corrida de demo, y dejá `ALLOW_INSECURE=true` como alternativa para desarrollo
local cuando eso resulte inconveniente:

```bash
export ALLOW_INSECURE=true
```

## 4. Corrélo

```bash
uv run python -m agents_system.demo
```

## 5. Llamá a la API compatible con OpenAI

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
    "model": "demo-sales-agent",
    "messages": [{"role": "user", "content": "What items are available?"}]
  }'
```

El proveedor del modelo y la base demo son decisiones locales del operador. No
corras este recorrido contra una base de producción ni expongas el servidor
local a una red no confiable.
