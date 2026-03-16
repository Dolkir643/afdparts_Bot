#!/usr/bin/with-contenv bashio

export BOT_TOKEN=$(bashio::config 'bot_token')
export AFDPARTS_LOGIN=$(bashio::config 'afdparts_login')
export AFDPARTS_PASSWORD=$(bashio::config 'afdparts_password')
export TELEGRAM_ORDER_CHAT_ID=$(bashio::config 'telegram_order_chat_id')

mkdir -p /data
cd /usr/src/app
exec python -u tg_bot.py
