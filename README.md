# SAMCRO — Bot de bitácora y nómina para Discord

Cada trabajador tiene su **propio canal privado** en Discord donde sube lo
que hizo (con foto/captura de pantalla). El bot calcula automáticamente
cuánto se le debe, y **todos los domingos a las 16:00** postea el resumen de
pago solo, sin que nadie tenga que hacer nada.

## Cómo queda armado (v3)

- **Un canal por trabajador**, no por sección (ej: `#bitacora-alan-smith`).
  Ahí esa persona registra TODO lo que hace, sin importar en qué área — cada
  vez que usa el comando elige a qué sección pertenece ese registro
  (Mecánica, Bar, Tatuajes o Show), así que si un mecánico ayuda un día en
  el bar, lo registra igual en su mismo canal, solo cambia la sección.
- El bot postea ahí mismo un mensaje con la foto, qué hizo, cuánto cobró y
  su comisión — funciona como bitácora visual para el jefe de esa área y
  para Uds. como liderazgo.
- **Aprobación del jefe**: cada registro queda "⏳ Pendiente" hasta que el
  jefe del área correspondiente (o Liderazgo) reacciona con ✅ en el
  mensaje. Solo lo aprobado cuenta para el pago. Se puede desactivar en
  `config.py` (`REQUIERE_APROBACION = False`) si más adelante prefieren
  sistema de honor.
- **Corte semanal automático**: todos los domingos a las 16:00 (hora de
  Chile, configurable), el bot:
  1. Postea en el canal de CADA trabajador cuánto se ganó esa semana, con el
     desglose por sección.
  2. Postea un resumen consolidado en `#nomina-resumen` con todos los
     trabajadores agrupados por sección y el total del club completo.
  Esto es solo el CÁLCULO automático -- sigan usando `/marcar-pagado`
  después de pagarle a la gente en el juego, para que quede historial.

## 1. Antes de invitar el bot: prepara el servidor

Crea estos elementos en tu Discord (nombres EXACTOS, o edítalos en
`config.py` si prefieres otros):

**Categoría** para los canales personales:
- `📋 BITÁCORAS PERSONALES` (vacía, el bot va a crear un canal por
  trabajador ahí dentro)

**Un canal aparte** para el resumen semanal:
- `#nomina-resumen` — visible solo para los 4 jefes de área + Liderazgo

**Roles:**
- Trabajador (uno o más por persona si apoya en varias áreas): `Mecánico`,
  `Bartender`, `Tatuador`, `Bailarina` (agrega `Prospecto` si aplica)
- Encargado de área: `Jefe Mecánica`, `Jefe Bar`, `Jefe Tatuajes`,
  `Jefe Show`
- Liderazgo (Uds. dos, ve y aprueba TODO): el nombre por defecto es
  `Liderazgo SAMCRO` — cámbialo en `config.py` (`ROL_LIDERAZGO`) por el que
  prefieran usar.

Todos estos nombres deben coincidir EXACTO (mayúsculas, tildes) con
`config.py`.

## 2. Crear la aplicación del bot

1. https://discord.com/developers/applications → **New Application** →
   "SAMCRO".
2. Pestaña **Bot** → **Reset Token** → cópialo (va en `.env`, nunca lo
   compartas).
3. Activa **Server Members Intent**.
4. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`.
   Permisos: `Manage Channels` (para crear los canales personales),
   `Send Messages`, `Embed Links`, `Attach Files`, `Add Reactions`,
   `Read Message History`, `Use Slash Commands`, `Mention Everyone` (para
   avisarle a cada trabajador su pago). Invita el bot con el link generado.

## 3. Configurar y correr

```bash
cp .env.example .env
```

Edita `.env` con tu `DISCORD_TOKEN` y `GUILD_ID` (activa "Modo desarrollador"
en Discord → Ajustes → Avanzado, clic derecho sobre el ícono del server →
"Copiar ID"). El `GUILD_ID` es OBLIGATORIO en esta versión: el bot lo
necesita para saber en qué servidor postear el corte semanal automático.

Revisa `config.py`: tarifas, comisiones, nombres de rol, hora de corte
(`HORA_CIERRE_SEMANA`) y si quieren aprobación o sistema de honor.

```bash
pip install -r requirements.txt
python bot.py
```

## 4. Dejarlo corriendo 24/7 en Railway (gratis)

Mientras corre en tu computador, el bot solo funciona con tu PC encendido —
y el corte semanal automático de los domingos NO se va a disparar si el bot
está apagado en ese momento. Railway.app lo deja corriendo solo, sin que
dependa de tu compu. Esta carpeta ya viene lista para subirse ahí (trae
`Procfile`, `.python-version` y `.gitignore`).

**Paso 1 — Sube el código a GitHub (sin usar la terminal):**
1. Crea una cuenta gratis en https://github.com si no tienes.
2. Click en "New repository" → nómbralo `samcro-bot` → márcalo **Private**
   (importante, aunque el token no vaya en el código, mejor privado) →
   "Create repository".
3. En la página del repo, click "uploading an existing file" (o
   "Add file" → "Upload files") y arrastra TODOS los archivos de esta
   carpeta **excepto** `.env` (ese nunca se sube a ningún lado — el token
   va directo en Railway, no en GitHub). Confirma el commit.

**Paso 2 — Conecta Railway:**
1. Entra a https://railway.app y crea una cuenta (puedes usar la de GitHub
   directamente, es lo más rápido).
2. "New Project" → "Deploy from GitHub repo" → elige `samcro-bot`.
3. Railway va a detectar que es Python y usar el `Procfile` para saber que
   tiene que correr `python bot.py` como un **worker** (proceso de fondo,
   no una web) — no tienes que configurar nada más ahí.

**Paso 3 — Agrega las variables secretas:**
1. Dentro del proyecto en Railway, ve a la pestaña **Variables**.
2. Agrega `DISCORD_TOKEN` con el token de tu bot, y `GUILD_ID` con el ID de
   tu servidor (los mismos valores que habrías puesto en `.env`).
3. Railway va a reiniciar el servicio solo y el bot debería conectarse. En
   la pestaña **Deployments** → **View Logs** deberías ver
   `SAMCRO bot conectado como SAMCRO#XXXX`.

Desde ese momento el bot queda corriendo 24/7, y el corte semanal de los
domingos a las 16:00 se va a disparar solo sin que nadie tenga que hacer
nada. El plan gratuito de Railway alcanza de sobra para un bot como este.

Si prefieres otra opción: **Replit** (sube los archivos, configura
"Secrets" con las mismas variables, activa "Always On"), o un **VPS**
propio con `pm2`/`systemd` si tu empresa termina consiguiendo uno.

Avísame en qué paso te quedaste si algo no calza (por ejemplo si Railway te
pide tarjeta para el plan gratuito, o si los logs muestran un error) y lo
resolvemos juntos.

## 5. Cómo se usa en el día a día

**Para darle canal a un trabajador nuevo (lo hace un jefe o Liderazgo, una
vez por persona):**
```
/crear-canal-trabajador trabajador:@Alan Smith
```
Esto crea `#bitacora-alan-smith`, visible solo para Alan, los 4 jefes y
Liderazgo, y le manda un mensaje de bienvenida explicándole los comandos.

**Trabajador (ej. Alan, mecánico):**
1. Hace la reparación en el juego, saca captura de pantalla.
2. Entra a SU canal personal (`#bitacora-alan-smith`).
3. `/registrar-servicio seccion:Mecánica servicio:"Reparación motor" monto:2500 foto:[captura]`
4. Queda "⏳ Pendiente" hasta que el Jefe Mecánica lo apruebe con ✅.
5. Si un día ayuda en el bar, en ese MISMO canal usa
   `/registrar-servicio seccion:"Bar / Comida" ...`
6. Puede revisar `/mi-resumen` en cualquier momento.

**Jefe de área:**
1. Ve pasar los registros de su gente en los canales personales (tiene
   acceso a todos, porque están vinculados a su rol de jefe).
2. Reacciona ✅ a los que están correctos.
3. `/nomina` para ver el resumen de su sección, `/exportar-nomina` para el
   CSV, `/marcar-pagado` cuando ya pagó.

**Liderazgo (Uds. dos):**
- Mismo flujo pero para las 4 áreas a la vez. `/nomina` sin elegir sección
  muestra las cuatro juntas.
- Los domingos a las 16:00 les va a llegar solo, en `#nomina-resumen`, el
  total que hay que pagarle a cada persona y el total del club completo.

## Semana de pago

Corre de **lunes a domingo 16:00** por defecto (`DIA_CIERRE_SEMANA` y
`HORA_CIERRE_SEMANA` en `config.py`, editable). El corte automático solo se
dispara una vez por semana aunque el bot revise cada 10 minutos -- si el bot
estuvo apagado justo a esa hora, lo procesa apenas vuelve a estar online (no
se pierde, pero sí se atrasa).

## Estructura de archivos

```
samcro-bot/
├── bot.py            # comandos, aprobación y corte semanal automático
├── database.py        # acceso a SQLite (samcro.db se crea solo)
├── config.py           # secciones, roles, tarifas, hora de corte
├── requirements.txt
├── .env.example
└── README.md
```

## Ideas para más adelante

- Sistema de pago por evento privado para Show/Tatuajes (por ahora usa el
  mismo esquema de monto libre + comisión).
- `/editar-registro` para que un jefe corrija un registro equivocado.
- `/ranking` semanal por diversión entre áreas.
