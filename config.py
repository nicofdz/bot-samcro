"""
Configuración central del bot SAMCRO — v3 (canal personal por trabajador).

CÓMO FUNCIONA AHORA: cada TRABAJADOR tiene su propio canal privado de
bitácora (ej: #bitacora-alan-smith), no un canal compartido por sección.
Ahí es donde esa persona sube lo que hizo (con foto) durante la semana. El
canal lo crea el bot automáticamente con /crear-canal-trabajador (lo usa un
jefe o Liderazgo una vez por persona) y queda visible solo para: esa
persona, los jefes de las áreas donde trabaja, y Liderazgo.

Como una misma persona puede apoyar en más de un área, cada vez que registra
algo elige a qué SECCIÓN pertenece ese registro específico (ya no lo define
el canal, lo define un menú desplegable en el propio comando).

Todos los domingos a la hora definida en HORA_CIERRE_SEMANA, el bot:
  1. Calcula el total aprobado de la semana para cada trabajador.
  2. Postea el resumen de pago EN EL CANAL PERSONAL de cada trabajador.
  3. Postea un resumen consolidado (todos los trabajadores, por sección) en
     el canal de resumen para que Uds. paguen de una vez.

SECCIONES: cada sección tiene:
  - "nombre_visible": cómo se muestra en los mensajes del bot.
  - "rol_jefe": nombre EXACTO del rol de Discord del encargado de esa
    sección (junto con Liderazgo, puede aprobar con ✅ y ver /nomina).
  - "tarifa_hora": cuánto se paga por hora trabajada (en tu moneda RP).
  - "comision_servicio": % de comisión sobre el monto de cada servicio
    (0.30 = 30%).

Puedes editar tarifas y nombres de rol sin tocar el resto del código -- pero
deben coincidir EXACTO (mayúsculas, tildes, espacios) con los roles reales
de tu servidor.
"""

SECCIONES = {
    "mecanica": {
        "nombre_visible": "🔧 Mecánica",
        "rol_jefe": ["Jefe de Mecánico", "Jefe Mecánica"],
        "tarifa_hora": 0,
        "comision_servicio": 0.30,
        "usa_sueldo_base": True,
    },
    "bar": {
        "nombre_visible": "🍺 Bar / Comida",
        "rol_jefe": ["Jefe de Barra", "Jefe Bar"],
        "tarifa_hora": 120,
        "comision_servicio": 0.20,
        "usa_sueldo_base": False,
    },
    "tatuajes": {
        "nombre_visible": "🖋️ Tatuajes",
        "rol_jefe": ["Jefe Tatuador", "Jefe Tatuajes"],
        "tarifa_hora": 100,
        "comision_servicio": 0.40,
        "usa_sueldo_base": False,
    },
    "show": {
        "nombre_visible": "💃 Bailarinas / Show",
        "rol_jefe": ["Jefa de Bailarinas", "Jefe Show"],
        "tarifa_hora": 100,
        "comision_servicio": 0.35,
        "usa_sueldo_base": False,
    },
}

SUELDO_BASE_TRABAJADOR = 10000
SUELDO_BASE_JEFE = 20000
HORAS_MINIMAS_SUELDO_BASE = 10.0

PORCENTAJE_COMISION_POR_ROL = {
    "Jefe de Mecánico": 0.40,
    "Jefe Mecánica": 0.40,
    "Mecánico Experto": 0.40,
    "Mecánico Avanzado": 0.35,
    "Mecánico Intermedio": 0.35,
    "Mecánico Principiante": 0.30,
    "Mecánico Practicante": 0.30,
    "Mecánico": 0.30,
}

# Roles de TRABAJADOR habilitados para tener canal personal y registrar.
ROLES_TRABAJADOR = [
    "Mecánico",
    "Mecánico Experto",
    "Mecánico Intermedio",
    "Mecánico Practicante",
    "Jefe de Mecánico",
    "Jefe Mecánica",
    "Bartender",
    "Jefe de Barra",
    "Jefe Bar",
    "Tatuador",
    "Jefe Tatuador",
    "Jefe Tatuajes",
    "Bailarina",
    "Bailarín",
    "Jefa de Bailarinas",
    "Jefe Show",
    "Prospecto",
]

# Roles de liderazgo total (ven y aprueban TODAS las áreas y la nómina completa del club).
ROLES_LIDERAZGO = ["Dueños", "Sub Dueño", "Jefe de local", "Liderazgo SAMCRO"]
ROL_LIDERAZGO = ROLES_LIDERAZGO

# Nombre EXACTO de la categoría donde el bot va a crear los canales
# personales de cada trabajador. Créala tú primero en Discord (puede estar
# vacía) y pon aquí el mismo nombre.
CATEGORIA_BITACORAS = "📋 BITÁCORAS PERSONALES"

# Nombre EXACTO del canal donde el bot postea el resumen consolidado de
# TODOS los trabajadores cada semana, para que jefes y liderazgo paguen de
# un vistazo. Créalo tú primero (visible solo para jefes + liderazgo).
CANAL_RESUMEN_NOMINA = "nomina-resumen"

# Si es True, cada registro queda "pendiente" hasta que el jefe de esa área
# (o Liderazgo) lo aprueba reaccionando con ✅ en el canal personal del
# trabajador. Solo lo aprobado cuenta para la nómina. Si lo pones en False,
# todo cuenta automático apenas se registra (sistema de honor).
REQUIERE_APROBACION = True

# Día y hora en que se corta la semana y el bot postea los pagos
# automáticamente. DIA: 0 = lunes ... 6 = domingo. HORA en formato 24h
# "HH:MM", en la zona horaria de ZONA_HORARIA.
DIA_CIERRE_SEMANA = 6          # domingo
HORA_CIERRE_SEMANA = "16:00"   # 16:00 hrs

# Zona horaria del club (para que el corte semanal caiga a la hora real)
ZONA_HORARIA = "America/Santiago"
