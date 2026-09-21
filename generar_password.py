"""
generar_password.py — utilidad de línea de comandos para setear la contraseña
inicial (o resetear) de un asesor, ya que bcrypt no se puede calcular en SQL puro.

Uso:
    python3 generar_password.py virginia.capia "una-clave-fuerte"

Imprime el UPDATE listo para correr contra la base de datos webchat.
No se conecta a la base de datos él mismo, a propósito — así puedes revisar
el SQL antes de ejecutarlo.
"""

import sys
import bcrypt


def main():
    if len(sys.argv) != 3:
        print("Uso: python3 generar_password.py <usuario> <password>")
        sys.exit(1)

    usuario, password = sys.argv[1], sys.argv[2]
    hash_ = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    print("\nUPDATE asesores SET password_hash = '{}' WHERE usuario = '{}';\n".format(hash_, usuario))


if __name__ == "__main__":
    main()
