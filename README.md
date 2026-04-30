# Bot de Bandas — Discord

Bot para asignar y quitar roles de banda en un servidor de roleplay con sistema de cooldowns.

## Flujo

Hay **dos canales separados**:

### 📥 Canal de SOLICITAR rango
1. El **jefe** de una banda escribe un mensaje mencionando al usuario:
   > Solicito rango a @JuanPerez
2. Un **admin/staff** reacciona con ✅ al mensaje del jefe.
3. El bot:
   - Identifica la banda según el rol de jefe del autor.
   - Verifica los cooldowns del usuario mencionado.
   - Si todo está bien → asigna el rol de miembro de la banda.
   - Si no → responde indicando el motivo y el tiempo restante.

### 📤 Canal de QUITAR rango
1. El jefe escribe:
   > Quito rango a @JuanPerez
2. Un admin/staff reacciona con ✅.
3. El bot remueve el rol de miembro y registra la salida (inicia el cooldown).

## Reglas de cooldown

- **5 días** desde que el usuario dejó la última banda para poder unirse a una **distinta**.
- **4 días** desde que el usuario dejó una banda para poder volver a la **misma**.
- Un usuario no puede estar en dos bandas a la vez (debe salir primero).

## Comando útil

- `!estado [@usuario]` → muestra la banda actual y los cooldowns activos del usuario.

## Setup

### 1. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 2. Crear base de datos PostgreSQL
```bash
createdb banda_bot
```
El bot crea las tablas automáticamente al iniciar.

### 3. Configurar `.env`
Copia `.env.example` a `.env` y completa los valores:
```bash
cp .env.example .env
```

Variables:
- `DISCORD_TOKEN` — token del bot
- `DATABASE_URL` — URL de PostgreSQL (`postgresql://user:pass@host:port/db`)
- `GUILD_ID` — ID del servidor
- `REQUEST_CHANNEL_ID` — ID del canal de solicitar rango
- `REMOVE_CHANNEL_ID` — ID del canal de quitar rango

### 4. Configurar bandas en `bot.py`

Edita estas dos constantes al inicio del archivo:

```python
LEADER_TO_MEMBER_ROLE = {
    111111111111111111: 222222222222222222,  # ID rol Jefe Banda A -> ID rol Miembro Banda A
    333333333333333333: 444444444444444444,  # ID rol Jefe Banda B -> ID rol Miembro Banda B
    # ... una entrada por cada banda
}

STAFF_ROLE_IDS = {
    555555555555555555,  # ID del rol staff/admin
}
```

Para obtener los IDs: activa **Modo Desarrollador** en Discord (Ajustes → Avanzado) → click derecho sobre canal/rol → "Copiar ID".

### 5. Ejecutar
```bash
python bot.py
```

## Permisos del bot

- `Manage Roles` (gestionar roles)
- `Read Messages` / `View Channels`
- `Send Messages`
- `Read Message History`
- `Add Reactions`

⚠️ **Importante**: el rol del bot debe estar **por encima** de los roles de banda en la jerarquía del servidor para poder asignarlos/quitarlos.

## Esquema de la base de datos

```sql
band_membership (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT,
    user_id      BIGINT,
    band_role_id BIGINT,
    joined_at    TIMESTAMPTZ DEFAULT NOW(),
    left_at      TIMESTAMPTZ            -- NULL = membresía activa
);
```

Cada vez que un usuario entra a una banda se crea una fila. Cuando sale, se actualiza `left_at`. Esto deja un historial completo y permite calcular los cooldowns con precisión.
