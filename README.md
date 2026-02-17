# 🔥 Promodescuentos — Bot de Alertas Inteligentes

Un bot de Telegram que detecta ofertas virales en [Promodescuentos.com](https://www.promodescuentos.com) **antes de que sean obvias para el público general**. No se limita a revisar temperatura; utiliza un motor de puntuación basado en gravedad, aceleración y patrones históricos para predecir cuáles ofertas llegarán a 500°+ cuando apenas van por 30°.

---

## 📌 ¿Cómo funciona?

El bot ejecuta un ciclo continuo cada 5–12 minutos:

```
Scraping → Almacenamiento → Análisis → ¿Es viral? → Notificación
```

1. **Extrae** las ofertas más recientes de la sección ["Nuevas"](https://www.promodescuentos.com/nuevas)
2. **Guarda** cada oferta y su estado actual (temperatura, tiempo de vida) en la base de datos
3. **Analiza** cada oferta con el Motor de Puntuación Viral
4. **Envía** notificaciones a Telegram solo si la oferta supera el umbral dinámico

La clave no es *ver* qué ya es popular, sino **predecir** qué *será* popular.

---

## 🧠 Motor de Puntuación Viral

### El Problema con la Velocidad Lineal

El enfoque ingenuo es calcular `temperatura / minutos`. Pero esto genera problemas:

| Oferta | Temp | Tiempo | Velocidad | ¿Realmente es buena? |
|---|---|---|---|---|
| A | 3° | 1 min | **3.0** | ❌ Probablemente 2 amigos votando |
| B | 50° | 10 min | **5.0** | ✅ Crecimiento explosivo real |
| C | 100° | 50 min | **2.0** | ⚠️ Normal, ya es tarde |
| D | 30° | 5 min (4 AM) | **6.0** | 🚀 Extraordinaria a esa hora |

La velocidad lineal trata a A y B como comparables. Nuestro motor no.

### La Fórmula: Gravedad Viral

Inspirada en el algoritmo de [Hacker News](https://news.ycombinator.com/), usamos un modelo de **gravedad** donde el tiempo juega en contra:

```
Viral Score = (temperatura - 1) / (horas + 0.1) ^ gravedad
```

- **`gravedad = 1.2`** (configurable) — controla qué tan agresivamente penalizamos el paso del tiempo
- **`0.1`** — offset de ~6 minutos que estabiliza ofertas recién publicadas
- Una oferta con **50° en 10 minutos** → Score ≈ **182**
- La misma con **50° en 5 horas** → Score ≈ **8**

> El Score Viral captura una realidad intuitiva: 50° en 10 minutos es **extraordinario**, mientras que 50° en 5 horas es **mediocre**.

### Señales Adicionales

El Score Viral es solo la base. Se multiplica por dos factores adicionales:

#### 📈 Detección de Aceleración (2ª Derivada)

En cada ciclo, el bot compara la velocidad **actual** de la oferta contra su velocidad en el **snapshot anterior** almacenado en la base de datos.

- Si la velocidad se está **duplicando** → multiplicador `2.0x` (la oferta se está acelerando)
- Si la velocidad no cambia → multiplicador `1.0x`
- Si la velocidad **baja** → multiplicador `0.5x` (perdió tracción)

Esto detecta el patrón clásico de viralidad: 20° a los 10 min, 40° a los 15 min, 80° a los 20 min = **crecimiento exponencial**.

#### 🌙 Traffic Shaping (Hora del Día)

Una oferta que consigue 30° a las 4:00 AM (cuando casi nadie navega) es **mucho más impresionante** que una que lo hace al mediodía. El sistema normaliza esto con multiplicadores por horario (zona horaria de Ciudad de México):

| Horario | Multiplicador | Razón |
|---|---|---|
| 00:00 – 07:00 | `1.5x` | Tráfico mínimo, cada voto vale más |
| 07:00 – 09:00 | `1.2x` | Usuarios despertando |
| 09:00 – 22:00 | `1.0x` | Hora pico, estándar |
| 22:00 – 00:00 | `1.3x` | Tráfico cayendo |

### Score Final y Decisión

```
Score Final = Viral Score × Multiplicador de Tráfico × Aceleración
```

Si el **Score Final ≥ umbral** (por defecto `50.0`, auto-ajustable), la oferta se considera **viral** y se envía la notificación.

### 🛡️ Filtro Anti-Ruido

Antes de calcular cualquier score, el sistema verifica:

- ❌ **Temperatura < 15°** → Se ignora completamente (evita falsos positivos por 2-3 votos de amigos)
- ❌ **Oferta expirada** → Se descarta (el texto contiene "Expiró")

Solo ofertas con un mínimo de "capital semilla" (≥ 15°) entran al análisis.

---

## 🔥 Sistema de Ratings (Iconos de Fuego)

Cada oferta viral recibe un rating de 1 a 4 fuegos basado en su Score Final:

| Score Final | Rating | Significado |
|---|---|---|
| ≥ 500 | 🔥🔥🔥🔥 | Oferta legendaria, probablemente romperá 1000° |
| ≥ 200 | 🔥🔥🔥 | Muy caliente, alto potencial viral |
| ≥ 100 | 🔥🔥 | Caliente, crecimiento sólido |
| ≥ umbral | 🔥 | Detectada temprano, vale la pena monitorear |

### Notificaciones Progresivas

El bot **solo notifica cuando el rating sube**. Si una oferta ya fue notificada como 🔥🔥 y ahora es 🔥🔥🔥, se envía una nueva notificación con el rating actualizado. Pero si mantiene el mismo rating, no se re-notifica. Esto evita el spam.

---

## 🎯 AutoTuner — Aprendizaje Automático de Umbrales

El sistema no usa umbrales fijos. Se optimiza solo basándose en los datos históricos de tu propia base de datos.

### ¿Qué Optimiza?

En cada inicio de la aplicación (y periódicamente), el **AutoTuner** ejecuta análisis SQL sobre el historial completo de ofertas:

#### 1. Umbral Viral Dinámico (`viral_threshold`)

Busca el **percentil 20** de los scores virales de las ofertas que eventualmente llegaron a **200°+**. En otras palabras: *"¿Cuál es el score mínimo que tuvieron el 80% de las ofertas ganadoras?"*

Si la respuesta es `42.5`, entonces el umbral se ajusta a `42.5`. Esto significa que el bot capturará al 80% de las ofertas que llegarán a ser exitosas.

#### 2. Análisis de "Golden Ratio" (Ratio de Oro)

El AutoTuner ejecuta consultas predictivas en múltiples puntos de control:

```
🎯 A los 15 min: Si tiene ≥ 20°, ¿qué % llega a 200°?  → 37.5% (6/16 deals)
🎯 A los 15 min: Si tiene ≥ 30°, ¿qué % llega a 500°?  → 20.0% (2/10 deals)
🎯 A los 30 min: Si tiene ≥ 30°, ¿qué % llega a 200°?  → 34.8% (8/23 deals)
🎯 A los 30 min: Si tiene ≥ 50°, ¿qué % llega a 500°?  → 15.4% (2/13 deals)
🎯 A la 1 hora: Si tiene ≥ 50°, ¿qué % llega a 200°?   → 30.8% (8/26 deals)
```

> Estos son datos reales de la base de datos. A medida que se acumulan más datos históricos, las predicciones serán más precisas y el AutoTuner refinará los umbrales automáticamente.

#### 3. Velocidades Legacy (Retrocompatibilidad)

También calcula percentiles de velocidad lineal para mantener compatibilidad con las reglas originales.

### El Ciclo de Mejora

```
Recopilar datos → Analizar patrones → Ajustar umbrales → Detectar mejor → Recopilar más datos
```

Con cada iteración, el bot se vuelve más inteligente. Después de algunas semanas de operación, el `viral_threshold` se habrá calibrado automáticamente a los patrones específicos de Promodescuentos.

---

## 📱 Notificaciones de Telegram

### ¿Cuándo se envía una notificación?

Se envía **si y solo si** se cumplen **todas** estas condiciones:

1. ✅ La oferta **no ha expirado**
2. ✅ La temperatura es **≥ 15°** (filtro anti-ruido)
3. ✅ El **Score Final** supera el umbral dinámico (default `50.0`)
4. ✅ El **rating actual** es **mayor** que el máximo rating previamente registrado para esa oferta

### Contenido de la notificación

Cada mensaje incluye:
- **Título** de la oferta
- **Temperatura** actual con iconos de fuego (🔥 a 🔥🔥🔥🔥)
- **Tiempo** desde publicación
- **Comercio** (Amazon, Liverpool, Walmart, etc.)
- **Precio** y **descuento** (si están disponibles)
- **Código de cupón** (si existe)
- **Descripción** breve
- **Botón "Ver Oferta"** con enlace directo

### Flujo de suscripción

Los usuarios interactúan con el bot por comandos:
- `/start` o `/subscribe` — Suscribirse a alertas
- `/stop` o `/unsubscribe` — Cancelar suscripción

---

## 🗄️ ¿Qué datos se almacenan?

| Tabla | Propósito |
|---|---|
| `deals` | Cada oferta única (URL, título, comercio, imagen, rating máximo visto) |
| `deal_history` | Snapshots temporales: temperatura, velocidad, score viral, horas desde publicación |
| `subscribers` | Chat IDs de Telegram suscritos |
| `system_config` | Umbrales dinámicos del AutoTuner (clave-valor) |

El historial de snapshots (`deal_history`) es el corazón del sistema predictivo. Cada vez que el bot escanea, guarda el estado actual de cada oferta, creando una serie temporal que alimenta tanto la aceleración en tiempo real como el análisis Golden Ratio.

---

## ⚙️ Configuración del Sistema

Todos los parámetros son ajustables vía `system_config` en la base de datos:

| Clave | Default | Descripción |
|---|---|---|
| `viral_threshold` | `50.0` | Score mínimo para considerar una oferta viral |
| `min_seed_temp` | `15.0` | Temperatura mínima para entrar al análisis |
| `gravity` | `1.2` | Factor de penalización temporal (mayor = más estricto) |
| `score_tier_4` | `500.0` | Score para 🔥🔥🔥🔥 |
| `score_tier_3` | `200.0` | Score para 🔥🔥🔥 |
| `score_tier_2` | `100.0` | Score para 🔥🔥 |

Estos valores son **semillas iniciales**. El AutoTuner los ajustará automáticamente con el tiempo.

---

## 🏗️ Stack Tecnológico

| Componente | Tecnología |
|---|---|
| Framework | FastAPI (async) |
| Base de datos | PostgreSQL + asyncpg |
| ORM | SQLAlchemy 2.0 (async) |
| HTTP Client | httpx (connection pooling) |
| Scraping | BeautifulSoup4 |
| Contenedor | Docker (python:3.13-slim) |
| Notificaciones | Telegram Bot API |

### Arquitectura

```
app/
├── core/           # Configuración y logging
├── db/             # Motor async de SQLAlchemy
├── models/         # Modelos declarativos (Deal, DealHistory, Subscriber, SystemConfig)
├── repositories/   # Acceso a datos (patrón Repository)
├── services/       # Lógica de negocio
│   ├── analyzer.py   # Motor de Puntuación Viral
│   ├── optimizer.py   # AutoTuner (aprendizaje de umbrales)
│   ├── scraper.py     # Extracción de datos
│   ├── deals.py       # Unit of Work (transacciones atómicas)
│   └── telegram.py    # Notificaciones (concurrencia controlada)
├── dependencies.py # Inyección de dependencias (FastAPI Depends)
└── main.py         # Orquestación, lifespan, scraper loop
```

---

## 🚀 Ejecución

### Variables de Entorno

```env
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname
TELEGRAM_BOT_TOKEN=tu-token-de-bot
APP_BASE_URL=https://tu-dominio.com
ADMIN_CHAT_IDS=123456789,987654321
```

### Docker

```bash
docker build -t promodescuentos-bot .
docker run -d --env-file .env promodescuentos-bot
```

### Local

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 10000
```
