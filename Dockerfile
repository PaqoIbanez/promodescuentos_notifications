FROM python:3.11-slim

# Install minimal system dependencies (if any are needed for requests/bs4, usually none for slim)
# requests and bs4 on python-slim usually work out of the box or might need gcc for some wheels, but pure python usually fine.
# We'll keep apt-get update just in case we need to add something later, but remove the heavy stuff.
RUN apt-get update && apt-get install -y \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Chrome installation removed

# Configurar el directorio de trabajo
WORKDIR /app

# Crear directorio para logs y archivos de depuración
RUN mkdir -p /app/debug

# Copiar los archivos de requisitos
COPY requirements.txt .

# Instalar dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código fuente
COPY . .

# Exponer el puerto para el health check
EXPOSE 10000

# Comando para ejecutar la aplicación
CMD ["python", "scrape_promodescuentos.py"] 