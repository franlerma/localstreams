#!/bin/bash
# Ejecutado por linuxserver/wireguard al iniciar el contenedor (custom-cont-init.d).
# Los engines AceStream usan este contenedor como gateway: sin MASQUERADE por wg0
# sus paquetes salen con la IP privada de Docker y Mullvad los descarta.
# No requiere que wg0 exista todavia (iptables acepta interfaces futuras).
iptables -t nat -C POSTROUTING -o wg0 -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -o wg0 -j MASQUERADE
echo "[mullvad-nat] MASQUERADE por wg0 aplicado"
