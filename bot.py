"""
Bot de bitácora y nómina para SAMCRO MC — v3 (canal personal por trabajador
+ corte semanal automático).

Cómo funciona:
  1. Cada TRABAJADOR tiene su propio canal privado (ej: #bitacora-alan-smith),
     creado con /crear-canal-trabajador (lo usa un jefe o Liderazgo, una vez
     por persona). Ahí es donde esa persona sube lo que hizo, con foto.
  2. Como alguien puede apoyar en más de un área, cada registro elige su
     SECCIÓN con un menú desplegable (no depende del canal).
  3. El bot postea un embed PÚBLICO en el canal personal del trabajador (con
     la foto si la hay). Si REQUIERE_APROBACION está activo, queda
     "⏳ Pendiente" hasta que el jefe de esa área (o Liderazgo) reacciona con
     ✅. Solo lo aprobado cuenta para la nómina.
  4. Todos los domingos a la hora configurada (HORA_CIERRE_SEMANA), el bot:
       - postea el total de la semana en el canal personal de cada uno,
       - postea un resumen consolidado por sección en el canal de resumen
         (#nomina-resumen) para que jefes/liderazgo paguen de un vistazo.
     Esto es automático, no reemplaza el registro histórico de pago -- sigan
     usando /marcar-pagado después de pagar en el juego.

Comandos:
  /crear-canal-trabajador  -> (jefe/liderazgo) crea el canal personal de alguien
  /registrar-horas         -> registrar horas trabajadas (elige la sección)
  /registrar-servicio      -> registrar un servicio con foto (elige la sección)
  /mi-resumen               -> cuánto llevo ganado esta semana (aprobado + pendiente)
  /nomina                   -> (jefe/liderazgo) nómina de la semana por persona
  /exportar-nomina          -> (jefe/liderazgo) CSV de la semana
  /marcar-pagado             -> (jefe/liderazgo) deja constancia de pago
  /tarifas                   -> ve las tarifas vigentes de una sección
"""
import base64
import csv
import io
import os
import re
import unicodedata
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web

import config
import database as db

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

intents = discord.Intents.default()
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!samcro-", intents=intents)

# ---------- Servidor Web integrado para el Dashboard ----------

async def handle_dashboard(request):
    try:
        with open("dashboard.html", "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error al cargar dashboard.html: {e}", status=500)


async def handle_api_nomina(request):
    try:
        inicio_utc, fin_utc, _, _ = rango_semana_actual()
        data = {}
        for seccion_key in config.SECCIONES.keys():
            resumen = await calcular_nomina(seccion_key, inicio_utc, fin_utc, solo_validados=True)
            data[seccion_key] = [
                {
                    "nombre": v["nombre"],
                    "horas": v["horas"],
                    "pagoHoras": v["pago_horas"],
                    "nServicios": v["n_servicios"],
                    "comisiones": v["pago_comisiones"],
                    "total": v["total"]
                }
                for v in resumen.values()
            ]
        return web.json_response(data)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def auth_middleware(app, handler):
    async def middleware(request):
        auth_user = os.getenv("DASHBOARD_USER")
        auth_pass = os.getenv("DASHBOARD_PASS")
        if auth_user and auth_pass:
            auth_header = request.headers.get("Authorization")
            if not auth_header:
                return web.Response(
                    status=401,
                    text="Acceso denegado. Inicia sesion.",
                    headers={"WWW-Authenticate": 'Basic realm="SAMCRO Dashboard"'}
                )
            try:
                auth_type, encoded = auth_header.split(" ", 1)
                if auth_type.lower() == "basic":
                    decoded = base64.b64decode(encoded).decode("utf-8")
                    username, password = decoded.split(":", 1)
                    if username == auth_user and password == auth_pass:
                        return await handler(request)
            except Exception:
                pass
            return web.Response(
                status=401,
                text="Credenciales incorrectas.",
                headers={"WWW-Authenticate": 'Basic realm="SAMCRO Dashboard"'}
            )
        return await handler(request)
    return middleware


async def iniciar_servidor_web():
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/api/nomina", handle_api_nomina)
    
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Servidor web del Dashboard iniciado en http://localhost:{port}")


async def custom_setup_hook():
    bot.loop.create_task(iniciar_servidor_web())

bot.setup_hook = custom_setup_hook


UTC = ZoneInfo("UTC")
TZ_CLUB = ZoneInfo(config.ZONA_HORARIA)


# ---------- Helpers generales ----------

def slugify(nombre: str) -> str:
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", nombre) if not unicodedata.combining(c))
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", sin_tildes).strip("-").lower()
    return slug[:80] or "trabajador"


def es_trabajador(member: discord.Member):
    nombres_roles = {r.name for r in member.roles}
    return bool(nombres_roles.intersection(config.ROLES_TRABAJADOR)) or es_liderazgo(member)


def es_liderazgo(member: discord.Member):
    return any(r.name == config.ROL_LIDERAZGO for r in member.roles)


def es_jefe_de(member: discord.Member, seccion_key: str):
    if es_liderazgo(member):
        return True
    rol_jefe = config.SECCIONES[seccion_key]["rol_jefe"]
    return any(r.name == rol_jefe for r in member.roles)


def es_algun_jefe(member: discord.Member):
    return es_liderazgo(member) or bool(secciones_de_jefe(member))


def secciones_de_jefe(member: discord.Member):
    if es_liderazgo(member):
        return list(config.SECCIONES.keys())
    nombres_roles = {r.name for r in member.roles}
    return [k for k, v in config.SECCIONES.items() if v["rol_jefe"] in nombres_roles]


def _hora_cierre():
    h, m = config.HORA_CIERRE_SEMANA.split(":")
    return int(h), int(m)


def rango_semana_actual():
    """Semana activa (todavía no pagada): (inicio_utc, fin_utc, inicio_local, fin_local)."""
    h, m = _hora_cierre()
    ahora_local = datetime.now(TZ_CLUB)
    dias_hasta_cierre = (config.DIA_CIERRE_SEMANA - ahora_local.weekday()) % 7
    fin_local = (ahora_local + timedelta(days=dias_hasta_cierre)).replace(hour=h, minute=m, second=0, microsecond=0)
    if fin_local < ahora_local:
        fin_local += timedelta(days=7)
    inicio_local = fin_local - timedelta(days=7)
    return (inicio_local.astimezone(UTC).replace(tzinfo=None),
            fin_local.astimezone(UTC).replace(tzinfo=None), inicio_local, fin_local)


def semana_recien_cerrada():
    """La última semana que ya terminó (para el corte automático)."""
    h, m = _hora_cierre()
    ahora_local = datetime.now(TZ_CLUB)
    dias_desde_cierre = (ahora_local.weekday() - config.DIA_CIERRE_SEMANA) % 7
    fin_local = (ahora_local - timedelta(days=dias_desde_cierre)).replace(hour=h, minute=m, second=0, microsecond=0)
    if fin_local > ahora_local:
        fin_local -= timedelta(days=7)
    inicio_local = fin_local - timedelta(days=7)
    return (inicio_local.astimezone(UTC).replace(tzinfo=None),
            fin_local.astimezone(UTC).replace(tzinfo=None), inicio_local, fin_local)


async def calcular_nomina(seccion_key: str, inicio_utc: datetime, fin_utc: datetime, solo_validados=True):
    seccion = config.SECCIONES[seccion_key]
    registros = await db.obtener_registros_semana(seccion_key, inicio_utc.isoformat(), fin_utc.isoformat(),
                                                    solo_validados=solo_validados)
    resumen = {}
    for r in registros:
        did = r["discord_id"]
        if did not in resumen:
            resumen[did] = {"nombre": r["nombre"], "horas": 0.0, "n_servicios": 0,
                             "monto_servicios": 0.0, "pago_horas": 0.0, "pago_comisiones": 0.0}
        if r["tipo"] == "horas":
            resumen[did]["horas"] += r["horas"]
        else:
            resumen[did]["n_servicios"] += 1
            resumen[did]["monto_servicios"] += r["monto"]
            resumen[did]["pago_comisiones"] += r["comision"]

    for datos in resumen.values():
        datos["pago_horas"] = round(datos["horas"] * seccion["tarifa_hora"], 2)
        datos["pago_comisiones"] = round(datos["pago_comisiones"], 2)
        datos["total"] = round(datos["pago_horas"] + datos["pago_comisiones"], 2)
    return resumen


def _peso(n):
    return f"${n:,.0f}".replace(",", ".")


def _embed_estado(validado: bool):
    return ("⏳ Pendiente de aprobación", discord.Color.orange()) if not validado else \
           ("✅ Aprobado", discord.Color.green())


SECCION_CHOICES = [app_commands.Choice(name=v["nombre_visible"], value=k) for k, v in config.SECCIONES.items()]


# ---------- Eventos ----------

@bot.event
async def on_ready():
    await db.iniciar_db()
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        if not revisar_cierre_semanal.is_running():
            revisar_cierre_semanal.start()
    else:
        await bot.tree.sync()
        print("Aviso: GUILD_ID no está configurado, el corte semanal automático no se puede activar "
              "(no sé en qué servidor postear).")
    print(f"SAMCRO bot conectado como {bot.user}")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != "✅" or payload.user_id == bot.user.id:
        return
    if not config.REQUIERE_APROBACION:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    registro = await db.registro_por_mensaje(str(payload.message_id))
    if not registro or registro["validado"]:
        return

    member = guild.get_member(payload.user_id) or await guild.fetch_member(payload.user_id)
    if not es_jefe_de(member, registro["seccion"]):
        return

    await db.aprobar_por_mensaje(str(payload.message_id), member.display_name)

    channel = guild.get_channel(payload.channel_id)
    try:
        mensaje = await channel.fetch_message(payload.message_id)
        if mensaje.embeds:
            embed = mensaje.embeds[0]
            embed.color = discord.Color.green()
            for i, field in enumerate(embed.fields):
                if field.name == "Estado":
                    embed.set_field_at(i, name="Estado", value=f"✅ Aprobado por {member.display_name}",
                                        inline=field.inline)
                    break
            await mensaje.edit(embed=embed)
    except discord.NotFound:
        pass


# ---------- Setup: canal personal de trabajador ----------

@bot.tree.command(name="crear-canal-trabajador",
                   description="(Jefes/Liderazgo) Crea el canal personal de bitácora de un trabajador")
@app_commands.describe(trabajador="La persona para la que se crea el canal")
async def crear_canal_trabajador(interaction: discord.Interaction, trabajador: discord.Member):
    if not es_algun_jefe(interaction.user):
        await interaction.response.send_message("Solo un jefe de área o Liderazgo puede crear canales.",
                                                  ephemeral=True)
        return
    if not es_trabajador(trabajador):
        await interaction.response.send_message(
            f"{trabajador.display_name} todavía no tiene ningún rol de trabajador. Asígnaselo primero.",
            ephemeral=True)
        return

    existente = await db.canal_de_trabajador(str(trabajador.id))
    if existente:
        canal_existente = interaction.guild.get_channel(int(existente["canal_id"]))
        await interaction.response.send_message(
            f"{trabajador.display_name} ya tiene su canal: "
            f"{canal_existente.mention if canal_existente else '#' + existente['nombre']}", ephemeral=True)
        return

    categoria = discord.utils.get(interaction.guild.categories, name=config.CATEGORIA_BITACORAS)
    if not categoria:
        await interaction.response.send_message(
            f'No encontré la categoría "{config.CATEGORIA_BITACORAS}". Créala primero en Discord con ese nombre '
            "exacto (Ajustes del servidor -> Canales -> Crear categoría).", ephemeral=True)
        return

    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        trabajador: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
        interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                            manage_messages=True),
    }
    for rname in [s["rol_jefe"] for s in config.SECCIONES.values()] + [config.ROL_LIDERAZGO]:
        rol = discord.utils.get(interaction.guild.roles, name=rname)
        if rol:
            overwrites[rol] = discord.PermissionOverwrite(view_channel=True, send_messages=True, add_reactions=True)

    nombre_canal = f"bitacora-{slugify(trabajador.display_name)}"
    canal = await interaction.guild.create_text_channel(
        nombre_canal, category=categoria, overwrites=overwrites,
        topic=f"Bitácora personal de {trabajador.display_name} — /registrar-horas y /registrar-servicio aquí.")

    await db.vincular_canal(str(canal.id), str(trabajador.id), trabajador.display_name)

    bienvenida = discord.Embed(
        title=f"👋 Bienvenido/a, {trabajador.display_name}",
        description="Este es tu canal personal de bitácora. Acá registras lo que hiciste durante la semana "
                     "(con foto/captura si puedes).",
        color=discord.Color.blurple())
    bienvenida.add_field(name="Registrar un servicio", value="`/registrar-servicio`", inline=False)
    bienvenida.add_field(name="Registrar horas trabajadas", value="`/registrar-horas`", inline=False)
    bienvenida.add_field(name="Ver tu resumen de la semana", value="`/mi-resumen`", inline=False)
    bienvenida.set_footer(
        text=f"Todos los domingos a las {config.HORA_CIERRE_SEMANA} se calcula tu pago automáticamente.")
    await canal.send(content=trabajador.mention, embed=bienvenida)

    await interaction.response.send_message(f"✅ Canal creado: {canal.mention}", ephemeral=True)


# ---------- Comandos: trabajador ----------

async def _validar_canal_personal(interaction: discord.Interaction):
    vinculo = await db.trabajador_de_canal(str(interaction.channel.id))
    if not vinculo:
        await interaction.response.send_message(
            "Este comando se usa dentro de tu canal personal de bitácora. Si todavía no tienes uno, pídele a tu "
            "jefe que lo cree con `/crear-canal-trabajador`.", ephemeral=True)
        return None
    if vinculo["discord_id"] != str(interaction.user.id):
        await interaction.response.send_message("Este es el canal personal de otro trabajador — usa el tuyo.",
                                                  ephemeral=True)
        return None
    return vinculo


@bot.tree.command(name="registrar-horas", description="Registra horas trabajadas (en tu canal personal)")
@app_commands.describe(seccion="Área en la que trabajaste", horas="Cantidad de horas (ej: 2.5)",
                        nota="Comentario opcional")
@app_commands.choices(seccion=SECCION_CHOICES)
async def registrar_horas(interaction: discord.Interaction, seccion: app_commands.Choice[str], horas: float,
                           nota: str = None):
    if not await _validar_canal_personal(interaction):
        return
    if not es_trabajador(interaction.user):
        await interaction.response.send_message("No tienes un rol de trabajador. Pídele a un admin que te lo asigne.",
                                                  ephemeral=True)
        return
    if horas <= 0 or horas > 16:
        await interaction.response.send_message("Ingresa un número de horas válido (entre 0 y 16).", ephemeral=True)
        return

    seccion_key = seccion.value
    info_seccion = config.SECCIONES[seccion_key]
    auto_validado = 0 if config.REQUIERE_APROBACION else 1
    registro_id = await db.registrar_horas(str(interaction.user.id), interaction.user.display_name, seccion_key,
                                            horas, nota, validado=auto_validado)

    estado_txt, color = _embed_estado(bool(auto_validado))
    embed = discord.Embed(title="🕒 Turno registrado", description=info_seccion["nombre_visible"], color=color)
    embed.add_field(name="Horas", value=f"{horas} h")
    embed.add_field(name="Pago estimado", value=_peso(horas * info_seccion["tarifa_hora"]))
    if nota:
        embed.add_field(name="Nota", value=nota, inline=False)
    embed.add_field(name="Estado", value=estado_txt, inline=False)
    if config.REQUIERE_APROBACION:
        embed.set_footer(text="El jefe del área aprueba reaccionando con ✅ a este mensaje.")

    await interaction.response.send_message(embed=embed)
    mensaje = await interaction.original_response()
    await db.set_mensaje(registro_id, str(mensaje.id), str(interaction.channel.id))
    if config.REQUIERE_APROBACION:
        await mensaje.add_reaction("✅")


@bot.tree.command(name="registrar-servicio", description="Registra un servicio que hiciste (en tu canal personal)")
@app_commands.describe(
    seccion="Área a la que pertenece este servicio",
    servicio="Qué hiciste (ej: 'Cambio de aceite', 'Tatuaje brazo', 'Show 20:00')",
    monto="Monto cobrado por el servicio",
    foto="Captura de pantalla de respaldo (recomendado)",
    nota="Comentario opcional",
)
@app_commands.choices(seccion=SECCION_CHOICES)
async def registrar_servicio(interaction: discord.Interaction, seccion: app_commands.Choice[str], servicio: str,
                              monto: float, foto: discord.Attachment = None, nota: str = None):
    if not await _validar_canal_personal(interaction):
        return
    if not es_trabajador(interaction.user):
        await interaction.response.send_message("No tienes un rol de trabajador. Pídele a un admin que te lo asigne.",
                                                  ephemeral=True)
        return
    if monto <= 0:
        await interaction.response.send_message("El monto debe ser mayor a 0.", ephemeral=True)
        return

    seccion_key = seccion.value
    info_seccion = config.SECCIONES[seccion_key]
    comision = round(monto * info_seccion["comision_servicio"], 2)
    foto_url = foto.url if foto else None
    auto_validado = 0 if config.REQUIERE_APROBACION else 1

    registro_id = await db.registrar_servicio(str(interaction.user.id), interaction.user.display_name, seccion_key,
                                                servicio, monto, comision, nota, foto_url, validado=auto_validado)

    estado_txt, color = _embed_estado(bool(auto_validado))
    embed = discord.Embed(title="🧾 Servicio registrado",
                           description=f"{info_seccion['nombre_visible']} — **{servicio}**", color=color)
    embed.add_field(name="Monto cobrado", value=_peso(monto))
    embed.add_field(name="Comisión", value=_peso(comision))
    if nota:
        embed.add_field(name="Nota", value=nota, inline=False)
    embed.add_field(name="Estado", value=estado_txt, inline=False)
    if foto_url:
        embed.set_image(url=foto_url)
    if config.REQUIERE_APROBACION:
        embed.set_footer(text="El jefe del área aprueba reaccionando con ✅ a este mensaje.")

    await interaction.response.send_message(embed=embed)
    mensaje = await interaction.original_response()
    await db.set_mensaje(registro_id, str(mensaje.id), str(interaction.channel.id))
    if config.REQUIERE_APROBACION:
        await mensaje.add_reaction("✅")


@bot.tree.command(name="mi-resumen", description="Ve cuánto llevas ganado esta semana (aprobado y pendiente)")
async def mi_resumen(interaction: discord.Interaction):
    inicio_utc, fin_utc, inicio_local, fin_local = rango_semana_actual()
    total_aprobado = 0.0
    total_pendiente = 0.0
    detalle = []

    for seccion_key, seccion_info in config.SECCIONES.items():
        aprobados = await calcular_nomina(seccion_key, inicio_utc, fin_utc, solo_validados=True)
        todos = await calcular_nomina(seccion_key, inicio_utc, fin_utc, solo_validados=False)
        d_ap = aprobados.get(str(interaction.user.id))
        d_todos = todos.get(str(interaction.user.id))
        if d_ap:
            total_aprobado += d_ap["total"]
        if d_todos:
            pendiente_neto = d_todos["total"] - (d_ap["total"] if d_ap else 0)
            if pendiente_neto > 0:
                total_pendiente += pendiente_neto
        if d_ap or d_todos:
            detalle.append(seccion_info["nombre_visible"])

    if not detalle:
        await interaction.response.send_message(
            "Todavía no tienes registros esta semana. Usa `/registrar-horas` o `/registrar-servicio` en tu canal "
            "personal.", ephemeral=True)
        return

    embed = discord.Embed(title="📋 Tu resumen de la semana", color=discord.Color.blurple())
    embed.add_field(name="Áreas con registros", value=", ".join(detalle), inline=False)
    embed.add_field(name="✅ Aprobado (te lo pagan)", value=_peso(total_aprobado))
    if config.REQUIERE_APROBACION:
        embed.add_field(name="⏳ Pendiente de aprobación", value=_peso(total_pendiente))
    embed.set_footer(text=f"Semana: {inicio_local.strftime('%d-%m')} al {fin_local.strftime('%d-%m %H:%M')}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- Comandos: jefe / liderazgo ----------

def _validar_acceso_seccion(interaction: discord.Interaction, seccion_solicitada: str = None):
    if es_liderazgo(interaction.user):
        return list(config.SECCIONES.keys()) if not seccion_solicitada else [seccion_solicitada]
    jefe_de = secciones_de_jefe(interaction.user)
    if not jefe_de:
        return None
    if seccion_solicitada:
        return [seccion_solicitada] if seccion_solicitada in jefe_de else None
    return jefe_de


@bot.tree.command(name="nomina", description="(Jefes/Liderazgo) Ve la nómina de la semana de tu sección")
@app_commands.describe(seccion="Sección a consultar (Liderazgo puede ver cualquiera)")
@app_commands.choices(seccion=SECCION_CHOICES)
async def nomina(interaction: discord.Interaction, seccion: app_commands.Choice[str] = None):
    seccion_key = seccion.value if seccion else None
    secciones_permitidas = _validar_acceso_seccion(interaction, seccion_key)
    if not secciones_permitidas:
        await interaction.response.send_message(
            "No tienes permiso para ver esto (necesitas ser jefe de una sección o Liderazgo).", ephemeral=True)
        return

    inicio_utc, fin_utc, inicio_local, fin_local = rango_semana_actual()
    await interaction.response.defer(ephemeral=True)

    for sec_key in secciones_permitidas:
        resumen = await calcular_nomina(sec_key, inicio_utc, fin_utc, solo_validados=True)
        pendientes = await db.contar_pendientes(sec_key, inicio_utc.isoformat(), fin_utc.isoformat())
        seccion_info = config.SECCIONES[sec_key]

        embed = discord.Embed(
            title=f"💰 Nómina — {seccion_info['nombre_visible']}",
            description=f"Semana {inicio_local.strftime('%d-%m')} al {fin_local.strftime('%d-%m %H:%M')}"
                         + (f"\n⏳ {pendientes} registro(s) esperando tu aprobación (✅)" if pendientes else ""),
            color=discord.Color.gold(),
        )
        if not resumen:
            embed.description += "\n\nSin registros aprobados esta semana."
        else:
            total_seccion = 0
            for datos in sorted(resumen.values(), key=lambda d: -d["total"]):
                total_seccion += datos["total"]
                valor = (f"Horas: {datos['horas']} h ({_peso(datos['pago_horas'])}) · "
                         f"Servicios: {datos['n_servicios']} ({_peso(datos['pago_comisiones'])})\n"
                         f"**Total a pagar: {_peso(datos['total'])}**")
                embed.add_field(name=datos["nombre"], value=valor, inline=False)
            embed.set_footer(text=f"Total sección: {_peso(total_seccion)}")

        await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="exportar-nomina", description="(Jefes/Liderazgo) Exporta la nómina de la semana como CSV")
@app_commands.choices(seccion=SECCION_CHOICES)
async def exportar_nomina(interaction: discord.Interaction, seccion: app_commands.Choice[str]):
    secciones_permitidas = _validar_acceso_seccion(interaction, seccion.value)
    if not secciones_permitidas:
        await interaction.response.send_message("No tienes permiso para exportar esta sección.", ephemeral=True)
        return

    inicio_utc, fin_utc, _, _ = rango_semana_actual()
    resumen = await calcular_nomina(seccion.value, inicio_utc, fin_utc, solo_validados=True)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Nombre", "Horas", "Pago horas", "N servicios", "Comisiones", "TOTAL"])
    for datos in resumen.values():
        writer.writerow([datos["nombre"], datos["horas"], datos["pago_horas"],
                          datos["n_servicios"], datos["pago_comisiones"], datos["total"]])
    buffer.seek(0)

    file = discord.File(io.BytesIO(buffer.getvalue().encode("utf-8")),
                         filename=f"nomina_{seccion.value}_{inicio_utc.strftime('%Y%m%d')}.csv")
    await interaction.response.send_message(
        f"Nómina de {config.SECCIONES[seccion.value]['nombre_visible']} lista:", file=file, ephemeral=True)


@bot.tree.command(name="marcar-pagado",
                   description="(Jefes/Liderazgo) Deja registro de que ya pagaste la nómina de esta semana")
@app_commands.choices(seccion=SECCION_CHOICES)
async def marcar_pagado(interaction: discord.Interaction, seccion: app_commands.Choice[str]):
    secciones_permitidas = _validar_acceso_seccion(interaction, seccion.value)
    if not secciones_permitidas:
        await interaction.response.send_message("No tienes permiso para esto.", ephemeral=True)
        return

    import json
    inicio_utc, fin_utc, _, _ = rango_semana_actual()
    resumen = await calcular_nomina(seccion.value, inicio_utc, fin_utc, solo_validados=True)
    await db.guardar_pago(seccion.value, inicio_utc.isoformat(), fin_utc.isoformat(),
                           interaction.user.display_name, json.dumps(resumen, ensure_ascii=False))

    total = sum(d["total"] for d in resumen.values())
    await interaction.response.send_message(
        f"✅ Marcado como pagado: {config.SECCIONES[seccion.value]['nombre_visible']} — "
        f"{_peso(total)} repartidos entre {len(resumen)} persona(s).", ephemeral=True)


@bot.tree.command(name="tarifas", description="Ve las tarifas vigentes de una sección")
@app_commands.choices(seccion=SECCION_CHOICES)
async def tarifas(interaction: discord.Interaction, seccion: app_commands.Choice[str]):
    info = config.SECCIONES[seccion.value]
    embed = discord.Embed(title=f"Tarifas — {info['nombre_visible']}", color=discord.Color.blue())
    embed.add_field(name="Pago por hora", value=_peso(info["tarifa_hora"]))
    embed.add_field(name="Comisión por servicio", value=f"{info['comision_servicio']*100:.0f}%")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- Corte semanal automático ----------

@tasks.loop(minutes=10)
async def revisar_cierre_semanal():
    inicio_utc, fin_utc, inicio_local, fin_local = semana_recien_cerrada()
    clave_semana = fin_local.isoformat()
    ultima = await db.obtener_estado("ultima_semana_pagada")
    if ultima == clave_semana:
        return

    guild = bot.get_guild(int(GUILD_ID))
    if not guild:
        return

    print(f"Procesando corte semanal: {inicio_local:%d-%m} al {fin_local:%d-%m %H:%M}")

    # 1) Resumen personal en el canal de cada trabajador
    canales = await db.todos_los_canales_trabajador()
    for canal_info in canales:
        channel = guild.get_channel(int(canal_info["canal_id"]))
        if not channel:
            continue
        registros = await db.obtener_registros_discord_id_semana(
            canal_info["discord_id"], inicio_utc.isoformat(), fin_utc.isoformat(), solo_validados=True)
        if not registros:
            continue

        por_seccion = {}
        total_general = 0.0
        for r in registros:
            sk = r["seccion"]
            tarifa = config.SECCIONES[sk]["tarifa_hora"]
            por_seccion.setdefault(sk, {"horas": 0.0, "pago_horas": 0.0, "n_servicios": 0, "comisiones": 0.0})
            if r["tipo"] == "horas":
                por_seccion[sk]["horas"] += r["horas"]
                por_seccion[sk]["pago_horas"] += r["horas"] * tarifa
            else:
                por_seccion[sk]["n_servicios"] += 1
                por_seccion[sk]["comisiones"] += r["comision"]

        embed = discord.Embed(
            title="💰 Pago semanal",
            description=f"Semana {inicio_local.strftime('%d-%m')} al {fin_local.strftime('%d-%m %H:%M')}",
            color=discord.Color.green(),
        )
        for sk, d in por_seccion.items():
            sub_total = d["pago_horas"] + d["comisiones"]
            total_general += sub_total
            embed.add_field(
                name=config.SECCIONES[sk]["nombre_visible"],
                value=f"{d['horas']} h ({_peso(d['pago_horas'])}) + {d['n_servicios']} servicios "
                      f"({_peso(d['comisiones'])}) = **{_peso(sub_total)}**",
                inline=False)
        embed.add_field(name="TOTAL A PAGAR", value=f"**{_peso(total_general)}**", inline=False)
        embed.set_footer(text="Este monto ya fue aprobado por tu jefe. Avísale para coordinar el pago.")
        try:
            await channel.send(content=f"<@{canal_info['discord_id']}>", embed=embed)
        except discord.Forbidden:
            pass

    # 2) Resumen consolidado para jefes/liderazgo
    canal_resumen = discord.utils.get(guild.text_channels, name=config.CANAL_RESUMEN_NOMINA)
    if canal_resumen:
        total_club = 0.0
        for sk, info_seccion in config.SECCIONES.items():
            resumen = await calcular_nomina(sk, inicio_utc, fin_utc, solo_validados=True)
            if not resumen:
                continue
            total_seccion = sum(d["total"] for d in resumen.values())
            total_club += total_seccion
            embed = discord.Embed(
                title=f"💰 Cierre semanal — {info_seccion['nombre_visible']}",
                description=f"Semana {inicio_local.strftime('%d-%m')} al {fin_local.strftime('%d-%m %H:%M')}",
                color=discord.Color.gold())
            for datos in sorted(resumen.values(), key=lambda d: -d["total"]):
                embed.add_field(name=datos["nombre"], value=_peso(datos["total"]), inline=True)
            embed.set_footer(text=f"Total {info_seccion['nombre_visible']}: {_peso(total_seccion)}")
            await canal_resumen.send(embed=embed)
        await canal_resumen.send(f"**🏍️ Total a pagar en todo el club esta semana: {_peso(total_club)}**")
    else:
        print(f'Aviso: no encontré el canal "{config.CANAL_RESUMEN_NOMINA}" para el resumen consolidado.')

    await db.guardar_estado("ultima_semana_pagada", clave_semana)


@revisar_cierre_semanal.before_loop
async def antes_de_revisar():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Falta DISCORD_TOKEN en tu archivo .env (copia .env.example y complétalo).")
    bot.run(TOKEN)
