"""
Bot de Discord para asignar roles de banda con sistema de cooldowns y límites.

REGLAS:
- Solicitar MIEMBRO (mensaje SIN palabra 'jefe'):
    * Máx 5 asignaciones aprobadas por banda por semana (lunes 00:00 UTC).
    * Cooldown 5d para otra banda, 4d para misma banda.
    * No puede estar ya en otra banda.

- Solicitar JEFE (mensaje CON palabra 'jefe'):
    * El usuario debe haber sido miembro de esa banda al menos 15 días
      (sumando todas sus membresías de MIEMBRO en esa banda).
    * Máx 3 jefes ACTIVOS por banda (simultáneos).
    * El bot detecta si el mensaje contiene la palabra 'jefe' (case-insensitive).
"""

import os
import re
import logging
from datetime import datetime, timedelta, timezone

import discord
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
WEEKLY_MEMBER_LIMIT = 5
MAX_LEADERS_PER_BAND = 3
MIN_DAYS_AS_MEMBER_FOR_LEADER = 15

REACTION_CONFIRM = "✅"
LEADER_KEYWORD = "jefe"  # Palabra clave para detectar solicitud de jefe
BYPASS_COOLDOWN_KEYWORDS = {"cooldown", "cd"}  # Palabras que saltan ambos cooldowns

# Configuración de bandas. Para cada banda especifica:
#   - leader_role:   ID del rol de jefe
#   - member_role:   ID del rol de miembro
#   - capacity:      máximo de personas activas (miembros + jefes)
#   - owner_id:      ID del usuario dueño de la banda (puede solicitar/quitar jefes; nadie puede quitarle el rango)
BANDS_CONFIG: list[dict] = [    
     {
        "name":        "GFS",
        "leader_role": 895419164091646002,
         "member_role": 1499222197628043364,
         "capacity":    15,
         "owner_id":    768192318083432518,
 },
     {
         "name":        "Karo gang",
         "leader_role": 1499230616590356622,
         "member_role": 1499230847314690199,
         "capacity":    12,
         "owner_id":    768192318083432518,
     },
]

# Mapeos derivados (se calculan al inicio, no editar a mano)
LEADER_TO_MEMBER_ROLE: dict[int, int] = {}
MEMBER_TO_LEADER_ROLE: dict[int, int] = {}
BAND_CAPACITY: dict[int, int] = {}  # member_role_id -> capacity
BAND_OWNER: dict[int, int] = {}     # member_role_id -> owner user_id

# IDs de roles que pueden confirmar (admin/staff)
STAFF_ROLE_IDS: set[int] = {
    794004890837712987
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
intents.message_content = True  # Necesario para los comandos de texto (!estado, !banda)

bot = commands.Bot(command_prefix="!", intents=intents)
bot.db_pool: asyncpg.Pool | None = None  # type: ignore[attr-defined]


# ===== Base de datos =====
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS band_membership (
    id            BIGSERIAL PRIMARY KEY,
    guild_id      BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    band_role_id  BIGINT NOT NULL,
    role_kind     TEXT NOT NULL DEFAULT 'member',  -- 'member' | 'leader'
    joined_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    left_at       TIMESTAMPTZ
);

-- Migración: añadir columna si no existe (para bases ya creadas)
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
"""


async def init_db() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    return pool


async def get_active_membership(user_id: int, guild_id: int, role_kind: str | None = None):
    """Devuelve la membresía activa del usuario. Si role_kind se da, filtra."""
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
    """Última membresía cerrada (de tipo role_kind)."""
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
            """
            INSERT INTO band_membership (guild_id, user_id, band_role_id, role_kind)
            VALUES ($1, $2, $3, $4)
            """,
            guild_id, user_id, band_role_id, role_kind,
        )


async def close_membership(membership_id: int) -> None:
    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE band_membership SET left_at = NOW() WHERE id = $1",
            membership_id,
        )


async def count_weekly_member_assignments(guild_id: int, band_role_id: int) -> int:
    """Cuenta asignaciones de MIEMBRO aprobadas en la banda durante la semana actual (lunes 00:00 UTC en adelante)."""
    now = datetime.now(timezone.utc)
    # Lunes de esta semana a las 00:00 UTC
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
    """Cuenta jefes activos en la banda. band_role_id es el rol de MIEMBRO (la 'banda')."""
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
    """Cuenta personas únicas activas en la banda (cada persona cuenta 1, sea miembro, jefe o ambos)."""
    async with bot.db_pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT COUNT(DISTINCT user_id) FROM band_membership
            WHERE guild_id = $1 AND band_role_id = $2 AND left_at IS NULL
            """,
            guild_id, band_role_id,
        )


async def continuous_member_days_in_band(user_id: int, guild_id: int, band_role_id: int) -> float:
    """
    Días SEGUIDOS que el usuario lleva como miembro ACTIVO en esta banda.
    Solo considera la membresía actual (left_at IS NULL). Si no es miembro activo, devuelve 0.
    """
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
    """Devuelve el role_id de miembro de la banda según el rol de jefe del autor."""
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


def get_first_user_mention(message: discord.Message) -> discord.Member | None:
    for user in message.mentions:
        if not user.bot:
            member = message.guild.get_member(user.id)
            if member:
                return member
    return None


def is_leader_request(message: discord.Message) -> bool:
    """True si el mensaje contiene la palabra 'jefe' como palabra completa (case-insensitive)."""
    return bool(re.search(rf"\b{LEADER_KEYWORD}\b", message.content, re.IGNORECASE))


def is_bypass_cooldown(message: discord.Message) -> bool:
    """True si el mensaje contiene 'cooldown' o 'cd' como palabra completa (case-insensitive)."""
    pattern = r"\b(" + "|".join(BYPASS_COOLDOWN_KEYWORDS) + r")\b"
    return bool(re.search(pattern, message.content, re.IGNORECASE))


# ===== Verificaciones =====
async def check_member_assign(user_id: int, guild_id: int, target_band_role_id: int, bypass_cooldowns: bool = False) -> tuple[bool, str]:
    """Verifica si se puede asignar rol de MIEMBRO a un usuario.

    Si bypass_cooldowns=True, se saltan los cooldowns de 4 y 5 días
    (pero se mantienen las demás restricciones: no estar en otra banda y límite semanal).
    """
    now = datetime.now(timezone.utc)

    # 1. ¿Ya tiene una banda activa?
    active = await get_active_membership(user_id, guild_id, role_kind="member")
    if active:
        if active["band_role_id"] == target_band_role_id:
            return False, "el usuario ya pertenece a esta banda."
        return False, (
            f"el usuario ya pertenece a otra banda (<@&{active['band_role_id']}>). "
            "Debe salir de ella primero en el canal de quitar rango."
        )

    # 2. Cooldown misma banda (4 días) — se salta si bypass
    if not bypass_cooldowns:
        last_same = await get_last_membership(user_id, guild_id, target_band_role_id, role_kind="member")
        if last_same:
            elapsed = now - last_same["left_at"]
            if elapsed < timedelta(days=COOLDOWN_SAME_BAND_DAYS):
                remaining = timedelta(days=COOLDOWN_SAME_BAND_DAYS) - elapsed
                return False, f"❌ cooldown de la **misma banda** activo. Faltan **{_format_remaining(remaining)}**."

    # 3. Cooldown otra banda (5 días) — se salta si bypass
    if not bypass_cooldowns:
        last_any = await get_last_membership(user_id, guild_id, role_kind="member")
        if last_any and last_any["band_role_id"] != target_band_role_id:
            elapsed = now - last_any["left_at"]
            if elapsed < timedelta(days=COOLDOWN_OTHER_BAND_DAYS):
                remaining = timedelta(days=COOLDOWN_OTHER_BAND_DAYS) - elapsed
                return False, (
                    f"❌ cooldown por haber estado en **otra banda** "
                    f"(<@&{last_any['band_role_id']}>) activo. Faltan **{_format_remaining(remaining)}**."
                )

    # 4. Límite semanal de la banda (5 nuevos por semana) — NO se salta con bypass
    weekly_count = await count_weekly_member_assignments(guild_id, target_band_role_id)
    if weekly_count >= WEEKLY_MEMBER_LIMIT:
        return False, (
            f"❌ esta banda ya alcanzó el límite de **{WEEKLY_MEMBER_LIMIT} nuevos miembros esta semana** "
            f"({weekly_count}/{WEEKLY_MEMBER_LIMIT}). El contador se reinicia el lunes 00:00 UTC."
        )

    # 5. Capacidad máxima de la banda (miembros + jefes) — NO se salta con bypass
    capacity = BAND_CAPACITY.get(target_band_role_id)
    if capacity is not None:
        active_total = await count_active_total_in_band(guild_id, target_band_role_id)
        if active_total >= capacity:
            return False, (
                f"❌ esta banda está **llena** ({active_total}/{capacity} personas, "
                f"incluyendo jefes). Debe salir alguien primero."
            )

    return True, ""


async def check_leader_assign(user_id: int, guild_id: int, target_band_role_id: int) -> tuple[bool, str]:
    """Verifica si se puede asignar rol de JEFE."""
    # 1. ¿Ya es jefe activo de alguna banda?
    active_leader = await get_active_membership(user_id, guild_id, role_kind="leader")
    if active_leader:
        if active_leader["band_role_id"] == target_band_role_id:
            return False, "el usuario ya es jefe de esta banda."
        return False, (
            f"el usuario ya es jefe de otra banda (<@&{active_leader['band_role_id']}>). "
            "Debe dejar el cargo primero."
        )

    # 2. Debe llevar al menos 15 días SEGUIDOS como miembro activo de esta banda
    days_as_member = await continuous_member_days_in_band(user_id, guild_id, target_band_role_id)
    if days_as_member <= 0:
        return False, (
            f"❌ el usuario no es miembro activo de esta banda. "
            f"Debe llevar al menos **{MIN_DAYS_AS_MEMBER_FOR_LEADER} días seguidos** como miembro."
        )
    if days_as_member < MIN_DAYS_AS_MEMBER_FOR_LEADER:
        days_remaining = MIN_DAYS_AS_MEMBER_FOR_LEADER - days_as_member
        return False, (
            f"❌ el usuario lleva solo **{days_as_member:.1f} días seguidos** como miembro de esta banda. "
            f"Necesita **{MIN_DAYS_AS_MEMBER_FOR_LEADER} días seguidos** mínimo "
            f"(faltan ~{days_remaining:.1f} días). Si sale de la banda, el contador se reinicia."
        )

    # 3. Máximo 3 jefes activos en la banda
    leader_count = await count_active_leaders(guild_id, target_band_role_id)
    if leader_count >= MAX_LEADERS_PER_BAND:
        return False, (
            f"❌ esta banda ya tiene el máximo de **{MAX_LEADERS_PER_BAND} jefes** activos "
            f"({leader_count}/{MAX_LEADERS_PER_BAND}). Debe salir uno primero."
        )

    return True, ""


# ===== Eventos =====
@bot.event
async def on_ready():
    # Construir los mapeos derivados desde BANDS_CONFIG
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
        await channel.send(
            f"⚠️ {reactor.mention} El autor del mensaje no tiene rol de jefe de ninguna banda configurada."
        )
        return

    band_role = guild.get_role(band_member_role_id)
    if band_role is None:
        await channel.send(f"⚠️ Rol de banda no encontrado (ID {band_member_role_id}).")
        return

    target = get_first_user_mention(message)
    if target is None:
        await channel.send(
            f"⚠️ {reactor.mention} No se encontró ninguna mención de usuario en el mensaje."
        )
        return

    # ¿Es solicitud de jefe o de miembro?
    leader_request = is_leader_request(message)
    bypass = is_bypass_cooldown(message)

    if payload.channel_id == REQUEST_CHANNEL_ID:
        if leader_request:
            await handle_assign_leader(channel, target, band_role, reactor, leader)
        else:
            await handle_assign_member(channel, target, band_role, reactor, leader, bypass=bypass)
    else:
        if leader_request:
            await handle_remove_leader(channel, target, band_role, reactor, leader)
        else:
            await handle_remove_member(channel, target, band_role, reactor, leader)


# ===== Handlers de asignación =====
async def handle_assign_member(channel, member, band_role, reactor, leader, bypass: bool = False):
    can_join, msg = await check_member_assign(member.id, channel.guild.id, band_role.id, bypass_cooldowns=bypass)
    if not can_join:
        await channel.send(f"⛔ {member.mention}: {msg}")
        return

    reason = f"Miembro asignado por {reactor} (jefe: {leader})"
    if bypass:
        reason += " [BYPASS COOLDOWN]"

    try:
        await member.add_roles(band_role, reason=reason)
    except discord.Forbidden:
        await channel.send("⚠️ No tengo permisos para asignar ese rol.")
        return

    await open_membership(member.id, channel.guild.id, band_role.id, role_kind="member")
    weekly = await count_weekly_member_assignments(channel.guild.id, band_role.id)
    bypass_note = " ⚡ *Cooldowns saltados*" if bypass else ""
    await channel.send(
        f"✅ {member.mention} se ha unido a {band_role.mention} como **miembro**.{bypass_note}\n"
        f"Solicitado por: {leader.mention} · Confirmado por: {reactor.mention} · "
        f"Cupo semanal: {weekly}/{WEEKLY_MEMBER_LIMIT}"
    )


async def handle_assign_leader(channel, member, band_role, reactor, leader):
    # Solo el dueño de la banda puede solicitar nuevos jefes
    owner_id = BAND_OWNER.get(band_role.id)
    if owner_id is None:
        await channel.send(
            f"⚠️ {band_role.mention} no tiene dueño configurado. No se pueden solicitar jefes."
        )
        return
    if leader.id != owner_id:
        await channel.send(
            f"⛔ Solo el **dueño** de {band_role.mention} (<@{owner_id}>) puede solicitar nuevos jefes."
        )
        return

    can_join, msg = await check_leader_assign(member.id, channel.guild.id, band_role.id)
    if not can_join:
        await channel.send(f"⛔ {member.mention}: {msg}")
        return

    # Buscar el rol de jefe correspondiente a esta banda
    leader_role_id = MEMBER_TO_LEADER_ROLE.get(band_role.id)
    if leader_role_id is None:
        await channel.send(f"⚠️ No hay rol de jefe configurado para {band_role.mention}.")
        return
    leader_role = channel.guild.get_role(leader_role_id)
    if leader_role is None:
        await channel.send(f"⚠️ Rol de jefe (ID {leader_role_id}) no encontrado en el servidor.")
        return

    try:
        await member.add_roles(leader_role, reason=f"Jefe asignado por {reactor} (solicitó: {leader})")
    except discord.Forbidden:
        await channel.send("⚠️ No tengo permisos para asignar el rol de jefe.")
        return

    # Registramos como 'leader' usando el ID del rol de MIEMBRO como referencia de la banda
    await open_membership(member.id, channel.guild.id, band_role.id, role_kind="leader")
    leader_count = await count_active_leaders(channel.guild.id, band_role.id)
    await channel.send(
        f"👑 {member.mention} ha sido ascendido a **jefe** de {band_role.mention}.\n"
        f"Solicitado por: {leader.mention} · Confirmado por: {reactor.mention} · "
        f"Jefes activos: {leader_count}/{MAX_LEADERS_PER_BAND}"
    )


# ===== Handlers de remoción =====
async def handle_remove_member(channel, member, band_role, reactor, leader):
    # El dueño no puede ser removido como miembro
    owner_id = BAND_OWNER.get(band_role.id)
    if owner_id and member.id == owner_id:
        await channel.send(
            f"⛔ No se le puede quitar el rango al **dueño** de la banda ({member.mention})."
        )
        return

    active = await get_active_membership(member.id, channel.guild.id, role_kind="member")
    if not active or active["band_role_id"] != band_role.id:
        await channel.send(f"⚠️ {member.mention} no está activamente como miembro en {band_role.mention}.")
        return

    if band_role in member.roles:
        try:
            await member.remove_roles(band_role, reason=f"Removido por {reactor} (jefe: {leader})")
        except discord.Forbidden:
            await channel.send("⚠️ No tengo permisos para remover ese rol.")
            return

    await close_membership(active["id"])
    await channel.send(
        f"👋 {member.mention} ha salido de {band_role.mention}. El cooldown empieza ahora.\n"
        f"Solicitado por: {leader.mention} · Confirmado por: {reactor.mention}"
    )


async def handle_remove_leader(channel, member, band_role, reactor, leader):
    # Solo el dueño puede quitar jefes
    owner_id = BAND_OWNER.get(band_role.id)
    if owner_id is None:
        await channel.send(
            f"⚠️ {band_role.mention} no tiene dueño configurado. No se pueden quitar jefes."
        )
        return
    if leader.id != owner_id:
        await channel.send(
            f"⛔ Solo el **dueño** de {band_role.mention} (<@{owner_id}>) puede quitar el rango de jefe."
        )
        return

    # El dueño no puede ser removido (ni por sí mismo)
    if member.id == owner_id:
        await channel.send(
            f"⛔ No se le puede quitar el rango al **dueño** de la banda ({member.mention})."
        )
        return

    active = await get_active_membership(member.id, channel.guild.id, role_kind="leader")
    if not active or active["band_role_id"] != band_role.id:
        await channel.send(f"⚠️ {member.mention} no es jefe activo de {band_role.mention}.")
        return

    leader_role_id = MEMBER_TO_LEADER_ROLE.get(band_role.id)
    leader_role = channel.guild.get_role(leader_role_id) if leader_role_id else None
    if leader_role and leader_role in member.roles:
        try:
            await member.remove_roles(leader_role, reason=f"Jefe removido por {reactor} (solicitó: {leader})")
        except discord.Forbidden:
            await channel.send("⚠️ No tengo permisos para remover el rol de jefe.")
            return

    await close_membership(active["id"])
    await channel.send(
        f"👋 {member.mention} ya no es jefe de {band_role.mention}.\n"
        f"Solicitado por: {leader.mention} · Confirmado por: {reactor.mention}"
    )


# ===== Comando estado =====
@bot.command(name="estado")
async def estado_cmd(ctx, member: discord.Member | None = None):
    member = member or ctx.author
    active_member = await get_active_membership(member.id, ctx.guild.id, role_kind="member")
    active_leader = await get_active_membership(member.id, ctx.guild.id, role_kind="leader")
    last = await get_last_membership(member.id, ctx.guild.id, role_kind="member")

    lines = [f"**Estado de {member.display_name}:**"]
    if active_member:
        lines.append(f"• Miembro de: <@&{active_member['band_role_id']}> (desde {active_member['joined_at']:%Y-%m-%d %H:%M} UTC)")
    if active_leader:
        lines.append(f"• Jefe de: <@&{active_leader['band_role_id']}> (desde {active_leader['joined_at']:%Y-%m-%d %H:%M} UTC)")
    if not active_member and not active_leader:
        lines.append("• Sin banda actual.")

    if last:
        now = datetime.now(timezone.utc)
        elapsed = now - last["left_at"]
        same_remaining = timedelta(days=COOLDOWN_SAME_BAND_DAYS) - elapsed
        other_remaining = timedelta(days=COOLDOWN_OTHER_BAND_DAYS) - elapsed
        lines.append(f"• Última banda: <@&{last['band_role_id']}> (salió {last['left_at']:%Y-%m-%d %H:%M} UTC)")
        if same_remaining.total_seconds() > 0:
            lines.append(f"• Cooldown misma banda: {_format_remaining(same_remaining)} restantes")
        if other_remaining.total_seconds() > 0:
            lines.append(f"• Cooldown otra banda: {_format_remaining(other_remaining)} restantes")
        if same_remaining.total_seconds() <= 0 and other_remaining.total_seconds() <= 0:
            lines.append("• Sin cooldowns activos.")

    await ctx.send("\n".join(lines))


# ===== Comando: ver estado de la banda =====
@bot.command(name="banda")
async def banda_cmd(ctx, role: discord.Role):
    """Muestra cupo semanal, jefes activos, capacidad y dueño de una banda."""
    if role.id not in MEMBER_TO_LEADER_ROLE:
        await ctx.send(f"⚠️ {role.mention} no es una banda configurada.")
        return
    weekly = await count_weekly_member_assignments(ctx.guild.id, role.id)
    leaders = await count_active_leaders(ctx.guild.id, role.id)
    total = await count_active_total_in_band(ctx.guild.id, role.id)
    capacity = BAND_CAPACITY.get(role.id, "?")
    owner_id = BAND_OWNER.get(role.id)
    owner_line = f"• Dueño: <@{owner_id}>\n" if owner_id else "• Dueño: *no configurado*\n"
    await ctx.send(
        f"**{role.name}**\n"
        f"{owner_line}"
        f"• Personas activas: {total}/{capacity}\n"
        f"• Jefes activos: {leaders}/{MAX_LEADERS_PER_BAND}\n"
        f"• Nuevos miembros esta semana: {weekly}/{WEEKLY_MEMBER_LIMIT}"
    )


# ===== Comandos de registro manual (solo staff) =====
def _staff_only():
    """Decorador para comandos que solo puede usar staff/admin."""
    async def predicate(ctx):
        if is_staff(ctx.author):
            return True
        await ctx.send("⛔ Solo admins/staff pueden usar este comando.")
        return False
    return commands.check(predicate)


@bot.command(name="registrar_miembro")
@_staff_only()
async def registrar_miembro_cmd(ctx, member: discord.Member, banda: discord.Role, dias_atras: int = 0):
    """Registra manualmente a un miembro existente en la BD.
    Uso: !registrar_miembro @user @RolBanda [dias_atras]
    Ejemplo: !registrar_miembro @Juan @LosLobos 20  (lo registra como si llevara 20 días)
    """
    if banda.id not in MEMBER_TO_LEADER_ROLE:
        await ctx.send(f"⚠️ {banda.mention} no es una banda configurada.")
        return

    # Verificar que no haya ya una membresía activa de tipo 'member'
    active = await get_active_membership(member.id, ctx.guild.id, role_kind="member")
    if active and active["band_role_id"] == banda.id:
        await ctx.send(f"⚠️ {member.mention} ya está registrado como miembro de {banda.mention}.")
        return
    if active:
        await ctx.send(
            f"⚠️ {member.mention} ya está registrado en otra banda (<@&{active['band_role_id']}>). "
            "Debe salir de ella primero."
        )
        return

    # Insertar con la fecha calculada
    joined_at = datetime.now(timezone.utc) - timedelta(days=dias_atras)
    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO band_membership (guild_id, user_id, band_role_id, role_kind, joined_at)
            VALUES ($1, $2, $3, 'member', $4)
            """,
            ctx.guild.id, member.id, banda.id, joined_at,
        )

    # Asignar el rol si no lo tiene ya
    if banda not in member.roles:
        try:
            await member.add_roles(banda, reason=f"Registrado manualmente por {ctx.author}")
        except discord.Forbidden:
            await ctx.send("⚠️ Registrado en BD, pero no tengo permisos para asignar el rol.")
            return

    await ctx.send(
        f"✅ {member.mention} registrado como miembro de {banda.mention} "
        f"(fecha de entrada: hace {dias_atras} días)."
    )


@bot.command(name="registrar_jefe")
@_staff_only()
async def registrar_jefe_cmd(ctx, member: discord.Member, banda: discord.Role, dias_atras: int = 0):
    """Registra manualmente a un jefe existente en la BD.
    Uso: !registrar_jefe @user @RolBanda [dias_atras]
    Útil para registrar al dueño inicial u otros jefes que ya tenían el rol antes del bot.
    """
    if banda.id not in MEMBER_TO_LEADER_ROLE:
        await ctx.send(f"⚠️ {banda.mention} no es una banda configurada.")
        return

    # Verificar que no haya ya una membresía activa de tipo 'leader'
    active = await get_active_membership(member.id, ctx.guild.id, role_kind="leader")
    if active and active["band_role_id"] == banda.id:
        await ctx.send(f"⚠️ {member.mention} ya está registrado como jefe de {banda.mention}.")
        return
    if active:
        await ctx.send(
            f"⚠️ {member.mention} ya es jefe de otra banda (<@&{active['band_role_id']}>)."
        )
        return

    joined_at = datetime.now(timezone.utc) - timedelta(days=dias_atras)
    async with bot.db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO band_membership (guild_id, user_id, band_role_id, role_kind, joined_at)
            VALUES ($1, $2, $3, 'leader', $4)
            """,
            ctx.guild.id, member.id, banda.id, joined_at,
        )

    # Asignar el rol de jefe si no lo tiene
    leader_role_id = MEMBER_TO_LEADER_ROLE.get(banda.id)
    leader_role = ctx.guild.get_role(leader_role_id) if leader_role_id else None
    if leader_role and leader_role not in member.roles:
        try:
            await member.add_roles(leader_role, reason=f"Registrado manualmente por {ctx.author}")
        except discord.Forbidden:
            await ctx.send("⚠️ Registrado en BD, pero no tengo permisos para asignar el rol de jefe.")
            return

    await ctx.send(
        f"👑 {member.mention} registrado como jefe de {banda.mention} "
        f"(fecha de entrada: hace {dias_atras} días)."
    )


# ===== Main =====
async def main():
    bot.db_pool = await init_db()
    log.info("Base de datos inicializada.")
    await bot.start(TOKEN)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())