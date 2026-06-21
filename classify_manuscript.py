import os
import re
import json
import boto3
from urllib.parse import unquote_plus
 
s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
 
PROCESSED_BUCKET = os.environ.get("PROCESSED_BUCKET_NAME", "centinela-processed-manuscripts-dev")
TABLE_NAME = os.environ.get("TABLE_NAME", "centinela-integridad")
 
# ---------------------------------------------------------------------------
# Diccionario Bilingüe de keywords por tema.
# Soporta documentos en español e inglés.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Diccionario Bilingüe de keywords por tema.
# Depurado para evitar colisiones (falsos positivos) entre ingenierías.
# ---------------------------------------------------------------------------
TOPIC_KEYWORDS = {
    "medicina": [
        # Español
        "paciente", "clínico", "clínica", "tratamiento", "diagnóstico",
        "fármaco", "medicamento", "ensayo clínico", "vacuna", "terapia",
        "síntoma", "patología", "salud pública", "epidemiología",
        "hospital", "cirugía", "enfermedad", "dosis", "biomédico",
        # Inglés
        "patient", "clinical", "treatment", "diagnosis", "drug",
        "medicine", "vaccine", "therapy", "symptom", "pathology",
        "public health", "epidemiology", "surgery", "disease", "dose",
        "biomedical"
    ],
    "biotecnologia": [
        # Español
        "adn", "rna", "gen", "genoma", "genómica", "crispr",
        "celular", "proteína", "bioreactor", "cultivo celular",
        "biotecnología", "secuenciación", "transgénico", "enzima",
        "microorganismo", "plásmido", "vector viral", "biología molecular",
        # Inglés
        "dna", "gene", "genome", "genomics", "cellular", "protein",
        "cell culture", "biotechnology", "sequencing", "transgenic",
        "enzyme", "microorganism", "plasmid", "viral vector", 
        "molecular biology"
    ],
    "ingenieria": [
        # Español (Términos estrictamente físicos/industriales)
        "circuito", "mecatrónica", "ingeniería", "diseño estructural",
        "simulación", "control", "optimización", "material", "termodinámica",
        "mecánico", "eléctrico", "civil", "infraestructura", "industrial",
        # Inglés
        "circuit", "mechatronics", "engineering", "structural design",
        "simulation", "optimization", "mechanical", "electrical", "thermodynamics",
        "infrastructure", "industrial", "civil engineering"
    ],
    "computacion": [
        # Español
        "algoritmo", "software", "hardware", "ciencias de la computación", 
        "estructura de datos", "autómata", "inteligencia artificial", 
        "aprendizaje automático", "redes neuronales", "base de datos", 
        "ciberseguridad", "programación", "computación en la nube", 
        "visión por computadora", "segmentación de instancias", "ocr", 
        "procesamiento de imágenes", "estimación de pose",
        # Inglés
        "algorithm", "software", "hardware", "computer science", 
        "data structure", "automaton", "automata", "artificial intelligence", 
        "machine learning", "neural network", "database", 
        "cybersecurity", "programming", "cloud computing", "computer vision",
        "instance segmentation", "ocr", "bounding box", "keypoint", "pose estimation", 
        "deep learning", "trie", "suffix tree", "pointer", "string matching"
    ]
}
 
DEFAULT_TOPIC = "general"
 
 
def detectar_tema(texto_md: str) -> dict:
    """
    Cuenta ocurrencias (case-insensitive, palabra completa cuando aplica)
    de las keywords de cada tema y devuelve el tema con mayor score.
 
    Retorna un dict con el tema ganador y el detalle de scores, útil para
    logging/debug.
    """
    texto = texto_md.lower()
    scores = {}
 
    for tema, keywords in TOPIC_KEYWORDS.items():
        total = 0
        for kw in keywords:
            # \b no funciona bien con tildes/espacios en frases, así que
            # usamos conteo simple de substring para frases compuestas
            # y boundary regex solo para palabras simples de una token.
            if " " in kw:
                total += texto.count(kw)
            else:
                total += len(re.findall(r"\b" + re.escape(kw) + r"\b", texto))
        scores[tema] = total
 
    tema_ganador = max(scores, key=scores.get)
 
    # Si nadie superó un mínimo de señales, lo mandamos a "general"
    # en vez de forzar una categoría con score 0 o muy débil.
    if scores[tema_ganador] < 3:
        tema_ganador = DEFAULT_TOPIC
 
    return {"topic": tema_ganador, "scores": scores}
 
 
def actualizar_dynamo(manuscript_id: str, topic: str):
    table = dynamodb.Table(TABLE_NAME)
    table.update_item(
        Key={
            "PK": f"MANUSCRIPT#{manuscript_id}", 
            "SK": "METADATA"
        },
        UpdateExpression="SET Topic = :t",
        ExpressionAttributeValues={":t": topic},
    )
 
 
def lambda_handler(event, context):
    resultados = []
 
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
 
        # Descargar el markdown
        obj = s3.get_object(Bucket=bucket, Key=key)
        texto_md = obj["Body"].read().decode("utf-8")
 
        # Detectar tema
        deteccion = detectar_tema(texto_md)
        topic = deteccion["topic"]
 
        # manuscriptId se asume como el nombre base del archivo, ej:
        # "550e8400-e29b-41d4-a716-446655440000.md"
        # Ajustar este parsing si el equipo del Extractor usa otra convención.
        manuscript_id = os.path.splitext(os.path.basename(key))[0]
 
        # Copiar a PROCESSED organizado por carpeta de tema
        nuevo_key = f"{topic}/{os.path.basename(key)}"
        s3.copy_object(
            Bucket=PROCESSED_BUCKET,
            CopySource={"Bucket": bucket, "Key": key},
            Key=nuevo_key,
        )
 
        # Registrar el tema en DynamoDB para que el Worker LLM lo use
        try:
            actualizar_dynamo(manuscript_id, topic)
        except Exception as e:
            # No tumbamos la Lambda si Dynamo falla; lo dejamos en logs
            # para no perder el resultado de clasificación ya calculado.
            print(f"[WARN] No se pudo actualizar DynamoDB para {manuscript_id}: {e}")
 
        print(f"[OK] {key} -> tema='{topic}' scores={deteccion['scores']}")
 
        resultados.append({
            "manuscriptId": manuscript_id,
            "topic": topic,
            "processedKey": nuevo_key,
            "scores": deteccion["scores"],
        })
 
    return {
        "statusCode": 200,
        "body": json.dumps(resultados, ensure_ascii=False),
    }
