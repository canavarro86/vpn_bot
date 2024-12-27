#!/bin/bash

CLIENT_NAME=$1
OUTPUT_DIR="/etc/openvpn/clients_configs"
EASYRSA_DIR="/etc/openvpn/server/easy-rsa"
TEMPLATE="/etc/openvpn/client-template.ovpn"

if [ -z "$CLIENT_NAME" ]; then
    echo "Usage: $0 <client_name>"
    exit 1
fi

# Генерация клиентских сертификатов и ключей, если их нет
if [ ! -f "$EASYRSA_DIR/pki/issued/$CLIENT_NAME.crt" ] || [ ! -f "$EASYRSA_DIR/pki/private/$CLIENT_NAME.key" ]; then
    echo "Клиентские сертификаты и ключи отсутствуют. Генерация новых..."
    cd $EASYRSA_DIR
    ./easyrsa build-client-full $CLIENT_NAME nopass
    if [ $? -ne 0 ]; then
        echo "Ошибка при генерации сертификатов клиента."
        exit 1
    fi
    echo "Сертификаты и ключи для клиента $CLIENT_NAME успешно сгенерированы."
fi

# Проверка наличия необходимых файлов
if [ ! -f "$EASYRSA_DIR/pki/ca.crt" ] || [ ! -f "$EASYRSA_DIR/pki/issued/$CLIENT_NAME.crt" ] || [ ! -f "$EASYRSA_DIR/pki/private/$CLIENT_NAME.key" ] || [ ! -f "/etc/openvpn/ta.key" ]; then
    echo "Ошибка: Отсутствуют необходимые файлы (CA, client certificates, или ta.key)."
    exit 1
fi

# Чтение содержимого сертификатов и ключей
CA_CERT=$(cat $EASYRSA_DIR/pki/ca.crt)
CLIENT_CERT=$(cat $EASYRSA_DIR/pki/issued/$CLIENT_NAME.crt)
CLIENT_KEY=$(cat $EASYRSA_DIR/pki/private/$CLIENT_NAME.key)
TLS_AUTH=$(cat /etc/openvpn/ta.key)

# Заменяем плейсхолдеры в шаблоне
CONFIG=$(cat $TEMPLATE)
CONFIG=${CONFIG//"{ca_cert}"/"$CA_CERT"}
CONFIG=${CONFIG//"{client_cert}"/"$CLIENT_CERT"}
CONFIG=${CONFIG//"{client_key}"/"$CLIENT_KEY"}
CONFIG=${CONFIG//"{tls_auth}"/"$TLS_AUTH"}

# Сохраняем конфигурационный файл
OUTPUT_FILE="$OUTPUT_DIR/$CLIENT_NAME.ovpn"
mkdir -p $OUTPUT_DIR
echo "$CONFIG" > $OUTPUT_FILE

echo "Конфигурационный файл создан: $OUTPUT_FILE"
