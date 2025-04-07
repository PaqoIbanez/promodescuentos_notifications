#!/usr/bin/env bash
# Instalar Chrome y sus dependencias
apt-get update
apt-get install -y wget gnupg unzip xvfb libxi6 libgconf-2-4

# Instalar Chrome
wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add -
echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list
apt-get update
apt-get install -y google-chrome-stable

# Verificar la instalación de Chrome
google-chrome --version

# Instalar dependencias de Python
pip install -r requirements.txt 