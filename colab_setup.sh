#!/usr/bin/env bash
# Installation + lancement des services pour Google Colab.
# Usage : bash colab_setup.sh
# Prérequis : un fichier .env rempli dans le dossier courant.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "❌ .env introuvable (copie .env.example en .env et remplis-le)"; exit 1
fi
set -a; source .env; set +a

: "${TELEGRAM_API_ID:?TELEGRAM_API_ID manquant dans .env}"
: "${TELEGRAM_API_HASH:?TELEGRAM_API_HASH manquant dans .env}"
ARIA2_SECRET="${ARIA2_SECRET:-rssaria2}"

echo "📦 Dépendances système..."
apt-get -qq update
apt-get -qq install -y aria2 curl ffmpeg

echo "🐍 Dépendances Python..."
pip install -q -r requirements.txt

# --- telegram-bot-api (compilation ~10-20 min la 1re fois) ---
BIN_DIR="${BIN_DIR:-/content/bin}"
mkdir -p "$BIN_DIR"
if [ ! -x "$BIN_DIR/telegram-bot-api" ]; then
  echo "🔨 Compilation de telegram-bot-api (une seule fois)..."
  apt-get -qq install -y make git zlib1g-dev libssl-dev gperf cmake g++
  rm -rf /tmp/tgbotapi
  git clone --recursive -q https://github.com/tdlib/telegram-bot-api.git /tmp/tgbotapi
  mkdir -p /tmp/tgbotapi/build && cd /tmp/tgbotapi/build
  cmake -DCMAKE_BUILD_TYPE=Release .. > /dev/null
  cmake --build . --target install -j"$(nproc)" > /dev/null
  cp /tmp/tgbotapi/build/telegram-bot-api "$BIN_DIR/" 2>/dev/null \
    || cp "$(command -v telegram-bot-api)" "$BIN_DIR/"
  cd - > /dev/null
fi

# --- Lancement des services ---
pkill -f telegram-bot-api || true
pkill -f "aria2c --enable-rpc" || true
sleep 1

mkdir -p "${DOWNLOAD_DIR:-downloads}" /content/tgbotapi-data

echo "🚀 Lancement de aria2 (RPC :6800)..."
aria2c --enable-rpc --rpc-listen-port=6800 \
  --rpc-secret="$ARIA2_SECRET" \
  --max-connection-per-server=16 --split=16 \
  --continue=true --daemon=true

echo "🚀 Lancement de telegram-bot-api (:8081)..."
nohup "$BIN_DIR/telegram-bot-api" \
  --api-id="$TELEGRAM_API_ID" --api-hash="$TELEGRAM_API_HASH" \
  --local --http-ip-address=127.0.0.1 --http-port=8081 \
  --dir=/content/tgbotapi-data > /content/tgbotapi.log 2>&1 &

sleep 3
echo "✅ Services prêts. Lance maintenant : python bot.py"
