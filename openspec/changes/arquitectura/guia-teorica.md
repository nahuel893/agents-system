# Guía Teórica Fundamental: Arquitectura de un Agente de Ventas por WhatsApp

*Una guía para desarrolladores que quieren entender el POR QUÉ antes del CÓMO*

---

## PARTE 1: Ajustes Críticos

---

### Tema 1: Vector Indexes — HNSW vs IVFFlat

**¿Qué es?**

Imaginá que tenés un catálogo de 5.000 productos y un cliente te escribe "quiero algo para el dolor de cabeza". No podés buscar eso con un `WHERE nombre LIKE '%dolor de cabeza%'` porque el producto se llama "Ibuprofeno 400mg". Necesitás una búsqueda que entienda el **significado**, no las palabras exactas. Acá es donde entran los **vectores** y los **índices vectoriales**.
Un **vector** es una lista de números que representa el significado de un texto en un espacio matemático. "Ibuprofeno 400mg" y "algo para el dolor de cabeza" terminan siendo listas de números que están *cerca* una de la otra en ese espacio. Un **índice vectorial** es una estructura de datos que te permite encontrar los vectores más cercanos a uno dado de forma eficiente, sin tener que comparar contra todos los demás uno por uno.

Sin un índice, buscar el vecino más cercano entre 50.000 vectores requiere 50.000 comparaciones. Eso es **búsqueda exhaustiva** (fuerza bruta). Con un catálogo chico funciona. Con uno grande, es inaceptable en tiempo real.

**Analogía**

Pensá en una biblioteca enorme. La búsqueda exhaustiva es recorrer TODOS los estantes de TODOS los pisos buscando un libro. **IVFFlat** es como dividir la biblioteca en secciones temáticas: "Ciencia", "Historia", "Ficción". Cuando buscás algo, primero decidís en qué sección(es) buscar, y después recorrés solo esas. **HNSW** es como tener amigos en la biblioteca: le preguntás a uno que sabe mucho y te dice "hablá con Marta en el tercer piso", Marta te dice "hablá con Juan en el estante 7", y Juan te lleva al libro exacto. Es una cadena de contactos cada vez más específicos.

**¿Cómo funciona por dentro?**

**IVFFlat** (Inverted File with Flat compression) funciona en dos fases. Primero, durante la **construcción del índice**, toma todos los vectores y los agrupa en **clusters** usando un algoritmo como k-means. Cada cluster tiene un **centroide** (el punto central del grupo). Esto genera una partición del espacio similar a las **celdas de Voronoi**: cada región del espacio "pertenece" al centroide más cercano. Segundo, durante la **búsqueda**, toma el vector de consulta, encuentra los centroides más cercanos, y busca exhaustivamente solo dentro de esos clusters. El parámetro **nprobe** controla en cuántos clusters buscar: más probes = más precisión pero más lento.

El problema fundamental de IVFFlat es que necesita suficientes datos para generar clusters significativos. Con pocos datos (digamos, menos de 10.000 vectores), los clusters son ruidosos y la calidad de búsqueda se degrada. Además, si los datos cambian mucho, hay que **reconstruir** el índice periódicamente porque los centroides quedan desactualizados.

**HNSW** (Hierarchical Navigable Small World) funciona de una manera completamente distinta. Imaginá una estructura de capas, como un edificio. En el piso más alto hay muy pocos nodos — los "hubs" más conectados. En el piso de abajo hay más nodos. Y en la planta baja están TODOS los nodos. Cada nodo tiene conexiones con sus vecinos en su capa y con nodos en la capa inferior.

Cuando buscás, empezás en la capa más alta y hacés una **búsqueda greedy** (avara): vas al vecino más cercano a tu objetivo, y desde ahí al siguiente más cercano, hasta que no podés mejorar. Entonces bajás una capa y repetís con más detalle. Es como hacer zoom: primero ubicás la zona general, después el barrio, después la calle, después la casa.

La genialidad es que esta estructura se inspira en las **skip lists** (listas con saltos), donde tenés "autopistas" en las capas superiores para moverte rápido, y "calles locales" en las capas inferiores para precisión. El tradeoff es que HNSW usa más memoria (tiene que almacenar todas esas conexiones) y la construcción del índice es más lenta, pero la búsqueda es consistentemente rápida sin importar el tamaño de los datos.

El concepto de **recall** es clave acá. Como ambos son métodos de **búsqueda aproximada del vecino más cercano** (ANN — Approximate Nearest Neighbor), no garantizan encontrar EL más cercano, sino uno muy cercano. El recall mide qué porcentaje de los verdaderos vecinos más cercanos encontró el algoritmo. Un recall de 0.95 significa que 95 de cada 100 veces encontró el resultado correcto. HNSW generalmente logra recall más alto que IVFFlat con la misma velocidad.

**¿Por qué nos importa en este proyecto?**

En agents-badie, el catálogo de productos probablemente empiece con cientos o pocos miles de productos, no millones. Con ese volumen, IVFFlat no tiene suficientes datos para formar clusters útiles. HNSW, en cambio, funciona bien desde el primer vector. Además, un bot de ventas necesita respuestas rápidas y precisas — si le recomendás el producto equivocado a un cliente, perdés la venta. El recall importa mucho más acá que en, digamos, un sistema de recomendaciones donde "parecido" alcanza.

**pgvector** es la extensión de PostgreSQL que hace posible almacenar vectores y crear estos índices directamente en tu base de datos relacional, sin necesidad de un servicio separado como Pinecone o Weaviate. La ventaja es operacional: una base de datos menos que mantener.

> **Dato clave**: HNSW es la elección correcta cuando tu dataset es chico-mediano, necesitás alto recall, y podés pagar el costo extra de memoria. IVFFlat brilla con datasets grandes donde el costo de memoria de HNSW se vuelve prohibitivo.

**Conceptos relacionados que necesitás conocer**

- **k-means clustering**: Algoritmo que agrupa datos en k clusters minimizando la distancia al centroide. Es la base de IVFFlat.
- **Skip lists**: Estructura de datos probabilística con múltiples niveles que permite búsqueda en O(log n). Es la inspiración de las capas de HNSW.
- **Approximate Nearest Neighbor (ANN)**: Familia de algoritmos que sacrifican precisión perfecta por velocidad. Tanto HNSW como IVFFlat son ANN.
- **Dimensionality curse**: A medida que aumentan las dimensiones de los vectores, la noción de "distancia" se degrada — todos los puntos parecen estar a distancia similar. Esto hace que los índices vectoriales sean aún más necesarios.

---

### Tema 2: Embeddings y Modelos de Embedding

**¿Qué es?**

Un **embedding** es la representación numérica del significado de un texto. Es una lista (vector) de números decimales — por ejemplo, 1536 números — que captura las relaciones semánticas entre conceptos. La idea central es que textos con significado similar producen vectores que están cerca en el **espacio vectorial**, que es simplemente el espacio matemático donde viven estos vectores.

¿Por qué necesitamos esto? Porque las computadoras no entienden significado — entienden números. Un embedding es el puente entre el lenguaje humano y la matemática que permite buscar por significado.

**Analogía**

Pensá en un mapa. Cada ciudad tiene coordenadas (latitud, longitud) — dos números que representan su ubicación. Ciudades cercanas tienen coordenadas similares. Un embedding es lo mismo pero para significado: cada texto tiene "coordenadas" en un espacio de significado. "Zapatillas para correr" y "calzado deportivo running" están tan cerca en ese mapa semántico como Buenos Aires y Avellaneda en el mapa geográfico. Solo que en vez de 2 coordenadas, tenés 1536 (o las que use tu modelo).

**¿Cómo funciona por dentro?**

Un **modelo de embedding** es una red neuronal entrenada para producir estos vectores. Durante el entrenamiento, el modelo aprende que "rey" y "monarca" deberían estar cerca, y que "rey" y "banana" deberían estar lejos. Lo fascinante es que estas relaciones se vuelven operaciones matemáticas: el famoso ejemplo es que `vector("rey") - vector("hombre") + vector("mujer") ≈ vector("reina")`.

La **similitud coseno** (cosine similarity) es la métrica que usamos para medir qué tan parecidos son dos vectores. En vez de medir la distancia euclidiana (la "línea recta" entre dos puntos), mide el **ángulo** entre los vectores. Si dos vectores apuntan en la misma dirección, el coseno del ángulo es 1 (idénticos). Si apuntan en direcciones opuestas, es -1 (opuestos). Si son perpendiculares, es 0 (sin relación). ¿Por qué coseno y no distancia? Porque la magnitud (largo) del vector no nos importa — nos importa la dirección. Un texto más largo podría producir un vector de mayor magnitud sin ser más relevante.

Las **dimensiones** son la cantidad de números en cada vector. Más dimensiones = más capacidad para capturar matices semánticos, pero también más memoria, más cómputo para comparar, y potencialmente más ruido. Es el clásico tradeoff entre expresividad y eficiencia.

Acá entra **Matryoshka Representation Learning (MRL)**, una técnica elegante. Normalmente, si un modelo produce vectores de 1536 dimensiones, necesitás las 1536 para que funcionen bien. MRL entrena el modelo de forma que las primeras N dimensiones ya contengan la información más importante, como las muñecas rusas (matryoshkas) — la muñeca más chica ya es completa en sí misma. Podés truncar el vector a 512 o 256 dimensiones y seguir teniendo una representación útil, solo con menos detalle. Esto es enorme para performance: menos dimensiones = índices más chicos, búsquedas más rápidas, menos almacenamiento.

Sobre los modelos específicos: **text-embedding-3-small** de OpenAI produce vectores de 1536 dimensiones (truncables gracias a MRL), fue entrenado con un enfoque generalista, y es barato. **voyage-3** de Voyage AI está optimizado para dominios específicos como código o documentos técnicos, y tiene architectural differences en cómo procesa el texto (diferentes estrategias de tokenización y atención). La elección depende de tu caso de uso y presupuesto.

**Re-embedding** significa volver a generar todos los vectores de tu base de datos cuando cambiás de modelo. ¿Por qué? Porque cada modelo produce un espacio vectorial diferente. El vector que text-embedding-3-small genera para "ibuprofeno" y el que genera voyage-3 NO son comparables — son coordenadas en mapas diferentes. Si cambiás de modelo, tenés que re-generar todos los vectores. No hay atajo.

**¿Por qué nos importa en este proyecto?**

En agents-badie, los embeddings son el corazón de la búsqueda de productos. Cuando un cliente escribe "necesito algo para la fiebre de mi nene", el sistema convierte eso en un vector y busca los productos más cercanos en el espacio vectorial. La calidad de los embeddings determina directamente si el bot encuentra el producto correcto. Un modelo con dimensiones reducidas (vía MRL) permite búsquedas más rápidas — crucial para un bot conversacional donde cada segundo de latencia es un cliente que se impacienta.

> **Dato clave**: Nunca mezcles vectores de modelos diferentes en la misma búsqueda. Si cambiás de modelo de embedding, tenés que re-procesar TODO el catálogo. Es costoso, así que elegí bien desde el principio.

**Conceptos relacionados que necesitás conocer**

- **Tokenización**: Cómo el modelo divide el texto en unidades antes de procesarlo. Afecta directamente la calidad del embedding.
- **Espacio latente**: El espacio de alta dimensionalidad donde viven los embeddings. "Latente" porque captura información que no es explícita en el texto original.
- **Similitud coseno vs distancia euclidiana**: Dos formas de medir cercanía entre vectores. Coseno ignora magnitud; euclidiana no. Para embeddings normalizados, son equivalentes.

---

### Tema 3: TTL (Time To Live) en Redis

**¿Qué es?**

**Redis** es una base de datos en memoria — todo vive en la RAM, lo que la hace extremadamente rápida (microsegundos por operación). Se usa para cosas que necesitan acceso ultrarrápido: caché, sesiones, contadores, colas. En el contexto de un bot conversacional, Redis guarda el **estado de la conversación**: qué dijo el usuario, qué respondió el bot, en qué paso del flujo está, qué tiene en el carrito.

**TTL** (Time To Live) es un temporizador que le ponés a cada dato: "este dato vive 30 minutos, después desaparece automáticamente". Es la forma en que Redis se limpia solo, sin que vos tengas que borrar datos manualmente.

**Analogía**

Redis es como un pizarrón en una cocina de restaurante. Los pedidos se escriben ahí porque el cocinero necesita verlos RÁPIDO — no va a buscar en un archivo. El TTL es como una regla del restaurante: "si un pedido lleva más de 30 minutos en el pizarrón sin que nadie lo toque, se borra". **TTL fijo** es exactamente eso: 30 minutos desde que se escribió, sin importar nada. **TTL deslizante** (sliding) es como decir "30 minutos desde la ÚLTIMA VEZ que alguien lo miró o modificó" — cada interacción reinicia el reloj.

**¿Cómo funciona por dentro?**

Redis implementa TTL de forma bastante directa. Cada clave puede tener un timestamp de expiración. Redis usa dos estrategias para limpiar claves expiradas: **lazy expiration** (cuando alguien intenta leer la clave, Redis verifica si expiró y la borra) y **active expiration** (periódicamente, Redis muestrea claves aleatorias y borra las expiradas). Esta combinación mantiene la memoria controlada sin gastar CPU constantemente.

Un **checkpointer** en el contexto de **LangGraph** (el framework de agentes que usa este proyecto) es el componente responsable de guardar el estado del grafo de ejecución después de cada nodo. Es como un save point en un videojuego: si algo falla, podés retomar desde el último checkpoint. El checkpointer usa Redis como backend, guardando el estado serializado con un TTL.

La **serialización de estado** es el proceso de convertir el estado en memoria (objetos de Python con sus atributos, listas, diccionarios) en una secuencia de bytes que se puede guardar en Redis. Cuando necesitás recuperar el estado, lo **deserializás** de vuelta a objetos. Formatos comunes son JSON (legible pero limitado en tipos de datos) y pickle/msgpack (binarios, más eficientes, soportan más tipos).

¿Qué pasa cuando el estado expira a mitad de una conversación? El cliente escribió hace 45 minutos, el TTL era de 30, y ahora manda otro mensaje. El checkpointer busca el estado en Redis, no lo encuentra, y el agente arranca desde cero — sin saber qué se habló antes, qué había en el carrito, nada. Para el cliente es una experiencia horrible: "ya te dije que quiero tres cajas de ibuprofeno" y el bot responde "¡Hola! ¿En qué puedo ayudarte?".

La diferencia entre **TTL fijo** y **TTL deslizante** es fundamental acá. Con TTL fijo de 30 minutos, una conversación que empezó hace 29 minutos expira en 1 minuto sin importar que el usuario esté activamente escribiendo. Con TTL deslizante, cada mensaje del usuario reinicia el contador. La conversación solo expira después de 30 minutos de **inactividad**, que es generalmente lo que querés.

**¿Por qué nos importa en este proyecto?**

Un bot de ventas por WhatsApp tiene un patrón de uso muy particular: el cliente escribe, se va a hacer otra cosa, vuelve 10 minutos después, agrega algo al carrito, se distrae de nuevo. La conversación puede durar una hora con largos silencios entre mensajes. Un TTL fijo de 30 minutos mataría muchas conversaciones legítimas. Un TTL deslizante de 30 minutos es mucho más apropiado: mientras el cliente siga interactuando, el estado se mantiene.

Pero ojo: el TTL deslizante tiene un costo. Cada interacción requiere una escritura adicional a Redis para resetear el timer. Con miles de conversaciones concurrentes, esas escrituras se suman. Y hay que definir qué hacer cuando SÍ expira: ¿se pierde todo? ¿Se persiste el carrito en PostgreSQL antes de expirar? Estas decisiones de diseño son críticas.

> **Dato clave**: TTL deslizante es casi siempre mejor para conversaciones. Pero necesitás un mecanismo de persistencia para los datos que NO deben perderse (como un carrito con productos) — el TTL controla la sesión conversacional, no los datos de negocio.

**Conceptos relacionados que necesitás conocer**

- **Serialización**: Convertir objetos en memoria a bytes y viceversa. JSON, MessagePack, Protocol Buffers, pickle son formatos comunes.
- **State machines**: Máquinas de estado que definen transiciones válidas. El estado que guardamos en Redis representa un punto en esa máquina.
- **Eviction policies**: Cuando Redis se queda sin memoria, tiene que decidir qué borrar. LRU (Least Recently Used), LFU (Least Frequently Used) son las más comunes. Diferente de TTL, que es proactivo.

---

### Tema 4: Procesamiento Sync vs Async de Tareas

**¿Qué es?**

Cuando un usuario manda un mensaje al bot, algunas cosas necesitan pasar AHORA (responderle) y otras pueden pasar DESPUÉS (actualizar analytics, sincronizar inventario, mandar una notificación al vendedor). La pregunta es: ¿hacemos todo en el mismo hilo de ejecución, o delegamos el trabajo pesado para no bloquear la respuesta?

Esto es fundamentalmente sobre **concurrencia**: la capacidad de manejar múltiples cosas al mismo tiempo. Y hay formas MUY diferentes de lograrla.

**Analogía**

Imaginá un restaurante. El mozo (**sync**) puede hacer una sola cosa a la vez: toma el pedido, va a la cocina, espera que lo preparen, vuelve con el plato. Mientras espera en la cocina, sus otras mesas esperan. Terrible.

Un mozo **async** toma el pedido, lo deja en la cocina, y MIENTRAS se prepara va a atender otras mesas. No está parado esperando. Cuando la cocina tiene el plato listo, le avisa y lo lleva. Esto es el **event loop**: un mozo que nunca se queda parado, siempre está atendiendo a alguien mientras espera que las cosas se cocinen.

Ahora, ¿qué pasa si una tarea no es "esperar que cocinen" sino "cortar 50 kilos de verdura"? Eso es **CPU-bound** — requiere trabajo activo, no espera. El mozo async no te sirve acá; necesitás otro cocinero (otro proceso). Esa es la diferencia entre **I/O-bound** (esperar respuestas de red, disco, APIs) y **CPU-bound** (cálculos, procesamiento pesado).

**¿Cómo funciona por dentro?**

Python tiene un **Global Interpreter Lock (GIL)** que impide que dos threads ejecuten código Python simultáneamente. Por eso la concurrencia en Python se basa en **asyncio** y el **event loop**. El event loop es un bucle infinito que revisa: "¿hay alguna tarea lista para avanzar?". Cuando una tarea hace un `await` (por ejemplo, esperar la respuesta de una API), le dice al event loop "no tengo nada que hacer hasta que llegue la respuesta" y el loop pasa a otra tarea. Cuando la respuesta llega, el loop retoma la tarea original.

**FastAPI** corre sobre **uvicorn**, un servidor ASGI (Asynchronous Server Gateway Interface). ASGI es el protocolo que permite manejar requests de forma asíncrona. Cada request entrante se convierte en una coroutine que el event loop maneja. Esto significa que FastAPI puede manejar miles de requests concurrentes con un solo proceso, siempre que las operaciones sean I/O-bound.

**BackgroundTasks** de FastAPI es el mecanismo más simple para diferir trabajo: le decís "cuando termines de responder, ejecutá esta función". Internamente, las tareas se ejecutan en el MISMO event loop, después de enviar la respuesta HTTP. Es simple, no requiere infraestructura extra, pero tiene limitaciones serias: si el proceso se cae, las tareas pendientes se pierden. Si una tarea tarda mucho o bloquea el event loop, afecta a TODOS los requests. No hay reintentos, no hay monitoring, no hay distribución de carga.

Un **task queue** como **Celery** resuelve todo esto. La idea es: en vez de ejecutar la tarea en el mismo proceso, la mandás a una cola. Un **worker** (otro proceso, potencialmente en otra máquina) la toma y la ejecuta. Si falla, se reintenta. Si el worker se cae, otro toma la tarea. La cola necesita un **message broker** — un intermediario que almacena los mensajes. **RabbitMQ** y **Redis** (sí, el mismo Redis) son los brokers más comunes.

La arquitectura es: `Productor → Broker → Worker`. El productor (tu aplicación FastAPI) encola la tarea. El broker la almacena hasta que un worker la toma. El worker la ejecuta y opcionalmente guarda el resultado en un **result backend**.

**¿Por qué nos importa en este proyecto?**

En agents-badie, cuando llega un mensaje de WhatsApp, el webhook necesita responder rápido (WhatsApp tiene timeouts). Procesar el mensaje con el agente LLM, buscar en el catálogo, actualizar el estado — todo eso lleva tiempo. Si hacés todo en el request handler, WhatsApp puede dar timeout y reenviar el mensaje (generando duplicados, que es el Tema 5).

Para el MVP, `BackgroundTasks` probablemente alcanza: procesás el mensaje del LLM en background y respondés al webhook inmediatamente. Pero a medida que crezca (más clientes simultáneos, más tareas pesadas, necesidad de reintentos), la migración a Celery o similar se vuelve necesaria.

> **Dato clave**: El procesamiento in-process (BackgroundTasks) se rompe cuando necesitás: reintentos confiables, distribución entre múltiples máquinas, o garantía de que las tareas no se pierden si el proceso se cae. Ese es el momento de agregar una cola de tareas.

**Conceptos relacionados que necesitás conocer**

- **Coroutines**: Funciones que pueden pausarse y resumirse. En Python, se definen con `async def` y se pausan con `await`.
- **Concurrencia vs Paralelismo**: Concurrencia es manejar múltiples tareas overlapping; paralelismo es ejecutarlas LITERALMENTE al mismo tiempo (múltiples CPUs). asyncio es concurrencia, multiprocessing es paralelismo.
- **Backpressure**: Qué pasa cuando la cola se llena más rápido de lo que los workers consumen. Sin manejo de backpressure, eventualmente te quedás sin memoria.

---

### Tema 5: Idempotencia de Webhooks y Deduplicación de Mensajes

**¿Qué es?**

Un **webhook** es una llamada HTTP que un servicio externo hace a tu servidor cuando algo pasa. En vez de que vos preguntes constantemente "¿hay mensajes nuevos?" (**polling**), WhatsApp te AVISA cada vez que llega uno. Es como la diferencia entre llamar al restaurante cada 5 minutos para preguntar si tu pedido está listo, versus que te manden un mensaje cuando lo está.

El problema es que WhatsApp (y cualquier proveedor de webhooks) puede mandarte el MISMO webhook más de una vez. Y si tu sistema no está preparado, procesa el mensaje dos veces: el bot responde dos veces, el producto se agrega al carrito dos veces, etc.

**Analogía**

Imaginate que trabajás en un banco y te llegan formularios de transferencia por correo. Un día te llegan dos sobres idénticos del mismo cliente pidiendo transferir $1.000. ¿Transferís $2.000 o $1.000? Si la operación es **idempotente**, el resultado es el mismo sin importar cuántas veces la ejecutes: transferís $1.000 una sola vez. El sobre duplicado no tiene efecto. Sin idempotencia, transferís $2.000 y el cliente te demanda.

**¿Cómo funciona por dentro?**

¿Por qué llegan duplicados? Por la garantía de entrega **at-least-once** (al menos una vez). Hay tres semánticas de entrega posibles:

- **At-most-once** (como máximo una vez): "mando el webhook y si se pierde, mala suerte". Nunca hay duplicados, pero podés perder mensajes.
- **At-least-once** (al menos una vez): "mando el webhook y si no recibo confirmación, lo reenvío". Nunca perdés mensajes, pero puede haber duplicados.
- **Exactly-once** (exactamente una vez): El santo grial. En la práctica, es casi imposible de lograr en sistemas distribuidos sin coordinación costosa.

WhatsApp usa at-least-once porque perder un mensaje es inaceptable. El mecanismo es: manda el webhook, espera un HTTP 200 en N segundos. Si no lo recibe (timeout de red, tu servidor tardó en responder, crash), asume que falló y **reintenta**. Tu servidor tal vez SÍ recibió y procesó el primer intento, pero la respuesta se perdió en el camino. Resultado: duplicado.

La solución es la **idempotencia**: hacer que procesar el mismo mensaje dos veces tenga el mismo efecto que procesarlo una. ¿Cómo? Con un **registro de deduplicación**. Cada webhook de WhatsApp tiene un ID único (`message_id`). Antes de procesar, verificás: "¿ya vi este ID?". Si sí, lo ignorás. Si no, lo procesás y registrás el ID.

Acá es donde entra **SET NX** de Redis (`SET key value NX EX ttl`). `NX` significa "set only if Not eXists" — solo guarda el valor si la clave NO existe. Es una operación **atómica**: no hay forma de que dos requests vean que la clave no existe y ambos la creen. Uno gana, el otro pierde. Esto resuelve las **race conditions** — la situación donde dos procesos simultáneos intentan hacer lo mismo y se pisan.

El flujo es:
1. Llega webhook con `message_id = "abc123"`
2. Ejecutás `SET dedup:abc123 1 NX EX 300` (guárdalo solo si no existe, con TTL de 5 minutos)
3. Si Redis responde OK → es nuevo, procesalo
4. Si Redis responde nil → ya lo procesaste, ignoralo

El TTL en la clave de deduplicación es importante: no podés guardar TODOS los IDs para siempre (consumirías memoria infinita). 5-10 minutos es suficiente porque los reintentos de WhatsApp pasan en segundos o minutos, no en horas.

Un **middleware** es código que se ejecuta antes (y/o después) de cada request. Poner la lógica de deduplicación en un middleware significa que TODOS los webhooks pasan por la verificación automáticamente, sin que cada endpoint tenga que implementarla. Es un punto de control centralizado.

**¿Por qué nos importa en este proyecto?**

En un bot de ventas, un mensaje duplicado puede significar: agregar un producto al carrito dos veces, enviar dos respuestas al cliente (confusión), o peor, confirmar un pedido dos veces. La deduplicación no es un nice-to-have — es obligatoria. Y como vimos en el Tema 4, si tu procesamiento async tarda más que el timeout de WhatsApp, VAS a recibir duplicados. Son compañeros inseparables.

> **Dato clave**: La combinación de at-least-once delivery + SET NX en Redis te da "effectively-once" processing. No es exactly-once teórico, pero es exactly-once práctico: el primer request gana, los duplicados se descartan atómicamente.

**Conceptos relacionados que necesitás conocer**

- **Operaciones atómicas**: Operaciones que se ejecutan completamente o no se ejecutan, sin estado intermedio visible. SET NX es atómica. Un "check then set" en dos pasos NO lo es.
- **Race condition**: Cuando el resultado depende del timing entre dos procesos concurrentes. Es uno de los bugs más difíciles de detectar y reproducir.
- **Idempotency key**: Un identificador que permite al servidor distinguir entre un reintento legítimo y una operación nueva. El `message_id` cumple esta función.

---

### Tema 6: Testing de Sistemas Basados en LLM

**¿Qué es?**

Testear software tradicional es relativamente directo: dado input X, esperás output Y. Si `suma(2, 3)` no devuelve `5`, el test falla. Pero cuando tu sistema incluye un LLM, el mismo input puede producir outputs diferentes cada vez. "¿Qué analgésicos tenés?" podría responderse como "Tenemos ibuprofeno y paracetamol" o "Contamos con varias opciones: ibuprofeno 400mg, paracetamol 500mg..." — ambas correctas, pero textualmente diferentes.

Esto rompe el paradigma tradicional de testing. Necesitás una estrategia completamente nueva.

**Analogía**

Testear software determinista es como evaluar un examen de matemáticas: hay UNA respuesta correcta. Testear sistemas con LLM es como evaluar un ensayo: hay múltiples respuestas válidas, lo que importa es si el contenido es correcto, coherente y relevante. No podés comparar con una plantilla — necesitás un evaluador que entienda el dominio.

**¿Cómo funciona por dentro?**

La **pirámide de testing** tradicional tiene tres niveles: muchos tests unitarios (rápidos, baratos), menos tests de integración, y pocos tests end-to-end (lentos, frágiles). Con sistemas de IA, esta pirámide se deforma. Los tests unitarios siguen aplicando para la lógica determinista (parseo, validación, routing), pero la capa del LLM necesita estrategias diferentes.

**Tests de golden files** (archivos de referencia) son un punto medio. Guardás un conjunto curado de pares input/output que representan el comportamiento esperado. No esperás match exacto — verificás propiedades: "la respuesta menciona al menos un producto", "no contiene información falsa", "incluye el precio". Son **assertions semánticas**, no textuales.

**LLM-as-judge** es una técnica donde usás un LLM para evaluar la calidad de la salida de otro LLM. Le das al juez: el input, la respuesta generada, y criterios de evaluación ("¿la respuesta es relevante? ¿Es correcta? ¿Es profesional?"). El juez devuelve un score. Suena circular, pero funciona sorprendentemente bien cuando el juez es un modelo más capaz o cuando los criterios son claros. La limitación es que tiene un costo por evaluación y puede tener sus propios sesgos.

**Testing parametrizado** (`pytest.mark.parametrize`) permite correr el mismo test con múltiples inputs sin duplicar código. Definís una tabla de casos: "si el usuario dice X, la respuesta debe contener Y" y pytest genera un test por cada fila. Es invaluable para cubrir muchos escenarios de conversación sin escribir 50 funciones de test.

La distinción entre **determinista** y **no-determinista** es crucial para decidir tu estrategia:
- Lógica de routing (¿a qué nodo va este mensaje?) → determinista → test exacto
- Parsing de cantidades ("dame 3 cajas" → 3) → determinista → test exacto
- Respuesta del LLM → no determinista → assertions semánticas o LLM-as-judge
- Orden de herramientas usadas → semi-determinista → test de secuencia

Las **fixtures** en testing (específicamente en pytest) son funciones que preparan el estado necesario para los tests. Una fixture puede crear una base de datos temporal, configurar un cliente Redis mock, o preparar un catálogo de productos de prueba. Se inyectan automáticamente en los tests que las necesitan. Son fundamentales para tests reproducibles.

**¿Por qué nos importa en este proyecto?**

En agents-badie tenés un agente conversacional completo con múltiples nodos, herramientas, y acceso a un catálogo. Testear solo el "happy path" no alcanza. Necesitás verificar que:
- El agente busca productos cuando debe (herramienta correcta)
- Los resultados de búsqueda son relevantes (calidad del embedding + LLM)
- El flujo conversacional es coherente (estado)
- Los edge cases no rompen nada (cantidades negativas, productos inexistentes, mensajes vacíos)

La combinación correcta es: tests unitarios para la lógica determinista (parseo, validación, router), tests parametrizados para cubrir variantes del catálogo, y LLM-as-judge para evaluar la calidad conversacional en tests de integración.

> **Dato clave**: No intentes hacer tests de igualdad textual contra salidas de LLM — vas a tener tests flaky que fallan por cambios cosméticos irrelevantes. Testeá PROPIEDADES y COMPORTAMIENTOS, no texto exacto.

**Conceptos relacionados que necesitás conocer**

- **Flaky tests**: Tests que a veces pasan y a veces fallan sin cambios en el código. Son el enemigo de la confianza en el suite de tests.
- **Mocking**: Reemplazar componentes reales (como la llamada al LLM) con versiones controladas para tests predecibles. Útil para la capa determinista, pero peligroso si mockeas demasiado y nunca testeas la integración real.
- **Regression testing**: Verificar que cambios nuevos no rompen funcionalidad existente. Especialmente importante cuando actualizás prompts — un cambio puede mejorar un caso y romper diez.

---

## PARTE 2: Gaps

---

### Tema 7: Estrategia de Testing (Pirámide para Sistemas Multi-Agente)

**¿Qué es?**

En el Tema 6 hablamos de cómo testear la interacción con LLMs. Pero un sistema multi-agente es más que eso: son múltiples componentes (agentes, herramientas, bases de datos, APIs externas) que interactúan entre sí. La pregunta acá es más amplia: ¿cómo organizás tu estrategia de testing para cubrir todos los niveles de un sistema así?

**Analogía**

Pensá en testear un edificio. Los tests unitarios son como verificar que cada ladrillo individual es sólido. Los tests de integración verifican que las paredes se sostienen cuando pegás los ladrillos. Los **contract tests** verifican que las puertas encajan en los marcos — que las interfaces entre componentes son compatibles. Y los tests end-to-end son como invitar gente a vivir y ver si el edificio funciona como vivienda. Necesitás TODOS estos niveles.

**¿Cómo funciona por dentro?**

Los **tests de contrato** (contract tests) verifican que dos componentes cumplen el acuerdo de interfaz entre ellos. En un sistema multi-agente, el "contrato" entre el agente de ventas y la herramienta de búsqueda de catálogo podría ser: "la herramienta recibe un string de búsqueda y devuelve una lista de objetos con campos nombre, precio y stock". Un contract test verifica que ambos lados respetan este acuerdo, sin necesidad de correr el sistema completo. Si alguien cambia el formato de salida de la herramienta, el contract test falla antes de que el error llegue a producción.

Los **tests de integración** en el contexto de agentes son diferentes a los tradicionales. No estás testeando que "el servicio A habla bien con la base de datos B". Estás testeando que una secuencia de pasos del agente produce el resultado correcto: "el usuario pide un producto → el agente invoca la herramienta de búsqueda → recibe resultados → formula una respuesta". Esto implica tener múltiples componentes corriendo, pero podés mockear los externos (la API de WhatsApp, el LLM) para mantener el test rápido y determinista.

Un **test harness** es un framework o estructura que envuelve a tu sistema para facilitar el testing. Incluye: fixtures para inicializar estado, utilities para simular mensajes de usuario, helpers para inspeccionar el estado del agente después de cada paso, y mecanismos para capturar las herramientas que el agente invocó. Es como un banco de pruebas en una fábrica: una estructura diseñada específicamente para poder testear el producto desde todos los ángulos.

Testear **máquinas de estado** requiere verificar: (1) que todas las transiciones válidas funcionan, (2) que las transiciones inválidas son rechazadas, y (3) que el estado se mantiene consistente. En un agente conversacional, la máquina de estado define flujos como: `saludo → búsqueda → carrito → checkout → confirmación`. Necesitás verificar que el agente no salta de `saludo` a `confirmación`, y que el estado del carrito se preserva correctamente entre transiciones.

**¿Por qué nos importa en este proyecto?**

agents-badie tiene un grafo de agentes con múltiples nodos, herramientas externas (búsqueda de catálogo, WhatsApp API), y estado persistente en Redis. Sin una estrategia de testing clara, vas a descubrir bugs en producción cuando un cliente real tenga una experiencia rota. Los contract tests son especialmente importantes porque el agente depende de interfaces bien definidas con sus herramientas — y esas interfaces van a evolucionar.

> **Dato clave**: En sistemas multi-agente, los contract tests entre agente y herramientas son tu primera línea de defensa. Son rápidos, deterministas, y atrapan el tipo de bugs que más frecuentemente causan regresiones.

**Conceptos relacionados que necesitás conocer**

- **Test doubles**: Mocks, stubs, fakes, spies — diferentes formas de reemplazar dependencias en tests. Cada una tiene un propósito diferente.
- **State explosion**: A medida que crecen los estados posibles, la cantidad de tests necesarios para cubrirlos crece exponencialmente. Necesitás técnicas como property-based testing para manejar esto.
- **Snapshot testing**: Guardar la salida de un test y comparar contra ella en ejecuciones futuras. Útil para detectar cambios inesperados en las respuestas del agente.

---

### Tema 8: Versionado de Prompts

**¿Qué es?**

Los prompts en un sistema con LLM son tan importantes como el código. Un cambio en el system prompt puede alterar completamente el comportamiento del agente: hacerlo más formal, más técnico, más propenso a errores, o más preciso. El **versionado de prompts** es la práctica de tratar los prompts como artefactos versionados, con historial de cambios, la posibilidad de comparar versiones, y la capacidad de hacer rollback si una nueva versión empeora las cosas.

**Analogía**

Imaginá que sos el director de un call center y tenés un guion que siguen los operadores. Cada vez que modificás el guion, algunos clientes tienen una experiencia mejor y otros peor. Sin versionado, no podés volver a la versión anterior, no sabés qué cambió ni cuándo, y no podés comparar métricas entre versiones. Es como editar un documento sin "Ctrl+Z" ni historial de cambios.

**¿Cómo funciona por dentro?**

El **prompt engineering** es el arte y ciencia de diseñar las instrucciones que le das al LLM. Un system prompt define la personalidad, restricciones, formato de respuesta, y conocimiento del agente. El problema es que los prompts no son estáticos: evolucionan constantemente. Descubrís que el agente es demasiado verboso, ajustás. Descubrís que no maneja bien las negaciones, ajustás. Cada ajuste puede tener efectos secundarios no previstos.

El **versionado** aplica los mismos principios que git aplica al código: cada versión tiene un identificador, un timestamp, y un diff contra la anterior. Se puede usar **semantic versioning** adaptado: major (cambio de comportamiento fundamental), minor (mejora sin romper), patch (corrección menor). Por ejemplo, pasar de "respondé siempre en español" a "respondé en el idioma del cliente" sería un major change.

Un **prompt registry** es un sistema centralizado donde vivien todas las versiones de todos los prompts, con metadata (autor, fecha, métricas de performance). Puede ser tan simple como una tabla en la base de datos o tan sofisticado como una plataforma dedicada (LangSmith, PromptLayer). El punto es que el prompt no vive hardcodeado en el código sino que es configurable y trazable.

El **A/B testing** de prompts es enviar un porcentaje de tráfico a cada versión y comparar métricas. El 50% de los usuarios recibe la versión A del prompt, el 50% la B. Después de suficiente tráfico, comparás: ¿cuál tiene más conversiones? ¿Cuál genera menos escalaciones a humanos? ¿Cuál tiene mejor satisfacción? Sin A/B testing, los cambios de prompt son basados en intuición.

**¿Por qué nos importa en este proyecto?**

En agents-badie, el prompt define cómo el bot vende. Un prompt mal calibrado puede ser demasiado agresivo ("¡COMPRÁ YA!"), demasiado pasivo (nunca cierra la venta), o simplemente incorrecto (recomienda productos equivocados). Poder versionar prompts permite:
1. Experimentar con diferentes estilos de venta sin miedo
2. Hacer rollback si una versión empeora las métricas
3. Correlacionar cambios de prompt con cambios en conversión
4. Tener un historial claro de la evolución del comportamiento del bot

Esto se conecta directamente con el Tema 6 (testing): tus tests de golden files deberían correr contra CADA versión nueva de prompt antes de que llegue a producción.

> **Dato clave**: Un prompt no versionado en producción es como deploy sin git — cuando algo se rompe, no sabés qué cambió ni cómo volver atrás. Tratá los prompts como código: versionados, testeados, y deployeados de forma controlada.

**Conceptos relacionados que necesitás conocer**

- **Feature flags**: Mecanismo para habilitar/deshabilitar funcionalidades sin deploy. Se puede usar para controlar qué versión de prompt está activa.
- **Canary deployment**: Similar a A/B testing pero con un porcentaje inicial muy chico (5-10%) para detectar problemas antes de afectar a todos.
- **Evaluation metrics**: Métricas específicas para evaluar prompts — task completion rate, hallucination rate, user satisfaction.

---

### Tema 9: Degradación Elegante y Circuit Breakers

**¿Qué es?**

En un sistema que depende de servicios externos (API de OpenAI, WhatsApp, base de datos), algo SIEMPRE va a fallar eventualmente. La pregunta no es "¿va a fallar?" sino "¿qué hace tu sistema CUANDO falla?". **Degradación elegante** (graceful degradation) es la estrategia de ofrecer funcionalidad reducida pero útil en vez de fallar completamente. Un **circuit breaker** es un patrón de diseño que previene que una falla en cascada tire abajo todo el sistema.

**Analogía**

Pensá en el sistema eléctrico de tu casa. Si enchufás demasiadas cosas y un circuito se sobrecarga, el **disyuntor** (circuit breaker) salta y corta la electricidad en ESA parte de la casa, pero el resto sigue funcionando. Sin el disyuntor, toda la instalación se quemaría. Además, cuando el disyuntor salta, no podés simplemente forzar el paso de electricidad — primero desconectás lo que causó el problema y después lo reactivás. Esa es exactamente la lógica del patrón.

**¿Cómo funciona por dentro?**

La diferencia entre **graceful degradation** y **fail-fast** es filosófica y práctica. Fail-fast dice: "si algo está mal, fallá inmediatamente y avisá". Es apropiado durante desarrollo o para errores irrecuperables. Graceful degradation dice: "si algo está mal, hacé lo mejor que puedas con lo que tenés". Para un bot de ventas, fail-fast significaría no responderle al cliente; graceful degradation significaría responder con un mensaje genérico como "estoy teniendo dificultades, ¿podés intentar de nuevo en un momento?".

El **circuit breaker pattern** tiene tres estados:

1. **Cerrado** (Closed): Todo funciona normal. Los requests pasan al servicio externo. El circuit breaker cuenta los errores.
2. **Abierto** (Open): Demasiados errores se acumularon (superaron un threshold). El circuit breaker BLOQUEA los requests — ni siquiera los intenta. Devuelve una respuesta de error inmediatamente. ¿Por qué? Porque si el servicio está caído, mandarle más requests solo lo empeora (y ralentiza tu sistema esperando timeouts).
3. **Semi-abierto** (Half-Open): Después de un tiempo, el circuit breaker deja pasar UN request de prueba. Si funciona, vuelve a Cerrado. Si falla, vuelve a Abierto. Es el "probar si ya se arregló".

La **tolerancia a fallas** (fault tolerance) es la capacidad del sistema completo de seguir funcionando cuando uno o más componentes fallan. Se logra combinando múltiples estrategias: circuit breakers, retries, fallbacks, redundancia.

Una **estrategia de fallback** es el plan B cuando el servicio principal no está disponible. Si la API de OpenAI no responde, ¿qué hacés? Opciones: (1) usar un modelo de respaldo más chico, (2) responder con plantillas pre-armadas basadas en keywords, (3) escalar directamente a un humano, (4) pedir al usuario que espere. La elección depende del contexto y la criticidad.

El **exponential backoff** es una estrategia de reintentos donde cada intento espera más que el anterior: primer reintento a 1 segundo, segundo a 2 segundos, tercero a 4 segundos, cuarto a 8, etc. ¿Por qué? Porque si el servicio está sobrecargado, bombardearlo con reintentos inmediatos empeora la situación. El backoff le da tiempo a recuperarse. Se suele agregar **jitter** (aleatoriedad) para evitar que miles de clientes reintenten todos exactamente al mismo momento (el "thundering herd" problem).

**¿Por qué nos importa en este proyecto?**

agents-badie depende de al menos tres servicios externos: la API del LLM (OpenAI), la API de WhatsApp, y la base de datos. Si cualquiera falla, sin circuit breakers y degradación elegante, el bot deja de funcionar completamente. Un cliente escribe y no recibe respuesta. Peor aún, los reintentos de WhatsApp generan más carga sobre un sistema ya comprometido (esto se conecta directamente con el Tema 5 — idempotencia).

Con circuit breakers, cuando la API del LLM falla: el circuito se abre, los siguientes mensajes reciben un fallback inmediato ("estamos experimentando demoras, un agente humano te va a contactar"), y el sistema prueba periódicamente si la API volvió. Esto se conecta también con el Tema 10 (human handoff) — el fallback natural cuando el bot no puede funcionar es escalar a un humano.

> **Dato clave**: Circuit breaker + exponential backoff + fallback strategy = resiliencia. Sin ninguno de los tres, una falla de 30 segundos en la API externa se convierte en una caída de todo tu sistema de ventas.

**Conceptos relacionados que necesitás conocer**

- **Thundering herd**: Cuando muchos clientes reintentan simultáneamente después de una falla, sobrecargando el servicio que acaba de recuperarse. Jitter mitiga esto.
- **Bulkhead pattern**: Aislar recursos para que la falla de un componente no consuma todos los recursos del sistema. Como los compartimentos estancos de un barco.
- **Health checks**: Endpoints que permiten verificar si un servicio está funcionando. El circuit breaker en estado half-open es esencialmente un health check.

---

### Tema 10: Handoff Humano en IA Conversacional

**¿Qué es?**

No importa qué tan bueno sea tu bot — hay situaciones que requieren un humano. Un cliente enojado, una consulta técnica compleja, un reclamo, una situación que el bot no fue diseñado para manejar. El **handoff humano** es el proceso de transferir una conversación del bot a un agente humano, de forma que sea invisible (o al menos suave) para el cliente.

**Analogía**

Es como el sistema de derivación telefónica. Llamás al banco y te atiende el sistema automatizado. Para cosas simples ("consultar saldo"), funciona perfecto. Pero cuando decís "quiero hablar con un operador" o el sistema detecta que no puede resolver tu problema, te transfiere a un humano. La clave es que el humano SABE por qué te transfirieron, qué ya intentaste, y no te hace repetir todo. Esa es la diferencia entre un buen handoff y uno malo.

**¿Cómo funciona por dentro?**

La **escalación** en chatbots puede ser explícita (el usuario pide hablar con un humano) o implícita (el sistema detecta que no puede resolver el caso). Los triggers implícitos incluyen: sentimiento negativo persistente, múltiples intentos fallidos de resolver un problema, consultas fuera del dominio del bot, o score de confianza bajo en las respuestas del LLM.

El **silent mode** (modo silencioso) o **bot pause** es el mecanismo por el cual el bot deja de responder automáticamente en esa conversación. Cuando un humano toma el control, el bot tiene que CALLARSE. Si el bot sigue respondiendo mientras el humano intenta ayudar, es un desastre. Implementar esto requiere un flag en el estado de la conversación: `is_bot_active = false`. Cada mensaje entrante verifica este flag antes de procesarlo con el agente. El humano reactiva el bot cuando termina.

El **patrón de handoff** completo tiene estas fases:
1. **Detección**: Se identifica la necesidad de escalación (explícita o implícita).
2. **Preparación**: Se recopila el contexto de la conversación — historial, estado del carrito, intentos previos del bot, razón de la escalación.
3. **Transferencia**: Se notifica al equipo humano con todo el contexto. Se activa el silent mode en el bot.
4. **Asistencia humana**: El agente humano atiende al cliente con todo el contexto disponible.
5. **Retorno**: Opcionalmente, el agente humano devuelve el control al bot y reactiva su modo activo.

La **preservación de contexto** durante el handoff es lo que hace la diferencia entre una buena y una mala experiencia. El agente humano necesita ver: qué productos buscó el cliente, qué tiene en el carrito, qué preguntas hizo, por qué se escaló. Sin esto, el cliente tiene que repetir todo desde cero — la experiencia más frustrante posible.

**¿Por qué nos importa en este proyecto?**

agents-badie es un bot de VENTAS. Un cliente que quiere comprar pero tiene una duda que el bot no puede resolver es una venta perdida si no hay forma de escalarlo a un humano. Y un cliente enojado que el bot sigue tratando de atender con respuestas genéricas puede causar daño reputacional. El handoff humano no es un feature "nice to have" — es la red de seguridad de todo el sistema.

La conexión con otros temas es directa: el estado de la conversación que guardamos en Redis (Tema 3) es lo que necesitamos preservar durante el handoff. Los circuit breakers (Tema 9) pueden triggerear una escalación automática cuando la API del LLM falla. Y la identidad del usuario (Tema 12) nos permite rutear la conversación al agente humano correcto.

> **Dato clave**: El mejor bot del mundo necesita una puerta de salida a un humano. Y esa puerta tiene que preservar TODO el contexto de la conversación, o la experiencia se rompe completamente.

**Conceptos relacionados que necesitás conocer**

- **Sentiment analysis**: Detección del tono emocional del usuario. Puede ser un trigger para escalación automática.
- **Routing**: Dirigir la conversación escalada al agente humano correcto basado en el tipo de consulta, idioma, disponibilidad, etc.
- **SLA (Service Level Agreement)**: Tiempo máximo que el cliente debería esperar para ser atendido por un humano después de la escalación.

---

### Tema 11: Parsing Multi-Item y Desambiguación

**¿Qué es?**

Cuando un cliente escribe "quiero 2 cajas de ibuprofeno, 1 de amoxicilina y algo para la tos", el sistema necesita: (1) identificar que son TRES pedidos separados, (2) extraer las cantidades, (3) encontrar los productos correctos en el catálogo, y (4) manejar la ambigüedad de "algo para la tos" (que no es un producto específico). Esto es **parsing multi-item** con **desambiguación**.

**Analogía**

Imaginá a un mozo en un restaurante. Un comensal dice: "para mí la milanesa napolitana, para ella una ensalada y dos aguas, ah y también un postre de esos ricos que tienen". El mozo tiene que: separar los pedidos (4 items), manejar cantidades ("dos" aguas), resolver ambigüedades ("esos ricos que tienen" — ¿cuáles?) preguntando "¿tiramisú o flan?", y coordinar todo con la cocina y la barra. Es exactamente el mismo problema.

**¿Cómo funciona por dentro?**

El **Named Entity Recognition (NER)** es la tarea de identificar y clasificar entidades nombradas en texto: nombres de personas, lugares, organizaciones, y en nuestro caso, productos y cantidades. Los LLMs modernos son excelentes en NER porque entienden el contexto — saben que "ibuprofeno" es un producto y "2 cajas" es una cantidad, sin necesidad de entrenamiento específico para tu catálogo.

La **extracción estructurada** (structured extraction) es pedirle al LLM que convierta texto libre en un formato estructurado. En vez de que el LLM responda con texto, le pedís que responda con una lista de objetos: `[{producto: "ibuprofeno", cantidad: 2, unidad: "cajas"}, ...]`. Esto se logra con un **structured extraction prompt** que define el formato de salida esperado, idealmente con un schema JSON que el LLM debe seguir.

La **desambiguación** es el proceso de resolver referencias vagas o ambiguas. "Algo para la tos" podría ser un jarabe, pastillas, o caramelos. Hay dos estrategias: (1) buscar en el catálogo y presentar opciones al usuario ("tenemos jarabe para la tos y pastillas, ¿cuál preferís?"), o (2) usar el contexto previo de la conversación para inferir la intención. La primera es más segura; la segunda es más fluida pero más propensa a errores.

El **partial matching** (coincidencia parcial) maneja el caso donde el usuario dice "ibuprofe" en vez de "ibuprofeno 400mg". El sistema necesita encontrar el producto más cercano usando búsqueda vectorial (Tema 1 y 2) o fuzzy matching (similitud de strings). La búsqueda vectorial es mejor para coincidencias semánticas ("calmante" → "ibuprofeno") mientras que fuzzy matching es mejor para coincidencias ortográficas ("ibuprofe" → "ibuprofeno").

**asyncio.gather** es la herramienta de Python para ejecución paralela asíncrona. Cuando tenés tres búsquedas de productos independientes, en vez de buscar uno, después el siguiente, después el tercero (secuencial), podés lanzar las tres búsquedas simultáneamente y esperar a que TODAS terminen. Si cada búsqueda tarda 200ms, secuencial = 600ms, paralelo = 200ms. Es una diferencia enorme en experiencia de usuario. Internamente, gather registra las tres coroutines en el event loop (Tema 4) y resuelve cuando todas completaron.

**¿Por qué nos importa en este proyecto?**

Un bot de ventas que solo puede procesar UN producto por mensaje obliga al cliente a escribir múltiples mensajes para un pedido de varios items. Es lento, frustrante, y antinatural. La gente dice "quiero esto, esto y esto" en un solo mensaje. El parsing multi-item es lo que permite una conversación natural.

La desambiguación es igualmente crítica: si el bot interpreta mal "algo para la tos" y agrega el producto equivocado al carrito, el cliente pierde confianza. Es preferible preguntar antes que equivocarse. Y la ejecución paralela con `asyncio.gather` se conecta directamente con el Tema 4 — aprovechás la naturaleza asíncrona del sistema para que buscar tres productos no sea tres veces más lento que buscar uno.

> **Dato clave**: Extractá estructuradamente primero, buscá en paralelo después, y si hay ambigüedad PREGUNTÁ. Nunca asumas — una pregunta cuesta menos que un producto equivocado en el carrito.

**Conceptos relacionados que necesitás conocer**

- **Fuzzy matching**: Algoritmos como Levenshtein distance que miden qué tan parecidas son dos strings. Complementa la búsqueda vectorial para errores ortográficos.
- **Structured output / function calling**: Capacidad de los LLMs modernos de generar output en formatos estrictos (JSON schema). Reduce enormemente el parseo post-hoc.
- **Confidence scoring**: Asignar un nivel de confianza a cada match. Si la confianza es baja, es mejor preguntar al usuario que adivinar.

---

### Tema 12: Resolución de Identidad (phone_number como thread_id)

**¿Qué es?**

En un bot de WhatsApp, necesitás saber QUIÉN es cada usuario para mantener su conversación, su carrito, su historial. La forma más obvia es usar el número de teléfono como identificador. Pero esto tiene implicaciones profundas que no son evidentes a primera vista. La **resolución de identidad** (identity resolution) es el proceso de determinar de forma unívoca quién es quién en tu sistema.

**Analogía**

Imaginá un hotel donde identifican a cada huésped por su número de habitación. Simple y efectivo... hasta que el huésped cambia de habitación. O hasta que dos personas comparten habitación. O hasta que el mismo huésped viene dos veces y le dan habitaciones diferentes. Un número de habitación es una **clave natural** — tiene significado en el mundo real, pero puede cambiar. Un "número de huésped" asignado por el sistema sería una **clave surrogada** — no tiene significado fuera del sistema, pero es estable.

**¿Cómo funciona por dentro?**

Una **clave natural** (natural key) es un identificador que proviene del dominio del negocio: número de teléfono, DNI, email, dirección. Tiene la ventaja de ser inmediatamente significativa — sabés qué es y podés buscar por ella. La desventaja es que puede cambiar (la persona cambia de número de teléfono), puede ser reutilizada (un número de teléfono se recicla), o puede tener diferentes formatos (+54 9 11 vs 011 vs 5491112345678).

Una **clave surrogada** (surrogate key) es un identificador generado por el sistema: UUID, auto-increment, etc. No significa nada fuera de tu base de datos, pero es estable, única, y nunca cambia.

La **capa de indirección** (mapping layer) es un patrón donde creás una tabla que mapea claves naturales a claves surrogadas: `phone_number → user_id`. Esto te da lo mejor de ambos mundos: buscás por número de teléfono (natural, conveniente) pero internamente todo referencia al `user_id` (estable, inmutable). Si el usuario cambia de número, actualizás UN registro en la tabla de mapeo y todo el historial sigue vinculado.

Las **race conditions** en este contexto aparecen cuando dos mensajes del mismo usuario llegan simultáneamente (no es raro en WhatsApp — imaginate que manda un mensaje y una foto casi al mismo tiempo). Ambos requests buscan el usuario, no lo encuentran (es nuevo), e intentan crearlo. Sin protección, terminás con dos registros para el mismo usuario. Las soluciones incluyen: unique constraints en la base de datos (la segunda inserción falla), operaciones atómicas tipo `INSERT ... ON CONFLICT DO NOTHING`, o locks distribuidos.

Un punto sutil pero importante: `thread_id` en LangGraph es el identificador de la conversación, no del usuario. Si usás el número de teléfono directamente como `thread_id`, estás acoplando identidad con conversación. ¿Qué pasa si querés que el usuario pueda tener múltiples conversaciones? ¿O si querés archivar una conversación vieja e iniciar una nueva? Con la indirección: `phone_number → user_id`, `user_id + timestamp → thread_id`, tenés flexibilidad total.

**¿Por qué nos importa en este proyecto?**

En agents-badie, el número de WhatsApp es la forma principal de identificar al usuario. Para un MVP, usar `phone_number` directamente como `thread_id` es pragmático y funciona. Pero necesitás ser consciente de las limitaciones:
- Normalización: `+5491112345678` y `5491112345678` y `01112345678` son el mismo número pero strings diferentes.
- Reciclaje: Si una persona deja su número y otra lo obtiene, ¿hereda la conversación anterior?
- Multi-sesión: ¿Puede el mismo usuario tener una conversación de ventas y una de soporte simultáneas?

La capa de indirección resuelve todo esto. Es un cambio pequeño en la arquitectura con un impacto enorme en flexibilidad futura. Se conecta con el Tema 3 (TTL en Redis) porque el `thread_id` es la clave que usa el checkpointer para guardar estado, y con el Tema 5 (deduplicación) porque la combinación de `phone_number + message_id` es lo que identifica unívocamente un evento.

> **Dato clave**: Usá claves naturales para BUSCAR y claves surrogadas para REFERENCIAR. La capa de indirección entre ambas es barata de implementar y te salva de refactors dolorosos en el futuro.

**Conceptos relacionados que necesitás conocer**

- **Normalización de datos**: Transformar variantes del mismo dato a un formato canónico. Crítico para números de teléfono con diferentes formatos.
- **Upsert**: Operación que inserta si no existe o actualiza si ya existe. Esencial para el mapping layer.
- **Eventual consistency**: En sistemas distribuidos, los datos pueden estar temporalmente desincronizados. Relevante cuando el mapping layer se cachea.

---

### Tema 13: Observabilidad y Logging Estructurado

**¿Qué es?**

¿Cómo sabés que tu sistema está funcionando bien? ¿Cómo encontrás la causa cuando algo falla? ¿Cómo detectás que la performance se está degradando ANTES de que los usuarios se quejen? La **observabilidad** es la capacidad de entender el estado interno de tu sistema mirando sus outputs. No es lo mismo que monitoreo (que se fija en métricas predefinidas) — la observabilidad te permite hacer preguntas que NO anticipaste.

**Analogía**

Pensá en un médico. El monitoreo es como medir la temperatura y la presión: sabés si están en rango normal, pero si algo está mal, esas dos mediciones no te dicen qué es. La observabilidad es como tener un análisis de sangre completo, radiografías, y el historial del paciente — podés diagnosticar problemas que ni siquiera sabías que tenías que buscar. Los **tres pilares** son como tres tipos de estudio médico: los logs son el historial clínico (qué pasó), las métricas son los signos vitales (cómo está ahora), y los traces son las radiografías (cómo fluye todo por dentro).

**¿Cómo funciona por dentro?**

Los **tres pilares de la observabilidad** son:

**Logs**: Registros de eventos discretos. "A las 14:32:07, el usuario +5491112345678 envió un mensaje". El **logging estructurado** versus el **logging no estructurado** es la diferencia entre:

- No estructurado: `"User +5491112345678 sent message at 2024-01-15 14:32:07"` — un string legible para humanos pero difícil de parsear automáticamente.
- Estructurado: `{"timestamp": "2024-01-15T14:32:07Z", "user": "+5491112345678", "event": "message_received", "message_id": "abc123"}` — un objeto con campos tipados que podés filtrar, agregar, y analizar programáticamente.

El logging estructurado es infinitamente más poderoso. Podés buscar "todos los eventos del usuario X en la última hora", "todos los errores del tipo Y", "el tiempo promedio de respuesta". Con logs no estructurados, tenés que parsear texto con regex — frágil, lento, y propenso a errores.

**Métricas**: Mediciones numéricas a lo largo del tiempo. "Latencia promedio de respuesta: 1.2 segundos", "Requests por segundo: 150", "Tasa de error: 0.3%". Las métricas te permiten detectar tendencias y poner alertas: "si la latencia supera 3 segundos, notificame".

**Traces**: El recorrido completo de una operación a través del sistema. Un **trace** representa un request desde que entra hasta que sale, y cada paso intermedio es un **span**. Por ejemplo: un mensaje de WhatsApp genera un trace con spans para: recepción del webhook, deduplicación, invocación del agente, búsqueda en catálogo, generación de respuesta, envío por WhatsApp. Cada span tiene duración y metadata.

El **correlation ID** (o request ID) es un identificador único que se genera al principio de un request y se propaga a TODOS los componentes que participan en procesarlo. Cuando algo falla, buscás por ese ID y ves la historia completa de ese request a través de todos los servicios. Sin correlation ID, correlacionar logs de diferentes servicios es como buscar una aguja en un pajar.

La **propagación de contexto** (context propagation) es el mecanismo por el cual el correlation ID y otra metadata viajan de servicio en servicio. Normalmente se hace a través de headers HTTP o metadata en mensajes de cola. **OpenTelemetry** (OTel) es el estándar de la industria para esto: un framework open source que provee APIs y SDKs para instrumentar tu código y generar los tres pilares de forma consistente. La ventaja de OTel es que es vendor-neutral — generás los datos una vez y los mandás a cualquier backend (Datadog, Grafana, Jaeger, etc.).

El **distributed tracing** (tracing distribuido) cobra importancia cuando tu sistema tiene múltiples servicios. Un mensaje de WhatsApp puede pasar por: API gateway → webhook handler → Redis (dedup) → LangGraph agent → OpenAI API → pgvector search → response formatter → WhatsApp API. Sin tracing distribuido, si la respuesta tarda 5 segundos, ¿cómo sabés dónde está el cuello de botella? Con un trace, ves que el 4 segundos los pasó esperando a OpenAI. Listo, sabés exactamente dónde actuar.

**¿Por qué nos importa en este proyecto?**

agents-badie tiene una cadena de procesamiento con múltiples puntos de falla: el webhook de WhatsApp, la deduplicación en Redis, el agente LLM, la búsqueda vectorial en pgvector, y el envío de respuesta. Sin observabilidad, cuando un cliente reporta "el bot no me respondió", no tenés forma de saber QUÉ pasó. ¿Llegó el mensaje? ¿Se deduplicó incorrectamente? ¿El LLM falló? ¿La respuesta se envió pero WhatsApp la rechazó?

El logging estructurado con correlation IDs permite seguir el recorrido de CADA mensaje a través de todo el sistema. Es la diferencia entre adivinar y diagnosticar. Se conecta con prácticamente todos los otros temas: podés medir la latencia de la búsqueda vectorial (Tema 1), tracear cuándo expira un TTL (Tema 3), detectar duplicados procesados (Tema 5), y monitorear la tasa de escalación a humanos (Tema 10).

> **Dato clave**: La observabilidad no es un "add-on" que ponés después. Si no lo diseñás desde el principio, agregarlo después requiere tocar CADA componente del sistema. Empezá con logging estructurado y correlation IDs — son la inversión de menor costo y mayor retorno.

**Conceptos relacionados que necesitás conocer**

- **Cardinality**: La cantidad de valores únicos que puede tomar una dimensión de una métrica. Alta cardinalidad (como user_id) es cara de almacenar y consultar.
- **Sampling**: En sistemas con mucho tráfico, no guardás TODOS los traces. Guardás un porcentaje representativo. El truco es asegurarse de que los errores siempre se sampleen (head-based vs tail-based sampling).
- **SLI/SLO/SLA**: Service Level Indicator (métrica), Service Level Objective (meta interna), Service Level Agreement (compromiso con el cliente). La observabilidad es lo que te permite saber si estás cumpliendo tus SLOs.
- **Log aggregation**: Centralizar logs de múltiples servicios en un solo lugar para poder buscar y correlacionar. ELK stack (Elasticsearch, Logstash, Kibana) y Grafana Loki son opciones populares.

---

## Conexiones Entre Temas

Para cerrar, lo más importante: estos 13 temas NO son islas. Forman un sistema interconectado:

- Los **embeddings** (2) se buscan con **índices vectoriales** (1) y la calidad de búsqueda se mide con **tests** (6, 7)
- El **estado** de la conversación vive en **Redis con TTL** (3) y es lo que se preserva en el **handoff humano** (10)
- El procesamiento **async** (4) causa los **duplicados** que la **idempotencia** (5) resuelve
- Los **circuit breakers** (9) triggerean **degradación elegante** que puede incluir **handoff humano** (10)
- El **versionado de prompts** (8) necesita **tests de LLM** (6) para validar cambios
- La **identidad del usuario** (12) es la clave para toda la **observabilidad** (13) — el correlation ID de negocio
- El **parsing multi-item** (11) depende de los **embeddings** (2) para buscar y del procesamiento **async** (4) para ser rápido

Cada decisión arquitectónica afecta a las demás. Eso es lo que hace que la arquitectura sea una disciplina: no es elegir la mejor opción para cada pieza individual, sino elegir opciones que funcionen bien JUNTAS como sistema.

---

*"La diferencia entre un junior y un senior no es que el senior sepa más tecnologías — es que entiende cómo las piezas encajan entre sí." — Y vos, leyendo esto, ya estás un paso más cerca.*
