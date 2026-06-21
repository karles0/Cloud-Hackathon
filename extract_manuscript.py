import os
import re
import json
import urllib.parse

import boto3

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

QUEUE_URL  = os.environ["QUEUE_URL"]
TABLE_NAME = os.environ["TABLE_NAME"]

s3    = boto3.client("s3")
sqs   = boto3.client("sqs")
tabla = boto3.resource("dynamodb").Table(TABLE_NAME)

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)

# Sub-rutas que el PDF-a-MD introduce después del DOI real: /v2/response1, /v1/review, etc.
_DOI_GARBAGE_RE = re.compile(
    r"/v\d+/(response|review|comment|reply|correction)\d*$", re.I
)

ENCABEZADO_REF = re.compile(
    r"^[#\s]*\b(references?|bibliograf[íi]a|works\s+cited|literatura\s+citada|obras\s+citadas)\s*$",
    re.I)

ENCABEZADO_MD = re.compile(r"^#{1,6}\s+\S")
PAGINA_MD     = re.compile(r"^#{1,6}\s+page\s+\d+\s*$", re.I)

MARCADOR_ENTRADA = re.compile(r"^\s*(\[?\d{1,3}\]?[.\)]|[-*\u2022])\s+")

_SEPARADOR_APA = re.compile(
    r"(?:"
    r"\b20\d\d[a-e]?\."
    r"|\.html\."
    r"|\.md\."
    r"|/\."
    r")\s+"
    r"(?=[A-Z]\.\s"
    r"|[A-Z]{2}[@\-\w]*[.,]"
    r")"
)

# FIX contexto: cubrir LaTeX $...$, $$...$$, \(...\) y \[...\] (estilo Pandoc/MathJax).
# Los conversores PDF->MD (pandoc, mathpix) usan \(...\) en vez de $...$.
_LATEX_RE = re.compile(
    r"\$\$.*?\$\$"                        # display $$...$$
    r"|\$(?:[^$\n]|\\[\s\S]){1,300}?\$"  # inline $...$
    r"|\\\(.*?\\\)"                        # Pandoc inline \(...\)
    r"|\\\[.*?\\\]",                       # Pandoc display \[...\]
    re.DOTALL,
)

# Colas de LaTeX sin apertura (fragmento empieza en medio de una expresión).
_LATEX_COLA_RE = re.compile(r"(?:^|(?<=\s))\\+\w[\w{}^_\\,!|]*\$", re.MULTILINE)

# Lineas de ruido estructural (no prosa citante).
_RUIDO_LN_RE = re.compile(
    r"^#{1,6}\s"
    r"|^\s*>\s"
    r"|^\s*\|.*\|\s*$"
    r"|^```",
    re.IGNORECASE,
)

# Lineas que son SOLO el nombre de un entorno, sin prosa.
_SOLO_ENTORNO_RE = re.compile(
    r"^\s*\*\*(?:Fig|Theorem|Lemma|Corollary|Definition)\s*\d*\.?\*\*\s*$",
    re.IGNORECASE,
)


def lambda_handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key    = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        tema, manuscript_id = _parse_key(key)
        md = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8", "ignore")

        referencias = _extraer_referencias(md)
        print(f"[{manuscript_id}] tema={tema} referencias={len(referencias)}")

        _registrar_total(manuscript_id, tema, len(referencias))
        _guardar_refs_pendientes(manuscript_id, tema, referencias)
        _enviar_a_cola(manuscript_id, tema, referencias)

    return {"ok": True}


def _parse_key(key):
    partes = key.split("/")
    tema          = partes[-2] if len(partes) >= 2 else "General"
    manuscript_id = os.path.splitext(partes[-1])[0]
    return tema, manuscript_id


def _limpiar_doi(doi):
    """Limpia trailing garbage que el regex DOI_RE puede capturar del PDF.

    - Quita puntuacion final (.,);:)
    - Quita sufijos de version arXiv: 10.48550/arXiv.YYYY.NNNNNvN → sin vN
      (solo para arXiv; en preprints.org el .v1 ES parte del DOI)
    - Quita sub-rutas /v2/response1, /v1/review, etc. que no son DOI real
    - Quita slash final residual
    """
    doi = doi.rstrip(".,);:")
    # Sub-rutas de revisión/respuesta (PDF insertó la URL completa, no solo el DOI)
    doi = _DOI_GARBAGE_RE.sub("", doi)
    # Sufijo de version en DOIs arXiv (10.48550/arXiv.YYMM.NNNNN)
    doi = re.sub(r"(10\.48550/arXiv\.\d{4}\.\d{4,5})v\d+$", r"\1", doi, flags=re.I)
    doi = doi.rstrip("/")
    return doi if doi else None


def _extraer_referencias(md):
    lineas = md.splitlines()

    # Buscar el ULTIMO encabezado de referencias (no el primero).
    inicio = None
    for idx, ln in enumerate(lineas):
        if ENCABEZADO_REF.match(ln):
            inicio = idx

    if inicio is None:
        return _fallback_dois(md)

    cuerpo = "\n".join(lineas[:inicio])

    refs_lineas = []
    for ln in lineas[inicio + 1:]:
        if ENCABEZADO_MD.match(ln) and not PAGINA_MD.match(ln):
            break
        if re.match(r'^---$', ln.strip()):
            refs_lineas.append('')
            continue
        if PAGINA_MD.match(ln):
            refs_lineas.append('')
            continue
        if re.match(r'^\d+\s*$', ln.strip()):
            refs_lineas.append('')
            continue
        refs_lineas.append(ln)

    entradas = _separar_entradas(refs_lineas)

    referencias = []
    for i, texto in enumerate(entradas, start=1):
        m   = DOI_RE.search(texto)
        doi = _limpiar_doi(m.group(0)) if m else None
        contexto = _buscar_contexto(cuerpo, i)
        referencias.append({
            "refId":    f"{i:04d}",
            "doi":      doi,
            "citaCruda": texto[:1000],
            "contexto": (contexto or "")[:1500],
        })
    return referencias


def _separar_entradas(lineas):
    marcadas = sum(1 for ln in lineas if MARCADOR_ENTRADA.match(ln))
    entradas = []

    if marcadas >= 2:
        actual = []
        for ln in lineas:
            if MARCADOR_ENTRADA.match(ln):
                if actual:
                    entradas.append(_limpiar(" ".join(actual)))
                actual = [MARCADOR_ENTRADA.sub("", ln, count=1)]
            elif ln.strip():
                actual.append(ln.strip())
        if actual:
            entradas.append(_limpiar(" ".join(actual)))
    else:
        bloque  = []
        bloques = []
        for ln in lineas:
            if ln.strip():
                bloque.append(ln.strip())
            elif bloque:
                bloques.append(_limpiar(" ".join(bloque)))
                bloque = []
        if bloque:
            bloques.append(_limpiar(" ".join(bloque)))

        bloques = [b for b in bloques if len(b) >= 8]

        for bloque_texto in bloques:
            if len(bloque_texto) > 500:
                entradas.extend(_separar_apa(bloque_texto))
            else:
                entradas.append(bloque_texto)

    return [e for e in entradas if len(e) >= 8]


def _separar_apa(texto):
    matches = list(_SEPARADOR_APA.finditer(texto))
    if not matches:
        return [texto]

    sub_entradas = []
    ultimo_fin   = 0
    for m in matches:
        sub = re.sub(r'\s+', ' ', texto[ultimo_fin:m.end()]).strip()
        if len(sub) >= 20:
            sub_entradas.append(sub)
        ultimo_fin = m.end()
    sub = re.sub(r'\s+', ' ', texto[ultimo_fin:]).strip()
    if len(sub) >= 20:
        sub_entradas.append(sub)

    return sub_entradas if sub_entradas else [texto]


def _limpiar(texto):
    texto = texto.replace("*", "").replace("`", "")
    return re.sub(r'\s+', ' ', texto).strip()


def _buscar_contexto(cuerpo, numero):
    """Busca la marca de cita [n] / [3,7,2] / (n) y devuelve prosa limpia.

    Cubre LaTeX $...$, $$...$$, \\(...\\) y \\[...\\] (estilos pandoc/MathJax).
    """
    if not cuerpo:
        return None

    patron = re.compile(
        r"[\[(][^)\]]{0,30}"
        rf"\b{re.escape(str(numero))}\b"
        r"[^)\]]{0,30}[\])]"
    )
    matches = list(patron.finditer(cuerpo))
    if not matches:
        return None

    interior_re = re.compile(rf"^\s*\[?\s*{re.escape(str(numero))}\s*[\])]?\s*$")
    exclusivos  = [m for m in matches if interior_re.match(m.group().strip("[]()").strip())]
    elegido     = exclusivos[0] if exclusivos else matches[0]
    pos         = elegido.start()

    todas_lineas = cuerpo.splitlines(keepends=True)
    acum         = 0
    linea_match  = 0
    for i, ln in enumerate(todas_lineas):
        if acum + len(ln) > pos:
            linea_match = i
            break
        acum += len(ln)

    inicio   = max(0, linea_match - 3)
    fin      = min(len(todas_lineas), linea_match + 4)
    fragmento = "".join(todas_lineas[inicio:fin])

    fragmento = _LATEX_RE.sub("", fragmento)
    fragmento = _LATEX_COLA_RE.sub("", fragmento)

    lineas_prosa = []
    for ln in fragmento.splitlines():
        if not ln.strip():
            continue
        if _RUIDO_LN_RE.match(ln):
            continue
        if _SOLO_ENTORNO_RE.match(ln):
            continue
        lineas_prosa.append(ln.strip())

    resultado = " ".join(lineas_prosa).strip()
    return resultado if len(resultado) >= 20 else None


def _fallback_dois(md):
    referencias = []
    vistos      = set()
    i           = 0
    for linea in md.splitlines():
        m = DOI_RE.search(linea)
        if not m:
            continue
        doi = _limpiar_doi(m.group(0))
        if not doi or doi in vistos:
            continue
        vistos.add(doi)
        i += 1
        referencias.append({
            "refId":    f"{i:04d}",
            "doi":      doi,
            "citaCruda": linea.strip()[:1000],
            "contexto": linea.strip()[:1500],
        })
    return referencias


def _registrar_total(manuscript_id, tema, total):
    estado = "COMPLETADO" if total == 0 else "PROCESANDO"
    expr = "SET tema = :t, totalRefs = :n, refsProcesadas = :z, refsRetractadas = :z, #estado = :e"
    vals = {":t": tema, ":n": total, ":z": 0, ":e": estado}

    if total == 0:
        expr += ", indiceIntegridad = :i"
        vals[":i"] = 100

    tabla.update_item(
        Key={"PK": f"MANUSCRIPT#{manuscript_id}", "SK": "METADATA"},
        UpdateExpression=expr,
        ExpressionAttributeNames={"#estado": "estado"},
        ExpressionAttributeValues=vals,
    )


def _guardar_refs_pendientes(manuscript_id, tema, referencias):
    with tabla.batch_writer() as bw:
        for r in referencias:
            item = {
                "PK":        f"MANUSCRIPT#{manuscript_id}",
                "SK":        f"REF#{r['refId']}",
                "tipo":      "referencia",
                "tema":      tema,
                "citaCruda": r["citaCruda"],
                "contexto":  r["contexto"],
                "estado":    "PENDIENTE",
            }
            if r["doi"]:
                item["doi"] = r["doi"]
            bw.put_item(Item=item)


def _enviar_a_cola(manuscript_id, tema, referencias):
    for i in range(0, len(referencias), 10):
        lote = referencias[i:i + 10]
        entries = [{
            "Id":          r["refId"],
            "MessageBody": json.dumps({
                "manuscriptId": manuscript_id,
                "tema":         tema,
                "refId":        r["refId"],
                "doi":          r["doi"],
                "citaCruda":    r["citaCruda"],
                "contexto":     r["contexto"],
            }),
        } for r in lote]
        sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=entries)
