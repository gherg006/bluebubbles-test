#!/usr/bin/env bash
# Create a private CA and a certificate valid for the BlueBubbles LAN IP.
# Run once as root: sudo bash provision_tls.sh 192.168.0.150
set -euo pipefail

server_ip="${1:-192.168.0.150}"
if [[ ! "$server_ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    echo "Pass a valid IPv4 address as the only argument." >&2
    exit 2
fi

service_user="${BLUEBUBBLES_SERVICE_USER:-zmacleod}"
base_dir="/etc/bluebubbles"
ca_dir="$base_dir/ca"
tls_dir="$base_dir/tls"
root_key="$ca_dir/root-ca-key.pem"
root_cert="$ca_dir/root-ca.pem"
server_key="$tls_dir/server-key.pem"
server_csr="$tls_dir/server.csr"
server_cert="$tls_dir/server-cert.pem"
extensions="$tls_dir/server-extensions.cnf"

install -d -m 700 "$ca_dir" "$tls_dir"

if [[ ! -f "$root_key" || ! -f "$root_cert" ]]; then
    openssl genrsa -out "$root_key" 4096
    openssl req -x509 -new -sha256 -days 3650 -key "$root_key" \
        -out "$root_cert" -subj "/CN=BlueBubbles LAN Root CA"
fi

openssl genrsa -out "$server_key" 2048
openssl req -new -key "$server_key" -out "$server_csr" \
    -subj "/CN=BlueBubbles LAN Server"
printf 'subjectAltName=IP:%s\nextendedKeyUsage=serverAuth\nkeyUsage=digitalSignature,keyEncipherment\n' \
    "$server_ip" > "$extensions"
openssl x509 -req -sha256 -days 825 -in "$server_csr" -CA "$root_cert" \
    -CAkey "$root_key" -CAcreateserial -out "$server_cert" -extfile "$extensions"

chown root:root "$root_key"
chmod 600 "$root_key"
chown root:"$service_user" "$server_key" "$server_cert"
chmod 640 "$server_key"
chmod 644 "$server_cert" "$root_cert"
rm -f "$server_csr" "$extensions"

echo "TLS certificate installed at $server_cert"
echo "Public root CA is $root_cert"
