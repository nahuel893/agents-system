# Canales: agregar un canal de mensajería detrás del puerto

Issue #22. Un **canal** es por donde una conversación llega a la plataforma y
por donde sale la respuesta: WhatsApp hoy, Telegram o un proveedor de SMS
mañana. Un canal hace tres cosas y nada más:

1. **Autenticar** una entrega entrante (Meta firma con un header HMAC; otro
   proveedor puede enviar un token secreto compartido).
2. **Normalizar** el payload en los mensajes que acepta el inbox durable, y
   después en el único turno que ejecuta el agente.
3. **Enviar** una respuesta de vuelta.

Todo lo que hay entre esos extremos es agnóstico del canal y pertenece a la
plataforma: la lectura acotada del body, la aceptación durable y el dedup, el
worker diferido, la búsqueda del participante, ejecutar el turno, registrarlo,
los reintentos y las fallas terminales. El puerto es
`src/agents_system/channels/port.py`; el canal WhatsApp incorporado
(`src/agents_system/channels/whatsapp.py`) es su primer adaptador. Agregar un
canal no edita ningún módulo de la plataforma.

---

## El puerto

```python
class ChannelAdapter(Protocol):
    def verify_inbound(self, body: bytes, headers: Headers) -> None: ...
    def read_inbound(self, body: bytes) -> InboundDelivery: ...
    def extract_turn(
        self, payload: dict[str, Any], message_id: str
    ) -> InboundTurn | None: ...
    async def send_text(self, to: str, body: str) -> None: ...
```

| Método | Corre | Contrato |
|---|---|---|
| `verify_inbound` | En el request, sobre el body crudo, después del techo de tamaño y **antes de parsear o persistir nada** | Rechaza lanzando `fastapi.HTTPException` (normalmente 403). Compara secretos en tiempo constante (`hmac.compare_digest` / `secrets.compare_digest`). |
| `read_inbound` | En el request, solo después de que `verify_inbound` pasó | Devuelve `InboundDelivery(payload, messages)`. `payload` (un dict serializable a JSON) se guarda junto a cada mensaje; `messages` es un `InboundRef(message_id, conversation_key)` por mensaje. Sin mensajes significa "reconocer, no persistir nada" (un callback de estado, un body malformado). No debe lanzar ante entradas con forma de ataque. |
| `extract_turn` | En el worker diferido, una vez por mensaje guardado | Dado el `payload` guardado y el id propio del mensaje, devuelve `InboundTurn(message_id, sender, text)`, o `None` para completarlo sin turno (un mensaje que no es texto, por ejemplo). |
| `send_text` | En el worker diferido | Envía `body` a `to` (el remitente, normalizado para este canal por el directorio de participantes de la implementación). Lanza ante una falla: el worker reintenta. La entrega es **al menos una vez** —un envío ambiguo puede repetirse—. |

Reglas en las que se apoya la plataforma:

- `message_id` es el id propio del canal para el mensaje. El inbox deduplica
  sobre él, acotado al canal (guardado como `<canal>:<id>`), así un proveedor
  que reentrega el mismo id se acepta una sola vez. Debe ser estable entre
  reentregas.
- `conversation_key` es lo que debe procesarse en orden de llegada —
  normalmente el remitente—. Nunca debe estar vacío; un mensaje sin remitente
  utilizable necesita una clave propia (WhatsApp usa `unresolved:<id>`) para
  no bloquear a nadie.
- Ninguno de los métodos de entrada puede tocar la base de datos: la
  aceptación es de la plataforma.

Un adaptador cuyo proveedor verifica la URL del webhook con un GET (Meta lo
hace) también implementa `SubscriptionHandshake.verify_subscription(params) ->
str`; para cualquier otro canal, `GET /webhook/{nombre}` responde 405.

El handshake de WhatsApp compara `hub.verify_token` con `WHATSAPP_VERIFY_TOKEN`
en tiempo constante, sobre bytes UTF-8 (así que un token no ASCII es un 403
normal, nunca un fallo). La variable no tiene un valor por defecto utilizable:
con el canal de WhatsApp habilitado (`WHATSAPP_RUNTIME_ID` definido), un
`WHATSAPP_VERIFY_TOKEN` vacío detiene el arranque, porque un token configurado
vacío coincidiría con un request que envía un `hub.verify_token` vacío o
ausente. El adaptador además rechaza por sí mismo un token configurado vacío,
sea lo que sea que haya dejado pasar el chequeo de arranque
(`ALLOW_INSECURE=true` omite solo el chequeo de arranque, para desarrollo
local).

## Registrar un canal

```python
from agents_system.channels.port import Channel
from agents_system.main import create_app

app = create_app(
    registry_factory=my_registry_factory,
    agents={"support": "support-agent"},
    grants={"support": ["read:catalog"]},
    channels={"telegram": Channel(adapter=TelegramChannel(...), runtime_id="support")},
)
```

- El canal se sirve en `POST /webhook/telegram` (y `GET` si tiene handshake).
  `/webhook` sin nombre sigue siendo el canal WhatsApp incorporado;
  `/webhook/whatsapp` lo alcanza por nombre.
- Un nombre tiene de 1 a 32 letras minúsculas, dígitos, `_` o `-`, y empieza
  con una letra. `whatsapp` está reservado. Se guarda en
  `webhook_inbox.channel`, así que hay que tratarlo como permanente una vez
  que se aceptaron mensajes con él.
- `runtime_id` nombra el runtime registrado que responde al canal. El arranque
  falla salvo que esté registrado, resuelva `untrusted_input: true` (ADR-002
  C.13: un canal de webhook entrega entrada externa no confiable) y su
  presupuesto de ejecución deje 60 segundos de margen dentro del lease del
  outbox —los mismos controles que recibe `WHATSAPP_RUNTIME_ID`—.
- Un único worker diferido sirve a todos los canales. El `thread_id` del turno
  (y el hilo del recorder) es el remitente normalizado acotado al canal
  (`telegram:+54911...`), así dos canales nunca comparten una conversación.
  Se une al checkpointer compartido siempre que el proceso construya uno
  (`WHATSAPP_CHECKPOINTER_ENABLED`, que a pesar del nombre es el interruptor
  de todo el proceso).
- Registrar cualquier canal exige un directorio de participantes que conozca
  el canal (sección siguiente), o el arranque falla.

## La identidad del participante es por canal (requisito de seguridad)

Una dirección de remitente solo significa algo dentro del canal que la
autenticó. Los mismos dígitos pueden ser el teléfono de un cliente de WhatsApp
y, en otro canal, un id de chat o un valor que controla quien envía. Si un
único directorio resolviera los remitentes de todos los canales sin conocer el
canal, cualquiera que pudiera llegar a un segundo canal podría enviar el número
de un cliente existente y hacer que el turno se ejecute y se registre con el id
de participante de ese cliente.

Por eso al directorio siempre se le dice el canal:

```python
class ChannelParticipantDirectory(Protocol):
    def normalize_channel_address(self, raw: str, *, channel: str) -> str: ...
    async def resolve_channel_address(
        self, session: Any, address: str, *, channel: str
    ) -> Participant | None: ...
```

- La implementación decide, de forma explícita y por canal, a qué identidad
  pertenece una dirección. Hay que mantener los canales en espacios de
  identidad separados salvo que la implementación tenga su propia prueba de
  que una dirección en un canal es la misma persona en otro. Por lo demás el
  contrato es el de `ParticipantDirectory`: un `ValueError` al normalizar
  descarta la entrega, y la resolución es dueña de la transacción de su sesión.
- Un `ParticipantDirectory` común, que no conoce el canal, sigue funcionando
  en una implementación solo de WhatsApp: WhatsApp es el espacio de
  direcciones para el que se escribió.
- Un worker ligado a **cualquier** otro canal rechaza un directorio que no
  conoce el canal. El lifespan construye ese worker, así que
  `create_app(channels=...)` con un `participant_directory` que no conoce el
  canal falla en el arranque, nombrando los canales.

## La configuración pertenece al adaptador

La configuración de un canal —su token, su secreto, su cliente HTTP— son los
argumentos del constructor del adaptador (o su propia clase
`pydantic-settings` con su propio prefijo de entorno). No se agrega nada a
los `Settings` planos de la plataforma, y `config.py` nunca se edita para
agregar un canal.

El canal WhatsApp incorporado sigue la misma regla en el código:
`WhatsAppChannelSettings.from_settings(settings)` proyecta la porción de
WhatsApp (`META_WEBHOOK_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_GRAPH_API_URL`, `WHATSAPP_RUNTIME_ID`,
`WHATSAPP_CHECKPOINTER_ENABLED`) y el adaptador lee solo esa vista. Las
variables de entorno conservan sus nombres: separarlas en un modelo de
configuración anidado renombraría cada variable desplegada sin ningún
beneficio de comportamiento, así que la "separación de settings" de la que
dependía el issue original se resuelve como esta vista y no como una ruptura
de configuración. Quedan dos restos, anotados pero sin cambiar:
`META_PHONE_NUMBER_ID` y `META_ACCESS_TOKEN` están declarados pero ningún
código los lee, y `webhook_max_body_bytes` / `webhook_worker_*` son,
correctamente, agnósticos del canal.

## Límites

- **Solo texto.** `InboundTurn` lleva texto; los medios quedan fuera de
  alcance, igual que para WhatsApp.
- **Un único directorio de participantes.** La implementación pasa un solo
  directorio, y sirve a todos los canales. Se le dice el canal de cada
  dirección (ver arriba), así que un canal cuyas direcciones no son números de
  teléfono se atiende con la lógica por canal de ese directorio.
- **No es para APIs de request/respuesta.** El adaptador compatible con
  OpenAI (`/v1/chat/completions`, Open WebUI) responde en la misma respuesta
  HTTP; no tiene webhook entrante, ni traspaso durable, ni envío saliente, así
  que no es un canal en este sentido y queda donde está.

## Prueba

`tests/test_channel_port.py` define un canal falso solo en el archivo de
test, lo registra con `create_app(channels=...)` y conduce un turno completo
por la ruta real, el `accept_inbound_message` real, el worker diferido real y
un `AgentRuntime` real construido por el harness (solo el SQL del outbox y el
proveedor del modelo son en memoria). El mismo archivo prueba que al
directorio se le dice el canal de cada remitente, y que un directorio que no
conoce el canal se rechaza para un segundo canal al construir el worker y en
el arranque. Los `tests/test_webhook.py` y `tests/test_webhook_worker.py` sin
cambios fijan el comportamiento de WhatsApp, incluido el directorio que no
conoce el canal.

> Espejo en inglés: `docs/platform/channel.md` (mantener ambos al día juntos).
