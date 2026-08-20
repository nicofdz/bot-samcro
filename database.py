"""
Capa de datos del bot SAMCRO.
Soporta SQLite local (samcro.db) y PostgreSQL en la nube (DATABASE_URL en Render/Railway).
"""
import os
from datetime import datetime
import aiosqlite

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

try:
    import asyncpg
except ImportError:
    asyncpg = None

DB_PATH = os.getenv("DB_PATH", "samcro.db")
_db_dir = os.path.dirname(os.path.abspath(DB_PATH))
if _db_dir and not DATABASE_URL:
    os.makedirs(_db_dir, exist_ok=True)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS registros (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT NOT NULL,
    nombre TEXT NOT NULL,
    seccion TEXT NOT NULL,
    tipo TEXT NOT NULL,
    horas REAL DEFAULT 0,
    servicio_nombre TEXT,
    monto REAL DEFAULT 0,
    comision REAL DEFAULT 0,
    nota TEXT,
    foto_url TEXT,
    mensaje_id TEXT,
    canal_id TEXT,
    validado INTEGER DEFAULT 0,
    validado_por TEXT,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pagos_realizados (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seccion TEXT NOT NULL,
    semana_inicio TEXT NOT NULL,
    semana_fin TEXT NOT NULL,
    pagado_por TEXT NOT NULL,
    pagado_en TEXT NOT NULL,
    detalle_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canales_trabajador (
    canal_id TEXT PRIMARY KEY,
    discord_id TEXT NOT NULL,
    nombre TEXT NOT NULL,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS estado (
    clave TEXT PRIMARY KEY,
    valor TEXT
);

CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    rol TEXT NOT NULL,
    permisos TEXT NOT NULL,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sesiones (
    session_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expira_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turnos_activos (
    discord_id TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    seccion TEXT NOT NULL,
    canal_id TEXT NOT NULL,
    hora_inicio TEXT NOT NULL
);
"""

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS registros (
    id SERIAL PRIMARY KEY,
    discord_id TEXT NOT NULL,
    nombre TEXT NOT NULL,
    seccion TEXT NOT NULL,
    tipo TEXT NOT NULL,
    horas DOUBLE PRECISION DEFAULT 0,
    servicio_nombre TEXT,
    monto DOUBLE PRECISION DEFAULT 0,
    comision DOUBLE PRECISION DEFAULT 0,
    nota TEXT,
    foto_url TEXT,
    mensaje_id TEXT,
    canal_id TEXT,
    validado INTEGER DEFAULT 0,
    validado_por TEXT,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pagos_realizados (
    id SERIAL PRIMARY KEY,
    seccion TEXT NOT NULL,
    semana_inicio TEXT NOT NULL,
    semana_fin TEXT NOT NULL,
    pagado_por TEXT NOT NULL,
    pagado_en TEXT NOT NULL,
    detalle_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS canales_trabajador (
    canal_id TEXT PRIMARY KEY,
    discord_id TEXT NOT NULL,
    nombre TEXT NOT NULL,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS estado (
    clave TEXT PRIMARY KEY,
    valor TEXT
);

CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    rol TEXT NOT NULL,
    permisos TEXT NOT NULL,
    creado_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sesiones (
    session_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expira_en TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turnos_activos (
    discord_id TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    seccion TEXT NOT NULL,
    canal_id TEXT NOT NULL,
    hora_inicio TEXT NOT NULL
);
"""


def _format_sql(query: str) -> str:
    if not DATABASE_URL or "?" not in query:
        return query
    parts = query.split("?")
    res = []
    for i, p in enumerate(parts[:-1]):
        res.append(f"{p}${i+1}")
    res.append(parts[-1])
    return "".join(res)


async def _execute(query: str, params: tuple = ()):
    if DATABASE_URL:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            return await conn.execute(_format_sql(query), *params)
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.lastrowid


async def _fetch_all(query: str, params: tuple = ()):
    if DATABASE_URL:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            rows = await conn.fetch(_format_sql(query), *params)
            return [dict(r) for r in rows]
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]


async def _fetch_one(query: str, params: tuple = ()):
    if DATABASE_URL:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            row = await conn.fetchrow(_format_sql(query), *params)
            return dict(row) if row else None
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None


async def iniciar_db():
    if DATABASE_URL:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            for stmt in PG_SCHEMA.strip().split(";"):
                if stmt.strip():
                    await conn.execute(stmt)
        finally:
            await conn.close()
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(SQLITE_SCHEMA)
            await db.commit()


# ---------- Registros ----------

async def registrar_horas(discord_id: str, nombre: str, seccion: str, horas: float,
                           nota: str = None, validado: int = 0):
    query = """INSERT INTO registros (discord_id, nombre, seccion, tipo, horas, nota, creado_en, validado)
               VALUES (?, ?, ?, 'horas', ?, ?, ?, ?)"""
    params = (discord_id, nombre, seccion, horas, nota, datetime.utcnow().isoformat(), validado)
    if DATABASE_URL:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            row = await conn.fetchrow(_format_sql(query) + " RETURNING id", *params)
            return row["id"]
        finally:
            await conn.close()
    else:
        return await _execute(query, params)


async def registrar_servicio(discord_id: str, nombre: str, seccion: str, servicio_nombre: str,
                              monto: float, comision: float, nota: str = None, foto_url: str = None,
                              validado: int = 0):
    query = """INSERT INTO registros
               (discord_id, nombre, seccion, tipo, servicio_nombre, monto, comision, nota, foto_url,
                creado_en, validado)
               VALUES (?, ?, ?, 'servicio', ?, ?, ?, ?, ?, ?, ?)"""
    params = (discord_id, nombre, seccion, servicio_nombre, monto, comision, nota, foto_url,
              datetime.utcnow().isoformat(), validado)
    if DATABASE_URL:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            row = await conn.fetchrow(_format_sql(query) + " RETURNING id", *params)
            return row["id"]
        finally:
            await conn.close()
    else:
        return await _execute(query, params)


async def set_mensaje(registro_id: int, mensaje_id: str, canal_id: str):
    await _execute("UPDATE registros SET mensaje_id = ?, canal_id = ? WHERE id = ?", (mensaje_id, canal_id, registro_id))


async def registro_por_mensaje(mensaje_id: str):
    return await _fetch_one("SELECT * FROM registros WHERE mensaje_id = ?", (mensaje_id,))


async def aprobar_por_mensaje(mensaje_id: str, aprobado_por: str):
    row = await _fetch_one("SELECT * FROM registros WHERE mensaje_id = ?", (mensaje_id,))
    if not row:
        return None
    if row["validado"]:
        return row
    await _execute("UPDATE registros SET validado = 1, validado_por = ? WHERE mensaje_id = ?", (aprobado_por, mensaje_id))
    row["validado"] = 1
    row["validado_por"] = aprobado_por
    return row


async def obtener_registro_por_id(registro_id: int):
    return await _fetch_one("SELECT * FROM registros WHERE id = ?", (registro_id,))


async def anular_registro(registro_id: int, anulado_por: str = "admin"):
    await _execute("UPDATE registros SET validado = -1, validado_por = ? WHERE id = ?", (anulado_por, registro_id))
    return await _fetch_one("SELECT * FROM registros WHERE id = ?", (registro_id,))


async def restaurar_registro(registro_id: int, restaurado_por: str = "admin"):
    await _execute("UPDATE registros SET validado = 1, validado_por = ? WHERE id = ?", (restaurado_por, registro_id))
    return await _fetch_one("SELECT * FROM registros WHERE id = ?", (registro_id,))


async def obtener_registros_semana(seccion: str, inicio_iso: str, fin_iso: str,
                                    discord_id: str = None, solo_validados: bool = True):
    query = """SELECT id, discord_id, nombre, tipo, horas, servicio_nombre, monto, comision, nota,
                      foto_url, validado, validado_por, creado_en
               FROM registros
               WHERE seccion = ? AND creado_en >= ? AND creado_en < ?"""
    params = [seccion, inicio_iso, fin_iso]
    if solo_validados:
        query += " AND validado = 1"
    if discord_id:
        query += " AND discord_id = ?"
        params.append(discord_id)
    query += " ORDER BY creado_en ASC"
    return await _fetch_all(query, tuple(params))


async def obtener_registros_discord_id_semana(discord_id: str, inicio_iso: str, fin_iso: str,
                                               solo_validados: bool = True):
    query = """SELECT seccion, tipo, horas, servicio_nombre, monto, comision, creado_en, validado
               FROM registros
               WHERE discord_id = ? AND creado_en >= ? AND creado_en < ?"""
    params = [discord_id, inicio_iso, fin_iso]
    if solo_validados:
        query += " AND validado = 1"
    query += " ORDER BY creado_en ASC"
    return await _fetch_all(query, tuple(params))


async def contar_pendientes(seccion: str, inicio_iso: str, fin_iso: str):
    row = await _fetch_one(
        """SELECT COUNT(*) as count FROM registros
           WHERE seccion = ? AND creado_en >= ? AND creado_en < ? AND validado = 0""",
        (seccion, inicio_iso, fin_iso),
    )
    return row["count"] if row else 0


async def guardar_pago(seccion: str, semana_inicio: str, semana_fin: str, pagado_por: str, detalle_json: str):
    await _execute(
        """INSERT INTO pagos_realizados (seccion, semana_inicio, semana_fin, pagado_por, pagado_en, detalle_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (seccion, semana_inicio, semana_fin, pagado_por, datetime.utcnow().isoformat(), detalle_json),
    )


# ---------- Canales personales de trabajador ----------

async def vincular_canal(canal_id: str, discord_id: str, nombre: str):
    await _execute("DELETE FROM canales_trabajador WHERE discord_id = ?", (discord_id,))
    await _execute(
        """INSERT INTO canales_trabajador (canal_id, discord_id, nombre, creado_en)
           VALUES (?, ?, ?, ?)""",
        (canal_id, discord_id, nombre, datetime.utcnow().isoformat()),
    )


async def trabajador_de_canal(canal_id: str):
    return await _fetch_one("SELECT * FROM canales_trabajador WHERE canal_id = ?", (canal_id,))


async def canal_de_trabajador(discord_id: str):
    return await _fetch_one("SELECT * FROM canales_trabajador WHERE discord_id = ?", (discord_id,))


async def todos_los_canales_trabajador():
    return await _fetch_all("SELECT * FROM canales_trabajador")


# ---------- Estado (para el corte semanal automático) ----------

async def obtener_estado(clave: str):
    row = await _fetch_one("SELECT valor FROM estado WHERE clave = ?", (clave,))
    return row["valor"] if row else None


async def guardar_estado(clave: str, valor: str):
    await _execute(
        "INSERT INTO estado (clave, valor) VALUES (?, ?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
        (clave, valor),
    )


# ---------- Autenticación, Hashing y Usuarios ----------
import hashlib

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
    return salt.hex() + "$" + key.hex()


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        salt_hex, key_hex = stored_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        key = bytes.fromhex(key_hex)
        new_key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100000)
        return key == new_key
    except Exception:
        return False


async def crear_usuario(username: str, password_plain: str, rol: str, permisos_json: str):
    password_hash = hash_password(password_plain)
    await _execute(
        """INSERT INTO usuarios (username, password_hash, rol, permisos, creado_en)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(username) DO UPDATE SET
           password_hash=excluded.password_hash, rol=excluded.rol, permisos=excluded.permisos""",
        (username, password_hash, rol, permisos_json, datetime.utcnow().isoformat()),
    )


async def actualizar_usuario(username: str, rol: str, permisos_json: str, password_plain: str = None):
    if password_plain and password_plain.strip():
        password_hash = hash_password(password_plain)
        await _execute(
            "UPDATE usuarios SET rol = ?, permisos = ?, password_hash = ? WHERE username = ?",
            (rol, permisos_json, password_hash, username),
        )
    else:
        await _execute(
            "UPDATE usuarios SET rol = ?, permisos = ? WHERE username = ?",
            (rol, permisos_json, username),
        )


async def obtener_usuario(username: str):
    return await _fetch_one("SELECT * FROM usuarios WHERE username = ?", (username,))


async def eliminar_usuario(username: str):
    await _execute("DELETE FROM usuarios WHERE username = ?", (username,))


async def listar_usuarios():
    return await _fetch_all("SELECT username, rol, permisos, creado_en FROM usuarios ORDER BY username ASC")


# ---------- Sesiones ----------

async def crear_sesion(session_id: str, username: str, expira_en_iso: str):
    await _execute(
        "INSERT INTO sesiones (session_id, username, expira_en) VALUES (?, ?, ?)",
        (session_id, username, expira_en_iso),
    )


async def verificar_sesion(session_id: str):
    return await _fetch_one(
        "SELECT * FROM sesiones WHERE session_id = ? AND expira_en > ?",
        (session_id, datetime.utcnow().isoformat()),
    )


async def eliminar_sesion(session_id: str):
    await _execute("DELETE FROM sesiones WHERE session_id = ?", (session_id,))


async def limpiar_sesiones_expiradas():
    await _execute("DELETE FROM sesiones WHERE expira_en <= ?", (datetime.utcnow().isoformat(),))


# ---------- Turnos Activos (Entrada / Salida) ----------

async def iniciar_turno(discord_id: str, nombre: str, seccion: str, canal_id: str):
    activo = await _fetch_one("SELECT * FROM turnos_activos WHERE discord_id = ?", (discord_id,))
    if activo:
        return None
    now_iso = datetime.utcnow().isoformat()
    await _execute(
        """INSERT INTO turnos_activos (discord_id, nombre, seccion, canal_id, hora_inicio)
           VALUES (?, ?, ?, ?, ?)""",
        (discord_id, nombre, seccion, canal_id, now_iso),
    )
    return now_iso


async def obtener_turno_activo(discord_id: str):
    return await _fetch_one("SELECT * FROM turnos_activos WHERE discord_id = ?", (discord_id,))


async def finalizar_turno(discord_id: str):
    activo = await _fetch_one("SELECT * FROM turnos_activos WHERE discord_id = ?", (discord_id,))
    if not activo:
        return None
    await _execute("DELETE FROM turnos_activos WHERE discord_id = ?", (discord_id,))
    return activo
