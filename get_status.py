import os
import json
import boto3
from decimal import Decimal

TABLE_NAME = os.environ.get("TABLE_NAME", "centinela-integridad")
dynamodb = boto3.resource("dynamodb")
tabla = dynamodb.Table(TABLE_NAME)

# Mapeo del estado interno (español) al estado público del contrato de API.
# El frontend espera: PENDING | PROCESSING | COMPLETED | ERROR.
ESTADO_A_STATUS = {
    "PENDIENTE":  "PENDING",
    "PROCESANDO": "PROCESSING",
    "COMPLETADO": "COMPLETED",
    "ERROR":      "ERROR",
}


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


def _to_contract(manuscript_id, item):
    """Traduce el item METADATA de DynamoDB al contrato público (camelCase, inglés).

    Forma esperada por el frontend (ver API_CONTRACT.md / openapi.yml):
      { manuscriptId, fileName, status, progress:{totalBatches,processedBatches},
        globalIntegrityIndex, topic }
    """
    estado = item.get("estado", "PENDIENTE")
    status = ESTADO_A_STATUS.get(estado, "PROCESSING")

    total = int(item.get("totalRefs", 0) or 0)
    procesadas = int(item.get("refsProcesadas", 0) or 0)

    # indiceIntegridad solo existe cuando el manuscrito está cerrado.
    indice = item.get("indiceIntegridad")
    indice = int(indice) if indice is not None else None

    # El tema lo escribe el Extractor ("tema"); el Clasificador deja "Topic".
    topic = item.get("tema") or item.get("Topic")

    return {
        "manuscriptId": manuscript_id,
        "fileName": item.get("fileName", ""),
        "status": status,
        "progress": {
            "totalBatches": total,
            "processedBatches": procesadas,
        },
        "globalIntegrityIndex": indice,
        "topic": topic,
    }


def lambda_handler(event, context):
    manuscript_id = event["pathParameters"]["id"]

    try:
        response = tabla.get_item(
            Key={"PK": f"MANUSCRIPT#{manuscript_id}", "SK": "METADATA"}
        )
        item = response.get("Item")

        if not item:
            return {
                "statusCode": 404,
                "headers": _cors_headers(),
                "body": json.dumps({"error": "Manuscrito no encontrado"})
            }

        return {
            "statusCode": 200,
            "headers": _cors_headers(),
            "body": json.dumps(_to_contract(manuscript_id, item), cls=DecimalEncoder)
        }
    except Exception as e:
        print(f"[ERROR] get_status: {e}")
        return {
            "statusCode": 500,
            "headers": _cors_headers(),
            "body": json.dumps({"error": "Error interno del servidor"})
        }
