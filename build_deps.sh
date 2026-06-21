#!/bin/bash

# Salir inmediatamente si algún comando falla
set -e

DEPS_DIR="markitdown-deps"

echo "============================================="
echo "🚀 Iniciando empaquetado optimizado para AWS"
echo "============================================="

echo -e "\n🧹 1. Limpiando instalaciones previas..."
rm -rf ./$DEPS_DIR

# Lee las dependencias desde tu archivo requirements.txt
echo -e "\n📦 2. Instalando dependencias (Amazon Linux - Python 3.14)..."
pip install -r requirements.txt -t ./$DEPS_DIR \
    --python-version 3.14 \
    --only-binary=:all:

echo -e "\n✂️ 3. Iniciando proceso de poda (Pruning)..."

# A. Eliminar cachés y archivos compilados de Python (.pyc)
echo "   -> Borrando cachés..."
find ./$DEPS_DIR -type d -name "__pycache__" -exec rm -rf {} +
find ./$DEPS_DIR -type f -name "*.pyc" -delete

# B. Eliminar metadatos de instalación de Pip
echo "   -> Borrando metadatos .dist-info y .egg-info..."
find ./$DEPS_DIR -type d -name "*.dist-info" -exec rm -rf {} +
find ./$DEPS_DIR -type d -name "*.egg-info" -exec rm -rf {} +

# C. Eliminar librerías pre-instaladas nativamente en AWS Lambda
# Esto incluye boto3 y todas sus dependencias subyacentes
echo "   -> Borrando SDKs de AWS redundantes..."
rm -rf ./$DEPS_DIR/boto3*
rm -rf ./$DEPS_DIR/botocore*
rm -rf ./$DEPS_DIR/urllib3*
rm -rf ./$DEPS_DIR/s3transfer*
rm -rf ./$DEPS_DIR/jmespath*

# D. Eliminar carpetas de pruebas unitarias internas de las librerías
echo "   -> Borrando tests internos..."
find ./$DEPS_DIR -type d -name "tests" -exec rm -rf {} +
find ./$DEPS_DIR -type d -name "test" -exec rm -rf {} +

# E. Eliminar archivos fuente de C/Cython que ya fueron compilados en .so
echo "   -> Borrando código fuente C/C++..."
find ./$DEPS_DIR -type f -name "*.c" -delete
find ./$DEPS_DIR -type f -name "*.pyx" -delete
find ./$DEPS_DIR -type f -name "*.pxd" -delete

echo -e "\n✅ Dependencias listas y optimizadas."
echo "📊 Tamaño final de la carpeta $DEPS_DIR:"
du -sh ./$DEPS_DIR

echo "============================================="
echo "   ¡Listo para ejecutar 'sls deploy'!   "
echo "============================================="
