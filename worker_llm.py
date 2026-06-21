import os
import re
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TABLE_NAME   = os.environ["TABLE_NAME"]
GROQ_MODEL   = os.environ["GROQ_MODEL"]
CONTACT_EMAIL = os.environ["CONTACT_EMAIL"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

tabla = boto3.resource("dynamodb").Table(TABLE_NAME)

# Las claves deben coincidir con los temas que produce classify_manuscript.py
# (en minúscula): medicina | biotecnologia | ingenieria | computacion | general.
PROMPTS_POR_TEMA = {
    "medicina":      "Eres un revisor experto en medicina y ciencias clinicas.",
    "biotecnologia": "Eres un revisor experto en biotecnologia y ciencias de la vida.",
    "ingenieria":    "Eres un revisor experto en ingenieria y ciencias aplicadas.",
    "computacion":   "Eres un revisor experto en informatica e inteligencia artificial.",
}
PROMPT_DEFECTO = "Eres un revisor experto en integridad cientifica."

INSTRUCCION = (
    " El articulo citado fue RETRACTADO. Analiza como lo usa el autor y responde "
    "UNICAMENTE con un JSON valido con esta forma: "
    '{"veredicto": "ignora_retraccion" | "cita_como_error" | "incierto", '
    '"justificacion": "<una frase breve>", "confianza": <numero entre 0 y 1>}. '
    'Usa "ignora_retraccion" si el autor lo presenta como evidencia valida sin notar '
    'que fue retractado; usa "cita_como_error" si lo menciona justamente como ejemplo '
    "de error, fraude o caso retractado."
)

# Stopwords para el chequeo de concordancia titulo<->cita.
# Palabras tan genéricas que su presencia/ausencia no dice nada del match.
_STOP_MATCH = {
    # inglés común
    'that','with','from','this','have','been','were','their','they','which',
    'when','also','more','into','over','after','than','some','these','then',
    'such','only','other','most','both','very','about','will','make','made',
    # términos científicos genéricos (aparecen en miles de títulos)
    'language','model','models','learning','neural','network','networks',
    'deep','large','using','based','study','analysis','approach','method',
    'methods','towards','toward','system','systems','data','results','human',
    'training','trained','scale','efficient','evaluation','performance',
    # venue / publicación
    'conference','proceedings','international','journal','review','workshop',
    'symposium','annual','advances','transactions','letters','bulletin',
    'ieee','acm','arxiv','preprint','technical','report',
}


def lambda_handler(event, context):
    fallidas = []
    for record in event["Records"]:
        try:
            _procesar(json.loads(record["body"]))
        except Exception as e:
            print(f"ERROR en mensaje {record['messageId']}: {e}")
            fallidas.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": fallidas}


def _procesar(msg):
    manuscript_id = msg["manuscriptId"]
    ref_id        = msg["refId"]
    tema          = msg.get("tema", "General")
    doi           = msg.get("doi")
    cita          = msg.get("citaCruda", "")

    metodo = "doi_directo"
    if not doi and cita:
        doi    = _buscar_doi_crossref(cita)
        metodo = "match_bibliografico" if doi else "sin_match"

    retraccion = _consultar_crossref(doi)
    retraccion["metodo"] = metodo
    if doi:
        retraccion["doiResuelto"] = doi

    veredicto = None
    if retraccion.get("retractada"):
        estado    = "RETRACTADA"
        veredicto = _juzgar_con_groq(tema, cita, msg.get("contexto", ""))
    elif not retraccion.get("verificada"):
        estado = "NO_VERIFICADA"
    else:
        estado = "OK"

    primera_vez = _guardar_resultado(manuscript_id, ref_id, estado, retraccion, veredicto)
    if primera_vez:
        _sumar_procesada(manuscript_id, bool(retraccion.get("retractada")))


def _consultar_crossref(doi):
    """Devuelve si el DOI esta retractado segun Crossref."""
    if not doi:
        return {"verificada": False}

    url = ("https://api.crossref.org/works/"
           + urllib.parse.quote(doi, safe="")
           + "?mailto=" + urllib.parse.quote(CONTACT_EMAIL))
    req = urllib.request.Request(
        url, headers={"User-Agent": f"CentinelaIntegridad/1.0 (mailto:{CONTACT_EMAIL})"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"verificada": False}
        cuerpo = e.read().decode("utf-8", "ignore")[:300]
        print(f"[Crossref] HTTP {e.code} para DOI {doi}: {cuerpo}")
        raise

    actualizaciones = data.get("message", {}).get("updated-by", []) or []
    retracciones   = [u for u in actualizaciones if u.get("type") == "retraction"]
    preocupaciones = [u for u in actualizaciones if "concern" in (u.get("type") or "")]
    return {
        "verificada":            True,
        "retractada":            len(retracciones) > 0,
        "expresionPreocupacion": len(preocupaciones) > 0,
    }


def _buscar_doi_crossref(cita):
    """Resuelve el DOI de una cita sin DOI usando busqueda bibliografica de Crossref.

    Criterio de aceptacion (en orden):
      1. Si el score de Crossref >= 85 → aceptar directamente (Crossref muy seguro).
      2. Si 50 <= score < 85 → chequeo de concordancia titulo<->cita (umbral 0.25).
      3. Si score < 50 → descartar (demasiado ruido).

    Usar el score de Crossref como señal primaria es más confiable que solo comparar
    palabras del título, porque Crossref también considera autores, año, venue, etc.
    """
    if not cita:
        return None

    url = ("https://api.crossref.org/works?rows=1"
           "&select=DOI,title,score"
           "&query.bibliographic=" + urllib.parse.quote(cita[:300])
           + "&mailto=" + urllib.parse.quote(CONTACT_EMAIL))
    req = urllib.request.Request(
        url, headers={"User-Agent": f"CentinelaIntegridad/1.0 (mailto:{CONTACT_EMAIL})"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        cuerpo = e.read().decode("utf-8", "ignore")[:300]
        print(f"[Crossref-bib] HTTP {e.code}: {cuerpo}")
        raise

    items = (data.get("message", {}).get("items") or [])
    if not items:
        return None

    item          = items[0]
    doi           = item.get("DOI")
    crossref_score = float(item.get("score") or 0)

    if not doi:
        return None

    # Score alto: Crossref está seguro, confiar en él.
    if crossref_score >= 85:
        return doi

    # Score muy bajo: demasiado ruido, descartar.
    if crossref_score < 50:
        print(f"[match] descartado por score bajo: {doi} (crossref_score={crossref_score:.0f})")
        return None

    # Score medio (50-84): chequeo adicional de concordancia por titulo.
    titulos = item.get("title") or []
    titulo  = titulos[0] if titulos else ""
    t_words = [w for w in re.findall(r"\w+", titulo.lower())
               if len(w) > 3 and w not in _STOP_MATCH]

    # Si el titulo no tiene palabras distintivas tras filtrar → aceptar
    # (no podemos juzgar, y el score ya es razonable).
    if len(t_words) <= 2:
        return doi

    cita_low = cita.lower()
    hits     = sum(1 for w in t_words if w in cita_low)
    ratio    = hits / len(t_words)

    if ratio < 0.25:
        print(f"[match] descartado por baja coincidencia: {doi} "
              f"({hits}/{len(t_words)}, crossref_score={crossref_score:.0f})")
        return None

    return doi


def _juzgar_con_groq(tema, cita, contexto):
    sistema = PROMPTS_POR_TEMA.get(tema, PROMPT_DEFECTO) + INSTRUCCION
    usuario = (f"Entrada bibliografica:\n{cita}\n\n"
               f"Contexto donde aparece la cita:\n{contexto}")
    cuerpo = json.dumps({
        "model":           GROQ_MODEL,
        "temperature":     0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": sistema},
            {"role": "user",   "content": usuario},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        GROQ_URL, data=cuerpo, method="POST",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type":  "application/json",
            "User-Agent":    "CentinelaIntegridad/1.0",
        },
    )
    for intento in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data     = json.loads(resp.read().decode("utf-8"))
                contenido = data["choices"][0]["message"]["content"]
                return json.loads(contenido, parse_float=Decimal)
        except urllib.error.HTTPError as e:
            if e.code == 429 and intento < 2:
                time.sleep(2 ** intento)
                continue
            cuerpo_err = e.read().decode("utf-8", "ignore")[:300]
            print(f"[Groq] HTTP {e.code}: {cuerpo_err}")
            raise


def _guardar_resultado(manuscript_id, ref_id, estado, retraccion, veredicto):
    expr = "SET #estado = :e, estaRetractada = :r, verificada = :v"
    vals = {
        ":e":        estado,
        ":r":        bool(retraccion.get("retractada")),
        ":v":        bool(retraccion.get("verificada")),
        ":pendiente": "PENDIENTE",
    }
    if veredicto is not None:
        expr += ", veredictoLLM = :vd"
        vals[":vd"] = veredicto
    if metodo := retraccion.get("metodo"):
        expr += ", metodoVerificacion = :m"
        vals[":m"] = metodo
    if doi_res := retraccion.get("doiResuelto"):
        expr += ", doiVerificado = :dv"
        vals[":dv"] = doi_res
    try:
        tabla.update_item(
            Key={"PK": f"MANUSCRIPT#{manuscript_id}", "SK": f"REF#{ref_id}"},
            UpdateExpression=expr,
            ConditionExpression="#estado = :pendiente",
            ExpressionAttributeNames={"#estado": "estado"},
            ExpressionAttributeValues=vals,
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            print(f"[idempotencia] REF#{ref_id} ya estaba procesada; no se recuenta")
            return False
        raise


def _sumar_procesada(manuscript_id, retractada):
    resp = tabla.update_item(
        Key={"PK": f"MANUSCRIPT#{manuscript_id}", "SK": "METADATA"},
        UpdateExpression="ADD refsProcesadas :uno, refsRetractadas :r",
        ExpressionAttributeValues={":uno": 1, ":r": (1 if retractada else 0)},
        ReturnValues="ALL_NEW",
    )
    item      = resp["Attributes"]
    total     = int(item.get("totalRefs", 0))
    procesadas = int(item.get("refsProcesadas", 0))
    if total and procesadas >= total:
        _cerrar_manuscrito(manuscript_id, total, int(item.get("refsRetractadas", 0)))


def _cerrar_manuscrito(manuscript_id, total, retractadas):
    indice = int(round((1 - retractadas / total) * 100)) if total else 0
    tabla.update_item(
        Key={"PK": f"MANUSCRIPT#{manuscript_id}", "SK": "METADATA"},
        UpdateExpression="SET #estado = :c, indiceIntegridad = :i",
        ExpressionAttributeNames={"#estado": "estado"},
        ExpressionAttributeValues={":c": "COMPLETADO", ":i": indice},
    )
    print(f"[cierre] {manuscript_id} COMPLETADO. Indice de Integridad: {indice} "
          f"({retractadas}/{total} retractadas)")
