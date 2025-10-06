# AI News Recolector

AI News Recolector es un servicio que rastrea fuentes RSS de inteligencia artificial, elimina duplicados con ayuda de modelos LLM y distribuye resúmenes a través de **webhooks** y **Telegram**. Ideal para crear newsletters o canales de alertas automatizados.

## Características
- Monitorea múltiples feeds RSS/Atom de tecnología y IA.
- Limpieza de HTML y normalización de contenido.
- Detección de duplicados usando OpenRouter/OpenAI (configurable).
- Persistencia ligera en SQLite para evitar reenvíos.
- Generación opcional de imágenes descriptivas vía ImgBB.
- Envío de resúmenes enriquecidos a Telegram y a un webhook HTTP.
- Programación mediante `JobQueue` de `python-telegram-bot`.

## Arquitectura
```
main.py         # Servicio principal asíncrono
requirements.txt
Dockerfile      # Imagen ligera (python:3.11-slim)
test2.py        # Utilidad CLI para generar imágenes (usa OpenAI Image API)
```

## Requisitos previos
- Python 3.11+
- Claves para los servicios que desee activar (Telegram, OpenRouter/OpenAI, ImgBB).

## Variables de entorno
Cree un archivo `.env` a partir de `.env.example`:

| Variable | Descripción |
|----------|-------------|
| `WEBHOOK_URL` | Endpoint HTTP que recibirá las noticias procesadas. |
| `WEBHOOK_SECRET` | Token opcional para firmar las peticiones. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Datos del bot/canal destino. |
| `OPENROUTER_API_KEY` | Clave OpenRouter para deduplicación mediante LLM. |
| `OPENAI_API_KEY` | Clave para generación de imágenes/textos (opcional). |
| `IMGBB_API_KEY` | Clave para subir imágenes base64 a ImgBB (opcional). |
| `CHECK_INTERVAL_MINUTES` | Frecuencia de chequeo (por defecto 15 min). |

El servicio funcionará aún sin ImgBB ni imagen, pero omitirá ese paso.

## Instalación local
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

## Uso con Docker
```bash
docker build -t ainews-recolector .
docker run --rm \
  -e WEBHOOK_URL=... \
  -e TELEGRAM_BOT_TOKEN=... \
  -e TELEGRAM_CHAT_ID=... \
  ainews-recolector
```
Monte un volumen para `news_log.db` si desea persistencia más allá del contenedor.

## Flujo resumido
1. El job programado consulta los feeds definidos en `NEWS_URLS`.
2. El contenido se limpia y se verifica contra la base SQLite.
3. Se ejecuta un check de duplicados utilizando LLMs (opcional).
4. Se generan resúmenes y, si procede, imágenes.
5. Se envían mensajes formateados al webhook y a Telegram.

## Roadmap
- Panel web para editar fuentes dinámicamente.
- Integración con bases vectoriales para clustering de noticias.
- Cache distribuida (Redis) para despliegues multiinstancia.
- Exportación automática a Notion/Google Sheets.

## Licencia
Distribución bajo licencia **MIT**.
