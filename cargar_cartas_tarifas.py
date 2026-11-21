#!/usr/bin/env python3
"""cargar_cartas_tarifas.py — Registra en el webchat las cartas de tarifas (un PDF por cliente) que ya subiste al
bucket privado. Se corre en Cloud Shell; solo necesita el Excel. No toca la base: genera un .sql.

Antes: sube los PDF, con su nombre original, a gs://sanitas-webchat-cartas/cartas-tarifas/<año>/ (desde la consola
de Cloud Storage; se puede hacer por partes). Ese bucket es privado: los PDF solo se abren desde el webchat.

  1) Lee el Excel (columna del N° de documento + columna del nombre del PDF).
  2) Lista los PDF que hay en el bucket y busca el de cada fila (sin distinguir mayúsculas/minúsculas ni la forma
     de las tildes).
  3) Genera cartas_tarifas_<año>.sql (idempotente, en ASCII) y errores_cartas_<año>.csv con las filas que no se
     pudieron cargar (p. ej. el PDF aún no está en el bucket: súbelo y vuelve a correr el script).

Uso:
  pip install --user openpyxl
  python3 cargar_cartas_tarifas.py cartas.xlsx
  python3 cargar_cartas_tarifas.py cartas.xlsx --anio 2027 --col-doc "NRO DOCUMENTO" --col-archivo "ARCHIVO"

Sin --col-doc / --col-archivo se detectan solas por el encabezado (documento/DNI/DOI y archivo/PDF/carta).
"""

import argparse
import csv
import os
import re
import subprocess
import sys
import unicodedata

BUCKET_POR_DEFECTO = "sanitas-webchat-cartas"   # privado
FILAS_POR_INSERT = 500


def normalizar_doc(valor) -> str:
    """Igual que cartas_tarifas.normalizar_doc: letras y dígitos en mayúsculas; un número de menos de 8 dígitos
    se completa con ceros a la izquierda (Excel le quita el 0 inicial a los DNI)."""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    limpio = re.sub(r"[^A-Za-z0-9]", "", str(valor if valor is not None else "")).upper()
    if limpio.isdigit() and len(limpio) < 8:
        limpio = limpio.zfill(8)
    return limpio if re.fullmatch(r"[A-Z0-9]{6,15}", limpio) else ""


def literal_sql(texto: str) -> str:
    """Literal SQL en ASCII: si hay tildes/ñ usa U&'...' con escapes Unicode."""
    if all(ord(c) < 128 for c in texto):
        return "'" + texto.replace("'", "''") + "'"
    partes = []
    for c in texto:
        o = ord(c)
        if c == "'":
            partes.append("''")
        elif c == "\\":
            partes.append("\\\\")
        elif o < 128:
            partes.append(c)
        elif o <= 0xFFFF:
            partes.append("\\%04X" % o)
        else:
            partes.append("\\+%06X" % o)
    return "U&'" + "".join(partes) + "'"


def clave_nombre(nombre: str) -> str:
    """Para comparar nombres: sin mayúsculas, con los espacios repetidos como uno solo y con las tildes en la misma
    forma (Windows/Mac/Excel las guardan distinto)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", nombre)).strip().lower()


_PATRON_CODIGO = re.compile(r"(\d{5,})\s*([A-Za-z0-9]+)\s*\+\s*(\d+)(?:\.pdf)?\s*$", re.IGNORECASE)


def codigo_carta(nombre: str) -> str | None:
    """Parte final del nombre que identifica la carta sin depender de cómo se escribió el nombre del cliente:
    "+D'UGARD LIZA KATHE 70007528 47497208+374.pdf" -> "70007528|47497208|374" (contrato | documento | correlativo).
    Sirve cuando el nombre trae apóstrofes, puntos o espacios distintos en el Excel y en el archivo."""
    m = _PATRON_CODIGO.search(unicodedata.normalize("NFC", nombre or ""))
    return "|".join(g.upper() for g in m.groups()) if m else None


def detectar_columna(encabezados: list, pedida: str | None, patrones: tuple, que: str) -> int:
    normal = [str(h or "").strip().lower() for h in encabezados]
    if pedida:
        p = pedida.strip().lower()
        if p in normal:
            return normal.index(p)
        if p.isdigit() and 1 <= int(p) <= len(normal):
            return int(p) - 1
        sys.exit(f"❌ No encuentro la columna '{pedida}'. Encabezados: {encabezados}")
    for patron in patrones:
        for i, h in enumerate(normal):
            if re.search(patron, h):
                return i
    sys.exit(f"❌ No pude detectar la columna de {que}. Encabezados: {encabezados}. "
             f"Indícala con --col-{'doc' if que == 'documento' else 'archivo'}")


def pdfs_del_bucket(bucket: str, prefijo: str) -> dict:
    """{clave_nombre: nombre real} de los PDF que hay en gs://bucket/prefijo/ (también {"#codigo": nombre real},
    ver codigo_carta; si dos PDF comparten código, ese código no se usa)."""
    destino = f"gs://{bucket}/{prefijo}/"
    try:
        salida = subprocess.run(["gcloud", "storage", "ls", destino], capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        sys.exit("❌ No encuentro el comando gcloud (corre el script en Cloud Shell)")
    except subprocess.CalledProcessError as e:
        sys.exit(f"❌ No pude listar {destino}: {e.stderr.strip()[:300]}")
    indice = {}
    for linea in salida.splitlines():
        linea = linea.strip()
        if linea.startswith(destino) and linea.lower().endswith(".pdf"):
            nombre = linea[len(destino):]
            if "/" not in nombre:            # solo los de esa carpeta, no subcarpetas
                indice.setdefault(clave_nombre(nombre), nombre)
                codigo = codigo_carta(nombre)
                if codigo:
                    for clave in ("#" + codigo, "~" + codigo.rsplit("|", 1)[0]):   # con y sin correlativo
                        indice[clave] = None if clave in indice else nombre     # None = repetido: no se usa
    return indice


def main():
    ap = argparse.ArgumentParser(description="Registra en el webchat las cartas de tarifas ya subidas al bucket")
    ap.add_argument("excel")
    ap.add_argument("--anio", type=int, default=2027)
    ap.add_argument("--hoja", help="Nombre de la hoja (por defecto, la primera)")
    ap.add_argument("--col-doc", help="Encabezado (o número de columna, desde 1) del N° de documento")
    ap.add_argument("--col-archivo", help="Encabezado (o número de columna, desde 1) del nombre del PDF")
    ap.add_argument("--bucket", default=BUCKET_POR_DEFECTO)
    ap.add_argument("--sin-correlativo", action="store_true",
                    help="Si no hay coincidencia exacta, emparejar por contrato + documento aunque el número final "
                         "(+NNN) sea distinto, solo cuando hay UN solo PDF y UNA sola fila con ese contrato y documento")
    args = ap.parse_args()

    try:
        import openpyxl
    except ImportError:
        sys.exit("❌ Falta openpyxl: pip install --user openpyxl")

    prefijo = f"cartas-tarifas/{args.anio}"
    indice = pdfs_del_bucket(args.bucket, prefijo)
    total_pdfs = len({v for v in indice.values() if v})
    print(f"☁️  {total_pdfs} PDF en gs://{args.bucket}/{prefijo}/")

    libro = openpyxl.load_workbook(args.excel, read_only=True, data_only=True)
    hoja = libro[args.hoja] if args.hoja else libro.worksheets[0]
    filas = hoja.iter_rows(values_only=True)
    encabezados = list(next(filas, []) or [])
    col_doc = detectar_columna(encabezados, args.col_doc,
                               (r"documento", r"\bdni\b", r"\bdoi\b", r"nro|n°|num"), "documento")
    col_arc = detectar_columna(encabezados, args.col_archivo,
                               (r"archivo", r"pdf", r"carta", r"nombre"), "archivo")
    if col_doc == col_arc:
        sys.exit("❌ La columna de documento y la de archivo salieron iguales: indícalas con --col-doc y --col-archivo")
    print(f"📑 Hoja '{hoja.title}': documento = '{encabezados[col_doc]}', archivo = '{encabezados[col_arc]}'")

    filas = list(filas)
    # Cuántas filas del Excel tienen cada contrato + documento (para --sin-correlativo: solo si es una sola)
    filas_por_contrato = {}
    for fila in filas:
        crudo = fila[col_arc] if fila and col_arc < len(fila) else None
        codigo = codigo_carta(str(crudo or ""))
        if codigo:
            clave = codigo.rsplit("|", 1)[0]
            filas_por_contrato[clave] = filas_por_contrato.get(clave, 0) + 1

    registros, errores, vistos, por_contrato = [], [], set(), []
    for n, fila in enumerate(filas, start=2):
        fila = list(fila or [])
        crudo_doc = fila[col_doc] if col_doc < len(fila) else None
        crudo_arc = fila[col_arc] if col_arc < len(fila) else None
        if crudo_doc in (None, "") and crudo_arc in (None, ""):
            continue                       # fila vacía
        archivo = os.path.basename(str(crudo_arc or "").strip().replace("\\", "/"))
        doc = normalizar_doc(crudo_doc)
        if not doc:
            errores.append((n, crudo_doc, archivo, "Documento inválido"))
            continue
        if not archivo:
            errores.append((n, crudo_doc, archivo, "Sin nombre de archivo"))
            continue
        real = indice.get(clave_nombre(archivo)) or indice.get(clave_nombre(archivo + ".pdf"))
        codigo = codigo_carta(archivo)
        if not real and codigo:
            real = indice.get("#" + codigo)   # mismo contrato, documento y correlativo
        if not real and codigo and args.sin_correlativo:
            contrato = codigo.rsplit("|", 1)[0]
            if filas_por_contrato.get(contrato) == 1:
                real = indice.get("~" + contrato)
                if real:
                    por_contrato.append((n, crudo_doc, archivo, real))
        if not real:
            errores.append((n, crudo_doc, archivo, "El PDF no está en el bucket"))
            continue
        if (doc, real) in vistos:
            errores.append((n, crudo_doc, archivo, "Repetido (mismo documento y archivo): se carga una vez"))
            continue
        vistos.add((doc, real))
        registros.append((doc, real, f"{prefijo}/{real}"))

    sql_nombre = f"cartas_tarifas_{args.anio}.sql"
    with open(sql_nombre, "w", encoding="ascii") as f:
        f.write(f"-- {sql_nombre}: cartas de tarifas {args.anio} (generado por cargar_cartas_tarifas.py)\n"
                "-- Idempotente. Requiere migracion_cartas_tarifas.sql.\n"
                "BEGIN;\n")
        for i in range(0, len(registros), FILAS_POR_INSERT):
            tramo = registros[i:i + FILAS_POR_INSERT]
            f.write("INSERT INTO cartas_tarifas (anio, documento, archivo, ruta) VALUES\n")
            f.write(",\n".join(f"({args.anio}, {literal_sql(d)}, {literal_sql(a)}, {literal_sql(r)})"
                               for d, a, r in tramo))
            f.write("\nON CONFLICT (anio, documento, archivo) DO UPDATE\n"
                    "   SET texto = CASE WHEN cartas_tarifas.ruta = EXCLUDED.ruta THEN cartas_tarifas.texto END,\n"
                    "       ruta = EXCLUDED.ruta, activo = TRUE;\n")
        f.write("COMMIT;\n"
                f"SELECT count(*) AS cartas, count(DISTINCT documento) AS documentos\n"
                f"  FROM cartas_tarifas WHERE anio = {args.anio} AND activo;\n")

    errores_nombre = f"errores_cartas_{args.anio}.csv"
    with open(errores_nombre, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["fila_excel", "documento", "archivo", "motivo"])
        w.writerows(errores)

    docs = {}
    for d, _a, _r in registros:
        docs[d] = docs.get(d, 0) + 1
    varias = sum(1 for v in docs.values() if v > 1)
    usados = {a for _d, a, _r in registros}
    sobrantes = sorted({v for v in indice.values() if v} - usados)
    sobran = len(sobrantes)
    sobrantes_nombre = f"pdfs_sin_excel_{args.anio}.txt"
    with open(sobrantes_nombre, "w", encoding="utf-8") as f:
        f.write("\n".join(sobrantes) + ("\n" if sobrantes else ""))
    print(f"\n✅ {len(registros)} cartas listas para {len(docs)} documentos"
          + (f" ({varias} documentos con más de una carta)" if varias else ""))
    print(f"⚠️  {len(errores)} filas con problemas -> {errores_nombre}" if errores else "👌 Sin filas con problemas")
    if sobran:
        print(f"ℹ️  {sobran} PDF del bucket no aparecen en el Excel -> {sobrantes_nombre}")
    if por_contrato:
        nombre = f"emparejados_por_contrato_{args.anio}.csv"
        with open(nombre, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["fila_excel", "documento", "archivo_excel", "pdf_usado"])
            w.writerows(por_contrato)
        print(f"🔗 {len(por_contrato)} emparejadas por contrato + documento (correlativo distinto) -> {nombre}")
    print(f"\nSiguiente paso: en psql\n  \\i {os.path.abspath(sql_nombre)}\n")


if __name__ == "__main__":
    main()
