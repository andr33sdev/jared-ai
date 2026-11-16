# Jared 🫡

Asistente personal por Telegram con Claude como cerebro. Tiene la personalidad de Jared Dunn (Silicon Valley).

**Qué hace**
- Charla por texto, audio o foto (por ejemplo, la foto de un ticket se anota como gasto).
- Va armando una memoria permanente a partir de lo que hablan: ideas, pensamientos, gastos, tareas, intereses, personas, objetivos y proyectos. También mantiene un perfil tuyo actualizado.
- Recordatorios (únicos o recurrentes) que te llegan por Telegram.
- Google Calendar: consulta y crea eventos, y te avisa 30 minutos antes de cada uno.
- Busca en internet y todas las mañanas te trae novedades sobre tus intereses.
- Te escribe sin que le hables: resumen a las 08:00, chequeo a las 16:00 (solo si hay algo útil) y una reflexión nocturna en la que completa la memoria.
- Modo charla para pensar en voz alta con audios: al cerrar te deja conclusiones y próximos pasos.
- Recordatorios con botones: *✅ Hecho*, *+1 hora* y *Mañana*.
- Tareas con cierre: le decís "ya lo hice" o tocás ✅ en la lista de pendientes.
- Ayuda-memoria 30 minutos antes de cada reunión, con lo que sabe del tema y de la gente.
- Resumen semanal el domingo a las 21 y repaso de una idea vieja los miércoles a las 19.
- Presupuesto mensual por rubro, con avisos al 80 % y al 100 %.
- Gmail en solo lectura: los mails que necesitan acción aparecen en el resumen de la mañana.
- Te dice cuánto lleva gastado en Claude este mes (estimado).
- **Minutas:** le contás una reunión y la ordena. Tus acciones quedan como tareas y lo que deben otros, como esperas.
- **Esperas:** "Juan me tiene que mandar X el viernes". Ese día te pregunta si llegó, con botones, y si no llegó te redacta el reclamo.
- **Documentos:** le mandás un PDF, lo resume, lo guarda y después le podés preguntar sobre él.
- **Fechas importantes:** cumpleaños, aniversarios y vencimientos. Avisa 7 días antes, el día anterior y el mismo día, con ideas de regalo.
- **Hábitos:** a las 21:30 te pregunta con botones y lleva tus rachas.

**Arquitectura:** Python con FastAPI. Telegram le avisa cada mensaje nuevo por webhook, Claude (Sonnet 5.5) hace de cerebro con herramientas y búsqueda web, y la base es Postgres en Supabase. Corre en Render (plan gratis) y tiene un reloj interno que revisa cada minuto si hay recordatorios o tareas pendientes.

---

## Puesta en marcha (una sola vez, unos 30 minutos)

### 1. Bot de Telegram
1. En Telegram hablale a **@BotFather** y mandale `/newbot`.
2. Nombre: `Jared`. Usuario: algo como `jared_andres_bot`.
3. Guardá el **token** que te da → `TELEGRAM_BOT_TOKEN`.

### 2. Claude (Anthropic)
1. Entrá a https://console.anthropic.com y cargá crédito (con USD 10 alcanza para arrancar).
2. **API Keys → Create key** → `ANTHROPIC_API_KEY`.
3. En **Settings → Privacy**, habilitá **Web search**.

### 3. Base de datos (Supabase, gratis)
1. Entrá a https://supabase.com → **New project** (región São Paulo). Guardá la contraseña.
2. Andá a **Connect → Session pooler** y copiá la URL, reemplazando `[YOUR-PASSWORD]` por tu contraseña → `DATABASE_URL`.
   Usá la del *pooler*: la conexión directa no funciona desde Render.
3. Las tablas se crean solas la primera vez que arranca Jared.

### 4. Servidor (Render, gratis)
1. Entrá a https://render.com con tu cuenta de GitHub → **New → Blueprint** → elegí este repo.
2. Completá las variables que te pide (las de los pasos 1 a 3). Dejá `TELEGRAM_OWNER_ID` vacío por ahora.
3. Deploy. Cuando termine, escribile a tu bot: te va a responder con **tu ID**.
4. En Render → **Environment**, cargá ese número en `TELEGRAM_OWNER_ID` y guardá (se reinicia solo).

### 5. Que no se duerma
El plan gratis de Render apaga el servidor después de 15 minutos sin tráfico, y apagado Jared no puede escribirte solo.
1. Entrá a https://cron-job.org (gratis) → **Create cronjob**.
2. URL: `https://TU-SERVICIO.onrender.com/health`, cada **10 minutos**.

Con un solo servicio prendido todo el mes se usan unas 744 de las 750 horas gratis. No crees otro servicio gratis en la misma cuenta de Render, porque se pasarían del límite.

### 6. Audios (recomendado)
Si ya cargaste `ELEVENLABS_API_KEY` (paso 9), los audios funcionan sin hacer nada más: se transcriben con ElevenLabs Scribe, que cuesta alrededor de USD 0,20 por hora de audio. Groq es la alternativa gratis:
1. Entrá a https://console.groq.com → **API Keys** → `GROQ_API_KEY`. Es gratis.
2. Groq solo **transcribe** tus audios con Whisper; el que piensa y responde es siempre Claude.
3. La voz de Jared (`es-AR-TomasNeural`) sale de Edge TTS, no necesita clave. Si algún día falla, Jared responde solo en texto.

### 7. Google Calendar (opcional)
1. En https://console.cloud.google.com creá un proyecto → **APIs & Services → Library** → habilitá **Google Calendar API**.
2. **IAM & Admin → Service Accounts → Create**. Después, dentro de la cuenta: **Keys → Add key → JSON**. Se descarga un archivo.
3. Pegá **todo el contenido** de ese JSON en `GOOGLE_SERVICE_ACCOUNT_JSON`.
4. En Google Calendar → tu calendario → **Configuración y uso compartido → Compartir con personas** → agregá el mail de la cuenta de servicio (`...@...iam.gserviceaccount.com`) con permiso **"Hacer cambios en eventos"**.
5. `GOOGLE_CALENDAR_ID` = tu Gmail.

### 8. Gmail (opcional)
Usa tu cuenta personal en modo **solo lectura**: Jared nunca manda, borra ni mueve mails.
1. En el mismo proyecto de Google Cloud: **APIs & Services → Library** → habilitá **Gmail API**.
2. **Google Auth Platform** (o "OAuth consent screen") → configurá la app: nombre "Jared", tipo **External**, con tu mail de soporte.
3. En **Audience**, tocá **Publish app** para pasarla a *In production*. Si la dejás en *Testing*, Google corta el permiso cada 7 días y vas a tener que reconectar con `/gmail`.
4. **Clients → Create client → Web application**. En *Authorized redirect URIs* agregá `https://TU-SERVICIO.onrender.com/google/callback`.
5. Copiá el *Client ID* y el *Client secret* en Render como `GOOGLE_OAUTH_CLIENT_ID` y `GOOGLE_OAUTH_CLIENT_SECRET`.
6. En Telegram mandale `/gmail` a Jared, abrí el link y aceptá. Google te va a avisar que la app no está verificada: es tu propia app, así que tocá **Configuración avanzada → Ir a Jared**.

### 9. Llamada por voz (opcional, recomendado)
Decile a Jared "te llamo" y te manda un botón 📞. Se abre una página donde hablás de corrido, sin apretar nada, y Jared te contesta en voz alta. Usa la misma memoria y, al colgar, te manda el resumen por Telegram.
- Funciona mejor en **Chrome en Android**: tu voz se pasa a texto en el navegador, gratis. En otros navegadores se mantiene apretado el círculo para hablar, y en ese caso hace falta `GROQ_API_KEY`.
- **Voz premium (ElevenLabs):** creá una cuenta en elevenlabs.io → **API Keys** → `ELEVENLABS_API_KEY`. En **Voices → Explore** filtrá por *Spanish* y acento *Argentinian*, elegí una voz masculina y copiá su *Voice ID* → `ELEVENLABS_VOICE_ID`.
- **Tope de gasto:** `ELEVENLABS_MONTHLY_CHARS` (por defecto 100.000 caracteres, unos USD 4). Al 90 % te avisa, y al llegar al tope sigue con la voz gratis hasta el mes siguiente. Desde la página podés cambiar entre la voz premium y la gratis.
- Si compartiste el link sin querer, decile "generá un link nuevo de llamada" y el anterior deja de funcionar.
- **Velocidad:** Jared empieza a hablar con la primera oración mientras arma el resto, y apenas terminás suena una muletilla corta ("A ver…"). `CALL_PAUSE_MS` (900 por defecto) es el silencio que espera antes de responder: si te corta cuando hacés pausas para pensar, subilo a 1200.

---

## Uso
No hay comandos: a Jared le hablás como a una persona. Por ejemplo:
- "Te llamo" te manda el botón para hablarle por voz en vivo.
- "Charlemos un rato" para pensar en voz alta por escrito o con audios, y "listo, cerremos" para que haga el cierre.
- "¿Qué tengo pendiente?" te muestra las tareas con botones para cerrarlas.
- "¿Qué me tienen que mandar?", "¿qué recordatorios tengo?", "¿cómo voy con la plata?"
- "Respondeme con audios" o "volvé al texto".
- "Te cuento cómo fue la reunión…" arma la minuta.

Solo quedan dos comandos de sistema: `/resumen` (el resumen del día en el momento) y `/gmail` (para conectar el correo).

Los horarios automáticos se cambian con `BRIEF_TIME`, `CHECKIN_TIME`, `REFLECTION_TIME`, `WEEKLY_TIME`/`WEEKLY_DAY` e `IDEAS_TIME`/`IDEAS_DAY`, más `DATES_TIME` (09:30), `WAITS_TIME` (10:00) y `HABITS_TIME` (21:30) (los días van de 0 = lunes a 6 = domingo). Con `MEETING_PREP=false` se apaga la ayuda-memoria de reuniones.

## Costos
- Render, Supabase, cron-job.org, Groq y Telegram: gratis.
- Claude se paga por uso. Para que salga barato, Jared usa dos modelos:
  - **Claude Haiku 5.5** para el día a día y las tareas automáticas. Es unas 20 veces más barato que Sonnet.
  - **Claude Sonnet 5.5** solo para el modo charla y para leer documentos, donde conviene más inteligencia.
- Además, la conversación se guarda en caché para no pagarla completa en cada vuelta, y cada respuesta hace como máximo 3 búsquedas web (alrededor de USD 0,01 cada una).
- Costo estimado: **USD 1–4 por mes** con uso diario. Lo que más pesa son las búsquedas web del resumen matinal; si querés bajarlo, poné `NEWS_IN_BRIEF=false`.
- Si un día preferís que todo use Sonnet, cambiá `JARED_MODEL` a `claude-sonnet-5-5`.

## Límites conocidos
- Los bots de Telegram no pueden hacer llamadas de voz, por eso la llamada es una página web aparte. Entre que terminás de hablar y Jared responde pasan 2 o 3 segundos.
- En Render gratis, si el despertador falla, el primer mensaje puede tardar alrededor de un minuto mientras el servidor arranca.

## Desarrollo
```bash
pip install -r requirements.txt pytest pytest-asyncio
TEST_DATABASE_URL=postgresql://... pytest
```
