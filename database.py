"""
Capa de datos del bot SAMCRO. Usa SQLite (un solo archivo, samcro.db) así
que no necesitas montar un servidor de base de datos aparte.
"""
import os
from datetime import datetime
import aiosqlite

DB_PATH = os.getenv("DB_PATH", "samcro.db")
_db_dir = os.path.dirname(os.path.abspath(DB_PATH))
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS registros (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id TEXT NOT NULL,
    nombre TEXT NOT NULL,
    seccion TEXT NOT NULL,
    tipo TEXT NOT NULL,              -- 'horas' o 'servicio'
    horas REAL DEFAULT 0,
    servicio_nombre TEXT,
    monto REAL DEFAULT 0,            -- monto cobrado del servicio (base de la comisión)
    comision REAL DEFAULT 0,         -- lo que efectivamente gana el trabajador por este registro
    nota TEXT,
    foto_url TEXT,
    mensaje_id TEXT,                 -- id del mensaje del embed (para la aprobación por reacción)
    canal_id TEXT,
    validado INTEGER DEFAULT 0,      -- 0 = pendiente, 1 = aprobado por el jefe de área / liderazgo
    validado_por TEXT,
    creado_en TEXT NOT NULL          -- ISO timestamp
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

-- Un canal personal de bitácora por trabajador (uno por persona).
CREATE TABLE IF NOT EXISTS canales_trabajador (
    canal_id TEXT PRIMARY KEY,
    discord_id TEXT NOT NULL,
    nombre TEXT NOT NULL,
    creado_en TEXT NOT NULL
);

-- Estado simple del bot (para no duplicar el corte semanal automático).
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
"""


async def iniciar_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


# ---------- Registros ----------

async def registrar_horas(discord_id: str, nombre: str, seccion: str, horas: float,
                           nota: str = None, validado: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO registros (discord_id, nombre, seccion, tipo, horas, nota, creado_en, validado)
               VALUES (?, ?, ?, 'horas', ?, ?, ?, ?)""",
            (discord_id, nombre, seccion, horas, nota, datetime.utcnow().isoformat(), validado),
        )
        await db.commit()
        return cursor.lastrowid


async def registrar_servicio(discord_id: str, nombre: str, seccion: str, servicio_nombre: str,
                              monto: float, comision: float, nota: str = None, foto_url: str = None,
                              validado: int = 0):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO registros
               (discord_id, nombre, seccion, tipo, servicio_nombre, monto, comision, nota, foto_url,
                creado_en, validado)
               VALUES (?, ?, ?, 'servicio', ?, ?, ?, ?, ?, ?, ?)""",
            (discord_id, nombre, seccion, servicio_nombre, monto, comision, nota, foto_url,
             datetime.utcnow().isoformat(), validado),
        )
        await db.commit()
        return cursor.lastrowid


async def set_mensaje(registro_id: int, mensaje_id: str, canal_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE registros SET mensaje_id = ?, canal_id = ? WHERE id = ?",
                          (mensaje_id, canal_id, registro_id))
        await db.commit()


async def registro_por_mensaje(mensaje_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM registros WHERE mensaje_id = ?", (mensaje_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def aprobar_por_mensaje(mensaje_id: str, aprobado_por: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM registros WHERE mensaje_id = ?", (mensaje_id,)) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        if row["validado"]:
            return dict(row)
        await db.execute("UPDATE registros SET validado = 1, validado_por = ? WHERE mensaje_id = ?",
                          (aprobado_por, mensaje_id))
        await db.commit()
        row_dict = dict(row)
        row_dict["validado"] = 1
        row_dict["validado_por"] = aprobado_por
        return row_dict


async def obtener_registros_semana(seccion: str, inicio_iso: str, fin_iso: str,
                                    discord_id: str = None, solo_validados: bool = True):
    query = """SELECT discord_id, nombre, tipo, horas, servicio_nombre, monto, comision, creado_en,
                      foto_url, validado
               FROM registros
               WHERE seccion = ? AND creado_en >= ? AND creado_en < ?"""
    params = [seccion, inicio_iso, fin_iso]
    if solo_validados:
        query += " AND validado = 1"
    if discord_id:
        query += " AND discord_id = ?"
        params.append(discord_id)
    query += " ORDER BY creado_en ASC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def obtener_registros_discord_id_semana(discord_id: str, inicio_iso: str, fin_iso: str,
                                               solo_validados: bool = True):
    """Todos los registros de una persona en la semana, en TODAS las secciones (para su canal personal)."""
    query = """SELECT seccion, tipo, horas, servicio_nombre, monto, comision, creado_en, validado
               FROM registros
               WHERE discord_id = ? AND creado_en >= ? AND creado_en < ?"""
    params = [discord_id, inicio_iso, fin_iso]
    if solo_validados:
        query += " AND validado = 1"
    query += " ORDER BY creado_en ASC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def contar_pendientes(seccion: str, inicio_iso: str, fin_iso: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COUNT(*) FROM registros
               WHERE seccion = ? AND creado_en >= ? AND creado_en < ? AND validado = 0""",
            (seccion, inicio_iso, fin_iso),
        ) as cursor:
            (n,) = await cursor.fetchone()
            return n


async def guardar_pago(seccion: str, semana_inicio: str, semana_fin: str, pagado_por: str, detalle_json: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO pagos_realizados (seccion, semana_inicio, semana_fin, pagado_por, pagado_en, detalle_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (seccion, semana_inicio, semana_fin, pagado_por, datetime.utcnow().isoformat(), detalle_json),
        )
        await db.commit()


# ---------- Canales personales de trabajador ----------

async def vincular_canal(canal_id: str, discord_id: str, nombre: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM canales_trabajador WHERE discord_id = ?", (discord_id,))
        await db.execute(
            """INSERT INTO canales_trabajador (canal_id, discord_id, nombre, creado_en)
               VALUES (?, ?, ?, ?)""",
            (canal_id, discord_id, nombre, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def trabajador_de_canal(canal_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM canales_trabajador WHERE canal_id = ?", (canal_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def canal_de_trabajador(discord_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM canales_trabajador WHERE discord_id = ?", (discord_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def todos_los_canales_trabajador():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM canales_trabajador") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# ---------- Estado (para el corte semanal automático) ----------

async def obtener_estado(clave: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT valor FROM estado WHERE clave = ?", (clave,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def guardar_estado(clave: str, valor: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO estado (clave, valor) VALUES (?, ?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
            (clave, valor),
        )
        await db.commit()


# ---------- Autenticación, Hashing y Usuarios ----------
import hashlib
import os

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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO usuarios (username, password_hash, rol, permisos, creado_en)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(username) DO UPDATE SET
               password_hash=excluded.password_hash, rol=excluded.rol, permisos=excluded.permisos""",
            (username, password_hash, rol, permisos_json, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def actualizar_usuario(username: str, rol: str, permisos_json: str, password_plain: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if password_plain and password_plain.strip():
            password_hash = hash_password(password_plain)
            await db.execute(
                "UPDATE usuarios SET rol = ?, permisos = ?, password_hash = ? WHERE username = ?",
                (rol, permisos_json, password_hash, username),
            )
        else:
            await db.execute(
                "UPDATE usuarios SET rol = ?, permisos = ? WHERE username = ?",
                (rol, permisos_json, username),
            )
        await db.commit()


async def obtener_usuario(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM usuarios WHERE username = ?", (username,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def eliminar_usuario(username: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM usuarios WHERE username = ?", (username,))
        await db.commit()


async def listar_usuarios():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT username, rol, permisos, creado_en FROM usuarios ORDER BY username ASC") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# ---------- Sesiones ----------

async def crear_sesion(session_id: str, username: str, expira_en_iso: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sesiones (session_id, username, expira_en) VALUES (?, ?, ?)",
            (session_id, username, expira_en_iso),
        )
        await db.commit()


async def verificar_sesion(session_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sesiones WHERE session_id = ? AND expira_en > ?",
            (session_id, datetime.utcnow().isoformat()),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def eliminar_sesion(session_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sesiones WHERE session_id = ?", (session_id,))
        await db.commit()


async def limpiar_sesiones_expiradas():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM sesiones WHERE expira_en <= ?", (datetime.utcnow().isoformat(),))
        await db.commit()
