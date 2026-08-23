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
import asyncio
import base64
import csv
import io
import json
import os
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiohttp
from aiohttp import web

import config
import database as db

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

intents = discord.Intents.default()
intents.members = True
intents.reactions = True
intents.message_content = True

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
        user = request["user"]
        user_permisos = json.loads(user["permisos"])
        
        inicio_utc, fin_utc, _, _ = rango_semana_actual()
        guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
        data = {}
        for seccion_key in config.SECCIONES.keys():
            if seccion_key in user_permisos or "consolidado" in user_permisos:
                resumen = await calcular_nomina(seccion_key, inicio_utc, fin_utc, solo_validados=True, guild=guild)
                data[seccion_key] = [
                    {
                        "nombre": v["nombre"],
                        "horas": v["horas"],
                        "pagoHoras": v["pago_horas"],
                        "nServicios": v["n_servicios"],
                        "comisiones": v["pago_comisiones"],
                        "total": v["total"],
                        "esJefe": v.get("es_jefe", False)
                    }
                    for v in resumen.values()
                ]
            else:
                data[seccion_key] = []
        turnos_activos = await db.obtener_todos_turnos_activos()
        data["_turnos_activos"] = turnos_activos
        return web.json_response(data)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_detalles(request):
    try:
        user = request["user"]
        user_permisos = json.loads(user["permisos"])
        
        seccion_key = request.match_info.get("seccion")
        username = request.match_info.get("username")
        
        if not (seccion_key in user_permisos or "consolidado" in user_permisos):
            return web.json_response({"error": "No autorizado para esta sección"}, status=403)
            
        inicio_utc, fin_utc, _, _ = rango_semana_actual()
        
        registros = await db.obtener_registros_semana(seccion_key, inicio_utc.isoformat(), fin_utc.isoformat(), solo_validados=False)
        detalles = [r for r in registros if r.get("nombre") == username or r.get("discord_id") == username]
        
        turnos_activos = await db.obtener_todos_turnos_activos()
        for ta in turnos_activos:
            if ta["nombre"] == username or ta["discord_id"] == username:
                detalles.insert(0, {
                    "id": "activo",
                    "tipo": "horas",
                    "horas": 0,
                    "servicio_nombre": None,
                    "monto": 0,
                    "comision": 0,
                    "nota": f"🟢 Turno activo en {config.SECCIONES.get(ta['seccion'], {}).get('nombre_visible', ta['seccion'])}",
                    "foto_url": None,
                    "validado": 2,
                    "validado_por": None,
                    "creado_en": ta["hora_inicio"]
                })
                break
                
        return web.json_response(detalles)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_anular_registro(request):
    try:
        user = request["user"]
        registro_id = int(request.match_info.get("id"))
        payload = await request.json()
        anular = payload.get("anular", True)
        
        reg = await db.obtener_registro_por_id(registro_id)
        if not reg:
            return web.json_response({"error": "Registro no encontrado"}, status=404)
            
        user_permisos = json.loads(user["permisos"])
        if not (reg["seccion"] in user_permisos or "consolidado" in user_permisos):
            return web.json_response({"error": "No autorizado para esta sección"}, status=403)
            
        if anular:
            res = await db.anular_registro(registro_id, user["username"])
        else:
            res = await db.restaurar_registro(registro_id, user["username"])
            
        return web.json_response({"success": True, "registro": res})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_login(request):
    try:
        payload = await request.json()
        username = payload.get("username", "").strip()
        password = payload.get("password", "")
        
        if not username or not password:
            return web.json_response({"error": "Faltan credenciales"}, status=400)
            
        user = await db.obtener_usuario(username)
        if not user or not db.verify_password(user["password_hash"], password):
            return web.json_response({"error": "Usuario o contraseña incorrectos"}, status=401)
            
        session_id = str(uuid.uuid4())
        expira_en = (datetime.utcnow() + timedelta(days=7)).isoformat()
        await db.crear_sesion(session_id, username, expira_en)
        
        response = web.json_response({
            "username": user["username"],
            "rol": user["rol"],
            "permisos": json.loads(user["permisos"]),
            "debe_cambiar_password": bool(user.get("debe_cambiar_password", 0))
        })
        response.set_cookie(
            "session_id",
            session_id,
            max_age=7 * 24 * 60 * 60,
            httponly=True,
            samesite="Lax"
        )
        return response
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_logout(request):
    session_id = request.cookies.get("session_id")
    if session_id:
        await db.eliminar_sesion(session_id)
    response = web.json_response({"success": True})
    response.del_cookie("session_id")
    return response


async def handle_api_me(request):
    user = request["user"]
    db_user = await db.obtener_usuario(user["username"])
    target_user = db_user if db_user else user
    return web.json_response({
        "username": target_user["username"],
        "rol": target_user["rol"],
        "permisos": json.loads(target_user["permisos"]),
        "debe_cambiar_password": bool(target_user.get("debe_cambiar_password", 0))
    })


async def handle_listar_usuarios(request):
    user = request["user"]
    if user["rol"] != "superadmin":
        return web.json_response({"error": "No autorizado"}, status=403)
        
    try:
        usuarios = await db.listar_usuarios()
        for u in usuarios:
            u["permisos"] = json.loads(u["permisos"])
        return web.json_response(usuarios)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_crear_usuario(request):
    user = request["user"]
    if user["rol"] != "superadmin":
        return web.json_response({"error": "No autorizado"}, status=403)
        
    try:
        payload = await request.json()
        username = payload.get("username", "").strip()
        password = payload.get("password", "")
        rol = payload.get("rol", "jefe")
        permisos = payload.get("permisos", [])
        
        if not username or not password:
            return web.json_response({"error": "Faltan campos obligatorios"}, status=400)
            
        await db.crear_usuario(username, password, rol, json.dumps(permisos), debe_cambiar_password=1)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_cambiar_mi_password(request):
    try:
        user = request["user"]
        payload = await request.json()
        nueva_pass = payload.get("nueva_password", "").strip()
        
        if not nueva_pass or len(nueva_pass) < 4:
            return web.json_response({"error": "La contraseña debe tener al menos 4 caracteres"}, status=400)
            
        await db.cambiar_password_usuario(user["username"], nueva_pass)
        return web.json_response({"success": True, "mensaje": "Contraseña actualizada exitosamente"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_eliminar_usuario(request):
    user = request["user"]
    if user["rol"] != "superadmin":
        return web.json_response({"error": "No autorizado"}, status=403)
        
    try:
        username_to_delete = request.match_info.get("username")
        if username_to_delete == user["username"]:
            return web.json_response({"error": "No puedes eliminarte a ti mismo"}, status=400)
            
        target = await db.obtener_usuario(username_to_delete)
        if not target:
            return web.json_response({"error": "El usuario no existe"}, status=404)
            
        await db.eliminar_usuario(username_to_delete)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_editar_usuario(request):
    user = request["user"]
    if user["rol"] != "superadmin":
        return web.json_response({"error": "No autorizado"}, status=403)
        
    try:
        username_to_edit = request.match_info.get("username")
        payload = await request.json()
        rol = payload.get("rol", "jefe")
        permisos = payload.get("permisos", [])
        password = payload.get("password", None)
        
        target = await db.obtener_usuario(username_to_edit)
        if not target:
            return web.json_response({"error": "El usuario no existe"}, status=404)
            
        await db.actualizar_usuario(username_to_edit, rol, json.dumps(permisos), password)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_limpiar_bdd(request):
    user = request["user"]
    if user["rol"] != "superadmin":
        return web.json_response({"error": "Solo el SuperAdmin puede realizar esta acción"}, status=403)
        
    try:
        await db.limpiar_registros_bdd()
        return web.json_response({"success": True, "mensaje": "Se borraron todos los registros manteniendo intactas las cuentas de usuario."})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def auth_middleware(app, handler):
    async def middleware(request):
        public_paths = ["/", "/api/login", "/api/logout"]
        if request.path in public_paths or request.path.startswith("/logo/"):
            return await handler(request)
            
        session_id = request.cookies.get("session_id")
        session = None
        if session_id:
            session = await db.verificar_sesion(session_id)
            
        if not session:
            return web.json_response({"error": "No autorizado"}, status=401)
            
        user = await db.obtener_usuario(session["username"])
        if not user:
            return web.json_response({"error": "Usuario no encontrado"}, status=401)
            
        request["user"] = user
        return await handler(request)
    return middleware


async def iniciar_servidor_web():
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_get("/", handle_dashboard)
    app.router.add_post("/api/login", handle_api_login)
    app.router.add_post("/api/logout", handle_api_logout)
    app.router.add_get("/api/me", handle_api_me)
    app.router.add_get("/api/nomina", handle_api_nomina)
    app.router.add_get("/api/detalles/{seccion}/{username}", handle_api_detalles)
    
    app.router.add_get("/api/usuarios", handle_listar_usuarios)
    app.router.add_post("/api/usuarios", handle_crear_usuario)
    app.router.add_put("/api/usuarios/{username}", handle_editar_usuario)
    app.router.add_delete("/api/usuarios/{username}", handle_eliminar_usuario)
    app.router.add_put("/api/registros/{id}/anular", handle_anular_registro)
    app.router.add_put("/api/mi-password", handle_cambiar_mi_password)
    app.router.add_post("/api/admin/limpiar-bdd", handle_limpiar_bdd)
    
    os.makedirs("uploads", exist_ok=True)
    app.router.add_static("/uploads", "uploads")
    app.router.add_static("/logo", "LOGO")
    
    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Servidor web del Dashboard iniciado en http://localhost:{port}")


async def custom_setup_hook():
    await db.iniciar_db()
    admin_user = os.getenv("DASHBOARD_USER") or os.getenv("ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("DASHBOARD_PASS") or os.getenv("ADMIN_PASSWORD", "samcro2026")
    existing = await db.obtener_usuario(admin_user)
    if not existing:
        await db.crear_usuario(admin_user, admin_pass, "superadmin", json.dumps(["consolidado", "mecanica", "bar", "tatuajes", "show"]), debe_cambiar_password=0)
    bot.loop.create_task(iniciar_servidor_web())
    bot.add_view(PanelControlView())
    bot.add_view(FinalizarTurnoView())

    # Sincronización de comandos slash — se hace UNA SOLA VEZ aquí en setup_hook,
    # nunca en on_ready, para evitar rate limits de Discord/Cloudflare al reconectar.
    async def _sync_commands():
        await bot.wait_until_ready()
        if GUILD_ID:
            try:
                guild_id_int = int(str(GUILD_ID).strip())
                guild = discord.Object(id=guild_id_int)
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                print(f"✅ Comandos Slash sincronizados al servidor {guild_id_int}: {len(synced)} comandos.")
            except Exception as e:
                print(f"❌ Error al sincronizar comandos al servidor: {e}")
        else:
            try:
                synced = await bot.tree.sync()
                print(f"✅ Comandos Slash sincronizados globalmente: {len(synced)} comandos.")
            except Exception as e:
                print(f"❌ Error al sincronizar comandos globales: {e}")

    bot.loop.create_task(_sync_commands())



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
    if isinstance(config.ROL_LIDERAZGO, (list, tuple, set)):
        return any(r.name in config.ROL_LIDERAZGO for r in member.roles)
    return any(r.name == config.ROL_LIDERAZGO for r in member.roles)


def es_jefe_de(member: discord.Member, seccion_key: str):
    if es_liderazgo(member):
        return True
    rol_jefe = config.SECCIONES[seccion_key]["rol_jefe"]
    if isinstance(rol_jefe, (list, tuple, set)):
        return any(r.name in rol_jefe for r in member.roles)
    return any(r.name == rol_jefe for r in member.roles)


def es_algun_jefe(member: discord.Member):
    return es_liderazgo(member) or bool(secciones_de_jefe(member))


def secciones_de_jefe(member: discord.Member):
    if es_liderazgo(member):
        return list(config.SECCIONES.keys())
    nombres_roles = {r.name for r in member.roles}
    resultado = []
    for k, v in config.SECCIONES.items():
        rj = v["rol_jefe"]
        if isinstance(rj, (list, tuple, set)):
            if any(r in nombres_roles for r in rj):
                resultado.append(k)
        elif rj in nombres_roles:
            resultado.append(k)
    return resultado


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


def es_jefe_de_seccion(member: discord.Member, seccion_key: str) -> bool:
    if not member or not hasattr(member, "roles"):
        return False
    if es_liderazgo(member):
        return True
    roles_jefe = config.SECCIONES.get(seccion_key, {}).get("rol_jefe", [])
    if isinstance(roles_jefe, str):
        roles_jefe = [roles_jefe]
    member_role_names = {r.name for r in member.roles}
    return bool(member_role_names.intersection(roles_jefe))


def obtener_porcentaje_comision(member: discord.Member, seccion_key: str, servicio_nombre: str = "") -> float:
    if member and hasattr(member, "roles"):
        if es_jefe_de_seccion(member, seccion_key):
            return config.PORCENTAJES_JEFE.get(seccion_key, 0.40)
        for role in member.roles:
            if role.name in config.PORCENTAJE_COMISION_POR_ROL:
                return config.PORCENTAJE_COMISION_POR_ROL[role.name]

    if seccion_key == "tatuajes":
        if "blackout" in (servicio_nombre or "").lower():
            return 0.20
        return 0.30

    return config.SECCIONES.get(seccion_key, {}).get("comision_servicio", 0.30)


async def obtener_trabajadores_activos_con_canal(guild: discord.Guild = None):
    """
    Retorna un diccionario {discord_id: info_canal} con los trabajadores que actualmente
    tienen un canal de bitácora activo existente en Discord.
    Desvincula automáticamente los canales que hayan sido borrados de Discord.
    """
    canales = await db.todos_los_canales_trabajador()
    activos = {}
    if not guild and GUILD_ID:
        try:
            guild = bot.get_guild(int(GUILD_ID))
        except Exception:
            guild = None
    
    for c in canales:
        canal_id_str = c["canal_id"]
        if guild:
            canal_obj = guild.get_channel(int(canal_id_str))
            if not canal_obj:
                try:
                    await db.desvincular_canal(canal_id_str)
                except Exception:
                    pass
                continue
        activos[c["discord_id"]] = c
    return activos


async def calcular_nomina(seccion_key: str, inicio_utc: datetime, fin_utc: datetime, solo_validados=True, guild: discord.Guild = None):
    seccion = config.SECCIONES[seccion_key]
    registros = await db.obtener_registros_semana(seccion_key, inicio_utc.isoformat(), fin_utc.isoformat(),
                                                    solo_validados=solo_validados)
    
    trabajadores_activos = await obtener_trabajadores_activos_con_canal(guild)
    resumen = {}
    
    # 1. Incluir a todos los trabajadores con bitácora activa en la lista (incluso si tienen $0)
    for did, info in trabajadores_activos.items():
        resumen[did] = {
            "nombre": info["nombre"],
            "horas": 0.0,
            "n_servicios": 0,
            "monto_servicios": 0.0,
            "pago_horas": 0.0,
            "pago_comisiones": 0.0,
            "es_jefe": False,
            "discord_id": did
        }

    # 2. Acumular registros solo para trabajadores con canal activo (los borrados se omiten)
    for r in registros:
        did = r["discord_id"]
        if did not in trabajadores_activos:
            continue
            
        if did not in resumen:
            resumen[did] = {
                "nombre": r["nombre"],
                "horas": 0.0,
                "n_servicios": 0,
                "monto_servicios": 0.0,
                "pago_horas": 0.0,
                "pago_comisiones": 0.0,
                "es_jefe": False,
                "discord_id": did
            }
            
        if r["tipo"] == "horas":
            resumen[did]["horas"] += r["horas"]
        else:
            resumen[did]["n_servicios"] += 1
            resumen[did]["monto_servicios"] += r["monto"]
            resumen[did]["pago_comisiones"] += r["comision"]

    for did, datos in resumen.items():
        member = None
        if guild and did.isdigit():
            member = guild.get_member(int(did))
            if not member:
                try:
                    member = await guild.fetch_member(int(did))
                except Exception:
                    member = None

        es_jefe = es_jefe_de_seccion(member, seccion_key) if member else False
        if not es_jefe:
            target_user = await db.obtener_usuario(datos["nombre"])
            if target_user and target_user.get("rol") in ["superadmin", "jefe"]:
                es_jefe = True

        datos["es_jefe"] = es_jefe

        if es_jefe:
            if datos["horas"] >= config.HORAS_MINIMAS_SUELDO_BASE:
                datos["pago_horas"] = float(config.SUELDO_BASE_JEFE)
            else:
                datos["pago_horas"] = 0.0
        elif seccion.get("usa_sueldo_base"):
            if datos["horas"] >= config.HORAS_MINIMAS_SUELDO_BASE:
                datos["pago_horas"] = float(config.SUELDO_BASE_TRABAJADOR)
            else:
                datos["pago_horas"] = 0.0
        else:
            datos["pago_horas"] = round(datos["horas"] * seccion.get("tarifa_hora", 0), 2)

        datos["pago_comisiones"] = round(datos["pago_comisiones"], 2)
        datos["total"] = round(datos["pago_horas"] + datos["pago_comisiones"], 2)

    return resumen


def _peso(n):
    return f"${n:,.0f}".replace(",", ".")


def _embed_estado(validado: bool):
    return ("⏳ Pendiente de aprobación", discord.Color.orange()) if not validado else \
           ("✅ Aprobado", discord.Color.green())


async def procesar_attachment_a_base64(att: discord.Attachment) -> str:
    """
    Lee los datos brutos de la imagen directamente del Attachment de Discord
    usando la conexión autenticada de discord.py y la convierte en Data URL Base64.
    Se almacena permanentemente en la BDD para evitar caducidad de enlaces o borrados.
    """
    if not att:
        return None
    try:
        data = await att.read()
        ext = att.filename.split(".")[-1].lower() if "." in att.filename else "png"
        if ext == "jpg":
            ext = "jpeg"
        if ext not in ["png", "jpeg", "webp", "gif"]:
            ext = "png"
            
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img_fmt = "PNG" if ext == "png" else "JPEG"
            img.save(buf, format=img_fmt, quality=80, optimize=True)
            data = buf.getvalue()
        except Exception as pe:
            print(f"Aviso al optimizar imagen con Pillow: {pe}")

        b64_str = base64.b64encode(data).decode('utf-8')
        return f"data:image/{ext};base64,{b64_str}"
    except Exception as e:
        print(f"Error procesando attachment a Base64: {e}")
        return getattr(att, "url", None)


SECCION_CHOICES = [app_commands.Choice(name=v["nombre_visible"], value=k) for k, v in config.SECCIONES.items()]


# ---------- Modals y UI para el Panel de Control ----------

class HorasModal(discord.ui.Modal):
    def __init__(self, seccion_key: str):
        self.seccion_key = seccion_key
        info_seccion = config.SECCIONES[seccion_key]
        super().__init__(title=f"Registrar Horas: {info_seccion['nombre_visible']}")

        self.horas_input = discord.ui.TextInput(
            label="Cantidad de Horas Trabajadas",
            placeholder="Ej: 4.5 o 8",
            min_length=1,
            max_length=5,
            required=True
        )
        self.nota_input = discord.ui.TextInput(
            label="Nota / Comentario (Opcional)",
            style=discord.TextStyle.long,
            placeholder="Escribe alguna observación...",
            required=False,
            max_length=100
        )
        self.add_item(self.horas_input)
        self.add_item(self.nota_input)

    async def on_submit(self, interaction: discord.Interaction):
        vinculo = await db.trabajador_de_canal(str(interaction.channel.id))
        if not vinculo or vinculo["discord_id"] != str(interaction.user.id):
            await interaction.response.send_message("Este panel solo puede ser usado por el dueño de la bitácora.", ephemeral=True)
            return

        try:
            horas = float(self.horas_input.value.replace(",", "."))
        except ValueError:
            await interaction.response.send_message("Por favor, ingresa un número de horas válido.", ephemeral=True)
            return

        if horas <= 0 or horas > 16:
            await interaction.response.send_message("Ingresa un número de horas válido (entre 0 y 16).", ephemeral=True)
            return

        seccion_key = self.seccion_key
        info_seccion = config.SECCIONES[seccion_key]
        auto_validado = 0 if config.REQUIERE_APROBACION else 1
        nota = self.nota_input.value.strip() or None

        registro_id = await db.registrar_horas(
            str(interaction.user.id),
            interaction.user.display_name,
            seccion_key,
            horas,
            nota,
            validado=auto_validado
        )

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


class ServicioModal(discord.ui.Modal):
    def __init__(self, seccion_key: str):
        self.seccion_key = seccion_key
        info_seccion = config.SECCIONES[seccion_key]
        super().__init__(title=f"Registrar Servicio: {info_seccion['nombre_visible']}")

        self.servicio_input = discord.ui.TextInput(
            label="Nombre del Servicio / Venta",
            placeholder="Ej: Cambio de aceite, Tatuaje brazo, Show 22:00",
            min_length=2,
            max_length=50,
            required=True
        )
        self.monto_input = discord.ui.TextInput(
            label="Monto Cobrado ($)",
            placeholder="Ej: 50000 (sin puntos ni signos)",
            min_length=1,
            max_length=10,
            required=True
        )
        self.nota_input = discord.ui.TextInput(
            label="Nota / Comentario (Opcional)",
            style=discord.TextStyle.long,
            placeholder="Escribe alguna observación...",
            required=False,
            max_length=100
        )
        
        self.add_item(self.servicio_input)
        self.add_item(self.monto_input)
        self.add_item(self.nota_input)

    async def on_submit(self, interaction: discord.Interaction):
        vinculo = await db.trabajador_de_canal(str(interaction.channel.id))
        if not vinculo or vinculo["discord_id"] != str(interaction.user.id):
            await interaction.response.send_message("Este panel solo puede ser usado por el dueño de la bitácora.", ephemeral=True)
            return

        try:
            monto = float(self.monto_input.value.strip())
        except ValueError:
            await interaction.response.send_message("Por favor, ingresa un monto válido (solo números).", ephemeral=True)
            return

        if monto <= 0:
            await interaction.response.send_message("El monto debe ser mayor a 0.", ephemeral=True)
            return

        # Responder de inmediato de forma efímera para no expirar la interacción (límite de 3 segundos)
        await interaction.response.send_message(
            "🧾 **Datos recibidos.**\n"
            "Por favor, **sube la foto/captura de pantalla** de respaldo en este canal en los próximos 60 segundos (o escribe `omitir` si no tienes foto).",
            ephemeral=True
        )

        def check(m):
            return m.channel.id == interaction.channel.id and m.author.id == interaction.user.id

        foto_url = None
        foto_url_db = None
        try:
            msg = await bot.wait_for('message', check=check, timeout=60.0)
            if msg.attachments:
                att = msg.attachments[0]
                foto_url = att.url
                foto_url_db = att.url
            elif msg.content.lower().strip() in ['omitir', 'no', 'ninguna', 'skip']:
                try:
                    await msg.delete()
                except Exception:
                    pass
        except asyncio.TimeoutError:
            pass

        seccion_key = self.seccion_key
        info_seccion = config.SECCIONES[seccion_key]
        servicio = self.servicio_input.value.strip()
        porcentaje_comision = obtener_porcentaje_comision(interaction.user, seccion_key, servicio)
        comision = round(monto * porcentaje_comision, 2)
        auto_validado = 0 if config.REQUIERE_APROBACION else 1
        nota = self.nota_input.value.strip() or None

        registro_id = await db.registrar_servicio(
            str(interaction.user.id),
            interaction.user.display_name,
            seccion_key,
            servicio,
            monto,
            comision,
            nota,
            foto_url_db,
            validado=auto_validado
        )

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

        canal = interaction.channel
        mensaje = await canal.send(embed=embed)
        await db.set_mensaje(registro_id, str(mensaje.id), str(interaction.channel.id))
        if config.REQUIERE_APROBACION:
            await mensaje.add_reaction("✅")


class IniciarTurnoDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=v["nombre_visible"], value=k, description=f"Tarifa: {_peso(v['tarifa_hora'])}/h")
            for k, v in config.SECCIONES.items()
        ]
        super().__init__(
            custom_id="select_iniciar_turno_panel",
            placeholder="🟢 Iniciar Turno de Trabajo en...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        vinculo = await db.trabajador_de_canal(str(interaction.channel.id))
        if not vinculo or vinculo["discord_id"] != str(interaction.user.id):
            await interaction.response.send_message("Este panel solo puede ser usado por el dueño de la bitácora.", ephemeral=True)
            return

        seccion_key = self.values[0]
        try:
            await interaction.message.edit(view=PanelControlView())
        except Exception:
            pass

        now_iso = await db.iniciar_turno(str(interaction.user.id), interaction.user.display_name, seccion_key, str(interaction.channel.id))
        if not now_iso:
            activo = await db.obtener_turno_activo(str(interaction.user.id))
            sec_name = config.SECCIONES[activo['seccion']]['nombre_visible'] if activo else "otra área"
            await interaction.response.send_message(f"⚠️ Ya tienes un turno activo en **{sec_name}**. Debes finalizarlo antes de iniciar otro.", ephemeral=True)
            return

        dt = datetime.fromisoformat(now_iso).replace(tzinfo=UTC).astimezone(TZ_CLUB)
        embed = discord.Embed(
            title="🟢 Turno Iniciado",
            description=f"**Trabajador:** {interaction.user.display_name}\n"
                        f"**Área:** {config.SECCIONES[seccion_key]['nombre_visible']}\n"
                        f"**Hora de Entrada:** {dt.strftime('%H:%M:%S')}\n\n"
                        f"📌 Presiona el botón rojo de abajo cuando desees **finalizar tu turno**.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, view=FinalizarTurnoView(), ephemeral=True)


class HorasDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=v["nombre_visible"], value=k, description=f"Tarifa: {_peso(v['tarifa_hora'])}/h")
            for k, v in config.SECCIONES.items()
        ]
        super().__init__(
            custom_id="select_horas_panel",
            placeholder="🕒 Registrar Horas Manuales en...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        vinculo = await db.trabajador_de_canal(str(interaction.channel.id))
        if not vinculo or vinculo["discord_id"] != str(interaction.user.id):
            await interaction.response.send_message("Este panel solo puede ser usado por el dueño de la bitácora.", ephemeral=True)
            return

        seccion_key = self.values[0]
        try:
            await interaction.message.edit(view=PanelControlView())
        except Exception:
            pass

        modal = HorasModal(seccion_key)
        await interaction.response.send_modal(modal)


class ServicioDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=v["nombre_visible"], value=k, description=f"Comisión: {int(v['comision_servicio']*100)}%")
            for k, v in config.SECCIONES.items()
        ]
        super().__init__(
            custom_id="select_servicio_panel",
            placeholder="🧾 Registrar Servicio en...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        vinculo = await db.trabajador_de_canal(str(interaction.channel.id))
        if not vinculo or vinculo["discord_id"] != str(interaction.user.id):
            await interaction.response.send_message("Este panel solo puede ser usado por el dueño de la bitácora.", ephemeral=True)
            return

        seccion_key = self.values[0]
        try:
            await interaction.message.edit(view=PanelControlView())
        except Exception:
            pass

        modal = ServicioModal(seccion_key)
        await interaction.response.send_modal(modal)


class FinalizarTurnoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔴 Finalizar Turno de Trabajo", style=discord.ButtonStyle.danger, custom_id="btn_finalizar_turno_active")
    async def btn_finalizar_turno_active(self, interaction: discord.Interaction, button: discord.ui.Button):
        activo = await db.finalizar_turno(str(interaction.user.id))
        if not activo:
            await interaction.response.send_message("⚠️ No tienes ningún turno activo en este momento.", ephemeral=True)
            return

        inicio_dt = datetime.fromisoformat(activo["hora_inicio"])
        fin_dt = datetime.utcnow()
        duracion = (fin_dt - inicio_dt).total_seconds()
        horas = round(duracion / 3600.0, 2)
        if horas < 0.01:
            horas = 0.01

        seccion_key = activo["seccion"]
        info_seccion = config.SECCIONES[seccion_key]
        auto_validado = 0 if config.REQUIERE_APROBACION else 1

        registro_id = await db.registrar_horas(
            str(interaction.user.id),
            interaction.user.display_name,
            seccion_key,
            horas,
            nota="Marcaje automático de entrada y salida",
            validado=auto_validado
        )

        inicio_local = inicio_dt.replace(tzinfo=UTC).astimezone(TZ_CLUB)
        fin_local = fin_dt.replace(tzinfo=UTC).astimezone(TZ_CLUB)

        estado_txt, color = _embed_estado(bool(auto_validado))
        embed = discord.Embed(
            title="🔴 Turno Finalizado",
            description=f"**Área:** {info_seccion['nombre_visible']}",
            color=color
        )
        embed.add_field(name="Horario Entrada - Salida", value=f"{inicio_local.strftime('%H:%M')} a {fin_local.strftime('%H:%M')}", inline=True)
        embed.add_field(name="Tiempo Trabajado", value=f"**{horas} h**", inline=True)

        if info_seccion.get("usa_sueldo_base"):
            pago_txt = "Sueldo base ($10k/$20k al cumplir 10 hrs)"
        else:
            pago_txt = _peso(horas * info_seccion["tarifa_hora"])

        embed.add_field(name="Pago Estimado", value=pago_txt, inline=True)
        embed.add_field(name="Estado", value=estado_txt, inline=False)
        if config.REQUIERE_APROBACION:
            embed.set_footer(text="El jefe del área aprueba reaccionando con ✅ a este mensaje.")

        button.disabled = True
        await interaction.response.edit_message(content="✅ **Turno finalizado correctamente.**", view=self)

        canal = interaction.channel
        mensaje = await canal.send(embed=embed)
        await db.set_mensaje(registro_id, str(mensaje.id), str(interaction.channel.id))
        if config.REQUIERE_APROBACION:
            await mensaje.add_reaction("✅")


class PanelControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(IniciarTurnoDropdown())
        self.add_item(ServicioDropdown())

    @discord.ui.button(label="📊 Ver Mi Resumen Semanal", style=discord.ButtonStyle.secondary, custom_id="btn_resumen_panel")
    async def btn_resumen(self, interaction: discord.Interaction, button: discord.ui.Button):
        vinculo = await db.trabajador_de_canal(str(interaction.channel.id))
        if not vinculo or vinculo["discord_id"] != str(interaction.user.id):
            await interaction.response.send_message("Este panel solo puede ser usado por el dueño de la bitácora.", ephemeral=True)
            return

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
                "Todavía no tienes registros esta semana.", ephemeral=True)
            return

        embed = discord.Embed(title="📋 Tu resumen de la semana", color=discord.Color.blurple())
        embed.add_field(name="Áreas con registros", value=", ".join(detalle), inline=False)
        embed.add_field(name="✅ Aprobado (te lo pagan)", value=_peso(total_aprobado))
        if config.REQUIERE_APROBACION:
            embed.add_field(name="⏳ Pendiente de aprobación", value=_peso(total_pendiente))
        embed.set_footer(text=f"Semana: {inicio_local.strftime('%d-%m')} al {fin_local.strftime('%d-%m %H:%M')}")

        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- Eventos ----------

@bot.event
async def on_ready():
    # Inicializar Super Admin automático
    admin_user = os.getenv("DASHBOARD_USER") or os.getenv("ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("DASHBOARD_PASS") or os.getenv("ADMIN_PASSWORD", "samcro2026")
    if admin_user and admin_pass:
        permisos = ["consolidado"] + list(config.SECCIONES.keys())
        await db.crear_usuario(admin_user, admin_pass, "superadmin", json.dumps(permisos), debe_cambiar_password=0)

    # Iniciar tarea de cierre semanal (solo si no está corriendo ya)
    if GUILD_ID:
        if not revisar_cierre_semanal.is_running():
            revisar_cierre_semanal.start()

    print(f"SAMCRO bot conectado exitosamente como {bot.user}")



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


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    try:
        vinculo = await db.trabajador_de_canal(str(channel.id))
        if vinculo:
            await db.desvincular_canal(str(channel.id))
            print(f"🗑️ Bitácora eliminada ({channel.name}). Se desvinculó al trabajador {vinculo['nombre']}.")
    except Exception as e:
        print(f"Error en on_guild_channel_delete: {e}")


# ---------- Setup: canal personal de trabajador ----------

@bot.tree.command(name="crear-canal-trabajador",
                   description="(Jefes/Liderazgo) Crea el canal personal de bitácora de un trabajador")
@app_commands.describe(trabajador="La persona para la que se crea el canal")
async def crear_canal_trabajador(interaction: discord.Interaction, trabajador: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not es_algun_jefe(interaction.user):
        await interaction.followup.send("Solo un jefe de área o Liderazgo puede crear canales.", ephemeral=True)
        return
    if not es_trabajador(trabajador):
        await interaction.followup.send(
            f"{trabajador.display_name} todavía no tiene ningún rol de trabajador. Asígnaselo primero.",
            ephemeral=True)
        return

    existente = await db.canal_de_trabajador(str(trabajador.id))
    if existente:
        canal_existente = interaction.guild.get_channel(int(existente["canal_id"]))
        if canal_existente:
            await interaction.followup.send(
                f"{trabajador.display_name} ya tiene su canal: {canal_existente.mention}", ephemeral=True)
            return

    categoria = discord.utils.get(interaction.guild.categories, name=config.CATEGORIA_BITACORAS)
    if not categoria:
        await interaction.followup.send(
            f'No encontré la categoría "{config.CATEGORIA_BITACORAS}". Créala primero en Discord con ese nombre '
            "exacto (Ajustes del servidor -> Canales -> Crear categoría).", ephemeral=True)
        return

    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
        trabajador: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True),
        interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                            manage_messages=True),
    }
    roles_permiso = []
    for s in config.SECCIONES.values():
        rj = s["rol_jefe"]
        if isinstance(rj, (list, tuple, set)):
            roles_permiso.extend(rj)
        else:
            roles_permiso.append(rj)

    if isinstance(config.ROL_LIDERAZGO, (list, tuple, set)):
        roles_permiso.extend(config.ROL_LIDERAZGO)
    else:
        roles_permiso.append(config.ROL_LIDERAZGO)

    for rname in set(roles_permiso):
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
        description="Este es tu canal personal de bitácora. Acá puedes registrar tus turnos y comisiones "
                     "fácilmente usando los menús interactivos de abajo.",
        color=discord.Color.blurple())
    bienvenida.add_field(
        name="🎮 Panel de Control",
        value="¡Usa los menús desplegables para registrar horas y servicios! Presiona el botón de resumen para ver tus ganancias semanales.",
        inline=False
    )
    bienvenida.add_field(
        name="Comandos alternativos (Si prefieres escribir)",
        value="*   `/registrar-horas`\n*   `/registrar-servicio`\n*   `/mi-resumen`",
        inline=False
    )
    bienvenida.set_footer(
        text=f"Todos los domingos a las {config.HORA_CIERRE_SEMANA} se calcula tu pago automáticamente.")
    await canal.send(content=trabajador.mention, embed=bienvenida, view=PanelControlView())

    await interaction.followup.send(f"✅ Canal creado: {canal.mention}", ephemeral=True)


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


@bot.tree.command(name="iniciar-turno", description="Marca tu hora de entrada para comenzar a trabajar")
@app_commands.describe(seccion="Área en la que vas a trabajar")
@app_commands.choices(seccion=SECCION_CHOICES)
async def iniciar_turno_cmd(interaction: discord.Interaction, seccion: app_commands.Choice[str]):
    if not await _validar_canal_personal(interaction):
        return
    if not es_trabajador(interaction.user):
        await interaction.response.send_message("No tienes un rol de trabajador.", ephemeral=True)
        return

    seccion_key = seccion.value
    now_iso = await db.iniciar_turno(str(interaction.user.id), interaction.user.display_name, seccion_key, str(interaction.channel.id))
    if not now_iso:
        activo = await db.obtener_turno_activo(str(interaction.user.id))
        sec_name = config.SECCIONES[activo['seccion']]['nombre_visible'] if activo else "otra área"
        await interaction.response.send_message(f"⚠️ Ya tienes un turno activo en **{sec_name}**. Debes finalizarlo antes de iniciar otro.", ephemeral=True)
        return

    dt = datetime.fromisoformat(now_iso).replace(tzinfo=UTC).astimezone(TZ_CLUB)
    embed = discord.Embed(
        title="🟢 Turno Iniciado",
        description=f"**Trabajador:** {interaction.user.display_name}\n"
                    f"**Área:** {config.SECCIONES[seccion_key]['nombre_visible']}\n"
                    f"**Hora de Entrada:** {dt.strftime('%H:%M:%S')}",
        color=discord.Color.green()
    )
    embed.set_footer(text="Haz clic en 🔴 Finalizar Turno en el panel al terminar tu jornada.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="finalizar-turno", description="Marca tu hora de salida y calcula las horas trabajadas")
@app_commands.describe(nota="Comentario opcional sobre tu jornada")
async def finalizar_turno_cmd(interaction: discord.Interaction, nota: str = None):
    if not await _validar_canal_personal(interaction):
        return

    activo = await db.finalizar_turno(str(interaction.user.id))
    if not activo:
        await interaction.response.send_message("⚠️ No tienes ningún turno activo en este momento. Inicia uno con `/iniciar-turno` o en el panel.", ephemeral=True)
        return

    inicio_dt = datetime.fromisoformat(activo["hora_inicio"])
    fin_dt = datetime.utcnow()
    duracion = (fin_dt - inicio_dt).total_seconds()
    horas = round(duracion / 3600.0, 2)
    if horas < 0.01:
        horas = 0.01

    seccion_key = activo["seccion"]
    info_seccion = config.SECCIONES[seccion_key]
    auto_validado = 0 if config.REQUIERE_APROBACION else 1

    registro_id = await db.registrar_horas(
        str(interaction.user.id),
        interaction.user.display_name,
        seccion_key,
        horas,
        nota or "Marcaje automático de entrada y salida",
        validado=auto_validado
    )

    inicio_local = inicio_dt.replace(tzinfo=UTC).astimezone(TZ_CLUB)
    fin_local = fin_dt.replace(tzinfo=UTC).astimezone(TZ_CLUB)

    estado_txt, color = _embed_estado(bool(auto_validado))
    embed = discord.Embed(
        title="🔴 Turno Finalizado",
        description=f"**Área:** {info_seccion['nombre_visible']}",
        color=color
    )
    embed.add_field(name="Horario Entrada - Salida", value=f"{inicio_local.strftime('%H:%M')} a {fin_local.strftime('%H:%M')}", inline=True)
    embed.add_field(name="Tiempo Trabajado", value=f"**{horas} h**", inline=True)
    embed.add_field(name="Pago Estimado", value=_peso(horas * info_seccion["tarifa_hora"]), inline=True)
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
    foto_url_db = await procesar_attachment_a_base64(foto) if foto else None
    auto_validado = 0 if config.REQUIERE_APROBACION else 1

    registro_id = await db.registrar_servicio(str(interaction.user.id), interaction.user.display_name, seccion_key,
                                                servicio, monto, comision, nota, foto_url_db, validado=auto_validado)

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
@bot.tree.command(name="panel", description="Muestra el panel de control interactivo para registrar turnos y servicios")
async def mostrar_panel(interaction: discord.Interaction):
    vinculo = await db.trabajador_de_canal(str(interaction.channel.id))
    if not vinculo:
        await interaction.response.send_message(
            "Este comando solo se puede usar dentro de tu canal personal de bitácora.", ephemeral=True)
        return
    if vinculo["discord_id"] != str(interaction.user.id):
        await interaction.response.send_message("Este es el canal personal de otro trabajador — usa el tuyo.",
                                                  ephemeral=True)
        return

    embed = discord.Embed(
        title="🏍️ Panel de Control SAMCRO",
        description="Utiliza los menús desplegables para registrar tus turnos y servicios sin tener que escribir comandos.\n\n"
                    "*   **Registrar Horas:** Elige tu área y escribe cuántas horas trabajaste.\n"
                    "*   **Registrar Servicio:** Elige tu área, escribe el servicio, el cobro y opcionalmente añade una foto.",
        color=discord.Color.dark_grey()
    )
    await interaction.response.send_message(embed=embed, view=PanelControlView())


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

    # 1.5) Limpieza automática de mensajes en el chat de Discord (La base de datos permanece 100% intacta)
    for canal_info in canales:
        try:
            channel = guild.get_channel(int(canal_info["canal_id"]))
            if channel:
                await channel.purge(limit=100)
                bienvenida = discord.Embed(
                    title=f"👋 Bitácora de {canal_info['nombre']}",
                    description="Este es tu canal personal de bitácora para la nueva semana. Usa el panel interactivo de abajo para registrar tus turnos y servicios.",
                    color=discord.Color.blurple()
                )
                bienvenida.set_footer(text=f"Todos los domingos a las {config.HORA_CIERRE_SEMANA} se calcula tu pago automáticamente.")
                await channel.send(content=f"<@{canal_info['discord_id']}>", embed=bienvenida, view=PanelControlView())
        except Exception as e:
            print(f"Error al limpiar chat de bitácora {canal_info['canal_id']}: {e}")

    # 2) Resumen consolidado para jefes/liderazgo
    canal_resumen = discord.utils.get(guild.text_channels, name=config.CANAL_RESUMEN_NOMINA)
    if canal_resumen:
        total_club = 0.0
        secciones_con_datos = 0
        for sk, info_seccion in config.SECCIONES.items():
            resumen = await calcular_nomina(sk, inicio_utc, fin_utc, solo_validados=True)
            if not resumen:
                continue
            total_seccion = sum(d["total"] for d in resumen.values())
            total_club += total_seccion
            secciones_con_datos += 1
            embed = discord.Embed(
                title=f"💰 Cierre semanal — {info_seccion['nombre_visible']}",
                description=f"Semana {inicio_local.strftime('%d-%m')} al {fin_local.strftime('%d-%m %H:%M')}",
                color=discord.Color.gold())
            for datos in sorted(resumen.values(), key=lambda d: -d["total"]):
                embed.add_field(name=datos["nombre"], value=_peso(datos["total"]), inline=True)
            embed.set_footer(text=f"Total {info_seccion['nombre_visible']}: {_peso(total_seccion)}")
            await canal_resumen.send(embed=embed)
        
        if secciones_con_datos > 0:
            await canal_resumen.send(f"**🏍️ Total a pagar en todo el club esta semana: {_peso(total_club)}**")
        else:
            await canal_resumen.send(f"ℹ️ **Cierre semanal ({inicio_local.strftime('%d-%m')} al {fin_local.strftime('%d-%m %H:%M')}):** No hay registros aprobados para esta semana.")
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
