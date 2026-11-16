# Jared 🫡

Asistente personal por Telegram con Claude como cerebro. Tiene la personalidad de Jared Dunn (Silicon Valley).

**Qué hace**
- Charla por texto, audio o foto (por ejemplo, la foto de un ticket se anota como gasto).
- Va armando una memoria permanente a partir de lo que hablan: ideas, pensamientos, gastos, tareas, intereses, personas, objetivos y proyectos. También mantiene un perfil tuyo actualizado.
- Recordatorios (únicos o recurrentes) que te llegan por Telegram.
- Google Calendar: consulta y crea eventos, y te avisa 30 minutos antes de cada uno.
- Busca en internet y todas las mañanas te trae novedades sobre tus intereses.
- Te escribe sin que le hables: resumen a las 08:00, chequeo a las 16:00 (solo si hay algo útil) y una reflexión nocturna en la que completa la memoria.
- `/charla` abre un modo para pensar en voz alta con audios y `/fin` lo cierra con conclusiones y próximos pasos.
- Recordatorios con botones: *✅ Hecho*, *+1 hora* y *Mañana*.
- Tareas con cierre: le decís "ya lo hice" o tocás ✅ en `/pendientes`.
- Ayuda-memoria 30 minutos antes de cada reunión, con lo que sabe del tema y de la gente.
- Resumen semanal el domingo a las 21 y repaso de una idea vieja los miércoles a las 19.
- Presupuesto mensual por rubro, con avisos al 80 % y al 100 %.
- Gmail en solo lectura: los mails que necesitan acción aparecen en el resumen de la mañana.
- `/costo` muestra el gasto estimado en Claude del mes.

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

### 6. Audios (opcional, recomendado)
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

---

## Uso
- `/ayuda`: lo que sabe hacer.
- `/charla` y `/fin`: modo para pensar en voz alta.
- `/resumen`: el resumen del día en el momento.
- `/recordatorios` y `/memoria`: qué tiene pendiente y qué anotó.
- `/pendientes`: tareas abiertas, con botones para cerrarlas.
- `/presupuesto` y `/costo`: topes de gasto del mes y gasto estimado en Claude.
- `/gmail`: conectar el correo.
- `/voz si` o `/voz no`: respuestas en audio. Si le mandás un audio, te contesta también con audio.

Los horarios automáticos se cambian con `BRIEF_TIME`, `CHECKIN_TIME`, `REFLECTION_TIME`, `WEEKLY_TIME`/`WEEKLY_DAY` e `IDEAS_TIME`/`IDEAS_DAY` (los días van de 0 = lunes a 6 = domingo). Con `MEETING_PREP=false` se apaga la ayuda-memoria de reuniones.

## Costos
- Render, Supabase, cron-job.org, Groq y Telegram: gratis.
- Claude: se paga por uso, y las búsquedas web se cobran aparte (alrededor de USD 0,01 cada una). Con uso diario normal (resumen matinal con novedades, chequeo, reflexión y algunas charlas) calculá **aprox. USD 5–15 por mes**. Si querés gastar menos, poné `NEWS_IN_BRIEF=false`.

## Límites conocidos
- Los bots de Telegram no pueden hacer llamadas de voz reales. Lo de "llamarlo" se resuelve con `/charla` y audios de ida y vuelta.
- En Render gratis, si el despertador falla, el primer mensaje puede tardar alrededor de un minuto mientras el servidor arranca.

## Desarrollo
```bash
pip install -r requirements.txt pytest pytest-asyncio
TEST_DATABASE_URL=postgresql://... pytest
```
