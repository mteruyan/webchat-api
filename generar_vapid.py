"""Genera las llaves VAPID para los avisos push. Se corre UNA sola vez.

    python3 generar_vapid.py

Imprime la llave privada (va a Secret Manager, NO se comparte ni se sube a git) y la pública
(no es secreta; el servidor la deriva sola, se muestra solo por referencia).
"""
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

k = ec.generate_private_key(ec.SECP256R1())
b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
print("PRIVADA:", b(k.private_numbers().private_value.to_bytes(32, "big")))
print("PUBLICA:", b(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)))
