#!/usr/bin/env bash

USERNAME="$1"
if [ -z "$USERNAME" ]; then
  echo "Usage: $0 <username>"
  exit 1
fi

echo "Generating certificates for $USERNAME..."

# Пути к файлам
EASYRSA_DIR="/etc/openvpn/server/easy-rsa"
CA="$EASYRSA_DIR/pki/ca.crt"
CERT="$EASYRSA_DIR/pki/issued/$USERNAME.crt"
KEY="$EASYRSA_DIR/pki/private/$USERNAME.key"
REQ="$EASYRSA_DIR/pki/reqs/$USERNAME.req"
TA="/etc/openvpn/server/ta.key"
CONFIG_DIR="/etc/openvpn/client"
OUTPUT="$CONFIG_DIR/$USERNAME.ovpn"

# Проверка существования сертификатов
if [ -f "$KEY" ] || [ -f "$CERT" ]; then
  echo "Certificates for $USERNAME already exist. Removing existing certificates..."
  cd "$EASYRSA_DIR" || exit 1
  echo yes | ./easyrsa revoke "$USERNAME"
  ./easyrsa gen-crl
  rm -f "$KEY" "$CERT" "$REQ"
  echo "Existing certificates removed."
fi

# Генерация сертификатов
cd "$EASYRSA_DIR" || exit 1
echo yes | ./easyrsa build-client-full "$USERNAME" nopass
if [ $? -ne 0 ]; then
    echo "Error: Failed to generate certificates for $USERNAME"
    exit 1
fi

mkdir -p "$CONFIG_DIR"

# Создаем конфигурационный файл
cat > "$OUTPUT" <<EOF
client
dev tun
proto udp
remote 45.76.38.87 1194
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
auth SHA512
ignore-unknown-option block-outside-dns
verb 3

<ca>
$(cat "$CA")
</ca>
<cert>
$(sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' "$CERT")
</cert>
<key>
$(cat "$KEY")
</key>
<tls-auth>
$(cat "$TA")
</tls-auth>
key-direction 1
EOF

echo "Configuration file created at $OUTPUT"
exit 0
