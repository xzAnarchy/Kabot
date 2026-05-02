"""
Bot de Discord para asignar roles de banda con sistema de cooldowns y límites.
Versión con SLASH COMMANDS (/).

REGLAS:
- Solicitar MIEMBRO (mensaje SIN palabra 'jefe'):
    * Máx 5 asignaciones aprobadas por banda por semana (lunes 00:00 UTC)
    * Cooldown 5d para otra banda, 4d para misma banda
    * No puede estar ya en otra banda
    * Capacidad máxima de la banda (configurable por banda)

- Solicitar JEFE (mensaje CON palabra 'jefe'):
    * El usuario debe llevar al menos 15 días seguidos como miembro de esa banda
    * Máx 3 jefes activos por banda
    * Solo el dueño puede solicitar nuevos jefes

- Bypass de cooldowns: si el mensaje contiene 'cooldown' o 'cd', se saltan
  los cooldowns de 4 y 5 días (NO se salta el cooldown de desmantelación
  ni el límite semanal ni la capacidad).

- Desmantelación: expulsa a todos y aplica cooldown de 7 días que bloquea
  unirse a CUALQUIER banda.
"""

import os
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
import asyncpg
from dotenv import load_dotenv

load_dotenv()

# ===== Configuración =====
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))

REQUEST_CHANNEL_ID = int(os.getenv("REQUEST_CHANNEL_ID", "0"))
REMOVE_CHANNEL_ID = int(os.getenv("REMOVE_CHANNEL_ID", "0"))

COOLDOWN_OTHER_BAND_DAYS = 5
COOLDOWN_SAME_BAND_DAYS = 4
DISBANDMENT_COOLDOWN_DAYS = 7
WEEKLY_MEMBER_LIMIT = 5
MAX_LEADERS_PER_BAND = 3
MIN_DAYS_AS_MEMBER_FOR_LEADER = 15

REACTION_CONFIRM = "⬇️"
LEADER_KEYWORD = "jefe"  # Palabra clave para detectar solicitud de jefe
BYPASS_COOLDOWN_KEYWORDS = {"cooldown", "cd"}  # Palabras que saltan ambos cooldowns
DEMOTE_KEYWORDS = {"degradar", "degradación", "degradacion", "bajar"}

# Configuración de bandas. Para cada banda especifica:
#   - leader_role:   ID del rol de jefe
#   - member_role:   ID del rol de miembro
#   - capacity:      máximo de personas activas (miembros + jefes)
#   - owner_id:      ID del usuario dueño de la banda (puede solicitar/quitar jefes; nadie puede quitarle el rango)
BANDS_CONFIG: list[dict] = [    
     {
        "name":        "Poison Crew",
        "leader_role": 1194817495211720724,
        "member_role": 1183559269476479038,
        "capacity":    15,
        "owner_id":    1041454182344949771,
 },
     {
         "name":        "Sin Ley",
         "leader_role": 1202433355229040741,
         "member_role": 1202433274425778228,
         "capacity":    15,
         "owner_id":    1317252417645052009,
     },
     {
         "name":        "K9",
         "leader_role": 1209199793730232360,
         "member_role": 1209199543904894986,
         "capacity":    12,
         "owner_id":    1199435499824226534,
     },
     {
         "name":        "La-Sub21",
         "leader_role": 1211157133341757450,
         "member_role": 1211157225146421289,
         "capacity":    15,
         "owner_id":    728800359518306314,
     },
     {
         "name":        "Faze",
         "leader_role": 1213885972148658226,
         "member_role": 1213885841223712818,
         "capacity":    12,
         "owner_id":    702329561622249535,
     },
     {
         "name":        "Pinky Blinders",
         "leader_role": 1232570043175534602,
         "member_role": 1232570426790641684,
         "capacity":    15,
         "owner_id":    430830587641856021,
     },
     {
         "name":        "Faze",
         "leader_role": 1213885972148658226,
         "member_role": 1213885841223712818,
         "capacity":    12,
         "owner_id":    702329561622249535,
     },
     {
         "name":        "Esex Gang",
         "leader_role": 1256718554422706246,
         "member_role": 1256718594067140648,
         "capacity":    15,
         "owner_id":    753750899158941726,
     },
     {
         "name":        "Barrio Chino",
         "leader_role": 1266240280580063256,
         "member_role": 1266240330764910622,
         "capacity":    15,
         "owner_id":    1213250777598918842,
     },
     {
         "name":        "Enigma Crew",
         "leader_role": 1281111966827679836,
         "member_role": 1281112212160774235,
         "capacity":    12,
         "owner_id":    1275573986314686575,
     },
     {
         "name":        "The Monkeys",
         "leader_role": 1283249693412954126,
         "member_role": 1283249702959058956,
         "capacity":    15,
         "owner_id":    1304140223076368435,
     },
     {
         "name":        "Underblood",
         "leader_role": 1283255788139184188,
         "member_role": 1283255795122831410,
         "capacity":    15,
         "owner_id":    1006279091563008092,
     },
     {
         "name":        "C´est La Mort",
         "leader_role": 1355029136783179786,
         "member_role": 1355028215000858624,
         "capacity":    15,
         "owner_id":    1345387913260568637,
     },
     {
         "name":        "Legends Never Die",
         "leader_role": 1407916113685381262,
         "member_role": 1407916043132993567,
         "capacity":    12,
         "owner_id":    700171212654903396,
     },
     {
         "name":        "Spt",
         "leader_role": 1276961456642064384,
         "member_role": 1276961557124747398,
         "capacity":    12,
         "owner_id":    1155673440808865843,
     },
     {
         "name":        "TDZ",
         "leader_role": 1349210777499865109,
         "member_role": 1349210967984050237,
         "capacity":    15,
         "owner_id":    605235307910135808,
     },
]

# Mapeos derivados (se calculan al inicio, no editar a mano)
LEADER_TO_MEMBER_ROLE: dict[int, int] = {}
MEMBER_TO_LEADER_ROLE: dict[int, int] = {}
BAND_CAPACITY: dict[int, int] = {}  # member_role_id -> capacity
BAND_OWNER: dict[int, int] = {}     # member_role_id -> owner user_id

# IDs de roles que pueden confirmar (admin/staff)
STAFF_ROLE_IDS: set[int] = {
    1318979211205021837
}

# ===== Logging =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("band-bot")

# ===== Bot setup =====
intents = discord.Intents.default()
intents.members = True
intents.reactions = True
# message_content NO es necesario para slash commands. Solo se usa para procesar
# mensajes en los canales de solicitar/quitar (donde el bot lee menciones y palabras clave).
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.db_pool: asyncpg.Pool | None = None  # type: ignore[attr-defined]


# ===== Base de datos =====
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS band_membership (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    band_role_id  BIGINT NOT NULL,
    role_kind     TEXT NOT NULL DEFAULT 'member',
    joined_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    left_at       TIMESTAMPTZ
);

ALTER TABLE band_membership
    ADD COLUMN IF NOT EXISTS role_kind TEXT NOT NULL DEFAULT 'member';

CREATE INDEX IF NOT EXISTS idx_membership_user
    ON band_membership (guild_id, user_id, left_at DESC NULLS FIRST);

CREATE INDEX IF NOT EXISTS idx_membership_active
    ON band_membership (guild_id, user_id) WHERE left_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_membership_band_active
    ON band_membership (guild_id, band_role_id, role_kind) WHERE left_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_membership_band_joined
    ON band_membership (guild_id, band_role_id, joined_at);

CREATE TABLE IF NOT EXISTS disbandment_cooldown (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_disbandment_user
    ON disbandment_cooldown (guild_id, user_id, expires_at DESC);
"""


async def init_db() -> asyncpg.Pool:
    if DATABASE_URL:
        try:
            after_at = DATABASE_URL.split("@")[1].split("/")[0]
            log.info(f"Conectando a la base de datos en: {after_at}")
        except Exception:
            log.info("DATABASE_URL configurada pero formato no estándar.")
    else:
        log.error("¡DATABASE_URL no está definida!")

    db_url = DATABASE_URL
    ssl_required = False
    if db_url and "sslmode=require" in db_url:
        ssl_required = True
        db_url = db_url.replace("?sslmode=require", "").replace("&sslmode=require", "")

    kwargs = {"min_size": 1, "max_size": 5}
    if ssl_required or (db_url and "neon.tech" in db_url):
        kwargs["ssl"] = "require"

    pool = await asyncpg.create_pool(db_url, **kwargs)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    return pool


async def get_active_membership(user_id: int, guild_id: int, role_kind: str | None = None):
    async with bot.db_pool.acquire() as conn:
        if role_kind is None:
            return await conn.fetchrow(
                """
                SELECT id, band_role_id, role_kind, joined_at
                FROM band_membership
                WHERE guild_id = $1 AND user_id = $2 AND left_at IS NULL
                ORDER BY joined_at DESC LIMIT 1
                """,
                guild_id, user_id,
            )
        return await conn.fetchrow(
            """
            SELECT id, band_role_id, role_kind, joined_at
            FROM band_membership
            WHERE guild_id = $1 AND user_id = $2 AND role_kind = $3 AND left_at IS NULL
            ORDER BY joined_at DESC LIMIT 1
            """,
            guild_id, user_id, role_kind,
        )


async def get_last_membership(user_id: int, guild_id: int, band_role_id: int | None = None, role_kind: str = "member"):
    async with bot.db_pool.acquire() as conn:
        if band_role_id is None:
            return await conn.fetchrow(
                """
                SELECT band_role_id, joined_at, left_at
                FROM band_membership
                WHERE guild_id = $1 AND user_id = $2 AND role_kind = $3 AND left_at IS NOT NULL
                ORDER BY left_at DESC LIMIT 1
                """,
                guild_id, user_id, role_kind,
            )
        return await conn.fetchrow(
            """
            SELECT band_role_id, joined_at, left_at
            FROM band_membership
            WHERE guild_id = $1 AND user_id = $2 AND band_role_id = $3
              AND role_kind = $4 AND left_at IS NOT NULL
            ORDER BY left_at DESC LIMIT 1
            """,
            guild_id, user_id, band_role_id, role_kind,
        )


async def open_membership(user_id: int, guild_id: int, band_role_id: int, role_kind: str = "member") -> None:
    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO band_membership (guild_id, user_id, band_role_id, role_kind) VALUES ($1, $2, $3, $4)",
            guild_id, user_id, band_role_id, role_kind,
        )


async def close_membership(membership_id: int) -> None:
    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE band_membership SET left_at = NOW() WHERE id = $1",
            membership_id,
        )


async def add_disbandment_cooldown(user_id: int, guild_id: int, days: int, reason: str) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(days=days)
    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO disbandment_cooldown (guild_id, user_id, expires_at, reason) VALUES ($1, $2, $3, $4)",
            guild_id, user_id, expires_at, reason,
        )


async def get_active_disbandment_cooldown(user_id: int, guild_id: int):
    async with bot.db_pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT expires_at, reason FROM disbandment_cooldown
            WHERE guild_id = $1 AND user_id = $2 AND expires_at > NOW()
            ORDER BY expires_at DESC LIMIT 1
            """,
            guild_id, user_id,
        )


async def count_weekly_member_assignments(guild_id: int, band_role_id: int) -> int:
    now = datetime.now(timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    async with bot.db_pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(*) FROM band_membership
            WHERE guild_id = $1 AND band_role_id = $2
              AND role_kind = 'member' AND joined_at >= $3
            """,
            guild_id, band_role_id, monday,
        )


async def count_active_leaders(guild_id: int, band_role_id: int) -> int:
    async with bot.db_pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(*) FROM band_membership
            WHERE guild_id = $1 AND band_role_id = $2
              AND role_kind = 'leader' AND left_at IS NULL
            """,
            guild_id, band_role_id,
        )


async def count_active_total_in_band(guild_id: int, band_role_id: int) -> int:
    async with bot.db_pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(DISTINCT user_id) FROM band_membership
            WHERE guild_id = $1 AND band_role_id = $2 AND left_at IS NULL
            """,
            guild_id, band_role_id,
        )


async def continuous_member_days_in_band(user_id: int, guild_id: int, band_role_id: int) -> float:
    async with bot.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT joined_at FROM band_membership
            WHERE guild_id = $1 AND user_id = $2 AND band_role_id = $3
              AND role_kind = 'member' AND left_at IS NULL
            ORDER BY joined_at DESC LIMIT 1
            """,
            guild_id, user_id, band_role_id,
        )
    if row is None:
        return 0.0
    elapsed = datetime.now(timezone.utc) - row["joined_at"]
    return elapsed.total_seconds() / 86400


# ===== Utilidades =====
SEPARATOR = "-" * 49


def format_message(*lines: str) -> str:
    """Formatea un mensaje uniforme: todas las líneas con viñeta + separador final."""
    if not lines:
        return SEPARATOR
    formatted = [f"- {line}" for line in lines]
    return "\n".join(formatted) + "\n" + SEPARATOR


def _format_remaining(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or "menos de 1m"


def get_band_from_leader(member: discord.Member) -> int | None:
    member_role_ids = {r.id for r in member.roles}
    matching = member_role_ids & LEADER_TO_MEMBER_ROLE.keys()
    if not matching:
        return None
    leader_role_id = next(iter(matching))
    return LEADER_TO_MEMBER_ROLE[leader_role_id]


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return bool({r.id for r in member.roles} & STAFF_ROLE_IDS)


def get_all_user_mentions(message: discord.Message) -> list[discord.Member]:
    """Devuelve TODOS los usuarios mencionados (no bots), preservando el orden y sin duplicados."""
    seen = set()
    members = []
    for user in message.mentions:
        if user.bot or user.id in seen:
            continue
        member = message.guild.get_member(user.id)
        if member:
            seen.add(user.id)
            members.append(member)
    return members


def is_leader_request(message: discord.Message) -> bool:
    return bool(re.search(rf"\b{LEADER_KEYWORD}\b", message.content, re.IGNORECASE))


def is_bypass_cooldown(message: discord.Message) -> bool:
    pattern = r"\b(" + "|".join(BYPASS_COOLDOWN_KEYWORDS) + r")\b"
    return bool(re.search(pattern, message.content, re.IGNORECASE))


def is_demote_request(message: discord.Message) -> bool:
    """True si el mensaje contiene una palabra clave de degradación."""
    pattern = r"\b(" + "|".join(DEMOTE_KEYWORDS) + r")\b"
    return bool(re.search(pattern, message.content, re.IGNORECASE))


# ===== Verificaciones =====
async def check_member_assign(user_id: int, guild_id: int, target_band_role_id: int, bypass_cooldowns: bool = False) -> tuple[bool, list[str]]:
    """Verifica si se puede asignar miembro. Devuelve (puede, [linea_titulo, motivo, estado_actual])."""
    now = datetime.now(timezone.utc)

    active = await get_active_membership(user_id, guild_id, role_kind="member")
    if active:
        if active["band_role_id"] == target_band_role_id:
            return False, [
                "No puedes ingresar a esta OD",
                "Motivo: Ya pertenece a esta banda",
                f"Estado actual: Activo en <@&{active['band_role_id']}>",
            ]
        return False, [
            "No puedes ingresar a esta OD",
            f"Motivo: Ya pertenece a otra banda. Debe salir primero",
            f"Estado actual: Activo en <@&{active['band_role_id']}>",
        ]

    # Cooldown por desmantelación — NO se salta con bypass
    disband_cd = await get_active_disbandment_cooldown(user_id, guild_id)
    if disband_cd:
        remaining = disband_cd["expires_at"] - now
        return False, [
            "No puedes ingresar a esta OD",
            f"Motivo: Cooldown por desmantelación activo ({disband_cd['reason'] or 'sin razón'})",
            f"Estado actual: Faltan {_format_remaining(remaining)}",
        ]

    if not bypass_cooldowns:
        last_same = await get_last_membership(user_id, guild_id, target_band_role_id, role_kind="member")
        if last_same:
            elapsed = now - last_same["left_at"]
            if elapsed < timedelta(days=COOLDOWN_SAME_BAND_DAYS):
                remaining = timedelta(days=COOLDOWN_SAME_BAND_DAYS) - elapsed
                return False, [
                    "Aún no puedes ingresar a la misma OD",
                    "Motivo: Cooldown activo",
                    f"Estado actual: Faltan {_format_remaining(remaining)}",
                ]

        last_any = await get_last_membership(user_id, guild_id, role_kind="member")
        if last_any and last_any["band_role_id"] != target_band_role_id:
            elapsed = now - last_any["left_at"]
            if elapsed < timedelta(days=COOLDOWN_OTHER_BAND_DAYS):
                remaining = timedelta(days=COOLDOWN_OTHER_BAND_DAYS) - elapsed
                return False, [
                    "No puedes ingresar a esta OD",
                    "Motivo: Cooldown activo",
                    f"Estado actual: Faltan {_format_remaining(remaining)}",
                ]

    weekly_count = await count_weekly_member_assignments(guild_id, target_band_role_id)
    if weekly_count >= WEEKLY_MEMBER_LIMIT:
        return False, [
            "No puedes ingresar a esta OD",
            "Motivo: Ya alcanzaron el limite de rotación de integrantes por semana",
            f"Estado actual: {weekly_count}/{WEEKLY_MEMBER_LIMIT} - Se reinicia el lunes 00:00 UTC",
        ]

    capacity = BAND_CAPACITY.get(target_band_role_id)
    if capacity is not None:
        active_total = await count_active_total_in_band(guild_id, target_band_role_id)
        if active_total >= capacity:
            return False, [
                "No puedes ingresar a esta OD",
                "Motivo: Esta banda está llena, debe ser expulsado alguien primero",
                f"Estado actual: {active_total}/{capacity} integrantes",
            ]

    return True, []


async def check_leader_assign(user_id: int, guild_id: int, target_band_role_id: int) -> tuple[bool, list[str]]:
    """Verifica si se puede asignar jefe. Devuelve (puede, [linea_titulo, motivo, estado_actual])."""
    active_leader = await get_active_membership(user_id, guild_id, role_kind="leader")
    if active_leader:
        if active_leader["band_role_id"] == target_band_role_id:
            return False, [
                "No puede ser ascendido a Jefe",
                "Motivo: Ya es Jefe de esta banda",
                f"Estado actual: Activo en <@&{active_leader['band_role_id']}>",
            ]
        return False, [
            "No puede ser ascendido a Jefe",
            "Motivo: Ya es Jefe de otra banda. Debe dejar el cargo primero",
            f"Estado actual: Activo como Jefe en <@&{active_leader['band_role_id']}>",
        ]

    days_as_member = await continuous_member_days_in_band(user_id, guild_id, target_band_role_id)
    if days_as_member <= 0:
        return False, [
            "No puede ser ascendido a Jefe",
            f"Motivo: Necesita completar {MIN_DAYS_AS_MEMBER_FOR_LEADER} días seguidos en la OD",
            f"Estado actual: Tiene 0 días - Faltan {MIN_DAYS_AS_MEMBER_FOR_LEADER} días",
        ]
    if days_as_member < MIN_DAYS_AS_MEMBER_FOR_LEADER:
        days_have = int(days_as_member)
        days_remaining = MIN_DAYS_AS_MEMBER_FOR_LEADER - days_have
        return False, [
            "No puede ser ascendido a Jefe",
            f"Motivo: Necesita completar {MIN_DAYS_AS_MEMBER_FOR_LEADER} días seguidos en la OD",
            f"Estado actual: Tiene {days_have} días - Faltan {days_remaining} días",
        ]

    leader_count = await count_active_leaders(guild_id, target_band_role_id)
    if leader_count >= MAX_LEADERS_PER_BAND:
        return False, [
            "No puede ser ascendido a Jefe",
            f"Motivo: Esta banda ya tiene el máximo de {MAX_LEADERS_PER_BAND} Jefes activos",
            f"Estado actual: Jefes activos {leader_count}/{MAX_LEADERS_PER_BAND}",
        ]

    return True, []


# ===== Eventos =====
@bot.event
async def on_ready():
    global LEADER_TO_MEMBER_ROLE, MEMBER_TO_LEADER_ROLE, BAND_CAPACITY, BAND_OWNER
    LEADER_TO_MEMBER_ROLE = {b["leader_role"]: b["member_role"] for b in BANDS_CONFIG}
    MEMBER_TO_LEADER_ROLE = {b["member_role"]: b["leader_role"] for b in BANDS_CONFIG}
    BAND_CAPACITY = {b["member_role"]: b["capacity"] for b in BANDS_CONFIG}
    BAND_OWNER = {b["member_role"]: b["owner_id"] for b in BANDS_CONFIG if b.get("owner_id")}

    log.info(f"Bot conectado como {bot.user} (ID: {bot.user.id})")
    log.info(f"Canal solicitar: {REQUEST_CHANNEL_ID} | Canal quitar: {REMOVE_CHANNEL_ID}")
    log.info(f"Bandas configuradas: {len(BANDS_CONFIG)}")
    for b in BANDS_CONFIG:
        owner = b.get("owner_id", "sin dueño")
        log.info(f"  - {b['name']}: capacidad {b['capacity']}, dueño: {owner}")

    # Sincronizar slash commands con el guild (instantáneo, no global)
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info(f"✅ Sincronizados {len(synced)} slash commands en el guild {GUILD_ID}")
    else:
        synced = await bot.tree.sync()
        log.info(f"✅ Sincronizados {len(synced)} slash commands globalmente (puede tardar hasta 1h)")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    if str(payload.emoji) != REACTION_CONFIRM:
        return
    if payload.channel_id not in (REQUEST_CHANNEL_ID, REMOVE_CHANNEL_ID):
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    reactor = guild.get_member(payload.user_id)
    if reactor is None or not is_staff(reactor):
        return

    channel = guild.get_channel(payload.channel_id)
    if channel is None:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden):
        return

    leader = guild.get_member(message.author.id)
    if leader is None or leader.bot:
        return

    band_member_role_id = get_band_from_leader(leader)
    if band_member_role_id is None:
        await channel.send(format_message(
            f"{reactor.mention} El autor del mensaje no tiene rol de **Jefe** de ninguna banda configurada"
        ))
        return

    band_role = guild.get_role(band_member_role_id)
    if band_role is None:
        await channel.send(format_message(f"Rol de banda no encontrado (ID {band_member_role_id})"))
        return

    targets = get_all_user_mentions(message)
    if not targets:
        await channel.send(format_message(
            f"{reactor.mention} No se encontró ninguna mención de usuario en el mensaje"
        ))
        return

    leader_request = is_leader_request(message)
    demote_request = is_demote_request(message)
    bypass = is_bypass_cooldown(message)

    # Procesar cada mención por separado
    for target in targets:
        if payload.channel_id == REQUEST_CHANNEL_ID:
            # Asignar: la palabra 'jefe' decide si se asigna jefe o miembro
            if leader_request:
                await handle_assign_leader(channel, target, band_role, reactor, leader)
            else:
                await handle_assign_member(channel, target, band_role, reactor, leader, bypass=bypass)
        else:
            # Quitar:
            #   - 'degradar'/'bajar' -> degrada a miembro (mantiene en la banda, no consume cupo)
            #   - 'jefe' -> solo quita el rol de jefe (sin asignar miembro)
            #   - sin palabras especiales -> expulsión total (jefe y miembro)
            if demote_request:
                await handle_demote(channel, target, band_role, reactor, leader)
            elif leader_request:
                await handle_remove_leader_only(channel, target, band_role, reactor, leader)
            else:
                await handle_remove_full(channel, target, band_role, reactor, leader)


# ===== Handlers de asignación/remoción (por reacción) =====
async def handle_assign_member(channel, member, band_role, reactor, leader, bypass: bool = False):
    can_join, error_lines = await check_member_assign(member.id, channel.guild.id, band_role.id, bypass_cooldowns=bypass)
    if not can_join:
        # error_lines[0] es el título, el resto son viñetas
        await channel.send(format_message(
            f"{member.mention} {error_lines[0]}",
            *error_lines[1:],
        ))
        return

    reason = f"Miembro asignado por {reactor} (jefe: {leader})"
    if bypass:
        reason += " [BYPASS COOLDOWN]"

    try:
        await member.add_roles(band_role, reason=reason)
    except discord.Forbidden:
        await channel.send(format_message("No tengo permisos para asignar ese rol"))
        return

    await open_membership(member.id, channel.guild.id, band_role.id, role_kind="member")
    weekly = await count_weekly_member_assignments(channel.guild.id, band_role.id)
    bypass_note = " (Cooldowns saltados)" if bypass else ""
    await channel.send(format_message(
        f"{member.mention} Ahora es el nuevo integrante de {band_role.mention}{bypass_note}",
        f"Solicitado por {leader.mention}",
        f"Confirmado por {reactor.mention}",
        f"Estado actual: Cupo Semanal {weekly}/{WEEKLY_MEMBER_LIMIT}",
    ))


async def handle_assign_leader(channel, member, band_role, reactor, leader):
    owner_id = BAND_OWNER.get(band_role.id)
    if owner_id is None:
        await channel.send(format_message(f"{band_role.mention} no tiene **dueño** configurado"))
        return
    if leader.id != owner_id:
        await channel.send(format_message(
            f"Solo el **dueño** de {band_role.mention} (<@{owner_id}>) puede solicitar nuevos Jefes"
        ))
        return

    can_join, error_lines = await check_leader_assign(member.id, channel.guild.id, band_role.id)
    if not can_join:
        await channel.send(format_message(
            f"{member.mention} {error_lines[0]}",
            *error_lines[1:],
        ))
        return

    leader_role_id = MEMBER_TO_LEADER_ROLE.get(band_role.id)
    if leader_role_id is None:
        await channel.send(format_message(f"No hay rol de **Jefe** configurado para {band_role.mention}"))
        return
    leader_role = channel.guild.get_role(leader_role_id)
    if leader_role is None:
        await channel.send(format_message(f"Rol de **Jefe** (ID {leader_role_id}) no encontrado"))
        return

    # Cerrar la membresía de tipo 'member' (si existe) — los jefes no son miembros
    active_member = await get_active_membership(member.id, channel.guild.id, role_kind="member")
    if active_member and active_member["band_role_id"] == band_role.id:
        await close_membership(active_member["id"])

    # Quitar el rol de miembro y agregar el de jefe en Discord
    try:
        if band_role in member.roles:
            await member.remove_roles(band_role, reason=f"Ascendido a jefe por {leader}")
        await member.add_roles(leader_role, reason=f"Jefe asignado por {reactor} (solicitó: {leader})")
    except discord.Forbidden:
        await channel.send(format_message("No tengo permisos para gestionar los roles"))
        return

    await open_membership(member.id, channel.guild.id, band_role.id, role_kind="leader")
    leader_count = await count_active_leaders(channel.guild.id, band_role.id)
    await channel.send(format_message(
        f"{member.mention} Ha sido ascendido a Jefe de {band_role.mention}",
        f"Solicitado por {leader.mention}",
        f"Confirmado por {reactor.mention}",
        f"Estado actual: Jefes activos {leader_count}/{MAX_LEADERS_PER_BAND}",
    ))


async def handle_remove_full(channel, member, band_role, reactor, leader):
    """Expulsa COMPLETAMENTE de la banda: quita rol de jefe (si lo tiene), quita rol de miembro,
    cierra todas las membresías activas y aplica cooldown."""
    owner_id = BAND_OWNER.get(band_role.id)
    if owner_id and member.id == owner_id:
        await channel.send(format_message(f"No se le puede quitar el rango al **dueño** de la banda ({member.mention})"))
        return

    active_member = await get_active_membership(member.id, channel.guild.id, role_kind="member")
    active_leader = await get_active_membership(member.id, channel.guild.id, role_kind="leader")

    is_member_here = active_member and active_member["band_role_id"] == band_role.id
    is_leader_here = active_leader and active_leader["band_role_id"] == band_role.id

    if not is_member_here and not is_leader_here:
        await channel.send(format_message(f"{member.mention} no está activamente en {band_role.mention}"))
        return

    # Si es jefe, solo el dueño puede expulsarlo
    if is_leader_here and leader.id != owner_id:
        await channel.send(format_message(
            f"Solo el **dueño** de {band_role.mention} (<@{owner_id}>) puede expulsar a un **Jefe**"
        ))
        return

    # Quitar todos los roles de la banda en Discord
    leader_role_id = MEMBER_TO_LEADER_ROLE.get(band_role.id)
    leader_role = channel.guild.get_role(leader_role_id) if leader_role_id else None
    roles_to_remove = []
    if leader_role and leader_role in member.roles:
        roles_to_remove.append(leader_role)
    if band_role in member.roles:
        roles_to_remove.append(band_role)
    if roles_to_remove:
        try:
            await member.remove_roles(*roles_to_remove, reason=f"Expulsado por {reactor} (jefe: {leader})")
        except discord.Forbidden:
            await channel.send(format_message("No tengo permisos para remover los roles"))
            return

    # Cerrar todas las membresías activas
    if is_leader_here:
        await close_membership(active_leader["id"])
    if is_member_here:
        await close_membership(active_member["id"])

    await channel.send(format_message(
        f"{member.mention} Ha salido de {band_role.mention}",
        f"Solicitado por {leader.mention}",
        f"Confirmado por {reactor.mention}",
        "Estado actual: Se activó el cooldown",
    ))


async def handle_remove_leader_only(channel, member, band_role, reactor, leader):
    """Solo quita el rol de jefe (sin degradar ni asignar miembro). El usuario queda sin nada."""
    owner_id = BAND_OWNER.get(band_role.id)
    if owner_id is None:
        await channel.send(format_message(f"{band_role.mention} no tiene **dueño** configurado"))
        return
    if leader.id != owner_id:
        await channel.send(format_message(
            f"Solo el **dueño** de {band_role.mention} (<@{owner_id}>) puede quitar el rango de **Jefe**"
        ))
        return

    if member.id == owner_id:
        await channel.send(format_message(f"No se le puede quitar el rango al **dueño** ({member.mention})"))
        return

    active = await get_active_membership(member.id, channel.guild.id, role_kind="leader")
    if not active or active["band_role_id"] != band_role.id:
        await channel.send(format_message(f"{member.mention} no es **Jefe** activo de {band_role.mention}"))
        return

    leader_role_id = MEMBER_TO_LEADER_ROLE.get(band_role.id)
    leader_role = channel.guild.get_role(leader_role_id) if leader_role_id else None

    # Solo quitar el rol de jefe, no asignar nada
    if leader_role and leader_role in member.roles:
        try:
            await member.remove_roles(leader_role, reason=f"Jefe removido por {reactor} (solicitó: {leader})")
        except discord.Forbidden:
            await channel.send(format_message("No tengo permisos para remover el rol de **Jefe**"))
            return

    await close_membership(active["id"])

    leader_count = await count_active_leaders(channel.guild.id, band_role.id)
    await channel.send(format_message(
        f"{member.mention} Ya no es Jefe de {band_role.mention}",
        f"Solicitado por {leader.mention}",
        f"Confirmado por {reactor.mention}",
        f"Estado actual: Jefes activos {leader_count}/{MAX_LEADERS_PER_BAND}",
    ))


async def handle_demote(channel, member, band_role, reactor, leader):
    """Degrada de jefe a miembro: quita rol de jefe, asigna rol de miembro.
    NO consume cupo semanal porque es un cambio de rol, no una entrada nueva."""
    owner_id = BAND_OWNER.get(band_role.id)
    if owner_id is None:
        await channel.send(format_message(f"{band_role.mention} no tiene **dueño** configurado"))
        return
    if leader.id != owner_id:
        await channel.send(format_message(
            f"Solo el **dueño** de {band_role.mention} (<@{owner_id}>) puede degradar a un **Jefe**"
        ))
        return

    if member.id == owner_id:
        await channel.send(format_message(f"No se le puede degradar al **dueño** ({member.mention})"))
        return

    active = await get_active_membership(member.id, channel.guild.id, role_kind="leader")
    if not active or active["band_role_id"] != band_role.id:
        await channel.send(format_message(f"{member.mention} no es **Jefe** activo de {band_role.mention}"))
        return

    leader_role_id = MEMBER_TO_LEADER_ROLE.get(band_role.id)
    leader_role = channel.guild.get_role(leader_role_id) if leader_role_id else None

    # Quitar rol de jefe y poner rol de miembro
    try:
        if leader_role and leader_role in member.roles:
            await member.remove_roles(leader_role, reason=f"Degradado por {reactor} (solicitó: {leader})")
        if band_role not in member.roles:
            await member.add_roles(band_role, reason=f"Degradado a integrante por {leader}")
    except discord.Forbidden:
        await channel.send(format_message("No tengo permisos para gestionar los roles"))
        return

    # Cerrar la membresía 'leader'
    await close_membership(active["id"])

    # Abrir nueva membresía 'member' con flag para no contar en cupo semanal.
    # Para no contar en el cupo semanal, marcamos joined_at como la fecha original
    # de cuando entró a la banda (recuperándola de su última membresía 'member' cerrada,
    # o si no existe, usamos la fecha en que se hizo jefe).
    last_member = await get_last_membership(member.id, channel.guild.id, band_role.id, role_kind="member")
    # Usar la fecha más antigua razonable: o cuando fue miembro originalmente, o cuando fue jefe
    if last_member:
        original_joined = last_member["joined_at"]
    else:
        original_joined = active["joined_at"]

    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO band_membership (guild_id, user_id, band_role_id, role_kind, joined_at) VALUES ($1, $2, $3, 'member', $4)",
            channel.guild.id, member.id, band_role.id, original_joined,
        )

    leader_count = await count_active_leaders(channel.guild.id, band_role.id)
    await channel.send(format_message(
        f"{member.mention} Ya no es Jefe de {band_role.mention}, ha sido degradado a integrante",
        f"Solicitado por {leader.mention}",
        f"Confirmado por {reactor.mention}",
        f"Estado actual: Jefes activos {leader_count}/{MAX_LEADERS_PER_BAND}",
    ))


# ===== SLASH COMMANDS =====

@bot.tree.command(name="estado", description="Muestra el estado y cooldowns de un usuario")
@app_commands.describe(usuario="El usuario a consultar (por defecto, tú mismo)")
async def estado_slash(interaction: discord.Interaction, usuario: discord.Member | None = None):
    member = usuario or interaction.user
    active_member = await get_active_membership(member.id, interaction.guild.id, role_kind="member")
    active_leader = await get_active_membership(member.id, interaction.guild.id, role_kind="leader")
    last = await get_last_membership(member.id, interaction.guild.id, role_kind="member")

    lines = [f"**Estado de {member.display_name}:**"]
    if active_member:
        lines.append(f"Miembro de: <@&{active_member['band_role_id']}> (desde {active_member['joined_at']:%Y-%m-%d %H:%M} UTC)")
    if active_leader:
        lines.append(f"Jefe de: <@&{active_leader['band_role_id']}> (desde {active_leader['joined_at']:%Y-%m-%d %H:%M} UTC)")
    if not active_member and not active_leader:
        lines.append("Sin banda actual")

    if last:
        now = datetime.now(timezone.utc)
        elapsed = now - last["left_at"]
        same_remaining = timedelta(days=COOLDOWN_SAME_BAND_DAYS) - elapsed
        other_remaining = timedelta(days=COOLDOWN_OTHER_BAND_DAYS) - elapsed
        lines.append(f"Última banda: <@&{last['band_role_id']}> (salió {last['left_at']:%Y-%m-%d %H:%M} UTC)")
        if same_remaining.total_seconds() > 0:
            lines.append(f"Cooldown misma banda: {_format_remaining(same_remaining)} restantes")
        if other_remaining.total_seconds() > 0:
            lines.append(f"Cooldown otra banda: {_format_remaining(other_remaining)} restantes")
        if same_remaining.total_seconds() <= 0 and other_remaining.total_seconds() <= 0:
            lines.append("Sin cooldowns activos")

    disband_cd = await get_active_disbandment_cooldown(member.id, interaction.guild.id)
    if disband_cd:
        remaining = disband_cd["expires_at"] - datetime.now(timezone.utc)
        lines.append(
            f"**Cooldown por desmantelación**: {_format_remaining(remaining)} restantes "
            f"({disband_cd['reason']})"
        )

    await interaction.response.send_message(format_message(*lines))


@bot.tree.command(name="banda", description="Muestra cupo, jefes y dueño de una banda")
@app_commands.describe(banda="La banda a consultar")
async def banda_slash(interaction: discord.Interaction, banda: discord.Role):
    if banda.id not in MEMBER_TO_LEADER_ROLE:
        await interaction.response.send_message(format_message(f"{banda.mention} no es una banda configurada"), ephemeral=True)
        return
    weekly = await count_weekly_member_assignments(interaction.guild.id, banda.id)
    leaders = await count_active_leaders(interaction.guild.id, banda.id)
    total = await count_active_total_in_band(interaction.guild.id, banda.id)
    capacity = BAND_CAPACITY.get(banda.id, "?")
    owner_id = BAND_OWNER.get(banda.id)
    owner_line = f"**Dueño**: <@{owner_id}>" if owner_id else "**Dueño**: no configurado"
    await interaction.response.send_message(format_message(
        f"**{banda.name}**",
        owner_line,
        f"Personas activas: {total}/{capacity}",
        f"Jefes activos: {leaders}/{MAX_LEADERS_PER_BAND}",
        f"Nuevos miembros esta semana: {weekly}/{WEEKLY_MEMBER_LIMIT}",
    ))


@bot.tree.command(name="registrar_miembro", description="[Staff] Registra manualmente a un miembro existente")
@app_commands.describe(
    usuario="El usuario a registrar",
    banda="La banda donde registrarlo",
    dias_atras="Hace cuántos días entró (0 = hoy)",
)
async def registrar_miembro_slash(
    interaction: discord.Interaction,
    usuario: discord.Member,
    banda: discord.Role,
    dias_atras: int = 0,
):
    if not is_staff(interaction.user):
        await interaction.response.send_message(format_message("Solo admins/staff pueden usar este comando"), ephemeral=True)
        return
    if banda.id not in MEMBER_TO_LEADER_ROLE:
        await interaction.response.send_message(format_message(f"{banda.mention} no es una banda configurada"), ephemeral=True)
        return

    active = await get_active_membership(usuario.id, interaction.guild.id, role_kind="member")
    if active and active["band_role_id"] == banda.id:
        await interaction.response.send_message(format_message(f"{usuario.mention} ya está registrado como **miembro** de {banda.mention}"), ephemeral=True)
        return
    if active:
        await interaction.response.send_message(format_message(
            f"{usuario.mention} ya está en otra banda (<@&{active['band_role_id']}>)"
        ), ephemeral=True)
        return

    joined_at = datetime.now(timezone.utc) - timedelta(days=dias_atras)
    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO band_membership (guild_id, user_id, band_role_id, role_kind, joined_at) VALUES ($1, $2, $3, 'member', $4)",
            interaction.guild.id, usuario.id, banda.id, joined_at,
        )

    if banda not in usuario.roles:
        try:
            await usuario.add_roles(banda, reason=f"Registrado manualmente por {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(format_message("Registrado en BD pero no tengo permisos para asignar el rol"), ephemeral=True)
            return

    await interaction.response.send_message(format_message(
        f"{usuario.mention} registrado como **miembro** de {banda.mention}",
        f"Fecha de entrada: hace {dias_atras} días",
        f"Confirmado por: {interaction.user.mention}",
    ))


@bot.tree.command(name="registrar_jefe", description="[Staff] Registra manualmente a un jefe existente")
@app_commands.describe(
    usuario="El usuario a registrar como jefe",
    banda="La banda donde registrarlo",
    dias_atras="Hace cuántos días es jefe (0 = hoy)",
)
async def registrar_jefe_slash(
    interaction: discord.Interaction,
    usuario: discord.Member,
    banda: discord.Role,
    dias_atras: int = 0,
):
    if not is_staff(interaction.user):
        await interaction.response.send_message(format_message("Solo admins/staff pueden usar este comando"), ephemeral=True)
        return
    if banda.id not in MEMBER_TO_LEADER_ROLE:
        await interaction.response.send_message(format_message(f"{banda.mention} no es una banda configurada"), ephemeral=True)
        return

    active = await get_active_membership(usuario.id, interaction.guild.id, role_kind="leader")
    if active and active["band_role_id"] == banda.id:
        await interaction.response.send_message(format_message(f"{usuario.mention} ya es **Jefe** de {banda.mention}"), ephemeral=True)
        return
    if active:
        await interaction.response.send_message(format_message(
            f"{usuario.mention} ya es **Jefe** de otra banda (<@&{active['band_role_id']}>)"
        ), ephemeral=True)
        return

    joined_at = datetime.now(timezone.utc) - timedelta(days=dias_atras)
    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO band_membership (guild_id, user_id, band_role_id, role_kind, joined_at) VALUES ($1, $2, $3, 'leader', $4)",
            interaction.guild.id, usuario.id, banda.id, joined_at,
        )

    leader_role_id = MEMBER_TO_LEADER_ROLE.get(banda.id)
    leader_role = interaction.guild.get_role(leader_role_id) if leader_role_id else None
    if leader_role and leader_role not in usuario.roles:
        try:
            await usuario.add_roles(leader_role, reason=f"Registrado manualmente por {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(format_message("Registrado en BD pero no tengo permisos para asignar el rol"), ephemeral=True)
            return

    await interaction.response.send_message(format_message(
        f"{usuario.mention} registrado como **Jefe** de {banda.mention}",
        f"Fecha de entrada: hace {dias_atras} días",
        f"Confirmado por: {interaction.user.mention}",
    ))


@bot.tree.command(name="desmantelacion", description="[Staff] Desmantela una banda y aplica cooldown de 7 días a todos")
@app_commands.describe(banda="La banda a desmantelar")
async def desmantelacion_slash(interaction: discord.Interaction, banda: discord.Role):
    if not is_staff(interaction.user):
        await interaction.response.send_message(format_message("Solo admins/staff pueden usar este comando"), ephemeral=True)
        return
    if banda.id not in MEMBER_TO_LEADER_ROLE:
        await interaction.response.send_message(format_message(f"{banda.mention} no es una banda configurada"), ephemeral=True)
        return

    # Confirmación con un botón
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.confirmed = False

        @discord.ui.button(label="Confirmar desmantelación", style=discord.ButtonStyle.danger, emoji="💥")
        async def confirm(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
            if btn_interaction.user.id != interaction.user.id:
                await btn_interaction.response.send_message("Solo quien ejecutó el comando puede confirmar.", ephemeral=True)
                return
            self.confirmed = True
            self.stop()
            await btn_interaction.response.defer()

        @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
        async def cancel(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
            if btn_interaction.user.id != interaction.user.id:
                await btn_interaction.response.send_message("Solo quien ejecutó el comando puede cancelar.", ephemeral=True)
                return
            self.stop()
            await btn_interaction.response.defer()

    view = ConfirmView()
    await interaction.response.send_message(format_message(
        "**CONFIRMACIÓN REQUERIDA**",
        f"Vas a desmantelar **{banda.name}**:",
        "Expulsa a TODOS los miembros y Jefes (incluido el dueño)",
        f"Cooldown de **{DISBANDMENT_COOLDOWN_DAYS} días** para unirse a CUALQUIER banda",
        "Esta acción NO se puede deshacer",
        "Tienes 30 segundos para confirmar",
    ), view=view)

    await view.wait()
    if not view.confirmed:
        await interaction.followup.send(format_message("Desmantelación cancelada"))
        return

    async with bot.db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, user_id, role_kind FROM band_membership WHERE guild_id = $1 AND band_role_id = $2 AND left_at IS NULL",
            interaction.guild.id, banda.id,
        )

    if not rows:
        await interaction.followup.send(format_message(f"{banda.mention} no tiene miembros ni Jefes activos"))
        return

    affected_user_ids = {row["user_id"] for row in rows}
    leader_role_id = MEMBER_TO_LEADER_ROLE.get(banda.id)
    leader_role = interaction.guild.get_role(leader_role_id) if leader_role_id else None
    expelled_count = 0
    role_errors = 0
    reason_str = f"Desmantelación de {banda.name} por {interaction.user}"

    for user_id in affected_user_ids:
        for row in rows:
            if row["user_id"] == user_id:
                await close_membership(row["id"])

        member = interaction.guild.get_member(user_id)
        if member is not None:
            roles_to_remove = []
            if banda in member.roles:
                roles_to_remove.append(banda)
            if leader_role and leader_role in member.roles:
                roles_to_remove.append(leader_role)
            if roles_to_remove:
                try:
                    await member.remove_roles(*roles_to_remove, reason=reason_str)
                except discord.Forbidden:
                    role_errors += 1

        await add_disbandment_cooldown(
            user_id, interaction.guild.id, DISBANDMENT_COOLDOWN_DAYS,
            reason=f"Desmantelación de {banda.name}",
        )
        expelled_count += 1

    msg_lines = [
        f"**{banda.name}** ha sido desmantelada",
        f"Personas expulsadas: **{expelled_count}**",
        f"Cooldown aplicado: **{DISBANDMENT_COOLDOWN_DAYS} días** para unirse a cualquier banda",
        f"Confirmado por: {interaction.user.mention}",
    ]
    if role_errors:
        msg_lines.append(f"No pude quitar el rol a {role_errors} usuarios (problema de permisos)")
    await interaction.followup.send(format_message(*msg_lines))


# ===== Main =====
async def main():
    bot.db_pool = await init_db()
    log.info("Base de datos inicializada.")
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
