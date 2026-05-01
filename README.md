# Bot de Bandas — Discord

Bot para gestionar roles de bandas en un servidor de roleplay con sistema de cooldowns, límites semanales, jerarquía dueño/jefe/miembro, y desmantelación de bandas.

---

## 📋 Cómo funciona

### Asignación y remoción de roles (por reacción)

Hay **dos canales separados**:

#### 📥 Canal de SOLICITAR rango

1. Un **jefe** de una banda escribe un mensaje mencionando al usuario:
   > `Solicito rango a @JuanPerez`
2. Un **admin/staff** reacciona con ✅ al mensaje.
3. El bot identifica la banda según el rol de jefe del autor, verifica todas las reglas y asigna el rol.

#### 📤 Canal de QUITAR rango

1. El jefe escribe:
   > `Quito rango a @JuanPerez`
2. Un admin/staff reacciona con ✅.
3. El bot remueve el rol y registra la salida (inicia el cooldown).

### Palabras clave especiales en el mensaje

El bot lee el contenido del mensaje del jefe para detectar:

| Palabra           | Efecto                                                                |
| ----------------- | --------------------------------------------------------------------- |
| `jefe`            | Procesa la acción como solicitud/remoción de **jefe** (no de miembro) |
| `cooldown` o `cd` | Salta los cooldowns de 4 y 5 días al asignar miembro                  |

Ejemplos:

- `Solicito rango a @user` → asigna miembro
- `Solicito jefe a @user` → asigna jefe (solo el dueño puede)
- `Solicito rango a @user cd` → asigna miembro saltándose los cooldowns
- `Quito jefe a @user` → quita rol de jefe (solo el dueño puede)

---

## 🎯 Reglas del sistema

### Para asignar MIEMBRO

| Restricción             | Detalle                             | ¿Salta con `cd`? |
| ----------------------- | ----------------------------------- | ---------------- |
| Ya está en otra banda   | Debe salir primero                  | No               |
| Cooldown desmantelación | 7 días para CUALQUIER banda         | No               |
| Cooldown misma banda    | 4 días desde la salida              | Sí               |
| Cooldown otra banda     | 5 días desde la salida              | Sí               |
| Límite semanal          | Máx. 5 nuevos miembros/banda/semana | No               |
| Capacidad de banda      | 12 o 15 según configuración         | No               |

### Para asignar JEFE

| Restricción                   | Detalle                                         |
| ----------------------------- | ----------------------------------------------- |
| Solo el dueño puede solicitar | El jefe que escribe debe ser el `owner_id`      |
| Días seguidos como miembro    | Mínimo 15 días seguidos (sin salir) en la banda |
| Máximo de jefes activos       | 3 por banda                                     |
| No puede ser jefe de 2 bandas | Una sola jefatura activa por persona            |

### Protecciones del dueño

- No se le puede quitar el rol de miembro
- No se le puede quitar el rol de jefe
- Solo es removido en caso de desmantelación

---

## 🤖 Slash Commands

Escribe `/` en cualquier canal donde el bot tenga acceso para ver el menú con autocompletado.

| Comando                                             | Quién      | Descripción                                                           |
| --------------------------------------------------- | ---------- | --------------------------------------------------------------------- |
| `/estado [usuario]`                                 | Cualquiera | Banda actual, última banda y cooldowns activos                        |
| `/banda [banda]`                                    | Cualquiera | Cupo, jefes activos, dueño y nuevos miembros de la semana             |
| `/registrar_miembro [usuario] [banda] [días_atrás]` | Staff      | Registra manualmente a un miembro existente                           |
| `/registrar_jefe [usuario] [banda] [días_atrás]`    | Staff      | Registra manualmente a un jefe existente (útil para el dueño inicial) |
| `/desmantelacion [banda]`                           | Staff      | Expulsa a todos y aplica cooldown de 7 días                           |

### Sobre `/desmantelacion`

Sale un botón rojo de confirmación que solo quien ejecutó el comando puede pulsar (timeout 30 s). Si confirma:

- Cierra todas las membresías activas (miembros + jefes) en la BD
- Quita los roles de Discord
- Aplica un cooldown de 7 días que bloquea unirse a CUALQUIER banda
- Esto incluye al dueño

---

## ⚙️ Setup

### 1. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 2. Crear base de datos

Opciones:

- **Local**: instalar PostgreSQL y crear una BD vacía con `createdb banda_bot`
- **Cloud (recomendado)**: crear cuenta gratis en [Neon](https://neon.tech) y copiar la connection string

El bot crea las tablas automáticamente al iniciar.

### 3. Configurar variables de entorno

Copia `.env.example` a `.env` y completa los valores:

```bash
cp .env.example .env
```

Variables:

- `DISCORD_TOKEN` — token del bot ([Discord Developer Portal](https://discord.com/developers/applications))
- `DATABASE_URL` — URL de PostgreSQL (`postgresql://user:pass@host:port/db?sslmode=require` para Neon)
- `GUILD_ID` — ID de tu servidor (con esto los slash commands se sincronizan al instante)
- `REQUEST_CHANNEL_ID` — ID del canal de solicitar rango
- `REMOVE_CHANNEL_ID` — ID del canal de quitar rango

### 4. Configurar bandas en `bot.py`

Edita las constantes al inicio del archivo:

```python
BANDS_CONFIG = [
    {
        "name":        "Los Lobos",
        "leader_role": 111111111111111111,  # ID del rol de jefe
        "member_role": 222222222222222222,  # ID del rol de miembro
        "capacity":    15,                  # Máx. personas activas (jefes + miembros)
        "owner_id":    777777777777777777,  # ID del usuario dueño
    },
    {
        "name":        "Los Halcones",
        "leader_role": 333333333333333333,
        "member_role": 444444444444444444,
        "capacity":    12,
        "owner_id":    888888888888888888,
    },
]

STAFF_ROLE_IDS = {
    555555555555555555,  # ID del rol staff/admin
}
```

Para obtener los IDs: activa **Modo Desarrollador** en Discord (Ajustes → Avanzado) → click derecho sobre canal/rol/usuario → "Copiar ID".

### 5. Activar Privileged Intents en el portal de Discord

En [Discord Developer Portal](https://discord.com/developers/applications) → tu bot → **Bot** → activar:

- ✅ **Server Members Intent**
- ✅ **Message Content Intent**

### 6. Ejecutar

```bash
python bot.py
```

Si todo está bien, en los logs verás:

```
[INFO] Bot conectado como TuBot#1234
[INFO] Bandas configuradas: N
[INFO] ✅ Sincronizados 5 slash commands en el guild ...
```

---

## 🛡️ Permisos del bot

El bot necesita estos permisos en Discord:

- `Manage Roles`
- `View Channels`
- `Send Messages`
- `Read Message History`
- `Add Reactions`
- `Use Slash Commands`

⚠️ **Importante**: el rol del bot debe estar **por encima** de los roles de banda en la jerarquía del servidor para poder asignarlos/quitarlos.

---

## 🚀 Hosting 24/7

Para que el bot esté siempre online:

| Opción                            | Coste               | Dificultad      |
| --------------------------------- | ------------------- | --------------- |
| **Railway** + **Neon** (Postgres) | Gratis con créditos | ⭐ Muy fácil    |
| **Oracle Cloud Free Tier**        | Gratis para siempre | ⭐⭐ Media      |
| **VPS (Hetzner, Vultr)**          | $3-5/mes            | ⭐⭐⭐ Avanzada |

### Setup rápido en Railway

1. Subir el código a GitHub
2. En Railway: **New Project** → **Deploy from GitHub**
3. Añadir las variables de entorno en la pestaña **Variables**
4. Para la BD: usar [Neon](https://neon.tech) y pegar su `DATABASE_URL`

⚠️ La URL de Neon debe terminar en `?sslmode=require`. El bot lo maneja automáticamente.

---

## 🗄️ Esquema de la base de datos

```sql
band_membership (
    id           BIGSERIAL PRIMARY KEY,
    guild_id     BIGINT,
    user_id      BIGINT,
    band_role_id BIGINT,           -- ID del rol de MIEMBRO de la banda
    role_kind    TEXT,              -- 'member' | 'leader'
    joined_at    TIMESTAMPTZ DEFAULT NOW(),
    left_at      TIMESTAMPTZ        -- NULL = activa
);

disbandment_cooldown (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT,
    user_id     BIGINT,
    expires_at  TIMESTAMPTZ,
    reason      TEXT
);
```

Cada vez que un usuario entra a una banda se crea una fila en `band_membership`. Cuando sale, se actualiza `left_at`. Esto deja un historial completo y permite calcular cooldowns con precisión.

⚠️ **Importante con las fechas**: si modificas `joined_at` manualmente, usa siempre UTC con timezone explícito:

```sql
-- Correcto:
UPDATE band_membership SET joined_at = NOW() - INTERVAL '15 days' WHERE id = X;
UPDATE band_membership SET joined_at = '2026-04-14 10:00:00+00' WHERE id = X;

-- Incorrecto (usa la timezone de tu sesión, que puede dar días negativos):
UPDATE band_membership SET joined_at = '2026-04-14 10:00:00' WHERE id = X;
```

---

## 🔧 Configuración avanzada

Las constantes al inicio de `bot.py` son fácilmente modificables:

```python
COOLDOWN_OTHER_BAND_DAYS = 5         # Días para unirse a otra banda
COOLDOWN_SAME_BAND_DAYS = 4          # Días para volver a la misma
DISBANDMENT_COOLDOWN_DAYS = 7        # Días tras desmantelación
WEEKLY_MEMBER_LIMIT = 5              # Máx. miembros nuevos/semana/banda
MAX_LEADERS_PER_BAND = 3             # Máx. jefes activos por banda
MIN_DAYS_AS_MEMBER_FOR_LEADER = 15   # Días seguidos como miembro para ser jefe

LEADER_KEYWORD = "jefe"              # Palabra que activa solicitud de jefe
BYPASS_COOLDOWN_KEYWORDS = {"cooldown", "cd"}  # Palabras que saltan cooldowns
```
