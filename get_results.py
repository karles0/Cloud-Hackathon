import os
import json
import boto3
from boto3.dynamodb.conditions import Key
from decimal import Decimal

TABLE_NAME = os.environ.get("TABLE_NAME", "centinela-integridad")
dynamodb = boto3.resource("dynamodb")
tabla = dynamodb.Table(TABLE_NAME)


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj) if obj % 1 else int(obj)
        return super(DecimalEncoder, self).default(obj)


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Credentials": True,
    }


def _construir_contexto(item):
    """Genera el texto de análisis legible (analysisContext) a partir del
    veredicto del LLM y el estado de verificación de la referencia."""
    estado = item.get("estado")
    retractada = bool(item.get("estaRetractada"))

    if retractada:
        veredicto_llm = item.get("veredictoLLM") or {}
        veredicto = veredicto_llm.get("veredicto")
        justificacion = (veredicto_llm.get("justificacion") or "").strip()

        if veredicto == "ignora_retraccion":
            base = ("El artículo citado fue RETRACTADO y el autor lo presenta como "
                    "evidencia válida, sin advertir la retractación.")
        elif veredicto == "cita_como_error":
            base = ("El artículo citado fue retractado; el autor lo menciona "
                    "explícitamente como ejemplo de error o caso retractado.")
        else:
            base = "El artículo citado fue RETRACTADO."

        return f"{base} {justificacion}".strip() if justificacion else base

    if estado == "OK":
        return "Cita válida y vigente. No hay registros de retractación asociados."
    if estado == "NO_VERIFICADA":
        return "No se pudo verificar la referencia (sin DOI resoluble en Crossref)."
    if estado == "PENDIENTE":
        return "Análisis en curso."

    # Fallback: contexto crudo extraído del manuscrito.
    return (item.get("contexto") or "").strip()


def _to_contract(item):
    """Traduce un item REF# de DynamoDB a un EvaluationResult del contrato."""
    ref_id = item.get("SK", "").replace("REF#", "")
    return {
        "referenceId": ref_id,
        "citationText": item.get("citaCruda", ""),
        "isZombie": bool(item.get("estaRetractada")),
        "analysisContext": _construir_contexto(item),
    }


def lambda_handler(event, context):
    manuscript_id = event["pathParameters"]["id"]

    try:
        response = tabla.query(
            KeyConditionExpression=Key("PK").eq(f"MANUSCRIPT#{manuscript_id}") & Key("SK").begins_with("REF#")
        )
        items = sorted(response.get("Items", []), key=lambda it: it.get("SK", ""))

        results = [_to_contract(it) for it in items]
        zombie_count = sum(1 for r in results if r["isZombie"])

        payload = {
            "manuscriptId": manuscript_id,
            "totalEvaluated": len(results),
            "zombieCount": zombie_count,
            "results": results,
        }

        return {
            "statusCode": 200,
            "headers": _cors_headers(),
            "body": json.dumps(payload, cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"[ERROR] get_results: {e}")
        return {
            "statusCode": 500,
            "headers": _cors_headers(),
            "body": json.dumps({"error": "Error interno del servidor"})
        }
